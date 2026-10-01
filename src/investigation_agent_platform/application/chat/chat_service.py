# src/investigation_agent_platform/application/chat/chat_service.py
"""Durable conversational chat orchestration (Part 11.10).

Streams provider text as versioned SSE events, persists both sides of every
turn, and accounts tokens/cost. Suggested actions are deterministic,
session-derived advisories (view investigation, list evidence) — never
keyword-sniffed triggers, never executed here.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from investigation_agent_platform.domain.common.extension import CapabilityScope
from investigation_agent_platform.domain.common.provenance import ProvenanceRecord
from investigation_agent_platform.domain.investigation.chat import (
    CHAT_CONTRACT_VERSION,
    MAX_CHAT_MESSAGES_PER_SESSION,
    MAX_STREAM_SECONDS,
    MAX_SUGGESTED_ACTIONS,
    ChatMessage,
    ChatRole,
    ChatSessionStatus,
    ChatStreamEvent,
    InvestigationChatSession,
    SuggestedAction,
)

logger = logging.getLogger(__name__)

CHAT_BUILDER_PLUGIN_ID = "chat-service"
CHAT_BUILDER_VERSION = "1.0"

_HISTORY_TURNS = 20
_MAX_ACCUMULATED_CHARS = 32000

_RESTRICTED_CLASSIFICATIONS = frozenset({"RESTRICTED", "SECRET", "TOP_SECRET"})


def _event(kind: str, data: dict[str, Any]) -> ChatStreamEvent:
    return ChatStreamEvent(event=kind, protocol_version=CHAT_CONTRACT_VERSION, data=data)


def _error_event(code: str) -> ChatStreamEvent:
    return _event("error", {"code": code})


class ChatQuotaExceededError(Exception):
    """Raised when a chat session/stream is quota-rejected."""

    def __init__(self, tenant_id: str, detail: str = "") -> None:
        super().__init__(f"Chat quota exceeded for tenant {tenant_id}: {detail}")
        self.tenant_id = tenant_id


class KnowledgeChatContextBuilder:
    """Chat context from existing knowledge retrieval (Part 11.10).

    Investigation-bound sessions pull ACTIVE artifacts for the session's
    application; pre-investigation sessions get no corpus snippets (their
    session-scoped envelope, not investigation identity, authorizes them).
    """

    def __init__(self, artifact_repo: Any | None = None, limit: int = 5) -> None:
        self._artifact_repo = artifact_repo
        self._limit = max(1, min(limit, 20))

    async def build_context(self, tenant_id: str, system_prompt: str, query: str) -> Any:
        from investigation_agent_platform.ports.reasoning.streaming import ChatContext

        snippets: list[str] = []
        application_id, _, terms = (query or "").partition("\x1f")
        _ = terms
        if self._artifact_repo is not None and application_id:
            try:
                artifacts = await self._artifact_repo.list_active_for_reuse(
                    tenant_id, application_id
                )
                for artifact in artifacts[: self._limit]:
                    statement = getattr(artifact, "statement", "")
                    if statement:
                        snippets.append(str(statement)[:1000])
            except Exception as exc:
                logger.warning(
                    "Chat context retrieval failed; continuing without snippets",
                    extra={"tenant_id": tenant_id, "error": str(exc)},
                )
        return ChatContext(system_prompt=system_prompt, snippets=snippets)


class ChatService:
    """Application service for durable chat sessions and streamed turns."""

    def __init__(
        self,
        chat_repo: Any,
        artifact_repo: Any | None = None,
        quota_enforcer: Any | None = None,
        quota_policy: Any | None = None,
    ) -> None:
        self._chat_repo = chat_repo
        self._artifact_repo = artifact_repo
        self._quota_enforcer = quota_enforcer
        self._quota_policy = quota_policy
        self._context_builder = KnowledgeChatContextBuilder(artifact_repo)

    # -- sessions --------------------------------------------------------

    async def create_session(
        self,
        tenant_id: str,
        investigation_id: UUID | None = None,
        application_id: str | None = None,
        classification: str = "INTERNAL",
        allowed_provider: str = "openai",
        authorization_reference: str = "",
    ) -> InvestigationChatSession:
        """Create one session, quota-guarded when an enforcer is present."""
        if self._quota_enforcer is not None and self._quota_policy is not None:
            scope = CapabilityScope(tenant_id=tenant_id, application_id=application_id)
            decision = await self._quota_enforcer.check(scope, "chat.session", self._quota_policy)
            if not decision.allowed:
                raise ChatQuotaExceededError(tenant_id, "max_active_chat_sessions")
            try:
                return await self._create_session_row(
                    tenant_id,
                    investigation_id,
                    application_id,
                    classification,
                    allowed_provider,
                    authorization_reference,
                )
            finally:
                await self._quota_enforcer.release(scope, "chat.session")
        return await self._create_session_row(
            tenant_id,
            investigation_id,
            application_id,
            classification,
            allowed_provider,
            authorization_reference,
        )

    async def _create_session_row(
        self,
        tenant_id: str,
        investigation_id: UUID | None,
        application_id: str | None,
        classification: str,
        allowed_provider: str,
        authorization_reference: str,
    ) -> InvestigationChatSession:
        session = InvestigationChatSession(
            tenant_id=tenant_id,
            investigation_id=investigation_id,
            application_id=application_id,
            classification=classification,
            allowed_provider=allowed_provider,
            authorization_reference=authorization_reference,
        )
        await self._chat_repo.save_session(tenant_id, session)
        return session

    async def get_session(
        self, tenant_id: str, session_id: UUID
    ) -> InvestigationChatSession | None:
        session: InvestigationChatSession | None = await self._chat_repo.get_session(
            tenant_id, session_id
        )
        return session

    async def list_messages(
        self, tenant_id: str, session_id: UUID, limit: int = 100, offset: int = 0
    ) -> tuple[list[ChatMessage], int]:
        session = await self._chat_repo.get_session(tenant_id, session_id)
        if session is None:
            return [], 0
        result: tuple[list[ChatMessage], int] = await self._chat_repo.list_messages(
            tenant_id, session_id, limit, offset
        )
        return result

    async def delete_session(self, tenant_id: str, session_id: UUID) -> bool:
        """Delete a session + messages; legal hold refuses (returns False)."""
        session = await self._chat_repo.get_session(tenant_id, session_id)
        if session is None:
            return False
        if session.legal_hold:
            return False
        await self._chat_repo.delete_session(tenant_id, session_id)
        return True

    async def purge_expired(
        self, tenant_id: str, retention_days: int, limit: int = 500
    ) -> list[UUID]:
        """Delete sessions older than retention (legal hold excluded)."""
        from investigation_agent_platform.domain.investigation.chat import (
            CHAT_RETENTION_DAYS,
        )

        cutoff = datetime.now(UTC) - timedelta(days=max(1, retention_days or CHAT_RETENTION_DAYS))
        aged = await self._chat_repo.sessions_older_than(tenant_id, cutoff, limit)
        purged: list[UUID] = []
        for session in aged:
            if session.legal_hold:
                continue
            await self._chat_repo.delete_session(tenant_id, session.id)
            purged.append(session.id)
        return purged

    # -- streaming -------------------------------------------------------

    def _check_envelope_policy(self, session: InvestigationChatSession, gateway: Any) -> str | None:
        """Return an error code when the session policy forbids streaming."""
        if (
            session.classification.upper() in _RESTRICTED_CLASSIFICATIONS
            and not session.authorization_reference
        ):
            return "POLICY_DENIED"
        provider = str(
            getattr(getattr(gateway, "_config", None), "provider", "openai") or "openai"
        ).lower()
        allowed = (session.allowed_provider or "openai").lower()
        compatible = {provider, "any"} | (
            {"azure", "openai"} if provider in ("azure", "openai") else set()
        )
        if allowed not in compatible:
            return "POLICY_DENIED"
        return None

    def _suggested_actions(self, session: InvestigationChatSession) -> list[SuggestedAction]:
        """Deterministic session-derived advisories (never keyword-sniffed)."""
        actions: list[SuggestedAction] = []
        if session.investigation_id is not None:
            investigation_id = str(session.investigation_id)
            actions.append(
                SuggestedAction(
                    action_type="view_investigation",
                    label="View investigation",
                    endpoint=f"/api/v1/investigations/{investigation_id}",
                    method="GET",
                    payload={"investigation_id": investigation_id},
                    provenance=self._provenance(session, {}),
                )
            )
            actions.append(
                SuggestedAction(
                    action_type="list_evidence",
                    label="List evidence",
                    endpoint=f"/api/v1/investigations/{investigation_id}/evidence",
                    method="GET",
                    payload={"investigation_id": investigation_id},
                    provenance=self._provenance(session, {}),
                )
            )
        return actions[:MAX_SUGGESTED_ACTIONS]

    def _provenance(
        self, session: InvestigationChatSession, digests: dict[str, str]
    ) -> ProvenanceRecord:
        return ProvenanceRecord(
            plugin_id=CHAT_BUILDER_PLUGIN_ID,
            plugin_version=CHAT_BUILDER_VERSION,
            schema_version=CHAT_CONTRACT_VERSION,
            input_digests=digests,
            metadata={"session_id": str(session.id)},
        )

    async def stream_reply(
        self,
        tenant_id: str,
        session_id: UUID,
        user_text: str,
        gateway: Any,
    ) -> AsyncIterator[ChatStreamEvent]:
        """Yield versioned SSE events for one turn (async generator).

        Emits `metadata`, then `token`* , then exactly one of `final` /
        `error`. Provider failures after the first token surface as `error`
        with the partial text persisted — never retried, never duplicated.
        """
        from investigation_agent_platform.infrastructure.reasoning.pricing import (
            estimate_cost,
        )
        from investigation_agent_platform.infrastructure.reasoning.streaming_adapters import (
            count_text_tokens,
        )

        session = await self._chat_repo.get_session(tenant_id, session_id)
        if session is None:
            yield _error_event("SESSION_NOT_FOUND")
            return
        if session.status != ChatSessionStatus.ACTIVE:
            yield _error_event("SESSION_CLOSED")
            return
        policy_error = self._check_envelope_policy(session, gateway)
        if policy_error is not None:
            yield _error_event(policy_error)
            return

        scope = CapabilityScope(tenant_id=tenant_id, application_id=session.application_id)
        quota_held = False
        if self._quota_enforcer is not None and self._quota_policy is not None:
            decision = await self._quota_enforcer.check(scope, "chat.stream", self._quota_policy)
            if not decision.allowed:
                yield _error_event("QUOTA_EXCEEDED")
                return
            quota_held = True

        model_name = str(getattr(getattr(gateway, "_config", None), "model_name", "gpt-4o-mini"))
        try:
            messages, _ = await self._chat_repo.list_messages(
                tenant_id, session_id, limit=MAX_CHAT_MESSAGES_PER_SESSION, offset=0
            )
            if len(messages) >= MAX_CHAT_MESSAGES_PER_SESSION:
                yield _error_event("SESSION_FULL")
                return
            user_message = ChatMessage(
                tenant_id=tenant_id,
                session_id=session_id,
                role=ChatRole.USER,
                content=user_text,
            )
            await self._chat_repo.save_message(tenant_id, user_message)

            history = [
                {"role": m.role.value, "content": m.content} for m in messages[-_HISTORY_TURNS:]
            ]
            if session.investigation_id is not None:
                system_prompt = (
                    "You are an investigation assistant. Answer about the "
                    f"investigation {session.investigation_id} using the "
                    "provided context. Be concise."
                )
                query = f"{session.application_id or ''}\x1f{user_text}"
            else:
                system_prompt = (
                    "You are an investigation assistant. No investigation is "
                    "attached to this session; answer generally and do not "
                    "invent investigation details."
                )
                query = f"\x1f{user_text}"
            context = await self._context_builder.build_context(tenant_id, system_prompt, query)
            if context.snippets:
                system_prompt = f"{system_prompt}\n\nKnown context:\n" + "\n".join(
                    f"- {snippet}" for snippet in context.snippets
                )

            yield _event(
                "metadata",
                {
                    "session_id": str(session_id),
                    "model": model_name,
                    "investigation_id": (
                        str(session.investigation_id)
                        if session.investigation_id is not None
                        else None
                    ),
                },
            )

            prompt_tokens = count_text_tokens(model_name, system_prompt + user_text)
            accumulated: list[str] = []
            accumulated_chars = 0
            finish_reason: str | None = None
            started = time.perf_counter()
            provider_stream = gateway.stream(tenant_id, system_prompt, history)
            try:
                async with asyncio.timeout(MAX_STREAM_SECONDS):
                    async for chunk in provider_stream:
                        if time.perf_counter() - started > MAX_STREAM_SECONDS:
                            break
                        if chunk.text:
                            piece = chunk.text[: _MAX_ACCUMULATED_CHARS - accumulated_chars]
                            if not piece:
                                break  # bound reached: stop consuming
                            accumulated.append(piece)
                            accumulated_chars += len(piece)
                            yield _event("token", {"text": piece})
                        if chunk.done:
                            finish_reason = chunk.finish_reason
                            break
            except TimeoutError:
                logger.warning("Chat stream timed out", extra={"tenant_id": tenant_id})
                await self._persist_partial(
                    tenant_id, session, "".join(accumulated), model_name, prompt_tokens
                )
                yield _error_event("STREAM_TIMEOUT")
                return
            except Exception as exc:
                logger.warning("Chat stream failed", extra={"error": str(exc)})
                await self._persist_partial(
                    tenant_id, session, "".join(accumulated), model_name, prompt_tokens
                )
                yield _error_event("PROVIDER_ERROR")
                return
            finally:
                aclose = getattr(provider_stream, "aclose", None)
                if callable(aclose):
                    try:
                        await aclose()
                    except Exception:
                        pass

            full_text = "".join(accumulated)
            completion_tokens = count_text_tokens(model_name, full_text)
            cost, pricing_version = estimate_cost(model_name, prompt_tokens, completion_tokens)
            actions = self._suggested_actions(session)
            final_payload = {
                "content": full_text,
                "suggested_actions": [a.model_dump(mode="json") for a in actions],
                "usage": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "estimated_cost_usd": cost,
                    "pricing_version": pricing_version,
                },
                "finish_reason": finish_reason,
            }
            # Schema-validate the final response before emission.
            validated_message = ChatMessage(
                tenant_id=tenant_id,
                session_id=session_id,
                role=ChatRole.ASSISTANT,
                content=full_text or "(no response)",
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                estimated_cost_usd=cost,
                provenance=self._provenance(session, {}),
            )
            for action in actions:
                SuggestedAction.model_validate(action.model_dump(mode="json"))
            await self._chat_repo.save_message(tenant_id, validated_message)
            final_payload["message_id"] = str(validated_message.id)
            yield _event("final", final_payload)
        finally:
            if quota_held and self._quota_enforcer is not None:
                try:
                    await self._quota_enforcer.release(scope, "chat.stream")
                except Exception:
                    pass

    async def _persist_partial(
        self,
        tenant_id: str,
        session: InvestigationChatSession,
        partial_text: str,
        model_name: str,
        prompt_tokens: int,
    ) -> None:
        """Persist interrupted output explicitly marked partial (never silent)."""
        from investigation_agent_platform.infrastructure.reasoning.streaming_adapters import (
            count_text_tokens,
        )

        try:
            await self._chat_repo.save_message(
                tenant_id,
                ChatMessage(
                    tenant_id=tenant_id,
                    session_id=session.id,
                    role=ChatRole.ASSISTANT,
                    content=(partial_text or "(interrupted)")[:8000],
                    prompt_tokens=prompt_tokens,
                    completion_tokens=count_text_tokens(model_name, partial_text),
                    provenance=self._provenance(session, {"mode": "partial"}),
                ),
            )
        except Exception as exc:
            logger.warning("Partial chat persistence failed", extra={"error": str(exc)})
