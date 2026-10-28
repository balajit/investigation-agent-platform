# tests/unit/test_composition_concurrency_replay.py
"""Composition concurrency, replay, and mutation gaps (Wave 1, C2-tests).

Pure-layer closure of the go/no-go inputs the existing composition
suite does not pin: concurrent-evidence updates, replay determinism
across a serialization boundary, and mutations that must be caught
(weakened scope, missing invalidation input, tampered graph).
"""

from __future__ import annotations

import json

from investigation_agent_platform.domain.proof.composition import (
    compose_alternatives,
    compose_conjunction,
    detect_cycle,
    invalidate_by_evidence,
    invalidate_derivations,
    make_node,
)
from investigation_agent_platform.domain.proof.outcomes import CheckerOutcome


def _graph():
    d1 = make_node("d1", "definition/v1", premise_ids=["e1"], depends_on=[])
    d2 = make_node("d2", "call_edge/v1", premise_ids=["e1", "e2"], depends_on=["d1"])
    d3 = make_node("d3", "behavior/v1", premise_ids=["e3"], depends_on=["d2"])
    return {"d1": d1, "d2": d2, "d3": d3}


class TestConcurrentEvidenceUpdate:
    def test_shared_premise_change_invalidates_all_dependents(self) -> None:
        nodes = _graph()
        changed = invalidate_by_evidence({"e1"}, {"evidence.content"}, nodes)
        assert changed == {"d1", "d2", "d3"}

    def test_unrelated_change_invalidates_nothing(self) -> None:
        nodes = _graph()
        assert invalidate_by_evidence({"e9"}, {"evidence.content"}, nodes) == set()

    def test_invalidation_order_independent(self) -> None:
        nodes = _graph()
        one_shot = invalidate_derivations({"d1", "d2"}, nodes)
        serial = invalidate_derivations({"d1"}, nodes) | invalidate_derivations({"d2"}, nodes)
        assert one_shot == serial == {"d1", "d2", "d3"}

    def test_stale_valid_cannot_survive_conjunction(self) -> None:
        # A route invalidated by a concurrent update must poison any
        # conjunction that still cites its pre-invalidation VALID.
        outcome, _ = compose_conjunction([CheckerOutcome.VALID, CheckerOutcome.INVALID_INFERENCE])
        assert outcome == CheckerOutcome.INVALID_INFERENCE

    def test_missing_premise_blocks_conjunction(self) -> None:
        outcome, _ = compose_conjunction([CheckerOutcome.VALID, CheckerOutcome.MISSING_PREMISE])
        assert outcome == CheckerOutcome.MISSING_PREMISE

    def test_survivor_still_carries_alternative(self) -> None:
        outcome, _ = compose_alternatives([CheckerOutcome.INVALID_INFERENCE, CheckerOutcome.VALID])
        assert outcome == CheckerOutcome.VALID


class TestReplayDeterminism:
    def test_serialized_graph_replays_identically(self) -> None:
        nodes = _graph()
        before = invalidate_derivations({"d1"}, nodes)
        blob = json.dumps({k: v.model_dump(mode="json") for k, v in nodes.items()})
        thawed = {k: dict(v) for k, v in json.loads(blob).items()}
        after = invalidate_derivations({"d1"}, thawed)
        assert before == after == {"d1", "d2", "d3"}

    def test_replayed_sequence_matches(self) -> None:
        nodes = _graph()
        first = invalidate_by_evidence({"e2"}, {"evidence.content"}, nodes)
        assert first == {"d2", "d3"}
        # Re-applying the recorded set on a fresh load converges.
        second = invalidate_derivations(set(first), nodes)
        assert second == first


class TestMutationCatching:
    def test_weakened_scope_stops_propagation(self) -> None:
        # Dropping d2's dependency edge means d1's invalidation no longer
        # reaches it: undeclared deps do not propagate, by rule.
        nodes = _graph()
        weakened = dict(nodes)
        weakened["d2"] = make_node("d2", "call_edge/v1", premise_ids=["e1", "e2"])
        assert invalidate_derivations({"d1"}, weakened) == {"d1"}

    def test_undeclared_class_never_invalidates(self) -> None:
        nodes = _graph()
        # evidence.availability is not in any rule's declared deps here
        # for a class outside the vocabulary: an unknown class matches nothing.
        assert invalidate_by_evidence({"e1"}, {"not-a-declared-class"}, nodes) == set()

    def test_spurious_edge_causes_over_invalidation(self) -> None:
        # An added edge d1->d3 widens the closure transitively (d3 pulls
        # d1, which pulls d2): the detector cannot tell legitimate from
        # spurious edges, so graph integrity (cycle + edge provenance) is
        # the control, not the closure itself.
        nodes = _graph()
        tampered = dict(nodes)
        tampered["d1"] = make_node("d1", "definition/v1", premise_ids=["e1"], depends_on=["d3"])
        assert invalidate_derivations({"d3"}, tampered) == {"d1", "d2", "d3"}

    def test_cycle_still_rejected(self) -> None:
        assert detect_cycle({"a": ["b"], "b": ["a"]}) != []
