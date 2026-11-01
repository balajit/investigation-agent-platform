"""R8: a denial leaves no attempt; prompt and cascade are authorization-blind.

Drives the pure seams (prompt block, steering snapshot, cascade) plus the
execute-activity denial stamp. Written first, red before the fix.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from investigation_agent_platform.application.worker.workflows.forcing import (
    PostReasonCascadeState,
    _carry_gap_orders,
    apply_post_reason_cascade,
)
from investigation_agent_platform.domain.investigation.loop_version import LOOP_VERSION_LVR
from investigation_agent_platform.domain.profile.models import (
    ApplicationProfile,
    InvestigationConfig,
    ProfileStatus,
    Service,
    ServiceCode,
)


def _profile(
    evidence_types,
    capabilities,
    *,
    status=ProfileStatus.ACTIVE,
    version=2,
) -> ApplicationProfile:
    return ApplicationProfile(
        id="app1",
        tenant_id="t1",
        name="n",
        description="d",
        environment="production",
        version=version,
        status=status,
        investigation=InvestigationConfig(enabledEvidenceTypes=list(evidence_types)),
        allowed_capabilities=list(capabilities),
        services=[
            Service(
                name="a",
                code=ServiceCode(org="org", repos=["a"], source_roots=["src"]),
            )
        ],
    )


class _Repos:
    def __init__(self, revisions):  # type: ignore[no-untyped-def]
        self._revisions = revisions

    async def list_revisions(self, tenant_id, application_id):  # type: ignore[no-untyped-def]
        assert (tenant_id, application_id) == ("t1", "app1")
        return self._revisions

    async def get_by_application_id(self, tenant_id, application_id):  # type: ignore[no-untyped-def]
        revisions = [r for r in self._revisions if r.status == ProfileStatus.ACTIVE]
        actives = sorted(revisions, key=lambda r: r.version, reverse=True)
        return actives[0] if actives else None


def _coordinator(revisions):  # type: ignore[no-untyped-def]
    from investigation_agent_platform.application.investigation.reasoning import (
        ReasoningCoordinator,
    )

    # Allow-all safety double: these tests target capability gates, not the
    # safety gate (WP-08 requires a usable policy at construction).
    policy = AsyncMock()
    policy.validate_prompt_safety = AsyncMock(return_value=True)
    return ReasoningCoordinator(policy, profile_repo=_Repos(revisions))


def _prompt_profile() -> ApplicationProfile:
    # Declares SEARCH_LOGS and GET_CODE; enables code but not logs.
    return _profile(["SOURCE_CODE", "COMMIT_HISTORY"], ["SEARCH_LOGS", "GET_CODE"])


@pytest.mark.asyncio
async def test_r8_t01_prompt_honors_evidence_type_gate() -> None:
    coordinator = _coordinator([_prompt_profile()])
    block = await coordinator._application_context_block("t1", "app1", loop_version=LOOP_VERSION_LVR)
    assert block["allowed_capabilities"] == ["GET_CODE"]
    unavailable = {item["action"]: item["reason"] for item in block["unavailable_actions"]}
    assert unavailable["SEARCH_LOGS"] == "evidence_type_not_enabled"
    assert "GET_CODE" not in unavailable


@pytest.mark.asyncio
async def test_r8_t02_prompt_uses_authorizer_revision() -> None:
    draft = _profile(
        ["LOG", "SOURCE_CODE"], ["SEARCH_LOGS", "GET_CODE", "QUERY_STATE"], version=2,
        status=ProfileStatus.DRAFT,
    )
    active = _profile(["LOG"], ["SEARCH_LOGS"], version=1)
    coordinator = _coordinator([draft, active])
    block = await coordinator._application_context_block("t1", "app1", loop_version=LOOP_VERSION_LVR)
    assert block["allowed_capabilities"] == ["SEARCH_LOGS"]


@pytest.mark.asyncio
async def test_r8_t03_pre_lvr_run_keeps_legacy_block() -> None:
    coordinator = _coordinator([_prompt_profile()])
    block = await coordinator._application_context_block("t1", "app1", loop_version="lv7")
    assert block["allowed_capabilities"] == ["SEARCH_LOGS", "GET_CODE"]
    assert "unavailable_actions" not in block


@pytest.mark.asyncio
async def test_r8_t04_steering_fails_open() -> None:
    from investigation_agent_platform.application.security.capability_steering import (
        steering_snapshot,
    )

    class _Raising:
        async def list_revisions(self, tenant_id, application_id):  # type: ignore[no-untyped-def]
            raise RuntimeError("store down")

    unavailable, known = await steering_snapshot(_Raising(), "t1", "app1")
    assert (unavailable, known) == ((), False)
    unavailable, known = await steering_snapshot(None, "t1", "app1")
    assert (unavailable, known) == ((), False)


def _state(**over):  # type: ignore[no-untyped-def]
    base = PostReasonCascadeState(
        reason_data={
            "next_action": "GET_CODE",
            "action_parameters": {"file_path": "svc/new.py", "start_line": 1, "end_line": 20},
        },
        action_history=[],
        evidence_banked_total=5,
        triage_route="BUG",
        description="where is the order filter implemented",
        knowledge_seed={},
        max_iterations=10,
        iteration=3,
        prev_focus={
            "surface": "svc/old.py",
            "surface_kind": "file",
            "line_start": 1,
            "line_end": 50,
        },
        is_defer=False,
        d6_fired=False,
        absence_forced=False,
        counts={},
        formulated_count=0,
        tenant_id="tenant-a",
        investigation_id="inv-1",
        loop_version=LOOP_VERSION_LVR,
    )
    for key, value in over.items():
        setattr(base, key, value)
    return base


def _log_setup(**over):  # type: ignore[no-untyped-def]
    return _state(
        reason_data={
            "next_action": "FIND_SYMBOL",
            "action_parameters": {"symbol_name": "normalize"},
        },
        prev_focus=None,
        description="service error failing with exception in logs",
        **over,
    )


def _requery_carrier(action_type="SEARCH_CODE"):  # type: ignore[no-untyped-def]
    return {
        "kind": "work_order",
        "order_kind": "REQUERY",
        "iteration": 1,
        "turn_issued": 1,
        "claim_ref": "c1",
        "query": "order filter",
        "scope": "",
        "max_attempts": 3,
        "consumed": False,
        "order": {
            "work_order_id": "00000000-0000-0000-0000-000000000001",
            "kind": "REQUERY",
            "action_type": action_type,
            "action_parameters": {"query": "order filter"},
            "addresses": {
                "triage_route": "BUG",
                "claim_kind": "untyped",
                "has_unbeaten_rival": False,
            },
            "cost_estimate": 1,
            "seed_source": "test:carrier",
            "status": "PENDING",
        },
    }


def _gap_carrier():  # type: ignore[no-untyped-def]
    carrier = _carry_gap_orders(
        {"claim_gaps": [{"gap_id": "g1", "kind": "DEFINITION",
                          "target": {"file_path": "svc/old.py"}}]},
        2,
    )
    assert carrier is not None
    return carrier


def _r8_cases():  # type: ignore[no-untyped-def]
    history_two_empties = [
        {"action": "SEARCH_CODE", "evidence_added": 0, "parameters": {"query": "x"}},
        {"action": "SEARCH_CODE", "evidence_added": 0, "parameters": {"query": "x"}},
    ]
    return [
        # (rule, state maker, denied action, force flag, counter key)
        ("pivot", lambda: _state(), "GET_CODE", "forced_pivot_recollect", "pivot"),
        ("walk_stack", lambda: _state(
            knowledge_seed={"frames": [{"file": "svc/a.py", "line": 42, "function": "price"}]}
        ), "WALK_STACK", "forced_walk_stack", "walk_stack"),
        ("requery", lambda: _state(
            reason_data={"next_action": "GET_CODE",
                         "action_parameters": {"file_path": "svc/old.py"}},
            prev_focus=None,
            action_history=[_requery_carrier()],
        ), "SEARCH_CODE", "forced_requery", None),
        ("floor", lambda: _state(
            reason_data={"next_action": "FORMULATE_HYPOTHESIS"},
            evidence_banked_total=1,
            knowledge_seed={"failing_symbol": "price"},
            prev_focus=None,
        ), None, "forced_floor", "floor"),
        ("reread", lambda: _state(
            reason_data={
                "next_action": "VERIFY_HYPOTHESIS",
                "action_parameters": {},
                "hypotheses_evaluated": ["Widget is implemented in svc/new.py"],
            },
            prev_focus=None,
            formulated_count=1,
        ), "GET_CODE", "forced_reread", "reread"),
        ("callsite", lambda: _state(
            reason_data={
                "next_action": "FORMULATE_HYPOTHESIS",
                "claims": [{"claim_kind": "CALL_EDGE",
                            "target": {"file_path": "svc/caller.py", "symbol": "S"}}],
            },
            prev_focus=None,
            formulated_count=1,
        ), "GET_CODE", "forced_callsite", "callsite"),
        ("log_search", _log_setup, "SEARCH_LOGS", "forced_log_search", "log_search"),
        ("replan", lambda: _state(
            reason_data={"next_action": "SEARCH_CODE",
                         "action_parameters": {"file_path": "svc/new.py"}},
            action_history=list(history_two_empties),
            prev_focus=None,
            description="where is the order filter implemented",
        ), "SEARCH_LOGS", "forced_replan", "replan"),
        ("smallest_fetch", lambda: _state(
            reason_data={"next_action": "FORMULATE_HYPOTHESIS"},
            prev_focus=None,
            action_history=[_gap_carrier()],
            formulated_count=1,
        ), "GET_CODE", "forced_smallest_fetch", "smallest_fetch_definition"),
    ]


@pytest.mark.parametrize(
    "rule,make,denied,flag,counter", _r8_cases(),
    ids=[c[0] for c in _r8_cases()],
)
def test_r8_t07_every_rule_honors_the_set(rule, make, denied, flag, counter) -> None:  # type: ignore[no-untyped-def]
    clean = apply_post_reason_cascade(make())
    assert clean.reason_data.get(flag) is True, rule
    target = denied or str(clean.reason_data.get("next_action") or "")
    assert target, rule
    denied_state = make()
    denied_state.unavailable_actions = (target,)
    out = apply_post_reason_cascade(denied_state)
    assert out.reason_data.get(flag) is not True, rule
    if counter is not None:
        assert out.counts.get(counter, 0) == 0, rule


def test_r8_t05_log_forcing_yields_to_denied_surface() -> None:
    clean = apply_post_reason_cascade(_log_setup())
    assert clean.reason_data.get("forced_log_search") is True
    out = apply_post_reason_cascade(_log_setup(unavailable_actions=("SEARCH_LOGS",)))
    assert out.reason_data.get("forced_log_search") is not True
    assert out.counts.get("log_search", 0) == 0
    assert out.reason_data["next_action"] == "FIND_SYMBOL"


def test_r8_t06_walk_stack_yields() -> None:
    seed = {"frames": [{"file": "svc/a.py", "line": 42, "function": "price"}]}
    clean = apply_post_reason_cascade(_state(knowledge_seed=dict(seed)))
    assert clean.reason_data.get("forced_walk_stack") is True
    out = apply_post_reason_cascade(
        _state(knowledge_seed=dict(seed), unavailable_actions=("WALK_STACK",))
    )
    assert out.reason_data.get("forced_walk_stack") is not True
    assert out.counts.get("walk_stack", 0) == 0


def test_r8_t08_carried_order_for_denied_action_is_consumed() -> None:
    carrier = _requery_carrier()
    first = apply_post_reason_cascade(
        _state(
            reason_data={"next_action": "GET_CODE",
                         "action_parameters": {"file_path": "svc/old.py"}},
            prev_focus=None,
            action_history=[carrier],
            unavailable_actions=("SEARCH_CODE",),
        )
    )
    assert first.reason_data.get("forced_requery") is not True
    assert carrier.get("consumed") is True
    assert first.consumed_order_ref is None
    second = apply_post_reason_cascade(
        _state(
            reason_data={"next_action": "GET_CODE",
                         "action_parameters": {"file_path": "svc/old.py"}},
            prev_focus=None,
            action_history=[carrier],
            unavailable_actions=("SEARCH_CODE",),
        )
    )
    assert second.reason_data.get("forced_requery") is not True
    assert second.reason_data["next_action"] == "GET_CODE"


def test_r8_t09_plan_drops_unavailable_steps() -> None:
    from investigation_agent_platform.application.worker.workflows.forcing import (
        _investigation_plan,
    )

    seed = {"frames": [{"file": "svc/a.py", "line": 42}]}
    plan = _investigation_plan("BUG", seed, unavailable=frozenset({"WALK_STACK", "GET_CODE"}))
    assert plan and len(plan) <= 5
    assert "WALK_STACK" not in plan and "GET_CODE" not in plan
    everything = _investigation_plan(
        "BUG", seed,
        unavailable=frozenset({"WALK_STACK", "GET_CODE", "SEARCH_LOGS", "SEARCH_CODE",
                               "QUERY_STATE", "CORRELATE", "FIND_SYMBOL", "FIND_CALL_GRAPH",
                               "GET_CODE_HISTORY", "TRACE_LOGIC", "TRACE_JOURNEY",
                               "WALK_SESSION_GRAPH", "SEARCH_REF_DOCS", "PROBE_API"}),
    )
    assert everything and all(
        step in ("FORMULATE_HYPOTHESIS", "VERIFY_HYPOTHESIS", "CONCLUDE") for step in everything
    )


def test_r8_t10_replan_alternative_must_be_available() -> None:
    from investigation_agent_platform.application.worker.workflows.forcing import (
        _should_force_replan,
        turn_view,
    )

    history = [
        {"action": "SEARCH_CODE", "evidence_added": 0, "parameters": {"query": "x"}},
        {"action": "SEARCH_CODE", "evidence_added": 0, "parameters": {"query": "x"}},
    ]
    reason = {"next_action": "SEARCH_CODE", "action_parameters": {"file_path": "svc/new.py"}}
    turns = turn_view(list(history), LOOP_VERSION_LVR)
    clean = _should_force_replan(
        "where is the order filter implemented", dict(reason), turns, 0
    )
    assert clean is not None and clean["next_action"] == "SEARCH_LOGS"
    assert (
        _should_force_replan(
            "where is the order filter implemented", dict(reason), turns, 0,
            unavailable=frozenset({"SEARCH_LOGS"}),
        )
        is None
    )


def test_r8_t11_pre_lvr_cascade_is_unchanged() -> None:
    forced_old = 0
    burned_old = 0
    for _ in range(6):
        out = apply_post_reason_cascade(_log_setup(loop_version="lv7"))
        forced_old += 1 if out.reason_data.get("forced_log_search") is True else 0
        burned_old += int(out.counts.get("log_search", 0) or 0)
    assert forced_old > 0 and burned_old > 0
    forced_new = 0
    burned_new = 0
    for _ in range(6):
        out = apply_post_reason_cascade(
            _log_setup(loop_version=LOOP_VERSION_LVR, unavailable_actions=("SEARCH_LOGS",))
        )
        forced_new += 1 if out.reason_data.get("forced_log_search") is True else 0
        burned_new += int(out.counts.get("log_search", 0) or 0)
    assert forced_new == 0 and burned_new == 0


def _execute_ctx(profile, markers, loop_version):  # type: ignore[no-untyped-def]
    from investigation_agent_platform.api.dependencies import AppContext, set_app_context
    from investigation_agent_platform.application.investigation.services import (
        ExecuteActionService,
    )
    from investigation_agent_platform.application.worker.activities import (
        execute_action_activity,
    )
    from investigation_agent_platform.infrastructure.security.profile_authorizer import (
        ProfileBasedActionAuthorizer,
        ProfileBasedCapabilityRegistry,
    )

    repos = _Repos([profile])
    service = ExecuteActionService(
        SimpleNamespace(),
        SimpleNamespace(),
        ProfileBasedActionAuthorizer(repos),
        ProfileBasedCapabilityRegistry(repos),
    )
    ctx = AppContext()
    ctx.execute_action_service = lambda: service  # type: ignore[method-assign]
    ctx.investigation_repo = SimpleNamespace(
        get_by_id=AsyncMock(
            return_value=SimpleNamespace(
                application_id="app1", metadata={"loop_version": loop_version}
            )
        )
    )
    ctx.profile_repo = SimpleNamespace(
        get_by_application_id=AsyncMock(return_value=profile)
    )

    async def _append(tenant_id, event, investigation_id):  # type: ignore[no-untyped-def]
        markers.append(event)

    ctx.timeline_repo = SimpleNamespace(append=_append)
    set_app_context(ctx)
    return execute_action_activity


def _execute_input(action, loop_version):  # type: ignore[no-untyped-def]
    from investigation_agent_platform.application.worker.activities import ExecuteActionInput

    return ExecuteActionInput(
        tenant_id="t1",
        investigation_id=str(uuid4()),
        action=action,
        parameters={},
        loop_version=loop_version,
    )


@pytest.mark.asyncio
async def test_r8_t04_activity_still_denies_when_snapshot_fails_open() -> None:
    execute_action_activity = _execute_ctx(_profile(["SOURCE_CODE"], []), [], LOOP_VERSION_LVR)
    result = await execute_action_activity(_execute_input("GET_CODE", LOOP_VERSION_LVR))
    assert result.success is True
    assert result.data["status"] == "BLOCKED"
    assert result.data["action_outcome"]["miss_reason"] == "authorization_denied"


@pytest.mark.asyncio
async def test_r8_t12_denial_writes_attempt_and_marker() -> None:
    markers: list = []
    execute_action_activity = _execute_ctx(_profile(["SOURCE_CODE"], []), markers, LOOP_VERSION_LVR)
    result = await execute_action_activity(_execute_input("GET_CODE", LOOP_VERSION_LVR))
    outcome = result.data["action_outcome"]
    assert outcome["retrieval_attempt"]["outcome"] == "AUTH_DENIED"
    assert outcome["error_code"] == "capability_not_declared"
    assert len(markers) == 1
    attributes = markers[0].attributes
    assert attributes["miss_reason"] == "authorization_denied"
    assert attributes["action_type"] == "GET_CODE"
    assert attributes["attempt"]["outcome"] == "AUTH_DENIED"


@pytest.mark.asyncio
async def test_r8_t13_pre_lvr_denial_writes_nothing() -> None:
    markers: list = []
    execute_action_activity = _execute_ctx(_profile(["SOURCE_CODE"], []), markers, "lv7")
    result = await execute_action_activity(_execute_input("GET_CODE", "lv7"))
    assert result.data["status"] == "BLOCKED"
    assert result.data["action_outcome"].get("retrieval_attempt") is None
    assert markers == []


def test_r8_t14_denial_never_triggers_requery() -> None:
    from uuid import uuid4 as _uuid4

    from investigation_agent_platform.application.investigation.verification import (
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

    def _hyp():  # type: ignore[no-untyped-def]
        return Hypothesis(
            tenant_id="t",
            investigation_id=_uuid4(),
            title="Widget is defined in src/widgets.py",
            description="Widget is defined in src/widgets.py",
            statement="Widget is defined in src/widgets.py",
            status=HypothesisStatus.SUPPORTED,
            supporting_evidence_ids=[],
            assessments=[
                HypothesisEvidenceAssessment(
                    hypothesis_id=_uuid4(),
                    evidence_id=None,
                    assessment=AssessmentType.SUPPORTS,
                    strength=AssessmentStrength.MODERATE,
                    reason="claim unresolved: no usable evidence",
                    claim_kind=ClaimKind.DEFINITION,
                    claim_target=ClaimTarget(symbol="Widget"),
                    search_query="Widget",
                    search_scope="src/",
                    reason_code="CLAIM_UNRESOLVED",
                    schema_version="v2",
                )
            ],
        )

    recent = [{"action": "SEARCH_CODE", "parameters": {"query": "Widget"}}]
    # Negative control: the same denial classified as a failure directs a retry.
    denied_as_failure = [([], {"search_query": "Widget", "scope": "src/",
                               "outcome": "SEARCH_FAILED"})]
    assert (
        select_ledger_requery(_hyp(), [], denied_as_failure, recent, 3) is not None
    )
    # A recorded AUTH_DENIED never directs one, with or without the filter.
    denied = [([], {"search_query": "Widget", "scope": "src/", "outcome": "AUTH_DENIED"})]
    assert select_ledger_requery(_hyp(), [], denied, recent, 3) is None
    assert (
        select_ledger_requery(
            _hyp(), [], denied, recent, 3, unavailable_actions=frozenset({"SEARCH_CODE"})
        )
        is None
    )
    # A stored query whose matched action is unavailable is skipped.
    assert select_ledger_requery(_hyp(), [], [], recent, 3) is not None
    assert (
        select_ledger_requery(
            _hyp(), [], [], recent, 3, unavailable_actions=frozenset({"SEARCH_CODE"})
        )
        is None
    )
