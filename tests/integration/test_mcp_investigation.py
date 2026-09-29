# tests/integration/test_mcp_investigation.py
"""Part 9 Phase 6 integration: MCP tool -> services -> gateway -> adapter -> mocked ES.

Proves the handlers never bypass the abstraction layer and the full stack
preserves isolation, pagination binding, provenance, redaction, and
completeness end to end.
"""

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from investigation_agent_platform.application.evidence.selector import (
    EvidenceProviderSelector,
)
from investigation_agent_platform.application.investigation.composition import (
    build_investigation_services,
)
from investigation_agent_platform.infrastructure.evidence.logs.trace_telemetry import (
    TraceTelemetryExtractor,
)
from investigation_agent_platform.infrastructure.evidence.runtime.elastic import (
    AsyncElasticAdapter,
    ElasticAdapterSettings,
)
from investigation_agent_platform.infrastructure.evidence.security import (
    QuerySafetyPolicy,
    SensitiveDataRedactor,
)
from investigation_agent_platform.mcp.context import (
    McpRequestContext,
    bind_mcp_context,
    reset_mcp_context,
)
from investigation_agent_platform.mcp.server import build_mcp_server

TENANT = "tenant-a"
APP_ID = "checkout-app"
TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
SECRET_SQL_PASSWORD = "hunter2-secret-value"
GITHUB_TOKEN = "ghp_abcdefghijklmnopqrstuvwxyz"


def _hit(doc_id, message, extra_source=None, sort_ts=1767225600000):  # type: ignore[no-untyped-def]
    source = {"@timestamp": "2026-01-01T00:00:00Z", "message": message}
    source.update(extra_source or {})
    return {
        "_id": doc_id,
        "_index": "logs-tenant-a-1-production-1",
        "_source": source,
        "sort": [sort_ts, doc_id],
    }


def _code_hit(doc_id, trace_id=TRACE_ID, message="checkout timeout", secret_sql=None):  # type: ignore[no-untyped-def]
    return _hit(
        doc_id,
        message,
        {
            "service": {"name": "checkout"},
            "log": {"level": "ERROR", "origin": {"function": "checkout.submit"}},
            "code": {"function": "checkout.submit", "filepath": "/app/checkout.py", "lineno": 142},
            "labels": {"trace_id": trace_id},
            "db": {
                "statement": secret_sql or "SELECT id FROM payments.transactions WHERE id = 7",
                "dialect": "postgres",
            },
        },
    )


class _EsClient:
    """Mocked AsyncElasticsearch routing ids-queries and bool-queries."""

    def __init__(self, hits):  # type: ignore[no-untyped-def]
        self._hits = {hit["_id"]: hit for hit in hits}
        self.calls = []
        self.search = AsyncMock(side_effect=self._route)

    async def _route(self, index=None, body=None, **kwargs):  # type: ignore[no-untyped-def]
        self.calls.append({"index": index, "body": body})
        query = (body or {}).get("query", {})
        if "ids" in query:
            wanted = query["ids"]["values"]
            found = [self._hits[doc_id] for doc_id in wanted if doc_id in self._hits]
            return {"hits": {"hits": found, "total": {"value": len(found)}}}
        size = (body or {}).get("size", 10)
        ordered = sorted(self._hits.values(), key=lambda hit: hit["_id"])
        search_after = (body or {}).get("search_after")
        if search_after is not None and len(search_after) >= 2:
            ordered = [hit for hit in ordered if hit["_id"] > search_after[1]]
        page = ordered[:size]
        return {"hits": {"hits": page, "total": {"value": len(ordered)}}}


