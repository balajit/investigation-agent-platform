# src/investigation_agent_platform/application/worker/activities/fulfillment.py
"""Temporal activity thin adapters — each delegates to the application/domain layer via AppContext."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from temporalio import activity

try:
    from temporalio.exceptions import ApplicationFailure  # type: ignore[attr-defined]
except ImportError:
    from temporalio.exceptions import (
        ApplicationError as ApplicationFailure,  # type: ignore[assignment, no-redef]
    )

logger = logging.getLogger(__name__)

from investigation_agent_platform.infrastructure.observability.tracing import traced

from .common import (
    COMP_ACTVTY,
    OUTCOME_COMPLETED,
    OUTCOME_FAILED,
    OUTCOME_STARTED,
    _abandon_reservation,
    _activity_extra,
    _activity_outcome,
    _application_failure_from_exc,
    _coerced_data,
    _complete_reservation,
    _reserve_or_replay,
    elapsed_ms,
    story_log,
    tick,
)
from .inputs import GenericActivityResult, _get_ctx


@dataclass
class RecordInputRequirementInput:
    tenant_id: str
    investigation_id: str
    reason: str = ""
    json_schema: dict[str, Any] = field(default_factory=dict)
    classification: str = "INTERNAL"
    resume_status: str = "INVESTIGATING"
    promote_to_evidence: bool = False
    expires_in_seconds: int | None = None


@dataclass
class SetInputRequirementStateInput:
    tenant_id: str
    requirement_id: str
    expected_version: int
    state: str = "FULFILLED"


@dataclass
class PromoteFulfillmentEvidenceInput:
    tenant_id: str
    investigation_id: str
    requirement_id: str
    data: dict[str, Any] = field(default_factory=dict)
    classification: str = "INTERNAL"


@activity.defn
@traced("worker", outcome_from=_activity_outcome, extra_from=_activity_extra)
async def record_input_requirement_activity(
    params: RecordInputRequirementInput,
) -> GenericActivityResult:
    # TO_DO-011: not idempotency-wired — each call mints a fresh requirement
    # row (new UUID), so no deterministic key is derivable from the inputs; a
    # caller-supplied idempotency key would need repository-level dedup owned
    # by Wave 3. The workflow emits one record call per input marker.
    """Persist one InputRequirement row and return its identity + wait budget."""
    from datetime import timedelta

    from investigation_agent_platform.domain.investigation.input_requirements import (
        AWAITING_INPUT_SOURCES,
        InputRequirement,
    )
    from investigation_agent_platform.domain.investigation.models import InvestigationStatus

    t0 = tick()
    data = _coerced_data(
        RecordInputRequirementInput, params, activity="record_input_requirement_activity"
    )
    tenant_id_probe = str(data.get("tenant_id", ""))
    inv_id_probe = str(data.get("investigation_id", ""))
    story_log(
        logger,
        logging.INFO,
        "record_input_requirement_activity started",
        comp=COMP_ACTVTY,
        func="record_input_requirement_activity",
        stage="execute",
        step="record",
        outcome=OUTCOME_STARTED,
        tenant_id=tenant_id_probe,
        investigation_id=inv_id_probe,
        event="record_input_requirement_activity started",
    )
    try:
        tenant_id = str(data.get("tenant_id", ""))
        inv_id_str = str(data.get("investigation_id", ""))
        reason = str(data.get("reason", "") or "Additional input required")
        if not tenant_id or not inv_id_str:
            return GenericActivityResult(
                success=False, error="tenant_id and investigation_id required"
            )
        investigation_id = UUID(inv_id_str)
        try:
            resume_status = InvestigationStatus(str(data.get("resume_status", "INVESTIGATING")))
        except ValueError:
            resume_status = InvestigationStatus.INVESTIGATING
        if resume_status not in AWAITING_INPUT_SOURCES:
            return GenericActivityResult(
                success=False,
                error=f"resume_status {resume_status.value} cannot suspend for input",
            )
        expires_in = data.get("expires_in_seconds")
        expires_at = None
        wait_timeout_seconds: float | None = None
        if isinstance(expires_in, (int, float)) and expires_in > 0:
            from datetime import UTC, datetime

            expires_at = datetime.now(UTC) + timedelta(seconds=float(expires_in))
            wait_timeout_seconds = float(expires_in)
        requirement = InputRequirement(
            tenant_id=tenant_id,
            investigation_id=investigation_id,
            reason=reason[:512],
            json_schema=dict(data.get("json_schema", {}) or {}),
            classification=str(data.get("classification", "INTERNAL")),
            resume_status=resume_status,
            promote_to_evidence=bool(data.get("promote_to_evidence", False)),
            expires_at=expires_at,
        )
        ctx = _get_ctx()
        await ctx.input_repo.create_requirement(tenant_id, requirement)
        result_data: dict[str, Any] = {
            "requirement_id": str(requirement.requirement_id),
            "requirement_version": requirement.requirement_version,
            "resume_status": resume_status.value,
            "promote_to_evidence": requirement.promote_to_evidence,
        }
        if wait_timeout_seconds is not None:
            result_data["wait_timeout_seconds"] = wait_timeout_seconds
        story_log(
            logger,
            logging.INFO,
            "record_input_requirement_activity completed",
            comp=COMP_ACTVTY,
            func="record_input_requirement_activity",
            stage="execute",
            step="record",
            outcome=OUTCOME_COMPLETED,
            tenant_id=tenant_id,
            investigation_id=inv_id_str,
            event="record_input_requirement_activity completed",
            outputs={
                "outcome": "recorded",
                "requirement_id": str(requirement.requirement_id),
            },
            elapsed_ms=elapsed_ms(t0),
            requirement_id=str(requirement.requirement_id),
        )
        return GenericActivityResult(success=True, data=result_data)
    except ApplicationFailure:
        raise
    except Exception as exc:
        story_log(
            logger,
            logging.ERROR,
            "record_input_requirement_activity failed",
            comp=COMP_ACTVTY,
            func="record_input_requirement_activity",
            stage="execute",
            step="record",
            outcome=OUTCOME_FAILED,
            tenant_id=tenant_id_probe,
            investigation_id=inv_id_probe,
            event="record_input_requirement_activity failed",
            error=str(exc),
            error_type=type(exc).__name__,
            exc_info=True,
        )
        raise _application_failure_from_exc(exc) from exc


@activity.defn
@traced("worker", outcome_from=_activity_outcome, extra_from=_activity_extra)
async def set_input_requirement_state_activity(
    params: SetInputRequirementStateInput,
) -> GenericActivityResult:
    # TO_DO-011: not idempotency-wired — the compare-and-set on
    # (requirement_id, expected_version) IS the idempotency mechanism: a
    # retry with the same version conflicts loudly instead of duplicating.
    """Compare-and-set a requirement's state (FULFILLED/EXPIRED/CANCELLED)."""
    from investigation_agent_platform.domain.investigation.input_requirements import (
        RequirementState,
    )

    t0 = tick()
    data = _coerced_data(
        SetInputRequirementStateInput, params, activity="set_input_requirement_state_activity"
    )
    tenant_id_probe = str(data.get("tenant_id", ""))
    story_log(
        logger,
        logging.INFO,
        "set_input_requirement_state_activity started",
        comp=COMP_ACTVTY,
        func="set_input_requirement_state_activity",
        stage="execute",
        step="set_state",
        outcome=OUTCOME_STARTED,
        tenant_id=tenant_id_probe,
        event="set_input_requirement_state_activity started",
        inputs={"requirement_id": str(data.get("requirement_id", ""))},
        requirement_id=str(data.get("requirement_id", "")),
    )
    try:
        tenant_id = str(data.get("tenant_id", ""))
        req_id_str = str(data.get("requirement_id", ""))
        if not tenant_id or not req_id_str:
            return GenericActivityResult(
                success=False, error="tenant_id and requirement_id required"
            )
        try:
            state = RequirementState(str(data.get("state", "FULFILLED")))
        except ValueError:
            return GenericActivityResult(
                success=False, error=f"unknown requirement state {data.get('state')!r}"
            )
        try:
            expected_version = int(data.get("expected_version", 1))
        except (TypeError, ValueError):
            return GenericActivityResult(success=False, error="expected_version invalid")
        ctx = _get_ctx()
        await ctx.input_repo.set_state(tenant_id, UUID(req_id_str), expected_version, state)
        story_log(
            logger,
            logging.INFO,
            "set_input_requirement_state_activity completed",
            comp=COMP_ACTVTY,
            func="set_input_requirement_state_activity",
            stage="execute",
            step="set_state",
            outcome=OUTCOME_COMPLETED,
            tenant_id=tenant_id,
            event="set_input_requirement_state_activity completed",
            outputs={"outcome": "state_set", "state": state.value},
            elapsed_ms=elapsed_ms(t0),
            requirement_id=req_id_str,
            state=state.value,
        )
        return GenericActivityResult(success=True, data={"state": state.value})
    except ApplicationFailure:
        raise
    except Exception as exc:
        story_log(
            logger,
            logging.ERROR,
            "set_input_requirement_state_activity failed",
            comp=COMP_ACTVTY,
            func="set_input_requirement_state_activity",
            stage="execute",
            step="set_state",
            outcome=OUTCOME_FAILED,
            tenant_id=tenant_id_probe,
            event="set_input_requirement_state_activity failed",
            error=str(exc),
            error_type=type(exc).__name__,
            exc_info=True,
            requirement_id=str(data.get("requirement_id", "")),
        )
        raise _application_failure_from_exc(exc) from exc


