"""Tests for rabin_cdc.recipe: optimality, collisions, errors, roundtrip.

The independent reference (tests/_recipe_reference.py) never imports the
library; it re-derives execution and optimality from the task statement.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rabin_cdc import (  # noqa: E402
    MAX_NEW_BYTES,
    MAX_OLD_TOTAL_BYTES,
    CopyChunk,
    InvalidRecipeInputError,
    LiteralChunk,
    RecipeExecutionError,
    RecipeTooLargeError,
    apply_recipe,
    build_recipe,
    recipe_cost,
    split_stream,
)

from tests._recipe_reference import (  # noqa: E402
    cost_of,
    enumerate_recipes,
    interpret,
    optimal_cost,
    to_tuple_ops,
)


def constant_digest(seed: bytes) -> bytes:
    """Pathological digest: everything collides into one bucket."""
    return b"collision"


class RoundtripTests(unittest.TestCase):
    def test_repeated_blocks(self):
        old = [b"abcde" * 10, b"abcde" * 10, b"XY" * 25]
        new = b"abcdeabcde" + b"!!" + b"XYXYXY" + b"deabc"
        recipe = build_recipe(old, new)
        self.assertEqual(apply_recipe(old, recipe), new)
        self.assertEqual(recipe_cost(recipe), optimal_cost(old, new))

    def test_cross_block_fragment(self):
        # The same 6-byte fragment lives inside two different blocks; the
        # deterministic reference order must pick block 0.
        old = [b"012345shared_fragment_xyz", b"aaashared_fragment_zzz"]
        new = b"shared_fragment"
        recipe = build_recipe(old, new)
        self.assertEqual(apply_recipe(old, recipe), new)
        self.assertEqual(recipe, [CopyChunk(0, 6, 15)])

    def test_partial_block_ranges(self):
        old = [b"hello world, this is one old block"]
        new = b"world, hello"
        recipe = build_recipe(old, new)
        self.assertEqual(apply_recipe(old, recipe), new)
        self.assertEqual(recipe_cost(recipe), optimal_cost(old, new))
        # "world," and " hello" are copyable; only ", " needs literals...
        # exact split is the DP's choice, but literal bytes must be minimal.
        literal = sum(len(op.data) for op in recipe if isinstance(op, LiteralChunk))
        self.assertEqual(literal, optimal_cost(old, new)[0])

    def test_empty_new_content(self):
        self.assertEqual(build_recipe([b"abc"], b""), [])
        self.assertEqual(apply_recipe([b"abc"], []), b"")

    def test_empty_old_and_empty_new(self):
        self.assertEqual(build_recipe([], b""), [])
        self.assertEqual(apply_recipe([], []), b"")

    def test_no_old_blocks_all_literal(self):
        new = b"brand new content"
        recipe = build_recipe([], new)
        self.assertEqual(recipe, [LiteralChunk(new)])
        self.assertEqual(apply_recipe([], recipe), new)

    def test_empty_old_block_entries_ignored(self):
        old = [b"", b"abc", b""]
        new = b"abcabc"
        recipe = build_recipe(old, new)
        self.assertEqual(apply_recipe(old, recipe), new)
        self.assertEqual(recipe, [CopyChunk(1, 0, 3), CopyChunk(1, 0, 3)])

    def test_new_content_larger_than_any_block(self):
        old = [b"ab"]
        new = b"ab" * 100
        recipe = build_recipe(old, new)
        self.assertEqual(apply_recipe(old, recipe), new)
        self.assertEqual(recipe_cost(recipe), optimal_cost(old, new))


class DigestCollisionTests(unittest.TestCase):
    def test_constant_digest_still_exact(self):
        # Every 1-byte seed hashes the same: candidates are all positions,
        # and only byte comparison may decide.  Result must stay optimal.
        old = [b"alpha", b"beta", b"gamma", b"alpha"]
        new = b"alphabetagamma!beta"
        for digest in (None, constant_digest):
            recipe = build_recipe(old, new, digest_fn=digest)
            self.assertEqual(apply_recipe(old, recipe), new)
            self.assertEqual(recipe_cost(recipe), optimal_cost(old, new))

    def test_collision_never_fakes_equality(self):
        # With a colliding digest, b"B" is a candidate for b"A"; bytes must
        # overrule the digest, so the only honest recipe is a literal.
        old = [b"BBB"]
        new = b"AAA"
        recipe = build_recipe(old, new, digest_fn=constant_digest)
        self.assertEqual(recipe, [LiteralChunk(b"AAA")])


class ExhaustiveOptimalityTests(unittest.TestCase):
    """Short strings: enumerate every feasible recipe, prove DP optimal."""

    CASES = [
        ([b"ab", b"ba"], b"abba"),
        ([b"aa", b"aa"], b"aaaa"),
        ([b"abc", b"def"], b"defabcx"),
        ([b"aba"], b"ababa"),
        ([b"xy", b"xy", b"z"], b"zxyxy"),
        ([b"abc"], b"cba"),
    ]

    def test_exhaustive(self):
        for old, new in self.CASES:
            with self.subTest(old=old, new=new):
                recipe = build_recipe(old, new)
                tuple_ops = to_tuple_ops(recipe)
                # Independent interpreter confirms the bytes.
                self.assertEqual(interpret(old, tuple_ops), new)
                got_cost = cost_of(tuple_ops)
                # Enumerate all feasible recipes within the claimed budget.
                best = None
                for candidate in enumerate_recipes(old, new, *got_cost):
                    self.assertEqual(interpret(old, candidate), new)
                    cand_cost = cost_of(candidate)
                    if best is None or cand_cost < best:
                        best = cand_cost
                self.assertIsNotNone(best)
                self.assertEqual(got_cost, best)
                self.assertEqual(got_cost, optimal_cost(old, new))


class ErrorPathTests(unittest.TestCase):
    def test_old_blocks_single_bytes_rejected(self):
        with self.assertRaises(InvalidRecipeInputError):
            build_recipe(b"not a sequence of blocks", b"x")

    def test_old_blocks_bad_container(self):
        with self.assertRaises(InvalidRecipeInputError):
            build_recipe(123, b"x")

    def test_old_block_item_type(self):
        for bad in (123, True, 1.5, None, ["x"]):
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidRecipeInputError):
                    build_recipe([b"ok", bad], b"x")

    def test_new_content_type(self):
        for bad in (123, True, 1.5, None, "text"):
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidRecipeInputError):
                    build_recipe([b"ok"], bad)

    def test_old_total_limit(self):
        old = [b"x" * (MAX_OLD_TOTAL_BYTES + 1)]
        with self.assertRaises(RecipeTooLargeError):
            build_recipe(old, b"x")
        with self.assertRaises(RecipeTooLargeError):
            apply_recipe(old, [])

    def test_new_content_limit(self):
        with self.assertRaises(RecipeTooLargeError):
            build_recipe([], b"x" * (MAX_NEW_BYTES + 1))

    def test_apply_recipe_output_limit(self):
        recipe = [LiteralChunk(b"x" * (MAX_NEW_BYTES + 1))]
        with self.assertRaises(RecipeTooLargeError):
            apply_recipe([], recipe)

    def test_apply_recipe_bad_container(self):
        with self.assertRaises(InvalidRecipeInputError):
            apply_recipe([], b"not a recipe")
        with self.assertRaises(InvalidRecipeInputError):
            apply_recipe([], 42)

    def test_apply_recipe_unknown_op(self):
        with self.assertRaises(InvalidRecipeInputError):
            apply_recipe([], [("copy", 0, 0, 1)])

    def test_copy_chunk_field_types(self):
        old = [b"abc"]
        for bad_op in (
            CopyChunk(True, 0, 1),
            CopyChunk(0, 1.5, 1),
            CopyChunk(0, 0, "1"),
        ):
            with self.subTest(bad_op=bad_op):
                with self.assertRaises(InvalidRecipeInputError):
                    apply_recipe(old, [bad_op])

    def test_copy_chunk_out_of_range(self):
        old = [b"abc"]
        for bad_op in (
            CopyChunk(1, 0, 1),   # no such block
            CopyChunk(-1, 0, 1),  # negative block
            CopyChunk(0, 2, 2),   # range past block end
            CopyChunk(0, -1, 2),  # negative start
            CopyChunk(0, 0, 0),   # empty copy
        ):
            with self.subTest(bad_op=bad_op):
                with self.assertRaises(RecipeExecutionError):
                    apply_recipe(old, [bad_op])

    def test_literal_data_type(self):
        with self.assertRaises(InvalidRecipeInputError):
            apply_recipe([], [LiteralChunk(123)])

    def test_bytes_like_inputs_accepted(self):
        old = (bytearray(b"abc"), memoryview(b"def"))
        recipe = build_recipe(old, memoryview(b"abcdef"))
        self.assertEqual(apply_recipe(list(old), recipe), b"abcdef")


class ChunkerIntegrationTests(unittest.TestCase):
    """Old blocks come from split_stream; chunker behaviour is unchanged."""

    def test_recipe_over_chunked_old_content(self):
        old_content = bytes(range(256)) * 4  # 1024 bytes
        blocks = split_stream([old_content])
        self.assertTrue(blocks)
        old = [old_content[b.start : b.end] for b in blocks]
        # New content: a slice of the old stream plus an edit.
        new = old_content[100:900] + b"<edited>" + old_content[40:120]
        recipe = build_recipe(old, new)
        self.assertEqual(apply_recipe(old, recipe), new)
        self.assertEqual(recipe_cost(recipe), optimal_cost(old, new))
        # old_content contains every byte value, so even the "<edited>"
        # bytes are individually copyable: the optimum is 0 literal bytes.
        self.assertEqual(recipe_cost(recipe)[0], 0)


class FuzzOptimalityTests(unittest.TestCase):
    """Small deterministic fuzz against the brute-force reference."""

    def test_random_small_cases(self):
        state = 0xC0FFEE

        def rand(mod):
            nonlocal state
            state = (state * 1103515245 + 12345) & 0x7FFFFFFF
            return (state >> 16) % mod

        alphabet = b"abc"
        for trial in range(150):
            old = [
                bytes(alphabet[rand(3)] for _ in range(rand(9)))
                for _ in range(rand(4))
            ]
            new = bytes(alphabet[rand(3)] for _ in range(1 + rand(10)))
            recipe = build_recipe(old, new)
            self.assertEqual(apply_recipe(old, recipe), new)
            self.assertEqual(
                recipe_cost(recipe),
                optimal_cost(old, new),
                f"trial {trial}: old={old!r} new={new!r}",
            )


if __name__ == "__main__":
    unittest.main()
