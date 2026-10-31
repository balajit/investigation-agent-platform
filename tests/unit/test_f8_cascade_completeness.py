# tests/unit/test_f8_cascade_completeness.py
"""F8 D14: cascade completeness contract — every rule writes its force and counters."""

import ast
import inspect

from investigation_agent_platform.application.worker.workflows import forcing
from investigation_agent_platform.domain.investigation.forcing_rules import (
    COUNTER_KEYS,
    RULES,
    Rule,
)

WORKFLOW_COUNTED = {"requery"}


def completeness_problems(
    source: str, rules: tuple[Rule, ...] = RULES, keys: tuple[str, ...] = COUNTER_KEYS
) -> list[str]:
    tree = ast.parse(source)
    writes: set[str] = set()
    counted: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Store):
            seg = ast.unparse(node.slice)
            if seg.startswith("Force.") and seg.endswith(".value"):
                writes.add(seg.removeprefix("Force.").removesuffix(".value"))
            if ast.unparse(node.value) == "_counts" and isinstance(node.slice, ast.Constant):
                counted.add(str(node.slice.value))
    problems = []
    for rule in rules:
        if rule.force.name not in writes:
            problems.append(f"rule {rule.name} never writes Force.{rule.force.name}")
        for key in rule.counter_keys:
            if key not in WORKFLOW_COUNTED and key not in counted:
                problems.append(f"rule {rule.name} never increments counter {key}")
    problems += [f"cascade counts unknown key {k}" for k in sorted(counted - set(keys))]
    return problems


def test_the_cascade_writes_every_rules_force_and_counters() -> None:
    assert completeness_problems(inspect.getsource(forcing.apply_post_reason_cascade)) == []
