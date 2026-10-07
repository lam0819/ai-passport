/*
 * Copyright (c) Meta Platforms, Inc. and affiliates.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

/*
 * Chat turns to an own relay server (CONFIG_MUSE_CHAT_RELAY).
 *
 * The board streams each push-to-talk note to CONFIG_MUSE_CHAT_RELAY_URL as
 * the body of one HTTP POST (raw 16 kHz mono WAV, chunked). The reply comes
 * back as a sequence of frames:
 *
 *   [1 type byte][4 length bytes, little-endian][payload]
 *   0x00 END    no payload; the reply is complete
 *   0x01 TEXT   UTF-8 delta appended to the reply text (the caption)
 *   0x02 AUDIO  PCM16 little-endian, 16 kHz mono, played through the speaker
 *
 * Frames stream live, so the caption grows and the voice starts as soon as
 * the model produces them. The relay (relay/relay.py in this repository)
 * speaks this protocol. An HTTP worker task owns the request so the voice
 * task only copies PCM into a small block ring, feeds the reply audio into a
 * stream buffer, and polls for events. The upload body is chunk-framed here
 * because esp_http_client writes request bodies verbatim once
 * esp_http_client_open(c, -1) sets Transfer-Encoding: chunked.
 */
#include "muse_chat.h"
#include "muse_chat_priv.h"

#include <stdio.h>
#include <string.h>

#include "esp_crt_bundle.h"
#include "esp_http_client.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "freertos/stream_buffer.h"
#include "freertos/task.h"

#include "muse_wifi.h"

static const char *TAG = "muse_chat_relay";

#define MIC_RATE 16000
#define BLOCK 640                       /* one 20 ms voice chunk (320 frames) */
#define RING_BLOCKS 24                  /* about 480 ms: covers the TLS setup */
#define RING_SIZE (BLOCK * RING_BLOCKS)
#define AUDIO_STREAM 32768              /* ~1 s of reply speech buffered ahead */
#define EV_TEXT 72
#define TEXT_MAX 1024
#define SEND_WAIT_MS 3000               /* ring full: the network has stalled this long */
#define STOP_WAIT_MS 3000               /* a previous turn's request to wind down */
#define HTTP_TIMEOUT_MS 90000           /* the model can take a while to answer */
#define REPLY_TIMEOUT_US 60000000       /* to the first text, not the whole speech */
#define AUDIO_WAIT_US 6000000           /* no voice this long: caption pages as text */
#define SETTLE_US 3000000               /* quiet after the speech before the turn ends */
#define READ_CHARS_PER_S 14             /* caption scroll, about speaking pace */
#define WORKER_STACK 10240

/* Response frames (see the header comment). */
#define FRAME_END 0x00
#define FRAME_TEXT 0x01
#define FRAME_AUDIO 0x02
#define FRAME_MAX 65536

typedef struct {
    char text[EV_TEXT];
    muse_hatch_ev_t type;
} ev_t;

typedef enum { T_IDLE, T_TALKING, T_UPLOAD, T_REPLY } phase_t;

/* Voice task only. */
static struct {
    phase_t phase;
    uint32_t gen;
    uint8_t stage[BLOCK];               /* PCM waiting for a whole block */
    size_t stage_len;
    char text[TEXT_MAX];                /* reply text so far, appended in place */
    size_t synced;                      /* bytes copied out of s_reply */
    char shown[EV_TEXT];
    char error[EV_TEXT];
    int64_t t_end, t_reply, t_show;
    bool sent, replied;
} s_turn;

/* The upload in flight. gen 0: no turn yet. */
static volatile uint32_t s_gen;
static volatile uint32_t s_end_gen;     /* producer flushed all audio for this gen */
static portMUX_TYPE s_up_mux = portMUX_INITIALIZER_UNLOCKED;
static struct {
    uint32_t gen;
    bool open;                          /* the turn accepts audio / a reply */
    bool active;                        /* the worker owns the request */
    bool uploaded;                      /* the body is complete */
    bool done;                          /* the response was read (or failed) */
    int status;                         /* HTTP status, -1 on a transport error */
    size_t len;                         /* reply text bytes in s_reply */
} s_up;
static char s_reply[TEXT_MAX + 64];

/* Single-producer single-consumer block ring. Every entry is one BLOCK slot;
 * s_blen holds how much of it is real (the last block of a note is partial). */
