# src/investigation_agent_platform/domain/entity/models.py
"""System and business entity domain models."""

from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


class EntityType(StrEnum):
    SESSION = "SESSION"
    REQUEST = "REQUEST"
    USER = "USER"
    ORDER = "ORDER"
    TRANSACTION = "TRANSACTION"
    JOB = "JOB"
    SERVICE = "SERVICE"
    HOST = "HOST"
    DATABASE_RECORD = "DATABASE_RECORD"
    CLASS = "CLASS"
    METHOD = "METHOD"
    COMMIT = "COMMIT"
    EXCEPTION = "EXCEPTION"
    TRACE = "TRACE"


class InvestigationEntity(BaseModel):
    """Discovered system or business domain noun entity with tenant isolation."""

    model_config = ConfigDict(frozen=True)

    id: UUID = Field(default_factory=uuid4)
    tenant_id: str = Field(..., max_length=128)
    investigation_id: UUID
    entity_type: EntityType
    external_identifier: str = Field(..., max_length=512)
    provider: str = Field(default="system", max_length=128)
    name: str = Field(..., max_length=256)
    attributes: dict[str, Any] = Field(default_factory=dict)
    source_evidence_ids: list[UUID] = Field(default_factory=list, max_length=100)

    @property
    def deduplication_key(self) -> str:
        """Composite key ensuring entity identity uniqueness across providers."""
        return f"{self.tenant_id}:{self.entity_type.value}:{self.provider}:{self.external_identifier}"