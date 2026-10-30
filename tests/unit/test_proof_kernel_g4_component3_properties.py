"""Component 3 composition properties + mutation sensitivity (G4C3).

P1 monotonicity (no strengthening without discharge), P2 refutation
sensitivity + removal recomputation, P3 scope non-escalation, P4
deterministic replay, P5 invalidation closure exhaustive to 4 nodes
(572 graphs x 4 scenarios), P6 provenance propagation. Pure,
deterministic, replay-trivial: no Hypothesis, no randomness, no network,
no database. Every expected outcome is a pinned literal from the D2
property inventory, never derived from the implementation under test.

Producer assertions go through ``apply_rule`` (certify refusal via
``certify`` raising ``RULE_NOT_DISCHARGED``); checker assertions go
through the independent ``checker_semantics`` functions (the exact path
``check_certificate`` dispatches to) so intentionally inadmissible
premise sets need no digest replumbing, plus full ``certify``-to-
``check_certificate`` round trips where the contract admits them.

D6 detector-mapping table (mutation -> edit anchor -> expected
detector -> actual result -> verdict). Mutations run separately in
scratch copies per the D3 procedure; this module only freezes the
mapping. Actual-result/verdict fields stay PENDING until a run fills
them in the traceability record, never here.

| ID | Target invariant | Edit anchor | Expected detector(s) | Actual result | Verdict |
|---|---|---|---|---|---|
| M1 | Absence exact-one-coverage | rules.py::validate_absence_premises: delete the len(covers) > 1 or len(rows) != 1 guard | test_absence_multi_coverage_rejects (existing test_absence_empty_premises_is_missing must still pass) | FIRED test_absence_multi_coverage_rejects + test_monotonicity_extra_premise_rejects_absence; empty_premises passed | DETECTED |
| M2 | Scope/revision equality | checker_semantics.py::_revision_scope_check: replace != with == | test_k11_direct_definition_revision_mismatch, test_k11_direct_call_edge_revision_mismatch, test_k11_direct_behavior_revision_mismatch (direct-semantics K11 detectors in test_proof_kernel_g4_component4_integration.py — re-pointed per R-C3-01; the end-to-end test_scope_swap_rejected/K11 names pass via the redundant certificate-level inline K11 at certificate.py:588-600 and do NOT fire on this mutant) | Mapped detectors did NOT fire (pass via the redundant certificate-level inline K11, certificate.py:588-600). FIRED instead: g5 stale/sensitivity trio, test_monotonicity_extra_valid_callsite_preserves_discharge, test_authority_withdrawal_rechecks_unsupported | DETECTED with mapping deviation (see traceability residual R-C3-01) |
| M3 | Semantic validation load-bearing | rules.py::validate_behavior_premises: return (DISCHARGED, neutered) unconditionally | all behavior invalid goldens, test_checker_rejects_behavior_predicate_mismatch, test_checker_independent_of_monkeypatched_producer | 23 behavior producer goldens fired incl. test_behavior_predicate_mismatch_invalid. test_checker_rejects_behavior_predicate_mismatch + monkeypatched-producer independence correctly did NOT fire (checker independent of producer) | DETECTED with mapping note (see R-C3-02) |
| M4 | Dependency edge completeness | composition.py::RULE_DEPS: remove scope.repository from behavior/v1 | test_scope_repository_change_invalidates_behavior_only, test_composition_accepts_new_rule_nodes_and_deps | All 3 fired (scope_repository C3 + oracle drift-pin + composition dep pin C2); P5 operational/undeclared still empty (D4 guard holds) | DETECTED |
| M5 | Complete-empty admission | rules.py::validate_absence_premises: accept search_status INCOMPLETE as discharging | test_absence_incomplete_search_unsupported, test_absence_failed_search_unsupported | test_absence_incomplete_search_unsupported FIRED; test_absence_failed_search_unsupported passed (mutation admits only INCOMPLETE, not FAILED — mapping over-specified) | DETECTED with mapping note (see R-C3-03) |
| M6 | Freshness pin load-bearing | certificate.py::check_certificate: ignore expected_context_id | test_k8_k15_stale_context_rejected, test_checker_stale_context_rejected_for_behavior_certificate, decision-level test_stale_context_denies_k8_k15 | Both check_certificate-level detectors FIRED (+ g5 stale end-to-end). test_stale_context_denies_k8_k15 passed (the linearization sub-step's own stale-context layer, untouched by M6) | DETECTED with mapping note (see R-C3-04) |
"""

from __future__ import annotations

import hashlib
from typing import Any

import pytest

