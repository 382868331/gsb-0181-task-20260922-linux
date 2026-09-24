"""Tests for :func:`replace_region` (middle-of-object replacement mapping).

Expected values come from three independent sources, never from the code
under test:

* hand-computed cases on all-zero streams (whose block geometry is fixed:
  64-byte ``content`` blocks plus one tail);
* analytic relations (partition of old/new blocks, offset shifts of prefix
  vs. recovered suffix blocks relative to the length delta);
* ``tests/_slow_reference.py`` (bit-level long division, independent of
  ``rabin_cdc``) for the edited chunkings, together with a small
  dynamic-programming LCS alignment written independently in this file to
  derive the expected kept/replaced/added mapping.
"""

from __future__ import annotations

import hashlib
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rabin_cdc import (  # noqa: E402
    MAX_BLOCK_BYTES,
    MAX_TOTAL_BYTES,
    REASON_CONTENT,
    REASON_TAIL,
    CDCError,
    InputTooLargeError,
    InvalidBatchTypeError,
    InvalidEditError,
    KeptBlock,
    ReplaceResult,
    replace_region,
    split_stream,
)
from tests._slow_reference import slow_split  # noqa: E402
from tests._data import lcg_bytes  # noqa: E402

WINDOW = 32
ZERO64_HASH = hashlib.sha256(b"\x00" * 64).hexdigest()


def ref_tuples(blocks):
    return [(b.start, b.end, b.size, b.sha256, b.reason) for b in blocks]


def edit(data, start, end, replacement):
    return data[:start] + replacement + data[end:]


def dp_align(old_hashes, new_hashes):
    """Independently derive an order-preserving pairing via LCS DP.

    Returns (pairs, replaced_old_indices, added_new_indices).  Used only on
    streams whose hashes are unique *within each side*, so every LCS tie is
    immaterial.
    """
    n, m = len(old_hashes), len(new_hashes)
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        for j in range(m - 1, -1, -1):
            if old_hashes[i] == new_hashes[j]:
                dp[i][j] = dp[i + 1][j + 1] + 1
            else:
                dp[i][j] = max(dp[i + 1][j], dp[i][j + 1])
    pairs = []
    i = j = 0
    while i < n and j < m:
        if old_hashes[i] == new_hashes[j]:
            pairs.append((i, j))
            i += 1
            j += 1
        elif dp[i + 1][j] >= dp[i][j + 1]:
            i += 1
        else:
            j += 1
    paired_old = {i for i, _ in pairs}
    paired_new = {j for _, j in pairs}
    return (
        pairs,
        [i for i in range(n) if i not in paired_old],
        [j for j in range(m) if j not in paired_new],
    )


def assert_partition(testcase, result):
    """Structural invariants any ReplaceResult must satisfy."""
    old = list(result.old_blocks)
    new = list(result.new_blocks)

    def by_start(blocks):
        return sorted(blocks, key=lambda b: b.start)

    # kept + replaced partitions the old chunking (with multiplicity);
    # kept twins + added partitions the new chunking.
    kept_old = [k.old for k in result.kept]
    testcase.assertEqual(by_start(kept_old + list(result.replaced)), old)
    kept_new = [k.new for k in result.kept]
    testcase.assertEqual(by_start(kept_new + list(result.added)), new)

    # kept is reported in old stream order, with strictly increasing old
    # positions and strictly increasing new positions (monotone alignment).
    old_starts = [k.old.start for k in result.kept]
    new_starts = [k.new.start for k in result.kept]
    testcase.assertEqual(old_starts, sorted(old_starts))
    testcase.assertEqual(new_starts, sorted(new_starts))
    testcase.assertEqual(len(set(old_starts)), len(old_starts))
    testcase.assertEqual(len(set(new_starts)), len(new_starts))

    for kept in result.kept:
        testcase.assertIsInstance(kept, KeptBlock)
        testcase.assertEqual(kept.old.sha256, kept.new.sha256)
        testcase.assertEqual(kept.old.size, kept.new.size)
        testcase.assertEqual(kept.sha256, kept.old.sha256)
        testcase.assertEqual(
            kept.offset_shift, kept.new.start - kept.old.start
        )

    # Old and new block lists each tile their object.
    for blocks, size in ((old, result.old_size), (new, result.new_size)):
        testcase.assertEqual(sum(b.size for b in blocks), size)
        cursor = 0
        for block in blocks:
            testcase.assertEqual(block.start, cursor)
            cursor = block.end
        testcase.assertEqual(cursor, size)

    testcase.assertEqual(
        result.new_size - result.old_size,
        len(result.replacement) - (result.end - result.start),
    )


