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

Custom firmware for the **FoloToy AI Passport** (ESP32-C3, 8 MB flash, no
PSRAM) that turns it into a personal voice assistant. The device talks to a
small relay server you run yourself (a personal cloud computer works well),
which does speech to text, answers with web search, and speaks the reply
back. No vendor account, no phone pairing app.

## What it does

- **Push-to-talk** — hold OK, speak, release. The reply appears on screen and
  is spoken through the speaker.
- **Web-search answers** — real stock prices, weather, news, schedules.
- **Continuous conversation** — after each reply it listens again by itself,
  no button needed; stops after a quiet pause or a press.
- **On-device Wi-Fi setup**, Nokia style — menu → Wi-Fi → pick a network →
  type the password with the multi-tap keypad. Saved in NVS.
- **Cantonese & Chinese** — the reply language mirrors what you speak
  (Simplified, Traditional or spoken Cantonese), and the caption font covers
  GB2312, Big5 and the Cantonese characters.  廣東話：識聽識講，繁體字幕。
- **Menu** — volume, speaker, brightness, mic gain, auto-sleep, phone setup,
  Wi-Fi, continuous chat, status, battery, reset, screen off, power off.

## Layout

| Path | What |
|---|---|
| `relay/relay.py` | The relay: runs on your own machine or cloud box. Python 3, standard library only. |
| `esp32/` | The ESP-IDF firmware for the Passport. |
| `skills/`, `linux/` | Upstream extras; not needed for this firmware. |

## The relay

`relay/relay.py` receives the recorded WAV over HTTP, transcribes it
(`gpt-4o-transcribe`), answers with web search (`gpt-5-search-api` /
`gpt-4o-search-preview`), and returns speech (`gpt-4o-mini-tts`) as a framed
stream the device plays while the caption follows the voice. The reply text
is capped to one or two short sentences.

Configuration is all environment variables — see the file header. Minimum:

```sh
RELAY_TOKEN=<a long random secret>      # the device sends this
OPENAI_API_KEY=<your key>               # stays on the relay
RELAY_SMART=1                           # web-search answers
python3 relay/relay.py
```

A public HTTPS URL for the relay can come from a tunnel (e.g. `cloudflared`).
Keep the device's relay token and URL in the gitignored build config, never
in a tracked file.

## Build & flash

ESP-IDF **v6.0.1** with the `esp32c3` tools:

```sh
. ~/esp/esp-idf-v6/export.sh
cd esp32
mkdir -p build-passport
cat > build-passport/sdkconfig.local << 'EOF'
CONFIG_MUSE_CHAT_RELAY=y
CONFIG_MUSE_CHAT_RELAY_URL="https://your-relay.example/ask"
CONFIG_MUSE_CHAT_RELAY_TOKEN="your token"
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

Wi-Fi is configured on the device itself (menu → Wi-Fi), not in the build.
Do not pass `--erase-all`; a normal flash keeps NVS (Wi-Fi, settings).

## Origin and license

This is a fork of the community firmware for this board
([RongleCat/ai-passport-muse](https://github.com/RongleCat/ai-passport-muse),
itself a fork of [facebookincubator/muse-gadget-sdk](https://github.com/facebookincubator/muse-gadget-sdk)).
The vendor cloud is replaced by the personal relay, with additions: the
on-device Wi-Fi setup, continuous conversation, the relay chat backend
(`esp32/components/muse/muse_chat_relay.c`), and a GB2312 + Big5 + Cantonese
caption font.

Apache License 2.0 — see [`LICENSE`](LICENSE). Third-party files keep their
own licenses (minimp3 CC0, `pixel_font.c` BSD-2-Clause, the caption font
SIL OFL 1.1, LVGL MIT). Flashing custom firmware can brick the device or
void warranties — proceed at your own risk.
