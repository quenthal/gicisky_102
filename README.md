# Gicisky 10.2 ESP32 MQTT-to-BLE Bridge (work in progress)

Home Assistant renders images for a Gicisky/PickSmart 10.2-inch (960x640, BWR)
e-paper display and sends the binary payload to a dedicated ESP32 over MQTT.
The ESP32 stores the image in LittleFS, verifies its MD5, disables WiFi during
BLE transmission, and restores WiFi to publish the result.

## Status

**Pre-release / hardware verification required.** This repository is a sanitized
starter snapshot, not a verified release. The `fonts/` directory bundles only
redistributable assets: NotoSansKR (SIL OFL 1.1) and the Material Design Icons font
and metadata. Fonts with unclear redistribution terms (CookieRun, GmarketSans,
OwnglyphParkDaHyun) are not included; see the integration README for using them
from `/config/www/fonts/`.

## Branding

`custom_components/gicisky_102/brand/icon.png` and `logo.png` contain the
user-provided Gicisky MQTT artwork (both currently use the same image).
Review any Gicisky trademarks before publicly distributing branding.

## Architecture

`HA write action -> renderer -> compression2 or raw encoding -> MQTT chunks ->
ESP32 LittleFS and integrity check -> WiFi off -> BLE -> WiFi on -> MQTT result`

## Installation

1. Copy `custom_components/gicisky_102/` into HA `config/custom_components/`.
2. The integration `fonts/` directory already contains the required fonts.
3. Configure the HA MQTT integration to use the same broker as the ESP32.
4. Copy `firmware/esp32_gicisky_bridge/secrets.example.h` to `secrets.h` and
   set your network credentials, MQTT endpoint, panel ID and BLE MAC.
5. Build the Arduino sketch with ESP32 Arduino core, PubSubClient and
   NimBLE-Arduino 1.4.x (original sketch suggests 1.4.3; unverified here).
6. Add `Gicisky 10.2` under HA Devices & Services and set the same panel ID.
7. Try `examples/simple_write.yaml` and inspect ESP32 serial logs and HA sensors.

## Topics and framing

- `gicisky/<panel>/cmd/push`: sequential MQTT frames (LE uint32 seq, LE uint16 total, data up to 1024 bytes)
- `gicisky/<panel>/result`: retained JSON of last write attempt
- `gicisky/<panel>/battery`: retained JSON battery and RF data
- `gicisky/<panel>/status`: retained online/offline status
- `gicisky/<panel>/cmd/restart`: ESP32 restart command
- The assembled payload: `GCSK` + version byte + LE uint32 length + 16-byte MD5 + data.

## Security and limitations

- Broker connection currently uses plaintext MQTT, so restrict access to a trusted LAN.
- The restart topic lacks application-level authorization: enforce ACLs on broker.
- The payload has no per-request correlation ID; retained results can become stale.
- No end-to-end hardware integration tests have been performed in this snapshot.
- The full original household agenda automation is deliberately **not** included:
  it contains private entity names, calendars, and household details.

## Attribution

This project incorporates substantial components adapted from
[eigger/hass-gicisky](https://github.com/eigger/hass-gicisky), copyright
(c) 2025 eigger, released under the MIT License. Upstream development
continues at [eigger/hass-ble-esl](https://github.com/eigger/hass-ble-esl).
`custom_components/gicisky_102/gicisky_ble/compression.py` is vendored unmodified
from hass-ble-esl (`esl_ble/picksmart/quicklz.py`, commit 84db415), which uses the
panel firmware's 6-bit QuickLZ hash (hass-gicisky PR #78). The vendored renderer should still be reviewed
against an exact upstream revision before a tagged release. This repository is not affiliated
with or endorsed by the upstream maintainers.

## Tests

Encoder, QuickLZ, framing and MQTT chunking tests run without Home Assistant or
hardware:

```sh
python -m venv .venv && .venv/bin/pip install -r requirements-test.txt
.venv/bin/python -m pytest tests
```

## Priorities before release

- Confirm the MDI webfont/metadata license notice is included with the assets.
- Add tests for the HA side (config flow, write lifecycle, entities).
- Correlate writes and results with a unique request ID; handle unexpected
  retained MQTT results, reboots and long write times.
- Validate size/length limits and MQTT chunk reassembly error recovery.
- Test WiFi reconnect failures, panel absence and interrupted BLE writes.
- Run on actual ESP32 and panel before labelling a release stable.
