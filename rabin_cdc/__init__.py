"""rabin_cdc: content-defined chunking with a rolling GF(2) fingerprint.

Reusable, dependency-free library.  The primary entry point is
:func:`split_stream`; :class:`Chunker` exposes the same algorithm as a
stateful API.  See ``chunker`` module docstrings for the full contract.
"""

from __future__ import annotations

from ._errors import (
    CDCError,
    ConfigurationError,
    EditValidationError,
    InputTooLargeError,
    InvalidBatchTypeError,
    StreamFinishedError,
)
from .chunker import (
    CUT_MASK,
    FINGERPRINT_BITS,
    MAX_BLOCK_BYTES,
    MAX_TOTAL_BYTES,
    MIN_BLOCK_BYTES,
    REASON_CONTENT,
    REASON_MAX,
    REASON_TAIL,
    WINDOW_BYTES,
    Block,
    BytesLike,
    Chunker,
    split_stream,
)
from .diff import (
    KEEP,
    NEW,
    REPLACE,
    BlockMapping,
    ReplacementResult,
    diff_chunks,
    replace_region,
)
from ._fingerprint import POLY, WINDOW_SIZE

__all__ = [
    "Block",
    "BlockMapping",
    "BytesLike",
    "CDCError",
    "CUT_MASK",
    "Chunker",
    "ConfigurationError",
    "EditValidationError",
    "FINGERPRINT_BITS",
    "InputTooLargeError",
    "InvalidBatchTypeError",
    "KEEP",
    "MAX_BLOCK_BYTES",
    "MAX_TOTAL_BYTES",
    "MIN_BLOCK_BYTES",
    "NEW",
    "POLY",
    "REASON_CONTENT",
    "REASON_MAX",
    "REASON_TAIL",
    "REPLACE",
    "ReplacementResult",
    "StreamFinishedError",
    "WINDOW_BYTES",
    "WINDOW_SIZE",
    "diff_chunks",
    "replace_region",
    "split_stream",
]
