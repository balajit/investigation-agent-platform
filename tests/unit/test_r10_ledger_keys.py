"""R10 (LV_R tranche): attempt-accounting edges (a), (b), (c), (d), (g).

(a) the budget key is the executed attempt's key; (b) typed history
parameters with an honest lossy flag; (c) a failed or absent durable
read authorizes nothing; (d) the newest linked attempts are kept;
(g) the count key includes the action type. Written first, red before
the fix.
"""

from __future__ import annotations

from uuid import uuid4

from investigation_agent_platform.application.evidence.retrieval_ledger import (
    build_claim_ledger,
    count_persisted_attempts,
    directive_consumed,
)
from investigation_agent_platform.application.investigation.verification import (
    _latest_linked_outcome,
    select_ledger_requery,
)
from investigation_agent_platform.domain.hypothesis.models import (
    AssessmentStrength,
    AssessmentType,
    ClaimKind,
    ClaimTarget,
    Hypothesis,
    HypothesisEvidenceAssessment,
    HypothesisStatus,
)


def _stored_hyp(scope=None):  # type: ignore[no-untyped-def]
    return Hypothesis(
        tenant_id="t",
        investigation_id=uuid4(),
        title="Widget is defined in a/B.java",
        description="Widget is defined in a/B.java",
        statement="Widget is defined in a/B.java",
        status=HypothesisStatus.SUPPORTED,
        supporting_evidence_ids=[],
        assessments=[
            HypothesisEvidenceAssessment(
                hypothesis_id=uuid4(),
                evidence_id=None,
                assessment=AssessmentType.SUPPORTS,
                strength=AssessmentStrength.MODERATE,
                reason="claim unresolved: no usable evidence",
                claim_kind=ClaimKind.DEFINITION,
                claim_target=ClaimTarget(symbol="Widget"),
                search_query="Widget",
                search_scope=scope,
                reason_code="CLAIM_UNRESOLVED",
                schema_version="v2",
            )
        ],
    )


def _recent_search_code():  # type: ignore[no-untyped-def]
    # A stored code search whose executed scope differs from the ledger key.
    return [
        {
            "action": "SEARCH_CODE",
            "parameters": {
                "query": "Widget",
                "repository": "r",
                "revision": "abc",
                "path_prefix": "/",
            },
        }
    ]


_EXEC_SCOPE = "r@abc:/:query=Widget"


def _failed_attempt(query, scope, attempt_id, action_type="SEARCH_CODE"):  # type: ignore[no-untyped-def]
    return (
        [],
        {
            "search_query": query,
            "scope": scope,
            "outcome": "SEARCH_FAILED",
            "attempt_id": attempt_id,
            "action_type": action_type,
        },
    )


def _exec_scope_of(recent):  # type: ignore[no-untyped-def]
    from investigation_agent_platform.application.evidence.retrieval_ledger import (
        extract_search_query,
    )

    _, scope, _ = extract_search_query("SEARCH_CODE", recent[0]["parameters"])
    return scope


def _eight_turns(executed_key_budget):  # type: ignore[no-untyped-def]
    hyp = _stored_hyp()
    recent = _recent_search_code()
    recorded: list = []
    directives = 0
    for turn in range(8):
        directive = select_ledger_requery(
            hyp, [], recorded, recent, 3,
            unavailable_actions=frozenset(),
            executed_key_budget=executed_key_budget,
        )
        if directive is None:
            continue
        directives += 1
        # A real dispatch records under its executed key, not the
        # directive's stamped scope.
        recorded.append(
            _failed_attempt("Widget", _exec_scope_of(recent), f"turn-{turn}")
        )
    return directives


def test_r10_t01_requery_bounded_for_scope_less_stored_query() -> None:
    assert _eight_turns(executed_key_budget=True) == 3
    # Negative control: counting under the ledger key fires every turn.
    assert _eight_turns(executed_key_budget=False) == 8


def test_r10_t02_explicit_stored_scope_still_bounded() -> None:
    hyp = _stored_hyp(scope="src/")
    recent = _recent_search_code()
    recorded: list = []
    directives = []
    for turn in range(8):
        directive = select_ledger_requery(
            hyp, [], recorded, recent, 3,
            unavailable_actions=frozenset(),
            executed_key_budget=True,
        )
        if directive is None:
            continue
        directives.append(directive)
        recorded.append(
            _failed_attempt(directive["query"], directive["scope"], f"turn-{turn}")
        )
    assert len(directives) <= 3
    assert directives and all(d["scope"] == "src/" for d in directives)


