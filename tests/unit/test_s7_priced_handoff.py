"""T2 remainder: numeric handoff price and concrete next-fetch targets."""

from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from investigation_agent_platform.domain.finding.models import (
    MAX_FETCH_PRICE,
    HandoffState,
    InvestigationConclusion,
    S7Handoff,
    classify_terminal_handoff,
    derive_gap_fetch,
    gap_fetch_for_hypotheses,
    names_concrete_fetch_target,
    price_next_fetch,
)
from investigation_agent_platform.domain.hypothesis.models import (
    ClaimKind,
    ClaimTarget,
    Hypothesis,
    HypothesisEvidenceAssessment,
    HypothesisStatus,
)
from investigation_agent_platform.infrastructure.persistence.finding_repository import (
    _handoff_from_row,
)

# --- price field -----------------------------------------------------------


def test_price_defaults_to_unpriced() -> None:
    assert S7Handoff(state=HandoffState.FRONTIER_EXHAUSTED).price is None


@pytest.mark.parametrize("bad", [-0.1, MAX_FETCH_PRICE + 1, float("nan")])
def test_price_rejects_out_of_range_values(bad: float) -> None:
    with pytest.raises(ValidationError):
        S7Handoff(state=HandoffState.BUDGET_DEFERRED, price=bad)


# --- what is concrete, what it costs -----------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "GET_CODE svc/src/main/java/com/x/Foo.java lines 10-40",
        "SEARCH_CODE Foo",
        "GET_CODE_HISTORY svc/Foo.java",
        "Open Foo.java",
    ],
)
def test_named_actions_and_paths_are_concrete(text: str) -> None:
    assert names_concrete_fetch_target(text)


@pytest.mark.parametrize(
    "text",
    [
        None,
        "",
        "Bind supporting evidence and a cited causal chain to a candidate hypothesis, then re-verify.",
        "Narrow or re-scope the empty-search proof to the claimed scope, then re-verify.",
        "Restore the blocked provider fetch and resume, then re-verify.",
    ],
)
def test_template_prose_is_not_concrete_and_is_unpriced(text: str | None) -> None:
    assert not names_concrete_fetch_target(text)
    assert price_next_fetch(text) is None


def test_price_counts_distinct_named_actions_one_unit_each() -> None:
    assert price_next_fetch("GET_CODE svc/Foo.java") == 1.0
    assert price_next_fetch("GET_CODE svc/Foo.java then SEARCH_CODE Foo") == 2.0
    assert (
        price_next_fetch("GET_CODE a/B.java, GET_CODE a/C.java") == 1.0
    )  # same action, distinct set
    assert price_next_fetch("GET_CODE_HISTORY svc/Foo.java") == 1.0  # not read as GET_CODE too


def test_path_only_fetch_costs_one_implied_read() -> None:
    assert price_next_fetch("Read svc/src/Foo.java lines 40-80") == 1.0


# --- builder prices a concrete fetch ------------------------------------------


def test_builder_prices_a_concrete_next_check() -> None:
    handoff = classify_terminal_handoff(
        status="INSUFFICIENT_EVIDENCE",
        reason="budget_exceeded",
        next_check="GET_CODE svc/Foo.java lines 10-40, then SEARCH_CODE Foo",
    )
    assert handoff.state == HandoffState.BUDGET_DEFERRED
    assert handoff.next_fetch is not None and "svc/Foo.java" in handoff.next_fetch
    assert handoff.price == 2.0


# --- serializes through the existing JSONB column ------------------------------


def _priced() -> S7Handoff:
    return S7Handoff(
        state=HandoffState.BUDGET_DEFERRED,
        next_fetch="GET_CODE svc/Foo.java lines 10-40",
        price=1.0,
        resume_pointer="resume at the blocked fetch",
        recommended_next_steps=["GET_CODE svc/Foo.java lines 10-40"],
    )


def test_priced_handoff_round_trips_through_the_handoff_column_shape() -> None:
    # The repository writes handoff.model_dump(mode="json") into a JSONB column
    # and rehydrates it with _handoff_from_row; no schema change is involved.
    stored = json.loads(json.dumps(_priced().model_dump(mode="json")))
    assert stored["price"] == 1.0
    assert stored["next_fetch"] == "GET_CODE svc/Foo.java lines 10-40"
    revived = _handoff_from_row(SimpleNamespace(handoff=stored))
    assert revived == _priced()


def test_pre_price_rows_still_rehydrate_unpriced() -> None:
    stored = _priced().model_dump(mode="json")
    stored.pop("price")
    revived = _handoff_from_row(SimpleNamespace(handoff=stored))
    assert revived is not None and revived.price is None


