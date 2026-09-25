"""Boundary context: splice independently fed segments of one byte stream.

Processing adjacent segments of the *same* byte stream in separate sessions
must produce exactly the blocks obtained from one uninterrupted run.  This
module provides the export / resume entry points:

* :func:`export_context` snapshots a live :class:`Chunker` mid-stream;
* :func:`restore_context` validates a snapshot wholesale and returns a new
  :class:`Chunker` whose Rabin window, fingerprint, open (not yet emitted)
  block content and absolute offsets are identical, so :meth:`Chunker.add`
  simply continues the stream;
* :func:`serialize_context` / :func:`deserialize_context` pack a snapshot
  into a self-describing, integrity-protected byte blob;
* the tail block is still committed only by an explicit
  :meth:`Chunker.finish`.

Snapshot contents
-----------------
A :class:`ContextSnapshot` carries a format version, a fingerprint of the
algorithm parameters, the logical rolling-window bytes (the whole window,
never a truncated suffix), the window length, the current 16-bit fingerprint,
the full open-block payload (bytes already accepted but not part of any
emitted block) together with its SHA-256, the open block's absolute start
offset and the cumulative absolute stream offset.

Consistency is checked **as a whole before any chunker is constructed**:
format version and parameter fingerprint must match this build; window
length, open-block length and cumulative offset must agree with each other;
and the fingerprint is re-derived from the stored window by *direct
polynomial long division* -- the rolling update tables are deliberately not
used here.

Snapshots are immutable (``frozen`` dataclass, ``bytes`` fields, read-only
parameter mapping); exporting copies out of the live chunker, so mutating the
chunker afterwards can never alter a snapshot, and restoring copies again.
"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from ._errors import ContextError
from ._fingerprint import POLY, WINDOW_SIZE
from .chunker import (
    CUT_MASK,
    FINGERPRINT_BITS,
    MAX_BLOCK_BYTES,
    MAX_TOTAL_BYTES,
    MIN_BLOCK_BYTES,
    Chunker,
)

__all__ = [
    "CONTEXT_FORMAT_VERSION",
    "ContextSnapshot",
    "export_context",
    "restore_context",
    "serialize_context",
    "deserialize_context",
    "parameter_fingerprint",
]

#: Bumped only when the on-disk/over-the-wire snapshot meaning changes.
CONTEXT_FORMAT_VERSION = 1

#: Portable blob magic for :func:`serialize_context`.
_CONTEXT_MAGIC = b"RCDC1"

#: Fixed-size header after the magic (all integers big-endian, unsigned):
#: version u8, param_fingerprint u64, window_len u32, fingerprint u16,
#: block_start u64, total u64, finished u8, open-block SHA-256 (32 bytes).
_HEADER = struct.Struct(">BQIHQQB32s")
_LEN_PREFIX = struct.Struct(">I")
_DIGEST_SIZE = 32

_FINGERPRINT_MAX = (1 << FINGERPRINT_BITS) - 1

#: Canonical parameter set captured by every snapshot.
_PARAMETERS: Mapping[str, int] = MappingProxyType(
    {
        "poly": POLY,
        "window_bytes": WINDOW_SIZE,
        "min_block_bytes": MIN_BLOCK_BYTES,
        "max_block_bytes": MAX_BLOCK_BYTES,
        "cut_mask": CUT_MASK,
        "fingerprint_bits": FINGERPRINT_BITS,
        "max_total_bytes": MAX_TOTAL_BYTES,
    }
)


def parameter_fingerprint(parameters: Mapping[str, int] = _PARAMETERS) -> int:
    """Hash the canonical algorithm parameters into a 64-bit fingerprint.

    A snapshot produced under different chunking constants cannot be silently
    resumed by this build: its parameter fingerprint will not match.
    """
    canonical = "|".join(
        f"{key}={parameters[key]}"
        for key in (
            "poly",
            "window_bytes",
            "min_block_bytes",
            "max_block_bytes",
            "cut_mask",
            "fingerprint_bits",
            "max_total_bytes",
        )
    )
    digest = hashlib.sha256(
        f"rabin_cdc-context-v{CONTEXT_FORMAT_VERSION}|{canonical}".encode("ascii")
    ).digest()
    return int.from_bytes(digest[:8], "big")


@dataclass(frozen=True, slots=True)
class ContextSnapshot:
    """Immutable boundary context for resuming a stream elsewhere.

    Attributes:
        version: Snapshot format version (:data:`CONTEXT_FORMAT_VERSION`).
        parameters: Read-only mapping of the algorithm constants in force.
        param_fingerprint: Hash of ``parameters``, checked on restore.
        window: Logical rolling-window bytes, oldest byte first.  Its length
            is ``min(32, total)``; the full window is always present.
        window_len: Length of ``window`` in bytes (must equal ``len(window)``
            and ``min(32, total)``).
        fingerprint: Current 16-bit window remainder modulo P(x).
        open_block: All accepted bytes not yet covered by an emitted block,
            i.e. the block still being assembled (its not-yet-output SHA-256
            input is carried here, never discarded with the window).
        block_start: Absolute stream offset of ``open_block``'s first byte.
        total: Cumulative number of bytes accepted into the stream.
        finished: Whether the stream was already finished; such a snapshot
            may be restored for inspection but rejects further ``add``.
        open_sha256: Lowercase hex SHA-256 of ``open_block``; redundant with
            the payload, so a tampered payload cannot pass validation.
    """

    version: int
    parameters: Mapping[str, int]
    param_fingerprint: int
    window: bytes
    window_len: int
    fingerprint: int
    open_block: bytes
    block_start: int
    total: int
    finished: bool
    open_sha256: str


def _is_plain_int(value: object) -> bool:
    """Reject bool where an integer field is required."""
    return isinstance(value, int) and not isinstance(value, bool)


def _direct_remainder(window: bytes) -> int:
    """Remainder of ``window``'s polynomial modulo P by direct long division.

    This is the independent check used when restoring a context: the whole
    window is shifted in one bit at a time, MSB first.  It must not consult
    the rolling update tables (SHIFT/BYTE/REMOVE) -- a snapshot has to verify
    the fingerprint it carries, not merely replay a recurrence.
    """
    remainder = 0
    for byte in window:
        for bit in (7, 6, 5, 4, 3, 2, 1, 0):
            top = (remainder >> 15) & 1
            remainder = ((remainder << 1) | ((byte >> bit) & 1)) & 0xFFFF
            if top:
                remainder ^= POLY & 0xFFFF
    return remainder


def export_context(chunker: Chunker) -> ContextSnapshot:
    """Export an immutable, detached boundary context from a live chunker.

    Works both before and after :meth:`Chunker.finish` (a finished stream
    exports an empty open block).  The returned snapshot never aliases the
    chunker's internal buffers: feeding more bytes to the chunker cannot
    change it.
    """
    (
        ring,
        head,
        filled,
        fp,
        payload,
        block_start,
        total,
        finished,
    ) = chunker._snapshot_state()

    # Read the circular window in logical (oldest-first) order and copy into
    # immutable bytes.  The *entire* window is exported -- never just the
    # most recent bytes -- and the open block payload is exported separately
    # so its SHA-256 input cannot be lost.
    if filled == WINDOW_SIZE:
        window = bytes(ring[head:]) + bytes(ring[:head])
    else:
        window = bytes(ring[:filled])

    return ContextSnapshot(
        version=CONTEXT_FORMAT_VERSION,
        parameters=_PARAMETERS,
        param_fingerprint=parameter_fingerprint(),
        window=window,
        window_len=filled,
        fingerprint=fp,
        open_block=bytes(payload),
        block_start=block_start,
        total=total,
        finished=finished,
        open_sha256=hashlib.sha256(payload).hexdigest(),
    )


def restore_context(snapshot: ContextSnapshot) -> Chunker:
    """Validate ``snapshot`` wholesale and return a resumed :class:`Chunker`.

    Raises :class:`ContextError` if the format version or parameter
    fingerprint is foreign, any field has the wrong type/range, the window
    length / open-block length / cumulative offsets disagree, or the stored
    fingerprint does not equal a direct long division of the stored window.
    Validation completes (and fails) before any chunker state exists, so a
    rejected snapshot can never leave a partially constructed chunker.
    """
    _validate(snapshot)
    assert isinstance(snapshot, ContextSnapshot)

    window = snapshot.window
    filled = snapshot.window_len

    # Rebuild the internal ring layout: while full the oldest byte lives at
    # index 0 (head == 0); while filling, bytes occupy [0, filled) and head
    # points at the next free slot, exactly as in a freshly started stream.
    ring = bytearray(WINDOW_SIZE)
    if filled == WINDOW_SIZE:
        ring[:] = window
        head = 0
    else:
        ring[:filled] = window
        head = filled

    return Chunker._from_state(
        ring=ring,
        head=head,
        filled=filled,
        fp=snapshot.fingerprint,
        payload=bytearray(snapshot.open_block),
        block_start=snapshot.block_start,
        total=snapshot.total,
        finished=snapshot.finished,
    )


def _fail(field: str, message: str) -> None:
    raise ContextError(f"invalid context snapshot ({field}): {message}")


def _validate(snapshot: object) -> None:
    if not isinstance(snapshot, ContextSnapshot):
        raise ContextError(
            "invalid context snapshot: expected ContextSnapshot, got "
            f"{type(snapshot).__name__}"
        )

    if not _is_plain_int(snapshot.version):
        _fail("version", "must be an int")
    if snapshot.version != CONTEXT_FORMAT_VERSION:
        _fail(
            "version",
            f"unsupported format version {snapshot.version}; this build "
            f"supports {CONTEXT_FORMAT_VERSION}",
        )

    if not isinstance(snapshot.parameters, Mapping):
        _fail("parameters", "must be a mapping")
    if dict(snapshot.parameters) != dict(_PARAMETERS):
        _fail(
            "parameters",
            "algorithm parameters differ from this build's fixed constants",
        )
    if not _is_plain_int(snapshot.param_fingerprint):
        _fail("param_fingerprint", "must be an int")
    if snapshot.param_fingerprint != parameter_fingerprint():
        _fail(
            "param_fingerprint",
            f"mismatch: snapshot {snapshot.param_fingerprint:#018x}, this "
            f"build {parameter_fingerprint():#018x}",
        )

    if not isinstance(snapshot.window, bytes):
        _fail("window", "must be bytes")
    if not _is_plain_int(snapshot.window_len):
        _fail("window_len", "must be an int")
    if not 0 <= snapshot.window_len <= WINDOW_SIZE:
        _fail("window_len", f"must be in [0, {WINDOW_SIZE}]")
    if len(snapshot.window) != snapshot.window_len:
        _fail(
            "window_len",
            f"window length field {snapshot.window_len} disagrees with "
            f"len(window) {len(snapshot.window)}",
        )

    if not _is_plain_int(snapshot.fingerprint):
        _fail("fingerprint", "must be an int")
    if not 0 <= snapshot.fingerprint <= _FINGERPRINT_MAX:
        _fail("fingerprint", f"must be a {FINGERPRINT_BITS}-bit unsigned value")

    if not isinstance(snapshot.open_block, bytes):
        _fail("open_block", "must be bytes")
    for name, value in (
        ("block_start", snapshot.block_start),
        ("total", snapshot.total),
    ):
        if not _is_plain_int(value):
            _fail(name, "must be an int")
    if not 0 <= snapshot.total <= MAX_TOTAL_BYTES:
        _fail("total", f"must be in [0, {MAX_TOTAL_BYTES}]")
    if not 0 <= snapshot.block_start <= snapshot.total:
        _fail(
            "block_start",
            f"must satisfy 0 <= block_start ({snapshot.block_start}) <= "
            f"total ({snapshot.total})",
        )
    if len(snapshot.open_block) != snapshot.total - snapshot.block_start:
        _fail(
            "open_block",
            f"open block length {len(snapshot.open_block)} must equal "
            f"total - block_start "
            f"({snapshot.total} - {snapshot.block_start} = "
            f"{snapshot.total - snapshot.block_start})",
        )

    # The window slides over the uninterrupted stream and is never reset, so
    # after ``total`` accepted bytes it always holds min(total, 32) bytes.
    if snapshot.window_len != min(snapshot.total, WINDOW_SIZE):
        _fail(
            "window_len",
            f"must equal min(total, {WINDOW_SIZE}) = "
            f"{min(snapshot.total, WINDOW_SIZE)} for an unbroken stream",
        )

    if not isinstance(snapshot.finished, bool):
        _fail("finished", "must be a bool")
    if snapshot.finished and (snapshot.open_block or snapshot.block_start != snapshot.total):
        _fail(
            "finished",
            "a finished stream must carry no open block "
            "(block_start == total, open_block empty)",
        )

    if not isinstance(snapshot.open_sha256, str):
        _fail("open_sha256", "must be a lowercase hex str")
    if (
        len(snapshot.open_sha256) != 64
        or any(ch not in "0123456789abcdef" for ch in snapshot.open_sha256)
    ):
        _fail("open_sha256", "must be 64 lowercase hex characters")
    actual_digest = hashlib.sha256(snapshot.open_block).hexdigest()
    if actual_digest != snapshot.open_sha256:
        _fail(
            "open_sha256",
            "digest does not match the stored open-block payload; the "
            "not-yet-output block bytes or their SHA input are corrupt",
        )

    # Both the window and the open block end at the same stream position
    # (``total``) because the window is never reset and the open block is a
    # contiguous suffix of the accepted bytes.  The window must therefore be
    # a suffix of the open block whenever the latter is long enough; if the
    # stream has no cut yet, they overlap in the stream prefix and the open
    # block must be a suffix of (i.e. equal to) the whole window region.
    if snapshot.total:
        overlap = min(snapshot.window_len, len(snapshot.open_block))
        if overlap and snapshot.window[-overlap:] != snapshot.open_block[-overlap:]:
            _fail(
                "window",
                "the rolling window and the open block must be suffixes of "
                "one another (both end at total), but their trailing "
                f"{overlap} bytes disagree",
            )

    # The decisive cross-check: recompute the window remainder directly by
    # polynomial long division, never via the rolling update tables.
    direct = _direct_remainder(snapshot.window)
    if direct != snapshot.fingerprint:
        _fail(
            "fingerprint",
            f"stored fingerprint {snapshot.fingerprint:#06x} does not match "
            f"the direct polynomial division of the stored window "
            f"{direct:#06x}",
        )


def serialize_context(snapshot: ContextSnapshot) -> bytes:
    """Pack a snapshot into a self-describing portable byte blob.

    Layout (all integers big-endian unsigned): ``RCDC1`` magic, version u8,
    parameter fingerprint u64, window length u32, window remainder u16,
    block start u64, total u64, finished u8, open-block SHA-256 (32 bytes),
    then the window bytes and the open-block bytes (each preceded by a u32
    length), and finally a SHA-256 over every preceding byte.  The trailing
    digest makes any transport corruption fail loudly; restoring a parsed
    blob still goes through the full validation of :func:`restore_context`
    (including direct long division of the window).
    """
    if not isinstance(snapshot, ContextSnapshot):
        raise ContextError(
            "serialize_context expects a ContextSnapshot, got "
            f"{type(snapshot).__name__}"
        )
    header = _HEADER.pack(
        snapshot.version,
        snapshot.param_fingerprint,
        snapshot.window_len,
        snapshot.fingerprint,
        snapshot.block_start,
        snapshot.total,
        1 if snapshot.finished else 0,
        bytes.fromhex(snapshot.open_sha256),
    )
    body = b"".join(
        (
            _CONTEXT_MAGIC,
            header,
            _LEN_PREFIX.pack(len(snapshot.window)),
            snapshot.window,
            _LEN_PREFIX.pack(len(snapshot.open_block)),
            snapshot.open_block,
        )
    )
    return body + hashlib.sha256(body).digest()


def deserialize_context(blob: bytes) -> ContextSnapshot:
    """Parse a blob produced by :func:`serialize_context`.

    Verifies the trailing whole-blob SHA-256 and structural framing here;
    semantic consistency is verified later by :func:`restore_context`.
    A truncated, corrupted or wrongly typed blob raises :class:`ContextError`.
    """
    if isinstance(blob, bool) or not isinstance(blob, (bytes, bytearray)):
        raise ContextError(
            f"context blob must be bytes-like, got {type(blob).__name__}"
        )
    blob = bytes(blob)
    if len(blob) < _DIGEST_SIZE:
        raise ContextError("context blob is truncated (shorter than digest)")
    body, digest = blob[:-_DIGEST_SIZE], blob[-_DIGEST_SIZE:]
    if hashlib.sha256(body).digest() != digest:
        raise ContextError(
            "context blob integrity check failed: SHA-256 mismatch "
            "(corrupt or tampered blob)"
        )
    if not body.startswith(_CONTEXT_MAGIC):
        raise ContextError("context blob has a bad magic prefix")
    header_start = len(_CONTEXT_MAGIC)
    try:
        (
            version,
            param_fp,
            window_len,
            fp,
            block_start,
            total,
            finished,
            open_digest,
        ) = _HEADER.unpack_from(body, header_start)
    except struct.error as exc:
        raise ContextError(f"context blob header is truncated: {exc}") from exc

    cursor = header_start + _HEADER.size

    def read_field(name: str, at: int) -> tuple[bytes, int]:
        if at + _LEN_PREFIX.size > len(body):
            raise ContextError(f"context blob is truncated at {name} length")
        (length,) = _LEN_PREFIX.unpack_from(body, at)
        at += _LEN_PREFIX.size
        if at + length > len(body):
            raise ContextError(f"context blob is truncated inside {name}")
        return body[at : at + length], at + length

    window, cursor = read_field("window", cursor)
    open_block, cursor = read_field("open_block", cursor)
    if cursor != len(body):
        raise ContextError("context blob has trailing garbage bytes")

    return ContextSnapshot(
        version=version,
        parameters=_PARAMETERS,
        param_fingerprint=param_fp,
        window=window,
        window_len=window_len,
        fingerprint=fp,
        open_block=open_block,
        block_start=block_start,
        total=total,
        finished=bool(finished),
        open_sha256=open_digest.hex(),
    )
