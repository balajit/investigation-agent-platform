# tests/unit/test_proof_kernel_fixture_integrity.py
"""Self-check for proof-kernel fixtures (TEST-ONLY, stdlib only).

Covers: discovery, JSON validity, duplicate IDs, digest-vector
reproduction, recognized error codes/reasons, K/A-T mapping coverage,
Layer-1 schema conformance over every certificate example, Layer-1 vs
Layers-2/3 separation, ledger freshness. The schema validator is the
single harness implementation in scripts/proof_kernel_ledger.py.
Semantic checker decisions stay PENDING (checker_version=none) and are
never counted as passing; nothing here claims semantic acceptance,
proof validity, or publication eligibility (R4 stays pending by design).
"""

import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import proof_kernel_ledger as ledger

FIXDIR = Path(__file__).resolve().parents[1] / "fixtures" / "proof_kernel"

REQUIRED_MAPS = {
    "K1",
    "K2",
    "K3",
    "K4",
    "K7",
    "K8",
    "K9",
    "K10",
    "K11",
    "K13",
    "K14",
    "K15",
    "A",
    "B",
    "C",
    "F",
    "G",
    "H",
    "M",
    "K17-proposed",  # proposed: K1-K16 lacks a mandatory-premise-omission case; see COMP-missing-conjunct
}

CANON_ERRORS = {
    "MISSING_FIELD",
    "UNKNOWN_ENUM",
    "NULL_FIELD",
    "NOT_NORMALIZED",
    "BAD_PATH",
    "DUPLICATE_ASSUMPTIONS",
    "UNKNOWN_NORMALIZATION_OP",
    "INVALID_RANGE",
    "CANON_VERSION_MISMATCH",
}


def load(name):
    return json.loads((FIXDIR / name).read_bytes())


def test_ledger_structural_check_passes():
    assert ledger.main(check_only=True) == 0


def _snapshot_fixdir():
    return {
        p.name: (p.stat().st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest())
        for p in FIXDIR.glob("*.json")
    }


def test_check_is_non_mutating():
    # --check must detect without modifying: same bytes and mtimes after.
    before = _snapshot_fixdir()
    assert ledger.main(check_only=True) == 0
    assert _snapshot_fixdir() == before


def test_check_detects_stale_missing_undeclared_and_duplicate(tmp_path, capsys):
    import shutil

    for p in FIXDIR.glob("*.json"):
        if p.name != "fixture_ledger.json":
            shutil.copy(p, tmp_path / p.name)
    assert ledger.main(check_only=False, fixdir=tmp_path) == 0
    assert ledger.main(check_only=True, fixdir=tmp_path) == 0
    # Stale: a single-byte change breaks the committed per-file digest.
    target = tmp_path / "canon_invalid_vectors.json"
    raw = target.read_bytes()
    assert b"MISSING_FIELD" in raw
    target.write_bytes(raw.replace(b"MISSING_FIELD", b"MISSING_FlELD", 1))
    assert ledger.main(check_only=True, fixdir=tmp_path) == 1
    assert "stale ledger record" in capsys.readouterr().err
    shutil.copy(FIXDIR / "canon_invalid_vectors.json", target)
    # Missing required fixture.
    (tmp_path / "canon_spec_v1.json").unlink()
    assert ledger.main(check_only=True, fixdir=tmp_path) == 1
    assert "required fixture missing" in capsys.readouterr().err
    shutil.copy(FIXDIR / "canon_spec_v1.json", tmp_path / "canon_spec_v1.json")
    # Undeclared artifact must not evade validation.
    (tmp_path / "rogue.json").write_text('{"rogue": true}')
    assert ledger.main(check_only=True, fixdir=tmp_path) == 1
    assert "undeclared artifact" in capsys.readouterr().err
    (tmp_path / "rogue.json").unlink()
    # Duplicate fixture id.
    inv = tmp_path / "cert_invalid_examples.json"
    doc = json.loads(inv.read_bytes())
    doc["invalid"].append(dict(doc["invalid"][0]))
    inv.write_text(json.dumps(doc))
    assert ledger.main(check_only=True, fixdir=tmp_path) == 1
    assert "duplicate fixture id" in capsys.readouterr().err


