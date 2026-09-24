#!/usr/bin/env python3
"""Fixed-input demonstration for rabin_cdc.

Run with::

    python demo.py

Everything shown is genuinely computed (no pre-recorded output, no sleep):

1. Content-defined chunking: a deterministic stream is chunked, one byte is
   inserted in the middle, and the reusable-block ratio (SHA-256 content
   addressing) plus the realignment point are printed.
2. Middle-region replacement (this iteration's new entry point):
   ``replace_region`` re-chunks the whole object under the unchanged splitting
   rules and reports a keep / replace / new block mapping.  The demo covers a
   normal cross-boundary edit, head/tail edits, an empty-object and a
   whole-object-deletion boundary, and stable identity of identical-content
   blocks (all-zero data with nine byte-identical 64-byte blocks).
3. Real contract rejections actually raised: over-1-MiB result, non-bytes
   batch, out-of-range edit offset, ``bool``/``float`` offsets.
"""

from __future__ import annotations

import sys
import time

from rabin_cdc import (
    KEEP,
    MAX_TOTAL_BYTES,
    NEW,
    REPLACE,
    Block,
    EditValidationError,
    InputTooLargeError,
    InvalidBatchTypeError,
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


def status_counts(result) -> dict[str, int]:
    counts = {KEEP: 0, REPLACE: 0, NEW: 0}
    for m in result.mappings:
        counts[m.status] += 1
    return counts


def short(sha: str) -> str:
    return sha[:12] + "..."


def section(title: str) -> None:
    print("-" * 72)
    print(title)


# ---------------------------------------------------------------------------
# Part 1: original content-defined chunking behaviour
# ---------------------------------------------------------------------------
def demo_chunking() -> None:
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

    edited_hashes = {b.sha256 for b in edited}
    reused = [b for b in original if b.sha256 in edited_hashes]
    reused_bytes = sum(b.size for b in reused)
    print("-" * 72)
    print("抗边界扰动 / 去重复用 (按 SHA-256 内容寻址)")
    print(f"  可复用块数比例    : {len(reused)}/{len(original)} "
          f"= {len(reused) / len(original):.4%}")
    print(f"  可复用字节比例    : {reused_bytes}/{len(data)} "
          f"= {reused_bytes / len(data):.4%}")

    old_bounds = boundaries(original)
    new_bounds = boundaries(edited)
    shared_prefix = 0
    for a, b in zip(old_bounds, new_bounds):
        if a != b:
            break
        shared_prefix += 1
    edited_by_hash = {b.sha256: b for b in edited}
    first_realiged = None
    for block in original:
        if block.start > INSERT_AT and block.sha256 in edited_by_hash:
            first_realiged = (block, edited_by_hash[block.sha256])
            break
    print(f"  插入点前一致边界  : 前 {shared_prefix} 个切块点完全相同")
    if first_realiged is not None:
        block, twin = first_realiged
        print(
            f"  恢复后的首个同块 : 原始 [{block.start},{block.end}) "
            f"-> 插入后 [{twin.start},{twin.end}), 仅整体后移 "
            f"{twin.start - block.start} 字节, 大小/SHA-256 不变"
        )


# ---------------------------------------------------------------------------
# Part 2: middle-region replacement with the keep/replace/new mapping
# ---------------------------------------------------------------------------
def demo_replace_normal() -> None:
    section("中间替换入口 replace_region(data, offset, old_length, replacement)")
    data = lcg_bytes(6000, seed=246813)
    # Pick a real cut boundary near the middle and deliberately straddle it.
    old_blocks = split_stream([data])
    boundary = old_blocks[len(old_blocks) // 2].end
    start, stop = boundary - 13, boundary + 51
    replacement = b"R" * 90
    result = replace_region(data, start, stop - start, replacement)

    print(f"  对象长度          : {len(data)} -> {len(result.new_data)} bytes")
    print(f"  替换区间(新对象)  : [{result.edit_start}, {result.edit_end}) "
          f"(原区间 [{start},{stop}), 净变化 +{len(replacement) - (stop - start)})")
    print(f"  块数              : 旧 {len(result.old_blocks)} -> "
          f"新 {len(result.new_blocks)}")
    counts = status_counts(result)
    print(f"  映射统计          : keep={counts[KEEP]}  "
          f"replace={counts[REPLACE]}  new={counts[NEW]}")

    kept = [m for m in result.mappings if m.status == KEEP]
    replaced = [m for m in result.mappings if m.status == REPLACE]
    added = [m for m in result.mappings if m.status == NEW]

    print("  示例 keep 行 (旧块在新布局中逐字节保留):")
    for m in kept[:2]:
        ob = result.old_blocks[m.old_index]
        nb = result.new_blocks[m.new_index]
        print(f"    old#{m.old_index} [{ob.start},{ob.end}) {ob.size}B -> "
              f"new#{m.new_index} [{nb.start},{nb.end}) {nb.size}B "
              f"sha256={short(m.sha256)}")
    print("  示例 replace 行 (旧块消失):")
    for m in replaced[:2]:
        ob = result.old_blocks[m.old_index]
        print(f"    old#{m.old_index} [{ob.start},{ob.end}) {ob.size}B "
              f"reason={ob.reason} sha256={short(m.sha256)}")
    print("  示例 new 行 (编辑产生的全新块):")
    for m in added[:2]:
        nb = result.new_blocks[m.new_index]
        print(f"    new#{m.new_index} [{nb.start},{nb.end}) {nb.size}B "
              f"reason={nb.reason} sha256={short(m.sha256)}")

    # Analytically exact consequences.
    delta = len(replacement) - (stop - start)
    causal_kept = [m for m in kept
                   if result.old_blocks[m.old_index].end <= start]
    for m in causal_kept:
        ob, nb = result.old_blocks[m.old_index], result.new_blocks[m.new_index]
        assert (ob.start, ob.end) == (nb.start, nb.end)
    recovered = [m for m in kept
                 if result.new_blocks[m.new_index].start
                 >= stop + len(replacement) - (stop - start) + 32 + 256]
    for m in recovered:
        ob, nb = result.old_blocks[m.old_index], result.new_blocks[m.new_index]
        assert nb.start == ob.start + delta and nb.size == ob.size
    print(f"  编辑点前因果保留  : {len(causal_kept)} 块偏移完全不变")
    print(f"  越过 32+256 后恢复: {len(recovered)} 块与原块逐字节相同"
          f"(整体平移 {delta:+d})")
    # Row partition property.
    assert counts[KEEP] + counts[REPLACE] == len(result.old_blocks)
    assert counts[KEEP] + counts[NEW] == len(result.new_blocks)
    # Object really reconstructed.
    assert result.new_data == data[:start] + replacement + data[stop:]
    print("  校验              : 新旧对象拼接关系、映射覆盖均通过断言")


def demo_identical_blocks() -> None:
    section("相同内容块的稳定标识 (全零数据: 9 个完全相同的 64 字节块 + 24 尾块)")
    data = bytes(600)
    # Insert one more identical 64-byte block at offset 64.
    result = replace_region(data, 64, 0, bytes(64))
    pairs = [(m.old_index, m.new_index) for m in result.mappings
             if m.status == KEEP]
    added = [m for m in result.mappings if m.status == NEW]
    replaced = [m for m in result.mappings if m.status == REPLACE]
    print(f"  插入 64 个零字节后: 旧 {len(result.old_blocks)} 块 -> "
          f"新 {len(result.new_blocks)} 块")
    print(f"  位置化配对        : 第 k 个相同块配第 k 个, keep 对 = {pairs}")
    print(f"  新增块            : new#{added[0].new_index} "
          f"{added[0].size}B (第 10 个相同的 64 字节块)")
    print(f"  replace 行        : {len(replaced)} (24 字节尾块仍与旧尾块相同, keep)")
    assert pairs == [(i, i) for i in range(9)] + [(9, 10)]
    assert len(added) == 1 and added[0].new_index == 9 and added[0].size == 64
    assert replaced == []

    # Deleting one identical occurrence marks exactly that occurrence gone.
    result2 = replace_region(data, 0, 64, b"")
    pairs2 = [(m.old_index, m.new_index) for m in result2.mappings
              if m.status == KEEP]
    gone = [m.old_index for m in result2.mappings if m.status == REPLACE]
    print(f"  删掉首个 64 字节块: keep 对 = {pairs2}")
    print(f"  被替换的旧块      : old#{gone[0]} (相同内容也不会张冠李戴)")
    assert pairs2 == [(i, i) for i in range(8)] + [(9, 8)]
    assert gone == [8]


def demo_edges() -> None:
    section("边界情形: 首/尾编辑、空对象、整段删除、等价空操作")
    data = lcg_bytes(2000, seed=1357)

    head = replace_region(data, 0, 0, b"HEAD!")
    print(f"  头部插入 5 字节   : keep={status_counts(head)[KEEP]}, "
          f"replace={status_counts(head)[REPLACE]}, new={status_counts(head)[NEW]}; "
          f"首个旧块必被替换 -> "
          f"old#0 replaced = "
          f"{any(m.status == REPLACE and m.old_index == 0 for m in head.mappings)}")
    assert any(m.status == REPLACE and m.old_index == 0 for m in head.mappings)

    tail = replace_region(data, len(data), 0, b"TAIL!")
    complete_old = [b for b in tail.old_blocks if b.end < len(data)]
    kept_idx = {m.old_index for m in tail.mappings if m.status == KEEP}
    print(f"  尾部追加 5 字节   : {len(complete_old)} 个完整块全部在原偏移保留, "
          f"仅尾块被替换")
    assert all(i in kept_idx for i in range(len(complete_old)))

    empty = replace_region(b"", 0, 0, b"abc")
    print(f"  空对象插入 abc    : 旧 0 块, 新 1 块, 唯一映射 = "
          f"{empty.mappings[0].status} (size={empty.mappings[0].size})")
    assert [(m.status, m.new_index) for m in empty.mappings] == [(NEW, 0)]

    wiped = replace_region(b"abcdef", 0, 6, b"")
    print(f"  整段删除 6 字节   : 新对象为空, 唯一映射 = "
          f"{wiped.mappings[0].status} old#"
          f"{wiped.mappings[0].old_index}")
    assert wiped.new_data == b"" and wiped.mappings[0].status == REPLACE

    noop = replace_region(data, 100, 50, data[100:150])
    print(f"  同内容替换(空操作): {status_counts(noop)[KEEP]} 块全部 keep, "
          f"切分结果逐块不变")
    assert all(m.status == KEEP for m in noop.mappings)
    assert [(b.start, b.end, b.sha256) for b in noop.old_blocks] == \
           [(b.start, b.end, b.sha256) for b in noop.new_blocks]


def demo_errors() -> None:
    section("接口边界拒绝 (实际触发, 非预录):")
    try:
        split_stream([b"\x00" * (MAX_TOTAL_BYTES + 1)])
    except InputTooLargeError as exc:
        print(f"  [OK] split_stream 超过 1 MiB -> {type(exc).__name__}")

    try:
        split_stream(["not-a-bytes-batch"])
    except InvalidBatchTypeError as exc:
        print(f"  [OK] 非法批次类型 -> {type(exc).__name__}")

    data = lcg_bytes(1000, seed=246)
    try:
        replace_region(data, 1001, 0, b"")
    except EditValidationError as exc:
        print(f"  [OK] offset 越界 -> {type(exc).__name__}: {exc}")

    try:
        replace_region(data, 0, 1001, b"")
    except EditValidationError as exc:
        print(f"  [OK] 区间越过对象尾 -> {type(exc).__name__}")

    try:
        replace_region(data, True, 0, b"")
    except (EditValidationError, InvalidBatchTypeError) as exc:
        print(f"  [OK] bool 不当作整数偏移 -> {type(exc).__name__}")

    try:
        replace_region(data, 0, 1.5, b"")
    except EditValidationError as exc:
        print(f"  [OK] float 长度被拒 -> {type(exc).__name__}")

    try:
        replace_region(data, 0, 0, "not-bytes")
    except InvalidBatchTypeError as exc:
        print(f"  [OK] replacement 非 bytes-like -> {type(exc).__name__}")

    try:
        replace_region(b"\x00" * MAX_TOTAL_BYTES, 0, 0, b"x")
    except InputTooLargeError as exc:
        print(f"  [OK] 替换后超过 1 MiB -> {type(exc).__name__}")

    # Failed call leaves nothing half-updated: the object is never mutated and
    # a retry with valid arguments works normally.
    mutable = bytearray(data)
    try:
        replace_region(mutable, -1, 0, b"")
    except EditValidationError:
        pass
    ok = replace_region(mutable, 0, 1, b"Z")
    assert ok.new_data[:1] == b"Z" and bytes(mutable) == data
    print("  [OK] 被拒调用后原对象不变, 合法重试正常")


def main() -> int:
    started = time.perf_counter()
    demo_chunking()
    demo_replace_normal()
    demo_identical_blocks()
    demo_edges()
    demo_errors()
    print("-" * 72)
    print(f"真实计算耗时: {time.perf_counter() - started:.3f}s "
          f"(无 sleep, 无预录结果)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
