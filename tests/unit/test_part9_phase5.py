# tests/unit/test_part9_phase5.py
"""Part 9 Phase 5: trusted scope, application services, composition, gateway-path wiring."""

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import UUID

import pytest

from investigation_agent_platform.application.investigation.composition import (
    InvestigationServices,
    build_investigation_services,
)
from investigation_agent_platform.application.investigation.context import (
    InvestigationScope,
    require_investigation,
)
from investigation_agent_platform.application.investigation.evidence_detail import (
    EvidenceDetailService,
)
from investigation_agent_platform.application.investigation.runtime_evidence import (
    RuntimeEvidenceSearchResult,
    RuntimeEvidenceService,
)
from investigation_agent_platform.application.investigation.trace_investigation import (
    TraceInvestigationService,
)
from investigation_agent_platform.domain.common.exceptions import (
    ApplicationProfileNotFoundException,
    EvidenceNotFoundException,
    SecurityPolicyViolationException,
)
from investigation_agent_platform.domain.evidence.models import Evidence, EvidenceType
from investigation_agent_platform.domain.evidence.requests import RuntimeEvidenceRequest, TimeRange
from investigation_agent_platform.domain.provenance.models import (
    EvidenceFreshness,
    EvidenceProvenance,
    QueryFingerprint,
    SourceLocation,
)
from investigation_agent_platform.ports.evidence.gateway import EvidenceQueryResult

TENANT = "tenant-a"
APP_ID = "checkout-app"


def _scope(iid=None, tenant=TENANT):  # type: ignore[no-untyped-def]
    return InvestigationScope.create(tenant_id=tenant, investigation_id=iid or uuid.uuid4())


def _investigation(iid, tenant=TENANT, app_id=APP_ID):  # type: ignore[no-untyped-def]
    return SimpleNamespace(id=iid, tenant_id=tenant, application_id=app_id)


def _profile(environment="production", max_evidence=25, mapping_source_id=None):  # type: ignore[no-untyped-def]
    return SimpleNamespace(
        environment=environment,
        observability_configuration=SimpleNamespace(mapping_source_id=mapping_source_id),
        investigation_configuration=SimpleNamespace(max_evidence_per_query=max_evidence),
    )


def _evidence(iid, eid=None, tenant=TENANT, attrs=None):  # type: ignore[no-untyped-def]
    now = datetime.now(UTC)
    eid = eid or uuid.uuid4()
    return Evidence(
        tenant_id=tenant,
        investigation_id=iid,
        evidence_id=eid,
        evidence_type=EvidenceType.RUNTIME_LOG,
        provider="ELASTIC",
        source="elasticsearch://idx/doc",
        title="t",
        summary="s",
        fingerprint="fp",
        observed_at=now,
        retrieved_at=now,
        attributes=attrs if attrs is not None else {"message": "m"},
        provenance=EvidenceProvenance(
            tenant_id=tenant,
            investigation_id=iid,
            provider_type="ELASTIC",
            requested_provider_id="elastic-primary",
            actual_provider_id="elastic-primary",
            source_system="Elasticsearch",
            retrieval_timestamp=now,
            query_fingerprint=QueryFingerprint(
                provider_type="ELASTIC", operation="SEARCH", normalized_query_hash="f" * 64
            ),
            source_location=SourceLocation(system="Elasticsearch", identifier="idx/doc"),
        ),
        freshness=EvidenceFreshness(observed_at=now, retrieved_at=now),
    )


def _request(**overrides):  # type: ignore[no-untyped-def]
    now = datetime.now(UTC)
    kwargs = {
        "environment": "production",
        "identifiers": {"trace_id": "abc"},
        "time_range": TimeRange(start_time=now - timedelta(minutes=5), end_time=now),
        "limit": 10,
    }
    kwargs.update(overrides)
    return RuntimeEvidenceRequest(**kwargs)


class _InvestigationRepo:
    def __init__(self, investigations=None):  # type: ignore[no-untyped-def]
        self._store = investigations or {}
        self.calls = []

    async def get_by_id(self, tenant_id, investigation_id):  # type: ignore[no-untyped-def]
        self.calls.append((tenant_id, investigation_id))
        inv = self._store.get((tenant_id, investigation_id))
        return inv


class _EvidenceRepo:
    def __init__(self, rows=None):  # type: ignore[no-untyped-def]
        self._rows = rows or {}
        self.saved = []

    async def get_by_id(self, tenant_id, evidence_id):  # type: ignore[no-untyped-def]
        return self._rows.get((tenant_id, evidence_id))

    async def save_batch(self, tenant_id, evidence_list, investigation_id=None):  # type: ignore[no-untyped-def]
        self.saved.append((tenant_id, list(evidence_list), investigation_id))


