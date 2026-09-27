"""Multi-source rebuild planning on top of content-defined chunking.

New content is chunked with :func:`split_stream`; every target block whose
SHA-256 already exists in one or more *sources* (old contents) can be reused
instead of stored/transmitted again.  Each source has its own read cost:

* ``open_cost``  -- fixed cost paid once if *any* block is read from it;
* ``byte_cost``  -- extra cost per reused byte read from it.

The planner picks, for every reusable target block, exactly one source and
records the choice explicitly in the plan.  Because opening a source has a
fixed cost, the planner avoids reading an expensive source for only a few
bytes: it minimises ``sum(open_cost of used sources) + sum(byte_cost *
reused bytes per source)`` with a deterministic greedy + local-search
heuristic (documented, not a proof of optimality).

If a source turns out to be missing at rebuild time, :func:`rebuild` raises
:class:`MissingSourceError` and the caller may re-plan without that source
(:func:`plan_rebuild` accepts any source subset).  During rebuild every
reused slice is hashed and compared with the plan reference; material that
does not match its reference is rejected with :class:`ContentMismatchError`.
The rebuilt byte string is verified against the plan's target SHA-256.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Callable, Iterable, Optional, Sequence, Tuple, Union

from ._errors import CDCError
from .chunker import BytesLike, split_stream

__all__ = [
    "Source",
    "PlanEntry",
    "RebuildPlan",
    "RebuildResult",
    "RebuildError",
    "InvalidSourceError",
    "MissingSourceError",
    "ContentMismatchError",
    "TargetMismatchError",
    "plan_rebuild",
    "rebuild",
]

KIND_REUSE = "reuse"
KIND_LITERAL = "literal"


class RebuildError(CDCError):
    """Base class for rebuild planning / execution failures."""


class InvalidSourceError(RebuildError, TypeError):
    """A source description has an illegal type or value."""


class MissingSourceError(RebuildError):
    """The reader could not supply a source the plan depends on."""

    def __init__(self, source: str):
        self.source = source
        super().__init__(f"source {source!r} is missing; re-plan without it")


class ContentMismatchError(RebuildError):
    """Material read from a source does not match the planned reference."""


class TargetMismatchError(RebuildError):
    """The rebuilt byte string does not match the plan's target hash."""


