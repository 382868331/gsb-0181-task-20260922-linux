#!/usr/bin/env python3
"""Fixed-input demo for the offline assembly recipe generator.

Run with::

    python demo_iteration.py

Everything is computed live (well under 90 seconds; typically < 1s):

1. Deterministic old content is chunked with split_stream; the chunk
   payloads become the old blocks.
2. A new version is assembled by rearranging/editing the old bytes, an
   optimal recipe is generated, executed, and verified byte for byte.
3. Two real rejections are triggered: oversize new content and a
   tampered recipe op.
"""

from __future__ import annotations

import time

from rabin_cdc import (
    MAX_NEW_BYTES,
    CopyChunk,
    LiteralChunk,
    RecipeExecutionError,
    RecipeTooLargeError,
    apply_recipe,
    build_recipe,
    recipe_cost,
    split_stream,
)


def lcg_bytes(length: int, seed: int) -> bytes:
    """Deterministic in-repo sample data (glibc-style LCG)."""
    x = seed & 0x7FFFFFFF
    out = bytearray(length)
    for i in range(length):
        x = (x * 1103515245 + 12345) & 0x7FFFFFFF
        # Restrict to 32 distinct byte values so the inserted ASCII header
        # below genuinely needs literal bytes (not everything is copyable).
        out[i] = (x >> 16) % 32
    return bytes(out)


def main() -> None:
    started = time.perf_counter()

    # 1. Old content -> content-defined chunks -> old blocks.
    old_content = lcg_bytes(1600, seed=20260926)
    blocks = split_stream([old_content])
    old_blocks = [old_content[b.start : b.end] for b in blocks]
    print(f"old content: {len(old_content)} bytes -> {len(blocks)} chunks")

    # 2. New version: keep the middle, swap two regions, edit a few bytes.
    new_content = (
        old_content[700:1500]
        + b"<inserted v2 header>"
        + old_content[100:650]
        + old_content[1500:1600]
    )
    recipe = build_recipe(old_blocks, new_content)
    rebuilt = apply_recipe(old_blocks, recipe)
    assert rebuilt == new_content, "recipe must rebuild new content exactly"

    literal_bytes, op_count = recipe_cost(recipe)
    copied = sum(
        op.length for op in recipe if isinstance(op, CopyChunk)
    )
    literals = sum(1 for op in recipe if isinstance(op, LiteralChunk))
    reuse = copied / len(new_content) if new_content else 1.0
    print(
        f"new content: {len(new_content)} bytes -> recipe of {op_count} ops "
        f"({op_count - literals} copy / {literals} literal)"
    )
    print(
        f"copied {copied} bytes from old blocks, "
        f"literal {literal_bytes} bytes, reuse {reuse:.4%}"
    )
    print(f"byte-exact rebuild verified: {rebuilt == new_content}")

    # 3a. Real rejection: new content above the 2048-byte limit.
    try:
        build_recipe(old_blocks, b"x" * (MAX_NEW_BYTES + 1))
    except RecipeTooLargeError as exc:
        print(f"rejected oversize new content: {exc}")

    # 3b. Real rejection: tampered recipe references outside its block.
    tampered = [CopyChunk(0, 10**6, 4)]
    try:
        apply_recipe(old_blocks, tampered)
    except RecipeExecutionError as exc:
        print(f"rejected tampered recipe: {exc}")

    elapsed = time.perf_counter() - started
    print(f"computed live in {elapsed:.3f}s (budget: 90s)")


if __name__ == "__main__":
    main()
