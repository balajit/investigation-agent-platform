# src/investigation_agent_platform/infrastructure/persistence/proof_commit_repository.py
"""Test-only persistence for the Review Stage C1+C2 commit path.

New code with no production callers. Every method takes an explicit
RLS-scoped session owned by the caller (the commit service), so the
check-then-commit sequence runs in ONE transaction. Nothing here is
imported by verify/conclude paths.
"""

from __future__ import annotations

import hashlib
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from investigation_agent_platform.domain.common.exceptions import ConcurrencyError
from investigation_agent_platform.domain.finding.models import conclusion_rewrite_allowed
from investigation_agent_platform.infrastructure.persistence.models import (
    ContradictionORM,
    EvidenceORM,
    FindingORM,
    HypothesisORM,
    InvestigationConclusionORM,
    ProofDerivationORM,
    ProofPublicationCommitORM,
)


async def lock_hypothesis_row(
    session: AsyncSession, tenant_id: str, hypothesis_id: UUID
) -> HypothesisORM | None:
    """Load the hypothesis row holding a row lock (serializes commits)."""
    result = await session.execute(
        select(HypothesisORM)
        .where(
            HypothesisORM.id == hypothesis_id,
            HypothesisORM.tenant_id == tenant_id,
        )
        .with_for_update()
    )
    return result.scalars().first()


async def evidence_fingerprint_digest(
    session: AsyncSession, tenant_id: str, investigation_id: UUID
) -> str:
    """sha256 over sorted per-tenant evidence fingerprints (context check)."""
    result = await session.execute(
        select(EvidenceORM.fingerprint).where(
            EvidenceORM.tenant_id == tenant_id,
            EvidenceORM.investigation_id == investigation_id,
        )
    )
    prints = sorted({str(f or "") for f in result.scalars().all() if str(f or "")})
    return "sha256:" + hashlib.sha256("|".join(prints).encode("utf-8")).hexdigest()


async def unresolved_contradiction_count(
    session: AsyncSession, tenant_id: str, investigation_id: UUID
) -> int:
    """Count UNRESOLVED contradictions (reconciliation gate input)."""
    result = await session.execute(
        select(func.count())
        .select_from(ContradictionORM)
        .where(
            ContradictionORM.tenant_id == tenant_id,
            ContradictionORM.investigation_id == investigation_id,
            ContradictionORM.resolution_status == "UNRESOLVED",
        )
    )
    return int(result.scalar() or 0)


async def store_derivation_nodes(
    session: AsyncSession,
    tenant_id: str,
    investigation_id: UUID,
    nodes: list[dict[str, Any]],
) -> int:
    """Insert derivation nodes; existing rows are kept (idempotent)."""
    if not nodes:
        return 0
    stmt = (
        insert(ProofDerivationORM)
        .values(
            [
                {
                    "id": uuid4(),
                    "tenant_id": tenant_id,
                    "investigation_id": investigation_id,
                    "derivation_id": str(n["derivation_id"]),
                    "proposition_id": str(n.get("proposition_id") or ""),
                    "context_id": str(n.get("context_id") or ""),
                    "rule_id": str(n.get("rule_id") or ""),
                    "rule_version": str(n.get("rule_version") or "v1"),
                    "premise_ids": list(n.get("premise_ids") or []),
                    "depends_on": list(n.get("depends_on") or []),
                    "route": str(n.get("route") or "mandatory"),
                }
                for n in nodes
            ]
        )
        .on_conflict_do_nothing(constraint="uq_proof_derivation_tenant_der")
    )
    result = await session.execute(stmt)
    return int(result.rowcount or 0)


async def load_derivation_graph(
    session: AsyncSession, tenant_id: str, investigation_id: UUID
) -> dict[str, dict[str, Any]]:
    """Load stored nodes as the pure composition module's graph mapping."""
    result = await session.execute(
        select(ProofDerivationORM).where(
            ProofDerivationORM.tenant_id == tenant_id,
            ProofDerivationORM.investigation_id == investigation_id,
        )
    )
    graph: dict[str, dict[str, Any]] = {}
    for row in result.scalars().all():
        graph[str(row.derivation_id)] = {
            "derivation_id": str(row.derivation_id),
            "rule_id": str(row.rule_id),
            "premise_ids": list(row.premise_ids or []),
            "depends_on": list(row.depends_on or []),
        }
    return graph


async def find_commit(
    session: AsyncSession, tenant_id: str, derivation_id: str, gate_policy_version: str
) -> ProofPublicationCommitORM | None:
    """Existing commit for this derivation + policy version, if any."""
    result = await session.execute(
        select(ProofPublicationCommitORM).where(
            ProofPublicationCommitORM.tenant_id == tenant_id,
            ProofPublicationCommitORM.derivation_id == derivation_id,
            ProofPublicationCommitORM.gate_policy_version == gate_policy_version,
        )
    )
    return result.scalars().first()


