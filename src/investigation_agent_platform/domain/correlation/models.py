# src/investigation_agent_platform/domain/correlation/models.py
"""Correlation graph and relationship domain models."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator


class CorrelationType(StrEnum):
    """Edge types defining relationships in investigation correlation graphs."""

    CAUSED_BY = "CAUSED_BY"
    CORRELATES_WITH = "CORRELATES_WITH"
    TRIGGERED = "TRIGGERED"
    MUTATED_STATE = "MUTATED_STATE"
    LOGGED_ERROR = "LOGGED_ERROR"


class CorrelationNode(BaseModel):
    """Vertex in the ephemeral correlation graph wrapping an evidence entity."""

    model_config = ConfigDict(frozen=True)

    node_id: UUID = Field(default_factory=uuid4)
    evidence_id: UUID
    node_type: str = Field(..., max_length=128)
    attributes: dict[str, Any] = Field(default_factory=dict)


class CorrelationEdge(BaseModel):
    """Directed, weighted edge linking two correlation nodes."""

    model_config = ConfigDict(frozen=True)

    edge_id: UUID = Field(default_factory=uuid4)
    source_id: UUID
    target_id: UUID
    relationship_type: CorrelationType
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    metadata: dict[str, Any] = Field(default_factory=dict)


class TemporalRule(BaseModel):
    """Time-window alignment constraints for correlating cross-system events."""

    model_config = ConfigDict(frozen=True)

    max_delta_seconds: int = Field(default=300, ge=1, le=86400)
    must_precede: bool = True


class CorrelationGraph(BaseModel):
    """Container for multi-node correlated graph topologies with explicit scoping."""

    model_config = ConfigDict(frozen=True)

    tenant_id: str = Field(..., max_length=128)
    investigation_id: UUID
    nodes: list[CorrelationNode] = Field(default_factory=list, max_length=1000)
    edges: list[CorrelationEdge] = Field(default_factory=list, max_length=5000)
    temporal_rules: list[TemporalRule] = Field(default_factory=list, max_length=20)

    @field_validator("edges")
    @classmethod
    def validate_edge_endpoints_exist(cls, edges: list[CorrelationEdge], info: ValidationInfo) -> list[CorrelationEdge]:
        nodes = info.data.get("nodes", [])
        node_ids = {node.node_id for node in nodes}
        if nodes:
            for edge in edges:
                if edge.source_id not in node_ids or edge.target_id not in node_ids:
                    raise ValueError(
                        f"Edge endpoint ({edge.source_id} -> {edge.target_id}) does not exist in graph nodes."
                    )
        return edges