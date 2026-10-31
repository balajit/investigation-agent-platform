# tests/unit/test_f8_forcing_rules.py
"""F8: typed forcing flags, an ordered rule table and the stand-down contract.

Before: the cascade's `forced_*` flags were string keys, each rule's stand-down guard
was a hand-written list of `not _reason_data.get("forced_x")` lines, and nothing
failed when a new rule was added without guards. `forced_requery` was a bool in the
reason data but a dict (or False) in the history entry.

After: `Force` is a StrEnum whose values are the existing flag keys (stored shapes are
unchanged), `RULES` is the ordered rule table that holds each rule's stand-down set,
the cascade asks `_stands_down(rule, ...)` instead of repeating guards, and a contract
test requires every rule to stand down on every earlier force unless an exemption with
a reason is recorded. The history entry keeps `forced_requery` as a bool and carries the
consumption stamp in its own `requery_consumed` dict key.

This is a behavior-preserving refactor: the legacy guards are copied below as the
expected sets, so a drift in the table fails here.
"""

from __future__ import annotations

import inspect
import re

import pytest

from investigation_agent_platform.application.evidence.retrieval_ledger import directive_consumed
from investigation_agent_platform.application.worker.workflows import forcing, run_investigation
from investigation_agent_platform.application.worker.workflows.forcing import (
    PostReasonCascadeState,
    _fired_forces,
    _stands_down,
    apply_post_reason_cascade,
)
from investigation_agent_platform.domain.investigation.forcing_rules import (
    RULES,
    Force,
    Rule,
    check_rule_contract,
)

# --- the flag keys ---------------------------------------------------------------------------------

LEGACY_FLAG_KEYS = {
    "ABSENCE_DISCONFIRMATION": "forced_absence_disconfirmation",
    "NOT_CHECKABLE": "forced_not_checkable",
    "PIVOT_RECOLLECT": "forced_pivot_recollect",
    "WALK_STACK": "forced_walk_stack",
    "REQUERY": "forced_requery",
    "FLOOR": "forced_floor",
    "FORMULATE": "forced_formulate",
    "REREAD": "forced_reread",
    "CALLSITE": "forced_callsite",
    "LOG_SEARCH": "forced_log_search",
    "REPLAN": "forced_replan",
    "SMALLEST_FETCH": "forced_smallest_fetch",
}


def test_force_values_are_exactly_the_existing_flag_keys() -> None:
    assert {f.name: f.value for f in Force} == LEGACY_FLAG_KEYS


def test_force_members_are_plain_strings_so_stored_shapes_do_not_change() -> None:
    assert Force.WALK_STACK == "forced_walk_stack"
    assert {Force.WALK_STACK.value: True} == {"forced_walk_stack": True}


# --- the table ---------------------------------------------------------------------------------------

#: The stand-down guards exactly as the cascade hand-wrote them before F8, copied from
#: the old inline `not _reason_data.get(...)` lines (defer, D6 and absence are turn
#: state, handled for every rule and not listed).
LEGACY_STAND_DOWN = {
    "pivot": set(),
    "walk_stack": set(),
    "requery": {"forced_walk_stack", "forced_pivot_recollect"},
    "floor": {"forced_walk_stack", "forced_requery"},
    "formulate": {
        "forced_walk_stack",
        "forced_floor",
        "forced_requery",
        "forced_pivot_recollect",
    },
    "synthesis": {
        "forced_walk_stack",
        "forced_formulate",
        "forced_floor",
        "forced_requery",
        "forced_pivot_recollect",
    },
    "reread": {"forced_formulate", "forced_walk_stack", "forced_floor", "forced_requery"},
    "callsite": {
        "forced_formulate",
        "forced_walk_stack",
        "forced_floor",
        "forced_reread",
        "forced_requery",
        "forced_pivot_recollect",
    },
    "log_search": {
        "forced_formulate",
        "forced_reread",
        "forced_walk_stack",
        "forced_floor",
        "forced_callsite",
        "forced_requery",
        "forced_pivot_recollect",
    },
    "replan": {
        "forced_formulate",
        "forced_reread",
        "forced_log_search",
        "forced_walk_stack",
        "forced_floor",
        "forced_callsite",
        "forced_requery",
        "forced_pivot_recollect",
    },
    "smallest_fetch": {
        "forced_formulate",
        "forced_reread",
        "forced_walk_stack",
        "forced_floor",
        "forced_callsite",
        "forced_requery",
        "forced_log_search",
        "forced_replan",
        "forced_not_checkable",
        "forced_pivot_recollect",
    },
}


