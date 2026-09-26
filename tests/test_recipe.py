"""Tests for the exact offline copy/literal recipe generator."""

from __future__ import annotations

import hashlib
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rabin_cdc import (  # noqa: E402
    MAX_RECIPE_NEW_BYTES,
    MAX_RECIPE_OLD_BYTES,
    CopyInstruction,
    LiteralInstruction,
    RecipeError,
    SourceBlock,
    apply_recipe,
    generate_recipe,
)  # noqa: E402
from tests._recipe_reference import (  # noqa: E402
    apply_recipe_reference,
    enumerate_best_recipe,
)


def plain_instructions(instructions):
    result = []
    for instruction in instructions:
        if isinstance(instruction, LiteralInstruction):
            result.append(("literal", instruction.data))
        elif isinstance(instruction, CopyInstruction):
            result.append(
                (
                    "copy",
                    instruction.block_index,
                    instruction.start,
                    instruction.start + instruction.length,
                )
            )
        else:
            raise AssertionError("unknown instruction")
    return tuple(result)


class RecipeContractTest(unittest.TestCase):
    def test_partial_ranges_repeated_and_cross_block_fragments(self):
        blocks = [b"abcdefgh", b"xyzabcqq", b"abcdefgh"]
        target = b"qqabcxyzDEFgh"
        recipe = generate_recipe(blocks, target)
        block_payloads = [bytes(block) for block in blocks]
        got = plain_instructions(recipe.instructions)
        rebuilt = apply_recipe_reference(got, block_payloads)
        self.assertEqual(rebuilt, target)
        self.assertEqual(apply_recipe(recipe, blocks), target)
        self.assertEqual(recipe.literal_bytes, 3)
        self.assertEqual(
            got,
            (
                ("copy", 1, 6, 8),
                ("copy", 0, 0, 3),
                ("copy", 1, 0, 3),
                ("literal", b"DEF"),
                ("copy", 0, 6, 8),
            ),
        )

    def test_digest_collision_candidate_is_rejected_by_byte_compare(self):
        target_window = b"BBBB"
        forged_digest = hashlib.sha256(target_window).hexdigest()
        blocks = [
            SourceBlock(b"AAAA", sha256=forged_digest),
            b"BBBB",
        ]
        target = b"BBBB!"
        recipe = generate_recipe(blocks, target)
        self.assertEqual(apply_recipe(recipe, blocks), target)
        self.assertGreaterEqual(recipe.rejected_digest_candidates, 1)
        self.assertEqual(plain_instructions(recipe.instructions)[0], ("copy", 1, 0, 4))

    def test_empty_old_and_new_content(self):
        empty_recipe = generate_recipe([b"", b""], b"")
        self.assertEqual(empty_recipe.instructions, ())
        self.assertEqual(apply_recipe(empty_recipe, [b"", b""]), b"")

        recipe = generate_recipe([], b"abc")
        self.assertEqual(recipe.instructions, (LiteralInstruction(b"abc"),))
        self.assertEqual(recipe.literal_bytes, 3)
        self.assertEqual(apply_recipe(recipe, []), b"abc")

    def test_bytes_like_inputs_are_accepted(self):
        recipe = generate_recipe(
            [bytearray(b"ab"), memoryview(b"cd")],
            bytearray(b"dcba"),
        )
        self.assertEqual(apply_recipe(recipe, [b"ab", b"cd"]), b"dcba")

    def test_single_source_block_and_single_bytes_are_accepted(self):
        block = SourceBlock(b"hello world")
        recipe = generate_recipe(block, b"world")
        self.assertEqual(apply_recipe(recipe, block), b"world")
        recipe = generate_recipe(b"hello world", b"hello")
        self.assertEqual(apply_recipe(recipe, b"hello world"), b"hello")

    def test_limits_and_bad_inputs(self):
        with self.assertRaises(RecipeError):
            generate_recipe([b"x" * (MAX_RECIPE_OLD_BYTES + 1)], b"")
        with self.assertRaises(RecipeError):
            generate_recipe([], b"x" * (MAX_RECIPE_NEW_BYTES + 1))
        with self.assertRaises(RecipeError):
            generate_recipe("not-a-block-collection", b"a")
        with self.assertRaises(RecipeError):
            generate_recipe([123], b"a")
        with self.assertRaises(RecipeError):
            generate_recipe([b"a"], "not bytes")
        with self.assertRaises(RecipeError):
            generate_recipe([SourceBlock(b"a", sha256=123)], b"a")

    def test_apply_rejects_invalid_reference(self):
        recipe = generate_recipe([b"abc"], b"ca")
        with self.assertRaises(RecipeError):
            apply_recipe(object(), [b"abc"])
        bad = generate_recipe([b"abcdef"], b"fa")
        bad.instructions  # normal recipe is valid against six bytes
        with self.assertRaises(RecipeError):
            apply_recipe(bad, [b"a"])
        with self.assertRaises(RecipeError):
            apply_recipe(
                type(recipe)(
                    (CopyInstruction(0, 2, 5),),
                    3,
                    5,
                    0,
                ),
                [b"abc"],
            )
        with self.assertRaises(RecipeError):
            apply_recipe(
                type(recipe)(
                    (CopyInstruction(0, 0, 0),),
                    3,
                    0,
                    0,
                ),
                [b"abc"],
            )


class EnumeratedOptimalityTest(unittest.TestCase):
    def test_short_strings_all_partitions_match_brute_force(self):
        block_sets = [
            [b"01", b"10", b"001"],
            [b"111", b"010", b""],
            [b"0", b"1"],
            [],
        ]
        for blocks in block_sets:
            for length in range(9):
                for value in range(1 << length):
                    target = f"{value:0{length}b}".encode() if length else b""
                    recipe = generate_recipe(blocks, target)
                    got = plain_instructions(recipe.instructions)
                    expected_literals, expected = enumerate_best_recipe(
                        target, blocks
                    )
                    self.assertEqual(got, expected, msg=(blocks, target))
                    self.assertEqual(recipe.literal_bytes, expected_literals)
                    self.assertEqual(
                        apply_recipe_reference(got, blocks), target
                    )


if __name__ == "__main__":
    unittest.main()
