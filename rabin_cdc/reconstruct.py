"""Multi-source reconstruction planning for content-defined blocks.

A *target* is the Block list of some new stream.  Its blocks may already
exist, addressed by SHA-256, in one or more old *sources* (previous
archives).  Each source charges a fixed cost once it is read at all plus
a per-byte cost, so reading an expensive source for a handful of bytes is
wasteful.  plan_reconstruction picks, for every target block, either one
source to reuse it from or marks it as new data, minimising the total
read cost.  When a block's hash is available from several sources the
plan records exactly which one was chosen; Plan.replan recomputes the
plan when sources become unavailable.

execute_plan fetches the chosen material, verifies every payload against
the SHA-256 and size the plan refers to (mismatched material is rejected
with BlockVerificationError), assembles the target bytes and reports how
much data was reused, how much is new, and which sources were actually
read.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from typing import Callable, Iterable, Mapping, Optional, Tuple, Union

from ._errors import (
    BlockVerificationError,
    ConfigurationError,
    MissingBlockDataError,
)
from .chunker import Block

__all__ = [
    "KIND_NEW",
    "KIND_REUSE",
    "MAX_SOURCES",
    "Plan",
    "PlanStep",
    "ReconstructionReport",
    "Source",
    "execute_plan",
    "plan_reconstruction",
]

#: Exact cost optimisation enumerates source subsets; keep it bounded.
MAX_SOURCES = 20

#: Step kinds in a Plan.
KIND_REUSE = "reuse"
KIND_NEW = "new"

Cost = Union[int, float]


def _check_cost(value: object, label: str) -> Cost:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigurationError(
            f"{label}: cost must be an int or float, got "
            f"{type(value).__name__}"
        )
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        raise ConfigurationError(f"{label}: cost must be finite, got {value!r}")
    if value < 0:
        raise ConfigurationError(f"{label}: cost must be >= 0, got {value!r}")
    return value


def _check_hashes(hashes: object, label: str) -> frozenset:
    if isinstance(hashes, (bytes, bytearray, memoryview, str)):
        raise ConfigurationError(
            f"{label}: hashes must be an iterable of hex digest strings, "
            f"not a single {type(hashes).__name__}"
        )
    try:
        items = frozenset(hashes)  # type: ignore[arg-type]
    except TypeError as exc:
        raise ConfigurationError(
            f"{label}: hashes must be an iterable of hex digest strings, got "
            f"{type(hashes).__name__}"
        ) from exc
    for digest in items:
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(c not in "0123456789abcdef" for c in digest)
        ):
            raise ConfigurationError(
                f"{label}: expected lowercase 64-char hex SHA-256 digests, "
                f"got {digest!r}"
            )
    return items


@dataclass(frozen=True, slots=True)
class Source:
    """One old archive that may supply blocks.

    Attributes:
        name: Unique, non-empty source identifier.
        fixed_cost: Charged once if any block is read from this source.
        byte_cost: Charged per reused byte read from this source.
        hashes: SHA-256 hex digests of the blocks this source holds.
    """

    name: str
    fixed_cost: Cost
    byte_cost: Cost
    hashes: frozenset

    def __init__(
        self,
        name: str,
        fixed_cost: Cost,
        byte_cost: Cost,
        hashes: Iterable[str],
    ) -> None:
        if not isinstance(name, str) or not name:
            raise ConfigurationError(
                f"source name must be a non-empty str, got {name!r}"
            )
        object.__setattr__(self, "name", name)
        object.__setattr__(
            self, "fixed_cost", _check_cost(fixed_cost, f"source {name!r}")
        )
        object.__setattr__(
            self, "byte_cost", _check_cost(byte_cost, f"source {name!r}")
        )
        object.__setattr__(
            self, "hashes", _check_hashes(hashes, f"source {name!r}")
        )


@dataclass(frozen=True, slots=True)
class PlanStep:
    """One planned step, in target order.

    Attributes:
        block: The target block this step produces.
        kind: KIND_REUSE or KIND_NEW.
        source: Name of the chosen source for reuse steps; None for new
            steps.  When a block exists in several sources this records the
            single one the plan selected.
    """

    block: Block
    kind: str
    source: Optional[str]


@dataclass(frozen=True, slots=True)
class Plan:
    """A cost-minimal reconstruction plan over the available sources."""

    steps: Tuple[PlanStep, ...]
    sources: Tuple[Source, ...]
    total_cost: Cost
    reused_bytes: int
    new_bytes: int

    @property
    def sources_used(self) -> Tuple[str, ...]:
        """Names of sources the plan reads, in first-use order."""
        seen: list[str] = []
        for step in self.steps:
            if step.source is not None and step.source not in seen:
                seen.append(step.source)
        return tuple(seen)

    def replan(self, unavailable: Iterable[str]) -> "Plan":
        """Re-plan without the given sources (e.g. they went offline).

        Blocks that were reused from a missing source are re-assigned to
        the cheapest remaining source holding them, or become new.
        Unknown source names raise ConfigurationError.
        """
        if isinstance(unavailable, str):
            raise ConfigurationError(
                "unavailable must be an iterable of source names, not a "
                "single str"
            )
        try:
            gone = frozenset(unavailable)
        except TypeError as exc:
            raise ConfigurationError(
                "unavailable must be an iterable of source names"
            ) from exc
        known = {s.name for s in self.sources}
        unknown = gone - known
        if unknown:
            raise ConfigurationError(
                f"unknown source name(s): {sorted(unknown)!r}"
            )
        remaining = [s for s in self.sources if s.name not in gone]
        return plan_reconstruction(
            [step.block for step in self.steps], remaining
        )


def _check_blocks(blocks: object) -> list:
    if isinstance(blocks, Block):
        raise ConfigurationError(
            "blocks must be an iterable of Block, not a single Block"
        )
    try:
        items = list(blocks)  # type: ignore[arg-type]
    except TypeError as exc:
        raise ConfigurationError(
            f"blocks must be an iterable of Block, got "
            f"{type(blocks).__name__}"
        ) from exc
    for index, block in enumerate(items):
        if not isinstance(block, Block):
            raise ConfigurationError(
                f"blocks[{index}]: expected Block, got "
                f"{type(block).__name__}"
            )
    return items


def _check_sources(sources: object) -> list:
    if isinstance(sources, Source):
        raise ConfigurationError(
            "sources must be an iterable of Source, not a single Source"
        )
    try:
        items = list(sources)  # type: ignore[arg-type]
    except TypeError as exc:
        raise ConfigurationError(
            f"sources must be an iterable of Source, got "
            f"{type(sources).__name__}"
        ) from exc
    if len(items) > MAX_SOURCES:
        raise ConfigurationError(
            f"at most {MAX_SOURCES} sources are supported, got {len(items)}"
        )
    names: set = set()
    for index, source in enumerate(items):
        if not isinstance(source, Source):
            raise ConfigurationError(
                f"sources[{index}]: expected Source, got "
                f"{type(source).__name__}"
            )
        if source.name in names:
            raise ConfigurationError(f"duplicate source name {source.name!r}")
        names.add(source.name)
    return items


def plan_reconstruction(
    blocks: Iterable[Block],
    sources: Iterable[Source],
    *,
    new_byte_cost: Cost = 1,
) -> Plan:
    """Build a cost-minimal reconstruction plan for blocks.

    Each block whose SHA-256 is present in at least one source becomes a
    reuse step naming the chosen source; every other block becomes a new
    step (payload to be supplied at execution time).  The total cost of a
    set of sources is the sum of their fixed costs plus, per reused block,
    byte_cost * size of the cheapest chosen source holding it; the plan
    minimises this exactly over all source subsets.

    Raises:
        ConfigurationError: malformed blocks/sources (bad types, negative
            or non-finite costs, duplicate names, too many sources).
    """
    block_list = _check_blocks(blocks)
    source_list = _check_sources(sources)
    new_byte_cost = _check_cost(new_byte_cost, "new_byte_cost")
    n = len(source_list)

    # holders[i]: source indices that hold block i.
    holders = [
        [j for j, s in enumerate(source_list) if b.sha256 in s.hashes]
        for b in block_list
    ]

    best_mask = 0
    best_cost: Optional[Cost] = None
    for mask in range(1 << n):
        cost: Cost = 0
        for j, s in enumerate(source_list):
            if mask & (1 << j):
                cost += s.fixed_cost
        for i, block in enumerate(block_list):
            choices = [
                source_list[j].byte_cost
                for j in holders[i]
                if mask & (1 << j)
            ]
            if choices:
                cost += min(choices) * block.size
            else:
                cost += new_byte_cost * block.size
        if best_cost is None or cost < best_cost:
            best_cost = cost
            best_mask = mask

    steps = []
    reused_bytes = 0
    new_bytes = 0
    for i, block in enumerate(block_list):
        choices = [j for j in holders[i] if best_mask & (1 << j)]
        if choices:
            chosen = min(choices, key=lambda j: source_list[j].byte_cost)
            steps.append(
                PlanStep(block, KIND_REUSE, source_list[chosen].name)
            )
            reused_bytes += block.size
        else:
            steps.append(PlanStep(block, KIND_NEW, None))
            new_bytes += block.size

    return Plan(
        steps=tuple(steps),
        sources=tuple(source_list),
        total_cost=best_cost if best_cost is not None else 0,
        reused_bytes=reused_bytes,
        new_bytes=new_bytes,
    )


@dataclass(frozen=True, slots=True)
class ReconstructionReport:
    """Outcome of execute_plan.

    Attributes:
        data: The reconstructed target bytes (blocks concatenated in plan
            order).
        sha256: Lowercase hex SHA-256 of data.
        total_bytes: len(data).
        reused_bytes / new_bytes: Bytes taken from sources / supplied as
            new payloads.
        reused_blocks / new_blocks: Block counts per kind.
        sources_used: Names of sources actually read, in first-use order.
    """

    data: bytes
    sha256: str
    total_bytes: int
    reused_bytes: int
    new_bytes: int
    reused_blocks: int
    new_blocks: int
    sources_used: Tuple[str, ...]


def _verified_payload(payload: object, block: Block, origin: str) -> bytes:
    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise BlockVerificationError(
            f"{origin}: expected a bytes-like payload for block "
            f"[{block.start},{block.end}), got {type(payload).__name__}"
        )
    data = bytes(payload)
    if len(data) != block.size:
        raise BlockVerificationError(
            f"{origin}: block [{block.start},{block.end}) expects "
            f"{block.size} bytes, got {len(data)}"
        )
    digest = hashlib.sha256(data).hexdigest()
    if digest != block.sha256:
        raise BlockVerificationError(
            f"{origin}: block [{block.start},{block.end}) hash mismatch: "
            f"expected {block.sha256}, got {digest}"
        )
    return data


def execute_plan(
    plan: Plan,
    readers: Mapping[str, Callable[[str], object]],
    new_data: Optional[Mapping[str, object]] = None,
) -> ReconstructionReport:
    """Execute plan and return the reconstructed bytes plus statistics.

    Args:
        plan: A plan from plan_reconstruction or Plan.replan.
        readers: Maps a source name to a callable fetching one block payload
            by its SHA-256 hex digest.
        new_data: Maps SHA-256 hex digests of new blocks to their payloads.

    Every fetched payload is checked against the size and SHA-256 the to
    which the plan refers; mismatched material is rejected with
    BlockVerificationError and nothing is returned.  A missing reader or
    missing new payload raises MissingBlockDataError.
    """
    if not isinstance(plan, Plan):
        raise ConfigurationError(
            f"plan must be a Plan, got {type(plan).__name__}"
        )
    if readers is None:
        readers = {}
    if new_data is None:
        new_data = {}

    parts: list = []
    reused_bytes = 0
    new_bytes = 0
    reused_blocks = 0
    new_blocks = 0
    sources_used: list = []

    for step in plan.steps:
        block = step.block
        if step.kind == KIND_REUSE:
            reader = readers.get(step.source)
            if reader is None:
                raise MissingBlockDataError(
                    f"no reader supplied for source {step.source!r} "
                    f"needed by block [{block.start},{block.end})"
                )
            payload = _verified_payload(
                reader(block.sha256), block, f"source {step.source!r}"
            )
            reused_bytes += block.size
            reused_blocks += 1
            if step.source not in sources_used:
                sources_used.append(step.source)
        else:
            if block.sha256 not in new_data:
                raise MissingBlockDataError(
                    f"no new-data payload supplied for block "
                    f"[{block.start},{block.end}) ({block.sha256})"
                )
            payload = _verified_payload(
                new_data[block.sha256], block, "new data"
            )
            new_bytes += block.size
            new_blocks += 1
        parts.append(payload)

    data = b"".join(parts)
    return ReconstructionReport(
        data=data,
        sha256=hashlib.sha256(data).hexdigest(),
        total_bytes=len(data),
        reused_bytes=reused_bytes,
        new_bytes=new_bytes,
        reused_blocks=reused_blocks,
        new_blocks=new_blocks,
        sources_used=tuple(sources_used),
    )