from investigation_agent_platform.domain.proof.canonical import canonical_dict_bytes
from investigation_agent_platform.domain.proof.certificate import (
    CertificationError,
    CheckRequest,
    certify,
    check_certificate,
)
from investigation_agent_platform.domain.proof.checker_semantics import (
    check_absence_semantics,
    check_behavior_semantics,
    check_call_edge_semantics,
    check_definition_semantics,
    check_history_semantics,
)
from investigation_agent_platform.domain.proof.composition import (
    OPERATIONAL_CLASSES,
    RULE_DEPS,
    detect_cycle,
    invalidate_by_evidence,
    invalidate_derivations,
    make_node,
)
from investigation_agent_platform.domain.proof.evaluation import EvaluationInputs
from investigation_agent_platform.domain.proof.manifest import ManifestCoverage, build_manifest
from investigation_agent_platform.domain.proof.outcomes import CheckerOutcome
from investigation_agent_platform.domain.proof.publication import (
    _decide_linearization,
)
from investigation_agent_platform.domain.proof.rules import (
    RuleOutcome,
    apply_rule,
    get_rule,
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
RULE_BUNDLE = "rules/v2"


def _real_hash(artifact: dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical_dict_bytes(dict(artifact))).hexdigest()


def _strip_envelope(record: dict[str, Any]) -> dict[str, Any]:
    return {
        k: v for k, v in record.items() if k not in ("evidence_id", "content_hash", "premise_type")
    }


# ---------------------------------------------------------------------------
# Producer builders (mirror component-2 conventions)
# ---------------------------------------------------------------------------


def _definition_claim(**over: Any) -> dict[str, Any]:
    claim: dict[str, Any] = {"symbol": "OrderService", "file_path": "svc.py"}
    claim.update(over)
    return claim


def _definition_premise(**over: Any) -> dict[str, Any]:
    premise: dict[str, Any] = {
        "evidence_id": "ev-def-001",
        "content_hash": SHA_A,
        "premise_type": "RANGE_READ_FACT",
        "repository": "synthetic/service",
        "file_path": "svc.py",
        "revision": "rev-1",
        "line_start": 10,
        "line_end": 14,
        "target": "OrderService",
        "window_text": "class OrderService:\n    pass\n",
    }
    premise.update(over)
    return premise


def _call_edge_claim(**over: Any) -> dict[str, Any]:
    claim: dict[str, Any] = {"caller": "AuthFilter", "callee": "OrderService"}
    claim.update(over)
    return claim


def _call_edge_premise(evidence_id: str = "ev-edge-001", **over: Any) -> dict[str, Any]:
    premise: dict[str, Any] = {
        "evidence_id": evidence_id,
        "content_hash": SHA_A,
        "premise_type": "NORMALIZED_CALLSITE_FACT",
        "repository": "synthetic/service",
        "file_path": "svc.py",
        "revision": "rev-1",
        "line_start": 20,
        "line_end": 20,
        "caller": "AuthFilter",
        "callee": "OrderService",
        "call_text": "AuthFilter.check(); OrderService.create()",
    }
    premise.update(over)
    return premise


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


def _absence_manifest(**over: Any) -> dict[str, Any]:
    manifest: dict[str, Any] = {
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
    manifest.update(over)
    return manifest


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


# ---------------------------------------------------------------------------
# Golden certify setups (one per rule; reused by P2/P4/P6)
# ---------------------------------------------------------------------------


def _definition_setup() -> tuple[dict[str, Any], Any, list[dict[str, Any]], dict[str, Any]]:
    artifact = {
        "file_path": "svc.py",
        "repository": "synthetic/service",
        "revision": "rev-1",
        "line_start": 10,
        "line_end": 14,
        "target": "OrderService",
        "window_text": "class OrderService:\n    pass\n",
    }
    digest = _real_hash(artifact)
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
        "content_hash": digest,
        "evidence_id": "row-1",
        "premise_type": "RANGE_READ_FACT",
        "scope": {"revision": "rev-1"},
    }
    manifest = build_manifest(
        ["row-1"], {"row-1": digest}, premise_types={"row-1": "RANGE_READ_FACT"}
    )
    return proposition, manifest, [premise], {"row-1": artifact}


def _call_edge_setup(
    rows: tuple[str, ...] = ("row-1",),
) -> tuple[dict[str, Any], Any, list[dict[str, Any]], dict[str, Any]]:
    texts = {
        "row-1": "AuthFilter.check(); OrderService.create()",
        "row-2": "if ready: AuthFilter.check(); OrderService.create()",
    }
    artifacts = {
        rid: {
            "caller": "AuthFilter",
            "callee": "OrderService",
            "call_text": texts[rid],
            "file_path": "svc.py",
            "repository": "synthetic/service",
            "revision": "rev-1",
            "line_start": 20,
            "line_end": 20,
        }
        for rid in rows
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
            "content_hash": _real_hash(artifacts[rid]),
            "evidence_id": rid,
            "premise_type": "NORMALIZED_CALLSITE_FACT",
            "scope": {"revision": "rev-1"},
        }
        for rid in rows
    ]
    manifest = build_manifest(
        list(rows),
        {rid: _real_hash(artifacts[rid]) for rid in rows},
        premise_types=dict.fromkeys(rows, "NORMALIZED_CALLSITE_FACT"),
    )
    return proposition, manifest, premises, artifacts


def _behavior_setup() -> tuple[dict[str, Any], Any, list[dict[str, Any]], dict[str, Any]]:
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
    return proposition, manifest, premises, artifacts


def _absence_setup() -> tuple[dict[str, Any], Any, list[dict[str, Any]], dict[str, Any]]:
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
    return proposition, manifest, premises, artifacts


def _history_setup() -> tuple[dict[str, Any], Any, list[dict[str, Any]], dict[str, Any]]:
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
    return proposition, manifest, premises, artifacts


_SETUPS = {
    "definition/v1": _definition_setup,
    "call_edge/v1": _call_edge_setup,
    "behavior/v1": _behavior_setup,
    "absence/v1": _absence_setup,
    "history/v1": _history_setup,
}


def _certify_golden(rule_id: str) -> tuple[dict[str, Any], dict[str, Any], Any, dict[str, Any]]:
    """Certify the golden application for a rule; returns cert/prop/manifest/artifacts."""
    proposition, manifest, premises, artifacts = _SETUPS[rule_id]()
    rule = get_rule(rule_id)
    inputs = EvaluationInputs(
        proposition=proposition,
        manifest=manifest,
        rule_bundle_version=RULE_BUNDLE,
        rule_id=rule.rule_id,
        rule_version=rule.rule_version,
        implementation_digest=rule.implementation_digest,
    )
    universe = "repository revision path prefix" if rule_id == "absence/v1" else None
    cert = certify(inputs, premises, artifacts, universe)
    return cert, proposition, manifest, artifacts


def _check_request(
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
        "rule_bundle_version": RULE_BUNDLE,
        "artifacts": artifacts,
    }
    if cert.get("rule_id") == "behavior/v1" and "mapping_authority" not in over:
        over = {"mapping_authority": {"approved_provenance_refs": [AUTHORITY]}, **over}
    base.update(over)
    return CheckRequest(**base)


def _checker_entry(
    evidence_id: str, premise_type: str, content_hash: str, revision: str | None = None
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "evidence_id": evidence_id,
        "content_hash": content_hash,
        "premise_type": premise_type,
    }
    if revision is not None:
        entry["scope"] = {"revision": revision}
    return entry


# ---------------------------------------------------------------------------
# P1 — Monotonicity (no strengthening without discharge)
# ---------------------------------------------------------------------------


def test_monotonicity_extra_premise_rejects_definition() -> None:
    premises = [
        _definition_premise(),
        _definition_premise(evidence_id="ev-def-002", content_hash=SHA_B),
    ]
    outcome, _ = apply_rule(get_rule("definition/v1"), _definition_claim(), premises)
    assert outcome == RuleOutcome.MISSING_PREMISE
    entries = [
        _checker_entry("ev-def-001", "RANGE_READ_FACT", SHA_A, "rev-1"),
        _checker_entry("ev-def-002", "RANGE_READ_FACT", SHA_B, "rev-1"),
    ]
    cert = {"premises": entries, "scope": {"revision": "rev-1"}}
    proposition = {
        "form": "EXISTENCE",
        "quantifier": "EXISTENTIAL",
        "target": {"symbol": "OrderService", "file_path": "svc.py"},
    }
    manifest = build_manifest(
        ["ev-def-001", "ev-def-002"],
        {"ev-def-001": SHA_A, "ev-def-002": SHA_B},
        premise_types={
            "ev-def-001": "RANGE_READ_FACT",
            "ev-def-002": "RANGE_READ_FACT",
        },
    )
    checker_outcome, _ = check_definition_semantics(cert, proposition, manifest, {})
    assert checker_outcome == CheckerOutcome.MISSING_PREMISE


