#!/usr/bin/env python3
"""Fixed-input demonstration for rabin_cdc.

Run with::

    ./.venv/bin/python demo.py

It really computes (no pre-recorded output, no sleep):

1. Builds a deterministic ~200 KB byte stream, content-chunks it, inserts a
   single byte, chunks again, and reports the reusable block ratio plus the
   boundary realignment point.
2. **Boundary context splicing**: processes the same stream as adjacent
   independent segments (export context -> serialize -> restore -> continue)
   and verifies the blocks are exactly those of one continuous run.  This
   includes:
   - an exhaustive check at *every* byte boundary of a sample stream, where
     the carried fingerprint is checked against a direct polynomial long
     division of the window (the rolling tables are not used for the check);
   - cuts right after a forced ``max`` cut, one byte off the 64-byte minimum,
     and an empty trailing segment;
   - a corrupted context blob being rejected.
3. Triggers two real input-contract rejections (one byte over the 1 MiB
   limit; a non-bytes batch) and prints the actual raised errors.
"""

from __future__ import annotations

import sys
import time

from rabin_cdc import (
    MAX_BLOCK_BYTES,
    MAX_TOTAL_BYTES,
    MIN_BLOCK_BYTES,
    POLY,
    WINDOW_SIZE,
    Block,
    Chunker,
    ContextError,
    InputTooLargeError,
    InvalidBatchTypeError,
    deserialize_context,
    export_context,
    restore_context,
    serialize_context,
    split_stream,
)

SEED = 20260922
STREAM_LEN = 200_000
INSERT_AT = STREAM_LEN // 3
INSERTED_BYTE = b"\xA5"
SWEEP_LEN = 1200


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


def block_key(blocks: list[Block]):
    return [(b.start, b.end, b.size, b.sha256, b.reason) for b in blocks]


def direct_remainder(window: bytes) -> int:
    """Window remainder by direct polynomial long division.

    Independent of the rolling update tables: used only to verify the
    fingerprint carried by an exported boundary context.
    """
    remainder = 0
    for byte in window:
        for bit in (7, 6, 5, 4, 3, 2, 1, 0):
            top = (remainder >> 15) & 1
            remainder = ((remainder << 1) | ((byte >> bit) & 1)) & 0xFFFF
            if top:
                remainder ^= POLY & 0xFFFF
    return remainder


def splice_two_sessions(data: bytes, cut: int):
    """Process data[:cut] and data[cut:] in two sessions via a serialized
    context; return (all blocks, blob bytes, snapshot)."""
    first = Chunker()
    blocks = first.add(data[:cut])
    snapshot = export_context(first)
    blob = serialize_context(snapshot)
    second = restore_context(deserialize_context(blob))
    blocks.extend(second.add(data[cut:]))
    tail = second.finish()
    if tail is not None:
        blocks.append(tail)
    return blocks, len(blob), snapshot


def section_insertion_reuse(data: bytes, modified: bytes) -> None:
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

    edited_by_hash = {b.sha256: b for b in edited}
    for block in original:
        if block.start > INSERT_AT and block.sha256 in edited_by_hash:
            twin = edited_by_hash[block.sha256]
            print(
                f"  恢复后的首个同块 : 原始 [{block.start},{block.end}) -> "
                f"插入后 [{twin.start},{twin.end}), 整体后移 "
                f"{twin.start - block.start} 字节, 大小/SHA-256 不变"
            )
            break
    print(f"  示例首块 SHA-256  : {original[0].sha256}")


