"""WP-B3 typed rules + semantic validators: frozen-registry tests.

Covers the registry contract: closed membership, stable digests,
kind routing with unsupported abstention, DEFINITION discharge rules,
CALL_EDGE K1 forgery rejection, and the HEURISTIC never-discharges
invariant.
"""

from __future__ import annotations

import pytest

from investigation_agent_platform.domain.hypothesis.models import ClaimKind
from investigation_agent_platform.domain.proof.rules import (
    RULE_BUNDLE_VERSION,
    RULE_REGISTRY,
    ProofRule,
    RuleOutcome,
    Soundness,
    UnknownRule,
    apply_rule,
    get_rule,
    rule_for_kind,
)


def _range(symbol: str, window: str, path: str = "svc.py") -> dict:
    return {
        "premise_type": "RANGE_READ_FACT",
        "file_path": path,
        "window_text": window,
        "symbol": symbol,
    }


def _callsite(caller: str, callee: str, text: str) -> dict:
    return {
        "premise_type": "NORMALIZED_CALLSITE_FACT",
        "caller": caller,
        "callee": callee,
        "call_text": text,
        "file_path": "svc.py",
    }


def test_registry_is_closed_and_versioned() -> None:
    assert set(RULE_REGISTRY) == {
        "definition/v1",
        "call_edge/v1",
        "behavior/v1",
        "absence/v1",
        "history/v1",
    }
    assert RULE_BUNDLE_VERSION == "rules/v2"
    rule = get_rule("definition/v1")
    assert rule.rule_version == "v1"
    assert rule.soundness == Soundness.SOUND
    assert rule.implementation_digest.startswith("sha256:")
    with pytest.raises(UnknownRule):
        get_rule("causal/v1")


def test_contract_digests_are_stable() -> None:
    first = {rid: r.implementation_digest for rid, r in RULE_REGISTRY.items()}
    import importlib

    import investigation_agent_platform.domain.proof.rules as rules_mod

    importlib.reload(rules_mod)
    assert {rid: r.implementation_digest for rid, r in rules_mod.RULE_REGISTRY.items()} == first


def test_kind_routing_abstains_where_unregistered() -> None:
    assert rule_for_kind(ClaimKind.DEFINITION).rule_id == "definition/v1"
    assert rule_for_kind(ClaimKind.CALL_EDGE).rule_id == "call_edge/v1"
    assert rule_for_kind(ClaimKind.BEHAVIOR).rule_id == "behavior/v1"
    assert rule_for_kind(ClaimKind.ABSENCE).rule_id == "absence/v1"
    assert rule_for_kind(ClaimKind.HISTORY).rule_id == "history/v1"
    assert rule_for_kind(ClaimKind.CAUSAL) is None
    assert rule_for_kind("DEFINITION").rule_id == "definition/v1"
    assert rule_for_kind(None) is None


def test_definition_discharges_on_symbol_showing_range_read() -> None:
    rule = get_rule("definition/v1")
    outcome, _ = apply_rule(
        rule, {"symbol": "OrderService"}, [_range("OrderService", "class OrderService:")]
    )
    assert outcome == RuleOutcome.DISCHARGED


def test_definition_rejects_presence_and_symbol_free_windows() -> None:
    rule = get_rule("definition/v1")
    outcome, _ = apply_rule(
        rule,
        {"symbol": "OrderService"},
        [{"premise_type": "SEARCH_HIT", "file_path": "svc.py", "window_text": "OrderService"}],
    )
    assert outcome == RuleOutcome.INADMISSIBLE_PREMISE
    outcome, _ = apply_rule(
        rule, {"symbol": "OrderService"}, [_range("OrderService", "class Other:")]
    )
    assert outcome == RuleOutcome.INVALID_INFERENCE
    outcome, _ = apply_rule(rule, {"symbol": "OrderService"}, [])
    assert outcome == RuleOutcome.MISSING_PREMISE
    outcome, _ = apply_rule(
        rule, {"symbol": "OrderService"}, [_range("OrderService", "class X:"), _range("OrderService", "class Y:")]
    )
    assert outcome == RuleOutcome.MISSING_PREMISE


def test_call_edge_discharges_on_bound_shown_callsite() -> None:
    rule = get_rule("call_edge/v1")
    outcome, _ = apply_rule(
        rule,
        {"caller": "AuthFilter", "callee": "OrderService"},
        [_callsite("AuthFilter", "OrderService", "AuthFilter.check(); OrderService.create()")],
    )
    assert outcome == RuleOutcome.DISCHARGED


def test_call_edge_rejects_relabel_forgery_k1() -> None:
    rule = get_rule("call_edge/v1")
    forged = {
        "premise_type": "SEARCH_HIT",
        "caller": "AuthFilter",
        "callee": "OrderService",
        "call_text": "AuthFilter.check(); OrderService.create()",
        "file_path": "svc.py",
    }
    outcome, reason = apply_rule(
        rule, {"caller": "AuthFilter", "callee": "OrderService"}, [forged]
    )
    assert outcome == RuleOutcome.INADMISSIBLE_PREMISE
    comment = dict(forged)
    comment["premise_type"] = "COMMENT"
    outcome, _ = apply_rule(
        rule, {"caller": "AuthFilter", "callee": "OrderService"}, [comment]
    )
    assert outcome == RuleOutcome.INADMISSIBLE_PREMISE


def test_call_edge_rejects_unbound_or_unshown_edges() -> None:
    rule = get_rule("call_edge/v1")
    outcome, _ = apply_rule(rule, {"caller": "A", "callee": "B"}, [])
    assert outcome == RuleOutcome.MISSING_PREMISE
    outcome, _ = apply_rule(rule, {"caller": "A"}, [_callsite("A", "B", "A(); B()")])
    assert outcome == RuleOutcome.MISSING_PREMISE
    outcome, _ = apply_rule(
        rule, {"caller": "A", "callee": "B"}, [_callsite("A", "C", "A(); C()")]
    )
    assert outcome == RuleOutcome.INVALID_INFERENCE


def test_heuristic_rules_never_discharge() -> None:
    heuristic = ProofRule(
        rule_id="rank/v1",
        rule_version="v1",
        input_types=["RANGE_READ_FACT"],
        soundness=Soundness.HEURISTIC,
    )
    outcome, _ = apply_rule(
        heuristic, {"symbol": "S"}, [_range("S", "class S:")]
    )
    assert outcome == RuleOutcome.UNSUPPORTED_CONSTRUCT


def test_contract_digest_detects_validator_change() -> None:
    from investigation_agent_platform.domain.proof.rules import _contract_digest

    rule = RULE_REGISTRY["definition/v1"]
    base = {
        "rule_id": rule.rule_id,
        "rule_version": rule.rule_version,
        "input_types": list(rule.input_types),
        "preconditions": list(rule.preconditions),
        "assumptions": list(rule.assumptions),
        "soundness": rule.soundness,
    }
    same = _contract_digest(validator=rule.validator, **base)
    assert same == rule.implementation_digest
    swapped = _contract_digest(validator="domain.proof.rules:validate_call_edge_premises", **base)
    assert swapped != same
    assert swapped.startswith("sha256:")


def test_unknown_validator_binding_fails_closed() -> None:
    rule = get_rule("definition/v1").model_copy(update={"validator": "elsewhere:other"})
    outcome, _ = apply_rule(
        rule, {"symbol": "S"}, [_range("S", "class S:")]
    )
    assert outcome == RuleOutcome.INADMISSIBLE_PREMISE