def test_check_detects_malformed_unknown_status_and_checkerless_semantic(tmp_path, capsys):
    import shutil

    for p in FIXDIR.glob("*.json"):
        if p.name != "fixture_ledger.json":
            shutil.copy(p, tmp_path / p.name)
    assert ledger.main(check_only=False, fixdir=tmp_path) == 0
    lpath = tmp_path / "fixture_ledger.json"
    base = json.loads(lpath.read_bytes())
    # Unknown status value.
    bad = json.loads(json.dumps(base))
    bad["records"][0]["status"] = "VERIFIED"
    lpath.write_text(json.dumps(bad))
    assert ledger.main(check_only=True, fixdir=tmp_path) == 1
    assert "unknown status" in capsys.readouterr().err
    # Malformed record (missing required keys).
    bad = json.loads(json.dumps(base))
    del bad["records"][1]["fixture_digest"]
    del bad["records"][1]["status"]
    lpath.write_text(json.dumps(bad))
    assert ledger.main(check_only=True, fixdir=tmp_path) == 1
    assert "malformed" in capsys.readouterr().err
    # Semantic claim without a checker is rejected, not recorded.
    bad = json.loads(json.dumps(base))
    bad["records"][2]["status"] = "semantic-pass"
    bad["records"][2]["checker_version"] = "none"
    lpath.write_text(json.dumps(bad))
    assert ledger.main(check_only=True, fixdir=tmp_path) == 1
    assert "semantic claim without a checker" in capsys.readouterr().err


