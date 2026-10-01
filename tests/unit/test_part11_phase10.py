# tests/unit/test_part11_phase10.py
"""Task 8.b — Conversational chat tests (Phase 11.10).

Service behavior against the in-memory chat repository; the SSE surface
with `TestClient` (no lifespan); one durable SQL round-trip against real
PostgreSQL when reachable (skipped otherwise). No network, no LLM calls —
streaming gateways are fakes.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from investigation_agent_platform.api.app import create_app
from investigation_agent_platform.api.dependencies import AppContext, set_app_context
from investigation_agent_platform.application.chat.chat_service import (
    ChatQuotaExceededError,
    ChatService,
)
from investigation_agent_platform.domain.investigation.chat import (
    CHAT_RETENTION_DAYS,
    ChatMessage,
    ChatRole,
    InvestigationChatSession,
    SuggestedAction,
)
from investigation_agent_platform.ports.reasoning.streaming import StreamTextChunk

# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


class _FakeStreamGateway:
    """Canned chunk stream; tracks provider-stream closure."""

    def __init__(
        self,
        chunks: list[str] | None = None,
        fail_after: int | None = None,
        provider: str = "openai",
    ) -> None:
        self._chunks = chunks if chunks is not None else ["hello ", "world"]
        self._fail_after = fail_after
        self._config = SimpleNamespace(
            provider=provider, model_name="gpt-4o-mini", temperature=0.0, max_tokens=4096
        )
        self.closed = False
        self.calls: list[tuple[str, str, list[dict[str, str]]]] = []

    async def stream(self, tenant_id: str, system_prompt: str, messages: list[dict[str, str]]):
        self.calls.append((tenant_id, system_prompt, messages))
        try:
            for i, text in enumerate(self._chunks):
                if self._fail_after is not None and i >= self._fail_after:
                    raise RuntimeError("provider exploded")
                yield StreamTextChunk(text=text)
            yield StreamTextChunk(done=True, finish_reason="stop")
        finally:
            self.closed = True


async def _collect(service: ChatService, tenant: str, session_id, text: str):
    return [
        event
        async for event in service.stream_reply(tenant, session_id, text, _FakeStreamGateway())
    ]


def _kinds(events) -> list[str]:
    return [event.event for event in events]


# ---------------------------------------------------------------------------
# service: sessions
# ---------------------------------------------------------------------------


class TestChatSessions:
    pytestmark = pytest.mark.asyncio

    async def test_create_get_list_delete_roundtrip(self) -> None:
        ctx = AppContext()
        service = ChatService(ctx.chat_repo)
        session = await service.create_session("tenant-a")
        assert session.status.value == "ACTIVE"
        fetched = await service.get_session("tenant-a", session.id)
        assert fetched is not None and fetched.id == session.id
        items, total = await service.list_messages("tenant-a", session.id)
        assert (items, total) == ([], 0)
        assert await service.delete_session("tenant-a", session.id) is True
        assert await service.get_session("tenant-a", session.id) is None

    async def test_cross_tenant_rejected(self) -> None:
        ctx = AppContext()
        service = ChatService(ctx.chat_repo)
        session = await service.create_session("tenant-a")
        assert await service.get_session("tenant-b", session.id) is None
        items, total = await service.list_messages("tenant-b", session.id)
        assert (items, total) == ([], 0)
        assert await service.delete_session("tenant-b", session.id) is False
        # Owner still intact.
        assert await service.get_session("tenant-a", session.id) is not None

    async def test_legal_hold_blocks_delete(self) -> None:
        ctx = AppContext()
        service = ChatService(ctx.chat_repo)
        session = await service.create_session("tenant-a")
        held = session.model_copy(update={"legal_hold": True})
        await ctx.chat_repo.save_session("tenant-a", held)
        assert await service.delete_session("tenant-a", session.id) is False
        assert await service.get_session("tenant-a", session.id) is not None

    async def test_quota_rejection_on_create(self) -> None:
        ctx = AppContext()

        class _Deny:
            async def check(self, scope: object, op: str, policy: object) -> object:
                return SimpleNamespace(allowed=False)

            async def release(self, scope: object, op: str) -> None:
                pass  # pragma: no cover

        service = ChatService(ctx.chat_repo, quota_enforcer=_Deny(), quota_policy=object())
        with pytest.raises(ChatQuotaExceededError):
            await service.create_session("tenant-a")

    async def test_purge_expired_skips_hold_and_fresh(self) -> None:
        ctx = AppContext()
        service = ChatService(ctx.chat_repo)
        old = InvestigationChatSession(
            tenant_id="tenant-a",
            created_at=datetime.now(UTC) - timedelta(days=CHAT_RETENTION_DAYS + 10),
        )
        held = InvestigationChatSession(
            tenant_id="tenant-a",
            legal_hold=True,
            created_at=datetime.now(UTC) - timedelta(days=CHAT_RETENTION_DAYS + 10),
        )
        fresh = await service.create_session("tenant-a")
        await ctx.chat_repo.save_session("tenant-a", old)
        await ctx.chat_repo.save_session("tenant-a", held)
        purged = await service.purge_expired("tenant-a", CHAT_RETENTION_DAYS)
        assert purged == [old.id]
        assert await service.get_session("tenant-a", held.id) is not None
        assert await service.get_session("tenant-a", fresh.id) is not None


# ---------------------------------------------------------------------------
# service: streaming
# ---------------------------------------------------------------------------


class TestChatStreaming:
    pytestmark = pytest.mark.asyncio

    async def test_event_ordering_and_final_schema(self) -> None:
        ctx = AppContext()
        service = ChatService(ctx.chat_repo)
        session = await service.create_session("tenant-a")
        gateway = _FakeStreamGateway()
        events = [
            event async for event in service.stream_reply("tenant-a", session.id, "hi", gateway)
        ]
        assert _kinds(events) == ["metadata", "token", "token", "final"]
        assert all(event.protocol_version == "1.0" for event in events)
        final = events[-1].data
        assert final["usage"]["prompt_tokens"] > 0
        assert final["usage"]["completion_tokens"] > 0
        assert final["finish_reason"] == "stop"
        assert final["message_id"]
        # Schema-validated final: message + actions re-validate.
        ChatMessage.model_validate(
            {
                "tenant_id": "tenant-a",
                "session_id": str(session.id),
                "role": "assistant",
                "content": final["content"],
            }
        )
        for action in final["suggested_actions"]:
            SuggestedAction.model_validate(action)
        # Both sides persisted.
        items, total = await service.list_messages("tenant-a", session.id)
        assert total == 2
        assert [m.role for m in items] == [ChatRole.USER, ChatRole.ASSISTANT]
        assert items[1].content == "hello world"
        assert gateway.closed is True

    async def test_investigation_bound_suggests_actions(self) -> None:
        ctx = AppContext()
        service = ChatService(ctx.chat_repo)
        investigation_id = uuid4()
        session = await service.create_session("tenant-a", investigation_id=investigation_id)
        events = await _collect(service, "tenant-a", session.id, "status?")
        final = events[-1].data
        assert len(final["suggested_actions"]) == 2
        endpoints = {a["endpoint"] for a in final["suggested_actions"]}
        assert f"/api/v1/investigations/{investigation_id}" in endpoints
        assert f"/api/v1/investigations/{investigation_id}/evidence" in endpoints

    async def test_pre_investigation_suggests_nothing(self) -> None:
        ctx = AppContext()
        service = ChatService(ctx.chat_repo)
        session = await service.create_session("tenant-a")
        events = await _collect(service, "tenant-a", session.id, "hello")
        assert events[-1].data["suggested_actions"] == []

    async def test_provider_failure_persists_partial_once(self) -> None:
        ctx = AppContext()
        service = ChatService(ctx.chat_repo)
        session = await service.create_session("tenant-a")
        gateway = _FakeStreamGateway(fail_after=1)
        events = [
            event async for event in service.stream_reply("tenant-a", session.id, "hi", gateway)
        ]
        assert _kinds(events) == ["metadata", "token", "error"]
        assert events[-1].data["code"] == "PROVIDER_ERROR"
        items, total = await service.list_messages("tenant-a", session.id)
        assert total == 2  # user + one partial, never duplicated
        assert items[1].content == "hello "
        assert gateway.closed is True

    async def test_disconnect_closes_provider_stream(self) -> None:
        ctx = AppContext()
        service = ChatService(ctx.chat_repo)
        session = await service.create_session("tenant-a")
        gateway = _FakeStreamGateway(chunks=[f"chunk-{i} " for i in range(50)])
        seen: list[str] = []
        stream = service.stream_reply("tenant-a", session.id, "hi", gateway)
        try:
            async for event in stream:
                seen.append(event.event)
                if event.event == "token":
                    break  # consumer disconnects after the first token
        finally:
            # ASGI servers aclose the response generator on disconnect;
            # this delivers teardown deterministically (not on GC).
            await stream.aclose()
        assert seen == ["metadata", "token"]
        assert gateway.closed is True

    async def test_unknown_and_closed_sessions(self) -> None:
        ctx = AppContext()
        service = ChatService(ctx.chat_repo)
        events = await _collect(service, "tenant-a", uuid4(), "hi")
        assert _kinds(events) == ["error"]
        assert events[0].data["code"] == "SESSION_NOT_FOUND"
        session = await service.create_session("tenant-a")
        closed = session.model_copy(update={"status": "CLOSED"})
        await ctx.chat_repo.save_session("tenant-a", closed)
        events = await _collect(service, "tenant-a", session.id, "hi")
        assert events[0].data["code"] == "SESSION_CLOSED"

    async def test_policy_denied(self) -> None:
        ctx = AppContext()
        service = ChatService(ctx.chat_repo)
        restricted = await service.create_session("tenant-a", classification="SECRET")
        events = await _collect(service, "tenant-a", restricted.id, "hi")
        assert events[0].data["code"] == "POLICY_DENIED"
        mismatched = await service.create_session("tenant-a", allowed_provider="anthropic")
        gateway = _FakeStreamGateway(provider="openai")
        events = [
            event async for event in service.stream_reply("tenant-a", mismatched.id, "hi", gateway)
        ]
        assert events[0].data["code"] == "POLICY_DENIED"

    async def test_stream_quota_rejection(self) -> None:
        ctx = AppContext()

        class _Deny:
            async def check(self, scope: object, op: str, policy: object) -> object:
                return SimpleNamespace(allowed=False)

            async def release(self, scope: object, op: str) -> None:
                pass  # pragma: no cover

        service = ChatService(ctx.chat_repo)
        session = await service.create_session("tenant-a")
        guarded = ChatService(ctx.chat_repo, quota_enforcer=_Deny(), quota_policy=object())
        events = await _collect(guarded, "tenant-a", session.id, "hi")
        assert events[0].data["code"] == "QUOTA_EXCEEDED"

    async def test_session_full(self) -> None:
        from investigation_agent_platform.domain.investigation.chat import (
            MAX_CHAT_MESSAGES_PER_SESSION,
            ChatMessage,
            ChatRole,
        )

        ctx = AppContext()
        service = ChatService(ctx.chat_repo)
        session = await service.create_session("tenant-a")
        for _ in range(MAX_CHAT_MESSAGES_PER_SESSION):
            await ctx.chat_repo.save_message(
                "tenant-a",
                ChatMessage(
                    tenant_id="tenant-a",
                    session_id=session.id,
                    role=ChatRole.USER,
                    content="filler",
                ),
            )
        events = await _collect(service, "tenant-a", session.id, "one more")
        assert events[0].data["code"] == "SESSION_FULL"


# ---------------------------------------------------------------------------
# durable SQL round-trip (real PostgreSQL when reachable)
# ---------------------------------------------------------------------------


class TestChatSqlDurable:
    def _factory(self):
        import os

        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        uri = os.environ.get("IAP_DATABASE_URI", "postgresql://iap:iap@localhost:5452/iap")
        if uri.startswith("postgresql://"):
            uri = "postgresql+asyncpg://" + uri[len("postgresql://") :]
        engine = create_async_engine(uri, pool_pre_ping=True)
        return engine, async_sessionmaker(engine, expire_on_commit=False)

    async def test_sql_roundtrip_survives_repo_rebuild(self) -> None:
        from investigation_agent_platform.infrastructure.persistence.chat_repository import (
            SqlAlchemyChatSessionRepository,
        )

        engine, factory = self._factory()
        try:
            repo = SqlAlchemyChatSessionRepository(factory)
            tenant = "chat-sql-probe"
            session = InvestigationChatSession(tenant_id=tenant)
            try:
                await repo.save_session(tenant, session)
            except Exception as exc:
                await engine.dispose()
                pytest.skip(f"postgres unreachable: {exc}")
            # New repository instance over the same database: durable, not
            # process-local memory.
            repo2 = SqlAlchemyChatSessionRepository(factory)
            fetched = await repo2.get_session(tenant, session.id)
            assert fetched is not None and fetched.id == session.id
            message = ChatMessage(
                tenant_id=tenant, session_id=session.id, role=ChatRole.USER, content="ping"
            )
            await repo2.save_message(tenant, message)
            items, total = await repo2.list_messages(tenant, session.id)
            assert total == 1 and items[0].content == "ping"
            assert await repo2.count_active_sessions(tenant) == 1
            await repo2.delete_session(tenant, session.id)
            assert await repo2.get_session(tenant, session.id) is None
            await engine.dispose()
        except Exception:
            await engine.dispose()
            raise


TestChatSqlDurable.test_sql_roundtrip_survives_repo_rebuild = pytest.mark.asyncio(
    TestChatSqlDurable.test_sql_roundtrip_survives_repo_rebuild
)


# ---------------------------------------------------------------------------
# API: sessions, SSE, history, delete
# ---------------------------------------------------------------------------


def _parse_sse(text: str) -> list[tuple[str, dict]]:
    import json

    events: list[tuple[str, dict]] = []
    for block in text.strip().split("\n\n"):
        kind, _, payload = block.partition("\ndata: ")
        assert kind.startswith("event: ")
        events.append((kind[len("event: ") :], json.loads(payload)))
    return events


class TestChatEndpoints:
    def _client(self, monkeypatch) -> tuple[TestClient, AppContext]:  # type: ignore[no-untyped-def]
        monkeypatch.setenv("IAP_ENVIRONMENT", "development")
        monkeypatch.setenv("IAP_DATABASE_URI", "postgresql://iap:iap@localhost:5452/iap")
        monkeypatch.setenv("IAP_LLM_API_KEY", "sk-test")
        ctx = AppContext()
        set_app_context(ctx)
        return TestClient(create_app()), ctx

    def _auth(self, tenant: str = "tenant-a") -> dict[str, str]:
        return {"X-Tenant-ID": tenant}

    def _streaming(self, monkeypatch, gateway: _FakeStreamGateway | None = None):  # type: ignore[no-untyped-def]
        gateway = gateway or _FakeStreamGateway()
        monkeypatch.setattr(
            "investigation_agent_platform.infrastructure.reasoning.streaming_adapters.OpenAIStreamingGateway",
            lambda cfg: gateway,
        )
        return gateway

    def test_create_requires_idempotency_key(self, monkeypatch) -> None:
        client, _ = self._client(monkeypatch)
        assert (
            client.post("/api/v1/chat/sessions", json={}, headers=self._auth()).status_code == 400
        )

    def test_create_replay_and_conflict(self, monkeypatch) -> None:
        client, _ = self._client(monkeypatch)
        headers = {**self._auth(), "X-Idempotency-Key": "chat-1"}
        first = client.post("/api/v1/chat/sessions", json={}, headers=headers)
        assert first.status_code == 201, first.text
        session_id = first.json()["session"]["id"]
        second = client.post("/api/v1/chat/sessions", json={}, headers=headers)
        assert second.status_code == 201
        assert second.json()["session"]["id"] == session_id
        conflict = client.post(
            "/api/v1/chat/sessions",
            json={"classification": "SECRET"},
            headers=headers,
        )
        assert conflict.status_code == 409

    def test_sse_ordering_and_final(self, monkeypatch) -> None:
        client, _ = self._client(monkeypatch)
        self._streaming(monkeypatch)
        session_id = client.post(
            "/api/v1/chat/sessions",
            json={},
            headers={**self._auth(), "X-Idempotency-Key": "chat-sse-1"},
        ).json()["session"]["id"]
        resp = client.post(
            f"/api/v1/chat/sessions/{session_id}/messages",
            json={"content": "hello"},
            headers=self._auth(),
        )
        assert resp.status_code == 200, resp.text
        assert resp.headers["content-type"].startswith("text/event-stream")
        events = _parse_sse(resp.text)
        assert [kind for kind, _ in events] == ["metadata", "token", "token", "final"]
        final = events[-1][1]["data"]
        assert final["content"] == "hello world"
        assert set(final["usage"]) >= {"prompt_tokens", "completion_tokens"}

    def test_sse_auth_and_ids(self, monkeypatch) -> None:
        client, _ = self._client(monkeypatch)
        self._streaming(monkeypatch)
        assert (
            client.post(
                "/api/v1/chat/sessions/not-a-uuid/messages",
                json={"content": "hi"},
                headers=self._auth(),
            ).status_code
            == 400
        )
        assert (
            client.post(
                f"/api/v1/chat/sessions/{uuid4()}/messages",
                json={"content": "hi"},
                headers=self._auth(),
            ).status_code
            == 404
        )
        session_id = client.post(
            "/api/v1/chat/sessions",
            json={},
            headers={**self._auth(), "X-Idempotency-Key": "chat-sse-2"},
        ).json()["session"]["id"]
        assert (
            client.post(
                f"/api/v1/chat/sessions/{session_id}/messages",
                json={"content": "hi"},
                headers=self._auth("tenant-b"),
            ).status_code
            == 404
        )

    def test_history_pagination(self, monkeypatch) -> None:
        client, _ = self._client(monkeypatch)
        self._streaming(monkeypatch, _FakeStreamGateway(chunks=["yo"]))
        session_id = client.post(
            "/api/v1/chat/sessions",
            json={},
            headers={**self._auth(), "X-Idempotency-Key": "chat-hist-1"},
        ).json()["session"]["id"]
        client.post(
            f"/api/v1/chat/sessions/{session_id}/messages",
            json={"content": "one"},
            headers=self._auth(),
        )
        body = client.get(
            f"/api/v1/chat/sessions/{session_id}/messages?limit=1&offset=0",
            headers=self._auth(),
        ).json()
        assert body["total"] == 2
        assert len(body["items"]) == 1
        assert body["items"][0]["role"] == "user"

    def test_delete_flows(self, monkeypatch) -> None:
        import anyio

        client, ctx = self._client(monkeypatch)
        session_id = client.post(
            "/api/v1/chat/sessions",
            json={},
            headers={**self._auth(), "X-Idempotency-Key": "chat-del-1"},
        ).json()["session"]["id"]
        assert (
            client.delete(f"/api/v1/chat/sessions/{session_id}", headers=self._auth()).json()[
                "status"
            ]
            == "DELETED"
        )
        assert (
            client.delete(f"/api/v1/chat/sessions/{session_id}", headers=self._auth()).status_code
            == 404
        )
        held_id = client.post(
            "/api/v1/chat/sessions",
            json={},
            headers={**self._auth(), "X-Idempotency-Key": "chat-del-2"},
        ).json()["session"]["id"]
        held_uuid = UUID(held_id)

        async def _hold() -> None:
            session = await ctx.chat_repo.get_session("tenant-a", held_uuid)
            assert session is not None
            await ctx.chat_repo.save_session(
                "tenant-a", session.model_copy(update={"legal_hold": True})
            )

        anyio.run(_hold)
        assert (
            client.delete(f"/api/v1/chat/sessions/{held_id}", headers=self._auth()).status_code
            == 409
        )

    def test_suggested_action_executes_through_authorized_api(self, monkeypatch) -> None:
        """The advisory endpoint in `final` is a normal authorized route."""
        from uuid import UUID as _UUID

        client, _ = self._client(monkeypatch)
        self._streaming(monkeypatch)
        investigation_id = str(_UUID(int=1))
        session_id = client.post(
            "/api/v1/chat/sessions",
            json={"investigation_id": investigation_id},
            headers={**self._auth(), "X-Idempotency-Key": "chat-act-1"},
        ).json()["session"]["id"]
        resp = client.post(
            f"/api/v1/chat/sessions/{session_id}/messages",
            json={"content": "status?"},
            headers=self._auth(),
        )
        final = _parse_sse(resp.text)[-1][1]["data"]
        endpoint = final["suggested_actions"][0]["endpoint"]
        # No tenant header → the suggested action cannot bypass auth.
        assert client.get(endpoint).status_code == 401
        # Wrong tenant → denied.
        assert client.get(endpoint, headers=self._auth("tenant-b")).status_code in (
            403,
            404,
        )


# ---------------------------------------------------------------------------
# migration 013
# ---------------------------------------------------------------------------


class TestMigration013:
    def test_013_chain(self) -> None:
        import importlib.util
        from pathlib import Path

        path = (
            Path(__file__).resolve().parents[2] / "migrations" / "versions" / "013_chat_sessions.py"
        )
        assert path.is_file()
        spec = importlib.util.spec_from_file_location("migration_013", path)
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert mod.revision == "013_chat_sessions"
        assert mod.down_revision == "012_finding_clusters"

    def test_013_is_ancestor_of_head(self) -> None:
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        cfg = Config("alembic.ini")
        script = ScriptDirectory.from_config(cfg)
        # 013 is no longer the head (014 superseded it); it must remain a
        # linear ancestor of the current single head.
        rev = script.get_revision(script.get_heads()[0])
        seen = []
        while rev is not None:
            seen.append(rev.revision)
            down = rev.down_revision
            rev = script.get_revision(down) if down else None
        assert "013_chat_sessions" in seen
        assert seen.index("014_reference_documents") < seen.index("013_chat_sessions")
