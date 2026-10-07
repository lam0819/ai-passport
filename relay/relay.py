#!/usr/bin/env python3
"""
AI Passport relay.

Accepts a raw 16 kHz mono WAV on POST /ask (the FoloToy AI Passport streams it
while the button is held) and answers with a stream of frames that carry the
reply text and the spoken reply:

  [1 type byte][4 length bytes, little-endian][payload]
  0x00 END    no payload; the reply is complete
  0x01 TEXT   UTF-8 delta appended to the reply text (the caption)
  0x02 AUDIO  PCM16 little-endian, 16 kHz mono (played on the speaker)

Default mode: one call to RELAY_AUDIO_MODEL (openai/gpt-audio-mini), which
hears the WAV and replies with speech plus its transcript. Speech is resampled
to 16 kHz for the device.

Text mode (RELAY_AUDIO_MODEL empty): the WAV goes to RELAY_MODEL (or
RELAY_STT_MODEL + RELAY_CHAT_MODEL for two stages) and only text is returned.
Set RELAY_TTS_MODEL to add spoken replies on top (text first, then speech).

Environment:
  RELAY_API_KEY       API key (falls back to OPENAI_API_KEY, then
                      OPENROUTER_API_KEY)
  RELAY_BASE_URL      OpenAI-compatible base URL, default OpenRouter
                      (https://openrouter.ai/api/v1). For OpenAI direct:
                      https://api.openai.com/v1
  RELAY_SMART         "1": answer with web search (transcribe -> browsable
                      chat model -> speech); slower but answers real questions
  RELAY_SMART_MODEL   chat model for smart mode (default probes
                      gpt-4o-search-preview, gpt-5-search, gpt-4o-mini)
  RELAY_SMART_STT_MODEL   transcription model for smart mode
  RELAY_SMART_TTS_MODEL   speech model for smart mode (default gpt-4o-mini-tts)
  RELAY_TTS_SPEED     speech rate, default 1.12 (0.25-4.0)
  RELAY_STT_LANGUAGE  optional ISO code passed to transcription (e.g. zh)
  RELAY_TOKEN         shared secret the device sends as Authorization: Bearer
  RELAY_PORT          listen port, default 8787
  RELAY_AUDIO_MODEL   single-call audio chat model. Default
                      openai/gpt-audio-mini (OpenRouter); for OpenAI direct use
                      gpt-4o-mini-audio-preview. Empty: use the text path below
  RELAY_TTS_MODEL     text-to-speech model for the text path (e.g.
                      openai/gpt-audio-mini or gpt-4o-mini-audio-preview);
                      empty: text only
  RELAY_TTS_VOICE     voice name, default alloy
  RELAY_MODEL         text-path single-call model, default google/gemini-2.5-flash
  RELAY_STT_MODEL     two-stage transcription model
  RELAY_CHAT_MODEL    two-stage chat model; non-empty enables two-stage
  RELAY_LANGUAGE      force a reply language, e.g. "Cantonese (Hong Kong,
                      Traditional Chinese)". Empty: mirror what the user spoke
  RELAY_SYSTEM        system prompt override (replaces everything above)

Test without the device:
  curl -s http://localhost:8787/                      # health page
  curl -s -X POST --data-binary @voice.wav \
       -H "Authorization: Bearer $RELAY_TOKEN" \
       -H "Content-Type: audio/wav" -o out.bin http://localhost:8787/ask
  # out.bin then holds the frames above (xxd | head)
"""
import base64
import json
import os
import re
import struct
import sys
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    import audioop  # removed in Python 3.13; optional
except ImportError:  # pragma: no cover
    audioop = None

PORT = int(os.environ.get("RELAY_PORT", "8787"))
BASE_URL = os.environ.get("RELAY_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")
API_KEY = (os.environ.get("RELAY_API_KEY")
           or os.environ.get("OPENAI_API_KEY")
           or os.environ.get("OPENROUTER_API_KEY", ""))
