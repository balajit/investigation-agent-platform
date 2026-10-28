# src/investigation_agent_platform/domain/proof/validator_fingerprints.py
"""Executable validator drift control (fingerprints/v1): pure, no I/O on import.

Pins the exact source bytes of the seven semantic-boundary modules whose
unchecked modification could silently change proof semantics without a
contract or version change:

- ``premises.py`` — strict parsing, literal tags, nested closed keys,
  scalar restrictions, null/omission behavior;
- ``rules.py`` — producer validators and the closed registry;
- ``checker_semantics.py`` — independent checker semantics;
- ``manifest.py`` — manifest key-set binding and typed coverage;
- ``evaluation.py`` — context construction and required bundle version;
- ``canonical.py`` — identity and byte normalization primitives.

The fingerprint controls checked-in source bytes only. It is not a
deployment-image or interpreter attestation, and updating a fingerprint
requires a reviewed contract/version change with associated semantic
tests — the ``--check`` entry point never blesses changes automatically.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

FINGERPRINT_SCHEMA_VERSION = "validator-fingerprints/v1"

FINGERPRINTED_FILES: tuple[str, ...] = (
    "src/investigation_agent_platform/domain/proof/premises.py",
    "src/investigation_agent_platform/domain/proof/rules.py",
    "src/investigation_agent_platform/domain/proof/checker_semantics.py",
    "src/investigation_agent_platform/domain/proof/second_path.py",
    "src/investigation_agent_platform/domain/proof/manifest.py",
    "src/investigation_agent_platform/domain/proof/evaluation.py",
    "src/investigation_agent_platform/domain/proof/canonical.py",
)

MANIFEST_REL_PATH = "tests/fixtures/proof_kernel/validator_fingerprints_v1.json"


def fingerprint_file(root: Path, relpath: str) -> str:
    """SHA-256 (``sha256:``-prefixed) of a module's exact source bytes."""
    raw = (Path(root) / relpath).read_bytes()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def compute_manifest(root: Path) -> dict[str, Any]:
    """Compute the fingerprint manifest for the seven modules."""
    return {
        "schema_version": FINGERPRINT_SCHEMA_VERSION,
        "files": {relpath: fingerprint_file(root, relpath) for relpath in FINGERPRINTED_FILES},
    }


def verify_manifest(root: Path, manifest: Mapping[str, Any]) -> list[str]:
    """Check pinned digests against current source bytes.

    Returns failure lines (empty when clean). Fails closed on unknown
    schema versions, missing files, digest mismatches, and manifest
    entries outside the reviewed module set.
    """
    errors: list[str] = []
    if not isinstance(manifest, dict):
        return ["fingerprint manifest is not an object"]
    if manifest.get("schema_version") != FINGERPRINT_SCHEMA_VERSION:
        return [f"unsupported fingerprint schema: {manifest.get('schema_version')!r}"]
    pinned = manifest.get("files")
    if not isinstance(pinned, dict):
        return ["fingerprint manifest has no files mapping"]
    for relpath in FINGERPRINTED_FILES:
        if relpath not in pinned:
            errors.append(f"missing fingerprint entry: {relpath}")
            continue
        try:
            actual = fingerprint_file(root, relpath)
        except OSError:
            errors.append(f"fingerprinted source missing: {relpath}")
            continue
        if pinned[relpath] != actual:
            errors.append(
                f"source changed without reviewed fingerprint update: {relpath}\n"
                f"  expected: {pinned[relpath]}\n"
                f"  actual:   {actual}"
            )
    for relpath in pinned:
        if relpath not in FINGERPRINTED_FILES:
            errors.append(f"unreviewed fingerprint entry: {relpath}")
    return errors
