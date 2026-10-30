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
import os
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, status
from fastapi.responses import StreamingResponse

from investigation_agent_platform.api.dependencies import get_app_context
from investigation_agent_platform.api.errors import ApiError, sanitize_extra
from investigation_agent_platform.api.tenant import require_principal, require_tenant
from investigation_agent_platform.api.v1.schemas.chat import (
    ChatSessionResponse,
    CreateSessionBody,
    PostMessageBody,
    SessionDeleteAck,
)
from investigation_agent_platform.api.v1.schemas.envelopes import ListEnvelope
from investigation_agent_platform.api.v1.schemas.errors import COMMON_ERROR_RESPONSES
from investigation_agent_platform.application.chat.chat_service import (
    ChatQuotaExceededError,
    ChatService,
)
from investigation_agent_platform.domain.common.exceptions import (
    ConcurrencyError,
    IdempotencyConflictError,
    IdempotencyInProgressError,
)
from investigation_agent_platform.domain.investigation.chat import (
    ChatMessage,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["chat"])


# Router-local input models moved to api.v1.schemas.chat (TO_DO-018);
# re-exported here so existing imports keep working.
__all__ = ["CreateSessionBody", "PostMessageBody", "router"]


def _is_production() -> bool:
    """Explicit-production check (unset env counts as non-production).

    Production deployments set IAP_ENVIRONMENT=production; until Wave 4
    (TO_DO-005) makes it the single source of truth, an unset variable must
    not flip test/dev traffic into fail-closed behavior.
    """
    return os.environ.get("IAP_ENVIRONMENT", "").lower() == "production"


def _load_quota_policy() -> Any | None:
    """Load the chat quota policy; None when config is unavailable."""
    from investigation_agent_platform.domain.common.quotas import QuotaPolicy
    from investigation_agent_platform.infrastructure.configuration.config import (
        load_application_config_from_env as _load_cfg,
    )

    return QuotaPolicy.model_validate(_load_cfg().quotas.model_dump(mode="json"))


def _require_quota_guard(
    tenant_id: str, quota_policy: Any | None, quota_enforcer: Any | None
) -> None:
    """Fail closed in production when the SSE/session quota guard is missing (TO_DO-027).

    Non-production keeps the best-effort warning path so dev/test traffic is
    unaffected; production refuses with 429 + Retry-After rather than
    streaming unguarded.
    """
    if quota_policy is not None and quota_enforcer is not None:
        return
    if not _is_production():
        return
    logger.warning(
        "Chat quota guard unavailable in production; refusing unguarded stream",
        extra=sanitize_extra({"tenant_id": tenant_id, "event": "chat.quota_guard_missing"}),
    )
    raise ApiError(
        code="QUOTA_GUARD_UNAVAILABLE",
        http_status=status.HTTP_429_TOO_MANY_REQUESTS,
        message="Chat quota enforcement is unavailable; try again shortly",
        headers={"Retry-After": "60"},
    )


def _chat_service(ctx: Any, *, tenant_id: str = "", guarded: bool = False) -> ChatService:
    quota_policy = None
    try:
        quota_policy = _load_quota_policy()
    except Exception:
        logger.warning(
            "Chat without quota guard; config unavailable",
            extra=sanitize_extra({"tenant_id": tenant_id, "event": "chat.quota_policy_missing"}),
        )
    quota_enforcer = getattr(ctx, "quota_enforcer", None)
    if guarded:
        _require_quota_guard(tenant_id, quota_policy, quota_enforcer)
    return ChatService(
        chat_repo=getattr(ctx, "chat_repo", None),
        artifact_repo=getattr(ctx, "artifact_repo", None),
        quota_enforcer=quota_enforcer,
        quota_policy=quota_policy,
        profile_repo=getattr(ctx, "profile_repo", None),
    )


def _guarded_chat_service(ctx: Any, tenant_id: str) -> ChatService:
    """Chat service for cost-incurring routes: fail closed in production (TO_DO-027)."""
    return _chat_service(ctx, tenant_id=tenant_id, guarded=True)


