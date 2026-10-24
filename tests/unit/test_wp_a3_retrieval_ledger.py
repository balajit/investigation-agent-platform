"""WP-A3 retrieval ledger (Stage A, additive-only): G3 gate tests.

Covers the G3 exit criteria: frozen outcome vocabulary, distinct
search_failed vs scoped-empty classification, novelty + proof-lift
measurement, per-(query, scope) bound marking, tolerant ActionOutcome
readers, per-action query extraction, and fail-open behavior at the
execute_action_activity wiring point.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from investigation_agent_platform.api.dependencies import AppContext, set_app_context
from investigation_agent_platform.application.evidence.retrieval_ledger import (
    RetrievalAttempt,
    RetrievalLedger,
    RetrievalOutcome,
    classify_retrieval_outcome,
    extract_search_query,
    record_retrieval_attempt,
)
from investigation_agent_platform.application.worker.activities import (
    ExecuteActionInput,
    execute_action_activity,
)
from investigation_agent_platform.domain.investigation.models import ActionOutcome


def test_outcome_vocabulary_is_frozen() -> None:
    assert sorted(o.value for o in RetrievalOutcome) == [
        "PARTIAL_ROWS",
        "ROWS_RETURNED",
        "SCOPED_EMPTY",
        "SEARCH_FAILED",
        "UNSCOPED_EMPTY",
    ]


def test_classify_rows_beat_everything() -> None:
    assert (
        classify_retrieval_outcome("FAILED", "timeout", 2, "repo@rev:query=q")
        == RetrievalOutcome.PARTIAL_ROWS
    )
    assert (
        classify_retrieval_outcome("SUCCEEDED", None, 1, None)
        == RetrievalOutcome.ROWS_RETURNED
    )


def test_classify_partial_rows_never_read_as_ordinary_success() -> None:
    # Rows plus a failure marker: partial, with the marker preserved.
    assert (
        classify_retrieval_outcome("SUCCEEDED", "timeout", 2, "s", source_statuses=["SUCCEEDED"])
        == RetrievalOutcome.PARTIAL_ROWS
    )
    assert (
        classify_retrieval_outcome("SUCCEEDED", "rate_limited", 1, "s")
        == RetrievalOutcome.PARTIAL_ROWS
    )
    # Rows plus budget truncation: partial even on a clean request.
    assert (
        classify_retrieval_outcome("SUCCEEDED", None, 5, "s", truncated_rows=3)
        == RetrievalOutcome.PARTIAL_ROWS
    )
    # Rows plus a failed fanned-out source: partial.
    assert (
        classify_retrieval_outcome(
            "SUCCEEDED", None, 4, "s", source_statuses=["SUCCEEDED", "DEGRADED"]
        )
        == RetrievalOutcome.PARTIAL_ROWS
    )
    assert (
        classify_retrieval_outcome(
            "SUCCEEDED", None, 4, "s", source_statuses=["SUCCEEDED", "SKIPPED"]
        )
        == RetrievalOutcome.PARTIAL_ROWS
    )
    # Clean rows from clean sources: ordinary success.
    assert (
        classify_retrieval_outcome(
            "SUCCEEDED", None, 4, "s", source_statuses=["SUCCEEDED", "SUCCEEDED"]
        )
        == RetrievalOutcome.ROWS_RETURNED
    )
    # Deterministic precedence: empty still classifies by failure/scope.
    assert (
        classify_retrieval_outcome("DEGRADED", "timeout", 0, "s", truncated_rows=9)
        == RetrievalOutcome.SEARCH_FAILED
    )


def test_classify_failures_are_distinct_from_clean_empties() -> None:
    assert classify_retrieval_outcome("FAILED", None, 0, "s") == RetrievalOutcome.SEARCH_FAILED
    assert classify_retrieval_outcome("BLOCKED", None, 0, "s") == RetrievalOutcome.SEARCH_FAILED
    for miss in (
        "timeout",
        "auth_failed",
        "runtime_provider_not_configured",
        "provider_unavailable",
        "rate_limited",
        "missing_file_path",
        "missing_query",
        "unsupported_capability",
        "authorization_denied",
    ):
        assert (
            classify_retrieval_outcome("DEGRADED", miss, 0, "repo@rev:query=q")
            == RetrievalOutcome.SEARCH_FAILED
        ), miss


def test_classify_clean_empty_uses_scope() -> None:
    assert (
        classify_retrieval_outcome("EMPTY", None, 0, "repo@rev:query=q")
        == RetrievalOutcome.SCOPED_EMPTY
    )
    assert (
        classify_retrieval_outcome("SUCCEEDED", None, 0, "repo@rev:query=q")
        == RetrievalOutcome.SCOPED_EMPTY
    )
    assert (
        classify_retrieval_outcome("DEGRADED", "refdoc_no_hits", 0, "scope")
        == RetrievalOutcome.SCOPED_EMPTY
    )
    assert classify_retrieval_outcome("EMPTY", None, 0, None) == RetrievalOutcome.UNSCOPED_EMPTY
    assert classify_retrieval_outcome("EMPTY", None, 0, "  ") == RetrievalOutcome.UNSCOPED_EMPTY


def test_ledger_measures_novelty() -> None:
    ledger = RetrievalLedger()
    first = ledger.record("q", "p", "s", RetrievalOutcome.ROWS_RETURNED, ["a", "b"], 2)
    assert first.novel_rows == 2
    assert first.attempt_index == 0
    assert first.over_budget is False
    assert first.lifted is False
    second = ledger.record("q", "p", "s", RetrievalOutcome.ROWS_RETURNED, ["b", "c"], 2)
    assert second.novel_rows == 1
    assert second.attempt_index == 1


def test_ledger_measures_proof_lift_on_empty_then_rows() -> None:
    ledger = RetrievalLedger()
    empty = ledger.record("q", "p", "s", RetrievalOutcome.SCOPED_EMPTY, [], 0)
    assert empty.lifted is False
    lifted = ledger.record("q", "p", "s", RetrievalOutcome.ROWS_RETURNED, ["a"], 1)
    assert lifted.lifted is True
    assert lifted.novel_rows == 1


def test_ledger_marks_over_budget_without_blocking() -> None:
    ledger = RetrievalLedger(max_attempts=2)
    attempts = [
        ledger.record("q", "p", "s", RetrievalOutcome.SCOPED_EMPTY, [], 0) for _ in range(3)
    ]
    assert [a.attempt_index for a in attempts] == [0, 1, 2]
    assert [a.over_budget for a in attempts] == [False, False, True]
    # Marking only — the attempt is still recorded with its outcome.
    assert attempts[2].outcome == RetrievalOutcome.SCOPED_EMPTY


def test_ledger_attempt_carries_bound_in_force() -> None:
    # Remediation plan ARCH-HIGH-013: the env-controlled bound rides with
    # the attempt so identical inputs under different settings are
    # distinguishable in run metadata.
    assert RetrievalLedger().record(
        "q", "p", "s", RetrievalOutcome.ROWS_RETURNED, ["a"], 1
    ).max_attempts == 3
    assert RetrievalLedger(max_attempts=7).record(
        "q", "p", "s", RetrievalOutcome.ROWS_RETURNED, ["a"], 1
    ).max_attempts == 7


def test_extract_search_query_mirrors_scope_conventions() -> None:
    query, scope, _ = extract_search_query(
        "SEARCH_CODE", {"query": "nope", "repository": "r", "revision": "abc", "path_prefix": "/"}
    )
    assert query == "nope"
    assert scope == "r@abc:/:query=nope"
    query, scope, _ = extract_search_query("FIND_SYMBOL", {"symbol_name": "S"})
    assert query == "S"
    assert scope is not None and "symbol=S" in scope
    query, scope, _ = extract_search_query("SEARCH_CODE", {"scope": "custom", "query": "q"})
    assert scope == "custom"
    assert extract_search_query("CORRELATE", {}) == ("", None, "")


def test_attempt_carries_partial_signals() -> None:
    from investigation_agent_platform.application.evidence.retrieval_ledger import (
        _LEDGERS,
    )

    _LEDGERS.clear()
    attempt = record_retrieval_attempt(
        "inv-sig",
        "SEARCH_CODE",
        {"query": "q"},
        "SUCCEEDED",
        "timeout",
        ["r1"],
        1,
        truncated_rows=2,
        source_statuses=["SUCCEEDED", "DEGRADED"],
    )
    assert attempt is not None
    assert attempt.outcome == RetrievalOutcome.PARTIAL_ROWS
    assert attempt.signals == ["failure:timeout", "source:DEGRADED", "truncated:2"]
    _LEDGERS.clear()


def test_clean_attempt_carries_no_signals() -> None:
    from investigation_agent_platform.application.evidence.retrieval_ledger import (
        _LEDGERS,
    )

    _LEDGERS.clear()
    attempt = record_retrieval_attempt(
        "inv-clean",
        "SEARCH_CODE",
        {"query": "q"},
        "SUCCEEDED",
        None,
        ["r1"],
        1,
        source_statuses=["SUCCEEDED"],
    )
    assert attempt is not None
    assert attempt.outcome == RetrievalOutcome.ROWS_RETURNED
    assert attempt.signals == []
    _LEDGERS.clear()


def test_action_outcome_tolerates_missing_attempt() -> None:
    legacy = ActionOutcome.model_validate({"action_type": "SEARCH_CODE"})
    assert legacy.retrieval_attempt is None


def test_action_outcome_tolerates_unknown_future_outcome() -> None:
    # RetrievalAttempt.outcome is stored as a plain string in the
    # ActionOutcome dict: an unknown future code still validates.
    legacy = ActionOutcome.model_validate({
        "action_type": "SEARCH_CODE",
        "retrieval_attempt": {
            "search_query": "q",
            "outcome": "SOME_FUTURE_OUTCOME",
            "rows_returned": 1,
        },
    })
    assert legacy.retrieval_attempt is not None
    assert legacy.retrieval_attempt["outcome"] == "SOME_FUTURE_OUTCOME"
    attempt = RetrievalAttempt(
        search_query="q",
        scope="s",
        outcome=RetrievalOutcome.SCOPED_EMPTY,
    )
    round_tripped = ActionOutcome.model_validate(
        {"action_type": "SEARCH_CODE", "retrieval_attempt": attempt.model_dump(mode="json")}
    )
    assert round_tripped.retrieval_attempt is not None
    assert round_tripped.retrieval_attempt["outcome"] == "SCOPED_EMPTY"


def _ctx_with(gateway: Any) -> Any:
    ctx = AppContext()
    service = SimpleNamespace(
        authorize=AsyncMock(return_value=None),
        execute=AsyncMock(return_value=None),
    )
    ctx.execute_action_service = lambda: service  # type: ignore[method-assign]
    ctx.action_repo = SimpleNamespace(count_by_investigation=AsyncMock(return_value=0))
    ctx.evidence_repo = SimpleNamespace(
        find_by_investigation_id=AsyncMock(return_value=[]),
        save_batch=AsyncMock(return_value=None),
        save=AsyncMock(return_value=None),
    )
    ctx.profile_repo = SimpleNamespace(get_by_application_id=AsyncMock(return_value=None))
    ctx.hypothesis_repo = SimpleNamespace(
        find_by_investigation_id=AsyncMock(return_value=[]),
        save=AsyncMock(),
    )
    ctx.evidence_gateway = gateway
    set_app_context(ctx)
    return ctx


def _input(action: str, parameters: dict[str, Any]) -> Any:
    return ExecuteActionInput(
        tenant_id="tenant-a",
        investigation_id=str(uuid4()),
        action=action,
        parameters=parameters,
    )


@pytest.mark.asyncio
async def test_wiring_records_partial_rows_on_failed_source() -> None:
    from investigation_agent_platform.application.worker.activities import (
        execute_action_activity,
    )

    row = SimpleNamespace(evidence_id=uuid4())

    async def _get_state(tenant_id, investigation_id, application_id, req):  # type: ignore[no-untyped-def]
        return SimpleNamespace(
            items=[row],
            total_count=1,
            provider_metadata={
                "per_source": [
                    {"source": "primary", "status": "SUCCEEDED", "items": 1},
                    {"source": "archive", "status": "DEGRADED", "items": 0},
                ]
            },
        )

    _ctx_with(SimpleNamespace(get_application_state=_get_state))
    result = await execute_action_activity(
        _input("QUERY_STATE", {"template_id": "t1", "parameters": {"k": "v"}})
    )
    assert result.data["items_count"] == 1
    attempt = result.data["action_outcome"]["retrieval_attempt"]
    assert attempt is not None
    assert attempt["outcome"] == "PARTIAL_ROWS"
    assert attempt["signals"] == ["source:DEGRADED"]
    assert attempt["rows_returned"] == 1


@pytest.mark.asyncio
async def test_wiring_records_scoped_empty_attempt() -> None:
    async def _search_code(tenant_id, investigation_id, application_id, req):  # type: ignore[no-untyped-def]
        return SimpleNamespace(items=[], total_count=0, provider_metadata={})

    _ctx_with(SimpleNamespace(search_code=_search_code))
    result = await execute_action_activity(
        _input("SEARCH_CODE", {"query": "nope", "revision": "abc123"})
    )
    assert result.data["items_count"] == 0
    outcome = result.data["action_outcome"]
    assert outcome["status"] == "EMPTY"
    attempt = outcome["retrieval_attempt"]
    assert attempt is not None
    assert attempt["outcome"] == "SCOPED_EMPTY"
    assert attempt["search_query"] == "nope"
    assert "nope" in (attempt["scope"] or "")
    assert attempt["rows_returned"] == 0
    assert attempt["attempt_index"] == 0
    assert attempt["over_budget"] is False


def test_novelty_is_id_based_not_content_based() -> None:
    # Documented limit (K14): novelty tracks first-seen row IDs. Two
    # distinct IDs with identical content both count as novel — novelty
    # does not prove corroboration independence.
    ledger = RetrievalLedger()
    first = ledger.record("q", "p", "s", RetrievalOutcome.ROWS_RETURNED, ["a"], 1)
    second = ledger.record("q", "p", "s", RetrievalOutcome.ROWS_RETURNED, ["b"], 1)
    assert (first.novel_rows, second.novel_rows) == (1, 1)


def test_module_bound_marks_over_budget(monkeypatch: Any) -> None:
    from investigation_agent_platform.application.evidence.retrieval_ledger import (
        _LEDGERS,
    )

    monkeypatch.setenv("IAP_WP_A3_MAX_ATTEMPTS", "1")
    _LEDGERS.clear()
    params: dict[str, Any] = {"query": "q"}
    first = record_retrieval_attempt("inv-bound", "SEARCH_CODE", params, "EMPTY", None, [], 0)
    second = record_retrieval_attempt("inv-bound", "SEARCH_CODE", params, "EMPTY", None, [], 0)
    assert first is not None and first.over_budget is False
    assert second is not None and second.over_budget is True
    assert second.outcome == RetrievalOutcome.SCOPED_EMPTY
    _LEDGERS.clear()
