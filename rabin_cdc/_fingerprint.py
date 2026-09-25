"""GF(2) polynomial remainder fingerprints used by the chunker.

Mathematical convention
-----------------------
A byte stream ``b0, b1, ...`` is read as one polynomial over GF(2) in arrival
order: the first byte holds the highest powers and each byte is emitted
MSB-first (bit 7 first).  Concretely, after ``n`` bytes::

    F(x) = b0 * x^(8(n-1)) + b1 * x^(8(n-2)) + ... + b_(n-1)

and the fingerprint is the 16-bit remainder modulo

    P(x) = x^16 + x^12 + x^5 + 1          (bit vector ``0x11021``).

A byte value is therefore its own degree-<8 polynomial (bit k of the byte is
the coefficient of x^k).

Rolling 32-byte window (W = 32)
-------------------------------
Let ``f`` be the remainder of the current window polynomial.  When byte
``out`` leaves and byte ``new`` enters::

    f' = (f * x^8 mod P) XOR new XOR remove[out]

with ``remove[out] = out * x^(8*32) mod P = out * x^256 mod P``.  While the
window is still filling in there is no removal term.  ``f * x^8 mod P`` is
split over the high/low byte of ``f``::

    f*x^8 = hi * x^16 + lo * x^8

which makes every per-byte update two table lookups (plus the removal
lookup once the window is full) and XORs -- the window polynomial is never
re-long-divided at a position.

The tables themselves are built once at import time by bit-level GF(2) long
division; no third-party fingerprint implementation is used.
"""

from __future__ import annotations

from array import array

__all__ = [
    "POLY",
    "WINDOW_SIZE",
    "SHIFT_TABLE",
    "BYTE_TABLE",
    "REMOVE_TABLE",
    "shift8",
    "stream_remainder",
]

#: Modulus P(x) = x^16 + x^12 + x^5 + 1, bit 16 included for long division.
POLY = 0x11021

#: Rolling window length in bytes, fixed by the task contract.
WINDOW_SIZE = 32

_FINGERPRINT_MASK = 0xFFFF
_WINDOW_BIT_SHIFT = 8 * WINDOW_SIZE  # 256


def _reduce(value: int) -> int:
    """Reduce any non-negative integer polynomial modulo :data:`POLY`.

    Schoolbook long division over GF(2), one bit at a time, MSB first.  Used
    only while building the tables and in the independent test reference.
    """
    for bit in range(value.bit_length() - 1, 15, -1):
        if value & (1 << bit):
            value ^= POLY << (bit - 16)
    return value & _FINGERPRINT_MASK


def _mul_mod(a: int, b: int) -> int:
    """Multiply two reduced polynomials modulo P (table builder only)."""
    product = 0
    for bit in range(16):
        if b & (1 << bit):
            product ^= a << bit
    return _reduce(product)


def _build_shift_table() -> array:
    """T1[hi] = hi * x^16 mod P, for the high byte hi of a fingerprint."""
    table = array("H", [0]) * 256
    for hi in range(256):
        table[hi] = _reduce(hi << 16)
    return table


def _build_byte_table() -> array:
    """T2[lo] = lo * x^8 mod P, for the low byte lo of a fingerprint."""
    table = array("H", [0]) * 256
    for lo in range(256):
        table[lo] = _reduce(lo << 8)
    return table


def _build_remove_table() -> array:
    """T3[out] = out * x^256 mod P for a byte leaving a 32-byte window."""
    # x^256 mod P via repeated squaring of x (x is the 16-bit value 0x0002).
    x_pow = 0x0002
    x_256 = 1
    exponent = _WINDOW_BIT_SHIFT
    while exponent:
        if exponent & 1:
            x_256 = _mul_mod(x_256, x_pow)
        x_pow = _mul_mod(x_pow, x_pow)
        exponent >>= 1
    table = array("H", [0]) * 256
    for out in range(256):
        table[out] = _mul_mod(out, x_256)
    return table


SHIFT_TABLE: array = _build_shift_table()
BYTE_TABLE: array = _build_byte_table()
REMOVE_TABLE: array = _build_remove_table()


def shift8(fingerprint: int) -> int:
    """Return ``fingerprint * x^8 mod P`` using the lookup tables."""
    return SHIFT_TABLE[(fingerprint >> 8) & 0xFF] ^ BYTE_TABLE[fingerprint & 0xFF]


def stream_remainder(data: bytes) -> int:
    """Remainder of ``data``'s full polynomial modulo P, computed directly.

    The whole byte string is reduced in one shot by bit-level long
    division (first byte holds the highest powers, MSB first); the rolling
    update tables are never consulted.  This is the reference path used to
    validate exported boundary contexts: a stored fingerprint is trusted
    only if it equals the remainder recomputed straight from the window
    bytes.
    """
    return _reduce(int.from_bytes(data, "big"))
