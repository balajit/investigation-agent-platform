"""Validator fingerprint control tests (fingerprints/v1).

The manifest pins exact source bytes for the eight semantic-boundary
modules. These tests prove the control detects unreviewed drift without
blessing changes: a modified copy fails, the repository copy passes,
and structural manifest defects fail closed.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from investigation_agent_platform.domain.proof.validator_fingerprints import (
    FINGERPRINTED_FILES,
    compute_manifest,
    verify_manifest,
)

ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = ROOT / "tests" / "fixtures" / "proof_kernel" / "validator_fingerprints_v1.json"


def _load_manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def test_fingerprint_manifest_covers_reviewed_modules() -> None:
    manifest = _load_manifest()
    assert manifest["schema_version"] == "validator-fingerprints/v1"
    assert set(manifest["files"]) == set(FINGERPRINTED_FILES)
    assert len(manifest["files"]) == 8


def test_fingerprint_check_passes_on_repository_copy() -> None:
    assert verify_manifest(ROOT, _load_manifest()) == []


def test_validator_fingerprint_check_detects_unreviewed_source_change(tmp_path: Path) -> None:
    for relpath in FINGERPRINTED_FILES:
        target = tmp_path / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / relpath, target)
    manifest = compute_manifest(tmp_path)
    assert verify_manifest(tmp_path, manifest) == []
    victim = tmp_path / FINGERPRINTED_FILES[1]
    victim.write_text(victim.read_text(encoding="utf-8") + "\n# unreviewed change\n")
    errors = verify_manifest(tmp_path, manifest)
    assert errors, "modified source must fail the fingerprint check"
    assert any(FINGERPRINTED_FILES[1] in error for error in errors)
    assert any("expected:" in error and "actual:" in error for error in errors)


def test_fingerprint_check_fails_closed_on_unknown_schema() -> None:
    manifest = _load_manifest()
    manifest["schema_version"] = "validator-fingerprints/v9"
    errors = verify_manifest(ROOT, manifest)
    assert len(errors) == 1
    assert "unsupported fingerprint schema" in errors[0]
