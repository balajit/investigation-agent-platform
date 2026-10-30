# tests/unit/test_loop_exit_contract.py
"""Pin the loop terminal contract after the Step-1 exit removal.

Step 1 (cdd28fb) deleted the readiness-only ``0.95`` loop break from
``RunInvestigationWorkflow._run_inner``: the loop now runs to its iteration
bound and terminates only on verification, a forced terminal override
(conclude / defer / budget / cancel / D6), or the bound itself. A high
self-reported readiness without verification buys more turns, never an
early exit (the overconfidence counter at ``>= 0.8`` only instruments).

No Temporal test harness exists in this repo, so this is an AST
contract tripwire over run_investigation.py (same technique as
test_part9_phase5.py import hygiene): it fails if anyone re-adds a
readiness-gated break, and pins the iteration bound. It does not
execute the workflow.
"""

import ast
import pathlib

_WORKFLOW = (
    pathlib.Path(__file__).resolve().parents[2]
    / "src"
    / "investigation_agent_platform"
    / "application"
    / "worker"
    / "workflows"
    / "run_investigation.py"
)


def _main_loop() -> tuple[ast.While, dict[int, ast.AST]]:
    """Return the ``while iteration < max_iterations`` loop and a child->parent map."""
    tree = ast.parse(_WORKFLOW.read_text(encoding="utf-8"), filename=str(_WORKFLOW))
    cls = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.name == "RunInvestigationWorkflow"
    )
    run_fn = next(
        node
        for node in cls.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "_run_inner"
    )
    parents: dict[int, ast.AST] = {}
    for node in ast.walk(run_fn):
        for child in ast.iter_child_nodes(node):
            parents.setdefault(id(child), node)
    loops = [
        node
        for node in ast.walk(run_fn)
        if isinstance(node, ast.While)
        and "max_iterations" in ast.get_source_segment(str(_WORKFLOW.read_text()), node.test)
    ]
    assert len(loops) == 1, f"expected exactly one bounded main loop, found {len(loops)}"
    return loops[0], parents


def _breaks_in(loop: ast.While) -> list[ast.Break]:
    """Break statements in the loop, not descending into nested defs/lambdas."""
    found: list[ast.Break] = []

    def visit(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue
            if isinstance(child, ast.Break):
                found.append(child)
                continue
            visit(child)

    visit(loop)
    return found


def test_main_loop_bound_is_iteration_based() -> None:
    loop, _ = _main_loop()
    source = ast.get_source_segment(_WORKFLOW.read_text(encoding="utf-8"), loop.test)
    assert source is not None and "max_iterations" in source
    assert "readiness" not in source.lower()


def test_no_readiness_gated_break() -> None:
    loop, parents = _main_loop()
    source = _WORKFLOW.read_text(encoding="utf-8")
    gated: list[str] = []
    for brk in _breaks_in(loop):
        node: ast.AST | None = brk
        while node is not None and not isinstance(node, ast.If):
            node = parents.get(id(node))
        if node is not None:
            condition = ast.get_source_segment(source, node.test) or ""
            if "readiness" in condition.lower():
                gated.append(condition.strip())
    assert gated == [], f"readiness-gated loop exits reintroduced: {gated}"
    # The tripwire sees the loop's real exits; guard against it going
    # blind (e.g. loop refactored beyond recognition).
    assert len(_breaks_in(loop)) >= 3
