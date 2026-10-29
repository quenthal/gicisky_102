"""MQTT bridge: encode a rendered image and ship it to the ESP32 bridge.

The 10.2" panel cannot be driven over BLE from Home Assistant; an ESP32 holds the
whole payload and pushes it over BLE part-by-part. This module is the HA-side half:

  encode()              rendered PIL image -> panel byte format. Default is
                        compression2/QuickLZ (~45-70 KB, content dependent);
                        per-column 0x75 (162 KB) remains as the fallback
                        (compress: false).
  frame_payload()       prepend [GCSK][ver][len][md5] for the ESP to verify.
  publish_image()       chunk + publish to gicisky/<panel>/cmd/push via HA MQTT.
  async_subscribe()     listen to battery/result/status -> HA dispatcher.

GEOMETRY (proven, byte-validated): the panel's columns are the image's rows, so we
read the image plain row-major and frame each row (WIDTH px = WIDTH/8 bytes) as one
panel column (w_cols = HEIGHT, bpl = WIDTH/8). No rotation/mirror. This is
byte-identical to the original column-major pipeline -- just simpler. The SAME
planes feed compression2, so both formats stay consistent.

COMPRESSION2 STATUS: works. Two causes of "transfers but never refreshes" were
fixed: (1) the panel decodes QuickLZ with a 6-bit (64-bucket) hash, so the
encoder must use the same table size (gicisky_ble/compression.py); (2) the panel signals completion by requesting one part PAST the
end, and the old bridge disconnected at that instant -- mid-decompress. The
patched bridge waits for the final status, holds the link for the commit, and
sends compression2 parts write-without-response with the result checked.
"""
from __future__ import annotations

import hashlib
import json
import logging
import struct

from PIL import Image

from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.components import mqtt

from .const import (
    DEFAULT_COMPRESS,
    PANEL_WIDTH as WIDTH,
    PANEL_HEIGHT as HEIGHT,
    DEFAULT_THRESHOLD,
    DEFAULT_RED_THRESHOLD,
    topic_push,
    topic_battery,
    topic_result,
    topic_status,
    signal_update,
)

_LOGGER = logging.getLogger(__name__)

CHUNK = 1024            # MQTT payload chunk size (matches the ESP reassembler)
FRAME_MAGIC = b"GCSK"
VER_PER_COLUMN = 1
VER_COMPRESSION2 = 2


# --------------------------------------------------------------------------
# Plane packing (shared by per-column AND compression2; planes must match).
# --------------------------------------------------------------------------
def _pack_planes(img: Image.Image, threshold: int, red_threshold: int):
    """Plain row-major pack into the two 1-bit planes (BW + red).
    Returns (byte_data, byte_data_red)."""
    canvas = img.convert("RGB").resize((WIDTH, HEIGHT))
    px = canvas.load()
    byte_data: list[int] = []
    byte_data_red: list[int] = []
    cur = cur_red = 0
    bit = 7
    for y in range(HEIGHT):
        for x in range(WIDTH):
            r, g, b = px[x, y]
            lum = ((r * 38) + (g * 75) + (b * 15)) >> 7
            if lum > threshold:
                cur |= (1 << bit)
            if r > red_threshold and g < red_threshold:
                cur_red |= (1 << bit)
            bit -= 1
            if bit < 0:
                byte_data.append(cur)
                byte_data_red.append(cur_red)
                cur = cur_red = 0
                bit = 7
    if bit != 7:
        byte_data.append(cur)
        byte_data_red.append(cur_red)
    return byte_data, byte_data_red


def build_payload(img: Image.Image, threshold: int = DEFAULT_THRESHOLD,
                  red_threshold: int = DEFAULT_RED_THRESHOLD) -> bytes:
    """Per-column 0x75 payload -- the proven, working format."""
    byte_data, byte_data_red = _pack_planes(img, threshold, red_threshold)
    w_cols = HEIGHT          # panel columns = image rows
    bpl = WIDTH // 8         # bytes per column = row width / 8
    buf = [0, 0, 0, 0]
    for plane in (byte_data, byte_data_red):
        pos = 0
        for _ in range(w_cols):
            buf += [0x75, bpl + 7, bpl, 0, 0, 0, 0]
            buf += plane[pos:pos + bpl]
            pos += bpl
    total = len(buf)
    buf[0] = total & 0xFF
    buf[1] = (total >> 8) & 0xFF
    buf[2] = (total >> 16) & 0xFF
    buf[3] = (total >> 24) & 0xFF
    return bytes(buf)


