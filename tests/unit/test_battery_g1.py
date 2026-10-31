# tests/unit/test_battery_g1.py
"""G1 (bound-or-concrete-handoff) is computed, not asserted.

Review finding T2: the earlier live-battery proxy counted any abstention as
a handoff because the handoff builder always emits filler text. These tests
pin that filler, state labels, and hypothesis-linked rows do not satisfy G1.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import tranche_b_battery as battery

EID = "11111111-1111-1111-1111-111111111111"


def _evidence(*ids: str, miss_reason: str | None = None) -> dict:
    items = []
    for eid in ids:
        attrs = {"miss_reason": miss_reason} if miss_reason else {}
        items.append({"evidence_id": eid, "evidence_type": "SOURCE_CODE", "attributes": attrs})
    return {"items": items}


def test_answered_with_resolvable_cited_row_is_bound() -> None:
    ok, kind, _ = battery.score_g1(
        {"status": "ANSWERED", "supporting_evidence_ids": [EID]}, _evidence(EID)
    )
    assert (ok, kind) == (True, "bound")


def test_answered_without_citation_is_a_miss() -> None:
    ok, kind, _ = battery.score_g1({"status": "ANSWERED", "supporting_evidence_ids": []}, {})
    assert (ok, kind) == (False, "miss")


def test_answered_citing_unreturned_row_is_a_miss() -> None:
    ok, kind, _ = battery.score_g1(
        {"status": "ANSWERED", "supporting_evidence_ids": [EID]}, _evidence("other")
    )
    assert (ok, kind) == (False, "miss")


def test_hypothesis_link_alone_does_not_bind() -> None:
    ok, kind, _ = battery.score_g1(
        {
            "status": "ROOT_CAUSE_LIKELY",
            "supporting_evidence_ids": [],
            "supporting_hypothesis_ids": ["h1"],
        },
        _evidence(EID),
    )
    assert (ok, kind) == (False, "miss")


def test_abstention_with_next_fetch_is_a_handoff() -> None:
    ok, kind, _ = battery.score_g1(
        {
            "status": "INSUFFICIENT_EVIDENCE",
            "handoff": {
                "state": "FRONTIER_EXHAUSTED",
                "next_fetch": "GET_CODE A.java 10-40",
                "price": 1.0,
            },
        },
        {},
    )
    assert (ok, kind) == (True, "handoff")


def test_abstention_with_eliminated_rival_is_a_handoff() -> None:
    ok, kind, _ = battery.score_g1(
        {"status": "NO_ROOT_CAUSE_FOUND", "eliminated_rivals": [{"statement": "x"}]}, {}
    )
    assert (ok, kind) == (True, "handoff")


def test_generic_template_next_fetch_is_a_miss() -> None:
    # Real wording observed from the handoff builder: names no file or action.
    for text in (
        "Bind supporting evidence and a cited causal chain to a candidate hypothesis, then re-verify.",
        "Narrow or re-scope the empty-search proof to the claimed scope, then re-verify.",
        "Resolve 'claim citations kept zero bound rows (evaporated); INCONCLUSIVE pending'",
    ):
        ok, kind, detail = battery.score_g1(
            {
                "status": "INSUFFICIENT_EVIDENCE",
                "handoff": {"state": "FRONTIER_EXHAUSTED", "next_fetch": text},
            },
            {},
        )
        assert (ok, kind) == (False, "miss"), text
        assert "generic" in detail


def test_next_fetch_naming_a_path_or_action_is_concrete() -> None:
    for text in (
        "Read service/src/main/java/com/sample/OrderFilter.java lines 40-80",
        "SEARCH_CODE for SampleOrderFilter",
        "Open OrderFilter.java",
    ):
        ok, kind, _ = battery.score_g1(
            {"status": "INSUFFICIENT_EVIDENCE", "handoff": {"next_fetch": text, "price": 1.0}}, {}
        )
        assert (ok, kind) == (True, "handoff"), text


def test_empty_handoff_is_a_miss_and_the_builder_emits_no_filler() -> None:
    from investigation_agent_platform.domain.finding.models import (
        ConclusionStatus,
        classify_terminal_handoff,
    )

    handoff = classify_terminal_handoff(
        status=ConclusionStatus.INSUFFICIENT_EVIDENCE,
        reason="insufficient",
        next_check=None,
        limitations=[],
        ranked_alternatives=[],
        eliminated_rivals=[],
    )
    assert handoff.recommended_next_steps == [], "the generic filler sentence is gone"
    assert handoff.next_fetch is None
    ok, kind, _ = battery.score_g1(
        {
            "status": "INSUFFICIENT_EVIDENCE",
            "handoff": handoff.model_dump(mode="json"),
            "recommended_next_steps": handoff.recommended_next_steps,
        },
        {},
    )
    assert (ok, kind) == (False, "miss")


def test_state_label_and_na_target_do_not_count() -> None:
    ok, kind, _ = battery.score_g1(
        {
            "status": "INSUFFICIENT_EVIDENCE",
            "handoff": {"state": "BUDGET_DEFERRED", "next_fetch": "n/a"},
        },
        {},
    )
    assert (ok, kind) == (False, "miss")


def test_missing_conclusion_is_a_miss() -> None:
    assert battery.score_g1(None, None)[:2] == (False, "miss")


def test_summary_rate_and_kinds() -> None:
    summary = battery.g1_summary(
        [("L1", True, "bound", ""), ("L2", True, "handoff", ""), ("L3", False, "miss", "")]
    )
    assert summary["satisfied"] == 2 and summary["total"] == 3
    assert summary["kinds"] == {"bound": 1, "handoff": 1, "miss": 1}
    assert abs(summary["rate"] - 2 / 3) < 1e-9
    assert battery.g1_summary([])["rate"] == 0.0


def test_stamp_sql_is_tenant_scoped_and_only_touches_created_rows() -> None:
    sql = battery.CLAIM_BINDING_STAMP_SQL
    assert "tenant_id = $3" in sql
    assert "status = 'CREATED'" in sql
    assert "claim_binding_version" in sql


def test_stamp_choices_come_from_the_engine_not_a_copy() -> None:
    from investigation_agent_platform.application.investigation.verification import (
        KNOWN_CLAIM_BINDING_VERSIONS,
    )

    assert battery.known_claim_binding_versions() == sorted(KNOWN_CLAIM_BINDING_VERSIONS)
    assert "cbv3" in battery.known_claim_binding_versions()


def test_concrete_but_unpriced_next_fetch_is_a_miss() -> None:
    for price in (None, 0, 0.0, -1, "1", True, float("nan")):
        handoff: dict = {"state": "FRONTIER_EXHAUSTED", "next_fetch": "GET_CODE A.java"}
        if price is not None:
            handoff["price"] = price
        ok, kind, detail = battery.score_g1(
            {"status": "INSUFFICIENT_EVIDENCE", "handoff": handoff}, {}
        )
        assert (ok, kind) == (False, "miss"), price
        assert "unpriced" in detail


def test_price_is_reported_in_the_detail() -> None:
    ok, _, detail = battery.score_g1(
        {
            "status": "INSUFFICIENT_EVIDENCE",
            "handoff": {"next_fetch": "GET_CODE A.java then SEARCH_CODE Foo", "price": 2.0},
        },
        {},
    )
    assert ok and "price 2" in detail


def test_builder_output_with_a_gap_target_satisfies_g1_end_to_end() -> None:
    from investigation_agent_platform.domain.finding.models import classify_terminal_handoff

    handoff = classify_terminal_handoff(
        status="INSUFFICIENT_EVIDENCE",
        reason="insufficient",
        next_check="Narrow or re-scope the empty-search proof to the claimed scope, then re-verify.",
        gap_fetch="SEARCH_CODE Foo within svc/",
    )
    ok, kind, detail = battery.score_g1(
        {"status": "INSUFFICIENT_EVIDENCE", "handoff": handoff.model_dump(mode="json")}, {}
    )
    assert (ok, kind) == (True, "handoff"), detail


def test_builder_output_from_template_prose_alone_still_misses_g1() -> None:
    from investigation_agent_platform.domain.finding.models import classify_terminal_handoff

    handoff = classify_terminal_handoff(
        status="INSUFFICIENT_EVIDENCE",
        reason="insufficient",
        next_check="Bind a causal chain with cited evidence for each link, then re-verify.",
    )
    ok, kind, _ = battery.score_g1(
        {"status": "INSUFFICIENT_EVIDENCE", "handoff": handoff.model_dump(mode="json")}, {}
    )
    assert (ok, kind) == (False, "miss")
