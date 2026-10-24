"""Harness registry loading, schema validation, and §8 authority scans."""

from __future__ import annotations

from pathlib import Path

import pytest

from investigation_agent_platform.harness.registry import (
    CELLS,
    REGISTRY_ID,
    HarnessValidationError,
    check_record,
    load_registry,
    scan_authority,
    validate_record,
)

DOCS = Path(__file__).resolve().parents[2] / "docs"
REGISTRY_PATH = DOCS / "IAP-proof-kernel-d3-stageC-harness-schemas-v1.13.json"


def test_load_registry_identity() -> None:
    reg = load_registry(REGISTRY_PATH)
    assert reg["$id"] == REGISTRY_ID
    assert "requiredMetricSet" in reg["$defs"]
    assert "formulaCatalog" in reg["$defs"]


def test_cells_match_registry_cell_enum() -> None:
    reg = load_registry(REGISTRY_PATH)
    assert tuple(reg["$defs"]["cell"]["enum"]) == tuple(CELLS)


def test_load_registry_rejects_wrong_identity(tmp_path) -> None:
    other = tmp_path / "reg.json"
    other.write_text('{"$id": "other"}', encoding="utf-8")
    with pytest.raises(HarnessValidationError, match="mismatch"):
        load_registry(other)


def test_validate_record_accepts_join_contract_turn() -> None:
    import json

    vec = json.loads(
        (DOCS / "IAP-proof-kernel-d3-stageC-schema-vectors-v1.15.json").read_text(encoding="utf-8")
    )
    reg = load_registry(REGISTRY_PATH)
    validate_record(reg, "caseTurn", vec["report_cases"][0]["turns"][0])
    validate_record(reg, "caseReceipt", vec["report_cases"][0]["receipts"][0])


def test_validate_record_rejects_unknown_property() -> None:
    reg = load_registry(REGISTRY_PATH)
    with pytest.raises(HarnessValidationError, match="schema violation"):
        validate_record(reg, "caseTurn", {"count_key": [], "zzz_unknown": 1})


def test_scan_authority_rejects_alias_key() -> None:
    with pytest.raises(HarnessValidationError, match="alias key"):
        scan_authority({"nested": {"Authorised": True}})


def test_scan_authority_rejects_fullwidth_alias() -> None:
    # Fullwidth "ａuthorized" NFKC-folds to "authorized".
    with pytest.raises(HarnessValidationError, match="alias key"):
        scan_authority({"nested": {"ａuthorized": 1}})


def test_scan_authority_rejects_true_declaration() -> None:
    with pytest.raises(HarnessValidationError, match="must be false"):
        scan_authority({"declaration_requirement_satisfied": True})


def test_scan_authority_accepts_false_declaration() -> None:
    scan_authority({"declaration_requirement_satisfied": False})


def test_scan_authority_rejects_forbidden_verdict() -> None:
    with pytest.raises(HarnessValidationError, match="forbidden verdict"):
        scan_authority({"verdict": "PROVEN"})


@pytest.mark.parametrize(
    "verdict", ["HOLD", "hold", "FEASIBILITY_DEMONSTRATED", "INSUFFICIENT_EVIDENCE"]
)
def test_scan_authority_accepts_closed_verdicts(verdict) -> None:
    scan_authority({"verdict": verdict})


def test_scan_authority_rejects_forbidden_value_in_text_field() -> None:
    with pytest.raises(HarnessValidationError, match="forbidden authority value"):
        scan_authority({"native": {"status": "AUTHORIZED"}})


def test_scan_authority_rejects_forbidden_value_deep() -> None:
    with pytest.raises(HarnessValidationError, match="forbidden authority value"):
        scan_authority({"a": [{"b": {"c": "proven"}}]})


def test_scan_authority_accepts_legitimate_compounds() -> None:
    scan_authority(
        {
            "cell": "accept-PASS",
            "native": {"status": "s", "producing_path": "general-path"},
            "verdict": "HOLD",
        }
    )


def test_load_registry_rejects_same_id_modified_bytes(tmp_path) -> None:
    import shutil

    other = tmp_path / "reg.json"
    shutil.copy(REGISTRY_PATH, other)
    # Modify a content byte while keeping the $id unchanged: the digest
    # check — not the identity check — must fire.
    raw = other.read_bytes().replace(b'"receipt/v2"', b'"receipt/v3"', 1)
    assert b"registry/v12" in raw
    other.write_bytes(raw)
    with pytest.raises(HarnessValidationError, match="digest mismatch"):
        load_registry(other)


def test_scan_authority_rejects_verified_as_authority() -> None:
    with pytest.raises(HarnessValidationError, match="verified used as authority"):
        scan_authority({"report": {"verified": True}})


def test_scan_authority_rejects_verified_string_as_authority() -> None:
    with pytest.raises(HarnessValidationError, match="verified used as authority"):
        scan_authority({"receipt": {"verified": "yes"}})


def test_scan_authority_accepts_native_verified() -> None:
    scan_authority({"native": {"verified": True, "status": "s"}})
    scan_authority({"native": {"verified": False}})


def test_scan_authority_rejects_non_boolean_native_verified() -> None:
    with pytest.raises(HarnessValidationError, match="non-boolean native verified"):
        scan_authority({"native": {"verified": "yes"}})


def test_scan_authority_rejects_nested_native_lookalike() -> None:
    with pytest.raises(HarnessValidationError, match="verified used as authority"):
        scan_authority({"report": {"native": {"verified": True}}})
    with pytest.raises(HarnessValidationError, match="verified used as authority"):
        scan_authority([{"native": {"verified": True}}])


def test_check_record_runs_scan_before_schema() -> None:
    reg = load_registry(REGISTRY_PATH)
    # Alias key inside an otherwise schema-valid turn: scan must fire first.
    import json

    vec = json.loads(
        (DOCS / "IAP-proof-kernel-d3-stageC-schema-vectors-v1.15.json").read_text(encoding="utf-8")
    )
    turn = dict(vec["report_cases"][0]["turns"][0])
    turn["authz"] = "x"
    with pytest.raises(HarnessValidationError, match="alias key"):
        check_record(reg, "caseTurn", turn)