def test_r10_t03_marker_stamp_and_budget_agree() -> None:
    hyp = _stored_hyp()
    directive = select_ledger_requery(
        hyp, [], [], _recent_search_code(), 3,
        unavailable_actions=frozenset(),
        executed_key_budget=True,
    )
    assert directive is not None
    assert directive["scope"] == _EXEC_SCOPE

    def _marker(recorded):  # type: ignore[no-untyped-def]
        return {
            "claim_ref": directive["claim_ref"],
            "query": directive["query"],
            "scope": directive["scope"],
            "max_attempts": directive["max_attempts"],
            "turn_issued": 0,
            "action_type": directive["action_type"],
        }

    below = [_failed_attempt("Widget", _EXEC_SCOPE, f"t{i}")[1] for i in range(2)]
    assert directive_consumed(_marker(below), [], [([], a) for a in below]) is False
    at_budget = [_failed_attempt("Widget", _EXEC_SCOPE, f"t{i}")[1] for i in range(3)]
    assert directive_consumed(_marker(at_budget), [], [([], a) for a in at_budget]) is True


def test_r10_t04_pre_lvr_keeps_ledger_key() -> None:
    # Same loop as T01 without the flag: the ledger key never sees the
    # executed attempts, so the loop is unbounded. Documented regression
    # pin; deleted when the pin is retired.
    assert _eight_turns(executed_key_budget=False) > 3


def test_r10_t05_booleans_and_numbers_survive_history() -> None:
    from investigation_agent_platform.application.worker.workflows.forcing import (
        history_params,
    )

    params = {"case_sensitive": False, "limit": 10, "query": "Widget"}
    out, lossy = history_params(params, typed=True)
    assert out == params and lossy is False
    from types import SimpleNamespace

    ledger = SimpleNamespace(
        search_query="Widget", scope="@HEAD:/:query=Widget", query_source="derived"
    )
    from investigation_agent_platform.application.investigation.verification import (
        _match_recent_action,
    )

    action, matched = _match_recent_action(
        ledger, [{"action": "SEARCH_CODE", "parameters": dict(params)}]
    )
    assert action == "SEARCH_CODE"
    assert matched == params
    assert matched["case_sensitive"] is False and isinstance(matched["limit"], int)


def test_r10_t06_long_values_are_flagged_not_silently_cut() -> None:
    from types import SimpleNamespace

    from investigation_agent_platform.application.investigation.verification import (
        _match_recent_action,
    )
    from investigation_agent_platform.application.worker.workflows.forcing import (
        history_params,
    )

    params = {"query": "x" * 1500, "tags": ["a", "b"]}
    params.update({f"k{i}": i for i in range(25)})
    out, lossy = history_params(params, typed=True)
    assert lossy is True
    assert len(out["query"]) == 1024
    assert len(out) == 20
    ledger = SimpleNamespace(search_query="Widget", scope="", query_source="derived")
    entry = {"action": "SEARCH_CODE", "parameters": dict(out), "params_lossy": True}
    assert _match_recent_action(ledger, [entry]) == (None, None)
    out_plain, lossy_plain = history_params("not-a-dict", typed=True)  # type: ignore[arg-type]
    assert (out_plain, lossy_plain) == ({}, False)


def test_r10_t07_pre_lvr_shape_unchanged() -> None:
    from investigation_agent_platform.application.worker.workflows.forcing import (
        history_params,
    )

    params = {"case_sensitive": False, "limit": 10, "query": "x" * 500}
    out, lossy = history_params(params, typed=False)
    assert out == {"case_sensitive": "False", "limit": "10", "query": "x" * 200}
    assert lossy is False


def _linked_attempts(n, outcome="SEARCH_FAILED"):  # type: ignore[no-untyped-def]
    return [
        ([], {"search_query": "Widget", "scope": "s", "outcome": outcome,
              "attempt_id": f"a{i}"})
        for i in range(n)
    ]