class HandCheckedZeroTests(unittest.TestCase):
    """Zero streams cut into 64-byte content blocks plus one tail: the
    entire block geometry and the duplicate-content mapping is hand-counted."""

    def test_delete_one_whole_zero_block_in_middle(self):
        data = bytes(600)  # 9 * 64 + 24
        # Block 4 is exactly [256, 320): delete the whole block on a
        # boundary, so the zero geometry stays hand-countable.
        result = replace_region(data, 256, 320, b"")
        self.assertEqual(result.old_size, 600)
        self.assertEqual(result.new_size, 536)  # 8 * 64 + 24

        old_z = [b for b in result.old_blocks if b.sha256 == ZERO64_HASH]
        new_z = [b for b in result.new_blocks if b.sha256 == ZERO64_HASH]
        self.assertEqual(len(old_z), 9)
        self.assertEqual(len(new_z), 8)
        self.assertTrue(all(b.reason == REASON_CONTENT for b in old_z))

        kept_z = [k for k in result.kept if k.sha256 == ZERO64_HASH]
        self.assertEqual(len(kept_z), 8)
        self.assertEqual(len(result.replaced), 1)
        self.assertEqual(result.replaced[0].sha256, ZERO64_HASH)
        self.assertEqual(result.added, ())

        # The 24-byte tail survives (same content), shifted by -64.
        tail_kept = [k for k in result.kept if k.old.reason == REASON_TAIL]
        self.assertEqual(len(tail_kept), 1)
        self.assertEqual(tail_kept[0].old.size, 24)
        self.assertEqual(tail_kept[0].offset_shift, -64)
        assert_partition(self, result)

    def test_insert_one_zero_byte_changes_only_the_tail(self):
        data = bytes(600)
        result = replace_region(data, 300, 300, b"\x00")
        self.assertEqual(result.new_size, 601)  # 9 * 64 + 25

        kept_z = [k for k in result.kept if k.sha256 == ZERO64_HASH]
        self.assertEqual(len(kept_z), 9)
        self.assertEqual(len(result.replaced), 1)
        self.assertEqual(result.replaced[0].size, 24)
        self.assertEqual(len(result.added), 1)
        self.assertEqual(result.added[0].size, 25)
        self.assertEqual(result.added[0].reason, REASON_TAIL)

        # Blocks strictly before the insertion do not move at all.
        for kept in result.kept:
            if kept.old.end <= 300:
                self.assertEqual(kept.offset_shift, 0)

    def test_duplicate_identical_blocks_have_one_stable_identity(self):
        # Four identical 64-zero blocks at the head: all share one hash and
        # all survive a far-away append; copies are paired one-to-one.
        data = bytes(256) + lcg_bytes(900, seed=31)
        result = replace_region(data, len(data), len(data), b"\x00" * 10)
        zero_kept = [k for k in result.kept if k.sha256 == ZERO64_HASH]
        self.assertEqual(len(zero_kept), 4)
        shifts = sorted(k.offset_shift for k in zero_kept)
        self.assertEqual(shifts, [0, 0, 0, 0])