static uint8_t s_ring[RING_SIZE];
static uint16_t s_blen[RING_BLOCKS];
static volatile size_t s_head, s_tail;   /* block-aligned byte offsets */
static SemaphoreHandle_t s_blocks;       /* free ring blocks */
static StreamBufferHandle_t s_audio;     /* reply PCM waiting to be played */
static TaskHandle_t s_worker;
static QueueHandle_t s_events;

/* ---- Small helpers ---- */

static bool relay_configured(void)
{
#if defined(CONFIG_MUSE_CHAT_RELAY_URL)
    return CONFIG_MUSE_CHAT_RELAY_URL[0] != '\0';
#else
    return false;
#endif
}

static void emit(muse_hatch_ev_t type, const char *text)
{
    ev_t ev = { .type = type };
    strlcpy(ev.text, text ? text : "", sizeof(ev.text));
    xQueueSend(s_events, &ev, 0);
}

static void up_publish(uint32_t gen, int status, size_t len)
{
    portENTER_CRITICAL(&s_up_mux);
    if (s_up.gen == gen) {
        s_up.status = status;
        s_up.len = len;
        s_up.done = true;
        s_up.active = false;
    }
    portEXIT_CRITICAL(&s_up_mux);
}

/* Stops accepting a turn. The worker notices s_gen and winds the request down. */
static void end_turn(void)
{
    s_gen++;
    portENTER_CRITICAL(&s_up_mux);
    s_up.open = false;
    portEXIT_CRITICAL(&s_up_mux);
    s_turn.phase = T_IDLE;
}

static void fail(const char *why)
{
    ESP_LOGW(TAG, "turn failed: %s", why);
    strlcpy(s_turn.error, why, sizeof(s_turn.error));
    end_turn();
    emit(MUSE_HATCH_EV_ERROR, why);
}

/* How far through the reply reading has got. */
static size_t read_at(int64_t now)
{
    return (size_t)((now - s_turn.t_show) * READ_CHARS_PER_S / 1000000);
}

/* Scrolls the reply through the caption at reading pace; true once it's all been shown. */
static bool scroll(int64_t now)
{
    size_t len = strlen(s_turn.text);
    if (len == 0) {
        return false;
    }
    size_t at = read_at(now);
    char line[EV_TEXT];
    if (muse_hatch_caption_at(s_turn.text, at < len ? at : len - 1, line, sizeof(line))
        && strcmp(line, s_turn.shown)) {
        strlcpy(s_turn.shown, line, sizeof(s_turn.shown));
        emit(MUSE_HATCH_EV_REPLY, line);
    }
    return at >= len;
}

/* ---- The upload worker ---- */

static bool http_write_all(esp_http_client_handle_t c, const char *p, size_t n)
{
    size_t off = 0;
    while (off < n) {
        int w = esp_http_client_write(c, p + off, (int)(n - off));
        if (w <= 0) {
            return false;
        }
        off += (size_t)w;
    }
    return true;
}

/* esp_http_client sets Transfer-Encoding: chunked for open(c, -1) but writes
 * bytes verbatim, so the framing is done here. */
static bool http_write_chunk(esp_http_client_handle_t c, const uint8_t *p, size_t n)
{
    char hdr[16];
    int k = snprintf(hdr, sizeof(hdr), "%zx\r\n", n);
    return http_write_all(c, hdr, (size_t)k)
        && (n == 0 || http_write_all(c, (const char *)p, n))
        && http_write_all(c, "\r\n", 2);
}

static bool read_exact(esp_http_client_handle_t c, uint8_t *out, size_t n, uint32_t gen)
{
    size_t got = 0;
    while (got < n) {
        if (s_gen != gen) {
            return false;
        }
        int r = esp_http_client_read(c, (char *)out + got, (int)(n - got));
        if (r <= 0) {
            return false;
        }
        got += (size_t)r;
    }
    return true;
}

/* Copies reply PCM into the playback buffer, waiting for the voice task to
 * drain it. Bounded waits so a cancelled turn winds down promptly. */
static bool push_audio(const uint8_t *p, size_t n, uint32_t gen)
{
    size_t off = 0;
    while (off < n) {
        if (s_gen != gen) {
            return false;
        }
        off += xStreamBufferSend(s_audio, p + off, n - off, pdMS_TO_TICKS(500));
    }
    return true;
}