def test_monotonicity_extra_premise_rejects_behavior() -> None:
    extra = dict(_behavior_premises()[0], evidence_id="ev-code-002", content_hash=SHA_D)
    premises = _behavior_premises() + [extra]
    outcome, _ = apply_rule(get_rule("behavior/v1"), _behavior_claim(), premises)
    assert outcome == RuleOutcome.INADMISSIBLE_PREMISE
    entries = [
        _checker_entry("ev-code-001", "RANGE_READ_FACT", SHA_A),
        _checker_entry("ev-code-002", "RANGE_READ_FACT", SHA_D),
        _checker_entry("ev-runtime-001", "RUNTIME_OBSERVATION_FACT", SHA_B),
        _checker_entry("ev-build-map-001", "BUILD_SOURCE_MAPPING", SHA_C),
    ]
    cert = {"premises": entries, "scope": {}}
    proposition = {
        "form": "OBSERVATION",
        "quantifier": "OBSERVATIONAL",
        "relation": "RUNTIME_OBSERVATION",
        "modality": "RUNTIME",
        "predicate": "returns_status(target)",
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
        [e["evidence_id"] for e in entries],
        {e["evidence_id"]: e["content_hash"] for e in entries},
        premise_types={
            "ev-code-001": "RANGE_READ_FACT",
            "ev-code-002": "RANGE_READ_FACT",
            "ev-runtime-001": "RUNTIME_OBSERVATION_FACT",
            "ev-build-map-001": "BUILD_SOURCE_MAPPING",
        },
    )
    checker_outcome, _ = check_behavior_semantics(cert, proposition, manifest, {})
    assert checker_outcome == CheckerOutcome.INADMISSIBLE_PREMISE


def test_monotonicity_extra_premise_rejects_absence() -> None:
    wrong_type = {
        "evidence_id": "ev-stray-001",
        "content_hash": SHA_C,
        "premise_type": "RANGE_READ_FACT",
    }
    premises = _absence_premises() + [wrong_type]
    outcome, _ = apply_rule(get_rule("absence/v1"), _absence_claim(), premises, _absence_manifest())
    assert outcome == RuleOutcome.INADMISSIBLE_PREMISE
    entries = [
        _checker_entry("ev-coverage-001", "ABSENCE_COVERAGE_FACT", SHA_B),
        _checker_entry("ev-stray-001", "RANGE_READ_FACT", SHA_C),
    ]
    cert = {"premises": entries, "scope": {}}
    proposition = {
        "form": "ABSENCE",
        "quantifier": "UNIVERSAL",
        "modality": "STATIC",
        "completeness": "CLOSED_WORLD",
        "predicate": "contains_symbol(target)",
        "target": {"universe": "repository revision path prefix"},
        "scope": {"repository": "synthetic/service", "revision": REV_B, "path_scope": "src/"},
    }
    manifest = build_manifest(
        ["ev-coverage-001", "ev-stray-001"],
        {"ev-coverage-001": SHA_B, "ev-stray-001": SHA_C},
        premise_types={
            "ev-coverage-001": "ABSENCE_COVERAGE_FACT",
            "ev-stray-001": "RANGE_READ_FACT",
        },
    )
    checker_outcome, _ = check_absence_semantics(cert, proposition, manifest, {})
    assert checker_outcome == CheckerOutcome.INADMISSIBLE_PREMISE


def test_monotonicity_extra_premise_rejects_history() -> None:
    extra = dict(_history_premises()[0], evidence_id="ev-history-002", content_hash=SHA_B)
    premises = _history_premises() + [extra]
    outcome, _ = apply_rule(get_rule("history/v1"), _history_claim(), premises)
    assert outcome == RuleOutcome.INADMISSIBLE_PREMISE
    entries = [
        _checker_entry("ev-history-001", "COMMIT_HISTORY", SHA_A),
        _checker_entry("ev-history-002", "COMMIT_HISTORY", SHA_B),
    ]
    cert = {"premises": entries, "scope": {}}
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
    manifest = build_manifest(
        ["ev-history-001", "ev-history-002"],
        {"ev-history-001": SHA_A, "ev-history-002": SHA_B},
        premise_types={
            "ev-history-001": "COMMIT_HISTORY",
            "ev-history-002": "COMMIT_HISTORY",
        },
    )
    checker_outcome, _ = check_history_semantics(cert, proposition, manifest, {})
    assert checker_outcome == CheckerOutcome.INADMISSIBLE_PREMISE


def test_monotonicity_extra_valid_callsite_preserves_discharge() -> None:
    premises = [_call_edge_premise("ev-edge-001"), _call_edge_premise("ev-edge-002")]
    outcome, _ = apply_rule(get_rule("call_edge/v1"), _call_edge_claim(), premises)
    assert outcome == RuleOutcome.DISCHARGED
    proposition, manifest, cert_premises, artifacts = _call_edge_setup(("row-1", "row-2"))
    rule = get_rule("call_edge/v1")
    inputs = EvaluationInputs(
        proposition=proposition,
        manifest=manifest,
        rule_bundle_version=RULE_BUNDLE,
        rule_id=rule.rule_id,
        rule_version=rule.rule_version,
        implementation_digest=rule.implementation_digest,
    )
    cert = certify(inputs, cert_premises, artifacts)
    checker_outcome, _ = check_certificate(_check_request(cert, proposition, manifest, artifacts))
    assert checker_outcome == CheckerOutcome.VALID


def test_callsite_wrong_type_producer_checker_both_reject() -> None:
    stray = {
        "evidence_id": "ev-stray-001",
        "content_hash": SHA_B,
        "premise_type": "RANGE_READ_FACT",
    }
    premises = [_call_edge_premise(), stray]
    outcome, _ = apply_rule(get_rule("call_edge/v1"), _call_edge_claim(), premises)
    assert outcome == RuleOutcome.INADMISSIBLE_PREMISE
    entries = [
        _checker_entry("ev-edge-001", "NORMALIZED_CALLSITE_FACT", SHA_A, "rev-1"),
        _checker_entry("ev-stray-001", "RANGE_READ_FACT", SHA_B, "rev-1"),
    ]
    cert = {"premises": entries, "scope": {"revision": "rev-1"}}
    proposition = {
        "form": "EXISTENCE",
        "quantifier": "EXISTENTIAL",
        "target": {"caller": "AuthFilter", "callee": "OrderService"},
    }
    manifest = build_manifest(
        ["ev-edge-001", "ev-stray-001"],
        {"ev-edge-001": SHA_A, "ev-stray-001": SHA_B},
        premise_types={
            "ev-edge-001": "NORMALIZED_CALLSITE_FACT",
            "ev-stray-001": "RANGE_READ_FACT",
        },
    )
    checker_outcome, _ = check_call_edge_semantics(cert, proposition, manifest, {})
    assert checker_outcome == CheckerOutcome.INADMISSIBLE_PREMISE


