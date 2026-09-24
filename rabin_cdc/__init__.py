"""rabin_cdc: content-defined chunking with a rolling GF(2) fingerprint.

Reusable, dependency-free library.  The primary entry point is
:func:`split_stream`; :class:`Chunker` exposes the same algorithm as a
stateful API.  See ``chunker`` module docstrings for the full contract.
"""

from __future__ import annotations

from ._errors import (
    CDCError,
    ConfigurationError,
    InputTooLargeError,
    InvalidBatchTypeError,
    InvalidEditError,
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
from .replace import KeptBlock, ReplaceResult, replace_region
from ._fingerprint import POLY, WINDOW_SIZE

__all__ = [
    "Block",
    "BytesLike",
    "CDCError",
    "CUT_MASK",
    "Chunker",
    "ConfigurationError",
    "FINGERPRINT_BITS",
    "InputTooLargeError",
    "InvalidBatchTypeError",
    "InvalidEditError",
    "KeptBlock",
    "MAX_BLOCK_BYTES",
    "MAX_TOTAL_BYTES",
    "MIN_BLOCK_BYTES",
    "POLY",
    "REASON_CONTENT",
    "REASON_MAX",
    "REASON_TAIL",
    "ReplaceResult",
    "StreamFinishedError",
    "WINDOW_BYTES",
    "WINDOW_SIZE",
    "replace_region",
    "split_stream",
]
