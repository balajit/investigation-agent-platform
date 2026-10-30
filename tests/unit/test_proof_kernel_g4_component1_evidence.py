"""Structural integrity for G4 Component 1 evidence fixtures.

This suite validates fixture-file serialization only. It does not construct
proof premise models, execute rules, or assert semantic checker outcomes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[2]
FIXTURE_DIR = ROOT / "tests" / "fixtures" / "proof_kernel" / "g4_component1"
SCHEMA_PATH = FIXTURE_DIR / "fixture_schema_v1.json"
CORPUS_FILES = (
    "behavior_evidence_v1.json",
    "absence_evidence_v1.json",
    "history_evidence_v1.json",
)
EXPECTED_CASES: dict[str, set[str]] = {
    "behavior_evidence_v1.json": {f"G4E-BEH-{index:03d}" for index in range(1, 16)},
    "absence_evidence_v1.json": {f"G4E-ABS-{index:03d}" for index in range(1, 17)},
    "history_evidence_v1.json": {f"G4E-HIS-{index:03d}" for index in range(1, 14)},
}
EXPECTED_MAPS_TO: dict[str, list[str]] = {
    "G4E-BEH-001": ["BEHAVIOR", "K16"],
    "G4E-BEH-002": ["BEHAVIOR", "P"],
    "G4E-BEH-003": ["BEHAVIOR", "P"],
    "G4E-BEH-004": ["BEHAVIOR", "K16"],
    "G4E-BEH-005": ["BEHAVIOR", "N"],
    "G4E-BEH-006": ["BEHAVIOR", "K11"],
    "G4E-BEH-007": ["BEHAVIOR", "K11"],
    "G4E-BEH-008": ["BEHAVIOR", "K11"],
    "G4E-BEH-009": ["BEHAVIOR", "K11"],
    "G4E-BEH-010": ["BEHAVIOR", "D"],
    "G4E-BEH-011": ["BEHAVIOR", "D"],
    "G4E-BEH-012": ["BEHAVIOR", "K10"],
    "G4E-BEH-013": ["BEHAVIOR", "K10"],
    "G4E-BEH-014": ["BEHAVIOR", "K14"],
    "G4E-BEH-015": ["BEHAVIOR", "K9"],
    "G4E-ABS-001": ["ABSENCE", "K3"],
    "G4E-ABS-002": ["ABSENCE", "K3"],
    "G4E-ABS-003": ["ABSENCE", "K3"],
    "G4E-ABS-004": ["ABSENCE", "K3"],
    "G4E-ABS-005": ["ABSENCE", "K3"],
    "G4E-ABS-006": ["ABSENCE", "K3"],
    "G4E-ABS-007": ["ABSENCE", "K11"],
    "G4E-ABS-008": ["ABSENCE", "K11"],
    "G4E-ABS-009": ["ABSENCE", "D"],
    "G4E-ABS-010": ["ABSENCE", "K10"],
    "G4E-ABS-011": ["ABSENCE", "K10"],
    "G4E-ABS-012": ["ABSENCE", "K1"],
    "G4E-ABS-013": ["ABSENCE", "N"],
    "G4E-ABS-014": ["ABSENCE", "K3"],
    "G4E-ABS-015": ["ABSENCE", "K3"],
    "G4E-ABS-016": ["ABSENCE", "K3"],
    "G4E-HIS-001": ["HISTORY", "K16"],
    "G4E-HIS-002": ["HISTORY", "K11"],
    "G4E-HIS-003": ["HISTORY", "K9"],
    "G4E-HIS-004": ["HISTORY", "K9"],
    "G4E-HIS-005": ["HISTORY", "K9"],
    "G4E-HIS-006": ["HISTORY", "D"],
    "G4E-HIS-007": ["HISTORY", "D"],
    "G4E-HIS-008": ["HISTORY", "K11"],
    "G4E-HIS-009": ["HISTORY", "K10"],
    "G4E-HIS-010": ["HISTORY", "K10"],
    "G4E-HIS-011": ["HISTORY", "K7"],
    "G4E-HIS-012": ["HISTORY", "A"],
    "G4E-HIS-013": ["HISTORY", "K16"],
}
PREMISE_TYPES = {
    "RANGE_READ_FACT",
    "NORMALIZED_CALLSITE_FACT",
    "RUNTIME_OBSERVATION_FACT",
    "BUILD_SOURCE_MAPPING",
    "ABSENCE_COVERAGE_FACT",
    "COMMIT_HISTORY",
}


def _load_json(path: Path) -> Any:
    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            assert key not in result, f"duplicate JSON object key {key!r} in {path}"
            result[key] = value
        return result

    return json.loads(path.read_bytes(), object_pairs_hook=reject_duplicate_keys)


def _schema_error_messages(error: Any) -> list[str]:
    messages = [f"{list(error.absolute_path)}: {error.message}"]
    for nested in error.context:
        messages.extend(_schema_error_messages(nested))
    return messages


def test_component1_fixture_files_match_frozen_inventory() -> None:
    expected = {"fixture_schema_v1.json", *CORPUS_FILES}
    actual = {path.name for path in FIXTURE_DIR.glob("*") if path.is_file()}
    assert actual == expected


def test_component1_fixture_schema_is_valid_json_schema() -> None:
    schema = _load_json(SCHEMA_PATH)
    Draft202012Validator.check_schema(schema)


def test_component1_corpus_matches_schema_and_expected_ids() -> None:
    schema = _load_json(SCHEMA_PATH)
    validator = Draft202012Validator(schema)
    seen: set[str] = set()

    for filename in CORPUS_FILES:
        document = _load_json(FIXTURE_DIR / filename)
        assert document["schema_version"] == "g4-evidence-fixture/v1"
        assert isinstance(document["fixtures"], list)
        actual_ids: set[str] = set()
        records_by_id: dict[str, dict[str, Any]] = {}

        for record in document["fixtures"]:
            errors = sorted(validator.iter_errors(record), key=lambda error: list(error.path))
            assert not errors, (
                f"{filename}/{record.get('fixture_id')}: "
                f"{[message for error in errors for message in _schema_error_messages(error)]}"
            )

            fixture_id = record["fixture_id"]
            assert fixture_id not in seen, f"duplicate fixture ID: {fixture_id}"
            seen.add(fixture_id)
            actual_ids.add(fixture_id)
            records_by_id[fixture_id] = record

            expected = record["expectation"]
            assert expected["layer"] == "EVIDENCE"
            assert expected["class"] in {
                "CANDIDATE_INPUT",
                "REJECT_INPUT",
                "UNSUPPORTED_INPUT",
                "OWNER_ADJUDICATION_REQUIRED",
                "PREMISE_UNAVAILABLE",
            }
            assert "observed" not in record
            assert record["maps_to"] == EXPECTED_MAPS_TO[fixture_id]
            _assert_manifest_bindings(record)

        assert actual_ids == EXPECTED_CASES[filename], (
            f"{filename}: missing={sorted(EXPECTED_CASES[filename] - actual_ids)}, "
            f"extra={sorted(actual_ids - EXPECTED_CASES[filename])}"
        )
        _assert_mutations_resolve(records_by_id)


def _assert_mutations_resolve(records_by_id: dict[str, dict[str, Any]]) -> None:
    for record in records_by_id.values():
        mutation = record.get("mutation")
        if mutation is None:
            continue

        baseline_id = mutation["baseline_fixture_id"]
        assert baseline_id in EXPECTED_MAPS_TO, (record["fixture_id"], baseline_id)
        baseline = _find_record(baseline_id)
        assert baseline is not None, f"{record['fixture_id']}: baseline fixture does not exist"
        baseline_family = baseline["family"]
        assert baseline_family == record["family"], (record["fixture_id"], baseline_id)

        if record["fixture_id"] in {"G4E-BEH-012", "G4E-HIS-009"}:
            assert mutation["field_path"] == "inputs.available_evidence_ids"
            assert mutation["operation"] == "replace"
            baseline_available = baseline["inputs"]["available_evidence_ids"]
            missing_artifact_id = (
                "ev-runtime-001" if record["fixture_id"] == "G4E-BEH-012" else "ev-history-001"
            )
            assert mutation["value"] == [
                evidence_id
                for evidence_id in baseline_available
                if evidence_id != missing_artifact_id
            ]
        if record["family"] == "ABSENCE" and record["fixture_id"] == "G4E-ABS-016":
            assert mutation["field_path"] == "inputs.manifest.evidence_ids"

        segments = mutation["field_path"].split(".")
        assert segments[0] == "inputs" and len(segments) > 1, record["fixture_id"]
        value: Any = baseline["inputs"]
        for segment in segments[1:-1]:
            assert isinstance(value, dict) and segment in value, (
                record["fixture_id"],
                mutation["field_path"],
                segment,
            )
            value = value[segment]
        final_key = segments[-1]
        assert isinstance(value, dict) and final_key in value, (
            record["fixture_id"],
            mutation["field_path"],
        )
        assert mutation["operation"] == "remove" or (
            mutation["operation"] == "replace" and final_key in value
        ), record["fixture_id"]


def _apply_mutation(baseline_inputs: dict[str, Any], mutation: dict[str, Any]) -> dict[str, Any]:
    """Materialize a derived fixture by applying its mutation to a baseline copy.

    Only the schema's closed operation set is supported; anything else
    fails loudly instead of silently producing an unasserted variant.
    """
    import copy

    materialized = copy.deepcopy(baseline_inputs)
    segments = mutation["field_path"].split(".")
    operation = mutation["operation"]
    if segments[0] != "inputs" or len(segments) < 2:
        raise AssertionError(f"mutation path must start with inputs.: {mutation['field_path']}")
    if operation not in ("replace", "remove"):
        raise AssertionError(f"unsupported mutation operation: {operation}")
    target: Any = materialized
    for segment in segments[1:-1]:
        if not isinstance(target, dict) or segment not in target:
            raise AssertionError(f"mutation path does not resolve: {mutation['field_path']}")
        target = target[segment]
    final_key = segments[-1]
    if not isinstance(target, dict) or final_key not in target:
        raise AssertionError(f"mutation target missing: {mutation['field_path']}")
    if operation == "replace":
        target[final_key] = mutation["value"]
    else:
        del target[final_key]
    return materialized


def _count_leaf_differences(first: Any, second: Any) -> int:
    """Count leaf-level differences between two JSON values (missing key = 1)."""
    if isinstance(first, dict) and isinstance(second, dict):
        total = 0
        for key in set(first) | set(second):
            if key not in first or key not in second:
                total += 1
            else:
                total += _count_leaf_differences(first[key], second[key])
        return total
    if isinstance(first, list) and isinstance(second, list):
        if len(first) != len(second):
            return 1
        return sum(_count_leaf_differences(a, b) for a, b in zip(first, second))
    return 0 if first == second and type(first) is type(second) else 1


def test_component1_mutations_apply_with_single_field_delta() -> None:
    for filename in CORPUS_FILES:
        document = _load_json(FIXTURE_DIR / filename)
        for record in document["fixtures"]:
            mutation = record.get("mutation")
            if mutation is None:
                continue
            baseline = _find_record(mutation["baseline_fixture_id"])
            assert baseline is not None, record["fixture_id"]
            materialized = _apply_mutation(baseline["inputs"], mutation)
            assert materialized != baseline["inputs"], record["fixture_id"]
            assert _count_leaf_differences(baseline["inputs"], materialized) == 1, (
                record["fixture_id"],
                mutation["field_path"],
            )


def _find_record(fixture_id: str) -> dict[str, Any] | None:
    for filename in CORPUS_FILES:
        document = _load_json(FIXTURE_DIR / filename)
        for record in document["fixtures"]:
            if record["fixture_id"] == fixture_id:
                return record
    return None


def _assert_manifest_bindings(record: dict[str, Any]) -> None:
    """Inventory consistency between fixture manifest maps and records.

    Component 1 digests are symbolic placeholders by envelope design, so
    this asserts map/record agreement (same IDs, same recorded strings),
    not cryptographic binding. Real digest-to-byte binding is proven by
    Component 2 vectors over computed digests.
    """
    inputs = record["inputs"]
    family = record["family"]
    manifest = inputs.get("manifest")
    if manifest is None:
        return
    premise_records = {
        key: value
        for key, value in inputs.items()
        if key.endswith("_premise") or key == "build_source_mapping"
    }
    if family == "ABSENCE":
        premise_records["coverage_evidence"] = inputs["coverage_evidence"]
    premise_records = {
        name: premise
        for name, premise in premise_records.items()
        if premise.get("evidence_id") is not None
    }

    ids = manifest["evidence_ids"]
    hashes = manifest["content_hashes"]
    types = manifest["premise_types"]
    assert set(ids) == set(hashes) == set(types), record["fixture_id"]
    assert len(ids) == len(set(ids)), record["fixture_id"]
    assert set(types.values()) <= PREMISE_TYPES, record["fixture_id"]

    for premise in premise_records.values():
        evidence_id = premise["evidence_id"]
        assert evidence_id in ids, (record["fixture_id"], evidence_id)
        assert hashes[evidence_id] == premise["content_hash"], (record["fixture_id"], evidence_id)
        assert types[evidence_id] == premise["premise_type"], (record["fixture_id"], evidence_id)

    available_ids = inputs.get("available_evidence_ids", [])
    assert len(available_ids) == len(set(available_ids)), record["fixture_id"]
    assert set(available_ids) <= set(ids), record["fixture_id"]

    if family == "BEHAVIOR":
        if record["expectation"]["class"] == "CANDIDATE_INPUT":
            assert set(available_ids) == set(ids), record["fixture_id"]
        else:
            assert set(available_ids) <= set(ids), record["fixture_id"]
    elif family == "ABSENCE":
        coverage_id = inputs["coverage_evidence_id"]
        assert set(ids) == {coverage_id}, record["fixture_id"]
        assert set(available_ids) == {coverage_id}, record["fixture_id"]
        assert types[coverage_id] == "ABSENCE_COVERAGE_FACT", record["fixture_id"]
        assert inputs["coverage_evidence"]["premise_type"] == types[coverage_id], record[
            "fixture_id"
        ]
    elif family == "HISTORY":
        assert set(available_ids) == set(ids), record["fixture_id"]


def test_component1_provenance_amendment_a2_pins() -> None:
    behavior = _load_json(FIXTURE_DIR / "behavior_evidence_v1.json")["fixtures"]
    by_id = {record["fixture_id"]: record for record in behavior}
    assert by_id["G4E-BEH-001"]["expectation"]["class"] == "CANDIDATE_INPUT"
    assert by_id["G4E-BEH-004"]["expectation"]["class"] == "UNSUPPORTED_INPUT"
    assert by_id["G4E-BEH-001"]["maps_to"] == ["BEHAVIOR", "K16"]
    assert by_id["G4E-BEH-004"]["maps_to"] == ["BEHAVIOR", "K16"]
    # Amendment A2 additions: the candidate carries the asserted value and
    # the build-source mapping digest the rule joins on; the unsupported
    # case carries neither.
    assert by_id["G4E-BEH-001"]["inputs"]["proposition"]["asserted_value"] == 200
    assert by_id["G4E-BEH-001"]["inputs"]["build_source_mapping"]["mapping_digest"].startswith(
        "sha256:"
    )
    assert "asserted_value" not in by_id["G4E-BEH-004"]["inputs"].get("proposition", {})
    assert "build_source_mapping" not in by_id["G4E-BEH-004"]["inputs"]


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
