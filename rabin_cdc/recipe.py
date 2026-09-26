"""Offline byte-exact copy/literal recipe generation.

The generator is deliberately small: it accepts at most 2048 old bytes and
2048 new bytes, models every useful copy start with dynamic programming, and
returns instructions that can be checked independently by concatenation.

Block digests are only an index.  Every digest hit is followed by an exact
byte comparison; a digest match by itself never produces a copy.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Iterable, NamedTuple, Optional, Union

from ._errors import RecipeError

__all__ = [
    "MAX_RECIPE_OLD_BYTES",
    "MAX_RECIPE_NEW_BYTES",
    "SourceBlock",
    "LiteralInstruction",
    "CopyInstruction",
    "Instruction",
    "Recipe",
    "generate_recipe",
    "apply_recipe",
]

MAX_RECIPE_OLD_BYTES = 2048
MAX_RECIPE_NEW_BYTES = 2048

BytesLike = Union[bytes, bytearray, memoryview]
BlockInput = Union[BytesLike, "SourceBlock"]


@dataclass(frozen=True, slots=True)
class SourceBlock:
    """One old block and, optionally, its caller-supplied digest."""

    payload: bytes
    sha256: Optional[str] = None


@dataclass(frozen=True, slots=True)
class LiteralInstruction:
    """Append literal bytes that are not available from an old block."""

    data: bytes


@dataclass(frozen=True, slots=True)
class CopyInstruction:
    """Append the half-open old-block slice ``[start, start + length)``."""

    block_index: int
    start: int
    length: int

    @property
    def end(self) -> int:
        return self.start + self.length


Instruction = Union[LiteralInstruction, CopyInstruction]


@dataclass(frozen=True, slots=True)
class Recipe:
    """A materializable sequence of copy and literal instructions."""

    instructions: tuple[Instruction, ...]
    old_size: int
    new_size: int
    literal_bytes: int
    rejected_digest_candidates: int = 0


class _Choice(NamedTuple):
    literal_bytes: int
    instruction_count: int
    instruction: Instruction
    next_position: int
    sequence_key: tuple


def _as_bytes(value: object, what: str) -> bytes:
    if isinstance(value, bool) or not isinstance(
        value, (bytes, bytearray, memoryview)
    ):
        raise RecipeError(
            f"{what} must be bytes-like, got {type(value).__name__}"
        )
    if isinstance(value, memoryview):
        if value.format != "B" or value.itemsize != 1 or value.ndim != 1:
            raise RecipeError(
                f"{what} memoryview must be one-dimensional unsigned bytes"
            )
    return bytes(value)


def _normalize_blocks(old_blocks: object) -> list[SourceBlock]:
    if isinstance(
        old_blocks, (bytes, bytearray, memoryview, SourceBlock)
    ):
        candidates = [old_blocks]
    else:
        try:
            candidates = list(old_blocks)  # type: ignore[arg-type]
        except TypeError as exc:
            raise RecipeError(
                "old_blocks must be a SourceBlock or an iterable of blocks"
            ) from exc

    blocks: list[SourceBlock] = []
    total = 0
    for index, candidate in enumerate(candidates):
        if isinstance(candidate, SourceBlock):
            payload = _as_bytes(candidate.payload, f"old block {index} payload")
            digest = candidate.sha256
            if digest is not None and not isinstance(digest, str):
                raise RecipeError(f"old block {index} sha256 must be a string")
        else:
            payload = _as_bytes(candidate, f"old block {index}")
            digest = None
        if digest is None:
            digest = hashlib.sha256(payload).hexdigest()
        total += len(payload)
        if total > MAX_RECIPE_OLD_BYTES:
            raise RecipeError(
                f"total old content is {total} bytes, exceeding the "
                f"{MAX_RECIPE_OLD_BYTES} byte limit"
            )
        blocks.append(SourceBlock(payload, digest))
    return blocks


def _verified_digest_hits(
    blocks: list[SourceBlock], new_content: bytes
) -> int:
    """Exercise the digest index and count false digest candidates.

    Target-window digests are grouped by length.  A source digest only
    selects candidates; equality is then decided with ``==`` on bytes.
    """

    lengths = {len(block.payload) for block in blocks}
    target_by_length_digest: dict[tuple[int, str], list[tuple[int, bytes]]] = {}
    for length in lengths:
        if length == 0 or length > len(new_content):
            continue
        for start in range(len(new_content) - length + 1):
            window = new_content[start : start + length]
            digest = hashlib.sha256(window).hexdigest()
            target_by_length_digest.setdefault((length, digest), []).append(
                (start, window)
            )

    rejected = 0
    for block in blocks:
        hits = target_by_length_digest.get((len(block.payload), block.sha256))
        if hits is None:
            continue
        for _start, window in hits:
            if block.payload != window:
                rejected += 1
    return rejected


def generate_recipe(
    old_blocks: BlockInput | Iterable[BlockInput], new_content: BytesLike
) -> Recipe:
    """Return an exact recipe from old block bytes to ``new_content``.

    Optimization order is literal bytes, then instruction count, then the
    deterministic copy order ``(block_index, start, end)``.
    """

    blocks = _normalize_blocks(old_blocks)
    target = _as_bytes(new_content, "new_content")
    if len(target) > MAX_RECIPE_NEW_BYTES:
        raise RecipeError(
            f"new_content is {len(target)} bytes, exceeding the "
            f"{MAX_RECIPE_NEW_BYTES} byte limit"
        )

    rejected = _verified_digest_hits(blocks, target)
    n = len(target)
    old_alphabet = {byte for block in blocks for byte in block.payload}

    # lcp[block][offset] is recomputed for one target start i at a time.
    # Before block/offset is updated, it holds the LCP at target start i+1;
    # ascending offsets preserve the previous target row at offset + 1.
    lcp: list[list[int]] = [[0] * len(block.payload) for block in blocks]
    choices: list[Optional[_Choice]] = [None] * (n + 1)
    sequence_keys: list[Optional[tuple]] = [None] * (n + 1)
    suffix_ranks: list[tuple] = [()] * (n + 1)
    choices[n] = _Choice(0, 0, LiteralInstruction(b""), n, ())
    sequence_keys[n] = ()
    suffix_ranks[n] = ()

    tree_size = 1
    while tree_size < n + 1:
        tree_size <<= 1
    infinity = (n + 2, n + 2, n + 2, (3,))
    tree: list[tuple] = [infinity] * (2 * tree_size)
    tree[tree_size + n] = (0, 0, n, ())
    position = tree_size + n
    while position > 1:
        position >>= 1
        tree[position] = min(tree[position << 1], tree[position << 1 | 1])

    def range_best(left: int, right: int) -> tuple:
        left += tree_size
        right += tree_size
        result = infinity
        while left < right:
            if left & 1:
                result = min(result, tree[left])
                left += 1
            if right & 1:
                right -= 1
                result = min(result, tree[right])
            left >>= 1
            right >>= 1
        return result

    for i in range(n - 1, -1, -1):
        best: Optional[_Choice] = None

        for block_index, block in enumerate(blocks):
            payload = block.payload
            block_lcp = lcp[block_index]
            for start in range(len(payload)):
                if target[i] != payload[start]:
                    block_lcp[start] = 0
                    continue
                if start + 1 < len(payload):
                    block_lcp[start] = block_lcp[start + 1] + 1
                else:
                    block_lcp[start] = 1
                length = min(block_lcp[start], n - i)
                best_end_literals, _suffix_count, end, _suffix_key = (
                    range_best(i + 1, i + length + 1)
                )
                suffix = choices[end]
                assert suffix is not None
                sequence_key = (
                    0,
                    block_index,
                    start,
                    end,
                    suffix_ranks[end],
                )
                choice = _Choice(
                    best_end_literals,
                    suffix.instruction_count + 1,
                    CopyInstruction(block_index, start, end - i),
                    end,
                    sequence_key,
                )
                compare_key = (
                    choice.literal_bytes,
                    choice.instruction_count,
                    sequence_key,
                )
                best_compare = None if best is None else (
                    best.literal_bytes,
                    best.instruction_count,
                    best.sequence_key,
                )
                if best is None or compare_key < best_compare:
                    best = choice

        if best is None:
            run_end = i + 1
            while run_end < n and target[run_end] not in old_alphabet:
                run_end += 1
            suffix = choices[run_end]
            assert suffix is not None
            sequence_key = (1, bytes(target[i:run_end]), suffix_ranks[run_end])
            best = _Choice(
                run_end - i + suffix.literal_bytes,
                suffix.instruction_count + 1,
                LiteralInstruction(target[i:run_end]),
                run_end,
                sequence_key,
            )
        else:
            end = best.next_position
            if isinstance(best.instruction, CopyInstruction):
                copy = best.instruction
                sequence_key = (
                    0,
                    copy.block_index,
                    copy.start,
                    end,
                    suffix_ranks[end],
                )
            else:
                sequence_key = (1, best.instruction.data, suffix_ranks[end])
            best = _Choice(
                best.literal_bytes,
                best.instruction_count,
                best.instruction,
                end,
                sequence_key,
            )
        choices[i] = best
        sequence_keys[i] = best.sequence_key
        suffix_ranks[i] = best.sequence_key
        leaf = tree_size + i
        tree[leaf] = (
            best.literal_bytes,
            best.instruction_count,
            i,
            best.sequence_key,
        )
        leaf >>= 1
        while leaf:
            tree[leaf] = min(tree[leaf << 1], tree[leaf << 1 | 1])
            leaf >>= 1

    start = 0
    instructions: list[Instruction] = []
    while start < n:
        choice = choices[start]
        assert choice is not None
        instructions.append(choice.instruction)
        start = choice.next_position

    root = choices[0]
    assert root is not None
    return Recipe(
        instructions=tuple(instructions),
        old_size=sum(len(block.payload) for block in blocks),
        new_size=n,
        literal_bytes=root.literal_bytes,
        rejected_digest_candidates=rejected,
    )


def apply_recipe(
    recipe: Recipe, old_blocks: BlockInput | Iterable[BlockInput]
) -> bytes:
    """Materialize a recipe and validate every reference."""

    if not isinstance(recipe, Recipe):
        raise RecipeError("recipe must be a Recipe instance")
    blocks = _normalize_blocks(old_blocks)
    output = bytearray()
    for index, instruction in enumerate(recipe.instructions):
        if isinstance(instruction, LiteralInstruction):
            if isinstance(instruction.data, bool) or not isinstance(
                instruction.data, (bytes, bytearray, memoryview)
            ):
                raise RecipeError(
                    f"instruction {index}: literal data must be bytes"
                )
            data = _as_bytes(instruction.data, f"instruction {index} literal")
            output.extend(data)
        elif isinstance(instruction, CopyInstruction):
            if (
                isinstance(instruction.block_index, bool)
                or isinstance(instruction.start, bool)
                or isinstance(instruction.length, bool)
                or not isinstance(instruction.block_index, int)
                or not isinstance(instruction.start, int)
                or not isinstance(instruction.length, int)
            ):
                raise RecipeError(
                    f"instruction {index}: copy fields must be ints"
                )
            if instruction.block_index < 0 or instruction.block_index >= len(blocks):
                raise RecipeError(
                    f"instruction {index}: block index {instruction.block_index} "
                    f"is outside 0..{len(blocks) - 1}"
                )
            payload = blocks[instruction.block_index].payload
            start = instruction.start
            length = instruction.length
            if start < 0 or length <= 0 or start + length > len(payload):
                raise RecipeError(
                    f"instruction {index}: slice [{start},{start + length}) "
                    f"exceeds block length {len(payload)}"
                )
            output.extend(payload[start : start + length])
        else:
            raise RecipeError(f"instruction {index}: unknown instruction type")
    return bytes(output)
