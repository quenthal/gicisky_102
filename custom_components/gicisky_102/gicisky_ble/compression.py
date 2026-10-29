# Vendored from eigger/hass-ble-esl custom_components/ble_esl/esl_ble/picksmart/quicklz.py
# (commit 84db415, 2026-09-27), MIT License, Copyright (c) 2025 eigger. Unmodified.
# 6-bit (64-bucket) hash as the panel firmware decodes it (hass-gicisky PR #78).
# Chosen over the production copy (MATCHLEN 6, ~0.6 % smaller): with
# _UNCONDITIONAL_MATCHLEN_COMPRESSOR = 12 no match is emitted in a chunk's last
# 16 bytes, so the output also decodes on a firmware whose QuickLZ tail mode
# starts earlier than stock (MATCHLEN 6 output breaks on such a decoder).
"""QuickLZ level 1, as the PickSmart firmware decodes it (6-bit hash, 64-byte chunks)."""

from __future__ import annotations

import struct

_CWORD_LEN = 4
_HASH_VALUES = 64  # Vendor firmware uses 6-bit hash (0x3F). Stock QuickLZ is 4096.
_NO_ENTRY = -1
_MINOFFSET = 2
_UNCONDITIONAL_MATCHLEN_COMPRESSOR = 12
_UNCOMPRESSED_END = 4
_CHUNK_SIZE = 64


def _hash_func(fetch: int) -> int:
    return ((fetch >> 12) ^ fetch) & (_HASH_VALUES - 1)


def _fast_read_3(data: bytes | bytearray, pos: int) -> int:
    """Read 3 bytes as little-endian uint24."""
    if pos + 3 > len(data):
        return 0
    return data[pos] | (data[pos + 1] << 8) | (data[pos + 2] << 16)


def _same(data: bytes | bytearray, pos: int, n: int) -> bool:
    """Check if n+1 bytes starting from pos are identical."""
    if pos < 0 or pos + n >= len(data):
        return False
    v = data[pos]
    for i in range(1, n + 1):  # noqa: SIM110  (hot path; generator form is slower)
        if data[pos + i] != v:
            return False
    return True


def _qlz_compress_core(source: bytes | bytearray) -> bytes | None:
    """QuickLZ Level 1 core compression. Returns None if no compression gain."""
    size = len(source)
    last_byte_idx = size - 1
    last_matchstart = last_byte_idx - _UNCONDITIONAL_MATCHLEN_COMPRESSOR - _UNCOMPRESSED_END

    if last_matchstart < 0:
        return None

    out = bytearray(size * 2 + 400)
    cword_ptr = 0
    dst = _CWORD_LEN
    cword_val = 1 << 31
    src = 0
    lits = 0

    h_offset = [_NO_ENTRY] * _HASH_VALUES
    h_cache = [0] * _HASH_VALUES

    while src <= last_matchstart:
        if (cword_val & 1) == 1:
            if src > (size >> 1) and (dst > src - (src >> 5)):
                return None
            struct.pack_into("<I", out, cword_ptr, (cword_val >> 1) | (1 << 31))
            cword_ptr = dst
            dst += _CWORD_LEN
            cword_val = 1 << 31

        fetch = _fast_read_3(source, src)
        h = _hash_func(fetch)
        cached = fetch ^ h_cache[h]
        h_cache[h] = fetch
        o = h_offset[h]
        h_offset[h] = src

        dist = src - o
        if (
            (cached & 0xFFFFFF) == 0
            and o != _NO_ENTRY
            and (
                dist > _MINOFFSET
                or (src == o + 1 and lits >= 3 and src > 3 and _same(source, src - 3, 6))
            )
        ):
            matchlen = 3
            remaining = min(255, last_byte_idx - _UNCOMPRESSED_END - src + 1)
            while matchlen < remaining and source[src + matchlen] == source[o + matchlen]:
                matchlen += 1

            h_shifted = h << 4
            cword_val = (cword_val >> 1) | (1 << 31)

            if matchlen < 18:
                val = (matchlen - 2) | h_shifted
                out[dst] = val & 0xFF
                out[dst + 1] = (val >> 8) & 0xFF
                dst += 2
            else:
                out[dst] = h_shifted & 0xFF
                out[dst + 1] = (h_shifted >> 8) & 0xFF
                out[dst + 2] = matchlen & 0xFF
                dst += 3

            src += matchlen
            lits = 0
        else:
            lits += 1
            out[dst] = source[src]
            src += 1
            dst += 1
            cword_val = cword_val >> 1

    while src <= last_byte_idx:
        if (cword_val & 1) == 1:
            struct.pack_into("<I", out, cword_ptr, (cword_val >> 1) | (1 << 31))
            cword_ptr = dst
            dst += _CWORD_LEN
            cword_val = 1 << 31

        if src <= last_byte_idx - 2:
            f = _fast_read_3(source, src)
            hh = _hash_func(f)
            h_cache[hh] = f
            h_offset[hh] = src

        out[dst] = source[src]
        src += 1
        dst += 1
        cword_val = cword_val >> 1

    while (cword_val & 1) != 1:
        cword_val = cword_val >> 1
    struct.pack_into("<I", out, cword_ptr, (cword_val >> 1) | (1 << 31))

    compressed_size = dst
    if compressed_size >= size:
        return None

    return bytes(out[:compressed_size])


