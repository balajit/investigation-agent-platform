# src/investigation_agent_platform/ports/correlation/engine.py
"""Deterministic correlation engine driven port protocol."""

from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from investigation_agent_platform.domain.profile.models import CorrelationProfile


class CorrelationNode(BaseModel):
    """Represents a discrete node within a correlation graph."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    label: str
    node_type: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class CorrelationEdge(BaseModel):
    """Represents a directed link between nodes within a correlation graph."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_id: UUID
    target_id: UUID
    relationship_type: str
    weight: float = 1.0
    metadata: dict[str, Any] = Field(default_factory=dict)


class CorrelationGraph(BaseModel):
    """Immutable graph contract representing correlated investigation entities."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    nodes: list[CorrelationNode] = Field(default_factory=list)
    edges: list[CorrelationEdge] = Field(default_factory=list)
    root_node_ids: list[UUID] = Field(default_factory=list)


class CorrelationResult(BaseModel):
    """Structured response container for correlation operations."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: str
    application_id: str
    graph: CorrelationGraph
    correlation_score: float = Field(ge=0.0, le=1.0)
    execution_time_ms: float
    metadata: dict[str, Any] = Field(default_factory=dict)


@runtime_checkable
class CorrelationEngine(Protocol):
    """Port interface for deterministic tenant-isolated graph correlation logic."""

    async def correlate(
        self, tenant_id: str, seed_identifiers: dict[str, str], profile: CorrelationProfile
    ) -> CorrelationResult: ...


@runtime_checkable
class CorrelationExpander(Protocol):
    """Port interface for expanding a correlation graph from root evidence IDs.

    Consumed by the Evidence Gateway when a request explicitly asks to traverse
    the graph from a set of root evidence nodes.
    """

    async def expand_correlation(
        self,
        tenant_id: str,
        application_id: str,
        root_evidence_ids: list[UUID],
        max_depth: int,
    ) -> CorrelationGraph: ...