TOKEN = os.environ.get("RELAY_TOKEN", "")
AUDIO_MODEL = os.environ.get("RELAY_AUDIO_MODEL", "openai/gpt-audio-mini")
TTS_MODEL = os.environ.get("RELAY_TTS_MODEL", "")
VOICE = os.environ.get("RELAY_TTS_VOICE", "alloy")
MODEL = os.environ.get("RELAY_MODEL", "google/gemini-2.5-flash")
STT_MODEL = os.environ.get("RELAY_STT_MODEL", "google/gemini-2.5-flash")
CHAT_MODEL = os.environ.get("RELAY_CHAT_MODEL", "")
CHAT_URL = f"{BASE_URL}/chat/completions"

SMART = os.environ.get("RELAY_SMART", "").lower() not in ("", "0", "false", "no")
SMART_MODEL = os.environ.get("RELAY_SMART_MODEL", "")
SMART_STT_MODEL = os.environ.get("RELAY_SMART_STT_MODEL", "")
SMART_TTS_MODEL = os.environ.get("RELAY_SMART_TTS_MODEL", "")
STT_LANGUAGE = os.environ.get("RELAY_STT_LANGUAGE", "")
try:
    TTS_SPEED = float(os.environ.get("RELAY_TTS_SPEED", "1.12"))
except ValueError:
    TTS_SPEED = 1.12
TTS_SPEED = min(4.0, max(0.25, TTS_SPEED))

# First hit wins; remembered so later requests skip the misses.
_smart_pick = {"stt": "", "llm": "", "tts": ""}
# Debug: GET /last shows what the most recent request heard and used.
_last = {}
MAX_BODY = 4 * 1024 * 1024
OUT_RATE = 16000

FRAME_END, FRAME_TEXT, FRAME_AUDIO = 0, 1, 2

LANGUAGE = os.environ.get("RELAY_LANGUAGE", "").strip()
_base_system = (
    "You are the assistant inside a small handheld voice device. The user "
    "speaks a voice message; answer it directly. Reply in the same language "
    "AND dialect the user spoke, in the matching script: if they speak "
    "Cantonese, answer in colloquial spoken Cantonese (Traditional Chinese, "
    "Hong Kong); if they speak Mandarin, answer in Mandarin (Simplified "
    "Chinese); otherwise use their language. Never switch languages. Keep "
    "replies to one or two short sentences (about 50 characters), plain "
    "speech, no markdown."
)
if LANGUAGE:
    _base_system += f" Always reply in {LANGUAGE}."
SYSTEM = os.environ.get("RELAY_SYSTEM") or _base_system
TRANSCRIBE_PROMPT = (
    "Transcribe this speech exactly. Output only the transcription, in its "
    "original language, with no commentary."
)
TTS_PROMPT = (
    "You are a text-to-speech engine. Read the text below aloud exactly as "
    "written, with natural intonation. Do not add, remove, or comment."
)


def frame(fr_type, payload=b""):
    return bytes((fr_type,)) + struct.pack("<I", len(payload)) + payload


def openrouter(payload, stream=False):
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    }
    if "openrouter" in BASE_URL:
        headers["X-Title"] = "AI Passport Relay"
    req = urllib.request.Request(
        CHAT_URL,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers=headers,
    )
    return urllib.request.urlopen(req, timeout=300)


def iter_events(resp):
    """Yields SSE data payloads (bytes) from a streaming response."""
    buf = b""
    while True:
        chunk = resp.read(4096)
        if not chunk:
            break
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            line = line.strip()
            if line.startswith(b"data:"):
                yield line[5:].strip()


def message_text(data):
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return ""


def audio_part(wav_b64):
    return {"type": "input_audio", "input_audio": {"data": wav_b64, "format": "wav"}}


class Resampler:
    """Stateful s16le mono resampling to OUT_RATE.

    audioop when it exists; otherwise a pure-Python linear interpolator, so
    the pitch stays right on Pythons without it (3.13+)."""

    def __init__(self, rate):
        self.rate = rate
        self.state = None
        self.pos = 0.0

    def feed(self, pcm):
        if not pcm or self.rate == OUT_RATE:
            return pcm
        if audioop is not None:
            out, self.state = audioop.ratecv(pcm, 2, 1, self.rate, OUT_RATE, self.state)
            return out
        n = len(pcm) // 2
        if n == 0:
            return b""
        samples = struct.unpack("<%dh" % n, pcm[: n * 2])
        ratio = self.rate / OUT_RATE
        out = bytearray()
        pos = self.pos
        while True:
            i = int(pos)
            if i + 1 >= n:
                break
            frac = pos - i
            v = int(samples[i] * (1.0 - frac) + samples[i + 1] * frac)
            out += struct.pack("<h", v)
            pos += ratio
        self.pos = pos - n
        return bytes(out)


