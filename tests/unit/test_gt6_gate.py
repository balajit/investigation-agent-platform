# tests/unit/test_gt6_gate.py
"""G-T6 harness code, offline (D25-D29).

GT6.07-09 are the live injected-claim runs (require owner approval; run
against the live stack, not here). GT6.11/12 negatives are run by hand.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))


def _healthy(**over):  # type: ignore[no-untyped-def]
    base = {
        "expect_sha": "abc123",
        "expect_loop_version": "lv13",
        "api_cwd_sha": "abc123",
        "worker_cwd_sha": "abc123",
        "api_dirty": False,
        "worker_dirty": False,
        "health_ready": True,
        "profile": {"status": "ACTIVE", "allowed_capabilities": ["SEARCH_CODE"]},
        "tenant_investigation_count": 0,
        "canary_loop_version": "lv13",
        "pin_stamp_rows": 1,
        "canary_verdict": "PASS",
    }
    base.update(over)
    return base


def test_gt601_preflight_passes_on_a_healthy_probe() -> None:
    import gate_preflight as gp

    checks = gp.evaluate(**_healthy())
    assert len(checks) == 9
    assert gp.main_ok(checks)


def test_gt602_preflight_fails_on_each_defect() -> None:
    import gate_preflight as gp

    cases = [
        {"api_cwd_sha": "deadbeef"},
        {"api_dirty": True},
        {"worker_cwd_sha": "deadbeef"},
        {"worker_dirty": True},
        {"health_ready": False},
        {"profile": {"status": "DRAFT", "allowed_capabilities": ["SEARCH_CODE"]}},
        {"profile": {"status": "ACTIVE", "allowed_capabilities": []}},
        {"tenant_investigation_count": 3},
        {"canary_loop_version": "lv7"},
        {"pin_stamp_rows": 0},
        {"canary_verdict": "FAIL"},
    ]
    assert len(cases) == 11
    for defect in cases:
        checks = gp.evaluate(**_healthy(**defect))
        assert not gp.main_ok(checks), defect
        assert sum(1 for c in checks if not c.ok) >= 1


def test_gt604_cells_are_validated_before_analysis(tmp_path) -> None:
    import json

    import battery_stats as stats

    good = {
        "case": "L1", "verdict": "PASS", "valid": True, "rubric": "r2",
        "git_sha": "abc123", "pin": "cbv1", "cell": "after",
    }
    bad = [
        dict(good, rubric="r0", case="L2"),
        dict(good, git_sha="", case="L3"),
        dict(good, pin="cbv2", case="L4"),
    ]
    path = tmp_path / "runs.json"
    path.write_text(json.dumps([good] + bad), encoding="utf-8")
    records, rejections = stats.load_records(str(path), expect_rubric="r2", expect_sha="abc123", expect_pin="cbv1")
    assert [r["case"] for r in records] == ["L1"]
    assert len(rejections) == 3


def test_gt605_pin_stamp_mismatch_marks_invalid() -> None:
    import tranche_b_battery as battery

    pins = {"L1": ("gid", "cbv1")}
    assert battery.check_pin(pins, "L1", "cbv3") != ""
    assert battery.check_pin(pins, "L1", "cbv1") == ""
    assert battery.check_pin({}, "L1", "cbv3") == ""


def test_gt606_decision_rule_flags_a_25_point_drop() -> None:
    import battery_stats as stats

    def cell(passes: int, total: int = 18):  # type: ignore[no-untyped-def]
        runs = ([{"case": f"c{i}", "verdict": "PASS", "valid": True} for i in range(passes)]
                + [{"case": f"x{i}", "verdict": "FAIL", "valid": True} for i in range(total - passes)])
        return stats.summarize("cell", "r2", runs)

    flagged, _ = stats.flags_regression(cell(15), cell(10))
    assert flagged
    not_flagged, _ = stats.flags_regression(cell(15), cell(13))
    assert not not_flagged


def test_gt603_strict_fresh_refuses_cache_served_runs() -> None:
    import tranche_b_battery as battery

    inv = {
        "id": "x",
        "status": "CANCELLED",
        "metadata": {"disposition": {"action": "serve_cached"}},
    }
    assert battery.should_follow_primary(True, inv) is False
    assert battery.should_follow_primary(False, inv) is True


def test_gt610_plan_enumerates_cells_and_repeats_reproducibly() -> None:
    import gate_inject_claims as gic

    a = gic.plan_cells(7)
    b = gic.plan_cells(7)
    assert a == b
    assert len(a) == 4 * 3
    tenants = [c["tenant"] for c in a]
    assert len(set(tenants)) == len(tenants)
    assert {c["pin"] for c in a} == {"cbv1", "cbv3"}
    assert {c["tree"] for c in a} == {"230d34b", "d7a3969"}
    assert gic.plan_cells(8) != a


def test_binds_and_score_assessments() -> None:
    import gate_inject_claims as gic

    group = {"target": {"symbol": "doFilter", "file_path": "f.py"}, "grounded": True}
    assert gic.binds({"claim_target": {"symbol": "doFilter", "file_path": "f.py"}, "evidence_id": "e1"}, group)
    assert not gic.binds({"claim_target": {"symbol": "doFilter", "file_path": "f.py"}, "evidence_id": None}, group)
    assert not gic.binds({"claim_target": {"symbol": "other", "file_path": "f.py"}, "evidence_id": "e1"}, group)
    case = {"expect": {"claim_groups": [group, {"target": {"symbol": "v", "file_path": "f.py"}, "grounded": False}]}}
    assert gic.score_assessments(case, [{"claim_target": {"symbol": "doFilter", "file_path": "f.py"}, "evidence_id": "e1"}]) == (True, "")
    assert gic.score_assessments(case, [])[0] is False
