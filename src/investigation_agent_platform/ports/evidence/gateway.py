# src/investigation_agent_platform/ports/evidence/gateway.py
"""Evidence gateway port protocol and standardized query result envelopes."""

from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from investigation_agent_platform.domain.evidence.models import Evidence
from investigation_agent_platform.domain.evidence.requests import (
    ApplicationStateRequest,
    CallGraphRequest,
    CodeHistoryRequest,
    CodeSearchRequest,
    RuntimeEvidenceRequest,
    SourceRequest,
    SymbolRequest,
)


class EvidenceQueryResult(BaseModel):
    """Standardized result envelope for evidence query results."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    items: list[Evidence] = Field(default_factory=list)
    cursor: str | None = None
    total_count: int = 0
    has_more: bool = False
    provider_metadata: dict[str, Any] = Field(default_factory=dict)
    execution_metadata: dict[str, Any] = Field(
        default_factory=lambda: {
            "execution_time_ms": 0.0,
            "partial_result": False,
            "errors": [],
            "tokens_consumed": 0,
        }
    )


@runtime_checkable
class EvidenceGatewayProtocol(Protocol):
    """Primary asynchronous port abstraction governing all evidence discovery operations."""

    async def search_runtime_evidence(
        self, tenant_id: str, investigation_id: UUID, application_id: str, request: RuntimeEvidenceRequest
    ) -> EvidenceQueryResult:
        ...

    async def get_application_state(
        self, tenant_id: str, investigation_id: UUID, application_id: str, request: ApplicationStateRequest
    ) -> EvidenceQueryResult:
        ...

    async def search_code(
        self, tenant_id: str, investigation_id: UUID, application_id: str, request: CodeSearchRequest
    ) -> EvidenceQueryResult:
        ...

    async def find_symbol(
        self, tenant_id: str, investigation_id: UUID, application_id: str, request: SymbolRequest
    ) -> EvidenceQueryResult:
        ...

    async def get_source(
        self, tenant_id: str, investigation_id: UUID, application_id: str, request: SourceRequest
    ) -> Evidence:
        ...

    async def find_call_graph(
        self, tenant_id: str, investigation_id: UUID, application_id: str, request: CallGraphRequest
    ) -> EvidenceQueryResult:
        ...

    async def get_code_history(
        self, tenant_id: str, investigation_id: UUID, application_id: str, request: CodeHistoryRequest
    ) -> EvidenceQueryResult:
        ...

    async def correlate(
        self, tenant_id: str, investigation_id: UUID, application_id: str, root_evidence_ids: list[UUID], max_depth: int
    ) -> EvidenceQueryResult:
        ...