class WavStrip:
    """Strips a RIFF header from a streamed wav, remembering its rate."""

    def __init__(self):
        self.buf = b""
        self.rate = None
        self.ready = False

    def feed(self, data):
        if self.ready:
            return data
        self.buf += data
        fmt = self.buf.find(b"fmt ")
        data_at = self.buf.find(b"data")
        if fmt >= 0 and data_at >= 0 and len(self.buf) >= data_at + 8:
            try:
                self.rate = struct.unpack("<I", self.buf[fmt + 8:fmt + 12])[0]
            except struct.error:
                self.rate = None
            rest = self.buf[data_at + 8:]
            self.buf = b""
            self.ready = True
            return rest
        return b""


def open_stream_with_format(payload):
    """Tries pcm16 first, then wav; returns (response, used_format)."""
    for fmt in ("pcm16", "wav"):
        payload["audio"]["format"] = fmt
        try:
            return openrouter(payload, stream=True), fmt
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "ignore")[:200]
            if fmt == "pcm16":
                print(f"audio format pcm16 rejected ({e.code}): {body}", file=sys.stderr)
                continue
            raise
    raise RuntimeError("no audio format accepted")


def run_audio_chat(wav_b64, begin, send):
    """One audio-in/audio-out call (RELAY_AUDIO_MODEL)."""
    payload = {
        "model": AUDIO_MODEL,
        "stream": True,
        "modalities": ["text", "audio"],
        "audio": {"voice": VOICE, "format": "pcm16"},
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": [
                {"type": "text", "text": "The voice message:"},
                audio_part(wav_b64),
            ]},
        ],
    }
    resp, fmt = open_stream_with_format(payload)
    begin()
    strip = WavStrip() if fmt == "wav" else None
    conv = None

    def on_text(t):
        send(frame(FRAME_TEXT, t.encode("utf-8", "ignore")))

    def on_audio(raw):
        nonlocal strip, conv
        if strip is not None:
            raw = strip.feed(raw)
            if not raw:
                return
        if conv is None:
            conv = Resampler(strip.rate if strip and strip.rate else 24000)
        out = conv.feed(raw)
        if out:
            send(frame(FRAME_AUDIO, out))

    try:
        for event in iter_events(resp):
            if event == b"[DONE]":
                break
            try:
                obj = json.loads(event)
            except ValueError:
                continue
            choices = obj.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta") or {}
            audio = delta.get("audio") or {}
            if audio.get("transcript"):
                on_text(audio["transcript"])
            elif delta.get("content"):
                on_text(delta["content"])
            if audio.get("data"):
                on_audio(base64.b64decode(audio["data"]))
    finally:
        resp.close()
    send(frame(FRAME_END))


def api_post(path, data, content_type, timeout=300):
    req = urllib.request.Request(
        f"{BASE_URL}{path}",
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": content_type,
        },
    )
    return urllib.request.urlopen(req, timeout=timeout)


def transcribe(wav):
    """/v1/audio/transcriptions; walks candidate models on model errors."""
    candidates = [m for m in [SMART_STT_MODEL, _smart_pick["stt"],
                              "gpt-4o-transcribe", "gpt-4o-mini-transcribe",
                              "whisper-1"] if m]
    last = None
    for model in candidates:
        boundary = "----aipassportrelay"
        head = []
        head.append((f"--{boundary}\r\nContent-Disposition: form-data; name=\"model\""
                     f"\r\n\r\n{model}\r\n").encode())
        if STT_LANGUAGE:
            head.append((f"--{boundary}\r\nContent-Disposition: form-data; name=\"language\""
                         f"\r\n\r\n{STT_LANGUAGE}\r\n").encode())
        head.append((f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\";"
                     f" filename=\"voice.wav\"\r\nContent-Type: audio/wav\r\n\r\n").encode())
        tail = f"\r\n--{boundary}--\r\n".encode()
        body = b"".join(head) + wav + tail
        try:
            resp = api_post("/audio/transcriptions", body,
                            f"multipart/form-data; boundary={boundary}")
            data = json.load(resp)
            _smart_pick["stt"] = model
            return (data.get("text") or "").strip()
        except urllib.error.HTTPError as e:
            last = e
            print(f"stt {model}: {e.code}", file=sys.stderr)
    raise last if last else RuntimeError("no transcription model")