def test_priced_handoff_survives_the_conclusion_api_dump() -> None:
    conclusion = InvestigationConclusion(
        tenant_id="t",
        investigation_id=uuid4(),
        status="INSUFFICIENT_EVIDENCE",
        limitations=["x"],
        handoff=_priced(),
    )
    dumped = conclusion.model_dump(mode="json")  # what the conclusion endpoint returns
    assert dumped["handoff"]["price"] == 1.0
    again = InvestigationConclusion.model_validate(dumped)
    assert again.handoff is not None and again.handoff.price == 1.0


# --- chunk 2: the builder emits a concrete target, never prose, in next_fetch -------

PROSE = "Bind a causal chain with cited evidence for each link, then re-verify."


def _hyp(kind: ClaimKind | None, target: ClaimTarget | None, **kw: object) -> Hypothesis:
    hid = uuid4()
    assessments = (
        [
            HypothesisEvidenceAssessment(
                hypothesis_id=hid,
                assessment="NEUTRAL",
                strength="WEAK",
                reason="unresolved obligation",
                claim_kind=kind,
                claim_target=target,
            )
        ]
        if kind is not None
        else []
    )
    return Hypothesis(
        id=hid,
        tenant_id="t",
        investigation_id=uuid4(),
        title="h",
        statement="s",
        description="d",
        assessments=assessments,
        **kw,  # type: ignore[arg-type]
    )


@pytest.mark.parametrize(
    ("kind", "target", "expected"),
    [
        (
            ClaimKind.DEFINITION,
            ClaimTarget(symbol="Foo", file_path="svc/Foo.java"),
            "GET_CODE svc/Foo.java for Foo",
        ),
        (
            ClaimKind.DEFINITION,
            ClaimTarget(file_path="svc/Foo.java", line_start=10, line_end=40),
            "GET_CODE svc/Foo.java lines 10-40",
        ),
        (ClaimKind.DEFINITION, ClaimTarget(symbol="Foo"), "SEARCH_CODE Foo"),
        (ClaimKind.ABSENCE, ClaimTarget(symbol="Foo", scope="svc/"), "SEARCH_CODE Foo within svc/"),
        (ClaimKind.ABSENCE, ClaimTarget(symbol="Foo"), "SEARCH_CODE Foo"),
        (
            ClaimKind.CALL_EDGE,
            ClaimTarget(symbol="bar", counterpart="Caller.run", file_path="svc/Caller.java"),
            "GET_CODE svc/Caller.java (call site of bar)",
        ),
        (ClaimKind.CALL_EDGE, ClaimTarget(symbol="bar"), "SEARCH_CODE bar call sites"),
        (ClaimKind.HISTORY, ClaimTarget(file_path="svc/Foo.java"), "GET_CODE_HISTORY svc/Foo.java"),
        (
            ClaimKind.BEHAVIOR,
            ClaimTarget(symbol="Foo", file_path="svc/Foo.java"),
            "GET_CODE svc/Foo.java for Foo",
        ),
        (ClaimKind.CAUSAL, ClaimTarget(symbol="Foo"), "SEARCH_CODE Foo"),
    ],
)
def test_gap_fetch_names_the_claims_own_target(
    kind: ClaimKind, target: ClaimTarget, expected: str
) -> None:
    fetch = derive_gap_fetch(kind, target)
    assert fetch == expected
    assert names_concrete_fetch_target(fetch)
    assert price_next_fetch(fetch) == 1.0


@pytest.mark.parametrize(
    ("kind", "target"),
    [
        (None, ClaimTarget(symbol="Foo")),
        (ClaimKind.NOT_CHECKABLE, ClaimTarget(symbol="Foo")),
        (ClaimKind.DEFINITION, None),
        (ClaimKind.DEFINITION, ClaimTarget()),
        (ClaimKind.ABSENCE, ClaimTarget(scope="svc/")),
        (ClaimKind.HISTORY, ClaimTarget(symbol="Foo")),
    ],
)
def test_gap_fetch_is_none_when_nothing_concrete_can_be_named(
    kind: ClaimKind | None, target: ClaimTarget | None
) -> None:
    assert derive_gap_fetch(kind, target) is None


def test_gap_fetch_never_invents_a_runtime_fetch_for_behavior_claims() -> None:
    assert derive_gap_fetch(ClaimKind.BEHAVIOR, ClaimTarget(scope="runtime")) is None


