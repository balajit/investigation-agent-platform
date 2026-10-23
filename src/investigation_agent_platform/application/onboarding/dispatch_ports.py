"""Phase 2 workflow boundary interfaces; deliberately no dispatcher behavior."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True)
class PinnedWorkflowInput:
    """Reference to one immutable request revision and execution generation.

    ``dispatch_id`` carries the control-store row identity when the caller
    holds it (deterministic workflows never invent it). UUID objects are
    fine in-process; the Temporal boundary converts to payload-safe strings.
    """

    tenant_id: str
    request_id: UUID
    revision: int
    execution_generation: int
    workflow_id: str
    input_reference: str
    input_version: str
    schema_version: str
    dispatch_id: str = ""


@dataclass(frozen=True)
class WorkflowStartReceipt:
    """Evidence returned by a start/describe operation, not a dispatch state.

    ``status`` is the described lifecycle state ("unknown" unless the
    lookup reports otherwise); reconciler mirrors only explicit terminal
    statuses and never invents an outcome from "unknown".
    """

    workflow_id: str
    run_id: str
    input_reference: str
    input_version: str
    status: str = "unknown"


class WorkflowStartPort(Protocol):
    """External workflow start capability for the future Phase 2 dispatcher."""

    async def start(self, workflow_input: PinnedWorkflowInput) -> WorkflowStartReceipt: ...


class WorkflowLookupPort(Protocol):
    """Read-only execution lookup for ambiguous-start reconciliation."""

    async def lookup(self, *, workflow_id: str) -> WorkflowStartReceipt | None: ...
