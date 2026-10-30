"""Component 2 rule, checker, and contract tests: behavior/v1, absence/v1,
history/v1 (rules/v2, cert/v2).

Producer-validator vectors (one defect per vector), independent checker
wiring and import-separation proofs, certify-to-checker round trips,
hash-preimage contract vectors, rule-bundle gating, and D4a
first-failure precedence vectors. Differential producer/checker
agreement is asserted per rule; expected bindings are pinned as
literals, never derived from the implementation under test.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from investigation_agent_platform.domain.hypothesis.models import ClaimKind
from investigation_agent_platform.domain.proof.canonical import canonical_dict_bytes
from investigation_agent_platform.domain.proof.certificate import (
    CERT_SCHEMA_VERSION,
    CertificationError,
    CheckRequest,
    certify,
    check_certificate,
)
from investigation_agent_platform.domain.proof.checker_semantics import (
    check_absence_semantics,
    check_history_semantics,
)
from investigation_agent_platform.domain.proof.composition import RULE_DEPS, make_node
from investigation_agent_platform.domain.proof.evaluation import EvaluationInputs, evaluate
from investigation_agent_platform.domain.proof.manifest import ManifestCoverage, build_manifest
from investigation_agent_platform.domain.proof.outcomes import CheckerOutcome
from investigation_agent_platform.domain.proof.rules import (
    APPROVED_BUILD_MAPPING_AUTHORITIES,
    RULE_BUNDLE_VERSION,
    RULE_REGISTRY,
    RuleOutcome,
    apply_rule,
    get_rule,
    rule_for_kind,
    validate_absence_premises,
    validate_behavior_premises,
    validate_history_premises,
)

REV_A = "a" * 40
REV_B = "b" * 40
REV_C = "c" * 40
REV_D = "d" * 40
REV_E = "e" * 40
SHA_A = "sha256:" + "a" * 64
SHA_B = "sha256:" + "b" * 64
SHA_C = "sha256:" + "c" * 64
SHA_D = "sha256:" + "d" * 64
AUTHORITY = "synthetic-test-only-authority-001"


_PINNED_BEHAVIOR_BINDINGS = {
    "form": "OBSERVATION",
    "quantifier": "OBSERVATIONAL",
    "relation": "RUNTIME_OBSERVATION",
    "modality": "RUNTIME",
    "predicate": "returns_status(target)",
    "asserted_value": 200,
    "symbol": "target",
    "file_path": "src/service.py",
    "repository": "synthetic/service",
    "revision": REV_A,
    "build_identity": "build-001",
    "execution_id": "exec-001",
    "environment": "test",
}

_PINNED_ABSENCE_BINDINGS = {
    "form": "ABSENCE",
    "quantifier": "UNIVERSAL",
    "modality": "STATIC",
    "completeness": "CLOSED_WORLD",
    "predicate": "contains_symbol(target)",
    "universe": "repository revision path prefix",
    "repository": "synthetic/service",
    "revision": REV_B,
    "path_scope": "src/",
}

_PINNED_HISTORY_BINDINGS = {
    "form": "HISTORICAL",
    "quantifier": "EXISTENTIAL",
    "modality": "HISTORY",
    "predicate": "changed_at_commit(file, commit)",
    "repository": "synthetic/service",
    "commit_id": REV_C,
    "file_path": "src/service.py",
    "subject": "Update target handling",
    "repository_revision": REV_D,
}


def _behavior_claim(**over: Any) -> dict[str, Any]:
    claim: dict[str, Any] = {
        "form": "OBSERVATION",
        "quantifier": "OBSERVATIONAL",
        "relation": "RUNTIME_OBSERVATION",
        "modality": "RUNTIME",
        "symbol": "target",
        "file_path": "src/service.py",
        "repository": "synthetic/service",
        "revision": REV_A,
        "build_identity": "build-001",
        "execution_id": "exec-001",
        "environment": "test",
        "predicate": "returns_status(target)",
        "asserted_value": 200,
    }
    claim.update(over)
    return claim


def _behavior_premises() -> list[dict[str, Any]]:
    return [
        {
            "evidence_id": "ev-code-001",
            "content_hash": SHA_A,
            "premise_type": "RANGE_READ_FACT",
            "repository": "synthetic/service",
            "file_path": "src/service.py",
            "revision": REV_A,
            "line_start": 10,
            "line_end": 14,
            "target": "target",
            "window_text": "def target(): ...",
        },
        {
            "evidence_id": "ev-runtime-001",
            "content_hash": SHA_B,
            "premise_type": "RUNTIME_OBSERVATION_FACT",
            "repository": "synthetic/service",
            "symbol": "target",
            "predicate": "returns_status(target)",
            "observed_value": 200,
            "execution_id": "exec-001",
            "build_identity": "build-001",
            "environment": "test",
            "source_revision": REV_A,
        },
        {
            "evidence_id": "ev-build-map-001",
            "content_hash": SHA_C,
            "premise_type": "BUILD_SOURCE_MAPPING",
            "build_identity": "build-001",
            "repository": "synthetic/service",
            "source_revision": REV_A,
            "provenance_ref": AUTHORITY,
            "mapping_digest": SHA_D,
        },
    ]


def _absence_claim(**over: Any) -> dict[str, Any]:
    claim: dict[str, Any] = {
        "form": "ABSENCE",
        "quantifier": "UNIVERSAL",
        "modality": "STATIC",
        "completeness": "CLOSED_WORLD",
        "universe": "repository revision path prefix",
        "predicate": "contains_symbol(target)",
        "repository": "synthetic/service",
        "revision": REV_B,
        "path_scope": "src/",
    }
    claim.update(over)
    return claim


def _absence_manifest() -> dict[str, Any]:
    return {
        "evidence_ids": ["ev-coverage-001"],
        "content_hashes": {"ev-coverage-001": SHA_B},
        "premise_types": {"ev-coverage-001": "ABSENCE_COVERAGE_FACT"},
        "retrieval_config": {
            "scope": "repository revision path prefix",
            "search_config_digest": SHA_A,
        },
        "exclusions": [],
        "coverage": "COMPLETE_EMPTY",
    }


def _absence_premises() -> list[dict[str, Any]]:
    return [
        {
            "evidence_id": "ev-coverage-001",
            "content_hash": SHA_B,
            "premise_type": "ABSENCE_COVERAGE_FACT",
            "universe": "repository revision path prefix",
            "predicate": "contains_symbol(target)",
            "search_status": "COMPLETE_EMPTY",
            "search_config_digest": SHA_A,
            "scope": {"repository": "synthetic/service", "revision": REV_B, "path_scope": "src/"},
            "exclusions": [],
            "coverage_basis": {"kind": "synthetic_complete_inventory", "value": "all enumerated"},
            "match_count": 0,
        }
    ]


def _history_claim(**over: Any) -> dict[str, Any]:
    claim: dict[str, Any] = {
        "form": "HISTORICAL",
        "quantifier": "EXISTENTIAL",
        "modality": "HISTORY",
        "repository": "synthetic/service",
        "commit_id": REV_C,
        "file_path": "src/service.py",
        "subject": "Update target handling",
        "repository_revision": REV_D,
    }
    claim.update(over)
    return claim


def _history_premises() -> list[dict[str, Any]]:
    return [
        {
            "evidence_id": "ev-history-001",
            "content_hash": SHA_A,
            "premise_type": "COMMIT_HISTORY",
            "repository": "synthetic/service",
            "repository_revision": REV_D,
            "commit_id": REV_C,
            "file_path": "src/service.py",
            "subject": "Update target handling",
        }
    ]


def test_registry_bundle_version_is_v2_with_five_rules() -> None:
    assert RULE_BUNDLE_VERSION == "rules/v2"
    assert set(RULE_REGISTRY) == {
        "definition/v1",
        "call_edge/v1",
        "behavior/v1",
        "absence/v1",
        "history/v1",
    }
    assert "synthetic-test-only-authority-001" in APPROVED_BUILD_MAPPING_AUTHORITIES


def test_rule_for_kind_routes_new_kinds_and_abstains_causal() -> None:
    assert rule_for_kind(ClaimKind.BEHAVIOR).rule_id == "behavior/v1"
    assert rule_for_kind(ClaimKind.ABSENCE).rule_id == "absence/v1"
    assert rule_for_kind(ClaimKind.HISTORY).rule_id == "history/v1"
    assert rule_for_kind(ClaimKind.CAUSAL) is None
    assert rule_for_kind("DEFINITION").rule_id == "definition/v1"


def test_swapped_validator_binding_fails_closed() -> None:
    rule = get_rule("behavior/v1").model_copy(
        update={"validator": "domain.proof.rules:validate_definition_premises"}
    )
    outcome, _ = apply_rule(rule, _behavior_claim(), _behavior_premises())
    assert outcome == RuleOutcome.INADMISSIBLE_PREMISE


def test_behavior_discharge_golden() -> None:
    outcome, _ = validate_behavior_premises(_behavior_claim(), _behavior_premises())
    assert outcome == RuleOutcome.DISCHARGED


def test_behavior_discharge_through_apply_rule() -> None:
    outcome, _ = apply_rule(get_rule("behavior/v1"), _behavior_claim(), _behavior_premises())
    assert outcome == RuleOutcome.DISCHARGED


def test_behavior_missing_runtime_unsupported() -> None:
    premises = [p for p in _behavior_premises() if p["premise_type"] != "RUNTIME_OBSERVATION_FACT"]
    outcome, _ = validate_behavior_premises(_behavior_claim(), premises)
    assert outcome == RuleOutcome.MISSING_PREMISE


def test_behavior_missing_code_unsupported() -> None:
    premises = [p for p in _behavior_premises() if p["premise_type"] != "RANGE_READ_FACT"]
    outcome, _ = validate_behavior_premises(_behavior_claim(), premises)
    assert outcome == RuleOutcome.MISSING_PREMISE


def test_behavior_missing_mapping_unsupported() -> None:
    premises = [p for p in _behavior_premises() if p["premise_type"] != "BUILD_SOURCE_MAPPING"]
    outcome, _ = validate_behavior_premises(_behavior_claim(), premises)
    assert outcome == RuleOutcome.MISSING_PREMISE


def test_behavior_extra_premise_inadmissible() -> None:
    premises = _behavior_premises() + [dict(_behavior_premises()[0], evidence_id="ev-extra-001")]
    outcome, _ = validate_behavior_premises(_behavior_claim(), premises)
    assert outcome == RuleOutcome.INADMISSIBLE_PREMISE


def test_behavior_wrong_premise_type_missing() -> None:
    premises = _behavior_premises()
    premises[0] = dict(premises[0], premise_type="SEARCH_HIT")
    outcome, _ = validate_behavior_premises(_behavior_claim(), premises)
    # D4a order 2: the required RANGE_READ_FACT type is absent (the
    # SEARCH_HIT record does not satisfy the code-premise slot).
    assert outcome == RuleOutcome.MISSING_PREMISE


def test_behavior_missing_premise_wins_over_value_mismatch() -> None:
    premises = [p for p in _behavior_premises() if p["premise_type"] != "RUNTIME_OBSERVATION_FACT"]
    outcome, _ = validate_behavior_premises(_behavior_claim(predicate="other(x)"), premises)
    assert outcome == RuleOutcome.MISSING_PREMISE


def test_checker_unavailable_artifact_wins_over_predicate_mismatch() -> None:
    request = _behavior_check_request(runtime_override={"predicate": "other(x)"})
    emptied = request.model_copy(update={"artifacts": {}})
    outcome, _ = check_certificate(emptied)
    assert outcome == CheckerOutcome.PREMISE_UNAVAILABLE


def test_checker_cycle_rejected_for_behavior_certificate() -> None:
    request = _behavior_check_request()
    der_id = str(request.certificate["derivation_id"])
    cyclic = request.model_copy(update={"derivation_deps": {der_id: [der_id]}})
    assert check_certificate(cyclic)[0] == CheckerOutcome.INVALID_INFERENCE
    acyclic = request.model_copy(update={"derivation_deps": {"other": [der_id]}})
    assert check_certificate(acyclic)[0] == CheckerOutcome.VALID


def test_checker_stale_context_rejected_for_behavior_certificate() -> None:
    request = _behavior_check_request()
    stale = request.model_copy(update={"expected_context_id": "sha256:" + "0" * 64})
    assert check_certificate(stale)[0] == CheckerOutcome.STALE_CERTIFICATE
    fresh = request.model_copy(update={"expected_context_id": request.certificate["context_id"]})
    assert check_certificate(fresh)[0] == CheckerOutcome.VALID


def test_behavior_duplicate_evidence_id_inadmissible() -> None:
    premises = _behavior_premises()
    premises[1] = dict(premises[1], evidence_id="ev-code-001")
    outcome, _ = validate_behavior_premises(_behavior_claim(), premises)
    assert outcome == RuleOutcome.INADMISSIBLE_PREMISE


def test_behavior_predicate_mismatch_invalid() -> None:
    outcome, _ = validate_behavior_premises(
        _behavior_claim(predicate="other(x)"), _behavior_premises()
    )
    assert outcome == RuleOutcome.INVALID_INFERENCE


def test_behavior_value_mismatch_invalid() -> None:
    outcome, _ = validate_behavior_premises(
        _behavior_claim(asserted_value=500), _behavior_premises()
    )
    assert outcome == RuleOutcome.INVALID_INFERENCE


def test_behavior_bool_is_not_int() -> None:
    outcome, _ = validate_behavior_premises(
        _behavior_claim(asserted_value=True), _behavior_premises()
    )
    assert outcome == RuleOutcome.INVALID_INFERENCE


def test_behavior_float_asserted_value_inadmissible() -> None:
    outcome, _ = validate_behavior_premises(
        _behavior_claim(asserted_value=200.5), _behavior_premises()
    )
    assert outcome == RuleOutcome.INADMISSIBLE_PREMISE


def test_behavior_build_mismatch_invalid() -> None:
    outcome, _ = validate_behavior_premises(
        _behavior_claim(build_identity="build-002"), _behavior_premises()
    )
    assert outcome == RuleOutcome.INVALID_INFERENCE


def test_behavior_environment_mismatch_invalid() -> None:
    outcome, _ = validate_behavior_premises(
        _behavior_claim(environment="production"), _behavior_premises()
    )
    assert outcome == RuleOutcome.INVALID_INFERENCE


def test_behavior_unapproved_mapping_abstains() -> None:
    premises = _behavior_premises()
    premises[2] = dict(premises[2], provenance_ref="self-asserted-map")
    outcome, _ = validate_behavior_premises(_behavior_claim(), premises)
    assert outcome == RuleOutcome.UNSUPPORTED_CONSTRUCT


def test_behavior_mapping_tuple_mismatch_invalid() -> None:
    premises = _behavior_premises()
    premises[2] = dict(premises[2], source_revision=REV_E)
    outcome, _ = validate_behavior_premises(_behavior_claim(), premises)
    assert outcome == RuleOutcome.INVALID_INFERENCE


def test_behavior_wrong_header_unsupported() -> None:
    outcome, _ = validate_behavior_premises(_behavior_claim(form="EXISTENCE"), _behavior_premises())
    assert outcome == RuleOutcome.UNSUPPORTED_CONSTRUCT


def test_behavior_unknown_claim_field_inadmissible() -> None:
    outcome, _ = validate_behavior_premises(_behavior_claim(extra_field="x"), _behavior_premises())
    assert outcome == RuleOutcome.INADMISSIBLE_PREMISE


def test_behavior_code_window_must_show_target() -> None:
    premises = _behavior_premises()
    premises[0] = dict(premises[0], window_text="def unrelated():\n    pass\n")
    outcome, reasons = validate_behavior_premises(_behavior_claim(), premises)
    assert outcome == RuleOutcome.INVALID_INFERENCE
    assert any("window" in reason for reason in [reasons])


def test_absence_discharge_golden() -> None:
    outcome, _ = validate_absence_premises(
        _absence_claim(), _absence_premises(), _absence_manifest()
    )
    assert outcome == RuleOutcome.DISCHARGED


def test_absence_discharge_through_apply_rule_with_manifest() -> None:
    outcome, _ = apply_rule(
        get_rule("absence/v1"), _absence_claim(), _absence_premises(), _absence_manifest()
    )
    assert outcome == RuleOutcome.DISCHARGED


def test_absence_missing_manifest_is_missing() -> None:
    outcome, _ = validate_absence_premises(_absence_claim(), _absence_premises(), None)
    assert outcome == RuleOutcome.MISSING_PREMISE


def test_absence_empty_premises_is_missing() -> None:
    outcome, _ = validate_absence_premises(_absence_claim(), [], _absence_manifest())
    assert outcome == RuleOutcome.MISSING_PREMISE


def test_absence_unbound_coverage_id_is_missing() -> None:
    manifest = _absence_manifest()
    manifest["evidence_ids"] = []
    outcome, _ = validate_absence_premises(_absence_claim(), _absence_premises(), manifest)
    assert outcome == RuleOutcome.MISSING_PREMISE


def test_absence_hash_mismatch_inadmissible_at_rule_layer() -> None:
    # Rule layer has no PREMISE_UNAVAILABLE member: mismatched bytes are
    # inadmissible premises here. certify() and the checker report the
    # same condition as PREMISE_UNAVAILABLE (Component 1 ABS-011).
    manifest = _absence_manifest()
    manifest["content_hashes"] = {"ev-coverage-001": SHA_C}
    outcome, _ = validate_absence_premises(_absence_claim(), _absence_premises(), manifest)
    assert outcome == RuleOutcome.INADMISSIBLE_PREMISE


def test_absence_wrong_manifest_type_inadmissible() -> None:
    manifest = _absence_manifest()
    manifest["premise_types"] = {"ev-coverage-001": "RANGE_READ_FACT"}
    outcome, _ = validate_absence_premises(_absence_claim(), _absence_premises(), manifest)
    assert outcome == RuleOutcome.INADMISSIBLE_PREMISE


def test_absence_incomplete_search_unsupported() -> None:
    premises = _absence_premises()
    premises[0] = dict(premises[0], search_status="INCOMPLETE")
    outcome, _ = validate_absence_premises(_absence_claim(), premises, _absence_manifest())
    assert outcome == RuleOutcome.UNSUPPORTED_CONSTRUCT


def test_absence_failed_search_unsupported() -> None:
    premises = _absence_premises()
    premises[0] = dict(premises[0], search_status="FAILED")
    outcome, _ = validate_absence_premises(_absence_claim(), premises, _absence_manifest())
    assert outcome == RuleOutcome.UNSUPPORTED_CONSTRUCT


def test_absence_nonzero_match_invalid() -> None:
    premises = _absence_premises()
    premises[0] = dict(premises[0], match_count=1)
    outcome, _ = validate_absence_premises(_absence_claim(), premises, _absence_manifest())
    assert outcome == RuleOutcome.INVALID_INFERENCE


def test_absence_predicate_mismatch_invalid() -> None:
    outcome, _ = validate_absence_premises(
        _absence_claim(predicate="contains_symbol(other)"), _absence_premises(), _absence_manifest()
    )
    assert outcome == RuleOutcome.INVALID_INFERENCE


def test_absence_scope_mismatch_invalid() -> None:
    outcome, _ = validate_absence_premises(
        _absence_claim(path_scope="tests/"), _absence_premises(), _absence_manifest()
    )
    assert outcome == RuleOutcome.INVALID_INFERENCE


def test_absence_config_digest_mismatch_invalid() -> None:
    manifest = _absence_manifest()
    manifest["retrieval_config"] = dict(manifest["retrieval_config"], search_config_digest=SHA_D)
    outcome, _ = validate_absence_premises(_absence_claim(), _absence_premises(), manifest)
    assert outcome == RuleOutcome.INVALID_INFERENCE


def test_absence_exclusions_mismatch_invalid() -> None:
    manifest = _absence_manifest()
    manifest["exclusions"] = ["tests/"]
    outcome, _ = validate_absence_premises(_absence_claim(), _absence_premises(), manifest)
    assert outcome == RuleOutcome.INVALID_INFERENCE


def test_absence_relevant_exclusion_abstains() -> None:
    premises = _absence_premises()
    premises[0] = dict(premises[0], exclusions=["src/"])
    manifest = _absence_manifest()
    manifest["exclusions"] = ["src/"]
    outcome, _ = validate_absence_premises(_absence_claim(), premises, manifest)
    assert outcome == RuleOutcome.UNSUPPORTED_CONSTRUCT


def test_absence_wrong_header_unsupported() -> None:
    outcome, _ = validate_absence_premises(
        _absence_claim(form="EXISTENCE", quantifier="EXISTENTIAL"),
        _absence_premises(),
        _absence_manifest(),
    )
    assert outcome == RuleOutcome.UNSUPPORTED_CONSTRUCT


def test_absence_unknown_claim_field_inadmissible() -> None:
    outcome, _ = validate_absence_premises(
        _absence_claim(extra_field="x"), _absence_premises(), _absence_manifest()
    )
    assert outcome == RuleOutcome.INADMISSIBLE_PREMISE


def test_history_discharge_golden() -> None:
    outcome, _ = validate_history_premises(_history_claim(), _history_premises())
    assert outcome == RuleOutcome.DISCHARGED


def test_history_discharge_through_apply_rule() -> None:
    outcome, _ = apply_rule(get_rule("history/v1"), _history_claim(), _history_premises())
    assert outcome == RuleOutcome.DISCHARGED


def test_history_repository_mismatch_invalid() -> None:
    outcome, _ = validate_history_premises(
        _history_claim(repository="synthetic/other"), _history_premises()
    )
    assert outcome == RuleOutcome.INVALID_INFERENCE


def test_history_commit_mismatch_invalid() -> None:
    outcome, _ = validate_history_premises(_history_claim(commit_id=REV_E), _history_premises())
    assert outcome == RuleOutcome.INVALID_INFERENCE


def test_history_file_mismatch_invalid() -> None:
    outcome, _ = validate_history_premises(
        _history_claim(file_path="src/other.py"), _history_premises()
    )
    assert outcome == RuleOutcome.INVALID_INFERENCE


def test_history_subject_mismatch_invalid() -> None:
    outcome, _ = validate_history_premises(
        _history_claim(subject="Different change"), _history_premises()
    )
    assert outcome == RuleOutcome.INVALID_INFERENCE


def test_history_query_revision_mismatch_invalid() -> None:
    outcome, _ = validate_history_premises(
        _history_claim(repository_revision=REV_E), _history_premises()
    )
    assert outcome == RuleOutcome.INVALID_INFERENCE


def test_history_abbreviated_premise_sha_inadmissible() -> None:
    premises = _history_premises()
    premises[0] = dict(premises[0], commit_id="c" * 12)
    outcome, _ = validate_history_premises(_history_claim(), premises)
    assert outcome == RuleOutcome.INADMISSIBLE_PREMISE


def test_history_uppercase_premise_sha_inadmissible() -> None:
    premises = _history_premises()
    premises[0] = dict(premises[0], commit_id="C" * 40)
    outcome, _ = validate_history_premises(_history_claim(), premises)
    assert outcome == RuleOutcome.INADMISSIBLE_PREMISE


def test_history_abbreviated_claim_sha_inadmissible() -> None:
    outcome, _ = validate_history_premises(_history_claim(commit_id="main"), _history_premises())
    assert outcome == RuleOutcome.INADMISSIBLE_PREMISE


def test_history_null_optional_header_invalid() -> None:
    outcome, _ = validate_history_premises(_history_claim(relation=None), _history_premises())
    assert outcome == RuleOutcome.INVALID_INFERENCE


def test_history_unexpected_header_unsupported() -> None:
    outcome, _ = validate_history_premises(
        _history_claim(relation="RUNTIME_OBSERVATION"), _history_premises()
    )
    assert outcome == RuleOutcome.UNSUPPORTED_CONSTRUCT


def test_history_wrong_form_unsupported() -> None:
    outcome, _ = validate_history_premises(_history_claim(form="CAUSAL"), _history_premises())
    assert outcome == RuleOutcome.UNSUPPORTED_CONSTRUCT


def test_history_missing_premise() -> None:
    outcome, _ = validate_history_premises(_history_claim(), [])
    assert outcome == RuleOutcome.MISSING_PREMISE


def test_history_unknown_claim_field_inadmissible() -> None:
    outcome, _ = validate_history_premises(_history_claim(extra_field="x"), _history_premises())
    assert outcome == RuleOutcome.INADMISSIBLE_PREMISE


def test_composition_accepts_new_rule_nodes_and_deps() -> None:
    assert set(RULE_DEPS) == {
        "definition/v1",
        "call_edge/v1",
        "behavior/v1",
        "absence/v1",
        "history/v1",
    }
    node = make_node("der-b1", "behavior/v1", premise_ids=["ev-code-001"], depends_on=[])
    assert node.rule_id == "behavior/v1"
    assert "scope.build_identity" in RULE_DEPS["behavior/v1"]
    assert "scope.repository" in RULE_DEPS["behavior/v1"]
    assert "coverage.universe" in RULE_DEPS["absence/v1"]
    assert "history.commit" in RULE_DEPS["history/v1"]


def _strip_envelope(record: dict[str, Any]) -> dict[str, Any]:
    return {
        k: v for k, v in record.items() if k not in ("evidence_id", "content_hash", "premise_type")
    }


def _real_hash(artifact: dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical_dict_bytes(artifact)).hexdigest()


def _behavior_check_request(
    runtime_override: dict[str, Any] | None = None,
    authority_refs: list[str] | None = (AUTHORITY,),
    validation: dict[str, Any] | None = None,
) -> CheckRequest:
    premises = _behavior_premises()
    if runtime_override:
        premises[1] = dict(premises[1], **runtime_override)
    artifacts: dict[str, dict[str, Any]] = {}
    entries: list[dict[str, Any]] = []
    hashes: dict[str, str] = {}
    types: dict[str, str] = {}
    for record in premises:
        art = _strip_envelope(record)
        digest = _real_hash(art)
        pid = str(record["evidence_id"])
        artifacts[pid] = art
        entries.append(
            {
                "evidence_id": pid,
                "content_hash": digest,
                "premise_type": str(record["premise_type"]),
                "scope": {"revision": REV_A},
            }
        )
        hashes[pid] = digest
        types[pid] = str(record["premise_type"])
    proposition: dict[str, Any] = {
        "canon_version": "canon/v1",
        "predicate": "returns_status(target)",
        "quantifier": "OBSERVATIONAL",
        "form": "OBSERVATION",
        "relation": "RUNTIME_OBSERVATION",
        "modality": "RUNTIME",
        "asserted_value": 200,
        "target": {"symbol": "target", "file_path": "src/service.py"},
        "scope": {
            "repository": "synthetic/service",
            "revision": REV_A,
            "build_identity": "build-001",
            "execution_id": "exec-001",
            "environment": "test",
        },
    }
    manifest = build_manifest(
        [str(p["evidence_id"]) for p in premises],
        hashes,
        {"scope": "synthetic/service"},
        [],
        premise_types=types,
    )
    rule = get_rule("behavior/v1")
    # Bindings are a pinned literal (see _PINNED_BEHAVIOR_BINDINGS): the
    # gate must not import the implementation's own projection to satisfy
    # its own binding check. The projection itself is pinned separately
    # by test_claim_projection_matches_pinned_bindings.
    inference = {"rule": "behavior/v1", "bindings": dict(_PINNED_BEHAVIOR_BINDINGS)}
    derived = evaluate(
        EvaluationInputs(
            proposition=proposition,
            manifest=manifest,
            rule_bundle_version="rules/v2",
            rule_id=rule.rule_id,
            rule_version=rule.rule_version,
            implementation_digest=rule.implementation_digest,
            inference=inference,
        )
    )
    cert = {
        "assumptions": [],
        "canonical_proposition": {
            key: proposition[key]
            for key in (
                "predicate",
                "quantifier",
                "canon_version",
                "form",
                "relation",
                "modality",
                "asserted_value",
            )
        },
        "context_id": derived.context_id,
        "derivation_id": derived.derivation_id,
        "implementation_digest": rule.implementation_digest,
        "inference_instance": inference,
        "input_manifest_digest": derived.manifest_digest,
        "premises": entries,
        "proof_classification": "SOUND",
        "proposition_id": derived.proposition_id,
        "rule_id": rule.rule_id,
        "rule_version": rule.rule_version,
        "schema_version": CERT_SCHEMA_VERSION,
        "scope": dict(proposition["scope"]),
        "validation": validation
        if validation is not None
        else {"result": "REJECT", "reasons": ["producer must not self-accept; checker decides"]},
    }
    return CheckRequest(
        certificate=cert,
        proposition=proposition,
        manifest=manifest,
        rule_bundle_version="rules/v2",
        artifacts=artifacts,
        mapping_authority={"approved_provenance_refs": list(authority_refs)}
        if authority_refs is not None
        else None,
    )


def test_checker_semantics_module_imports_no_producer_validators() -> None:
    import investigation_agent_platform.domain.proof.checker_semantics as module

    source = Path(module.__file__).read_text(encoding="utf-8")
    for banned in (
        "proof.rules",
        "proof.certificate",
        "validate_definition_premises",
        "validate_call_edge_premises",
        "validate_behavior_premises",
        "validate_absence_premises",
        "validate_history_premises",
    ):
        assert banned not in source, banned


def test_checker_accepts_valid_behavior_certificate() -> None:
    outcome, _ = check_certificate(_behavior_check_request())
    assert outcome == CheckerOutcome.VALID


def test_checker_ignores_producer_self_accept() -> None:
    request = _behavior_check_request(validation={"result": "ACCEPT", "reasons": ["producer"]})
    outcome, _ = check_certificate(request)
    assert outcome == CheckerOutcome.VALID


def test_checker_rejects_behavior_without_mapping_authority() -> None:
    outcome, reasons = check_certificate(_behavior_check_request(authority_refs=None))
    assert outcome == CheckerOutcome.UNSUPPORTED_CONSTRUCT
    assert any("authority" in reason for reason in reasons)


def test_checker_independent_of_monkeypatched_producer(monkeypatch: Any) -> None:
    import investigation_agent_platform.domain.proof.rules as rules_module

    monkeypatch.setattr(
        rules_module,
        "validate_behavior_premises",
        lambda claim, premises, manifest=None: (RuleOutcome.DISCHARGED, "always"),
    )
    outcome, _ = check_certificate(
        _behavior_check_request(runtime_override={"predicate": "other(x)"})
    )
    assert outcome == CheckerOutcome.INVALID_INFERENCE


def test_checker_rejects_behavior_predicate_mismatch() -> None:
    outcome, _ = check_certificate(
        _behavior_check_request(runtime_override={"predicate": "other(x)"})
    )
    assert outcome == CheckerOutcome.INVALID_INFERENCE


def test_checker_rejects_mismatched_rule_bundle_version() -> None:
    mismatched = _behavior_check_request().model_copy(update={"rule_bundle_version": "rules/v1"})
    outcome, reasons = check_certificate(mismatched)
    assert outcome == CheckerOutcome.CHECKER_FAILURE
    assert any("bundle" in reason for reason in reasons)


def test_checker_request_requires_bundle_version() -> None:
    payload = _behavior_check_request().model_dump()
    del payload["rule_bundle_version"]
    with pytest.raises(ValidationError):
        CheckRequest(**payload)


def test_certify_rejects_mismatched_rule_bundle_version() -> None:
    rule = get_rule("definition/v1")
    inputs = EvaluationInputs(
        proposition={"form": "EXISTENCE", "quantifier": "EXISTENTIAL"},
        manifest=build_manifest([], {}, premise_types={}),
        rule_bundle_version="rules/v1",
        rule_id=rule.rule_id,
        rule_version=rule.rule_version,
        implementation_digest=rule.implementation_digest,
    )
    with pytest.raises(CertificationError, match="UNSUPPORTED_BUNDLE"):
        certify(inputs, [], {})


def test_absence_semantics_direct_golden_and_mismatch() -> None:
    record = _absence_premises()[0]
    art = _strip_envelope(record)
    digest = _real_hash(art)
    manifest = build_manifest(
        ["ev-coverage-001"],
        {"ev-coverage-001": digest},
        {"scope": "repository revision path prefix", "search_config_digest": SHA_A},
        [],
        premise_types={"ev-coverage-001": "ABSENCE_COVERAGE_FACT"},
        coverage_evidence_id="ev-coverage-001",
        coverage_evidence={
            "premise_type": "ABSENCE_COVERAGE_FACT",
            "universe": "repository revision path prefix",
            "predicate": "contains_symbol(target)",
            "search_status": "COMPLETE_EMPTY",
            "search_config_digest": SHA_A,
            "scope": {"repository": "synthetic/service", "revision": REV_B, "path_scope": "src/"},
            "exclusions": [],
            "coverage_basis": {"kind": "synthetic_complete_inventory", "value": "all enumerated"},
            "match_count": 0,
        },
    )
    proposition = {
        "form": "ABSENCE",
        "quantifier": "UNIVERSAL",
        "modality": "STATIC",
        "completeness": "CLOSED_WORLD",
        "predicate": "contains_symbol(target)",
        "target": {"universe": "repository revision path prefix"},
        "scope": {"repository": "synthetic/service", "revision": REV_B, "path_scope": "src/"},
    }
    entry = {
        "evidence_id": "ev-coverage-001",
        "content_hash": digest,
        "premise_type": "ABSENCE_COVERAGE_FACT",
        "scope": {"revision": REV_B},
    }
    cert = {"premises": [entry], "scope": dict(proposition["scope"])}
    outcome, _ = check_absence_semantics(cert, proposition, manifest, {"ev-coverage-001": art})
    assert outcome == CheckerOutcome.VALID
    bad_prop = dict(proposition, predicate="contains_symbol(other)")
    outcome, _ = check_absence_semantics(cert, bad_prop, manifest, {"ev-coverage-001": art})
    assert outcome == CheckerOutcome.INVALID_INFERENCE


def test_history_semantics_direct_golden_and_forgery() -> None:
    record = _history_premises()[0]
    art = _strip_envelope(record)
    digest = _real_hash(art)
    manifest = build_manifest(
        ["ev-history-001"],
        {"ev-history-001": digest},
        {},
        [],
        premise_types={"ev-history-001": "COMMIT_HISTORY"},
    )
    proposition = {
        "form": "HISTORICAL",
        "quantifier": "EXISTENTIAL",
        "modality": "HISTORY",
        "predicate": "changed_at_commit(file, commit)",
        "target": {
            "repository": "synthetic/service",
            "commit_id": REV_C,
            "file_path": "src/service.py",
            "subject": "Update target handling",
        },
        "scope": {"repository_revision": REV_D},
    }
    entry = {
        "evidence_id": "ev-history-001",
        "content_hash": digest,
        "premise_type": "COMMIT_HISTORY",
        "scope": {"revision": REV_D},
    }
    cert = {"premises": [entry], "scope": dict(proposition["scope"])}
    outcome, _ = check_history_semantics(cert, proposition, manifest, {"ev-history-001": art})
    assert outcome == CheckerOutcome.VALID
    forged = dict(art, commit_id=REV_E)
    outcome, _ = check_history_semantics(cert, proposition, manifest, {"ev-history-001": forged})
    assert outcome == CheckerOutcome.PREMISE_UNAVAILABLE


def test_preimage_equals_normalized_model_payload_without_envelope() -> None:
    from investigation_agent_platform.domain.proof.premises import RangeReadFact

    record = _behavior_premises()[0]
    model = RangeReadFact.model_validate(record)
    payload = model.model_dump(mode="json", exclude={"evidence_id", "content_hash", "premise_type"})
    assert set(payload) == set(record) - {"evidence_id", "content_hash", "premise_type"}
    digest = "sha256:" + hashlib.sha256(canonical_dict_bytes(payload)).hexdigest()
    assert digest == _real_hash(_strip_envelope(record))


def test_preimage_ignores_stored_envelope_values() -> None:
    record = _behavior_premises()[0]
    baseline = _real_hash(_strip_envelope(record))
    relabeled = dict(record, evidence_id="ev-code-999", content_hash=SHA_B)
    assert set(_strip_envelope(relabeled)) == set(_strip_envelope(record))
    assert _real_hash(_strip_envelope(relabeled)) == baseline


def test_claim_projection_matches_pinned_bindings() -> None:
    from investigation_agent_platform.domain.proof.certificate import _claim_for_rule

    behavior_proposition = {
        "canon_version": "canon/v1",
        "predicate": "returns_status(target)",
        "quantifier": "OBSERVATIONAL",
        "form": "OBSERVATION",
        "relation": "RUNTIME_OBSERVATION",
        "modality": "RUNTIME",
        "asserted_value": 200,
        "target": {"symbol": "target", "file_path": "src/service.py"},
        "scope": {
            "repository": "synthetic/service",
            "revision": REV_A,
            "build_identity": "build-001",
            "execution_id": "exec-001",
            "environment": "test",
        },
    }
    assert _claim_for_rule(behavior_proposition, "behavior/v1") == _PINNED_BEHAVIOR_BINDINGS
    absence_proposition = {
        "canon_version": "canon/v1",
        "form": "ABSENCE",
        "quantifier": "UNIVERSAL",
        "modality": "STATIC",
        "completeness": "CLOSED_WORLD",
        "predicate": "contains_symbol(target)",
        "target": {"universe": "repository revision path prefix"},
        "scope": {"repository": "synthetic/service", "revision": REV_B, "path_scope": "src/"},
    }
    assert _claim_for_rule(absence_proposition, "absence/v1") == _PINNED_ABSENCE_BINDINGS
    history_proposition = {
        "canon_version": "canon/v1",
        "form": "HISTORICAL",
        "quantifier": "EXISTENTIAL",
        "modality": "HISTORY",
        "predicate": "changed_at_commit(file, commit)",
        "target": {
            "repository": "synthetic/service",
            "commit_id": REV_C,
            "file_path": "src/service.py",
            "subject": "Update target handling",
        },
        "scope": {"repository_revision": REV_D},
    }
    assert _claim_for_rule(history_proposition, "history/v1") == _PINNED_HISTORY_BINDINGS


def _certified_round_trip(
    rule_id: str,
    proposition: dict[str, Any],
    records: list[dict[str, Any]],
    manifest: Any,
    artifacts: dict[str, dict[str, Any]],
    premises: list[dict[str, Any]],
    universe: str | None = None,
    mapping_authority: dict[str, Any] | None = None,
) -> dict[str, Any]:
    rule = get_rule(rule_id)
    inputs = EvaluationInputs(
        proposition=proposition,
        manifest=manifest,
        rule_bundle_version="rules/v2",
        rule_id=rule.rule_id,
        rule_version=rule.rule_version,
        implementation_digest=rule.implementation_digest,
    )
    cert = certify(inputs, premises, artifacts, universe)
    assert cert["schema_version"] == CERT_SCHEMA_VERSION
    request = CheckRequest(
        certificate=cert,
        proposition=proposition,
        manifest=manifest,
        rule_bundle_version="rules/v2",
        artifacts=artifacts,
        mapping_authority=mapping_authority,
    )
    outcome, reasons = check_certificate(request)
    assert outcome == CheckerOutcome.VALID, reasons
    return cert


def test_certify_then_check_behavior_round_trip() -> None:
    records = _behavior_premises()
    artifacts: dict[str, dict[str, Any]] = {}
    premises: list[dict[str, Any]] = []
    hashes: dict[str, str] = {}
    types: dict[str, str] = {}
    for record in records:
        art = _strip_envelope(record)
        digest = _real_hash(art)
        pid = str(record["evidence_id"])
        artifacts[pid] = art
        hashes[pid] = digest
        types[pid] = str(record["premise_type"])
        premises.append(dict(record, content_hash=digest))
    proposition = {
        "canon_version": "canon/v1",
        "predicate": "returns_status(target)",
        "quantifier": "OBSERVATIONAL",
        "form": "OBSERVATION",
        "relation": "RUNTIME_OBSERVATION",
        "modality": "RUNTIME",
        "asserted_value": 200,
        "target": {"symbol": "target", "file_path": "src/service.py"},
        "scope": {
            "repository": "synthetic/service",
            "revision": REV_A,
            "build_identity": "build-001",
            "execution_id": "exec-001",
            "environment": "test",
        },
    }
    manifest = build_manifest(
        [str(r["evidence_id"]) for r in records],
        hashes,
        {"scope": "synthetic/service"},
        [],
        premise_types=types,
    )
    _certified_round_trip(
        "behavior/v1",
        proposition,
        records,
        manifest,
        artifacts,
        premises,
        mapping_authority={"approved_provenance_refs": [AUTHORITY]},
    )


def test_certify_then_check_absence_round_trip() -> None:
    record = _absence_premises()[0]
    art = _strip_envelope(record)
    digest = _real_hash(art)
    artifacts = {"ev-coverage-001": art}
    premises = [dict(record, content_hash=digest)]
    manifest = build_manifest(
        ["ev-coverage-001"],
        {"ev-coverage-001": digest},
        {"scope": "repository revision path prefix", "search_config_digest": SHA_A},
        [],
        premise_types={"ev-coverage-001": "ABSENCE_COVERAGE_FACT"},
        coverage_evidence_id="ev-coverage-001",
        coverage_evidence={
            "premise_type": "ABSENCE_COVERAGE_FACT",
            "universe": "repository revision path prefix",
            "predicate": "contains_symbol(target)",
            "search_status": "COMPLETE_EMPTY",
            "search_config_digest": SHA_A,
            "scope": {"repository": "synthetic/service", "revision": REV_B, "path_scope": "src/"},
            "exclusions": [],
            "coverage_basis": {"kind": "synthetic_complete_inventory", "value": "all enumerated"},
            "match_count": 0,
        },
        coverage=ManifestCoverage.COMPLETE_EMPTY,
    )
    proposition = {
        "canon_version": "canon/v1",
        "form": "ABSENCE",
        "quantifier": "UNIVERSAL",
        "modality": "STATIC",
        "completeness": "CLOSED_WORLD",
        "predicate": "contains_symbol(target)",
        "target": {"universe": "repository revision path prefix"},
        "scope": {"repository": "synthetic/service", "revision": REV_B, "path_scope": "src/"},
    }
    _certified_round_trip(
        "absence/v1",
        proposition,
        [record],
        manifest,
        artifacts,
        premises,
        universe="repository revision path prefix",
    )


def test_certify_then_check_history_round_trip() -> None:
    record = _history_premises()[0]
    art = _strip_envelope(record)
    digest = _real_hash(art)
    artifacts = {"ev-history-001": art}
    premises = [dict(record, content_hash=digest)]
    manifest = build_manifest(
        ["ev-history-001"],
        {"ev-history-001": digest},
        {},
        [],
        premise_types={"ev-history-001": "COMMIT_HISTORY"},
    )
    proposition = {
        "canon_version": "canon/v1",
        "form": "HISTORICAL",
        "quantifier": "EXISTENTIAL",
        "modality": "HISTORY",
        "predicate": "changed_at_commit(file, commit)",
        "target": {
            "repository": "synthetic/service",
            "commit_id": REV_C,
            "file_path": "src/service.py",
            "subject": "Update target handling",
        },
        "scope": {"repository_revision": REV_D},
    }
    _certified_round_trip("history/v1", proposition, [record], manifest, artifacts, premises)
