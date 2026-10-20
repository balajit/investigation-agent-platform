# tests/unit/test_code_recall_gateway.py
"""Steps 4-5: evidence-gateway fuzzy-fallback wiring (D4/D5).

Deterministic recall → miss classification → capped fallback → authority
verification → verified IDs as range-read targets, strictly behind
``IAP_COGNEE_ENABLED`` (default off). The search callable is injected, so no
``cognee`` package is needed; fuzzy output is never banked as Evidence rows.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from investigation_agent_platform.application.evidence.code_recall import (
    FALLBACK_CANDIDATE_CAP,
    FALLBACK_RUN_CAP,
    CodeRecallMiss,
)
from investigation_agent_platform.domain.common.exceptions import (
    TopologyAmbiguousMatchError,
)
from investigation_agent_platform.domain.evidence.requests import SymbolRequest


def _node(**overrides: object) -> SimpleNamespace:
    fields: dict[str, object] = {
        "tenant_id": "tenant-a",
        "repository_id": "acme--pay",
        "revision": "rev-1",
        "granularity": "macro",
        "node_id": str(uuid.uuid4()),
        "qualified_name": "svc::Handler::charge",
        "file_path": "src/svc/handler.py",
        "start_line": 10,
        "end_line": 40,
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


class _Metrics:
    """Fake telemetry port capturing record_metric calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, float, dict[str, str]]] = []

    def record_metric(
        self, metric_name: str, value: float, tags: dict[str, str]
    ) -> None:
        self.calls.append((metric_name, float(value), dict(tags)))

    def names(self) -> list[str]:
        return [name for name, _, _ in self.calls]

    def by_name(self, name: str) -> list[tuple[float, dict[str, str]]]:
        return [(value, tags) for n, value, tags in self.calls if n == name]


def _symbol(qualified_name: str = "svc::Handler") -> MagicMock:
    symbol = MagicMock()
    symbol.model_dump = MagicMock(return_value={"qualified_name": qualified_name})
    return symbol


def _gateway(
    *,
    find_symbol_result: Any = None,
    find_symbol_error: Exception | None = None,
    search_rows: list[dict[str, Any]] | None = None,
    authority: dict[str, Any] | None = None,
    telemetry: _Metrics | None = None,
) -> tuple[Any, _Metrics, list[dict[str, Any]]]:
    from investigation_agent_platform.application.evidence.gateway import (
        AsyncEvidenceGateway,
    )

    search_calls: list[dict[str, Any]] = []

    async def _search_fn(**kwargs: Any) -> list[dict[str, Any]]:
        search_calls.append(dict(kwargs))
        return list(search_rows or [])

    async def _authority_fn(
        tenant_id: str, repository_id: str, revision: str
    ) -> dict[str, Any]:
        assert tenant_id == "tenant-a"
        assert repository_id == "acme--pay"
        assert revision == "rev-1"
        return dict(authority or {})

    provider = MagicMock()
    provider.profile = SimpleNamespace(name="test-profile")
    if find_symbol_error is not None:
        provider.find_symbol = AsyncMock(side_effect=find_symbol_error)
    else:
        provider.find_symbol = AsyncMock(return_value=find_symbol_result or [])
    selector = MagicMock()
    selector.get_code_intelligence_provider = AsyncMock(return_value=provider)
    policy = MagicMock()
    policy.validate_symbol_request = AsyncMock(return_value=None)
    metrics = telemetry or _Metrics()
    gateway = AsyncEvidenceGateway(
        provider_selector=selector,
        query_safety_policy=policy,
        sanitizer=MagicMock(),
        telemetry=metrics,  # type: ignore[arg-type]
        fuzzy_search_fn=_search_fn,
        fuzzy_authority_fn=_authority_fn,
        fuzzy_dataset_fn=lambda t, r, v: "ds-1",
    )
    return gateway, metrics, search_calls


def _request(symbol: str = "charge") -> SymbolRequest:
    return SymbolRequest(repository="acme--pay", revision="rev-1", symbol_name=symbol)


