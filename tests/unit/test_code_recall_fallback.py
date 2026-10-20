# tests/unit/test_code_recall_fallback.py
"""D1-D3 unit tests: miss taxonomy, retrieval-only fallback, authority verification.

Pure logic: no `cognee` package required. The search callable is injected, and
the retrieval-only guard is exercised before any injected call runs.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import pytest

from investigation_agent_platform.application.evidence.code_recall import (
    FALLBACK_CANDIDATE_CAP,
    FALLBACK_RUN_CAP,
    FALLBACK_SEARCH_ORDER,
    CodeRecallMiss,
    classify_code_recall_miss,
    fuzzy_fallback_candidates,
    verify_fuzzy_candidates,
)
from investigation_agent_platform.domain.common.exceptions import TopologyNotConfiguredError
from investigation_agent_platform.infrastructure.topology.cognee_adapter import (
    assert_retrieval_only,
)


@dataclass
class _Node:
    tenant_id: str = "tenant-a"
    repository_id: str = "acme--pay"
    revision: str = "rev-1"
    granularity: str = "macro"


def _authority(node_id: str, **overrides: object) -> dict[str, object]:
    fields: dict[str, object] = {
        "tenant_id": "tenant-a",
        "repository_id": "acme--pay",
        "revision": "rev-1",
        "granularity": "macro",
    }
    fields.update(overrides)
    return {node_id: _Node(**fields)}  # type: ignore[arg-type]


# --- D1: taxonomy classes ---


def test_taxonomy_closed_enum() -> None:
    assert {m.value for m in CodeRecallMiss} == {
        "NO_MISS",
        "ZERO_EXACT_HITS",
        "AMBIGUOUS_SYMBOL",
        "AMBIGUOUS_OVERLOAD",
        "CONCEPT_LOCATION_NO_ANCHOR",
    }


def test_exact_hits_suppress_fallback_even_for_concept_questions() -> None:
    assert (
        classify_code_recall_miss(
            question_shape="code",
            exact_hits=2,
            candidate_count=0,
            problem="where is auth handled?",
        )
        is CodeRecallMiss.NO_MISS
    )


def test_zero_exact_hits() -> None:
    assert (
        classify_code_recall_miss(
            question_shape="code", exact_hits=0, candidate_count=0, problem="charge"
        )
        is CodeRecallMiss.ZERO_EXACT_HITS
    )


def test_ambiguous_symbol_beats_zero_hits() -> None:
    assert (
        classify_code_recall_miss(
            question_shape="code", exact_hits=0, candidate_count=3, problem="charge"
        )
        is CodeRecallMiss.AMBIGUOUS_SYMBOL
    )


@pytest.mark.parametrize(
    "problem",
    [
        "Where is auth handled?",
        "which service handles refunds",
        "who owns payments",
        "handles rate limiting somewhere?",
    ],
)
def test_concept_location_no_anchor(problem: str) -> None:
    assert (
        classify_code_recall_miss(
            question_shape="code", exact_hits=0, candidate_count=0, problem=problem
        )
        is CodeRecallMiss.CONCEPT_LOCATION_NO_ANCHOR
    )


def test_concept_markers_require_code_shape() -> None:
    assert (
        classify_code_recall_miss(
            question_shape="ticket",
            exact_hits=0,
            candidate_count=0,
            problem="where is auth handled?",
        )
        is CodeRecallMiss.ZERO_EXACT_HITS
    )


def test_outside_taxonomy_never_escalates() -> None:
    assert (
        classify_code_recall_miss(
            question_shape="unknown-shape",
            exact_hits=0,
            candidate_count=0,
            problem="something vague",
        )
        is CodeRecallMiss.ZERO_EXACT_HITS
    )
    assert (
        classify_code_recall_miss(
            question_shape="unknown-shape",
            exact_hits=1,
            candidate_count=5,
            problem="something vague",
        )
        is CodeRecallMiss.NO_MISS
    )


def test_empty_problem_does_not_raise() -> None:
    assert (
        classify_code_recall_miss(
            question_shape="code", exact_hits=0, candidate_count=0, problem=""
        )
        is CodeRecallMiss.ZERO_EXACT_HITS
    )


# --- D2: fallback guards ---


def test_fallback_order_excludes_cypher_and_completion() -> None:
    assert "CYPHER" not in FALLBACK_SEARCH_ORDER
    assert FALLBACK_SEARCH_ORDER == ("CODE", "CHUNKS_LEXICAL", "CHUNKS", "SUMMARIES")
    for search_type in FALLBACK_SEARCH_ORDER:
        assert_retrieval_only(search_type)


def test_completion_types_rejected() -> None:
    with pytest.raises(TopologyNotConfiguredError):
        assert_retrieval_only("CHUNKS_COMPLETION")
    with pytest.raises(TopologyNotConfiguredError):
        assert_retrieval_only("CODE_COMPLETION")


@pytest.mark.asyncio
async def test_completion_in_order_rejected_before_any_call() -> None:
    import investigation_agent_platform.application.evidence.code_recall as cr

    calls: list[str] = []

    async def _search_fn(**kwargs: object) -> list[dict[str, object]]:
        calls.append(str(kwargs.get("search_type")))
        return [{"node_id": "x"}]

    monkey_order = ("CODE_COMPLETION",)
    original = cr.FALLBACK_SEARCH_ORDER
    cr.FALLBACK_SEARCH_ORDER = monkey_order  # type: ignore[assignment]
    try:
        with pytest.raises(TopologyNotConfiguredError):
            await fuzzy_fallback_candidates(
                query_text="q", dataset="d", run_used=0, search_fn=_search_fn
            )
    finally:
        cr.FALLBACK_SEARCH_ORDER = original
    assert calls == []


@pytest.mark.asyncio
async def test_run_budget_exhaustion_returns_empty_without_calling_search() -> None:
    calls: list[dict[str, object]] = []

    async def _search_fn(**kwargs: object) -> list[dict[str, object]]:
        calls.append(dict(kwargs))
        return [{"node_id": "abc"}]

    outcome = await fuzzy_fallback_candidates(
        query_text="q", dataset="d", run_used=FALLBACK_RUN_CAP, search_fn=_search_fn
    )
    assert outcome.candidates_seen == 0
    assert outcome.candidate_ids == []
    assert outcome.search_types_used == []
    assert calls == []


@pytest.mark.asyncio
async def test_per_call_cap_truncates_candidates() -> None:
    rows = [{"node_id": f"node-{i}"} for i in range(FALLBACK_CANDIDATE_CAP + 15)]

    async def _search_fn(**kwargs: object) -> list[dict[str, object]]:
        return rows

    outcome = await fuzzy_fallback_candidates(
        query_text="q", dataset="d", run_used=0, search_fn=_search_fn
    )
    assert outcome.candidates_seen == FALLBACK_CANDIDATE_CAP
    assert len(outcome.candidate_ids) == FALLBACK_CANDIDATE_CAP
    assert outcome.search_types_used == ["CODE"]


@pytest.mark.asyncio
async def test_stops_after_first_nonempty_search_type() -> None:
    seen_types: list[str] = []

    async def _search_fn(**kwargs: object) -> list[dict[str, object]]:
        search_type = str(kwargs["search_type"])
        seen_types.append(search_type)
        if search_type == "CODE":
            return []
        return [{"node_id": "node-1"}]

    outcome = await fuzzy_fallback_candidates(
        query_text="q", dataset="d", run_used=0, search_fn=_search_fn
    )
    assert outcome.candidate_ids == ["node-1"]
    assert outcome.search_types_used == ["CODE", "CHUNKS_LEXICAL"]
    assert seen_types == ["CODE", "CHUNKS_LEXICAL"]


@pytest.mark.asyncio
async def test_dedupes_candidate_ids() -> None:
    async def _search_fn(**kwargs: object) -> list[dict[str, object]]:
        return [{"node_id": "a"}, {"node_id": "a"}, {"node_id": "b"}]

    outcome = await fuzzy_fallback_candidates(
        query_text="q", dataset="d", run_used=0, search_fn=_search_fn
    )
    assert outcome.candidate_ids == ["a", "b"]
    assert outcome.candidates_seen == 2


@pytest.mark.asyncio
async def test_empty_results_yield_empty_outcome() -> None:
    async def _search_fn(**kwargs: object) -> list[dict[str, object]]:
        return []

    outcome = await fuzzy_fallback_candidates(
        query_text="q", dataset="d", run_used=0, search_fn=_search_fn
    )
    assert outcome.candidates_seen == 0
    assert outcome.candidate_ids == []
    assert outcome.search_types_used == list(FALLBACK_SEARCH_ORDER)


# --- D3: verification ---


def test_verify_keeps_exact_match() -> None:
    node_id = str(uuid.uuid4())
    kept, discarded = verify_fuzzy_candidates(
        [node_id], _authority(node_id), "tenant-a", "acme--pay", "rev-1"
    )
    assert kept == [uuid.UUID(node_id)]
    assert discarded == 0


def test_verify_discards_unmapped() -> None:
    kept, discarded = verify_fuzzy_candidates(["nope"], {}, "tenant-a", "acme--pay", "rev-1")
    assert kept == [] and discarded == 1


def test_verify_discards_cross_tenant() -> None:
    node_id = str(uuid.uuid4())
    kept, discarded = verify_fuzzy_candidates(
        [node_id],
        _authority(node_id, tenant_id="tenant-b"),
        "tenant-a",
        "acme--pay",
        "rev-1",
    )
    assert kept == [] and discarded == 1


def test_verify_discards_cross_repository() -> None:
    node_id = str(uuid.uuid4())
    kept, discarded = verify_fuzzy_candidates(
        [node_id],
        _authority(node_id, repository_id="other--repo"),
        "tenant-a",
        "acme--pay",
        "rev-1",
    )
    assert kept == [] and discarded == 1


def test_verify_discards_cross_revision() -> None:
    node_id = str(uuid.uuid4())
    kept, discarded = verify_fuzzy_candidates(
        [node_id], _authority(node_id, revision="rev-2"), "tenant-a", "acme--pay", "rev-1"
    )
    assert kept == [] and discarded == 1


def test_verify_discards_micro_tier() -> None:
    node_id = str(uuid.uuid4())
    kept, discarded = verify_fuzzy_candidates(
        [node_id],
        _authority(node_id, granularity="micro"),
        "tenant-a",
        "acme--pay",
        "rev-1",
    )
    assert kept == [] and discarded == 1


def test_verify_discards_malformed_uuid_even_when_mapped() -> None:
    kept, discarded = verify_fuzzy_candidates(
        ["not-a-uuid"], _authority("not-a-uuid"), "tenant-a", "acme--pay", "rev-1"
    )
    assert kept == [] and discarded == 1


def test_verify_drops_scores_and_snippets_only_ids_cross() -> None:
    node_id = str(uuid.uuid4())
    kept, discarded = verify_fuzzy_candidates(
        [node_id], _authority(node_id), "tenant-a", "acme--pay", "rev-1"
    )
    assert len(kept) == 1 and isinstance(kept[0], uuid.UUID)
    assert discarded == 0


def test_fully_discarded_fallback_is_normal_outcome() -> None:
    kept, discarded = verify_fuzzy_candidates(
        ["ghost-1", "ghost-2"], {}, "tenant-a", "acme--pay", "rev-1"
    )
    assert kept == []
    assert discarded == 2


def test_new_module_imports_no_cognee_package() -> None:
    from pathlib import Path

    import investigation_agent_platform.application.evidence.code_recall as cr

    source = Path(cr.__file__).read_text(encoding="utf-8")
    assert "import cognee" not in source
    assert "from cognee" not in source