@activity.defn
@traced("worker", outcome_from=_activity_outcome, extra_from=_activity_extra)
async def promote_fulfillment_evidence_activity(
    params: PromoteFulfillmentEvidenceInput,
) -> GenericActivityResult:
    """Persist caller-supplied fulfillment data as DOCUMENTATION evidence.

    Only runs when the requirement opted in (``promote_to_evidence``); the
    classification travels from the requirement, and provenance records the
    requirement id so the evidence is traceable back to its source.
    """
    import hashlib
    import json
    from datetime import UTC, datetime

    from investigation_agent_platform.domain.evidence.models import (
        ClassificationLevel,
        Evidence,
        EvidenceType,
    )
    from investigation_agent_platform.domain.provenance.models import (
        EvidenceFreshness,
        EvidenceProvenance,
        QueryFingerprint,
        SourceLocation,
    )

    data = _coerced_data(
        PromoteFulfillmentEvidenceInput,
        params,
        activity="promote_fulfillment_evidence_activity",
    )
    t0 = tick()
    tenant_id_probe = str(data.get("tenant_id", ""))
    inv_id_probe = str(data.get("investigation_id", ""))
    story_log(
        logger,
        logging.INFO,
        "promote_fulfillment_evidence_activity started",
        comp=COMP_ACTVTY,
        func="promote_fulfillment_evidence_activity",
        stage="execute",
        step="promote",
        outcome=OUTCOME_STARTED,
        tenant_id=tenant_id_probe,
        investigation_id=inv_id_probe,
        event="promote_fulfillment_evidence_activity started",
        inputs={"requirement_id": str(data.get("requirement_id", ""))},
        requirement_id=str(data.get("requirement_id", "")),
    )
    try:
        tenant_id = str(data.get("tenant_id", ""))
        inv_id_str = str(data.get("investigation_id", ""))
        req_id_str = str(data.get("requirement_id", ""))
        payload = data.get("data", {}) or {}
        if not tenant_id or not inv_id_str or not req_id_str:
            return GenericActivityResult(
                success=False,
                error="tenant_id, investigation_id and requirement_id required",
            )
        if not isinstance(payload, dict):
            return GenericActivityResult(success=False, error="fulfillment data invalid")
        try:
            classification = ClassificationLevel(str(data.get("classification", "INTERNAL")))
        except ValueError:
            classification = ClassificationLevel.INTERNAL
        investigation_id = UUID(inv_id_str)
        now = datetime.now(UTC)
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        snippet = canonical[:4000]
        evidence = Evidence(
            tenant_id=tenant_id,
            investigation_id=investigation_id,
            evidence_type=EvidenceType.DOCUMENTATION,
            provider="input-fulfillment",
            source=f"input-requirement:{req_id_str}",
            title="Caller-supplied investigation input",
            summary=f"Fulfillment for input requirement {req_id_str}",
            content_snippet=snippet,
            attributes={"requirement_id": req_id_str},
            observed_at=None,
            retrieved_at=now,
            provenance=EvidenceProvenance(
                tenant_id=tenant_id,
                investigation_id=investigation_id,
                provider_type="input-fulfillment",
                requested_provider_id="input-fulfillment",
                actual_provider_id="input-fulfillment",
                source_system="input-requirement",
                retrieval_timestamp=now,
                query_fingerprint=QueryFingerprint(
                    provider_type="input-fulfillment",
                    operation="fulfill",
                    normalized_query_hash=hashlib.sha256(req_id_str.encode()).hexdigest(),
                ),
                source_location=SourceLocation(system="input-requirement", identifier=req_id_str),
            ),
            freshness=EvidenceFreshness(observed_at=None, retrieved_at=now),
            classification=classification,
            fingerprint=hashlib.sha256(
                f"input-fulfillment:{req_id_str}:{canonical}".encode()
            ).hexdigest(),
        )
        ctx = _get_ctx()
        # TO_DO-011: idempotency-wired — the evidence fingerprint is
        # deterministic over (requirement_id, canonical payload), so the
        # requirement id is the deterministic reservation key.
        _idem_active_key: str | None = None
        _idem_operation = "activity:promote_fulfillment"
        _idem_key = f"promote_fulfillment:{req_id_str}"
        _replayed, _idem_active_key = await _reserve_or_replay(
            ctx=ctx,
            tenant_id=tenant_id,
            key=_idem_key,
            request_payload={
                "tenant_id": tenant_id,
                "investigation_id": inv_id_str,
                "requirement_id": req_id_str,
            },
            operation=_idem_operation,
            activity="promote_fulfillment_evidence_activity",
            investigation_id=inv_id_str,
        )
        if _replayed is not None:
            return GenericActivityResult(
                success=bool(_replayed.get("success", True)),
                data=dict(_replayed.get("data") or {}),
                error=_replayed.get("error"),
            )
        await ctx.evidence_repo.save(tenant_id, evidence, investigation_id)
        if _idem_active_key is not None:
            await _complete_reservation(
                ctx=ctx,
                tenant_id=tenant_id,
                key=_idem_active_key,
                operation=_idem_operation,
                response={
                    "success": True,
                    "data": {"evidence_id": str(evidence.evidence_id)},
                },
                activity="promote_fulfillment_evidence_activity",
                investigation_id=inv_id_str,
            )
        story_log(
            logger,
            logging.INFO,
            "promote_fulfillment_evidence_activity completed",
            comp=COMP_ACTVTY,
            func="promote_fulfillment_evidence_activity",
            stage="execute",
            step="promote",
            outcome=OUTCOME_COMPLETED,
            tenant_id=tenant_id,
            investigation_id=inv_id_str,
            event="promote_fulfillment_evidence_activity completed",
            outputs={
                "outcome": "promoted",
                "evidence_id": str(evidence.evidence_id),
            },
            elapsed_ms=elapsed_ms(t0),
            requirement_id=req_id_str,
            evidence_id=str(evidence.evidence_id),
        )
        return GenericActivityResult(success=True, data={"evidence_id": str(evidence.evidence_id)})
    except ApplicationFailure:
        await _abandon_reservation(
            ctx=locals().get("ctx"),
            tenant_id=str(locals().get("tenant_id", "") or ""),
            key=locals().get("_idem_active_key"),
            operation=str(locals().get("_idem_operation", "") or ""),
            activity="promote_fulfillment_evidence_activity",
            investigation_id=str(locals().get("inv_id_str", "") or ""),
        )
        raise
    except Exception as exc:
        await _abandon_reservation(
            ctx=locals().get("ctx"),
            tenant_id=str(locals().get("tenant_id", "") or ""),
            key=locals().get("_idem_active_key"),
            operation=str(locals().get("_idem_operation", "") or ""),
            activity="promote_fulfillment_evidence_activity",
            investigation_id=str(locals().get("inv_id_str", "") or ""),
        )
        story_log(
            logger,
            logging.ERROR,
            "promote_fulfillment_evidence_activity failed",
            comp=COMP_ACTVTY,
            func="promote_fulfillment_evidence_activity",
            stage="execute",
            step="promote",
            outcome=OUTCOME_FAILED,
            tenant_id=tenant_id_probe,
            investigation_id=inv_id_probe,
            event="promote_fulfillment_evidence_activity failed",
            error=str(exc),
            error_type=type(exc).__name__,
            exc_info=True,
        )
        raise _application_failure_from_exc(exc) from exc


# ---------------------------------------------------------------------------
# Part 11.7: finding clustering
# ---------------------------------------------------------------------------


@dataclass
class RunClusteringInput:
    tenant_id: str
    job_id: str = ""
    limit: int = 500
    batch_size: int = 25


@dataclass
class UpdateClusteringJobInput:
    tenant_id: str
    job_id: str
    status: str = ""
    stage: dict[str, Any] | None = None
    error: str | None = None
    progress: int | None = None
    total: int | None = None