def clean_text(s):
    """Drops the search models' source links; the caption and the voice only
    want the sentence."""
    s = re.sub(r"\[([^\]]*)\]\([^)]*\)", "", s)
    s = re.sub(r"https?://\S+", "", s)
    s = s.replace("*", "")
    s = re.sub(r"[（(]\s*[)）]", "", s)   # the empty pairs the links left behind
    s = re.sub(r"[ \t]+", " ", s)
    return s.strip()


def chat_stream(text):
    """Answers text, walking candidate models; returns the finished text.

    The search models take their time before the first token (the search runs
    first), so the text is delivered as one piece rather than streamed; the
    links they add are stripped before anyone sees them."""
    candidates = [m for m in [SMART_MODEL, _smart_pick["llm"],
                              "gpt-5-search-api", "gpt-4o-search-preview",
                              "gpt-5-search", "gpt-4o-mini-search-preview",
                              "gpt-4o-mini", AUDIO_MODEL] if m]
    messages = [
        {"role": "system", "content": SYSTEM + (
            " Use web search when the question needs current facts (prices, "
            "news, weather, sports, schedules). Never include links, URLs, or "
            "source lists.")},
        {"role": "user", "content": text},
    ]
    last = None
    for model in candidates:
        payload = {"model": model, "stream": True, "messages": messages}
        if "search" in model:
            payload["web_search_options"] = {}
        resp = None
        for attempt in (0, 1):
            try:
                resp = openrouter(payload, stream=True)
                break
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "ignore")[:160]
                print(f"chat {model}: {e.code} {detail}", file=sys.stderr)
                last = e
                if attempt == 0 and "web_search_options" in payload:
                    payload.pop("web_search_options")
                    continue
                break
            except Exception as e:  # noqa: BLE001
                print(f"chat {model}: {e!r}", file=sys.stderr)
                last = e
                break
        if resp is None:
            continue
        t0 = time.time()
        full = []
        cites = 0
        try:
            for event in iter_events(resp):
                if event == b"[DONE]":
                    break
                try:
                    obj = json.loads(event)
                except ValueError:
                    continue
                choices = obj.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                piece = delta.get("content")
                if piece:
                    full.append(piece)
                for _ in delta.get("annotations") or []:
                    cites += 1
        finally:
            resp.close()
        answer = clean_text("".join(full))
        took = time.time() - t0
        print(f"chat {model} ok {len(answer)} chars {took:.1f}s cites={cites}",
              file=sys.stderr)
        _last.update({"llm": model, "chat_s": round(took, 1), "cites": cites})
        if cites or "search" in model:
            _smart_pick["llm"] = model
        return answer
    raise last if last else RuntimeError("no chat model")


