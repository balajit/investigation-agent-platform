# tests/unit/test_g3_retrieval_benefit.py
"""G3 retrieval-benefit gate (design: docs/IAP-proof-kernel-g3-design-v1.0.md).

Step 1 vectors: durable budget authority, unknown-citation eligibility,
normalized directive scope, and stable attempt IDs. Later steps extend
this file per D7 (claim-join goldens, forcing-rule state machines,
guardrail negatives, battery smoke).
"""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from investigation_agent_platform.application.evidence.retrieval_ledger import (
    WP_A3_DEFAULT_MAX_ATTEMPTS,
    ClaimRetrievalBinding,
    ClaimRetrievalLedger,
    attempt_id_for,
    count_persisted_attempts,
    re_retrieval_directive,
    recorded_retrieval_attempts,
)
from investigation_agent_platform.application.investigation.verification import (
    collect_verification_diagnostics,
    select_ledger_requery,
)


def _binding(*, unknown: list[str]) -> ClaimRetrievalBinding:
    return ClaimRetrievalBinding(
        claim_key="k|CAUSAL", claim_ref="abcdef0123456789", unknown=list(unknown)
    )


def _ledger(
    *, query: str = "SampleOrderFilter", scope: str | None = "repo@HEAD:/:query=q"
) -> ClaimRetrievalLedger:
    return ClaimRetrievalLedger(claim_key="k|CAUSAL", search_query=query, scope=scope)


def _recorded(query: str, scope: str | None, n: int) -> list[tuple[list[str], dict]]:
    return [(["row-1"], {"search_query": query, "scope": scope}) for _ in range(n)]


def test_unknown_citations_require_zero_persisted_attempts() -> None:
    binding = _binding(unknown=["ev-1"])
    assert (
        re_retrieval_directive(
            binding,
            _ledger(),
            None,
            [],
            "SEARCH_CODE",
            {"path_prefix": "/"},
        )
        is not None
    )
    assert (
        re_retrieval_directive(
            binding,
            _ledger(),
            None,
            _recorded("SampleOrderFilter", "repo@HEAD:/:query=q", 1),
            "SEARCH_CODE",
            {"path_prefix": "/"},
        )
        is None
    )


def test_budget_uses_durable_count() -> None:
    binding = _binding(unknown=[])
    recorded = _recorded("SampleOrderFilter", "repo@HEAD:/:query=q", WP_A3_DEFAULT_MAX_ATTEMPTS)
    assert (
        re_retrieval_directive(
            binding,
            _ledger(),
            "SEARCH_FAILED",
            recorded,
            "SEARCH_CODE",
            {"path_prefix": "/"},
        )
        is None
    )
    assert (
        re_retrieval_directive(
            binding,
            _ledger(),
            "SEARCH_FAILED",
            _recorded("SampleOrderFilter", "repo@HEAD:/:query=q", 1),
            "SEARCH_CODE",
            {"path_prefix": "/"},
        )
        is not None
    )


def test_directive_requires_recorded_attempts() -> None:
    binding = _binding(unknown=["ev-1"])
    with pytest.raises(TypeError):
        re_retrieval_directive(  # type: ignore[call-arg]
            binding,
            _ledger(),
            None,
            "SEARCH_CODE",
            {"path_prefix": "/"},
        )


def test_directive_scope_is_normalized() -> None:
    binding = _binding(unknown=["ev-1"])
    directive = re_retrieval_directive(
        binding,
        _ledger(scope="  repo@HEAD:/:query=q  "),
        None,
        [],
        "SEARCH_CODE",
        {"path_prefix": "/"},
    )
    assert directive is not None
    assert directive["scope"] == "repo@HEAD:/:query=q"
    none_scope = re_retrieval_directive(
        binding,
        _ledger(query="q", scope=None),
        None,
        [],
        "SEARCH_CODE",
        {"path_prefix": "/"},
    )
    assert none_scope is not None
    assert none_scope["scope"] == ""


def test_directive_carries_durable_index() -> None:
    binding = _binding(unknown=[])
    directive = re_retrieval_directive(
        binding,
        _ledger(),
        "SEARCH_FAILED",
        _recorded("SampleOrderFilter", "repo@HEAD:/:query=q", 2),
        "SEARCH_CODE",
        {"path_prefix": "/"},
    )
    assert directive is not None
    assert directive["attempt_index"] == 2


def test_count_persisted_attempts_normalizes_scope() -> None:
    recorded = _recorded("q", "  s  ", 2) + _recorded("q", "other", 1)
    assert count_persisted_attempts(recorded, "q", "s") == 2
    assert count_persisted_attempts(recorded, "  q ", None) == 0


def test_recorded_attempts_preserve_top_level_attempt_id() -> None:
    events = [
        SimpleNamespace(
            event_type="RETRIEVAL_ATTEMPT",
            attributes={
                "attempt_id": "abc123",
                "evidence_ids": ["row-1"],
                "attempt": {"search_query": "q", "scope": "s"},
            },
        ),
        SimpleNamespace(event_type="STATE_CHANGE", attributes={}),
    ]
    recorded = recorded_retrieval_attempts(events)
    assert len(recorded) == 1
    assert recorded[0][0] == ["row-1"]
    assert recorded[0][1]["attempt_id"] == "abc123"


def test_retrieval_attempt_event_type_round_trips() -> None:
    from datetime import UTC, datetime

    from investigation_agent_platform.domain.timeline.models import (
        TimelineEvent,
        TimelineEventType,
    )
    from investigation_agent_platform.infrastructure.persistence.timeline_repository import (
        SqlAlchemyTimelineRepository,
    )

    assert TimelineEventType("RETRIEVAL_ATTEMPT") is TimelineEventType.RETRIEVAL_ATTEMPT
    row = SimpleNamespace(
        id=uuid4(),
        tenant_id="t-1",
        investigation_id=uuid4(),
        timestamp=datetime.now(UTC),
        event_type="RETRIEVAL_ATTEMPT",
        description="search_code attempt abc123: SUCCEEDED (3 banked)",
        attributes={"attempt_id": "abc123", "evidence_ids": ["row-1"]},
        entity_ids=[],
        evidence_ids=[],
    )
    event = SqlAlchemyTimelineRepository._from_orm(row)
    assert isinstance(event, TimelineEvent)
    assert event.event_type is TimelineEventType.RETRIEVAL_ATTEMPT
    assert event.evidence_ids == []
    assert event.attributes["evidence_ids"] == ["row-1"]


def test_verify_evaluation_event_type_round_trips() -> None:
    from datetime import UTC, datetime

    from investigation_agent_platform.domain.timeline.models import (
        TimelineEvent,
        TimelineEventType,
    )
    from investigation_agent_platform.infrastructure.persistence.timeline_repository import (
        SqlAlchemyTimelineRepository,
    )

    assert TimelineEventType("VERIFY_EVALUATION") is TimelineEventType.VERIFY_EVALUATION
    row = SimpleNamespace(
        id=uuid4(),
        tenant_id="t-1",
        investigation_id=uuid4(),
        timestamp=datetime.now(UTC),
        event_type="VERIFY_EVALUATION",
        description="verify evaluation turn 2: 1 bindings, 0 missing",
        attributes={"turn": 2, "bindings": [], "ledgers": []},
        entity_ids=[],
        evidence_ids=[],
    )
    event = SqlAlchemyTimelineRepository._from_orm(row)
    assert isinstance(event, TimelineEvent)
    assert event.event_type is TimelineEventType.VERIFY_EVALUATION
    assert event.attributes["turn"] == 2


@pytest.mark.asyncio
async def test_retrieval_marker_carries_action_execution_id() -> None:
    from unittest.mock import AsyncMock

    from investigation_agent_platform.api.dependencies import AppContext, set_app_context
    from investigation_agent_platform.application.worker.activities import (
        ExecuteActionInput,
        execute_action_activity,
    )

    ctx = AppContext()
    appended: list = []

    async def _tl_find(tenant_id: str, investigation_id: object) -> list:
        return []

    async def _tl_append(tenant_id: str, event: object, investigation_id: object) -> None:
        appended.append(event)

    service = SimpleNamespace(
        authorize=AsyncMock(return_value=None),
        execute=AsyncMock(return_value=None),
    )
    ctx.execute_action_service = lambda: service  # type: ignore[method-assign]
    ctx.action_repo = SimpleNamespace(count_by_investigation=AsyncMock(return_value=0))
    ctx.evidence_repo = SimpleNamespace(
        find_by_investigation_id=AsyncMock(return_value=[]),
        save_batch=AsyncMock(return_value=None),
    )
    ctx.profile_repo = SimpleNamespace(get_by_application_id=AsyncMock(return_value=None))
    ctx.evidence_gateway = SimpleNamespace(
        search_runtime_evidence=AsyncMock(
            return_value=SimpleNamespace(items=[SimpleNamespace(id="row-1")])
        )
    )
    ctx.timeline_repo = SimpleNamespace(  # type: ignore[attr-defined]
        find_by_investigation_id=_tl_find,
        append=_tl_append,
    )
    set_app_context(ctx)
    result = await execute_action_activity(
        ExecuteActionInput(
            tenant_id="tenant-a",
            investigation_id=str(uuid4()),
            action="SEARCH_LOGS",
            parameters={"keywords": ["k"], "limit": 5},
        )
    )
    assert result.success is True
    markers = [
        e
        for e in appended
        if str(getattr(getattr(e, "event_type", ""), "value", "")) == "RETRIEVAL_ATTEMPT"
    ]
    assert len(markers) == 1
    assert (
        markers[0].attributes["action_execution_id"] == result.data["action_outcome"]["action_id"]
    )


