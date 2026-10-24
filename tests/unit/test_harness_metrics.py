"""Metrics recompute and R1–R8 report verification (§4)."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from investigation_agent_platform.harness.freeze import freeze_digest
from investigation_agent_platform.harness.metrics import (
    attempt_tally,
    capture_totals,
    cell_counts,
    check_formula_catalog,
    control_match,
    eligible_t1_turns,
    exclusion_breakdown,
    formula_digest_for,
    tap_populations,
    verify_report,
)
from investigation_agent_platform.harness.registry import (
    HarnessValidationError,
    load_registry,
)

DOCS = Path(__file__).resolve().parents[2] / "docs"


def _vectors():
    import json

    return json.loads(
        (DOCS / "IAP-proof-kernel-d3-stageC-schema-vectors-v1.15.json").read_text(encoding="utf-8")
    )


def _context():

    reg = load_registry(DOCS / "IAP-proof-kernel-d3-stageC-harness-schemas-v1.13.json")
    vec = _vectors()
    return {
        "registry": reg,
        "formulas": vec["formulas"],
        "catalog": reg["$defs"]["formulaCatalog"]["const"],
        "required": reg["$defs"]["requiredMetricSet"]["const"],
        "reasons": set(reg["$defs"]["reasonCode"]["enum"]),
        "base": next(c for c in vec["report_cases"] if c["id"] == "RC-valid-terminal"),
    }


def test_eligible_t1_and_cells() -> None:
    ctx = _context()
    turns = ctx["base"]["turns"]
    assert len(eligible_t1_turns(turns)) == 20
    cells = cell_counts(turns)
    assert sum(cells.values()) == 20
    assert cells["accept-PASS"] == 6


def test_attempt_tally_rejects_duplicates() -> None:
    with pytest.raises(HarnessValidationError, match="duplicate attempt"):
        attempt_tally(
            [{"id": "a", "disposition": "ELIGIBLE"}, {"id": "a", "disposition": "ELIGIBLE"}]
        )


def test_exclusion_breakdown_rejects_open_reason() -> None:
    with pytest.raises(HarnessValidationError, match="closed vocabulary"):
        exclusion_breakdown([{"eligibility": "EXCLUDED", "reason": "MYSTERY"}], {"JOIN_OK"})


def test_capture_totals_rejects_unknown_kind() -> None:
    with pytest.raises(HarnessValidationError, match="unknown capture event kind"):
        capture_totals([], [], [{"kind": "mystery"}])


def test_tap_populations_shape() -> None:
    ctx = _context()
    taps = tap_populations(ctx["base"]["turns"])
    assert taps["T1"]["population"] == 23
    assert taps["T3"]["population"] == 1


def test_control_match_single_contribution() -> None:
    ctx = _context()
    num, den, matched = control_match(ctx["base"]["controls"], {"L1", "L2", "L3"})
    assert (num, den) == (3, 3)
    assert len(matched) == 3
    with pytest.raises(HarnessValidationError, match="outside frozen cohort"):
        control_match(ctx["base"]["controls"], {"L1"})


def test_formula_digest_matches_catalog() -> None:
    ctx = _context()
    text = ctx["catalog"]["CONTROL_MATCH"]["text"]
    assert formula_digest_for(text) == ctx["catalog"]["CONTROL_MATCH"]["digest"]


def test_check_formula_catalog_rejects_text_change() -> None:
    ctx = _context()
    tampered = copy.deepcopy(ctx["formulas"])
    tampered["CONTROL_MATCH"] = {
        "text": ctx["formulas"]["CONTROL_MATCH"]["text"] + "amended\n",
        "digest": ctx["formulas"]["CONTROL_MATCH"]["digest"],
    }
    with pytest.raises(HarnessValidationError, match="pinned catalog"):
        check_formula_catalog(tampered, ctx["catalog"], ctx["required"])


def _verify(case, ctx, registry=None) -> None:
    inputs = {
        k: case[k]
        for k in (
            "turns",
            "attempts",
            "receipts",
            "controls",
            "cohort_fixtures",
            "capture_events",
            "control_native_divergence",
        )
    }
    if "formulas" in case:
        inputs["formulas"] = case["formulas"]
    verify_report(
        inputs=inputs,
        report=case["report"],
        registry=registry if registry is not None else ctx["registry"],
        terminal=case["terminal"],
        freeze=case["freeze"],
    )


def test_verify_report_accepts_base_case() -> None:
    ctx = _context()
    _verify(ctx["base"], ctx)


def test_verify_report_rejects_modified_registry_policy() -> None:
    ctx = _context()
    altered_registry = copy.deepcopy(ctx["registry"])
    altered_registry["$defs"]["requiredMetricSet"]["const"].append("UNPINNED_METRIC")
    with pytest.raises(HarnessValidationError, match="registry digest mismatch"):
        _verify(ctx["base"], ctx, registry=altered_registry)


def test_verify_report_rejects_cell_tamper() -> None:
    ctx = _context()
    case = copy.deepcopy(ctx["base"])
    case["report"]["metrics"][1]["numerator"] += 1
    with pytest.raises(HarnessValidationError, match="CELL_ACCEPT_PASS mismatch"):
        _verify(case, ctx)


def test_verify_report_rejects_nested_authority_claim_in_input() -> None:
    ctx = _context()
    case = copy.deepcopy(ctx["base"])
    case["turns"][0]["activity"]["note"] = "proven"
    with pytest.raises(HarnessValidationError, match="forbidden authority value"):
        _verify(case, ctx)


def test_verify_report_rejects_extra_report_field() -> None:
    ctx = _context()
    case = copy.deepcopy(ctx["base"])
    case["report"]["unrecognized"] = "ignored before"
    with pytest.raises(
        HarnessValidationError, match="report schema violation.*Additional properties"
    ):
        _verify(case, ctx)


def test_verify_report_rejects_stray_receipt_mismatch() -> None:
    ctx = _context()
    case = copy.deepcopy(ctx["base"])
    for receipt in case["receipts"]:
        if receipt["id"] == "e-cap-1":
            receipt["workflow_id"] = "w-ghost"
    with pytest.raises(HarnessValidationError, match="differs from activity identity"):
        _verify(case, ctx)


def test_verify_report_rejects_receipt_chain_mismatch() -> None:
    ctx = _context()
    case = copy.deepcopy(ctx["base"])
    case["receipts"][1]["previous_digest"] = "sha256:" + "aa" * 32
    with pytest.raises(HarnessValidationError, match="previous_digest does not match"):
        _verify(case, ctx)


def test_verify_report_rejects_catalog_tamper() -> None:
    ctx = _context()
    vec = _vectors()
    case = next(c for c in vec["report_cases"] if c["id"] == "RC-formula-catalog-mismatch")
    with pytest.raises(HarnessValidationError, match="pinned catalog"):
        _verify(case, ctx)


def test_verify_report_rejects_capture_metric_tamper() -> None:
    ctx = _context()
    case = copy.deepcopy(ctx["base"])
    for metric in case["report"]["metrics"]:
        if metric["id"] == "CAPTURE_HEALTH":
            metric["numerator"] += 1
    with pytest.raises(HarnessValidationError, match="CAPTURE_HEALTH"):
        _verify(case, ctx)


def test_verify_report_rejects_report_freeze_mismatch() -> None:
    ctx = _context()
    vec = _vectors()
    case = next(c for c in vec["report_cases"] if c["id"] == "RC-report-freeze-mismatch")
    with pytest.raises(HarnessValidationError, match="report freeze_hash does not match"):
        _verify(case, ctx)


def test_verify_report_rejects_freeze_self_hash_mismatch() -> None:
    ctx = _context()
    vec = _vectors()
    case = next(c for c in vec["report_cases"] if c["id"] == "RC-freeze-self-hash-mismatch")
    with pytest.raises(HarnessValidationError, match="freeze_hash does not match"):
        _verify(case, ctx)


def test_verify_report_rejects_freeze_run_set_mismatch() -> None:
    ctx = _context()
    case = copy.deepcopy(ctx["base"])
    case["freeze"]["run_set_id"] = "rs-002"
    case["freeze"]["freeze_hash"] = freeze_digest(case["freeze"])
    case["report"]["freeze_hash"] = case["freeze"]["freeze_hash"]
    with pytest.raises(HarnessValidationError, match="freeze run_set_id does not match"):
        _verify(case, ctx)


def test_verify_report_rejects_missing_inputs_block() -> None:
    ctx = _context()
    case = copy.deepcopy(ctx["base"])
    inputs = {
        k: case[k]
        for k in (
            "attempts",
            "receipts",
            "controls",
            "cohort_fixtures",
            "capture_events",
            "control_native_divergence",
        )
    }
    with pytest.raises(HarnessValidationError, match="missing required block: turns"):
        verify_report(
            inputs=inputs,
            report=case["report"],
            registry=ctx["registry"],
            terminal=case["terminal"],
            freeze=case["freeze"],
        )


def test_verify_report_rejects_non_object_inputs() -> None:
    ctx = _context()
    case = ctx["base"]
    for bad in (None, [], "turns"):
        with pytest.raises(HarnessValidationError, match="inputs must be an object"):
            verify_report(
                inputs=bad,
                report=case["report"],
                registry=ctx["registry"],
                terminal=case["terminal"],
                freeze=case["freeze"],
            )


def test_verify_report_rejects_non_list_inputs_block() -> None:
    ctx = _context()
    case = copy.deepcopy(ctx["base"])
    case["receipts"] = {"e-0": "not-a-list"}
    inputs = {
        k: case[k]
        for k in (
            "turns",
            "attempts",
            "receipts",
            "controls",
            "cohort_fixtures",
            "capture_events",
            "control_native_divergence",
        )
    }
    with pytest.raises(HarnessValidationError, match="inputs receipts must be an array"):
        verify_report(
            inputs=inputs,
            report=case["report"],
            registry=ctx["registry"],
            terminal=case["terminal"],
            freeze=case["freeze"],
        )


def test_verify_report_rejects_control_label_tamper() -> None:
    ctx = _context()
    vec = _vectors()
    case = next(c for c in vec["report_cases"] if c["id"] == "RC-control-label-tamper")
    with pytest.raises(HarnessValidationError, match="CONTROL_MATCH numerator"):
        _verify(case, ctx)


def test_verify_report_rejects_control_counterpart_missing() -> None:
    ctx = _context()
    vec = _vectors()
    case = next(c for c in vec["report_cases"] if c["id"] == "RC-control-counterpart-missing")
    with pytest.raises(HarnessValidationError, match="CONTROL_MATCH numerator"):
        _verify(case, ctx)


def test_verify_report_rejects_control_order_mismatch() -> None:
    ctx = _context()
    vec = _vectors()
    case = next(c for c in vec["report_cases"] if c["id"] == "RC-control-order-mismatch")
    with pytest.raises(HarnessValidationError, match="CONTROL_MATCH numerator"):
        _verify(case, ctx)


def test_verify_report_rejects_reservation_over_freeze_cap() -> None:
    ctx = _context()
    vec = _vectors()
    case = next(c for c in vec["report_cases"] if c["id"] == "RC-reservation-over-freeze-cap")
    assert case["attempts"]["total"] == 49
    assert case["freeze"]["total_reservations_max"] == 48
    with pytest.raises(HarnessValidationError, match="exceeds frozen reservation limit"):
        _verify(case, ctx)


def test_control_match_recomputes_label_from_counterpart() -> None:
    # A MATCHED label with truthy counterpart_ref/order fields but a
    # divergent shadow counterpart contributes nothing: the label is
    # recomputed, never trusted.
    ctx = _context()
    controls = copy.deepcopy(ctx["base"]["controls"])
    assert control_match(controls, {"L1", "L2", "L3"})[0] == 3
    controls[0]["counterpart"]["tenant"] = "other-tenant"
    assert control_match(controls, {"L1", "L2", "L3"})[0] == 2
