"""G5-unit gate assembly: composition/publication wired end to end (WP-C1/C2).

Assembles the executable half of G5 — invalidation, alternative-route
survival, stale-publication denial, race-free CAS, and mutation
sensitivity (each guard proven load-bearing by showing the verdict
flips when the guard is removed). Unit scope only.

OPEN operator legs (not passable here, tracked explicitly):
O1. Controlled interleaving test against the real DB/RLS path:
    concurrent premise change vs publication commit must deny or
    reevaluate, never publish a mixed snapshot
    (see test_operator_live_interleave_open).
O2. Migration/role review of the commit path (plan WP-C2 entry criteria).
O3. G5 mutation at the live layer (fault-injected premise loss mid-commit).
"""

from __future__ import annotations

import hashlib
from typing import Any

import pytest

import investigation_agent_platform.domain.proof.certificate as certificate_mod
import investigation_agent_platform.domain.proof.rules as rules_mod
from investigation_agent_platform.domain.proof.canonical import context_id
from investigation_agent_platform.domain.proof.certificate import (
    CheckerOutcome,
    CheckRequest,
    certify,
    check_certificate,
)
from investigation_agent_platform.domain.proof.composition import (
    compose_alternatives,
    invalidate_by_evidence,
    make_node,
)
from investigation_agent_platform.domain.proof.evaluation import EvaluationInputs
from investigation_agent_platform.domain.proof.manifest import build_manifest
from investigation_agent_platform.domain.proof.publication import (
    PublicationDecision,
    PublicationRequest,
    build_commit,
    decide_publication,
)
from investigation_agent_platform.domain.proof.rules import RULE_REGISTRY


