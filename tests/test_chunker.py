"""Contract tests for :func:`split_stream` and :class:`Chunker`."""

from __future__ import annotations

import hashlib
import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rabin_cdc import (  # noqa: E402
    MAX_BLOCK_BYTES,
    MAX_TOTAL_BYTES,
    MIN_BLOCK_BYTES,
    REASON_CONTENT,
    REASON_MAX,
    REASON_TAIL,
    Block,
    Chunker,
    InputTooLargeError,
    InvalidBatchTypeError,
    StreamFinishedError,
    split_stream,
)
from tests._slow_reference import slow_split  # noqa: E402
from tests._data import lcg_bytes, mixed_stream  # noqa: E402


def tuples(blocks):
    return [(b.start, b.end, b.size, b.sha256, b.reason) for b in blocks]


class BasicContractTest(unittest.TestCase):
    def test_empty_stream_yields_no_blocks(self):
        self.assertEqual(split_stream([]), [])
        self.assertEqual(split_stream([b"", b"", bytearray()]), [])
        chunker = Chunker()
        self.assertEqual(chunker.add(b""), [])
        self.assertIsNone(chunker.finish())

    def test_short_stream_is_one_tail_block(self):
        blocks = split_stream([b"abc"])
        self.assertEqual(len(blocks), 1)
        block = blocks[0]
        self.assertEqual(block.start, 0)
        self.assertEqual(block.end, 3)
        self.assertEqual(block.size, 3)
        self.assertEqual(
            block.sha256, hashlib.sha256(b"abc").hexdigest()
        )
        self.assertEqual(block.reason, REASON_TAIL)

    def test_blocks_tile_the_stream_with_absolute_offsets(self):
        data = lcg_bytes(5000)
        blocks = split_stream([data])
        self.assertEqual(blocks[0].start, 0)
        cursor = 0
        for block in blocks:
            self.assertEqual(block.start, cursor)
            self.assertEqual(block.end, block.start + block.size)
            self.assertEqual(
                block.sha256,
                hashlib.sha256(data[block.start : block.end]).hexdigest(),
            )
            cursor = block.end
        self.assertEqual(cursor, len(data))

    def test_size_limits_and_reasons(self):
        data = mixed_stream(8000)
        blocks = split_stream([data])
        self.assertTrue(blocks)
        for block in blocks:
            if block.reason == REASON_TAIL:
                self.assertLessEqual(block.size, MAX_BLOCK_BYTES)
            elif block.reason == REASON_MAX:
                self.assertEqual(block.size, MAX_BLOCK_BYTES)
            else:
                self.assertEqual(block.reason, REASON_CONTENT)
                self.assertGreaterEqual(block.size, MIN_BLOCK_BYTES)
                self.assertLessEqual(block.size, MAX_BLOCK_BYTES)

    def test_constant_run_forces_max_cuts(self):
        # 0x01-filled regions never present a zero low-6-bits window, so a
        # long constant region is cut forcibly at 256 bytes.
        data = b"\x01" * 1000
        blocks = split_stream([data])
        self.assertEqual(
            [b.size for b in blocks[:-1]], [256, 256, 256]
        )
        self.assertTrue(all(b.reason == REASON_MAX for b in blocks[:-1]))
        self.assertEqual(blocks[-1].reason, REASON_TAIL)
        self.assertEqual(blocks[-1].size, 232)


class ZeroDataTest(unittest.TestCase):
    """A 32-byte zero window has remainder zero, so zero data cuts content."""

    def test_under_one_block_stays_tail(self):
        for n in (1, 31, 32, 33, 63):
            blocks = split_stream([bytes(n)])
            self.assertEqual(len(blocks), 1, msg=f"n={n}")
            self.assertEqual(blocks[0].size, n)
            self.assertEqual(blocks[0].reason, REASON_TAIL)

    def test_at_minimum_cuts_content_on_zero_window(self):
        blocks = split_stream([bytes(64)])
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0].size, 64)
        self.assertEqual(blocks[0].reason, REASON_CONTENT)

    def test_zero_stream_tiles_in_64_byte_content_blocks(self):
        blocks = split_stream([bytes(600)])
        sizes = [b.size for b in blocks]
        self.assertEqual(sizes, [64] * 9 + [24])
        self.assertTrue(all(b.reason == REASON_CONTENT for b in blocks[:-1]))
        self.assertEqual(blocks[-1].reason, REASON_TAIL)

    def test_matches_independent_reference(self):
        for n in (0, 1, 32, 63, 64, 65, 256, 600):
            data = bytes(n)
            self.assertEqual(tuples(split_stream([data])), tuples(slow_split(data)))


