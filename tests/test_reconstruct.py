"""Tests for multi-source reconstruction planning and execution."""

from __future__ import annotations

import hashlib
import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rabin_cdc import (  # noqa: E402
    KIND_NEW,
    KIND_REUSE,
    Block,
    BlockVerificationError,
    ConfigurationError,
    MissingBlockDataError,
    Plan,
    Source,
    execute_plan,
    plan_reconstruction,
    split_stream,
)
from tests._data import lcg_bytes, mixed_stream  # noqa: E402


def block(payload: bytes, start: int = 0) -> Block:
    return Block(
        start=start,
        end=start + len(payload),
        size=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
        reason="tail",
    )


def sources_for(blks, **costs):
    out = []
    for name, picked in costs.items():
        hashes = {blks[i].sha256 for i in picked}
        fixed, per_byte = picked.cost if hasattr(picked, "cost") else (1, 1)
        out.append(Source(name, fixed, per_byte, hashes))
    return out


class PlanningTest(unittest.TestCase):
    def setUp(self):
        self.payloads = [bytes([i + 1]) * 100 for i in range(6)]
        self.blocks = [
            block(p, start=i * 100) for i, p in enumerate(self.payloads)
        ]

    def make_sources(self):
        b = self.blocks
        return [
            # Cheap per byte but a high fixed cost; holds 0..3.
            Source("bulk", fixed_cost=1000, byte_cost=0,
                   hashes={b[i].sha256 for i in range(4)}),
            # Small fixed cost, free per byte; holds 2..5.
            Source("edge", fixed_cost=5, byte_cost=0,
                   hashes={b[i].sha256 for i in range(2, 6)}),
            # Very expensive on both axes but also holds block 0.
            Source("archive", fixed_cost=5000, byte_cost=10,
                   hashes={b[0].sha256, b[1].sha256}),
        ]

    def test_plan_uses_single_cheap_source_and_marks_new(self):
        plan = plan_reconstruction(self.blocks, self.make_sources())
        chosen = {i: step.source for i, step in enumerate(plan.steps)}
        # edge covers blocks 2..5 alone; blocks 0,1 from bulk would also
        # incur bulk's 1000 fixed cost -> they must be new instead.
        self.assertEqual(chosen[0], None)
        self.assertEqual(chosen[1], None)
        self.assertEqual(chosen[2], "edge")
        self.assertEqual(chosen[5], "edge")
        self.assertEqual(plan.reused_bytes, 400)
        self.assertEqual(plan.new_bytes, 200)
        self.assertEqual(plan.sources_used, ("edge",))
        self.assertEqual(plan.total_cost, 205)

    def test_expensive_source_avoided_for_few_bytes(self):
        b = self.blocks
        # bulk (1000 fixed) holds only one small block; must not be read.
        sources = [
            Source("bulk", 1000, 0, {b[0].sha256}),
            Source("edge", 5, 0, {b[i].sha256 for i in range(1, 6)}),
        ]
        plan = plan_reconstruction(b, sources)
        kinds = {i: step.kind for i, step in enumerate(plan.steps)}
        self.assertEqual(kinds[0], KIND_NEW)
        self.assertNotIn("bulk", plan.sources_used)

    def test_expensive_source_paid_off_for_many_bytes(self):
        b = self.blocks
        sources = [
            Source("bulk", 10, 0, {b[i].sha256 for i in range(6)}),
            Source("edge", 5, 2, {b[i].sha256 for i in range(6)}),
        ]
        plan = plan_reconstruction(b, sources, new_byte_cost=1)
        # bulk: 10 flat vs edge: 5 + 2*600 = 1205 vs all new: 600
        self.assertEqual(plan.sources_used, ("bulk",))
        self.assertEqual(plan.total_cost, 10)

    def test_duplicate_block_records_chosen_source(self):
        b = self.blocks
        sources = [
            Source("a", fixed_cost=0, byte_cost=2, hashes={b[0].sha256}),
            Source("b", fixed_cost=0, byte_cost=0, hashes={b[0].sha256}),
        ]
        plan = plan_reconstruction([b[0]], sources)
        self.assertEqual(len(plan.steps), 1)
        self.assertEqual(plan.steps[0].kind, KIND_REUSE)
        self.assertEqual(plan.steps[0].source, "b")

    def test_no_sources_means_all_new(self):
        plan = plan_reconstruction(self.blocks, [])
        self.assertTrue(all(s.kind == KIND_NEW for s in plan.steps))
        self.assertEqual(plan.new_bytes, 600)
        self.assertEqual(plan.reused_bytes, 0)
        self.assertEqual(plan.sources_used, ())

    def test_empty_target(self):
        plan = plan_reconstruction([], [Source("a", 1, 1, set())])
        self.assertEqual(plan.steps, ())
        self.assertEqual(plan.total_cost, 0)

    def test_replan_dropping_source(self):
        b = self.blocks
        sources = [
            Source("bulk", fixed_cost=100, byte_cost=0,
                   hashes={b[i].sha256 for i in range(4)}),
            Source("edge", fixed_cost=5, byte_cost=0,
                   hashes={b[i].sha256 for i in range(2, 6)}),
        ]
        plan = plan_reconstruction(b, sources)
        self.assertIn("bulk", plan.sources_used)
        self.assertIn("edge", plan.sources_used)
        replanned = plan.replan(["edge"])
        # blocks 0..3 still in bulk; 4..5 become new.
        chosen = {i: step.source for i, step in enumerate(replanned.steps)}
        self.assertEqual(chosen[2], "bulk")
        self.assertEqual(chosen[4], None)
        # Original plan is immutable.
        self.assertEqual(plan.steps[4].source, "edge")

    def test_replan_unknown_source_rejected(self):
        plan = plan_reconstruction(self.blocks, self.make_sources())
        with self.assertRaises(ConfigurationError):
            plan.replan(["nope"])
        with self.assertRaises(ConfigurationError):
            plan.replan("edge")

    def test_invalid_source_configuration(self):
        b = self.blocks
        with self.assertRaises(ConfigurationError):
            Source("", 1, 1, set())
        with self.assertRaises(ConfigurationError):
            Source("a", True, 1, set())
        with self.assertRaises(ConfigurationError):
            Source("a", -1, 1, set())
        with self.assertRaises(ConfigurationError):
            Source("a", float("nan"), 1, set())
        with self.assertRaises(ConfigurationError):
            Source("a", float("inf"), 1, set())
        with self.assertRaises(ConfigurationError):
            Source("a", 1, 0, {"not-a-hash"})
        with self.assertRaises(ConfigurationError):
            Source("a", 1, 0, b[0].sha256)  # single str, not iterable set
        with self.assertRaises(ConfigurationError):
            plan_reconstruction(b, [Source("a", 1, 1, set()),
                                    Source("a", 1, 1, set())])
        with self.assertRaises(ConfigurationError):
            plan_reconstruction(b[0], [])  # single Block, not iterable
        with self.assertRaises(ConfigurationError):
            plan_reconstruction(b, [Source(f"s{i}", 0, 0, set())
                                    for i in range(21)])

    def test_plan_with_real_chunker_blocks(self):
        data = mixed_stream(6000, 7)
        blocks = split_stream([data])
        old = split_stream([data[:3000] + data[3000:]])
        source = Source("v1", fixed_cost=0, byte_cost=0,
                        hashes={b.sha256 for b in old})
        plan = plan_reconstruction(blocks, [source])
        self.assertTrue(all(s.source == "v1" for s in plan.steps))
        self.assertEqual(plan.reused_bytes, len(data))


