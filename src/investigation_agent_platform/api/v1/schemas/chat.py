# src/investigation_agent_platform/api/v1/schemas/chat.py
"""Response models for the chat router (JSON routes only; SSE is out of scope)."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from investigation_agent_platform.domain.investigation.chat import (
    MAX_CHAT_MESSAGE_CHARS,
    InvestigationChatSession,
)


class CreateSessionBody(BaseModel):
    """POST /chat/sessions request body (input model; response is ChatSessionResponse)."""

    investigation_id: UUID | None = Field(
        default=None, description="Investigation to attach the session to"
    )
    application_id: str | None = Field(
        default=None, max_length=128, description="Application profile scope for the session"
    )
    classification: str = Field(
        default="INTERNAL", max_length=32, description="Data classification gate for model routing"
    )
    allowed_provider: str = Field(
        default="openai", max_length=32, description="Permitted LLM provider for this session"
    )
    authorization_reference: str = Field(
        default="",
        max_length=256,
        description="Authorization ticket reference for elevated data classes",
    )


class PostMessageBody(BaseModel):
    """POST /chat/sessions/{session_id}/messages request body (streams SSE)."""

    content: str = Field(
        ..., min_length=1, max_length=MAX_CHAT_MESSAGE_CHARS, description="User message content"
    )


class ChatSessionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session: InvestigationChatSession


class SessionDeleteAck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    status: str
