"""Middle-of-object replacement with a retained/replaced/added block mapping.

This module adds one object-level entry point on top of the unchanged
chunking rules in :mod:`rabin_cdc.chunker`:

    result = replace_region(obj, start, end, replacement)

replaces the half-open byte range ``obj[start:end]`` with ``replacement``
(insertion when ``start == end``, deletion when ``replacement == b""``),
re-chunks the old and the new object with :func:`split_stream`, and returns a
:class:`ReplaceResult` mapping every old block to one of:

* **kept**    -- the block's content (identified by its SHA-256) is present in
                 the new chunking; a :class:`KeptBlock` pairs the old block
                 with its new twin (offsets may have shifted);
* **replaced**-- the old block's content no longer exists in the new object;
* **added**   -- a new block whose content did not exist in the old object.

``kept`` old blocks plus ``replaced`` partition the old chunking; ``kept``
new blocks plus ``added`` partition the new chunking.

Stable identity
---------------
A block's identity is its payload SHA-256, never its position: identical
content anywhere in either object always maps to the same identity.  When
the same content occurs several times, copies are paired deterministically
in stream order -- common prefix first, then common suffix, then earliest
feasible matches in between (a monotone, order-preserving alignment).  The
edit only ever churns a small bounded region around itself; blocks before
the edit keep their offsets and blocks after realignment keep theirs
shifted by the byte-length delta.  The mapping itself does not depend on
that bound: the whole object is rescanned, as this iteration explicitly
allows.

The function is pure: all validation happens before any chunking, so an
invalid call raises without touching its arguments.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Deque, Dict, List

from ._errors import (
    InputTooLargeError,
    InvalidBatchTypeError,
    InvalidEditError,
)
from .chunker import MAX_TOTAL_BYTES, Block, split_stream

__all__ = [
    "KeptBlock",
    "ReplaceResult",
    "replace_region",
]


@dataclass(frozen=True, slots=True)
class KeptBlock:
    """One old block whose content survives in the new chunking.

    Attributes:
        old: The block as chunked from the original object.
        new: The block as chunked from the edited object.  ``old.sha256 ==
            new.sha256`` and ``old.size == new.size`` always; only the
            absolute offsets may differ.
    """

    old: Block
    new: Block

    @property
    def sha256(self) -> str:
        """Stable content identity shared by the paired blocks."""
        return self.old.sha256

    @property
    def offset_shift(self) -> int:
        """Signed byte shift of the content: ``new.start - old.start``."""
        return self.new.start - self.old.start


@dataclass(frozen=True, slots=True)
class ReplaceResult:
    """Outcome of one :func:`replace_region` call.

    Attributes:
        start: Start of the replaced half-open range in the old object.
        end: End of the replaced half-open range in the old object.
        replacement: The bytes spliced in.
        old_blocks: Full chunking of the original object (old stream order).
        new_blocks: Full chunking of the edited object (new stream order).
        kept: Old blocks surviving by content identity, paired with their
            new twins (in old stream order).
        replaced: Old blocks whose content disappeared (old stream order).
        added: New blocks whose content did not exist before (new stream
            order).
    """

    start: int
    end: int
    replacement: bytes
    old_blocks: tuple[Block, ...]
    new_blocks: tuple[Block, ...]
    kept: tuple[KeptBlock, ...]
    replaced: tuple[Block, ...]
    added: tuple[Block, ...]

    @property
    def old_size(self) -> int:
        """Length of the original object in bytes."""
        return sum(b.size for b in self.old_blocks)

    @property
    def new_size(self) -> int:
        """Length of the edited object in bytes."""
        return sum(b.size for b in self.new_blocks)

    @property
    def length_delta(self) -> int:
        """Byte-length change: ``new_size - old_size``."""
        return self.new_size - self.old_size


def _check_bytes_like(name: str, value: object) -> bytes:
    """Validate an object/replacement argument and return plain ``bytes``.

    Mirrors :func:`rabin_cdc.chunker._check_batch`: bools are not accepted,
    and memoryviews must be one-dimensional unsigned bytes.
    """
    if isinstance(value, bool) or not isinstance(
        value, (bytes, bytearray, memoryview)
    ):
        raise InvalidBatchTypeError(
            f"{name}: expected a bytes-like object (bytes, bytearray or "
            f"memoryview), got {type(value).__name__}"
        )
    if isinstance(value, memoryview):
        if value.format != "B" or value.itemsize != 1 or value.ndim != 1:
            raise InvalidBatchTypeError(
                f"{name}: memoryview must be one-dimensional unsigned bytes "
                f"(format='B', itemsize=1, ndim=1), got format={value.format!r}, "
                f"itemsize={value.itemsize}, ndim={value.ndim}"
            )
    return bytes(value)


def _check_offset(name: str, value: object) -> int:
    """Validate a half-open-range endpoint: a real non-bool ``int``."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidEditError(
            f"{name}: offset must be an integer, got {type(value).__name__}"
        )
    if value < 0:
        raise InvalidEditError(f"{name}: offset must be >= 0, got {value}")
    return value