def test_from_orm_maps_unknown_event_type() -> None:
    from datetime import UTC, datetime

    from investigation_agent_platform.domain.timeline.models import TimelineEventType
    from investigation_agent_platform.infrastructure.persistence.timeline_repository import (
        SqlAlchemyTimelineRepository,
    )

    row = SimpleNamespace(
        id=uuid4(),
        tenant_id="t-1",
        investigation_id=uuid4(),
        timestamp=datetime.now(UTC),
        event_type="FUTURE_TYPE_FROM_NEWER_CODE",
        description="forward-compat probe",
        attributes={"attempt_id": "abc123"},
        entity_ids=[],
        evidence_ids=[],
    )
    event = SqlAlchemyTimelineRepository._from_orm(row)
    assert event.event_type is TimelineEventType.UNKNOWN
    assert event.attributes["attempt_id"] == "abc123"


def test_count_persisted_attempts_dedupes_stable_ids() -> None:
    recorded = _recorded("q", "s", 1) + _recorded("q", "s", 1)
    assert count_persisted_attempts(recorded, "q", "s") == 2
    identified = [
        (["row-1"], {"search_query": "q", "scope": "s", "attempt_id": "same-id"}),
        (["row-1"], {"search_query": "q", "scope": "s", "attempt_id": "same-id"}),
        (["row-1"], {"search_query": "q", "scope": "s", "attempt_id": "other-id"}),
    ]
    assert count_persisted_attempts(identified, "q", "s") == 2


def _directive_result(**overrides: object) -> dict:
    data = {
        "query": "SampleOrderFilter",
        "scope": "repo@HEAD:/:query=q",
        "action_type": "SEARCH_CODE",
        "action_parameters": {"path_prefix": "/"},
        "claim_ref": "abcdef0123456789",
        "attempt_index": 0,
        "max_attempts": 3,
    }
    data.update(overrides)
    return {"ledger_requery": data}


def _carrier(**overrides: object) -> dict:
    """Carried REQUERY order entry (2b step 5 carrier shape)."""
    entry: dict = {
        "kind": "work_order",
        "order_kind": "REQUERY",
        "iteration": 1,
        "turn_issued": 1,
        "claim_ref": "abc",
        "query": "q",
        "scope": "",
        "max_attempts": 3,
        "consumed": False,
        "order": {
            "work_order_id": "00000000-0000-0000-0000-000000000001",
            "kind": "REQUERY",
            "action_type": "SEARCH_CODE",
            "action_parameters": {},
            "addresses": {
                "triage_route": "BUG",
                "claim_kind": "untyped",
                "has_unbeaten_rival": False,
            },
            "cost_estimate": 1,
            "max_attempts": 3,
            "seed_source": "test:carrier",
            "status": "PENDING",
        },
    }
    entry.update(overrides)
    return entry


def test_order_forcing_emits_typed_action() -> None:
    from investigation_agent_platform.application.worker.workflows import (
        _carry_requery_order,
        _should_force_order_requery,
    )

    carrier = _carry_requery_order(_directive_result(), 3)
    assert carrier is not None
    assert carrier["kind"] == "work_order"
    assert carrier["order_kind"] == "REQUERY"
    assert carrier["turn_issued"] == 3 == carrier["iteration"]
    assert carrier["consumed"] is False
    assert carrier["scope"] == "repo@HEAD:/:query=q"
    assert carrier["order"]["action_type"] == "SEARCH_CODE"
    assert carrier["order"]["status"] == "PENDING"
    history = [{"iteration": 0, "action": "SEARCH_LOGS"}, carrier]
    override = _should_force_order_requery(history, [])
    assert override is not None
    assert override["action_type"] == "SEARCH_CODE"
    assert override["action_parameters"] == {"path_prefix": "/"}
    assert override["order_index"] == 1


def test_order_verify_result_carried_to_next_cascade() -> None:
    from investigation_agent_platform.application.worker.workflows import (
        _carry_requery_order,
        _should_force_order_requery,
    )

    carrier = _carry_requery_order(_directive_result(scope=None), 2)
    assert carrier is not None
    assert carrier["scope"] == ""
    history = [carrier]
    fired = _should_force_order_requery(history, [])
    assert fired is not None and "action_type" in fired
    # Dispatched turn stamps the entry and retires the carrier; a later
    # turn sees the stamp and does not re-fire.
    carrier["consumed"] = True
    history.append(
        {
            "iteration": 3,
            "action": fired["action_type"],
            "requery_consumed": {
                "claim_ref": carrier["claim_ref"],
                "query": carrier["query"],
                "scope": carrier["scope"],
                "turn": 3,
                "outcome_status": "EMPTY",
            },
        }
    )
    assert _should_force_order_requery(history, []) is None


def test_order_forcing_consumed_no_repeat() -> None:
    from investigation_agent_platform.application.worker.workflows import (
        _should_force_order_requery,
    )

    assert _should_force_order_requery([_carrier(consumed=True)], []) is None


def test_order_failed_dispatch_stays_consumed() -> None:
    from investigation_agent_platform.application.worker.workflows import (
        _should_force_order_requery,
    )

    carrier = _carrier(scope="s")
    history = [
        carrier,
        {
            "iteration": 2,
            "action": "SEARCH_CODE",
            "requery_consumed": {
                "claim_ref": "abc",
                "query": "q",
                "scope": "s",
                "turn": 2,
                "outcome_status": "FAILED",
            },
        },
    ]
    result = _should_force_order_requery(history, [])
    assert result is None or "action_type" not in result


def test_order_forcing_budget_exhausted_no_repeat() -> None:
    from investigation_agent_platform.application.worker.workflows import (
        _should_force_order_requery,
    )

    recorded = [
        (["row-1"], {"search_query": "q", "scope": "s", "attempt_id": "id-1"}),
        (["row-2"], {"search_query": "q", "scope": "s", "attempt_id": "id-2"}),
        (["row-3"], {"search_query": "q", "scope": "s", "attempt_id": "id-3"}),
    ]
    result = _should_force_order_requery([_carrier(scope="s")], recorded)
    assert result is None or "action_type" not in result


def test_order_carry_rejects_malformed_directive() -> None:
    from investigation_agent_platform.application.worker.workflows import (
        _carry_requery_order,
        _should_force_order_requery,
    )

    assert _carry_requery_order(_directive_result(action_type="NOPE"), 1) is None
    assert _carry_requery_order(_directive_result(query="  "), 1) is None
    assert _carry_requery_order({"ledger_requery": "prose"}, 1) is None
    assert (
        _carry_requery_order(
            _directive_result(action_parameters={"path_prefix": {"nested": "no-scalars"}}),
            1,
        )
        is None
    )
    # A corrupt carrier payload never dispatches — retired as exhausted.
    corrupt = _carrier(order={"kind": "REQUERY"})
    result = _should_force_order_requery([corrupt], [])
    assert result is not None
    assert result.get("mark_consumed") == "budget-exhausted"
    assert "action_type" not in result


def test_order_scope_normalization() -> None:
    from investigation_agent_platform.application.worker.workflows import (
        _carry_requery_order,
    )

    assert _carry_requery_order(_directive_result(scope=None), 0)["scope"] == ""
    assert _carry_requery_order(_directive_result(scope="  s  "), 0)["scope"] == "s"


def _verify_harness(
    *,
    hypotheses: list,
    inv_metadata: dict,
    timeline_events: list,
) -> None:
    from unittest.mock import AsyncMock

    from investigation_agent_platform.api.dependencies import AppContext, set_app_context

    ctx = AppContext()

    async def _get(tenant_id: str, investigation_id: object) -> object:
        return SimpleNamespace(metadata=dict(inv_metadata))

    ctx.investigation_repo = SimpleNamespace(get_by_id=_get)  # type: ignore[attr-defined]
    ctx.evidence_repo = SimpleNamespace(  # type: ignore[attr-defined]
        find_by_investigation_id=AsyncMock(return_value=[]),
    )
    ctx.hypothesis_repo = SimpleNamespace(  # type: ignore[attr-defined]
        find_by_investigation_id=AsyncMock(return_value=list(hypotheses)),
        save=AsyncMock(),
    )
    ctx.timeline_repo = SimpleNamespace(  # type: ignore[attr-defined]
        find_by_investigation_id=AsyncMock(return_value=list(timeline_events)),
    )
    set_app_context(ctx)


def _claim_hypothesis(inv_id: object) -> object:
    from investigation_agent_platform.domain.hypothesis.models import (
        AssessmentStrength,
        AssessmentType,
        Hypothesis,
        HypothesisEvidenceAssessment,
        HypothesisStatus,
    )

    missing = uuid4()
    return Hypothesis(
        tenant_id="t",
        investigation_id=inv_id,  # type: ignore[arg-type]
        title="SampleOrderFilter",
        statement="SampleOrderFilter",
        description="SampleOrderFilter",
        status=HypothesisStatus.SUPPORTED,
        supporting_evidence_ids=[],
        assessments=[
            HypothesisEvidenceAssessment(
                hypothesis_id=uuid4(),
                evidence_id=missing,
                assessment=AssessmentType.NEUTRAL,
                strength=AssessmentStrength.WEAK,
                reason="unresolved citation",
            )
        ],
    )