def test_the_table_lists_every_cascade_rule_in_evaluation_order() -> None:
    assert [r.name for r in RULES] == [
        "pivot",
        "walk_stack",
        "requery",
        "floor",
        "formulate",
        "synthesis",
        "reread",
        "callsite",
        "log_search",
        "replan",
        "smallest_fetch",
    ]


@pytest.mark.parametrize("rule", RULES, ids=lambda r: r.name)
def test_each_rules_stand_down_set_matches_the_legacy_guards(rule: Rule) -> None:
    assert {f.value for f in rule.stands_down_on} == LEGACY_STAND_DOWN[rule.name]


def test_the_table_satisfies_the_contract() -> None:
    assert check_rule_contract(RULES) == []


def test_synthesis_shares_the_formulate_force() -> None:
    by_name = {r.name: r for r in RULES}
    assert by_name["synthesis"].force == by_name["formulate"].force == Force.FORMULATE


# --- the contract checker itself (it must be able to fail) -----------------------------------------------


def _rule(name, force, down=(), exempt=None):  # type: ignore[no-untyped-def]
    return Rule(name=name, force=force, stands_down_on=frozenset(down), exempt=dict(exempt or {}))


def test_a_rule_that_forgets_an_earlier_force_is_a_violation() -> None:
    table = (
        _rule("a", Force.WALK_STACK),
        _rule("b", Force.FLOOR, down=()),  # forgot WALK_STACK, no exemption
    )
    problems = check_rule_contract(table)
    assert any("b" in p and "walk_stack" in p.lower() for p in problems), problems


def test_an_exemption_without_a_reason_is_a_violation() -> None:
    table = (
        _rule("a", Force.WALK_STACK),
        _rule("b", Force.FLOOR, exempt={Force.WALK_STACK: "  "}),
    )
    assert check_rule_contract(table)


def test_a_documented_exemption_satisfies_the_contract() -> None:
    table = (
        _rule("a", Force.WALK_STACK),
        _rule("b", Force.FLOOR, exempt={Force.WALK_STACK: "disjoint by construction"}),
    )
    assert check_rule_contract(table) == []


def test_standing_down_and_exempting_the_same_force_is_a_violation() -> None:
    table = (
        _rule("a", Force.WALK_STACK),
        _rule("b", Force.FLOOR, down=[Force.WALK_STACK], exempt={Force.WALK_STACK: "x"}),
    )
    assert check_rule_contract(table)


def test_standing_down_on_a_later_force_is_a_violation() -> None:
    table = (
        _rule("a", Force.WALK_STACK, down=[Force.FLOOR]),
        _rule("b", Force.FLOOR),
    )
    assert check_rule_contract(table)


def test_duplicate_rule_names_are_a_violation() -> None:
    assert check_rule_contract((_rule("a", Force.WALK_STACK), _rule("a", Force.FLOOR)))


def test_the_recorded_exemptions_are_exactly_the_documented_three() -> None:
    exempt = {r.name: sorted(f.value for f in r.exempt) for r in RULES if r.exempt}
    assert exempt == {
        "walk_stack": ["forced_pivot_recollect"],
        "floor": ["forced_pivot_recollect"],
        "reread": ["forced_pivot_recollect"],
    }
    for rule in RULES:
        for reason in rule.exempt.values():
            assert len(reason.strip()) > 20


