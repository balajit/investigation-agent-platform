"""G6-harness unit half: replay recorded histories through the shadow policy.

Covers the forward-plan Step 3 exit: determinism over fixture
histories, divergence fixtures (shadow-vs-recorded), budget-accounting
checks, recovery-priority checks, and a static zero-side-effects proof
over the policy + harness modules. The live measurement leg (replay on
real runs) is operator-owned and recorded OPEN, not claimed here.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from investigation_agent_platform.application.investigation.action_policy import (
    PolicyError,
    decide,
)
from investigation_agent_platform.application.investigation.replay import (
    RecordedStep,
    replay_history,
    report_to_json_dict,
)

_FIXTURES = Path(__file__).parent.parent / "fixtures" / "replay"


def _load(name: str) -> list[RecordedStep]:
    raw = json.loads((_FIXTURES / name).read_text())
    return [RecordedStep.model_validate(step) for step in raw["steps"]]


def test_deterministic_replay_identical_reports() -> None:
    for name in (
        "agree_basic.json",
        "diverge_forcing.json",
        "budget_exhaustion.json",
        "recovery.json",
    ):
        steps = _load(name)
        first = report_to_json_dict(replay_history(steps))
        second = report_to_json_dict(replay_history(_load(name)))
        assert first == second


def test_agreement_fixture_zero_divergence() -> None:
    report = replay_history(_load("agree_basic.json"))
    assert report.total == 3
    assert report.matched == 3 and report.diverged == 0
    assert all(s.cause == "" for s in report.steps)
    assert [s.selected for s in report.steps] == ["CORRELATE"] * 3


def test_divergence_fixture_flagged_with_forcing_cause() -> None:
    report = replay_history(_load("diverge_forcing.json"))
    assert report.total == 2
    assert report.matched == 0 and report.diverged == 2
    assert report.steps[0].selected == "WALK_STACK"
    assert "walk_stack" in report.steps[0].cause
    assert report.steps[1].selected == "FORMULATE_HYPOTHESIS"
    assert "formulate" in report.steps[1].cause


def test_budget_exhaustion_terminal_agreement() -> None:
    report = replay_history(_load("budget_exhaustion.json"))
    assert report.total == 4
    assert report.matched == 3 and report.diverged == 1
    assert report.terminal_steps == 3
    pre, exhausted, tokens, missed = report.steps
    assert not pre.shadow_terminal and pre.selected == "CORRELATE"
    assert exhausted.shadow_terminal and exhausted.selected == "CONCLUDE"
    assert tokens.shadow_terminal and tokens.selected == "CONCLUDE"
    assert missed.shadow_terminal and not missed.match
    assert missed.selected == "CONCLUDE" and missed.recorded == "SEARCH_CODE"
    assert "shadow-terminal" in missed.cause


def test_recovery_blind_retry_diverges_then_agrees() -> None:
    steps = _load("recovery.json")
    shadow = decide(steps[0].snapshot)
    penalized = next(c for c in shadow.candidates if c.action == "GET_CODE")
    assert penalized.priority > 1000 and "penalized" in penalized.reason
    report = replay_history(steps)
    assert report.total == 2
    blind, sound = report.steps
    assert not blind.match
    assert blind.recorded_was_withheld
    assert "dispatcher denied" in blind.withheld_reason
    assert "recorded-withheld" in blind.cause
    assert blind.selected == "CORRELATE"
    assert sound.match and sound.selected == "CORRELATE"


def test_unknown_recorded_action_tolerated_as_divergence() -> None:
    report = replay_history([RecordedStep(snapshot={}, recorded_action="FUTURE_ACTION")])
    assert report.total == 1 and report.diverged == 1
    assert not report.steps[0].match
    assert report.steps[0].cause != ""


def test_malformed_step_fails_closed() -> None:
    with pytest.raises(PolicyError):
        replay_history(["not-a-step"])  # type: ignore[list-item]


def test_shadow_never_stalls_on_fixtures() -> None:
    for name in (
        "agree_basic.json",
        "diverge_forcing.json",
        "budget_exhaustion.json",
        "recovery.json",
    ):
        for step in replay_history(_load(name)).steps:
            assert step.selected is not None


def _module_ast(name: str) -> ast.Module:
    root = Path(__file__).parent.parent.parent
    return ast.parse(
        (
            root
            / "src"
            / "investigation_agent_platform"
            / "application"
            / "investigation"
            / f"{name}.py"
        ).read_text()
    )


_ALLOWED_TOP_IMPORTS = {
    "action_policy": {"__future__", "enum", "typing", "pydantic", "investigation_agent_platform"},
    "replay": {"__future__", "typing", "pydantic", "investigation_agent_platform"},
}
_ALLOWED_FROM_NAMES = {
    "action_policy": {"ActionType"},
    "replay": {"PlannerSnapshot", "PolicyDecision", "PolicyError", "PolicyErrorCode", "decide"},
}
_ALLOWED_FROM_MODULES = {
    "action_policy": {"investigation_agent_platform.domain.investigation.models"},
    "replay": {"investigation_agent_platform.application.investigation.action_policy"},
}
_FORBIDDEN_ATTRS = {
    "now",
    "utcnow",
    "today",
    "time",
    "sleep",
    "monotonic",
    "perf_counter",
    "open",
    "system",
    "popen",
    "execl",
    "spawnl",
    "__import__",
    "exit",
}


@pytest.mark.parametrize("module", ["action_policy", "replay"])
def test_static_zero_side_effects(module: str) -> None:
    """Policy + harness modules cannot reach I/O, clock, or execution.

    Top-level imports are allowlisted; intra-package from-imports are
    name-pinned (the policy touches the domain only via the ActionType
    enum — no model constructors, hence no timestamp default_factory
    clock reads); no clock/io/execution attribute, dynamic import,
    eval, or exec appears anywhere in either file.
    """
    tree = _module_ast(module)
    top_level = {
        (n.names[0].name if isinstance(n, ast.Import) else n.module or "").split(".")[0]
        for n in ast.walk(tree)
        if isinstance(n, (ast.Import, ast.ImportFrom))
    }
    assert top_level <= _ALLOWED_TOP_IMPORTS[module], top_level
    from_names = {
        a.asname or a.name
        for n in ast.walk(tree)
        if isinstance(n, ast.ImportFrom)
        and (n.module or "").startswith("investigation_agent_platform")
        for a in n.names
    }
    assert from_names == _ALLOWED_FROM_NAMES[module], from_names
    from_modules = {
        n.module or ""
        for n in ast.walk(tree)
        if isinstance(n, ast.ImportFrom)
        and (n.module or "").startswith("investigation_agent_platform")
    }
    assert from_modules == _ALLOWED_FROM_MODULES[module], from_modules
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert not (attrs & _FORBIDDEN_ATTRS), attrs & _FORBIDDEN_ATTRS
    calls = {
        n.func.id
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    assert not (calls & {"eval", "exec", "open", "__import__"})


def test_operator_live_measurement_open() -> None:
    """Unit half claims nothing about live runs (operator leg stays OPEN).

    OPEN: G6-measure — shadow-vs-live divergence, budget, and recovery
    measured on real runs by the operator. The replay module exposes no
    live-run, sampling, or measurement entrypoint; this test pins that.
    """
    from investigation_agent_platform.application.investigation import replay

    public = {n for n in dir(replay) if not n.startswith("_")}
    assert public <= {
        "Any",
        "BaseModel",
        "ConfigDict",
        "Field",
        "annotations",
        "RecordedStep",
        "StepReport",
        "ReplayReport",
        "replay_history",
        "report_to_json_dict",
        "PlannerSnapshot",
        "PolicyDecision",
        "PolicyError",
        "PolicyErrorCode",
        "decide",
    }, public
    assert not any("live" in n.lower() or "measure" in n.lower() for n in public)
