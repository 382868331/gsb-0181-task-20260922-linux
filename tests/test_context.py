"""Tests for spliceable boundary contexts (export/restore).

The headline contract: for the same input, splitting at *every* byte
boundary into two independently processed segments must reproduce the exact
blocks of one continuous chunking run -- absolute intervals, SHA-256 hashes
and cut reasons.  Expected fingerprints come from
``tests/_slow_reference.py`` which divides the window polynomial directly
(long division), never from the rolling update tables.
"""

from __future__ import annotations

import dataclasses
import hashlib
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rabin_cdc import (  # noqa: E402
    CONTEXT_FORMAT_VERSION,
    MAX_BLOCK_BYTES,
    MIN_BLOCK_BYTES,
    WINDOW_SIZE,
    Chunker,
    ContextError,
    ContextSnapshot,
    StreamFinishedError,
    deserialize_context,
    export_context,
    restore_context,
    serialize_context,
)
from tests._slow_reference import slow_remainder, slow_split  # noqa: E402
from tests._data import lcg_bytes, mixed_stream  # noqa: E402


def tuples(blocks):
    return [(b.start, b.end, b.size, b.sha256, b.reason) for b in blocks]


def splice(data, cut):
    """Process data[:cut] and data[cut:] in two sessions via a context."""
    first = Chunker()
    emitted = first.add(data[:cut])
    snapshot = export_context(first)
    second = restore_context(snapshot)
    emitted.extend(second.add(data[cut:]))
    tail = second.finish()
    if tail is not None:
        emitted.append(tail)
    return emitted, snapshot


class EveryByteBoundaryEquivalenceTest(unittest.TestCase):
    """Same input cut at EVERY byte boundary -> identical block list."""

    def _assert_all_boundaries(self, data):
        baseline = tuples(slow_split(data))
        for cut in range(len(data) + 1):
            blocks, snapshot = splice(data, cut)
            self.assertEqual(tuples(blocks), baseline, msg=f"cut={cut}")
            # The carried fingerprint must equal the window divided directly
            # by polynomial long division (not the rolling tables).
            window = data[max(0, cut - WINDOW_SIZE) : cut]
            self.assertEqual(snapshot.window, window, msg=f"cut={cut}")
            self.assertEqual(
                snapshot.fingerprint, slow_remainder(window), msg=f"cut={cut}"
            )

    def test_zeros(self):
        # Zero data cuts "content" every 64 bytes: dense boundary exercise.
        self._assert_all_boundaries(bytes(400))

    def test_constant_run(self):
        # 0x01 never zeros low 6 bits: every non-tail cut is "max".
        self._assert_all_boundaries(b"\x01" * 400)

    def test_pseudorandom(self):
        self._assert_all_boundaries(lcg_bytes(600, seed=31337))

    def test_mixed(self):
        self._assert_all_boundaries(mixed_stream(700))

    def test_tiny_prefixes(self):
        # Lengths around the 32-byte window fill and the 64-byte minimum.
        for n in (0, 1, 2, 31, 32, 33, 63, 64, 65, 66, 127, 128, 129):
            self._assert_all_boundaries(lcg_bytes(n, seed=700 + n))


class BoundaryEdgeCaseTest(unittest.TestCase):
    """Immediately after a forced max cut; one byte off the minimum;
    and an empty trailing segment."""

    def test_split_right_after_forced_max_cut(self):
        # 0x01*256 is a max cut; resume exactly at 255, 256 and 257.
        data = lcg_bytes(300, seed=2)
        forced = b"\x01" * MAX_BLOCK_BYTES + data
        baseline = tuples(slow_split(forced))
        for cut in (MAX_BLOCK_BYTES - 1, MAX_BLOCK_BYTES, MAX_BLOCK_BYTES + 1):
            blocks, _ = splice(forced, cut)
            self.assertEqual(tuples(blocks), baseline, msg=f"cut={cut}")
        blocks, _ = splice(forced, MAX_BLOCK_BYTES)
        self.assertEqual(blocks[0].reason, "max")
        self.assertEqual(blocks[0].size, MAX_BLOCK_BYTES)

    def test_split_one_byte_off_minimum(self):
        # Zero stream cuts content exactly at byte 64.  Resume just before,
        # at, and just after that decision point.
        data = bytes(64) + lcg_bytes(64, seed=15)
        baseline = tuples(slow_split(data))
        for cut in (MIN_BLOCK_BYTES - 1, MIN_BLOCK_BYTES, MIN_BLOCK_BYTES + 1):
            blocks, snapshot = splice(data, cut)
            self.assertEqual(tuples(blocks), baseline, msg=f"cut={cut}")
            self.assertEqual(snapshot.block_start, 0 if cut < 64 else 64)

    def test_empty_trailing_segment(self):
        data = lcg_bytes(300, seed=21)
        # The continuation segment is empty: the tail must still be emitted
        # only by the explicit finish().
        blocks, snapshot = splice(data, len(data))
        self.assertEqual(tuples(blocks), tuples(slow_split(data)))
        self.assertEqual(snapshot.total, len(data))
        second = restore_context(snapshot)
        self.assertEqual(second.add(b""), [])
        tail = second.finish()
        self.assertIsNotNone(tail)
        self.assertEqual(tail.reason, "tail")

    def test_empty_leading_segment(self):
        data = lcg_bytes(200, seed=22)
        first = Chunker()
        self.assertEqual(first.add(b""), [])
        snapshot = export_context(first)
        second = restore_context(snapshot)
        blocks = second.add(data)
        tail = second.finish()
        if tail is not None:
            blocks.append(tail)
        self.assertEqual(tuples(blocks), tuples(slow_split(data)))

    def test_exactly_empty_stream_two_segments(self):
        first = Chunker()
        snapshot = export_context(first)
        second = restore_context(snapshot)
        self.assertEqual(second.add(b""), [])
        self.assertIsNone(second.finish())
        self.assertEqual(tuples(slow_split(b"")), [])