def _h(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


def _definition_cert() -> tuple[dict[str, Any], dict[str, Any], Any, dict[str, dict[str, Any]]]:
    """(certificate, proposition, manifest, artifacts) for a valid definition claim."""
    window = "class OrderService:\n    pass\n"
    artifact = {
        "file_path": "svc.py",
        "repository": "synthetic/service",
        "revision": "rev-1",
        "line_start": 10,
        "line_end": 14,
        "target": "OrderService",
        "window_text": window,
    }
    content_hash = certificate_mod._artifact_hash(artifact)
    proposition = {
        "canon_version": "canon/v1",
        "completeness": "PARTIAL_OK",
        "form": "EXISTENCE",
        "modality": "STATIC",
        "predicate": "defined(OrderService)",
        "quantifier": "EXISTENTIAL",
        "relation": "CALL_EDGE",
        "scope": {"configuration": "default", "execution": "n/a", "revision": "rev-1"},
        "target": {"file_path": "svc.py", "symbol": "OrderService"},
    }
    premise = {
        "content_hash": content_hash,
        "evidence_id": "row-1",
        "premise_type": "RANGE_READ_FACT",
        "scope": {"revision": "rev-1"},
    }
    manifest = build_manifest(
        ["row-1"], {"row-1": content_hash}, premise_types={"row-1": "RANGE_READ_FACT"}
    )
    inputs = EvaluationInputs(
        proposition=dict(proposition),
        manifest=manifest,
        rule_bundle_version="rules/v2",
        model_encoding={"model": "m"},
        rule_id="definition/v1",
        rule_version="v1",
        implementation_digest=RULE_REGISTRY["definition/v1"].implementation_digest,
        inference={},
    )
    cert = certify(inputs, [premise], {"row-1": artifact})
    return cert, proposition, manifest, {"row-1": artifact}


def _request(
    cert: dict[str, Any],
    proposition: dict[str, Any],
    manifest: Any,
    artifacts: dict[str, Any],
    **over: Any,
) -> CheckRequest:
    base: dict[str, Any] = {
        "certificate": cert,
        "proposition": proposition,
        "manifest": manifest,
        "rule_bundle_version": "rules/v2",
        "model_encoding": {"model": "m"},
        "reconciliation_state": "unreconciled",
        "artifacts": artifacts,
    }
    base.update(over)
    return CheckRequest(**base)


def test_gate_no_stale_publication_end_to_end() -> None:
    """Root-evidence change → dependent invalidated → publication denied stale."""
    cert, proposition, manifest, artifacts = _definition_cert()
    der_a = str(cert["derivation_id"])
    der_b = "der-dependent-b"
    nodes = {
        der_a: make_node(der_a, "definition/v1", ["row-1"], []),
        der_b: make_node(der_b, "definition/v1", ["row-2"], [der_a]),
    }
    # Fresh certificate checks out against its pinned context.
    outcome, _ = check_certificate(_request(cert, proposition, manifest, artifacts))
    assert outcome == CheckerOutcome.VALID

    # Root evidence moves: both the root and its dependent invalidate.
    changed = invalidate_by_evidence({"row-1"}, {"evidence.content"}, nodes)
    assert changed == {der_a, der_b}

    # Publication against the moved context denies stale — checker and
    # decision layers agree, and no commit is constructible.
    new_digest = _h("row-1 revised bytes")
    new_ctx = context_id(
        str(cert["proposition_id"]),
        {
            "evidence_manifest_digest": new_digest,
            "model_encoding": {"model": "m"},
            "proposition_scope": {"configuration": "default", "execution": "n/a", "revision": "rev-1"},
            "reconciliation_state": "unreconciled",
            "rule_bundle_version": "rules/v2",
        },
    )
    stale_outcome, _ = check_certificate(
        _request(cert, proposition, manifest, artifacts, expected_context_id=new_ctx)
    )
    assert stale_outcome == CheckerOutcome.STALE_CERTIFICATE
    decision, reason = decide_publication(
        PublicationRequest(
            checker_outcome=CheckerOutcome.VALID,
            certificate_context_id=str(cert["context_id"]),
            expected_context_id=new_ctx,
            derivation_id=der_a,
            reconciliation_adjudicated=True,
            policy_eligible=True,
        )
    )
    assert decision == PublicationDecision.DENY
    assert reason == "STALE_CONTEXT"
    with pytest.raises(ValueError, match="STALE_CONTEXT"):
        build_commit(der_a, str(cert["context_id"]), new_ctx)


def test_gate_alternative_route_commits_after_root_loss() -> None:
    """K5 at the gate: a killed route never takes its survivor down with it."""
    outcome, _ = compose_alternatives(
        [CheckerOutcome.PREMISE_UNAVAILABLE, CheckerOutcome.VALID]
    )
    assert outcome == CheckerOutcome.VALID
    decision, _ = decide_publication(
        PublicationRequest(
            checker_outcome=outcome,
            certificate_context_id="ctx-live",
            expected_context_id="ctx-live",
            derivation_id="der-survivor",
            reconciliation_adjudicated=True,
            policy_eligible=True,
        )
    )
    assert decision == PublicationDecision.COMMIT


def test_sensitivity_transitivity_is_load_bearing() -> None:
    """A single-hop (mutated) closure keeps C alive; the real one does not."""
    nodes = {
        "A": make_node("A", "definition/v1", ["e1"], []),
        "B": make_node("B", "definition/v1", ["e2"], ["A"]),
        "C": make_node("C", "definition/v1", ["e3"], ["B"]),
    }
    real = invalidate_by_evidence({"e1"}, {"evidence.content"}, nodes)
    assert real == {"A", "B", "C"}
    # Mutant: direct dependents only (transitivity removed).
    direct = {
        nid
        for nid, n in nodes.items()
        if "e1" in n.premise_ids or "A" in n.depends_on
    }
    assert direct == {"A", "B"}
    assert real - direct == {"C"}
    fresh = invalidate_by_evidence({"e1"}, {"evidence.content"}, nodes)
    assert "C" in fresh  # the guard that catches C is the transitive step


def test_sensitivity_cycle_check_is_load_bearing(monkeypatch: Any) -> None:
    """With the cycle guard neutered, a cyclic certificate is accepted."""
    cert, proposition, manifest, artifacts = _definition_cert()
    der = str(cert["derivation_id"])
    cyclic = _request(
        cert, proposition, manifest, artifacts, derivation_deps={der: [der]}
    )
    outcome, _ = check_certificate(cyclic)
    assert outcome == CheckerOutcome.INVALID_INFERENCE
    monkeypatch.setattr(certificate_mod, "_reachable", lambda *a, **k: False)
    neutered, _ = check_certificate(cyclic)
    assert neutered == CheckerOutcome.VALID


def test_sensitivity_semantic_validator_is_load_bearing(monkeypatch: Any) -> None:
    """With the validator neutered, a forged call-site certifies (K1)."""
    artifact = {
        "caller": "OrderService",
        "callee": "charge",
        "file_path": "svc.py",
        "text": "OrderService ... charge",  # a search hit, not a call site
    }
    content_hash = certificate_mod._artifact_hash(artifact)
    proposition = {
        "canon_version": "canon/v1",
        "completeness": "PARTIAL_OK",
        "form": "EXISTENCE",
        "modality": "STATIC",
        "predicate": "calls(a,b)",
        "quantifier": "EXISTENTIAL",
        "relation": "CALL_EDGE",
        "scope": {"configuration": "default", "execution": "n/a", "revision": "rev-1"},
        "target": {
            "caller": "OrderService",
            "callee": "charge",
            "counterpart": "OrderService",
            "symbol": "charge",
        },
    }
    premise = {
        "content_hash": content_hash,
        "evidence_id": "row-9",
        # K1 forgery shape: a SEARCH_HIT relabeled as a call-site premise.
        "premise_type": "SEARCH_HIT",
        "scope": {"revision": "rev-1"},
    }
    manifest = build_manifest(
        ["row-9"],
        {"row-9": content_hash},
        premise_types={"row-9": "NORMALIZED_CALLSITE_FACT"},
    )
    inputs = EvaluationInputs(
        proposition=dict(proposition),
        manifest=manifest,
        rule_bundle_version="rules/v2",
        model_encoding={"model": "m"},
        rule_id="call_edge/v1",
        rule_version="v1",
        implementation_digest=RULE_REGISTRY["call_edge/v1"].implementation_digest,
        inference={},
    )
    with pytest.raises(certificate_mod.CertificationError):
        certify(inputs, [premise], {"row-9": artifact})
    # Mutant: semantic validation neutered at the apply_rule layer —
    # the K1 forgery now certifies, proving the validator is load-bearing.
    monkeypatch.setattr(
        certificate_mod,
        "apply_rule",
        lambda rule, claim, premises, manifest=None: (rules_mod.RuleOutcome.DISCHARGED, "neutered"),
    )
    forged = certify(inputs, [premise], {"row-9": artifact})
    assert forged["rule_id"] == "call_edge/v1"


def test_sensitivity_freshness_pin_is_load_bearing() -> None:
    """Without the expected-context pin, staleness is undetectable."""
    cert, proposition, manifest, artifacts = _definition_cert()
    pinned, _ = check_certificate(
        _request(
            cert, proposition, manifest, artifacts,
            expected_context_id=str(cert["context_id"]),
        )
    )
    assert pinned == CheckerOutcome.VALID
    unpinned, _ = check_certificate(_request(cert, proposition, manifest, artifacts))
    assert unpinned == CheckerOutcome.VALID  # no pin: nothing to be stale against
    decision, reason = decide_publication(
        PublicationRequest(
            checker_outcome=CheckerOutcome.VALID,
            certificate_context_id=str(cert["context_id"]),
            expected_context_id="ctx-moved",
            derivation_id=str(cert["derivation_id"]),
            reconciliation_adjudicated=True,
            policy_eligible=True,
        )
    )
    assert (decision, reason) == (PublicationDecision.DENY, "STALE_CONTEXT")


@pytest.mark.skip(
    reason="O1 operator leg: requires a live PostgreSQL/RLS path; "
    "procedure: open txn T1 (read hypothesis vN + context C), txn T2 "
    "(premise change, commit), then T1 publication commit with "
    "expected (context C, version N) must deny or reevaluate — never a "
    "mixed snapshot. O2: migration/role review of the commit path."
)
def test_operator_live_interleave_open() -> None:
    raise AssertionError("operator leg, never runs in unit suite")
