# tests/unit/test_phase4_observability.py
"""Phase 4.2: dashboards query emitted series, not mocks.

The real OpenTelemetry adapter writes into an InMemoryMetricReader-backed
provider; assertions read exported series back (names + label sets +
counts). A separate wiring test proves activities reach the port.
"""

from __future__ import annotations

import contextlib
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest


def _reader_provider():
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    reader = InMemoryMetricReader()
    return MeterProvider(metric_readers=[reader]), reader


def _series(reader):
    data = reader.get_metrics_data()
    out = {}
    if data is None:
        return out
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for metric in sm.metrics:
                for point in metric.data.data_points:
                    key = (metric.name, tuple(sorted(dict(point.attributes or {}).items())))
                    out[key] = out.get(key, 0) + point.value
    return out


@contextlib.contextmanager
def _adapter_ctx(*, enabled=True):
    from unittest.mock import patch

    from opentelemetry import metrics

    from investigation_agent_platform.infrastructure.observability.telemetry import (
        OpenTelemetryObservabilityAdapter,
    )
    from investigation_agent_platform.ports.observability.telemetry import SpanContext

    provider, reader = _reader_provider()
    # Bind instruments to a reader-backed provider without touching the
    # process-global meter provider (set_meter_provider is do-once).
    with patch.object(metrics, "get_meter", provider.get_meter):
        adapter = OpenTelemetryObservabilityAdapter(
            service_name="test-phase4", otlp_endpoint="localhost:4317", enabled=enabled
        )
    sctx = SpanContext(tenant_id="t", investigation_id=uuid4())
    yield adapter, sctx, reader


class TestEmittedSeries:
    def test_provider_failure_separates_from_no_match(self):
        with _adapter_ctx() as (adapter, sctx, reader):
            adapter.record_code_action(sctx, "SEARCH_CODE", "DEGRADED", "timeout")
            adapter.record_code_action(sctx, "SEARCH_CODE", "DEGRADED", "timeout")
            adapter.record_code_action(sctx, "WALK_STACK", "EMPTY", "walk_stack_no_resolution")
        series = _series(reader)
        timeout_key = (
            "code.action.executions",
            (("action", "SEARCH_CODE"), ("miss_reason", "timeout"), ("outcome", "DEGRADED")),
        )
        nomatch_key = (
            "code.action.executions",
            (
                ("action", "WALK_STACK"),
                ("miss_reason", "walk_stack_no_resolution"),
                ("outcome", "EMPTY"),
            ),
        )
        assert series.get(timeout_key) == 2
        assert series.get(nomatch_key) == 1

    def test_verification_and_limitation_labels(self):
        with _adapter_ctx() as (adapter, sctx, reader):
            adapter.record_verification(sctx, "DEFINITION", "VERIFIED", "none")
            adapter.record_verification(sctx, "BEHAVIOR", "VERIFIED", "RIVAL_UNTESTED")
            adapter.record_conclusion_limitation(sctx, "RIVAL_UNTESTED")
            adapter.record_location_answer(sctx, True)
            adapter.record_location_answer(sctx, False)
            adapter.record_revision_check(sctx, "GET_CODE", True)
            adapter.record_revision_check(sctx, "GET_CODE", False)
            adapter.record_symbol_empty(sctx, "FIND_SYMBOL")
            adapter.record_replay_mismatch(sctx)
            adapter.record_code_packets(sctx, included=3, truncated=1, dropped=2)
        series = _series(reader)
        assert (
            series[
                (
                    "verification.decisions",
                    (("claim_kind", "DEFINITION"), ("limitation", "none"), ("status", "VERIFIED")),
                )
            ]
            == 1
        )
        assert series[("conclusion.limitations", (("kind", "RIVAL_UNTESTED"),))] == 1
        assert series[("verification.location_answers", (("has_untested_rival", "true"),))] == 1
        assert series[("verification.location_answers", (("has_untested_rival", "false"),))] == 1
        assert series[("code.revision.checks", (("match", "true"), ("operation", "GET_CODE")))] == 1
        assert series[("code.symbol.empty", (("tool", "FIND_SYMBOL"),))] == 1
        assert series[("replay.outcome.mismatch", ())] == 1
        assert series[("prompt.code_packets", (("disposition", "included"),))] == 3
        assert series[("prompt.code_packets", (("disposition", "truncated"),))] == 1
        assert series[("prompt.code_packets", (("disposition", "dropped"),))] == 2

    def test_disabled_adapter_emits_nothing(self):
        with _adapter_ctx(enabled=False) as (adapter, sctx, reader):
            adapter.record_code_action(sctx, "SEARCH_CODE", "DEGRADED", "timeout")
            adapter.record_verification(sctx, "DEFINITION", "VERIFIED", "none")
        assert _series(reader) == {}

    def test_label_values_are_bounded(self):
        # File paths, ranges, and SHAs must never become label values.
        with _adapter_ctx() as (adapter, sctx, reader):
            adapter.record_code_action(sctx, "SEARCH_CODE", "DEGRADED", "timeout")
        series = _series(reader)
        blob = str(series)
        assert (
            "deadbeef" not in blob
            and ".java" not in blob
            and ":" not in str([k for k in series]).replace("code.action.executions", "")
        )