class WindowAndOpenBlockTest(unittest.TestCase):
    """The window is not reset and the whole not-yet-output block (its SHA
    input) survives the export, even when far longer than 32 bytes."""

    def test_window_not_reset_at_segment_end(self):
        data = lcg_bytes(1000, seed=33)
        cut = 500
        first = Chunker()
        first.add(data[:cut])
        snapshot = export_context(first)
        # Full 32-byte window equal to the stream's last 32 bytes, fp from
        # direct division of exactly that window.
        self.assertEqual(snapshot.window, data[cut - WINDOW_SIZE : cut])
        self.assertEqual(snapshot.window_len, WINDOW_SIZE)
        self.assertEqual(
            snapshot.fingerprint,
            slow_remainder(data[cut - WINDOW_SIZE : cut]),
        )

    def test_open_block_is_full_unemitted_suffix_not_last_32_bytes(self):
        # No content cut occurs in a 0x01 run, so at cut=300 the open block
        # spans the whole 44-byte tail after the 256 max cut: far more than
        # the 32-byte window must be retained.
        data = b"\x01" * 300
        cut = 300
        first = Chunker()
        first.add(data[:cut])
        snapshot = export_context(first)
        last_emitted_end = MAX_BLOCK_BYTES
        self.assertEqual(
            snapshot.open_block, data[last_emitted_end:cut]
        )
        self.assertEqual(len(snapshot.open_block), cut - last_emitted_end)
        self.assertEqual(
            snapshot.open_sha256,
            hashlib.sha256(data[last_emitted_end:cut]).hexdigest(),
        )
        # 44 bytes of not-yet-output SHA input retained, not just 32.
        self.assertEqual(len(snapshot.open_block), 44)
        self.assertGreater(len(snapshot.open_block), WINDOW_SIZE)

    def test_open_block_agrees_with_continuous_run_at_every_cut(self):
        data = lcg_bytes(500, seed=44)
        continuous = slow_split(data)
        for cut in range(len(data) + 1):
            first = Chunker()
            first.add(data[:cut])
            snapshot = export_context(first)
            # Some continuous block always covers the cut position.
            self.assertTrue(
                any(b.start <= cut <= b.end for b in continuous), msg=f"cut={cut}"
            )
            # Only content/max blocks exist before finish(); the reference
            # tail block (if any) must not be counted as a cut point.
            ends = [b.end for b in continuous if b.reason != "tail"]
            starts = [end for end in ends if end <= cut]
            block_start = starts[-1] if starts else 0
            self.assertEqual(snapshot.block_start, block_start, msg=f"cut={cut}")
            self.assertEqual(
                snapshot.open_block, data[block_start:cut], msg=f"cut={cut}"
            )
            self.assertEqual(
                snapshot.open_sha256,
                hashlib.sha256(data[block_start:cut]).hexdigest(),
                msg=f"cut={cut}",
            )

    def test_offset_fields_consistent(self):
        data = mixed_stream(1200)
        cut = 432
        first = Chunker()
        first.add(data[:cut])
        snapshot = export_context(first)
        self.assertEqual(snapshot.total, cut)
        self.assertEqual(
            len(snapshot.open_block), snapshot.total - snapshot.block_start
        )
        self.assertEqual(snapshot.window_len, min(cut, WINDOW_SIZE))
        self.assertEqual(snapshot.version, CONTEXT_FORMAT_VERSION)


