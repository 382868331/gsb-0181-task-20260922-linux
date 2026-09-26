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
    InvalidRecipeInputError,
    RecipeExecutionError,
    RecipeTooLargeError,
    StreamFinishedError,
)
from .recipe import (
    MAX_NEW_BYTES,
    MAX_OLD_TOTAL_BYTES,
    CopyChunk,
    LiteralChunk,
    RecipeOp,
    apply_recipe,
    build_recipe,
    recipe_cost,
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

__all__ = [
    "Block",
    "BytesLike",
    "CDCError",
    "CUT_MASK",
    "Chunker",
    "ConfigurationError",
    "CopyChunk",
    "FINGERPRINT_BITS",
    "InputTooLargeError",
    "InvalidBatchTypeError",
    "InvalidRecipeInputError",
    "LiteralChunk",
    "MAX_BLOCK_BYTES",
    "MAX_NEW_BYTES",
    "MAX_OLD_TOTAL_BYTES",
    "MAX_TOTAL_BYTES",
    "MIN_BLOCK_BYTES",
    "POLY",
    "REASON_CONTENT",
    "REASON_MAX",
    "REASON_TAIL",
    "RecipeExecutionError",
    "RecipeOp",
    "RecipeTooLargeError",
    "StreamFinishedError",
    "WINDOW_BYTES",
    "WINDOW_SIZE",
    "apply_recipe",
    "build_recipe",
    "recipe_cost",
    "split_stream",
]
