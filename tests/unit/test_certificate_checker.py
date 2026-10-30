"""WP-B4 certificate serializer + independent checker: G4-adjacent tests.

Accept/reject/replay from schema + registry alone. Each rejection maps
to its K-suite fault: K1 forgery, K2 cycle, K3/K4 coverage/quantifier,
K9 digest, K10 missing artifact, K11 scope, K8/K15 staleness. Emitted
certificates validate against the Layer-1 schema harness; producer
validation is proven ignored.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from proof_kernel_ledger import validate_against_schema

from investigation_agent_platform.domain.proof.certificate import (
    CERT_SCHEMA_VERSION,
    CertificationError,
    CheckerOutcome,
    CheckRequest,
    _artifact_hash,
    certify,
    check_certificate,
)
from investigation_agent_platform.domain.proof.evaluation import EvaluationInputs
from investigation_agent_platform.domain.proof.manifest import (
    ManifestCoverage,
    build_manifest,
)

SCHEMA = json.loads((Path("tests/fixtures/proof_kernel") / "cert_schema_v2.json").read_text())


def _h(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


def _definition_setup() -> tuple[Any, Any, Any]:
    """(EvaluationInputs, schema premise entry, artifact record)."""
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
        "content_hash": _artifact_hash(artifact),
        "evidence_id": "row-1",
        "premise_type": "RANGE_READ_FACT",
        "scope": {"revision": "rev-1"},
    }
    manifest = build_manifest(
        ["row-1"],
        {"row-1": _artifact_hash(artifact)},
        premise_types={"row-1": "RANGE_READ_FACT"},
    )
    inputs = EvaluationInputs(
        proposition=dict(proposition),
        manifest=manifest,
        rule_bundle_version="rules/v2",
        model_encoding={"model": "m"},
        rule_id="definition/v1",
        rule_version="v1",
        implementation_digest=_impl_digest(),
        inference={},
    )
    return inputs, premise, artifact


def _impl_digest() -> str:
    from investigation_agent_platform.domain.proof.rules import RULE_REGISTRY

    return RULE_REGISTRY["definition/v1"].implementation_digest


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


def test_emitted_certificate_is_layer1_valid() -> None:
    inputs, premise, artifact = _definition_setup()
    cert = certify(inputs, [premise], {"row-1": artifact})
    assert validate_against_schema(cert, SCHEMA) == []
    assert cert["schema_version"] == CERT_SCHEMA_VERSION
    assert cert["validation"] == {
        "reasons": ["producer must not self-accept; checker decides"],
        "result": "REJECT",
    }


def test_valid_certificate_accepts_and_replays() -> None:
    inputs, premise, artifact = _definition_setup()
    cert = certify(inputs, [premise], {"row-1": artifact})
    req = _request(cert, inputs.proposition, inputs.manifest, {"row-1": artifact})
    first = check_certificate(req)
    assert first == (CheckerOutcome.VALID, [])
    assert check_certificate(req) == first


def test_k1_forgery_rejected_and_producer_validation_ignored() -> None:
    inputs, premise, artifact = _definition_setup()
    cert = certify(inputs, [premise], {"row-1": artifact})
    forged = dict(cert["premises"][0])
    forged["premise_type"] = "SEARCH_HIT"
    cert["premises"] = [forged]
    cert["validation"] = {"result": "ACCEPT", "reasons": ["producer says so"]}
    req = _request(cert, inputs.proposition, inputs.manifest, {"row-1": artifact})
    outcome, reasons = check_certificate(req)
    assert outcome == CheckerOutcome.INADMISSIBLE_PREMISE
    assert reasons


def test_k9_digest_and_version_mismatch_rejected() -> None:
    inputs, premise, artifact = _definition_setup()
    cert = certify(inputs, [premise], {"row-1": artifact})
    cert["implementation_digest"] = "sha256:" + "1" * 64
    req = _request(cert, inputs.proposition, inputs.manifest, {"row-1": artifact})
    assert check_certificate(req)[0] == CheckerOutcome.INVALID_INFERENCE
    cert = certify(inputs, [premise], {"row-1": artifact})
    cert["rule_version"] = "v9"
    req = _request(cert, inputs.proposition, inputs.manifest, {"row-1": artifact})
    assert check_certificate(req)[0] == CheckerOutcome.INVALID_INFERENCE


def test_k10_missing_or_mismatched_artifact_unavailable() -> None:
    inputs, premise, artifact = _definition_setup()
    cert = certify(inputs, [premise], {"row-1": artifact})
    req = _request(cert, inputs.proposition, inputs.manifest, {})
    assert check_certificate(req)[0] == CheckerOutcome.PREMISE_UNAVAILABLE
    req = _request(
        cert,
        inputs.proposition,
        inputs.manifest,
        {"row-1": {"file_path": "svc.py", "symbol": "OrderService", "text": "tampered bytes"}},
    )
    assert check_certificate(req)[0] == CheckerOutcome.PREMISE_UNAVAILABLE


def test_k11_scope_revision_mismatch_rejected() -> None:
    inputs, premise, artifact = _definition_setup()
    premise["scope"] = {"revision": "rev-2"}
    cert = certify(inputs, [premise], {"row-1": artifact})
    req = _request(cert, inputs.proposition, inputs.manifest, {"row-1": artifact})
    assert check_certificate(req)[0] == CheckerOutcome.SCOPE_MISMATCH


def test_k2_cycle_rejected_acyclic_passes() -> None:
    inputs, premise, artifact = _definition_setup()
    cert = certify(inputs, [premise], {"row-1": artifact})
    der = cert["derivation_id"]
    req = _request(
        cert,
        inputs.proposition,
        inputs.manifest,
        {"row-1": artifact},
        derivation_deps={der: [der]},
    )
    assert check_certificate(req)[0] == CheckerOutcome.INVALID_INFERENCE
    req = _request(
        cert,
        inputs.proposition,
        inputs.manifest,
        {"row-1": artifact},
        derivation_deps={"other": [der]},
    )
    assert check_certificate(req)[0] == CheckerOutcome.VALID


def test_k2_multinode_cycle_rejected() -> None:
    inputs, premise, artifact = _definition_setup()
    cert = certify(inputs, [premise], {"row-1": artifact})
    der = cert["derivation_id"]
    req = _request(
        cert,
        inputs.proposition,
        inputs.manifest,
        {"row-1": artifact},
        derivation_deps={"a": [der], der: ["b"], "b": ["a"]},
    )
    assert check_certificate(req)[0] == CheckerOutcome.INVALID_INFERENCE
    req = _request(
        cert,
        inputs.proposition,
        inputs.manifest,
        {"row-1": artifact},
        derivation_deps={"a": [der], "b": ["a"]},
    )
    assert check_certificate(req)[0] == CheckerOutcome.VALID


def test_k8_k15_stale_context_rejected() -> None:
    inputs, premise, artifact = _definition_setup()
    cert = certify(inputs, [premise], {"row-1": artifact})
    req = _request(
        cert,
        inputs.proposition,
        inputs.manifest,
        {"row-1": artifact},
        expected_context_id="sha256:" + "0" * 64,
    )
    assert check_certificate(req)[0] == CheckerOutcome.STALE_CERTIFICATE
    req = _request(
        cert,
        inputs.proposition,
        inputs.manifest,
        {"row-1": artifact},
        expected_context_id=cert["context_id"],
    )
    assert check_certificate(req)[0] == CheckerOutcome.VALID


def test_k3_unbounded_absence_refuses_certification() -> None:
    inputs, premise, artifact = _definition_setup()
    proposition = dict(inputs.proposition)
    proposition["form"] = "ABSENCE"
    bad = EvaluationInputs(
        proposition=proposition,
        manifest=inputs.manifest,
        rule_bundle_version="rules/v2",
        model_encoding={"model": "m"},
        rule_id="definition/v1",
        rule_version="v1",
        implementation_digest=_impl_digest(),
    )
    with pytest.raises(CertificationError):
        certify(bad, [premise], {"row-1": artifact})


def test_k3_coverage_boundary_missing_incomplete_empty_rejected() -> None:
    inputs, premise, artifact = _definition_setup()
    proposition = dict(inputs.proposition)
    proposition["form"] = "ABSENCE"
    complete_manifest = build_manifest(
        ["row-1"],
        {"row-1": _artifact_hash(artifact)},
        coverage=ManifestCoverage.COMPLETE_EMPTY,
        premise_types={"row-1": "RANGE_READ_FACT"},
    )
    complete_inputs = EvaluationInputs(
        proposition=proposition,
        manifest=complete_manifest,
        rule_bundle_version="rules/v2",
        model_encoding={"model": "m"},
        rule_id="definition/v1",
        rule_version="v1",
        implementation_digest=_impl_digest(),
    )
    cert = certify(complete_inputs, [premise], {"row-1": artifact}, universe="repo@rev:/:query=q")
    del cert["coverage"]
    req = _request(cert, proposition, complete_manifest, {"row-1": artifact})
    assert check_certificate(req)[0] == CheckerOutcome.MISSING_PREMISE
    cert = certify(complete_inputs, [premise], {"row-1": artifact}, universe="repo@rev:/:query=q")
    cert["premises"] = []
    req = _request(cert, proposition, complete_manifest, {"row-1": artifact})
    assert check_certificate(req)[0] == CheckerOutcome.MISSING_PREMISE
    inputs, premise, artifact = _definition_setup()
    proposition = dict(inputs.proposition)
    proposition["form"] = "ABSENCE"
    incomplete_manifest = build_manifest(
        ["row-1"],
        {"row-1": _artifact_hash(artifact)},
        coverage=ManifestCoverage.INCOMPLETE,
        premise_types={"row-1": "RANGE_READ_FACT"},
    )
    incomplete_inputs = EvaluationInputs(
        proposition=proposition,
        manifest=incomplete_manifest,
        rule_bundle_version="rules/v2",
        model_encoding={"model": "m"},
        rule_id="definition/v1",
        rule_version="v1",
        implementation_digest=_impl_digest(),
    )
    cert = certify(incomplete_inputs, [premise], {"row-1": artifact}, universe="repo@rev:/:query=q")
    req = _request(cert, proposition, incomplete_manifest, {"row-1": artifact})
    assert check_certificate(req)[0] == CheckerOutcome.MISSING_PREMISE
    complete_manifest = build_manifest(
        ["row-1"],
        {"row-1": _artifact_hash(artifact)},
        coverage=ManifestCoverage.COMPLETE_EMPTY,
        premise_types={"row-1": "RANGE_READ_FACT"},
    )
    complete_inputs = EvaluationInputs(
        proposition=proposition,
        manifest=complete_manifest,
        rule_bundle_version="rules/v2",
        model_encoding={"model": "m"},
        rule_id="definition/v1",
        rule_version="v1",
        implementation_digest=_impl_digest(),
    )
    cert = certify(complete_inputs, [premise], {"row-1": artifact}, universe="repo@rev:/:query=q")
    req = _request(cert, proposition, complete_manifest, {"row-1": artifact})
    assert check_certificate(req)[0] == CheckerOutcome.UNSUPPORTED_CONSTRUCT


def test_certify_refuses_non_discharging_artifact_mismatch_and_bad_premise() -> None:
    from investigation_agent_platform.domain.proof.certificate import CertificationError

    inputs, premise, artifact = _definition_setup()
    forged = dict(premise)
    forged["premise_type"] = "SEARCH_HIT"
    with pytest.raises(CertificationError) as excinfo:
        certify(inputs, [forged], {"row-1": artifact})
    assert excinfo.value.code == "RULE_NOT_DISCHARGED"
    with pytest.raises(CertificationError) as excinfo:
        certify(inputs, ["not-a-premise"], {"row-1": artifact})  # type: ignore[list-item]
    assert excinfo.value.code == "BAD_PREMISE"
    tampered = dict(artifact)
    tampered["window_text"] = "class SomethingElse:\n    pass\n"
    with pytest.raises(CertificationError) as excinfo:
        certify(inputs, [premise], {"row-1": tampered})
    assert excinfo.value.code == "PREMISE_UNAVAILABLE"


def _callsite_setup() -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any], Any]:
    from investigation_agent_platform.domain.proof.manifest import build_manifest

    texts = {
        "row-1": "AuthFilter.check(); OrderService.create()",
        "row-2": "if ready: AuthFilter.check(); OrderService.create()",
    }
    artifacts = {
        rid: {
            "caller": "AuthFilter",
            "callee": "OrderService",
            "call_text": text,
            "file_path": "svc.py",
            "repository": "synthetic/service",
            "revision": "rev-1",
            "line_start": 20,
            "line_end": 20,
        }
        for rid, text in texts.items()
    }
    proposition = {
        "canon_version": "canon/v1",
        "completeness": "PARTIAL_OK",
        "form": "EXISTENCE",
        "modality": "STATIC",
        "predicate": "calls(AuthFilter,OrderService)",
        "quantifier": "EXISTENTIAL",
        "relation": "CALL_EDGE",
        "scope": {"configuration": "default", "execution": "n/a", "revision": "rev-1"},
        "target": {"counterpart": "AuthFilter", "file_path": "svc.py", "symbol": "OrderService"},
    }
    premises = [
        {
            "content_hash": _artifact_hash(artifacts[rid]),
            "evidence_id": rid,
            "premise_type": "NORMALIZED_CALLSITE_FACT",
            "scope": {"revision": "rev-1"},
        }
        for rid in ("row-1", "row-2")
    ]
    manifest = build_manifest(
        ["row-1", "row-2"],
        {rid: _artifact_hash(artifacts[rid]) for rid in artifacts},
        premise_types={"row-1": "NORMALIZED_CALLSITE_FACT", "row-2": "NORMALIZED_CALLSITE_FACT"},
    )
    inputs = EvaluationInputs(
        proposition=dict(proposition),
        manifest=manifest,
        rule_bundle_version="rules/v2",
        model_encoding={"model": "m"},
        rule_id="call_edge/v1",
        rule_version="v1",
        implementation_digest=_impl_digest_for("call_edge/v1"),
        inference={},
    )
    return inputs, premises, artifacts, manifest


def _impl_digest_for(rule_id: str) -> str:
    from investigation_agent_platform.domain.proof.rules import RULE_REGISTRY

    return RULE_REGISTRY[rule_id].implementation_digest


def test_call_edge_reorder_invalidates_derivation() -> None:
    inputs, premises, artifacts, manifest = _callsite_setup()
    cert = certify(inputs, premises, artifacts)
    req = _request(cert, inputs.proposition, manifest, artifacts)
    assert check_certificate(req)[0] == CheckerOutcome.VALID
    reordered = dict(cert)
    reordered["premises"] = [cert["premises"][1], cert["premises"][0]]
    req = _request(reordered, inputs.proposition, manifest, artifacts)
    outcome, reasons = check_certificate(req)
    assert outcome == CheckerOutcome.INVALID_INFERENCE
    assert reasons


def test_tampered_bindings_rejected() -> None:
    inputs, premise, artifact = _definition_setup()
    cert = certify(inputs, [premise], {"row-1": artifact})
    tampered = dict(cert)
    bindings = dict(cert["inference_instance"]["bindings"])
    bindings["symbol"] = "SomethingElse"
    tampered["inference_instance"] = dict(cert["inference_instance"])
    tampered["inference_instance"]["bindings"] = bindings
    req = _request(tampered, inputs.proposition, inputs.manifest, {"row-1": artifact})
    assert check_certificate(req)[0] == CheckerOutcome.INVALID_INFERENCE


def test_tampered_classification_rejected() -> None:
    inputs, premise, artifact = _definition_setup()
    cert = certify(inputs, [premise], {"row-1": artifact})
    assert cert["proof_classification"] == "SOUND"
    cert["proof_classification"] = "CONSERVATIVE"
    req = _request(cert, inputs.proposition, inputs.manifest, {"row-1": artifact})
    outcome, reasons = check_certificate(req)
    assert outcome == CheckerOutcome.INVALID_INFERENCE
    assert reasons


def test_k4_universal_not_established_by_existential_rules() -> None:
    inputs, premise, artifact = _definition_setup()
    proposition = dict(inputs.proposition)
    proposition["quantifier"] = "UNIVERSAL"
    uni_inputs = EvaluationInputs(
        proposition=proposition,
        manifest=inputs.manifest,
        rule_bundle_version="rules/v2",
        model_encoding={"model": "m"},
        rule_id="definition/v1",
        rule_version="v1",
        implementation_digest=_impl_digest(),
    )
    cert = certify(uni_inputs, [premise], {"row-1": artifact}, universe="repo@rev:/:query=q")
    req = _request(cert, proposition, inputs.manifest, {"row-1": artifact})
    assert check_certificate(req)[0] == CheckerOutcome.UNSUPPORTED_CONSTRUCT


def test_tampered_ids_rejected() -> None:
    inputs, premise, artifact = _definition_setup()
    cert = certify(inputs, [premise], {"row-1": artifact})
    cert["proposition_id"] = "sha256:" + "f" * 64
    req = _request(cert, inputs.proposition, inputs.manifest, {"row-1": artifact})
    assert check_certificate(req)[0] == CheckerOutcome.INVALID_INFERENCE


def test_header_form_mismatch_rejected() -> None:
    inputs, premise, artifact = _definition_setup()
    cert = certify(inputs, [premise], {"row-1": artifact})
    cert["canonical_proposition"] = dict(cert["canonical_proposition"])
    cert["canonical_proposition"]["form"] = "ABSENCE"
    req = _request(cert, inputs.proposition, inputs.manifest, {"row-1": artifact})
    outcome, reasons = check_certificate(req)
    assert outcome == CheckerOutcome.INVALID_INFERENCE
    assert any("header/proposition mismatch: form" in r for r in reasons)


def test_header_dropped_key_rejected() -> None:
    inputs, premise, artifact = _definition_setup()
    cert = certify(inputs, [premise], {"row-1": artifact})
    cert["canonical_proposition"] = dict(cert["canonical_proposition"])
    del cert["canonical_proposition"]["relation"]
    req = _request(cert, inputs.proposition, inputs.manifest, {"row-1": artifact})
    assert check_certificate(req)[0] == CheckerOutcome.INVALID_INFERENCE


def test_assumptions_swap_rejected() -> None:
    inputs, premise, artifact = _definition_setup()
    cert = certify(inputs, [premise], {"row-1": artifact})
    cert["assumptions"] = ["attacker-assumption"]
    req = _request(cert, inputs.proposition, inputs.manifest, {"row-1": artifact})
    outcome, reasons = check_certificate(req)
    assert outcome == CheckerOutcome.INVALID_INFERENCE
    assert any("assumptions mismatch" in r for r in reasons)


def test_scope_swap_rejected() -> None:
    inputs, premise, artifact = _definition_setup()
    cert = certify(inputs, [premise], {"row-1": artifact})
    cert["scope"] = {"revision": "rev-9"}
    req = _request(cert, inputs.proposition, inputs.manifest, {"row-1": artifact})
    outcome, reasons = check_certificate(req)
    assert outcome == CheckerOutcome.SCOPE_MISMATCH
    assert any("scope mismatch" in r for r in reasons)


def test_premise_scope_missing_revision_rejected() -> None:
    inputs, premise, artifact = _definition_setup()
    cert = certify(inputs, [premise], {"row-1": artifact})
    cert["premises"] = [dict(cert["premises"][0])]
    cert["premises"][0]["scope"] = {}
    req = _request(cert, inputs.proposition, inputs.manifest, {"row-1": artifact})
    outcome, reasons = check_certificate(req)
    assert outcome == CheckerOutcome.SCOPE_MISMATCH
    assert any("missing revision" in r for r in reasons)


def test_consistent_rehash_binding_swap_rejected() -> None:
    """ARCH-HIGH-003 crown jewel: proposition P1, bindings + artifacts of
    P2, every ID recomputed so all recomputation gates pass. Only the
    binding-vs-target consistency check can catch it."""
    from investigation_agent_platform.domain.proof.canonical import (
        context_id as _context_id,
    )
    from investigation_agent_platform.domain.proof.canonical import (
        derivation_id as _derivation_id,
    )
    from investigation_agent_platform.domain.proof.manifest import manifest_digest

    inputs1, premise1, artifact1 = _definition_setup()
    cert1 = certify(inputs1, [premise1], {"row-1": artifact1})
    window2 = "class SomethingElse:\n    pass\n"
    artifact2 = {
        "file_path": "svc.py",
        "repository": "synthetic/service",
        "revision": "rev-1",
        "line_start": 10,
        "line_end": 14,
        "target": "SomethingElse",
        "window_text": window2,
    }
    prop2 = dict(inputs1.proposition)
    prop2["target"] = {"file_path": "svc.py", "symbol": "SomethingElse"}
    manifest2 = build_manifest(
        ["row-1"],
        {"row-1": _artifact_hash(artifact2)},
        premise_types={"row-1": "RANGE_READ_FACT"},
    )
    premise2 = {
        "content_hash": _artifact_hash(artifact2),
        "evidence_id": "row-1",
        "premise_type": "RANGE_READ_FACT",
        "scope": {"revision": "rev-1"},
    }
    inputs2 = EvaluationInputs(
        proposition=prop2,
        manifest=manifest2,
        rule_bundle_version="rules/v2",
        model_encoding={"model": "m"},
        rule_id="definition/v1",
        rule_version="v1",
        implementation_digest=_impl_digest(),
        inference={},
    )
    cert2 = certify(inputs2, [premise2], {"row-1": artifact2})
    forged = dict(cert2)
    forged["canonical_proposition"] = dict(cert1["canonical_proposition"])
    forged["proposition_id"] = cert1["proposition_id"]
    forged["scope"] = dict(cert1["scope"])
    forged["assumptions"] = list(cert1["assumptions"])
    forged["input_manifest_digest"] = manifest_digest(manifest2)
    forged["context_id"] = _context_id(
        cert1["proposition_id"],
        {
            "evidence_manifest_digest": forged["input_manifest_digest"],
            "model_encoding": {"model": "m"},
            "proposition_scope": dict(inputs1.proposition["scope"]),
            "reconciliation_state": "unreconciled",
            "rule_bundle_version": "rules/v2",
        },
    )
    forged["derivation_id"] = _derivation_id(
        forged["context_id"],
        "definition/v1",
        "v1",
        cert2["implementation_digest"],
        ["row-1"],
        cert2["inference_instance"],
    )
    req = _request(forged, inputs1.proposition, manifest2, {"row-1": artifact2})
    outcome, reasons = check_certificate(req)
    assert outcome == CheckerOutcome.INVALID_INFERENCE
    assert any("bindings diverge" in r for r in reasons)


def test_coverage_universe_mismatch_rejected() -> None:
    inputs, premise, artifact = _definition_setup()
    proposition = dict(inputs.proposition)
    proposition["form"] = "ABSENCE"
    scoped_manifest = build_manifest(
        ["row-1"],
        {"row-1": _artifact_hash(artifact)},
        coverage=ManifestCoverage.COMPLETE_EMPTY,
        retrieval_config={"scope": "repo@rev:/:query=q"},
        premise_types={"row-1": "RANGE_READ_FACT"},
    )
    scoped_inputs = EvaluationInputs(
        proposition=proposition,
        manifest=scoped_manifest,
        rule_bundle_version="rules/v2",
        model_encoding={"model": "m"},
        rule_id="definition/v1",
        rule_version="v1",
        implementation_digest=_impl_digest(),
    )
    cert = certify(scoped_inputs, [premise], {"row-1": artifact}, universe="other-scope")
    req = _request(cert, proposition, scoped_manifest, {"row-1": artifact})
    outcome, reasons = check_certificate(req)
    assert outcome == CheckerOutcome.INVALID_INFERENCE
    assert any("universe differs" in r for r in reasons)


def test_coverage_missing_universe_or_exclusions_rejected() -> None:
    inputs, premise, artifact = _definition_setup()
    proposition = dict(inputs.proposition)
    proposition["form"] = "ABSENCE"
    complete_manifest = build_manifest(
        ["row-1"],
        {"row-1": _artifact_hash(artifact)},
        coverage=ManifestCoverage.COMPLETE_EMPTY,
        premise_types={"row-1": "RANGE_READ_FACT"},
    )
    complete_inputs = EvaluationInputs(
        proposition=proposition,
        manifest=complete_manifest,
        rule_bundle_version="rules/v2",
        model_encoding={"model": "m"},
        rule_id="definition/v1",
        rule_version="v1",
        implementation_digest=_impl_digest(),
    )
    cert = certify(complete_inputs, [premise], {"row-1": artifact}, universe="repo@rev:/:query=q")
    cert["coverage"] = dict(cert["coverage"])
    cert["coverage"]["universe"] = ""
    req = _request(cert, proposition, complete_manifest, {"row-1": artifact})
    assert check_certificate(req)[0] == CheckerOutcome.MISSING_PREMISE
    cert["coverage"]["universe"] = "repo@rev:/:query=q"
    cert["coverage"]["exclusions"] = "not-a-list"
    req = _request(cert, proposition, complete_manifest, {"row-1": artifact})
    assert check_certificate(req)[0] == CheckerOutcome.MISSING_PREMISE


def test_unknown_schema_or_rule_fails_closed() -> None:
    inputs, premise, artifact = _definition_setup()
    cert = certify(inputs, [premise], {"row-1": artifact})
    cert["schema_version"] = "cert/v9"
    req = _request(cert, inputs.proposition, inputs.manifest, {"row-1": artifact})
    assert check_certificate(req)[0] == CheckerOutcome.CHECKER_FAILURE
    cert = certify(inputs, [premise], {"row-1": artifact})
    cert["rule_id"] = "causal/v1"
    req = _request(cert, inputs.proposition, inputs.manifest, {"row-1": artifact})
    assert check_certificate(req)[0] == CheckerOutcome.UNSUPPORTED_CONSTRUCT


def test_cert_v1_rejected_without_reread() -> None:
    inputs, premise, artifact = _definition_setup()
    cert = certify(inputs, [premise], {"row-1": artifact})
    assert cert["schema_version"] == "cert/v2"
    cert["schema_version"] = "cert/v1"
    req = _request(cert, inputs.proposition, inputs.manifest, {"row-1": artifact})
    assert check_certificate(req)[0] == CheckerOutcome.CHECKER_FAILURE


def test_causal_abstention_denies_end_to_end_k13() -> None:
    """CAUSAL can never reach VALID: no rule, no cert, checker denies, publication denies."""
    from investigation_agent_platform.domain.proof.publication import (
        DenyReason,
        PublicationDecision,
        _decide_linearization,
    )
    from investigation_agent_platform.domain.proof.rules import (
        UnknownRule,
        get_rule,
        rule_for_kind,
    )

    assert rule_for_kind("CAUSAL") is None
    with pytest.raises(UnknownRule):
        get_rule("causal/v1")
    inputs, premise, artifact = _definition_setup()
    cert = certify(inputs, [premise], {"row-1": artifact})
    cert["rule_id"] = "causal/v1"
    req = _request(cert, inputs.proposition, inputs.manifest, {"row-1": artifact})
    outcome, _ = check_certificate(req)
    assert outcome == CheckerOutcome.UNSUPPORTED_CONSTRUCT
    decision, reason = _decide_linearization(
        checker_outcome=outcome,
        certificate_context_id="ctx-1",
        expected_context_id="ctx-1",
        reconciliation_adjudicated=True,
        policy_eligible=True,
    )
    assert decision == PublicationDecision.DENY
    assert reason.startswith(DenyReason.NOT_CHECKER_ACCEPTED.value)
