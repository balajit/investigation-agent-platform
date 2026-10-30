"""Model-level tests for G4 strict premise records (premises/v1).

Serialization boundary only: every closed shape parses, and every
closed-boundary mutation fails. Semantic rule/checker behavior belongs
to the Component 2 rule suite, not here.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from investigation_agent_platform.domain.proof.premises import (
    AbsenceCoverage,
    AbsenceCoverageFact,
    BuildSourceMapping,
    CommitHistory,
    NormalizedCallsiteFact,
    RangeReadFact,
    RuntimeObservationFact,
)

FIXTURE_DIR = (
    Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "proof_kernel" / "g4_component1"
)


def _baseline(filename: str, fixture_id: str) -> dict[str, Any]:
    document = json.loads((FIXTURE_DIR / filename).read_text(encoding="utf-8"))
    for record in document["fixtures"]:
        if record["fixture_id"] == fixture_id:
            return record["inputs"]
    raise AssertionError(f"missing fixture {fixture_id} in {filename}")


REV_A = "a" * 40
REV_B = "b" * 40
SHA_A = "sha256:" + "a" * 64
SHA_B = "sha256:" + "b" * 64
SHA_C = "sha256:" + "c" * 64
SHA_D = "sha256:" + "d" * 64


def _range_read(**overrides: object) -> dict[str, object]:
    record: dict[str, object] = {
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
    }
    record.update(overrides)
    return record


def _callsite(**overrides: object) -> dict[str, object]:
    record: dict[str, object] = {
        "evidence_id": "ev-edge-001",
        "content_hash": SHA_B,
        "premise_type": "NORMALIZED_CALLSITE_FACT",
        "repository": "synthetic/service",
        "file_path": "src/service.py",
        "revision": REV_A,
        "line_start": 20,
        "line_end": 20,
        "caller": "caller",
        "callee": "target",
        "call_text": "caller.target()",
    }
    record.update(overrides)
    return record


def _runtime(**overrides: object) -> dict[str, object]:
    record: dict[str, object] = {
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
    }
    record.update(overrides)
    return record


def _mapping(**overrides: object) -> dict[str, object]:
    record: dict[str, object] = {
        "evidence_id": "ev-build-map-001",
        "content_hash": SHA_C,
        "premise_type": "BUILD_SOURCE_MAPPING",
        "build_identity": "build-001",
        "repository": "synthetic/service",
        "source_revision": REV_A,
        "provenance_ref": "synthetic-test-only-authority-001",
        "mapping_digest": SHA_D,
    }
    record.update(overrides)
    return record


def _coverage(**overrides: object) -> dict[str, object]:
    record: dict[str, object] = {
        "evidence_id": "ev-coverage-001",
        "content_hash": SHA_B,
        "premise_type": "ABSENCE_COVERAGE_FACT",
        "universe": "repository revision path prefix",
        "predicate": "contains_symbol(target)",
        "search_status": "COMPLETE_EMPTY",
        "search_config_digest": SHA_A,
        "scope": {"repository": "synthetic/service", "revision": REV_B, "path_scope": "src/"},
        "exclusions": [],
        "coverage_basis": {"kind": "synthetic_complete_inventory", "value": "all files enumerated"},
        "match_count": 0,
    }
    record.update(overrides)
    return record


def _history(**overrides: object) -> dict[str, object]:
    record: dict[str, object] = {
        "evidence_id": "ev-history-001",
        "content_hash": SHA_A,
        "premise_type": "COMMIT_HISTORY",
        "repository": "synthetic/service",
        "repository_revision": "d" * 40,
        "commit_id": "c" * 40,
        "file_path": "src/service.py",
        "subject": "Update target handling",
    }
    record.update(overrides)
    return record


def test_range_read_positive_shape() -> None:
    premise = RangeReadFact.model_validate(_range_read())
    assert premise.target == "target"
    assert premise.model_dump(mode="json") == {
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
    }


def test_callsite_positive_shape() -> None:
    premise = NormalizedCallsiteFact.model_validate(_callsite())
    assert (premise.caller, premise.callee) == ("caller", "target")


def test_runtime_positive_shapes() -> None:
    assert RuntimeObservationFact.model_validate(_runtime()).observed_value == 200
    assert (
        RuntimeObservationFact.model_validate(_runtime(observed_value="ok")).observed_value == "ok"
    )
    assert (
        RuntimeObservationFact.model_validate(_runtime(observed_value=True)).observed_value is True
    )
    assert (
        RuntimeObservationFact.model_validate(_runtime(observed_value=None)).observed_value is None
    )


def test_mapping_positive_shape() -> None:
    assert BuildSourceMapping.model_validate(_mapping()).mapping_digest == SHA_D


def test_coverage_positive_shape() -> None:
    assert AbsenceCoverageFact.model_validate(_coverage()).match_count == 0


def test_history_positive_shape() -> None:
    assert CommitHistory.model_validate(_history()).commit_id == "c" * 40


def test_missing_required_field_rejected() -> None:
    record = _range_read()
    del record["window_text"]
    with pytest.raises(ValidationError):
        RangeReadFact.model_validate(record)


def test_explicit_null_on_required_field_rejected() -> None:
    with pytest.raises(ValidationError):
        RangeReadFact.model_validate(_range_read(target=None))


def test_unknown_field_rejected() -> None:
    with pytest.raises(ValidationError):
        RangeReadFact.model_validate(_range_read(extra_field="nope"))


def test_unknown_nested_scope_key_rejected() -> None:
    with pytest.raises(ValidationError):
        AbsenceCoverageFact.model_validate(
            _coverage(
                scope={"repository": "s", "revision": REV_B, "path_scope": "src/", "extra": "x"}
            )
        )


def test_wrong_literal_tag_rejected() -> None:
    with pytest.raises(ValidationError):
        RangeReadFact.model_validate(_range_read(premise_type="NORMALIZED_CALLSITE_FACT"))


def test_malformed_content_hash_rejected() -> None:
    with pytest.raises(ValidationError):
        RangeReadFact.model_validate(_range_read(content_hash="sha256:zzz"))
    with pytest.raises(ValidationError):
        RangeReadFact.model_validate(_range_read(content_hash="aaaa"))


def test_inverted_line_range_rejected() -> None:
    with pytest.raises(ValidationError):
        RangeReadFact.model_validate(_range_read(line_start=14, line_end=10))
    with pytest.raises(ValidationError):
        NormalizedCallsiteFact.model_validate(_callsite(line_start=5, line_end=4))


def test_non_positive_line_numbers_rejected() -> None:
    with pytest.raises(ValidationError):
        RangeReadFact.model_validate(_range_read(line_start=0, line_end=4))


def test_strict_mode_rejects_coercion() -> None:
    with pytest.raises(ValidationError):
        RangeReadFact.model_validate(_range_read(line_start="10"))


def test_float_observed_value_rejected() -> None:
    with pytest.raises(ValidationError):
        RuntimeObservationFact.model_validate(_runtime(observed_value=200.5))


def test_uppercase_history_sha_rejected() -> None:
    with pytest.raises(ValidationError):
        CommitHistory.model_validate(_history(commit_id="C" * 40))


def test_abbreviated_history_sha_rejected() -> None:
    with pytest.raises(ValidationError):
        CommitHistory.model_validate(_history(commit_id="c" * 12))
    with pytest.raises(ValidationError):
        CommitHistory.model_validate(_history(repository_revision="d" * 12))


def test_longer_revision_digest_rejected_for_full_sha_fields() -> None:
    with pytest.raises(ValidationError):
        RuntimeObservationFact.model_validate(_runtime(source_revision="a" * 64))


def test_records_are_frozen() -> None:
    premise = RangeReadFact.model_validate(_range_read())
    with pytest.raises(ValidationError):
        premise.target = "other"  # type: ignore[misc]


def test_exclusions_are_immutable_tuples() -> None:
    coverage = AbsenceCoverageFact.model_validate(_coverage())
    assert isinstance(coverage.exclusions, tuple)
    with pytest.raises(AttributeError):
        coverage.exclusions.append("src/")  # type: ignore[attr-defined]
    with pytest.raises(TypeError):
        coverage.exclusions[0] = "src/"  # type: ignore[index]


def test_manifest_coverage_exclusions_are_immutable() -> None:
    record = AbsenceCoverage.model_validate(
        {k: v for k, v in _coverage().items() if k not in ("evidence_id", "content_hash")}
    )
    assert isinstance(record.exclusions, tuple)
    with pytest.raises(AttributeError):
        record.exclusions.append("src/")  # type: ignore[attr-defined]


def test_component1_behavior_baseline_loads_through_strict_models() -> None:
    inputs = _baseline("behavior_evidence_v1.json", "G4E-BEH-001")
    code = RangeReadFact.model_validate(inputs["code_premise"])
    runtime = RuntimeObservationFact.model_validate(inputs["runtime_premise"])
    mapping = BuildSourceMapping.model_validate(inputs["build_source_mapping"])
    assert code.target in code.window_text
    assert runtime.predicate == inputs["proposition"]["predicate"]
    assert runtime.observed_value == inputs["proposition"]["asserted_value"]
    assert mapping.provenance_ref == "synthetic-test-only-authority-001"


def test_component1_absence_baseline_loads_through_strict_models() -> None:
    inputs = _baseline("absence_evidence_v1.json", "G4E-ABS-001")
    coverage = AbsenceCoverage.model_validate(inputs["coverage_evidence"])
    assert coverage.search_status == "COMPLETE_EMPTY"
    assert coverage.match_count == 0
    assert coverage.universe == inputs["proposition"]["target"]["universe"]


def test_component1_history_baseline_loads_through_strict_models() -> None:
    inputs = _baseline("history_evidence_v1.json", "G4E-HIS-001")
    history = CommitHistory.model_validate(inputs["history_premise"])
    assert history.commit_id == inputs["proposition"]["target"]["commit_id"]
    assert history.subject == inputs["proposition"]["target"]["subject"]
