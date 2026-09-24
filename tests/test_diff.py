"""Tests for middle-region replacement and the keep/replace/new mapping.

Expected chunkings come from the independent long-division reference
(``tests._slow_reference.py``, which never imports :mod:`rabin_cdc`); the
mapping expectations are hand-derived on fixed vectors (all-zero data has a
known chunk pattern) or re-derived analytically (causal prefix, post-horizon
recovery, SHA-256 payload equality).  The mapping pairing itself is
independently re-implemented below (``reference_mapping``) rather than taken
from the code under test.
"""

from __future__ import annotations

import hashlib
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rabin_cdc import (  # noqa: E402
    KEEP,
    MAX_BLOCK_BYTES,
    MAX_TOTAL_BYTES,
    NEW,
    REPLACE,
    WINDOW_BYTES,
    Block,
    EditValidationError,
    InputTooLargeError,
    InvalidBatchTypeError,
    diff_chunks,
    replace_region,
    split_stream,
)
from tests._slow_reference import slow_split, slow_window_fingerprints  # noqa: E402
from tests._data import lcg_bytes  # noqa: E402


def h(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def reference_mapping(old, new):
    """Independent re-derivation of the positional pairing contract.

    For each SHA-256 the k-th old occurrence pairs with the k-th new
    occurrence; unpaired old occurrences are 'replace', unpaired new ones are
    'new'.  Returns (pairs list of (oi, nj), replaced old indexes,
    added new indexes).
    """
    old_pos: dict[str, list[int]] = {}
    new_pos: dict[str, list[int]] = {}
    for i, b in enumerate(old):
        old_pos.setdefault(b.sha256, []).append(i)
    for j, b in enumerate(new):
        new_pos.setdefault(b.sha256, []).append(j)
    pairs: list[tuple[int, int]] = []
    paired_old: set[int] = set()
    paired_new: set[int] = set()
    for digest, positions in old_pos.items():
        others = new_pos.get(digest, [])
        for k in range(min(len(positions), len(others))):
            oi, nj = positions[k], others[k]
            pairs.append((oi, nj))
            paired_old.add(oi)
            paired_new.add(nj)
    pairs.sort()
    replaced = [i for i in range(len(old)) if i not in paired_old]
    added = [j for j in range(len(new)) if j not in paired_new]
    return pairs, replaced, added


def block_tuples(blocks):
    return [(b.start, b.end, b.size, b.sha256, b.reason) for b in blocks]


# All-zero stream chunking is a fixed mathematical vector:
#   bytes(600) -> nine 64-byte content blocks + one 24-byte tail.
ZERO600 = bytes(600)
H64 = h(bytes(64))
H24 = h(bytes(24))


class ZeroVectorHandComputedTest(unittest.TestCase):
    """Mapping outcomes derived by hand on the fixed all-zero chunk vector."""

    def test_insert_one_zero_byte_in_middle(self):
        # New object is 601 zero bytes: nine 64-blocks + a 25-byte tail.
        result = replace_region(ZERO600, 300, 0, b"\x00")
        self.assertEqual(len(result.old_blocks), 10)
        self.assertEqual(len(result.new_blocks), 10)
        self.assertEqual(result.edit_start, 300)
        self.assertEqual(result.edit_end, 301)
        keeps = [m for m in result.mappings if m.status == KEEP]
        replaces = [m for m in result.mappings if m.status == REPLACE]
        adds = [m for m in result.mappings if m.status == NEW]
        self.assertEqual(
            [(m.old_index, m.new_index) for m in keeps],
            [(i, i) for i in range(9)],
        )
        self.assertEqual([(m.old_index, m.size) for m in replaces], [(9, 24)])
        self.assertEqual([(m.new_index, m.size) for m in adds], [(9, 25)])
        for m in keeps:
            self.assertEqual(m.sha256, H64)
        self.assertEqual(replaces[0].sha256, H24)
        self.assertEqual(adds[0].sha256, h(bytes(25)))

    def test_delete_one_zero_byte_in_middle(self):
        # New object is 599 zero bytes: nine 64-blocks + a 23-byte tail.
        result = replace_region(ZERO600, 300, 1, b"")
        keeps = [(m.old_index, m.new_index) for m in result.mappings if m.status == KEEP]
        self.assertEqual(keeps, [(i, i) for i in range(9)])
        self.assertEqual(
            [(m.old_index, m.size) for m in result.mappings if m.status == REPLACE],
            [(9, 24)],
        )
        self.assertEqual(
            [(m.new_index, m.size) for m in result.mappings if m.status == NEW],
            [(9, 23)],
        )

    def test_inserting_one_extra_identical_block_pairs_positionally(self):
        # Insert 64 zero bytes at offset 64: 664 zeros -> ten 64-blocks + a
        # 24-byte tail.  Old occurrences 0..8 of H64 pair with new 0..8; the
        # 10th new 64-block is genuinely NEW; the 24-byte tails pair.
        result = replace_region(ZERO600, 64, 0, bytes(64))
        pairs = [(m.old_index, m.new_index) for m in result.mappings if m.status == KEEP]
        self.assertEqual(pairs, [(0, 0), (1, 1), (2, 2), (3, 3), (4, 4),
                                 (5, 5), (6, 6), (7, 7), (8, 8), (9, 10)])
        self.assertEqual(
            [m.old_index for m in result.mappings if m.status == REPLACE], []
        )
        added = [m for m in result.mappings if m.status == NEW]
        self.assertEqual(len(added), 1)
        self.assertEqual(added[0].new_index, 9)
        self.assertEqual(added[0].sha256, H64)
        self.assertEqual(added[0].size, 64)

    def test_deleting_one_of_identical_blocks_marks_that_occurrence_replaced(self):
        # Delete the first 64 bytes: 536 zeros -> eight 64-blocks + 24 tail.
        # Old occurrences 0..7 pair with new 0..7; old occurrence 8 is
        # replaced; the tails pair (same payload, same end offset).
        result = replace_region(ZERO600, 0, 64, b"")
        pairs = [(m.old_index, m.new_index) for m in result.mappings if m.status == KEEP]
        self.assertEqual(pairs, [(0, 0), (1, 1), (2, 2), (3, 3), (4, 4),
                                 (5, 5), (6, 6), (7, 7), (9, 8)])
        self.assertEqual(
            [m.old_index for m in result.mappings if m.status == REPLACE], [8]
        )
        self.assertEqual(
            [m.new_index for m in result.mappings if m.status == NEW], []
        )

    def test_identical_replacement_is_a_full_keep_noop(self):
        result = replace_region(ZERO600, 250, 70, ZERO600[250:320])
        self.assertEqual(result.new_data, ZERO600)
        self.assertEqual(
            block_tuples(result.new_blocks), block_tuples(result.old_blocks)
        )
        self.assertTrue(all(m.status == KEEP for m in result.mappings))
        self.assertEqual(len(result.mappings), 10)

    def test_tail_append_grows_only_the_tail_block(self):
        result = replace_region(ZERO600, 600, 0, b"\x01" * 10)
        # The nine full zero blocks are untouched at identical offsets; the
        # old 24-byte tail is replaced by a 34-byte tail.
        pairs = [(m.old_index, m.new_index) for m in result.mappings if m.status == KEEP]
        self.assertEqual(pairs, [(i, i) for i in range(9)])
        self.assertEqual(
            [m.old_index for m in result.mappings if m.status == REPLACE], [9]
        )
        added = [m for m in result.mappings if m.status == NEW]
        self.assertEqual(len(added), 1)
        self.assertEqual(added[0].size, 34)
        self.assertEqual(added[0].sha256, h(bytes(24) + b"\x01" * 10))


class EmptyAndWholeObjectTest(unittest.TestCase):
    def test_insert_into_empty_object(self):
        result = replace_region(b"", 0, 0, b"abc")
        self.assertEqual(result.new_data, b"abc")
        self.assertEqual(result.old_blocks, [])
        self.assertEqual(len(result.new_blocks), 1)
        self.assertEqual(len(result.mappings), 1)
        row = result.mappings[0]
        self.assertEqual((row.status, row.old_index, row.new_index), (NEW, None, 0))
        self.assertEqual(row.sha256, h(b"abc"))

    def test_delete_whole_object(self):
        result = replace_region(b"abcdef", 0, 6, b"")
        self.assertEqual(result.new_data, b"")
        self.assertEqual(result.new_blocks, [])
        self.assertEqual(len(result.old_blocks), 1)
        self.assertEqual(len(result.mappings), 1)
        row = result.mappings[0]
        self.assertEqual(
            (row.status, row.old_index, row.new_index), (REPLACE, 0, None)
        )

    def test_empty_replacement_of_empty_region_on_empty_object(self):
        result = replace_region(b"", 0, 0, b"")
        self.assertEqual(result.new_data, b"")
        self.assertEqual(result.mappings, [])


class HeadTailAndCrossBoundaryTest(unittest.TestCase):
    """Head edits, tail edits and edits straddling block boundaries."""

    def test_head_insertion_realigns_with_replaced_first_block(self):
        data = lcg_bytes(3000, seed=505)
        result = replace_region(data, 0, 0, b"\xA5" * 7)
        self.assertEqual(result.new_data, b"\xA5" * 7 + data)
        # Causality: the edit starts at 0, so the first old block cannot
        # survive; every other kept block is shifted by exactly +7.
        replaced_old = {m.old_index for m in result.mappings if m.status == REPLACE}
        self.assertIn(0, replaced_old)
        for m in result.mappings:
            if m.status == KEEP:
                ob = result.old_blocks[m.old_index]
                nb = result.new_blocks[m.new_index]
                self.assertEqual(nb.start, ob.start + 7)
                self.assertEqual(nb.size, ob.size)

    def test_tail_insertion_keeps_every_complete_block_put_at_same_offset(self):
        data = lcg_bytes(4000, seed=808)
        result = replace_region(data, len(data), 0, b"ZZ")
        # Every block ending before the append keeps identical offsets.
        old_complete = [b for b in result.old_blocks if b.end < len(data)]
        kept_pairs = {m.old_index: m.new_index for m in result.mappings
                      if m.status == KEEP}
        for i, block in enumerate(old_complete):
            self.assertIn(i, kept_pairs)
            self.assertEqual(
                result.new_blocks[kept_pairs[i]].start, block.start
            )
        # The old tail is replaced (it grew).
        last_old = len(result.old_blocks) - 1
        self.assertTrue(
            any(m.status == REPLACE and m.old_index == last_old
                for m in result.mappings)
        )

    def test_cross_boundary_edit_replaces_overlapping_blocks(self):
        # Pick a cut boundary, then replace an interval that starts inside the
        # block before it and ends inside the block after it.
        data = lcg_bytes(5000, seed=31337)
        old = split_stream([data])
        boundary = old[4].end
        start = boundary - 13
        end = boundary + 29
        result = replace_region(data, start, end - start, b"Q" * 41)
        self.assertEqual(
            result.new_data,
            data[:start] + b"Q" * 41 + data[end:],
        )
        old_hashes = {b.sha256 for b in result.old_blocks}
        for m in result.mappings:
            if m.status == KEEP:
                ob = result.old_blocks[m.old_index]
                nb = result.new_blocks[m.new_index]
                # Kept blocks must not straddle the edited interval.
                self.assertTrue(ob.end <= start or ob.start >= end)
                self.assertEqual(ob.sha256, nb.sha256)
            elif m.status == NEW:
                nb = result.new_blocks[m.new_index]
                # New-status blocks are the chunks produced by the edit.
                self.assertNotIn(nb.sha256, old_hashes)
        # Blocks fully before the edit survive at identical offsets.
        for m in result.mappings:
            if m.status == KEEP:
                ob = result.old_blocks[m.old_index]
                if ob.end <= start:
                    self.assertEqual(
                        result.new_blocks[m.new_index].start, ob.start
                    )

    def test_result_chunking_matches_independent_reference(self):
        data = lcg_bytes(3000, seed=20260924)
        for offset, old_len, repl in (
            (0, 5, b"abc"),
            (1000, 16, b"HELLO"),
            (1234, 0, b"\x00" * 100),
            (2000, 300, b""),
            (len(data), 0, b"tail"),
            (50, 50, data[50:100]),
        ):
            with self.subTest(offset=offset, old_len=old_len):
                result = replace_region(data, offset, old_len, repl)
                self.assertEqual(
                    block_tuples(result.old_blocks),
                    block_tuples(slow_split(data)),
                )
                self.assertEqual(
                    block_tuples(result.new_blocks),
                    block_tuples(slow_split(result.new_data)),
                )


class MappingContractTest(unittest.TestCase):
    def _check_invariants(self, result, data, start, old_len, repl):
        old, new = result.old_blocks, result.new_blocks
        # Tiling.
        self.assertEqual(old[0].start, 0) if old else None
        for blocks, payload in ((old, data), (new, result.new_data)):
            cursor = 0
            for b in blocks:
                self.assertEqual(b.start, cursor)
                self.assertEqual(b.end, cursor + b.size)
                self.assertEqual(b.sha256, h(payload[b.start:b.end]))
                cursor = b.end
            self.assertEqual(cursor, len(payload))

        # Coverage: every old/new index appears exactly once.
        old_rows = [m for m in result.mappings if m.old_index is not None]
        new_rows = [m for m in result.mappings if m.new_index is not None]
        self.assertEqual(sorted(m.old_index for m in old_rows),
                         list(range(len(old))))
        self.assertEqual(sorted(m.new_index for m in new_rows),
                         list(range(len(new))))

        # Status field consistency and hashes.
        for m in result.mappings:
            if m.status == KEEP:
                self.assertIsNotNone(m.old_index)
                self.assertIsNotNone(m.new_index)
                ob, nb = old[m.old_index], new[m.new_index]
                self.assertEqual(ob.sha256, nb.sha256)
                self.assertEqual(m.sha256, ob.sha256)
                self.assertEqual(m.size, ob.size)
                self.assertEqual(ob.size, nb.size)
                # The claimed payloads really are byte-identical.
                self.assertEqual(
                    data[ob.start:ob.end], result.new_data[nb.start:nb.end]
                )
            elif m.status == REPLACE:
                self.assertIsNone(m.new_index)
                self.assertEqual(m.sha256, old[m.old_index].sha256)
                self.assertEqual(m.size, old[m.old_index].size)
            else:
                self.assertEqual(m.status, NEW)
                self.assertIsNone(m.old_index)
                self.assertEqual(m.sha256, new[m.new_index].sha256)
                self.assertEqual(m.size, new[m.new_index].size)

        # Independent re-derivation of the pairing.
        pairs, replaced, added = reference_mapping(old, new)
        self.assertEqual(
            pairs,
            [(m.old_index, m.new_index) for m in result.mappings
             if m.status == KEEP],
        )
        self.assertEqual(
            replaced,
            [m.old_index for m in result.mappings if m.status == REPLACE],
        )
        self.assertEqual(
            added,
            [m.new_index for m in result.mappings if m.status == NEW],
        )

        # Ordering: keeps (old order), then replaces (old order), then new.
        statuses = [m.status for m in result.mappings]
        self.assertTrue(
            all(a == KEEP for a in statuses[:len(pairs)])
        )
        self.assertEqual(
            [m.status for m in result.mappings[len(pairs):len(pairs) + len(replaced)]],
            [REPLACE] * len(replaced),
        )
        self.assertEqual(
            [m.status for m in result.mappings[len(pairs) + len(replaced):]],
            [NEW] * len(added),
        )
        keep_old_idx = [m.old_index for m in result.mappings if m.status == KEEP]
        self.assertEqual(keep_old_idx, sorted(keep_old_idx))

        # Causal prefix: an old block ending at/before the edit start keeps
        # the exact same offsets in the new layout.
        for m in result.mappings:
            if m.status == KEEP and old[m.old_index].end <= start:
                ob, nb = old[m.old_index], new[m.new_index]
                self.assertEqual((nb.start, nb.end), (ob.start, ob.end))

    def test_randomized_edit_shapes(self):
        shapes = [
            (0, 0, 5),
            (1, 0, 1),
            (77, 1, 33),
            (500, 200, 0),
            (1000, 0, 257),
            (1500, 1500, 1),
            (2500, 1, 0),
        ]
        for seed in range(30):
            data = lcg_bytes(3000 + seed * 37, seed=10_000 + seed)
            for shape_index, (so, sl, rl) in enumerate(shapes):
                start = min(so, len(data))
                old_len = min(sl, len(data) - start)
                repl = lcg_bytes(rl, seed=seed * 100 + shape_index)
                with self.subTest(seed=seed, shape=(start, old_len, rl)):
                    result = replace_region(data, start, old_len, repl)
                    self._check_invariants(result, data, start, old_len, repl)

    def test_window_fingerprints_realign_analytically(self):
        # Deterministic analytic relation, re-derived by the independent
        # long-division reference: positions whose 32-byte window lies wholly
        # on one side of the edited interval carry the SAME window remainder
        # in the old and new object (before the edit at equal offsets; after
        # it at offsets shifted by exactly the byte delta).  This is why
        # chunking realigns; block-boundary *phase* locking itself has no
        # content-independent bound (an all-0x01 run only ever cuts at 256, so
        # incompatible phases never lock), which is precisely why reuse is
        # content-addressed by hash rather than claimed by offset.
        data = lcg_bytes(1500, seed=424242)
        cases = [
            (0, 0, b"\xAA" * 5),
            (100, 16, b"middle!"),
            (700, 200, b""),
            (900, 0, bytes(range(100))),
            (1499, 1, b"\x00" * 33),
            (0, 50, b"head"),
        ]
        for start, old_len, repl in cases:
            with self.subTest(case=(start, old_len, len(repl))):
                result = replace_region(data, start, old_len, repl)
                delta = len(repl) - old_len
                fps_old = slow_window_fingerprints(data)
                fps_new = slow_window_fingerprints(result.new_data)
                for p in range(start):
                    self.assertEqual(fps_new[p], fps_old[p])
                first_after = start + len(repl) + WINDOW_BYTES - 1
                for p in range(first_after, len(result.new_data)):
                    self.assertEqual(fps_new[p], fps_old[p - delta])

    def test_blocks_past_recovery_horizon_are_reused_on_fixed_points(self):
        # Project convention: on the fixed pseudo-random stream, once a block
        # starts WINDOW+MAX bytes past the end of the replaced region, every
        # later block (up to and including the tail) is byte-identical to a
        # block of the original object and is classified keep at the offset
        # shifted by the byte delta.  Values are real outputs of fixed
        # inputs; the chunkings themselves are checked against the slow
        # reference separately in test_result_chunking_matches_reference.
        for seed, at, old_len, repl_len in (
            (2026, 2000, 16, 17),
            (77, 1000, 8, 264),
            (55, 1234, 300, 0),
        ):
            with self.subTest(seed=seed):
                data = lcg_bytes(8000, seed=seed)
                repl = lcg_bytes(repl_len, seed=seed ^ 0xFFFF)
                result = replace_region(data, at, old_len, repl)
                delta = repl_len - old_len
                horizon = at + repl_len + WINDOW_BYTES + MAX_BLOCK_BYTES
                old_by_start = {b.start: b for b in result.old_blocks}
                kept_new = {m.new_index for m in result.mappings
                            if m.status == KEEP}
                recovered = [b for b in result.new_blocks if b.start >= horizon]
                self.assertGreater(len(recovered), 5)
                for j, nb in enumerate(result.new_blocks):
                    if nb.start >= horizon:
                        ob = old_by_start[nb.start - delta]
                        self.assertEqual(
                            (ob.sha256, ob.size, ob.reason),
                            (nb.sha256, nb.size, nb.reason),
                        )
                        self.assertIn(j, kept_new)
                # The tail block is recovered too at fixed points.
                tail = result.new_blocks[-1]
                self.assertGreaterEqual(tail.start, horizon)

    def test_diff_chunks_on_hand_built_block_lists(self):
        def mk(digest, start, size=64, reason="content"):
            return Block(start, start + size, size, digest, reason)

        # Old layout: A, B, A2, C.  New layout: A, X, A2 (elsewhere offsets).
        old = [mk("hash-A", 0), mk("hash-B", 1), mk("hash-A2", 2), mk("hash-C", 3)]
        new = [mk("hash-A", 10), mk("hash-X", 11), mk("hash-A2", 12)]
        mappings = diff_chunks(old, new)
        # A occurrences pair 1st<->1st and 2nd<->2nd, positionally.
        self.assertEqual(
            [(m.status, m.old_index, m.new_index) for m in mappings],
            [
                (KEEP, 0, 0),
                (KEEP, 2, 2),
                (REPLACE, 1, None),
                (REPLACE, 3, None),
                (NEW, None, 1),
            ],
        )

    def test_diff_chunks_empty_inputs(self):
        self.assertEqual(diff_chunks([], []), [])
        only_old = [Block(0, 2, 2, "hh", "tail")]
        mappings = diff_chunks(only_old, [])
        self.assertEqual(len(mappings), 1)
        self.assertEqual(
            (mappings[0].status, mappings[0].old_index, mappings[0].new_index),
            (REPLACE, 0, None),
        )
        only_new = [Block(0, 2, 2, "hh", "tail")]
        mappings = diff_chunks([], only_new)
        self.assertEqual(
            (mappings[0].status, mappings[0].old_index, mappings[0].new_index),
            (NEW, None, 0),
        )

    def test_helper_properties_partition_rows(self):
        data = lcg_bytes(2000, seed=909)
        result = replace_region(data, 600, 50, b"\x01" * 60)
        n_keep = sum(m.status == KEEP for m in result.mappings)
        n_repl = sum(m.status == REPLACE for m in result.mappings)
        n_new = sum(m.status == NEW for m in result.mappings)
        self.assertEqual(len(result.kept_blocks), n_keep)
        self.assertEqual(len(result.replaced_blocks), n_repl)
        self.assertEqual(len(result.added_blocks), n_new)
        self.assertEqual(n_keep + n_repl, len(result.old_blocks))
        self.assertEqual(n_keep + n_new, len(result.new_blocks))


class InputTypesAndAtomicityTest(unittest.TestCase):
    def test_bytearray_and_memoryview_inputs(self):
        data = bytearray(lcg_bytes(1500, seed=2))
        repl = memoryview(b"\x77" * 8)
        result = replace_region(data, 300, 8, repl)
        self.assertEqual(
            result.new_data, bytes(data[:300]) + b"\x77" * 8 + bytes(data[308:])
        )

    def test_input_object_is_not_mutated(self):
        data = bytearray(lcg_bytes(1000, seed=8))
        snapshot = bytes(data)
        replace_region(data, 100, 10, b"zzzzzzzzzz")
        self.assertEqual(bytes(data), snapshot)

    def test_bad_offset_and_length(self):
        data = lcg_bytes(500, seed=1)
        cases = [
            (lambda: replace_region(data, -1, 0, b""), "negative offset"),
            (lambda: replace_region(data, 501, 0, b""), "offset past end"),
            (lambda: replace_region(data, 499, 2, b""), "region past end"),
            (lambda: replace_region(data, 0, -3, b""), "negative length"),
            (lambda: replace_region(data, True, 0, b""), "bool offset"),
            (lambda: replace_region(data, 0, False, b""), "bool length"),
            (lambda: replace_region(data, 1.0, 0, b""), "float offset"),
            (lambda: replace_region(data, 0, 1.0, b""), "float length"),
            (lambda: replace_region(data, "0", 0, b""), "str offset"),
            (lambda: replace_region(data, None, 0, b""), "None offset"),
        ]
        for call, label in cases:
            with self.subTest(label):
                with self.assertRaises((EditValidationError,
                                        InvalidBatchTypeError)):
                    call()

    def test_bad_payload_types(self):
        data = lcg_bytes(500, seed=1)
        with self.assertRaises(InvalidBatchTypeError):
            replace_region("not-bytes", 0, 0, b"")
        with self.assertRaises(InvalidBatchTypeError):
            replace_region(12345, 0, 0, b"")
        with self.assertRaises(InvalidBatchTypeError):
            replace_region(None, 0, 0, b"")
        with self.assertRaises(InvalidBatchTypeError):
            replace_region(data, 0, 0, "not-bytes")
        with self.assertRaises(InvalidBatchTypeError):
            replace_region(data, 0, 0, 7)
        with self.assertRaises(InvalidBatchTypeError):
            replace_region(data, 0, 0, True)

    def test_wrong_memoryview_format_rejected(self):
        import array

        with self.assertRaises(InvalidBatchTypeError):
            replace_region(memoryview(array.array("H", [1, 2, 3])), 0, 0, b"")
        data = lcg_bytes(500, seed=1)
        with self.assertRaises(InvalidBatchTypeError):
            replace_region(data, 0, 0, memoryview(array.array("H", [1])))

    def test_result_over_limit_rejected(self):
        data = b"\x01" * MAX_TOTAL_BYTES
        with self.assertRaises(InputTooLargeError):
            replace_region(data, 0, 0, b"x")
        # Growing right at the boundary itself stays legal.
        result = replace_region(data[:MAX_TOTAL_BYTES - 2], 0, 0, b"\x00\x00")
        self.assertEqual(len(result.new_data), MAX_TOTAL_BYTES)

    def test_oversized_original_object_rejected_before_chunking(self):
        data = b"\x01" * (MAX_TOTAL_BYTES + 10)
        with self.assertRaises(InputTooLargeError):
            replace_region(data, 0, 100, b"")
        # A shrinking edit is not an escape hatch: the original object itself
        # violates the stream-size contract.
        with self.assertRaises(InputTooLargeError):
            replace_region(data, 0, len(data), b"")

    def test_error_messages_are_locatable(self):
        try:
            replace_region(b"abc", 9, 0, b"")
        except EditValidationError as exc:
            self.assertIn("9", str(exc))
            self.assertIn("3", str(exc))
        else:
            self.fail("expected EditValidationError")


class OriginalEntryPointsUnchangedTest(unittest.TestCase):
    """The new capability must not disturb the original public API."""

    def test_split_stream_contract_still_holds(self):
        data = lcg_bytes(3000, seed=2026)
        self.assertEqual(
            block_tuples(split_stream([data])), block_tuples(slow_split(data))
        )

    def test_old_block_identities_match_replace_originals(self):
        data = lcg_bytes(3000, seed=2026)
        result = replace_region(data, 1000, 16, b"new")
        self.assertEqual(
            block_tuples(result.old_blocks), block_tuples(split_stream([data]))
        )


if __name__ == "__main__":
    unittest.main()
