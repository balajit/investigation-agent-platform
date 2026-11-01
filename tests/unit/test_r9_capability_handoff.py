"""R9: authorization-caused abstention is indistinguishable from absence.

Pins the sixth handoff state, its precedence, the telemetry tightening,
the lifecycle denial context, and the profile lint. Written first, red
before the fix.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from investigation_agent_platform.domain.finding.models import (
    CLAIM_ACQUIRING_ACTIONS,
    ClaimTarget,
    HandoffState,
    classify_terminal_handoff,
    derive_gap_fetch,
    gap_claim_for_hypotheses,
    names_concrete_fetch_target,
)
from investigation_agent_platform.domain.hypothesis.models import (
    ClaimKind,
    HypothesisStatus,
)
from investigation_agent_platform.domain.profile.models import (
    ApplicationProfile,
    InvestigationConfig,
    ProfileStatus,
    Service,
    ServiceCode,
)


def _profile(evidence_types, capabilities) -> ApplicationProfile:  # type: ignore[no-untyped-def]
    return ApplicationProfile(
        id="app1",
        tenant_id="t1",
        name="n",
        description="d",
        environment="production",
        version=2,
        status=ProfileStatus.ACTIVE,
        investigation=InvestigationConfig(enabledEvidenceTypes=list(evidence_types)),
        allowed_capabilities=list(capabilities),
        services=[
            Service(
                name="a",
                code=ServiceCode(org="org", repos=["a"], source_roots=["src"]),
            )
        ],
    )


_DENIED = ("GET_CODE", "SEARCH_CODE")
_REASONS = {
    "GET_CODE": "evidence_type_not_enabled",
    "SEARCH_CODE": "capability_not_declared",
}
_FETCH = "GET_CODE a/B.java for Widget"


def _classify_denied(**over):  # type: ignore[no-untyped-def]
    params = {
        "status": "INSUFFICIENT_EVIDENCE",
        "reason": "insufficient",
        "gap_fetch": _FETCH,
        "gap_claim_kind": ClaimKind.DEFINITION,
        "unavailable_actions": _DENIED,
        "deny_reasons": dict(_REASONS),
        "application_id": "example-app",
    }
    params.update(over)
    return classify_terminal_handoff(**params)


def test_r9_t01_denied_acquiring_actions_produce_the_state(monkeypatch) -> None:
    handoff = _classify_denied()
    assert handoff.state == HandoffState.CAPABILITY_DENIED
    assert handoff.next_fetch == _FETCH
    assert handoff.price is not None and handoff.price > 0
    assert handoff.resume_pointer is not None
    assert "GET_CODE" in handoff.resume_pointer and "SEARCH_CODE" in handoff.resume_pointer
    assert "example-app" in handoff.resume_pointer
    assert handoff.recommended_next_steps
    assert "GET_CODE" in handoff.recommended_next_steps[0]
    # Negative control: without the rule the same inputs read as exhausted.
    from investigation_agent_platform.domain.finding import models

    monkeypatch.setattr(models, "capability_denied_for", lambda *args: [])
    assert _classify_denied().state == HandoffState.REASONING_DEFERRED


def test_r9_t02_available_alternative_prevents_it() -> None:
    handoff = _classify_denied(unavailable_actions=("GET_CODE",))
    assert handoff.state != HandoffState.CAPABILITY_DENIED


def test_r9_t03_table_pinned_to_fetch_derivation() -> None:
    from investigation_agent_platform.domain.finding.models import _FETCH_ACTION_TOKEN

    cases = [
        (ClaimKind.DEFINITION, ClaimTarget(symbol="W", file_path="a/B.java")),
        (ClaimKind.BEHAVIOR, ClaimTarget(symbol="W", file_path="a/B.java")),
        (ClaimKind.CAUSAL, ClaimTarget(symbol="W", file_path="a/B.java")),
        (ClaimKind.CALL_EDGE, ClaimTarget(symbol="W", file_path="a/B.java")),
        (ClaimKind.CALL_EDGE, ClaimTarget(symbol="W")),
        (ClaimKind.ABSENCE, ClaimTarget(symbol="W", scope="svc/")),
        (ClaimKind.HISTORY, ClaimTarget(file_path="a/B.java")),
    ]
    for kind, target in cases:
        fetch = derive_gap_fetch(kind, target)
        assert fetch, kind
        tokens = _FETCH_ACTION_TOKEN.findall(fetch)
        assert len(tokens) == 1, (kind, fetch)
        assert tokens[0] in CLAIM_ACQUIRING_ACTIONS[kind], (kind, fetch)


def test_r9_t04_precedence() -> None:
    proved = classify_terminal_handoff(
        status="ROOT_CAUSE_ESTABLISHED",
        gap_claim_kind=ClaimKind.DEFINITION,
        unavailable_actions=_DENIED,
        gap_fetch=_FETCH,
    )
    assert proved.state == HandoffState.PROVED
    crossed = classify_terminal_handoff(
        status="INSUFFICIENT_EVIDENCE",
        reason="insufficient",
        upstream_identity="tenant-x/app",
        gap_claim_kind=ClaimKind.DEFINITION,
        unavailable_actions=_DENIED,
        gap_fetch=_FETCH,
    )
    assert crossed.state == HandoffState.CROSS_BOUNDARY
    denied = classify_terminal_handoff(
        status="INSUFFICIENT_EVIDENCE",
        reason="budget_exceeded",
        gap_claim_kind=ClaimKind.DEFINITION,
        unavailable_actions=_DENIED,
        deny_reasons=dict(_REASONS),
        gap_fetch=_FETCH,
    )
    assert denied.state == HandoffState.CAPABILITY_DENIED


def test_r9_t05_no_claim_kind_falls_through() -> None:
    handoff = classify_terminal_handoff(
        status="INSUFFICIENT_EVIDENCE",
        reason="insufficient",
        gap_claim_kind=None,
        unavailable_actions=_DENIED,
        gap_fetch=_FETCH,
    )
    assert handoff.state == HandoffState.REASONING_DEFERRED


def test_r9_t06_loose_telemetry_match_removed() -> None:
    loose = classify_terminal_handoff(
        status="INSUFFICIENT_EVIDENCE",
        reason="insufficient",
        limitations=["the claim about the telemetry exporter retry loop was not verified"],
    )
    assert loose.state == HandoffState.REASONING_DEFERRED
    typed = classify_terminal_handoff(
        status="INSUFFICIENT_EVIDENCE",
        reason="provider_blocked:auth_failed",
    )
    assert typed.state == HandoffState.TELEMETRY_DEFERRED


def _hyp(kind, **kw):  # type: ignore[no-untyped-def]
    from investigation_agent_platform.domain.hypothesis.models import (
        Hypothesis,
        HypothesisEvidenceAssessment,
    )

    hid = uuid4()
    return Hypothesis(
        id=hid,
        tenant_id="t",
        investigation_id=uuid4(),
        title="h",
        statement="s",
        description="d",
        status=kw.get("status", HypothesisStatus.SUPPORTED),
        confidence_score=kw.get("confidence_score", 0.5),
        assessments=(
            [
                HypothesisEvidenceAssessment(
                    hypothesis_id=hid,
                    assessment="NEUTRAL",
                    strength="WEAK",
                    reason="unresolved obligation",
                    claim_kind=kind,
                    claim_target=kw.get("target"),
                )
            ]
            if kind is not None
            else []
        ),
    )


def test_r9_gap_claim_selects_strongest_unproven_claim() -> None:
    weak = _hyp(ClaimKind.DEFINITION, target=ClaimTarget(symbol="W"), confidence_score=0.2)
    strong = _hyp(
        ClaimKind.HISTORY, target=ClaimTarget(file_path="a/B.java"), confidence_score=0.9
    )
    verified = _hyp(
        ClaimKind.BEHAVIOR,
        target=ClaimTarget(symbol="V"),
        confidence_score=0.99,
        status=HypothesisStatus.VERIFIED,
    )
    assert gap_claim_for_hypotheses([weak, strong, verified]) == ClaimKind.HISTORY
    assert gap_claim_for_hypotheses([verified]) is None
    assert gap_claim_for_hypotheses([]) is None


def _lifecycle_ctx(profile_revisions, timeline_events):  # type: ignore[no-untyped-def]
    class _ProfileRepos:
        def __init__(self, revisions):  # type: ignore[no-untyped-def]
            self._revisions = revisions

        async def list_revisions(self, tenant_id, application_id):  # type: ignore[no-untyped-def]
            if isinstance(self._revisions, Exception):
                raise self._revisions
            return self._revisions

    class _Timeline:
        def __init__(self, events):  # type: ignore[no-untyped-def]
            self._events = events

        async def find_by_investigation_id(self, tenant_id, investigation_id):  # type: ignore[no-untyped-def]
            return self._events

    return SimpleNamespace(
        profile_repo=_ProfileRepos(profile_revisions),
        timeline_repo=_Timeline(timeline_events),
    )


def _denied_marker(action):  # type: ignore[no-untyped-def]
    return SimpleNamespace(
        event_type="RETRIEVAL_ATTEMPT",
        attributes={"action_type": action, "miss_reason": "authorization_denied"},
    )


@pytest.mark.asyncio
async def test_r9_t07_lifecycle_wiring_at_lvr() -> None:
    from investigation_agent_platform.application.worker.activities.lifecycle import (
        _denial_context,
        _denial_handoff_inputs,
        _denial_limitation_lines,
    )
    from investigation_agent_platform.domain.investigation.loop_version import LOOP_VERSION_LVR

    profile = _profile(["SOURCE_CODE"], [])
    ctx = _lifecycle_ctx(
        [profile],
        [_denied_marker("SEARCH_CODE"), _denied_marker("GET_CODE")],
    )
    blocked, reasons = await _denial_context(ctx, "t1", uuid4(), "app1")
    assert {"GET_CODE", "SEARCH_CODE"} <= set(blocked)
    assert reasons["GET_CODE"] == "capability_not_declared"
    assert reasons["SEARCH_CODE"] == "capability_not_declared"
    inputs = _denial_handoff_inputs(
        {"loop_version": LOOP_VERSION_LVR},
        gap_claim_for_hypotheses(
            [_hyp(ClaimKind.DEFINITION, target=ClaimTarget(symbol="W"))]
        ),
        blocked,
        reasons,
        "app1",
    )
    assert inputs["gap_claim_kind"] == ClaimKind.DEFINITION
    assert {"GET_CODE", "SEARCH_CODE"} <= set(inputs["unavailable_actions"])
    handoff = classify_terminal_handoff(
        status="INSUFFICIENT_EVIDENCE",
        reason="insufficient",
        gap_fetch=_FETCH,
        **inputs,
    )
    assert handoff.state == HandoffState.CAPABILITY_DENIED
    lines = _denial_limitation_lines(blocked, reasons, "app1")
    assert any(
        "GET_CODE" in line and "capability_not_declared" in line and "app1" in line
        for line in lines
    )


@pytest.mark.asyncio
async def test_r9_t08_pre_lvr_run_is_unchanged() -> None:
    from investigation_agent_platform.application.worker.activities.lifecycle import (
        _denial_handoff_inputs,
    )

    inputs = _denial_handoff_inputs(
        {"loop_version": "lv7"},
        ClaimKind.DEFINITION,
        ("GET_CODE", "SEARCH_CODE"),
        dict(_REASONS),
        "app1",
    )
    assert inputs == {}
    handoff = classify_terminal_handoff(
        status="INSUFFICIENT_EVIDENCE",
        reason="insufficient",
        gap_fetch=_FETCH,
    )
    assert handoff.state == HandoffState.REASONING_DEFERRED


@pytest.mark.asyncio
async def test_r9_t09_context_lookup_failure_fails_open() -> None:
    from investigation_agent_platform.application.worker.activities.lifecycle import (
        _denial_context,
    )

    ctx = _lifecycle_ctx(RuntimeError("profile store down"), [_denied_marker("SEARCH_CODE")])
    blocked, reasons = await _denial_context(ctx, "t1", uuid4(), "app1")
    assert blocked == ("SEARCH_CODE",)
    assert reasons == {"SEARCH_CODE": "denied_by_policy"}
    handoff = classify_terminal_handoff(
        status="INSUFFICIENT_EVIDENCE",
        reason="insufficient",
        gap_claim_kind=ClaimKind.DEFINITION,
        unavailable_actions=blocked,
        deny_reasons=reasons,
        gap_fetch="SEARCH_CODE Widget call sites",
    )
    assert handoff.state == HandoffState.REASONING_DEFERRED


def test_r9_t10_profile_lint() -> None:
    from investigation_agent_platform.domain.profile.capability_lint import (
        lint_profile_capabilities,
    )

    log_only = lint_profile_capabilities(_profile(["LOG"], ["SEARCH_LOGS"]))
    kinds = {line.split("claims of kind ")[1].split(" cannot")[0] for line in log_only}
    assert {"DEFINITION", "BEHAVIOR", "CAUSAL", "CALL_EDGE", "ABSENCE", "HISTORY"} <= kinds
    history_only = lint_profile_capabilities(
        _profile(["COMMIT_HISTORY"], ["GET_CODE_HISTORY"])
    )
    history_kinds = {
        line.split("claims of kind ")[1].split(" cannot")[0] for line in history_only
    }
    assert "HISTORY" not in history_kinds
    assert "DEFINITION" in history_kinds
    full = lint_profile_capabilities(
        _profile(
            ["LOG", "TRACE", "DATABASE_STATE", "SOURCE_CODE", "CALL_GRAPH",
             "COMMIT_HISTORY", "DOCUMENTATION", "API_PROBE"],
            ["SEARCH_LOGS", "CORRELATE", "QUERY_STATE", "WALK_SESSION_GRAPH",
             "SEARCH_CODE", "GET_CODE", "FIND_SYMBOL", "FIND_CALL_GRAPH",
             "TRACE_JOURNEY", "WALK_STACK", "TRACE_LOGIC", "GET_CODE_HISTORY",
             "SEARCH_REF_DOCS", "PROBE_API"],
        )
    )
    assert full == []


def test_r9_t11_round_trip_and_g1() -> None:
    from investigation_agent_platform.infrastructure.persistence.finding_repository import (
        _handoff_from_row,
    )

    handoff = _classify_denied()
    stored = json.loads(json.dumps(handoff.model_dump(mode="json")))
    revived = _handoff_from_row(SimpleNamespace(handoff=stored))
    assert revived == handoff
    assert names_concrete_fetch_target(revived.next_fetch)
    assert revived.price is not None and revived.price > 0
