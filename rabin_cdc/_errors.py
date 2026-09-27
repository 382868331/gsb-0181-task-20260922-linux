"""Public exceptions for the :mod:`rabin_cdc` package.

All errors raised by the library derive from :class:`CDCError` so callers can
catch the whole family with a single ``except``.  Type problems additionally
derive from :class:`TypeError`, value/range problems from :class:`ValueError`.
"""

from __future__ import annotations

__all__ = [
    "CDCError",
    "BlockVerificationError",
    "ConfigurationError",
    "InvalidBatchTypeError",
    "InputTooLargeError",
    "MissingBlockDataError",
    "StreamFinishedError",
]


class CDCError(Exception):
    """Base class for every error raised by :mod:`rabin_cdc`."""


class ConfigurationError(CDCError, ValueError):
    """A chunker parameter has an illegal type or range."""


class InvalidBatchTypeError(CDCError, TypeError):
    """A batch handed to ``add`` / ``split_stream`` is not bytes-like."""


class InputTooLargeError(CDCError, ValueError):
    """The cumulative stream would exceed :data:`MAX_TOTAL_BYTES`."""


class StreamFinishedError(CDCError, ValueError):
    """The stream was used after ``finish()`` ended it."""


class MissingBlockDataError(CDCError, ValueError):
    """Reconstruction needs a source reader or new-block payload that was
    not supplied."""


class BlockVerificationError(CDCError, ValueError):
    """Material fetched during reconstruction does not match the hash or
    size the plan refers to and must be rejected."""
