"""Deterministic demo of the cdc chunking library.

Runs a real computation in well under 8 seconds:
  1. normal case — chunk a 200,000-byte "file" and a modified copy
     (1 inserted byte + one replaced 4 KiB region), then report how many
     blocks of the modified version are reused unchanged (SHA-256 match);
  2. a genuinely triggered rejection — a non-bytes chunk is refused with a
     locatable error.

All data is generated deterministically in-repo (SHA-256 counter chain);
no network, files, or randomness.  Run:  python demo.py
"""

import hashlib
import time

from cdc import InvalidChunkTypeError, split_stream


def gen_bytes(n, seed):
    """Deterministic pseudo-random bytes (SHA-256 counter chain)."""
    out = bytearray()
    counter = 0
    while len(out) < n:
        out += hashlib.sha256(
            seed.to_bytes(8, "little") + counter.to_bytes(8, "little")
        ).digest()
        counter += 1
    return bytes(out[:n])


def main():
    started = time.perf_counter()

    original = gen_bytes(200_000, seed=1)
    # Modified copy: one byte inserted at 80,000 and a 4,096-byte region
    # replaced at 150,000.
    modified = (
        original[:80_000]
        + b"\x00"
        + original[80_000:150_000]
        + gen_bytes(4_096, seed=2)
        + original[150_000:]
    )

    # Feed the original in odd 997-byte chunks to show the result does not
    # depend on input chunking.
    blocks_a = split_stream(
        [original[i:i + 997] for i in range(0, len(original), 997)]
    )
    blocks_b = split_stream([modified])

    same_as_whole = split_stream([original]) == blocks_a
    print(f"original: {len(original)} bytes -> {len(blocks_a)} blocks")
    print(f"modified: {len(modified)} bytes -> {len(blocks_b)} blocks")
    print(f"chunking-independent result: {same_as_whole}")

    histogram = {}
    for b in blocks_a:
        histogram[b.reason] = histogram.get(b.reason, 0) + 1
    print(f"cut reasons (original): {histogram}")
    print("first 3 blocks of original:")
    for b in blocks_a[:3]:
        print(
            f"  [{b.start:>6}, {b.end:>6})  size={b.size:>3}  "
            f"reason={b.reason:<13}  sha256={b.sha256[:16]}..."
        )

    hashes_a = {b.sha256 for b in blocks_a}
    reused = sum(1 for b in blocks_b if b.sha256 in hashes_a)
    # Exact ratio plus a percentage rounded to 2 decimals (round-half-even);
    # see README for the float error statement.
    print(
        f"reused blocks in modified: {reused}/{len(blocks_b)} "
        f"({reused / len(blocks_b) * 100:.2f}%) — boundaries resynchronise "
        "after local edits"
    )

    # A genuinely triggered rejection: chunk 1 is not bytes-like.
    try:
        split_stream([b"valid chunk", "not bytes"])
    except InvalidChunkTypeError as exc:
        print(f"rejected as designed: {type(exc).__name__}: {exc}")

    print(f"elapsed: {time.perf_counter() - started:.2f}s")


if __name__ == "__main__":
    main()
