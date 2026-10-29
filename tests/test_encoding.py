"""Encoder, QuickLZ, framing and MQTT chunking tests (no hardware, no HA)."""
from __future__ import annotations

import asyncio
import hashlib
import random
import struct
import sys

import pytest
from PIL import Image, ImageDraw, ImageFont

import tail_decoder
from conftest import COMPONENT
from gicisky_102 import mqtt_bridge
from gicisky_102.gicisky_ble import compression

FONT = COMPONENT / "fonts" / "NotoSansKR-Regular.ttf"
PLANE_BYTES = 960 * 640 // 8


def _dashboard(seed: int, size: int = 22) -> Image.Image:
    """Dense text + lines + red: the content that broke the 4096-bucket hash."""
    rng = random.Random(seed)
    font = ImageFont.truetype(str(FONT), size)
    image = Image.new("RGB", (960, 640), "white")
    draw = ImageDraw.Draw(image)
    draw.text((24, 10), "Perheen kalenteri  la 25.10.", fill="black",
              font=ImageFont.truetype(str(FONT), 40))
    draw.line([0, 64, 960, 64], fill="red", width=3)
    words = ["Uinti", "Hammaslääkäri", "Kauppa", "Treenit", "Kokous", "Koulu"]
    for col in range(3):
        for row in range(14):
            text = f"{rng.randint(7, 21):02d}:{rng.choice(['00', '30'])} " + " ".join(
                rng.choice(words) for _ in range(2))
            draw.text((10 + col * 320, 75 + row * 39), text,
                      fill=rng.choice(["black"] * 4 + ["red"]), font=font)
    draw.line([(600 + i * 6, 560 - rng.randrange(40)) for i in range(60)],
              fill="black", width=2)
    return image


@pytest.fixture(scope="module")
def planes() -> bytes:
    byte_data, byte_data_red = mqtt_bridge._pack_planes(_dashboard(0), 128, 170)
    return bytes(byte_data) + bytes(byte_data_red)


def test_quicklz_uses_panel_hash_size():
    # The panel firmware decodes with a 64-bucket table (hass-gicisky PR #78).
    assert compression._HASH_VALUES == 64


def test_quicklz_roundtrip_dense_dashboard(planes):
    assert len(planes) == 2 * PLANE_BYTES
    packed = compression.compress(planes)
    assert len(packed) < len(planes) // 2
    assert compression.decompress(packed) == planes


@pytest.mark.parametrize("data", [
    b"\xff" * 1280,
    bytes(range(256)) * 5,
    bytes(random.Random(1).getrandbits(8) for _ in range(1280)),
])
def test_quicklz_roundtrip_edge_content(data):
    assert compression.decompress(compression.compress(data)) == data


def _golden_planes() -> bytes:
    """Deterministic panel-sized planes (no fonts, so no FreeType dependence)."""
    rng = random.Random(102)
    bw = bytearray(b"\xff" * PLANE_BYTES)
    for _ in range(400):  # text-like noise: short runs of mixed bytes
        pos = rng.randrange(PLANE_BYTES - 40)
        for i in range(rng.randrange(4, 40)):
            bw[pos + i] = rng.choice([0x00, 0x0f, 0xf0, 0x3c, 0xc3, 0x81, rng.getrandbits(8)])
    for row in range(110):  # black header band
        bw[row * 120:(row + 1) * 120] = b"\x00" * 120
    red = bytearray(PLANE_BYTES)
    for row in range(540, 640):  # red footer band
        red[row * 120:(row + 1) * 120] = b"\xff" * 120
    return bytes(bw) + bytes(red)


def test_quicklz_output_is_locked():
    # Locks the encoder output: any change to what goes on air fails here, even
    # if it still round-trips. (Production copy with MATCHLEN 6 gave 50434 B,
    # sha256 766f6475...; this is the upstream MATCHLEN 12 encoder.)
    packed = compression.compress(_golden_planes())
    assert len(packed) == 50548
    assert hashlib.sha256(packed).hexdigest() == (
        "08132dade66b5b13f2fdea9a956b42b6b046061d041342ab66b377eca0d8eab4")
    assert compression.decompress(packed) == _golden_planes()


@pytest.mark.parametrize("threshold", [6, 12])
def test_quicklz_survives_decoder_tail_mode(threshold):
    # Stock QuickLZ enters tail mode 6+4 bytes before a chunk's end; the panel's
    # threshold is vendor code. The encoder must decode up to threshold 12.
    planes = _golden_planes()
    assert tail_decoder.decompress(compression.compress(planes), threshold) == planes


def test_quicklz_force_raw(planes):
    raw = compression.compress(planes[:640], force_raw=True)
    assert raw[4] == 0x74
    assert compression.decompress(raw) == planes[:640]


def test_per_column_payload_layout():
    payload = mqtt_bridge.build_payload(Image.new("RGB", (960, 640), "white"))
    assert len(payload) == 162564
    assert struct.unpack_from("<I", payload)[0] == len(payload)
    # 2 planes x 640 lines, each [0x75, 127, 120, 0, 0, 0, 0] + 120 bytes
    assert payload[4:11] == bytes([0x75, 127, 120, 0, 0, 0, 0])
    assert payload[11:131] == b"\xff" * 120          # white -> BW bits set
    red_first = 4 + 640 * 127
    assert payload[red_first + 7:red_first + 127] == b"\x00" * 120


def test_frame_payload_header():
    body = b"hello panel"
    framed = mqtt_bridge.frame_payload(body, mqtt_bridge.VER_COMPRESSION2)
    assert framed[:4] == b"GCSK"
    assert framed[4] == 2
    assert struct.unpack_from("<I", framed, 5)[0] == len(body)
    assert framed[9:25] == hashlib.md5(body).digest()
    assert framed[25:] == body


def test_publish_image_chunks_reassemble():
    sent: list[bytes] = []

    async def async_publish(hass, topic, payload, qos=0, retain=False):
        assert topic == "gicisky/008b/cmd/push" and qos == 1 and not retain
        sent.append(payload)

    class Hass:
        async def async_add_executor_job(self, func, *args):
            return func(*args)

    sys.modules["homeassistant.components.mqtt"].async_publish = async_publish
    image = _dashboard(1)
    nchunks = asyncio.run(mqtt_bridge.publish_image(Hass(), "008b", image))
    assert nchunks == len(sent)
    stream = b""
    for seq, msg in enumerate(sent):
        got_seq, total = struct.unpack_from("<IH", msg)
        assert (got_seq, total) == (seq, nchunks)
        assert len(msg) - 6 <= mqtt_bridge.CHUNK
        stream += msg[6:]
    assert stream == mqtt_bridge.encode(image)
    length = struct.unpack_from("<I", stream, 5)[0]
    assert hashlib.md5(stream[25:25 + length]).digest() == stream[9:25]
