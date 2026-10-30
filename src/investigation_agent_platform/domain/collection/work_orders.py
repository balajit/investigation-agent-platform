# src/investigation_agent_platform/domain/collection/work_orders.py
"""P6 typed work orders (DES-reason-workorders §2, chunk 2a: domain only).

Work orders are the reasoner's structured alternative to verdicts:
hypotheses plus discriminating next fetches fed back to the collection
frontier as seeds. Each carries the failure character it addresses and
a cost estimate. The running loop consumes them in chunk 2b; this module
only defines the contract and the translation into frontier candidates.

Version-pinned via WORK_ORDER_VERSION_CURRENT (never env-switched).

Deliberate non-goals: no source-roots check here (dispatch validation
stays the single backstop at the validator, never duplicated into
domain); no fetch execution; no loop or Temporal contact — pure domain.
"""

from __future__ import annotations

import json
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from investigation_agent_platform.domain.collection.frontier import (
    DEFAULT_CANDIDATE_TTL_ROUNDS,
    FailureCharacter,
    FrontierCandidate,
    FrontierDimension,
    SeedFetch,
)
from investigation_agent_platform.domain.collection.frontier import (
    EvidenceState as FrontierEvidenceState,
)

#: Version pin for every work order this module emits.
WORK_ORDER_VERSION_CURRENT = "workorder-v1"

#: Bound on seeds translated per call: model output is unbounded, the
#: frontier intake is not. First-N in input order — deterministic given
#: the input, never silently sampled.
MAX_WORK_ORDER_SEEDS = 8


class WorkOrderKind(StrEnum):
    """Work-order families (DES-reason-workorders §2)."""

    #: A discriminating next fetch: hypothesis plus the cheapest
    #: observation that would discriminate it.
    FETCH_SEED = "FETCH_SEED"
    #: A bounded re-retrieval for an unsatisfied claim. Typed successor
    #: of the ledger_requery history markers (chunk 2b retires them).
    REQUERY = "REQUERY"
    #: A claim-gap fill for a named binding gap. Typed successor of the
    #: claim-gap feed markers (chunk 2b retires them).
    GAP_FILL = "GAP_FILL"


class WorkOrderStatus(StrEnum):
    """Work-order lifecycle. Separate from CandidateStatus on purpose:
    an order is consumed by seeding, a candidate by dispatch."""

    PENDING = "PENDING"
    CONSUMED = "CONSUMED"
    EXPIRED = "EXPIRED"


def _is_json_safe(value: object) -> bool:
    """Parameters ride Temporal workflow state: only JSON-native shapes
    survive replay. Reject anything json.dumps cannot round-trip."""
    try:
        json.dumps(value)
        return True
    except (TypeError, ValueError):
        return False


class WorkOrder(BaseModel):
    """One typed unit of collection work from the reasoner."""

    model_config = ConfigDict(frozen=True)

    work_order_id: UUID = Field(default_factory=uuid4)
    version: str = Field(default=WORK_ORDER_VERSION_CURRENT, max_length=16)
    kind: WorkOrderKind
    action_type: str = Field(..., min_length=1, max_length=64)
    action_parameters: dict[str, object] = Field(default_factory=dict)
    addresses: FailureCharacter
    cost_estimate: int = Field(..., ge=0)
    ttl_rounds: int = Field(default=DEFAULT_CANDIDATE_TTL_ROUNDS, ge=0)
    max_attempts: int = Field(default=1, ge=1)
    seed_source: str = Field(default="", max_length=256)
    status: WorkOrderStatus = WorkOrderStatus.PENDING

    @model_validator(mode="after")
    def _require_replay_safe_parameters(self) -> WorkOrder:
        if not _is_json_safe(self.action_parameters):
            raise ValueError("action_parameters must be JSON-serializable (Temporal replay safety)")
        return self


def _identity_from_parameters(
    parameters: dict[str, object],
) -> dict[str, str | None]:
    """Best-effort (file, span, commit) identity from fetch parameters.

    Fetch parameter shapes vary by capability; only the widely shared
    keys are read, everything else is ignored. Missing keys yield None —
    the candidate validator still requires at least one identity field,
    so unlocatable orders fail closed at translation (skipped, never
    guessed).
    """
    file_path = parameters.get("file_path")
    start = parameters.get("start_line")
    end = parameters.get("end_line")
    span: str | None = None
    try:
        if start is not None and end is not None:
            span = f"{int(start)}-{int(end)}"
        elif start is not None:
            span = str(int(start))
    except (TypeError, ValueError):
        span = None
    revision = parameters.get("revision")
    return {
        "file_path": str(file_path)[:1024] if file_path is not None else None,
        "span": span,
        "commit": str(revision)[:128] if revision is not None else None,
    }


def work_orders_to_seeds(
    work_orders: list[WorkOrder],
    character: FailureCharacter,
    evidence_state: FrontierEvidenceState,
    *,
    ttl_rounds: int = DEFAULT_CANDIDATE_TTL_ROUNDS,
    max_seeds: int = MAX_WORK_ORDER_SEEDS,
) -> tuple[list[FrontierCandidate], list[UUID]]:
    """Translate pending work orders into frontier candidates (DES §4).

    Returns (candidates, skipped_ids). Skipped orders are never retried
    here: unlocatable identity, non-dispatchable dimension
    (LOCAL_COMPLETE), over-cap overflow, and non-pending status all fail
    closed, and the caller records the skip. Cost is carried, never
    re-estimated — pricing stays in one place with the exit rule.
    """
    dimension: FrontierDimension | None = character.spread_dimension(evidence_state)
    if dimension is None:
        return [], [order.work_order_id for order in list(work_orders or [])]
    seeded: list[FrontierCandidate] = []
    skipped: list[UUID] = []
    for order in list(work_orders or []):
        if len(seeded) >= max_seeds:
            skipped.append(order.work_order_id)
            continue
        if order.status != WorkOrderStatus.PENDING:
            skipped.append(order.work_order_id)
            continue
        identity = _identity_from_parameters(dict(order.action_parameters or {}))
        locator = order.action_parameters.get("file_path")
        target = str(locator)[:1024] if locator is not None else order.action_type
        try:
            seeded.append(
                FrontierCandidate(
                    file_path=identity["file_path"],
                    span=identity["span"],
                    commit=identity["commit"],
                    dimension=dimension,
                    seed_fetch=SeedFetch(fetch_kind=order.action_type, target=target),
                    addresses=character,
                    cost_estimate=order.cost_estimate,
                    depth=0,
                    ttl_rounds=ttl_rounds,
                    seed_source=order.seed_source
                    or f"work-order:{order.kind.value}/{order.work_order_id}",
                )
            )
        except ValueError:
            skipped.append(order.work_order_id)
    return seeded, skipped