def build_payload_compression2(img: Image.Image, threshold: int = DEFAULT_THRESHOLD,
                               red_threshold: int = DEFAULT_RED_THRESHOLD,
                               force_raw: bool = False) -> bytes:
    """compression2 payload: SAME planes, then QuickLZ (6-bit hash). Default
    format; ~45-70 KB / ~185-300 parts vs 162 KB / 678 parts per-column."""
    from .gicisky_ble.compression import compress
    byte_data, byte_data_red = _pack_planes(img, threshold, red_threshold)
    raw = bytes(byte_data) + bytes(byte_data_red)
    return bytes(compress(raw, force_raw=force_raw))


def frame_payload(payload: bytes, ver: int) -> bytes:
    """[GCSK][ver:1][len:4 LE][md5:16][payload]."""
    md5 = hashlib.md5(payload).digest()
    return FRAME_MAGIC + bytes([ver]) + struct.pack("<I", len(payload)) + md5 + payload


def encode(img: Image.Image, compress: bool = DEFAULT_COMPRESS,
           threshold: int = DEFAULT_THRESHOLD,
           red_threshold: int = DEFAULT_RED_THRESHOLD) -> bytes:
    """Rendered image -> framed, integrity-checked payload. CPU-bound: run in executor."""
    if compress:
        payload = build_payload_compression2(img, threshold, red_threshold)
        ver = VER_COMPRESSION2
    else:
        payload = build_payload(img, threshold, red_threshold)
        ver = VER_PER_COLUMN
    return frame_payload(payload, ver)


# --------------------------------------------------------------------------
# Publish.
# --------------------------------------------------------------------------
async def publish_image(hass: HomeAssistant, panel: str, img: Image.Image, *,
                        compress: bool = DEFAULT_COMPRESS,
                        threshold: int = DEFAULT_THRESHOLD,
                        red_threshold: int = DEFAULT_RED_THRESHOLD) -> int:
    """Encode (executor) and publish in chunks to cmd/push.
    Each MQTT message = [seq:4 LE][nchunks:2 LE][<=1024 bytes]. Returns nchunks."""
    framed = await hass.async_add_executor_job(
        encode, img, compress, threshold, red_threshold)
    topic = topic_push(panel)
    nchunks = (len(framed) + CHUNK - 1) // CHUNK
    for seq in range(nchunks):
        body = framed[seq * CHUNK:(seq + 1) * CHUNK]
        msg = struct.pack("<IH", seq, nchunks) + body
        await mqtt.async_publish(hass, topic, msg, qos=1, retain=False)
    _LOGGER.debug("published %d bytes in %d chunks to %s", len(framed), nchunks, topic)
    return nchunks


# --------------------------------------------------------------------------
# Subscribe to bridge status -> dispatcher (entities listen).
# --------------------------------------------------------------------------
async def async_subscribe(hass: HomeAssistant, panel: str):
    """Subscribe to battery/result/status; rebroadcast parsed data over the
    dispatcher. Returns a list of unsubscribe callables."""
    sig = signal_update(panel)
    b_t, r_t, s_t = topic_battery(panel), topic_result(panel), topic_status(panel)

    def _message(msg) -> None:
        raw = msg.payload
        try:
            if msg.topic == s_t:
                data = {"status": raw.decode() if isinstance(raw, bytes) else str(raw)}
            else:
                data = json.loads(raw)
        except (ValueError, TypeError) as err:
            _LOGGER.debug("bad payload on %s: %s", msg.topic, err)
            return
        # MQTT subscription callbacks may run off the event loop; hop onto the loop
        # so async_dispatcher_send and all downstream entity updates are thread-safe
        # (HA 2026 + py3.14 raise RuntimeError on off-loop dispatch).
        hass.loop.call_soon_threadsafe(
            async_dispatcher_send, hass, sig, msg.topic, data
        )

    unsubs = []
    for topic in (b_t, r_t, s_t):
        unsubs.append(await mqtt.async_subscribe(hass, topic, _message, qos=1))
    _LOGGER.debug("subscribed to battery/result/status for %s", panel)
    return unsubs