def _streaming_gateway() -> Any:
    from investigation_agent_platform.infrastructure.configuration.config import (
        load_application_config_from_env,
    )
    from investigation_agent_platform.infrastructure.reasoning.streaming_adapters import (
        OpenAIStreamingGateway,
    )

    cfg = load_application_config_from_env().llm
    if cfg.provider.lower() not in ("openai", "azure", "muse"):
        raise ApiError(
            code="STREAMING_UNAVAILABLE",
            http_status=status.HTTP_503_SERVICE_UNAVAILABLE,
            message=f"Streaming is unavailable for provider {cfg.provider!r}",
        )
    return OpenAIStreamingGateway(cfg)


@router.post(
    "/chat/sessions",
    response_model=ChatSessionResponse,
    response_model_by_alias=True,
    response_model_exclude_unset=True,
    responses=COMMON_ERROR_RESPONSES,
    status_code=status.HTTP_201_CREATED,
)
async def create_chat_session(
    body: CreateSessionBody,
    x_tenant_id: str = Depends(require_tenant),
    principal_id: str = Depends(require_principal),
    x_idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
) -> ChatSessionResponse:
    """Create one durable chat session (idempotent)."""
    _ = principal_id
    if not x_idempotency_key:
        raise ApiError(
            code="IDEMPOTENCY_KEY_REQUIRED",
            http_status=status.HTTP_400_BAD_REQUEST,
            message="X-Idempotency-Key header is required",
        )
    ctx = get_app_context()
    if getattr(ctx, "chat_repo", None) is None:
        raise ApiError(
            code="CHAT_STORE_UNAVAILABLE",
            http_status=status.HTTP_503_SERVICE_UNAVAILABLE,
            message="Chat store is unavailable",
        )
    canonical = json.dumps(body.model_dump(mode="json"), sort_keys=True)
    request_hash = hashlib.sha256(canonical.encode()).hexdigest()
    try:
        cached, reserved = await ctx.idempotency_store.reserve_or_get(
            x_tenant_id, x_idempotency_key, request_hash, operation="chat-session"
        )
    except IdempotencyConflictError as exc:
        raise ApiError(
            code="IDEMPOTENCY_CONFLICT", http_status=status.HTTP_409_CONFLICT, message=str(exc)
        ) from exc
    except IdempotencyInProgressError as exc:
        raise ApiError(
            code="IDEMPOTENCY_CONFLICT", http_status=status.HTTP_409_CONFLICT, message=str(exc)
        ) from exc
    if not reserved:
        return ChatSessionResponse.model_validate(cached or {})
    service = _guarded_chat_service(ctx, x_tenant_id)
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
        raise ApiError(
            code="CONCURRENCY_CONFLICT",
            http_status=status.HTTP_429_TOO_MANY_REQUESTS,
            message=str(exc),
            headers={"Retry-After": "60"},
        ) from exc
    except ConcurrencyError as exc:
        raise ApiError(
            code="CONCURRENCY_CONFLICT", http_status=status.HTTP_409_CONFLICT, message=str(exc)
        ) from exc
    response_payload = ChatSessionResponse.model_validate(
        {"session": session.model_dump(mode="json")}
    )
    await ctx.idempotency_store.complete(
        x_tenant_id,
        x_idempotency_key,
        response_payload.model_dump(mode="json", exclude_unset=True),
        operation="chat-session",
    )
    return response_payload