class WindowPrefixTest(unittest.TestCase):
    """The first 32 bytes fill the window in; their prefix remainders must
    be carried exactly into the rolling state used for later cut decisions."""

    def test_stream_crossing_window_fill_boundary_matches_reference(self):
        # Lengths straddling WINDOW_SIZE (32) and the minimum (64): the
        # rolling recurrence is seeded by the no-removal prefix phase, and
        # the independent reference recomputes each window from scratch.
        for n in (1, 2, 31, 32, 33, 63, 64, 65, 128, 300):
            data = lcg_bytes(n, seed=600 + n)
            self.assertEqual(
                tuples(split_stream([data])),
                tuples(slow_split(data)),
                msg=f"n={n}",
            )

    def test_first_content_cut_seed_comes_from_prefix_state(self):
        # Independently search (via long division only) for a 64-byte string
        # whose position-63 window remainder has its low 6 bits clear, then
        # confirm the core makes exactly that cut.
        from tests._slow_reference import slow_window_fingerprints

        prefix = lcg_bytes(63, seed=123)
        candidate = None
        for value in range(256):
            data = prefix + bytes([value])
            if slow_window_fingerprints(data)[63] & 0x3F == 0:
                candidate = data
                break
        self.assertIsNotNone(candidate, "deterministic search must find a byte")
        blocks = split_stream([candidate])
        self.assertEqual(blocks[0].size, 64)
        self.assertEqual(blocks[0].reason, REASON_CONTENT)


