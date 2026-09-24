#!/usr/bin/env python3
"""Fixed-input demonstration for rabin_cdc.

Run with::

    python demo.py

It really computes (no pre-recorded output, no sleep):

1. Builds a deterministic ~200 KB byte stream, content-chunks it, inserts a
   single byte in the middle, chunks again, and reports the *reusable block
   ratio* (blocks addressed by SHA-256 that survive the one-byte edit), plus
   the boundary realignment point.
2. Demonstrates ``replace_region``: a middle replacement that returns the
   explicit kept/replaced/added block mapping (including a cross-boundary
   edit), head/tail boundary edits, and the stable identity of repeated
   content.
3. Triggers real input-contract rejections (one byte over the 1 MiB
   limit; a non-bytes batch; an out-of-bounds replacement range) and prints
   the actual raised errors.
"""

from __future__ import annotations

import hashlib
import sys
import time

from rabin_cdc import (
    MAX_TOTAL_BYTES,
    Block,
    CDCError,
    InputTooLargeError,
    InvalidBatchTypeError,
    InvalidEditError,
    replace_region,
    split_stream,
)

SEED = 20260922
STREAM_LEN = 200_000
INSERT_AT = STREAM_LEN // 3
INSERTED_BYTE = b"\xA5"


def lcg_bytes(length: int, seed: int) -> bytes:
    """glibc-style LCG: deterministic in-repo sample data, no attachments."""
    x = seed & 0x7FFFFFFF
    out = bytearray(length)
    for i in range(length):
        x = (x * 1103515245 + 12345) & 0x7FFFFFFF
        out[i] = (x >> 16) & 0xFF
    return bytes(out)