def test_callsite_duplicate_id_producer_checker_both_reject() -> None:
    premises = [_call_edge_premise(), _call_edge_premise()]
    outcome, _ = apply_rule(get_rule("call_edge/v1"), _call_edge_claim(), premises)
    assert outcome == RuleOutcome.INADMISSIBLE_PREMISE
    entries = [
        _checker_entry("ev-edge-001", "NORMALIZED_CALLSITE_FACT", SHA_A, "rev-1"),
        _checker_entry("ev-edge-001", "NORMALIZED_CALLSITE_FACT", SHA_A, "rev-1"),
    ]
    cert = {"premises": entries, "scope": {"revision": "rev-1"}}
    proposition = {
        "form": "EXISTENCE",
        "quantifier": "EXISTENTIAL",
        "target": {"caller": "AuthFilter", "callee": "OrderService"},
    }
    manifest = build_manifest(
        ["ev-edge-001", "ev-edge-002"],
        {"ev-edge-001": SHA_A, "ev-edge-002": SHA_B},
        premise_types={
            "ev-edge-001": "NORMALIZED_CALLSITE_FACT",
            "ev-edge-002": "NORMALIZED_CALLSITE_FACT",
        },
    )
    checker_outcome, _ = check_call_edge_semantics(cert, proposition, manifest, {})
    assert checker_outcome == CheckerOutcome.INADMISSIBLE_PREMISE


def test_absence_multi_coverage_rejects() -> None:
    second = dict(
        _absence_premises()[0],
        evidence_id="ev-coverage-002",
        content_hash=SHA_C,
    )
    premises = _absence_premises() + [second]
    outcome, _ = apply_rule(get_rule("absence/v1"), _absence_claim(), premises, _absence_manifest())
    assert outcome == RuleOutcome.INADMISSIBLE_PREMISE
    entries = [
        _checker_entry("ev-coverage-001", "ABSENCE_COVERAGE_FACT", SHA_B),
        _checker_entry("ev-coverage-002", "ABSENCE_COVERAGE_FACT", SHA_C),
    ]
    cert = {"premises": entries, "scope": {}}
    proposition = {
        "form": "ABSENCE",
        "quantifier": "UNIVERSAL",
        "modality": "STATIC",
        "completeness": "CLOSED_WORLD",
        "predicate": "contains_symbol(target)",
        "target": {"universe": "repository revision path prefix"},
        "scope": {"repository": "synthetic/service", "revision": REV_B, "path_scope": "src/"},
    }
    manifest = build_manifest(
        ["ev-coverage-001", "ev-coverage-002"],
        {"ev-coverage-001": SHA_B, "ev-coverage-002": SHA_C},
        premise_types={
            "ev-coverage-001": "ABSENCE_COVERAGE_FACT",
            "ev-coverage-002": "ABSENCE_COVERAGE_FACT",
        },
    )
    checker_outcome, _ = check_absence_semantics(cert, proposition, manifest, {})
    assert checker_outcome == CheckerOutcome.INADMISSIBLE_PREMISE


# ---------------------------------------------------------------------------
# P2 — Refutation sensitivity + removal recomputation
# ---------------------------------------------------------------------------


def _assert_certify_refuses(
    rule_id: str,
    premises: list[dict[str, Any]],
    artifacts: dict[str, Any],
) -> None:
    proposition, manifest, _, _ = _SETUPS[rule_id]()
    rule = get_rule(rule_id)
    inputs = EvaluationInputs(
        proposition=proposition,
        manifest=manifest,
        rule_bundle_version=RULE_BUNDLE,
        rule_id=rule.rule_id,
        rule_version=rule.rule_version,
        implementation_digest=rule.implementation_digest,
    )
    universe = "repository revision path prefix" if rule_id == "absence/v1" else None
    with pytest.raises(CertificationError) as excinfo:
        certify(inputs, premises, artifacts, universe)
    assert excinfo.value.code == "RULE_NOT_DISCHARGED"


