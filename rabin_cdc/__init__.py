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
    StreamFinishedError,
)
from .reconstruct import (
    KIND_NEW,
    KIND_REUSE,
    MAX_SOURCES,
    Plan,
    PlanStep,
    ReconstructionReport,
    Source,
    execute_plan,
    plan_reconstruction,
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
from ._fingerprint import POLY, WINDOW_SIZE
from ._errors import BlockVerificationError, MissingBlockDataError

__all__ = [
    "Block",
    "BlockVerificationError",
    "KIND_NEW",
    "KIND_REUSE",
    "MAX_SOURCES",
    "MissingBlockDataError",
    "Plan",
    "PlanStep",
    "ReconstructionReport",
    "Source",
    "execute_plan",
    "plan_reconstruction",
    "BytesLike",
    "CDCError",
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
    "split_stream",
]