class _ProfileRepo:
    def __init__(self, profile=None):  # type: ignore[no-untyped-def]
        self._profile = profile

    async def get_by_application_id(self, tenant_id, application_id, version=None):  # type: ignore[no-untyped-def]
        return self._profile


class _Gateway:
    def __init__(self, result=None):  # type: ignore[no-untyped-def]
        self._result = result or EvidenceQueryResult(items=[], has_more=False, total_count=0)
        self.calls = []

    async def search_runtime_evidence(self, tenant_id, investigation_id, application_id, request):  # type: ignore[no-untyped-def]
        self.calls.append((tenant_id, investigation_id, application_id, request))
        return self._result


class _Provider:
    def __init__(self, evidence=None):  # type: ignore[no-untyped-def]
        self._evidence = evidence
        self.search_calls = []
        self.get_calls = []

    async def search_runtime_evidence(
        self, tenant_id, investigation_id, request, profile, correlation_id=None
    ):  # type: ignore[no-untyped-def]
        self.search_calls.append((tenant_id, investigation_id, request, correlation_id))
        return EvidenceQueryResult(items=[], has_more=False, total_count=0)

    async def get_runtime_evidence(
        self,
        tenant_id,
        investigation_id,
        evidence_id,
        environment,
        mapping_lookup,
        correlation_id=None,
        mapping_source_id=None,
    ):  # type: ignore[no-untyped-def]
        self.get_calls.append(
            (tenant_id, investigation_id, evidence_id, environment, correlation_id,
             mapping_source_id)
        )
        resolved = await mapping_lookup.get_evidence(tenant_id, evidence_id)
        assert resolved is not None
        return self._evidence


class _Selector:
    def __init__(self, provider):  # type: ignore[no-untyped-def]
        self._provider = provider
        self.calls = []

    async def get_runtime_provider(self, tenant_id, application_id, environment):  # type: ignore[no-untyped-def]
        self.calls.append((tenant_id, application_id, environment))
        return self._provider


class _Sanitizer:
    def __init__(self):  # type: ignore[no-untyped-def]
        self.calls = []

    async def sanitize_evidence(self, tenant_id, evidence):  # type: ignore[no-untyped-def]
        self.calls.append((tenant_id, evidence))
        return evidence


# --- InvestigationScope ------------------------------------------------------------------


def test_scope_create_and_defaults():
    iid = uuid.uuid4()
    scope = InvestigationScope.create(tenant_id=TENANT, investigation_id=iid, actor_id="a")
    assert scope.tenant_id == TENANT and scope.investigation_id == iid
    assert scope.correlation_id and scope.actor_id == "a"


def test_scope_rejects_untrusted_shapes():
    iid = uuid.uuid4()
    with pytest.raises(SecurityPolicyViolationException):
        InvestigationScope.create(tenant_id="t-*", investigation_id=iid)
    with pytest.raises(SecurityPolicyViolationException):
        InvestigationScope.create(tenant_id="", investigation_id=iid)
    with pytest.raises(SecurityPolicyViolationException):
        InvestigationScope.create(tenant_id=TENANT, investigation_id="not-a-uuid")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_require_investigation_scope_checks():
    iid = uuid.uuid4()
    repo = _InvestigationRepo({(TENANT, iid): _investigation(iid)})
    inv = await require_investigation(repo, TENANT, iid)  # type: ignore[arg-type]
    assert inv.application_id == APP_ID
    with pytest.raises(SecurityPolicyViolationException):
        await require_investigation(repo, TENANT, uuid.uuid4())  # type: ignore[arg-type]
    foreign = _investigation(iid, tenant="other")
    repo2 = _InvestigationRepo({(TENANT, iid): foreign})
    with pytest.raises(SecurityPolicyViolationException):
        await require_investigation(repo2, TENANT, iid)  # type: ignore[arg-type]


# --- RuntimeEvidenceService -----------------------------------------------------------------


def _search_services(iid, items=None, has_more=False, profile=None):  # type: ignore[no-untyped-def]
    items = items if items is not None else []
    gateway = _Gateway(EvidenceQueryResult(items=items, has_more=has_more, total_count=len(items)))
    inv_repo = _InvestigationRepo({(TENANT, iid): _investigation(iid)})
    ev_repo = _EvidenceRepo()
    prof_repo = _ProfileRepo(profile or _profile())
    service = RuntimeEvidenceService(
        gateway=gateway,  # type: ignore[arg-type]
        investigation_repo=inv_repo,  # type: ignore[arg-type]
        evidence_repo=ev_repo,  # type: ignore[arg-type]
        profile_repo=prof_repo,  # type: ignore[arg-type]
    )
    return service, gateway, inv_repo, ev_repo