@pytest.fixture
def flag_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("IAP_COGNEE_ENABLED", raising=False)


@pytest.fixture
def flag_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IAP_COGNEE_ENABLED", "true")


@pytest.mark.asyncio
async def test_flag_off_symbol_hit_is_byte_identical(
    flag_off: None,
) -> None:
    gateway, metrics, search_calls = _gateway(
        find_symbol_result=[_symbol("a::b"), _symbol("a::c")]
    )
    result = await gateway.find_symbol(
        "tenant-a", uuid.uuid4(), "app-1", _request()
    )
    assert result.items == []
    assert result.total_count == 2
    assert result.provider_metadata == {
        "symbols": [{"qualified_name": "a::b"}, {"qualified_name": "a::c"}]
    }
    assert search_calls == []
    assert metrics.calls == []


@pytest.mark.asyncio
async def test_flag_off_empty_miss_is_byte_identical(flag_off: None) -> None:
    gateway, metrics, search_calls = _gateway(find_symbol_result=[])
    result = await gateway.find_symbol(
        "tenant-a", uuid.uuid4(), "app-1", _request()
    )
    assert result.items == []
    assert result.total_count == 0
    assert result.provider_metadata == {"symbols": []}
    assert search_calls == []
    assert metrics.calls == []


@pytest.mark.asyncio
async def test_flag_off_abstention_reraises_without_search(
    flag_off: None,
) -> None:
    gateway, metrics, search_calls = _gateway(
        find_symbol_error=TopologyAmbiguousMatchError("2 overloads match")
    )
    with pytest.raises(TopologyAmbiguousMatchError):
        await gateway.find_symbol("tenant-a", uuid.uuid4(), "app-1", _request())
    assert search_calls == []
    assert metrics.calls == []


@pytest.mark.asyncio
async def test_flag_defaults_off_without_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("IAP_COGNEE_ENABLED", raising=False)
    gateway, metrics, search_calls = _gateway(find_symbol_result=[])
    result = await gateway.find_symbol(
        "tenant-a", uuid.uuid4(), "app-1", _request()
    )
    assert result.provider_metadata == {"symbols": []}
    assert search_calls == []


@pytest.mark.asyncio
async def test_flag_on_fallback_verifies_and_targets(flag_on: None) -> None:
    kept_id = str(uuid.uuid4())
    other_id = str(uuid.uuid4())
    kept_node = _node(node_id=kept_id)
    other_node = _node(node_id=other_id, qualified_name="svc::Other::refund")
    authority = {kept_id: kept_node, other_id: other_node}
    search_rows = [
        # Fuzzy payload junk (scores/snippets/summaries) must not survive.
        {"node_id": kept_id, "score": 0.99, "snippet": "def charge(): ...",
         "summary": "handles payments", "qualified_name": "WRONG::NAME"},
        {"node_id": other_id, "score": 0.5},
        {"node_id": "unmapped-ghost", "score": 0.9, "snippet": "x"},
        {"no_node_id": True, "score": 1.0},
    ]
    gateway, metrics, search_calls = _gateway(
        find_symbol_result=[], search_rows=search_rows, authority=authority
    )
    result = await gateway.find_symbol(
        "tenant-a", uuid.uuid4(), "app-1", _request()
    )
    # Never banked as Evidence rows.
    assert result.items == []
    metadata = result.provider_metadata
    assert metadata["code_recall_miss"] == CodeRecallMiss.ZERO_EXACT_HITS.value
    targets = metadata["fuzzy_verified_targets"]
    assert [(t["node_id"], t["file_path"]) for t in targets] == [
        (kept_id, "src/svc/handler.py"),
        (other_id, "src/svc/handler.py"),
    ]
    assert targets[0]["start_line"] == 10
    assert targets[0]["end_line"] == 40
    # Symbols come from the authority, never from fuzzy payloads.
    assert targets[0]["qualified_name"] == "svc::Handler::charge"
    orientation = metadata["fuzzy_orientation"]
    assert orientation["unverified"] is True
    assert orientation["miss"] == CodeRecallMiss.ZERO_EXACT_HITS.value
    assert orientation["suggested_symbols"] == [
        "svc::Handler::charge",
        "svc::Other::refund",
    ]
    # Only IDs crossed the boundary: no fuzzy text anywhere in the envelope.
    dumped = result.model_dump_json()
    assert "snippet" not in dumped
    assert "WRONG::NAME" not in dumped
    assert "handles payments" not in dumped
    assert "unmapped-ghost" not in dumped
    # D5 counters at each stage.
    assert metrics.by_name("iap.code_recall.miss") == [
        (1.0, {"miss": "ZERO_EXACT_HITS"})
    ]
    assert metrics.by_name("iap.code_recall.fallback.candidates_seen") == [
        (3.0, {"miss": "ZERO_EXACT_HITS"})
    ]
    assert metrics.by_name("iap.code_recall.fallback.verified_kept") == [
        (2.0, {"miss": "ZERO_EXACT_HITS"})
    ]
    assert metrics.by_name("iap.code_recall.fallback.discarded") == [
        (1.0, {"miss": "ZERO_EXACT_HITS"})
    ]
    search_types = metrics.by_name("iap.code_recall.fallback.search_types_used")
    assert len(search_types) == 1
    assert search_types[0][1]["search_types"] == "CODE"
    # Injected search callable received the retrieval-only call.
    assert len(search_calls) == 1
    assert search_calls[0]["search_type"] == "CODE"
    assert search_calls[0]["dataset"] == "ds-1"


