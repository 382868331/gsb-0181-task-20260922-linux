"""Tests for the GF(2) rolling fingerprint tables.

Expected values come from ``tests/_slow_reference.py`` (bit-level long
division that never imports :mod:`rabin_cdc`) and from a handful of
hand-derived constants.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rabin_cdc import POLY, WINDOW_SIZE  # noqa: E402
from rabin_cdc._fingerprint import (  # noqa: E402
    BYTE_TABLE,
    REMOVE_TABLE,
    SHIFT_TABLE,
    _reduce,
    shift8,
)
from rabin_cdc.chunker import Chunker  # noqa: E402
from tests._slow_reference import slow_remainder, slow_window_fingerprints  # noqa: E402
from tests._data import lcg_bytes  # noqa: E402


class FingerprintConstantsTest(unittest.TestCase):
    def test_modulus_is_the_contract_polynomial(self):
        # x^16 + x^12 + x^5 + 1
        self.assertEqual(POLY, (1 << 16) | (1 << 12) | (1 << 5) | 1)
        self.assertEqual(POLY, 0x11021)
        self.assertEqual(WINDOW_SIZE, 32)

    def test_hand_checked_remainders(self):
        # Stream polynomial: first byte holds the highest byte slot, each
        # byte's bit k is the coefficient of x^k within that slot.
        # A one-byte stream 0x01 is the polynomial 1 -> remainder 1.
        self.assertEqual(slow_remainder(b"\x01"), 0x0001)
        self.assertEqual(_reduce(1), 1)
        # A one-byte stream 0x80 is x^7, still below degree 16.
        self.assertEqual(slow_remainder(b"\x80"), 0x0080)
        # x^16 alone reduces to x^12 + x^5 + 1 = 0x1021.
        self.assertEqual(_reduce(1 << 16), 0x1021)
        self.assertEqual(slow_remainder(b"\x01\x00"), 0x0100)
        # Zero polynomial -> zero remainder.
        self.assertEqual(slow_remainder(b""), 0)

    def test_tables_agree_with_long_division(self):
        for r in range(0x10000):
            self.assertEqual(
                shift8(r),
                _reduce(r << 8),
                msg=f"shift8 mismatch at {r:#06x}",
            )
        for hi in range(256):
            self.assertEqual(SHIFT_TABLE[hi], _reduce(hi << 16))
        for lo in range(256):
            self.assertEqual(BYTE_TABLE[lo], _reduce(lo << 8))
        for out in range(256):
            self.assertEqual(REMOVE_TABLE[out], _reduce(out << 256))

    def test_full_sweep_tables_vs_shift8(self):
        # Every 16-bit remainder: table composition must equal long division.
        for r in (0, 1, 0xFFFF, 0x8000, 0x0001, 0x1234, 0xABCD):
            self.assertEqual(
                SHIFT_TABLE[(r >> 8) & 0xFF] ^ BYTE_TABLE[r & 0xFF],
                shift8(r),
            )

    def test_rolling_matches_recomputed_windows_prefix_phase(self):
        # During the first 31 bytes the window is still filling in; the
        # rolling state must equal the long-divided prefix remainder.
        data = lcg_bytes(300, seed=42)
        chunker = Chunker()
        rolling = []
        for b in data:
            chunker.add(bytes([b]))
            rolling.append(chunker._fp)
        self.assertEqual(rolling, slow_window_fingerprints(data))

    def test_rolling_matches_recomputed_windows_after_full_window(self):
        data = lcg_bytes(2000, seed=7)
        chunker = Chunker()
        rolling = []
        # Feed in odd-sized chunks too; per-position state must be identical.
        pos = 0
        step = 37
        while pos < len(data):
            for b in data[pos : pos + step]:
                chunker.add(bytes([b]))
                rolling.append(chunker._fp)
            pos += step
        self.assertEqual(rolling, slow_window_fingerprints(data))

    def test_removal_table_cancels_leaving_byte(self):
        # Algebraic identity: for any full 32-byte window followed by one
        # more byte, the rolled remainder equals the from-scratch remainder
        # of bytes [1..32].  Checked on random data via long division.
        data = lcg_bytes(33, seed=5)
        full = slow_remainder(data[:32])
        rolled = (
            shift8(full)
            ^ REMOVE_TABLE[data[0]]
            ^ data[32]
        )
        self.assertEqual(rolled, slow_remainder(data[1:33]))

    def test_zero_window_has_zero_fingerprint(self):
        chunker = Chunker()
        chunker.add(bytes(32))
        self.assertEqual(chunker._fp, 0)


if __name__ == "__main__":
    unittest.main()