def _update_hash(
    out: bytearray,
    hash_offset: list[int],
    last_hashed: int,
    limit: int,
    dest_size: int,
) -> int:
    while last_hashed < limit:
        pos = last_hashed + 1
        if pos + 3 > dest_size:
            break
        fh = out[pos] | (out[pos + 1] << 8) | (out[pos + 2] << 16)
        hh = _hash_func(fh)
        hash_offset[hh] = pos
        last_hashed = pos
    return last_hashed


def _qlz_decompress_core(stream: bytes, dest_size: int) -> bytes:
    out = bytearray(dest_size)
    dst = 0
    src = 0
    n = len(stream)
    last_dst = dest_size - 1
    cword_val = 1

    hash_offset: list[int] = [_NO_ENTRY] * _HASH_VALUES
    last_hashed = -1

    while dst <= last_dst:
        if cword_val == 1:
            if src + _CWORD_LEN > n:
                break
            cword_val = struct.unpack("<I", stream[src : src + _CWORD_LEN])[0]
            src += _CWORD_LEN

        if (cword_val & 1) == 1:
            cword_val >>= 1
            if src + 2 > n:
                break
            fetch_lo = stream[src] | (stream[src + 1] << 8)
            h = (fetch_lo >> 4) & (_HASH_VALUES - 1)
            matchlen_indicator = fetch_lo & 0xF

            if matchlen_indicator != 0:
                matchlen = matchlen_indicator + 2
                src += 2
            else:
                if src + 3 > n:
                    break
                matchlen = stream[src + 2]
                src += 3

            safe_limit = dst - 3
            if safe_limit > last_hashed:
                last_hashed = _update_hash(out, hash_offset, last_hashed, safe_limit, dest_size)

            offset2 = hash_offset[h]
            match_start = dst
            for _ in range(matchlen):
                if dst > last_dst:
                    break
                if 0 <= offset2 < dest_size:
                    out[dst] = out[offset2]
                offset2 += 1
                dst += 1

            last_hashed = _update_hash(out, hash_offset, last_hashed, match_start, dest_size)
            last_hashed = dst - 1
        else:
            cword_val >>= 1
            if src >= n:
                break
            out[dst] = stream[src]
            dst += 1
            src += 1

    return bytes(out[:dest_size])


def _compress_chunked(data: bytes, force_raw: bool = False) -> bytes:
    output = bytearray()
    for i in range(0, len(data), _CHUNK_SIZE):
        chunk = data[i : i + _CHUNK_SIZE]
        n = len(chunk)
        compressed = None if force_raw else _qlz_compress_core(chunk)
        if compressed is not None:
            total_len = 3 + len(compressed)
            output.append(0x75)
            output.append(total_len & 0xFF)
            output.append(n & 0xFF)
            output.extend(compressed)
        else:
            total_len = 3 + n
            output.append(0x74)
            output.append(total_len & 0xFF)
            output.append(n & 0xFF)
            output.extend(chunk)
    return bytes(output)


def compress(data: bytes, force_raw: bool = False) -> bytes:
    """Compress data using QuickLZ Level 1 chunked encoding."""
    total_len = len(data)
    split = total_len // 2
    part1 = data[:split]
    part2 = data[split:]
    compressed_part1 = _compress_chunked(part1, force_raw=force_raw)
    compressed_part2 = _compress_chunked(part2, force_raw=force_raw)
    header = struct.pack("<I", len(part2))
    return header + compressed_part1 + compressed_part2


def _decompress_chunks(payload: bytes, start: int, max_bytes: int) -> tuple[bytes, int]:
    out = bytearray()
    pos = start
    while pos + 3 <= len(payload) and len(out) < max_bytes:
        magic = payload[pos]
        if magic not in (0x74, 0x75):
            pos += 1
            continue
        total_len = payload[pos + 1]
        if total_len < 6 or total_len > 80:
            pos += 1
            continue
        if pos + total_len > len(payload):
            break
        uncompressed_size = payload[pos + 2]
        if uncompressed_size == 0 or uncompressed_size > _CHUNK_SIZE:
            pos += 1
            continue
        stream = payload[pos + 3 : pos + total_len]
        if magic == 0x74:
            out.extend(stream[:uncompressed_size])
        else:
            chunk = _qlz_decompress_core(stream, uncompressed_size)
            out.extend(chunk)
        pos += total_len
    return bytes(out[:max_bytes]), pos


def decompress(payload: bytes) -> bytes:
    """Decompress QuickLZ Level 1 chunked payload."""
    if len(payload) < 4:
        return b""
    part2_len = struct.unpack("<I", payload[:4])[0]
    if part2_len == 0:
        part2_len = len(payload)
    part1_len = part2_len
    part1, next_pos = _decompress_chunks(payload, 4, part1_len)
    part2, _ = _decompress_chunks(payload, next_pos, part2_len)
    return part1 + part2
