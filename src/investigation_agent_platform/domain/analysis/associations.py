# src/investigation_agent_platform/domain/analysis/associations.py
"""Acquired judgement memory: association records + weights (Phase 3).

Deterministic capture on VERIFIED (zero extra LLM cost); misses recorded;
reinforcement on verify events; janitor prunes below threshold. Per-tenant;
cross-tenant only via fingerprint-proof sharing (caller-enforced).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

MIN_WEIGHT = 0.1
MAX_WEIGHT = 5.0
PRUNE_BELOW = 0.2
VERIFY_REWARD = 0.5
REFUTE_PENALTY = 0.7


@dataclass(frozen=True)
class AssociationRecord:
    tenant_id: str
    symptom_signature: str
    hub_entity: str
    root_cause_statement: str
    fix_locations: tuple[str, ...] = ()
    outcome: str = "VERIFIED"
    weight: float = 1.0


def symptom_signature(symptom: str, hub: str, error_class: str = "") -> str:
    canonical = json.dumps(
        {"s": symptom.strip().lower(), "h": hub.strip().lower(), "e": error_class.strip().lower()},
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"sha256:{hashlib.sha256(canonical.encode()).hexdigest()}"


@dataclass
class AssociationStore:
    """In-memory store; Postgres projection uses (tenant, signature, hub) unique key."""

    records: dict[tuple[str, str, str], AssociationRecord] = field(default_factory=dict)

    def capture(self, rec: AssociationRecord) -> AssociationRecord:
        key = (rec.tenant_id, rec.symptom_signature, rec.hub_entity)
        existing = self.records.get(key)
        if existing is not None:
            merged = AssociationRecord(
                tenant_id=rec.tenant_id,
                symptom_signature=rec.symptom_signature,
                hub_entity=rec.hub_entity,
                root_cause_statement=rec.root_cause_statement,
                fix_locations=rec.fix_locations,
                outcome=rec.outcome,
                weight=round(min(MAX_WEIGHT, existing.weight + 0.25), 4),
            )
            self.records[key] = merged
            return merged
        clamped = AssociationRecord(
            tenant_id=rec.tenant_id,
            symptom_signature=rec.symptom_signature,
            hub_entity=rec.hub_entity,
            root_cause_statement=rec.root_cause_statement,
            fix_locations=tuple(rec.fix_locations),
            outcome=rec.outcome,
            weight=round(max(MIN_WEIGHT, min(MAX_WEIGHT, rec.weight)), 4),
        )
        self.records[key] = clamped
        return clamped

    def reinforce(self, tenant_id: str, signature: str, hub: str, verified: bool) -> float:
        key = (tenant_id, signature, hub)
        rec = self.records.get(key)
        if rec is None:
            return 0.0
        delta = VERIFY_REWARD if verified else -REFUTE_PENALTY
        new_weight = round(max(MIN_WEIGHT, min(MAX_WEIGHT, rec.weight + delta)), 4)
        self.records[key] = AssociationRecord(
            tenant_id=rec.tenant_id,
            symptom_signature=rec.symptom_signature,
            hub_entity=rec.hub_entity,
            root_cause_statement=rec.root_cause_statement,
            fix_locations=rec.fix_locations,
            outcome="VERIFIED" if verified else "REFUTED",
            weight=new_weight,
        )
        return new_weight

    def recall(
        self, tenant_id: str, signature: str, *, threshold: float = 1.0, limit: int = 5
    ) -> list[AssociationRecord]:
        cands = [
            r
            for (t, s, _), r in self.records.items()
            if t == tenant_id and s == signature and r.weight >= threshold
        ]
        cands.sort(key=lambda r: (-r.weight, r.hub_entity))
        return cands[:limit]

    def sweep(self) -> int:
        """Janitor: prune below threshold. Returns pruned count."""
        doomed = [k for k, r in self.records.items() if r.weight < PRUNE_BELOW]
        for k in doomed:
            del self.records[k]
        return len(doomed)


def jump_start_packet(records: list[AssociationRecord]) -> dict:
    """Investigation-start recall packet: ranked associations as orientation (never citations)."""
    return {
        "type": "association_orientation",
        "ranked": [
            {
                "hub": r.hub_entity,
                "cause": r.root_cause_statement,
                "fix_locations": list(r.fix_locations),
                "weight": r.weight,
            }
            for r in sorted(records, key=lambda r: -r.weight)
        ],
    }


def bank_mid_loop_fact(
    statement: str, subject: str, predicate: str, obj: str, provenance: dict
) -> dict:
    """Mid-loop banking: prose-first for the prompt, triple-attached, provenance-bound."""
    statement = statement.strip()
    if not statement:
        raise ValueError("statement must be non-empty")
    return {
        "prose": statement,
        "triple": {"subject": subject, "predicate": predicate, "object": obj},
        "provenance": dict(provenance),
    }