class MultipleSegmentsTest(unittest.TestCase):
    def test_three_segments_with_two_contexts(self):
        data = lcg_bytes(2000, seed=20260925)
        cuts = [37, 700, 2000]
        baseline = tuples(slow_split(data))

        c0 = Chunker()
        blocks = c0.add(data[: cuts[0]])
        snap = export_context(c0)
        for prev, nxt in zip(cuts, cuts[1:]):
            cnext = restore_context(snap)
            blocks.extend(cnext.add(data[prev:nxt]))
            snap = export_context(cnext)
        cend = restore_context(snap)
        tail = cend.finish()
        if tail is not None:
            blocks.append(tail)
        self.assertEqual(tuples(blocks), baseline)

    def test_add_limit_still_enforced_after_restore(self):
        from rabin_cdc import MAX_TOTAL_BYTES, InputTooLargeError

        data = lcg_bytes(200, seed=1)
        first = Chunker()
        first.add(data)
        snapshot = export_context(first)
        second = restore_context(snapshot)
        oversize = b"\x00" * (MAX_TOTAL_BYTES - len(data) + 1)
        with self.assertRaises(InputTooLargeError):
            second.add(oversize)
        # Rejected batch is not consumed; offsets are untouched.
        self.assertEqual(second.total_bytes, len(data))


class SnapshotDetachmentTest(unittest.TestCase):
    """Caller-side mutation of an exported object must not hit live state."""

    def test_feeding_live_chunker_does_not_change_snapshot(self):
        data = lcg_bytes(500, seed=91)
        first = Chunker()
        first.add(data[:120])
        snapshot = export_context(first)
        first.add(data[120:400])
        self.assertEqual(snapshot.total, 120)
        self.assertEqual(len(snapshot.open_block), 120 - snapshot.block_start)
        self.assertEqual(snapshot.window, data[120 - WINDOW_SIZE : 120])

    def test_snapshot_fields_are_frozen(self):
        first = Chunker()
        first.add(b"abcdef")
        snapshot = export_context(first)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            snapshot.total = 42  # type: ignore[misc]
        with self.assertRaises(TypeError):
            snapshot.parameters["poly"] = 0

    def test_restoring_copies_not_aliases(self):
        data = lcg_bytes(300, seed=92)
        first = Chunker()
        first.add(data[:100])
        snapshot = export_context(first)
        second = restore_context(snapshot)
        second.add(data[100:200])
        # The snapshot remains exactly as exported.
        self.assertEqual(snapshot.total, 100)
        self.assertEqual(snapshot.open_block, data[snapshot.block_start : 100])
        # The live chunker's internal buffers are not the snapshot's bytes.
        self.assertIsNot(second._payload, snapshot.open_block)
        self.assertIsNot(second._ring, snapshot.window)

    def test_export_after_finish_and_resume(self):
        first = Chunker()
        first.add(b"hello")
        tail = first.finish()
        self.assertIsNotNone(tail)
        snapshot = export_context(first)
        self.assertTrue(snapshot.finished)
        self.assertEqual(snapshot.open_block, b"")
        second = restore_context(snapshot)
        with self.assertRaises(StreamFinishedError):
            second.add(b"x")
        with self.assertRaises(StreamFinishedError):
            second.finish()


class BlobRoundTripTest(unittest.TestCase):
    def test_serialize_roundtrip_matches(self):
        data = lcg_bytes(400, seed=58)
        cut = 233
        first = Chunker()
        pre = first.add(data[:cut])
        snapshot = export_context(first)
        blob = serialize_context(snapshot)
        restored = deserialize_context(blob)
        self.assertEqual(restored, snapshot)
        second = restore_context(restored)
        post = second.add(data[cut:])
        tail = second.finish()
        blocks = pre + post + ([tail] if tail is not None else [])
        self.assertEqual(tuples(blocks), tuples(slow_split(data)))
        self.assertIsInstance(blob, bytes)
        self.assertTrue(blob.startswith(b"RCDC1"))

    def test_blob_byte_flip_always_rejected(self):
        data = lcg_bytes(200, seed=59)
        first = Chunker()
        first.add(data[:120])
        blob = bytearray(serialize_context(export_context(first)))
        for idx in range(0, len(blob), 7):
            bad = bytearray(blob)
            bad[idx] ^= 0x01
            with self.assertRaises(ContextError):
                restore_context(deserialize_context(bytes(bad)))

    def test_truncated_and_garbage_blobs_rejected(self):
        first = Chunker()
        first.add(lcg_bytes(50, seed=60))
        blob = serialize_context(export_context(first))
        for bad in (b"", b"RCDC1", blob[:-1], blob[:20], b"X" * len(blob)):
            with self.assertRaises(ContextError):
                deserialize_context(bad)
        with self.assertRaises(ContextError):
            deserialize_context("not-bytes")  # type: ignore[arg-type]
        with self.assertRaises(ContextError):
            deserialize_context(True)


