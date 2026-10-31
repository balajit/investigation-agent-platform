# tests/unit/test_f2_positional_history_guard.py
"""F2-D20: AST guard against positional history reads outside carrier readers."""

from __future__ import annotations

import ast
import pathlib

BASE = pathlib.Path("src/investigation_agent_platform/application")
MODULES = [
    BASE / "worker/workflows/forcing.py",
    BASE / "worker/workflows/run_investigation.py",
    BASE / "evidence/retrieval_ledger.py",
    BASE / "investigation/shadow_capture.py",
]
HISTORY_NAMES = {"action_history", "history", "_history", "entries", "recent_actions", "source", "turns"}
CARRIER_READERS = {
    "_should_force_order_requery": "indexes into the full list to find and stamp the newest unconsumed REQUERY carrier",
    "_smallest_fetch_already_covered": "scans for a gap-id coverage stamp by key, newest first",
    "_t_smallest_fetch": "reads the newest feed carrier (a tombstone supersedes older feeds)",
    "_entry_at": "indexes the full list by a validated order index for the requery trigger",
}


def _is_history(node: ast.AST) -> bool:
    name = node.id if isinstance(node, ast.Name) else node.attr if isinstance(node, ast.Attribute) else ""
    return name in HISTORY_NAMES


def positional_problems(source: str, allow: dict[str, str] = CARRIER_READERS) -> list[str]:
    tree, problems = ast.parse(source), []
    for fn in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        takes_turns = any("Turns" in ast.unparse(a.annotation) for a in fn.args.args if a.annotation)
        for node in ast.walk(fn):
            hit = None
            if isinstance(node, ast.Subscript) and isinstance(node.value, (ast.Name, ast.Attribute)) and _is_history(node.value):
                if not (isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str)):
                    hit = ast.unparse(node)
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {"reversed", "enumerate"}:
                if node.args and any(_is_history(n) for n in ast.walk(node.args[0])):
                    hit = ast.unparse(node)
            if hit and not takes_turns and fn.name not in allow:
                problems.append(f"{fn.name}:{node.lineno} positional read {hit}")
    return problems


def test_no_positional_history_reads_outside_carrier_readers() -> None:
    for path in MODULES:
        assert positional_problems(path.read_text(encoding="utf-8")) == [], path.name


def test_guard_can_fail() -> None:
    flagged = positional_problems("def f(history):\n    return history[-2:]\n")
    assert len(flagged) == 1 and "f:" in flagged[0]
    clean = positional_problems("def f(turns: Turns):\n    return turns[-2:]\n")
    assert clean == []


def test_allowlist_cannot_rot() -> None:
    import inspect as _inspect

    from investigation_agent_platform.application.worker.workflows import forcing as _forcing

    for name in CARRIER_READERS:
        fn = getattr(_forcing, name, None)
        assert fn is not None, f"allowlist entry {name} no longer exists"
        assert positional_problems(_inspect.getsource(fn), allow={}) != [], f"allowlist entry {name} no longer indexes"