class BatchEquivalenceTest(unittest.TestCase):
    """Same bytes in ANY batch grouping -> identical blocks."""

    def _check(self, data):
        baseline = tuples(split_stream([data]))
        groupings = [
            [data],
            [data[i : i + 1] for i in range(len(data))],
            [data[:1], data[1:]],
            [data[:63], data[63:65], data[65:]],
            [b"", data[:10], b"", data[10:]],
            [bytearray(data)],
            [memoryview(data)],
            [memoryview(data)[: len(data) // 2], bytearray(data[len(data) // 2 :])],
        ]
        for grouping in groupings:
            self.assertEqual(
                tuples(split_stream(grouping)),
                baseline,
                msg=f"grouping head: {[len(g) for g in grouping[:4]]}",
            )

    def test_random_data(self):
        self._check(lcg_bytes(3000, seed=11))

    def test_mixed_data(self):
        self._check(mixed_stream(6000))

    def test_zeros(self):
        self._check(bytes(1000))

    def test_agrees_with_slow_reference(self):
        data = mixed_stream(4000)
        self.assertEqual(tuples(split_stream([data])), tuples(slow_split(data)))

    def test_randomized_fuzz_against_slow_reference(self):
        # Many independent pseudo-streams, odd batch boundaries; the rolling
        # core must match the from-scratch long-division reference every time.
        for seed in range(40):
            length = 1 + (seed * 137) % 1500
            data = lcg_bytes(length, seed=1000 + seed)
            groups = [data[i : i + (7 + seed % 53)] for i in range(0, length, 7 + seed % 53)]
            self.assertEqual(
                tuples(split_stream(groups)),
                tuples(slow_split(data)),
                msg=f"seed={seed} length={length}",
            )

    def test_max_cut_takes_priority_over_content(self):
        # At exactly 256 bytes the forced cut wins even when the window
        # fingerprint's low 6 bits happen to be zero.  Construct 255 bytes
        # of 0x01 (a constant run whose window low 6 bits never clear, so no
        # earlier content cut is possible) and pick the last byte so that
        # the position-255 remainder is zero modulo 64.
        from tests._slow_reference import slow_window_fingerprints

        prefix = b"\x01" * 255
        picked = None
        for value in range(256):
            data = prefix + bytes([value])
            if slow_window_fingerprints(data)[255] & 0x3F == 0:
                picked = data
                break
        self.assertIsNot(
            picked, None, "one of 256 last bytes must zero the low 6 bits"
        )
        blocks = split_stream([picked])
        self.assertEqual(blocks[0].size, 256)
        self.assertEqual(blocks[0].reason, REASON_MAX)

    def test_streaming_api_matches_one_shot(self):
        data = lcg_bytes(2500, seed=77)
        chunker = Chunker()
        got = []
        for i in range(0, len(data), 100):
            got.extend(chunker.add(data[i : i + 100]))
        tail = chunker.finish()
        if tail is not None:
            got.append(tail)
        self.assertEqual(tuples(got), tuples(split_stream([data])))


class InsertionRecoveryTest(unittest.TestCase):
    """One inserted byte perturbs only a bounded region; later boundaries
    realign and the affected blocks have identical SHA-256 hashes."""

    def test_insert_one_byte_recovers_identical_blocks(self):
        data = lcg_bytes(8000, seed=2026)
        insert_at = 2000
        modified = data[:insert_at] + b"\xA5" + data[insert_at:]

        original = split_stream([data])
        changed = split_stream([modified])

        # Blocks starting well past the insertion (+32 window + up to 256
        # block) must reappear byte-identical in the modified stream.
        horizon = insert_at + WINDOW_SAFE_MARGIN
        originals = [b for b in original if b.start >= horizon]
        changed_hashes = {b.sha256 for b in changed}
        recovered = [b for b in originals if b.sha256 in changed_hashes]
        self.assertGreater(len(originals), 10)
        self.assertEqual(len(recovered), len(originals))

        # Boundaries after recovery sit exactly one byte later.
        orig_tail_end = originals[0]
        match = next(b for b in changed if b.sha256 == orig_tail_end.sha256)
        self.assertEqual(match.start, orig_tail_end.start + 1)
        self.assertEqual(match.size, orig_tail_end.size)

    def test_insertion_only_changes_blocks_near_insertion(self):
        data = lcg_bytes(6000, seed=55)
        at = 1000
        modified = data[:at] + bytes([0x77]) + data[at:]
        original = split_stream([data])
        changed = split_stream([modified])

        # Blocks ending at or before the insertion point are untouched
        # (chunking is causal); every later block may shift by one byte.
        unchanged_before = [
            b for b in original if b.end <= at
        ]
        changed_hashes = {b.sha256 for b in changed}
        # Blocks fully before the insertion are completely untouched.
        for block in unchanged_before:
            self.assertIn(block.sha256, changed_hashes)

    def test_insertion_matches_independent_reference(self):
        data = lcg_bytes(2000, seed=9)
        modified = data[:500] + b"\x00" + data[500:]
        self.assertEqual(
            tuples(split_stream([modified])), tuples(slow_split(modified))
        )


WINDOW_SAFE_MARGIN = 32 + MAX_BLOCK_BYTES


class FinishSemanticsTest(unittest.TestCase):
    def test_finish_empts_nothing_on_empty(self):
        chunker = Chunker()
        self.assertIsNone(chunker.finish())

    def test_finish_emits_exactly_one_tail(self):
        chunker = Chunker()
        chunker.add(b"\x01" * 300)
        tail = chunker.finish()
        self.assertIsNotNone(tail)
        self.assertEqual(tail.reason, REASON_TAIL)

    def test_finish_is_idempotent_only_in_error(self):
        chunker = Chunker()
        chunker.add(b"x")
        self.assertIsNotNone(chunker.finish())
        with self.assertRaises(StreamFinishedError):
            chunker.finish()
        with self.assertRaises(StreamFinishedError):
            chunker.add(b"y")


class InputValidationTest(unittest.TestCase):
    def test_non_bytes_batch_rejected_with_index(self):
        with self.assertRaises(InvalidBatchTypeError) as ctx:
            split_stream([b"ok", 12345])
        self.assertIn("batch 1", str(ctx.exception))
        self.assertIn("int", str(ctx.exception))

    def test_single_bytes_object_rejected(self):
        with self.assertRaises(InvalidBatchTypeError):
            split_stream(b"raw bytes")

    def test_non_iterable_rejected(self):
        with self.assertRaises(InvalidBatchTypeError):
            split_stream(42)

    def test_none_rejected(self):
        with self.assertRaises(InvalidBatchTypeError):
            split_stream(None)

    def test_string_rejected(self):
        with self.assertRaises(InvalidBatchTypeError):
            split_stream(["abc"])

    def test_bool_is_not_an_integer_batch(self):
        with self.assertRaises(InvalidBatchTypeError):
            split_stream([True])

    def test_wrong_memoryview_format_rejected(self):
        with self.assertRaises(InvalidBatchTypeError):
            split_stream([memoryview(array_ushort())])

    def test_nan_and_infinity_not_silently_accepted(self):
        # No float parameter exists anywhere; floats used as batches must be
        # rejected outright rather than coerced (no silent "correction").
        for bad in (math.nan, math.inf, -math.inf, 1.5):
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidBatchTypeError):
                    split_stream([bad])  # type: ignore[list-item]


def array_ushort():
    import array

    return array.array("H", [1, 2, 3])


class SizeLimitAndAtomicityTest(unittest.TestCase):
    def test_exactly_one_mib_accepted(self):
        data = b"\x01" * MAX_TOTAL_BYTES
        blocks = split_stream([data])
        self.assertEqual(sum(b.size for b in blocks), MAX_TOTAL_BYTES)

    def test_one_byte_over_limit_rejected(self):
        with self.assertRaises(InputTooLargeError):
            split_stream([b"\x01" * (MAX_TOTAL_BYTES + 1)])

    def test_limit_accumulates_across_batches(self):
        chunker = Chunker()
        chunker.add(b"\x01" * (MAX_TOTAL_BYTES - 10))
        with self.assertRaises(InputTooLargeError):
            chunker.add(b"\x01" * 11)

    def test_failed_oversize_batch_leaves_state_unchanged(self):
        # Atomicity: the rejected batch must not be partially consumed.
        chunker = Chunker()
        before = chunker.add(b"abc")
        self.assertEqual(before, [])
        with self.assertRaises(InputTooLargeError):
            chunker.add(b"\x01" * (MAX_TOTAL_BYTES + 1))
        tail = chunker.finish()
        self.assertIsNotNone(tail)
        self.assertEqual(tail.size, 3)
        self.assertEqual(tail.sha256, hashlib.sha256(b"abc").hexdigest())

    def test_failed_type_batch_leaves_state_unchanged(self):
        chunker = Chunker()
        emitted = chunker.add(lcg_bytes(300, seed=3))
        with self.assertRaises(InvalidBatchTypeError):
            chunker.add("not bytes")
        good = lcg_bytes(300, seed=4)
        emitted.extend(chunker.add(good))
        tail = chunker.finish()
        emitted_blocks = emitted + ([tail] if tail else [])
        expected = split_stream([lcg_bytes(300, seed=3), good])
        self.assertEqual(tuples(emitted_blocks), tuples(expected))

    def test_iteration_failure_leaves_no_undocumented_partial_result(self):
        # If the iterable raises mid-way, split_stream propagates the error
        # rather than returning a silently truncated block list.
        def bad_gen():
            yield b"a" * 100
            raise RuntimeError("boom")

        with self.assertRaises(RuntimeError):
            split_stream(bad_gen())


class BlockDataclassTest(unittest.TestCase):
    def test_block_is_frozen(self):
        block = Block(0, 1, 1, "0" * 64, REASON_TAIL)
        with self.assertRaises(Exception):
            block.size = 5  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
