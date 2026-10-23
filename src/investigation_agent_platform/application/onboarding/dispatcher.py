# src/investigation_agent_platform/application/onboarding/dispatcher.py
"""Phase 2 dispatch execution and reconciliation (no Temporal worker loop here).

The dispatcher claims due dispatches and starts one deterministic workflow per
dispatch through the injected ports. The reconciler repairs stuck states and
never starts anything new: there is exactly one starter, so redelivery is
idempotent rather than raced.

Failure model (mirrors the workflow-correctness plan):
- start ok             -> record ACCEPTED + run id (fast path)
- start crashed/lost   -> reconciler adopts via describe (same fencing token)
- AlreadyStarted        -> describe: running adopts, terminal mirrors
- ambiguous timeout     -> describe found adopts; missing retries with backoff
- unreachable           -> release with backoff; attempts bound total retries
- fatal input           -> TERMINAL, no retry
- stale fencing token   -> write refused; reconciler owns recovery
- canceled request      -> no start; cancel the running workflow if any
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from investigation_agent_platform.application.onboarding.dispatch_ports import (
    PinnedWorkflowInput,
    WorkflowLookupPort,
    WorkflowStartPort,
)

MAX_DISPATCH_ATTEMPTS = 5
RETRY_BASE_BACKOFF_SECONDS = 60
RETRY_MAX_BACKOFF_SECONDS = 3600


class DispatchStartError(Exception):
    """Base for workflow-start failures the dispatcher must classify."""


class DispatchAlreadyStartedError(DispatchStartError):
    """A workflow with this deterministic ID already exists."""


class DispatchStartTimeoutError(DispatchStartError):
    """The start timed out without knowledge of the outcome."""


class DispatchUnreachableError(DispatchStartError):
    """The backend could not be reached; definitely not started."""


class DispatchFatalError(DispatchStartError):
    """The start will never succeed (invalid input); do not retry."""


class DispatchOutcome(StrEnum):
    NO_WORK = "no_work"
    STARTED = "started"
    ADOPTED_RUNNING = "adopted_running"
    ADOPTED_COMPLETED = "adopted_completed"
    RETRY_LATER = "retry_later"
    TERMINAL = "terminal"
    STALE = "stale"
    DEFERRED = "deferred"


@dataclass(frozen=True)
class DispatchResult:
    outcome: DispatchOutcome
    dispatch_id: Any = None
    run_id: str | None = None
    detail: str = ""


@dataclass
class ReconcileReport:
    requeued_expired: int = 0
    adopted_running: int = 0
    terminal_mirrored: int = 0
    retry_exhausted: int = 0
    canceled: int = 0
    deferred_cancel: int = 0
    no_op: int = 0
    notes: list[str] = field(default_factory=list)


def retry_backoff_seconds(attempts: int) -> int:
    """Exponential backoff with a cap. Pure: unit-testable without clocks."""
    if attempts < 0:
        raise ValueError("attempts must be >= 0")
    backoff: int = min(
        RETRY_BASE_BACKOFF_SECONDS * (2**attempts), RETRY_MAX_BACKOFF_SECONDS
    )
    return backoff


def terminal_for_mirror(workflow_status: str) -> tuple[bool, str]:
    """Map a described workflow status to (is_terminal, error_code).

    Unknown statuses are never terminal: the reconciler looks again rather
    than inventing an outcome.
    """
    normalized = (workflow_status or "").strip().lower()
    if normalized in ("completed", "succeeded", "success"):
        return True, ""
    if normalized in ("failed", "errored", "terminated", "timed_out", "timeout"):
        return True, f"workflow_{normalized}"
    if normalized in ("canceled", "cancelled"):
        return True, "workflow_canceled"
    return False, ""


class OnboardingDispatcher:
    """Owns workflow starts; reconciles stuck dispatches. Holds no durable state."""

    def __init__(
        self,
        *,
        dispatch_repository: Any,
        starter: WorkflowStartPort,
        lookup: WorkflowLookupPort,
        max_attempts: int = MAX_DISPATCH_ATTEMPTS,
    ) -> None:
        self._dispatches = dispatch_repository
        self._starter = starter
        self._lookup = lookup
        self._max_attempts = max_attempts

    async def dispatch_once(self, *, worker_id: str) -> DispatchResult:
        claim = await self._dispatches.claim_and_reconcile(worker_id=worker_id)
        if claim is None:
            return DispatchResult(DispatchOutcome.NO_WORK)
        return await self._start_claimed(worker_id=worker_id, claim=claim)

    async def _start_claimed(self, *, worker_id: str, claim: Any) -> DispatchResult:
        request_state = await self._dispatches.read_request_state(
            claim.tenant_id, claim.request_id, claim.revision
        )
        if request_state is None:
            await self._dispatches.mark_terminal(
                claim.tenant_id,
                claim.id,
                error_code="request_missing",
                fencing_token=claim.lease_fencing_token,
            )
            return DispatchResult(
                DispatchOutcome.TERMINAL,
                dispatch_id=claim.id,
                detail="request row missing for claimed dispatch",
            )
        if request_state in ("CANCEL_REQUESTED", "CANCELED"):
            await self._dispatches.mark_terminal(
                claim.tenant_id,
                claim.id,
                error_code="request_canceled",
                fencing_token=claim.lease_fencing_token,
            )
            return DispatchResult(
                DispatchOutcome.TERMINAL,
                dispatch_id=claim.id,
                detail="request canceled; not started",
            )
        current = await self._dispatches.read_dispatch(
            claim.tenant_id, claim.id
        )
        attempts = 0 if current is None else int(current.get("attempts", 0))
        if attempts >= self._max_attempts:
            await self._dispatches.mark_terminal(
                claim.tenant_id,
                claim.id,
                error_code="retry_budget_exhausted",
                fencing_token=claim.lease_fencing_token,
            )
            return DispatchResult(
                DispatchOutcome.TERMINAL,
                dispatch_id=claim.id,
                detail="retry budget exhausted",
            )
        workflow_input = PinnedWorkflowInput(
            tenant_id=claim.tenant_id,
            request_id=claim.request_id,
            revision=claim.revision,
            execution_generation=claim.execution_generation,
            workflow_id=claim.workflow_id,
            input_reference=str(claim.request_id),
            input_version=f"rev{claim.revision}g{claim.execution_generation}",
            schema_version="onboarding-dispatch/v1",
            dispatch_id=str(claim.id),
        )
        try:
            receipt = await self._starter.start(workflow_input)
        except DispatchAlreadyStartedError:
            return await self._adopt_existing(claim)
        except DispatchStartTimeoutError:
            return await self._disambiguate(claim)
        except DispatchUnreachableError as exc:
            await self._dispatches.release_for_retry(
                claim.tenant_id,
                claim.id,
                worker_id,
                claim.lease_fencing_token,
                f"unreachable:{exc}",
                retry_backoff_seconds(attempts),
            )
            return DispatchResult(
                DispatchOutcome.RETRY_LATER,
                dispatch_id=claim.id,
                detail=str(exc),
            )
        except DispatchFatalError as exc:
            await self._dispatches.mark_terminal(
                claim.tenant_id,
                claim.id,
                error_code=f"fatal:{exc}",
                fencing_token=claim.lease_fencing_token,
            )
            return DispatchResult(
                DispatchOutcome.TERMINAL, dispatch_id=claim.id, detail=str(exc)
            )
        recorded = await self._dispatches.record_workflow_accepted(
            claim.tenant_id, claim.id, claim.lease_fencing_token, receipt.run_id
        )
        if not recorded:
            return DispatchResult(
                DispatchOutcome.STALE,
                dispatch_id=claim.id,
                detail="fencing token superseded before run id landed",
            )
        return DispatchResult(
            DispatchOutcome.STARTED, dispatch_id=claim.id, run_id=receipt.run_id
        )

    async def _adopt_existing(self, claim: Any) -> DispatchResult:
        found = await self._lookup.lookup(workflow_id=claim.workflow_id)
        if found is None:
            await self._dispatches.release_for_retry(
                claim.tenant_id,
                claim.id,
                claim.lease_owner,
                claim.lease_fencing_token,
                "already_started_but_not_found",
                retry_backoff_seconds(0),
            )
            return DispatchResult(
                DispatchOutcome.RETRY_LATER,
                dispatch_id=claim.id,
                detail="already started but describe found nothing; retrying start",
            )
        return await self._adopt_or_mirror(claim, found)

    async def _disambiguate(self, claim: Any) -> DispatchResult:
        found = await self._lookup.lookup(workflow_id=claim.workflow_id)
        if found is None:
            await self._dispatches.release_for_retry(
                claim.tenant_id,
                claim.id,
                claim.lease_owner,
                claim.lease_fencing_token,
                "start_timeout_no_workflow_found",
                retry_backoff_seconds(0),
            )
            return DispatchResult(
                DispatchOutcome.RETRY_LATER,
                dispatch_id=claim.id,
                detail="ambiguous start resolved as not-started",
            )
        return await self._adopt_or_mirror(claim, found)

    async def _adopt_or_mirror(self, claim: Any, found: Any) -> DispatchResult:
        terminal, error_code = terminal_for_mirror(found.status)
        if terminal and error_code:
            await self._dispatches.mark_terminal(
                claim.tenant_id,
                claim.id,
                error_code=error_code,
                fencing_token=claim.lease_fencing_token,
            )
            return DispatchResult(
                DispatchOutcome.TERMINAL, dispatch_id=claim.id, detail=error_code
            )
        if terminal:
            recorded = await self._dispatches.record_workflow_accepted(
                claim.tenant_id, claim.id, claim.lease_fencing_token, found.run_id
            )
            if not recorded:
                return DispatchResult(DispatchOutcome.STALE, dispatch_id=claim.id)
            return DispatchResult(
                DispatchOutcome.ADOPTED_COMPLETED,
                dispatch_id=claim.id,
                run_id=found.run_id,
            )
        recorded = await self._dispatches.record_workflow_accepted(
            claim.tenant_id, claim.id, claim.lease_fencing_token, found.run_id
        )
        if not recorded:
            return DispatchResult(DispatchOutcome.STALE, dispatch_id=claim.id)
        return DispatchResult(
            DispatchOutcome.ADOPTED_RUNNING,
            dispatch_id=claim.id,
            run_id=found.run_id,
        )

    async def reconcile_once(
        self, *, worker_id: str, batch_limit: int = 25, canceler: Any = None
    ) -> ReconcileReport:
        """Repair stuck dispatches. Starts nothing: PENDING backlog belongs to
        the dispatcher; the reconciler fixes state and escalates what it
        cannot fix."""
        report = ReconcileReport()
        for _ in range(batch_limit):
            requeued = await self._dispatches.requeue_one_expired_lease(
                worker_id=worker_id
            )
            if requeued is None:
                break
            report.requeued_expired += 1

        accepted = await self._dispatches.list_by_state("ACCEPTED", batch_limit)
        for row in accepted:
            request_state = await self._dispatches.read_request_state(
                row["tenant_id"], row["request_id"], row["revision"]
            )
            found = await self._lookup.lookup(workflow_id=row["workflow_id"])
            if request_state in ("CANCEL_REQUESTED", "CANCELED"):
                finished = await self._complete_cancellation(row, found, canceler)
                if finished:
                    report.canceled += 1
                    report.notes.append(
                        f"canceled dispatch {row['id']} ({finished})"
                    )
                else:
                    report.deferred_cancel += 1
                continue
            if found is None:
                await self._dispatches.mark_terminal(
                    row["tenant_id"],
                    row["id"],
                    error_code="workflow_not_found",
                )
                report.terminal_mirrored += 1
                report.notes.append(
                    f"escalated missing workflow {row['workflow_id']}"
                )
                continue
            if found.run_id != row.get("temporal_run_id"):
                adopted = await self._dispatches.adopt_run_id(
                    row["tenant_id"], row["id"], found.run_id
                )
                if adopted:
                    report.notes.append(
                        f"adopted run {found.run_id} for {row['workflow_id']}"
                    )
            terminal, error_code = terminal_for_mirror(found.status)
            if terminal and error_code:
                await self._dispatches.mark_terminal(
                    row["tenant_id"], row["id"], error_code=error_code
                )
                report.terminal_mirrored += 1

        retryable = await self._dispatches.list_by_state("RETRYABLE", batch_limit)
        for row in retryable:
            if int(row.get("attempts", 0)) >= self._max_attempts:
                await self._dispatches.mark_terminal(
                    row["tenant_id"],
                    row["id"],
                    error_code="retry_budget_exhausted",
                )
                report.retry_exhausted += 1

        if (
            report.requeued_expired
            or report.terminal_mirrored
            or report.retry_exhausted
            or report.canceled
        ):
            return report
        report.no_op += 1
        return report

    async def _complete_cancellation(
        self, row: dict[str, Any], found: Any, canceler: Any
    ) -> str | None:
        """Finish a requested cancellation. Returns a note or None if deferred.

        A missing or terminally finished workflow needs no cancel call; a
        running one does. Without a canceler only the safe cases proceed.
        """
        terminal = False
        terminal_error = ""
        if found is not None:
            terminal, terminal_error = terminal_for_mirror(found.status)
        needs_cancel = found is not None and not terminal
        if needs_cancel:
            if canceler is None:
                return None
            await canceler.cancel(
                workflow_id=row["workflow_id"], reason="operator cancellation"
            )
        transitioned = await self._dispatches.complete_request_cancellation(
            row["tenant_id"], row["request_id"], row["revision"]
        )
        await self._dispatches.mark_terminal(
            row["tenant_id"], row["id"], error_code="canceled"
        )
        if not transitioned:
            return "request already canceled"
        if needs_cancel:
            return "workflow canceled, request canceled"
        return "no running workflow; request canceled"