@pytest.mark.asyncio
async def test_search_merges_trusted_context_and_persists():
    iid = uuid.uuid4()
    items = [_evidence(iid), _evidence(iid)]
    service, gateway, _, ev_repo = _search_services(iid, items=items)
    result = await service.search(_scope(iid), _request())
    (tenant, inv, app, req) = gateway.calls[0]
    assert (tenant, inv, app) == (TENANT, iid, APP_ID)
    assert isinstance(req, RuntimeEvidenceRequest)
    assert len(result.items) == 2
    assert result.has_more is False and result.completeness.complete is True
    (saved_tenant, saved_items, saved_iid) = ev_repo.saved[0]
    assert saved_tenant == TENANT and saved_iid == iid and len(saved_items) == 2


@pytest.mark.asyncio
async def test_search_completeness_pagination_and_partial():
    iid = uuid.uuid4()
    service, _, _, _ = _search_services(iid, items=[_evidence(iid)], has_more=True)
    result = await service.search(_scope(iid), _request())
    assert result.completeness.complete is False
    assert result.completeness.reason == "pagination_available"
    assert result.next_cursor is None

    partial_result = EvidenceQueryResult(
        items=[_evidence(iid)],
        has_more=False,
        total_count=1,
        execution_metadata={
            "execution_time_ms": 0.0,
            "partial_result": True,
            "errors": [{"code": "MALFORMED_PROVIDER_RECORD"}],
            "tokens_consumed": 0,
        },
    )
    gateway = _Gateway(partial_result)
    service2, _, _, _ = _search_services(iid)
    service2._gateway = gateway
    result2 = await service2.search(_scope(iid), _request())
    assert result2.completeness.complete is False
    assert result2.completeness.reason == "metadata_incomplete"


@pytest.mark.asyncio
async def test_search_rejects_unknown_investigation_without_provider_call():
    iid = uuid.uuid4()
    service, gateway, _, _ = _search_services(uuid.uuid4())
    with pytest.raises(SecurityPolicyViolationException):
        await service.search(_scope(iid), _request())
    assert gateway.calls == []


@pytest.mark.asyncio
async def test_search_requires_profile():
    iid = uuid.uuid4()
    gateway = _Gateway()
    service = RuntimeEvidenceService(
        gateway=gateway,  # type: ignore[arg-type]
        investigation_repo=_InvestigationRepo({(TENANT, iid): _investigation(iid)}),  # type: ignore[arg-type]
        evidence_repo=_EvidenceRepo(),  # type: ignore[arg-type]
        profile_repo=_ProfileRepo(None),  # type: ignore[arg-type]
    )
    with pytest.raises(ApplicationProfileNotFoundException):
        await service.search(_scope(iid), _request())
    assert gateway.calls == []


def test_from_query_result_next_cursor_mapping():
    result = EvidenceQueryResult(items=[], cursor="tok", has_more=True, total_count=0)
    mapped = RuntimeEvidenceSearchResult.from_query_result(result)
    assert mapped.next_cursor == "tok" and mapped.has_more is True


# --- EvidenceDetailService ---------------------------------------------------------------------


def _detail_services(iid, mapping=None, fresh=None, profile=None):  # type: ignore[no-untyped-def]
    provider = _Provider(evidence=fresh)
    selector = _Selector(provider)
    inv_repo = _InvestigationRepo({(TENANT, iid): _investigation(iid)})
    ev_repo = _EvidenceRepo()
    if mapping is not None:
        ev_repo._rows[(TENANT, mapping.evidence_id)] = mapping
    sanitizer = _Sanitizer()
    service = EvidenceDetailService(
        selector=selector,  # type: ignore[arg-type]
        investigation_repo=inv_repo,  # type: ignore[arg-type]
        evidence_repo=ev_repo,  # type: ignore[arg-type]
        profile_repo=_ProfileRepo(profile or _profile()),  # type: ignore[arg-type]
        sanitizer=sanitizer,  # type: ignore[arg-type]
    )
    return service, provider, ev_repo, sanitizer, selector