def tts_speech(text, send):
    """Speech via /v1/audio/speech, with a look-ahead cushion.

    The endpoint streams at close to real time. Forwarding each chunk as it
    arrives leaves the device playing from a nearly empty buffer, and jitter
    becomes chopped speech; waiting for the whole reply delays the voice too
    long. Keeping ~2 s in hand before forwarding starts gives the device's
    buffers something to ride on while the voice begins promptly."""
    candidates = [m for m in [SMART_TTS_MODEL, _smart_pick["tts"],
                              "gpt-4o-mini-tts", "tts-1"] if m]
    for model in candidates:
        payload = {
            "model": model,
            "input": text[:2400],
            "voice": VOICE,
            "response_format": "pcm",
        }
        if TTS_SPEED != 1.0:
            payload["speed"] = TTS_SPEED
        try:
            resp = api_post("/audio/speech", json.dumps(payload).encode(),
                            "application/json")
        except urllib.error.HTTPError as e:
            print(f"tts {model}: {e.code}", file=sys.stderr)
            continue
        _smart_pick["tts"] = model
        conv = Resampler(24000)
        lookahead = 2 * OUT_RATE * 2      # ~2 s of s16le
        buf = bytearray()
        total = 0
        first = None
        t0 = time.time()
        try:
            while True:
                chunk = resp.read(8192)
                if not chunk:
                    break
                out = conv.feed(chunk)
                if out:
                    buf += out
                    total += len(out)
                while len(buf) > lookahead:
                    n = min(len(buf) - lookahead, 16384)
                    send(frame(FRAME_AUDIO, bytes(buf[:n])))
                    del buf[:n]
                    if first is None:
                        first = time.time() - t0
        finally:
            resp.close()
        while buf:
            n = min(len(buf), 16384)
            send(frame(FRAME_AUDIO, bytes(buf[:n])))
            del buf[:n]
            if first is None:
                first = time.time() - t0
        _last.update({
            "tts": model,
            "tts_s": round(time.time() - t0, 1),
            "audio_s": round(total / 2 / OUT_RATE, 1),
            "first_audio_s": round(first if first is not None else 0, 1),
        })
        print(f"tts {model}: {total / 2 / OUT_RATE:.1f}s audio, first at "
              f"{first if first is not None else 0:.1f}s", file=sys.stderr)
        return True
    return False


def run_smart(wav, begin, send):
    t0 = time.time()
    text = transcribe(wav)
    if not text:
        raise RuntimeError("no transcript returned")
    print(f"heard: {text[:80]}", file=sys.stderr)
    _last.update({"heard": text[:200], "stt_s": round(time.time() - t0, 1)})
    perr = None
    try:
        answer = chat_stream(text)
    except Exception as e:  # noqa: BLE001
        perr = e
        answer = ""
    if perr is not None and not answer:
        raise perr
    begin()
    if answer:
        send(frame(FRAME_TEXT, answer.encode("utf-8", "ignore")))
        try:
            tts_speech(answer, send)
        except Exception as e:  # noqa: BLE001 - text already delivered
            print(f"tts failed: {e!r}", file=sys.stderr)
    send(frame(FRAME_END))


def answer_text(wav_b64):
    if CHAT_MODEL:
        stt = openrouter(
            STT_MODEL,
            [{"role": "user", "content": [{"type": "text", "text": TRANSCRIBE_PROMPT},
                                          audio_part(wav_b64)]}],
        )
        transcript = message_text(stt).strip()
        if not transcript:
            raise RuntimeError("no transcript returned")
        chat = openrouter(
            CHAT_MODEL,
            [{"role": "system", "content": SYSTEM},
             {"role": "user", "content": transcript}],
        )
        return message_text(chat)
    one = openrouter(
        MODEL,
        [{"role": "system", "content": SYSTEM},
         {"role": "user", "content": [{"type": "text", "text": "The voice message:"},
                                      audio_part(wav_b64)]}],
    )
    return message_text(one)


