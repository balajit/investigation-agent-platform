# tests/unit/test_h11_contradiction_severity.py
"""H11: a stray model-reported contradiction no longer poisons general verification.

The reason activity persists every model-reported contradiction as MEDIUM with no
evidence or hypothesis link, and nothing resolves it. `evaluate_hypothesis` counted
EVERY unresolved contradiction against a limit of zero, so one stray prose
contradiction forced CONTRADICTED on the general path, while ConclusionGate and the
specialized validators already block only HIGH/CRITICAL.

lv5 aligns the general path with the rest: only unresolved HIGH/CRITICAL
contradictions block. Lower severities stay recorded (and still appear as
against-evidence on a verified verdict). Older pins keep the old behavior.
"""

from __future__ import annotations

import pytest
from test_evidence_bound_verification import _ev, _hyp

from investigation_agent_platform.application.investigation.verification import (
    Contradiction,
    ContradictionSeverity,
    RootCauseVerificationPolicy,
    VerificationEngine,
    VerificationStatus,
)
from investigation_agent_platform.application.worker.activities.inputs import (
    _policy_for_loop_version,
)


def _evaluate(severity: ContradictionSeverity, policy: RootCauseVerificationPolicy):  # type: ignore[no-untyped-def]
    return VerificationEngine().evaluate_hypothesis(
        _hyp(),
        [_ev() for _ in range(6)],
        [Contradiction(statement="a stray prose contradiction", severity=severity)],
        [],
        policy,
    )


BLOCKING = RootCauseVerificationPolicy(blocking_contradictions_only=True)
LEGACY = RootCauseVerificationPolicy()


@pytest.mark.parametrize("severity", list(ContradictionSeverity))
def test_the_old_policy_still_blocks_on_any_unresolved_contradiction(severity) -> None:
    assert _evaluate(severity, LEGACY).status == VerificationStatus.CONTRADICTED


@pytest.mark.parametrize(
    "severity", [ContradictionSeverity.HIGH, ContradictionSeverity.CRITICAL]
)
def test_high_and_critical_still_block_under_the_new_policy(severity) -> None:
    assert _evaluate(severity, BLOCKING).status == VerificationStatus.CONTRADICTED


@pytest.mark.parametrize(
    "severity",
    [ContradictionSeverity.MEDIUM, ContradictionSeverity.LOW],
)
def test_lower_severities_no_longer_contradict_under_the_new_policy(severity) -> None:
    assert _evaluate(severity, BLOCKING).status != VerificationStatus.CONTRADICTED


def test_a_resolved_high_contradiction_does_not_block() -> None:
    resolved = Contradiction(
        statement="resolved", severity=ContradictionSeverity.HIGH
    ).resolve("someone", "checked")
    result = VerificationEngine().evaluate_hypothesis(
        _hyp(), [_ev() for _ in range(6)], [resolved], [], BLOCKING
    )
    assert result.status != VerificationStatus.CONTRADICTED


def test_the_limit_still_applies_to_the_blocking_count() -> None:
    policy = RootCauseVerificationPolicy(
        blocking_contradictions_only=True, max_allowed_unresolved_contradictions=1
    )
    one_high = [Contradiction(statement="a", severity=ContradictionSeverity.HIGH)]
    two_high = [
        Contradiction(statement="a", severity=ContradictionSeverity.HIGH),
        Contradiction(statement="b", severity=ContradictionSeverity.HIGH),
    ]
    engine = VerificationEngine()
    evidence = [_ev() for _ in range(6)]
    assert (
        engine.evaluate_hypothesis(_hyp(), evidence, one_high, [], policy).status
        != VerificationStatus.CONTRADICTED
    )
    assert (
        engine.evaluate_hypothesis(_hyp(), evidence, two_high, [], policy).status
        == VerificationStatus.CONTRADICTED
    )


# --- version routing ------------------------------------------------------------------------------


def test_lv5_and_unmarked_runs_use_the_blocking_only_policy() -> None:
    for metadata in ({}, None, {"loop_version": "lv5"}):
        out = _policy_for_loop_version(RootCauseVerificationPolicy(), metadata)
        assert out.blocking_contradictions_only is True, metadata


def test_older_pins_keep_the_old_policy() -> None:
    for pin in ("lv2", "lv3", "lv4"):
        out = _policy_for_loop_version(RootCauseVerificationPolicy(), {"loop_version": pin})
        assert out.blocking_contradictions_only is False, pin


def test_an_unreadable_pin_keeps_the_old_policy() -> None:
    out = _policy_for_loop_version(RootCauseVerificationPolicy(), {"loop_version": "lv99"})
    assert out.blocking_contradictions_only is False


def test_the_policy_is_otherwise_unchanged() -> None:
    base = RootCauseVerificationPolicy.runtime()
    out = _policy_for_loop_version(base, {})
    assert out.model_copy(
        update={
            "blocking_contradictions_only": False,
            "tolerant_band": False,
            "require_causal_step_for_causal_claims": False,
        }
    ) == base


# --- wiring -------------------------------------------------------------------------------------------


def test_both_choke_points_route_the_policy_by_loop_version() -> None:
    import inspect

    from investigation_agent_platform.application.worker.activities import (
        lifecycle,
        verification,
    )

    assert "_policy_for_loop_version(" in inspect.getsource(verification)
    assert "_policy_for_loop_version(" in inspect.getsource(lifecycle)
