"""Contract tests for boundary-context export / resume.

The central guarantee: splitting one byte stream into two adjacent
segments at *any* byte boundary, exporting a :class:`BoundaryContext` at
the cut and resuming a fresh chunker from it, yields exactly the blocks
(intervals, SHA-256, cut reason) of a continuous split.
"""

from __future__ import annotations

import dataclasses
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rabin_cdc import (  # noqa: E402
    CONTEXT_FORMAT_VERSION,
    MAX_BLOCK_BYTES,
    MAX_TOTAL_BYTES,
    MIN_BLOCK_BYTES,
    PARAMETERS_FINGERPRINT,
    REASON_MAX,
    WINDOW_BYTES,
    BoundaryContext,
    Chunker,
    InputTooLargeError,
    InvalidContextError,
    StreamFinishedError,
    split_stream,
    stream_remainder,
)
from tests._slow_reference import slow_remainder, slow_split  # noqa: E402
from tests._data import lcg_bytes, mixed_stream  # noqa: E402


def tuples(blocks):
    return [(b.start, b.end, b.size, b.sha256, b.reason) for b in blocks]


def split_via_context(data: bytes, cut: int):
    """Feed ``data[:cut]``, export, resume a fresh chunker, feed the rest."""
    left = Chunker()
    blocks = list(left.add(data[:cut]))
    context = left.export_context()
    right = Chunker.from_context(context)
    blocks.extend(right.add(data[cut:]))
    tail = right.finish()
    if tail is not None:
        blocks.append(tail)
    return blocks, context


class EveryBoundarySplitTest(unittest.TestCase):
    """Two-segment split at every byte boundary equals the continuous split."""

    def check_every_boundary(self, data: bytes) -> None:
        expected = tuples(split_stream([data]))
        # The continuous split itself agrees with the independent reference.
        self.assertEqual(expected, tuples(slow_split(data)))
        for cut in range(len(data) + 1):
            blocks, context = split_via_context(data, cut)
            self.assertEqual(context.offset, cut)
            self.assertEqual(
                tuples(blocks),
                expected,
                f"mismatch when split at byte boundary {cut}/{len(data)}",
            )

    def test_mixed_stream_every_boundary(self):
        # 700-byte constant runs force `max` cuts; LCG parts give
        # `content` cuts; the leftover gives a `tail` block.
        self.check_every_boundary(mixed_stream(800, seed=5))

    def test_lcg_stream_every_boundary(self):
        self.check_every_boundary(lcg_bytes(300, seed=7))

    def test_zero_stream_every_boundary(self):
        # Zero windows cut exactly at 64-byte content boundaries.
        self.check_every_boundary(b"\x00" * 200)


class SpecificBoundaryCasesTest(unittest.TestCase):
    def setUp(self):
        self.data = mixed_stream(3000, seed=11)
        self.expected = tuples(split_stream([self.data]))

    def test_right_after_forced_max_cut(self):
        blocks = split_stream([self.data])
        max_block = next(b for b in blocks if b.reason == REASON_MAX)
        cut = max_block.end
        result, context = split_via_context(self.data, cut)
        # The cut lands exactly on a block boundary: nothing is pending,
        # but the rolling window and fingerprint carry over unreset.
        self.assertEqual(context.pending, b"")
        self.assertEqual(context.offset, cut)
        self.assertEqual(len(context.window), WINDOW_BYTES)
        self.assertEqual(tuples(result), self.expected)

    def test_one_byte_short_of_minimum_block(self):
        blocks = split_stream([self.data])
        boundary = None
        for current, nxt in zip(blocks, blocks[1:]):
            if nxt.size >= MIN_BLOCK_BYTES:
                boundary = current.end
                break
        self.assertIsNotNone(boundary)
        cut = boundary + MIN_BLOCK_BYTES - 1  # pending is one byte short
        result, context = split_via_context(self.data, cut)
        self.assertEqual(len(context.pending), MIN_BLOCK_BYTES - 1)
        self.assertEqual(tuples(result), self.expected)

    def test_empty_tail_segment(self):
        # Export at the very end of the stream; the second segment is empty.
        left = Chunker()
        blocks = list(left.add(self.data))
        context = left.export_context()
        right = Chunker.from_context(context)
        tail = right.finish()  # no add() at all before finish
        if tail is not None:
            blocks.append(tail)
        self.assertEqual(tuples(blocks), self.expected)

    def test_export_at_offset_zero_equals_fresh_chunker(self):
        context = Chunker().export_context()
        self.assertEqual(context.offset, 0)
        self.assertEqual(context.window, b"")
        self.assertEqual(context.fingerprint, 0)
        self.assertEqual(context.pending, b"")
        right = Chunker.from_context(context)
        blocks = list(right.add(self.data))
        tail = right.finish()
        if tail is not None:
            blocks.append(tail)
        self.assertEqual(tuples(blocks), self.expected)


