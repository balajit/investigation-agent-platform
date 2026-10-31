# tests/unit/test_f10_replay_corpus.py
"""F10-B tier 1: replay corpus proves the cascade command-neutral (Sonnet F D12/D13)."""

import json
import pathlib

from tests.replay.iap_replay_harness import cascade_scenario_dispatch, check_input_parity
from tests.replay.scenarios import SCENARIOS

TRANSCRIPTS = pathlib.Path("tests/replay/transcripts")


def test_scenarios_have_committed_transcripts() -> None:
    for name in SCENARIOS:
        assert (TRANSCRIPTS / f"{name}.json").exists(), f"missing transcript for {name}"


def test_corpus_dispatch_matches_committed_transcripts() -> None:
    for name, turns in SCENARIOS.items():
        expected = json.loads((TRANSCRIPTS / f"{name}.json").read_text(encoding="utf-8"))
        assert cascade_scenario_dispatch(turns) == expected, name


def test_input_parity_checker_catches_tampering() -> None:
    recorded = [{"activity": "a", "input": {"x": 1}, "result": {}}]
    assert check_input_parity(recorded, recorded) == []
    tampered = [{"activity": "a", "input": {"x": 2}, "result": {}}]
    assert check_input_parity(recorded, tampered) != []


def test_replayer_mechanics_deferred_to_integration() -> None:
    """Tier 2 mechanics need a time-skipping server binary (download not authorized).

    The corpus (tier 1) is the gate for cascade refactors. Full workflow replay
    with SandboxedWorkflowRunner + input-parity interceptor is specified in
    Sonnet F D12 and deferred to an integration tier with owner approval.
    """
    import pytest

    pytest.skip("time-skipping server unavailable (binary download not authorized)")