@pytest.mark.asyncio
async def test_get_returns_sanitized_fresh_evidence():
    iid = uuid.uuid4()
    eid = uuid.uuid4()
    stored = _evidence(iid, eid)
    fresh = _evidence(iid, uuid.uuid4())
    service, provider, _, sanitizer, selector = _detail_services(iid, mapping=stored, fresh=fresh)
    scope = _scope(iid)
    result = await service.get(scope, eid)
    assert result is fresh
    assert sanitizer.calls and sanitizer.calls[0][1] is fresh
    (tenant, inv, got_eid, env, corr, mapping_id) = provider.get_calls[0]
    assert (tenant, inv, got_eid, env) == (TENANT, iid, eid, "production")
    assert corr == scope.correlation_id
    assert mapping_id is None
    assert selector.calls[0] == (TENANT, APP_ID, "production")


@pytest.mark.asyncio
async def test_get_unknown_or_foreign_evidence_not_found_without_provider_call():
    iid = uuid.uuid4()
    service, provider, _, _, _ = _detail_services(iid, mapping=None, fresh=_evidence(iid))
    with pytest.raises(EvidenceNotFoundException):
        await service.get(_scope(iid), uuid.uuid4())
    foreign = _evidence(UUID(int=9), uuid.uuid4(), tenant="other-tenant")
    service2, provider2, _, _, _ = _detail_services(iid, mapping=foreign, fresh=_evidence(iid))
    with pytest.raises(EvidenceNotFoundException):
        await service2.get(_scope(iid), foreign.evidence_id)
    assert provider.get_calls == [] and provider2.get_calls == []


@pytest.mark.asyncio
async def test_get_rejects_unknown_investigation():
    iid = uuid.uuid4()
    service, provider, _, _, _ = _detail_services(
        uuid.uuid4(), mapping=_evidence(iid), fresh=_evidence(iid)
    )
    with pytest.raises(SecurityPolicyViolationException):
        await service.get(_scope(iid), uuid.uuid4())
    assert provider.get_calls == []


# --- TraceInvestigationService ----------------------------------------------------------------------


def _trace_services(iid, items=None, profile=None, has_more=False):  # type: ignore[no-untyped-def]
    from investigation_agent_platform.application.investigation.trace_investigation import (
        TraceInvestigationService as Svc,
    )
    from investigation_agent_platform.infrastructure.evidence.logs.trace_telemetry import (
        TraceTelemetryExtractor,
    )

    items = items if items is not None else []
    gateway = _Gateway(EvidenceQueryResult(items=items, has_more=has_more, total_count=len(items)))
    ev_repo = _EvidenceRepo()
    service = Svc(
        gateway=gateway,  # type: ignore[arg-type]
        investigation_repo=_InvestigationRepo({(TENANT, iid): _investigation(iid)}),  # type: ignore[arg-type]
        evidence_repo=ev_repo,  # type: ignore[arg-type]
        profile_repo=_ProfileRepo(profile or _profile()),  # type: ignore[arg-type]
        derivation=TraceTelemetryExtractor(),
    )
    return service, gateway, ev_repo


@pytest.mark.asyncio
async def test_investigate_trace_delegates_with_policy_budget():
    from investigation_agent_platform.domain.evidence.requests import TraceTelemetryRequest

    iid = uuid.uuid4()
    eid = uuid.uuid4()
    item = _evidence(
        iid,
        eid,
        TENANT,
        {
            "code": {"function": "f", "lineno": 1},
            "db": {"statement": "SELECT 1 FROM t"},
            "labels": {"trace_id": "abc"},
        },
    )
    service, gateway, ev_repo = _trace_services(iid, items=[item], profile=_profile(max_evidence=25))
    request = TraceTelemetryRequest(trace_id="abc", environment="production", max_evidence=100)
    result = await service.investigate_trace(_scope(iid), request)
    assert result.telemetry.trace_id == "abc"
    assert result.telemetry.completeness.complete is True
    # Profile policy (25) caps the agent-supplied budget (100).
    (_, _, _, gateway_request) = gateway.calls[0]
    assert gateway_request.limit == 25
    assert gateway_request.identifiers == {"trace_id": "abc"}
    # Examined items persist for the get_evidence round-trip...
    assert ev_repo.saved and len(ev_repo.saved[0][1]) == 1
    # ...and contributing records are referenced with summaries.
    assert [(ref.evidence_id, ref.summary) for ref in result.evidence] == [(eid, "s")]


