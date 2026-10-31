"""Cap-verdict evaluator for the pivot rule (F4-D23, pure).

Turns rows of recorded telemetry into a verdict, so the cap decision is a
function with tests, not a judgment call. The evaluator never touches the
database; extraction is a separate, owner-approved read-only step.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Literal

MIN_RUNS = 30
STARVED_SHARE_TO_RAISE = 0.20
MIN_HARMED_CASES = 3
PRODUCTIVITY_FLOOR = 0.5
MIN_PIVOTS_FOR_PRODUCTIVITY = 20


@dataclass(frozen=True)
class RunRow:
    case_id: str
    pivots: int | None  # forced_pivot_count; None when the run predates the counter
    capped: int | None
    scoped_skipped: int | None
    productive: int | None
    passed: bool | None  # battery verdict for the run, when known


@dataclass(frozen=True)
class CapVerdict:
    verdict: Literal["keep", "raise", "lower", "insufficient"]
    reasons: tuple[str, ...]
    stats: dict[str, float]


def review(rows: list[RunRow], cap: int = 2) -> CapVerdict:
    pop = [r for r in rows if None not in (r.pivots, r.capped, r.productive)]
    n = len(pop)
    stats: dict[str, float] = {"runs": float(n)}
    if n < MIN_RUNS:
        return CapVerdict("insufficient", (f"{n} usable runs, need {MIN_RUNS}",), stats)
    pivots = sum(r.pivots or 0 for r in pop)
    productive = sum(r.productive or 0 for r in pop)
    stats["pivots"] = float(pivots)
    if pivots >= MIN_PIVOTS_FOR_PRODUCTIVITY:
        stats["productive_share"] = productive / pivots
        if stats["productive_share"] < PRODUCTIVITY_FLOOR:
            return CapVerdict("lower", (f"productive share {stats['productive_share']:.2f} below {PRODUCTIVITY_FLOOR}",), stats)
    starved = [r for r in pop if (r.pivots or 0) >= cap and (r.capped or 0) >= 1]
    stats["starved_share"] = len(starved) / n
    by_case: dict[str, dict[bool, list[bool]]] = defaultdict(lambda: {True: [], False: []})
    for r in pop:
        if r.passed is not None:
            by_case[r.case_id][r in starved].append(r.passed)
    harmed = sum(
        1 for sides in by_case.values()
        if sides[True] and sides[False] and (sum(sides[False]) / len(sides[False])) > (sum(sides[True]) / len(sides[True]))
    )
    stats["harmed_cases"] = float(harmed)
    if stats["starved_share"] >= STARVED_SHARE_TO_RAISE and harmed >= MIN_HARMED_CASES:
        return CapVerdict("raise", (f"starved {stats['starved_share']:.2f}, harmed cases {harmed}",), stats)
    return CapVerdict("keep", ("no starvation harm and productive pivots",), stats)
