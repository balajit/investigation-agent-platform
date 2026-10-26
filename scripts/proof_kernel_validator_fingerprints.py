"""Checked-in validator fingerprint control for the G4 proof kernel.

Usage:
    uv run python scripts/proof_kernel_validator_fingerprints.py          # record (reviewed change only)
    uv run python scripts/proof_kernel_validator_fingerprints.py --check  # verify; nonzero on drift

The record mode writes the pinned digests. It must only be run as part
of a reviewed contract/version change with associated semantic tests —
--check never updates the manifest automatically.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from investigation_agent_platform.domain.proof.validator_fingerprints import (
    MANIFEST_REL_PATH,
    compute_manifest,
    verify_manifest,
)

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / MANIFEST_REL_PATH


def main(check_only: bool) -> int:
    if not check_only:
        manifest = compute_manifest(ROOT)
        MANIFEST_PATH.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        print(f"recorded {len(manifest['files'])} fingerprints -> {MANIFEST_REL_PATH}")
        return 0
    try:
        manifest = json.loads(MANIFEST_PATH.read_bytes())
    except (OSError, json.JSONDecodeError) as exc:
        print(f"FINGERPRINT FAIL: cannot load manifest: {exc}")
        return 1
    errors = verify_manifest(ROOT, manifest)
    for error in errors:
        print(f"FINGERPRINT FAIL: {error}")
    if errors:
        return 1
    print(f"fingerprints clean: {len(manifest.get('files', {}))} modules verified")
    return 0


if __name__ == "__main__":
    sys.exit(main("--check" in sys.argv[1:]))
