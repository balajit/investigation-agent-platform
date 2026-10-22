"""Phase 2 workflow boundary interfaces; deliberately no dispatcher behavior."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True)
class PinnedWorkflowInput:
    """Reference to one immutable request revision and execution generation."""

    tenant_id: str
    request_id: UUID
    revision: int
    execution_generation: int
    workflow_id: str
    input_reference: str
    input_version: str
    schema_version: str


@dataclass(frozen=True)
class WorkflowStartReceipt:
    """Evidence returned by a start/describe operation, not a dispatch state."""

    workflow_id: str
    run_id: str
    input_reference: str
    input_version: str


class WorkflowStartPort(Protocol):
    """External workflow start capability for the future Phase 2 dispatcher."""

    async def start(self, workflow_input: PinnedWorkflowInput) -> WorkflowStartReceipt: ...


class WorkflowLookupPort(Protocol):
    """Read-only execution lookup for ambiguous-start reconciliation."""

    async def lookup(self, *, workflow_id: str) -> WorkflowStartReceipt | None: ...
