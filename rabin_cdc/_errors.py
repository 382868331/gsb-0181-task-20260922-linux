"""Public exceptions for the :mod:`rabin_cdc` package.

All errors raised by the library derive from :class:`CDCError` so callers can
catch the whole family with a single ``except``.  Type problems additionally
derive from :class:`TypeError`, value/range problems from :class:`ValueError`.
"""

from __future__ import annotations

__all__ = [
    "CDCError",
    "ConfigurationError",
    "InvalidBatchTypeError",
    "InvalidContextError",
    "InputTooLargeError",
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


class InvalidContextError(CDCError, ValueError):
    """An exported boundary context failed validation on resume.

    Raised for a wrong format version, a parameter-fingerprint mismatch,
    or any inconsistency between the cumulative offset, the rolling
    window, the stored fingerprint and the unclosed block payload.
    """


class StreamFinishedError(CDCError, ValueError):
    """The stream was used after ``finish()`` ended it."""