class ExecutionTest(unittest.TestCase):
    def setUp(self):
        self.data = mixed_stream(4096, 42)
        self.blocks = split_stream([self.data])

    def _store(self):
        return {b.sha256: self.data[b.start:b.end] for b in self.blocks}

    def test_full_reuse_matches_target_and_records_sources(self):
        store = self._store()
        src = Source("v1", 1, 0, set(store))
        plan = plan_reconstruction(self.blocks, [src])
        report = execute_plan(plan, {"v1": lambda h: store[h]})
        self.assertEqual(report.data, self.data)
        self.assertEqual(report.sha256,
                         hashlib.sha256(self.data).hexdigest())
        self.assertEqual(report.total_bytes, len(self.data))
        self.assertEqual(report.reused_bytes, len(self.data))
        self.assertEqual(report.new_bytes, 0)
        self.assertEqual(report.reused_blocks, len(self.blocks))
        self.assertEqual(report.sources_used, ("v1",))

    def test_multi_source_reuse_and_new_payloads(self):
        store = self._store()
        half = len(self.blocks) // 2
        src_a = Source("a", 0, 0,
                       {b.sha256 for b in self.blocks[:half]})
        src_b = Source("b", 0, 0,
                       {b.sha256 for b in self.blocks[half:]})
        plan = plan_reconstruction(self.blocks, [src_a, src_b])
        new_hashes = {}
        # Force the last block to be "new" by dropping it from b.
        src_b2 = Source("b", 0, 0,
                        {b.sha256 for b in self.blocks[half:-1]})
        plan = plan_reconstruction(self.blocks, [src_a, src_b2])
        for step in plan.steps:
            if step.kind == KIND_NEW:
                new_hashes[step.block.sha256] = store[step.block.sha256]
        readers = {
            "a": lambda h: store[h],
            "b": lambda h: store[h],
        }
        report = execute_plan(plan, readers, new_hashes)
        self.assertEqual(report.data, self.data)
        self.assertEqual(report.sources_used, ("a", "b"))
        self.assertEqual(report.new_blocks, 1)
        self.assertEqual(report.new_bytes, self.blocks[-1].size)

    def test_wrong_hash_payload_is_rejected(self):
        store = self._store()
        src = Source("v1", 0, 0, set(store))
        plan = plan_reconstruction(self.blocks, [src])

        def corrupt_reader(h):
            good = store[h]
            return good[:-1] + bytes([good[-1] ^ 0xFF])

        with self.assertRaises(BlockVerificationError):
            execute_plan(plan, {"v1": corrupt_reader})

    def test_wrong_size_payload_is_rejected(self):
        store = self._store()
        src = Source("v1", 0, 0, set(store))
        plan = plan_reconstruction(self.blocks, [src])
        with self.assertRaises(BlockVerificationError):
            execute_plan(plan, {"v1": lambda h: store[h] + b"\\x00"})

    def test_non_bytes_payload_rejected(self):
        store = self._store()
        src = Source("v1", 0, 0, set(store))
        plan = plan_reconstruction(self.blocks, [src])
        with self.assertRaises(BlockVerificationError):
            execute_plan(plan, {"v1": lambda h: "not bytes"})

    def test_missing_reader_and_missing_new_data(self):
        store = self._store()
        src = Source("v1", 0, 0, set(store))
        plan = plan_reconstruction(self.blocks, [src])
        with self.assertRaises(MissingBlockDataError):
            execute_plan(plan, {})

        plan_all_new = plan_reconstruction(self.blocks, [])
        with self.assertRaises(MissingBlockDataError):
            execute_plan(plan_all_new, {}, {})
        with self.assertRaises(ConfigurationError):
            execute_plan("not a plan", {})  # type: ignore[arg-type]

    def test_replan_after_source_offline_executes(self):
        store = self._store()
        src = Source("v1", 0, 0, set(store))
        plan = plan_reconstruction(self.blocks, [src])
        replanned = plan.replan(["v1"])
        self.assertTrue(all(s.kind == KIND_NEW for s in replanned.steps))
        report = execute_plan(replanned, {}, store)
        self.assertEqual(report.data, self.data)
        self.assertEqual(report.new_bytes, len(self.data))
        self.assertEqual(report.sources_used, ())


if __name__ == "__main__":
    unittest.main()