@pytest.mark.asyncio
async def test_flag_on_overload_abstention_maps_to_miss(flag_on: None) -> None:
    kept_id = str(uuid.uuid4())
    authority = {kept_id: _node(node_id=kept_id)}
    gateway, metrics, search_calls = _gateway(
        find_symbol_error=TopologyAmbiguousMatchError("2 overloads match"),
        search_rows=[{"node_id": kept_id}],
        authority=authority,
    )
    # Flag-on: the abstention maps to a miss and flows through fallback
    # instead of raising — structural truth still vetoes (no guessed rows).
    result = await gateway.find_symbol(
        "tenant-a", uuid.uuid4(), "app-1", _request()
    )
    assert result.items == []
    assert result.provider_metadata["code_recall_miss"] == (
        CodeRecallMiss.AMBIGUOUS_OVERLOAD.value
    )
    assert len(result.provider_metadata["fuzzy_verified_targets"]) == 1
    miss_counters = metrics.by_name("iap.code_recall.miss")
    assert miss_counters == [(1.0, {"miss": "AMBIGUOUS_OVERLOAD"})]
    assert search_calls != []


def test_classifier_never_emits_overload() -> None:
    from investigation_agent_platform.application.evidence.code_recall import (
        classify_code_recall_miss,
    )

    for shape in ("code", "ticket", "unknown-shape", ""):
        for hits in (0, 1, 5):
            for candidates in (0, 1, 7):
                for problem in ("", "charge", "where is auth handled?", "overload foo"):
                    assert (
                        classify_code_recall_miss(shape, hits, candidates, problem)
                        is not CodeRecallMiss.AMBIGUOUS_OVERLOAD
                    )


@pytest.mark.asyncio
async def test_run_budget_exhaustion_counts_without_search(
    flag_on: None,
) -> None:
    kept_id = str(uuid.uuid4())
    authority = {kept_id: _node(node_id=kept_id)}
    gateway, metrics, search_calls = _gateway(
        find_symbol_result=[],
        search_rows=[{"node_id": kept_id}],
        authority=authority,
    )
    investigation_id = uuid.uuid4()
    for _ in range(FALLBACK_RUN_CAP):
        result = await gateway.find_symbol(
            "tenant-a", investigation_id, "app-1", _request()
        )
        assert "fuzzy_orientation" in result.provider_metadata
    assert len(search_calls) == FALLBACK_RUN_CAP
    exhausted = await gateway.find_symbol(
        "tenant-a", investigation_id, "app-1", _request()
    )
    # Exhausted: normal counted outcome — deterministic envelope, no search.
    assert len(search_calls) == FALLBACK_RUN_CAP
    assert "fuzzy_orientation" not in exhausted.provider_metadata
    assert exhausted.provider_metadata["code_recall_miss"] == "ZERO_EXACT_HITS"
    assert metrics.by_name(
        "iap.code_recall.fallback.run_budget_exhausted"
    ) == [(1.0, {"miss": "ZERO_EXACT_HITS"})]