def _align(
    old_blocks: List[Block], new_blocks: List[Block]
) -> tuple[List[KeptBlock], List[Block], List[Block]]:
    """Pair old/new blocks by SHA-256 content identity, order preserving.

    Deterministic alignment in three stages: (1) common-prefix pairs at
    equal positions (the byte region before an edit chunks identically);
    (2) common-suffix pairs from the end (recovered blocks, only shifted by
    the length delta); (3) earliest-feasible in-order pairing of the blocks
    in between.  The greedy middle pass is the standard maximum-cardinality
    monotone match for an equality relation: taking the earliest available
    position leaves the most room for later old blocks.  Repeated identical
    content is therefore matched stably in stream order.
    """
    n = len(old_blocks)
    m = len(new_blocks)

    prefix = 0
    while (
        prefix < n
        and prefix < m
        and old_blocks[prefix].sha256 == new_blocks[prefix].sha256
    ):
        prefix += 1

    suffix = 0
    while (
        prefix + suffix < n
        and prefix + suffix < m
        and old_blocks[n - 1 - suffix].sha256
        == new_blocks[m - 1 - suffix].sha256
    ):
        suffix += 1

    old_middle = old_blocks[prefix : n - suffix]
    new_middle = new_blocks[prefix : m - suffix]

    queues: Dict[str, Deque[int]] = defaultdict(deque)
    for j, block in enumerate(new_middle):
        queues[block.sha256].append(j)

    kept: List[KeptBlock] = [
        KeptBlock(old_blocks[i], new_blocks[i]) for i in range(prefix)
    ]
    replaced: List[Block] = []
    matched_new: set[int] = set()
    last = -1
    for old_block in old_middle:
        positions = queues.get(old_block.sha256)
        while positions is not None and positions and positions[0] <= last:
            positions.popleft()
        if positions:
            j = positions.popleft()
            last = j
            matched_new.add(j)
            kept.append(KeptBlock(old_block, new_middle[j]))
        else:
            replaced.append(old_block)

    # Suffix pairs come after every middle block in old stream order.
    kept.extend(
        KeptBlock(old_blocks[n - k], new_blocks[m - k])
        for k in range(suffix, 0, -1)
    )

    added = [block for j, block in enumerate(new_middle) if j not in matched_new]
    return kept, replaced, added


def replace_region(
    obj: "bytes | bytearray | memoryview",
    start: int,
    end: int,
    replacement: "bytes | bytearray | memoryview",
) -> ReplaceResult:
    """Replace ``obj[start:end]`` with ``replacement`` and map old to new.

    Args:
        obj: The original object as one bytes-like object.
        start: Inclusive start offset of the replaced range (``0`` edits the
            head; ``start == end`` inserts).
        end: Exclusive end offset (``end == len(obj)`` edits the tail;
            ``replacement == b""`` deletes the range).
        replacement: Bytes-like content spliced in place of ``obj[start:end]``.

    Returns:
        A :class:`ReplaceResult` with both full chunkings and the
        kept/replaced/added mapping.

    Raises:
        InvalidBatchTypeError: ``obj`` or ``replacement`` is not bytes-like
            (this is also a :class:`TypeError`).
        InvalidEditError: ``start``/``end`` are not non-bool integers, are
            negative, or describe an out-of-order/out-of-bounds range.
        InputTooLargeError: The edited object would exceed
            :data:`MAX_TOTAL_BYTES` (1 MiB).
    """
    data = _check_bytes_like("obj", obj)
    rep = _check_bytes_like("replacement", replacement)
    start_i = _check_offset("start", start)
    end_i = _check_offset("end", end)

    if end_i < start_i:
        raise InvalidEditError(
            f"end ({end_i}) must not be smaller than start ({start_i})"
        )
    if end_i > len(data):
        raise InvalidEditError(
            f"end ({end_i}) is out of bounds for an object of {len(data)} bytes"
        )

    new_size = len(data) - (end_i - start_i) + len(rep)
    if new_size > MAX_TOTAL_BYTES:
        raise InputTooLargeError(
            f"edited object would be {new_size} bytes, exceeding the "
            f"{MAX_TOTAL_BYTES} byte (1 MiB) limit; old object is "
            f"{len(data)} bytes"
        )

    # All validation has happened before any byte is copied or chunked.
    new_data = data[:start_i] + rep + data[end_i:]
    old_blocks = split_stream([data])
    new_blocks = split_stream([new_data])
    kept, replaced, added = _align(old_blocks, new_blocks)

    return ReplaceResult(
        start=start_i,
        end=end_i,
        replacement=rep,
        old_blocks=tuple(old_blocks),
        new_blocks=tuple(new_blocks),
        kept=tuple(kept),
        replaced=tuple(replaced),
        added=tuple(added),
    )