class _Stack:
    """Full stack with fakes only at the repository boundary."""

    def __init__(self, hits, investigations=None):  # type: ignore[no-untyped-def]
        self.client = _EsClient(hits)
        self.adapter = AsyncElasticAdapter(
            client=self.client,  # type: ignore[arg-type]
            settings=ElasticAdapterSettings(cursor_signing_key=b"integration-test-key"),
        )
        self.selector = EvidenceProviderSelector()
        self.selector.register_runtime_provider("elastic-primary", self.adapter, default=True)
        self.selector.freeze()
        from investigation_agent_platform.application.evidence.gateway import (
            AsyncEvidenceGateway,
        )

        self.gateway = AsyncEvidenceGateway(
            provider_selector=self.selector,
            query_safety_policy=QuerySafetyPolicy(),
            sanitizer=SensitiveDataRedactor(),
        )
        self.investigations = investigations or {}
        self.evidence_rows = {}
        self.saved_batches = []
        stack = self

        class _InvestigationRepo:
            async def get_by_id(self, tenant_id, investigation_id):  # type: ignore[no-untyped-def]
                return stack.investigations.get((tenant_id, investigation_id))

        class _EvidenceRepo:
            async def get_by_id(self, tenant_id, evidence_id):  # type: ignore[no-untyped-def]
                return stack.evidence_rows.get((tenant_id, evidence_id))

            async def save_batch(self, tenant_id, evidence_list, investigation_id=None):  # type: ignore[no-untyped-def]
                stack.saved_batches.append((tenant_id, list(evidence_list), investigation_id))
                for evidence in evidence_list:
                    stack.evidence_rows[(tenant_id, evidence.evidence_id)] = evidence

        class _ProfileRepo:
            async def get_by_application_id(self, tenant_id, application_id, version=None):  # type: ignore[no-untyped-def]
                return SimpleNamespace(
                    environment="production",
                    observability_configuration=SimpleNamespace(mapping_source_id=None),
                    investigation_configuration=SimpleNamespace(max_evidence_per_query=100),
                )

        self.services = build_investigation_services(
            gateway=self.gateway,
            selector=self.selector,
            investigation_repo=_InvestigationRepo(),  # type: ignore[arg-type]
            evidence_repo=_EvidenceRepo(),  # type: ignore[arg-type]
            profile_repo=_ProfileRepo(),  # type: ignore[arg-type]
            sanitizer=SensitiveDataRedactor(),
            derivation=TraceTelemetryExtractor(),
        )
        self.server = build_mcp_server(self.services)

    def add_investigation(self, tenant=TENANT, iid=None, app_id=APP_ID):  # type: ignore[no-untyped-def]
        iid = iid or uuid.uuid4()
        self.investigations[(tenant, iid)] = SimpleNamespace(
            tenant_id=tenant, application_id=app_id
        )
        return iid

    async def call(self, tool, arguments, tenant=TENANT, iid=None, correlation="corr-1"):  # type: ignore[no-untyped-def]
        token = bind_mcp_context(
            McpRequestContext(
                tenant_id=tenant,
                investigation_id=iid,
                correlation_id=correlation,
                actor_id="tester",
            )
        )
        try:
            result = await self.server.call_tool(tool, arguments)
        finally:
            reset_mcp_context(token)
        return result.structured_content["result"]


def _search_args(**overrides):  # type: ignore[no-untyped-def]
    args = {
        "environment": "production",
        "identifiers": None,
        "keywords": ["timeout"],
        "services": ["checkout"],
        "severities": ["ERROR"],
        "start_time": "2026-01-01T00:00:00Z",
        "end_time": "2026-01-01T00:15:00Z",
        "limit": 10,
        "cursor": None,
    }
    args.update(overrides)
    return args


@pytest.mark.asyncio
async def test_search_pagination_round_trip_through_stack():
    stack = _Stack([_code_hit("doc1"), _code_hit("doc2"), _code_hit("doc3")])
    iid = stack.add_investigation()
    first = await stack.call("search_runtime_evidence", _search_args(limit=2), iid=iid)
    assert len(first["items"]) == 2
    assert first["has_more"] is True
    assert first["next_cursor"]
    assert first["completeness"]["reason"] == "pagination_available"
    item = first["items"][0]
    assert item["provenance"]["provider"] == "ELASTIC"
    assert len(item["provenance"]["query_fingerprint"]) == 64
    assert item["provenance"]["source"] == "runtime"
    assert "tenant_id" not in item["attributes"]

    bodies = [call["body"] for call in stack.client.calls]
    assert "search_after" not in bodies[-1]
    second = await stack.call(
        "search_runtime_evidence", _search_args(limit=2, cursor=first["next_cursor"]), iid=iid
    )
    assert "search_after" in stack.client.calls[-1]["body"]
    assert len(second["items"]) == 1
    assert second["has_more"] is False
    assert second["completeness"]["complete"] is True


@pytest.mark.asyncio
async def test_cursor_reused_with_different_query_rejected():
    stack = _Stack([_code_hit("doc1"), _code_hit("doc2")])
    iid = stack.add_investigation()
    first = await stack.call("search_runtime_evidence", _search_args(limit=2), iid=iid)
    replay = await stack.call(
        "search_runtime_evidence",
        _search_args(limit=2, cursor=first["next_cursor"], services=["billing"]),
        iid=iid,
    )
    assert replay["error"]["code"] == "INVALID_CURSOR"
    assert replay["error"]["retryable"] is False


@pytest.mark.asyncio
async def test_tampered_cursor_rejected():
    stack = _Stack([_code_hit("doc1"), _code_hit("doc2")])
    iid = stack.add_investigation()
    first = await stack.call("search_runtime_evidence", _search_args(limit=2), iid=iid)
    cursor = first["next_cursor"]
    bad = cursor[:-1] + ("A" if cursor[-1] != "A" else "B")
    replay = await stack.call("search_runtime_evidence", _search_args(limit=2, cursor=bad), iid=iid)
    assert replay["error"]["code"] == "INVALID_CURSOR"