async def insert_commit(
    session: AsyncSession,
    tenant_id: str,
    investigation_id: UUID,
    derivation_id: str,
    context_id: str,
    gate_policy_version: str,
    reconciliation_state: str,
) -> ProofPublicationCommitORM:
    """Insert the commit row; unique violation surfaces as IntegrityError."""
    row = ProofPublicationCommitORM(
        tenant_id=tenant_id,
        investigation_id=investigation_id,
        derivation_id=derivation_id,
        context_id=context_id,
        gate_policy_version=gate_policy_version,
        reconciliation_state=reconciliation_state,
        decision="COMMIT",
    )
    session.add(row)
    await session.flush()
    return row


async def advance_hypothesis_guarded(
    session: AsyncSession,
    tenant_id: str,
    hypothesis_id: UUID,
    expected_version: int,
    new_status: str,
) -> bool:
    """Version-guarded status advance; False when the row moved underneath."""
    result = await session.execute(
        update(HypothesisORM)
        .where(
            HypothesisORM.id == hypothesis_id,
            HypothesisORM.tenant_id == tenant_id,
            HypothesisORM.version == expected_version,
        )
        .values(status=new_status, version=expected_version + 1)
    )
    return int(result.rowcount or 0) == 1


async def insert_finding_record(
    session: AsyncSession, tenant_id: str, investigation_id: UUID, finding: dict[str, Any]
) -> UUID:
    """Finding projection (field mapping mirrors FindingRepository)."""
    row = FindingORM(
        id=uuid4(),
        tenant_id=tenant_id,
        investigation_id=investigation_id,
        finding_type=str(finding.get("finding_type") or "ROOT_CAUSE"),
        title=str(finding.get("title") or ""),
        statement=str(finding.get("statement") or ""),
        severity=str(finding.get("severity") or "MEDIUM"),
        confidence=float(finding.get("confidence") or 0.0),
        evidence_ids=[str(e) for e in (finding.get("evidence_ids") or [])],
        related_hypothesis_ids=[str(h) for h in (finding.get("related_hypothesis_ids") or [])],
        causal_chain=[str(c) for c in (finding.get("causal_chain") or [])],
        remediation_steps=list(finding.get("remediation_steps") or []),
    )
    session.add(row)
    await session.flush()
    return row.id


async def insert_conclusion_record(
    session: AsyncSession,
    tenant_id: str,
    investigation_id: UUID,
    conclusion: dict[str, Any],
) -> UUID:
    """Conclusion projection without last-write-wins.

    Field mapping mirrors FindingRepository.save_conclusion. Repeat commits
    for one investigation refresh the row only under
    ``conclusion_rewrite_allowed`` (same verdict, or forward from a
    non-verdict); overwriting a terminal verdict with a different status
    raises ConcurrencyError instead of silently winning, while findings
    append per commit as the projection audit trail. Read and write share
    the caller's transaction (FOR UPDATE).
    """
    new_status = str(conclusion.get("status") or "")
    current = (
        await session.execute(
            select(InvestigationConclusionORM.status)
            .where(
                InvestigationConclusionORM.tenant_id == tenant_id,
                InvestigationConclusionORM.investigation_id == investigation_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if not conclusion_rewrite_allowed(current, new_status):
        raise ConcurrencyError(
            f"Conclusion verdict {current} already recorded; "
            f"refusing overwrite with {new_status}"
        )
    values = {
        "tenant_id": tenant_id,
        "investigation_id": investigation_id,
        "status": new_status,
        "root_cause": str(conclusion.get("root_cause") or ""),
        "confidence": float(conclusion.get("confidence") or 0.0),
        "supporting_evidence_ids": [
            str(e) for e in (conclusion.get("supporting_evidence_ids") or [])
        ],
        "supporting_hypothesis_ids": [
            str(h) for h in (conclusion.get("supporting_hypothesis_ids") or [])
        ],
        "contradicting_evidence_ids": [
            str(e) for e in (conclusion.get("contradicting_evidence_ids") or [])
        ],
        "limitations": list(conclusion.get("limitations") or []),
        "recommended_next_steps": list(conclusion.get("recommended_next_steps") or []),
        "proven_callers": list(conclusion.get("proven_callers") or []),
        "unresolved_callers": list(conclusion.get("unresolved_callers") or []),
        "caller_coverage": conclusion.get("caller_coverage"),
        "verifier_version": str(conclusion.get("verifier_version") or "v2"),
    }
    if current is None:
        result = await session.execute(
            insert(InvestigationConclusionORM)
            .values(id=uuid4(), **values)
            .returning(InvestigationConclusionORM.id)
        )
        return result.scalar_one()
    result = await session.execute(
        update(InvestigationConclusionORM)
        .where(
            InvestigationConclusionORM.tenant_id == tenant_id,
            InvestigationConclusionORM.investigation_id == investigation_id,
        )
        .values(
            **{
                k: v
                for k, v in values.items()
                if k not in ("tenant_id", "investigation_id")
            }
        )
        .returning(InvestigationConclusionORM.id)
    )
    row_id = result.scalar_one_or_none()
    if row_id is None:
        raise ConcurrencyError("Conclusion row lost under lock")
    return row_id


__all__ = [
    "IntegrityError",
    "advance_hypothesis_guarded",
    "evidence_fingerprint_digest",
    "find_commit",
    "insert_commit",
    "insert_conclusion_record",
    "insert_finding_record",
    "load_derivation_graph",
    "lock_hypothesis_row",
    "store_derivation_nodes",
    "unresolved_contradiction_count",
]
