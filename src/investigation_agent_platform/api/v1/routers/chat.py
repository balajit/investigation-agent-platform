# src/investigation_agent_platform/api/v1/routers/chat.py
"""Conversational investigation chat (Part 11.10).

Sessions are durable and tenant-scoped. Message replies stream as versioned
SSE events (`token` / `metadata` / `final` / `error`); suggested actions in
`final` are advisory and execute only through normal authorized/idempotent
APIs. No keyword-sniffing triggers exist anywhere in this path.
"""

import asyncio
import hashlib
import json
import logging
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from investigation_agent_platform.api.dependencies import get_app_context
from investigation_agent_platform.api.tenant import require_principal, require_tenant
from investigation_agent_platform.application.chat.chat_service import (
    ChatQuotaExceededError,
    ChatService,
)
from investigation_agent_platform.domain.common.exceptions import (
    ConcurrencyError,
    IdempotencyConflictError,
    IdempotencyInProgressError,
)
from investigation_agent_platform.domain.investigation.chat import MAX_CHAT_MESSAGE_CHARS

logger = logging.getLogger(__name__)

router = APIRouter(tags=["chat"])


class CreateSessionBody(BaseModel):
    investigation_id: UUID | None = Field(default=None)
    application_id: str | None = Field(default=None, max_length=128)
    classification: str = Field(default="INTERNAL", max_length=32)
    allowed_provider: str = Field(default="openai", max_length=32)
    authorization_reference: str = Field(default="", max_length=256)


class PostMessageBody(BaseModel):
    content: str = Field(..., min_length=1, max_length=MAX_CHAT_MESSAGE_CHARS)


def _chat_service(ctx: Any) -> ChatService:
    quota_policy = None
    try:
        from investigation_agent_platform.domain.common.quotas import QuotaPolicy
        from investigation_agent_platform.infrastructure.configuration.config import (
            load_application_config_from_env as _load_cfg,
        )

        quota_policy = QuotaPolicy.model_validate(_load_cfg().quotas.model_dump(mode="json"))
    except Exception as exc:
        logger.warning("Chat without quota guard; config unavailable", extra={"error": str(exc)})
    return ChatService(
        chat_repo=getattr(ctx, "chat_repo", None),
        artifact_repo=getattr(ctx, "artifact_repo", None),
        quota_enforcer=getattr(ctx, "quota_enforcer", None),
        quota_policy=quota_policy,
    )


def _streaming_gateway() -> Any:
    from investigation_agent_platform.infrastructure.configuration.config import (
        load_application_config_from_env,
    )
    from investigation_agent_platform.infrastructure.reasoning.streaming_adapters import (
        OpenAIStreamingGateway,
    )

    cfg = load_application_config_from_env().llm
    if cfg.provider.lower() not in ("openai", "azure"):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Streaming is unavailable for provider {cfg.provider!r}",
        )
    return OpenAIStreamingGateway(cfg)


@router.post("/chat/sessions", status_code=status.HTTP_201_CREATED)
async def create_chat_session(
    body: CreateSessionBody,
    x_tenant_id: str = Depends(require_tenant),
    principal_id: str = Depends(require_principal),
    x_idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
) -> dict[str, Any]:
    """Create one durable chat session (idempotent)."""
    _ = principal_id
    if not x_idempotency_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="X-Idempotency-Key header is required",
        )
    ctx = get_app_context()
    if getattr(ctx, "chat_repo", None) is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Chat store is unavailable",
        )
    canonical = json.dumps(body.model_dump(mode="json"), sort_keys=True)
    request_hash = hashlib.sha256(canonical.encode()).hexdigest()
    try:
        cached, reserved = await ctx.idempotency_store.reserve_or_get(
            x_tenant_id, x_idempotency_key, request_hash, operation="chat-session"
        )
    except IdempotencyConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except IdempotencyInProgressError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    if not reserved:
        return cached or {}
    service = _chat_service(ctx)
    try:
        session = await service.create_session(
            x_tenant_id,
            investigation_id=body.investigation_id,
            application_id=body.application_id,
            classification=body.classification,
            allowed_provider=body.allowed_provider,
            authorization_reference=body.authorization_reference,
        )
    except ChatQuotaExceededError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=str(exc),
            headers={"Retry-After": "60"},
        ) from exc
    except ConcurrencyError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    response_payload: dict[str, Any] = {"session": session.model_dump(mode="json")}
    await ctx.idempotency_store.complete(
        x_tenant_id, x_idempotency_key, response_payload, operation="chat-session"
    )
    return response_payload


@router.post("/chat/sessions/{session_id}/messages")
async def post_chat_message(
    session_id: str,
    body: PostMessageBody,
    x_tenant_id: str = Depends(require_tenant),
) -> StreamingResponse:
    """Stream one assistant reply as versioned SSE events."""
    ctx = get_app_context()
    if getattr(ctx, "chat_repo", None) is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Chat store is unavailable",
        )
    try:
        session_uuid = UUID(session_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid session id format"
        ) from exc
    session = await ctx.chat_repo.get_session(x_tenant_id, session_uuid)
    if session is None or getattr(session, "tenant_id", x_tenant_id) != x_tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    gateway = _streaming_gateway()
    service = _chat_service(ctx)

    async def _events() -> AsyncIterator[str]:
        stream = service.stream_reply(x_tenant_id, session_uuid, body.content, gateway)
        try:
            async for event in stream:
                payload = json.dumps(event.model_dump(mode="json"))
                yield f"event: {event.event}\ndata: {payload}\n\n"
        except asyncio.CancelledError:
            # Client disconnect: generator teardown closes the provider
            # stream (see ChatService); nothing is retried or duplicated.
            raise
        except Exception as exc:
            logger.warning("Chat SSE failed", extra={"error": str(exc)})
            payload = json.dumps({"event": "error", "data": {"code": "STREAM_FAILED"}})
            yield f"event: error\ndata: {payload}\n\n"
        finally:
            # Deterministic teardown: deliver GeneratorExit to the service
            # generator so quota release + provider close run now, not on GC.
            aclose = getattr(stream, "aclose", None)
            if callable(aclose):
                try:
                    await aclose()
                except Exception:
                    pass

    return StreamingResponse(
        _events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/chat/sessions/{session_id}/messages")
async def list_chat_messages(
    session_id: str,
    x_tenant_id: str = Depends(require_tenant),
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """Paginated message history for one owned session."""
    ctx = get_app_context()
    try:
        session_uuid = UUID(session_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid session id format"
        ) from exc
    session = await ctx.chat_repo.get_session(x_tenant_id, session_uuid)
    if session is None or getattr(session, "tenant_id", x_tenant_id) != x_tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    service = _chat_service(ctx)
    items, total = await service.list_messages(x_tenant_id, session_uuid, limit, offset)
    return {
        "items": [message.model_dump(mode="json") for message in items],
        "total": total,
        "offset": offset,
        "limit": limit,
    }


@router.delete("/chat/sessions/{session_id}")
async def delete_chat_session(
    session_id: str,
    x_tenant_id: str = Depends(require_tenant),
) -> dict[str, Any]:
    """Delete one owned session + messages; legal hold refuses with 409."""
    ctx = get_app_context()
    try:
        session_uuid = UUID(session_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid session id format"
        ) from exc
    session = await ctx.chat_repo.get_session(x_tenant_id, session_uuid)
    if session is None or getattr(session, "tenant_id", x_tenant_id) != x_tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    service = _chat_service(ctx)
    if not await service.delete_session(x_tenant_id, session_uuid):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Legal hold is set; deletion refused",
        )
    return {"session_id": session_id, "status": "DELETED"}