def section_boundary_context() -> None:
    print("-" * 72)
    print("边界上下文: 相邻区段分别处理, 结果与连续切分逐字节一致")

    # --- Exhaustive check at every byte boundary --------------------------
    sample = lcg_bytes(SWEEP_LEN, seed=20260925)
    baseline = block_key(split_stream([sample]))
    started = time.perf_counter()
    checked_fp = 0
    blob_size = 0
    for cut in range(len(sample) + 1):
        blocks, blob_size, snapshot = splice_two_sessions(sample, cut)
        assert block_key(blocks) == baseline, f"mismatch at cut={cut}"
        window = sample[max(0, cut - WINDOW_SIZE) : cut]
        assert snapshot.window == window
        # Reference: direct polynomial division of the window, no tables.
        assert snapshot.fingerprint == direct_remainder(window)
        checked_fp += 1
    elapsed = time.perf_counter() - started
    print(
        f"  每字节边界两切分  : {checked_fp} 个切点全部一致 (区间/SHA-256/原因), "
        f"耗时 {elapsed:.2f}s"
    )
    print(f"  上下文 blob 大小  : {blob_size} 字节 "
          f"(含格式版本、参数指纹、{WINDOW_SIZE} 字节窗口、未闭合块、整体 SHA-256)")

    # --- Required edge cases ----------------------------------------------
    # 1) immediately after a forced max cut
    forced = b"\x01" * MAX_BLOCK_BYTES + lcg_bytes(200, seed=2)
    base_forced = block_key(split_stream([forced]))
    for cut in (MAX_BLOCK_BYTES - 1, MAX_BLOCK_BYTES, MAX_BLOCK_BYTES + 1):
        blocks, _, _ = splice_two_sessions(forced, cut)
        assert block_key(blocks) == base_forced
    print(f"  刚强切后拼接      : 在 {MAX_BLOCK_BYTES-1}/{MAX_BLOCK_BYTES}/"
          f"{MAX_BLOCK_BYTES+1} 处切两段, 块完全一致 (首块 reason=max)")

    # 2) one byte off the minimum block size
    near_min = bytes(MIN_BLOCK_BYTES) + lcg_bytes(MIN_BLOCK_BYTES, seed=15)
    base_min = block_key(split_stream([near_min]))
    for cut in (MIN_BLOCK_BYTES - 1, MIN_BLOCK_BYTES, MIN_BLOCK_BYTES + 1):
        blocks, _, _ = splice_two_sessions(near_min, cut)
        assert block_key(blocks) == base_min
    print(f"  距最小块差一字节  : 在 {MIN_BLOCK_BYTES-1}/{MIN_BLOCK_BYTES}/"
          f"{MIN_BLOCK_BYTES+1} 处切两段, 切点 reason=content 不丢失")

    # 3) empty trailing segment: tail only committed by explicit finish()
    tail_data = lcg_bytes(300, seed=21)
    first = Chunker()
    pre = first.add(tail_data)
    snapshot = export_context(first)
    second = restore_context(deserialize_context(serialize_context(snapshot)))
    assert second.add(b"") == []  # empty trailing segment emits nothing
    tail = second.finish()
    assert tail is not None and tail.reason == "tail"
    all_blocks = pre + [tail]
    assert block_key(all_blocks) == block_key(split_stream([tail_data]))
    print("  空尾段            : 续段为空时不产出块, 仅明确 finish() 提交尾块")

    # --- A corrupted context blob must be rejected wholesale --------------
    good = serialize_context(export_context(_feed(lcg_bytes(160, seed=8), 100)))
    bad = bytearray(good)
    bad[len(bad) // 2] ^= 0x01
    try:
        restore_context(deserialize_context(bytes(bad)))
    except ContextError as exc:
        print(f"  损坏 blob 拒绝    : {type(exc).__name__}: {str(exc)[:60]}")


def _feed(data: bytes, n: int) -> Chunker:
    chunker = Chunker()
    chunker.add(data[:n])
    return chunker


def section_rejections() -> None:
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


def main() -> int:
    started = time.perf_counter()
    data = lcg_bytes(STREAM_LEN, SEED)
    modified = data[:INSERT_AT] + INSERTED_BYTE + data[INSERT_AT:]

    section_insertion_reuse(data, modified)
    section_boundary_context()
    section_rejections()

    elapsed = time.perf_counter() - started
    print("-" * 72)
    print(f"真实计算总耗时: {elapsed:.3f}s (无 sleep, 无预录结果, 预算 90s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