/* Reads the framed reply. Runs on the worker; publishes with up_publish, so
 * nothing reads a half-written reply. */
static void relay_receive(esp_http_client_handle_t c, uint32_t gen)
{
    (void)esp_http_client_fetch_headers(c);
    int status = esp_http_client_get_status_code(c);
    size_t text_total = 0;
    if (status == 200) {
        uint8_t hdr[5];
        uint8_t tmp[512];
        for (;;) {
            if (s_gen != gen) {
                return;
            }
            if (!read_exact(c, hdr, sizeof(hdr), gen)) {
                break;   /* end of body or a truncated frame */
            }
            uint32_t len = (uint32_t)hdr[1] | (uint32_t)hdr[2] << 8
                | (uint32_t)hdr[3] << 16 | (uint32_t)hdr[4] << 24;
            if (len > FRAME_MAX) {
                ESP_LOGW(TAG, "bad frame length %u", (unsigned)len);
                status = -1;
                break;
            }
            if (hdr[0] == FRAME_END) {
                break;
            }
            if (hdr[0] == FRAME_TEXT) {
                uint32_t left = len;
                while (left) {
                    size_t n = left > sizeof(tmp) ? sizeof(tmp) : left;
                    if (!read_exact(c, tmp, n, gen)) {
                        status = -1;
                        break;
                    }
                    portENTER_CRITICAL(&s_up_mux);
                    size_t room = text_total < sizeof(s_reply) - 1
                                      ? sizeof(s_reply) - 1 - text_total
                                      : 0;
                    size_t copy = n < room ? n : room;
                    if (copy) {
                        memcpy(s_reply + text_total, tmp, copy);
                        text_total += copy;
                        s_reply[text_total] = '\0';
                    }
                    s_up.len = text_total;
                    portEXIT_CRITICAL(&s_up_mux);
                    left -= n;
                }
                if (status == -1) {
                    break;
                }
            } else if (hdr[0] == FRAME_AUDIO) {
                uint32_t left = len;
                while (left) {
                    if (s_gen != gen) {
                        return;
                    }
                    size_t n = left > sizeof(tmp) ? sizeof(tmp) : left;
                    if (!read_exact(c, tmp, n, gen)) {
                        status = -1;
                        break;
                    }
                    if (!push_audio(tmp, n, gen)) {
                        return;
                    }
                    left -= n;
                }
                if (status == -1) {
                    break;
                }
            } else {
                ESP_LOGW(TAG, "unknown frame type %u", hdr[0]);
                status = -1;
                break;
            }
        }
    }
    if (s_gen != gen) {
        return;
    }
    up_publish(gen, status, text_total);
}

static void relay_worker(void *arg)
{
    (void)arg;
    for (;;) {
        ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
        uint32_t gen;
        portENTER_CRITICAL(&s_up_mux);
        gen = s_up.gen;
        bool claim = s_up.open && !s_up.done && !s_up.active && gen && gen == s_gen;
        if (claim) {
            s_up.active = true;
        }
        portEXIT_CRITICAL(&s_up_mux);
        if (!claim) {
            continue;
        }

        esp_http_client_config_t cfg = {
            .url = CONFIG_MUSE_CHAT_RELAY_URL,
            .method = HTTP_METHOD_POST,
            .timeout_ms = HTTP_TIMEOUT_MS,
            .crt_bundle_attach = esp_crt_bundle_attach,
            .disable_auto_redirect = true,
        };
        esp_http_client_handle_t c = esp_http_client_init(&cfg);
        bool ok = c && esp_http_client_set_header(c, "Content-Type", "audio/wav") == ESP_OK;
#if defined(CONFIG_MUSE_CHAT_RELAY_TOKEN)
        if (ok && CONFIG_MUSE_CHAT_RELAY_TOKEN[0]) {
            char auth[192];
            snprintf(auth, sizeof(auth), "Bearer %s", CONFIG_MUSE_CHAT_RELAY_TOKEN);
            ok = esp_http_client_set_header(c, "Authorization", auth) == ESP_OK;
        }
#endif
        if (ok) {
            ok = esp_http_client_set_header(c, "Accept", "application/octet-stream") == ESP_OK;
        }
        esp_err_t err = ok ? esp_http_client_open(c, -1) : ESP_FAIL;
        if (err != ESP_OK) {
            ESP_LOGW(TAG, "connect failed: %s", esp_err_to_name(err));
            if (c) {
                esp_http_client_cleanup(c);
            }
            up_publish(gen, -1, 0);
            continue;
        }

        /* Stream the note. The producer only queues whole blocks, so a block
         * leaves the ring only once it is fully written. */
        bool write_failed = false;
        while (s_gen == gen) {
            while (s_head != s_tail) {
                size_t at = s_tail;
                size_t n = s_blen[at / BLOCK];
                if (!http_write_chunk(c, s_ring + at, n)) {
                    write_failed = true;
                    break;
                }
                s_tail = (s_tail + BLOCK) % RING_SIZE;
                xSemaphoreGive(s_blocks);
            }
            if (write_failed || s_end_gen == gen) {
                break;
            }
            /* Ring drained but the note is still being recorded: wait for more. */
            ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS(200));
        }

        if (write_failed || s_gen != gen) {
            if (write_failed) {
                ESP_LOGW(TAG, "upload failed");
                up_publish(gen, -1, 0);
            }
        } else if (!http_write_all(c, "0\r\n\r\n", 5)) {
            up_publish(gen, -1, 0);
        } else {
            portENTER_CRITICAL(&s_up_mux);
            if (s_up.gen == gen) {
                s_up.uploaded = true;
            }
            portEXIT_CRITICAL(&s_up_mux);
            relay_receive(c, gen);
        }
        esp_http_client_close(c);
        esp_http_client_cleanup(c);
        portENTER_CRITICAL(&s_up_mux);
        s_up.active = false;
        portEXIT_CRITICAL(&s_up_mux);
    }
}

