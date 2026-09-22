"""Deterministic sample data shared by the tests (no external files)."""

from __future__ import annotations


def lcg_bytes(length: int, seed: int = 123456789) -> bytes:
    """glibc-style LCG stream: deterministic, dependency-free pseudo-data."""
    x = seed & 0x7FFFFFFF
    out = bytearray(length)
    for i in range(length):
        x = (x * 1103515245 + 12345) & 0x7FFFFFFF
        out[i] = (x >> 16) & 0xFF
    return bytes(out)


def mixed_stream(length: int = 4096, seed: int = 99) -> bytes:
    """Low-entropy constant runs interleaved with LCG data.

    Constant byte runs never satisfy the low-6-bit-zero window test for the
    bytes 0x01/0x55 used here (fingerprint stays a fixed nonzero residue),
    so long enough runs force ``max`` cuts; LCG regions give ``content``
    cuts and stream leftovers give ``tail`` blocks.
    """
    rng = lcg_bytes(length, seed)
    parts: list[bytes] = []
    pos = 0
    rpos = 0
    value = 0x01
    while pos < length:
        run = min(700, length - pos)
        parts.append(bytes([value]) * run)
        pos += run
        if pos < length:
            take = min(333, length - pos)
            parts.append(rng[rpos : rpos + take])
            rpos += take
            pos += take
        value = 0x55 if value == 0x01 else 0x01
    return b"".join(parts)
