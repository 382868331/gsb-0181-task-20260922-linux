"""Middle-region replacement with an old-block -> new-block mapping.

This module adds one edit operation on top of the fixed chunking contract
in :mod:`rabin_cdc.chunker`:

* replace the half-open middle interval ``[offset, offset + old_length)`` of
  an existing object with ``replacement`` bytes (insert when
  ``old_length == 0``, delete when ``replacement`` is empty);
* re-chunk the *whole* resulting object with the exact same splitting rules
  (:func:`rabin_cdc.split_stream`);
* report which old blocks are byte-for-byte preserved, which ones were
  replaced, and which brand-new blocks the edit introduced.

Block identity is the SHA-256 content digest (``Block.sha256``): a chunk is
identified purely by its payload, which is exactly what makes the mapping
stable for identical-content blocks.  See :func:`replace_region` and
:func:`diff_chunks` for the precise definitions.

The implementation is deliberately a full re-scan (the task explicitly
allows rescanning the whole object and imposes no local-rescan bound); it
performs no persistent reference counting and keeps no block store.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional, Union

from .chunker import Block, split_stream
from ._errors import EditValidationError, InputTooLargeError, InvalidBatchTypeError

__all__ = [
    "KEEP",
    "REPLACE",
    "NEW",
    "BlockMapping",
    "ReplacementResult",
    "replace_region",
    "diff_chunks",
]

#: Mapping status: the old block survives unchanged at the same payload
#: position in the new layout.
KEEP = "keep"

#: Mapping status: an old block that was removed (its bytes overlapped the
#: edited middle interval, or chunk re-alignment changed its payload); it has
#: no new-block counterpart.
REPLACE = "replace"

#: Mapping status: a new block with no old-block counterpart (a genuinely new
#: chunk, not a moved identical one).
NEW = "new"


def _as_payload(value: object, what: str) -> bytes:
    """Validate an object/ replacement byte payload (never ``bool``)."""
    if isinstance(value, bool) or not isinstance(
        value, (bytes, bytearray, memoryview)
    ):
        raise InvalidBatchTypeError(
            f"{what}: expected a bytes-like object (bytes, bytearray or "
            f"memoryview), got {type(value).__name__}"
        )
    if isinstance(value, memoryview):
        if value.format != "B" or value.itemsize != 1 or value.ndim != 1:
            raise InvalidBatchTypeError(
                f"{what}: memoryview must be one-dimensional unsigned bytes "
                f"(format='B', itemsize=1, ndim=1), got format={value.format!r}, "
                f"itemsize={value.itemsize}, ndim={value.ndim}"
            )
    return bytes(value)


def _check_offset(value: object, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise EditValidationError(
            f"{what} must be an int, got {type(value).__name__}"
        )
    if value < 0:
        raise EditValidationError(f"{what} must be >= 0, got {value}")
    return value


@dataclass(frozen=True, slots=True)
class BlockMapping:
    """One row of the old-block / new-block correspondence.

    Exactly one of :attr:`old_index` / :attr:`new_index` may be ``None``:

    * ``status == "keep"``: both are set; the old block at ``old_index``
      survives as the new block at ``new_index`` (identical payload, hence
      identical ``sha256`` and ``size``).
    * ``status == "replace"``: only :attr:`old_index` is set; that old block
      does not appear in the new layout.
    * ``status == "new"``: only :attr:`new_index` is set; that new block has
      no old counterpart.
    """

    status: str
    old_index: Optional[int]
    new_index: Optional[int]
    sha256: str
    size: int


@dataclass(frozen=True, slots=True)
class ReplacementResult:
    """Outcome of one middle-region replacement.

    Attributes:
        old_blocks: Chunking of the object before the edit.
        new_blocks: Chunking of the object after the edit (offsets are
            absolute in the *new* object).
        mappings: One :class:`BlockMapping` per old/new block, in layout
            order: all ``keep`` rows first (old layout order), then all
            ``replace`` rows (old layout order), then all ``new`` rows (new
            layout order).  Every old and new block appears exactly once.
        edit_start: Start offset of the replaced region in the new object.
        edit_end: One-past-the-end offset of the replaced region in the new
            object (``edit_start + len(replacement)``).
        new_data: The full object bytes after replacement.
    """

    old_blocks: list[Block]
    new_blocks: list[Block]
    mappings: list[BlockMapping]
    edit_start: int
    edit_end: int
    new_data: bytes

    @property
    def kept_blocks(self) -> list[Block]:
        """Old blocks that survive unchanged, in old-layout order."""
        return [
            self.old_blocks[m.old_index]
            for m in self.mappings
            if m.status == KEEP
        ]

    @property
    def replaced_blocks(self) -> list[Block]:
        """Old blocks that disappear, in old-layout order."""
        return [
            self.old_blocks[m.old_index]
            for m in self.mappings
            if m.status == REPLACE
        ]

    @property
    def added_blocks(self) -> list[Block]:
        """Genuinely new blocks (no identical old payload), new-layout order."""
        return [
            self.new_blocks[m.new_index]
            for m in self.mappings
            if m.status == NEW
        ]


def _validate_edit(
    data_len: int,
    offset: object,
    old_length: object,
    new_len: int,
) -> tuple[int, int]:
    start = _check_offset(offset, "offset")
    if start > data_len:
        raise EditValidationError(
            f"offset {start} is outside the object of length {data_len}"
        )
    length = _check_offset(old_length, "old_length")
    if start + length > data_len:
        raise EditValidationError(
            f"region [{start}, {start + length}) exceeds object length "
            f"{data_len}"
        )
    # Both object versions must obey the same 1 MiB stream boundary; check
    # before any chunking so a rejected call computes no partial result.
    limit = 1 << 20
    if data_len > limit:
        raise InputTooLargeError(
            f"original object is {data_len} bytes, exceeding the {limit} "
            f"byte (1 MiB) limit"
        )
    result_len = data_len - length + new_len
    if result_len > limit:
        raise InputTooLargeError(
            f"object after replacement would be {result_len} bytes, "
            f"exceeding the {limit} byte (1 MiB) limit"
        )
    return start, length


def diff_chunks(
    old_blocks: Iterable[Block], new_blocks: Iterable[Block]
) -> list[BlockMapping]:
    """Build the keep/replace/new mapping for two chunkings of one object.

    Identity is content-based (``sha256``).  When the same payload occurs in
    both layouts (possibly multiple times), occurrences are paired in
    **payload-position order** using stable positional matching, so an
    identical block never drifts to a different identical copy.  Old payloads
    that no longer occur map to ``replace``; new payloads that never occurred
    before map to ``new``.

    The result covers every input block exactly once and is ordered
    keep-rows (old order), then replace-rows (old order), then new-rows (new
    order).
    """
    old_list = list(old_blocks)
    new_list = list(new_blocks)

    # Positions of each hash within each layout.
    old_positions: dict[str, list[int]] = {}
    for i, block in enumerate(old_list):
        old_positions.setdefault(block.sha256, []).append(i)
    new_positions: dict[str, list[int]] = {}
    for j, block in enumerate(new_list):
        new_positions.setdefault(block.sha256, []).append(j)

    # Positional pairing per hash: the k-th occurrence old <-> k-th new.
    keep_old: set[int] = set()
    keep_new: set[int] = set()
    keeps: list[BlockMapping] = []
    for digest, old_indexes in old_positions.items():
        new_indexes = new_positions.get(digest, [])
        paired = min(len(old_indexes), len(new_indexes))
        for k in range(paired):
            oi = old_indexes[k]
            nj = new_indexes[k]
            keep_old.add(oi)
            keep_new.add(nj)
            keeps.append(
                BlockMapping(
                    status=KEEP,
                    old_index=oi,
                    new_index=nj,
                    sha256=digest,
                    size=old_list[oi].size,
                )
            )
    keeps.sort(key=lambda m: m.old_index)

    replaces = [
        BlockMapping(
            status=REPLACE,
            old_index=i,
            new_index=None,
            sha256=block.sha256,
            size=block.size,
        )
        for i, block in enumerate(old_list)
        if i not in keep_old
    ]
    adds = [
        BlockMapping(
            status=NEW,
            old_index=None,
            new_index=j,
            sha256=block.sha256,
            size=block.size,
        )
        for j, block in enumerate(new_list)
        if j not in keep_new
    ]
    return keeps + replaces + adds


def replace_region(
    data: Union[bytes, bytearray, memoryview],
    offset: int,
    old_length: int,
    replacement: Union[bytes, bytearray, memoryview],
) -> ReplacementResult:
    """Replace the middle region ``[offset, offset+old_length)`` of ``data``.

    Args:
        data: The original object (bytes-like).  It is not mutated.
        offset: Absolute byte offset of the region start, ``0 <= offset <=
            len(data)``.  ``0`` and ``len(data)`` are valid (head/tail
            edits); middle edits use ``0 < offset < len(data)``.
        old_length: Number of original bytes to overwrite/remove,
            ``old_length >= 0`` and ``offset + old_length <= len(data)``.
            ``0`` inserts before ``offset``.
        replacement: Bytes-like payload to put in place of the region.  May
            be empty (a pure deletion).  Both endpoints and lengths of any
            size within the 1 MiB result bound are allowed.

    Returns:
        A :class:`ReplacementResult` with both chunkings and the
        keep/replace/new mapping.  Chunking uses exactly the fixed contract
        of :func:`rabin_cdc.split_stream`.

    Raises:
        InvalidBatchTypeError: ``data`` or ``replacement`` is not bytes-like
            (a one-dimensional ``B`` memoryview is accepted; ``bool`` is
            never an integer payload).
        EditValidationError: ``offset`` / ``old_length`` are not real ints,
            are negative, or run past the object.
        InputTooLargeError: The resulting object would exceed 1 MiB.

    Validation happens before any chunking, so a rejected call leaves no
    partially computed state behind.
    """
    data_bytes = _as_payload(data, "data")
    repl_bytes = _as_payload(replacement, "replacement")
    data_len = len(data_bytes)
    start, length = _validate_edit(data_len, offset, old_length, len(repl_bytes))

    new_data = data_bytes[:start] + repl_bytes + data_bytes[start + length :]

    old_blocks = split_stream([data_bytes])
    new_blocks = split_stream([new_data])
    mappings = diff_chunks(old_blocks, new_blocks)

    return ReplacementResult(
        old_blocks=old_blocks,
        new_blocks=new_blocks,
        mappings=mappings,
        edit_start=start,
        edit_end=start + len(repl_bytes),
        new_data=new_data,
    )
