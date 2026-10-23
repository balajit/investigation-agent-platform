# src/investigation_agent_platform/application/onboarding/activities.py
"""Phase 2 dispatch activities: the only places that touch control state.

Activities are methods on ``OnboardingActivities`` so the Temporal worker can
register a bound instance while unit tests instantiate the same class with
fakes. Workflow code references activity methods directly (typed, refactor
safe); all inputs are Temporal-payload-safe (str/int/bool/None only).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from temporalio import activity

from investigation_agent_platform.application.onboarding.steps import (
    AnalyzeCoverageInput,
)


@dataclass(frozen=True)
class VerifyRevisionInput:
    tenant_id: str
    request_id: str
    revision: int
    execution_generation: int


@dataclass(frozen=True)
class RevisionSnapshot:
    tenant_id: str
    request_id: str
    revision: int
    execution_generation: int
    content_hash: str
    state: str
    proceed: bool
    reason: str


@dataclass(frozen=True)
class HeartbeatInput:
    tenant_id: str
    request_id: str
    revision: int


@dataclass(frozen=True)
class HeartbeatOutcome:
    alive: bool
    cancel_requested: bool
    detail: str = ""


@dataclass(frozen=True)
class CompleteCancellationInput:
    tenant_id: str
    dispatch_id: str
    request_id: str
    revision: int


@dataclass(frozen=True)
class TerminalOutcome:
    terminal: bool
    error_code: str = ""


@dataclass(frozen=True)
class RecordTerminalInput:
    tenant_id: str
    dispatch_id: str
    error_code: str
    fencing_token: int | None = None


def _uuid(value: str | UUID) -> UUID:
    """Coerce activity string IDs to UUIDs. Invalid input fails closed."""
    return value if isinstance(value, UUID) else UUID(str(value))


class OnboardingActivities:
    """Activity implementations; constructed with repository access."""

    def __init__(self, dispatch_repository: Any) -> None:
        self._dispatches = dispatch_repository

    @activity.defn(name="verify_revision")
    async def verify_revision(self, inp: VerifyRevisionInput) -> RevisionSnapshot:
        """Confirm the revision the workflow was started for is dispatchable.

        Pure read plus judgment-free rule: proceed only from QUEUED (or route
        CANCEL_REQUESTED to the cancel path, never to work).
        """
        row = await self._dispatches.read_request_row(
            inp.tenant_id, _uuid(inp.request_id), inp.revision
        )
        if row is None:
            return RevisionSnapshot(
                tenant_id=inp.tenant_id,
                request_id=inp.request_id,
                revision=inp.revision,
                execution_generation=inp.execution_generation,
                content_hash="",
                state="missing",
                proceed=False,
                reason="request revision not found",
            )
        state = str(row.get("state", ""))
        content_hash = str(row.get("content_hash", ""))
        if state == "CANCEL_REQUESTED":
            return RevisionSnapshot(
                tenant_id=inp.tenant_id,
                request_id=inp.request_id,
                revision=inp.revision,
                execution_generation=inp.execution_generation,
                content_hash=content_hash,
                state=state,
                proceed=False,
                reason="cancellation requested",
            )
        if state != "QUEUED":
            return RevisionSnapshot(
                tenant_id=inp.tenant_id,
                request_id=inp.request_id,
                revision=inp.revision,
                execution_generation=inp.execution_generation,
                content_hash=content_hash,
                state=state,
                proceed=False,
                reason=f"request is {state}, not QUEUED",
            )
        return RevisionSnapshot(
            tenant_id=inp.tenant_id,
            request_id=inp.request_id,
            revision=inp.revision,
            execution_generation=inp.execution_generation,
            content_hash=content_hash,
            state=state,
            proceed=True,
            reason="revision verified dispatchable",
        )

    @activity.defn(name="heartbeat")
    async def heartbeat(self, inp: HeartbeatInput) -> HeartbeatOutcome:
        """Cancellation checkpoint and liveness evidence. Read-only by design.

        Heartbeats never mutate dispatch state: the reconciler owns staleness
        decisions from fencing tokens and workflow descriptions, not from a
        heartbeat table that could itself go stale.
        """
        state = await self._dispatches.read_request_state(
            inp.tenant_id, _uuid(inp.request_id), inp.revision
        )
        if state is None:
            return HeartbeatOutcome(
                alive=False, cancel_requested=False, detail="request missing"
            )
        if state in ("CANCEL_REQUESTED", "CANCELED"):
            return HeartbeatOutcome(
                alive=True, cancel_requested=True, detail=f"request is {state}"
            )
        if state != "QUEUED":
            return HeartbeatOutcome(
                alive=False, cancel_requested=False, detail=f"request is {state}"
            )
        return HeartbeatOutcome(
            alive=True, cancel_requested=False, detail="progressing"
        )

    @activity.defn(name="complete_cancellation")
    async def complete_cancellation(
        self, inp: CompleteCancellationInput
    ) -> TerminalOutcome:
        """Idempotent cancellation completion: dispatch TERMINAL, request CANCELED.

        Safe to retry and safe when partially applied: both transitions are
        conditional writes that converge on the same end state.
        """
        dispatch_uuid = _uuid(inp.dispatch_id)
        request_uuid = _uuid(inp.request_id)
        await self._dispatches.mark_terminal(
            inp.tenant_id, dispatch_uuid, "canceled"
        )
        transitioned = await self._dispatches.complete_request_cancellation(
            inp.tenant_id, request_uuid, inp.revision
        )
        if transitioned:
            return TerminalOutcome(terminal=True, error_code="canceled")
        state = await self._dispatches.read_request_state(
            inp.tenant_id, request_uuid, inp.revision
        )
        if state == "CANCELED":
            return TerminalOutcome(terminal=True, error_code="canceled")
        return TerminalOutcome(terminal=False, error_code="cancel-not-applicable")

    @activity.defn(name="record_terminal")
    async def record_terminal(self, inp: RecordTerminalInput) -> TerminalOutcome:
        """Record a terminal outcome with fencing. Stale holders fail closed."""
        if not inp.error_code:
            raise ValueError("error_code must be non-empty")
        marked = await self._dispatches.mark_terminal(
            inp.tenant_id, _uuid(inp.dispatch_id), inp.error_code, inp.fencing_token
        )
        if not marked:
            return TerminalOutcome(terminal=False, error_code="stale-holder")
        return TerminalOutcome(terminal=True, error_code=inp.error_code)

    @activity.defn(name="analyze_coverage")
    async def analyze_coverage(self, inputs: AnalyzeCoverageInput) -> dict[str, Any]:
        """DEP-07 analysis slot: intentionally unimplemented.

        Judgment (LLM reading of evidence) does not exist yet, and an unjudged
        report must never be filed. This stub fails closed so the workflow
        definition can carry the slot — inputs by reference, idempotency key
        (request + revision + generation) — before any model is wired in.
        """
        raise NotImplementedError(
            "coverage analysis judgment is not implemented; deterministic "
            f"assembly only (request {inputs.request_id} rev {inputs.revision} "
            f"gen {inputs.generation})"
        )
