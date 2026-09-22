"""Slow, independent reference implementation for tests.

This module deliberately does NOT import or call :mod:`cdc.core`.  It
re-derives everything directly from the mathematical contract:

* fingerprint = polynomial long division over GF(2) of the (zero-left-padded)
  32-byte window by P(x) = x^16 + x^12 + x^5 + 1, recomputed from scratch at
  every stream position (no rolling tables);
* cut rule identical to the contract: min 64 / max 256 bytes, low 6 bits of
  the fingerprint zero, hard cut at max, non-empty tail at end of stream.

Tests compare the optimised core against this reference; agreement is the
acceptance signal, not a self-consistency round trip.
"""

from __future__ import annotations

# P(x) = x^16 + x^12 + x^5 + 1
POLYNOMIAL = (1 << 16) | (1 << 12) | (1 << 5) | 1
POLY_DEGREE = POLYNOMIAL.bit_length() - 1  # 16

WINDOW_SIZE = 32
MIN_SIZE = 64
MAX_SIZE = 256
MASK = 0x3F


def fingerprint(window: bytes) -> int:
    """Remainder of the window polynomial modulo P, by long division.

    ``window`` may be shorter than WINDOW_SIZE; it is treated as
    left-padded with zero bytes (the stream-start prefix rule).
    """
    if not isinstance(window, (bytes, bytearray)):
        raise TypeError("window must be bytes-like")
    if len(window) > WINDOW_SIZE:
        raise ValueError(f"window longer than {WINDOW_SIZE} bytes")
    value = int.from_bytes(bytes(window).rjust(WINDOW_SIZE, b"\x00"), "big")
    for bit in range(value.bit_length() - 1, POLY_DEGREE - 1, -1):
        if (value >> bit) & 1:
            value ^= POLYNOMIAL << (bit - POLY_DEGREE)
    return value


def split(data: bytes) -> list[tuple[int, int, str]]:
    """Reference chunking of ``data``; returns (start, end, reason) triples.

    Recomputes the fingerprint from scratch at every position — O(n * window)
    bit operations, only suitable for test-sized inputs.
    """
    blocks: list[tuple[int, int, str]] = []
    start = 0
    for i in range(1, len(data) + 1):
        fp = fingerprint(data[max(0, i - WINDOW_SIZE):i])
        block_len = i - start
        if block_len >= MIN_SIZE and (fp & MASK) == 0:
            blocks.append((start, i, "fingerprint"))
            start = i
        elif block_len >= MAX_SIZE:
            blocks.append((start, i, "max_size"))
            start = i
    if start < len(data):
        blocks.append((start, len(data), "end_of_stream"))
    return blocks
