# tests/unit/test_workflow_determinism_lint.py
"""D11: static determinism lint over workflow-reachable code (Sonnet F)."""

from __future__ import annotations

import ast
import pathlib

SRC = pathlib.Path("src")
ROOTS = [
    "investigation_agent_platform.application.worker.workflows.run_investigation",
    "investigation_agent_platform.application.worker.workflows.forcing",
]
#: (module, function-or-class, construct) -> reason.
ACCEPTED: dict[tuple[str, str, str], str] = {
    (
        "investigation_agent_platform.domain.collection.work_orders",
        "WorkOrder",
        "uuid4",
    ): "latent: every workflow construction site passes explicit work_order_id (uuid5); default never fires in workflow path (W1-A, O-F10-3 skip)",
    (
        "investigation_agent_platform.domain.collection.frontier",
        "FrontierCandidate",
        "uuid4",
    ): "latent: no workflow caller after W1-A (advisory ranking deleted); retained library only",
    (
        "investigation_agent_platform.domain.collection.frontier",
        "FrontierCandidate",
        "datetime.now",
    ): "latent: seeded_at unread by production (W6); no workflow caller after W1-A",
}
FORBIDDEN_ATTRS = {
    ("uuid", "uuid4"),
    ("uuid", "uuid1"),
    ("random", "random"),
    ("random", "randint"),
    ("random", "choice"),
    ("time", "time"),
    ("time", "monotonic"),
    ("os", "environ"),
    ("os", "getenv"),
    ("datetime", "now"),
    ("datetime", "utcnow"),
    ("datetime", "today"),
    ("secrets", "token_hex"),
}
FORBIDDEN_NAMES = {"uuid4", "uuid1", "getenv"}


def _tree(module: str) -> ast.Module | None:
    path = SRC / (module.replace(".", "/") + ".py")
    return ast.parse(path.read_text(encoding="utf-8")) if path.exists() else None


def _defs(tree: ast.Module) -> dict[str, ast.AST]:
    return {n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}


def _platform_imports(tree: ast.Module) -> set[tuple[str, str]]:
    out: set[tuple[str, str]] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and n.level == 0 and (n.module or "").startswith(
            "investigation_agent_platform"
        ):
            out |= {(n.module, a.name) for a in n.names}  # type: ignore[arg-type]
    return out


def _violations(node: ast.AST) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for n in ast.walk(node):
        if (
            isinstance(n, ast.Attribute)
            and isinstance(n.value, ast.Name)
            and (n.value.id, n.attr) in FORBIDDEN_ATTRS
        ):
            found.append((n.lineno, f"{n.value.id}.{n.attr}"))
        if isinstance(n, ast.Name) and n.id in FORBIDDEN_NAMES and isinstance(n.ctx, ast.Load):
            found.append((n.lineno, n.id))
    return found


def _reachable(
    module: str, roots: list[str], seen: set[tuple[str, str]]
) -> list[tuple[str, str, int, str]]:
    tree = _tree(module)
    if tree is None:
        return []
    table, out, stack = _defs(tree), [], list(roots)
    while stack:
        name = stack.pop()
        if (module, name) in seen or name not in table:
            continue
        seen.add((module, name))
        node = table[name]
        out += [(module, name, line, what) for line, what in _violations(node)]
        used = {n.id for n in ast.walk(node) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
        stack += [u for u in used if u in table]
        for other_module, imported in _platform_imports(tree):
            if imported in used and imported not in table:
                out += _reachable(other_module, [imported], seen)
    return out


def _lint() -> list[tuple[str, str, int, str]]:
    seen: set[tuple[str, str]] = set()
    hits: list[tuple[str, str, int, str]] = []
    for module in ROOTS:
        tree = _tree(module)
        assert tree is not None
        hits += _reachable(module, list(_defs(tree)), seen)
    return sorted({h for h in hits if (h[0], h[1], h[3]) not in ACCEPTED})


def test_workflow_reachable_code_is_deterministic() -> None:
    assert _lint() == [], "workflow-reachable code reads clock, randomness or environment"