def _failed_attempt_event() -> object:
    return SimpleNamespace(
        event_type="RETRIEVAL_ATTEMPT",
        attributes={
            "attempt_id": "abc123",
            "evidence_ids": [],
            "attempt": {
                "search_query": "SampleOrderFilter",
                "scope": None,
                "outcome": "SEARCH_FAILED",
            },
        },
    )


def _matching_recent_actions() -> list:
    # Same-turn view of the persisted _failed_attempt_event: same stable
    # id so the merge treats them as one attempt, not two.
    return [
        {
            "iteration": 0,
            "action": "SEARCH_CODE",
            "parameters": {"path_prefix": "/"},
            "attempt_id": "abc123",
            "evidence_ids": [],
            "retrieval_attempt": {
                "search_query": "SampleOrderFilter",
                "scope": None,
                "outcome": "SEARCH_FAILED",
            },
        }
    ]


@pytest.mark.asyncio
async def test_verify_activity_emits_ledger_requery() -> None:
    from investigation_agent_platform.application.worker.activities import (
        VerifyRootCauseInput,
        verify_root_cause_activity,
    )

    inv_id = uuid4()
    _verify_harness(
        hypotheses=[_claim_hypothesis(inv_id)],
        inv_metadata={},
        timeline_events=[_failed_attempt_event()],
    )
    result = await verify_root_cause_activity(
        VerifyRootCauseInput(
            tenant_id="t",
            investigation_id=str(inv_id),
            recent_actions=_matching_recent_actions(),
        )
    )
    assert result.success is True
    data = result.data
    assert data["ledger_requery"] is not None
    assert data["ledger_requery"]["action_type"] == "SEARCH_CODE"
    assert data["ledger_requery"]["query"] == "SampleOrderFilter"
    assert data["ledger_requery"]["claim_ref"]


def test_recorded_attempt_summaries_converts_and_carries_id() -> None:
    from investigation_agent_platform.application.evidence.retrieval_ledger import (
        recorded_attempt_summaries,
    )

    assert recorded_attempt_summaries([]) is None
    assert recorded_attempt_summaries([{"iteration": 0, "action": "SEARCH_CODE"}]) is None
    assert recorded_attempt_summaries([{"retrieval_attempt": None}]) is None
    converted = recorded_attempt_summaries(
        [
            {"iteration": 0, "action": "SEARCH_LOGS"},
            {
                "iteration": 1,
                "action": "SEARCH_CODE",
                "attempt_id": "sum-1",
                "evidence_ids": ["row-1", "row-2"],
                "retrieval_attempt": {"search_query": "q", "scope": "s"},
            },
        ]
    )
    assert converted is not None
    assert len(converted) == 1
    banked, attempt = converted[0]
    assert banked == ["row-1", "row-2"]
    assert attempt["attempt_id"] == "sum-1"
    assert attempt["search_query"] == "q"


def test_merge_recorded_attempts_dedupes_by_id() -> None:
    from investigation_agent_platform.application.evidence.retrieval_ledger import (
        merge_recorded_attempts,
    )

    persisted = [
        (["row-1"], {"search_query": "q", "scope": "s", "attempt_id": "abc123"}),
    ]
    summaries = [
        ([], {"search_query": "q", "scope": "s", "attempt_id": "abc123"}),
        ([], {"search_query": "q", "scope": "s", "attempt_id": "novel-1"}),
        ([], {"search_query": "q", "scope": "s"}),
    ]
    merged = merge_recorded_attempts(persisted, summaries)
    assert len(merged) == 3
    assert merged[0][1]["attempt_id"] == "abc123"
    assert merged[1][1]["attempt_id"] == "novel-1"
    assert "attempt_id" not in merged[2][1]
    assert merge_recorded_attempts([], []) == []
    assert merge_recorded_attempts(None, None) == []


@pytest.mark.asyncio
async def test_verify_activity_falls_back_to_summaries_without_timeline() -> None:
    from investigation_agent_platform.api.dependencies import get_app_context
    from investigation_agent_platform.application.worker.activities import (
        VerifyRootCauseInput,
        verify_root_cause_activity,
    )

    inv_id = uuid4()
    _verify_harness(
        hypotheses=[_claim_hypothesis(inv_id)],
        inv_metadata={},
        timeline_events=[],
    )
    get_app_context().timeline_repo = None  # type: ignore[attr-defined]
    result = await verify_root_cause_activity(
        VerifyRootCauseInput(
            tenant_id="t",
            investigation_id=str(inv_id),
            recent_actions=_matching_recent_actions(),
        )
    )
    assert result.success is True
    assert result.data["ledger_requery"] is not None
    assert result.data["ledger_requery"]["action_type"] == "SEARCH_CODE"


@pytest.mark.asyncio
async def test_verify_activity_fails_closed_without_any_history() -> None:
    from investigation_agent_platform.api.dependencies import get_app_context
    from investigation_agent_platform.application.worker.activities import (
        VerifyRootCauseInput,
        verify_root_cause_activity,
    )

    inv_id = uuid4()
    _verify_harness(
        hypotheses=[_claim_hypothesis(inv_id)],
        inv_metadata={},
        timeline_events=[],
    )
    get_app_context().timeline_repo = None  # type: ignore[attr-defined]
    result = await verify_root_cause_activity(
        VerifyRootCauseInput(
            tenant_id="t",
            investigation_id=str(inv_id),
            recent_actions=[{"iteration": 0, "action": "SEARCH_LOGS"}],
        )
    )
    assert result.success is True
    assert result.data["ledger_requery"] is None


@pytest.mark.asyncio
async def test_verify_activity_legacy_omits_ledger_requery() -> None:
    from investigation_agent_platform.application.worker.activities import (
        VerifyRootCauseInput,
        verify_root_cause_activity,
    )

    inv_id = uuid4()
    _verify_harness(
        hypotheses=[_claim_hypothesis(inv_id)],
        inv_metadata={"verifier_version": "v1"},
        timeline_events=[_failed_attempt_event()],
    )
    result = await verify_root_cause_activity(
        VerifyRootCauseInput(
            tenant_id="t",
            investigation_id=str(inv_id),
            recent_actions=_matching_recent_actions(),
        )
    )
    assert result.success is True
    assert "ledger_requery" not in result.data


def test_ledger_directive_never_alters_verdict() -> None:
    from investigation_agent_platform.application.investigation.verification import (
        RootCauseVerificationPolicy,
        VerificationEngine,
    )
    from investigation_agent_platform.domain.hypothesis.models import (
        Hypothesis,
        HypothesisStatus,
    )

    iid = uuid4()
    hyp = Hypothesis(
        tenant_id="t",
        investigation_id=iid,
        title="cache causes outage",
        description="cache causes outage",
        statement="cache causes outage",
        status=HypothesisStatus.PROPOSED,
    )
    engine = VerificationEngine()
    policy = RootCauseVerificationPolicy()
    plain = engine.evaluate_hypothesis(hyp, [], [], [], policy)
    with_attempts = engine.evaluate_hypothesis(
        hyp,
        [],
        [],
        [],
        policy,
        recorded_attempts=[
            (["row-9"], {"search_query": "cache", "scope": "s"}),
        ],
    )
    assert with_attempts.status == plain.status
    assert with_attempts.explanation == plain.explanation
    assert with_attempts.confidence == plain.confidence
    assert with_attempts.corroborating_evidence_ids == plain.corroborating_evidence_ids


def test_attempt_id_for_is_stable_and_scope_insensitive() -> None:
    first = attempt_id_for("inv-1", 3, "search_code", "q", "  s ")
    assert first == attempt_id_for("inv-1", 3, "SEARCH_CODE", "q", "s")
    assert first != attempt_id_for("inv-1", 3, "SEARCH_CODE", "q", None)
    assert first != attempt_id_for("inv-1", 3, "SEARCH_CODE", "other", "s")
    assert first != attempt_id_for("inv-1", 4, "SEARCH_CODE", "q", "s")


def _hyp(*, statement: str = "Cache causes outage", cited: list[str]) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        statement=statement,
        supporting_evidence_ids=list(cited),
        refuting_evidence_ids=[],
        assessments=[SimpleNamespace(claim_kind=None, claim_target={}, cited_ids=list(cited))],
        causal_steps=[],
    )


def _supported(corroborating: list[str]) -> SimpleNamespace:
    return SimpleNamespace(
        status="SUPPORTED",
        tier="established",
        limitations=[],
        corroborating_evidence_ids=list(corroborating),
    )


def test_bindings_returned_without_other_diagnostics() -> None:
    hyp = _hyp(cited=["ev1"])
    evidence = [SimpleNamespace(evidence_id="ev1")]
    recorded = [(["ev1"], {"search_query": "q", "scope": None, "outcome": "ROWS_RETURNED"})]
    diag = collect_verification_diagnostics(hyp, evidence, _supported(["ev1"]), recorded)
    assert diag is not None
    assert diag.evaporated_ids == []
    assert len(diag.claim_bindings) == 1
    assert diag.claim_bindings[0].unmapped is False
    assert diag.claim_bindings[0].claim_local == ["ev1"]


def test_select_fails_closed_without_attempt_history() -> None:
    hyp = _hyp(statement="lookup Foo", cited=["ghost"])
    recent = [
        {
            "action": "SEARCH_CODE",
            "parameters": {"path_prefix": "/"},
            "retrieval_attempt": {"search_query": "lookup Foo", "scope": None},
        }
    ]
    assert select_ledger_requery(hyp, [], None, recent) is None
    assert select_ledger_requery(hyp, [], [], recent) is not None


