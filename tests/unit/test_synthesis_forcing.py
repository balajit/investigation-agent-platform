"""G8 synthesis-forcing rule: pure-function unit tests.

The rule lives in the workflow module (workflow-safe, no Temporal imports
at test time for the pure helper) and is wired into the investigation loop
to override gathering actions with FORMULATE_HYPOTHESIS when the model
holds unevaluated hypotheses against banked evidence.
"""

from investigation_agent_platform.application.worker.workflows import (
    _should_force_formulate,
)


def test_forces_when_pending_hypotheses_banked_evidence_and_turns_elapsed():
    assert (
        _should_force_formulate(
            iteration=2,
            formulated_count=0,
            hypotheses_evaluated=["h1"],
            evidence_banked_total=5,
            forced_count=0,
        )
        is True
    )


def test_no_force_once_formulated():
    assert _should_force_formulate(5, 1, ["h1"], 9, 0) is False


def test_no_force_without_pending_hypotheses():
    assert _should_force_formulate(5, 0, [], 9, 0) is False
    assert _should_force_formulate(2, 0, None, 9, 0) is False


def test_no_force_without_banked_evidence():
    assert _should_force_formulate(5, 0, ["h1"], 0, 0) is False


def test_no_force_before_two_turns_elapsed():
    assert _should_force_formulate(1, 0, ["h1"], 9, 0) is False


def test_no_force_after_cap_reached():
    assert _should_force_formulate(9, 0, ["h1"], 9, 2) is False


def test_reread_forces_get_code_for_definition_without_range_read():
    from investigation_agent_platform.application.worker.workflows import (
        _reread_target,
    )

    reason_data = {
        "next_action": "VERIFY_HYPOTHESIS",
        "hypotheses_evaluated": [
            "The execute action is implemented in src/worker/activities.py"
        ],
    }
    history = [
        {"action": "SEARCH_CODE", "evidence_added": 4},
        {"action": "SEARCH_LOGS", "evidence_added": 10},
    ]
    assert (
        _reread_target(reason_data, history, 0) == "src/worker/activities.py"
    )


def test_reread_skipped_when_range_read_already_banked():
    from investigation_agent_platform.application.worker.workflows import (
        _reread_target,
    )

    reason_data = {
        "next_action": "VERIFY_HYPOTHESIS",
        "hypotheses_evaluated": ["X is defined in src/a.py"],
    }
    history = [{"action": "GET_CODE", "evidence_added": 1}]
    assert _reread_target(reason_data, history, 0) is None


def test_reread_only_fires_on_verify_with_definition_claim():
    from investigation_agent_platform.application.worker.workflows import (
        _reread_target,
    )

    definition = {
        "next_action": "VERIFY_HYPOTHESIS",
        "hypotheses_evaluated": ["X is defined in src/a.py"],
    }
    gathering = {
        "next_action": "SEARCH_CODE",
        "hypotheses_evaluated": ["X is defined in src/a.py"],
    }
    causal = {
        "next_action": "VERIFY_HYPOTHESIS",
        "hypotheses_evaluated": ["Pool exhaustion causes latency"],
    }
    assert _reread_target(gathering, [], 0) is None
    assert _reread_target(causal, [], 0) is None
    assert _reread_target(definition, [], 0) == "src/a.py"
    assert _reread_target(definition, [], 2) is None
    assert _reread_target(None, [], 0) is None


def test_log_force_routes_error_complaint_to_search_logs():
    from investigation_agent_platform.application.worker.workflows import (
        _should_force_log_search,
    )

    desc = "Show me recent errors for the payments service"
    reason_data = {"next_action": "SEARCH_CODE", "action_parameters": {"query": "errors"}}
    params = _should_force_log_search(desc, reason_data, [], 0)
    assert params is not None
    assert params["keywords"] == ["errors"]
    assert params["services"] == []


