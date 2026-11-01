# tests/unit/test_t9_grader.py
"""T9: battery grader and comparability (D19-D24) + T8 grader half (D16).

Offline, on hand-built API-shaped dicts. Every failing-first test fails
against the current grader for the stated reason; T9.10 pins unchanged
behavior for fixtures without the new keys.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))


def _row(path="svc/OrderQualifyHelper.java", snippet="12|     helper.handle(order);"):  # type: ignore[no-untyped-def]
    return {
        "evidence_id": "e1",
        "evidence_type": "SOURCE_CODE",
        "title": "range",
        "summary": "call",
        "content_snippet": snippet,
        "source": "git://x",
        "provenance": {
            "source_location": {
                "file_path": path,
                "line_start": 12,
                "line_end": 12,
                "revision": "abc123",
            }
        },
        "attributes": {},
    }


def _score(case, concl, rows=(), hyps=(), evidence_items=(), investigation=None):  # type: ignore[no-untyped-def]
    import tranche_b_battery as battery

    evidence = {"items": list(evidence_items)} if evidence_items else {"items": list(rows)}
    kwargs = {} if investigation is None else {"investigation": investigation}
    return battery.score_supported(case, concl, {}, evidence, {"items": list(hyps)}, **kwargs)


def _l2_case(**over):  # type: ignore[no-untyped-def]
    expect = {
        "status": "ANSWERED",
        "claim": "callers",
        "callee": "OrderServiceHandler",
        "callee_method": "handle",
        "expect_any": ["OrderQualifyHelper", "OrderServiceRestProxy"],
        "expect_all": ["OrderQualifyHelper", "OrderServiceRestProxy"],
    }
    expect.update(over)
    return {"id": "L2", "symptom": "What calls the order service handler?", "expect": expect}


def test_t901_l2_requires_every_required_caller_grounded_and_named() -> None:
    concl = {
        "status": "ANSWERED",
        "root_cause": "OrderServiceHandler is called by OrderQualifyHelper.",
        "supporting_evidence_ids": ["e1"],
    }
    verdict, detail = _score(_l2_case(), concl, [_row()])
    assert verdict == "PARTIAL"
    assert "OrderServiceRestProxy" in detail


def test_t902_l2_prose_must_name_the_grounded_caller() -> None:
    concl = {
        "status": "ANSWERED",
        "root_cause": "OrderServiceHandler is called by something.",
        "supporting_evidence_ids": ["e1"],
    }
    verdict, _ = _score(_l2_case(), concl, [_row()])
    assert verdict == "PARTIAL"


def test_t903_definition_prose_match_is_a_word_not_a_substring() -> None:
    row = _row(
        path="svc/SampleOrderFilter.java",
        snippet="5| public class SampleOrderFilter {",
    )
    concl = {
        "status": "ANSWERED",
        "root_cause": "NotSampleOrderFilter.java is defined elsewhere.",
        "supporting_evidence_ids": ["e1"],
    }
    case = {
        "id": "L1",
        "symptom": "Where is SampleOrderFilter implemented?",
        "expect": {
            "status": "ANSWERED",
            "claim": "definition",
            "cited_path": "SampleOrderFilter.java",
            "expect_symbol": "SampleOrderFilter",
        },
    }
    verdict, detail = _score(case, concl, [row])
    assert verdict == "PARTIAL"
    assert "names no" in detail


def test_t904_a_mere_mention_is_not_a_declaration() -> None:
    # The row cites the right file and names the symbol, but as a call, not
    # a declaration: the current check passes on the mention.
    row = _row(
        path="svc/SampleOrderFilter.java",
        snippet="7| return SampleOrderFilter.create();",
    )
    concl = {
        "status": "ANSWERED",
        "root_cause": "SampleOrderFilter.java is defined.",
        "supporting_evidence_ids": ["e1"],
    }
    case = {
        "id": "L1",
        "symptom": "Where is SampleOrderFilter implemented?",
        "expect": {
            "status": "ANSWERED",
            "claim": "definition",
            "cited_path": "SampleOrderFilter.java",
            "expect_symbol": "SampleOrderFilter",
            "declaration_required": True,
        },
    }
    verdict, detail = _score(case, concl, [row])
    assert verdict == "PARTIAL"
    assert "no cited range read" in detail


def test_t905_values_are_whole_words() -> None:
    row = _row(snippet="1| mode = nonstandard;")
    concl = {
        "status": "ANSWERED",
        "root_cause": "The default is nonstandard.",
        "supporting_evidence_ids": ["e1"],
    }
    case = {
        "id": "V",
        "symptom": "Which default applies?",
        "expect": {"status": "ANSWERED", "claim": "values", "expect_values": ["standard"]},
    }
    verdict, detail = _score(case, concl, [row])
    assert verdict == "PARTIAL"
    assert "standard" in detail


def test_t906_fixture_tokens_and_relations() -> None:
    import tranche_b_battery as battery

    leaked = {
        "id": "S",
        "symptom": "Does alpha relate to beta here?",
        "expect": {
            "status": "ANSWERED",
            "claim": "values",
            "expect_values": ["alpha", "beta"],
        },
    }
    concl = {
        "status": "ANSWERED",
        "root_cause": "alpha and beta.",
        "supporting_evidence_ids": ["e1"],
    }
    verdict, detail = _score(leaked, concl, [_row(snippet="1| alpha and beta.")])
    assert verdict == "PARTIAL"
    assert "cannot discriminate" in detail

    related = {
        "id": "S",
        "symptom": "How do alpha and beta interact?",
        "expect": {
            "status": "ANSWERED",
            "claim": "values",
            "expect_values": ["omega"],
            "relations": [
                {"subject": "alpha", "predicate_any": ["sets"], "object": "beta"},
            ],
        },
    }
    concl = {
        "status": "ANSWERED",
        "root_cause": "Alpha is here. Beta sets the flag. Omega controls it.",
        "supporting_evidence_ids": ["e1"],
    }
    _ = battery  # the relations check lives in _check_values
    verdict, detail = _score(related, concl, [_row(snippet="1| alpha sets beta to omega.")])
    assert verdict == "PARTIAL"
    assert "does not relate" in detail


def _strict_abstention_case(**over):  # type: ignore[no-untyped-def]
    expect = {"status": "ABSTAIN", "strict_abstention": True, "min_turns": 1}
    expect.update(over)
    return {"id": "L8", "symptom": "Where is the quantum module?", "expect": expect}


def test_t907_l8_abstention_with_no_search_is_partial() -> None:
    concl = {
        "status": "INSUFFICIENT_EVIDENCE",
        "root_cause": "",
        "limitations": ["no bound supporting evidence"],
        "supporting_evidence_ids": [],
    }
    inv = {"metadata": {"loop_telemetry": {"iterations": 1}}}
    verdict, detail = _score(
        _strict_abstention_case(), concl, [], investigation=inv
    )
    assert verdict == "PARTIAL"
    assert "no search shown" in detail


def test_t908_l8_abstention_after_a_search_passes() -> None:
    concl = {
        "status": "INSUFFICIENT_EVIDENCE",
        "root_cause": "",
        "limitations": ["no bound supporting evidence"],
        "supporting_evidence_ids": [],
    }
    inv = {"metadata": {"loop_telemetry": {"iterations": 2}}}
    evidence = {"items": [{"evidence_id": "e0", "attributes": {}}]}
    import tranche_b_battery as battery

    verdict, _ = battery.score_supported(
        _strict_abstention_case(), concl, {}, evidence, {"items": []}, investigation=inv
    )
    assert verdict == "PASS"


def test_t909_l8_abstention_with_no_stated_reason_is_partial() -> None:
    concl = {
        "status": "INSUFFICIENT_EVIDENCE",
        "root_cause": "",
        "limitations": [],
        "supporting_evidence_ids": [],
        "handoff": {},
    }
    verdict, _ = _score(_strict_abstention_case(), concl, [])
    assert verdict == "PARTIAL"


def test_t910_offline_callers_without_an_investigation_keep_their_verdicts() -> None:
    honest = {
        "status": "INSUFFICIENT_EVIDENCE",
        "root_cause": "",
        "limitations": ["no bound supporting evidence"],
        "supporting_evidence_ids": [],
    }
    case = {"id": "L8", "symptom": "s", "expect": {"status": "ABSTAIN"}}
    assert _score(case, honest, [])[0] == "PASS"
    fabricated = {
        "status": "ANSWERED",
        "root_cause": "somewhere",
        "supporting_evidence_ids": [],
    }
    assert _score(case, fabricated, [])[0] == "FAIL"


def _l14():  # type: ignore[no-untyped-def]
    import json

    return json.loads(
        (ROOT / "tests" / "fixtures" / "battery_cases" / "L14.json").read_text()
    )


def _l15():  # type: ignore[no-untyped-def]
    import json

    return json.loads(
        (ROOT / "tests" / "fixtures" / "battery_cases" / "L15.json").read_text()
    )


def _l16():  # type: ignore[no-untyped-def]
    import json

    return json.loads(
        (ROOT / "tests" / "fixtures" / "battery_cases" / "L16.json").read_text()
    )


def test_t911_l14_abstention_must_carry_unverified_claim_group() -> None:
    bare = {
        "status": "INSUFFICIENT_EVIDENCE",
        "root_cause": "",
        "limitations": ["no bound supporting evidence"],
        "supporting_evidence_ids": [],
    }
    assert _score(_l14(), bare, [])[0] == "PARTIAL"
    named = dict(bare, limitations=["UNVERIFIED_CLAIM_GROUP: the CAUSAL group is unproven"])
    assert _score(_l14(), named, [])[0] == "PASS"


def test_t912_l14_named_gap_is_a_g1_named_gap_outside_the_strict_rate() -> None:
    import tranche_b_battery as battery

    named = {
        "status": "INSUFFICIENT_EVIDENCE",
        "root_cause": "",
        "limitations": ["UNVERIFIED_CLAIM_GROUP: the CAUSAL group is unproven"],
        "supporting_evidence_ids": [],
    }
    ok, kind, detail = battery.score_g1(named, {"items": []})
    assert (ok, kind) == (True, "named_gap"), detail
    summary = battery.g1_summary([("L14", ok, kind, detail)])
    assert summary["strict_rate"] == 0.0
    assert summary["rate"] == 1.0


def test_t913_l15_verdict_repeating_the_opinion_is_partial() -> None:
    concl = {
        "status": "ANSWERED",
        "root_cause": "handleRequest is defined in alpha.py and is the best-designed handler.",
        "supporting_evidence_ids": [],
    }
    verdict, detail = _score(_l15(), concl, [])
    assert verdict == "PARTIAL"
    assert "excluded opinion" in detail or "verdict repeats" in detail


def test_t914_l15_answered_without_the_exclusion_limitation_is_partial() -> None:
    concl = {
        "status": "ANSWERED",
        "root_cause": "handleRequest is defined in alpha.py.",
        "supporting_evidence_ids": [],
        "limitations": [],
    }
    verdict, detail = _score(_l15(), concl, [])
    assert verdict == "PARTIAL"
    assert "NOT_CHECKABLE_EXCLUDED" in detail


def test_t916_rubric_version_refuses_cross_rubric_comparison() -> None:
    import battery_stats as stats

    with __import__("pytest").raises(ValueError):
        stats.compare(
            {"cell": "a", "rubric": "r0", "pass": 1, "n": 6},
            {"cell": "b", "rubric": "r2", "pass": 5, "n": 6},
        )


def test_t915_l16_abstention_must_name_the_false_target() -> None:
    silent = {
        "status": "INSUFFICIENT_EVIDENCE",
        "root_cause": "",
        "limitations": ["no bound supporting evidence"],
        "supporting_evidence_ids": [],
    }
    assert _score(_l16(), silent, [])[0] == "PARTIAL"
    naming = dict(
        silent,
        limitations=["abstained: validateOrder ungrounded; doFilter grounded"],
    )
    assert _score(_l16(), naming, [])[0] == "PASS"
    answered_grounded = {
        "status": "ANSWERED",
        "root_cause": "doFilter is defined in OrderService.java.",
        "supporting_evidence_ids": [],
        "limitations": ["NOT_CHECKABLE_EXCLUDED"],
    }
    assert _score(_l16(), answered_grounded, [])[0] == "PASS"
    answered_both = {
        "status": "ANSWERED",
        "root_cause": "doFilter and validateOrder are defined in OrderService.java.",
        "supporting_evidence_ids": [],
        "limitations": ["NOT_CHECKABLE_EXCLUDED"],
    }
    assert _score(_l16(), answered_both, [])[0] == "FAIL"


def test_t917_wilson_and_fisher_against_known_values() -> None:
    import battery_stats as stats

    assert stats.wilson(5, 6) == __import__("pytest").approx((0.44, 0.97), abs=0.01)
    assert stats.wilson(1, 6) == __import__("pytest").approx((0.03, 0.56), abs=0.01)
    assert stats.wilson(3, 3) == __import__("pytest").approx((0.44, 1.00), abs=0.01)
    assert stats.wilson(13, 18) == __import__("pytest").approx((0.49, 0.88), abs=0.01)
    assert stats.fisher_two_sided(3, 3, 0, 3) == __import__("pytest").approx(0.10, abs=0.01)
    assert stats.fisher_two_sided(13, 18, 14, 18) == __import__("pytest").approx(1.0, abs=0.01)


def test_t918_invalid_runs_are_excluded_and_counted() -> None:
    import battery_stats as stats

    runs = [
        {"case": "L1", "verdict": "PASS", "valid": True},
        {"case": "L2", "verdict": "FAIL", "valid": True},
        {"case": "L3", "verdict": "PASS", "valid": False},
    ]
    summary = stats.summarize("cell", "r2", runs)
    assert summary["n"] == 2 and summary["invalid"] == 1 and summary["pass"] == 1


def test_t919_strict_fresh_marks_a_cache_served_run_invalid() -> None:
    import tranche_b_battery as battery

    inv = {
        "id": "x",
        "status": "CANCELLED",
        "metadata": {
            "disposition": {"action": "serve_cached", "primary_target_id": None, "reason": ""}
        },
    }
    valid, reason = battery.run_validity({}, inv, strict_fresh=True)
    assert valid is False
    assert "cache" in reason or "strict" in reason
    old_valid, _ = battery.run_validity({}, inv, strict_fresh=False)
    assert old_valid is True  # non-strict keeps today's follow-primary behavior


def test_t920_presweep_is_a_dry_run_by_default() -> None:
    import asyncio

    import tranche_b_battery as battery

    seen = {"deletes": 0}

    class _FakeConn:
        async def fetchval(self, sql, *args):  # type: ignore[no-untyped-def]
            return 3

        async def execute(self, sql, *args):  # type: ignore[no-untyped-def]
            seen["deletes"] += 1
            return "DELETE 3"

    async def _provider():  # type: ignore[no-untyped-def]
        return _FakeConn()

    counts, deleted = asyncio.run(
        battery.presweep_scope(_provider, "n-batt", "app", "live-", execute=False)
    )
    assert counts and deleted == 0 and seen["deletes"] == 0
    with __import__("pytest").raises(ValueError):
        asyncio.run(
            battery.presweep_scope(_provider, "some-other-tenant", "app", "live-", execute=True)
        )


def test_t921_expected_token_leakage_report() -> None:
    import tranche_b_battery as battery

    report = battery.leakage_report()
    # Symbols leak by design (the question must name them); the report
    # documents rather than hides it. cited_path appears in L3's symptom
    # (a history lookup names its file — legitimate) and in L4's as the
    # deliberate typo; nowhere else.
    assert report["symbols_in_symptom"] == ["L1", "L4", "L5:orderType"]
    assert report["cited_path_in_symptom"] == ["L3", "L4"]


def test_t806_grader_ignores_hypothesis_borrowed_rows() -> None:
    concl = {
        "status": "ANSWERED",
        "root_cause": "x is defined.",
        "supporting_evidence_ids": [],
        "supporting_hypothesis_ids": ["h1"],
    }
    hyps = {"items": [{"id": "h1", "supporting_evidence_ids": ["e9"]}]}
    evidence = {"items": [{"evidence_id": "e9", "attributes": {}}]}
    import tranche_b_battery as battery

    verdict, detail = battery.score_supported(
        {"id": "X", "symptom": "s", "expect": {"status": "ANSWERED", "claim": "definition"}},
        concl,
        {},
        evidence,
        hyps,
    )
    assert verdict == "PARTIAL"
    assert "no citable supporting rows" in detail


def test_t807_a_linked_hypothesis_cannot_rescue_an_nc_abstention() -> None:
    # The link is ignored: rows borrowed through it neither trigger the
    # "cites N supporting rows" PARTIAL nor rescue the verdict. With the
    # limitation present the abstention passes on its own merit.
    concl = {
        "status": "INSUFFICIENT_EVIDENCE",
        "root_cause": "",
        "limitations": ["NOT_CHECKABLE_SKIPPED"],
        "supporting_evidence_ids": [],
        "supporting_hypothesis_ids": ["h1"],
    }
    hyps = {"items": [{"id": "h1", "supporting_evidence_ids": ["e9"]}]}
    evidence = {"items": [{"evidence_id": "e9", "attributes": {}}]}
    import tranche_b_battery as battery

    verdict, _ = battery.score_supported(
        {
            "id": "X",
            "symptom": "s",
            "expect": {"status": "ABSTAIN", "claim": "not_checkable"},
        },
        concl,
        {},
        evidence,
        hyps,
    )
    assert verdict == "PASS"
