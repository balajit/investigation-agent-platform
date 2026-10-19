# src/investigation_agent_platform/domain/analysis/hub_ranking.py
"""Hub ranking pure logic (Acquired Judgement Phase 2, productized PoC).

DB hubs: score = fan_in + 0.5*fan_out + log10(rows+1); link-tables demoted;
edges on empty child tables flagged dormant and excluded from scoring.
Class ranking split output: hubs / vocabulary / communities (never one scale).
Witness merge: DB x code agreement scoring; disagreement reported, not hidden.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass(frozen=True)
class TableStats:
    name: str
    fan_in: int = 0
    fan_out: int = 0
    num_rows: int = 0
    is_link_table: bool = False


@dataclass(frozen=True)
class HubScore:
    name: str
    score: float
    fan_in: int
    fan_out: int
    num_rows: int
    dormant: bool = False


def score_tables(tables: list[TableStats]) -> list[HubScore]:
    """Score tables per PoC formula. Sorted descending; dormant (0 rows with inbound) flagged, unscored-last."""
    scored: list[HubScore] = []
    for t in tables:
        rows = max(0, t.num_rows)
        fan_in = max(0, t.fan_in)
        fan_out = max(0, t.fan_out)
        dormant = rows == 0 and fan_in > 0
        base = fan_in + 0.5 * fan_out + math.log10(rows + 1)
        if t.is_link_table:
            base *= 0.5
        if dormant:
            base = -1.0
        scored.append(HubScore(name=t.name, score=round(base, 4), fan_in=fan_in,
                               fan_out=fan_out, num_rows=rows, dormant=dormant))
    scored.sort(key=lambda h: (-h.score, h.name))
    return scored


@dataclass(frozen=True)
class ClassStats:
    name: str
    import_fan_in: int = 0
    hits_authority: float = 0.0
    entity_package: bool = False
    churn: int = 0
    test_refs: int = 0
    instantiations: int = 0
    is_enum: bool = False
    community: str = ""


@dataclass
class RankedClasses:
    hubs: list[str] = field(default_factory=list)
    vocabulary: list[str] = field(default_factory=list)
    communities: dict[str, list[str]] = field(default_factory=dict)


_GENERIC_STOPLIST = frozenset({"util", "helper", "config", "constants", "base", "abstract"})


def rank_classes(classes: list[ClassStats]) -> RankedClasses:
    """Split-output ranker. Enums/zero-signal types go to vocabulary; the rest ranked to hubs; community map always built."""
    def _signal(c: ClassStats) -> float:
        s = float(c.import_fan_in) + 2.0 * float(c.hits_authority)
        if c.entity_package:
            s += 5.0
        s += min(c.churn, 100) / 20.0
        s += min(c.test_refs, 50) / 10.0
        s += min(c.instantiations, 100) / 25.0
        if c.name.split(".")[-1].lower() in _GENERIC_STOPLIST:
            s *= 0.2
        return s

    out = RankedClasses()
    for c in classes:
        if c.community:
            out.communities.setdefault(c.community, []).append(c.name)
    for members in out.communities.values():
        members.sort()
    for c in classes:
        if c.is_enum or (c.import_fan_in == 0 and c.hits_authority == 0 and not c.entity_package):
            if c.name not in out.vocabulary:
                out.vocabulary.append(c.name)
    ranked = sorted(classes, key=lambda c: (-_signal(c), c.name))
    for c in ranked:
        if c.name in out.vocabulary:
            continue
        out.hubs.append(c.name)
    out.vocabulary.sort()
    return out


def witness_merge(db_hubs: list[str], code_hubs: list[str], top_n: int = 10) -> dict:
    """Agreement across witnesses is the confidence signal; disagreement reported.

    Returns {agreed: [...], db_only: [...], code_only: [...], confidence: float}.
    confidence = |agreed| / |union| over the top_n window.
    """
    db_top = db_hubs[:top_n]
    code_top = code_hubs[:top_n]
    db_set, code_set = set(db_top), set(code_top)
    agreed = sorted(db_set & code_set)
    union = db_set | code_set
    confidence = round(len(agreed) / len(union), 4) if union else 0.0
    return {
        "agreed": agreed,
        "db_only": sorted(db_set - code_set),
        "code_only": sorted(code_set - db_set),
        "confidence": confidence,
    }
