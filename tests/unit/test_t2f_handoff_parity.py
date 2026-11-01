# tests/unit/test_t2f_handoff_parity.py
"""T2F(a): live_battery has_handoff agrees with the G1 priced-handoff definition.

D30: one function (tranche_b_battery._concrete_handoff) so the two
instruments cannot disagree. The old proxy counted a state label or any
next-check prose as a handoff.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))


def _conclusion_row(**over: Any) -> dict:
    row = {
        "status": "INSUFFICIENT_EVIDENCE",
        "limitations": ["no bound supporting evidence"],
        "next_check": None,
        "recommended_next_steps": [],
        "handoff": {"state": "n/a", "version": "handoff-v1", "next_fetch": "n/a", "price": 0},
        "n_support": 0,
        "n_eliminated": 0,
        "eliminated_rivals": [],
    }
    row.update(over)
    return row


class _Conn:
    """Minimal asyncpg stand-in: inv row, no evidence, no hyps, canned conclusion."""

    def __init__(self, concl: dict):
        self._concl = concl

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        if "FROM investigations" in sql:
            return {
                "status": "COMPLETED",
                "started_at": None,
                "completed_at": None,
                "metadata_json": {},
            }
        if "FROM investigation_conclusions" in sql:
            return dict(self._concl)
        raise AssertionError(f"unexpected fetchrow: {sql[:60]}")

    async def fetch(self, sql: str, *args: Any) -> list:
        return []


def _analyze(concl: dict) -> dict:
    import asyncio

    import live_battery

    return asyncio.run(live_battery._analyze_one(_Conn(concl), "inv-1"))


def _priced_handoff() -> dict:
    return {
        "state": "BUDGET_DEFERRED",
        "version": "handoff-v1",
        "next_fetch": "SEARCH_CODE OrderServiceHandler",
        "price": 2.0,
    }


def test_t2f01_has_handoff_agrees_with_g1() -> None:
    priced = _analyze(_conclusion_row(handoff=_priced_handoff()))
    assert priced["has_handoff"] is True

    state_only = _analyze(
        _conclusion_row(
            handoff={
                "state": "FRONTIER_EXHAUSTED",
                "version": "handoff-v1",
                "next_fetch": "n/a",
                "price": 0,
            }
        )
    )
    assert state_only["has_handoff"] is False

    prose_only = _analyze(
        _conclusion_row(next_check="re-fetch the handler", recommended_next_steps=["try again"])
    )
    assert prose_only["has_handoff"] is False

    rivals = _analyze(
        _conclusion_row(eliminated_rivals=[{"rival": "x", "ruled_out_by": ["e1"]}])
    )
    assert rivals["has_handoff"] is True


def test_t2f02_live_battery_uses_the_shared_function() -> None:
    src = (ROOT / "scripts" / "live_battery.py").read_text(encoding="utf-8")
    assert "_concrete_handoff" in src
    assert 'bool(out["handoff_state"]) or bool(concl["next_check"])' not in src


def test_t2f04_report_shows_the_handoff_detail() -> None:
    import live_battery

    def _record(has: bool, detail: str) -> dict:
        return {
            "case": "L8",
            "label": "L8",
            "rep": 1,
            "verdict": "PASS",
            "status": "COMPLETED",
            "detail": "ok",
            "duration_s": 1.0,
            "evidence_total": 2,
            "hypotheses": [],
            "has_handoff": has,
            "handoff_detail": detail,
        }

    report = live_battery.build_report(
        {"ts": "t", "api_base": "a", "tenant": "t", "app_id": "app", "git_sha": "s",
         "prompt_version": "p", "llm": "m", "run_tag": "r"},
        [_record(True, "next_fetch (price 2): SEARCH_CODE OrderServiceHandler"),
         _record(False, "no next_fetch and no eliminated rivals")],
    )
    yes = next(line for line in report.splitlines() if line.startswith("| L8 |") and "2.0" in line or "SEARCH_CODE" in line)
    assert "SEARCH_CODE" in yes
    no = [line for line in report.splitlines() if line.startswith("| L8 |")]
    assert len(no) == 2
    assert any(line.rstrip().endswith("| - |") or "| - |" in line for line in no)
