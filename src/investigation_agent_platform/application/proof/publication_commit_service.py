# src/investigation_agent_platform/application/proof/publication_commit_service.py
"""Review Stage C2: context-bound transactional publication commit.

Test-only service with no production callers: it evaluates, certifies,
checks, and decides each route with the pure kernel, then — inside ONE
transaction — re-validates the live context (hypothesis version,
evidence-set digest, contradiction state), persists derivation nodes,
inserts the commit row, and projects lifecycle (hypothesis status,
finding, conclusion). Any failure denies with a stable code and writes
nothing. Competing commits serialize on the hypothesis row lock plus a
version-guarded status advance.

Stay-non-authoritative guardrail: this module is never imported by
verify/conclude paths; production writers are untouched.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import UUID

from sqlalchemy.exc import IntegrityError

from investigation_agent_platform.domain.proof.certificate import (
    CertificationError,
    CheckerOutcome,
    CheckRequest,
    certify,
    check_certificate,
)
from investigation_agent_platform.domain.proof.composition import (
    compose_alternatives,
    compose_conjunction,
)
from investigation_agent_platform.domain.proof.evaluation import (
    EvaluationInputs,
    evaluate,
)
from investigation_agent_platform.domain.proof.obligations import (
    DecisionPolicy,
    assemble_trusted_context_bytes,
    decide_guarded_promotion,
    encode_evidence_bytes,
)
from investigation_agent_platform.domain.proof.publication import (
    PublicationDecision,
)
from investigation_agent_platform.infrastructure.persistence.proof_commit_repository import (
    advance_hypothesis_guarded,
    evidence_fingerprint_digest,
    find_commit,
    insert_commit,
    insert_conclusion_record,
    insert_finding_record,
    lock_hypothesis_row,
    store_derivation_nodes,
    unresolved_contradiction_count,
)
from investigation_agent_platform.infrastructure.persistence.rls import (
    rls_session,
)

#: Writer routing (R4c decision: single-first). Operator allowlist of
#: publish modes; unset means single-only. Changing this to all/any is
#: an explicit owner decision per mode (recorded in the ledger entry).
_WRITER_ENV_VAR = "IAP_PUBLISH_WRITERS"


def _allowed_modes() -> frozenset[str]:
    raw = str(os.environ.get(_WRITER_ENV_VAR, "") or "")
    if not raw.strip():
        return frozenset({"single"})
    return frozenset(m.strip().lower() for m in raw.split(",") if m.strip())


#: Hypothesis statuses the commit may advance FROM (lifecycle graph).
_COMMITTABLE_FROM = frozenset({"SUPPORTED", "UNDER_INVESTIGATION"})


@dataclass(frozen=True)
class RouteInput:
    """One route to evaluate, certify, and check (pure kernel inputs)."""

    proposition: dict[str, Any]
    manifest: Any
    premises: list[dict[str, Any]] = field(default_factory=list)
    artifacts: dict[str, dict[str, Any]] = field(default_factory=dict)
    rule_bundle_version: str = "rules/v1"
    rule_id: str = "definition/v1"
    rule_version: str = "v1"
    implementation_digest: str = ""
    model_encoding: dict[str, Any] = field(default_factory=dict)
    universe: str | None = None
    route: str = "mandatory"
    depends_on: list[str] = field(default_factory=list)
    # Trusted provenance bindings for the guarded promotion entry: one
    # {evidence_id, content_digest, repository_id} record per premise,
    # supplied by the operator/test boundary (never inferred from the
    # artifact). Empty bindings deny at the policy gate by design.
    repository_bindings: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class CommitExpectation:
    """The context the caller evaluated under (linearization pin)."""

    hypothesis_id: UUID
    hypothesis_version: int
    evidence_digest: str


@dataclass(frozen=True)
class LifecycleProjection:
    """Lifecycle rows written inside the commit transaction on COMMIT."""

    new_status: str = "VERIFIED"
    finding: dict[str, Any] = field(default_factory=dict)
    conclusion: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CommitResult:
    """Outcome of one commit attempt (writes happen iff committed)."""

    committed: bool
    reason: str
    commit_id: str | None = None
    derivation_ids: tuple[str, ...] = ()


class ProofPublicationService:
    """Context-bound commit over an explicit session factory (test-only)."""

    def __init__(self, session_factory: Any) -> None:
        self._factory = session_factory

    async def commit(
        self,
        tenant_id: str,
        investigation_id: UUID,
        expectation: CommitExpectation,
        routes: list[RouteInput],
        mode: Literal["single", "all", "any"] = "single",
        gate_policy_version: str = "gate/v1",
        policy_eligible: bool = True,
        policy_reason: str = "",
        reconciliation_adjudicated: bool = False,
        lifecycle: LifecycleProjection | None = None,
        policy: DecisionPolicy | None = None,
    ) -> CommitResult:
        """Evaluate all routes purely, then commit atomically or deny.

        Promotion of each surviving route goes through the guarded
        obligation entry (policy + evidence + trusted context bytes);
        the scalar linearization step is internal to that entry and is
        never called directly here.
        """
        if mode not in _allowed_modes():
            return CommitResult(False, f"MODE_NOT_ENABLED:{mode}")
        if not routes:
            return CommitResult(False, "NO_ROUTES")
        if mode == "single" and len(routes) != 1:
            return CommitResult(False, "MODE_SINGLE_NEEDS_ONE_ROUTE")
        prepared: list[dict[str, Any]] = []
        try:
            for route in routes:
                inputs = EvaluationInputs(
                    proposition=dict(route.proposition),
                    manifest=route.manifest,
                    rule_bundle_version=route.rule_bundle_version,
                    model_encoding=dict(route.model_encoding or {}),
                    rule_id=route.rule_id,
                    rule_version=route.rule_version,
                    implementation_digest=route.implementation_digest,
                    inference={},
                )
                derivation = evaluate(inputs)
                cert = certify(
                    inputs, list(route.premises or []), dict(route.artifacts or {}), route.universe
                )
                check_request = CheckRequest(
                    certificate=cert,
                    proposition=dict(route.proposition),
                    manifest=route.manifest,
                    rule_bundle_version=route.rule_bundle_version,
                    model_encoding=dict(route.model_encoding or {}),
                    artifacts=dict(route.artifacts or {}),
                    expected_context_id=derivation.context_id,
                )
                outcome, reasons = check_certificate(check_request)
                prepared.append(
                    {
                        "route": route,
                        "derivation": derivation,
                        "certificate": cert,
                        "checker_outcome": outcome,
                        "checker_reasons": reasons,
                        "manifest_dump": route.manifest.model_dump(mode="json"),
                        "artifact_map": {
                            str(k): dict(v) for k, v in (route.artifacts or {}).items()
                        },
                    }
                )
        except (CertificationError, Exception) as exc:
            return CommitResult(False, f"EVALUATION_FAILED:{type(exc).__name__}")
        outcomes = [p["checker_outcome"] for p in prepared]
        if mode in ("single", "all"):
            composed, compose_reason = compose_conjunction(outcomes)
        else:
            composed, compose_reason = compose_alternatives(outcomes)
        if composed != CheckerOutcome.VALID:
            return CommitResult(False, f"COMPOSITION_DENIED:{compose_reason}")
        survivors = [p for p in prepared if p["checker_outcome"] == CheckerOutcome.VALID]
        if not survivors:
            return CommitResult(False, "NO_SURVIVING_ROUTE")
        async with rls_session(self._factory, tenant_id) as session:
            hyp = await lock_hypothesis_row(session, tenant_id, expectation.hypothesis_id)
            if hyp is None:
                return CommitResult(False, "HYPOTHESIS_NOT_FOUND")
            if int(hyp.version) != int(expectation.hypothesis_version):
                return CommitResult(False, "STALE_VERSION")
            current_digest = await evidence_fingerprint_digest(session, tenant_id, investigation_id)
            if current_digest != expectation.evidence_digest:
                return CommitResult(False, "STALE_CONTEXT")
            pending = await unresolved_contradiction_count(session, tenant_id, investigation_id)
            if pending and not reconciliation_adjudicated:
                return CommitResult(False, "CONFLICT_UNRESOLVED")
            committed_ids: list[str] = []
            first_commit_id: str | None = None
            fresh: list[dict[str, Any]] = []
            for item in survivors:
                derivation = item["derivation"]
                existing = await find_commit(
                    session, tenant_id, str(derivation.derivation_id), gate_policy_version
                )
                if existing is not None:
                    committed_ids.append(str(derivation.derivation_id))
                    if first_commit_id is None:
                        first_commit_id = str(existing.id)
                else:
                    fresh.append(item)
            if not fresh:
                return CommitResult(
                    True, "ALREADY_COMMITTED", first_commit_id, tuple(committed_ids)
                )
            if lifecycle is not None:
                # Hoisted validation (lifecycle fix 2026-10-26): lifecycle
                # denials must precede ALL writes — previously these checks ran
                # after derivation/commit persistence, leaving rows behind on
                # denial. `hyp` is the row locked above, still held in this
                # transaction, so no re-lock is needed. Placed after the
                # ALREADY_COMMITTED early return so idempotent recommit
                # (lifecycle re-presented on an already-VERIFIED hypothesis)
                # keeps returning ALREADY_COMMITTED.
                if str(lifecycle.new_status) != "VERIFIED":
                    return CommitResult(False, "UNSUPPORTED_PROJECTION_STATUS")
                if str(hyp.status) not in _COMMITTABLE_FROM:
                    return CommitResult(False, "LIFECYCLE_EDGE_DENIED")
            for item in fresh:
                derivation = item["derivation"]
                cert = item["certificate"]
                route = item["route"]
                evidence_bytes = encode_evidence_bytes(
                    {
                        "rule_id": str(route.rule_id),
                        "proposition": dict(route.proposition),
                        "certificate": dict(cert),
                        "manifest": item["manifest_dump"],
                        "artifacts": {str(k): dict(v) for k, v in item["artifact_map"].items()},
                    }
                )
                trusted_bytes = assemble_trusted_context_bytes(
                    rule_bundle_version=str(route.rule_bundle_version),
                    model_encoding=dict(route.model_encoding or {}),
                    reconciliation_state="unreconciled",
                    known_normalization_ops=[],
                    derivation_deps={},
                    repository_bindings=list(route.repository_bindings or ()),
                    expected_context_id=str(derivation.context_id),
                    reconciliation_adjudicated=bool(reconciliation_adjudicated),
                    policy_eligible=bool(policy_eligible),
                    policy_reason=str(policy_reason or ""),
                )
                decision, reason = decide_guarded_promotion(policy, evidence_bytes, trusted_bytes)
                if decision is not PublicationDecision.COMMIT:
                    return CommitResult(False, f"POLICY_DENIED:{reason}")
                await store_derivation_nodes(
                    session,
                    tenant_id,
                    investigation_id,
                    [
                        {
                            "derivation_id": str(derivation.derivation_id),
                            "proposition_id": str(derivation.proposition_id),
                            "context_id": str(derivation.context_id),
                            "rule_id": str(derivation.rule_id),
                            "rule_version": str(derivation.rule_version),
                            "premise_ids": list(derivation.premise_ids),
                            "depends_on": list(item["route"].depends_on),
                            "route": str(item["route"].route),
                        }
                    ],
                )
                try:
                    row = await insert_commit(
                        session,
                        tenant_id,
                        investigation_id,
                        str(derivation.derivation_id),
                        str(derivation.context_id),
                        gate_policy_version,
                        "adjudicated" if reconciliation_adjudicated else "unreconciled",
                    )
                except IntegrityError:
                    await session.rollback()
                    existing = await find_commit(
                        session, tenant_id, str(derivation.derivation_id), gate_policy_version
                    )
                    if existing is None:
                        return CommitResult(False, "COMMIT_RACE_LOST")
                    return CommitResult(
                        True,
                        "ALREADY_COMMITTED",
                        str(existing.id),
                        (str(derivation.derivation_id),),
                    )
                committed_ids.append(str(derivation.derivation_id))
                if first_commit_id is None:
                    first_commit_id = str(row.id)
            if lifecycle is not None:
                # Validation hoisted above (see pre-store gate): only the
                # version-guarded advance and the projections remain here.
                advanced = await advance_hypothesis_guarded(
                    session,
                    tenant_id,
                    expectation.hypothesis_id,
                    expectation.hypothesis_version,
                    "VERIFIED",
                )
                if not advanced:
                    return CommitResult(False, "STALE_VERSION")
                await insert_finding_record(
                    session, tenant_id, investigation_id, dict(lifecycle.finding or {})
                )
                await insert_conclusion_record(
                    session, tenant_id, investigation_id, dict(lifecycle.conclusion or {})
                )
            return CommitResult(True, "COMMITTED", first_commit_id, tuple(committed_ids))
