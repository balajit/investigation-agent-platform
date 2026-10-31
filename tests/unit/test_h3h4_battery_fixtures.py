"""H3/H4-sensitive battery fixtures (L14-L16): load-only validation.

Nothing here executes a run. The fixtures are loaded through the same loader
the battery uses (``battery.load_cases``) and graded as pure functions on
hand-built conclusion dicts.

Where the current grader cannot score an intended assertion (review T9 gaps),
the intended assertion is written as a strict xfail: it documents the exact
failing grader call, and it flips to a hard failure the day the grader learns
to score it, so the fixture's ``known_grader_gaps`` note gets updated. The
grader itself is not touched here.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from investigation_agent_platform.domain.hypothesis.models import ClaimKind

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import tranche_b_battery as battery

NEW_IDS = ("L14", "L15", "L16")
FILTER_PATH = "service/src/main/java/com/sample/orders/filter/SampleOrderFilter.java"
HANDLER_PATH = "service/src/main/java/com/sample/orders/handler/OrderServiceHandler.java"


def _cases() -> dict[str, dict]:
    return {c["id"]: c for c in battery.load_cases()}


# --- hand-built grader inputs (API-shaped dicts, as in test_battery_scoring) -------


def _row(eid: str, path: str, snippet: str) -> dict:
    return {
        "evidence_id": eid,
        "evidence_type": "SOURCE_CODE",
        "title": path,
        "summary": "",
        "source": f"git://repo/{path}@abc",
        "content_snippet": snippet,
        "attributes": {"read_mode": "range"},
        "provenance": {
            "source_location": {
                "file_path": path,
                "line_start": 1,
                "line_end": 40,
                "revision": "abc",
            }
        },
    }


def _concl(
    status: str,
    root: str = "",
    supporting: tuple[str, ...] = (),
    limitations: tuple[str, ...] = (),
) -> dict:
    return {
        "status": status,
        "root_cause": root,
        "supporting_evidence_ids": list(supporting),
        "supporting_hypothesis_ids": [],
        "limitations": list(limitations),
        "handoff": {},
    }


def _score(case_id: str, concl: dict, rows: list[dict]) -> str:
    verdict, _ = battery.score_supported(
        _cases()[case_id], concl, {}, {"items": rows}, {"items": []}
    )
    return verdict


# --- fixtures load and are well formed ------------------------------------------------


@pytest.mark.parametrize("case_id", NEW_IDS)
def test_fixture_loads_through_the_battery_loader_and_is_offline_only(case_id: str) -> None:
    case = _cases()[case_id]
    assert case["live"] is False, "claim groups are injected; nothing here runs live"
    assert case["offline_reason"].strip()
    assert case["symptom"].strip() and case["context"].strip()
    assert case["expect"]["status"] in ("ANSWERED", "ABSTAIN")


def test_new_fixtures_are_not_scheduled_live() -> None:
    live = {c["id"] for c in battery.load_cases() if c.get("live", True)}
    assert live == {"L1", "L2", "L3", "L4", "L5", "L8"}


@pytest.mark.parametrize("case_id", NEW_IDS)
def test_claim_groups_use_real_claim_kinds_and_state_groundedness(case_id: str) -> None:
    groups = _cases()[case_id]["expect"]["claim_groups"]
    assert len(groups) >= 2, "an H3/H4 shape needs at least two claim groups"
    valid = {k.value for k in ClaimKind}
    for group in groups:
        assert group["kind"] in valid
        assert isinstance(group["grounded"], bool)
    assert any(not g["grounded"] for g in groups), "each shape carries an ungrounded group"
    assert any(g["grounded"] for g in groups), "and a grounded one"


def test_l14_is_mixed_kind_with_one_ungrounded_causal() -> None:
    groups = _cases()["L14"]["expect"]["claim_groups"]
    assert {g["kind"] for g in groups} == {"DEFINITION", "CAUSAL"}
    causal = next(g for g in groups if g["kind"] == "CAUSAL")
    assert causal["grounded"] is False and causal["statement"]


def test_l15_is_not_checkable_plus_checkable_and_ships_the_opinion_as_a_limitation() -> None:
    expect = _cases()["L15"]["expect"]
    assert {g["kind"] for g in expect["claim_groups"]} == {"DEFINITION", "NOT_CHECKABLE"}
    assert expect["status"] == "ANSWERED"
    assert any("NOT_CHECKABLE_EXCLUDED" in line for line in expect["intended"])
    assert any("root_cause" in line and "opinion" in line for line in expect["intended"])


def test_l16_is_a_multi_target_definition_with_exactly_one_false_target() -> None:
    groups = _cases()["L16"]["expect"]["claim_groups"]
    assert [g["kind"] for g in groups] == ["DEFINITION", "DEFINITION"]
    assert len({g["target"]["file_path"] for g in groups}) == 1, "same file"
    assert len({g["target"]["symbol"] for g in groups}) == 2, "different symbols"
    assert [g.get("false_target", False) for g in groups] == [False, True]
    assert [g["grounded"] for g in groups] == [True, False]


@pytest.mark.parametrize("case_id", NEW_IDS)
def test_every_known_gap_names_the_assertion_the_grader_and_its_behavior(case_id: str) -> None:
    gaps = _cases()[case_id]["expect"]["known_grader_gaps"]
    assert gaps
    ids = [g["id"] for g in gaps]
    assert len(ids) == len(set(ids))
    for gap in gaps:
        for key in ("id", "assertion", "grader", "current_behavior", "t9"):
            assert str(gap.get(key, "")).strip(), (case_id, gap.get("id"), key)


@pytest.mark.parametrize("case_id", NEW_IDS)
def test_fixtures_invent_no_tenant_profile_or_capability(case_id: str) -> None:
    # Profiles and capabilities are ops-owned (R4/R5); a fixture must not
    # carry or assume one.
    text = str(_cases()[case_id]).lower()
    for forbidden in ("tenant", "profile", "capabilit", "allowed_"):
        assert forbidden not in text, forbidden


@pytest.mark.parametrize("case_id", NEW_IDS)
def test_fixtures_use_only_the_synthetic_sample_checkout(case_id: str) -> None:
    text = str(_cases()[case_id]["expect"].get("claim_groups"))
    for path in {FILTER_PATH, HANDLER_PATH}:
        text = text.replace(path, "")
    assert "src/main/java" not in text, "only the two known sample paths may appear"


# --- what the grader DOES score correctly ------------------------------------------


def test_l14_a_combined_answer_is_caught() -> None:
    rows = [_row("e1", FILTER_PATH, "72| public class SampleOrderFilter")]
    concl = _concl(
        "ANSWERED",
        "SampleOrderFilter is in SampleOrderFilter.java and causes the order handler timeouts",
        ("e1",),
    )
    assert _score("L14", concl, rows) == "FAIL"


def test_l14_an_abstention_passes() -> None:
    assert _score("L14", _concl("INSUFFICIENT_EVIDENCE"), []) == "PASS"


def test_l16_a_combined_answer_is_caught() -> None:
    rows = [_row("e1", FILTER_PATH, "81| public void doFilter(")]
    concl = _concl("ANSWERED", "doFilter and validateOrder are in SampleOrderFilter.java", ("e1",))
    assert _score("L16", concl, rows) == "FAIL"


def test_l16_an_abstention_passes() -> None:
    assert _score("L16", _concl("INSUFFICIENT_EVIDENCE"), []) == "PASS"


def test_l15_the_checkable_half_is_graded() -> None:
    rows = [_row("e1", HANDLER_PATH, "98| public class OrderServiceHandler {")]
    ok = _concl("ANSWERED", "OrderServiceHandler is implemented in OrderServiceHandler.java.", ("e1",))
    assert _score("L15", ok, rows) == "PASS"
    wrong_file = _row("e1", FILTER_PATH, "72| public class SampleOrderFilter")
    assert _score("L15", ok, [wrong_file]) == "PARTIAL"
    assert _score("L15", _concl("INSUFFICIENT_EVIDENCE"), []) == "FAIL"


# --- intended assertions the grader cannot score yet (T9): strict xfail ----------------

_GAP = "T9 grader gap, see the fixture's known_grader_gaps; flips when the grader scores it"


@pytest.mark.xfail(strict=True, reason=_GAP)
def test_l14_abstention_must_name_the_unverified_claim_group() -> None:
    # mixed-kind-limitation-unchecked
    verdict = _score("L14", _concl("INSUFFICIENT_EVIDENCE"), [])  # no limitation at all
    assert verdict != "PASS"


@pytest.mark.xfail(strict=True, reason=_GAP)
def test_l14_abstention_with_its_limitation_is_a_g1_handoff() -> None:
    # mixed-kind-g1-miss
    concl = _concl("INSUFFICIENT_EVIDENCE", limitations=("UNVERIFIED_CLAIM_GROUP: CAUSAL",))
    ok, _, _ = battery.score_g1(concl, {"items": []})
    assert ok is True


@pytest.mark.xfail(strict=True, reason=_GAP)
def test_l15_opinion_in_the_verdict_must_not_pass() -> None:
    # nc-opinion-in-verdict-unchecked
    rows = [_row("e1", HANDLER_PATH, "98| public class OrderServiceHandler {")]
    concl = _concl(
        "ANSWERED",
        "OrderServiceHandler is implemented in OrderServiceHandler.java and is the "
        "best-designed handler in the service.",
        ("e1",),
        limitations=("NOT_CHECKABLE_EXCLUDED: 1 claim",),
    )
    assert _score("L15", concl, rows) != "PASS"


@pytest.mark.xfail(strict=True, reason=_GAP)
def test_l15_answered_without_the_exclusion_limitation_must_not_pass() -> None:
    # nc-limitation-unchecked
    rows = [_row("e1", HANDLER_PATH, "98| public class OrderServiceHandler {")]
    concl = _concl(
        "ANSWERED", "OrderServiceHandler is implemented in OrderServiceHandler.java.", ("e1",)
    )
    assert _score("L15", concl, rows) != "PASS"


@pytest.mark.xfail(strict=True, reason=_GAP)
def test_l16_abstention_must_name_the_false_target() -> None:
    # multi-target-limitation-unchecked
    assert _score("L16", _concl("INSUFFICIENT_EVIDENCE"), []) != "PASS"