def test_canon_digest_vectors_reproduce_byte_for_byte():
    spec = load("canon_spec_v1.json")
    assert spec["serialization"].startswith("JSON object, keys sorted")
    for vec in spec["digest_vectors"]:
        canon = json.dumps(
            vec["proposition"], sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        assert canon.decode("utf-8") == vec["canonical_bytes"]
        assert "sha256:" + hashlib.sha256(canon).hexdigest() == vec["proposition_id"]


def test_canon_invalid_vectors_have_stable_codes():
    doc = load("canon_invalid_vectors.json")
    assert len(doc["invalid"]) >= 8
    for entry in doc["invalid"]:
        assert entry["expected_error"] in CANON_ERRORS, entry["id"]


def test_canon_vectors_state_full_expectations():
    doc = load("canon_v1_vectors.json")
    for vec in doc["vectors"]:
        exp = vec["expected"]
        assert exp["identity_computation"] == "SUCCESS", vec["vector_id"]
        assert isinstance(exp["same_proposition"], bool), vec["vector_id"]
        assert isinstance(exp["evidence_reuse_permitted"], bool), vec["vector_id"]
    v4 = next(v for v in doc["vectors"] if v["vector_id"] == "V4-rename-validated")
    assert isinstance(v4["normalization"], dict)
    assert "validator" in v4["normalization"] and "revision_binding" in v4["normalization"]


def test_cert_invalid_examples_mapped_with_reason_codes():
    doc = load("cert_invalid_examples.json")
    by_id = {}
    covered: set[str] = set()
    for entry in doc["invalid"]:
        assert entry["expects"] == "REJECT", entry["id"]
        assert entry.get("maps_to"), entry["id"]
        assert entry.get("expected_reason_code"), entry["id"]
        covered.update(entry["maps_to"])
        by_id[entry["id"]] = entry
    missing = REQUIRED_MAPS - covered
    assert not missing, f"uncovered kernel cases: {sorted(missing)}"
    # R1: mandatory conjunction omitting one required premise.
    assert by_id["COMP-missing-conjunct"]["expected_reason_code"] == "MISSING_PREMISE"
    assert by_id["COMP-missing-conjunct"]["maps_to"] == ["K17-proposed"]
    # R2: an alternative treated as sufficient without independently
    # satisfying its own rule requirements.
    assert by_id["K5-alternative-insufficient"]["expected_reason_code"] == "INADMISSIBLE_PREMISE"
    assert "K5" in by_id["K5-alternative-insufficient"]["maps_to"]


def test_cert_valid_and_provisional_separated():
    doc = load("cert_valid_examples.json")
    for entry in doc["valid"]:
        assert entry["expects"] == "ACCEPT", entry["id"]
    assert doc.get("provisional_valid"), "provisional coverage cases missing"
    for entry in doc["provisional_valid"]:
        assert entry["expects"] == "ACCEPT-when-supported", entry["id"]
        assert entry.get("status") == "DRAFT", entry["id"]


def test_fixture_ids_unique_across_matrix():
    seen: dict[str, str] = {}
    dupes = []
    for fname, section, idkey in [
        ("canon_v1_vectors.json", "vectors", "vector_id"),
        ("canon_invalid_vectors.json", "invalid", "id"),
        ("cert_valid_examples.json", "valid", "id"),
        ("cert_valid_examples.json", "provisional_valid", "id"),
        ("cert_invalid_examples.json", "invalid", "id"),
        ("cert_layer1_invalid_examples.json", "layer1_invalid", "id"),
    ]:
        for entry in load(fname).get(section, []):
            fid = entry[idkey]
            if fid in seen:
                dupes.append((fid, fname))
            seen[fid] = fname
    assert not dupes, f"duplicate fixture ids: {dupes}"


def test_ledger_matches_fixture_directory():
    ledger_path = FIXDIR / "fixture_ledger.json"
    assert ledger_path.exists(), "run scripts/proof_kernel_ledger.py first"
    payload = json.loads(ledger_path.read_bytes())
    by_file: dict[str, str] = {}
    for rec in payload["records"]:
        by_file.setdefault(rec["fixture_path"], rec["fixture_digest"])
    for relpath, claimed in by_file.items():
        raw = (FIXDIR / Path(relpath).name).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == claimed, f"stale ledger for {relpath}"
    recorded = {r["fixture_id"] for r in payload["records"]}
    assert "K1-semantic-forgery" in recorded and "V1-predicate-strengthening" in recorded
    assert "COMP-missing-conjunct" in recorded and "L1-short-digest" in recorded
    assert "P-ABSENCE-COVERAGE" in recorded
    # Full inventory, computed dynamically from the fixture files: every
    # declared entry must have a record; no hard-coded subset.
    expected_ids: set[str] = set()
    for fname, section, idkey in [
        ("canon_v1_vectors.json", "vectors", "vector_id"),
        ("canon_invalid_vectors.json", "invalid", "id"),
        ("cert_valid_examples.json", "valid", "id"),
        ("cert_valid_examples.json", "provisional_valid", "id"),
        ("cert_invalid_examples.json", "invalid", "id"),
        ("cert_layer1_invalid_examples.json", "layer1_invalid", "id"),
        ("definition_golden_vectors.json", "vectors", "id"),
        ("definition_invalid_vectors.json", "invalid", "id"),
    ]:
        for entry in json.loads((FIXDIR / fname).read_bytes()).get(section, []):
            expected_ids.add(entry[idkey])
    assert recorded == expected_ids, (
        f"missing {sorted(expected_ids - recorded)} extra {sorted(recorded - expected_ids)}"
    )
    for rec in payload["records"]:
        if rec["status"] == "layer1-reject-confirmed":
            assert rec["checker_version"] == "schema-harness-v1", rec["fixture_id"]
            assert rec["expected_outcome"] == "LAYER1-REJECT", rec["fixture_id"]
        else:
            assert rec["status"] == "pending-semantic", rec["fixture_id"]
            assert rec["checker_version"] == "none", rec["fixture_id"]
    # R4 guard: nothing in the ledger claims semantic acceptance, proof
    # validity, or publication eligibility. The only observed outcomes are
    # structural (Layer 1); everything else is explicitly pending.
    for rec in payload["records"]:
        assert rec["status"] not in ("semantic-pass", "semantic-fail"), rec["fixture_id"]
        assert rec["actual_outcome"] in (
            "PENDING-semantic-checker-absent",
            "REJECT-observed-at-layer-1",
        ), rec["fixture_id"]


def test_cert_examples_conform_to_schema():
    # R3: every certificate in the valid, provisional, and invalid
    # collections is checked against the grandfathered Layer-1 cert/v1
    # schema (v1 corpus is read-only history; v2 is normative for G4).
    schema = load("cert_schema_v1.json")
    checked = 0
    for fname, sections in (
        ("cert_valid_examples.json", ("valid", "provisional_valid")),
        ("cert_invalid_examples.json", ("invalid",)),
    ):
        doc = load(fname)
        for section in sections:
            for entry in doc.get(section, []):
                errs = ledger.validate_against_schema(entry["certificate"], schema)
                assert not errs, f"{fname}/{entry['id']}: {errs}"
                checked += 1
    assert checked == 2 + 6 + 21, f"expected 29 certificates, checked {checked}"


def test_layer_separation_structural_vs_semantic():
    # R3: Layer-1-invalid examples MUST fail schema validation, while every
    # schema-valid-but-semantically-invalid certificate MUST pass it. The
    # schema is therefore never the oracle for semantic rejection; Layers
    # 2/3 decide those cases once the checker exists.
    schema = load("cert_schema_v1.json")
    layer1 = load("cert_layer1_invalid_examples.json")["layer1_invalid"]
    assert len(layer1) == 3
    for entry in layer1:
        assert entry["expects"] == "LAYER1-REJECT", entry["id"]
        errs = ledger.validate_against_schema(entry["certificate"], schema)
        assert errs, f"{entry['id']} unexpectedly schema-valid"
    semantic_invalid = 0
    for fname, sections in (
        ("cert_valid_examples.json", ("valid", "provisional_valid")),
        ("cert_invalid_examples.json", ("invalid",)),
    ):
        doc = load(fname)
        for section in sections:
            for entry in doc.get(section, []):
                assert not ledger.validate_against_schema(entry["certificate"], schema), entry["id"]
                if section == "invalid":
                    semantic_invalid += 1
    assert semantic_invalid == 21, f"expected 21 semantic-invalid certs, saw {semantic_invalid}"


def test_record_accounting_reconciles():
    # Closure accounting, pinned: 67 records = 7 canon vectors + 9
    # canon-invalid + 2 valid + 6 provisional + 21 invalid + 3 layer-1
    # + 7 definition goldens + 12 definition invalids.
    # Provisional (DRAFT) and semantic-invalid sets are disjoint.
    canon = load("canon_v1_vectors.json")["vectors"]
    canon_inv = load("canon_invalid_vectors.json")["invalid"]
    valid_doc = load("cert_valid_examples.json")
    invalid = load("cert_invalid_examples.json")["invalid"]
    layer1 = load("cert_layer1_invalid_examples.json")["layer1_invalid"]
    goldens = load("definition_golden_vectors.json")["vectors"]
    definv = load("definition_invalid_vectors.json")["invalid"]
    assert len(canon) == 7, len(canon)
    assert len(canon_inv) == 9, len(canon_inv)
    assert len(valid_doc["valid"]) == 2, len(valid_doc["valid"])
    assert len(valid_doc["provisional_valid"]) == 6, len(valid_doc["provisional_valid"])
    assert len(invalid) == 21, len(invalid)
    assert len(layer1) == 3, len(layer1)
    assert len(goldens) == 7, len(goldens)
    assert len(definv) == 12, len(definv)
    prov_ids = {e["id"] for e in valid_doc["provisional_valid"]}
    inv_ids = {e["id"] for e in invalid}
    assert prov_ids.isdisjoint(inv_ids), f"overlap: {sorted(prov_ids & inv_ids)}"
    payload = json.loads((FIXDIR / "fixture_ledger.json").read_bytes())
    assert len(payload["records"]) == 67, len(payload["records"])
    pending = [r for r in payload["records"] if r["status"] == "pending-semantic"]
    confirmed = [r for r in payload["records"] if r["status"] == "layer1-reject-confirmed"]
    assert len(pending) == 64, len(pending)
    assert len(confirmed) == 3, len(confirmed)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