@pytest.mark.asyncio
async def test_telescoping_caps_per_call_inside_per_run(
    flag_on: None,
) -> None:
    rows = [{"node_id": f"node-{i}"} for i in range(FALLBACK_CANDIDATE_CAP + 15)]
    gateway, metrics, search_calls = _gateway(
        find_symbol_result=[], search_rows=rows, authority={}
    )
    result = await gateway.find_symbol(
        "tenant-a", uuid.uuid4(), "app-1", _request()
    )
    # Per-call cap telescopes inside the per-run budget: 25 rows in, 10 seen,
    # all discarded against the empty authority — counted, never an error.
    assert result.provider_metadata["fuzzy_candidates_seen"] == FALLBACK_CANDIDATE_CAP
    assert result.provider_metadata["fuzzy_verified_targets"] == []
    assert result.items == []
    seen = metrics.by_name("iap.code_recall.fallback.candidates_seen")
    assert seen == [(float(FALLBACK_CANDIDATE_CAP), {"miss": "ZERO_EXACT_HITS"})]
    discarded = metrics.by_name("iap.code_recall.fallback.discarded")
    assert discarded == [(float(FALLBACK_CANDIDATE_CAP), {"miss": "ZERO_EXACT_HITS"})]


@pytest.mark.asyncio
async def test_fully_discarded_fallback_is_normal_outcome(
    flag_on: None,
) -> None:
    gateway, metrics, search_calls = _gateway(
        find_symbol_result=[],
        search_rows=[{"node_id": "ghost-1"}, {"node_id": "ghost-2"}],
        authority={},
    )
    result = await gateway.find_symbol(
        "tenant-a", uuid.uuid4(), "app-1", _request()
    )
    assert result.items == []
    assert result.provider_metadata["fuzzy_verified_targets"] == []
    orientation = result.provider_metadata["fuzzy_orientation"]
    assert orientation["unverified"] is True
    assert orientation["suggested_symbols"] == []
    assert metrics.by_name("iap.code_recall.fallback.discarded") == [
        (2.0, {"miss": "ZERO_EXACT_HITS"})
    ]
    assert metrics.by_name("iap.code_recall.fallback.verified_kept") == [
        (0.0, {"miss": "ZERO_EXACT_HITS"})
    ]


@pytest.mark.asyncio
async def test_flag_on_without_seams_degrades_to_deterministic(
    flag_on: None,
) -> None:
    from investigation_agent_platform.application.evidence.gateway import (
        AsyncEvidenceGateway,
    )

    provider = MagicMock()
    provider.profile = SimpleNamespace(name="test-profile")
    provider.find_symbol = AsyncMock(return_value=[])
    selector = MagicMock()
    selector.get_code_intelligence_provider = AsyncMock(return_value=provider)
    policy = MagicMock()
    policy.validate_symbol_request = AsyncMock(return_value=None)
    gateway = AsyncEvidenceGateway(
        provider_selector=selector,
        query_safety_policy=policy,
        sanitizer=MagicMock(),
    )
    result = await gateway.find_symbol(
        "tenant-a", uuid.uuid4(), "app-1", _request()
    )
    assert result.items == []
    assert result.total_count == 0
    assert result.provider_metadata["symbols"] == []
    assert result.provider_metadata["code_recall_miss"] == "ZERO_EXACT_HITS"
    assert "fuzzy_orientation" not in result.provider_metadata


def test_gateway_module_imports_no_cognee_package() -> None:
    from pathlib import Path

    import investigation_agent_platform.application.evidence.gateway as gw

    source = Path(gw.__file__).read_text(encoding="utf-8")
    assert "import cognee" not in source
    assert "from cognee" not in source
