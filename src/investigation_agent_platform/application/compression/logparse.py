# src/investigation_agent_platform/application/compression/logparse.py
"""Generic log compression for agentic context (phase 1: exact tier).

App-agnostic by contract: this module knows lines, timestamps, signatures,
and runs — never tenants, investigations, MCP, or resolutions. IAP-specific
concerns (which IDs matter, tenant-scoped resolve, attach policy) live in
the consuming layer and arrive as injected predicates/callables.

Framework notes (recon 2026-10-17, verified against this repo's lockfile):
- Temporal merge across files: ``heapq.merge`` (lazy k-way) — stdlib optimal.
- Masking: single-pass compiled ``re`` alternation. Aho-Corasick
  (``pyahocorasick``) rejected: pattern count is single-digit; regex wins on
  zero-dependency cost at this scale.
- Tabular/groupby engines (``polars``/``duckdb``): rejected until sustained
  windows exceed ~100MB (startup + dependency cost dwarfs gains at KB scale).
  Documented escape hatch, not a TODO.
- Template mining (phase 2): Drain parse-trie (``drain3`` on PyPI, LogPAI
  lineage) is the evaluated fit — O(tokens) streaming, no training corpus.
  Not added as a dependency yet (exact tier first, per plan); the
  ``TemplateMiner`` protocol below mirrors ``drain3``'s surface
  (``add_log_message``/cluster introspection) so a future backend is a
  one-line swap, in-house or vendored.
- Invariants are property-tested with ``hypothesis`` (dev dependency):
  idempotence, count conservation, order preservation, first/last integrity.

Pipeline position: raw logs → THIS MODULE → compacted lines → IAP attach
pass → agent prompt. Output carries its own accounting (``Coverage``) so
callers enforce context budgets against it.
"""

from __future__ import annotations

import heapq
import json
import logging
import re
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
# Long hex runs (correlation/request IDs, hashes) — but not UUIDs (handled first).
HEX_RUN_RE = re.compile(r"\b[0-9a-fA-F]{16,}\b")
# Durations: `12.3ms`, `450 ms`.
DURATION_RE = re.compile(r"\b\d+(?:\.\d+)?\s?ms\b")
# Bare numbers with 4+ digits (counts, ports, byte sizes, epochs). Three-digit
# codes (HTTP 200/404/500, limits) are deliberately PRESERVED — merging across
# statuses would destroy the signal.
BIGNUM_RE = re.compile(r"\b\d{4,}\b")
FLOAT_RE = re.compile(r"\b\d+\.\d+\b")

# Single-pass masking alternation (one scan, leftmost match wins).
_MASK_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (UUID_RE, "<id>"),
    (HEX_RUN_RE, "<corr>"),
    (DURATION_RE, "<dur>"),
    (FLOAT_RE, "<n>"),
    (BIGNUM_RE, "<n>"),
]


def mask_volatiles(text: str) -> str:
    """Replace volatile tokens so repeats share one signature.

    Statuses, small ints, paths, and words survive; IDs, hashes, durations,
    and large numbers collapse. Deterministic and total (never raises).
    """
    try:
        masked = text
        for pattern, token in _MASK_PATTERNS:
            masked = pattern.sub(token, masked)
        return masked
    except Exception as exc:
        logger.warning("masking failed open: %s", exc)
        return text


@dataclass(frozen=True, slots=True)
class ParsedLine:
    """One log line, parsed or raw. Immutable; safe to share across passes."""

    seq: int
    source_file: str
    timestamp: datetime | None
    record: dict[str, Any] | None
    raw: str
    ids: frozenset[str] = frozenset()


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except (ValueError, TypeError, OverflowError):
        return None


def _harvest_ids(text: str) -> frozenset[str]:
    try:
        return frozenset(UUID_RE.findall(text))
    except Exception:
        return frozenset()


def parse_lines(files: Iterable[str]) -> Iterator[ParsedLine]:
    """Stream lines from files in given order. Never raises on bad input.

    JSON lines → structured (timestamp from `timestamp`, message fallback to
    `event`); anything else → raw with UUID harvest. Files are read lazily,
    one at a time — constant memory in file count, linear in lines.
    """
    seq = 0
    for path in files:
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                for raw in handle:
                    text = raw.rstrip("\n")
                    if not text.strip():
                        continue
                    record: dict[str, Any] | None = None
                    timestamp: datetime | None = None
                    try:
                        parsed = json.loads(text)
                        if isinstance(parsed, dict):
                            record = parsed
                            timestamp = _parse_timestamp(parsed.get("timestamp"))
                    except (json.JSONDecodeError, ValueError):
                        record = None
                    yield ParsedLine(
                        seq=seq,
                        source_file=path,
                        timestamp=timestamp,
                        record=record,
                        raw=text,
                        ids=_harvest_ids(text),
                    )
                    seq += 1
        except OSError as exc:
            logger.warning("skipping unreadable log file %s: %s", path, exc)
            continue


def filter_lines(
    lines: Iterable[ParsedLine], predicate: Callable[[ParsedLine], bool]
) -> Iterator[ParsedLine]:
    """Keep lines matching a caller-supplied predicate (e.g. ID containment)."""
    for line in lines:
        try:
            if predicate(line):
                yield line
        except Exception as exc:
            logger.warning("filter predicate failed open: %s", exc)
            continue


def order_temporal(lines: Iterable[ParsedLine]) -> list[ParsedLine]:
    """Stable temporal order; timestamp-less lines trail in arrival order.

    `heapq.merge` territory when inputs arrive pre-sorted per file; the list
    sort here is the general case (interleaved multi-file streams).
    """
    return sorted(
        lines, key=lambda line: (line.timestamp is None, line.timestamp, line.seq)
    )