/* ---- The turn, on the voice task ---- */

static bool push_block(const uint8_t *p, size_t n, int wait_ms)
{
    if (xSemaphoreTake(s_blocks, pdMS_TO_TICKS(wait_ms ? wait_ms : 1)) != pdTRUE) {
        return false;
    }
    size_t at = s_head;
    memcpy(s_ring + at, p, n);
    s_blen[at / BLOCK] = (uint16_t)n;
    __sync_synchronize();
    s_head = (at + BLOCK) % RING_SIZE;
    if (s_worker) {
        xTaskNotifyGive(s_worker);
    }
    return true;
}

/* Flushes the staged PCM: whole blocks while recording, the remainder at the
 * end of the note. */
static bool flush_stage(bool last)
{
    while (s_turn.stage_len >= BLOCK) {
        if (!push_block(s_turn.stage, BLOCK, SEND_WAIT_MS)) {
            return false;
        }
        memmove(s_turn.stage, s_turn.stage + BLOCK, s_turn.stage_len - BLOCK);
        s_turn.stage_len -= BLOCK;
    }
    if (last && s_turn.stage_len) {
        if (!push_block(s_turn.stage, s_turn.stage_len, SEND_WAIT_MS)) {
            return false;
        }
        s_turn.stage_len = 0;
    }
    return true;
}

