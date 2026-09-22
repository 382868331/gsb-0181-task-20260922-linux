"""Tests for the cdc package.

Expected values come from cdc.reference — an independent, slow,
from-scratch implementation that never calls the core — or directly from
the contract (sizes, offsets, hashes, error types).
"""

import hashlib
import unittest

from cdc import (
    Block,
    Chunker,
    InputTooLargeError,
    InvalidChunkTypeError,
    InvalidParameterError,
    MAX_INPUT_BYTES,
    REASON_END_OF_STREAM,
    REASON_FINGERPRINT,
    REASON_MAX_SIZE,
    StreamClosedError,
    split_stream,
)
from cdc import reference


def gen_bytes(n, seed=0xC0FFEE):
    """Deterministic pseudo-random bytes (SHA-256 counter chain)."""
    out = bytearray()
    counter = 0
    while len(out) < n:
        out += hashlib.sha256(
            seed.to_bytes(8, "little") + counter.to_bytes(8, "little")
        ).digest()
        counter += 1
    return bytes(out[:n])


class TestEmptyAndPrefix(unittest.TestCase):
    def test_zero_data(self):
        self.assertEqual(split_stream([]), [])
        self.assertEqual(split_stream([b""]), [])
        self.assertEqual(split_stream([b"", b"", b""]), [])
        self.assertEqual(Chunker().finish(), [])

    def test_window_prefix_matches_reference(self):
        # Fewer than window_size bytes: window is zero-left-padded.
        for n in (1, 2, 7, 31, 32, 33, 64):
            data = gen_bytes(n, seed=11)
            c = Chunker()
            c.update(data)
            expect = reference.fingerprint(data[-reference.WINDOW_SIZE:])
            self.assertEqual(c.fingerprint, expect, f"n={n}")

    def test_fingerprint_tracks_reference_everywhere(self):
        data = gen_bytes(500, seed=12)
        c = Chunker()
        for i, byte in enumerate(data):
            c.update(bytes([byte]))
            expect = reference.fingerprint(data[max(0, i - 31): i + 1])
            self.assertEqual(c.fingerprint, expect, f"position {i}")


class TestAgainstReference(unittest.TestCase):
    def test_boundaries_and_reasons_match_reference(self):
        for n in (0, 1, 63, 64, 65, 255, 256, 257, 1000, 5000, 20000):
            data = gen_bytes(n, seed=13)
            got = [(b.start, b.end, b.reason) for b in split_stream([data])]
            expect = reference.split(data)
            self.assertEqual(got, expect, f"n={n}")

    def test_window_not_reset_across_blocks(self):
        # The reference never resets its window; if the core reset the
        # window at a cut, boundaries after the first cut would diverge.
        data = gen_bytes(20000, seed=14)
        got = [(b.start, b.end) for b in split_stream([data])]
        expect = [(s, e) for s, e, _ in reference.split(data)]
        self.assertEqual(got, expect)
        self.assertGreater(len(got), 3)  # the test actually crosses cuts


class TestContract(unittest.TestCase):
    def test_intervals_contiguous_and_hashes_correct(self):
        data = gen_bytes(30000, seed=15)
        blocks = split_stream([data])
        self.assertTrue(blocks)
        self.assertEqual(blocks[0].start, 0)
        self.assertEqual(blocks[-1].end, len(data))
        for prev, cur in zip(blocks, blocks[1:]):
            self.assertEqual(prev.end, cur.start)
        for b in blocks:
            self.assertEqual(b.size, b.end - b.start)
            self.assertEqual(
                b.sha256, hashlib.sha256(data[b.start:b.end]).hexdigest()
            )

    def test_block_size_bounds_and_reasons(self):
        data = gen_bytes(50000, seed=16)
        blocks = split_stream([data])
        for b in blocks[:-1]:
            self.assertGreaterEqual(b.size, 64)
            self.assertLessEqual(b.size, 256)
            self.assertIn(b.reason, (REASON_FINGERPRINT, REASON_MAX_SIZE))
            if b.reason == REASON_MAX_SIZE:
                self.assertEqual(b.size, 256)
        tail = blocks[-1]
        self.assertEqual(tail.reason, REASON_END_OF_STREAM)
        self.assertGreaterEqual(tail.size, 1)
        self.assertLessEqual(tail.size, 256)

    def test_max_size_hard_cut_occurs(self):
        # Fixed generated data known to exercise the hard cut.
        data = gen_bytes(50000, seed=16)
        reasons = [b.reason for b in split_stream([data])]
        self.assertIn(REASON_MAX_SIZE, reasons)
        self.assertIn(REASON_FINGERPRINT, reasons)

    def test_chunking_equivalence(self):
        data = gen_bytes(40000, seed=17)
        whole = split_stream([data])
        by_one = split_stream([data[i:i + 1] for i in range(len(data))])
        by_seven = split_stream([data[i:i + 7] for i in range(0, len(data), 7)])
        sizes = (1, 5, 64, 255, 256, 1000, 3, 911)
        mixed_chunks, pos, k = [], 0, 0
        while pos < len(data):
            step = sizes[k % len(sizes)]
            mixed_chunks.append(data[pos:pos + step])
            pos += step
            k += 1
        mixed = split_stream(mixed_chunks)
        bare = split_stream(data)  # single bytes object accepted
        self.assertEqual(whole, by_one)
        self.assertEqual(whole, by_seven)
        self.assertEqual(whole, mixed)
        self.assertEqual(whole, bare)
        # Manual Chunker use agrees with split_stream.
        c = Chunker()
        manual = []
        for i in range(0, len(data), 13):
            manual.extend(c.update(data[i:i + 13]))
        manual.extend(c.finish())
        self.assertEqual(whole, manual)

    def test_determinism(self):
        data = gen_bytes(20000, seed=18)
        self.assertEqual(split_stream([data]), split_stream([data]))

    def test_insert_one_byte_boundaries_resync(self):
        data = gen_bytes(30000, seed=19)
        pos = 5000
        modified = data[:pos] + b"\xAA" + data[pos:]
        blocks_a = split_stream([data])
        blocks_b = split_stream([modified])
        ends_a = [b.end for b in blocks_a]
        ends_b = [b.end for b in blocks_b]
        # Before the insertion the boundaries are identical.
        self.assertEqual(
            [e for e in ends_a if e <= pos], [e for e in ends_b if e <= pos]
        )
        # After window + max_size bytes past the insertion, boundaries must
        # have re-synchronised (shifted by exactly one byte).
        sync_from = pos + 1 + 32 + 256
        tail_a = [e for e in ends_a if e >= sync_from]
        tail_b = [e - 1 for e in ends_b if e - 1 >= sync_from]
        self.assertTrue(tail_a, "expected re-synchronised boundaries")
        self.assertEqual(tail_a, tail_b)
        # The re-synchronised blocks are byte-identical (same SHA-256).
        hash_a = {b.sha256 for b in blocks_a}
        resynced = [b for b in blocks_b if b.end - 1 >= sync_from]
        self.assertTrue(all(b.sha256 in hash_a for b in resynced))


