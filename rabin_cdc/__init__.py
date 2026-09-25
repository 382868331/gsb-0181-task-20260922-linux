"""rabin_cdc: content-defined chunking with a rolling GF(2) fingerprint.

Reusable, dependency-free library.  The primary entry point is
:func:`split_stream`; :class:`Chunker` exposes the same algorithm as a
stateful API, and :class:`BoundaryContext` (via ``Chunker.export_context``
/ ``Chunker.from_context``) lets adjacent segments of one stream be
processed separately with results identical to a continuous split.  See
``chunker`` module docstrings for the full contract.
"""

from __future__ import annotations

from ._errors import (
    CDCError,
    ConfigurationError,
    InputTooLargeError,
    InvalidBatchTypeError,
    InvalidContextError,
    StreamFinishedError,
)
from .chunker import (
    CONTEXT_FORMAT_VERSION,
    CUT_MASK,
    FINGERPRINT_BITS,
    MAX_BLOCK_BYTES,
    MAX_TOTAL_BYTES,
    MIN_BLOCK_BYTES,
    PARAMETERS_FINGERPRINT,
    REASON_CONTENT,
    REASON_MAX,
    REASON_TAIL,
    WINDOW_BYTES,
    Block,
    BoundaryContext,
    BytesLike,
    Chunker,
    split_stream,
)
from ._fingerprint import POLY, WINDOW_SIZE, stream_remainder

__all__ = [
    "Block",
    "BoundaryContext",
    "BytesLike",
    "CDCError",
    "CONTEXT_FORMAT_VERSION",
    "CUT_MASK",
    "Chunker",
    "ConfigurationError",
    "FINGERPRINT_BITS",
    "InputTooLargeError",
    "InvalidBatchTypeError",
    "InvalidContextError",
    "MAX_BLOCK_BYTES",
    "MAX_TOTAL_BYTES",
    "MIN_BLOCK_BYTES",
    "PARAMETERS_FINGERPRINT",
    "POLY",
    "REASON_CONTENT",
    "REASON_MAX",
    "REASON_TAIL",
    "StreamFinishedError",
    "WINDOW_BYTES",
    "WINDOW_SIZE",
    "split_stream",
    "stream_remainder",
]