/* Moves the turn along; runs on the voice task each time it asks for events. */
static void pump(void)
{
    int64_t now = esp_timer_get_time();
    if (s_turn.phase == T_IDLE) {
        return;
    }
    if (s_turn.phase == T_TALKING || s_turn.phase == T_UPLOAD) {
        bool uploaded, done;
        int status;
        size_t len;
        portENTER_CRITICAL(&s_up_mux);
        bool mine = s_up.gen == s_turn.gen;
        uploaded = mine && s_up.uploaded;
        done = mine && s_up.done;
        status = s_up.status;
        len = s_up.len;
        portEXIT_CRITICAL(&s_up_mux);
        if (!s_turn.sent && uploaded) {
            s_turn.sent = true;
            s_turn.phase = T_UPLOAD;
            emit(MUSE_HATCH_EV_SENT, NULL);
        }
        if (len > s_turn.synced) {
            portENTER_CRITICAL(&s_up_mux);
            size_t used = strlen(s_turn.text);
            size_t room = sizeof(s_turn.text) - used - 1;
            size_t take = len - s_turn.synced;
            if (take > room) {
                take = room;
            }
            memcpy(s_turn.text + used, s_reply + s_turn.synced, take);
            s_turn.text[used + take] = '\0';
            portEXIT_CRITICAL(&s_up_mux);
            s_turn.synced = len;
            if (!s_turn.replied) {
                s_turn.replied = true;
                s_turn.t_show = now;
                s_turn.t_reply = now;
                s_turn.phase = T_REPLY;
                ESP_LOGI(TAG, "reply after %.2fs: %.80s",
                         (now - s_turn.t_end) / 1e6, s_turn.text);
            }
            (void)scroll(now);
        }
        if (done && !s_turn.replied) {
            if (status == 200) {
                fail("EMPTY REPLY");
            } else if (status < 0) {
                fail("CAN'T REACH SERVER");
            } else if (status == 401) {
                fail("RELAY AUTH FAILED (401)");
            } else if (status == 403) {
                fail("RELAY ACCESS DENIED (403)");
            } else {
                char why[EV_TEXT];
                snprintf(why, sizeof(why), "RELAY ERROR (HTTP %d)", status);
                fail(why);
            }
            return;
        }
        if (!s_turn.replied && s_turn.t_end && now - s_turn.t_end > REPLY_TIMEOUT_US) {
            fail("NO REPLY FROM SERVER");
        }
        return;
    }
    if (s_turn.phase == T_REPLY) {
        bool done;
        portENTER_CRITICAL(&s_up_mux);
        done = s_up.gen == s_turn.gen && s_up.done;
        portEXIT_CRITICAL(&s_up_mux);
        if (done && s_turn.replied && scroll(now)
            && xStreamBufferBytesAvailable(s_audio) == 0
            && now - s_turn.t_reply > SETTLE_US) {
            /* The caption is cleared with the turn. The reader still needs this body. */
            muse_hatch_keep_reply(s_turn.text);
            end_turn();
            emit(MUSE_HATCH_EV_DONE, NULL);
        }
    }
}

/* ---- Public ---- */

void muse_hatch_start(void)
{
    s_events = xQueueCreate(8, sizeof(ev_t));
    s_blocks = xSemaphoreCreateCounting(RING_BLOCKS, RING_BLOCKS);
    s_audio = xStreamBufferCreate(AUDIO_STREAM, 1);
    if (!s_events || !s_blocks || !s_audio
        || xTaskCreate(relay_worker, "relay_http", WORKER_STACK, NULL, 5, &s_worker) != pdPASS) {
        ESP_LOGE(TAG, "relay worker failed to start");
    }
}

void muse_hatch_status(muse_hatch_status_t *out)
{
    if (!relay_configured()) {
        out->state = MUSE_HATCH_NOT_SET;
        strlcpy(out->detail, "Set the relay URL", sizeof(out->detail));
    } else if (!muse_wifi_connected()) {
        out->state = MUSE_HATCH_OFFLINE;
        strlcpy(out->detail, "Waiting for Wi-Fi", sizeof(out->detail));
    } else {
        out->state = MUSE_HATCH_REACHABLE;
        strlcpy(out->detail, "Your server, text and voice", sizeof(out->detail));
    }
}

void muse_hatch_test(void)
{
}

void muse_hatch_config_changed(void)
{
}

void muse_hatch_set_resting(bool resting)
{
    (void)resting;
}

const char *muse_hatch_state_name(muse_hatch_state_t state)
{
    switch (state) {
    case MUSE_HATCH_NOT_SET: return "Not set up";
    case MUSE_HATCH_OFFLINE: return "Offline";
    case MUSE_HATCH_UNTESTED: return "Saved";
    case MUSE_HATCH_TESTING: return "Connecting";
    case MUSE_HATCH_REACHABLE: return "Connected";
    case MUSE_HATCH_UNREACHABLE: return "Can't connect";
    }
    return "";
}

bool muse_hatch_ready(void)
{
    return s_events && relay_configured() && muse_wifi_connected();
}

