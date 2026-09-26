"""Offline assembly recipes: rebuild new content from old blocks.

Public contract (fixed)
-----------------------
* build_recipe(old_blocks, new_content) returns a list of ops.  Each op is
  either CopyChunk(block, start, length) -- copy old_blocks[block]
  [start:start+length] -- or LiteralChunk(data) -- emit data verbatim.
* apply_recipe(old_blocks, recipe) executes a recipe and returns the
  assembled bytes.  Executing the recipe produced by build_recipe yields
  exactly new_content (byte for byte).
* Optimality, in order: (1) fewest literal bytes, (2) fewest ops,
  (3) deterministic reference order: at each output position a copy op is
  preferred over a literal op; copy ops order by (block, start, length);
  literal ops order by length.  The dynamic program compares the whole
  remaining op sequence lexicographically under these keys.
* Limits: total old bytes <= MAX_OLD_TOTAL_BYTES (2048) and new content
  <= MAX_NEW_BYTES (2048).  Anything larger is rejected, not truncated.
* Digests (SHA-256 of 1-byte seeds by default) are used ONLY to bucket
  candidate old positions.  Every candidate is verified by comparing real
  bytes before it may appear in a recipe; a digest collision can never
  make unequal bytes count as equal.

Invalid input never produces a partial recipe: all validation happens
before the search starts.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Callable, Optional, Sequence, Union

from ._errors import (
    InvalidRecipeInputError,
    RecipeExecutionError,
    RecipeTooLargeError,
)

__all__ = [
    "MAX_NEW_BYTES",
    "MAX_OLD_TOTAL_BYTES",
    "CopyChunk",
    "LiteralChunk",
    "RecipeOp",
    "apply_recipe",
    "build_recipe",
    "recipe_cost",
]

#: Total bytes across all old blocks may not exceed this.
MAX_OLD_TOTAL_BYTES = 2048

#: The new content may not exceed this many bytes.
MAX_NEW_BYTES = 2048

BytesLike = Union[bytes, bytearray, memoryview]


@dataclass(frozen=True, slots=True)
class CopyChunk:
    """Copy old_blocks[block][start : start + length] into the output."""

    block: int
    start: int
    length: int


@dataclass(frozen=True, slots=True)
class LiteralChunk:
    """Emit data verbatim into the output."""

    data: bytes


RecipeOp = Union[CopyChunk, LiteralChunk]


def _default_digest(seed: bytes) -> bytes:
    return hashlib.sha256(seed).digest()


def _check_bytes_like(value: object, what: str) -> bytes:
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    if isinstance(value, memoryview):
        if value.ndim == 1 and value.format in ("B", "b", "c"):
            return value.tobytes()
        raise InvalidRecipeInputError(
            f"{what}: memoryview must be a one-dimensional byte view, got "
            f"ndim={value.ndim} format={value.format!r}"
        )
    raise InvalidRecipeInputError(
        f"{what}: expected a bytes-like object, got {type(value).__name__}"
    )


def _check_old_blocks(old_blocks: object) -> list[bytes]:
    if isinstance(old_blocks, (bytes, bytearray, memoryview)):
        raise InvalidRecipeInputError(
            "old_blocks must be a sequence of bytes-like blocks, not a "
            f"single {type(old_blocks).__name__} object"
        )
    if not isinstance(old_blocks, (list, tuple)):
        raise InvalidRecipeInputError(
            "old_blocks must be a list or tuple of bytes-like blocks, got "
            f"{type(old_blocks).__name__}"
        )
    blocks = [
        _check_bytes_like(item, f"old_blocks[{index}]")
        for index, item in enumerate(old_blocks)
    ]
    total = sum(len(block) for block in blocks)
    if total > MAX_OLD_TOTAL_BYTES:
        raise RecipeTooLargeError(
            f"old blocks total {total} bytes, limit is "
            f"{MAX_OLD_TOTAL_BYTES}"
        )
    return blocks


def _check_new_content(new_content: object) -> bytes:
    new = _check_bytes_like(new_content, "new_content")
    if len(new) > MAX_NEW_BYTES:
        raise RecipeTooLargeError(
            f"new content is {len(new)} bytes, limit is {MAX_NEW_BYTES}"
        )
    return new


def _common_prefix(a: bytes, a0: int, b: bytes, b0: int, cap: int) -> int:
    """Length of the common prefix of a[a0:] and b[b0:], capped, in C time."""
    lo, hi = 1, cap
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if a[a0 : a0 + mid] == b[b0 : b0 + mid]:
            lo = mid
        else:
            hi = mid - 1
    return lo


def _match_table(
    old: Sequence[bytes],
    new: bytes,
    digest_fn: Callable[[bytes], object],
) -> list[Optional[list]]:
    """For each new position i, pos_of_len[i][L] = smallest (block, offset)
    whose bytes equal new[i:i+L], for 1 <= L <= max match at i; else None.

    Digests only bucket candidate positions; every candidate is verified
    against the real bytes before use, so injected digest collisions can
    only slow the search down, never corrupt it.
    """
    index: dict[object, list[tuple[int, int]]] = {}
    for block_index, block in enumerate(old):
        for offset in range(len(block)):
            key = digest_fn(block[offset : offset + 1])
            index.setdefault(key, []).append((block_index, offset))

    table: list[Optional[list]] = [None] * len(new)
    for i in range(len(new)):
        candidates = index.get(digest_fn(new[i : i + 1]))
        if not candidates:
            continue
        verified = []
        best = 0
        for block_index, offset in candidates:
            block = old[block_index]
            if offset >= len(block) or block[offset] != new[i]:
                continue  # digest collision or stale bucket: bytes disagree
            cap = min(len(block) - offset, len(new) - i)
            length = _common_prefix(block, offset, new, i, cap)
            verified.append(((block_index, offset), length))
            if length > best:
                best = length
        if not verified:
            continue
        verified.sort()
        pos_of_len = [None] * (best + 1)
        for length in range(1, best + 1):
            for position, span in verified:
                if span >= length:
                    pos_of_len[length] = position
                    break
        table[i] = pos_of_len
    return table


def build_recipe(
    old_blocks: Sequence[BytesLike],
    new_content: BytesLike,
    *,
    digest_fn: Optional[Callable[[bytes], object]] = None,
) -> list[RecipeOp]:
    """Build an optimal assembly recipe for new_content from old_blocks.

    Args:
        old_blocks: list/tuple of bytes-like old blocks (total <= 2048).
        new_content: bytes-like new content (<= 2048 bytes).
        digest_fn: candidate-bucketing digest for 1-byte seeds; defaults to
            SHA-256.  It never decides equality -- bytes do -- so a
            colliding digest_fn only costs time.  Exposed for tests.

    Returns:
        A list of CopyChunk / LiteralChunk ops.  Empty new content gives [].

    Raises:
        InvalidRecipeInputError: a type contract is violated.
        RecipeTooLargeError: a size limit is exceeded.
    """
    old = _check_old_blocks(old_blocks)
    new = _check_new_content(new_content)
    digest = _default_digest if digest_fn is None else digest_fn
    n = len(new)
    if n == 0:
        return []

    matches = _match_table(old, new, digest)

    # Suffix DP.  cost[i] = (literal_bytes, op_count) optimum for new[i:];
    # key[i] adds the deterministic tie-break so equal-cost recipes compare
    # by their op sequence: copy < literal, copies by (block, start,
    # length), literals by length.
    lit = [0] * (n + 1)
    ops = [0] * (n + 1)
    key: list[Optional[tuple]] = [None] * (n + 1)
    key[n] = (0, 0, None, None)
    choice: list[Optional[tuple]] = [None] * n
    for i in range(n - 1, -1, -1):
        best_key = None
        best_choice = None
        pos_of_len = matches[i]
        if pos_of_len is not None:
            for length in range(1, len(pos_of_len)):
                j = i + length
                block_index, offset = pos_of_len[length]
                cand = (
                    lit[j],
                    ops[j] + 1,
                    (0, block_index, offset, length),
                    key[j],
                )
                if best_key is None or cand < best_key:
                    best_key = cand
                    best_choice = ("copy", block_index, offset, length)
        for length in range(1, n - i + 1):
            j = i + length
            cand = (lit[j] + length, ops[j] + 1, (1, length), key[j])
            if best_key is None or cand < best_key:
                best_key = cand
                best_choice = ("lit", length)
        assert best_key is not None and best_choice is not None
        lit[i] = best_key[0]
        ops[i] = best_key[1]
        key[i] = best_key
        choice[i] = best_choice

    recipe: list[RecipeOp] = []
    i = 0
    while i < n:
        step = choice[i]
        assert step is not None
        if step[0] == "copy":
            _, block_index, offset, length = step
            recipe.append(CopyChunk(block_index, offset, length))
            i += length
        else:
            _, length = step
            recipe.append(LiteralChunk(new[i : i + length]))
            i += length
    return recipe


def _check_copy_chunk(op: CopyChunk, old: Sequence[bytes], index: int) -> None:
    for field_name, value in (
        ("block", op.block),
        ("start", op.start),
        ("length", op.length),
    ):
        if type(value) is not int:  # bool is not an int here
            raise InvalidRecipeInputError(
                f"op {index}: CopyChunk.{field_name} must be int, got "
                f"{type(value).__name__}"
            )
    if not 0 <= op.block < len(old):
        raise RecipeExecutionError(
            f"op {index}: block {op.block} out of range "
            f"(0..{len(old) - 1})"
        )
    block_len = len(old[op.block])
    if op.length < 1:
        raise RecipeExecutionError(
            f"op {index}: copy length must be >= 1, got {op.length}"
        )
    if not 0 <= op.start <= block_len - op.length:
        raise RecipeExecutionError(
            f"op {index}: range [{op.start}, {op.start + op.length}) "
            f"outside block {op.block} of {block_len} bytes"
        )


def apply_recipe(
    old_blocks: Sequence[BytesLike],
    recipe: Sequence[RecipeOp],
) -> bytes:
    """Execute a recipe against old_blocks and return the assembled bytes.

    Raises:
        InvalidRecipeInputError: malformed op types or field types.
        RecipeTooLargeError: limits exceeded (inputs or assembled output).
        RecipeExecutionError: an op references bytes outside its block.
    """
    old = _check_old_blocks(old_blocks)
    if isinstance(recipe, (bytes, bytearray, memoryview)) or not isinstance(
        recipe, (list, tuple)
    ):
        raise InvalidRecipeInputError(
            "recipe must be a list or tuple of CopyChunk/LiteralChunk ops, "
            f"got {type(recipe).__name__}"
        )
    out = bytearray()
    for index, op in enumerate(recipe):
        if isinstance(op, CopyChunk):
            _check_copy_chunk(op, old, index)
            block = old[op.block]
            out += block[op.start : op.start + op.length]
        elif isinstance(op, LiteralChunk):
            data = _check_bytes_like(op.data, f"op {index} literal data")
            out += data
        else:
            raise InvalidRecipeInputError(
                f"op {index}: expected CopyChunk or LiteralChunk, got "
                f"{type(op).__name__}"
            )
        if len(out) > MAX_NEW_BYTES:
            raise RecipeTooLargeError(
                f"assembled output exceeds {MAX_NEW_BYTES} bytes at op "
                f"{index}"
            )
    return bytes(out)


def recipe_cost(recipe: Sequence[RecipeOp]) -> tuple[int, int]:
    """Return (literal_bytes, op_count) for a recipe; display helper only."""
    literal = 0
    count = 0
    for op in recipe:
        count += 1
        if isinstance(op, LiteralChunk):
            literal += len(op.data)
    return literal, count
