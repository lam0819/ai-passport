<!--
Copyright (c) Meta Platforms, Inc. and affiliates.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
-->

# AI Passport

A **personal, self-hosted voice assistant** built on the FoloToy AI Passport
hardware (ESP32-C3, 8 MB flash, no PSRAM). Hold **OK**, speak, and the device
answers — on screen and out loud — using **your own relay** and **your own
model key**. No vendor account, no phone pairing app, no vendor cloud.

Written and spoken **Cantonese (Traditional)** and Mandarin (Simplified) are
first-class; the reply language mirrors what you speak.
  廣東話優先：識聽、識講、繁體字幕，答案會上網查最新資料。

## How it works

```
AI Passport (ESP32-C3)                       your relay (Zo / VPS / NUC)
  hold OK ──► 16 kHz WAV ── HTTP(S) ──────►   speech to text
  caption + speech  ◄── framed text + PCM ──  answer (with web search),
                                              then text-to-speech
```

One press, one request: transcribe → answer → speak. The relay,
[`relay/relay.py`](relay/relay.py) (Python 3, standard library only), streams
back small frames: caption text and 16 kHz speech. The caption follows the
voice, and the voice starts while the rest is still being synthesized.

## Features

- **Push-to-talk** — hold OK, speak, release; the reply is shown and spoken.
- **Web-search answers** — real stock prices, weather, news, schedules.
- **Continuous conversation** — after a reply the mic reopens by itself and
  the turn ends on a pause. Press OK any time to barge in; quiet ends it.
- **On-device Wi-Fi setup** — menu → Wi-Fi, pick the network, type the
  password with a Nokia-style multi-tap keypad. Saved in NVS; no app.
- **Languages** — Cantonese / Traditional / Simplified, mirroring the
  spoken language (or forced with one setting), GB2312 + Big5 + Cantonese
  caption font.
- **Menu** — volume, speaker, brightness, mic gain, auto-sleep, phone setup,
  Wi-Fi, continuous chat, status, battery, reset, screen off, power off.

## What differs from the original firmware

| | Original (vendor) | This build |
|---|---|---|
| Cloud | vendor account + pairing app | **your** relay and API key |
| Wi-Fi setup | via phone app pairing | on the device itself |
| Conversation | one question per press | **continuous** mode |
| Captions | Simplified Chinese only | + Traditional and Cantonese |
| Voice replies | board-dependent | spoken replies as standard |
| Images / vendor tunnel | yes | dropped (no PSRAM by design) |

## Quick start

**1. The relay** — on a machine you control, with a public HTTPS URL (a
small cloud box plus a `cloudflared` tunnel works):

```sh
export RELAY_TOKEN=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
export OPENAI_API_KEY=sk-...
export RELAY_SMART=1        # web-search answers
python3 relay/relay.py
```

**2. The firmware** — ESP-IDF v6.0.1 with the `esp32c3` tools:

```sh
. ~/esp/esp-idf-v6/export.sh
cd esp32
mkdir -p build-passport
cat > build-passport/sdkconfig.local << 'EOF'
CONFIG_MUSE_CHAT_RELAY=y
CONFIG_MUSE_CHAT_RELAY_URL="https://your-relay.example/ask"
CONFIG_MUSE_CHAT_RELAY_TOKEN="the RELAY_TOKEN above"
CONFIG_MBEDTLS_CERTIFICATE_BUNDLE_CROSS_SIGNED_VERIFY=y
EOF
idf.py -B build-passport -DIDF_TARGET=esp32c3 \
  -DSDKCONFIG=build-passport/sdkconfig \
  -DSDKCONFIG_DEFAULTS="sdkconfig.defaults;devices/sdkconfig.muse;devices/sdkconfig.muse-passport;build-passport/sdkconfig.local" \
  build
cd build-passport
python -m esptool --chip esp32c3 -p /dev/cu.usbmodem* -b 460800 \
  --before default-reset --after hard-reset write-flash @flash_args
```

**3. The device** — power on, open the menu (short UP), go to **Wi-Fi**,
join your network, then hold **OK** and talk. Continuous mode is on by
default: after each reply, just speak again.

Keep the relay URL and token in the gitignored `build-passport/` files or on
the relay itself — never in a tracked file. Do not flash `--erase-all`: a
normal flash keeps NVS (Wi-Fi and settings).

## Relay configuration

All environment variables; the file header lists every one.

| Variable | Meaning |
|---|---|
| `OPENAI_API_KEY` | your key (or `RELAY_API_KEY`); stays on the relay |
| `RELAY_BASE_URL` | OpenAI-compatible base, default `https://api.openai.com/v1` |
| `RELAY_TOKEN` | shared secret the device sends |
| `RELAY_SMART=1` | smart mode: transcribe → web-search chat → speech |
| `RELAY_LANGUAGE` | force a reply language, e.g. `Cantonese (Hong Kong)` |
| `RELAY_TTS_SPEED` | speech rate, default 1.12 |
| `RELAY_PORT` | listen port, default 8787 |

`GET /` is a health check; `GET /last` shows what the last request heard and
which models answered (seconds, web citations, audio length).

## Costs

Pay-per-use from your model provider. A typical turn (transcribe + one
searched answer + ~10 s of speech) is on the order of **one US cent** with
the default `gpt-4o-mini`-class models.

## Layout

| Path | What |
|---|---|
| `relay/relay.py` | The relay to run on your own machine. |
| `esp32/` | The ESP-IDF firmware for the Passport. |
| `esp32/components/muse/muse_chat_relay.c` | The firmware's relay backend. |
| `skills/`, `linux/` | Upstream extras; not needed for this firmware. |

## License and origin

A fork of the community firmware for this board
([RongleCat/ai-passport-muse](https://github.com/RongleCat/ai-passport-muse),
itself a fork of
[facebookincubator/muse-gadget-sdk](https://github.com/facebookincubator/muse-gadget-sdk)).
The vendor cloud is replaced by your own relay, with additions: the on-device
Wi-Fi setup, continuous conversation, the relay chat backend, and a
GB2312 + Big5 + Cantonese caption font.

Apache License 2.0 — see [`LICENSE`](LICENSE). Third-party files keep their
own licenses (minimp3 CC0, `pixel_font.c` BSD-2-Clause, the caption font
SIL OFL 1.1, LVGL MIT). Flashing custom firmware can brick the device or
void warranties — proceed at your own risk.