def test_log_force_skips_non_error_get_code_and_capped():
    from investigation_agent_platform.application.worker.workflows import (
        _should_force_log_search,
    )

    desc = "Show me recent errors for the payments service"
    code_target = {"next_action": "GET_CODE", "action_parameters": {"file_path": "src/a.py"}}
    assert _should_force_log_search(desc, code_target, [], 0) is None
    plain = {"next_action": "SEARCH_CODE", "action_parameters": {"query": "widget"}}
    assert _should_force_log_search("Where is the widget defined?", plain, [], 0) is None
    err = {"next_action": "SEARCH_CODE", "action_parameters": {"query": "errors"}}
    assert _should_force_log_search(desc, err, [], 2) is None
    assert (
        _should_force_log_search(
            desc, err, [{"action": "SEARCH_LOGS", "evidence_added": 5}], 0
        )
        is None
    )
    derived = _should_force_log_search(
        desc, {"next_action": "QUERY_STATE", "action_parameters": {}}, [], 0
    )
    assert derived is not None and len(derived["keywords"]) >= 1


def test_reason_summary_carries_forced_and_outcome():
    from investigation_agent_platform.application.worker.workflows import (
        _reason_summary,
    )

    # Raw decision shape (pre-execution) renders the old shape.
    plain = _reason_summary(
        {"next_action": "SEARCH_CODE", "conclusion_readiness": 0.0,
         "hypotheses_evaluated": [], "observations": ["o"]}
    )
    assert "action=SEARCH_CODE" in plain
    assert "forced=" not in plain
    assert "outcome=" not in plain
    # Enriched entry shape: dispatched action wins, forced + outcome shown.
    enriched = _reason_summary(
        {"next_action": "SEARCH_CODE", "conclusion_readiness": 0.25,
         "hypotheses_evaluated": ["h"], "observations": [],
         "action": "FORMULATE_HYPOTHESIS", "forced_formulate": True,
         "outcome_status": "SUCCEEDED", "evidence_added": 2,
         "miss_reason": None}
    )
    assert "action=FORMULATE_HYPOTHESIS" in enriched
    assert "forced=[formulate]" in enriched
    assert "outcome_status=SUCCEEDED" in enriched
    assert "evidence_added=2" in enriched


def test_derive_loop_telemetry_merges_counters_and_counts():
    from investigation_agent_platform.application.worker.activities import (
        _derive_loop_telemetry,
        _stamp_loop_telemetry,
    )

    assert _derive_loop_telemetry(None, {}, 0) is None
    assert _derive_loop_telemetry({}, {}, 0) is None
    merged = _derive_loop_telemetry(
        {"llm_tokens_total": 5224, "llm_cost_total": 0.01,
         "formulated_count": 1, "forced_formulate_count": 1,
         "iterations": 6, "junk": "dropped", "flag": True},
        {"hypotheses_formulated": 2, "hypotheses_bound": 1, "hypotheses_verified": 0},
        11,
    )
    assert merged["llm_tokens_total"] == 5224
    assert merged["forced_formulate_count"] == 1
    assert merged["hypotheses_bound"] == 1
    assert merged["evidence_banked_repo"] == 11
    assert "junk" not in merged and "flag" not in merged
    # Stamp helper mirrors _stamp_failure onto metadata.
    class _Inv:
        def __init__(self, metadata):
            self.metadata = metadata

        def model_copy(self, update=None):
            return _Inv({**(self.metadata or {}), **((update or {}).get("metadata", {}))})

    inv = _Inv({"disposition": {}})
    stamped = _stamp_loop_telemetry(inv, merged)
    assert stamped.metadata["loop_telemetry"]["iterations"] == 6
    assert stamped.metadata["disposition"] == {}
    assert _stamp_loop_telemetry(inv, None) is inv


def test_reason_summary_tolerates_merged_entry_counts():
    from investigation_agent_platform.application.worker.workflows import (
        _reason_summary,
    )

    # Exact live shape that raised TypeError: entry int counts shadow
    # decision lists after the call-site merge.
    decision = {
        "next_action": "SEARCH_CODE", "conclusion_readiness": 0.0,
        "hypotheses_evaluated": [], "observations": ["o"],
        "action_parameters": {"query": "x"},
    }
    entry = {
        "action": "SEARCH_CODE", "readiness": 0.0,
        "hypotheses": 0, "observations": 1,
        "outcome_status": "SUCCEEDED", "evidence_added": 3,
    }
    summary = _reason_summary({**decision, **entry})
    assert "action=SEARCH_CODE" in summary
    assert "observations=1" in summary
    assert "outcome_status=SUCCEEDED" in summary


