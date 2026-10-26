"""WP-C1 composition + invalidation graph: K2/K5/K6/K14 + guards.

Conjunction needs every route; alternatives need one survivor; closure
is transitive over explicit edges only; undeclared and operational
changes never invalidate; cycles reject; duplicates reject.
"""

from __future__ import annotations

import pytest

from investigation_agent_platform.domain.proof.certificate import CheckerOutcome
from investigation_agent_platform.domain.proof.composition import (
    CompositionError,
    CompositionErrorCode,
    assert_acyclic,
    compose_alternatives,
    compose_conjunction,
    detect_cycle,
    invalidate_by_evidence,
    invalidate_derivations,
    make_node,
)

V = CheckerOutcome.VALID


def _chain() -> dict:
    return {
        "A": make_node("A", "definition/v1", ["ev-1"], []),
        "B": make_node("B", "definition/v1", ["ev-2"], ["A"]),
        "C": make_node("C", "call_edge/v1", ["ev-3"], ["B"]),
        "D": make_node("D", "definition/v1", ["ev-9"], []),
    }


def test_conjunction_all_valid() -> None:
    outcome, _ = compose_conjunction([V, V])
    assert outcome == V


def test_conjunction_first_failure_wins() -> None:
    outcome, _ = compose_conjunction([V, CheckerOutcome.INVALID_INFERENCE, V])
    assert outcome == CheckerOutcome.INVALID_INFERENCE


def test_conjunction_empty_is_missing() -> None:
    outcome, _ = compose_conjunction([])
    assert outcome == CheckerOutcome.MISSING_PREMISE


def test_alternatives_survivor_carries_k5() -> None:
    outcome, _ = compose_alternatives(
        [CheckerOutcome.INVALID_INFERENCE, V, CheckerOutcome.MISSING_PREMISE]
    )
    assert outcome == V


def test_alternatives_all_fail_returns_first() -> None:
    outcome, _ = compose_alternatives(
        [CheckerOutcome.MISSING_PREMISE, CheckerOutcome.INVALID_INFERENCE]
    )
    assert outcome == CheckerOutcome.MISSING_PREMISE


def test_alternatives_empty_is_missing() -> None:
    outcome, _ = compose_alternatives([])
    assert outcome == CheckerOutcome.MISSING_PREMISE


def test_closure_transitive_unrelated_preserved_k6() -> None:
    invalidated = invalidate_derivations({"A"}, _chain())
    assert invalidated == {"A", "B", "C"}


def test_closure_mid_chain_invalidates_downstream_only() -> None:
    invalidated = invalidate_derivations({"B"}, _chain())
    assert invalidated == {"B", "C"}


def test_no_edge_no_invalidation_not_inferred() -> None:
    # B shares no edge with D: changing D never touches A/B/C.
    invalidated = invalidate_derivations({"D"}, _chain())
    assert invalidated == {"D"}


def test_evidence_closure_declared_class_invalidates() -> None:
    invalidated = invalidate_by_evidence({"ev-1"}, {"evidence.content"}, _chain())
    assert invalidated == {"A", "B", "C"}


def test_evidence_closure_undeclared_class_guards() -> None:
    assert invalidate_by_evidence({"ev-1"}, {"logging.config"}, _chain()) == set()
    assert invalidate_by_evidence({"ev-1"}, {"capability"}, _chain()) == set()


def test_evidence_closure_unknown_evidence_empty() -> None:
    assert invalidate_by_evidence({"ev-missing"}, {"evidence.content"}, _chain()) == set()


def test_cycle_rejects_k2() -> None:
    nodes = {
        "A": make_node("A", "definition/v1", ["ev-1"], ["B"]),
        "B": make_node("B", "definition/v1", ["ev-2"], ["A"]),
    }
    assert detect_cycle({"A": ["B"], "B": ["A"]}) != []
    with pytest.raises(CompositionError) as exc:
        assert_acyclic(nodes)
    assert exc.value.code == CompositionErrorCode.CYCLE.value


def test_self_edge_rejects() -> None:
    nodes = {"A": make_node("A", "definition/v1", ["ev-1"], ["A"])}
    with pytest.raises(CompositionError) as exc:
        assert_acyclic(nodes)
    assert exc.value.code == CompositionErrorCode.CYCLE.value


def test_acyclic_passes() -> None:
    assert_acyclic(_chain())
    assert detect_cycle({"A": ["B"], "B": ["C"], "C": []}) == []


def test_unknown_dependency_rejects_f_c1() -> None:
    # Closed-graph check: an edge to nothing is a caller bug, and
    # tolerating it would under-invalidate through the missing edge.
    nodes = {"A": make_node("A", "definition/v1", ["ev-1"], ["GHOST"])}
    with pytest.raises(CompositionError) as exc:
        assert_acyclic(nodes)
    assert exc.value.code == CompositionErrorCode.BAD_GRAPH.value


def test_duplicate_premise_rejects_k14() -> None:
    with pytest.raises(CompositionError) as exc:
        make_node("X", "definition/v1", ["ev-1", "ev-1"], [])
    assert exc.value.code == CompositionErrorCode.DUPLICATE_PREMISE.value


def test_unknown_rule_rejects() -> None:
    with pytest.raises(CompositionError) as exc:
        make_node("X", "causal/v1", ["ev-1"], [])
    assert exc.value.code == CompositionErrorCode.UNKNOWN_RULE.value
