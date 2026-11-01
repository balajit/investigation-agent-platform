"""WP-09 acceptance matrix contract tests."""

from investigation_agent_platform.application.acceptance.wp09 import (
    WP09_SCENARIOS,
    WP09ExecutionMode,
    WP09Status,
    initial_not_verified_results,
    result_for_unavailable,
    scenario_map,
    summarize_results,
)


def test_wp09_registry_contains_exact_a_to_i_scenarios() -> None:
    assert tuple(scenario_map()) == tuple("ABCDEFGHI")
    assert len(WP09_SCENARIOS) == 9


def test_wp09_scenarios_require_integrated_modes() -> None:
    assert scenario_map()["A"].required_mode is WP09ExecutionMode.LIVE_TEMPORAL
    assert scenario_map()["G"].required_mode is WP09ExecutionMode.DISPOSABLE_DB
    assert scenario_map()["H"].required_mode is WP09ExecutionMode.DISPOSABLE_DB


def test_unavailable_runtime_is_not_reported_as_pass() -> None:
    result = result_for_unavailable("E", "Temporal worker unavailable")
    assert result.status is WP09Status.NOT_VERIFIED
    assert result.as_dict()["scenario_id"] == "E"
    assert result.as_dict()["evidence"]


def test_initial_matrix_requires_explicit_a_to_i_execution() -> None:
    summary = summarize_results(initial_not_verified_results())
    assert summary["scenario_count"] == 9
    assert summary["all_passed"] is False
    assert summary["counts"][WP09Status.NOT_VERIFIED.value] == 9
