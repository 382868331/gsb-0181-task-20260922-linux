"""Content-defined chunking (CDC) with a Rabin-type GF(2) fingerprint.

Fingerprint definition (the contract, fixed here before anything else):

* The fingerprint window is the last ``window_size`` (default 32) bytes of the
  stream.  At the start of the stream, when fewer than ``window_size`` bytes
  have been seen, the window is conceptually left-padded with zero bytes.
* The window bytes are interpreted as a polynomial over GF(2), first (oldest)
  byte most significant: byte ``w[0]`` is the coefficient block of
  ``x^(8*(n-1))`` down to ``w[n-1]`` at ``x^0``.
* The fingerprint is the remainder of that polynomial modulo
  ``P(x) = x^16 + x^12 + x^5 + 1`` (degree 16, so the fingerprint is a
  16-bit value).

Cut rule:

* After each byte, let ``L`` be the current block length.
* If ``L >= min_size`` (default 64) and ``fingerprint & mask == 0``
  (default mask ``0x3F``, i.e. the low 6 bits are zero), cut with reason
  ``"fingerprint"``.
* Otherwise, if ``L >= max_size`` (default 256), cut with reason
  ``"max_size"`` (hard cut).
* ``finish()`` emits the remaining bytes as a final block with reason
  ``"end_of_stream"`` if and only if it is non-empty.

The fingerprint window is *not* reset at block boundaries; it always slides
over the whole stream.  The rolling update uses precomputed tables
(``_mod_table`` / ``_out_table``) so each byte costs O(1); the whole window
is never re-scanned per position.

Error semantics:

* Invalid parameters raise :class:`InvalidParameterError` (a ``ValueError``).
  Integer parameters reject ``bool``; floats (including NaN/Infinity) are
  rejected outright.
* Non-bytes-like chunks raise :class:`InvalidChunkTypeError` (a
  ``TypeError``) that names the offending chunk index.
* Total input beyond ``MAX_INPUT_BYTES`` (1 MiB) raises
  :class:`InputTooLargeError`.  The check happens *before* any byte of the
  offending ``update()`` is consumed, so a failed call leaves the chunker
  state untouched (documented atomicity, no silent partial mutation).
* Using a chunker after ``finish()`` raises :class:`StreamClosedError`.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass

# P(x) = x^16 + x^12 + x^5 + 1, bit i set <=> coefficient of x^i is 1.
POLYNOMIAL = (1 << 16) | (1 << 12) | (1 << 5) | 1
POLY_DEGREE = 16
FINGERPRINT_MODULUS = 1 << POLY_DEGREE  # fingerprints are in [0, 2^16)

DEFAULT_WINDOW_SIZE = 32
DEFAULT_MIN_SIZE = 64
DEFAULT_MAX_SIZE = 256
DEFAULT_MASK = 0x3F  # low 6 bits

MAX_INPUT_BYTES = 1 << 20  # 1 MiB hard input limit per stream

REASON_FINGERPRINT = "fingerprint"
REASON_MAX_SIZE = "max_size"
REASON_END_OF_STREAM = "end_of_stream"
REASONS = frozenset({REASON_FINGERPRINT, REASON_MAX_SIZE, REASON_END_OF_STREAM})

_BYTES_LIKE = (bytes, bytearray, memoryview)


class CDCError(Exception):
    """Base class for all errors raised by this package."""


class InvalidParameterError(CDCError, ValueError):
    """A constructor/parameter value violates the contract."""


class InvalidChunkTypeError(CDCError, TypeError):
    """A chunk (or the chunks iterable itself) is not bytes-like."""


class InputTooLargeError(CDCError, ValueError):
    """The stream would exceed MAX_INPUT_BYTES (1 MiB)."""


class StreamClosedError(CDCError, RuntimeError):
    """The chunker was used after finish()."""


@dataclass(frozen=True)
class Block:
    """One emitted chunk.

    ``start``/``end`` are absolute byte offsets into the logical stream
    (``end`` exclusive); ``size == end - start``; ``sha256`` is the hex
    digest of the block's bytes; ``reason`` is one of ``REASON_FINGERPRINT``,
    ``REASON_MAX_SIZE`` or ``REASON_END_OF_STREAM``.
    """

    start: int
    end: int
    size: int
    sha256: str
    reason: str


def _validate_int(name: str, value: object, lo: int, hi: int) -> int:
    """Validate an integer parameter; reject bool, NaN/Infinity, non-int."""
    if isinstance(value, bool):
        raise InvalidParameterError(
            f"{name} must be an int, not bool (got {value!r})"
        )
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise InvalidParameterError(
                f"{name} must be a finite integer, got {value!r}"
            )
        raise InvalidParameterError(
            f"{name} must be an int, got float {value!r}"
        )
    if not isinstance(value, int):
        raise InvalidParameterError(
            f"{name} must be an int, got {type(value).__name__} {value!r}"
        )
    if not lo <= value <= hi:
        raise InvalidParameterError(
            f"{name} must be in [{lo}, {hi}], got {value}"
        )
    return value


def _reduce_byte_shift(value: int) -> int:
    """Return (value << 8) mod P for value < 2^16, by direct long division."""
    r = value << 8
    for bit in range(r.bit_length() - 1, POLY_DEGREE - 1, -1):
        if (r >> bit) & 1:
            r ^= POLYNOMIAL << (bit - POLY_DEGREE)
    return r


def _build_tables(window_size: int) -> tuple[list[int], list[int]]:
    """Build the rolling-update tables.

    ``mod_table[b]  = (b << 16) mod P`` — reduction of the byte shifted out
    of the 16-bit fingerprint when the fingerprint is multiplied by x^8.

    ``out_table[b]  = (b * x^(8*window_size)) mod P`` — contribution to
    remove when byte ``b`` leaves the oldest window position.
    """
    mod_table = [_reduce_byte_shift(b << 8) for b in range(256)]
    out_table = []
    for b in range(256):
        r = b
        for _ in range(window_size):
            r = ((r & 0xFF) << 8) ^ mod_table[r >> 8]
        out_table.append(r)
    return mod_table, out_table


class Chunker:
    """Stateful content-defined chunker; see module docstring for the contract.

    Not thread-safe.  After :meth:`finish` the instance is closed.
    """

    def __init__(
        self,
        min_size: int = DEFAULT_MIN_SIZE,
        max_size: int = DEFAULT_MAX_SIZE,
        window_size: int = DEFAULT_WINDOW_SIZE,
        mask: int = DEFAULT_MASK,
    ) -> None:
        self._min_size = _validate_int("min_size", min_size, 1, MAX_INPUT_BYTES)
        self._max_size = _validate_int("max_size", max_size, 1, MAX_INPUT_BYTES)
        if self._min_size > self._max_size:
            raise InvalidParameterError(
                f"min_size ({self._min_size}) must be <= max_size ({self._max_size})"
            )
        self._window_size = _validate_int("window_size", window_size, 1, 1 << 16)
        self._mask = _validate_int("mask", mask, 0, FINGERPRINT_MODULUS - 1)

        self._mod_table, self._out_table = _build_tables(self._window_size)
        # Ring buffer holding the last window_size bytes, zero-initialised so
        # the stream-start prefix behaves as zero padding.
        self._window = bytearray(self._window_size)
        self._wpos = 0
        self._fp = 0
        self._pos = 0          # absolute stream position (bytes consumed)
        self._block_start = 0  # absolute start of the current open block
        self._buf = bytearray()  # bytes of the current open block (<= max_size)
        self._closed = False

    @property
    def fingerprint(self) -> int:
        """Current 16-bit window fingerprint (diagnostics/testing aid)."""
        return self._fp

    @property
    def bytes_consumed(self) -> int:
        """Total bytes accepted so far."""
        return self._pos

    @property
    def closed(self) -> bool:
        return self._closed

    def update(self, data: bytes | bytearray | memoryview) -> list[Block]:
        """Feed one chunk; return blocks completed by these bytes.

        On any error the chunker state is left exactly as before the call.
        """
        if self._closed:
            raise StreamClosedError(
                "update() called after finish(); the stream is closed"
            )
        if not isinstance(data, _BYTES_LIKE):
            raise InvalidChunkTypeError(
                f"chunk must be bytes-like, got {type(data).__name__}"
            )
        data = bytes(data)
        if self._pos + len(data) > MAX_INPUT_BYTES:
            raise InputTooLargeError(
                f"stream would exceed the {MAX_INPUT_BYTES}-byte (1 MiB) limit "
                f"({self._pos} already consumed, {len(data)} more given); "
                "no bytes from this call were consumed"
            )
        blocks: list[Block] = []
        for byte in data:
            self._feed(byte, blocks)
        return blocks

    def _feed(self, byte: int, blocks: list[Block]) -> None:
        out = self._window[self._wpos]
        self._window[self._wpos] = byte
        self._wpos += 1
        if self._wpos == self._window_size:
            self._wpos = 0
        fp = self._fp
        fp = ((fp & 0xFF) << 8) ^ self._mod_table[fp >> 8] ^ self._out_table[out] ^ byte
        self._fp = fp
        self._buf.append(byte)
        self._pos += 1
        block_len = self._pos - self._block_start
        if block_len >= self._min_size and (fp & self._mask) == 0:
            blocks.append(self._emit(REASON_FINGERPRINT))
        elif block_len >= self._max_size:
            blocks.append(self._emit(REASON_MAX_SIZE))

    def _emit(self, reason: str) -> Block:
        end = self._pos
        block = Block(
            start=self._block_start,
            end=end,
            size=end - self._block_start,
            sha256=hashlib.sha256(bytes(self._buf)).hexdigest(),
            reason=reason,
        )
        self._block_start = end
        self._buf.clear()
        return block

    def finish(self) -> list[Block]:
        """Close the stream; return the tail block iff it is non-empty."""
        if self._closed:
            raise StreamClosedError("finish() called twice")
        self._closed = True
        if self._pos > self._block_start:
            return [self._emit(REASON_END_OF_STREAM)]
        return []


def split_stream(
    chunks,
    *,
    min_size: int = DEFAULT_MIN_SIZE,
    max_size: int = DEFAULT_MAX_SIZE,
    window_size: int = DEFAULT_WINDOW_SIZE,
    mask: int = DEFAULT_MASK,
) -> list[Block]:
    """Chunk a stream given as an iterable of bytes-like chunks.

    A single bytes-like object is accepted as a one-chunk stream.  The result
    depends only on the concatenated bytes, never on how the input was split
    into chunks.  Any invalid input raises before/while building the result;
    since the list is returned only on success, a failed call yields no
    partial result.
    """
    chunker = Chunker(
        min_size=min_size, max_size=max_size, window_size=window_size, mask=mask
    )
    if isinstance(chunks, _BYTES_LIKE):
        iterable = (chunks,)
    else:
        try:
            iterable = iter(chunks)
        except TypeError:
            raise InvalidChunkTypeError(
                f"chunks must be an iterable of bytes-like objects, "
                f"got {type(chunks).__name__}"
            ) from None
    blocks: list[Block] = []
    for index, chunk in enumerate(iterable):
        if not isinstance(chunk, _BYTES_LIKE):
            raise InvalidChunkTypeError(
                f"chunk {index}: expected bytes-like, got {type(chunk).__name__}"
            )
        blocks.extend(chunker.update(chunk))
    blocks.extend(chunker.finish())
    return blocks
