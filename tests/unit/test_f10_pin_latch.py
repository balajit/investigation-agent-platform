# tests/unit/test_f10_pin_latch.py
"""F10-A: loop-pin posture, create-activity latch, determinism lint (Sonnet F D7/D8/D11).

D9 moot (W1-A deleted _order_id); D10 model parts skipped per O-F10-3 (W1-A done).
"""

import ast
import inspect
import pathlib

from investigation_agent_platform.application.worker.workflows import forcing  # noqa: F401


def test_no_workflow_patch_markers_before_go_live() -> None:
    workflows = pathlib.Path("src/investigation_agent_platform/application/worker/workflows")
    banned = {"patched", "deprecate_patch", "get_version"}
    hits: list[str] = []
    for path in sorted(workflows.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Attribute) and node.attr in banned:
                hits.append(f"{path.name}:{node.lineno} {node.attr}")
    assert not hits, f"posture is loop pin + replay corpus, not patched(): {hits}"


def test_workflow_latches_pin_from_history() -> None:
    import investigation_agent_platform.application.worker.workflows.run_investigation as ri

    src = inspect.getsource(ri)
    assert "input_data.loop_version or getattr(create_res" in src
    assert "LoopVersionMissing" in src
    # No direct CURRENT fallback left in workflow code.
    assert "input_data.loop_version or LOOP_VERSION_CURRENT" not in src


def test_create_activity_pins_loop_version() -> None:
    from investigation_agent_platform.application.worker.activities import reasoning as r

    assert "_pinned_loop_version" in dir(r)
    out_fields = set(r.CreateInvestigationOutput.__dataclass_fields__)
    assert "loop_version" in out_fields


def test_workflow_reachable_code_is_deterministic() -> None:
    import tests.unit.test_workflow_determinism_lint as lint

    assert lint._lint() == []
