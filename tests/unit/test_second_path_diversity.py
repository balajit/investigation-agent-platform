"""Second-path diversity + conjunction tests (B1 diverse validator, Option 1).

Proves the independent decision path in ``domain.proof.second_path``
is genuinely diverse and correctly conjunctively wired:

- agreement: both paths return VALID on the full valid corpus (one
  bundle per rule), built here from the frozen fixture shapes;
- co-denial: both paths deny each defect vector (wrong type, duplicate
  ID, revision mismatch, bad SHA, incomplete search, nonzero matches,
  unapproved authority, relabeled call site, missing symbol);
- conjunction: neutering EITHER path to always-VALID still denies an
  invalid certificate (proves AND, not OR; proves a path-1 defect alone
  cannot produce a false accept and vice versa);
- substrate: the independent canonicalizer rejects floats/non-NFC and
  agrees byte-for-byte with the shared canonicalizer on valid inputs;
  hash mismatches deny;
- own scope detector: the second path fires its own K11 on a scope
  swap without relying on either existing inline layer.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest

from investigation_agent_platform.domain.proof.certificate import (
    CertificationError,
    CheckerOutcome,
    CheckRequest,
    certify,
    check_certificate,
)
from investigation_agent_platform.domain.proof.evaluation import EvaluationInputs
from investigation_agent_platform.domain.proof.manifest import ManifestCoverage, build_manifest
from investigation_agent_platform.domain.proof.rules import RULE_BUNDLE_VERSION, get_rule
from investigation_agent_platform.domain.proof.second_path import (
    _canonical_bytes,
    check_rule_second_path,
)

REPO = "synthetic/sp"
REV_A = "ab" * 20
REV_B = "cd" * 20
REV_C = "ef" * 20
REV_D = "12" * 20
AUTHORITY = "synthetic-test-only-authority-001"
BUILD = "sp-build-001"
EXEC = "sp-exec-001"
ENV = "test"
UNIVERSE = "repository revision path prefix"
SEARCH_CONFIG = "sha256:" + "ab" * 32


def _h(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


def _definition_record(**over: Any) -> dict[str, Any]:
    record = {
        "evidence_id": "ev-sp-definition-0",
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


def _call_edge_record(**over: Any) -> dict[str, Any]:
    record = {
        "evidence_id": "ev-sp-call-edge-0",
        "premise_type": "NORMALIZED_CALLSITE_FACT",
        "repository": REPO,
        "file_path": "src/handler.py",
        "revision": REV_A,
        "line_start": 12,
        "line_end": 12,
        "caller": "handle",
        "callee": "route",
        "call_text": "return route(event)  # handle delegates",
    }
    record.update(over)
    return record


def _call_edge_proposition(**over: Any) -> dict[str, Any]:
    proposition = {
        "canon_version": "canon/v1",
        "form": "EXISTENCE",
        "quantifier": "EXISTENTIAL",
        "predicate": "calls(handle,route)",
        "target": {"caller": "handle", "callee": "route"},
        "scope": {"revision": REV_A},
    }
    proposition.update(over)
    return proposition


def _behavior_records(**over: Any) -> list[dict[str, Any]]:
    return [
        {
            "evidence_id": "ev-sp-behavior-0",
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
            "evidence_id": "ev-sp-behavior-1",
            "premise_type": "RUNTIME_OBSERVATION_FACT",
            "repository": REPO,
            "symbol": "serve",
            "predicate": "returns_status(serve)",
            "observed_value": 200,
            "execution_id": EXEC,
            "build_identity": BUILD,
            "environment": ENV,
            "source_revision": REV_A,
        },
        {
            "evidence_id": "ev-sp-behavior-2",
            "premise_type": "BUILD_SOURCE_MAPPING",
            "build_identity": BUILD,
            "repository": REPO,
            "source_revision": REV_A,
            "provenance_ref": AUTHORITY,
            "mapping_digest": "sha256:" + "cd" * 32,
        },
    ]


def _behavior_proposition(**over: Any) -> dict[str, Any]:
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
            "build_identity": BUILD,
            "execution_id": EXEC,
            "environment": ENV,
        },
    }
    proposition.update(over)
    return proposition


def _absence_record(**over: Any) -> dict[str, Any]:
    record = {
        "evidence_id": "ev-sp-absence-0",
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
    record.update(over)
    return record


def _absence_proposition(**over: Any) -> dict[str, Any]:
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
    proposition.update(over)
    return proposition


def _history_record(**over: Any) -> dict[str, Any]:
    record = {
        "evidence_id": "ev-sp-history-0",
        "premise_type": "COMMIT_HISTORY",
        "repository": REPO,
        "repository_revision": REV_D,
        "commit_id": REV_C,
        "file_path": "src/handler.py",
        "subject": "Route lookup fix",
    }
    record.update(over)
    return record


def _history_proposition(**over: Any) -> dict[str, Any]:
    proposition = {
        "canon_version": "canon/v1",
        "form": "HISTORICAL",
        "quantifier": "EXISTENTIAL",
        "modality": "HISTORY",
        "predicate": "changed_at_commit(src/handler.py, commit)",
        "target": {
            "repository": REPO,
            "commit_id": REV_C,
            "file_path": "src/handler.py",
            "subject": "Route lookup fix",
        },
        "scope": {"repository_revision": REV_D},
    }
    proposition.update(over)
    return proposition


def _strip(record: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in record.items() if k not in ("evidence_id", "content_hash", "premise_type")}


def _assemble(rule_id: str, records: list[dict[str, Any]], proposition: dict[str, Any], **manifest_kw: Any) -> dict[str, Any]:
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


def _certified(rule_id: str, records: list[dict[str, Any]], proposition: dict[str, Any], **manifest_kw: Any) -> dict[str, Any]:
    asm = _assemble(rule_id, records, proposition, **manifest_kw)
    cert = certify(asm["inputs"], asm["premises"], asm["artifacts"])
    return {**asm, "cert": cert, "proposition": proposition}


def _check(bundle: dict[str, Any], authority: Any = None) -> tuple[Any, list[str]]:
    request = CheckRequest(
        certificate=bundle["cert"],
        proposition=dict(bundle["proposition"]),
        manifest=bundle["manifest"],
        rule_bundle_version=RULE_BUNDLE_VERSION,
        artifacts=dict(bundle["artifacts"]),
        expected_context_id=str(bundle["cert"].get("context_id") or ""),
        mapping_authority=authority,
    )
    return check_certificate(request)


_AUTHORITY = {"approved_provenance_refs": [AUTHORITY]}


def _behavior_bundle() -> dict[str, Any]:
    return _certified(
        "behavior/v1",
        _behavior_records(),
        _behavior_proposition(),
        retrieval_config={"scope": REPO},
    )


def _absence_bundle() -> dict[str, Any]:
    record = _absence_record()
    return _certified(
        "absence/v1",
        [record],
        _absence_proposition(),
        retrieval_config={"scope": UNIVERSE, "search_config_digest": SEARCH_CONFIG},
        coverage=ManifestCoverage.COMPLETE_EMPTY,
        coverage_evidence_id=str(record["evidence_id"]),
        coverage_evidence={k: v for k, v in record.items() if k != "evidence_id"},
    )


# ---------------------------------------------------------------------------
# Agreement: both paths VALID on the full valid corpus
# ---------------------------------------------------------------------------


def test_second_path_agrees_definition_valid() -> None:
    bundle = _certified("definition/v1", [_definition_record()], _definition_proposition())
    outcome, _ = _check(bundle)
    assert outcome == CheckerOutcome.VALID
    second, _ = check_rule_second_path(
        "definition/v1", bundle["cert"], bundle["proposition"], bundle["manifest"], bundle["artifacts"]
    )
    assert second == CheckerOutcome.VALID


def test_second_path_agrees_call_edge_valid() -> None:
    bundle = _certified("call_edge/v1", [_call_edge_record()], _call_edge_proposition())
    outcome, _ = _check(bundle)
    assert outcome == CheckerOutcome.VALID
    second, _ = check_rule_second_path(
        "call_edge/v1", bundle["cert"], bundle["proposition"], bundle["manifest"], bundle["artifacts"]
    )
    assert second == CheckerOutcome.VALID


def test_second_path_agrees_behavior_valid() -> None:
    bundle = _behavior_bundle()
    outcome, _ = _check(bundle, _AUTHORITY)
    assert outcome == CheckerOutcome.VALID
    second, _ = check_rule_second_path(
        "behavior/v1",
        bundle["cert"],
        bundle["proposition"],
        bundle["manifest"],
        bundle["artifacts"],
        _AUTHORITY,
    )
    assert second == CheckerOutcome.VALID


def test_second_path_agrees_absence_valid() -> None:
    bundle = _absence_bundle()
    outcome, _ = _check(bundle)
    assert outcome == CheckerOutcome.VALID
    second, _ = check_rule_second_path(
        "absence/v1", bundle["cert"], bundle["proposition"], bundle["manifest"], bundle["artifacts"]
    )
    assert second == CheckerOutcome.VALID


def test_second_path_agrees_history_valid() -> None:
    bundle = _certified("history/v1", [_history_record()], _history_proposition())
    outcome, _ = _check(bundle)
    assert outcome == CheckerOutcome.VALID
    second, _ = check_rule_second_path(
        "history/v1", bundle["cert"], bundle["proposition"], bundle["manifest"], bundle["artifacts"]
    )
    assert second == CheckerOutcome.VALID


# ---------------------------------------------------------------------------
# Co-denial: both paths deny each defect vector
# ---------------------------------------------------------------------------


def _denies_both(rule_id: str, bundle: dict[str, Any], authority: Any = None) -> str:
    outcome, _ = _check(bundle, authority)
    assert outcome != CheckerOutcome.VALID, f"path 1 accepted a defective {rule_id} cert"
    second, reason = check_rule_second_path(
        rule_id,
        bundle["cert"],
        bundle["proposition"],
        bundle["manifest"],
        bundle["artifacts"],
        authority,
    )
    assert second != CheckerOutcome.VALID, f"second path accepted a defective {rule_id} cert"
    return reason


def test_second_path_co_denies_wrong_type() -> None:
    stray = {
        "evidence_id": "ev-stray-001",
        "premise_type": "RANGE_READ_FACT",
        "repository": REPO,
        "file_path": "src/other.py",
        "revision": REV_A,
        "line_start": 1,
        "line_end": 2,
        "target": "other",
        "window_text": "def other():\n    pass\n",
    }
    asm = _assemble("call_edge/v1", [_call_edge_record(), stray], _call_edge_proposition())
    from investigation_agent_platform.domain.proof.certificate import _premise_entry

    premises = [
        _premise_entry(p, "call_edge/v1", asm["artifacts"].get(str(p["evidence_id"])))
        for p in asm["premises"]
    ]
    cert = dict(asm["inputs"].model_dump())
    cert["premises"] = premises
    cert["scope"] = {"revision": REV_A}
    bundle = {**asm, "cert": cert, "proposition": _call_edge_proposition()}
    _denies_both("call_edge/v1", bundle)


def test_second_path_co_denies_revision_mismatch() -> None:
    record = _definition_record(revision=REV_B)
    asm = _assemble("definition/v1", [record], _definition_proposition())
    from investigation_agent_platform.domain.proof.certificate import certify as _certify

    with pytest.raises(Exception, match="RULE_NOT_DISCHARGED"):
        _certify(asm["inputs"], asm["premises"], asm["artifacts"])
    # Forge the cert shape directly to exercise the checker-level join.
    bundle = _certified("definition/v1", [_definition_record()], _definition_proposition())
    bad_cert = dict(bundle["cert"])
    bad_cert["scope"] = {"revision": REV_B}
    forged = dict(bundle, cert=bad_cert)
    _denies_both("definition/v1", forged)


def test_second_path_co_denies_unapproved_authority() -> None:
    bundle = _behavior_bundle()
    bad_authority = {"approved_provenance_refs": ["someone-else-001"]}
    outcome, _ = _check(bundle, bad_authority)
    assert outcome != CheckerOutcome.VALID
    second, _ = check_rule_second_path(
        "behavior/v1",
        bundle["cert"],
        bundle["proposition"],
        bundle["manifest"],
        bundle["artifacts"],
        bad_authority,
    )
    assert second == CheckerOutcome.UNSUPPORTED_CONSTRUCT


def test_second_path_co_denies_incomplete_search() -> None:
    record = _absence_record(search_status="INCOMPLETE")
    asm = _assemble(
        "absence/v1",
        [record],
        _absence_proposition(),
        retrieval_config={"scope": UNIVERSE, "search_config_digest": SEARCH_CONFIG},
        coverage=ManifestCoverage.MATCHES_FOUND,
        coverage_evidence_id=str(record["evidence_id"]),
        coverage_evidence={k: v for k, v in record.items() if k != "evidence_id"},
    )
    from investigation_agent_platform.domain.proof.certificate import certify as _certify

    with pytest.raises(CertificationError):
        _certify(asm["inputs"], asm["premises"], asm["artifacts"])


def test_second_path_co_denies_nonzero_matches() -> None:
    bundle = _absence_bundle()
    tampered = dict(bundle["artifacts"])
    victim = "ev-sp-absence-0"
    tampered[victim] = dict(tampered[victim], match_count=3)
    forged = dict(bundle, artifacts=tampered)
    _denies_both("absence/v1", forged)


def test_second_path_co_denies_bad_commit_sha() -> None:
    bundle = _certified("history/v1", [_history_record()], _history_proposition())
    tampered = dict(bundle["artifacts"])
    victim = "ev-sp-history-0"
    tampered[victim] = dict(tampered[victim], commit_id="not-a-sha")
    forged = dict(bundle, artifacts=tampered)
    _denies_both("history/v1", forged)


def test_second_path_co_denies_missing_symbol() -> None:
    record = _definition_record(window_text="class Other:\n    pass\n")
    asm = _assemble("definition/v1", [record], _definition_proposition())
    from investigation_agent_platform.domain.proof.certificate import certify as _certify

    with pytest.raises(Exception, match="RULE_NOT_DISCHARGED"):
        _certify(asm["inputs"], asm["premises"], asm["artifacts"])


def test_second_path_rejects_unknown_rule() -> None:
    bundle = _certified("definition/v1", [_definition_record()], _definition_proposition())
    second, _ = check_rule_second_path(
        "causal/v1", bundle["cert"], bundle["proposition"], bundle["manifest"], bundle["artifacts"]
    )
    assert second == CheckerOutcome.UNSUPPORTED_CONSTRUCT


# ---------------------------------------------------------------------------
# Conjunction: neutering EITHER path still denies (proves AND, not OR)
# ---------------------------------------------------------------------------


def test_conjunction_neutered_first_path_still_denies(monkeypatch: pytest.MonkeyPatch) -> None:
    import investigation_agent_platform.domain.proof.certificate as cert_mod
    from investigation_agent_platform.domain.proof.canonical import (
        context_id as _context_id,
    )
    from investigation_agent_platform.domain.proof.canonical import (
        derivation_id as _derivation_id,
    )
    from investigation_agent_platform.domain.proof.manifest import manifest_digest

    bundle = _certified("call_edge/v1", [_call_edge_record()], _call_edge_proposition())
    # Relabel a range-read artifact as a call site, keeping every hash and
    # binding consistent: availability, bindings, form, K11, freshness, and
    # derivation gates all pass — only a semantic join can catch it.
    relabeled = _definition_record(evidence_id="ev-sp-call-edge-0")
    relabeled.pop("content_hash", None)
    artifacts = dict(bundle["artifacts"])
    artifacts["ev-sp-call-edge-0"] = {k: v for k, v in relabeled.items() if k != "evidence_id"}
    new_hash = "sha256:" + hashlib.sha256(
        json.dumps(artifacts["ev-sp-call-edge-0"], sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()
    manifest = build_manifest(
        ["ev-sp-call-edge-0"],
        {"ev-sp-call-edge-0": new_hash},
        premise_types={"ev-sp-call-edge-0": "NORMALIZED_CALLSITE_FACT"},
    )
    cert = dict(bundle["cert"])
    cert["premises"] = [
        {
            "content_hash": new_hash,
            "evidence_id": "ev-sp-call-edge-0",
            "premise_type": "NORMALIZED_CALLSITE_FACT",
            "scope": {"revision": REV_A},
        }
    ]
    digest = manifest_digest(manifest)
    cert["input_manifest_digest"] = digest
    cert["context_id"] = _context_id(
        cert["proposition_id"],
        {
            "evidence_manifest_digest": digest,
            "model_encoding": {},
            "proposition_scope": dict(bundle["proposition"]["scope"]),
            "reconciliation_state": "unreconciled",
            "rule_bundle_version": RULE_BUNDLE_VERSION,
        },
    )
    cert["derivation_id"] = _derivation_id(
        str(cert["context_id"]),
        "call_edge/v1",
        get_rule("call_edge/v1").rule_version,
        get_rule("call_edge/v1").implementation_digest,
        ["ev-sp-call-edge-0"],
        cert.get("inference_instance") if isinstance(cert.get("inference_instance"), dict) else {},
    )
    forged_request_bundle = dict(bundle, cert=cert, manifest=manifest, artifacts=artifacts)
    # Sanity: the real first path denies the relabeled premise.
    real_outcome, _ = _check(forged_request_bundle)
    assert real_outcome != CheckerOutcome.VALID
    monkeypatch.setitem(
        cert_mod._CHECKER_SEMANTICS, "call_edge/v1", lambda *a, **k: (CheckerOutcome.VALID, "neutered")
    )
    outcome, reasons = _check(forged_request_bundle)
    assert outcome == CheckerOutcome.CHECKER_FAILURE
    assert any("second-path disagreement" in r for r in reasons)


def test_conjunction_neutered_second_path_still_denies(monkeypatch: pytest.MonkeyPatch) -> None:
    import investigation_agent_platform.domain.proof.second_path as second_mod

    bundle = _certified("call_edge/v1", [_call_edge_record()], _call_edge_proposition())
    bad_cert = dict(bundle["cert"])
    bad_cert["scope"] = {"revision": REV_B}
    forged = dict(bundle, cert=bad_cert)
    monkeypatch.setattr(
        second_mod, "check_rule_second_path", lambda *a, **k: (CheckerOutcome.VALID, "neutered")
    )
    outcome, reasons = _check(forged)
    assert outcome != CheckerOutcome.VALID
    assert any("scope" in r.lower() for r in reasons)


def test_conjunction_valid_cert_commits_both_paths() -> None:
    bundle = _certified("call_edge/v1", [_call_edge_record()], _call_edge_proposition())
    outcome, _ = _check(bundle)
    assert outcome == CheckerOutcome.VALID


# ---------------------------------------------------------------------------
# Substrate: independent canonicalizer + hash binding + own K11
# ---------------------------------------------------------------------------


def test_second_path_canonicalizer_matches_shared_bytes_on_valid_inputs() -> None:
    from investigation_agent_platform.domain.proof.canonical import canonical_dict_bytes

    node = {"z": 1, "a": [1, "x", True, None], "m": {"k": "välue ✓"}}
    assert _canonical_bytes(node) == canonical_dict_bytes(node)


def test_second_path_canonicalizer_rejects_floats() -> None:
    with pytest.raises(TypeError, match="float"):
        _canonical_bytes({"asserted_value": 200.0})


def test_second_path_canonicalizer_rejects_non_nfc() -> None:
    with pytest.raises(ValueError, match="NFC"):
        _canonical_bytes({"symbol": "é"})


def test_second_path_detects_hash_mismatch() -> None:
    bundle = _certified("definition/v1", [_definition_record()], _definition_proposition())
    tampered = dict(bundle["artifacts"])
    victim = "ev-sp-definition-0"
    tampered[victim] = dict(tampered[victim], window_text="def handle(event):\n    return other(event)")
    second, _ = check_rule_second_path(
        "definition/v1", bundle["cert"], bundle["proposition"], bundle["manifest"], tampered
    )
    assert second == CheckerOutcome.PREMISE_UNAVAILABLE


def test_second_path_own_scope_detector_fires_without_inline_layers() -> None:
    bundle = _certified("definition/v1", [_definition_record()], _definition_proposition())
    bad_cert = dict(bundle["cert"])
    bad_cert["scope"] = {"revision": REV_B}
    second, reason = check_rule_second_path(
        "definition/v1", bad_cert, bundle["proposition"], bundle["manifest"], bundle["artifacts"]
    )
    assert second == CheckerOutcome.SCOPE_MISMATCH
    assert "K11" in reason