def reason_counts(blocks: list[Block]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for block in blocks:
        counts[block.reason] = counts.get(block.reason, 0) + 1
    return counts


def boundaries(blocks: list[Block]) -> list[int]:
    return [b.end for b in blocks]


def main() -> int:
    started = time.perf_counter()

    data = lcg_bytes(STREAM_LEN, SEED)
    modified = data[:INSERT_AT] + INSERTED_BYTE + data[INSERT_AT:]

    original = split_stream([data])
    edited = split_stream([modified])

    print("=" * 72)
    print("内容定义分块演示 (content-defined chunking, GF(2) Rabin fingerprint)")
    print("=" * 72)
    print(f"原始流长度        : {len(data)} bytes (确定性 LCG, seed={SEED})")
    print(f"插入位置 / 字节   : {INSERT_AT} / 0x{INSERTED_BYTE[0]:02X}")
    print(f"原始分块数        : {len(original)}  {reason_counts(original)}")
    print(f"插入后分块数      : {len(edited)}  {reason_counts(edited)}")
    nontail = [b for b in original if b.reason != "tail"]
    print(
        f"非尾块大小范围    : {min(b.size for b in nontail)} .. "
        f"{max(b.size for b in nontail)} bytes (最小 64, 最大 256)"
    )

    # --- Reusable block ratio: SHA-256 content addressing -----------------
    edited_hashes = {b.sha256 for b in edited}
    reused = [b for b in original if b.sha256 in edited_hashes]
    reused_bytes = sum(b.size for b in reused)
    block_ratio = len(reused) / len(original)
    byte_ratio = reused_bytes / len(data)
    print("-" * 72)
    print("抗边界扰动 / 去重复用 (按 SHA-256 内容寻址)")
    print(f"  可复用块数比例    : {len(reused)}/{len(original)} = {block_ratio:.4%}")
    print(f"  可复用字节比例    : {reused_bytes}/{len(data)} = {byte_ratio:.4%}")

    # --- Boundary realignment point --------------------------------------
    old_bounds = boundaries(original)
    new_bounds = boundaries(edited)
    shared_prefix = 0
    for a, b in zip(old_bounds, new_bounds):
        if a != b:
            break
        shared_prefix += 1
    # First post-recovery original block found unchanged (offset shifts +1).
    first_realiged = None
    edited_by_hash = {b.sha256: b for b in edited}
    for block in original:
        if block.start > INSERT_AT and block.sha256 in edited_by_hash:
            twin = edited_by_hash[block.sha256]
            first_realiged = (block, twin)
            break
    print("-" * 72)
    print(
        f"  插入点前一致边界  : 前 {shared_prefix} 个切块点完全相同"
    )
    if first_realiged is not None:
        block, twin = first_realiged
        print(
            f"  恢复后的首个同块 : 原始 [{block.start},{block.end}) "
            f"-> 插入后 [{twin.start},{twin.end}), 仅整体后移 "
            f"{twin.start - block.start} 字节, 大小/SHA-256 不变"
        )
    print(f"  示例首块 SHA-256  : {original[0].sha256}")

    # --- replace_region: middle edit -> kept/replaced/added mapping ------
    print("-" * 72)
    print("中间替换入口 replace_region(obj, start, end, replacement)")
    boundary = original[len(original) // 2].start
    rep_start = boundary - 5          # straddles a real chunk boundary
    rep_end = boundary + 7
    rep = lcg_bytes(50, SEED ^ 0x55AA)
    res = replace_region(data, rep_start, rep_end, rep)
    print(
        f"  跨边界替换        : [{rep_start},{rep_end}) -> {len(rep)} bytes "
        f"(边界点 {boundary}, 长度变化 {res.length_delta:+d})"
    )
    print(
        f"  旧块 {len(res.old_blocks)} 个 -> 新块 {len(res.new_blocks)} 个; "
        f"保留 {len(res.kept)} / 替换 {len(res.replaced)} / 新增 {len(res.added)}"
    )
    moved = [k for k in res.kept if k.offset_shift]
    fixed = [k for k in res.kept if not k.offset_shift]
    print(
        f"  保留块中偏移不变  : {len(fixed)} 个; 仅整体平移: {len(moved)} 个 "
        f"(平移量集合 {sorted({k.offset_shift for k in moved})})"
    )
    churned_old = sorted(b.start for b in res.replaced)
    churned_new = sorted(b.start for b in res.added)
    print(f"  被替换旧块起点    : {churned_old}")
    print(f"  新增块起点        : {churned_new}")
    # Hand-checkable accounting identity on every call.
    assert len(res.kept) + len(res.replaced) == len(res.old_blocks)
    assert len(res.kept) + len(res.added) == len(res.new_blocks)

    # Head and tail boundary edits: prefix/suffix behavior.
    head = replace_region(data, 0, 0, b"\x5A" * 3)
    far = [k for k in head.kept if k.old.start >= 32 + 256]
    print(
        f"  首部插入 3 字节   : 恢复区 {len(far)} 个保留块全部后移 "
        f"{sorted({k.offset_shift for k in far})[0]} 字节"
    )
    tail = replace_region(data, len(data) - 10, len(data), b"zzz")
    print(
        f"  尾部替换          : 保留 {len(tail.kept)} / 替换 "
        f"{len(tail.replaced)} / 新增 {len(tail.added)}, 保留块偏移全为 0: "
        f"{all(k.offset_shift == 0 for k in tail.kept)}"
    )
    # Identical content anywhere shares one stable identity.
    dup = bytes(64) * 3 + lcg_bytes(900, SEED)
    dres = replace_region(dup, len(dup), len(dup), b"\x00" * 12)
    zero_hash = hashlib.sha256(bytes(64)).hexdigest()
    print(
        f"  相同内容稳定标识  : 3 个相同的 64 零字节块配对保留 "
        f"{sum(k.old.sha256 == zero_hash for k in dres.kept)} 个, "
        f"标识是 SHA-256 而非位置"
    )

    # --- Real contract rejections ----------------------------------------
    print("-" * 72)
    print("接口边界拒绝 (实际触发, 非预录):")
    try:
        split_stream([b"\x00" * (MAX_TOTAL_BYTES + 1)])
    except InputTooLargeError as exc:
        print(f"  [OK] 超过 1 MiB -> {type(exc).__name__}: {exc}")

    try:
        split_stream(["not-a-bytes-batch"])
    except InvalidBatchTypeError as exc:
        print(f"  [OK] 非法批次类型 -> {type(exc).__name__}: {exc}")

    try:
        replace_region(b"abc", 1, 9, b"")
    except InvalidEditError as exc:
        print(f"  [OK] 替换区间越界 -> {type(exc).__name__}: {exc}")

    try:
        replace_region(b"\x00" * MAX_TOTAL_BYTES, 0, 0, b"x")
    except InputTooLargeError as exc:
        print(f"  [OK] 编辑后超 1 MiB -> {type(exc).__name__}: {exc}")

    # All rejections are one catchable family.
    for bad in (
        lambda: replace_region("x", 0, 0, b""),
        lambda: replace_region(b"x", 0, 1, 1),
        lambda: replace_region(b"x", True, 1, b""),
    ):
        try:
            bad()
        except CDCError as exc:
            print(f"  [OK] 非法参数 -> {type(exc).__name__}: {exc}")

    elapsed = time.perf_counter() - started
    print("-" * 72)
    print(f"真实计算耗时: {elapsed:.3f}s (无 sleep, 无预录结果)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