@pytest.mark.asyncio
async def test_investigate_trace_truncation_reason():
    from investigation_agent_platform.domain.evidence.requests import TraceTelemetryRequest

    iid = uuid.uuid4()
    items = [_evidence(iid, None, TENANT, {"message": f"m{i}"}) for i in range(10)]
    service, _, _ = _trace_services(iid, items=items, has_more=True)
    request = TraceTelemetryRequest(trace_id="abc", environment="production", max_evidence=10)
    result = await service.investigate_trace(_scope(iid), request)
    assert result.telemetry.completeness.complete is False
    assert result.telemetry.completeness.reason == "application_limit"


@pytest.mark.asyncio
async def test_investigate_trace_rejects_unknown_investigation():
    from investigation_agent_platform.application.investigation.trace_investigation import (
        TraceInvestigationService as Svc,
    )
    from investigation_agent_platform.domain.evidence.requests import TraceTelemetryRequest
    from investigation_agent_platform.infrastructure.evidence.logs.trace_telemetry import (
        TraceTelemetryExtractor,
    )

    gateway = _Gateway()
    service = Svc(
        gateway=gateway,  # type: ignore[arg-type]
        investigation_repo=_InvestigationRepo({}),  # type: ignore[arg-type]
        evidence_repo=_EvidenceRepo(),  # type: ignore[arg-type]
        profile_repo=_ProfileRepo(_profile()),  # type: ignore[arg-type]
        derivation=TraceTelemetryExtractor(),
    )
    with pytest.raises(SecurityPolicyViolationException):
        await service.investigate_trace(
            _scope(uuid.uuid4()),
            TraceTelemetryRequest(trace_id="abc", environment="production"),
        )
    assert gateway.calls == []


# --- Composition + import hygiene ----------------------------------------------------------------------


def test_build_investigation_services():
    services = build_investigation_services(
        gateway=MagicMock(),
        selector=MagicMock(),
        investigation_repo=MagicMock(),
        evidence_repo=MagicMock(),
        profile_repo=MagicMock(),
        sanitizer=MagicMock(),
        derivation=MagicMock(),
    )
    assert isinstance(services, InvestigationServices)
    assert isinstance(services.runtime_evidence, RuntimeEvidenceService)
    assert isinstance(services.evidence_detail, EvidenceDetailService)
    assert isinstance(services.trace_investigation, TraceInvestigationService)


def test_application_services_do_not_import_infrastructure():
    """Application services depend on ports/domain only (import hygiene)."""
    import ast
    import pathlib

    base = pathlib.Path(__file__).resolve().parents[2]
    modules = [
        "src/investigation_agent_platform/application/investigation/context.py",
        "src/investigation_agent_platform/application/investigation/runtime_evidence.py",
        "src/investigation_agent_platform/application/investigation/evidence_detail.py",
        "src/investigation_agent_platform/application/investigation/trace_investigation.py",
        "src/investigation_agent_platform/application/investigation/composition.py",
    ]
    for relative in modules:
        tree = ast.parse((base / relative).read_text(encoding="utf-8"), filename=relative)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        assert not any(
            name.startswith("investigation_agent_platform.infrastructure")
            or name == "mcp"
            or name.startswith("mcp.")
            for name in imported
        ), f"{relative}: {sorted(imported)}"
        assert "elasticsearch" not in imported, relative


# --- Gateway-path wiring fixes ----------------------------------------------------------------------------


def _protocol_provider():  # type: ignore[no-untyped-def]
    from types import SimpleNamespace

    async def _search(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("must not be called")

    async def _get(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("must not be called")

    return SimpleNamespace(search_runtime_evidence=_search, get_runtime_evidence=_get)


def test_selector_default_runtime_fallback():
    import asyncio as _asyncio

    from investigation_agent_platform.application.evidence.selector import (
        EvidenceProviderSelector,
    )
    from investigation_agent_platform.domain.common.exceptions import ExecutionError

    provider = _protocol_provider()
    selector = EvidenceProviderSelector()
    selector.register_runtime_provider("elastic-primary", provider, default=True)
    selector.freeze()
    resolved = _asyncio.run(selector.get_runtime_provider("t", "app", "production"))
    assert resolved is provider

    selector2 = EvidenceProviderSelector()
    selector2.register_runtime_provider("elastic-primary", provider)
    selector2.freeze()
    with pytest.raises(ExecutionError):
        _asyncio.run(selector2.get_runtime_provider("t", "app", "production"))


def test_adapter_exposes_optional_profile():
    from investigation_agent_platform.infrastructure.evidence.runtime.elastic import (
        AsyncElasticAdapter,
        ElasticAdapterSettings,
    )

    adapter = AsyncElasticAdapter(
        client=MagicMock(), settings=ElasticAdapterSettings(cursor_signing_key=b"k")
    )
    assert adapter.profile is None
