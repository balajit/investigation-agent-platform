# tests/unit/test_part10_phase7.py
"""Part 10 Phase 7: service environment consistency, mapping stamping, bootstrap wiring."""

import logging
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from investigation_agent_platform.application.investigation.context import InvestigationScope
from investigation_agent_platform.application.investigation.runtime_evidence import (
    RuntimeEvidenceService,
)
from investigation_agent_platform.application.investigation.trace_investigation import (
    TraceInvestigationService,
)
from investigation_agent_platform.domain.common.exceptions import DomainValidationException
from investigation_agent_platform.domain.evidence.requests import (
    RuntimeEvidenceRequest,
    TimeRange,
    TraceTelemetryRequest,
)
from investigation_agent_platform.ports.evidence.gateway import EvidenceQueryResult

TENANT = "tenant-a"
APP_ID = "checkout-app"


def _scope(iid=None):  # type: ignore[no-untyped-def]
    return InvestigationScope.create(tenant_id=TENANT, investigation_id=iid or uuid.uuid4())


def _investigation(iid, app_id=APP_ID):  # type: ignore[no-untyped-def]
    return SimpleNamespace(id=iid, tenant_id=TENANT, application_id=app_id)


def _profile(environment="production", mapping_source_id=None):  # type: ignore[no-untyped-def]
    return SimpleNamespace(
        environment=environment,
        observability_configuration=SimpleNamespace(mapping_source_id=mapping_source_id),
        investigation_configuration=SimpleNamespace(max_evidence_per_query=100),
    )


class _Repos:
    def __init__(self, iid, profile):  # type: ignore[no-untyped-def]
        self._iid = iid
        self._profile = profile
        self.saved = []

    async def get_by_id(self, tenant_id, investigation_id):  # type: ignore[no-untyped-def]
        if (tenant_id, investigation_id) == (TENANT, self._iid):
            return _investigation(self._iid)
        return None

    async def get_by_application_id(self, tenant_id, application_id, version=None):  # type: ignore[no-untyped-def]
        return self._profile

    async def save_batch(self, tenant_id, evidence_list, investigation_id=None):  # type: ignore[no-untyped-def]
        self.saved.append(list(evidence_list))


def _request(environment="production"):  # type: ignore[no-untyped-def]
    now = datetime.now(UTC)
    return RuntimeEvidenceRequest(
        environment=environment,
        time_range=TimeRange(start_time=now - timedelta(minutes=5), end_time=now),
        limit=10,
    )


def _gateway():  # type: ignore[no-untyped-def]
    gateway = MagicMock()
    gateway.search_runtime_evidence = AsyncMock(
        return_value=EvidenceQueryResult(items=[], has_more=False, total_count=0)
    )
    return gateway


@pytest.mark.asyncio
async def test_search_rejects_environment_mismatch_before_provider_call():
    iid = uuid.uuid4()
    gateway = _gateway()
    service = RuntimeEvidenceService(
        gateway=gateway,  # type: ignore[arg-type]
        investigation_repo=_Repos(iid, _profile("production")),  # type: ignore[arg-type]
        evidence_repo=_Repos(iid, _profile("production")),  # type: ignore[arg-type]
        profile_repo=_Repos(iid, _profile("production")),  # type: ignore[arg-type]
    )
    with pytest.raises(DomainValidationException):
        await service.search(_scope(iid), _request(environment="staging"))
    gateway.search_runtime_evidence.assert_not_called()


@pytest.mark.asyncio
async def test_search_stamps_mapping_source_id():
    iid = uuid.uuid4()
    gateway = _gateway()
    service = RuntimeEvidenceService(
        gateway=gateway,  # type: ignore[arg-type]
        investigation_repo=_Repos(iid, _profile()),  # type: ignore[arg-type]
        evidence_repo=_Repos(iid, _profile()),  # type: ignore[arg-type]
        profile_repo=_Repos(iid, _profile(mapping_source_id="gen-verbose")),  # type: ignore[arg-type]
    )
    await service.search(_scope(iid), _request())
    (_, _, _, forwarded) = gateway.search_runtime_evidence.call_args.args
    assert forwarded.mapping_source_id == "gen-verbose"
    assert forwarded.environment == "production"


