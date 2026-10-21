# tests/unit/test_battery_scoring.py
"""Phase 4.1: the battery grades conclusions, not corpora.

The grader is pure on API-shaped dicts (conclusion/hypotheses/evidence
payloads mirror the router dumps), so every rubric dimension is provable
offline. A right file with a wrong citation must fail; a right file with
no range must fail a behavior case.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import tranche_b_battery as battery


def _row(
    eid,
    etype="SOURCE_CODE",
    file_path="src/SampleOrderFilter.java",
    line_start=72,
    line_end=80,
    revision="abc123",
    snippet="72| public class SampleOrderFilter",
    attributes=None,
):
    row_attributes = dict(attributes or {})
    location = {"file_path": file_path, "revision": revision}
    if line_start is not None:
        location["line_start"] = line_start
    if line_end is not None:
        location["line_end"] = line_end
    return {
        "evidence_id": eid,
        "evidence_type": etype,
        "title": f"Source: {file_path}",
        "summary": row_attributes.get("summary", f"snippet from {file_path}"),
        "content_snippet": snippet,
        "source": f"git://r/{file_path}@{revision}",
        "attributes": row_attributes,
        "provenance": {"source_location": location},
    }


def _conclusion(
    status="ANSWERED",
    root="SampleOrderFilter is implemented in src/SampleOrderFilter.java",
    supporting=None,
    hyps=None,
    limitations=None,
):
    return {
        "status": status,
        "root_cause": root,
        "supporting_evidence_ids": list(supporting or []),
        "supporting_hypothesis_ids": list(hyps or []),
        "limitations": list(limitations or []),
    }


def _hyps(items):
    return {"items": items}


def _hyp(hid, statement, supporting):
    return {"id": hid, "statement": statement, "supporting_evidence_ids": list(supporting)}


def _ev(items):
    return {"items": items}


def _case(**overrides):
    base = {
        "id": "T",
        "symptom": "t",
        "context": "t",
        "live": False,
        "expect": {
            "status": "ANSWERED",
            "claim": "definition",
            "cited_path": "SampleOrderFilter.java",
            "expect_symbol": "SampleOrderFilter",
        },
    }
    base.update(overrides)
    return base


class TestFixtureIntegrity:
    def test_all_fixtures_load_with_required_shape(self):
        cases = battery.load_cases()
        assert {c["id"] for c in cases} == {
            "L1", "L2", "L3", "L4", "L5", "L8", "L9", "L10", "L11", "L12",
        }
        live = {c["id"] for c in cases if c.get("live", True)}
        assert live == {"L1", "L2", "L3", "L4", "L5", "L8"}
        for case in cases:
            assert case["expect"]["status"] in ("ANSWERED", "ABSTAIN")

    def test_fixture_files_are_valid_json(self):
        for path in sorted((ROOT / "tests" / "fixtures" / "battery_cases").glob("*.json")):
            json.loads(path.read_text())


class TestDefinitionGrading:
    def test_supported_definition_passes(self):
        row = _row("e1")
        verdict, _ = battery.score_supported(
            _case(), _conclusion(supporting=["e1"]), {}, _ev([row]), _hyps([])
        )
        assert verdict == "PASS"

    def test_right_file_wrong_citation_is_partial(self):
        # Root_cause names the file; the only cited row is another file.
        other = _row("e1", file_path="src/Other.java", snippet="1| class Other")
        verdict, detail = battery.score_supported(
            _case(), _conclusion(supporting=["e1"]), {}, _ev([other]), _hyps([])
        )
        assert verdict == "PARTIAL"
        assert "SampleOrderFilter.java" in detail

    def test_range_less_citation_is_partial(self):
        hit = _row("e1", line_start=None, line_end=None, snippet="match SampleOrderFilter")
        verdict, _ = battery.score_supported(
            _case(), _conclusion(supporting=["e1"]), {}, _ev([hit]), _hyps([])
        )
        assert verdict == "PARTIAL"

    def test_symbol_missing_from_window_is_partial(self):
        row = _row("e1", snippet="72| public class SomethingElse")
        verdict, _ = battery.score_supported(
            _case(), _conclusion(supporting=["e1"]), {}, _ev([row]), _hyps([])
        )
        assert verdict == "PARTIAL"

    def test_answered_with_no_supporting_rows_is_partial(self):
        verdict, _ = battery.score_supported(
            _case(), _conclusion(supporting=[]), {}, _ev([]), _hyps([])
        )
        assert verdict == "PARTIAL"

    def test_wrong_status_fails(self):
        row = _row("e1")
        verdict, _ = battery.score_supported(
            _case(),
            _conclusion(status="INSUFFICIENT_EVIDENCE", root="", supporting=["e1"]),
            {},
            _ev([row]),
            _hyps([]),
        )
        assert verdict == "FAIL"

    def test_unknown_claim_fails(self):
        case = _case(expect={"status": "ANSWERED", "claim": "telepathy"})
        verdict, detail = battery.score_supported(
            case, _conclusion(supporting=["e1"]), {}, _ev([_row("e1")]), _hyps([])
        )
        assert verdict == "FAIL"
        assert "telepathy" in detail


class TestCallerGrading:
    def _caller_case(self):
        return _case(
            expect={
                "status": "ANSWERED",
                "claim": "callers",
                "callee": "OrderServiceHandler",
                "callee_method": "handle",
                "expect_any": ["OrderQualifyHelper", "OrderServiceRestProxy"],
            }
        )

    def test_shown_call_site_passes(self):
        row = _row(
            "e1",
            file_path="src/OrderQualifyHelper.java",
            snippet="10| public String qualify() {\n11|     return handler.handle(orderId);",
        )
        root = "OrderQualifyHelper calls OrderServiceHandler.handle from the qualify branch."
        verdict, _ = battery.score_supported(
            self._caller_case(), _conclusion(root=root, supporting=["e1"]), {}, _ev([row]), _hyps([])
        )
        assert verdict == "PASS"

    def test_search_hit_without_call_is_partial(self):
        row = _row(
            "e1",
            file_path="src/OrderQualifyHelper.java",
            snippet="1| class OrderQualifyHelper",
        )
        verdict, _ = battery.score_supported(
            self._caller_case(),
            _conclusion(root="OrderQualifyHelper calls OrderServiceHandler.", supporting=["e1"]),
            {},
            _ev([row]),
            _hyps([]),
        )
        assert verdict == "PARTIAL"

    def test_any_expected_caller_with_its_own_callsite_is_enough(self):
        row = _row(
            "e1",
            file_path="src/OrderServiceRestProxy.java",
            snippet="20| return handler.handle(orderId);",
        )
        verdict, detail = battery.score_supported(
            self._caller_case(),
            _conclusion(
                root="OrderServiceRestProxy calls OrderServiceHandler.handle.",
                supporting=["e1"],
            ),
            {},
            _ev([row]),
            _hyps([]),
        )
        assert verdict == "PASS", detail


class TestValuesHistoryAbstention:
    def test_grounded_values_pass(self):
        row = _row(
            "e1",
            snippet="5| if orderType is None:\n6|     orderType = STANDARD  # LegacyCodeMapper default",
        )
        root = "A null orderType falls back to STANDARD per the LegacyCodeMapper default branch."
        case = _case(
            expect={"status": "ANSWERED", "claim": "values",
                    "expect_values": ["STANDARD", "orderType", "LegacyCodeMapper"]}
        )
        verdict, _ = battery.score_supported(
            case, _conclusion(root=root, supporting=["e1"]), {}, _ev([row]), _hyps([])
        )
        assert verdict == "PASS"

    def test_prose_only_value_is_partial(self):
        row = _row("e1", snippet="1| unrelated")
        case = _case(
            expect={"status": "ANSWERED", "claim": "values", "expect_values": ["STANDARD"]}
        )
        verdict, _ = battery.score_supported(
            case,
            _conclusion(root="The default is STANDARD.", supporting=["e1"]),
            {},
            _ev([row]),
            _hyps([]),
        )
        assert verdict == "PARTIAL"

    def test_value_in_cited_path_counts_as_shown(self):
        # Live L5 shape: the 6-19 method window never names LegacyCodeMapper
        # in prose, but the cited row's path is the mapper file. Row identity
        # (same helper as the caller check) counts — snippet-only matching
        # would PARTIAL a fully evidenced answer.
        row = _row(
            "e1",
            file_path="service/src/main/java/com/sample/orders/legacy/LegacyCodeMapper.java",
            snippet="8|             return \"STANDARD\";",
        )
        case = _case(
            expect={"status": "ANSWERED", "claim": "values",
                    "expect_values": ["STANDARD", "LegacyCodeMapper"]}
        )
        root = "When orderType is null, LegacyCodeMapper.normalize returns 'STANDARD'."
        verdict, _ = battery.score_supported(
            case, _conclusion(root=root, supporting=["e1"]), {}, _ev([row]), _hyps([])
        )
        assert verdict == "PASS"

    def test_value_in_neither_prose_nor_row_is_partial(self):
        row = _row(
            "e1",
            file_path="service/src/main/java/com/sample/orders/legacy/OtherMapper.java",
            snippet="8|             return \"STANDARD\";",
        )
        case = _case(
            expect={"status": "ANSWERED", "claim": "values",
                    "expect_values": ["STANDARD", "LegacyCodeMapper"]}
        )
        root = "When orderType is null, LegacyCodeMapper.normalize returns 'STANDARD'."
        verdict, detail = battery.score_supported(
            case, _conclusion(root=root, supporting=["e1"]), {}, _ev([row]), _hyps([])
        )
        assert verdict == "PARTIAL"
        assert "LegacyCodeMapper" in detail

    def test_commit_row_passes_history(self):
        row = _row(
            "e1",
            etype="COMMIT_HISTORY",
            file_path="OrderServiceImpl.java",
            snippet="abc1234 tune pool",
            attributes={"summary": "tune pool"},
        )
        case = _case(
            expect={"status": "ANSWERED", "claim": "history", "cited_path": "OrderServiceImpl.java"}
        )
        root = "OrderServiceImpl.java changed in abc1234 to tune the pool."
        verdict, _ = battery.score_supported(
            case, _conclusion(root=root, supporting=["e1"]), {}, _ev([row]), _hyps([])
        )
        assert verdict == "PASS"

    def test_honest_abstention_passes(self):
        case = _case(expect={"status": "ABSTAIN"})
        verdict, _ = battery.score_supported(
            case, _conclusion(status="NO_ROOT_CAUSE_FOUND", root=""), {}, _ev([]), _hyps([])
        )
        assert verdict == "PASS"

    def test_fabricated_answer_fails(self):
        case = _case(expect={"status": "ABSTAIN"})
        verdict, _ = battery.score_supported(
            case, _conclusion(status="ANSWERED", root="quantum module"), {}, _ev([]), _hyps([])
        )
        assert verdict == "FAIL"


class TestOfflineDimensions:
    def test_rival_exemption_accepted(self):
        row = _row("e1", file_path="src/a/OrderServiceImpl.java",
                   snippet="10| public boolean isEligible")
        hyps = _hyps([
            _hyp("h1", "isEligible in a/OrderServiceImpl handles null", ["e1"]),
            _hyp("h2", "isEligible in b/OrderServiceImpl handles null", []),
        ])
        case = _case(expect={
            "status": "ANSWERED", "claim": "definition",
            "cited_path": "OrderServiceImpl.java", "expect_symbol": "isEligible",
            "rival_untested_exempt": True,
        })
        root = "isEligible is defined in src/a/OrderServiceImpl.java."
        verdict, _ = battery.score_supported(
            case, _conclusion(root=root, supporting=["e1"], hyps=["h1"]), {}, _ev([row]), hyps
        )
        assert verdict == "PASS"

    def test_rival_demoted_location_fails_exemption(self):
        row = _row("e1", file_path="src/a/OrderServiceImpl.java",
                   snippet="10| public boolean isEligible")
        hyps = _hyps([
            _hyp("h1", "isEligible in a handles null", ["e1"]),
            _hyp("h2", "isEligible in b handles null", []),
        ])
        case = _case(expect={
            "status": "ANSWERED", "claim": "definition",
            "cited_path": "OrderServiceImpl.java", "expect_symbol": "isEligible",
            "rival_untested_exempt": True,
        })
        root = "isEligible is defined in src/a/OrderServiceImpl.java."
        concl = _conclusion(root=root, supporting=["e1"], hyps=["h1"],
                            limitations=["RIVAL_UNTESTED: 1 rival never evaluated"])
        verdict, detail = battery.score_supported(case, concl, {}, _ev([row]), hyps)
        assert verdict == "PARTIAL"
        assert "exemption" in detail

    def test_revision_match_passes(self):
        row = _row("e1", revision="abc123")
        case = _case(expect={
            "status": "ANSWERED", "claim": "definition",
            "cited_path": "SampleOrderFilter.java", "expect_symbol": "SampleOrderFilter",
            "incident_revision": "abc123",
        })
        verdict, _ = battery.score_supported(
            case,
            _conclusion(supporting=["e1"]), {}, _ev([row]), _hyps([]),
        )
        assert verdict == "PASS"

    def test_head_row_against_pinned_incident_is_partial(self):
        row = _row("e1", revision="deadbeef-head-sha")
        case = _case(expect={
            "status": "ANSWERED", "claim": "definition",
            "cited_path": "SampleOrderFilter.java", "expect_symbol": "SampleOrderFilter",
            "incident_revision": "abc123",
        })
        verdict, detail = battery.score_supported(
            case, _conclusion(supporting=["e1"]), {}, _ev([row]), _hyps([])
        )
        assert verdict == "PARTIAL"
        assert "abc123" in detail

    def test_failure_rows_must_stay_uncited(self):
        bad = _row("e1", snippet="timeout", attributes={"miss_reason": "timeout"})
        case = _case(expect={"status": "ABSTAIN", "claim": "no_failure_citation"})
        verdict, _ = battery.score_supported(
            case, _conclusion(status="INSUFFICIENT_EVIDENCE", root=""), {}, _ev([bad]), _hyps([])
        )
        assert verdict == "PASS"
        verdict, detail = battery.score_supported(
            case,
            _conclusion(status="INSUFFICIENT_EVIDENCE", root="", supporting=["e1"]),
            {},
            _ev([bad]),
            _hyps([]),
        )
        assert verdict == "PARTIAL"
        assert "timeout" in detail

    def test_scoped_absence_proof_passes(self):
        proof = _row(
            "e1", etype="DATABASE_STATE", file_path="", line_start=None, line_end=None,
            revision="", snippet="0 rows",
            attributes={"search_scope": "audit_log/order-9", "match_count": 0},
        )
        case = _case(expect={
            "status": "ANSWERED", "claim": "absence",
            "absent_subject": "audit", "scope": "audit_log/order-9",
        })
        root = "No audit row was written: scoped query over audit_log/order-9 returned zero rows."
        verdict, _ = battery.score_supported(
            case, _conclusion(root=root, supporting=["e1"]), {}, _ev([proof]), _hyps([])
        )
        assert verdict == "PASS"

    def test_empty_search_alone_is_partial(self):
        case = _case(expect={
            "status": "ANSWERED", "claim": "absence",
            "absent_subject": "audit", "scope": "audit_log/order-9",
        })
        verdict, _ = battery.score_supported(
            case,
            _conclusion(root="No audit rows found.", supporting=[]),
            {},
            _ev([]),
            _hyps([]),
        )
        assert verdict == "PARTIAL"


class TestCleanupScope:
    def test_battery_tenant_wipes_all(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        import clean_battery

        assert clean_battery.decide_scope("n-batt", False) == "all"

    def test_foreign_tenant_tagged_only(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        import clean_battery

        assert clean_battery.decide_scope("n-a", False) == "tagged"

    def test_force_overrides_guard(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        import clean_battery

        assert clean_battery.decide_scope("n-a", True) == "all"


class TestCachedPrimary:
    def test_follows_served_conclusion(self):
        import sys

        sys.path.insert(0, str(ROOT / "scripts"))
        import live_battery

        pid = "11111111-2222-3333-4444-555555555555"
        concl = {
            "status": "ANSWERED",
            "root_cause": "SampleOrderFilter is implemented in src/SampleOrderFilter.java",
            "supporting_evidence_ids": ["e1"],
            "supporting_hypothesis_ids": [],
            "limitations": [],
        }
        row = {
            "evidence_id": "e1",
            "evidence_type": "SOURCE_CODE",
            "title": "t",
            "summary": "s",
            "content_snippet": "72| public class SampleOrderFilter",
            "source": "git://r/f@abc",
            "attributes": {},
            "provenance": {
                "source_location": {
                    "file_path": "src/SampleOrderFilter.java",
                    "line_start": 72,
                    "line_end": 80,
                    "revision": "abc",
                }
            },
        }
        calls = []

        def _fake_api(base, tenant, method, path, body=None, timeout=30):
            calls.append(path)
            if path.endswith("/conclusion"):
                return 200, {"conclusion": concl}
            if path.endswith("/findings"):
                return 200, {}
            if "evidence" in path:
                return 200, {"items": [row]}
            if path.endswith("/hypotheses"):
                return 200, {"items": []}
            return 200, {
                "investigation": {
                    "metadata": {
                        "disposition": {
                            "action": "serve_cached",
                            "reason": f"duplicate of {pid}",
                        }
                    }
                }
            }

        live_battery.api = _fake_api
        try:
            out, note = live_battery._cached_primary("b", "t", "deadbeef")
        finally:
            import importlib

            importlib.reload(live_battery)
        assert out is not None
        got_pid, got_concl, _, _, _ = out
        assert got_pid == pid
        assert got_concl["status"] == "ANSWERED"

    def test_non_cached_returns_none(self):
        import sys

        sys.path.insert(0, str(ROOT / "scripts"))
        import live_battery

        def _fake_api(base, tenant, method, path, body=None, timeout=30):
            return 200, {"investigation": {"metadata": {"disposition": {"action": "fresh"}}}}

        live_battery.api = _fake_api
        try:
            out, note = live_battery._cached_primary("b", "t", "deadbeef")
            assert out is None
            assert "fresh" in note
        finally:
            import importlib

            importlib.reload(live_battery)

    def test_prefers_primary_target_id_over_reason_regex(self):
        import sys

        sys.path.insert(0, str(ROOT / "scripts"))
        import live_battery

        pid = "22222222-3333-4444-5555-666666666666"
        concl = {"status": "ANSWERED", "root_cause": "r", "supporting_evidence_ids": []}

        def _fake_api(base, tenant, method, path, body=None, timeout=30):
            if path.endswith("/conclusion"):
                return 200, {"conclusion": concl}
            if path.endswith("/findings"):
                return 200, {}
            if "evidence" in path:
                return 200, {"items": []}
            if path.endswith("/hypotheses"):
                return 200, {"items": []}
            # Structured primary id present; reason carries no uuid
            # (older shape had only the reason text).
            return 200, {
                "investigation": {
                    "metadata": {
                        "disposition": {
                            "action": "serve_cached",
                            "primary_target_id": pid,
                            "reason": "prior conclusion re-verified CONFIRMED",
                        }
                    }
                }
            }

        live_battery.api = _fake_api
        try:
            out, note = live_battery._cached_primary("b", "t", "deadbeef")
        finally:
            import importlib

            importlib.reload(live_battery)
        assert out is not None
        assert out[0] == pid