class BoundaryEditTests(unittest.TestCase):
    """Head/tail edits and edits straddling chunk boundaries."""

    def test_head_insertion_recovers_with_constant_shift(self):
        data = lcg_bytes(6000, seed=101)
        delta = 5
        result = replace_region(data, 0, 0, b"\x5A" * delta)
        self.assertEqual(result.length_delta, delta)
        assert_partition(self, result)

        horizon = WINDOW + MAX_BLOCK_BYTES
        far = [
            k
            for k in result.kept
            if k.old.start >= horizon
        ]
        self.assertGreater(len(far), 5)
        for kept in far:
            self.assertEqual(kept.offset_shift, delta)

    def test_tail_edit_keeps_every_earlier_offset(self):
        data = lcg_bytes(6000, seed=102)
        cut = 5200
        rep = b"\xCC" * 13
        result = replace_region(data, cut, len(data), rep)
        assert_partition(self, result)
        # Nothing before the tail edit moves; only blocks intersecting the
        # edited suffix [cut, new_end) can be replaced or added.  Chunking of
        # the unchanged prefix [0, cut) is deterministic, so every replaced
        # old block must overlap cut and every kept block keeps offset 0.
        new_end = cut + len(rep)
        for kept in result.kept:
            self.assertEqual(kept.offset_shift, 0)
        for block in result.replaced:
            self.assertGreater(block.end, cut)
        for block in result.added:
            self.assertGreater(block.end, cut)
            self.assertLessEqual(block.start, new_end)

    def test_edit_straddling_a_chunk_boundary(self):
        data = lcg_bytes(5000, seed=103)
        old_blocks = split_stream([data])
        boundary = old_blocks[3].end  # a real content/max boundary
        result = replace_region(
            data, boundary - 1, boundary + 1, b"\x7E\x9D\x31"
        )
        self.assertEqual(result.length_delta, 1)
        assert_partition(self, result)

        # Blocks ending at or before the edit start are causally untouched.
        for kept in result.kept:
            if kept.old.end <= boundary - 1:
                self.assertEqual(kept.offset_shift, 0)
        horizon = boundary + 1 + WINDOW + MAX_BLOCK_BYTES
        for kept in result.kept:
            if kept.old.start >= horizon:
                self.assertEqual(kept.offset_shift, 1)

    def test_whole_object_replacement(self):
        data = bytes(128)
        result = replace_region(data, 0, len(data), b"abc")
        self.assertEqual(len(result.old_blocks), 2)
        self.assertEqual(result.kept, ())
        self.assertEqual(len(result.replaced), 2)
        self.assertEqual(len(result.added), 1)
        self.assertEqual(result.added[0].size, 3)

    def test_empty_object_insertion(self):
        result = replace_region(b"", 0, 0, b"abc")
        self.assertEqual(result.old_blocks, ())
        self.assertEqual(result.old_size, 0)
        self.assertEqual(result.kept, ())
        self.assertEqual(result.replaced, ())
        self.assertEqual(len(result.added), 1)
        self.assertEqual(result.added[0].reason, REASON_TAIL)

        empty = replace_region(b"", 0, 0, b"")
        self.assertEqual(empty.new_blocks, ())
        self.assertEqual(empty.added, ())


class NoChangeTests(unittest.TestCase):
    def test_zero_length_edit_is_identity(self):
        data = lcg_bytes(4000, seed=201)
        result = replace_region(data, 1234, 1234, b"")
        self.assertEqual(result.length_delta, 0)
        self.assertEqual(result.replaced, ())
        self.assertEqual(result.added, ())
        self.assertEqual(len(result.kept), len(result.old_blocks))
        for kept in result.kept:
            self.assertEqual(kept.offset_shift, 0)
            self.assertEqual(
                (kept.new.start, kept.new.end),
                (kept.old.start, kept.old.end),
            )

    def test_replacing_with_identical_bytes_is_identity(self):
        data = lcg_bytes(4000, seed=202)
        result = replace_region(data, 500, 1500, bytes(data[500:1500]))
        self.assertEqual(result.replaced, ())
        self.assertEqual(result.added, ())
        self.assertEqual(len(result.kept), len(result.old_blocks))

    def test_matches_slow_reference_chunkings(self):
        data = lcg_bytes(3000, seed=203)
        result = replace_region(data, 800, 1100, b"\x11" * 7)
        expected_old = slow_split(data)
        expected_new = slow_split(edit(data, 800, 1100, b"\x11" * 7))
        self.assertEqual(ref_tuples(result.old_blocks), ref_tuples(expected_old))
        self.assertEqual(ref_tuples(result.new_blocks), ref_tuples(expected_new))


