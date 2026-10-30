"""WP-B2 manifests + immutable evaluation: frozen-contract tests.

P = Evaluate(C, E, R, M): manifest digests bind premises by content;
missing or hash-mismatched premises fail typed (K10
PREMISE_UNAVAILABLE) instead of proving; reruns are deterministic and
inputs are never mutated — new evidence yields new IDs.
"""

from __future__ import annotations

from typing import Any

import pytest

from investigation_agent_platform.domain.proof.canonical import canonical_bytes
from investigation_agent_platform.domain.proof.evaluation import (
    Derivation,
    EvaluationInputs,
    PremiseUnavailable,
    evaluate,
)
from investigation_agent_platform.domain.proof.manifest import (
    ManifestConstructionError,
    ManifestCoverage,
    build_manifest,
    manifest_digest,
)


def _h(seed: str) -> str:
    import hashlib

    return "sha256:" + hashlib.sha256(seed.encode()).hexdigest()


def _prop() -> dict[str, Any]:
    return {
        "canon_version": "canon/v1",
        "predicate": "defined(OrderService)",
        "quantifier": "EXISTENTIAL",
    }


def _inputs(**over: Any) -> EvaluationInputs:
    base: dict[str, Any] = {
        "proposition": _prop(),
        "manifest": build_manifest(
            ["e1", "e2"],
            {"e1": _h("a"), "e2": _h("b")},
            premise_types={"e1": "RANGE_READ_FACT", "e2": "RANGE_READ_FACT"},
        ),
        "rule_bundle_version": "rules/v2",
        "model_encoding": {"model": "m", "encoding": "utf-8"},
        "rule_id": "definition/v1",
        "rule_version": "v1",
        "implementation_digest": "sha256:" + "0" * 64,
    }
    base.update(over)
    return EvaluationInputs(**base)


def _typed(ids: list[str]) -> dict[str, str]:
    return {pid: "RANGE_READ_FACT" for pid in ids}


def test_manifest_digest_binds_content_and_order() -> None:
    m1 = build_manifest(
        ["e1", "e2"], {"e1": _h("a"), "e2": _h("b")}, premise_types=_typed(["e1", "e2"])
    )
    m2 = build_manifest(
        ["e1", "e2"], {"e1": _h("a"), "e2": _h("b")}, premise_types=_typed(["e1", "e2"])
    )
    assert manifest_digest(m1) == manifest_digest(m2)
    assert manifest_digest(m1).startswith("sha256:")
    reordered = build_manifest(
        ["e2", "e1"], {"e1": _h("a"), "e2": _h("b")}, premise_types=_typed(["e1", "e2"])
    )
    assert manifest_digest(reordered) != manifest_digest(m1)


def test_manifest_rejects_premise_type_key_mismatch() -> None:
    with pytest.raises(ManifestConstructionError) as excinfo:
        build_manifest(
            ["e1", "e2"], {"e1": _h("a"), "e2": _h("b")}, premise_types={"e1": "RANGE_READ_FACT"}
        )
    assert excinfo.value.code == "PREMISE_TYPE_MISMATCH"


def test_manifest_rejects_missing_content_hash_key() -> None:
    with pytest.raises(ManifestConstructionError) as excinfo:
        build_manifest(["e1", "e2"], {"e1": _h("a")}, premise_types=_typed(["e1", "e2"]))
    assert excinfo.value.code == "CONTENT_HASH_KEY_MISMATCH"


def test_manifest_rejects_extra_content_hash_key() -> None:
    with pytest.raises(ManifestConstructionError) as excinfo:
        build_manifest(["e1"], {"e1": _h("a"), "e2": _h("b")}, premise_types=_typed(["e1"]))
    assert excinfo.value.code == "CONTENT_HASH_KEY_MISMATCH"


def test_manifest_rejects_unknown_premise_type() -> None:
    with pytest.raises(ManifestConstructionError) as excinfo:
        build_manifest(["e1"], {"e1": _h("a")}, premise_types={"e1": "SEARCH_HIT"})
    assert excinfo.value.code == "UNKNOWN_PREMISE_TYPE"