def test_omitted_unknown_ids_keep_claim_ref() -> None:
    cited = [f"ev-{i}" for i in range(55)]
    hyp = _hyp(cited=cited)
    diag = collect_verification_diagnostics(hyp, [], _supported([]), [])
    assert diag is not None
    assert len(diag.claim_bindings) == 1
    binding = diag.claim_bindings[0]
    assert binding.unmapped is True
    assert len(binding.unknown) == 50
    assert binding.omitted_unknown == 5
    assert {e.cited_by for e in diag.evaporated_ids} == {binding.claim_ref}


def test_claim_ref_fits_capped_fields_for_long_statements() -> None:
    from investigation_agent_platform.application.evidence.retrieval_ledger import (
        claim_key,
        claim_ref,
    )

    key = claim_key("x" * 2048, "CAUSAL")
    ref = claim_ref(key)
    assert len(ref) == 16
    assert len(ref) <= 64
    assert ref == claim_ref(claim_key("x" * 2048, "CAUSAL"))
    assert ref != claim_ref(claim_key("y" * 2048, "CAUSAL"))


def test_distinct_claims_split_by_kind_and_target() -> None:
    from investigation_agent_platform.application.evidence.retrieval_ledger import (
        _distinct_typed_claims,
    )

    hyp = SimpleNamespace(
        id=uuid4(),
        statement="Shared statement",
        supporting_evidence_ids=[],
        refuting_evidence_ids=[],
        assessments=[
            SimpleNamespace(claim_kind="CAUSAL", claim_target={"symbol": "A"}, cited_ids=["a1"]),
            SimpleNamespace(claim_kind="CAUSAL", claim_target={"symbol": "B"}, cited_ids=["b1"]),
            SimpleNamespace(
                claim_kind="COUNTERPART",
                claim_target={"symbol": "A"},
                cited_ids=["c1"],
            ),
        ],
        causal_steps=[],
    )
    groups = _distinct_typed_claims(hyp)
    assert len(groups) == 3
    target_keys = {g.get("target_key") for g in groups}
    assert len(target_keys) == 2
    assert "NO_TARGET" not in target_keys
    diag = collect_verification_diagnostics(hyp, [], _supported([]), [])
    assert diag is not None
    assert len(diag.claim_bindings) == 3
    assert len(diag.claim_ledgers) == 3
    paired = {(b.claim_key, b.target_ref) for b in diag.claim_bindings} == {
        (str(m.claim_key), str(m.target_ref)) for m in diag.claim_ledgers
    }
    assert paired
    refs = {b.target_ref for b in diag.claim_bindings}
    assert len(refs) == 2


def test_distinct_claims_split_with_real_model_targets() -> None:
    """H1: production assessments carry Pydantic ClaimTarget, not dicts.

    Same statement+kind with different real targets must stay separate
    (no NO_TARGET collapse), and dict-shaped and model-shaped identical
    targets must fingerprint identically.
    """
    from investigation_agent_platform.application.evidence.retrieval_ledger import (
        _distinct_typed_claims,
        derive_claim_query,
    )
    from investigation_agent_platform.domain.hypothesis.models import (
        ClaimKind,
        ClaimTarget,
    )

    hyp = SimpleNamespace(
        id=uuid4(),
        statement="Shared statement",
        supporting_evidence_ids=[],
        refuting_evidence_ids=[],
        assessments=[
            SimpleNamespace(
                claim_kind=ClaimKind.CAUSAL,
                claim_target=ClaimTarget(symbol="A"),
                cited_ids=["a1"],
            ),
            SimpleNamespace(
                claim_kind=ClaimKind.CAUSAL,
                claim_target=ClaimTarget(symbol="B"),
                cited_ids=["b1"],
            ),
        ],
        causal_steps=[],
    )
    groups = _distinct_typed_claims(hyp)
    assert len(groups) == 2
    assert {g.get("target_key") for g in groups} != {"NO_TARGET"}
    assert "NO_TARGET" not in {g.get("target_key") for g in groups}
    # A model target derives the symbol query (not the statement fallback),
    # identically to its dict-shaped twin.
    model_query, _ = derive_claim_query("Shared statement", "CAUSAL", ClaimTarget(symbol="A"))
    dict_query, _ = derive_claim_query("Shared statement", "CAUSAL", {"symbol": "A"})
    assert model_query == "A"
    assert model_query == dict_query


def test_unscoped_empty_supports_nothing() -> None:
    """D6: a clean empty search never places citations anywhere but unknown."""
    from investigation_agent_platform.application.evidence.retrieval_ledger import (
        build_claim_ledger,
    )

    ledger, binding, _full_unknown = build_claim_ledger(
        statement="S",
        claim_kind=None,
        claim_target=None,
        cited_ids=["ghost"],
        bank_ids=set(),
        attempts=[([], {"search_query": "S", "scope": None, "outcome": "UNSCOPED_EMPTY"})],
    )
    assert binding.claim_local == []
    assert binding.bank_fallback == []
    assert binding.unknown == ["ghost"]
    assert _full_unknown == ["ghost"]


def test_partial_rows_never_conjure_citations() -> None:
    """D6: partial rows resolve only overlapping IDs; the rest stay unknown."""
    from investigation_agent_platform.application.evidence.retrieval_ledger import (
        build_claim_ledger,
    )

    _ledger, binding, _full_unknown = build_claim_ledger(
        statement="S",
        claim_kind=None,
        claim_target=None,
        cited_ids=["ghost"],
        bank_ids=set(),
        attempts=[
            (
                [],
                {
                    "search_query": "S",
                    "scope": None,
                    "outcome": "PARTIAL_ROWS",
                    "signals": ["truncated:3"],
                },
            )
        ],
    )
    assert binding.claim_local == []
    assert binding.unknown == ["ghost"]
    assert _full_unknown == ["ghost"]


def test_ledger_records_carry_no_verdict_semantics() -> None:
    """D6: ledger outputs cannot discharge, validate, or verify anything."""
    from investigation_agent_platform.application.evidence.retrieval_ledger import (
        ClaimRetrievalBinding,
        ClaimRetrievalLedger,
    )

    verdict_fields = {
        "status",
        "tier",
        "verdict",
        "discharged",
        "valid",
        "verified",
        "confidence",
        "explanation",
    }
    assert not (set(ClaimRetrievalBinding.model_fields) & verdict_fields)
    assert not (set(ClaimRetrievalLedger.model_fields) & verdict_fields)


def _foo_hyp() -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        statement="Foo fails",
        supporting_evidence_ids=["evX"],
        refuting_evidence_ids=[],
        assessments=[
            SimpleNamespace(
                claim_kind=None,
                claim_target={"symbol": "Foo"},
                cited_ids=["evX"],
            )
        ],
        causal_steps=[],
    )


def test_bounded_search_requires_prior_truncation() -> None:
    prior_truncated = [
        (
            ["r1"],
            {"search_query": "Foo", "scope": None, "outcome": "ROWS_RETURNED", "truncated_rows": 2},
        ),
        (["r2"], {"search_query": "Foo", "scope": None, "outcome": "SCOPED_EMPTY"}),
    ]
    diag = collect_verification_diagnostics(_foo_hyp(), [], _supported([]), prior_truncated)
    assert diag is not None
    assert diag.claim_bindings[0].bounded_search is True


def test_bounded_search_rejects_self_only_truncation() -> None:
    self_truncated = [
        (["r1"], {"search_query": "Foo", "scope": None, "outcome": "SCOPED_EMPTY"}),
        (
            ["r2"],
            {"search_query": "Foo", "scope": None, "outcome": "SCOPED_EMPTY", "truncated_rows": 3},
        ),
    ]
    diag = collect_verification_diagnostics(_foo_hyp(), [], _supported([]), self_truncated)
    assert diag is not None
    assert diag.claim_bindings[0].bounded_search is False


# ---------------------------------------------------------------------------
# Battery metrics over persisted VERIFY_EVALUATION rows (hermetic, no DB).
# ---------------------------------------------------------------------------