def _check_cost(value: object, field: str, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidSourceError(
            f"source {name!r}: {field} must be an int, got "
            f"{type(value).__name__}"
        )
    if value < 0:
        raise InvalidSourceError(
            f"source {name!r}: {field} must be >= 0, got {value}"
        )
    return value


@dataclass(frozen=True)
class Source:
    """An old content that new blocks may be reused from.

    ``open_cost`` is paid once when at least one block is read from this
    source; ``byte_cost`` is paid per reused byte.  Both must be non-negative
    ints (``bool`` and floats -- including NaN/Infinity -- are rejected).
    """

    name: str
    data: bytes
    open_cost: int = 1
    byte_cost: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise InvalidSourceError(
                f"source name must be a non-empty str, got {self.name!r}"
            )
        if not isinstance(self.data, (bytes, bytearray, memoryview)):
            raise InvalidSourceError(
                f"source {self.name!r}: data must be bytes-like, got "
                f"{type(self.data).__name__}"
            )
        object.__setattr__(self, "data", bytes(self.data))
        _check_cost(self.open_cost, "open_cost", self.name)
        _check_cost(self.byte_cost, "byte_cost", self.name)


@dataclass(frozen=True)
class PlanEntry:
    """One planned step, in target order.

    * ``kind == "reuse"``: copy ``size`` bytes from ``source`` at
      ``source_offset``; the slice must hash to ``sha256``.
    * ``kind == "literal"``: new data, carried inline in ``literal``.
    """

    kind: str
    start: int
    end: int
    size: int
    sha256: str
    source: Optional[str] = None
    source_offset: Optional[int] = None
    literal: Optional[bytes] = None


@dataclass(frozen=True)
class RebuildPlan:
    """A complete, source-explicit rebuild plan for one target."""

    entries: Tuple[PlanEntry, ...]
    target_size: int
    target_sha256: str
    estimated_cost: int
    sources_used: Tuple[str, ...]

    @property
    def reused_bytes(self) -> int:
        return sum(e.size for e in self.entries if e.kind == KIND_REUSE)

    @property
    def literal_bytes(self) -> int:
        return sum(e.size for e in self.entries if e.kind == KIND_LITERAL)


@dataclass(frozen=True)
class RebuildResult:
    """Outcome of executing a plan: data plus reuse accounting."""

    data: bytes
    reused_bytes: int
    literal_bytes: int
    sources_used: Tuple[str, ...]
    blocks_reused: int
    blocks_literal: int


def _index_sources(
    sources: Sequence[Source],
) -> dict:
    """Map block SHA-256 -> list of (source index, offset) in each source."""
    index: dict = {}
    for pos, source in enumerate(sources):
        for block in split_stream([source.data]):
            index.setdefault(block.sha256, []).append((pos, block.start))
    return index


def _set_cost(
    chosen: frozenset,
    candidates: list,
    sizes: list,
    sources: Sequence[Source],
    literal_byte_cost: int,
) -> int:
    """Total cost when exactly the sources in ``chosen`` are opened."""
    cost = sum(sources[i].open_cost for i in chosen)
    for cand, size in zip(candidates, sizes):
        best = literal_byte_cost
        for idx in cand:
            if idx in chosen and sources[idx].byte_cost < best:
                best = sources[idx].byte_cost
        cost += best * size
    return cost


def _choose_sources(
    candidates: list,
    sizes: list,
    sources: Sequence[Source],
    literal_byte_cost: int,
) -> list:
    """Pick one source index (or None for literal) per target block.

    Deterministic greedy over source sets: start with no source open,
    repeatedly open the source whose fixed ``open_cost`` is outweighed by
    the per-byte savings it enables, then try dropping each opened source
    once.  This keeps an expensive source closed when it would only serve a
    few bytes.  It is a documented heuristic, not a proof of optimality;
    ties break towards the lowest source index for stable output.
    """
    chosen: frozenset = frozenset()
    best = _set_cost(chosen, candidates, sizes, sources, literal_byte_cost)
    improved = True
    while improved:
        improved = False
        for idx in range(len(sources)):
            if idx in chosen:
                continue
            trial = chosen | {idx}
            cost = _set_cost(trial, candidates, sizes, sources,
                             literal_byte_cost)
            if cost < best:
                chosen, best = trial, cost
                improved = True
                break
    for idx in sorted(chosen):
        trial = chosen - {idx}
        cost = _set_cost(trial, candidates, sizes, sources,
                         literal_byte_cost)
        if cost <= best:
            chosen, best = trial, cost

    assignment = []
    for cand in candidates:
        pick = None
        pick_cost = literal_byte_cost
        for idx in cand:
            if idx in chosen and sources[idx].byte_cost < pick_cost:
                pick, pick_cost = idx, sources[idx].byte_cost
        assignment.append(pick)
    return assignment


def plan_rebuild(
    target: BytesLike,
    sources: Iterable[Source],
    literal_byte_cost: int = 1,
) -> RebuildPlan:
    """Plan how to rebuild ``target`` reusing blocks from ``sources``.

    Every reusable target block is assigned to exactly one named source;
    blocks found nowhere become inline literals.  Sources that are missing
    at rebuild time can simply be dropped from ``sources`` and this function
    called again to re-plan.

    ``literal_byte_cost`` is the per-byte cost of introducing new (non
    reused) data; a block is only reused when reading it is cheaper.  It
    must be a positive int (``bool``/floats are rejected).
    """
    if not isinstance(target, (bytes, bytearray, memoryview)):
        raise InvalidSourceError(
            f"target must be bytes-like, got {type(target).__name__}"
        )
    if (
        isinstance(literal_byte_cost, bool)
        or not isinstance(literal_byte_cost, int)
        or literal_byte_cost <= 0
    ):
        raise InvalidSourceError(
            f"literal_byte_cost must be a positive int, got "
            f"{literal_byte_cost!r}"
        )
    target_bytes = bytes(target)
    source_list = list(sources)
    for pos, source in enumerate(source_list):
        if not isinstance(source, Source):
            raise InvalidSourceError(
                f"sources[{pos}] must be a Source, got "
                f"{type(source).__name__}"
            )
    names = [s.name for s in source_list]
    if len(set(names)) != len(names):
        raise InvalidSourceError(f"duplicate source names: {names!r}")

    target_blocks = split_stream([target_bytes])
    index = _index_sources(source_list)

    candidates = []
    sizes = []
    for block in target_blocks:
        hits = index.get(block.sha256, [])
        # cheapest byte_cost first, then lowest source index: deterministic.
        ordered = sorted(hits, key=lambda h: (source_list[h[0]].byte_cost, h[0]))
        candidates.append([pos for pos, _ in ordered])
        sizes.append(block.size)

    assignment = _choose_sources(
        candidates, sizes, source_list, literal_byte_cost
    )

    entries = []
    used = set()
    cost = 0
    for block, cand, src_idx in zip(target_blocks, candidates, assignment):
        if src_idx is None:
            cost += literal_byte_cost * block.size
            entries.append(
                PlanEntry(
                    kind=KIND_LITERAL,
                    start=block.start,
                    end=block.end,
                    size=block.size,
                    sha256=block.sha256,
                    literal=target_bytes[block.start:block.end],
                )
            )
            continue
        source = source_list[src_idx]
        offset = next(
            off for pos, off in index[block.sha256] if pos == src_idx
        )
        entries.append(
            PlanEntry(
                kind=KIND_REUSE,
                start=block.start,
                end=block.end,
                size=block.size,
                sha256=block.sha256,
                source=source.name,
                source_offset=offset,
            )
        )
        cost += source.byte_cost * block.size
        if src_idx not in used:
            used.add(src_idx)
            cost += source.open_cost

    return RebuildPlan(
        entries=tuple(entries),
        target_size=len(target_bytes),
        target_sha256=hashlib.sha256(target_bytes).hexdigest(),
        estimated_cost=cost,
        sources_used=tuple(sorted(source_list[i].name for i in used)),
    )


def rebuild(
    plan: RebuildPlan,
    reader: Callable[[str], Optional[BytesLike]],
) -> RebuildResult:
    """Execute ``plan``, reading source material through ``reader``.

    ``reader(name)`` must return the source's full bytes, or ``None`` when
    the source is missing (-> :class:`MissingSourceError`; re-plan without
    it).  Every reused slice is hashed and compared with the plan reference;
    mismatched material is rejected with :class:`ContentMismatchError`.  The
    assembled bytes are checked against the plan's target hash.
    """
    if not isinstance(plan, RebuildPlan):
        raise InvalidSourceError(
            f"plan must be a RebuildPlan, got {type(plan).__name__}"
        )
    if not callable(reader):
        raise InvalidSourceError("reader must be callable")

    out = bytearray()
    reused = 0
    literal = 0
    blocks_reused = 0
    blocks_literal = 0
    sources_used = []
    cache: dict = {}

    for entry in plan.entries:
        if entry.kind == KIND_LITERAL:
            if entry.literal is None or len(entry.literal) != entry.size:
                raise ContentMismatchError(
                    f"literal entry at [{entry.start},{entry.end}) is "
                    f"inconsistent with its declared size"
                )
            if hashlib.sha256(entry.literal).hexdigest() != entry.sha256:
                raise ContentMismatchError(
                    f"literal entry at [{entry.start},{entry.end}) does "
                    f"not match its reference hash"
                )
            out += entry.literal
            literal += entry.size
            blocks_literal += 1
            continue
        if entry.kind != KIND_REUSE or entry.source is None:
            raise ContentMismatchError(
                f"plan entry at [{entry.start},{entry.end}) has unknown "
                f"kind {entry.kind!r}"
            )
        if entry.source not in cache:
            material = reader(entry.source)
            if material is None:
                raise MissingSourceError(entry.source)
            if not isinstance(material, (bytes, bytearray, memoryview)):
                raise InvalidSourceError(
                    f"reader for {entry.source!r} returned "
                    f"{type(material).__name__}, expected bytes-like or None"
                )
            cache[entry.source] = bytes(material)
            sources_used.append(entry.source)
        data = cache[entry.source]
        off = entry.source_offset
        chunk = data[off:off + entry.size]
        if len(chunk) != entry.size:
            raise ContentMismatchError(
                f"source {entry.source!r} is too short for slice "
                f"[{off},{off + entry.size})"
            )
        if hashlib.sha256(chunk).hexdigest() != entry.sha256:
            raise ContentMismatchError(
                f"source {entry.source!r} bytes at offset {off} do not "
                f"match planned sha256 {entry.sha256}"
            )
        out += chunk
        reused += entry.size
        blocks_reused += 1

    if len(out) != plan.target_size or (
        hashlib.sha256(bytes(out)).hexdigest() != plan.target_sha256
    ):
        raise TargetMismatchError(
            "rebuilt bytes do not match the plan's target hash"
        )

    return RebuildResult(
        data=bytes(out),
        reused_bytes=reused,
        literal_bytes=literal,
        sources_used=tuple(sources_used),
        blocks_reused=blocks_reused,
        blocks_literal=blocks_literal,
    )
