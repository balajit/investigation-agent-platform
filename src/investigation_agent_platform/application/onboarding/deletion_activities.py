# src/investigation_agent_platform/application/onboarding/deletion_activities.py
"""Phase 5 deletion purge activities and workflow (deterministic, Temporal-native).

The purge workflow is intentionally thin: verify a consumed approval exists,
execute the approved plan entries through store adapters, and finalize the
honest terminal state. All writes are idempotent appends or conditional
transitions -- retries converge instead of duplicating.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID

from temporalio import activity, workflow

with workflow.unsafe.imports_passed_through():
    from investigation_agent_platform.application.onboarding import (
        deletion_activities as _deletion_activities,
    )


def _uuid(value: str | UUID) -> UUID:
    return value if isinstance(value, UUID) else UUID(str(value))


@dataclass(frozen=True)
class PurgeVerifyInput:
    tenant_id: str
    manifest_id: str


@dataclass(frozen=True)
class PurgeVerifyOutcome:
    authorized: bool
    detail: str = ""


@dataclass(frozen=True)
class PurgeExecuteInput:
    tenant_id: str
    manifest_id: str
    attempt: int = 1


@dataclass(frozen=True)
class PurgeExecuteOutcome:
    terminal_state: str
    entries: int = 0
    detail: str = ""


class DeletionActivities:
    """Purge activity implementations; constructed with service access."""

    def __init__(self, cleanup_service: Any) -> None:
        self._cleanup = cleanup_service

    @activity.defn(name="verify_purge_authorized")
    async def verify_purge_authorized(self, inp: PurgeVerifyInput) -> PurgeVerifyOutcome:
        """Proceed only with a consumed approval; fail closed otherwise."""
        try:
            status = await self._cleanup.get_deletion(inp.tenant_id, _uuid(inp.manifest_id))
        except Exception as exc:
            return PurgeVerifyOutcome(authorized=False, detail=str(exc))
        if status["state"] not in ("QUEUED", "IN_PROGRESS"):
            return PurgeVerifyOutcome(
                authorized=False, detail=f"manifest is {status['state']}, not queued"
            )
        return PurgeVerifyOutcome(authorized=True, detail="consumed approval verified")

    @activity.defn(name="execute_purge")
    async def execute_purge(self, inp: PurgeExecuteInput) -> PurgeExecuteOutcome:
        """Execute approved entries idempotently; finalize the terminal state."""
        try:
            result = await self._cleanup.run_purge(
                tenant_id=inp.tenant_id,
                manifest_id=_uuid(inp.manifest_id),
                attempt=inp.attempt,
            )
        except Exception as exc:
            return PurgeExecuteOutcome(terminal_state="INCOMPLETE", detail=str(exc))
        return PurgeExecuteOutcome(
            terminal_state=result["terminal_state"],
            entries=result["entries"],
            detail="purge finalized",
        )


@dataclass
class DeletionPurgeWorkflowInput:
    tenant_id: str
    manifest_id: str
    attempt: int = 1


@dataclass
class DeletionPurgeWorkflowResult:
    status: str
    terminal_state: str = ""
    detail: str = ""


@workflow.defn(name="OnboardingDeletionPurge", sandboxed=False)
class DeletionPurgeWorkflow:
    """One purge run: verify authorization, execute, report the terminal state."""

    @workflow.signal
    def cancel(self) -> None:
        self._cancel_requested = True

    def __init__(self) -> None:
        self._cancel_requested = False

    @workflow.run
    async def run(self, wf_input: DeletionPurgeWorkflowInput) -> DeletionPurgeWorkflowResult:
        verified = await workflow.execute_activity_method(
            _deletion_activities.DeletionActivities.verify_purge_authorized,
            PurgeVerifyInput(tenant_id=wf_input.tenant_id, manifest_id=wf_input.manifest_id),
            start_to_close_timeout=timedelta(minutes=5),
        )
        if not verified.authorized:
            return DeletionPurgeWorkflowResult(status="failed", detail=verified.detail)
        if self._cancel_requested:
            return DeletionPurgeWorkflowResult(status="canceled", detail="cancel signaled")
        outcome = await workflow.execute_activity_method(
            _deletion_activities.DeletionActivities.execute_purge,
            PurgeExecuteInput(
                tenant_id=wf_input.tenant_id,
                manifest_id=wf_input.manifest_id,
                attempt=wf_input.attempt,
            ),
            start_to_close_timeout=timedelta(minutes=30),
        )
        return DeletionPurgeWorkflowResult(
            status="complete",
            terminal_state=outcome.terminal_state,
            detail=outcome.detail,
        )
