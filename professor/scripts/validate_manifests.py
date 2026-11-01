#!/usr/bin/env python3
"""Validate G2 session manifests without modifying durable artifacts."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

try:
    from jsonschema import Draft202012Validator
except ImportError:
    print("BLOCKED: install requirements-test.txt", file=sys.stderr)
    raise SystemExit(2)


ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "workspace" / "evidence"
SCHEMA_PATH = EVIDENCE / "session-manifest.schema.json"
SECRET_URL = re.compile(r"https?://[^/\s\"\\]+:[^@/\s\"\\]+@")


def fail(message: str, failures: list[str]) -> None:
    print("FAIL: " + message)
    failures.append(message)


def main() -> int:
    failures: list[str] = []
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    manifests = sorted(EVIDENCE.glob("phase2-*.manifest.json"))
    if len(manifests) != 10:
        fail(f"expected 10 qualification manifests, found {len(manifests)}", failures)
    validator = Draft202012Validator(schema)
    seen_sessions: set[str] = set()
    for path in manifests:
        manifest = json.loads(path.read_text(encoding="utf-8"))
        errors = list(validator.iter_errors(manifest))
        if errors:
            fail(f"{path.name}: schema errors={len(errors)}", failures)
        session_id = manifest.get("session_id")
        if session_id in seen_sessions:
            fail(f"duplicate session_id: {session_id}", failures)
        seen_sessions.add(session_id)
        transcript = ROOT / manifest["evidence_integrity"]["transcript_path"]
        if not transcript.is_file():
            fail(f"{path.name}: missing transcript {transcript}", failures)
            continue
        digest = hashlib.sha256(transcript.read_bytes()).hexdigest()
        if digest != manifest["evidence_integrity"]["transcript_sha256"]:
            fail(f"{path.name}: transcript hash mismatch", failures)
        for item in manifest["repository"]["file_hashes"]:
            source = ROOT / item["path"]
            if not source.is_file():
                fail(f"{path.name}: missing hashed file {item['path']}", failures)
                continue
            actual = hashlib.sha256(source.read_bytes()).hexdigest()
            if actual != item["sha256"]:
                fail(f"{path.name}: file hash mismatch {item['path']}", failures)
    secret_hits = sum(len(SECRET_URL.findall(path.read_text(encoding="utf-8", errors="replace"))) for path in EVIDENCE.glob("*.json"))
    if secret_hits:
        fail(f"credential-shaped URLs found in evidence JSON: {secret_hits}", failures)
    for path in manifests:
        print("PASS: " + path.name)
    print(f"Manifest validation: {len(manifests) - bool(failures)} manifest set(s) passed, {len(failures)} failure(s).")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