class ExportIsolationTest(unittest.TestCase):
    def setUp(self):
        self.data = lcg_bytes(1500, seed=21)

    def test_export_does_not_disturb_the_live_chunker(self):
        chunker = Chunker()
        blocks = list(chunker.add(self.data[:500]))
        chunker.export_context()
        blocks.extend(chunker.add(self.data[500:]))
        tail = chunker.finish()
        if tail is not None:
            blocks.append(tail)
        self.assertEqual(tuples(blocks), tuples(split_stream([self.data])))

    def test_exported_context_is_immutable_and_detached(self):
        chunker = Chunker()
        chunker.add(self.data[:500])
        context = chunker.export_context()
        self.assertIsInstance(context.window, bytes)
        self.assertIsInstance(context.pending, bytes)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            context.offset = 0  # type: ignore[misc]
        with self.assertRaises(dataclasses.FrozenInstanceError):
            context.pending = b""  # type: ignore[misc]

    def test_context_is_reusable_and_survives_origin_finish(self):
        chunker = Chunker()
        first = list(chunker.add(self.data[:500]))
        context = chunker.export_context()
        tail0 = chunker.finish()  # closing the origin must not invalidate copy
        if tail0 is not None:
            first.append(tail0)
        full = tuples(split_stream([self.data]))
        suffix = [t for t in full if t[1] > 500]
        for _ in range(2):  # the same context resumes any number of times
            right = Chunker.from_context(context)
            blocks = list(right.add(self.data[500:]))
            tail = right.finish()
            if tail is not None:
                blocks.append(tail)
            self.assertEqual(tuples(blocks), suffix)

    def test_resume_detaches_mutable_caller_buffers(self):
        chunker = Chunker()
        chunker.add(self.data[:500])
        context = chunker.export_context()
        hostile_window = bytearray(context.window)
        hostile_pending = bytearray(context.pending)
        crafted = dataclasses.replace(
            context, window=hostile_window, pending=hostile_pending
        )
        right = Chunker.from_context(crafted)
        # Caller scribbles over the buffers after the resume.
        hostile_window[:] = b"\xFF" * len(hostile_window)
        hostile_pending[:] = b"\xFF" * len(hostile_pending)
        blocks = list(right.add(self.data[500:]))
        tail = right.finish()
        if tail is not None:
            blocks.append(tail)
        full = tuples(split_stream([self.data]))
        self.assertEqual(tuples(blocks), [t for t in full if t[1] > 500])

    def test_export_after_finish_rejected(self):
        chunker = Chunker()
        chunker.add(self.data[:10])
        chunker.finish()
        with self.assertRaises(StreamFinishedError):
            chunker.export_context()