def test_manifest_coverage_binding_requires_complete_linkage() -> None:
    with pytest.raises(ManifestConstructionError) as excinfo:
        build_manifest(
            ["ev-coverage-001"],
            {"ev-coverage-001": _h("a")},
            premise_types={"ev-coverage-001": "ABSENCE_COVERAGE_FACT"},
            coverage_evidence_id="ev-coverage-001",
        )
    assert excinfo.value.code == "COVERAGE_BINDING_MISMATCH"
    with pytest.raises(ManifestConstructionError) as excinfo:
        build_manifest(
            ["ev-coverage-001"],
            {"ev-coverage-001": _h("a")},
            premise_types={"ev-coverage-001": "RANGE_READ_FACT"},
            coverage_evidence_id="ev-coverage-001",
            coverage_evidence={
                "premise_type": "ABSENCE_COVERAGE_FACT",
                "universe": "u",
                "predicate": "p",
                "search_status": "COMPLETE_EMPTY",
                "search_config_digest": _h("cfg"),
                "scope": {"repository": "r", "revision": "a" * 40, "path_scope": "src/"},
                "exclusions": [],
                "coverage_basis": {"kind": "k", "value": "v"},
                "match_count": 0,
            },
        )
    assert excinfo.value.code == "COVERAGE_BINDING_MISMATCH"


def test_manifest_rejects_duplicates_and_bad_hashes() -> None:
    with pytest.raises(ManifestConstructionError) as excinfo:
        build_manifest(["e1", "e1"], {"e1": _h("a")})
    assert excinfo.value.code == "DUPLICATE_PREMISE_ID"
    with pytest.raises(ManifestConstructionError) as excinfo:
        build_manifest(["e1"], {"e1": "not-a-hash"})
    assert excinfo.value.code == "BAD_CONTENT_HASH"
    with pytest.raises(ManifestConstructionError) as excinfo:
        build_manifest(["e1"], {"e1": "sha256:" + "zz" * 32})
    assert excinfo.value.code == "BAD_CONTENT_HASH"


def test_empty_match_manifest_evaluates_for_absence() -> None:
    manifest = build_manifest(
        [], {}, {"scope": "repo@rev:/:query=q"}, coverage=ManifestCoverage.COMPLETE_EMPTY
    )
    out = evaluate(_inputs(manifest=manifest), available_hashes={})
    assert isinstance(out, Derivation)
    assert out.premise_ids == []


def test_missing_premise_is_unavailable_not_proof() -> None:
    with pytest.raises(PremiseUnavailable) as excinfo:
        evaluate(_inputs(), available_hashes={"e1": _h("a")})
    assert excinfo.value.missing == ["e2"]
    assert excinfo.value.mismatched == []


def test_hash_mismatch_is_unavailable() -> None:
    with pytest.raises(PremiseUnavailable) as excinfo:
        evaluate(_inputs(), available_hashes={"e1": _h("a"), "e2": _h("WRONG")})
    assert excinfo.value.missing == []
    assert excinfo.value.mismatched == ["e2"]


def test_rerun_deterministic_and_inputs_immutable() -> None:
    inputs = _inputs()
    before = inputs.model_dump(mode="json")
    first = evaluate(inputs, available_hashes={"e1": _h("a"), "e2": _h("b")})
    second = evaluate(inputs, available_hashes={"e1": _h("a"), "e2": _h("b")})
    assert first == second
    assert inputs.model_dump(mode="json") == before
    assert first.manifest_digest == manifest_digest(inputs.manifest)


def test_new_evidence_yields_new_ids_old_records_untouched() -> None:
    old = evaluate(_inputs(), available_hashes={"e1": _h("a"), "e2": _h("b")})
    new_manifest = build_manifest(
        ["e1", "e2", "e3"],
        {"e1": _h("a"), "e2": _h("b"), "e3": _h("c")},
        premise_types={"e1": "RANGE_READ_FACT", "e2": "RANGE_READ_FACT", "e3": "RANGE_READ_FACT"},
    )
    new = evaluate(
        _inputs(manifest=new_manifest),
        available_hashes={"e1": _h("a"), "e2": _h("b"), "e3": _h("c")},
    )
    assert new.manifest_digest != old.manifest_digest
    assert new.context_id != old.context_id
    assert new.derivation_id != old.derivation_id
    assert new.proposition_id == old.proposition_id
    # Old record unchanged (frozen).
    assert old.premise_ids == ["e1", "e2"]


def test_invalid_proposition_fails_closed() -> None:
    from investigation_agent_platform.domain.proof.canonical import CanonicalizationError

    with pytest.raises(CanonicalizationError):
        evaluate(_inputs(proposition={"quantifier": "EXISTENTIAL"}))


def test_canonical_bytes_still_reproduce_spec_vectors() -> None:
    import json
    from pathlib import Path

    spec = json.loads(Path("tests/fixtures/proof_kernel/canon_spec_v1.json").read_text())
    for vec in spec["digest_vectors"]:
        assert canonical_bytes(vec["proposition"]).decode() == vec["canonical_bytes"]
