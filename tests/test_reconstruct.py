"""Tests for multi-source rebuild planning (rabin_cdc.reconstruct)."""

from __future__ import annotations

import hashlib
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rabin_cdc import (
    ContentMismatchError,
    InvalidSourceError,
    MissingSourceError,
    Source,
    plan_rebuild,
    rebuild,
)

from tests._data import lcg_bytes  # noqa: E402


def make_world():
    """Three old contents; the target mixes regions of all three plus new
    data, so its blocks exist in several sources at once."""
    old_a = lcg_bytes(40_000, seed=11)
    old_b = lcg_bytes(40_000, seed=22)
    old_c = lcg_bytes(40_000, seed=33)
    target = (
        old_a[5_000:15_000]
        + old_b[10_000:18_000]
        + b"\x00" * 1_000  # brand-new data
        + old_c[20_000:36_000]
        + old_a[30_000:38_000]
    )
    return old_a, old_b, old_c, target


class SourceValidationTests(unittest.TestCase):
    def test_rejects_bad_cost_types(self):
        for bad in (True, 1.5, float("nan"), float("inf"), "1"):
            with self.assertRaises(InvalidSourceError):
                Source("s", b"x" * 100, open_cost=bad)
            with self.assertRaises(InvalidSourceError):
                Source("s", b"x" * 100, byte_cost=bad)

    def test_rejects_negative_cost_and_bad_name(self):
        with self.assertRaises(InvalidSourceError):
            Source("s", b"x" * 100, open_cost=-1)
        with self.assertRaises(InvalidSourceError):
            Source("", b"x" * 100)
        with self.assertRaises(InvalidSourceError):
            Source(7, b"x" * 100)

    def test_rejects_bad_target_and_duplicate_names(self):
        with self.assertRaises(InvalidSourceError):
            plan_rebuild("nope", [])
        with self.assertRaises(InvalidSourceError):
            plan_rebuild(b"x" * 100, [Source("d", b"x" * 100),
                                      Source("d", b"y" * 100)])
        with self.assertRaises(InvalidSourceError):
            plan_rebuild(b"x" * 100, [], literal_byte_cost=0)
        with self.assertRaises(InvalidSourceError):
            plan_rebuild(b"x" * 100, [], literal_byte_cost=True)


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.old_a, self.old_b, self.old_c, self.target = make_world()

    def test_plan_covers_target_and_records_sources(self):
        sources = [Source("a", self.old_a), Source("b", self.old_b),
                   Source("c", self.old_c)]
        plan = plan_rebuild(self.target, sources)
        self.assertEqual(plan.target_size, len(self.target))
        self.assertEqual(
            plan.target_sha256, hashlib.sha256(self.target).hexdigest()
        )
        pos = 0
        for entry in plan.entries:
            self.assertEqual(entry.start, pos)
            pos = entry.end
            if entry.kind == "reuse":
                self.assertIn(entry.source, {"a", "b", "c"})
                self.assertIsNotNone(entry.source_offset)
            else:
                self.assertEqual(entry.kind, "literal")
                self.assertEqual(
                    entry.literal, self.target[entry.start:entry.end]
                )
        self.assertEqual(pos, len(self.target))
        self.assertEqual(plan.reused_bytes + plan.literal_bytes,
                         len(self.target))

    def test_every_reuse_entry_points_at_matching_material(self):
        sources = [Source("a", self.old_a), Source("b", self.old_b),
                   Source("c", self.old_c)]
        plan = plan_rebuild(self.target, sources)
        by_name = {s.name: s.data for s in sources}
        for entry in plan.entries:
            if entry.kind != "reuse":
                continue
            chunk = by_name[entry.source][
                entry.source_offset:entry.source_offset + entry.size]
            self.assertEqual(hashlib.sha256(chunk).hexdigest(), entry.sha256)

    def test_shared_block_picks_cheapest_source(self):
        # Same content in two sources; the cheaper one must be chosen.
        shared = lcg_bytes(5_000, seed=7)
        cheap = Source("cheap", shared, open_cost=1, byte_cost=0)
        pricey = Source("pricey", shared, open_cost=1, byte_cost=5)
        plan = plan_rebuild(shared, [cheap, pricey])
        self.assertTrue(plan.entries)
        for entry in plan.entries:
            self.assertEqual(entry.kind, "reuse")
            self.assertEqual(entry.source, "cheap")
        self.assertEqual(plan.sources_used, ("cheap",))

    def test_expensive_source_avoided_for_small_data(self):
        # A tiny overlap with a very expensive source is not worth opening.
        base = lcg_bytes(20_000, seed=8)
        target = base[:200] + lcg_bytes(2_000, seed=9)
        expensive = Source("vault", base, open_cost=10_000, byte_cost=0)
        plan = plan_rebuild(target, [expensive], literal_byte_cost=1)
        self.assertEqual(plan.sources_used, ())
        self.assertEqual(plan.reused_bytes, 0)
        # With a cheap source the same 200-byte prefix region is reused.
        cheap = Source("cache", base, open_cost=1, byte_cost=0)
        plan2 = plan_rebuild(target, [cheap], literal_byte_cost=1)
        self.assertGreater(plan2.reused_bytes, 0)
        self.assertEqual(plan2.sources_used, ("cache",))

    def test_replan_without_missing_source(self):
        sources = [Source("a", self.old_a), Source("b", self.old_b),
                   Source("c", self.old_c)]
        full = plan_rebuild(self.target, sources)
        replanned = plan_rebuild(self.target, [s for s in sources
                                               if s.name != "c"])
        self.assertNotIn("c", replanned.sources_used)
        self.assertLess(replanned.reused_bytes, full.reused_bytes)
        result = rebuild(replanned, lambda name: {
            "a": self.old_a, "b": self.old_b}[name])
        self.assertEqual(result.data, self.target)


