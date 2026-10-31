# tests/unit/test_f8_cascade_completeness.py
"""F8 D14/D15: cascade completeness — one loop over RULES, every rule writes its force and counters.

Post-D15 the cascade writes flags/counters through the loop
(``rule.force.value``, ``counts[fire.counter]``), so presence is checked on
the table + triggers + loop structure instead of per-rule literals.
"""

import ast
import inspect

from investigation_agent_platform.application.worker.workflows import forcing
from investigation_agent_platform.application.worker.workflows.forcing import (
    TRIGGERS,
    check_triggers,
)
from investigation_agent_platform.domain.investigation.forcing_rules import (
    COUNTER_KEYS,
    RULES,
    Rule,
)

WORKFLOW_COUNTED = {"requery"}  # incremented by the workflow at the consume stamp, not by the cascade


def completeness_problems(
    rules: tuple[Rule, ...] = RULES,
    triggers: dict | None = None,
    keys: tuple[str, ...] = COUNTER_KEYS,
) -> list[str]:
    triggers = TRIGGERS if triggers is None else triggers
    problems = list(check_triggers(rules, triggers))
    for rule in rules:
        trigger = triggers.get(rule.name)
        if trigger is None:
            continue
        src = inspect.getsource(trigger)
        counters: set[str] = set()
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if f'counter="{node.value}"' in src or f"counter='{node.value}'" in src:
                    counters.add(node.value)
        # every rule counter is produced by its trigger (requery: workflow-counted).
        # smallest_fetch builds its per-kind key dynamically (f"smallest_fetch_{kind.lower()}").
        for key in rule.counter_keys:
            if key in WORKFLOW_COUNTED:
                continue
            if key in src:
                continue
            if key.startswith("smallest_fetch_") and 'smallest_fetch_{kind.lower()}' in src:
                continue
            problems.append(f"rule {rule.name} trigger never mentions counter {key}")
        for key in counters:
            if key not in keys:
                problems.append(f"rule {rule.name} trigger uses unknown counter {key}")
    return problems


def test_the_cascade_writes_every_rules_force_and_counters() -> None:
    assert completeness_problems() == []


def test_triggers_match_the_table() -> None:
    assert set(TRIGGERS) == {r.name for r in RULES}


def test_cascade_is_one_loop_over_the_table() -> None:
    source = inspect.getsource(forcing.apply_post_reason_cascade)
    assert source.count('_down("') == 0
    assert len([n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.For)]) >= 1
    assert "for rule in RULES:" in source
    assert "TRIGGERS[rule.name](ctx)" in source
    assert "rule.force.value" in source


def test_completeness_contract_can_fail() -> None:
    bad_rules = (
        Rule("probe", RULES[0].force, frozenset(), counters=("nope_not_a_counter",)),
    )
    assert completeness_problems(rules=bad_rules, triggers={"probe": TRIGGERS["pivot"]}) != []
    assert completeness_problems(triggers={}) != []
