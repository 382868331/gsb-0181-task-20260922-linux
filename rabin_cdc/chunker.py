"""Content-defined chunker (streaming, boundary-perturbation resistant).

Public contract (fixed)
-----------------------
* :func:`split_stream` takes any iterable of bytes-like batches and returns a
  list of :class:`Block`; splitting the same bytes over *any* batch grouping
  gives identical blocks.
* :class:`Chunker` is the underlying stateful API: :meth:`Chunker.add` feeds
  one batch (returns zero or more blocks) and :meth:`Chunker.finish` returns
  the non-empty tail block or ``None``.
* Block sizes: minimum ``MIN_BLOCK_BYTES`` = 64, maximum ``MAX_BLOCK_BYTES``
  = 256.  A block is cut at the first position, starting at byte 64 of the
  block, whose 32-byte-window fingerprint has its low 6 bits clear
  (``reason = "content"``); if no such position occurs, the block is cut
  forcibly at byte 256 (``reason = "max"``); the final unfinished block is
  emitted with ``reason = "tail"``.
* The rolling window is **not** reset at block boundaries: it slides over
  the uninterrupted byte stream.
* Offsets in :class:`Block` are absolute over the whole stream,
  ``[start, end)``; ``sha256`` is the lowercase hex digest of the block
  payload.
* At most ``MAX_TOTAL_BYTES`` = 1 MiB may be fed to one stream.

Invalid input never mutates state: type and size checks for a batch run
before any of its bytes are consumed.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Iterable, Iterator, Optional, Union

from ._errors import (
    ConfigurationError,
    InputTooLargeError,
    InvalidBatchTypeError,
    StreamFinishedError,
)
from ._fingerprint import (
    BYTE_TABLE,
    REMOVE_TABLE,
    SHIFT_TABLE,
    WINDOW_SIZE,
)

__all__ = [
    "MIN_BLOCK_BYTES",
    "MAX_BLOCK_BYTES",
    "MAX_TOTAL_BYTES",
    "WINDOW_BYTES",
    "FINGERPRINT_BITS",
    "CUT_MASK",
    "REASON_CONTENT",
    "REASON_MAX",
    "REASON_TAIL",
    "Block",
    "Chunker",
    "split_stream",
    "BytesLike",
]

#: A block may not be shorter than this (except the final tail block).
MIN_BLOCK_BYTES = 64

#: A block is forcibly cut once it reaches this length.
MAX_BLOCK_BYTES = 256

#: At most 1 MiB may be fed into a single stream.
MAX_TOTAL_BYTES = 1 << 20

#: Rolling window length in bytes.
WINDOW_BYTES = WINDOW_SIZE

#: Fingerprint width: remainder modulo a degree-16 polynomial.
FINGERPRINT_BITS = 16

#: Cut when ``fingerprint & CUT_MASK == 0``, i.e. the low 6 bits are clear.
CUT_MASK = 0x3F

REASON_CONTENT = "content"
REASON_MAX = "max"
REASON_TAIL = "tail"

BytesLike = Union[bytes, bytearray, memoryview]


@dataclass(frozen=True, slots=True)
class Block:
    """One emitted chunk.

    Attributes:
        start: Absolute stream offset of the first byte (inclusive).
        end: Absolute stream offset of one past the last byte (exclusive).
        size: Block length in bytes (``end - start``).
        sha256: Lowercase hex SHA-256 digest of the block's payload.
        reason: One of :data:`REASON_CONTENT`, :data:`REASON_MAX`,
            :data:`REASON_TAIL`.
    """

    start: int
    end: int
    size: int
    sha256: str
    reason: str


def _check_batch(batch: object, index: Optional[int]) -> bytes:
    """Validate one batch and return an object cheaply indexable to ints.

    Raises before the chunker consumes anything.  ``index`` is the batch
    position in :func:`split_stream`, used to make the error locatable.
    """
    where = "batch" if index is None else f"batch {index}"
    if isinstance(batch, bool) or not isinstance(
        batch, (bytes, bytearray, memoryview)
    ):
        raise InvalidBatchTypeError(
            f"{where}: expected a bytes-like batch (bytes, bytearray or "
            f"memoryview), got {type(batch).__name__}"
        )
    if isinstance(batch, memoryview):
        if batch.format != "B" or batch.itemsize != 1 or batch.ndim != 1:
            raise InvalidBatchTypeError(
                f"{where}: memoryview must be one-dimensional unsigned bytes "
                f"(format='B', itemsize=1, ndim=1), got format={batch.format!r}, "
                f"itemsize={batch.itemsize}, ndim={batch.ndim}"
            )
    return batch  # type: ignore[return-value]


class Chunker:
    """Stateful content-defined chunker.

    Feed bytes-like batches with :meth:`add`; call :meth:`finish` exactly
    once when the stream ends.  A finished chunker cannot be reused.
    """

    __slots__ = (
        "_ring",
        "_head",
        "_filled",
        "_fp",
        "_payload",
        "_block_start",
        "_total",
        "_finished",
    )

    def __init__(self) -> None:
        # Circular 32-byte window (oldest byte sits at _head while full).
        self._ring = bytearray(WINDOW_SIZE)
        self._head = 0
        self._filled = 0
        self._fp = 0
        # Bytes of the block currently being assembled.
        self._payload = bytearray()
        self._block_start = 0
        self._total = 0
        self._finished = False

    @property
    def total_bytes(self) -> int:
        """Number of bytes accepted into the stream so far."""
        return self._total

    @property
    def finished(self) -> bool:
        """Whether :meth:`finish` has been called."""
        return self._finished

    def add(self, batch: BytesLike) -> list[Block]:
        """Consume one bytes-like batch and return any completed blocks.

        Raises :class:`InvalidBatchTypeError` for a wrong type or
        :class:`InputTooLargeError` if the 1 MiB stream limit would be
        crossed; in either case no byte of the batch is consumed.
        """
        return self._add(batch, None)

    def _add(self, batch: object, index: Optional[int]) -> list[Block]:
        if self._finished:
            raise StreamFinishedError(
                "cannot add() after finish(); open a new Chunker"
            )
        data = _check_batch(batch, index)
        length = len(data)
        if self._total + length > MAX_TOTAL_BYTES:
            raise InputTooLargeError(
                f"stream size would be {self._total + length} bytes, "
                f"exceeding the {MAX_TOTAL_BYTES} byte (1 MiB) limit; "
                f"{self._total} bytes already accepted"
            )

        emitted: list[Block] = []
        # Localize hot state and tables for the per-byte loop.
        ring = self._ring
        head = self._head
        filled = self._filled
        fp = self._fp
        payload = self._payload
        block_start = self._block_start
        total = self._total

        shift_table = SHIFT_TABLE
        byte_table = BYTE_TABLE
        remove_table = REMOVE_TABLE
        window = WINDOW_SIZE

        append = payload.append
        for b in data:
            # Roll the 32-byte window: f <- f*x^8 XOR b_new XOR out*x^256,
            # all modulo P, via precomputed tables.  The window is global
            # and keeps sliding across block boundaries.
            if filled == window:
                fp = (
                    shift_table[(fp >> 8) & 0xFF]
                    ^ byte_table[fp & 0xFF]
                    ^ remove_table[ring[head]]
                    ^ b
                )
            else:
                fp = (
                    shift_table[(fp >> 8) & 0xFF]
                    ^ byte_table[fp & 0xFF]
                    ^ b
                )
                filled += 1
            ring[head] = b
            head = (head + 1) & (window - 1)

            append(b)
            total += 1
            block_len = total - block_start

            if block_len >= MAX_BLOCK_BYTES:
                reason = REASON_MAX
            elif block_len >= MIN_BLOCK_BYTES and (fp & CUT_MASK) == 0:
                reason = REASON_CONTENT
            else:
                continue

            emitted.append(
                Block(
                    start=block_start,
                    end=total,
                    size=block_len,
                    sha256=hashlib.sha256(payload).hexdigest(),
                    reason=reason,
                )
            )
            payload = bytearray()
            append = payload.append
            block_start = total

        # Publish local state back only after the whole batch succeeded.
        self._head = head
        self._filled = filled
        self._fp = fp
        self._payload = payload
        self._block_start = block_start
        self._total = total
        return emitted

    def finish(self) -> Optional[Block]:
        """End the stream and return the non-empty tail block, else ``None``.

        After this call the chunker is closed; further calls raise
        :class:`StreamFinishedError`.
        """
        if self._finished:
            raise StreamFinishedError("finish() called more than once")
        self._finished = True
        payload = self._payload
        if not payload:
            self._block_start = self._total
            return None
        block = Block(
            start=self._block_start,
            end=self._total,
            size=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
            reason=REASON_TAIL,
        )
        self._payload = bytearray()
        self._block_start = self._total
        return block


def _iter_batches(chunks: object) -> Iterator[object]:
    if isinstance(chunks, (bytes, bytearray, memoryview)):
        raise InvalidBatchTypeError(
            "chunks must be an iterable of bytes-like batches, not a single "
            f"{type(chunks).__name__} object"
        )
    try:
        iterator = iter(chunks)  # type: ignore[call-overload]
    except TypeError as exc:
        raise InvalidBatchTypeError(
            "chunks must be an iterable of bytes-like batches, got "
            f"{type(chunks).__name__}"
        ) from exc
    return iterator


def split_stream(chunks: Iterable[BytesLike]) -> list[Block]:
    """Split a byte stream into content-defined blocks.

    Args:
        chunks: Any iterable of ``bytes`` / ``bytearray`` / ``memoryview``
            batches.  Empty batches are allowed.  The result depends only on
            the concatenated bytes, never on how they are grouped into
            batches.

    Returns:
        The list of :class:`Block` objects covering ``[0, total)``.  An
        empty stream yields ``[]``.

    Raises:
        InvalidBatchTypeError: ``chunks`` or one of its batches has the
            wrong type (the offending batch index appears in the message).
        InputTooLargeError: Cumulative input would exceed 1 MiB.
    """
    iterator = _iter_batches(chunks)
    chunker = Chunker()
    blocks: list[Block] = []
    for index, batch in enumerate(iterator):
        blocks.extend(chunker._add(batch, index))
    tail = chunker.finish()
    if tail is not None:
        blocks.append(tail)
    return blocks
