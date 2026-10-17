"""G8 synthesis-forcing rule: pure-function unit tests.

The rule lives in the workflow module (workflow-safe, no Temporal imports
at test time for the pure helper) and is wired into the investigation loop
to override gathering actions with FORMULATE_HYPOTHESIS when the model
holds unevaluated hypotheses against banked evidence.
"""

from investigation_agent_platform.application.worker.workflows import (
    _should_force_formulate,
)


def test_forces_when_pending_hypotheses_banked_evidence_and_turns_elapsed():
    assert (
        _should_force_formulate(
            iteration=2,
            formulated_count=0,
            hypotheses_evaluated=["h1"],
            evidence_banked_total=5,
            forced_count=0,
        )
        is True
    )


def test_no_force_once_formulated():
    assert _should_force_formulate(5, 1, ["h1"], 9, 0) is False


def test_no_force_without_pending_hypotheses():
    assert _should_force_formulate(5, 0, [], 9, 0) is False
    assert _should_force_formulate(2, 0, None, 9, 0) is False


def test_no_force_without_banked_evidence():
    assert _should_force_formulate(5, 0, ["h1"], 0, 0) is False


def test_no_force_before_two_turns_elapsed():
    assert _should_force_formulate(1, 0, ["h1"], 9, 0) is False


def test_no_force_after_cap_reached():
    assert _should_force_formulate(9, 0, ["h1"], 9, 2) is False