def _battery_module():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "scripts" / "retrieval_benefit.py"
    spec = importlib.util.spec_from_file_location("retrieval_benefit", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _battery_attempt_rec(
    query: str,
    outcome: str,
    banked: list[str],
    exec_id: str | None,
    ts: str,
    scope: str | None = None,
) -> dict:
    return {
        "banked_ids": list(banked),
        "attempt": {
            "search_query": query,
            "scope": scope,
            "outcome": outcome,
            "truncated_rows": 0,
        },
        "action_execution_id": exec_id,
        "timestamp": ts,
    }


def _battery_binding(
    claim_key_value: str,
    local: list[str],
    unknown: list[str],
    bounded: bool = False,
    target: str = "",
) -> dict:
    return {
        "claim_key": claim_key_value,
        "claim_ref": "0123456789abcdef",
        "claim_local": list(local),
        "bank_fallback": [],
        "unknown": list(unknown),
        "bounded_search": bounded,
        "unmapped": False,
        "target_ref": target,
    }


def _battery_ledger(
    claim_key_value: str,
    query: str,
    scope: str | None = None,
    target: str = "",
    link_source: str = "query_match",
) -> dict:
    return {
        "claim_key": claim_key_value,
        "search_query": query,
        "scope": scope,
        "attempt_indexes": [],
        "link_source": link_source,
        "target_ref": target,
    }


def _battery_eval(
    ts: str,
    turn: int,
    bindings: list[dict],
    ledgers: list[dict],
    missing: list[dict] | None = None,
) -> dict:
    return {
        "timestamp": ts,
        "turn": turn,
        "bindings": list(bindings),
        "ledgers": list(ledgers),
        "missing_obligations": list(missing or []),
        "evaporated_count": 0,
    }


def _recovered_run(inv: str, row: str, ghost: str) -> tuple[dict, dict, dict, dict]:
    """One run whose re-retrieval lifted an empty key into the bank.

    Attempt timeline: fail (t0) < pre-eval (t1) < rows (t2) < post-eval
    (t3). The pre binding is unsatisfied (empty local, unknown ghost);
    the post binding changed and cites the novel durable row.
    """
    key = f"claim-{inv}"
    cohort = {inv: {"eligible_action_ids": [f"{inv}-exec-fail", f"{inv}-exec-rows"]}}
    attempts = {
        inv: [
            _battery_attempt_rec(
                "SampleOrderFilter",
                "SEARCH_FAILED",
                [],
                f"{inv}-exec-fail",
                "2026-10-20T10:00:00+00:00",
            ),
            _battery_attempt_rec(
                "SampleOrderFilter",
                "ROWS_RETURNED",
                [row],
                f"{inv}-exec-rows",
                "2026-10-20T10:02:00+00:00",
            ),
        ]
    }
    evals = {
        inv: [
            _battery_eval(
                "2026-10-20T10:01:00+00:00",
                1,
                [_battery_binding(key, [], [ghost])],
                [_battery_ledger(key, "SampleOrderFilter")],
            ),
            _battery_eval(
                "2026-10-20T10:03:00+00:00",
                2,
                [_battery_binding(key, [row], [])],
                [_battery_ledger(key, "SampleOrderFilter")],
            ),
        ]
    }
    banks = {inv: {row: "2026-10-20T10:02:30+00:00"}}
    return cohort, attempts, evals, banks


def _diagnostic_run(inv: str, ghosts: list[str]) -> tuple[dict, dict, dict, dict]:
    key = f"claim-{inv}"
    cohort = {inv: {"eligible_action_ids": [f"{inv}-exec"]}}
    attempts = {
        inv: [
            _battery_attempt_rec(
                "SampleOrderFilter",
                "SEARCH_FAILED",
                [],
                f"{inv}-exec",
                "2026-10-20T10:00:00+00:00",
            )
        ]
    }
    evals = {
        inv: [
            _battery_eval(
                "2026-10-20T10:01:00+00:00",
                1,
                [_battery_binding(key, [], ghosts)],
                [_battery_ledger(key, "SampleOrderFilter")],
            )
        ]
    }
    banks: dict = {inv: {}}
    return cohort, attempts, evals, banks


def test_battery_recovery_branch_passes() -> None:
    mod = _battery_module()
    cohort: dict = {}
    attempts: dict = {}
    evals: dict = {}
    banks: dict = {}
    for i in range(5):
        c, a, e, b = _recovered_run(f"inv-{i}", f"row-{i}", f"ghost-{i}")
        cohort.update(c)
        attempts.update(a)
        evals.update(e)
        banks.update(b)
    report = mod.compute_report(cohort, attempts, evals, banks, [])
    assert report["coverage"]["eligible_rate"] == 1.0
    assert report["coverage"]["matched_actions"] == 10
    assert report["evaluable_runs"] == 5
    assert report["recovery"]["chains"] >= 3
    assert report["recovery"]["investigations"] >= 3
    assert report["recovery"]["keys"][0]["claim_key"]
    assert report["branch_recovery"] is True
    assert report["pass"] is True
    assert "PASS=True" in mod.format_report(report)
    # M1: the sign-off caveat is unconditional — a passing report must
    # still say G3 is unclosed pending explicit owner sign-off.
    assert "needs explicit owner sign-off" in mod.format_report(report)


def test_battery_diagnostic_branch_passes() -> None:
    mod = _battery_module()
    cohort: dict = {}
    attempts: dict = {}
    evals: dict = {}
    banks: dict = {}
    for i in range(5):
        c, a, e, b = _diagnostic_run(f"inv-d{i}", [f"ghost-{i}-a", f"ghost-{i}-b"])
        cohort.update(c)
        attempts.update(a)
        evals.update(e)
        banks.update(b)
    report = mod.compute_report(cohort, attempts, evals, banks, [])
    assert report["n_evaporation_events"] == 10
    assert report["n_missing_obligations"] == 0
    assert report["attribution"]["rate"] == 1.0
    assert report["branch_diagnostic"] is True
    assert report["pass"] is True


def test_battery_missing_obligations_counted_and_joined() -> None:
    mod = _battery_module()
    key = mod.claim_key("GhostClaim", None)
    inv = "inv-mo"
    cohort = {inv: {"eligible_action_ids": [f"{inv}-exec"]}}
    attempts = {
        inv: [
            _battery_attempt_rec(
                "GhostQuery",
                "SEARCH_FAILED",
                [],
                f"{inv}-exec",
                "2026-10-20T10:00:00+00:00",
            )
        ]
    }
    evals = {
        inv: [
            _battery_eval(
                "2026-10-20T10:01:00+00:00",
                1,
                [_battery_binding(key, [], [])],
                [_battery_ledger(key, "GhostQuery")],
                missing=[{"claim": "GhostClaim", "claim_kind": None}],
            )
        ]
    }
    report = mod.compute_report(cohort, attempts, evals, {inv: {}}, [])
    assert report["n_missing_obligations"] == 1
    assert report["attribution"]["events"] == 1
    assert report["attribution"]["attributed"] == 1


def test_battery_missing_obligation_without_signal_unexplained() -> None:
    mod = _battery_module()
    inv = "inv-mo2"
    cohort = {inv: {"eligible_action_ids": [f"{inv}-exec"]}}
    attempts = {
        inv: [
            _battery_attempt_rec(
                "OtherQuery",
                "ROWS_RETURNED",
                ["row-1"],
                f"{inv}-exec",
                "2026-10-20T10:00:00+00:00",
            )
        ]
    }
    evals = {
        inv: [
            _battery_eval(
                "2026-10-20T10:01:00+00:00",
                1,
                [],
                [],
                missing=[{"claim": "LonelyClaim", "claim_kind": None}],
            )
        ]
    }
    report = mod.compute_report(
        cohort, attempts, evals, {inv: {"row-1": "2026-10-20T10:00:30+00:00"}}, []
    )
    assert report["attribution"]["events"] == 1
    assert report["attribution"]["attributed"] == 0
    assert report["attribution"]["unexplained"] == 1


def test_battery_per_action_coverage_pairs_join_key() -> None:
    mod = _battery_module()
    inv = "inv-cov"
    cohort = {inv: {"eligible_action_ids": ["exec-1", "exec-2"]}}
    attempts = {
        inv: [
            _battery_attempt_rec(
                "SampleOrderFilter",
                "ROWS_RETURNED",
                ["r"],
                "exec-1",
                "2026-10-20T10:00:00+00:00",
            ),
            _battery_attempt_rec(
                "SampleOrderFilter",
                "ROWS_RETURNED",
                ["r"],
                None,
                "2026-10-20T10:01:00+00:00",
            ),
        ]
    }
    report = mod.compute_report(cohort, attempts, {}, {inv: {"r": "2026-10-20T10:00:30+00:00"}}, [])
    assert report["coverage"]["eligible_actions"] == 2
    assert report["coverage"]["matched_actions"] == 1
    assert report["coverage"]["eligible_rate"] == 0.5
    assert report["pass"] is False


def test_battery_recovery_requires_changed_binding() -> None:
    mod = _battery_module()
    key = "claim-stuck"
    inv = "inv-stuck"
    cohort = {inv: {"eligible_action_ids": [f"{inv}-exec-fail", f"{inv}-exec-rows"]}}
    attempts = {
        inv: [
            _battery_attempt_rec(
                "SampleOrderFilter",
                "SEARCH_FAILED",
                [],
                f"{inv}-exec-fail",
                "2026-10-20T10:00:00+00:00",
            ),
            _battery_attempt_rec(
                "SampleOrderFilter",
                "ROWS_RETURNED",
                ["row-9"],
                f"{inv}-exec-rows",
                "2026-10-20T10:02:00+00:00",
            ),
        ]
    }
    # Post binding identical to pre: still unsatisfied, nothing changed.
    evals = {
        inv: [
            _battery_eval(
                "2026-10-20T10:01:00+00:00",
                1,
                [_battery_binding(key, [], ["ghost"])],
                [_battery_ledger(key, "SampleOrderFilter")],
            ),
            _battery_eval(
                "2026-10-20T10:03:00+00:00",
                2,
                [_battery_binding(key, [], ["ghost"])],
                [_battery_ledger(key, "SampleOrderFilter")],
            ),
        ]
    }
    report = mod.compute_report(
        cohort, attempts, evals, {inv: {"row-9": "2026-10-20T10:02:30+00:00"}}, []
    )
    assert report["recovery"]["chains"] == 0
    assert report["branch_recovery"] is False


def test_battery_recovery_requires_durable_rows() -> None:
    mod = _battery_module()
    key = "claim-nondurable"
    inv = "inv-nondurable"
    cohort = {inv: {"eligible_action_ids": [f"{inv}-exec-fail", f"{inv}-exec-rows"]}}
    attempts = {
        inv: [
            _battery_attempt_rec(
                "SampleOrderFilter",
                "SEARCH_FAILED",
                [],
                f"{inv}-exec-fail",
                "2026-10-20T10:00:00+00:00",
            ),
            _battery_attempt_rec(
                "SampleOrderFilter",
                "ROWS_RETURNED",
                ["row-evap"],
                f"{inv}-exec-rows",
                "2026-10-20T10:02:00+00:00",
            ),
        ]
    }
    evals = {
        inv: [
            _battery_eval(
                "2026-10-20T10:01:00+00:00",
                1,
                [_battery_binding(key, [], ["ghost"])],
                [_battery_ledger(key, "SampleOrderFilter")],
            ),
            _battery_eval(
                "2026-10-20T10:03:00+00:00",
                2,
                [_battery_binding(key, ["row-evap"], [])],
                [_battery_ledger(key, "SampleOrderFilter")],
            ),
        ]
    }
    # row-evap never made the evidence bank: evaporated, not recovered.
    report = mod.compute_report(cohort, attempts, evals, {inv: {}}, [])
    assert report["recovery"]["chains"] == 0


def test_battery_inconclusive_below_sample_floor() -> None:
    mod = _battery_module()
    cohort, attempts, evals, banks = _recovered_run("inv-0", "row-0", "ghost-0")
    report = mod.compute_report(cohort, attempts, evals, banks, [])
    assert report["n_runs"] == 1
    assert report["evaluable_runs"] == 1
    assert report["inconclusive"] is True
    assert report["pass"] is False


def test_battery_evaluable_floor_counts_evaluations() -> None:
    mod = _battery_module()
    cohort: dict = {}
    attempts: dict = {}
    evals: dict = {}
    banks: dict = {}
    for i in range(5):
        c, a, e, b = _recovered_run(f"inv-{i}", f"row-{i}", f"ghost-{i}")
        cohort.update(c)
        attempts.update(a)
        evals.update(e)
        banks.update(b)
    # Drop one run's evaluations: 5 cohort runs but only 4 evaluable.
    evals.pop("inv-4")
    report = mod.compute_report(cohort, attempts, evals, banks, [])
    assert report["n_runs"] == 5
    assert report["evaluable_runs"] == 4
    assert report["inconclusive"] is True
    assert report["pass"] is False


def test_battery_coverage_miss_fails() -> None:
    mod = _battery_module()
    cohort, attempts, evals, banks = _recovered_run("inv-0", "row-0", "ghost-0")
    cohort["inv-dark"] = {"eligible_action_ids": ["dark-exec"]}
    report = mod.compute_report(cohort, attempts, evals, banks, [])
    assert report["coverage"]["eligible_rate"] < 1.0
    assert report["branch_recovery"] is False
    assert report["branch_diagnostic"] is False
    assert report["pass"] is False


def test_battery_guard_hit_blocks_pass() -> None:
    mod = _battery_module()
    cohort: dict = {}
    attempts: dict = {}
    evals: dict = {}
    banks: dict = {}
    for i in range(5):
        c, a, e, b = _recovered_run(f"inv-g{i}", f"row-g{i}", f"ghost-g{i}")
        cohort.update(c)
        attempts.update(a)
        evals.update(e)
        banks.update(b)
    report = mod.compute_report(
        cohort, attempts, evals, banks, ["root cause with forced_requery marker"]
    )
    assert report["verdict_guard"]["clean"] is False
    assert report["pass"] is False


def test_battery_truncation_flags_from_persisted_bindings() -> None:
    mod = _battery_module()
    key = "claim-trunc"
    inv = "inv-trunc"
    cohort = {inv: {"eligible_action_ids": [f"{inv}-exec"]}}
    attempts = {
        inv: [
            _battery_attempt_rec(
                "SampleOrderFilter",
                "SCOPED_EMPTY",
                [],
                f"{inv}-exec",
                "2026-10-20T10:00:00+00:00",
            )
        ]
    }
    attempts[inv][0]["attempt"]["truncated_rows"] = 2
    evals = {
        inv: [
            _battery_eval(
                "2026-10-20T10:01:00+00:00",
                1,
                [_battery_binding(key, [], ["ghost"], bounded=True)],
                [_battery_ledger(key, "SampleOrderFilter")],
            )
        ]
    }
    report = mod.compute_report(cohort, attempts, evals, {inv: {}}, [])
    assert report["truncation"]["flags"] == 1
    assert report["truncation"]["applicable"] is True


def test_battery_truncation_not_applicable_without_truncation() -> None:
    mod = _battery_module()
    cohort, attempts, evals, banks = _recovered_run("inv-0", "row-0", "ghost-0")
    report = mod.compute_report(cohort, attempts, evals, banks, [])
    assert report["truncation"]["applicable"] is False
    assert "not-applicable" in mod.format_report(report)


def test_battery_cli_defaults() -> None:
    mod = _battery_module()
    args = mod.parse_args([])
    assert args.tenant is None
    assert args.days == 30


def test_battery_later_attempt_cannot_explain_earlier_eval() -> None:
    """H1: only attempts at or before the evaluation count as signal.

    The SEARCH_FAILED attempt happens after the evaluation it would
    otherwise match — attribution must stay at zero.
    """
    mod = _battery_module()
    key = "claim-late"
    inv = "inv-late"
    cohort = {inv: {"eligible_action_ids": [f"{inv}-exec"]}}
    attempts = {
        inv: [
            _battery_attempt_rec(
                "SampleOrderFilter",
                "SEARCH_FAILED",
                [],
                f"{inv}-exec",
                "2026-10-20T10:05:00+00:00",
            )
        ]
    }
    evals = {
        inv: [
            _battery_eval(
                "2026-10-20T10:01:00+00:00",
                1,
                [_battery_binding(key, [], ["ghost"])],
                [_battery_ledger(key, "SampleOrderFilter")],
            )
        ]
    }
    report = mod.compute_report(cohort, attempts, evals, {inv: {}}, [])
    assert report["attribution"]["events"] == 1
    assert report["attribution"]["attributed"] == 0


def test_battery_overlap_link_requires_banked_intersection() -> None:
    """H1: id_overlap ledgers need attempt-banked overlap with the cited set.

    Same key and a failed outcome are not enough when the ledger's basis
    is ID overlap and the attempt banked nothing the claim cites.
    """
    mod = _battery_module()
    key = "claim-overlap"
    inv = "inv-overlap"
    cohort = {inv: {"eligible_action_ids": [f"{inv}-exec"]}}
    attempts = {
        inv: [
            _battery_attempt_rec(
                "SampleOrderFilter",
                "SEARCH_FAILED",
                ["unrelated-row"],
                f"{inv}-exec",
                "2026-10-20T10:00:00+00:00",
            )
        ]
    }
    evals = {
        inv: [
            _battery_eval(
                "2026-10-20T10:01:00+00:00",
                1,
                [_battery_binding(key, [], ["ghost"])],
                [_battery_ledger(key, "SampleOrderFilter", link_source="id_overlap")],
            )
        ]
    }
    report = mod.compute_report(cohort, attempts, evals, {inv: {}}, [])
    assert report["attribution"]["events"] == 1
    assert report["attribution"]["attributed"] == 0


def test_battery_overlap_link_attributes_on_intersection() -> None:
    """H1 positive: banked overlap with the cited set attributes."""
    mod = _battery_module()
    key = "claim-overlap-hit"
    inv = "inv-overlap-hit"
    cohort = {inv: {"eligible_action_ids": [f"{inv}-exec"]}}
    attempts = {
        inv: [
            _battery_attempt_rec(
                "SampleOrderFilter",
                "SEARCH_FAILED",
                ["ghost"],
                f"{inv}-exec",
                "2026-10-20T10:00:00+00:00",
            )
        ]
    }
    evals = {
        inv: [
            _battery_eval(
                "2026-10-20T10:01:00+00:00",
                1,
                [_battery_binding(key, [], ["ghost"])],
                [_battery_ledger(key, "SampleOrderFilter", link_source="id_overlap")],
            )
        ]
    }
    report = mod.compute_report(cohort, attempts, evals, {inv: {}}, [])
    assert report["attribution"]["events"] == 1
    assert report["attribution"]["attributed"] == 1


def test_battery_target_distinct_claims_stay_separate() -> None:
    """H2: same claim_key with different targets joins separately.

    Two target-distinct bindings share a key; a same-key missing
    obligation expands to one unit per target, and each unit is signaled
    by its own target's ledger only.
    """
    mod = _battery_module()
    key = "SharedStatement|CAUSAL"
    inv = "inv-targets"
    cohort = {inv: {"eligible_action_ids": [f"{inv}-exec-a", f"{inv}-exec-b"]}}
    attempts = {
        inv: [
            _battery_attempt_rec(
                "SymbolA",
                "SEARCH_FAILED",
                [],
                f"{inv}-exec-a",
                "2026-10-20T10:00:00+00:00",
            ),
            _battery_attempt_rec(
                "SymbolB",
                "ROWS_RETURNED",
                ["row-b"],
                f"{inv}-exec-b",
                "2026-10-20T10:00:30+00:00",
            ),
        ]
    }
    evals = {
        inv: [
            _battery_eval(
                "2026-10-20T10:01:00+00:00",
                1,
                [
                    _battery_binding(key, [], ["ghost-a"], target="aaa"),
                    _battery_binding(key, ["row-b"], [], target="bbb"),
                ],
                [
                    _battery_ledger(key, "SymbolA", target="aaa"),
                    _battery_ledger(key, "SymbolB", target="bbb"),
                ],
                missing=[{"claim": "SharedStatement", "claim_kind": "CAUSAL"}],
            )
        ]
    }
    banks = {inv: {"row-b": "2026-10-20T10:00:45+00:00"}}
    report = mod.compute_report(cohort, attempts, evals, banks, [])
    assert report["attribution"]["events"] == 3
    assert report["n_missing_obligations"] == 2
    # ghost-a attributed via SymbolA failure; target-bbb missing unit has
    # no failed/partial attempt behind it and stays unexplained.
    assert report["attribution"]["attributed"] == 2
    assert report["attribution"]["unexplained"] == 1


def test_battery_recovery_keys_include_target() -> None:
    """H2: recovery chains are keyed (inv, claim_key, target_ref)."""
    mod = _battery_module()
    key = "SharedStatement|CAUSAL"
    inv = "inv-rectarget"
    cohort = {inv: {"eligible_action_ids": [f"{inv}-exec-fail", f"{inv}-exec-rows"]}}
    attempts = {
        inv: [
            _battery_attempt_rec(
                "SymbolA",
                "SEARCH_FAILED",
                [],
                f"{inv}-exec-fail",
                "2026-10-20T10:00:00+00:00",
            ),
            _battery_attempt_rec(
                "SymbolA",
                "ROWS_RETURNED",
                ["row-a"],
                f"{inv}-exec-rows",
                "2026-10-20T10:02:00+00:00",
            ),
        ]
    }
    evals = {
        inv: [
            _battery_eval(
                "2026-10-20T10:01:00+00:00",
                1,
                [_battery_binding(key, [], ["ghost-a"], target="aaa")],
                [_battery_ledger(key, "SymbolA", target="aaa")],
            ),
            _battery_eval(
                "2026-10-20T10:03:00+00:00",
                2,
                [_battery_binding(key, ["row-a"], [], target="aaa")],
                [_battery_ledger(key, "SymbolA", target="aaa")],
            ),
        ]
    }
    banks = {inv: {"row-a": "2026-10-20T10:02:30+00:00"}}
    report = mod.compute_report(cohort, attempts, evals, banks, [])
    assert report["recovery"]["chains"] == 1
    assert report["recovery"]["keys"][0]["target_ref"] == "aaa"


def test_battery_recovery_via_bank_timestamp_without_listed_row() -> None:
    """M2: a cap-truncated novel row still proves recovery via retrieved_at.

    The window attempt's persisted banked_ids omit the novel row (25-ID
    cap), but the row's bank retrieved_at falls inside (pre, post] and a
    same-key window attempt returned rows — the chain counts.
    """
    mod = _battery_module()
    key = "claim-cap"
    inv = "inv-cap"
    cohort = {inv: {"eligible_action_ids": [f"{inv}-exec-fail", f"{inv}-exec-rows"]}}
    attempts = {
        inv: [
            _battery_attempt_rec(
                "SampleOrderFilter",
                "SEARCH_FAILED",
                [],
                f"{inv}-exec-fail",
                "2026-10-20T10:00:00+00:00",
            ),
            _battery_attempt_rec(
                "SampleOrderFilter",
                "ROWS_RETURNED",
                ["other-1"],
                f"{inv}-exec-rows",
                "2026-10-20T10:02:00+00:00",
            ),
        ]
    }
    evals = {
        inv: [
            _battery_eval(
                "2026-10-20T10:01:00+00:00",
                1,
                [_battery_binding(key, [], ["ghost"])],
                [_battery_ledger(key, "SampleOrderFilter")],
            ),
            _battery_eval(
                "2026-10-20T10:03:00+00:00",
                2,
                [_battery_binding(key, ["row-cap"], [])],
                [_battery_ledger(key, "SampleOrderFilter")],
            ),
        ]
    }
    banks = {
        inv: {
            "row-cap": "2026-10-20T10:02:30+00:00",
            "other-1": "2026-10-20T10:02:00+00:00",
            "ghost": "2026-10-20T09:00:00+00:00",
        }
    }
    report = mod.compute_report(cohort, attempts, evals, banks, [])
    assert report["recovery"]["chains"] == 1
    assert report["recovery"]["keys"][0]["novel_rows"] == ["row-cap"]


def test_battery_recovery_timestamp_vetoed_by_other_key_banking() -> None:
    """H2: a different-key in-window attempt vetoes the timestamp proof.

    The timed row falls inside (pre, post] and a same-key window attempt
    returned rows, but another-key attempt also banked rows in-window —
    it could have introduced the timed row, so no chain counts. The
    listed proof (exact banked-ID linkage) is unaffected by this veto.
    """
    mod = _battery_module()
    key = "claim-veto"
    inv = "inv-veto"
    cohort = {
        inv: {"eligible_action_ids": [f"{inv}-exec-fail", f"{inv}-exec-rows", f"{inv}-exec-other"]}
    }
    attempts = {
        inv: [
            _battery_attempt_rec(
                "SampleOrderFilter",
                "SEARCH_FAILED",
                [],
                f"{inv}-exec-fail",
                "2026-10-20T10:00:00+00:00",
            ),
            _battery_attempt_rec(
                "SampleOrderFilter",
                "ROWS_RETURNED",
                ["other-1"],
                f"{inv}-exec-rows",
                "2026-10-20T10:02:00+00:00",
            ),
            _battery_attempt_rec(
                "OtherQuery",
                "ROWS_RETURNED",
                ["other-2"],
                f"{inv}-exec-other",
                "2026-10-20T10:02:15+00:00",
            ),
        ]
    }
    evals = {
        inv: [
            _battery_eval(
                "2026-10-20T10:01:00+00:00",
                1,
                [_battery_binding(key, [], ["ghost"])],
                [_battery_ledger(key, "SampleOrderFilter")],
            ),
            _battery_eval(
                "2026-10-20T10:03:00+00:00",
                2,
                [_battery_binding(key, ["row-cap"], [])],
                [_battery_ledger(key, "SampleOrderFilter")],
            ),
        ]
    }
    banks = {
        inv: {
            "row-cap": "2026-10-20T10:02:30+00:00",
            "other-1": "2026-10-20T10:02:00+00:00",
            "other-2": "2026-10-20T10:02:15+00:00",
            "ghost": "2026-10-20T09:00:00+00:00",
        }
    }
    report = mod.compute_report(cohort, attempts, evals, banks, [])
    assert report["recovery"]["chains"] == 0


def test_battery_recovery_rejects_predated_bank_row() -> None:
    """M2 negative: a cited row banked before pre is not a re-retrieval."""
    mod = _battery_module()
    key = "claim-predated"
    inv = "inv-predated"
    cohort = {inv: {"eligible_action_ids": [f"{inv}-exec-fail", f"{inv}-exec-rows"]}}
    attempts = {
        inv: [
            _battery_attempt_rec(
                "SampleOrderFilter",
                "SEARCH_FAILED",
                [],
                f"{inv}-exec-fail",
                "2026-10-20T10:00:00+00:00",
            ),
            _battery_attempt_rec(
                "SampleOrderFilter",
                "ROWS_RETURNED",
                ["other-1"],
                f"{inv}-exec-rows",
                "2026-10-20T10:02:00+00:00",
            ),
        ]
    }
    evals = {
        inv: [
            _battery_eval(
                "2026-10-20T10:01:00+00:00",
                1,
                [_battery_binding(key, [], ["ghost"])],
                [_battery_ledger(key, "SampleOrderFilter")],
            ),
            _battery_eval(
                "2026-10-20T10:03:00+00:00",
                2,
                [_battery_binding(key, ["row-old"], [])],
                [_battery_ledger(key, "SampleOrderFilter")],
            ),
        ]
    }
    banks = {
        inv: {
            "row-old": "2026-10-20T09:00:00+00:00",
            "other-1": "2026-10-20T10:02:00+00:00",
        }
    }
    report = mod.compute_report(cohort, attempts, evals, banks, [])
    assert report["recovery"]["chains"] == 0


@pytest.mark.asyncio
async def test_verify_activity_emits_marker_without_diagnostics() -> None:
    """M1: an empty evaluation still persists a skeleton marker row."""
    from unittest.mock import patch

    from investigation_agent_platform.api.dependencies import get_app_context
    from investigation_agent_platform.application.worker.activities import (
        VerifyRootCauseInput,
        verify_root_cause_activity,
    )
    from investigation_agent_platform.domain.timeline.models import TimelineEventType

    inv_id = uuid4()
    _verify_harness(
        hypotheses=[_claim_hypothesis(inv_id)],
        inv_metadata={},
        timeline_events=[_failed_attempt_event()],
    )
    appended: list = []

    async def _append(tenant_id: str, event: object, investigation_id: object) -> None:
        appended.append(event)

    get_app_context().timeline_repo = SimpleNamespace(  # type: ignore[attr-defined]
        find_by_investigation_id=get_app_context().timeline_repo.find_by_investigation_id,
        append=_append,
    )
    with patch(
        "investigation_agent_platform.application.investigation.verification."
        "collect_verification_diagnostics",
        return_value=None,
    ):
        result = await verify_root_cause_activity(
            VerifyRootCauseInput(
                tenant_id="t",
                investigation_id=str(inv_id),
                recent_actions=_matching_recent_actions(),
                iteration=5,
            )
        )
    assert result.success is True
    eval_events = [e for e in appended if e.event_type == TimelineEventType.VERIFY_EVALUATION]
    assert len(eval_events) == 1
    assert eval_events[0].attributes["turn"] == 5
    assert eval_events[0].attributes["bindings"] == []
    assert eval_events[0].attributes["ledgers"] == []
    assert eval_events[0].attributes["missing_obligations"] == []


def test_build_verify_evaluation_attributes_round_trip() -> None:
    from investigation_agent_platform.application.investigation.verification import (
        VerificationDiagnostics,
        build_verify_evaluation_attributes,
    )

    # None diagnostics yields a skeleton, not None: every current-version
    # turn persists a row so a missing row always means a write failure.
    skeleton = build_verify_evaluation_attributes(
        None, turn=1, hypothesis_id="h", verifier_version="v2"
    )
    assert skeleton is not None
    assert skeleton["turn"] == 1
    assert skeleton["bindings"] == []
    assert skeleton["ledgers"] == []
    assert skeleton["missing_obligations"] == []
    diagnostics = VerificationDiagnostics(
        claim_bindings=[
            ClaimRetrievalBinding(
                claim_key="k|CAUSAL",
                claim_ref="0123456789abcdef",
                claim_local=["row-1"],
                unknown=["ghost-1"],
            )
        ],
        claim_ledgers=[
            ClaimRetrievalLedger(claim_key="k|CAUSAL", search_query="SampleOrderFilter", scope=None)
        ],
        missing_obligations=[],
    )
    attrs = build_verify_evaluation_attributes(
        diagnostics, turn=3, hypothesis_id="hyp-1", verifier_version="v2"
    )
    assert attrs is not None
    assert attrs["turn"] == 3
    assert attrs["hypothesis_id"] == "hyp-1"
    assert attrs["verifier_version"] == "v2"
    assert attrs["bindings"][0]["claim_key"] == "k|CAUSAL"
    assert attrs["bindings"][0]["claim_local"] == ["row-1"]
    assert attrs["ledgers"][0]["search_query"] == "SampleOrderFilter"
    assert attrs["missing_obligations"] == []
    legacy = build_verify_evaluation_attributes(
        diagnostics, turn=None, hypothesis_id="hyp-1", verifier_version="v2"
    )
    assert legacy is not None and legacy["turn"] is None


@pytest.mark.asyncio
async def test_verify_activity_appends_evaluation_event() -> None:
    from investigation_agent_platform.api.dependencies import get_app_context
    from investigation_agent_platform.application.worker.activities import (
        VerifyRootCauseInput,
        verify_root_cause_activity,
    )
    from investigation_agent_platform.domain.timeline.models import TimelineEventType

    inv_id = uuid4()
    _verify_harness(
        hypotheses=[_claim_hypothesis(inv_id)],
        inv_metadata={},
        timeline_events=[_failed_attempt_event()],
    )
    appended: list = []

    async def _append(tenant_id: str, event: object, investigation_id: object) -> None:
        appended.append(event)

    get_app_context().timeline_repo = SimpleNamespace(  # type: ignore[attr-defined]
        find_by_investigation_id=get_app_context().timeline_repo.find_by_investigation_id,
        append=_append,
    )
    result = await verify_root_cause_activity(
        VerifyRootCauseInput(
            tenant_id="t",
            investigation_id=str(inv_id),
            recent_actions=_matching_recent_actions(),
            iteration=3,
        )
    )
    assert result.success is True
    eval_events = [e for e in appended if e.event_type == TimelineEventType.VERIFY_EVALUATION]
    assert len(eval_events) == 1
    attrs = eval_events[0].attributes
    assert attrs["turn"] == 3
    assert attrs["bindings"], "expected persisted claim bindings"
    binding_keys = {b.get("claim_key") for b in attrs["bindings"]}
    ledger_keys = {mm.get("claim_key") for mm in attrs["ledgers"]}
    assert binding_keys and binding_keys == ledger_keys
    assert isinstance(attrs["missing_obligations"], list)


@pytest.mark.asyncio
async def test_verify_activity_legacy_emits_no_evaluation_event() -> None:
    from investigation_agent_platform.api.dependencies import get_app_context
    from investigation_agent_platform.application.worker.activities import (
        VerifyRootCauseInput,
        verify_root_cause_activity,
    )
    from investigation_agent_platform.domain.timeline.models import TimelineEventType

    inv_id = uuid4()
    _verify_harness(
        hypotheses=[_claim_hypothesis(inv_id)],
        inv_metadata={"verifier_version": "v1"},
        timeline_events=[_failed_attempt_event()],
    )
    appended: list = []

    async def _append(tenant_id: str, event: object, investigation_id: object) -> None:
        appended.append(event)

    get_app_context().timeline_repo = SimpleNamespace(  # type: ignore[attr-defined]
        find_by_investigation_id=get_app_context().timeline_repo.find_by_investigation_id,
        append=_append,
    )
    result = await verify_root_cause_activity(
        VerifyRootCauseInput(
            tenant_id="t",
            investigation_id=str(inv_id),
            recent_actions=_matching_recent_actions(),
        )
    )
    assert result.success is True
    assert not [e for e in appended if e.event_type == TimelineEventType.VERIFY_EVALUATION]


# ---------------------------------------------------------------------------
# Battery hardening: exit codes, attestation, strengthened recovery.
# ---------------------------------------------------------------------------


class _FakeMappingsResult:
    def __init__(self, rows: list[dict]):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return [dict(r) for r in self._rows]


class _FakeAttestSession:
    """Minimal async session stub for _attest_schema (by query marker)."""

    def __init__(self, rows_by_marker: dict[str, list[dict]]):
        self._rows = rows_by_marker

    async def execute(self, stmt, params=None):
        sql = str(stmt)
        for marker, rows in self._rows.items():
            if marker in sql:
                return _FakeMappingsResult(rows)
        raise AssertionError(f"unexpected attestation query: {sql[:80]}")


def _attest_ok_rows(
    policy_qual: str = "((tenant_id = current_setting('app.tenant_id'::text, true)))",
) -> dict[str, list[dict]]:
    # Keyed on the catalog view the battery actually queries: FORCE status
    # lives in pg_class (pg_tables has no forcerowsecurity column), so the
    # "pg_class" marker below pins the corrected query — the old pg_tables
    # query would fail to match this stub, exactly as it failed live.
    return {
        "pg_class": [{"schemaname": "public", "rowsecurity": True, "forcerowsecurity": True}],
        "information_schema": [{"data_type": "character varying", "character_maximum_length": 64}],
        "pg_policies": [{"policyname": "tenant_isolation", "cmd": "ALL", "qual": policy_qual}],
    }


@pytest.mark.asyncio
async def test_battery_attestation_accepts_tenant_predicate() -> None:
    mod = _battery_module()
    await mod._attest_schema(_FakeAttestSession(_attest_ok_rows()), "timeline_events")


@pytest.mark.asyncio
async def test_battery_attestation_rejects_name_only_policy() -> None:
    mod = _battery_module()
    rows = _attest_ok_rows(policy_qual="((owner = current_user))")
    rows["pg_policies"] = [
        {"policyname": "tenant_scoped_ish", "cmd": "ALL", "qual": "((owner = current_user))"}
    ]
    try:
        await mod._attest_schema(_FakeAttestSession(rows), "timeline_events")
    except mod._BatteryUnreadable as exc:
        assert "tenant-isolation USING predicate" in str(exc)
    else:
        raise AssertionError("misleadingly named policy must fail attestation")


@pytest.mark.asyncio
async def test_battery_attestation_rejects_ambiguous_schema() -> None:
    mod = _battery_module()
    rows = _attest_ok_rows()
    rows["pg_class"] = [
        {"schemaname": "public", "rowsecurity": True, "forcerowsecurity": True},
        {"schemaname": "analytics", "rowsecurity": True, "forcerowsecurity": True},
    ]
    try:
        await mod._attest_schema(_FakeAttestSession(rows), "timeline_events")
    except mod._BatteryUnreadable as exc:
        assert "ambiguous attestation" in str(exc)
    else:
        raise AssertionError("multi-schema table must fail attestation")


@pytest.mark.asyncio
async def test_battery_attestation_rejects_unforced_rls() -> None:
    mod = _battery_module()
    rows = _attest_ok_rows()
    rows["pg_class"] = [{"schemaname": "public", "rowsecurity": True, "forcerowsecurity": False}]
    try:
        await mod._attest_schema(_FakeAttestSession(rows), "timeline_events")
    except mod._BatteryUnreadable as exc:
        assert "not enabled+forced" in str(exc)
    else:
        raise AssertionError("unforced RLS must fail attestation")


def test_battery_main_refuses_unscoped_run() -> None:
    mod = _battery_module()
    called: list = []
    from unittest import mock

    with mock.patch.object(mod, "run_battery", side_effect=lambda **kw: called.append(kw)):
        assert mod.main(["--days", "7"]) == 2
    assert called == []


def test_battery_main_exit_codes() -> None:
    mod = _battery_module()
    from unittest import mock

    with mock.patch.object(mod, "run_battery", return_value={"pass": True}):
        assert mod.main(["--tenant", "t1"]) == 0
    with mock.patch.object(mod, "run_battery", return_value={"pass": False}):
        assert mod.main(["--tenant", "t1"]) == 1

    def _boom(**kw):
        raise mod._BatteryUnreadable("no db")

    with mock.patch.object(mod, "run_battery", side_effect=_boom):
        assert mod.main(["--tenant", "t1"]) == 2