class ReferenceFuzzTests(unittest.TestCase):
    """Edited chunkings come from the slow reference; the mapping comes from
    an independent DP alignment.  Head, middle, tail and zero-length edits
    are all exercised."""

    def _check(self, data, start, end, rep, seed_tag):
        new_data = edit(data, start, end, rep)
        result = replace_region(data, start, end, rep)
        assert_partition(self, result)

        self.assertEqual(
            ref_tuples(result.old_blocks), ref_tuples(slow_split(data))
        )
        self.assertEqual(
            ref_tuples(result.new_blocks), ref_tuples(slow_split(new_data))
        )

        old_h = [b.sha256 for b in result.old_blocks]
        new_h = [b.sha256 for b in result.new_blocks]
        # Within each side block hashes must be unique so the DP alignment is
        # tie-free.  Random LCG content makes collisions astronomically
        # unlikely; a violation here would be a finding, not a skip.
        self.assertEqual(len(set(old_h)), len(old_h), msg=f"old dup {seed_tag}")
        self.assertEqual(len(set(new_h)), len(new_h), msg=f"new dup {seed_tag}")

        pairs, rep_idx, add_idx = dp_align(old_h, new_h)
        self.assertEqual(
            [(k.old.sha256, k.new.sha256) for k in result.kept],
            [(old_h[i], new_h[j]) for i, j in pairs],
            msg=seed_tag,
        )
        self.assertEqual(
            [b.sha256 for b in result.replaced],
            [old_h[i] for i in rep_idx],
            msg=seed_tag,
        )
        self.assertEqual(
            [b.sha256 for b in result.added],
            [new_h[j] for j in add_idx],
            msg=seed_tag,
        )

    def test_many_random_edits(self):
        cases = []
        for seed in range(24):
            length = 1500 + (seed * 487) % 4000
            data = lcg_bytes(length, seed=10_000 + seed)
            kind = seed % 5
            if kind == 0:  # head insertion
                start = end = 0
            elif kind == 1:  # tail replacement
                start = length - (seed % 300) - 1
                end = length
            elif kind == 2:  # insertion at a mid block boundary
                start = end = split_stream([data])[2 + seed % 5].end
            elif kind == 3:  # cross-boundary replacement
                b = split_stream([data])[1 + seed % 6].end
                start = max(0, b - 3)
                end = min(length, b + 4)
            else:  # plain middle replacement
                start = length // 3 + seed
                end = min(length, start + 1 + (seed * 37) % 200)
            rep = lcg_bytes(seed % 4, seed=20_000 + seed)
            cases.append((data, start, end, rep, f"seed={seed},kind={kind}"))

        for data, start, end, rep, tag in cases:
            with self.subTest(tag=tag):
                self._check(data, start, end, rep, tag)


class InputTypesTests(unittest.TestCase):
    def test_bytearray_and_memoryview_inputs(self):
        data = bytearray(lcg_bytes(2000, seed=301))
        rep = bytearray(b"\xAB\xCD")
        r1 = replace_region(data, 100, 120, rep)
        r2 = replace_region(memoryview(data), 100, 120, memoryview(rep))
        self.assertEqual(
            ref_tuples(r1.new_blocks), ref_tuples(r2.new_blocks)
        )

    def test_input_object_is_not_mutated(self):
        data = bytearray(lcg_bytes(1000, seed=302))
        snapshot = bytes(data)
        replace_region(data, 10, 40, b"z" * 30)
        self.assertEqual(bytes(data), snapshot)

    def test_result_and_blocks_are_immutable(self):
        result = replace_region(lcg_bytes(500, seed=303), 10, 20, b"q")
        self.assertIsInstance(result, ReplaceResult)
        with self.assertRaises(Exception):
            result.start = 1  # type: ignore[misc]
        with self.assertRaises(Exception):
            result.kept = ()  # type: ignore[misc]