def test_r10_t11_latest_linked_attempt_is_newest() -> None:
    attempts = _linked_attempts(12, outcome="SCOPED_EMPTY")
    attempts[10] = ([], {"search_query": "Widget", "scope": "s",
                         "outcome": "SEARCH_FAILED", "attempt_id": "a10"})
    attempts[11] = ([], {"search_query": "Widget", "scope": "s",
                         "outcome": "SEARCH_FAILED", "attempt_id": "a11"})
    ledger_old, _, _ = build_claim_ledger(
        "Widget is defined", "DEFINITION", {"symbol": "Widget"},
        cited_ids=[], bank_ids=set(), attempts=attempts,
        stored_query="Widget", stored_scope="s",
    )
    assert [int(i) for i in ledger_old.attempt_indexes] == list(range(10))
    assert _latest_linked_outcome(ledger_old, attempts) == "SCOPED_EMPTY"
    ledger_new, _, _ = build_claim_ledger(
        "Widget is defined", "DEFINITION", {"symbol": "Widget"},
        cited_ids=[], bank_ids=set(), attempts=attempts,
        stored_query="Widget", stored_scope="s", keep_newest=True,
    )
    assert [int(i) for i in ledger_new.attempt_indexes] == list(range(2, 12))
    assert _latest_linked_outcome(ledger_new, attempts) == "SEARCH_FAILED"


def test_r10_t12_at_most_10_linked_is_unchanged() -> None:
    attempts = _linked_attempts(7)
    for keep in (False, True):
        ledger, _, _ = build_claim_ledger(
            "Widget is defined", "DEFINITION", {"symbol": "Widget"},
            cited_ids=[], bank_ids=set(), attempts=attempts,
            stored_query="Widget", stored_scope="s", keep_newest=keep,
        )
        assert [int(i) for i in ledger.attempt_indexes] == list(range(7))


def test_r10_t17_action_type_separates_budgets() -> None:
    recorded = [
        _failed_attempt("Widget", _EXEC_SCOPE, f"s{i}", action_type="SEARCH_CODE")
        for i in range(3)
    ]
    assert count_persisted_attempts(recorded, "Widget", _EXEC_SCOPE) == 3
    assert (
        count_persisted_attempts(recorded, "Widget", _EXEC_SCOPE, action_type="SEARCH_CODE")
        == 3
    )
    assert (
        count_persisted_attempts(recorded, "Widget", _EXEC_SCOPE, action_type="FIND_SYMBOL")
        == 0
    )
    find_recent = [
        {"action": "FIND_SYMBOL", "parameters": {"symbol_name": "Widget"}}
    ]
    directive = select_ledger_requery(
        _stored_hyp(), [], recorded, find_recent, 3,
        unavailable_actions=frozenset(),
        executed_key_budget=True,
    )
    assert directive is not None
    assert directive["action_type"] == "FIND_SYMBOL"


def test_r10_t18_marker_carries_action_type() -> None:
    from investigation_agent_platform.application.worker.workflows.forcing import (
        _carry_requery_order,
    )

    marker = {
        "claim_ref": "c1",
        "query": "Widget",
        "scope": _EXEC_SCOPE,
        "max_attempts": 3,
        "turn_issued": 0,
        "action_type": "SEARCH_CODE",
    }
    same_type = [
        _failed_attempt("Widget", _EXEC_SCOPE, f"s{i}", action_type="SEARCH_CODE")
        for i in range(3)
    ]
    assert directive_consumed(marker, [], same_type) is True
    other_type = [
        _failed_attempt("Widget", _EXEC_SCOPE, f"f{i}", action_type="FIND_SYMBOL")
        for i in range(3)
    ]
    assert directive_consumed(marker, [], other_type) is False
    # A directive without a type is rejected at the carrier builder.
    assert _carry_requery_order({"ledger_requery": {"query": "Widget"}}, 1) is None


def test_r10_t20_decisions_never_read_process_local_ledger() -> None:
    import pathlib

    roots = [
        "src/investigation_agent_platform/application/evidence/retrieval_ledger.py",
        "src/investigation_agent_platform/application/investigation/verification.py",
        "src/investigation_agent_platform/application/worker/workflows/forcing.py",
    ]
    hits: list[str] = []
    for root in roots:
        lines = pathlib.Path(root).read_text().splitlines()
        in_record = False
        in_ledger_for = False
        for lineno, line in enumerate(lines, start=1):
            stripped = line.strip()
            if stripped.startswith("def record_retrieval_attempt("):
                in_record = True
            elif stripped.startswith("def _ledger_for("):
                in_ledger_for = True
            elif stripped.startswith("def ") and (in_record or in_ledger_for):
                in_record = in_ledger_for = False
            if ("_LEDGERS" in line or "_ledger_for" in line) and not (
                in_record or in_ledger_for or "_LEDGERS: " in line
            ):
                hits.append(f"{root}:{lineno}:{line.strip()}")
    assert hits == []
