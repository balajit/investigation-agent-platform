"""Obligation gating tests (K13 promotion half, v1.3 contract).

Pins the guarded promotion entry: expansion vectors (per-kind counts,
CAUSAL abstention without consulting the checker), policy binding
(exact repository equality, trusted bindings, no ambient authority),
structural outcome binding (no checker_outcome field on the entry),
snapshot integrity, genesis vectors (OPEN via unpinned revision with a
pinned twin that promotes; UNSUPPORTED via CAUSAL), evaluator-not-called
on checker denial, envelope rule_id mismatch, and the scalar bypass
closure (no public decide_publication symbol anywhere).
"""

from __future__ import annotations

import hashlib
import inspect
import json
from typing import Any

import pytest
from pydantic import ValidationError

from investigation_agent_platform.domain.proof import obligations as obligations_mod
from investigation_agent_platform.domain.proof.certificate import CheckerOutcome
from investigation_agent_platform.domain.proof.evaluation import EvaluationInputs
from investigation_agent_platform.domain.proof.manifest import ManifestCoverage, build_manifest
from investigation_agent_platform.domain.proof.obligations import (
    DecisionPolicy,
    ObligationStatus,
    decide_guarded_promotion,
    encode_evidence_bytes,
    encode_trusted_context_bytes,
    evaluate_obligations,
    expand_obligations,
)
from investigation_agent_platform.domain.proof.publication import DenyReason, PublicationDecision
from investigation_agent_platform.domain.proof.rules import RULE_BUNDLE_VERSION, get_rule

REPO = "synthetic/sp"
REV_A = "ab" * 20
REV_B = "cd" * 20
AUTHORITY = "synthetic-test-only-authority-001"
BUILD = "sp-build-001"
EXEC = "sp-exec-001"
ENV = "test"
UNIVERSE = "repository revision path prefix"
SEARCH_CONFIG = "sha256:" + "ab" * 32


def _policy(**over: Any) -> DecisionPolicy:
    base: dict[str, Any] = {
        "policy_id": "policy:t:unit:v1",
        "repositories": (REPO,),
        "approved_mapping_provenance_refs": (AUTHORITY,),
    }
    base.update(over)
    return DecisionPolicy(**base)


def _strip(record: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in record.items() if k not in ("evidence_id", "content_hash", "premise_type")}