def test_degraded_empty_result_shape():
    from investigation_agent_platform.application.worker.workflows import (
        _degraded_empty_result,
    )

    res = _degraded_empty_result("SEARCH_CODE", "idempotency_owner_busy")
    assert res.data["items_count"] == 0
    assert res.data["action_outcome"]["status"] == "DEGRADED"
    assert res.data["action_outcome"]["miss_reason"] == "idempotency_owner_busy"
    assert res.data["action_outcome"]["evidence_ids"] == []
    assert not res.data.get("budget_exceeded")


def test_caused_by_idempotency_wedge_walks_chain():
    from investigation_agent_platform.application.worker.workflows import (
        _caused_by_idempotency_wedge,
    )

    class FakeErr(Exception):
        def __init__(self, msg, type=None, cause=None):
            super().__init__(msg)
            self.type = type
            self.__cause__ = cause

    wedge = FakeErr("busy", type="IdempotencyRequestInProgress")
    wrapped = FakeErr("activity failed", cause=wedge)
    outer = FakeErr("outer", cause=wrapped)
    assert _caused_by_idempotency_wedge(outer) is True
    assert _caused_by_idempotency_wedge(wedge) is True
    assert _caused_by_idempotency_wedge(FakeErr("other", cause=FakeErr("x"))) is False
    assert _caused_by_idempotency_wedge(ValueError("plain")) is False


def test_walk_stack_forces_on_intake_seed_frames():
    from investigation_agent_platform.application.worker.workflows import (
        _should_force_walk_stack,
    )

    seed = {
        "error_class": "NullPointerException",
        "frames": [{"file": "OfferSvc.java", "line": 42, "function": "price"}],
    }
    reason_data = {"next_action": "SEARCH_LOGS", "action_parameters": {}}
    params = _should_force_walk_stack(seed, reason_data, [], 0)
    assert params is not None
    assert params["frames"] == seed["frames"]
    assert params["error_class"] == "NullPointerException"
    # Skips: already walking, done before, capped, no frames.
    assert (
        _should_force_walk_stack(
            seed, {"next_action": "WALK_STACK", "action_parameters": {}}, [], 0
        )
        is None
    )
    assert (
        _should_force_walk_stack(
            seed, reason_data, [{"action": "WALK_STACK", "evidence_added": 2}], 0
        )
        is None
    )
    assert _should_force_walk_stack(seed, reason_data, [], 2) is None
    assert _should_force_walk_stack({}, reason_data, [], 0) is None
    assert _should_force_walk_stack(None, reason_data, [], 0) is None
    # Downstream actions never overridden.
    assert (
        _should_force_walk_stack(
            seed, {"next_action": "VERIFY_HYPOTHESIS", "action_parameters": {}}, [], 0
        )
        is None
    )


def test_classify_failure_routes():
    from investigation_agent_platform.application.worker.workflows import (
        _classify_failure,
    )

    assert _classify_failure("SocketTimeoutException on checkout", {}) == "TRANSIENT"
    assert _classify_failure("HTTP 503 from pricing", {}) == "TRANSIENT"
    assert _classify_failure("FileNotFoundError application.yml", {}) == "CONFIG"
    assert _classify_failure("NPE in pricing", {"expected": {"a": 1}}) == "DATA"
    assert _classify_failure("offers are wrong, locationType missing", {}) == "DATA"
    assert _classify_failure(
        "NPE in validate",
        {"frames": [{"file": "x.py"}], "error_class": "NullPointerException"},
    ) == "BUG"
    assert _classify_failure("Where is the widget defined?", {}) == "UNKNOWN"
    assert _classify_failure(None, None) == "UNKNOWN"


def test_investigation_plan_skeleton():
    from investigation_agent_platform.application.worker.workflows import (
        _investigation_plan,
    )

    assert _investigation_plan("BUG", {"frames": [{"file": "x.py"}]})[0] == "WALK_STACK"
    assert _investigation_plan("DATA", {})[0] == "SEARCH_LOGS"
    assert _investigation_plan("UNKNOWN", {})[0] == "SEARCH_LOGS"
    assert len(_investigation_plan("BUG", {})) <= 5