@pytest.mark.asyncio
async def test_trace_rejects_environment_mismatch_and_stamps_mapping():
    from pathlib import Path

    from investigation_agent_platform.infrastructure.configuration.mapping_registry import (
        load_mapping_profiles,
    )
    from investigation_agent_platform.infrastructure.evidence.logs.trace_telemetry import (
        TraceTelemetryExtractor as RealExtractor,
    )

    shipped = Path(__file__).resolve().parents[2] / "config" / "observability-mappings"
    derivation = RealExtractor(mapping_registry=load_mapping_profiles(shipped))
    iid = uuid.uuid4()
    gateway = _gateway()
    profile = _profile(mapping_source_id="gen-verbose")
    service = TraceInvestigationService(
        gateway=gateway,  # type: ignore[arg-type]
        investigation_repo=_Repos(iid, profile),  # type: ignore[arg-type]
        evidence_repo=_Repos(iid, profile),  # type: ignore[arg-type]
        profile_repo=_Repos(iid, profile),  # type: ignore[arg-type]
        derivation=derivation,  # type: ignore[arg-type]
    )
    bad = TraceTelemetryRequest(trace_id="abc", environment="staging", max_evidence=10)
    with pytest.raises(DomainValidationException):
        await service.investigate_trace(_scope(iid), bad)
    gateway.search_runtime_evidence.assert_not_called()

    good = TraceTelemetryRequest(trace_id="abc", environment="production", max_evidence=10)
    result = await service.investigate_trace(_scope(iid), good)
    (_, _, _, forwarded) = gateway.search_runtime_evidence.call_args.args
    assert forwarded.mapping_source_id == "gen-verbose"
    assert result.telemetry.code_location_supported is False
    assert result.telemetry.sql_supported is False


def test_bootstrap_wires_registry_and_logs_none_required(tmp_path, caplog):
    from pydantic import SecretStr

    from investigation_agent_platform.api.dependencies import AppContext
    from investigation_agent_platform.bootstrap import _wire_evidence_gateway
    from investigation_agent_platform.infrastructure.configuration.config import (
        ApplicationConfig,
        BudgetConfig,
        DatabaseConfig,
        EvidenceConfig,
        LLMConfig,
        TelemetryConfig,
    )

    (tmp_path / "gen.yaml").write_text(
        yaml.safe_dump(
            {
                "source_id": "gen",
                "index_patterns": ["logz-gen-es-*"],
                "tenant_scope": {"strategy": "none_required"},
                "timestamp": {"candidates": ["@timestamp"], "value_type": "date_iso"},
                "severity": {"strategy": "unsupported"},
                "top_level_allowlist": ["@timestamp", "message"],
            }
        ),
        encoding="utf-8",
    )
    config = ApplicationConfig(
        environment="test",
        application_id="test",
        database=DatabaseConfig(connection_uri=SecretStr("postgresql://a:b@localhost/db")),
        llm=LLMConfig(api_key=SecretStr("sk"), model_name="gpt-4o"),
        budget=BudgetConfig(),
        telemetry=TelemetryConfig(enabled=False),
        evidence=EvidenceConfig(
            elasticsearch_url="http://localhost:9200",
            cursor_signing_key=SecretStr("test-signing-key"),
            mapping_profiles_path=str(tmp_path),
        ),
    )
    ctx = AppContext()
    with caplog.at_level(logging.WARNING):
        _wire_evidence_gateway(ctx, config)
    assert ctx.evidence_gateway is not None
    assert ctx.investigation_services is not None
    adapter = ctx.evidence_gateway._provider_selector._runtime_providers["elastic-primary"]
    assert adapter._mapping_registry is not None
    assert adapter._mapping_registry.resolve("gen").source_id == "gen"
    warnings = [record for record in caplog.records if "no tenant isolation" in record.message]
    assert warnings, "NONE_REQUIRED sources must log distinctly at startup"