class RebuildTests(unittest.TestCase):
    def setUp(self):
        self.old_a, self.old_b, self.old_c, self.target = make_world()
        self.sources = [Source("a", self.old_a), Source("b", self.old_b),
                        Source("c", self.old_c)]
        self.plan = plan_rebuild(self.target, self.sources)
        self.store = {s.name: s.data for s in self.sources}

    def test_roundtrip_bytes_and_accounting(self):
        result = rebuild(self.plan, self.store.get)
        self.assertEqual(result.data, self.target)
        self.assertEqual(result.reused_bytes, self.plan.reused_bytes)
        self.assertEqual(result.literal_bytes, self.plan.literal_bytes)
        self.assertEqual(result.reused_bytes + result.literal_bytes,
                         len(self.target))
        self.assertEqual(sorted(result.sources_used),
                         sorted(self.plan.sources_used))
        self.assertEqual(result.blocks_reused + result.blocks_literal,
                         len(self.plan.entries))

    def test_missing_source_rejected(self):
        def reader(name):
            if name == self.plan.sources_used[0]:
                return None
            return self.store.get(name)
        with self.assertRaises(MissingSourceError) as ctx:
            rebuild(self.plan, reader)
        self.assertEqual(ctx.exception.source, self.plan.sources_used[0])

    def test_corrupted_material_rejected(self):
        used = self.plan.sources_used[0]
        bad = bytearray(self.store[used])
        entry = next(e for e in self.plan.entries
                     if e.kind == "reuse" and e.source == used)
        bad[entry.source_offset] ^= 0xFF

        def reader(name):
            if name == used:
                return bytes(bad)
            return self.store.get(name)
        with self.assertRaises(ContentMismatchError):
            rebuild(self.plan, reader)

    def test_reader_type_checked(self):
        with self.assertRaises(InvalidSourceError):
            rebuild(self.plan, lambda name: 42)
        with self.assertRaises(InvalidSourceError):
            rebuild(self.plan, "not-callable")
        with self.assertRaises(InvalidSourceError):
            rebuild("not-a-plan", self.store.get)


if __name__ == "__main__":
    unittest.main()