class ContextValidationTest(unittest.TestCase):
    def setUp(self):
        self.data = lcg_bytes(1200, seed=33)
        self.cut = 700  # window full, pending non-empty
        chunker = Chunker()
        chunker.add(self.data[: self.cut])
        self.context = chunker.export_context()
        self.suffix = self.data[self.cut :]

    def assert_rejected(self, **changes):
        crafted = dataclasses.replace(self.context, **changes)
        with self.assertRaises(InvalidContextError):
            Chunker.from_context(crafted)

    def test_valid_context_roundtrips(self):
        right = Chunker.from_context(self.context)
        blocks = list(right.add(self.suffix))
        tail = right.finish()
        if tail is not None:
            blocks.append(tail)
        expected = tuples(split_stream([self.data]))
        self.assertEqual(tuples(blocks), [t for t in expected if t[1] > self.cut])

    def test_context_carries_version_and_parameter_fingerprint(self):
        self.assertEqual(self.context.format_version, CONTEXT_FORMAT_VERSION)
        self.assertEqual(self.context.parameters, PARAMETERS_FINGERPRINT)

    def test_non_context_rejected(self):
        for bad in (None, {}, "context", 42, b"bytes"):
            with self.assertRaises(InvalidContextError):
                Chunker.from_context(bad)

    def test_format_version_checked(self):
        for bad in (0, 2, 999, "1", 1.0, True, None):
            self.assert_rejected(format_version=bad)

    def test_parameter_fingerprint_checked(self):
        self.assert_rejected(parameters="0" * 16)
        self.assert_rejected(parameters="")
        self.assert_rejected(parameters=None)

    def test_offset_checked(self):
        for bad in (-1, MAX_TOTAL_BYTES + 1, 1.5, "700", True, None):
            self.assert_rejected(offset=bad)

    def test_window_length_must_match_offset(self):
        self.assert_rejected(window=self.context.window[:-1])
        self.assert_rejected(window=self.context.window + b"x")
        self.assert_rejected(window=b"")
        self.assert_rejected(window=123)

    def test_fingerprint_must_match_window_by_direct_division(self):
        self.assert_rejected(fingerprint=self.context.fingerprint ^ 0x0001)
        self.assert_rejected(fingerprint=-1)
        self.assert_rejected(fingerprint=0x10000)
        self.assert_rejected(fingerprint=True)
        # A tampered window of the right length no longer matches the
        # stored fingerprint (recomputed by direct polynomial division).
        tampered = bytes([self.context.window[0] ^ 0x01]) + self.context.window[1:]
        self.assertNotEqual(stream_remainder(tampered), self.context.fingerprint)
        self.assert_rejected(window=tampered)

    def test_pending_length_consistent_with_offset(self):
        self.assert_rejected(pending=b"p" * (self.cut + 1))  # > offset
        self.assert_rejected(pending=b"p" * MAX_BLOCK_BYTES)  # force-cut size
        self.assert_rejected(pending=b"p" * (MAX_BLOCK_BYTES + 1))
        self.assert_rejected(pending="not-bytes")

    def test_offset_at_stream_limit_resumes_but_cannot_grow(self):
        window = lcg_bytes(WINDOW_BYTES, seed=77)
        context = BoundaryContext(
            format_version=CONTEXT_FORMAT_VERSION,
            parameters=PARAMETERS_FINGERPRINT,
            offset=MAX_TOTAL_BYTES,
            window=window,
            fingerprint=stream_remainder(window),
            pending=b"",
        )
        chunker = Chunker.from_context(context)
        self.assertEqual(chunker.total_bytes, MAX_TOTAL_BYTES)
        with self.assertRaises(InputTooLargeError):
            chunker.add(b"x")
        self.assertIsNone(chunker.finish())


class DirectDivisionReferenceTest(unittest.TestCase):
    """The resume-time fingerprint check divides the window polynomial
    directly; it must agree with the independent slow reference and with
    the rolling state a live chunker exports."""

    def test_stream_remainder_matches_slow_reference(self):
        data = lcg_bytes(300, seed=55)
        for length in list(range(0, 40)) + [64, 100, 255, 300]:
            self.assertEqual(
                stream_remainder(data[:length]),
                slow_remainder(data[:length]),
                f"length {length}",
            )

    def test_exported_fingerprint_matches_direct_division_everywhere(self):
        data = lcg_bytes(400, seed=56)
        chunker = Chunker()
        for i, byte in enumerate(data):
            chunker.add(bytes([byte]))
            context = chunker.export_context()
            self.assertEqual(context.offset, i + 1)
            self.assertEqual(len(context.window), min(i + 1, WINDOW_BYTES))
            # Direct division over the exported window, not the tables.
            self.assertEqual(
                context.fingerprint, slow_remainder(context.window)
            )


if __name__ == "__main__":
    unittest.main()
