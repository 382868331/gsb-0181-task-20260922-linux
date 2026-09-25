"""rabin_cdc: content-defined chunking with a rolling GF(2) fingerprint.

Reusable, dependency-free library.  The primary entry point is
:func:`split_stream`; :class:`Chunker` exposes the same algorithm as a
stateful API.  Adjacent segments of one logical stream can be processed
separately via :func:`export_context` / :func:`restore_context`.  See
``chunker`` / ``_context`` module docstrings for the full contract.
"""

from __future__ import annotations

from ._errors import (
    CDCError,
    ConfigurationError,
    ContextError,
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
from ._context import (
    CONTEXT_FORMAT_VERSION,
    ContextSnapshot,
    deserialize_context,
    export_context,
    parameter_fingerprint,
    restore_context,
    serialize_context,
)
from ._fingerprint import POLY, WINDOW_SIZE

__all__ = [
    "Block",
    "BytesLike",
    "CDCError",
    "CONTEXT_FORMAT_VERSION",
    "ContextError",
    "ContextSnapshot",
    "CUT_MASK",
    "Chunker",
    "ConfigurationError",
    "FINGERPRINT_BITS",
    "InputTooLargeError",
    "InvalidBatchTypeError",
    "MAX_BLOCK_BYTES",
    "MAX_TOTAL_BYTES",
    "MIN_BLOCK_BYTES",
    "POLY",
    "REASON_CONTENT",
    "REASON_MAX",
    "REASON_TAIL",
    "StreamFinishedError",
    "WINDOW_BYTES",
    "WINDOW_SIZE",
    "deserialize_context",
    "export_context",
    "parameter_fingerprint",
    "restore_context",
    "serialize_context",
    "split_stream",
]