# --- the runtime check ---------------------------------------------------------------------------------------


def test_fired_forces_reads_the_flag_keys() -> None:
    data = {"forced_floor": True, "forced_replan": False, "forced_walk_stack": 1, "other": True}
    assert _fired_forces(data) == frozenset({Force.FLOOR, Force.WALK_STACK})


@pytest.mark.parametrize("data", [None, {}, "x", 5])
def test_fired_forces_of_nothing_is_empty(data) -> None:  # type: ignore[no-untyped-def]
    assert _fired_forces(data) == frozenset()


@pytest.mark.parametrize("rule", RULES, ids=lambda r: r.name)
def test_a_rule_stands_down_on_each_earlier_force_it_lists_and_only_those(rule: Rule) -> None:
    def down(fired):  # type: ignore[no-untyped-def]
        return _stands_down(
            rule.name,
            {f.value: True for f in fired},
            is_defer=False,
            absence_forced=False,
            d6_fired=False,
        )

    assert down(set()) is False
    for force in Force:
        expected = force in rule.stands_down_on
        assert down({force}) is expected, (rule.name, force)


@pytest.mark.parametrize("rule", RULES, ids=lambda r: r.name)
@pytest.mark.parametrize(
    "state",
    [
        {"is_defer": True},
        {"absence_forced": True},
        {"d6_fired": True},
    ],
)
def test_every_rule_stands_down_on_a_defer_absence_or_d6_turn(rule: Rule, state) -> None:  # type: ignore[no-untyped-def]
    kwargs = {"is_defer": False, "absence_forced": False, "d6_fired": False, **state}
    assert _stands_down(rule.name, {}, **kwargs) is True


def test_an_unknown_rule_name_fails_closed() -> None:
    assert _stands_down("nope", {}, is_defer=False, absence_forced=False, d6_fired=False) is True


# --- the cascade uses the table, structurally ---------------------------------------------------------------------


def _cascade_source() -> str:
    return inspect.getsource(forcing.apply_post_reason_cascade)


#: Payload keys the smallest-fetch rule stamps beside its flag. They carry data (the
#: fetch kind and the gap id), not a force, so they stay string keys.
_PAYLOAD_KEYS = {"forced_smallest_fetch_kind", "forced_smallest_fetch_gap_id"}


def test_the_cascade_holds_no_hand_written_flag_guards_or_flag_writes() -> None:
    source = _cascade_source()
    assert not re.findall(r'_reason_data\.get\(\s*"forced_', source), "guards must use the table"
    writes = re.findall(r'_reason_data\[\s*"(forced_[a-z_]+)"\s*\]', source)
    assert set(writes) <= _PAYLOAD_KEYS, f"flag writes must use Force members: {set(writes)}"


def test_the_cascade_consults_the_table_once_per_rule_in_table_order() -> None:
    names = re.findall(r'_down\(\s*"([a-z_]+)"\s*\)', _cascade_source())
    assert names == [r.name for r in RULES]


def test_every_flag_the_cascade_writes_is_a_force_member() -> None:
    written = set(re.findall(r"_reason_data\[Force\.([A-Z_]+)\.value\]", _cascade_source()))
    assert written, "premise: the cascade writes flags through Force"
    assert written <= {f.name for f in Force}


# --- turn-state: the D6 flag and the state agree --------------------------------------------------------------------


def _state(reason, **over):  # type: ignore[no-untyped-def]
    base = PostReasonCascadeState(
        reason_data=reason,
        action_history=[],
        evidence_banked_total=5,
        triage_route="BUG",
        description="where is the order filter implemented",
        knowledge_seed={},
        max_iterations=40,
        iteration=3,
        prev_focus={
            "surface": "svc/old.py",
            "surface_kind": "file",
            "line_start": None,
            "line_end": None,
        },
        is_defer=False,
        d6_fired=False,
        absence_forced=False,
        counts={},
        formulated_count=1,
        tenant_id="t",
        investigation_id="i",
    )
    for key, value in over.items():
        setattr(base, key, value)
    return base