def run_text_then_tts(wav_b64, begin, send):
    text = answer_text(wav_b64).strip()
    if not text:
        raise RuntimeError("empty reply")
    begin()
    send(frame(FRAME_TEXT, text.encode("utf-8", "ignore")))
    if TTS_MODEL:
        payload = {
            "model": TTS_MODEL,
            "stream": True,
            "modalities": ["text", "audio"],
            "audio": {"voice": VOICE, "format": "pcm16"},
            "messages": [
                {"role": "system", "content": TTS_PROMPT},
                {"role": "user", "content": text},
            ],
        }
        try:
            resp, fmt = open_stream_with_format(payload)
        except Exception as e:  # noqa: BLE001 - text already sent; speak nothing
            print(f"tts failed, text only: {e!r}", file=sys.stderr)
        else:
            strip = WavStrip() if fmt == "wav" else None
            conv = None
            try:
                for event in iter_events(resp):
                    if event == b"[DONE]":
                        break
                    try:
                        obj = json.loads(event)
                    except ValueError:
                        continue
                    choices = obj.get("choices") or []
                    if not choices:
                        continue
                    audio = (choices[0].get("delta") or {}).get("audio") or {}
                    if not audio.get("data"):
                        continue
                    raw = base64.b64decode(audio["data"])
                    if strip is not None:
                        raw = strip.feed(raw)
                        if not raw:
                            continue
                    if conv is None:
                        conv = Resampler(strip.rate if strip and strip.rate else 24000)
                    out = conv.feed(raw)
                    if out:
                        send(frame(FRAME_AUDIO, out))
            finally:
                resp.close()
    send(frame(FRAME_END))


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ai-passport-relay"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (time.strftime("%H:%M:%S"), fmt % args))

    def send_text(self, status, text):
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_body(self):
        te = self.headers.get("Transfer-Encoding", "").lower()
        if "chunked" in te:
            parts = []
            total = 0
            while True:
                line = self.rfile.readline(1024).strip()
                if not line:
                    continue
                size = int(line.split(b";")[0], 16)
                if size == 0:
                    while True:  # trailers
                        line = self.rfile.readline(1024)
                        if line in (b"\r\n", b"\n", b""):
                            break
                    break
                total += size
                if total > MAX_BODY:
                    raise ValueError("body too large")
                parts.append(self.rfile.read(size))
                self.rfile.read(2)  # CRLF
            return b"".join(parts)
        length = int(self.headers.get("Content-Length", "0"))
        if length > MAX_BODY:
            raise ValueError("body too large")
        return self.rfile.read(length)

    def begin_frames(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

    def send_chunk(self, data):
        self.wfile.write(b"%x\r\n" % len(data) + data + b"\r\n")
        self.wfile.flush()

    def end_frames(self):
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    def do_GET(self):
        if self.path in ("/", "/health"):
            self.send_text(200, "AI Passport relay up\n")
        elif self.path == "/last":
            self.send_text(200, json.dumps(_last, ensure_ascii=False) + "\n")
        else:
            self.send_text(404, "not found\n")

    def do_POST(self):
        if self.path != "/ask":
            self.send_text(404, "not found\n")
            return
        if TOKEN and self.headers.get("Authorization") != f"Bearer {TOKEN}":
            self.send_text(401, "bad token\n")
            return
        t0 = time.time()
        try:
            wav = self.read_body()
        except ValueError:
            self.send_text(413, "body too large\n")
            return
        if not wav:
            self.send_text(400, "empty body\n")
            return
        if not API_KEY:
            self.send_text(500, "relay has no API key\n")
            return

        _last.clear()
        wav_b64 = base64.b64encode(wav).decode("ascii")
        started = {"done": False}

        def begin():
            started["done"] = True
            self.begin_frames()

        def send(data):
            self.send_chunk(data)

        try:
            if SMART:
                run_smart(wav, begin, send)
            elif AUDIO_MODEL:
                run_audio_chat(wav_b64, begin, send)
            else:
                run_text_then_tts(wav_b64, begin, send)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "ignore")[:200]
            print(f"openrouter {e.code}: {detail}", file=sys.stderr)
            if not started["done"]:
                self.send_text(502, f"model error {e.code}\n")
            else:
                try:
                    self.end_frames()
                except OSError:
                    pass
            return
        except Exception as e:  # noqa: BLE001
            print(f"error: {e!r}", file=sys.stderr)
            if not started["done"]:
                self.send_text(502, "relay error\n")
            else:
                try:
                    self.end_frames()
                except OSError:
                    pass
            return
        self.end_frames()
        _last["total_s"] = round(time.time() - t0, 1)
        self.log_message("ask wav=%.1fKB %.1fs", len(wav) / 1024, time.time() - t0)


def main():
    if not API_KEY:
        print("RELAY_API_KEY (or OPENROUTER_API_KEY) is not set", file=sys.stderr)
        return 1
    if SMART:
        mode = f"smart via {BASE_URL} (stt + web-search chat + tts)"
    elif AUDIO_MODEL:
        mode = f"audio-chat model={AUDIO_MODEL} voice={VOICE} via {BASE_URL}"
    elif TTS_MODEL:
        mode = f"text + tts={TTS_MODEL} voice={VOICE} via {BASE_URL}"
    else:
        mode = f"text only (model={MODEL}) via {BASE_URL}"
    if audioop is None:
        print("warning: audioop missing; 24 kHz TTS passes through unsampled", file=sys.stderr)
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"AI Passport relay on :{PORT} ({mode})", file=sys.stderr)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
