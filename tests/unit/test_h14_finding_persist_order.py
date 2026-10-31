# tests/unit/test_h14_finding_persist_order.py
"""H14: a Finding is validated and bounded BEFORE anything is persisted.

Before: the verified hypothesis was saved VERIFIED, then `Finding(...)` was
built from an unbounded, undeduplicated causal chain. A chain over the model's
50-id cap raised after the hypothesis write, leaving a VERIFIED hypothesis with
no finding, and the retry re-crashed the same way.

After: ids are deduplicated (order preserved) and capped to the model limits
first; if the Finding still fails validation the run persists an honest
abstention and writes no VERIFIED hypothesis.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_claim_binding_phase3 import _code_row, _def_hyp, _def_read

from investigation_agent_platform.domain.finding.models import (
    CONCLUSION_MAX_CONTRADICTING_IDS,
    CONCLUSION_MAX_EVIDENCE_IDS,
    FINDING_MAX_CAUSAL_IDS,
    FINDING_MAX_EVIDENCE_IDS,
    Finding,
    InvestigationConclusion,
    dedupe_and_cap_ids,
)
from investigation_agent_platform.domain.hypothesis.models import HypothesisCausalStep

# --- the pure helper ---------------------------------------------------------------------------


def test_dedupe_preserves_first_seen_order() -> None:
    a, b, c = uuid4(), uuid4(), uuid4()
    assert dedupe_and_cap_ids([a, b, a, c, b], 10) == [a, b, c]


def test_cap_keeps_the_leading_ids() -> None:
    ids = [uuid4() for _ in range(7)]
    assert dedupe_and_cap_ids(ids, 3) == ids[:3]


def test_cap_applies_after_dedupe() -> None:
    a, b, c = uuid4(), uuid4(), uuid4()
    assert dedupe_and_cap_ids([a, a, a, b, c], 2) == [a, b]


@pytest.mark.parametrize("value", [None, [], ()])
def test_empty_inputs_give_an_empty_list(value) -> None:
    assert dedupe_and_cap_ids(value, 5) == []


def test_a_non_positive_cap_gives_an_empty_list() -> None:
    assert dedupe_and_cap_ids([uuid4()], 0) == []


def test_the_constants_track_the_model_limits() -> None:
    def cap(model, name):  # type: ignore[no-untyped-def]
        return next(
            m.max_length
            for m in model.model_fields[name].metadata
            if getattr(m, "max_length", None) is not None
        )

    assert cap(Finding, "evidence_ids") == FINDING_MAX_EVIDENCE_IDS
    assert cap(Finding, "causal_chain") == FINDING_MAX_CAUSAL_IDS
    assert cap(InvestigationConclusion, "supporting_evidence_ids") == CONCLUSION_MAX_EVIDENCE_IDS
    assert (
        cap(InvestigationConclusion, "contradicting_evidence_ids")
        == CONCLUSION_MAX_CONTRADICTING_IDS
    )


# --- the activity ---------------------------------------------------------------------------------


def _hyp_with_wide_causal_chain():  # type: ignore[no-untyped-def]
    """A verifying DEFINITION hypothesis whose causal steps cite 61 distinct rows,
    with 20 repeats of one shared row (80 cited ids, 61 distinct)."""
    read = _def_read()
    extras = [_code_row("GET_CODE", f"src/f{i}.py", "abc123", f"row {i}") for i in range(60)]
    steps = [
        HypothesisCausalStep(
            cause=f"c{i}",
            mechanism=f"m{i}",
            effect=f"e{i}",
            evidence_ids=[
                extras[i * 3].evidence_id,
                extras[i * 3 + 1].evidence_id,
                extras[i * 3 + 2].evidence_id,
                read.evidence_id,
            ],
        )
        for i in range(20)
    ]
    hyp = _def_hyp(read).model_copy(update={"tenant_id": "t", "causal_steps": steps})
    return hyp, [read, *extras]


async def _conclude(hyp, rows, *, finding_factory=None, hyp_saves=None):  # type: ignore[no-untyped-def]
    import test_claim_binding_phase1 as fixtures

    from investigation_agent_platform.application.worker.activities import lifecycle

    inv_id = uuid4()
    hyp = hyp.model_copy(update={"investigation_id": inv_id})
    captured: dict = {"findings": [], "conclusion": None}

    async def _save_conclusion(tenant_id, conclusion):  # type: ignore[no-untyped-def]
        captured["conclusion"] = conclusion

    async def _save_finding_record(tenant_id, finding):  # type: ignore[no-untyped-def]
        captured["findings"].append(finding)

    async def _noop(*args, **kwargs):  # type: ignore[no-untyped-def]
        return None

    ctx = fixtures._activity_ctx(
        {"claim_binding_version": "cbv1", "verifier_version": "v2"}, hyp, rows, []
    )
    ctx.finding_repo = SimpleNamespace(
        save_conclusion=_save_conclusion,
        save=_noop,
        save_finding=_noop,
        save_finding_record=_save_finding_record,
    )
    if hyp_saves is not None:

        async def _save(tenant_id, h, investigation_id, expected_version=0):  # type: ignore[no-untyped-def]
            hyp_saves.append(h)

        ctx.hypothesis_repo.save = _save
    # lifecycle imports Finding inside the function, at call time.
    import investigation_agent_platform.domain.finding.models as finding_models

    original = finding_models.Finding
    if finding_factory is not None:
        finding_models.Finding = finding_factory  # type: ignore[misc,assignment]
    try:
        await lifecycle._gate_conclusion_or_fail(
            "t", inv_id, ctx, shadow_notes=None, triage_route=""
        )
    finally:
        finding_models.Finding = original  # type: ignore[misc]
    return captured


@pytest.mark.asyncio
async def test_a_wide_duplicated_causal_chain_publishes_a_bounded_deduped_finding() -> None:
    hyp, rows = _hyp_with_wide_causal_chain()
    captured = await _conclude(hyp, rows)
    assert captured["findings"], "the finding must be persisted, not crash"
    finding = captured["findings"][0]
    assert len(finding.causal_chain) <= FINDING_MAX_CAUSAL_IDS
    assert len(set(finding.causal_chain)) == len(finding.causal_chain)
    assert len(finding.evidence_ids) <= FINDING_MAX_EVIDENCE_IDS
    assert len(set(finding.evidence_ids)) == len(finding.evidence_ids)
    conclusion = captured["conclusion"]
    assert conclusion is not None
    assert len(conclusion.supporting_evidence_ids) <= CONCLUSION_MAX_EVIDENCE_IDS


@pytest.mark.asyncio
async def test_a_finding_that_still_fails_validation_abstains_without_a_verified_hypothesis() -> (
    None
):
    from pydantic import ValidationError

    def _always_invalid(**kwargs):  # type: ignore[no-untyped-def]
        # Same failure class as the 50-id cap: validation raises on construction.
        return Finding.model_validate({**kwargs, "causal_chain": [uuid4()] * 51})

    hyp, rows = _hyp_with_wide_causal_chain()
    saves: list = []
    # Premise: constructing the invalid finding really raises.
    with pytest.raises(ValidationError):
        _always_invalid(
            tenant_id="t",
            investigation_id=uuid4(),
            finding_type="OBSERVATION",
            title="t",
            statement="s",
        )
    captured = await _conclude(hyp, rows, finding_factory=_always_invalid, hyp_saves=saves)
    assert captured["findings"] == [], "no finding may be saved"
    assert not [h for h in saves if str(getattr(h.status, "value", h.status)) == "VERIFIED"], (
        "a VERIFIED hypothesis must not be written when the finding cannot be built"
    )
    conclusion = captured["conclusion"]
    assert conclusion is not None, "an honest abstention is persisted instead of a crash"
    assert str(getattr(conclusion.status, "value", conclusion.status)) in {
        "INSUFFICIENT_EVIDENCE",
        "NO_ROOT_CAUSE_FOUND",
    }


@pytest.mark.asyncio
async def test_a_valid_conclusion_still_verifies_the_hypothesis_after_building_the_finding() -> None:
    read = _def_read()
    hyp = _def_hyp(read).model_copy(update={"tenant_id": "t"})
    hyp = hyp.model_copy(update={"status": hyp.status})
    saves: list = []
    captured = await _conclude(hyp, [read], hyp_saves=saves)
    assert captured["findings"], "baseline: a normal verdict still persists its finding"