def _assert_checker_empty_premises_missing(rule_id: str) -> None:
    cert = {"premises": [], "scope": {}}
    manifest = build_manifest([], {}, premise_types={})
    if rule_id == "definition/v1":
        proposition = {
            "form": "EXISTENCE",
            "quantifier": "EXISTENTIAL",
            "target": {"symbol": "OrderService", "file_path": "svc.py"},
        }
        outcome, _ = check_definition_semantics(cert, proposition, manifest, {})
    elif rule_id == "call_edge/v1":
        proposition = {
            "form": "EXISTENCE",
            "quantifier": "EXISTENTIAL",
            "target": {"caller": "AuthFilter", "callee": "OrderService"},
        }
        outcome, _ = check_call_edge_semantics(cert, proposition, manifest, {})
    elif rule_id == "behavior/v1":
        proposition = {
            "form": "OBSERVATION",
            "quantifier": "OBSERVATIONAL",
            "relation": "RUNTIME_OBSERVATION",
            "modality": "RUNTIME",
            "predicate": "returns_status(target)",
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
        outcome, _ = check_behavior_semantics(cert, proposition, manifest, {})
    elif rule_id == "absence/v1":
        proposition = {
            "form": "ABSENCE",
            "quantifier": "UNIVERSAL",
            "modality": "STATIC",
            "completeness": "CLOSED_WORLD",
            "predicate": "contains_symbol(target)",
            "target": {"universe": "repository revision path prefix"},
            "scope": {
                "repository": "synthetic/service",
                "revision": REV_B,
                "path_scope": "src/",
            },
        }
        outcome, _ = check_absence_semantics(cert, proposition, manifest, {})
    else:
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
        outcome, _ = check_history_semantics(cert, proposition, manifest, {})
    assert outcome == CheckerOutcome.MISSING_PREMISE


def _assert_withdrawn_artifact_unavailable(rule_id: str, drop_pid: str) -> None:
    cert, proposition, manifest, artifacts = _certify_golden(rule_id)
    remaining = {pid: art for pid, art in artifacts.items() if pid != drop_pid}
    request = _check_request(cert, proposition, manifest, remaining)
    outcome, _ = check_certificate(request)
    assert outcome == CheckerOutcome.PREMISE_UNAVAILABLE


def test_removal_definition_range_read_typed() -> None:
    outcome, _ = apply_rule(get_rule("definition/v1"), _definition_claim(), [])
    assert outcome == RuleOutcome.MISSING_PREMISE
    _assert_checker_empty_premises_missing("definition/v1")
    _assert_certify_refuses("definition/v1", [], {})
    _assert_withdrawn_artifact_unavailable("definition/v1", "row-1")


def test_removal_call_edge_callsite_typed() -> None:
    outcome, _ = apply_rule(get_rule("call_edge/v1"), _call_edge_claim(), [])
    assert outcome == RuleOutcome.MISSING_PREMISE
    _assert_checker_empty_premises_missing("call_edge/v1")
    _assert_certify_refuses("call_edge/v1", [], {})
    _assert_withdrawn_artifact_unavailable("call_edge/v1", "row-1")


def test_removal_behavior_code_typed() -> None:
    premises = [p for p in _behavior_premises() if p["premise_type"] != "RANGE_READ_FACT"]
    outcome, _ = apply_rule(get_rule("behavior/v1"), _behavior_claim(), premises)
    assert outcome == RuleOutcome.MISSING_PREMISE
    _assert_checker_empty_premises_missing("behavior/v1")
    _, _, golden_premises, golden_artifacts = _behavior_setup()
    kept = [p for p in golden_premises if p["premise_type"] != "RANGE_READ_FACT"]
    kept_artifacts = {pid: art for pid, art in golden_artifacts.items() if pid != "ev-code-001"}
    _assert_certify_refuses("behavior/v1", kept, kept_artifacts)
    _assert_withdrawn_artifact_unavailable("behavior/v1", "ev-code-001")


def test_removal_behavior_runtime_typed() -> None:
    premises = [p for p in _behavior_premises() if p["premise_type"] != "RUNTIME_OBSERVATION_FACT"]
    outcome, _ = apply_rule(get_rule("behavior/v1"), _behavior_claim(), premises)
    assert outcome == RuleOutcome.MISSING_PREMISE
    _assert_checker_empty_premises_missing("behavior/v1")
    _, _, golden_premises, golden_artifacts = _behavior_setup()
    kept = [p for p in golden_premises if p["premise_type"] != "RUNTIME_OBSERVATION_FACT"]
    kept_artifacts = {pid: art for pid, art in golden_artifacts.items() if pid != "ev-runtime-001"}
    _assert_certify_refuses("behavior/v1", kept, kept_artifacts)
    _assert_withdrawn_artifact_unavailable("behavior/v1", "ev-runtime-001")


def test_removal_behavior_mapping_typed() -> None:
    premises = [p for p in _behavior_premises() if p["premise_type"] != "BUILD_SOURCE_MAPPING"]
    outcome, _ = apply_rule(get_rule("behavior/v1"), _behavior_claim(), premises)
    assert outcome == RuleOutcome.MISSING_PREMISE
    _assert_checker_empty_premises_missing("behavior/v1")
    _, _, golden_premises, golden_artifacts = _behavior_setup()
    kept = [p for p in golden_premises if p["premise_type"] != "BUILD_SOURCE_MAPPING"]
    kept_artifacts = {
        pid: art for pid, art in golden_artifacts.items() if pid != "ev-build-map-001"
    }
    _assert_certify_refuses("behavior/v1", kept, kept_artifacts)
    _assert_withdrawn_artifact_unavailable("behavior/v1", "ev-build-map-001")


def test_removal_absence_coverage_typed() -> None:
    outcome, _ = apply_rule(get_rule("absence/v1"), _absence_claim(), [], _absence_manifest())
    assert outcome == RuleOutcome.MISSING_PREMISE
    _assert_checker_empty_premises_missing("absence/v1")
    _assert_certify_refuses("absence/v1", [], {})
    _assert_withdrawn_artifact_unavailable("absence/v1", "ev-coverage-001")


def test_removal_history_commit_typed() -> None:
    outcome, _ = apply_rule(get_rule("history/v1"), _history_claim(), [])
    assert outcome == RuleOutcome.MISSING_PREMISE
    _assert_checker_empty_premises_missing("history/v1")
    _assert_certify_refuses("history/v1", [], {})
    _assert_withdrawn_artifact_unavailable("history/v1", "ev-history-001")


def test_removal_leftover_row_invalid_inference() -> None:
    leftover = _call_edge_premise(caller="SomeoneElse", callee="SomethingElse")
    outcome, _ = apply_rule(get_rule("call_edge/v1"), _call_edge_claim(), [leftover])
    assert outcome == RuleOutcome.INVALID_INFERENCE
    artifact = _strip_envelope(leftover)
    digest = _real_hash(artifact)
    entries = [_checker_entry(str(leftover["evidence_id"]), "NORMALIZED_CALLSITE_FACT", digest)]
    cert = {"premises": entries, "scope": {}}
    proposition = {
        "form": "EXISTENCE",
        "quantifier": "EXISTENTIAL",
        "target": {"caller": "AuthFilter", "callee": "OrderService"},
    }
    manifest = build_manifest(
        [str(leftover["evidence_id"])],
        {str(leftover["evidence_id"]): digest},
        premise_types={str(leftover["evidence_id"]): "NORMALIZED_CALLSITE_FACT"},
    )
    checker_outcome, _ = check_call_edge_semantics(
        cert, proposition, manifest, {str(leftover["evidence_id"]): artifact}
    )
    assert checker_outcome == CheckerOutcome.INVALID_INFERENCE


def test_removal_recomputes_derivation_ids() -> None:
    rule = get_rule("call_edge/v1")

    def _certify_rows(rows: tuple[str, ...]) -> dict[str, Any]:
        proposition, manifest, premises, artifacts = _call_edge_setup(rows)
        saved_premises = [dict(p) for p in premises]
        saved_ids = list(manifest.evidence_ids)
        inputs = EvaluationInputs(
            proposition=proposition,
            manifest=manifest,
            rule_bundle_version=RULE_BUNDLE,
            rule_id=rule.rule_id,
            rule_version=rule.rule_version,
            implementation_digest=rule.implementation_digest,
        )
        cert = certify(inputs, premises, artifacts)
        assert premises == saved_premises
        assert list(manifest.evidence_ids) == saved_ids
        return cert

    full = _certify_rows(("row-1", "row-2"))
    reduced = _certify_rows(("row-1",))
    assert full["derivation_id"] != reduced["derivation_id"]
    assert full["input_manifest_digest"] != reduced["input_manifest_digest"]


# ---------------------------------------------------------------------------
# P3 — Scope non-escalation
# ---------------------------------------------------------------------------


def test_scope_change_never_discharges_definition() -> None:
    # Definition carries no scope-bound claim keys: the producer enforces
    # source location instead, so the scope-shaped mutation that rejects
    # here is a dropped file path (source-location scope -> inadmissible).
    artifact = {
        "file_path": "",
        "repository": "synthetic/service",
        "revision": "rev-1",
        "line_start": 10,
        "line_end": 14,
        "target": "OrderService",
        "window_text": "class OrderService:\n    pass\n",
    }
    digest = _real_hash(artifact)
    premise = {
        **artifact,
        "evidence_id": "row-1",
        "content_hash": digest,
        "premise_type": "RANGE_READ_FACT",
    }
    outcome, _ = apply_rule(get_rule("definition/v1"), _definition_claim(), [premise])
    assert outcome == RuleOutcome.INADMISSIBLE_PREMISE
    assert outcome != RuleOutcome.DISCHARGED
    proposition, _, _, _ = _definition_setup()
    manifest = build_manifest(
        ["row-1"], {"row-1": digest}, premise_types={"row-1": "RANGE_READ_FACT"}
    )
    rule = get_rule("definition/v1")
    inputs = EvaluationInputs(
        proposition=proposition,
        manifest=manifest,
        rule_bundle_version=RULE_BUNDLE,
        rule_id=rule.rule_id,
        rule_version=rule.rule_version,
        implementation_digest=rule.implementation_digest,
    )
    with pytest.raises(CertificationError) as excinfo:
        certify(inputs, [premise], {"row-1": artifact})
    assert excinfo.value.code == "RULE_NOT_DISCHARGED"


def test_scope_change_never_discharges_call_edge() -> None:
    # Call-edge claims bind caller/callee only: dropping the callee binding
    # is the rejecting scope-shaped mutation at producer level.
    outcome, _ = apply_rule(
        get_rule("call_edge/v1"), {"caller": "AuthFilter"}, [_call_edge_premise()]
    )
    assert outcome == RuleOutcome.MISSING_PREMISE
    assert outcome != RuleOutcome.DISCHARGED
    proposition, manifest, _, _ = _call_edge_setup()
    callee_free = dict(proposition)
    callee_free["target"] = {"counterpart": "AuthFilter", "file_path": "svc.py"}
    rule = get_rule("call_edge/v1")
    inputs = EvaluationInputs(
        proposition=callee_free,
        manifest=manifest,
        rule_bundle_version=RULE_BUNDLE,
        rule_id=rule.rule_id,
        rule_version=rule.rule_version,
        implementation_digest=rule.implementation_digest,
    )
    _, _, golden_premises, golden_artifacts = _call_edge_setup()
    with pytest.raises(CertificationError) as excinfo:
        certify(inputs, golden_premises, golden_artifacts)
    assert excinfo.value.code == "RULE_NOT_DISCHARGED"


def test_scope_change_never_discharges_behavior() -> None:
    outcome, _ = apply_rule(
        get_rule("behavior/v1"), _behavior_claim(revision=REV_E), _behavior_premises()
    )
    assert outcome == RuleOutcome.INVALID_INFERENCE
    assert outcome != RuleOutcome.DISCHARGED
    proposition, manifest, _, _ = _behavior_setup()
    bumped = dict(proposition)
    bumped["scope"] = dict(proposition["scope"], revision=REV_E)
    rule = get_rule("behavior/v1")
    inputs = EvaluationInputs(
        proposition=bumped,
        manifest=manifest,
        rule_bundle_version=RULE_BUNDLE,
        rule_id=rule.rule_id,
        rule_version=rule.rule_version,
        implementation_digest=rule.implementation_digest,
    )
    _, _, golden_premises, golden_artifacts = _behavior_setup()
    with pytest.raises(CertificationError) as excinfo:
        certify(inputs, golden_premises, golden_artifacts)
    assert excinfo.value.code == "RULE_NOT_DISCHARGED"


def test_scope_change_never_discharges_absence() -> None:
    outcome, _ = apply_rule(
        get_rule("absence/v1"),
        _absence_claim(revision=REV_C),
        _absence_premises(),
        _absence_manifest(),
    )
    assert outcome == RuleOutcome.INVALID_INFERENCE
    assert outcome != RuleOutcome.DISCHARGED
    proposition, manifest, _, _ = _absence_setup()
    bumped = dict(proposition)
    bumped["scope"] = dict(proposition["scope"], revision=REV_C)
    rule = get_rule("absence/v1")
    inputs = EvaluationInputs(
        proposition=bumped,
        manifest=manifest,
        rule_bundle_version=RULE_BUNDLE,
        rule_id=rule.rule_id,
        rule_version=rule.rule_version,
        implementation_digest=rule.implementation_digest,
    )
    _, _, golden_premises, golden_artifacts = _absence_setup()
    with pytest.raises(CertificationError) as excinfo:
        certify(inputs, golden_premises, golden_artifacts, "repository revision path prefix")
    assert excinfo.value.code == "RULE_NOT_DISCHARGED"


def test_scope_change_never_discharges_history() -> None:
    outcome, _ = apply_rule(
        get_rule("history/v1"), _history_claim(repository_revision=REV_E), _history_premises()
    )
    assert outcome == RuleOutcome.INVALID_INFERENCE
    assert outcome != RuleOutcome.DISCHARGED
    proposition, manifest, _, _ = _history_setup()
    bumped = dict(proposition)
    bumped["scope"] = {"repository_revision": REV_E}
    rule = get_rule("history/v1")
    inputs = EvaluationInputs(
        proposition=bumped,
        manifest=manifest,
        rule_bundle_version=RULE_BUNDLE,
        rule_id=rule.rule_id,
        rule_version=rule.rule_version,
        implementation_digest=rule.implementation_digest,
    )
    _, _, golden_premises, golden_artifacts = _history_setup()
    with pytest.raises(CertificationError) as excinfo:
        certify(inputs, golden_premises, golden_artifacts)
    assert excinfo.value.code == "RULE_NOT_DISCHARGED"


def test_scope_revision_change_invalidates_dependents() -> None:
    nodes = {
        "A": make_node("A", "definition/v1", ["ev-1"], []),
        "B": make_node("B", "behavior/v1", ["ev-2"], ["A"]),
        "C": make_node("C", "history/v1", ["ev-3"], ["B"]),
    }
    invalidated = invalidate_by_evidence({"ev-1"}, {"scope.revision"}, nodes)
    assert invalidated == {"A", "B", "C"}


def test_scope_repository_change_invalidates_behavior_only() -> None:
    nodes = {
        "D": make_node("D", "definition/v1", ["ev-d"], []),
        "E": make_node("E", "call_edge/v1", ["ev-e"], ["D"]),
        "Bh": make_node("Bh", "behavior/v1", ["ev-b"], ["D"]),
        "Ab": make_node("Ab", "absence/v1", ["ev-a"], ["D"]),
        "H": make_node("H", "history/v1", ["ev-h"], ["Bh"]),
    }
    changed = {"ev-d", "ev-e", "ev-b", "ev-a", "ev-h"}
    invalidated = invalidate_by_evidence(changed, {"scope.repository"}, nodes)
    assert invalidated == {"Bh", "Ab", "H"}
    assert "D" not in invalidated and "E" not in invalidated


# ---------------------------------------------------------------------------
# P4 — Deterministic replay
# ---------------------------------------------------------------------------


def test_replay_digests_stable_definition() -> None:
    first = _certify_golden("definition/v1")[0]
    second = _certify_golden("definition/v1")[0]
    for key in ("proposition_id", "context_id", "input_manifest_digest", "derivation_id"):
        assert first[key] == second[key]


def test_replay_digests_stable_behavior() -> None:
    first = _certify_golden("behavior/v1")[0]
    second = _certify_golden("behavior/v1")[0]
    for key in ("proposition_id", "context_id", "input_manifest_digest", "derivation_id"):
        assert first[key] == second[key]


def test_replay_digests_stable_absence() -> None:
    first = _certify_golden("absence/v1")[0]
    second = _certify_golden("absence/v1")[0]
    for key in ("proposition_id", "context_id", "input_manifest_digest", "derivation_id"):
        assert first[key] == second[key]


def test_replay_digests_stable_history() -> None:
    first = _certify_golden("history/v1")[0]
    second = _certify_golden("history/v1")[0]
    for key in ("proposition_id", "context_id", "input_manifest_digest", "derivation_id"):
        assert first[key] == second[key]


def test_replay_digests_stable_call_edge() -> None:
    first = _certify_golden("call_edge/v1")[0]
    second = _certify_golden("call_edge/v1")[0]
    for key in ("proposition_id", "context_id", "input_manifest_digest", "derivation_id"):
        assert first[key] == second[key]


_SIX_ORDERS = (
    ("d0", "d1", "d2", "d3", "d4", "d5"),
    ("d5", "d4", "d3", "d2", "d1", "d0"),
    ("d1", "d2", "d3", "d4", "d5", "d0"),
    ("d3", "d4", "d5", "d0", "d1", "d2"),
    ("d0", "d2", "d4", "d1", "d3", "d5"),
    ("d5", "d3", "d1", "d4", "d2", "d0"),
    ("d1", "d0", "d3", "d2", "d5", "d4"),
    ("d2", "d5", "d0", "d4", "d1", "d3"),
)


def _six_node_specs() -> dict[str, tuple[str, list[str], list[str]]]:
    return {
        "d0": ("definition/v1", ["ev-t3-0-0"], []),
        "d1": ("call_edge/v1", ["ev-t3-1-0"], ["d0"]),
        "d2": ("behavior/v1", ["ev-t3-2-0"], ["d0", "d1"]),
        "d3": ("absence/v1", ["ev-t3-3-0"], ["d2"]),
        "d4": ("history/v1", ["ev-t3-4-0"], ["d2"]),
        "d5": ("definition/v1", ["ev-t3-5-0"], ["d3", "d4"]),
    }


def test_invalidation_insertion_order_invariant() -> None:
    specs = _six_node_specs()
    by_derivation = invalidate_derivations
    by_evidence = invalidate_by_evidence
    derivation_sets = []
    evidence_sets = []
    for order in _SIX_ORDERS:
        nodes = {nid: make_node(nid, *specs[nid]) for nid in order}
        derivation_sets.append(by_derivation({"d0"}, nodes))
        nodes = {nid: make_node(nid, *specs[nid]) for nid in order}
        evidence_sets.append(by_evidence({"ev-t3-0-0"}, {"evidence.content"}, nodes))
    for result in derivation_sets:
        assert result == derivation_sets[0]
    for result in evidence_sets:
        assert result == evidence_sets[0]
    assert derivation_sets[0] == {"d0", "d1", "d2", "d3", "d4", "d5"}
    assert evidence_sets[0] == {"d0", "d1", "d2", "d3", "d4", "d5"}


def test_decision_request_key_order_invariant() -> None:
    base = {
        "checker_outcome": CheckerOutcome.VALID,
        "certificate_context_id": "ctx-1",
        "expected_context_id": "ctx-1",
        "derivation_id": "der-1",
        "reconciliation_adjudicated": True,
        "policy_eligible": True,
    }
    keys = list(base)
    rotations = [keys[i:] + keys[:i] for i in range(len(keys))]
    seen: set[tuple[str, str]] = set()
    for rotation in rotations:
        args = {k: base[k] for k in rotation}
        decision, reason = _decide_linearization(
            checker_outcome=args["checker_outcome"],
            certificate_context_id=args["certificate_context_id"],
            expected_context_id=args["expected_context_id"],
            reconciliation_adjudicated=args["reconciliation_adjudicated"],
            policy_eligible=args["policy_eligible"],
        )
        seen.add((decision.value, reason))
    assert seen == {("COMMIT", "linearization checklist satisfied")}


# ---------------------------------------------------------------------------
# P5 — Invalidation closure, exhaustive to 4 nodes (572 graphs x 4 scenarios)
# ---------------------------------------------------------------------------

_ROUND_ROBIN = (
    "definition/v1",
    "call_edge/v1",
    "behavior/v1",
    "absence/v1",
    "history/v1",
)


def _premises_for(index: int) -> list[str]:
    return [f"ev-t3-{index}-{j}" for j in range(index % 3 + 1)]


def _all_labeled_graphs() -> list[tuple[int, int, dict[str, Any]]]:
    """All labeled DAGs with n <= 4 nodes; index sequential from 0 per n."""
    graphs: list[tuple[int, int, dict[str, Any]]] = []
    for size in (1, 2, 3, 4):
        pairs = sorted((a, b) for a in range(size) for b in range(size) if a != b)
        index = 0
        for mask in range(1 << len(pairs)):
            deps: dict[str, list[str]] = {f"d{a}": [] for a in range(size)}
            for bit, (a, b) in enumerate(pairs):
                if (mask >> bit) & 1:
                    deps[f"d{a}"].append(f"d{b}")
            if detect_cycle(deps):
                continue
            nodes = {
                f"d{i}": make_node(f"d{i}", _ROUND_ROBIN[i % 5], _premises_for(i), deps[f"d{i}"])
                for i in range(size)
            }
            graphs.append((size, index, nodes))
            index += 1
    return graphs


def _graph_cases() -> list[tuple[int, int, str, dict[str, Any], set[str], set[str]]]:
    """(size, index, scenario, nodes, changed_evidence, changed_classes)."""
    cases = []
    for size, index, nodes in _all_labeled_graphs():
        owned: dict[str, str] = {}
        for nid, node in nodes.items():
            for pid in node.premise_ids:
                owned[pid] = nid
        roots = [nid for nid, node in nodes.items() if not node.depends_on]
        root_evidence = {pid for pid, nid in owned.items() if nid in set(roots)}
        all_evidence = set(owned)
        cases.append((size, index, "a", nodes, root_evidence, {"evidence.content"}))
        cases.append((size, index, "b", nodes, set(root_evidence), {"budget"}))
        cases.append((size, index, "c", nodes, set(root_evidence), {"telemetry.retention"}))
        cases.append((size, index, "d", nodes, set(all_evidence), {"scope.repository"}))
    return cases


def _independent_oracle(
    nodes: dict[str, Any], changed_evidence: set[str], changed_classes: set[str]
) -> set[str]:
    """Stage-1 direct set via premises∩changed_evidence and
    changed_classes∩RULE_DEPS; stage-2 BFS over reverse depends_on edges."""
    direct = {
        nid
        for nid, node in nodes.items()
        if (set(node.premise_ids) & changed_evidence)
        and (set(changed_classes) & set(RULE_DEPS[node.rule_id]))
    }
    reverse: dict[str, set[str]] = {nid: set() for nid in nodes}
    for nid, node in nodes.items():
        for dep in node.depends_on:
            reverse[dep].add(nid)
    seen = set(direct)
    stack = list(direct)
    while stack:
        current = stack.pop()
        for dependent in reverse[current]:
            if dependent not in seen:
                seen.add(dependent)
                stack.append(dependent)
    return seen


def _assert_scenario(scenario: str) -> None:
    cases = [c for c in _graph_cases() if c[2] == scenario]
    assert len(cases) == 572, f"enumerator must cover 572 graphs, got {len(cases)}"
    for size, index, _, nodes, changed_evidence, changed_classes in cases:
        expected = _independent_oracle(nodes, changed_evidence, changed_classes)
        actual = invalidate_by_evidence(changed_evidence, changed_classes, nodes)
        edges = {nid: list(node.depends_on) for nid, node in nodes.items()}
        assert actual == expected, (
            f"n={size} graph={index} edges={edges} scenario={scenario}: "
            f"impl={sorted(actual)} oracle={sorted(expected)}"
        )


def test_exhaustive_closure_content() -> None:
    _assert_scenario("a")


def test_exhaustive_closure_operational_empty() -> None:
    assert "budget" in OPERATIONAL_CLASSES
    _assert_scenario("b")


def test_exhaustive_closure_undeclared_empty() -> None:
    assert "telemetry.retention" not in OPERATIONAL_CLASSES
    assert all("telemetry.retention" not in deps for deps in RULE_DEPS.values())
    _assert_scenario("c")


def test_exhaustive_closure_repository_filtered() -> None:
    _assert_scenario("d")


def test_exhaustive_oracle_assumes_declared_deps() -> None:
    # Drift pin for the hand-worked (d) example: behavior/v1 and
    # history/v1 declare scope.repository; definition/v1 and
    # call_edge/v1 do not. Any dep-row change fails loudly here instead
    # of silently invalidating the (d) scenario expectations.
    assert "scope.repository" in RULE_DEPS["behavior/v1"]
    assert "scope.repository" in RULE_DEPS["history/v1"]
    assert "scope.repository" not in RULE_DEPS["definition/v1"]
    assert "scope.repository" not in RULE_DEPS["call_edge/v1"]


# ---------------------------------------------------------------------------
# P6 — Provenance propagation (non-independence)
# ---------------------------------------------------------------------------


def test_mapping_change_propagates_to_dependents() -> None:
    nodes = {
        "der-beh": make_node(
            "der-beh", "behavior/v1", ["ev-code-001", "ev-run-001", "ev-map-001"], []
        ),
        "der-dep": make_node("der-dep", "definition/v1", ["ev-dep-001"], ["der-beh"]),
    }
    invalidated = invalidate_by_evidence({"ev-map-001"}, {"evidence.content"}, nodes)
    assert invalidated == {"der-beh", "der-dep"}


def test_exclusion_addition_invalidates_absence() -> None:
    nodes = {
        "der-abs": make_node("der-abs", "absence/v1", ["ev-cov-001"], []),
        "der-other": make_node("der-other", "definition/v1", ["ev-other-001"], []),
    }
    invalidated = invalidate_by_evidence({"ev-cov-001"}, {"coverage.exclusions"}, nodes)
    assert invalidated == {"der-abs"}


def test_authority_withdrawal_rechecks_unsupported() -> None:
    cert, proposition, manifest, artifacts = _certify_golden("behavior/v1")
    approved = _check_request(cert, proposition, manifest, artifacts)
    outcome, _ = check_certificate(approved)
    assert outcome == CheckerOutcome.VALID
    withdrawn = _check_request(cert, proposition, manifest, artifacts, mapping_authority=None)
    outcome, reasons = check_certificate(withdrawn)
    assert outcome == CheckerOutcome.UNSUPPORTED_CONSTRUCT
    assert any("authority" in reason for reason in reasons)
    emptied = _check_request(
        cert, proposition, manifest, artifacts, mapping_authority={"approved_provenance_refs": []}
    )
    assert check_certificate(emptied)[0] == CheckerOutcome.UNSUPPORTED_CONSTRUCT


def test_revision_bump_never_discharges_definition() -> None:
    claim = _definition_claim(revision="rev-9")
    outcome, _ = apply_rule(get_rule("definition/v1"), claim, [_definition_premise()])
    assert outcome == RuleOutcome.INVALID_INFERENCE


def test_revision_bump_never_discharges_call_edge() -> None:
    claim = _call_edge_claim(revision="rev-9")
    outcome, _ = apply_rule(get_rule("call_edge/v1"), claim, [_call_edge_premise()])
    assert outcome == RuleOutcome.INVALID_INFERENCE


def test_missing_premise_revision_inadmissible_when_claim_pinned() -> None:
    claim = _definition_claim(revision="rev-1")
    premise = _definition_premise()
    del premise["revision"]
    outcome, _ = apply_rule(get_rule("definition/v1"), claim, [premise])
    assert outcome == RuleOutcome.INADMISSIBLE_PREMISE


def test_unpinned_claim_keeps_legacy_discharge() -> None:
    outcome, _ = apply_rule(get_rule("definition/v1"), _definition_claim(), [_definition_premise()])
    assert outcome == RuleOutcome.DISCHARGED
    outcome, _ = apply_rule(get_rule("call_edge/v1"), _call_edge_claim(), [_call_edge_premise()])
    assert outcome == RuleOutcome.DISCHARGED