@router.post(
    "/chat/sessions/{session_id}/messages",
    responses={
        200: {
            "description": "Versioned SSE event stream (text/event-stream, out of scope for JSON typing).",
            "content": {"text/event-stream": {"schema": {"type": "string"}}},
        },
        **COMMON_ERROR_RESPONSES,
    },
)
async def post_chat_message(
    session_id: str,
    body: PostMessageBody,
    x_tenant_id: str = Depends(require_tenant),
) -> StreamingResponse:
    """Stream one assistant reply as versioned SSE events."""
    ctx = get_app_context()
    if getattr(ctx, "chat_repo", None) is None:
        raise ApiError(
            code="CHAT_STORE_UNAVAILABLE",
            http_status=status.HTTP_503_SERVICE_UNAVAILABLE,
            message="Chat store is unavailable",
        )
    try:
        session_uuid = UUID(session_id)
    except ValueError as exc:
        raise ApiError(
            code="INVALID_SESSION_ID",
            http_status=status.HTTP_400_BAD_REQUEST,
            message="Invalid session id format",
        ) from exc
    session = await ctx.chat_repo.get_session(x_tenant_id, session_uuid)
    if session is None or getattr(session, "tenant_id", x_tenant_id) != x_tenant_id:
        raise ApiError(
            code="SESSION_NOT_FOUND",
            http_status=status.HTTP_404_NOT_FOUND,
            message="Session not found",
        )
    # Fail-closed order (TO_DO-027): the quota guard is checked before the
    # streaming gateway is built, so production never streams unguarded.
    service = _guarded_chat_service(ctx, x_tenant_id)
    gateway = _streaming_gateway()

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
        except Exception:
            logger.exception(
                "Chat SSE failed",
                extra=sanitize_extra({"tenant_id": x_tenant_id, "event": "chat.sse.failed"}),
            )
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


@router.get(
    "/chat/sessions/{session_id}/messages",
    response_model=ListEnvelope[ChatMessage],
    response_model_by_alias=True,
    response_model_exclude_unset=True,
    responses=COMMON_ERROR_RESPONSES,
)
async def list_chat_messages(
    session_id: str,
    x_tenant_id: str = Depends(require_tenant),
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> ListEnvelope[ChatMessage]:
    """Paginated message history for one owned session."""
    ctx = get_app_context()
    try:
        session_uuid = UUID(session_id)
    except ValueError as exc:
        raise ApiError(
            code="INVALID_SESSION_ID",
            http_status=status.HTTP_400_BAD_REQUEST,
            message="Invalid session id format",
        ) from exc
    session = await ctx.chat_repo.get_session(x_tenant_id, session_uuid)
    if session is None or getattr(session, "tenant_id", x_tenant_id) != x_tenant_id:
        raise ApiError(
            code="SESSION_NOT_FOUND",
            http_status=status.HTTP_404_NOT_FOUND,
            message="Session not found",
        )
    service = _chat_service(ctx, tenant_id=x_tenant_id)
    items, total = await service.list_messages(x_tenant_id, session_uuid, limit, offset)
    return ListEnvelope[ChatMessage].model_validate(
        {
            "items": [message.model_dump(mode="json") for message in items],
            "total": total,
            "offset": offset,
            "limit": limit,
        }
    )


@router.delete(
    "/chat/sessions/{session_id}",
    response_model=SessionDeleteAck,
    response_model_by_alias=True,
    response_model_exclude_unset=True,
    responses=COMMON_ERROR_RESPONSES,
)
async def delete_chat_session(
    session_id: str,
    x_tenant_id: str = Depends(require_tenant),
) -> SessionDeleteAck:
    """Delete one owned session + messages; legal hold refuses with 409."""
    ctx = get_app_context()
    try:
        session_uuid = UUID(session_id)
    except ValueError as exc:
        raise ApiError(
            code="INVALID_SESSION_ID",
            http_status=status.HTTP_400_BAD_REQUEST,
            message="Invalid session id format",
        ) from exc
    session = await ctx.chat_repo.get_session(x_tenant_id, session_uuid)
    if session is None or getattr(session, "tenant_id", x_tenant_id) != x_tenant_id:
        raise ApiError(
            code="SESSION_NOT_FOUND",
            http_status=status.HTTP_404_NOT_FOUND,
            message="Session not found",
        )
    service = _chat_service(ctx, tenant_id=x_tenant_id)
    if not await service.delete_session(x_tenant_id, session_uuid):
        raise ApiError(
            code="CHAT_LEGAL_HOLD",
            http_status=status.HTTP_409_CONFLICT,
            message="Legal hold is set; deletion refused",
        )
    return SessionDeleteAck(session_id=session_id, status="DELETED")
