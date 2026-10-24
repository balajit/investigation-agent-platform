"""WP-B1 canonical identity (canon/v1): frozen contract tests.

Exercised against the fixture vectors: D1–D3 digest vectors reproduce
byte-for-byte, V1–V7 identity expectations hold, X1–X9 inputs fail with
their stable codes. Owner-decided rules under test: assumptions sort
before hashing (set semantics); paths strip ./, //, trailing slash
(case preserved; backslash rejected per X5).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from investigation_agent_platform.domain.proof.canonical import (
    CANON_VERSION,
    CanonError,
    CanonicalizationError,
    canonical_bytes,
    context_id,
    decision_id,
    derivation_id,
    normalize_path,
    proposition_id,
    same_proposition,
)

FIXDIR = Path("tests/fixtures/proof_kernel")


def _load(name: str) -> Any:
    return json.loads((FIXDIR / name).read_text(encoding="utf-8"))


def test_spec_digest_vectors_reproduce_byte_for_byte() -> None:
    spec = _load("canon_spec_v1.json")
    assert spec["canon_version"] == CANON_VERSION
    for vec in spec["digest_vectors"]:
        raw = canonical_bytes(vec["proposition"])
        assert raw.decode("utf-8") == vec["canonical_bytes"]
        assert "sha256:" + hashlib.sha256(raw).hexdigest() == vec["proposition_id"]
        assert proposition_id(vec["proposition"]) == vec["proposition_id"]


def _vectors() -> dict[str, dict[str, Any]]:
    doc = _load("canon_v1_vectors.json")
    return {v["vector_id"]: v for v in doc["vectors"]}


def test_v1_predicate_strengthening_is_new_proposition() -> None:
    vec = _vectors()["V1-predicate-strengthening"]
    assert same_proposition(vec["proposition_a"], vec["proposition_b"]) is False


def test_v2_line_move_keeps_proposition_new_context() -> None:
    vec = _vectors()["V2-line-move"]
    assert same_proposition(vec["proposition_a"], vec["proposition_b"]) is True
    assert vec["expected"]["same_context"] is False


def test_v3_unvalidated_rename_does_not_merge() -> None:
    vec = _vectors()["V3-rename-unvalidated"]
    assert same_proposition(vec["proposition_a"], vec["proposition_b"]) is False


def test_v4_validated_rename_merges_with_registry_op() -> None:
    vec = _vectors()["V4-rename-validated"]
    assert same_proposition(vec["proposition_a"], vec["proposition_b"]) is False
    assert (
        same_proposition(
            vec["proposition_a"],
            vec["proposition_b"],
            known_normalization_ops=frozenset({"validated-rename"}),
            normalization=vec["normalization"],
        )
        is True
    )


def test_v4_bare_op_never_authorizes() -> None:
    vec = _vectors()["V4-rename-validated"]
    bare = {"op_id": "validated-rename"}
    assert (
        same_proposition(
            vec["proposition_a"],
            vec["proposition_b"],
            known_normalization_ops=frozenset({"validated-rename"}),
            normalization=bare,
        )
        is False
    )


def test_v5_quantifier_split_v6_scope_broadening_v7_revision() -> None:
    vecs = _vectors()
    assert (
        same_proposition(
            vecs["V5-quantifier-split"]["proposition_a"],
            vecs["V5-quantifier-split"]["proposition_b"],
        )
        is False
    )
    assert (
        same_proposition(
            vecs["V6-scope-broadening"]["proposition_a"],
            vecs["V6-scope-broadening"]["proposition_b"],
        )
        is True
    )
    assert (
        same_proposition(
            vecs["V7-revision-change"]["proposition_a"],
            vecs["V7-revision-change"]["proposition_b"],
        )
        is True
    )


def test_invalid_vectors_raise_stable_codes() -> None:
    doc = _load("canon_invalid_vectors.json")
    expected = {entry["id"]: entry["expected_error"] for entry in doc["invalid"]}
    assert len(expected) == 9
    for entry in doc["invalid"]:
        with pytest.raises(CanonicalizationError) as excinfo:
            canonical_bytes(entry["input"])
        assert excinfo.value.code == entry["expected_error"], entry["id"]


def test_assumptions_sort_before_hashing() -> None:
    base: dict[str, Any] = {
        "canon_version": "canon/v1",
        "predicate": "calls(A,B)",
        "quantifier": "EXISTENTIAL",
    }
    assert canonical_bytes({**base, "assumptions": ["b", "a"]}) == canonical_bytes(
        {**base, "assumptions": ["a", "b"]}
    )
    with pytest.raises(CanonicalizationError) as excinfo:
        canonical_bytes({**base, "assumptions": ["a", "a"]})
    assert excinfo.value.code == CanonError.DUPLICATE_ASSUMPTIONS.value


def test_path_strip_rules_and_backslash_rejection() -> None:
    assert normalize_path("./svc.py") == "svc.py"
    assert normalize_path("a//b.py") == "a/b.py"
    assert normalize_path("svc.py/") == "svc.py"
    assert normalize_path("Src/Svc.py") == "Src/Svc.py"
    with pytest.raises(CanonicalizationError) as excinfo:
        normalize_path("src\\svc.py")
    assert excinfo.value.code == CanonError.BAD_PATH.value
    base: dict[str, Any] = {
        "canon_version": "canon/v1",
        "predicate": "defined(S)",
        "quantifier": "EXISTENTIAL",
        "target": {"file_path": "./svc.py", "symbol": "S"},
    }
    stripped = dict(base)
    stripped["target"] = {"file_path": "svc.py", "symbol": "S"}
    assert canonical_bytes(base) == canonical_bytes(stripped)


def test_float_and_nfd_inputs_rejected() -> None:
    with pytest.raises(CanonicalizationError) as excinfo:
        canonical_bytes(
            {"canon_version": "canon/v1", "predicate": "p", "quantifier": "EXISTENTIAL",
             "scope": {"confidence": 0.5}}
        )
    assert excinfo.value.code == CanonError.INVALID_FLOAT.value
    with pytest.raises(CanonicalizationError) as excinfo:
        canonical_bytes(
            {"canon_version": "canon/v1", "predicate": "défini", "quantifier": "EXISTENTIAL"}
        )
    assert excinfo.value.code == CanonError.NOT_NORMALIZED.value


def test_context_derivation_decision_ids_are_stable_and_distinct() -> None:
    pid = proposition_id(
        {"canon_version": "canon/v1", "predicate": "defined(S)", "quantifier": "EXISTENTIAL"}
    )
    assert pid.startswith("sha256:") and len(pid) == len("sha256:") + 64
    ctx_a = context_id(pid, {"revision": "rev-1"})
    assert ctx_a == context_id(pid, {"revision": "rev-1"})
    assert ctx_a != context_id(pid, {"revision": "rev-2"})
    der = derivation_id(ctx_a, "call_edge/v1", "v1", "sha256:" + "0" * 64, ["a", "b"], {})
    assert der == derivation_id(ctx_a, "call_edge/v1", "v1", "sha256:" + "0" * 64, ["a", "b"], {})
    assert der != derivation_id(
        ctx_a, "call_edge/v1", "v1", "sha256:" + "0" * 64, ["a", "c"], {}
    )
    dec = decision_id(der, "cert-1", "gate/v3", {"fresh": True}, "clean")
    assert dec == decision_id(der, "cert-1", "gate/v3", {"fresh": True}, "clean")
    assert dec != decision_id(der, "cert-1", "gate/v3", {"fresh": False}, "clean")
    for value in (ctx_a, der, dec):
        assert value.startswith("sha256:") and len(value) == len("sha256:") + 64


def _minimal_proposition(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "canon_version": "canon/v1",
        "predicate": "defined(S)",
        "quantifier": "EXISTENTIAL",
    }
    base.update(over)
    return base


def test_unknown_top_level_field_rejected() -> None:
    with pytest.raises(CanonicalizationError) as excinfo:
        canonical_bytes(_minimal_proposition(bogus="x"))
    assert excinfo.value.code == CanonError.UNKNOWN_FIELD.value


def test_unknown_target_field_rejected() -> None:
    with pytest.raises(CanonicalizationError) as excinfo:
        canonical_bytes(_minimal_proposition(target={"symbol": "S", "bogus": 1}))
    assert excinfo.value.code == CanonError.UNKNOWN_FIELD.value


def test_context_keys_tolerated_and_unhashed() -> None:
    target = {"symbol": "S", "file_path": "svc.py"}
    base = _minimal_proposition(target=dict(target))
    base_id = proposition_id(base)
    enriched = _minimal_proposition(
        revision="rev-9",
        commit="abc123",
        configuration="prod",
        environment="staging",
        execution="run-7",
        scope={"configuration": "prod", "execution": "x", "revision": "rev-9"},
        file_path="svc.py",
        line_start=10,
        line_end=40,
        evidence_manifest_digest="sha256:" + "0" * 64,
        rule_bundle_version="rules/v1",
        model_config={"model": "m"},
        reconciliation_state="unreconciled",
        target={
            "symbol": "S",
            "file_path": "svc.py",
            "line_start": 10,
            "line_end": 40,
            "revision": "rev-9",
            "commit": "abc123",
        },
    )
    assert proposition_id(enriched) == base_id


def test_normalization_requires_pair_subjects() -> None:
    vec = _vectors()["V4-rename-validated"]
    a = vec["proposition_a"]
    b = vec["proposition_b"]
    norm = dict(vec["normalization"])
    ops = frozenset({"validated-rename"})
    assert same_proposition(a, b, ops, normalization=norm) is True
    reversed_subjects = dict(norm)
    reversed_subjects["subjects"] = [norm["subjects"][1], norm["subjects"][0]]
    assert same_proposition(a, b, ops, normalization=reversed_subjects) is True
    bare = dict(norm)
    del bare["subjects"]
    assert same_proposition(a, b, ops, normalization=bare) is False
    wrong = dict(norm)
    wrong["subjects"] = [norm["subjects"][0], "sha256:" + "f" * 64]
    assert same_proposition(a, b, ops, normalization=wrong) is False
    other = _vectors()["V1-predicate-strengthening"]
    assert (
        same_proposition(
            other["proposition_a"],
            other["proposition_b"],
            ops,
            normalization=norm,
        )
        is False
    )