class ValidationTests(unittest.TestCase):
    def test_obj_wrong_type(self):
        for bad in ("abc", 123, 1.5, None, True, [b"x"]):
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidBatchTypeError):
                    replace_region(bad, 0, 0, b"")  # type: ignore[arg-type]

    def test_replacement_wrong_type(self):
        with self.assertRaises(InvalidBatchTypeError):
            replace_region(b"abc", 0, 1, "x")
        with self.assertRaises(InvalidBatchTypeError):
            replace_region(b"abc", 0, 1, 5)
        with self.assertRaises(InvalidBatchTypeError):
            replace_region(b"abc", 0, 1, False)

    def test_wrong_memoryview_format(self):
        import array

        with self.assertRaises(InvalidBatchTypeError):
            replace_region(memoryview(array.array("H", [1, 2])), 0, 0, b"")
        with self.assertRaises(InvalidBatchTypeError):
            replace_region(b"abc", 0, 1, memoryview(array.array("H", [1])))

    def test_offset_types(self):
        for bad in ("1", 1.0, True, None, 1 + 0j):
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidEditError):
                    replace_region(b"abc", bad, 1, b"")  # type: ignore[arg-type]
                with self.assertRaises(InvalidEditError):
                    replace_region(b"abc", 0, bad, b"")  # type: ignore[arg-type]

    def test_negative_offsets(self):
        with self.assertRaises(InvalidEditError):
            replace_region(b"abc", -1, 1, b"")
        with self.assertRaises(InvalidEditError):
            replace_region(b"abc", 0, -1, b"")

    def test_end_before_start(self):
        with self.assertRaises(InvalidEditError):
            replace_region(b"abc", 2, 1, b"")

    def test_end_out_of_bounds(self):
        with self.assertRaises(InvalidEditError):
            replace_region(b"abc", 0, 4, b"")
        with self.assertRaises(InvalidEditError):
            replace_region(b"abc", 4, 4, b"")

    def test_error_hierarchy(self):
        self.assertTrue(issubclass(InvalidEditError, ValueError))
        self.assertTrue(issubclass(InvalidBatchTypeError, TypeError))
        self.assertTrue(issubclass(InvalidEditError, CDCError))
        self.assertTrue(issubclass(InputTooLargeError, CDCError))

    def test_insertion_over_limit_rejected_before_chunking(self):
        data = b"\x01" * MAX_TOTAL_BYTES
        with self.assertRaises(InputTooLargeError):
            replace_region(data, 0, 0, b"x")

    def test_deletion_at_limit_is_accepted(self):
        data = b"\x01" * MAX_TOTAL_BYTES
        result = replace_region(data, 0, 1, b"")
        self.assertEqual(result.new_size, MAX_TOTAL_BYTES - 1)
        self.assertEqual(
            sum(b.size for b in result.new_blocks), MAX_TOTAL_BYTES - 1
        )

    def test_failed_call_leaves_no_half_state(self):
        # Pure function: after a rejected call the original object's
        # chunking is exactly what it always was.
        data = lcg_bytes(2000, seed=304)
        before = ref_tuples(split_stream([data]))
        for call in (
            lambda: replace_region("not bytes", 0, 0, b""),
            lambda: replace_region(data, -1, 0, b""),
            lambda: replace_region(data, 0, 99999, b""),
            lambda: replace_region(data, 0, 0, "nope"),
        ):
            with self.assertRaises(CDCError):
                call()
        self.assertEqual(ref_tuples(split_stream([data])), before)


if __name__ == "__main__":
    unittest.main()
