#!/usr/bin/env python3
"""Fixed, fully computed demonstration of offline recipe generation."""

from __future__ import annotations

import hashlib
import time

from rabin_cdc import (
    CopyInstruction,
    LiteralInstruction,
    SourceBlock,
    apply_recipe,
    generate_recipe,
)


def lcg_bytes(length: int, seed: int) -> bytes:
    value = seed & 0x7FFFFFFF
    out = bytearray(length)
    for index in range(length):
        value = (value * 1103515245 + 12345) & 0x7FFFFFFF
        out[index] = (value >> 16) & 0xFF
    return bytes(out)


def main() -> int:
    started = time.perf_counter()
    base = lcg_bytes(768, 20260926)
    old_blocks = [base[:256], base[256:512], base[512:]]

    moved = old_blocks[1][40:120] + old_blocks[0][10:80]
    target = b"PREFIX-" + moved + b"-SUFFIX"

    collision_window = old_blocks[2][:64]
    forged = SourceBlock(
        b"X" * 64,
        sha256=hashlib.sha256(collision_window).hexdigest(),
    )
    candidate_blocks = old_blocks + [forged]
    target = collision_window + target + collision_window[48:]

    recipe = generate_recipe(candidate_blocks, target)
    rebuilt = apply_recipe(recipe, candidate_blocks)

    copy_bytes = sum(
        instruction.length
        for instruction in recipe.instructions
        if isinstance(instruction, CopyInstruction)
    )
    print("=" * 72)
    print("离线 copy/literal 配方演示（2048 字节内精确动态规划）")
    print("=" * 72)
    print(f"旧块数/总字节       : {len(candidate_blocks)} / {recipe.old_size}")
    print(f"新内容字节           : {recipe.new_size}")
    print(f"指令数               : {len(recipe.instructions)}")
    print(f"literal 字节         : {recipe.literal_bytes}")
    print(f"copy 字节            : {copy_bytes}")
    print(f"伪造摘要候选拒绝次数 : {recipe.rejected_digest_candidates}")
    print(f"独立拼接字节一致     : {rebuilt == target}")
    print(f"SHA-256(结果)        : {hashlib.sha256(rebuilt).hexdigest()}")
    print("-" * 72)
    for index, instruction in enumerate(recipe.instructions):
        if isinstance(instruction, CopyInstruction):
            print(
                f"  {index:02d}: copy block={instruction.block_index} "
                f"[{instruction.start},{instruction.start + instruction.length})"
            )
        else:
            assert isinstance(instruction, LiteralInstruction)
            preview = instruction.data
            if len(preview) > 24:
                preview = preview[:24] + b"..."
            print(f"  {index:02d}: literal {preview!r}")
    elapsed = time.perf_counter() - started
    print("-" * 72)
    print(f"真实计算耗时: {elapsed:.3f}s（无 sleep、无预录答案）")
    return 0 if rebuilt == target else 1


if __name__ == "__main__":
    raise SystemExit(main())