def signature(line: ParsedLine) -> tuple[str, str, str]:
    """Exact-tier grouping key: (logger, masked message, level).

    Logger scopes the shape (access vs worker vs broker chatter never merge);
    masking (above) collapses volatile tokens; level keeps errors distinct
    from info-shaped twins.
    """
    if line.record is not None:
        logger_name = str(line.record.get("logger", ""))
        message = str(
            line.record.get("message", line.record.get("event", line.raw))
        )
        level = str(line.record.get("level", ""))
    else:
        logger_name, message, level = "", line.raw, ""
    return (logger_name, mask_volatiles(message), level)


@dataclass(frozen=True, slots=True)
class Exemplar:
    """One collapsed run: first/last sighting + occurrence count."""

    signature: tuple[str, str, str]
    count: int
    first: ParsedLine
    last: ParsedLine
    forced_singleton: bool = False


def dedup_collapse(
    lines: Iterable[ParsedLine],
    keep_single: Callable[[ParsedLine], bool] | None = None,
) -> list[Exemplar]:
    """Collapse CONSECUTIVE same-signature runs (phase-1 exact tier).

    `keep_single` forces lines through uncollapsed (errors, transitions,
    verdicts — caller vocabulary). Non-consecutive repeats stay separate in
    this tier (template grouping is phase 2 via `TemplateMiner`). Order of
    first appearance is preserved; idempotent (collapsing exemplars is a
    fixed point — property-tested).
    """
    exemplars: list[Exemplar] = []
    current_key: tuple[str, str, str] | None = None
    current_first: ParsedLine | None = None
    current_last: ParsedLine | None = None
    current_count = 0

    def _flush() -> None:
        nonlocal current_key, current_first, current_last, current_count
        if current_key is not None and current_first is not None and current_last is not None:
            exemplars.append(
                Exemplar(
                    signature=current_key,
                    count=current_count,
                    first=current_first,
                    last=current_last,
                )
            )
        current_key, current_first, current_last, current_count = None, None, None, 0

    for line in lines:
        try:
            forced = bool(keep_single(line)) if keep_single is not None else False
        except Exception as exc:
            logger.warning("keep_single failed open (collapsing): %s", exc)
            forced = False
        if forced:
            _flush()
            exemplars.append(
                Exemplar(
                    signature=signature(line),
                    count=1,
                    first=line,
                    last=line,
                    forced_singleton=True,
                )
            )
            continue
        key = signature(line)
        if key == current_key and current_first is not None:
            current_last = line
            current_count += 1
        else:
            _flush()
            current_key, current_first, current_last, current_count = key, line, line, 1
    _flush()
    return exemplars


@dataclass(frozen=True, slots=True)
class Coverage:
    """Compression accounting for budget enforcement downstream."""

    lines_in: int
    exemplars_out: int
    dropped: int = 0
    first_ts: datetime | None = None
    last_ts: datetime | None = None

    @property
    def ratio(self) -> float:
        return (self.lines_in - self.exemplars_out) / self.lines_in if self.lines_in else 0.0


def coverage(lines_in: int, exemplars: list[Exemplar], dropped: int = 0) -> Coverage:
    """Summarize a compression pass (inputs consumed, outputs kept)."""
    stamps = [
        stamp
        for exemplar in exemplars
        for stamp in (exemplar.first.timestamp, exemplar.last.timestamp)
        if stamp is not None
    ]
    return Coverage(
        lines_in=lines_in,
        exemplars_out=len(exemplars),
        dropped=dropped,
        first_ts=min(stamps) if stamps else None,
        last_ts=max(stamps) if stamps else None,
    )


@runtime_checkable
class TemplateMiner(Protocol):
    """Phase-2 seam: streaming log-template mining (Drain-shaped).

    Mirrors the `drain3` surface so a future backend (vendored minimal Drain
    or the `drain3` package) is a one-line swap behind this protocol:
    feed masked message text, get back a stable cluster id; identical
    templates MUST map to identical ids (clustering is deterministic per
    miner instance). The exact tier above stays the fast path; the miner
    groups NON-CONSECUTIVE repeats the exact tier leaves separate.
    """

    def add_log_message(self, message: str) -> int:
        """Assign `message` to a template cluster; return its stable id."""
        ...

    def template(self, cluster_id: int) -> str | None:
        """Template string for a cluster id, or None if unknown."""
        ...


def merge_ordered(streams: list[Iterable[ParsedLine]]) -> Iterator[ParsedLine]:
    """Lazy k-way temporal merge for per-file pre-sorted streams.

    Each stream must already yield timestamp-ascending lines (timestamp-less
    lines trail per stream). Constant memory in stream count via heapq.
    """
    iterators = [iter(stream) for stream in streams]
    heap: list[tuple[tuple[bool, datetime | None, int], int, ParsedLine]] = []
    for index, iterator in enumerate(iterators):
        try:
            first = next(iterator)
            heapq.heappush(
                heap,
                ((first.timestamp is None, first.timestamp, first.seq), index, first),
            )
        except StopIteration:
            continue
    while heap:
        _, index, line = heapq.heappop(heap)
        yield line
        try:
            nxt = next(iterators[index])
            heapq.heappush(
                heap, ((nxt.timestamp is None, nxt.timestamp, nxt.seq), index, nxt)
            )
        except StopIteration:
            continue


# Re-export-friendly names for the consuming (IAP-side) layer.
__all__ = [
    "Coverage",
    "Exemplar",
    "ParsedLine",
    "TemplateMiner",
    "coverage",
    "dedup_collapse",
    "filter_lines",
    "mask_volatiles",
    "merge_ordered",
    "order_temporal",
    "parse_lines",
    "signature",
]