def _ctx_with(gateway):
    from investigation_agent_platform.api.dependencies import AppContext, set_app_context

    ctx = AppContext()
    saved = []

    async def _save_batch(tenant_id, items, investigation_id):  # type: ignore[no-untyped-def]
        saved.extend(items)

    service = SimpleNamespace(
        authorize=AsyncMock(return_value=None),
        execute=AsyncMock(return_value=None),
    )
    ctx.execute_action_service = lambda: service  # type: ignore[method-assign]
    ctx.action_repo = SimpleNamespace(count_by_investigation=AsyncMock(return_value=0))
    ctx.evidence_repo = SimpleNamespace(
        find_by_investigation_id=AsyncMock(return_value=[]),
        save_batch=_save_batch,
        save=AsyncMock(),
    )
    ctx.profile_repo = SimpleNamespace(get_by_application_id=AsyncMock(return_value=None))
    ctx.evidence_gateway = gateway
    set_app_context(ctx)
    return ctx, saved


class TestHookWiring:
    @pytest.mark.asyncio
    async def test_code_action_reaches_port(self):
        from investigation_agent_platform.application.worker.activities import (
            ExecuteActionInput,
            execute_action_activity,
        )
        from investigation_agent_platform.domain.common.exceptions import (
            ProviderTimeoutException,
        )

        calls = []

        class _FakeObs:
            def __getattr__(self, name):
                def _rec(*args):
                    calls.append((name, args))

                return _rec

        async def _search_code(tenant_id, investigation_id, application_id, req):  # type: ignore[no-untyped-def]
            raise ProviderTimeoutException("timed out")

        ctx, _ = _ctx_with(SimpleNamespace(search_code=_search_code))
        ctx.observability = _FakeObs()  # type: ignore[attr-defined]
        await execute_action_activity(
            ExecuteActionInput(
                tenant_id="tenant-a",
                investigation_id=str(uuid4()),
                action="SEARCH_CODE",
                parameters={"query": "price"},
            )
        )
        code_calls = [c for c in calls if c[0] == "record_code_action"]
        assert len(code_calls) == 1
        assert code_calls[0][1][1:] == ("SEARCH_CODE", "DEGRADED", "timeout")

    @pytest.mark.asyncio
    async def test_symbol_empty_reaches_port(self):
        from investigation_agent_platform.application.worker.activities import (
            ExecuteActionInput,
            execute_action_activity,
        )

        calls = []

        class _FakeObs:
            def __getattr__(self, name):
                def _rec(*args):
                    calls.append((name, args))

                return _rec

        async def _find_symbol(tenant_id, investigation_id, application_id, req):  # type: ignore[no-untyped-def]
            return SimpleNamespace(items=[], total_count=0, provider_metadata={"symbols": []})

        ctx, _ = _ctx_with(SimpleNamespace(find_symbol=_find_symbol))
        ctx.observability = _FakeObs()  # type: ignore[attr-defined]
        await execute_action_activity(
            ExecuteActionInput(
                tenant_id="tenant-a",
                investigation_id=str(uuid4()),
                action="FIND_SYMBOL",
                parameters={"symbol_name": "price"},
            )
        )
        assert any(c[0] == "record_symbol_empty" and c[1][1] == "FIND_SYMBOL" for c in calls)

    @pytest.mark.asyncio
    async def test_no_observability_is_silent(self):
        from investigation_agent_platform.application.worker.activities import (
            ExecuteActionInput,
            execute_action_activity,
        )
        from investigation_agent_platform.domain.common.exceptions import (
            ProviderTimeoutException,
        )

        async def _search_code(tenant_id, investigation_id, application_id, req):  # type: ignore[no-untyped-def]
            raise ProviderTimeoutException("timed out")

        _ctx_with(SimpleNamespace(search_code=_search_code))
        result = await execute_action_activity(
            ExecuteActionInput(
                tenant_id="tenant-a",
                investigation_id=str(uuid4()),
                action="SEARCH_CODE",
                parameters={"query": "price"},
            )
        )
        assert result.data["items_count"] == 0