def test_force_replan_redirects_repeated_empties():
    from investigation_agent_platform.application.worker.workflows import (
        _should_force_replan,
    )

    # Production shape: completed enriched turns only — the current
    # proposal arrives via reason_data, not history (the call site runs
    # before the current action is appended).
    history = [
        {"action": "SEARCH_CODE", "evidence_added": 0},
        {"action": "SEARCH_CODE", "evidence_added": 0},
    ]
    hit = _should_force_replan(
        "Show recent errors for payments",
        {"next_action": "SEARCH_CODE", "action_parameters": {}},
        history,
        0,
    )
    assert hit is not None
    assert hit["next_action"] == "SEARCH_LOGS"
    assert hit["action_parameters"]["keywords"]
    # Banked evidence blocks; different proposal blocks; cap blocks.
    # A single prior turn is not a pattern — needs two enriched empties.
    rich = [
        {"action": "SEARCH_CODE", "evidence_added": 0},
        {"action": "SEARCH_CODE", "evidence_added": 4},
    ]
    assert _should_force_replan("errors here", {"next_action": "SEARCH_CODE"}, rich, 0) is None
    assert _should_force_replan(
        "errors here", {"next_action": "QUERY_STATE"}, history, 0
    ) is None
    assert _should_force_replan(
        "errors here", {"next_action": "SEARCH_CODE"}, history, 2
    ) is None
    assert _should_force_replan(
        "errors here",
        {"next_action": "SEARCH_CODE"},
        [{"action": "SEARCH_CODE", "evidence_added": 0}],
        0,
    ) is None
    assert _should_force_replan(None, None, None, 0) is None


def test_prefer_discriminating_candidate():
    from uuid import uuid4

    from investigation_agent_platform.application.investigation.verification import (
        rank_hypotheses,
        select_verifiable_candidate,
    )

    shared, unique_a, unique_b = uuid4(), uuid4(), uuid4()

    def _hyp(supporting):
        from investigation_agent_platform.domain.hypothesis.models import Hypothesis

        return Hypothesis(
            tenant_id="t",
            investigation_id=uuid4(),
            title="t",
            description="d",
            statement="s",
            status="UNDER_INVESTIGATION",
            supporting_evidence_ids=supporting,
        )

    first = _hyp([shared, unique_a])
    second = _hyp([shared, unique_b, uuid4()])
    evidence_by_id = {shared: 1, unique_a: 1, unique_b: 1}
    evidence_by_id.update({h_id: 1 for h_id in second.supporting_evidence_ids})
    ranked = rank_hypotheses([first, second])
    chosen = select_verifiable_candidate(ranked, evidence_by_id)
    assert chosen is not None
    assert set(chosen.supporting_evidence_ids) == set(second.supporting_evidence_ids)


def test_force_synthesis_on_rich_unformulated_corpus():
    from investigation_agent_platform.application.worker.workflows import (
        _should_force_synthesis,
    )

    history = [
        {"action": "SEARCH_LOGS", "evidence_added": 10},
        {"action": "SEARCH_CODE", "evidence_added": 4},
        {"action": "SEARCH_CODE", "evidence_added": 0},
    ]
    assert (
        _should_force_synthesis(
            history, 14, 0, 0, {"next_action": "SEARCH_CODE"}
        )
        is True
    )
    # Formulated already / capped / thin corpus / single action / terminal proposal.
    assert _should_force_synthesis(history, 14, 1, 0, {"next_action": "SEARCH_CODE"}) is False
    assert _should_force_synthesis(history, 14, 0, 2, {"next_action": "SEARCH_CODE"}) is False
    assert _should_force_synthesis(history, 5, 0, 0, {"next_action": "SEARCH_CODE"}) is False
    assert _should_force_synthesis(
        [{"action": "SEARCH_CODE", "evidence_added": 9}], 9, 0, 0,
        {"next_action": "SEARCH_CODE"},
    ) is False
    assert _should_force_synthesis(history, 14, 0, 0, {"next_action": "VERIFY_HYPOTHESIS"}) is False
    assert _should_force_synthesis(None, 0, 0, 0, None) is False


def test_loop_telemetry_carries_overconfident_turns():
    from investigation_agent_platform.application.worker.activities import (
        _derive_loop_telemetry,
    )

    merged = _derive_loop_telemetry(
        {"llm_tokens_total": 100, "overconfident_turns": 3, "iterations": 5},
        {"hypotheses_formulated": 1, "hypotheses_bound": 1, "hypotheses_verified": 0},
        4,
    )
    assert merged["overconfident_turns"] == 3
