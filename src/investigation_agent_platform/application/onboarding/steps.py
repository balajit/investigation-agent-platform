# src/investigation_agent_platform/application/onboarding/steps.py
"""Step-ledger contracts for onboarding dispatch (Phase 2).

The ledger itself is the combination every phase already writes: dispatch
rows (state, fencing token, attempts, run id, error) plus the append-only
audit tables. This module defines the vocabulary for reading that history
as ordered steps — for the runbook, the reconciler, and observability —
without inventing a new store.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class DispatchStep(StrEnum):
    """Named steps of one dispatch lifecycle. Ordered by first occurrence."""

    ACCEPTED = "accepted"
    CLAIMED = "claimed"
    RENEWED = "renewed"
    STARTED = "started"
    HEARTBEAT = "heartbeat"
    RELEASED = "released"
    REQUEUED = "requeued"
    TERMINAL = "terminal"
    CANCELED = "canceled"


_AUDIT_ACTION_TO_STEP = {
    "CLAIM": DispatchStep.CLAIMED,
    "RENEW": DispatchStep.RENEWED,
    "RELEASE": DispatchStep.RELEASED,
    "STALE_FENCE": DispatchStep.REQUEUED,
}


@dataclass(frozen=True)
class StepRecord:
    """One ledger entry rendered from stored state. Human- and test-readable."""

    step: DispatchStep
    detail: str = ""
    fencing_token: int | None = None


def summarize_dispatch_steps(
    dispatch_row: dict[str, Any], audit_actions: list[str]
) -> list[StepRecord]:
    """Render the ordered step history for one dispatch.

    Pure function over already-persisted state: the audit trail contributes
    lease events (CLAIM/RENEW/RELEASE/STALE_FENCE, each carrying its fencing
    token), and the dispatch row contributes lifecycle facts (ACCEPTED run,
    terminal outcome, cancellation).
    """
    steps: list[StepRecord] = []
    for action in audit_actions:
        if action not in _AUDIT_ACTION_TO_STEP:
            raise ValueError(f"unknown dispatch audit action: {action!r}")
        steps.append(StepRecord(_AUDIT_ACTION_TO_STEP[action]))
    run_id = dispatch_row.get("temporal_run_id")
    if run_id:
        steps.append(StepRecord(DispatchStep.STARTED, detail=f"run {run_id}"))
    state = str(dispatch_row.get("state", ""))
    error = dispatch_row.get("last_error_code") or ""
    if state == "TERMINAL":
        if error == "canceled":
            steps.append(StepRecord(DispatchStep.CANCELED, error))
        else:
            steps.append(StepRecord(DispatchStep.TERMINAL, error))
    return steps


@dataclass(frozen=True)
class AnalyzeCoverageInput:
    """Phase 2/4 analysis-activity slot (DEP-07): inputs by reference only.

    The activity resolves verdicts deterministically from stored probe
    evidence and files the report under workflow authority; the agent
    judges, the workflow files. Idempotency key is request + revision +
    generation: retries of the same generation reassemble identically and
    the coverage repository treats matching replays as success.
    """

    tenant_id: str
    request_id: str
    revision: int
    generation: int
    snapshot_reference: str
    evidence_hashes: list[str] = field(default_factory=list)
    dispatch_id: str = ""