@pytest.mark.asyncio
async def test_unauthorized_identifier_key_rejected_before_provider_io():
    # Part 10: unknown identifier keys fail closed at the provider boundary
    # against the resolved mapping — safe envelope, zero ES traffic, never
    # a broadened query.
    stack = _Stack([_code_hit("doc1")])
    iid = stack.add_investigation()
    result = await stack.call(
        "search_runtime_evidence", _search_args(identifiers={"_source.password": "x"}), iid=iid
    )
    assert result["error"]["code"] == "INVALID_REQUEST"
    assert stack.client.calls == []


@pytest.mark.asyncio
async def test_investigate_trace_end_to_end_with_redaction():
    secret_sql = f"SELECT * FROM payments.transactions WHERE password = '{SECRET_SQL_PASSWORD}'"
    message = f"checkout timeout {GITHUB_TOKEN} investigate"
    stack = _Stack([_code_hit("doc1", message=message, secret_sql=secret_sql)])
    iid = stack.add_investigation()
    result = await stack.call(
        "investigate_trace",
        {
            "trace_id": TRACE_ID,
            "environment": "production",
            "start_time": "2026-01-01T00:00:00Z",
            "end_time": "2026-01-01T00:15:00Z",
            "max_evidence": 100,
        },
        iid=iid,
    )
    assert result["trace_id"] == TRACE_ID
    (location,) = result["code_locations"]
    assert location["function"] == "checkout.submit"
    assert location["evidence_ids"]
    (table,) = result["tables"]
    assert table["schema"] == "payments" and table["table"] == "transactions"
    (statement,) = result["sql_statements"]
    assert SECRET_SQL_PASSWORD not in statement["statement"]
    assert statement["parse_status"] == "parsed"
    assert result["evidence"]
    assert result["completeness"]["complete"] is True
    payload = str(result)
    assert SECRET_SQL_PASSWORD not in payload
    assert GITHUB_TOKEN not in payload


@pytest.mark.asyncio
async def test_get_evidence_round_trip_after_search():
    stack = _Stack([_code_hit("doc1")])
    iid = stack.add_investigation()
    found = await stack.call("search_runtime_evidence", _search_args(), iid=iid)
    evidence_id = found["items"][0]["evidence_id"]
    assert stack.saved_batches, "search must persist the identity mapping"
    detail = await stack.call("get_evidence", {"evidence_id": evidence_id}, iid=iid)
    assert detail["evidence_id"] == evidence_id
    assert detail["provenance"]["provider"] == "ELASTIC"


@pytest.mark.asyncio
async def test_get_cross_investigation_not_found_without_provider_call():
    stack = _Stack([_code_hit("doc1")])
    iid_a = stack.add_investigation()
    iid_b = stack.add_investigation(iid=uuid.uuid4())
    found = await stack.call("search_runtime_evidence", _search_args(), iid=iid_a)
    evidence_id = found["items"][0]["evidence_id"]
    calls_before = len(stack.client.calls)
    detail = await stack.call("get_evidence", {"evidence_id": evidence_id}, iid=iid_b)
    assert detail["error"]["code"] == "EVIDENCE_NOT_FOUND"
    assert len(stack.client.calls) == calls_before


@pytest.mark.asyncio
async def test_unknown_investigation_scope_forbidden():
    stack = _Stack([_code_hit("doc1")])
    result = await stack.call("search_runtime_evidence", _search_args(), iid=uuid.uuid4())
    assert result["error"]["code"] == "FORBIDDEN"


@pytest.mark.asyncio
async def test_foreign_tenant_cursor_rejected():
    stack = _Stack([_code_hit("doc1"), _code_hit("doc2")])
    iid = stack.add_investigation()
    first = await stack.call("search_runtime_evidence", _search_args(limit=2), iid=iid)
    other_iid = stack.add_investigation(tenant="other-tenant", iid=uuid.uuid4())
    replay = await stack.call(
        "search_runtime_evidence",
        _search_args(limit=2, cursor=first["next_cursor"]),
        tenant="other-tenant",
        iid=other_iid,
    )
    assert replay["error"]["code"] == "INVALID_CURSOR"


def _window_args(**overrides):  # type: ignore[no-untyped-def]
    now = datetime.now(UTC)
    args = _search_args(
        start_time=(now - timedelta(minutes=15)).isoformat(),
        end_time=now.isoformat(),
    )
    args.update(overrides)
    return args


@pytest.mark.asyncio
async def test_search_validates_time_window_semantics():
    stack = _Stack([_code_hit("doc1")])
    iid = stack.add_investigation()
    now = datetime.now(UTC)
    result = await stack.call(
        "search_runtime_evidence",
        _search_args(start_time=now.isoformat(), end_time=(now - timedelta(minutes=1)).isoformat()),
        iid=iid,
    )
    assert result["error"]["code"] == "INVALID_REQUEST"