class SnapshotValidationTest(unittest.TestCase):
    def setUp(self):
        first = Chunker()
        first.add(lcg_bytes(300, seed=70))
        self.snapshot = export_context(first)

    def _reject(self, snapshot, field=None):
        with self.assertRaises(ContextError) as ctx:
            restore_context(snapshot)
        if field is not None:
            self.assertIn(field, str(ctx.exception))

    def test_foreign_format_version(self):
        self._reject(dataclasses.replace(self.snapshot, version=99), "version")

    def test_bool_rejected_for_int_fields(self):
        self._reject(dataclasses.replace(self.snapshot, version=True), "version")
        self._reject(
            dataclasses.replace(self.snapshot, total=True, block_start=0),
            "total",
        )

    def test_parameter_fingerprint_mismatch(self):
        self._reject(
            dataclasses.replace(
                self.snapshot, param_fingerprint=self.snapshot.param_fingerprint ^ 1
            ),
            "param_fingerprint",
        )

    def test_foreign_parameter_map(self):
        other = dict(self.snapshot.parameters)
        other["poly"] = 0x1021
        self._reject(
            dataclasses.replace(self.snapshot, parameters=other), "parameters"
        )

    def test_fingerprint_out_of_range(self):
        self._reject(
            dataclasses.replace(self.snapshot, fingerprint=1 << 16), "fingerprint"
        )

    def test_stored_fingerprint_disagrees_with_window(self):
        self._reject(
            dataclasses.replace(self.snapshot, fingerprint=self.snapshot.fingerprint ^ 0x1),
            "fingerprint",
        )

    def test_window_length_field_disagrees(self):
        self._reject(
            dataclasses.replace(self.snapshot, window_len=31), "window_len"
        )

    def test_offsets_disagree(self):
        self._reject(
            dataclasses.replace(self.snapshot, block_start=self.snapshot.block_start + 1),
            "open_block",
        )
        self._reject(
            dataclasses.replace(self.snapshot, total=self.snapshot.total + 1),
            "open_block",
        )

    def test_negative_and_oversize_offsets(self):
        snap = dataclasses.replace(
            self.snapshot, total=-1, block_start=0, window=b"", window_len=0,
            fingerprint=0,
        )
        self._reject(snap, "total")
        snap = dataclasses.replace(
            self.snapshot, total=2 << 20, block_start=0,
            open_block=b"", open_sha256=hashlib.sha256(b"").hexdigest(),
        )
        self._reject(snap, "total")

    def test_open_block_digest_mismatch(self):
        self._reject(
            dataclasses.replace(self.snapshot, open_sha256="0" * 64),
            "open_sha256",
        )

    def test_bad_digest_format(self):
        self._reject(
            dataclasses.replace(self.snapshot, open_sha256="ABCDEF" + "0" * 58),
            "open_sha256",
        )
        self._reject(
            dataclasses.replace(self.snapshot, open_sha256="0" * 10),
            "open_sha256",
        )

    def test_window_payload_suffix_must_agree(self):
        # Flip a window byte while making the fingerprint match it: the
        # suffix overlap with the open block must still expose the lie.
        window = bytes([self.snapshot.window[0] ^ 0x80]) + self.snapshot.window[1:]
        self._reject(
            dataclasses.replace(
                self.snapshot,
                window=window,
                fingerprint=slow_remainder(window),
            ),
            "window",
        )

    def test_finished_snapshot_with_open_block_rejected(self):
        self._reject(
            dataclasses.replace(self.snapshot, finished=True), "finished"
        )

    def test_not_a_snapshot(self):
        self._reject({"total": 1})
        self._reject(object())

    def test_validation_is_all_or_nothing(self):
        # A rejected restore leaves nothing behind: retrying with the same
        # good snapshot still works.
        bad = dataclasses.replace(self.snapshot, fingerprint=0)
        for _ in range(3):
            with self.assertRaises(ContextError):
                restore_context(bad)
        chunker = restore_context(self.snapshot)
        self.assertEqual(chunker.total_bytes, self.snapshot.total)


if __name__ == "__main__":
    unittest.main()
