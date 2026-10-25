"""Proof-kernel fixture ledger generator (TEST-ONLY tooling, stdlib only).

Discovers the declared fixture set under tests/fixtures/proof_kernel/,
validates each certificate example against cert/v1 (Layer 1), and writes
fixture_ledger.json with one record per vector/example plus a per-file
digest. Layer-1 rejections are executed observations
(checker_version schema-harness-v1). Semantic validation (Layers 2/3) is
recorded as pending-semantic with checker_version 'none' until the
independent checker exists; pending records never count as passing.

Usage:
    python3 scripts/proof_kernel_ledger.py [--check]

--check exits nonzero on any structural mismatch without writing.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXDIR = ROOT / "tests" / "fixtures" / "proof_kernel"
LEDGER = FIXDIR / "fixture_ledger.json"

ENTRY_SECTIONS = [
    ("canon_v1_vectors.json", "vectors", "vector_id", "canon/v1"),
    ("canon_invalid_vectors.json", "invalid", "id", "canon/v1"),
    ("cert_valid_examples.json", "valid", "id", "cert/v1"),
    ("cert_valid_examples.json", "provisional_valid", "id", "cert/v1"),
    ("cert_invalid_examples.json", "invalid", "id", "cert/v1"),
    ("definition_golden_vectors.json", "vectors", "id", "definition/v1"),
    ("definition_invalid_vectors.json", "invalid", "id", "definition/v1"),
]

# Entries that MUST fail Layer-1 schema validation. A layer-1 example that
# passes validation is a harness defect, not a passing fixture.
LAYER1_SECTIONS = [
    ("cert_layer1_invalid_examples.json", "layer1_invalid", "id", "cert/v1"),
]


def _type_ok(inst, t):
    return (
        (t == "object" and isinstance(inst, dict))
        or (t == "array" and isinstance(inst, list))
        or (t == "string" and isinstance(inst, str))
        or (t == "null" and inst is None)
    )


def validate_against_schema(instance, schema, root=None):
    """Stdlib-only structural validation against our cert/v1 schema subset.

    Enforces const, enum, type, required, properties, additionalProperties,
    items, minItems, pattern, and local $refs. Not a full JSON Schema
    implementation; sufficient for the constructs cert/v1 uses. Returns a
    list of error strings; empty means Layer-1 PASS.
    """
    errors = []
    root = root if root is not None else schema

    def resolve(sch):
        if isinstance(sch, dict) and set(sch) == {"$ref"}:
            node = root
            ref = sch["$ref"]
            assert ref.startswith("#/"), ref
            for part in ref[2:].split("/"):
                node = node[part]
            return node
        if isinstance(sch, dict) and "$ref" in sch:
            node = resolve({"$ref": sch["$ref"]})
            merged = dict(node)
            merged.update({k: v for k, v in sch.items() if k != "$ref"})
            return merged
        return sch

    def check(inst, sch, path):
        sch = resolve(sch)
        if not isinstance(sch, dict):
            return
        if "const" in sch and inst != sch["const"]:
            errors.append(f"{path}: expected const {sch['const']!r}")
        if "enum" in sch and inst not in sch["enum"]:
            errors.append(f"{path}: {inst!r} not in enum")
        typ = sch.get("type")
        if typ is not None:
            types = typ if isinstance(typ, list) else [typ]
            if not any(_type_ok(inst, t) for t in types):
                errors.append(f"{path}: type mismatch for {type(inst).__name__}")
                return
        if isinstance(inst, dict):
            for req in sch.get("required", []):
                if req not in inst:
                    errors.append(f"{path}: missing required field {req}")
            props = sch.get("properties", {})
            for k, v in inst.items():
                if k in props:
                    check(v, props[k], f"{path}.{k}")
                elif sch.get("additionalProperties") is False:
                    errors.append(f"{path}: unknown field {k}")
        if isinstance(inst, list):
            items = sch.get("items")
            if isinstance(items, dict):
                for i, v in enumerate(inst):
                    check(v, items, f"{path}[{i}]")
            if "minItems" in sch and len(inst) < sch["minItems"]:
                errors.append(f"{path}: fewer than minItems {sch['minItems']}")
        pat = sch.get("pattern")
        if pat and isinstance(inst, str) and not re.fullmatch(pat, inst):
            errors.append(f"{path}: pattern mismatch {pat}")

    check(instance, schema, "$")
    return errors


def fail(msg: str) -> None:
    print(f"LEDGER FAIL: {msg}", file=sys.stderr)


def digest_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


DECLARED_FILES = (
    {fname for fname, _, _, _ in ENTRY_SECTIONS}
    | {fname for fname, _, _, _ in LAYER1_SECTIONS}
    | {
        "canon_spec_v1.json",
        "cert_schema_v1.json",
        "ledger_schema_v1.json",
        "fixture_ledger.json",
        # WP-A1 offline baseline (validated by
        # scripts/proof_kernel_baseline.py --check + baseline tests,
        # not by ledger record generation).
        "baseline_claims_v1.json",
        "baseline_manifest_v1.json",
        # D1 mapping artifact (validated by
        # test_d1_mapping_artifact_validates + dispatch tests,
        # not by ledger record generation).
        "dispatch_mapping.json",
        # D3 mapping artifact (validated by
        # test_d3_mapping_artifact_validates + definition tests,
        # not by ledger record generation).
        "definition_dispatch_mapping.json",
        # G4 cert/v2 schema (validated by the Component 2 rule suite
        # emitting v2 certificates, not by ledger record generation).
        "cert_schema_v2.json",
        # G4 validator fingerprints (verified by
        # scripts/proof_kernel_validator_fingerprints.py --check and the
        # fingerprint detector suite, not by ledger record generation).
        "validator_fingerprints_v1.json",
    }
)


def directory_inventory(fixdir: Path) -> list[str]:
    """Every required fixture accounted for; undeclared artifacts fail.

    Catches missing files and rogue JSON that would otherwise evade
    validation. Pure read-only listing.
    """
    errors: list[str] = []
    try:
        present = {p.name for p in fixdir.glob("*.json")}
    except OSError as e:
        return [f"cannot list {fixdir}: {e}"]
    for missing in sorted(DECLARED_FILES - present):
        if missing == "fixture_ledger.json":
            errors.append(
                "fixture_ledger.json missing: run scripts/proof_kernel_ledger.py to generate it"
            )
        else:
            errors.append(f"required fixture missing: {missing}")
    for extra in sorted(present - DECLARED_FILES):
        errors.append(f"undeclared artifact evades validation: {extra}")
    return errors


REQUIRED_LEDGER_KEYS = {
    "record_id",
    "fixture_id",
    "fixture_path",
    "fixture_digest",
    "contract_version",
    "test_id",
    "expected_outcome",
    "actual_outcome",
    "checker_version",
    "execution_id",
    "status",
    "failure_code",
}

KNOWN_LEDGER_STATUSES = {
    "structural-pass",
    "structural-fail",
    "pending-semantic",
    "layer1-reject-confirmed",
    "semantic-pass",
    "semantic-fail",
}


def verify_ledger_records(fixdir: Path, records: list) -> list[str]:
    """The committed ledger must match the fixture directory: same per-file
    digests and same fixture-id set. Detects stale, missing, duplicated, or
    inconsistent records without modifying anything."""
    errors: list[str] = []
    ledger_path = fixdir / "fixture_ledger.json"
    try:
        payload = json.loads(ledger_path.read_bytes())
    except OSError:
        return ["fixture_ledger.json missing: run scripts/proof_kernel_ledger.py to generate it"]
    except json.JSONDecodeError as e:
        return [f"fixture_ledger.json invalid: {e}"]
    committed: dict[str, dict] = {}
    for i, r in enumerate(payload.get("records", [])):
        if not isinstance(r, dict):
            errors.append(f"ledger record #{i} malformed: not an object")
            continue
        missing = REQUIRED_LEDGER_KEYS - set(r)
        if missing:
            errors.append(
                f"ledger record {r.get('fixture_id', '#' + str(i))} malformed: "
                f"missing {sorted(missing)}"
            )
            continue
        if r.get("status") not in KNOWN_LEDGER_STATUSES:
            errors.append(
                f"ledger record {r.get('fixture_id')}: unknown status {r.get('status')!r}"
            )
        if r.get("status") in ("semantic-pass", "semantic-fail") and r.get("checker_version") in (
            None,
            "none",
        ):
            errors.append(f"ledger record {r.get('fixture_id')}: semantic claim without a checker")
        if r.get("status") == "pending-semantic" and r.get("expected_outcome") == "LAYER1-REJECT":
            errors.append(
                f"ledger record {r.get('fixture_id')}: layer-1 rejection mislabeled pending"
            )
        if isinstance(r.get("fixture_id"), str):
            committed[r["fixture_id"]] = r
    current_ids = {r["fixture_id"] for r in records}
    if set(committed) != current_ids:
        errors.append(
            f"ledger id set differs: missing {sorted(current_ids - set(committed))} "
            f"extra {sorted(set(committed) - current_ids)}"
        )
    current_digest: dict[str, str] = {}
    for r in records:
        current_digest.setdefault(r["fixture_path"], r["fixture_digest"])
    for r in payload.get("records", []):
        if not isinstance(r, dict):
            continue
        want = current_digest.get(r.get("fixture_path", ""))
        if want is None:
            if "fixture_path" in r:
                errors.append(f"ledger references unknown path {r['fixture_path']}")
        elif r.get("fixture_digest") != want:
            errors.append(f"stale ledger record for {r['fixture_path']}: digest drift")
    return errors


def main(check_only: bool = False, fixdir: Path = FIXDIR) -> int:
    errors: list[str] = [
        e
        for e in directory_inventory(fixdir)
        # Write mode creates the ledger, so its absence is only an error for --check.
        if check_only or not e.startswith("fixture_ledger.json missing")
    ]
    records: list[dict] = []
    seen_ids: dict[str, str] = {}

    try:
        schema = json.loads((fixdir / "cert_schema_v1.json").read_bytes())
    except (OSError, json.JSONDecodeError) as e:
        fail(f"cannot load cert schema: {e}")
        return 1

    for fname, section, idkey, contract in ENTRY_SECTIONS:
        path = fixdir / fname
        try:
            raw = path.read_bytes()
        except OSError as e:
            errors.append(f"{fname} unreadable: {e}")
            continue
        try:
            doc = json.loads(raw)
        except json.JSONDecodeError as e:
            errors.append(f"{fname}: invalid JSON: {e}")
            continue
        entries = doc.get(section, [])
        for entry in entries:
            fid = entry.get(idkey, "<missing-id>")
            if fid in seen_ids:
                errors.append(f"duplicate fixture id {fid} in {fname} (also in {seen_ids[fid]})")
            else:
                seen_ids[fid] = fname
            expected = entry.get("expects") or entry.get("expected_error") or entry.get("expected")
            if expected is None:
                errors.append(f"{fname}/{fid}: no expected outcome")
            if "certificate" in entry:
                layer1 = validate_against_schema(entry["certificate"], schema)
                if layer1:
                    errors.append(
                        f"{fname}/{fid}: Layer-1 failure on a Layer-2/3 fixture: {layer1[0]}"
                    )
            records.append(
                {
                    "record_id": f"{fid}@{digest_bytes(raw)[:12]}",
                    "fixture_id": fid,
                    "fixture_path": f"tests/fixtures/proof_kernel/{fname}",
                    "fixture_digest": digest_bytes(raw),
                    "contract_version": contract,
                    "test_id": ",".join(entry.get("maps_to", [])) or entry.get("vector_id", fid),
                    "expected_outcome": str(expected),
                    "actual_outcome": "PENDING-semantic-checker-absent",
                    "checker_version": "none",
                    "execution_id": datetime.now(UTC).strftime("%Y%m%dT%H%M%S"),
                    "status": "pending-semantic",
                    "failure_code": None,
                }
            )

    # Layer-1 rejection set: each entry MUST fail schema validation, proving
    # the harness distinguishes structural failure from semantic rejection.
    for fname, section, idkey, contract in LAYER1_SECTIONS:
        path = fixdir / fname
        try:
            raw = path.read_bytes()
        except OSError as e:
            errors.append(f"{fname} unreadable: {e}")
            continue
        try:
            doc = json.loads(raw)
        except json.JSONDecodeError as e:
            errors.append(f"{fname}: invalid JSON: {e}")
            continue
        for entry in doc.get(section, []):
            fid = entry.get(idkey, "<missing-id>")
            if fid in seen_ids:
                errors.append(f"duplicate fixture id {fid} in {fname} (also in {seen_ids[fid]})")
            else:
                seen_ids[fid] = fname
            layer1 = validate_against_schema(entry.get("certificate", {}), schema)
            if not layer1:
                errors.append(f"{fname}/{fid}: layer-1 example unexpectedly schema-valid")
            records.append(
                {
                    "record_id": f"{fid}@{digest_bytes(raw)[:12]}",
                    "fixture_id": fid,
                    "fixture_path": f"tests/fixtures/proof_kernel/{fname}",
                    "fixture_digest": digest_bytes(raw),
                    "contract_version": contract,
                    "test_id": "L1",
                    "expected_outcome": "LAYER1-REJECT",
                    "actual_outcome": "REJECT-observed-at-layer-1",
                    "checker_version": "schema-harness-v1",
                    "execution_id": datetime.now(UTC).strftime("%Y%m%dT%H%M%S"),
                    "status": "layer1-reject-confirmed",
                    "failure_code": "LAYER1_SCHEMA",
                }
            )

    if errors:
        for e in errors:
            fail(e)
        return 1

    provisional = sum(1 for r in records if r["expected_outcome"] == "ACCEPT-when-supported")
    layer1 = sum(1 for r in records if r["status"] == "layer1-reject-confirmed")
    nfiles = len({f for f, _, _, _ in ENTRY_SECTIONS} | {f for f, _, _, _ in LAYER1_SECTIONS})
    print(f"fixtures: {len(records)} entries across {nfiles} files")
    print(f"provisional (DRAFT, not valid): {provisional}")
    print(f"layer-1 rejections confirmed by schema harness: {layer1}")
    print(
        "semantic status: remainder pending-semantic (checker_version=none); nothing semantically verified"
    )

    if check_only:
        errors.extend(verify_ledger_records(fixdir, records))
        if errors:
            for e in errors:
                fail(e)
            return 1
        print("structural check: PASS")
        return 0

    payload = {
        "note": "TEST-ONLY fixture-execution ledger. Pending-semantic records are not passing evidence.",
        "generated_at": datetime.now(UTC).isoformat(),
        "records": records,
    }
    out = fixdir / "fixture_ledger.json"
    out.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main("--check" in sys.argv[1:]))
