"""G8 synthesis-forcing rule: pure-function unit tests.

The rule lives in the workflow module (workflow-safe, no Temporal imports
at test time for the pure helper) and is wired into the investigation loop
to override gathering actions with FORMULATE_HYPOTHESIS when the model
holds unevaluated hypotheses against banked evidence.
"""

from investigation_agent_platform.application.worker.workflows import (
    _absence_disconfirmation_target,
    _should_force_formulate,
    _source_value_target,
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


def test_source_value_claim_forces_exact_java_range_read():
    reason = {
        "next_action": "VERIFY_HYPOTHESIS",
        "claims": [
            {
                "claim_kind": "DEFINITION",
                "value_semantics": "EXPLICIT_INITIALIZER",
                "target": {
                    "symbol": "retryCount",
                    "declaring_type": "TransactionProcessor",
                    "file_path": "src/TransactionProcessor.java",
                    "revision": "abc123",
                },
            }
        ],
    }

    assert _source_value_target(reason, [], 0) == {
        "file_path": "src/TransactionProcessor.java",
        "field_name": "retryCount",
        "declaring_type": "TransactionProcessor",
        "revision": "abc123",
        "start_line": 1,
        "end_line": 2000,
    }


def test_absence_disconfirmation_uses_cited_provider_location_and_key():
    from types import SimpleNamespace
    from uuid import uuid4

    candidate_id = uuid4()
    row = SimpleNamespace(
        evidence_id=candidate_id,
        tenant_id="tenant-a",
        investigation_id="investigation-a",
        provenance=SimpleNamespace(
            source_location=SimpleNamespace(
                file_path="src/handler.py",
                line_start=12,
                line_end=20,
                revision="abc123",
            ),
            query_fingerprint=SimpleNamespace(operation="SEARCH_CODE"),
        ),
        attributes={
            "discovery_only": True,
            "search_scope": "repo@abc123:src",
            "match_count": 1,
        },
    )
    target = _absence_disconfirmation_target(
        {
            "claims": [
                {
                    "claim_kind": "ABSENCE",
                    "evidence_ids": [str(candidate_id)],
                    "target": {"scope": "repo@abc123:src"},
                }
            ]
        },
        [row],
        [],
        0,
    )

    assert target is not None
    assert target["next_action"] == "GET_CODE"
    assert target["action_parameters"]["file_path"] == "src/handler.py"
    assert target["action_parameters"]["start_line"] == 12
    assert target["action_parameters"]["end_line"] == 20
    assert target["action_parameters"]["revision"] == "abc123"
    assert target["action_parameters"]["absence_disconfirmation_key"].startswith(
        "ABSENCE_DISCONFIRMATION|"
    )


def test_absence_disconfirmation_rejects_locationless_or_unrelated_candidates():
    from types import SimpleNamespace
    from uuid import uuid4

    def _row(operation, path, evidence_id=None):
        return SimpleNamespace(
            evidence_id=evidence_id or uuid4(),
            tenant_id="tenant-a",
            investigation_id="investigation-a",
            provenance=SimpleNamespace(
                source_location=SimpleNamespace(
                    file_path=path,
                    line_start=None if not path else 10,
                    line_end=None if not path else 20,
                    revision="abc123",
                ),
                query_fingerprint=SimpleNamespace(operation=operation),
            ),
            attributes={"discovery_only": True, "search_scope": "other-scope"},
        )

    assert _absence_disconfirmation_target(
        {
            "claims": [
                {"claim_kind": "ABSENCE", "target": {"scope": "repo@abc123:src"}}
            ]
        },
        [_row("FIND_CALL_GRAPH", "")],
        [],
        0,
    ) is None


def test_absence_disconfirmation_rejects_segment_collision_and_empty_row():
    from uuid import uuid4

    def _row(path, count, scope="repo@abc123:src/app", tenant="tenant-a"):
        return {
            "evidence_id": str(uuid4()),
            "tenant_id": tenant,
            "investigation_id": "investigation-a",
            "attributes": {
                "discovery_only": True,
                "search_scope": scope,
                "match_count": count,
            },
            "provenance": {
                "source_location": {
                    "file_path": path,
                    "line_start": 10,
                    "line_end": 20,
                    "revision": "abc123",
                },
                "query_fingerprint": {"operation": "SEARCH_CODE"},
            },
        }

    reason = {
        "claims": [
            {"claim_kind": "ABSENCE", "target": {"scope": "repo@abc123:src/app"}}
        ]
    }
    assert _absence_disconfirmation_target(
        reason, [_row("src/application.py", 1, scope="repo@abc123:src/application")], [], 0
    ) is None
    assert _absence_disconfirmation_target(reason, [_row("src/app.py", 0)], [], 0) is None
    assert _absence_disconfirmation_target(reason, [_row("src/app.py", True)], [], 0) is None
    assert _absence_disconfirmation_target(reason, [_row("src/app.py", 1, tenant="")], [], 0) is None
def test_source_value_forcing_is_bounded_and_covers_java_and_python():
    claim = {
        "claim_kind": "DEFINITION",
        "value_semantics": "LANGUAGE_DEFAULT",
        "target": {"symbol": "retryCount", "file_path": "src/TransactionProcessor.py"},
    }
    assert _source_value_target(
        {"next_action": "VERIFY_HYPOTHESIS", "claims": [claim]}, [], 0
    ) == {
        "file_path": "src/TransactionProcessor.py",
        "field_name": "retryCount",
        "start_line": 1,
        "end_line": 2000,
    }
    java_claim = {
        **claim,
        "target": {"symbol": "retryCount", "file_path": "src/TransactionProcessor.java"},
    }
    assert (
        _source_value_target({"next_action": "VERIFY_HYPOTHESIS", "claims": [java_claim]}, [], 2)
        is None
    )
    assert (
        _source_value_target({"next_action": "VERIFY_HYPOTHESIS", "claims": [claim]}, [], 2)
        is None
    )


def test_guarded_method_return_claim_forces_method_range_read():
    claim = {
        "claim_kind": "CAUSAL",
        "fact_category": "SOURCE_VALUE_FACT",
        "value_semantics": "GUARDED_RETURN_LITERAL",
        "target": {
            "symbol": "com.sample.LegacyCodeMapper.normalize",
            "declaring_type": "com.sample.LegacyCodeMapper",
            "file_path": "src/LegacyCodeMapper.java",
            "guard_condition": "orderType == null",
        },
    }
    assert _source_value_target(
        {"next_action": "VERIFY_HYPOTHESIS", "claims": [claim]}, [], 0
    ) == {
        "file_path": "src/LegacyCodeMapper.java",
        "method_name": "normalize",
        "guard_condition": "orderType == null",
        "declaring_type": "com.sample.LegacyCodeMapper",
        "start_line": 1,
        "end_line": 2000,
    }


def test_no_force_after_cap_reached():
    assert _should_force_formulate(9, 0, ["h1"], 9, 2) is False


def test_reread_forces_get_code_for_definition_without_range_read():
    from investigation_agent_platform.application.worker.workflows import (
        _reread_target,
    )

    reason_data = {
        "next_action": "VERIFY_HYPOTHESIS",
        "hypotheses_evaluated": ["The execute action is implemented in src/worker/activities.py"],
    }
    history = [
        {"action": "SEARCH_CODE", "evidence_added": 4},
        {"action": "SEARCH_LOGS", "evidence_added": 10},
    ]
    assert _reread_target(reason_data, history, 0) == "src/worker/activities.py"


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
        _should_force_log_search(desc, err, [{"action": "SEARCH_LOGS", "evidence_added": 5}], 0)
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
        {
            "next_action": "SEARCH_CODE",
            "conclusion_readiness": 0.0,
            "hypotheses_evaluated": [],
            "observations": ["o"],
        }
    )
    assert "action=SEARCH_CODE" in plain
    assert "forced=" not in plain
    assert "outcome=" not in plain
    # Enriched entry shape: dispatched action wins, forced + outcome shown.
    enriched = _reason_summary(
        {
            "next_action": "SEARCH_CODE",
            "conclusion_readiness": 0.25,
            "hypotheses_evaluated": ["h"],
            "observations": [],
            "action": "FORMULATE_HYPOTHESIS",
            "forced_formulate": True,
            "outcome_status": "SUCCEEDED",
            "evidence_added": 2,
            "miss_reason": None,
        }
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
        {
            "llm_tokens_total": 5224,
            "llm_cost_total": 0.01,
            "formulated_count": 1,
            "forced_formulate_count": 1,
            "iterations": 6,
            "junk": "dropped",
            "flag": True,
        },
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
        "next_action": "SEARCH_CODE",
        "conclusion_readiness": 0.0,
        "hypotheses_evaluated": [],
        "observations": ["o"],
        "action_parameters": {"query": "x"},
    }
    entry = {
        "action": "SEARCH_CODE",
        "readiness": 0.0,
        "hypotheses": 0,
        "observations": 1,
        "outcome_status": "SUCCEEDED",
        "evidence_added": 3,
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
    assert (
        _classify_failure(
            "NPE in validate",
            {"frames": [{"file": "x.py"}], "error_class": "NullPointerException"},
        )
        == "BUG"
    )
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
    assert _should_force_replan("errors here", {"next_action": "QUERY_STATE"}, history, 0) is None
    assert _should_force_replan("errors here", {"next_action": "SEARCH_CODE"}, history, 2) is None
    assert (
        _should_force_replan(
            "errors here",
            {"next_action": "SEARCH_CODE"},
            [{"action": "SEARCH_CODE", "evidence_added": 0}],
            0,
        )
        is None
    )
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
    assert _should_force_synthesis(history, 14, 0, 0, {"next_action": "SEARCH_CODE"}) is True
    # Formulated already / capped / thin corpus / single action / terminal proposal.
    assert _should_force_synthesis(history, 14, 1, 0, {"next_action": "SEARCH_CODE"}) is False
    assert _should_force_synthesis(history, 14, 0, 2, {"next_action": "SEARCH_CODE"}) is False
    assert _should_force_synthesis(history, 5, 0, 0, {"next_action": "SEARCH_CODE"}) is False
    assert (
        _should_force_synthesis(
            [{"action": "SEARCH_CODE", "evidence_added": 9}],
            9,
            0,
            0,
            {"next_action": "SEARCH_CODE"},
        )
        is False
    )
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


def _floor(**overrides):
    from investigation_agent_platform.application.worker.workflows import (
        _explore_floor_target,
    )

    params = {
        "reason_data": {"next_action": "VERIFY_HYPOTHESIS", "action_parameters": {}},
        "evidence_banked_total": 1,
        "triage_route": "BUG",
        "description": "Where is SampleOrderFilter implemented?",
        "seed": {"failing_symbol": "SampleOrderFilter"},
        "forced_count": 0,
    }
    params.update(overrides)
    return _explore_floor_target(
        params["reason_data"],
        params["evidence_banked_total"],
        params["triage_route"],
        params["description"],
        params["seed"],
        params["forced_count"],
    )


def test_floor_forces_read_on_thin_terminal_proposal():
    hit = _floor()
    assert hit is not None
    assert hit["next_action"] == "FIND_SYMBOL"
    assert hit["action_parameters"]["symbol_name"] == "SampleOrderFilter"


def test_floor_reads_named_path_first():
    hit = _floor(
        description="Read src/svc/handler.py around the retry branch",
        seed={"frames": [{"file": "src/svc/handler.py"}]},
    )
    assert hit is not None
    assert hit["next_action"] == "GET_CODE"
    assert hit["action_parameters"]["file_path"] == "src/svc/handler.py"


def test_floor_prefers_path_over_symbol():
    hit = _floor(description="Who calls the handler?")
    assert hit is not None
    assert hit["next_action"] == "FIND_SYMBOL"
    assert hit["action_parameters"]["symbol_name"] == "SampleOrderFilter"


def test_floor_passes_at_or_above_threshold():
    assert _floor(evidence_banked_total=3) is None
    assert _floor(evidence_banked_total=9) is None


def test_floor_ignores_gathering_proposals():
    assert _floor(reason_data={"next_action": "SEARCH_CODE", "action_parameters": {}}) is None


def test_floor_ignores_non_code_incidents():
    assert _floor(triage_route="DATA", description="Why is revenue down?", seed={}) is None


def test_floor_respects_cap():
    assert _floor(forced_count=2) is None


def test_floor_keyword_fallback():
    hit = _floor(description="Checkout latency spikes at midnight", seed={}, triage_route="BUG")
    assert hit is not None
    assert hit["next_action"] == "SEARCH_CODE"
    assert hit["action_parameters"]["query"]


def _callsite(reason_data, history=None, count=0, **kwargs):
    from investigation_agent_platform.application.worker.workflows import (
        _callsite_target,
    )

    return _callsite_target(reason_data, history or [], count, **kwargs)


def _edge_claim(
    symbol="reserve",
    counterpart="checkout",
    file_path="src/Checkout.java",
    revision="rev-1",
):
    return {
        "statement": f"{counterpart} calls {symbol}",
        "claim_kind": "CALL_EDGE",
        "evidence_ids": [],
        "target": {
            "symbol": symbol,
            "counterpart": counterpart,
            "file_path": file_path,
            "line_start": 10,
            "line_end": 20,
            "commit_id": revision,
        },
    }


def test_callsite_forces_unread_caller_file():
    hit = _callsite({"next_action": "VERIFY_HYPOTHESIS", "claims": [_edge_claim()]})
    assert hit is not None
    assert hit["next_action"] == "GET_CODE"
    assert hit["action_parameters"]["file_path"] == "src/Checkout.java"


def test_callsite_passes_read_caller_file():
    history = [
        {
            "action": "GET_CODE",
            "evidence_added": 2,
            "parameters": {
                "file_path": "src/Checkout.java",
                "caller_edge_key": "reserve|checkout|src/Checkout.java|rev-1",
                "caller_edge_symbol": "reserve",
                "caller_edge_counterpart": "checkout",
                "start_line": "10",
                "end_line": "20",
                "revision": "rev-1",
                "caller_edge_tenant_id": "tenant-a",
                "caller_edge_investigation_id": "inv-a",
            },
        },
    ]
    assert (
        _callsite(
            {"next_action": "VERIFY_HYPOTHESIS", "claims": [_edge_claim()]},
            history,
            tenant_id="tenant-a",
            investigation_id="inv-a",
        )
        is None
    )


def test_callsite_ignores_non_edge_claims():
    claims = [dict(_edge_claim(), claim_kind="DEFINITION")]
    assert _callsite({"next_action": "VERIFY_HYPOTHESIS", "claims": claims}) is None


def test_callsite_needs_target_file():
    claims = [dict(_edge_claim(), target={"symbol": "reserve"})]
    assert _callsite({"next_action": "VERIFY_HYPOTHESIS", "claims": claims}) is None


def test_callsite_respects_cap():
    assert _callsite({"next_action": "VERIFY_HYPOTHESIS", "claims": [_edge_claim()]}, [], 2) is None


def test_callsite_passes_target_window():
    claim = _edge_claim()
    claim["target"] = dict(claim["target"], line_start=88, line_end=104)
    hit = _callsite({"next_action": "VERIFY_HYPOTHESIS", "claims": [claim]})
    assert hit is not None
    assert hit["action_parameters"]["start_line"] == 88
    assert hit["action_parameters"]["end_line"] == 104


def test_callsite_does_not_suppress_same_path_wrong_revision_read():
    history = [
        {
            "action": "GET_CODE",
            "evidence_added": 1,
            "parameters": {
                "file_path": "src/Checkout.java",
                "caller_edge_key": "reserve|checkout|src/Checkout.java|old-rev",
                "caller_edge_symbol": "reserve",
                "caller_edge_counterpart": "checkout",
                "start_line": "10",
                "end_line": "20",
                "revision": "old-rev",
                "caller_edge_tenant_id": "tenant-b",
                "caller_edge_investigation_id": "inv-b",
            },
        }
    ]
    hit = _callsite(
        {"next_action": "VERIFY_HYPOTHESIS", "claims": [_edge_claim(revision="new-rev")]},
        history,
        tenant_id="tenant-a",
        investigation_id="inv-a",
    )
    assert hit is not None
    assert hit["action_parameters"]["revision"] == "new-rev"


def test_callsite_does_not_suppress_foreign_binding_read():
    history = [
        {
            "action": "GET_CODE",
            "evidence_added": 1,
            "parameters": {
                "file_path": "src/Checkout.java",
                "caller_edge_key": "reserve|checkout|src/Checkout.java|rev-1",
                "caller_edge_symbol": "reserve",
                "caller_edge_counterpart": "checkout",
                "start_line": "10",
                "end_line": "20",
                "revision": "rev-1",
                "caller_edge_tenant_id": "tenant-b",
                "caller_edge_investigation_id": "inv-b",
            },
        }
    ]
    hit = _callsite(
        {"next_action": "VERIFY_HYPOTHESIS", "claims": [_edge_claim()]},
        history,
        tenant_id="tenant-a",
        investigation_id="inv-a",
    )
    assert hit is not None


def test_callsite_does_not_suppress_same_path_wrong_range_read():
    history = [
        {
            "action": "GET_CODE",
            "evidence_added": 1,
            "parameters": {
                "file_path": "src/Checkout.java",
                "caller_edge_key": "reserve|checkout|src/Checkout.java|rev-1",
                "caller_edge_symbol": "reserve",
                "caller_edge_counterpart": "checkout",
                "start_line": "30",
                "end_line": "40",
                "revision": "rev-1",
            },
        }
    ]
    hit = _callsite(
        {"next_action": "VERIFY_HYPOTHESIS", "claims": [_edge_claim()]},
        history,
        tenant_id="tenant-a",
        investigation_id="inv-a",
    )
    assert hit is not None
    assert hit["action_parameters"]["start_line"] == 10


def test_behavior_without_category_falls_back_to_full_window_read():
    reason = {
        "next_action": "VERIFY_HYPOTHESIS",
        "claims": [{"claim_kind": "BEHAVIOR", "target": {"symbol": "normalize"}}],
    }
    history = [
        {
            "action": "GET_CODE",
            "evidence_added": 2,
            "parameters": {"file_path": "src/LegacyCodeMapper.java"},
        }
    ]
    assert _source_value_target(reason, history, 0) == {
        "file_path": "src/LegacyCodeMapper.java",
        "start_line": 1,
        "end_line": 2000,
    }


def test_behavior_fallback_skipped_when_category_typed_or_no_banked_file():
    behavior = {
        "next_action": "VERIFY_HYPOTHESIS",
        "claims": [{"claim_kind": "BEHAVIOR", "target": {"symbol": "normalize"}}],
    }
    assert _source_value_target(behavior, [], 0) is None
    assert _source_value_target(behavior, [{"action": "SEARCH_CODE", "evidence_added": 3}], 0) is None
    typed = {
        "next_action": "VERIFY_HYPOTHESIS",
        "claims": [
            {
                "claim_kind": "BEHAVIOR",
                "fact_category": "SOURCE_VALUE_FACT",
                "target": {"symbol": "normalize"},
            }
        ],
    }
    history = [
        {
            "action": "GET_CODE",
            "evidence_added": 2,
            "parameters": {"file_path": "src/LegacyCodeMapper.java"},
        }
    ]
    assert _source_value_target(typed, history, 0) is None
    assert _source_value_target(behavior, history, 2) is None
    method_read = [
        {
            "action": "GET_CODE",
            "evidence_added": 1,
            "parameters": {"file_path": "src/LegacyCodeMapper.java", "method_name": "normalize"},
        }
    ]
    assert _source_value_target(behavior, method_read, 0) is None
