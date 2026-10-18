# src/investigation_agent_platform/domain/investigation/chat.py
"""Durable conversational investigation chat (Part 11.10).

Sessions and messages are tenant-scoped durable records with retention and
purge. Investigation-bound sessions reuse knowledge retrieval and the
investigation-scoped prompt envelope; pre-investigation sessions carry a
session-scoped data-policy envelope instead — the investigation envelope's
`investigation_id` is never made optional to accommodate chat.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from investigation_agent_platform.domain.common.provenance import ProvenanceRecord

#: Contract version for chat stream events and suggested actions.
CHAT_CONTRACT_VERSION = "1.0"

#: Retention for chat sessions/messages (days); enforced on purge.
CHAT_RETENTION_DAYS = 90

#: Bounds keeping streams and sessions small.
MAX_CHAT_MESSAGE_CHARS = 8000
MAX_CHAT_MESSAGES_PER_SESSION = 500
MAX_STREAM_SECONDS = 300
MAX_SUGGESTED_ACTIONS = 5


class ChatRole(StrEnum):
    """Message author."""

    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class ChatSessionStatus(StrEnum):
    """Session lifecycle."""

    ACTIVE = "ACTIVE"
    CLOSED = "CLOSED"
    PURGED = "PURGED"


class InvestigationChatSession(BaseModel):
    """One durable chat session, optionally bound to an investigation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    tenant_id: str = Field(..., min_length=1, max_length=128)
    investigation_id: UUID | None = Field(
        default=None,
        description="Bound investigation; None means pre-investigation chat.",
    )
    application_id: str | None = Field(default=None, max_length=128)
    status: ChatSessionStatus = Field(default=ChatSessionStatus.ACTIVE)
    classification: str = Field(default="INTERNAL", max_length=32)
    allowed_provider: str = Field(default="openai", max_length=32)
    authorization_reference: str = Field(default="", max_length=256)
    legal_hold: bool = Field(default=False)
    contract_version: str = Field(default=CHAT_CONTRACT_VERSION, max_length=32)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ChatMessage(BaseModel):
    """One durable chat message within a session."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    tenant_id: str = Field(..., min_length=1, max_length=128)
    session_id: UUID
    role: ChatRole
    content: str = Field(..., min_length=1, max_length=MAX_CHAT_MESSAGE_CHARS)
    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)
    estimated_cost_usd: float = Field(default=0.0, ge=0.0)
    provenance: ProvenanceRecord | None = Field(default=None)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class SuggestedAction(BaseModel):
    """One advisory action suggestion (Part 11.10).

    Advisory only: the client executes through normal authorized/idempotent
    APIs where authorization and policy are re-evaluated. The LLM never
    executes actions.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    action_type: str = Field(..., min_length=1, max_length=64)
    label: str = Field(..., min_length=1, max_length=256)
    endpoint: str = Field(..., min_length=1, max_length=512)
    method: str = Field(default="POST", min_length=1, max_length=16)
    payload: dict[str, Any] = Field(default_factory=dict)
    provenance: ProvenanceRecord | None = Field(default=None)


class ChatSessionEnvelope(BaseModel):
    """Session-scoped data-policy envelope for pre-investigation chat.

    Mirrors the investigation prompt envelope's policy fields without an
    investigation identity, so pre-investigation chat is authorized without
    weakening the investigation-scoped envelope.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: str = Field(..., max_length=128)
    session_id: UUID
    classification: str = Field(default="INTERNAL", max_length=32)
    allowed_provider: str = Field(default="openai", max_length=32)
    authorization_reference: str = Field(default="", max_length=256)


class ChatStreamEvent(BaseModel):
    """One versioned SSE event (Part 11.10).

    Kinds: `token` (display text), `metadata` (safe usage/progress),
    `final` (complete schema-validated response + suggested actions),
    `error` (stable public error code, no raw provider text).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    event: str = Field(..., min_length=1, max_length=32)
    protocol_version: str = Field(default=CHAT_CONTRACT_VERSION, max_length=32)
    data: dict[str, Any] = Field(default_factory=dict)
