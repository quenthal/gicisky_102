# gicisky_102 — Home Assistant integration for the 10.2" Gicisky/PICKSMART panel

A **standalone, BLE-free** custom integration for the one large e‑paper panel that the
upstream `gicisky` integration cannot drive over BLE. Rendering happens **inside Home
Assistant** (real `hass` → `plot`/history and every renderer element work); the image
is encoded and published over MQTT to an **ESP32 bridge** that pushes it to the panel
over BLE.

This integration is deliberately separate from upstream `gicisky`:

- Keep `gicisky` for your small (BLE) panels — untouched.
- Use `gicisky_102` for the 10.2" panel — MQTT only, no BLE, no device table, no
  alternative transport flags.

## What's inside

```
gicisky_102/
  __init__.py          entry setup, the `write` service, status subscription
  manifest.json        domain + mqtt dependency + render requirements
  const.py             domain, geometry, topic/signal helpers
  config_flow.py       UI config (name + panel id)
  mqtt_bridge.py       encoder: compression2 (default) + per-column fallback + publish + subscribe
  render.py            adapter calling the vendored renderer with real hass
  entity.py            shared device_info + dispatcher-fed base entity
  image.py             the panel as an HA image entity (shows last render)
  sensor.py            Akku / Jännite / Signaali / Firmware
  binary_sensor.py     Synkassa / Yhteys
  services.yaml        `write` service schema for the UI
  translations/en.json config flow strings
  renderer.py          VENDORED copy (decoupled from upstream)
  gicisky_ble/
    compression.py     VENDORED from hass-ble-esl picksmart/quicklz.py (6-bit hash)
  fonts/               VENDORED font + MDI icon assets (see below)
```

## Fonts & icons (required — vendored)

The renderer loads font and MDI-icon assets **relative to its own folder**
(`dirname(__file__)`), so they must live in `gicisky_102/fonts/`. Three are
**mandatory** (the renderer has no fallback path for them):

- `NotoSansKR-Regular.ttf` — the default font for all text + progress bars. Includes
  Latin glyphs, so Finnish text renders fine.
- `materialdesignicons-webfont.ttf` — MDI glyphs, for `icon` elements.
- `materialdesignicons-webfont_meta.json` — MDI name→codepoint map, for `icon`.

All three (plus the optional extras below) are bundled in `fonts/`.

Also bundled: the other NotoSansKR weights (Thin … Black), selectable with
`font: fonts/NotoSansKR-Bold.ttf` etc. All bundled fonts are SIL OFL 1.1 and the MDI
assets are Apache 2.0 / OFL, so they can be redistributed.

**Extra fonts** (CookieRun, GmarketSans, OwnglyphParkDaHyun, …) are **not** bundled:
their redistribution terms are unclear. To use one, put the `.ttf` in
`/config/www/fonts/` and reference it by bare file name, e.g. `font: CookieRunBold.ttf`
(the renderer looks there when the name is not found in the integration folder). Do not
drop extras into `gicisky_102/fonts/`: a HACS update replaces that folder.

`tools/fetch_upstream_fonts.py` refreshes the set from upstream. Check each font's
redistribution license before publishing the repository.

## Install

1. Copy the `gicisky_102/` folder to `/config/custom_components/`.
2. Ensure the **MQTT** integration is configured (same broker the ESP bridge uses).
3. Restart Home Assistant.
4. Settings → Devices & Services → Add Integration → **Gicisky 10.2"**. Enter a name
   and the panel id (the MQTT topic key, e.g. `008b`).
5. The default encoding (compression2 on/off) lives in the integration's **Configure**
   dialog and applies immediately -- no restart. A `compress:` value in a service call
   always overrides it for that call.

The ESP32 bridge firmware (`firmware/esp32_gicisky_bridge/`) implements the
MQTT contract: it subscribes to `gicisky/<panel>/cmd/push` and publishes
`gicisky/<panel>/{battery,result,status}`.

## Use

Call the `gicisky_102.write` service with a list of renderer elements:

```yaml
service: gicisky_102.write
data:
  payload:
    - type: text
      value: "Perheen kalenteri"
      x: 24
      y: 16
      size: 40
    - type: plot
      x: 0
      y: 120
      width: 960
      height: 480
      entities:
        - entity: sensor.olohuone_lampotila
          duration: 24
```

`plot` works because rendering runs inside HA with the recorder available. The image
entity (`image.gicisky_10_2`) updates to show whatever was last rendered; the sensors
update whenever the bridge publishes battery/result/status.

## Encoding (proven)

The panel's columns are the image's rows, so the encoder reads the rendered 960×640
image **plain row-major** and the same two 1-bit planes feed both formats. This is
byte-identical to the original column-major pipeline — verified against the working
sender. No rotation or mirroring.

**Compression2/QuickLZ is the default** (`compress: true`, ~45-70 KB / ~185-300 BLE
parts depending on content, vs per-column's 162 564 bytes / 678 parts). Two things
had to be right for the panel to refresh on this format:

- **6-bit QuickLZ hash.** The panel firmware decodes with a 64-bucket hash table
  (`(fetch ^ (fetch >> 12)) & 0x3F`), not stock QuickLZ's 4096. With the stock
  table, simple images mostly survive but dense dashboards decode with thousands of
  wrong pixels. `gicisky_ble/compression.py` is vendored unmodified from
  hass-ble-esl, which carries this fix (hass-gicisky PR #78). Its encoder also
  leaves each chunk's last 16 bytes as literals, which decodes on any firmware
  tail-mode threshold up to 12 (stock QuickLZ uses 6).
- **Bridge commit grace.** The panel signals completion by requesting one part *past*
  the end of the payload; the bridge waits for the final status and holds the link
  through the commit. Compression2 parts go write-without-response (result checked
  and retried), per-column parts write-with-response.

`compress: false` selects the per-column 0x75 format as a fallback.

## Notes

- `plot` requires the **recorder** (default in HA). Other elements need no history.
- Rendering is offloaded to the executor; the MQTT publish loop is light.
- This integration does not connect to the panel over BLE at all — the ESP bridge is
  the only thing that talks BLE.