class TestErrors(unittest.TestCase):
    def test_non_bytes_chunk(self):
        with self.assertRaises(InvalidChunkTypeError):
            split_stream(["text"])
        with self.assertRaises(InvalidChunkTypeError):
            split_stream([b"ok", 123])
        # Error message locates the offending chunk.
        try:
            split_stream([b"ok", b"fine", 3.5])
        except InvalidChunkTypeError as exc:
            self.assertIn("chunk 2", str(exc))
        else:
            self.fail("expected InvalidChunkTypeError")
        # InvalidChunkTypeError is a TypeError for isinstance-based handling.
        self.assertTrue(issubclass(InvalidChunkTypeError, TypeError))

    def test_non_iterable_chunks(self):
        with self.assertRaises(InvalidChunkTypeError):
            split_stream(123)

    def test_parameter_validation(self):
        with self.assertRaises(InvalidParameterError):
            Chunker(min_size=True)  # bool rejected
        with self.assertRaises(InvalidParameterError):
            Chunker(max_size=False)
        with self.assertRaises(InvalidParameterError):
            Chunker(min_size=float("nan"))
        with self.assertRaises(InvalidParameterError):
            Chunker(max_size=float("inf"))
        with self.assertRaises(InvalidParameterError):
            Chunker(min_size=64.0)
        with self.assertRaises(InvalidParameterError):
            Chunker(min_size="64")
        with self.assertRaises(InvalidParameterError):
            Chunker(min_size=0)
        with self.assertRaises(InvalidParameterError):
            Chunker(min_size=300, max_size=100)
        with self.assertRaises(InvalidParameterError):
            Chunker(window_size=0)
        with self.assertRaises(InvalidParameterError):
            Chunker(mask=-1)
        with self.assertRaises(InvalidParameterError):
            Chunker(mask=1 << 16)
        self.assertTrue(issubclass(InvalidParameterError, ValueError))

    def test_input_limit_and_atomicity(self):
        c = Chunker()
        c.update(b"hello world")
        with self.assertRaises(InputTooLargeError) as ctx:
            c.update(b"y" * MAX_INPUT_BYTES)
        self.assertIn("1 MiB", str(ctx.exception))
        # Failed update consumed nothing: finish returns exactly the
        # 11 bytes accepted before.
        blocks = c.finish()
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0].size, 11)
        self.assertEqual(
            blocks[0].sha256, hashlib.sha256(b"hello world").hexdigest()
        )

    def test_input_limit_boundary(self):
        # Exactly 1 MiB is accepted.
        c = Chunker()
        blocks = c.update(b"z" * MAX_INPUT_BYTES)
        blocks += c.finish()
        self.assertEqual(sum(b.size for b in blocks), MAX_INPUT_BYTES)
        # One more byte would have failed.
        c2 = Chunker()
        c2.update(b"z" * MAX_INPUT_BYTES)
        with self.assertRaises(InputTooLargeError):
            c2.update(b"!")

    def test_use_after_finish(self):
        c = Chunker()
        c.update(b"data")
        c.finish()
        with self.assertRaises(StreamClosedError):
            c.update(b"more")
        with self.assertRaises(StreamClosedError):
            c.finish()

    def test_split_stream_failure_yields_no_partial_result(self):
        # The exception propagates; no list is returned.
        with self.assertRaises(InvalidChunkTypeError):
            split_stream([b"good", b"also good", None])


if __name__ == "__main__":
    unittest.main()