def _assemble(
    rule_id: str, records: list[dict[str, Any]], proposition: dict[str, Any], **manifest_kw: Any
) -> dict[str, Any]:
    artifacts: dict[str, dict[str, Any]] = {}
    hashes: dict[str, str] = {}
    types: dict[str, str] = {}
    premises: list[dict[str, Any]] = []
    for record in records:
        art = _strip(record)
        digest = "sha256:" + hashlib.sha256(
            json.dumps(art, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        ).hexdigest()
        pid = str(record["evidence_id"])
        artifacts[pid] = art
        hashes[pid] = digest
        types[pid] = str(record["premise_type"])
        premises.append(dict(record, content_hash=digest))
    manifest = build_manifest(
        [str(r["evidence_id"]) for r in records], hashes, premise_types=types, **manifest_kw
    )
    rule = get_rule(rule_id)
    inputs = EvaluationInputs(
        proposition=dict(proposition),
        manifest=manifest,
        rule_bundle_version=RULE_BUNDLE_VERSION,
        rule_id=rule.rule_id,
        rule_version=rule.rule_version,
        implementation_digest=rule.implementation_digest,
    )
    return {"inputs": inputs, "premises": premises, "manifest": manifest, "artifacts": artifacts}


def _definition_record(**over: Any) -> dict[str, Any]:
    record = {
        "evidence_id": "ev-ob-definition-0",
        "premise_type": "RANGE_READ_FACT",
        "repository": REPO,
        "file_path": "src/handler.py",
        "revision": REV_A,
        "line_start": 10,
        "line_end": 14,
        "target": "handle",
        "window_text": "def handle(event):\n    return route(event)",
    }
    record.update(over)
    return record


def _definition_proposition(**over: Any) -> dict[str, Any]:
    proposition = {
        "canon_version": "canon/v1",
        "form": "EXISTENCE",
        "quantifier": "EXISTENTIAL",
        "predicate": "defined(handle)",
        "target": {"symbol": "handle", "file_path": "src/handler.py"},
        "scope": {"revision": REV_A},
    }
    proposition.update(over)
    return proposition


def _bindings_for(records: list[dict[str, Any]], rule_id: str) -> list[dict[str, Any]]:
    asm = _assemble(rule_id, records, {})
    out = []
    for premise in asm["premises"]:
        pid = str(premise["evidence_id"])
        art = asm["artifacts"][pid]
        scope = art.get("scope") if isinstance(art.get("scope"), dict) else {}
        repo = str(art.get("repository") or scope.get("repository") or "")
        out.append({"evidence_id": pid, "content_digest": str(premise["content_hash"]), "repository_id": repo})
    return out


def _evidence_bytes(rule_id: str, records: list[dict[str, Any]], proposition: dict[str, Any], **manifest_kw: Any) -> tuple[bytes, dict[str, Any]]:
    from investigation_agent_platform.domain.proof.certificate import certify

    asm = _assemble(rule_id, records, proposition, **manifest_kw)
    cert = certify(asm["inputs"], asm["premises"], asm["artifacts"])
    payload = {
        "rule_id": rule_id,
        "proposition": proposition,
        "certificate": cert,
        "manifest": manifest_to_json(asm["manifest"]),
        "artifacts": asm["artifacts"],
    }
    return encode_evidence_bytes(payload), {"cert": cert, "manifest": asm["manifest"], "artifacts": asm["artifacts"]}


def manifest_to_json(manifest: Any) -> dict[str, Any]:
    return json.loads(manifest.model_dump_json())


def _trusted_bytes(
    bindings: list[dict[str, Any]], expected_context_id: str | None, **over: Any
) -> bytes:
    payload: dict[str, Any] = {
        "rule_bundle_version": RULE_BUNDLE_VERSION,
        "model_encoding": {},
        "reconciliation_state": "unreconciled",
        "known_normalization_ops": [],
        "derivation_deps": {},
        "repository_bindings": bindings,
        "expected_context_id": expected_context_id,
        "reconciliation_adjudicated": True,
        "policy_eligible": True,
        "policy_reason": "",
    }
    payload.update(over)
    return encode_trusted_context_bytes(payload)


def _guarded_definition(**over: Any) -> tuple[bytes, bytes, dict[str, Any]]:
    record_over = over.pop("record_over", {})
    prop_over = over.pop("prop_over", {})
    manifest_kw = over.pop("manifest_kw", {})
    record = _definition_record(**record_over)
    proposition = _definition_proposition(**prop_over)
    evidence, parts = _evidence_bytes("definition/v1", [record], proposition, **manifest_kw)
    bindings = _bindings_for([record], "definition/v1")
    trusted = _trusted_bytes(bindings, str(parts["cert"].get("context_id") or ""), **over)
    return evidence, trusted, parts


# ---------------------------------------------------------------------------
# Expansion
# ---------------------------------------------------------------------------


def test_expand_counts_and_ids_per_kind() -> None:
    expected = {
        "definition/v1": 3,
        "call_edge/v1": 3,
        "behavior/v1": 3,
        "absence/v1": 4,
        "history/v1": 3,
    }
    for rule_id, count in expected.items():
        obligations = expand_obligations(rule_id, {}, [], "der-x")
        assert len(obligations) == count
        assert [o.obligation_id for o in obligations] == [
            f"obl:der-x:{rule_id}:{i}" for i in range(count)
        ]
        assert all(o.status == ObligationStatus.OPEN for o in obligations)
        assert all(o.derived_from == "der-x" for o in obligations)


def test_expand_unknown_kind_unsupported_without_checker(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[str] = []
    monkeypatch.setattr(
        obligations_mod, "check_certificate", lambda *a, **k: called.append("checker") or (CheckerOutcome.VALID, [])
    )
    evidence = encode_evidence_bytes({"rule_id": "causal/v1", "proposition": {}, "certificate": None, "manifest": {}, "artifacts": {}})
    trusted = _trusted_bytes([], None)
    decision, reason = decide_guarded_promotion(_policy(), evidence, trusted)
    assert decision == PublicationDecision.DENY
    assert reason == DenyReason.UNSUPPORTED_OBLIGATION.value
    assert called == []


def test_expand_binds_derivation_and_evidence_ids() -> None:
    obligations = expand_obligations(
        "call_edge/v1", {}, [{"evidence_id": "a"}, {"evidence_id": "b"}], "der-9"
    )
    assert all(o.derived_from == "der-9" for o in obligations)
    assert all(o.evidence == ("a", "b") for o in obligations)


# ---------------------------------------------------------------------------
# Genesis: OPEN via unpinned revision; pinned twin promotes
# ---------------------------------------------------------------------------


def test_open_unpinned_definition_denies_while_pinned_twin_promotes() -> None:
    unpinned_prop = _definition_proposition()
    del unpinned_prop["scope"]
    record = _definition_record()
    evidence, parts = _evidence_bytes("definition/v1", [record], unpinned_prop)
    bindings = _bindings_for([record], "definition/v1")
    trusted = _trusted_bytes(bindings, str(parts["cert"].get("context_id") or ""))
    decision, reason = decide_guarded_promotion(_policy(), evidence, trusted)
    assert decision == PublicationDecision.DENY
    assert reason.startswith(DenyReason.OPEN_OBLIGATION.value)

    evidence2, trusted2, _ = _guarded_definition()
    decision2, _ = decide_guarded_promotion(_policy(), evidence2, trusted2)
    assert decision2 == PublicationDecision.COMMIT


def test_open_call_edge_unpinned_denies() -> None:
    record = {
        "evidence_id": "ev-ob-edge-0",
        "premise_type": "NORMALIZED_CALLSITE_FACT",
        "repository": REPO,
        "file_path": "src/handler.py",
        "revision": REV_A,
        "line_start": 12,
        "line_end": 12,
        "caller": "handle",
        "callee": "route",
        "call_text": "handle delegates: return route(event)",
    }
    proposition = {
        "canon_version": "canon/v1",
        "form": "EXISTENCE",
        "quantifier": "EXISTENTIAL",
        "predicate": "calls(handle,route)",
        "target": {"caller": "handle", "callee": "route"},
        "scope": {},
    }
    evidence, parts = _evidence_bytes("call_edge/v1", [record], proposition)
    bindings = _bindings_for([record], "call_edge/v1")
    trusted = _trusted_bytes(bindings, str(parts["cert"].get("context_id") or ""))
    decision, reason = decide_guarded_promotion(_policy(), evidence, trusted)
    assert decision == PublicationDecision.DENY
    assert reason.startswith(DenyReason.OPEN_OBLIGATION.value)


# ---------------------------------------------------------------------------
# Policy binding
# ---------------------------------------------------------------------------


def test_policy_wrong_repository_denies() -> None:
    evidence, trusted, _ = _guarded_definition()
    decision, reason = decide_guarded_promotion(_policy(repositories=("other/repo",)), evidence, trusted)
    assert decision == PublicationDecision.DENY
    assert reason.startswith(f"{DenyReason.POLICY_DENIED.value}:policy-scope:")


def test_policy_prefix_repo_does_not_authorize() -> None:
    evidence, _, parts = _guarded_definition()
    bindings = [
        {
            "evidence_id": "ev-ob-definition-0",
            "content_digest": parts["artifacts"]["ev-ob-definition-0"] and _digest_of(parts, "ev-ob-definition-0"),
            "repository_id": REPO,
        }
    ]
    trusted = _trusted_bytes(bindings, str(parts["cert"].get("context_id") or ""))
    decision, _ = decide_guarded_promotion(_policy(repositories=(REPO + "-suffix",)), evidence, trusted)
    assert decision == PublicationDecision.DENY


def _digest_of(parts: dict[str, Any], pid: str) -> str:
    from investigation_agent_platform.domain.proof.certificate import _artifact_hash

    return str(_artifact_hash(parts["artifacts"][pid]))


def test_policy_missing_binding_denies() -> None:
    evidence, _, parts = _guarded_definition()
    trusted = _trusted_bytes([], str(parts["cert"].get("context_id") or ""))
    decision, reason = decide_guarded_promotion(_policy(), evidence, trusted)
    assert decision == PublicationDecision.DENY
    assert reason.startswith(DenyReason.POLICY_DENIED.value)


def test_policy_digest_mismatch_denies() -> None:
    evidence, _, parts = _guarded_definition()
    bindings = [
        {
            "evidence_id": "ev-ob-definition-0",
            "content_digest": "sha256:" + "00" * 32,
            "repository_id": REPO,
        }
    ]
    trusted = _trusted_bytes(bindings, str(parts["cert"].get("context_id") or ""))
    decision, reason = decide_guarded_promotion(_policy(), evidence, trusted)
    assert decision == PublicationDecision.DENY
    assert reason.startswith(DenyReason.POLICY_DENIED.value)


def test_policy_binding_digest_matches_artifact_bytes() -> None:
    evidence, _, parts = _guarded_definition()
    payload = json.loads(bytes(evidence).decode())
    payload["artifacts"]["ev-ob-definition-0"]["window_text"] = "def changed(): pass"
    forged = encode_evidence_bytes(payload)
    bindings = _bindings_for([_definition_record()], "definition/v1")
    trusted = _trusted_bytes(bindings, str(parts["cert"].get("context_id") or ""))
    decision, reason = decide_guarded_promotion(_policy(), forged, trusted)
    assert decision == PublicationDecision.DENY
    assert reason.startswith(f"{DenyReason.POLICY_DENIED.value}:binding-digest-mismatch")


def test_policy_none_denies() -> None:
    evidence, trusted, _ = _guarded_definition()
    decision, reason = decide_guarded_promotion(None, evidence, trusted)
    assert decision == PublicationDecision.DENY
    assert reason.startswith(DenyReason.POLICY_DENIED.value)


def test_policy_empty_repositories_rejected_at_construction() -> None:
    with pytest.raises(ValidationError):
        _policy(repositories=())


def test_policy_duplicate_repositories_rejected_at_construction() -> None:
    with pytest.raises(ValueError):
        _policy(repositories=(REPO, REPO))


def test_policy_extra_binding_denies() -> None:
    evidence, _, parts = _guarded_definition()
    record = _definition_record()
    bindings = _bindings_for([record], "definition/v1")
    bindings.append({"evidence_id": "ev-extra", "content_digest": "sha256:" + "ff" * 32, "repository_id": REPO})
    trusted = _trusted_bytes(bindings, str(parts["cert"].get("context_id") or ""))
    decision, reason = decide_guarded_promotion(_policy(), evidence, trusted)
    assert decision == PublicationDecision.DENY
    assert reason.startswith(DenyReason.POLICY_DENIED.value)


def test_policy_artifact_repository_mismatch_denies() -> None:
    record = _definition_record(repository="other/repo")
    proposition = _definition_proposition()
    evidence, parts = _evidence_bytes("definition/v1", [record], proposition)
    digest = str(parts["cert"]["premises"][0]["content_hash"])
    bindings = [{"evidence_id": "ev-ob-definition-0", "content_digest": digest, "repository_id": REPO}]
    trusted = _trusted_bytes(bindings, str(parts["cert"].get("context_id") or ""))
    policy = _policy(repositories=("other/repo", REPO))
    decision, reason = decide_guarded_promotion(policy, evidence, trusted)
    # The trusted binding says synthetic/sp but the artifact says
    # other/repo: the artifact cannot authorize itself.
    assert decision == PublicationDecision.DENY
    assert reason.startswith(DenyReason.POLICY_DENIED.value)


def test_policy_scope_repository_must_join_trusted_premise() -> None:
    evidence, _, parts = _guarded_definition()
    payload = json.loads(bytes(evidence).decode())
    payload["proposition"]["scope"]["repository"] = "other/repo"
    forged = encode_evidence_bytes(payload)
    bindings = _bindings_for([_definition_record()], "definition/v1")
    trusted = _trusted_bytes(bindings, str(parts["cert"].get("context_id") or ""))
    decision, reason = decide_guarded_promotion(_policy(repositories=(REPO, "other/repo")), forged, trusted)
    assert decision == PublicationDecision.DENY
    assert reason.startswith(f"{DenyReason.POLICY_DENIED.value}:scope-binding-mismatch")


# ---------------------------------------------------------------------------
# Structural binding: no checker_outcome field; envelope rule_id match
# ---------------------------------------------------------------------------


def test_guarded_entry_has_no_checker_outcome_field() -> None:
    params = inspect.signature(decide_guarded_promotion).parameters
    assert set(params) == {"policy", "evidence_bytes", "trusted_context_bytes"}
    source = inspect.getsource(decide_guarded_promotion)
    assert "checker_outcome" not in source


def test_envelope_rule_id_mismatch_denies_before_checker(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[str] = []
    monkeypatch.setattr(
        obligations_mod, "check_certificate", lambda *a, **k: called.append("checker") or (CheckerOutcome.VALID, [])
    )
    record = _definition_record()
    proposition = _definition_proposition()
    evidence, parts = _evidence_bytes("definition/v1", [record], proposition)
    payload = json.loads(bytes(evidence).decode())
    payload["rule_id"] = "call_edge/v1"
    forged = encode_evidence_bytes(payload)
    bindings = _bindings_for([record], "definition/v1")
    trusted = _trusted_bytes(bindings, str(parts["cert"].get("context_id") or ""))
    decision, reason = decide_guarded_promotion(_policy(), forged, trusted)
    assert decision == PublicationDecision.DENY
    assert reason.startswith("RULE_MISMATCH")
    assert called == []


def test_internal_check_denial_skips_evaluation(monkeypatch: pytest.MonkeyPatch) -> None:
    evaluated: list[str] = []
    real_evaluate = obligations_mod.evaluate_obligations
    monkeypatch.setattr(
        obligations_mod,
        "evaluate_obligations",
        lambda *a, **k: evaluated.append("evaluated") or real_evaluate(*a, **k),
    )
    record = _definition_record(window_text="class Other:\n    pass\n")
    proposition = _definition_proposition()
    from investigation_agent_platform.domain.proof.certificate import CertificationError, certify

    asm = _assemble("definition/v1", [record], proposition)
    with pytest.raises(CertificationError):
        certify(asm["inputs"], asm["premises"], asm["artifacts"])
    # Forge a cert-shaped payload whose window lacks the symbol: the
    # internal checker denies INVALID_INFERENCE before evaluation.
    good_record = _definition_record()
    evidence, parts = _evidence_bytes("definition/v1", [good_record], proposition)
    payload = json.loads(bytes(evidence).decode())
    cert = payload["certificate"]
    artifact = payload["artifacts"]["ev-ob-definition-0"]
    artifact["window_text"] = "class Other:\n    pass\n"
    from investigation_agent_platform.domain.proof.certificate import _artifact_hash

    new_digest = str(_artifact_hash(artifact))
    for premise in cert["premises"]:
        premise["content_hash"] = new_digest
    payload["manifest"]["content_hashes"] = {"ev-ob-definition-0": new_digest}
    forged = encode_evidence_bytes(payload)
    bindings = [
        {
            "evidence_id": "ev-ob-definition-0",
            "content_digest": new_digest,
            "repository_id": REPO,
        }
    ]
    trusted = _trusted_bytes(bindings, str(cert.get("context_id") or ""))
    decision, reason = decide_guarded_promotion(_policy(), forged, trusted)
    assert decision == PublicationDecision.DENY
    assert reason.startswith(f"{DenyReason.NOT_CHECKER_ACCEPTED.value}:")
    assert evaluated == []


def test_evaluator_exception_denies_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        obligations_mod, "evaluate_obligations", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    evidence, trusted, _ = _guarded_definition()
    decision, reason = decide_guarded_promotion(_policy(), evidence, trusted)
    assert decision == PublicationDecision.DENY
    assert reason.startswith(f"{DenyReason.NOT_CHECKER_ACCEPTED.value}:CHECKER_FAILURE")


# ---------------------------------------------------------------------------
# Snapshot integrity
# ---------------------------------------------------------------------------


def test_snapshot_digest_stable_across_materialization() -> None:
    evidence, trusted, _ = _guarded_definition()
    first, _ = decide_guarded_promotion(_policy(), evidence, trusted)
    second, _ = decide_guarded_promotion(_policy(), evidence, trusted)
    assert (first, second) == (PublicationDecision.COMMIT, PublicationDecision.COMMIT)


def test_snapshot_non_canonical_bytes_deny() -> None:
    evidence, _, parts = _guarded_definition()
    bindings = _bindings_for([_definition_record()], "definition/v1")
    trusted = _trusted_bytes(bindings, str(parts["cert"].get("context_id") or ""))
    pretty = json.dumps(json.loads(bytes(evidence).decode()), indent=2).encode()
    decision, reason = decide_guarded_promotion(_policy(), pretty, trusted)
    assert decision == PublicationDecision.DENY
    assert reason.startswith(DenyReason.OBLIGATIONS_ABSENT.value)


def test_snapshot_duplicate_keys_deny() -> None:
    evidence, _, parts = _guarded_definition()
    bindings = _bindings_for([_definition_record()], "definition/v1")
    trusted = _trusted_bytes(bindings, str(parts["cert"].get("context_id") or ""))
    text = bytes(evidence).decode()
    duped = text.replace('"rule_id":"definition/v1"', '"rule_id":"definition/v1","rule_id":"definition/v1"', 1)
    assert duped != text, "duplicate-key injection must alter the payload"
    decision, reason = decide_guarded_promotion(_policy(), duped.encode(), trusted)
    assert decision == PublicationDecision.DENY
    assert reason.startswith(DenyReason.OBLIGATIONS_ABSENT.value)


def test_missing_trusted_field_denies_closed() -> None:
    evidence, _, parts = _guarded_definition()
    bindings = _bindings_for([_definition_record()], "definition/v1")
    payload = {
        "rule_bundle_version": RULE_BUNDLE_VERSION,
        "model_encoding": {},
        "reconciliation_state": "unreconciled",
        "known_normalization_ops": [],
        "derivation_deps": {},
        "repository_bindings": bindings,
        "reconciliation_adjudicated": True,
        "policy_eligible": True,
        "policy_reason": "",
    }
    trusted = encode_trusted_context_bytes(payload)
    decision, reason = decide_guarded_promotion(_policy(), evidence, trusted)
    assert decision == PublicationDecision.DENY


# ---------------------------------------------------------------------------
# Full promotion per rule kind
# ---------------------------------------------------------------------------


def _behavior_parts() -> tuple[bytes, bytes]:
    records = [
        {
            "evidence_id": "ev-ob-behavior-0",
            "premise_type": "RANGE_READ_FACT",
            "repository": REPO,
            "file_path": "src/serve.py",
            "revision": REV_A,
            "line_start": 10,
            "line_end": 14,
            "target": "serve",
            "window_text": "def serve(request):\n    return respond(request, 200)",
        },
        {
            "evidence_id": "ev-ob-behavior-1",
            "premise_type": "RUNTIME_OBSERVATION_FACT",
            "repository": REPO,
            "symbol": "serve",
            "predicate": "returns_status(serve)",
            "observed_value": 200,
            "execution_id": "ex-1",
            "build_identity": "b-1",
            "environment": "test",
            "source_revision": REV_A,
        },
        {
            "evidence_id": "ev-ob-behavior-2",
            "premise_type": "BUILD_SOURCE_MAPPING",
            "build_identity": "b-1",
            "repository": REPO,
            "source_revision": REV_A,
            "provenance_ref": AUTHORITY,
            "mapping_digest": "sha256:" + "cd" * 32,
        },
    ]
    proposition = {
        "canon_version": "canon/v1",
        "form": "OBSERVATION",
        "quantifier": "OBSERVATIONAL",
        "relation": "RUNTIME_OBSERVATION",
        "modality": "RUNTIME",
        "predicate": "returns_status(serve)",
        "asserted_value": 200,
        "target": {"symbol": "serve", "file_path": "src/serve.py"},
        "scope": {
            "repository": REPO,
            "revision": REV_A,
            "build_identity": "b-1",
            "execution_id": "ex-1",
            "environment": "test",
        },
    }
    evidence, parts = _evidence_bytes("behavior/v1", records, proposition, retrieval_config={"scope": REPO})
    bindings = _bindings_for(records, "behavior/v1")
    trusted = _trusted_bytes(bindings, str(parts["cert"].get("context_id") or ""))
    return evidence, trusted


def test_behavior_valid_promotes() -> None:
    evidence, trusted = _behavior_parts()
    decision, reason = decide_guarded_promotion(_policy(), evidence, trusted)
    assert decision == PublicationDecision.COMMIT, reason


def test_behavior_unapproved_authority_denies() -> None:
    evidence, trusted = _behavior_parts()
    policy = _policy(approved_mapping_provenance_refs=())
    decision, reason = decide_guarded_promotion(policy, evidence, trusted)
    assert decision == PublicationDecision.DENY
    assert reason.startswith(f"{DenyReason.NOT_CHECKER_ACCEPTED.value}:")


def test_absence_valid_promotes() -> None:
    record = {
        "evidence_id": "ev-ob-absence-0",
        "premise_type": "ABSENCE_COVERAGE_FACT",
        "universe": UNIVERSE,
        "predicate": "contains_symbol(legacy_hook)",
        "search_status": "COMPLETE_EMPTY",
        "search_config_digest": SEARCH_CONFIG,
        "scope": {"repository": REPO, "revision": REV_B, "path_scope": "src/"},
        "exclusions": [],
        "coverage_basis": {"kind": "synthetic_complete_inventory", "value": "all enumerated"},
        "match_count": 0,
    }
    proposition = {
        "canon_version": "canon/v1",
        "form": "ABSENCE",
        "quantifier": "UNIVERSAL",
        "modality": "STATIC",
        "completeness": "CLOSED_WORLD",
        "predicate": "contains_symbol(legacy_hook)",
        "target": {"universe": UNIVERSE},
        "scope": {"repository": REPO, "revision": REV_B, "path_scope": "src/"},
    }
    evidence, parts = _evidence_bytes(
        "absence/v1",
        [record],
        proposition,
        retrieval_config={"scope": UNIVERSE, "search_config_digest": SEARCH_CONFIG},
        coverage=ManifestCoverage.COMPLETE_EMPTY,
        coverage_evidence_id="ev-ob-absence-0",
        coverage_evidence={k: v for k, v in record.items() if k != "evidence_id"},
    )
    bindings = _bindings_for([record], "absence/v1")
    trusted = _trusted_bytes(bindings, str(parts["cert"].get("context_id") or ""))
    decision, reason = decide_guarded_promotion(_policy(), evidence, trusted)
    assert decision == PublicationDecision.COMMIT, reason


def test_absence_obligation_rejects_retrieval_config_mismatch() -> None:
    record = {
        "evidence_id": "ev-ob-absence-config-0",
        "premise_type": "ABSENCE_COVERAGE_FACT",
        "universe": UNIVERSE,
        "predicate": "contains_symbol(legacy_hook)",
        "search_status": "COMPLETE_EMPTY",
        "search_config_digest": SEARCH_CONFIG,
        "scope": {"repository": REPO, "revision": REV_B, "path_scope": "src/"},
        "exclusions": [],
        "coverage_basis": {"kind": "synthetic_complete_inventory", "value": "all enumerated"},
        "match_count": 0,
    }
    proposition = {
        "canon_version": "canon/v1",
        "form": "ABSENCE",
        "quantifier": "UNIVERSAL",
        "modality": "STATIC",
        "completeness": "CLOSED_WORLD",
        "predicate": "contains_symbol(legacy_hook)",
        "target": {"universe": UNIVERSE},
        "scope": {"repository": REPO, "revision": REV_B, "path_scope": "src/"},
    }
    evidence, parts = _evidence_bytes(
        "absence/v1", [record], proposition,
        retrieval_config={"scope": UNIVERSE, "search_config_digest": SEARCH_CONFIG},
        coverage=ManifestCoverage.COMPLETE_EMPTY,
        coverage_evidence_id="ev-ob-absence-config-0",
        coverage_evidence={k: v for k, v in record.items() if k != "evidence_id"},
    )
    manifest = parts["manifest"].model_copy(
        update={"retrieval_config": {"scope": UNIVERSE, "search_config_digest": "sha256:" + "ff" * 32}}
    )
    obligations = expand_obligations("absence/v1", parts["cert"]["inference_instance"]["bindings"], parts["cert"]["premises"], parts["cert"]["derivation_id"])
    resolved = evaluate_obligations(
        obligations,
        parts["cert"],
        parts["artifacts"],
        manifest,
        None,
    )
    assert any(item.status == ObligationStatus.OPEN for item in resolved)


def test_history_valid_promotes() -> None:
    record = {
        "evidence_id": "ev-ob-history-0",
        "premise_type": "COMMIT_HISTORY",
        "repository": REPO,
        "repository_revision": REV_B,
        "commit_id": REV_A,
        "file_path": "src/handler.py",
        "subject": "Route lookup fix",
    }
    proposition = {
        "canon_version": "canon/v1",
        "form": "HISTORICAL",
        "quantifier": "EXISTENTIAL",
        "modality": "HISTORY",
        "predicate": "changed_at_commit(src/handler.py, commit)",
        "target": {
            "repository": REPO,
            "commit_id": REV_A,
            "file_path": "src/handler.py",
            "subject": "Route lookup fix",
        },
        "scope": {"repository_revision": REV_B},
    }
    evidence, parts = _evidence_bytes("history/v1", [record], proposition)
    bindings = _bindings_for([record], "history/v1")
    trusted = _trusted_bytes(bindings, str(parts["cert"].get("context_id") or ""))
    decision, reason = decide_guarded_promotion(_policy(), evidence, trusted)
    assert decision == PublicationDecision.COMMIT, reason


def test_call_edge_valid_promotes() -> None:
    record = {
        "evidence_id": "ev-ob-edge-0",
        "premise_type": "NORMALIZED_CALLSITE_FACT",
        "repository": REPO,
        "file_path": "src/handler.py",
        "revision": REV_A,
        "line_start": 12,
        "line_end": 12,
        "caller": "handle",
        "callee": "route",
        "call_text": "handle delegates: return route(event)",
    }
    proposition = {
        "canon_version": "canon/v1",
        "form": "EXISTENCE",
        "quantifier": "EXISTENTIAL",
        "predicate": "calls(handle,route)",
        "target": {"caller": "handle", "callee": "route"},
        "scope": {"revision": REV_A},
    }
    evidence, parts = _evidence_bytes("call_edge/v1", [record], proposition)
    bindings = _bindings_for([record], "call_edge/v1")
    trusted = _trusted_bytes(bindings, str(parts["cert"].get("context_id") or ""))
    decision, reason = decide_guarded_promotion(_policy(), evidence, trusted)
    assert decision == PublicationDecision.COMMIT, reason


def test_missing_certificate_denies_absent() -> None:
    payload = {
        "rule_id": "definition/v1",
        "proposition": _definition_proposition(),
        "certificate": None,
        "manifest": {},
        "artifacts": {},
    }
    evidence = encode_evidence_bytes(payload)
    trusted = _trusted_bytes([], None)
    decision, reason = decide_guarded_promotion(_policy(), evidence, trusted)
    assert decision == PublicationDecision.DENY
    assert reason == DenyReason.OBLIGATIONS_ABSENT.value


# ---------------------------------------------------------------------------
# Scalar bypass closure
# ---------------------------------------------------------------------------


def test_no_public_scalar_promotion_symbol() -> None:
    import investigation_agent_platform.domain.proof.publication as publication_mod

    assert not hasattr(publication_mod, "decide_publication"), "public scalar promotion entry still exists"
    assert not hasattr(publication_mod, "PublicationRequest"), "caller-supplied request shape still exists"


def test_service_uses_guarded_entry_only() -> None:
    from pathlib import Path

    service = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "investigation_agent_platform"
        / "application"
        / "proof"
        / "publication_commit_service.py"
    )
    text = service.read_text(encoding="utf-8")
    assert "decide_guarded_promotion" in text
    assert "decide_publication" not in text
    assert "PublicationRequest" not in text
