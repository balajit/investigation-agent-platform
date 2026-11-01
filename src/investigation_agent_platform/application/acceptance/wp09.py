"""WP-09 S1-S7 acceptance scenario registry and result contract."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class WP09Status(StrEnum):
    PASS = "PASS"
    NOT_VERIFIED = "NOT_VERIFIED"
    BLOCKED = "BLOCKED"


class WP09ExecutionMode(StrEnum):
    OFFLINE = "OFFLINE"
    DISPOSABLE_DB = "DISPOSABLE_DB"
    LIVE_TEMPORAL = "LIVE_TEMPORAL"
    PROVIDER_DEPENDENT = "PROVIDER_DEPENDENT"


@dataclass(frozen=True)
class WP09Scenario:
    scenario_id: str
    title: str
    required_mode: WP09ExecutionMode
    existing_evidence: tuple[str, ...]
    acceptance: tuple[str, ...]


@dataclass(frozen=True)
class WP09Result:
    scenario_id: str
    status: WP09Status
    execution_mode: WP09ExecutionMode
    evidence: tuple[str, ...] = ()
    reason: str = ""
    observed: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "status": self.status.value,
            "execution_mode": self.execution_mode.value,
            "evidence": list(self.evidence),
            "reason": self.reason,
            "observed": dict(self.observed or {}),
        }


WP09_SCENARIOS: tuple[WP09Scenario, ...] = (
    WP09Scenario("A", "successful causal conclusion and fresh-context durable readback", WP09ExecutionMode.LIVE_TEMPORAL, ("tests/integration/test_verify_conclude_path.py", "scripts/live_battery.py"), ("terminal conclusion", "fresh API/DB readback", "supporting evidence")),
    WP09Scenario("B", "unqualified evidence and qualification failure", WP09ExecutionMode.DISPOSABLE_DB, ("tests/integration/test_s2_flow_battery_db.py", "tests/unit/test_s4_qualification.py"), ("qualification failure", "persisted outcome", "fresh readback")),
    WP09Scenario("C", "unresolved rival hypothesis", WP09ExecutionMode.LIVE_TEMPORAL, ("tests/evaluation/test_incident_replay.py",), ("rival persisted", "honest conclusion", "fresh readback")),
    WP09Scenario("D", "budget exhaustion with concrete priced handoff", WP09ExecutionMode.LIVE_TEMPORAL, ("tests/unit/test_s7_priced_handoff.py", "tests/integration/test_s2_flow_battery_db.py"), ("priced next fetch", "persisted handoff", "fresh readback")),
    WP09Scenario("E", "resume of the exact blocked fetch", WP09ExecutionMode.LIVE_TEMPORAL, ("tests/unit/test_s7_continuation.py", "tests/unit/test_s7_continuation_api.py"), ("exact action", "Temporal execution", "history-preserved readback")),
    WP09Scenario("F", "provider failure distinguished from empty result", WP09ExecutionMode.LIVE_TEMPORAL, ("tests/unit/test_wp_a3_retrieval_ledger.py",), ("typed failure", "clean empty", "distinct persisted outcomes")),
    WP09Scenario("G", "persistence failure with transaction rollback", WP09ExecutionMode.DISPOSABLE_DB, ("tests/unit/test_h14_conclusion_unit.py",), ("injected failure", "rollback", "fresh connection readback")),
    WP09Scenario("H", "tenant boundary across evidence, handoff, continuation, and conclusion", WP09ExecutionMode.DISPOSABLE_DB, ("tests/integration/test_tenancy_rls.py", "tests/unit/test_s7_continuation_api.py"), ("no cross-tenant read", "no existence oracle", "worker/API parity")),
    WP09Scenario("I", "prompt-safety rejection prevents LLM invocation", WP09ExecutionMode.LIVE_TEMPORAL, ("tests/unit/test_wp08_prompt_safety.py",), ("production composition", "typed rejection", "zero provider calls")),
)


def scenario_map() -> dict[str, WP09Scenario]:
    return {scenario.scenario_id: scenario for scenario in WP09_SCENARIOS}


def result_for_unavailable(scenario_id: str, reason: str) -> WP09Result:
    scenario = scenario_map()[scenario_id]
    return WP09Result(scenario_id, WP09Status.NOT_VERIFIED, scenario.required_mode, scenario.existing_evidence, reason)


def initial_not_verified_results(reason: str = "No integrated environment run recorded") -> list[WP09Result]:
    """Return an honest complete matrix before any A-I runner executes."""
    return [result_for_unavailable(scenario.scenario_id, reason) for scenario in WP09_SCENARIOS]


def summarize_results(results: list[WP09Result]) -> dict[str, Any]:
    """Summarize a scenario matrix without treating missing runs as passes."""
    counts = {status.value: 0 for status in WP09Status}
    for result in results:
        counts[result.status.value] = counts.get(result.status.value, 0) + 1
    return {
        "scenario_count": len(results),
        "counts": counts,
        "all_passed": len(results) == len(WP09_SCENARIOS)
        and counts[WP09Status.PASS.value] == len(WP09_SCENARIOS),
        "results": [result.as_dict() for result in results],
    }