void muse_hatch_turn_begin(void)
{
    if (!muse_hatch_ready()) {
        fail(relay_configured() ? "NO WI-FI" : "SET UP RELAY FIRST");
        return;
    }
    /* Wind down a previous request before the ring is reused. */
    portENTER_CRITICAL(&s_up_mux);
    bool busy = s_up.active;
    portEXIT_CRITICAL(&s_up_mux);
    for (int waited = 0; busy && waited < STOP_WAIT_MS; waited += 20) {
        vTaskDelay(pdMS_TO_TICKS(20));
        xTaskNotifyGive(s_worker);
        portENTER_CRITICAL(&s_up_mux);
        busy = s_up.active;
        portEXIT_CRITICAL(&s_up_mux);
    }
    if (busy) {
        fail("SERVER BUSY");
        return;
    }

    xQueueReset(s_events);
    memset(&s_turn, 0, sizeof(s_turn));
    /* The worker is parked: the ring and the playback buffer are ours. */
    while (xSemaphoreTake(s_blocks, 0) == pdTRUE) {
    }
    for (int i = 0; i < RING_BLOCKS; i++) {
        xSemaphoreGive(s_blocks);
    }
    s_head = s_tail = 0;
    s_end_gen = 0;
    xStreamBufferReset(s_audio);

    uint32_t gen = ++s_gen;
    portENTER_CRITICAL(&s_up_mux);
    s_up.gen = gen;
    s_up.open = true;
    s_up.uploaded = false;
    s_up.done = false;
    s_up.status = 0;
    s_up.len = 0;
    portEXIT_CRITICAL(&s_up_mux);
    s_turn.gen = gen;
    s_turn.phase = T_TALKING;

    uint8_t wav[MUSE_HATCH_WAV_HEADER];
    muse_hatch_wav_header(wav, MIC_RATE);
    if (!push_block(wav, sizeof(wav), SEND_WAIT_MS)) {
        fail("CAN'T KEEP UP");
    }
}

void muse_hatch_turn_audio(const int16_t *pcm, size_t frames)
{
    if (s_turn.phase != T_TALKING) {
        return;
    }
    const uint8_t *p = (const uint8_t *)pcm;
    size_t n = frames * 2;
    while (n) {
        size_t take = BLOCK - s_turn.stage_len < n ? BLOCK - s_turn.stage_len : n;
        memcpy(s_turn.stage + s_turn.stage_len, p, take);
        s_turn.stage_len += take;
        p += take;
        n -= take;
        if (s_turn.stage_len == BLOCK && !flush_stage(false)) {
            fail("CAN'T KEEP UP");
            return;
        }
    }
}

void muse_hatch_turn_end(void)
{
    if (s_turn.phase != T_TALKING) {
        emit(MUSE_HATCH_EV_ERROR, s_turn.error[0] ? s_turn.error : "CAN'T REACH SERVER");
        return;
    }
    if (!flush_stage(true)) {
        fail("CAN'T KEEP UP");
        return;
    }
    s_turn.t_end = esp_timer_get_time();
    s_end_gen = s_turn.gen;
    __sync_synchronize();
    if (s_worker) {
        xTaskNotifyGive(s_worker);
    }
}

void muse_hatch_turn_cancel(void)
{
    end_turn();
    xQueueReset(s_events);
}

muse_hatch_ev_t muse_hatch_turn_event(char *text, size_t cap)
{
    pump();
    ev_t ev;
    if (xQueueReceive(s_events, &ev, 0) != pdTRUE) {
        return MUSE_HATCH_EV_NONE;
    }
    strlcpy(text, ev.text, cap);
    return ev.type;
}

bool muse_hatch_turn_caption(size_t played, char *out, size_t cap)
{
    size_t len = strlen(s_turn.text);
    if (s_turn.phase != T_REPLY || !s_turn.replied || !len) {
        return false;
    }
    /* With speech, the page follows the voice so the two stay together.
     * Before the first sample, hold the opening page; if no speech ever
     * comes (the relay answered text-only), page at reading pace instead. */
    size_t at;
    int64_t now = esp_timer_get_time();
    if (played > 0) {
        at = played * READ_CHARS_PER_S / MIC_RATE;
    } else if (now - s_turn.t_show < AUDIO_WAIT_US) {
        at = 0;
    } else {
        at = read_at(now - AUDIO_WAIT_US);
    }
    return muse_hatch_caption_at(s_turn.text, at < len ? at : len - 1, out, cap);
}

size_t muse_hatch_turn_read(int16_t *pcm, size_t frames, int wait_ms)
{
    if (!s_audio || frames == 0) {
        return 0;
    }
    size_t got = xStreamBufferReceive(s_audio, pcm, frames * sizeof(int16_t),
                                      pdMS_TO_TICKS(wait_ms));
    return got / sizeof(int16_t);
}

size_t muse_hatch_mp3_selftest(int16_t **pcm)
{
    *pcm = NULL;
    return 0;
}