def test_the_d6_flag_alone_freezes_the_cascade_like_the_d6_state() -> None:
    """Production sets both together; the table makes them one definition."""
    proposal = {"next_action": "GET_CODE", "action_parameters": {"file_path": "svc/new.py"}}
    by_state = apply_post_reason_cascade(_state(dict(proposal), d6_fired=True))
    by_flag = apply_post_reason_cascade(_state({**proposal, "forced_not_checkable": True}))
    assert not by_state.reason_data.get("forced_pivot_recollect")
    assert not by_flag.reason_data.get("forced_pivot_recollect")


# --- forced_requery: one meaning per key ----------------------------------------------------------------------------


def _carrier():  # type: ignore[no-untyped-def]
    return {
        "kind": "work_order",
        "order_kind": "REQUERY",
        "iteration": 1,
        "turn_issued": 1,
        "claim_ref": "abc",
        "query": "SampleOrderFilter",
        "scope": "",
        "max_attempts": 3,
        "consumed": False,
        "order": {
            "work_order_id": "00000000-0000-0000-0000-000000000001",
            "kind": "REQUERY",
            "action_type": "SEARCH_CODE",
            "action_parameters": {"query": "SampleOrderFilter"},
            "addresses": {
                "triage_route": "BUG",
                "claim_kind": "untyped",
                "has_unbeaten_rival": False,
            },
            "cost_estimate": 1,
            "max_attempts": 3,
            "seed_source": "test:carrier",
            "status": "PENDING",
        },
    }


def _turn(iteration, **extra):  # type: ignore[no-untyped-def]
    return {"iteration": iteration, "action": "SEARCH_CODE", "evidence_added": 0, **extra}


STAMP = {"claim_ref": "abc", "query": "SampleOrderFilter", "scope": "", "turn": 2}


def test_a_requery_consumed_stamp_retires_the_carrier() -> None:
    carrier = _carrier()
    history = [carrier, _turn(2, requery_consumed=dict(STAMP))]
    assert directive_consumed(carrier, history, []) is True


def test_the_old_dict_under_forced_requery_is_no_longer_read() -> None:
    carrier = _carrier()
    history = [carrier, _turn(2, forced_requery=dict(STAMP))]
    assert directive_consumed(carrier, history, []) is False


def test_a_bool_forced_requery_is_a_flag_not_a_consumption_stamp() -> None:
    carrier = _carrier()
    assert directive_consumed(carrier, [carrier, _turn(2, forced_requery=True)], []) is False
    assert directive_consumed(carrier, [carrier, _turn(2, forced_requery=False)], []) is False


def test_a_stamp_for_a_different_claim_does_not_retire_the_carrier() -> None:
    carrier = _carrier()
    other = {**STAMP, "claim_ref": "zzz"}
    assert directive_consumed(carrier, [carrier, _turn(2, requery_consumed=other)], []) is False


def test_a_non_dict_stamp_is_ignored() -> None:
    carrier = _carrier()
    for bad in (True, "x", 5, None, [STAMP]):
        assert directive_consumed(carrier, [carrier, _turn(2, requery_consumed=bad)], []) is False


def test_the_workflow_writes_a_bool_flag_and_a_separate_consumption_stamp() -> None:
    source = inspect.getsource(run_investigation)
    assert '"forced_requery": False' not in source, "the history key is a bool flag, not False/dict"
    assert '"forced_requery": bool(_reason_data.get("forced_requery"' in source
    assert 'action_history[-1]["requery_consumed"] = {' in source
    assert 'action_history[-1]["forced_requery"] = {' not in source
