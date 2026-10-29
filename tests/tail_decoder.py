"""Stock QuickLZ 1.5 level-1 decoder model, including its tail mode.

Once a literal is met at dst >= last byte - threshold - UNCOMPRESSED_END, the
rest of the chunk is copied as literals and control-word bits are ignored. A
decoder whose threshold is larger than the encoder's MATCHLEN misreads any match
token after that point; the panel's exact threshold is unknown (vendor code).
"""
from __future__ import annotations

import struct

from gicisky_102.gicisky_ble import compression as _q

_UNCOMPRESSED_END = 4


def _core(stream: bytes, dest_size: int, threshold: int) -> bytes:
    out = bytearray(dest_size)
    dst = src = 0
    last = dest_size - 1
    last_matchstart = last - threshold - _UNCOMPRESSED_END
    cword = 1
    hash_offset = [-1] * _q._HASH_VALUES
    last_hashed = -1
    while dst <= last:
        if cword == 1:
            if src + 4 > len(stream):
                break
            cword = struct.unpack_from("<I", stream, src)[0]
            src += 4
        if cword & 1:
            cword >>= 1
            fetch = stream[src] | (stream[src + 1] << 8)
            h = (fetch >> 4) & (_q._HASH_VALUES - 1)
            if fetch & 0xF:
                matchlen = (fetch & 0xF) + 2
                src += 2
            else:
                matchlen = stream[src + 2]
                src += 3
            if dst - 3 > last_hashed:
                last_hashed = _q._update_hash(out, hash_offset, last_hashed, dst - 3, dest_size)
            offset = hash_offset[h]
            match_start = dst
            for _ in range(matchlen):
                if dst > last:
                    break
                if 0 <= offset < dest_size:
                    out[dst] = out[offset]
                offset += 1
                dst += 1
            last_hashed = _q._update_hash(out, hash_offset, last_hashed, match_start, dest_size)
            last_hashed = dst - 1
        elif dst < last_matchstart:
            out[dst] = stream[src]
            dst += 1
            src += 1
            cword >>= 1
        else:  # tail mode: literals to the end, cword bits ignored
            while dst <= last and src < len(stream):
                if cword == 1:
                    src += 4
                    cword = 1 << 31
                out[dst] = stream[src]
                dst += 1
                src += 1
                cword >>= 1
            break
    return bytes(out)


def _chunks(payload: bytes, pos: int, max_bytes: int, threshold: int) -> tuple[bytes, int]:
    out = bytearray()
    while pos + 3 <= len(payload) and len(out) < max_bytes:
        magic, total_len, size = payload[pos], payload[pos + 1], payload[pos + 2]
        body = payload[pos + 3:pos + total_len]
        out += body[:size] if magic == 0x74 else _core(body, size, threshold)
        pos += total_len
    return bytes(out[:max_bytes]), pos


def decompress(payload: bytes, threshold: int) -> bytes:
    """Decode a compression2 payload as a decoder with this tail threshold would."""
    part_len = struct.unpack_from("<I", payload)[0]
    first, pos = _chunks(payload, 4, part_len, threshold)
    second, _ = _chunks(payload, pos, part_len, threshold)
    return first + second
