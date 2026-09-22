"""Independent slow reference used ONLY by the test-suite.

This module deliberately does not import :mod:`rabin_cdc` at all: every
expected value is re-derived here from the raw mathematical specification
(bit-level GF(2) polynomial long division, re-reducing the whole window at
each position).  A passing test therefore compares two independent
implementations rather than the core against a copy of itself.

Spec implemented here:
    P(x)        = x^16 + x^12 + x^5 + 1   (0x11021)
    window      = 32 bytes, bytes emitted MSB-first
    block size  = 64..256, cut when rem & 0x3F == 0 (len >= 64),
                  forced cut at 256, tail block for the non-empty remainder
    window      = global to the stream, not reset at block boundaries
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import List, Optional, Sequence

POLY = 0x11021
WINDOW = 32
MIN_BLOCK = 64
MAX_BLOCK = 256
CUT_MASK = 0x3F


@dataclass(frozen=True)
class RefBlock:
    start: int
    end: int
    size: int
    sha256: str
    reason: str


def slow_remainder(data: Sequence[int]) -> int:
    """Remainder of ``data``'s full polynomial modulo P by long division.

    The first byte holds the highest powers; bit 7 of each byte is the next
    coefficient in stream order.
    """
    value = 0
    for b in data:
        value = (value << 8) | b
    bit = value.bit_length() - 1
    while bit >= 16:
        if value & (1 << bit):
            value ^= POLY << (bit - 16)
        bit -= 1
    return value & 0xFFFF


def slow_window_fingerprints(data: bytes) -> List[int]:
    """Recompute the window remainder from scratch at every byte position.

    Positions before the window is full use the prefix itself.  This is
    O(n * W) big-integer work -- intentionally simple and independent of
    the rolling tables.
    """
    out: List[int] = []
    for i in range(len(data)):
        lo = max(0, i - WINDOW + 1)
        out.append(slow_remainder(data[lo : i + 1]))
    return out


def slow_split(data: bytes) -> List[RefBlock]:
    """Chunk ``data`` by scanning freshly long-divided window remainders."""
    blocks: List[RefBlock] = []
    block_start = 0
    fps = slow_window_fingerprints(data)
    total = len(data)
    i = 0
    while i < total:
        block_len = i - block_start + 1  # length after absorbing data[i]
        cut: Optional[str] = None
        if block_len >= MAX_BLOCK:
            cut = "max"
        elif block_len >= MIN_BLOCK and (fps[i] & CUT_MASK) == 0:
            cut = "content"
        if cut is not None:
            payload = data[block_start : i + 1]
            blocks.append(
                RefBlock(
                    start=block_start,
                    end=i + 1,
                    size=len(payload),
                    sha256=hashlib.sha256(payload).hexdigest(),
                    reason=cut,
                )
            )
            block_start = i + 1
        i += 1
    if block_start < total:
        payload = data[block_start:]
        blocks.append(
            RefBlock(
                start=block_start,
                end=total,
                size=len(payload),
                sha256=hashlib.sha256(payload).hexdigest(),
                reason="tail",
            )
        )
    return blocks