def test_gap_fetch_for_hypotheses_picks_the_strongest_unproven_claim() -> None:
    verified = _hyp(
        ClaimKind.DEFINITION,
        ClaimTarget(symbol="Done"),
        status=HypothesisStatus.VERIFIED,
        confidence_score=0.99,
    )
    weak = _hyp(ClaimKind.DEFINITION, ClaimTarget(symbol="Weak"), confidence_score=0.2)
    strong = _hyp(ClaimKind.ABSENCE, ClaimTarget(symbol="Strong"), confidence_score=0.8)
    untyped = _hyp(None, None, confidence_score=0.95)
    assert gap_fetch_for_hypotheses([verified, weak, strong, untyped]) == "SEARCH_CODE Strong"
    assert gap_fetch_for_hypotheses([verified, untyped]) is None
    assert gap_fetch_for_hypotheses([]) is None
    assert gap_fetch_for_hypotheses(None) is None


def test_builder_uses_the_gap_target_and_keeps_prose_in_the_steps() -> None:
    handoff = classify_terminal_handoff(
        status="INSUFFICIENT_EVIDENCE",
        reason="insufficient",
        next_check=PROSE,
        gap_fetch="GET_CODE svc/Foo.java lines 10-40",
    )
    assert handoff.state == HandoffState.FRONTIER_EXHAUSTED
    assert handoff.next_fetch == "GET_CODE svc/Foo.java lines 10-40"
    assert handoff.price == 1.0
    assert PROSE in handoff.recommended_next_steps
    assert "Next fetch: GET_CODE svc/Foo.java lines 10-40" in handoff.recommended_next_steps


def test_builder_never_stores_prose_as_next_fetch() -> None:
    for reason in ("insufficient", "budget_exceeded", "provider_blocked:auth_failed"):
        handoff = classify_terminal_handoff(
            status="INSUFFICIENT_EVIDENCE", reason=reason, next_check=PROSE
        )
        assert handoff.next_fetch is None, reason
        assert handoff.price is None, reason


def test_non_concrete_gap_fetch_is_ignored() -> None:
    handoff = classify_terminal_handoff(
        status="INSUFFICIENT_EVIDENCE", reason="insufficient", gap_fetch="try harder"
    )
    assert handoff.next_fetch is None and handoff.price is None


def test_budget_deferred_resumes_at_the_gap_target() -> None:
    handoff = classify_terminal_handoff(
        status="INSUFFICIENT_EVIDENCE",
        reason="budget_exceeded",
        next_check=PROSE,
        gap_fetch="SEARCH_CODE Foo within svc/",
    )
    assert handoff.state == HandoffState.BUDGET_DEFERRED
    assert handoff.next_fetch == "SEARCH_CODE Foo within svc/"
    assert handoff.price == 1.0
    assert handoff.resume_pointer is not None and "SEARCH_CODE Foo" in handoff.resume_pointer


def test_gap_target_alone_without_prose_still_yields_a_priced_handoff() -> None:
    handoff = classify_terminal_handoff(
        status="INSUFFICIENT_EVIDENCE", reason="insufficient", gap_fetch="GET_CODE svc/Foo.java"
    )
    assert (handoff.next_fetch, handoff.price) == ("GET_CODE svc/Foo.java", 1.0)


# --- lifecycle collector (store -> gap fetch) ----------------------------------


class _HypRepo:
    def __init__(self, rows: list[Hypothesis] | Exception) -> None:
        self._rows = rows

    async def find_by_investigation_id(self, tenant_id: str, investigation_id: object) -> list:
        if isinstance(self._rows, Exception):
            raise self._rows
        return self._rows


async def test_collect_open_gap_fetch_reads_the_store_and_derives_the_target() -> None:
    from investigation_agent_platform.application.worker.activities.lifecycle import (
        _collect_open_gap_fetch,
    )

    ctx = SimpleNamespace(
        hypothesis_repo=_HypRepo([_hyp(ClaimKind.ABSENCE, ClaimTarget(symbol="Foo"))])
    )
    fetch = await _collect_open_gap_fetch(ctx, "t", uuid4(), gap_fetch_for_hypotheses)
    assert fetch == "SEARCH_CODE Foo"


@pytest.mark.parametrize(
    "ctx",
    [
        SimpleNamespace(),  # no repository
        SimpleNamespace(hypothesis_repo=_HypRepo(RuntimeError("db down"))),  # store fault
        SimpleNamespace(hypothesis_repo=_HypRepo([])),  # nothing to derive from
    ],
)
async def test_collect_open_gap_fetch_fails_open_to_none(ctx: SimpleNamespace) -> None:
    from investigation_agent_platform.application.worker.activities.lifecycle import (
        _collect_open_gap_fetch,
    )

    assert await _collect_open_gap_fetch(ctx, "t", uuid4(), gap_fetch_for_hypotheses) is None
