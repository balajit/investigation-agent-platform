"""Proof-kernel baseline freezer, WP-A1 (TEST-ONLY tooling, stdlib only).

Freezes the investigation battery as the reproducible baseline dataset:
per-claim records derived from tests/fixtures/battery_cases/*.json plus a
manifest pinning the repository revision and every input file digest.

Per-claim live traces (retrieved/bound evidence IDs from live runs) attach
under the same claim IDs when a live stack is available; until then each
live claim is explicitly marked attribution pending. Nothing here executes
the workflow, touches production code, or changes behavior.

Usage:
    python3 scripts/proof_kernel_baseline.py [--check]

--check regenerates in memory and fails without writing on any mismatch.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CASEDIR = ROOT / "tests" / "fixtures" / "battery_cases"
FIXDIR = ROOT / "tests" / "fixtures" / "proof_kernel"
MANIFEST = FIXDIR / "baseline_manifest_v1.json"
CLAIMS = FIXDIR / "baseline_claims_v1.json"

VERSION = "baseline/v1"

# Failure-attribution buckets (closure-harness vocabulary). Live runs fill
# `bucket` per claim; the frozen baseline only declares the taxonomy.
ATTRIBUTION_BUCKETS = [
    "never_retrieved",
    "retrieved_but_unbound",
    "bound_but_wrong_type",
    "withheld_by_refuting",
    "provider_or_analyzer_failed",
    "genuinely_insufficient",
    "wrong_revision_or_scope",
]

PROOF_REQUIREMENTS = {
    "DEFINITION": ["range read window containing the symbol, same file+revision"],
    "CALL_EDGE": ["shown call site for the named caller", "same file+revision binding"],
    "HISTORY": ["matching COMMIT_HISTORY row: file+commit+subject"],
    "BEHAVIOR": ["code path + runtime observation; code-only demotes"],
    "ABSENCE": ["scoped empty-search proof (COMPLETE_EMPTY within declared universe)"],
}

# battery-case `expect.claim` -> ClaimKind. Cases without a claim word are
# abstention shapes (no provable claim expected).
CLAIM_OF = {
    "definition": "DEFINITION",
    "callers": "CALL_EDGE",
    "history": "HISTORY",
    "values": "BEHAVIOR",
    "absence": "ABSENCE",
}


def fail(msg: str) -> None:
    print(f"BASELINE FAIL: {msg}", file=sys.stderr)


def digest_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def git_rev() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        return out.stdout.strip() if out.returncode == 0 else "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def load_cases() -> list[dict]:
    cases = []
    for path in sorted(CASEDIR.glob("*.json")):
        with open(path, encoding="utf-8") as fh:
            cases.append((path.name, json.load(fh)))
    return cases


def derive_claims() -> tuple[list[dict], list[dict]]:
    """Returns (claims, abstentions). Pure transform of the case fixtures."""
    claims: list[dict] = []
    abstentions: list[dict] = []
    for fname, case in load_cases():
        cid = case.get("id", fname)
        expect = case.get("expect", {}) or {}
        status = expect.get("status", "UNKNOWN")
        kind = CLAIM_OF.get(expect.get("claim", ""))
        live = bool(case.get("live", False))
        if kind is None:
            abstentions.append(
                {
                    "case_id": cid,
                    "expected_terminal": status,
                    "live": live,
                    "note": "abstention shape: no provable claim expected",
                }
            )
            continue
        if kind == "CALL_EDGE":
            callees = expect.get("expect_any", []) or []
            for caller in callees:
                claims.append(_claim(cid, f"{cid}.caller[{caller}]", kind, expect, status, live))
        else:
            claims.append(_claim(cid, f"{cid}.{kind.lower()}", kind, expect, status, live))
    return claims, abstentions


def _claim(cid, claim_id, kind, expect, status, live):
    target = {k: expect.get(k) for k in ("cited_path", "expect_symbol", "scope") if k in expect}
    if kind == "CALL_EDGE":
        target = {"caller": claim_id.split("[", 1)[1].rstrip("]"), **target}
    return {
        "claim_id": claim_id,
        "case_id": cid,
        "claim_kind": kind,
        "target": target,
        "expected_terminal": status,
        "live": live,
        "proof_requirements": PROOF_REQUIREMENTS[kind],
        "attribution": {
            "status": "live-pending" if live else "offline-gradable",
            "bucket": None,
            "evidence": None,
        },
    }


def build() -> tuple[dict, dict]:
    cases = load_cases()
    file_digests = {name: digest_bytes((CASEDIR / name).read_bytes()) for name, _ in cases}
    claims, abstentions = derive_claims()
    manifest = {
        "version": VERSION,
        "note": "TEST-ONLY frozen baseline. No production code reads these files.",
        "git_rev": git_rev(),
        "battery_cases": sorted(name for name, _ in cases),
        "battery_case_digests": file_digests,
        "attribution_buckets": ATTRIBUTION_BUCKETS,
        "live_trace_status": "pending: per-claim retrieved/bound IDs attach here after a live run",
    }
    dataset = {
        "version": VERSION,
        "claims": claims,
        "abstentions": abstentions,
    }
    return manifest, dataset


def main(check_only: bool = False) -> int:
    errors: list[str] = []
    manifest, dataset = build()

    for path, doc in ((MANIFEST, manifest), (CLAIMS, dataset)):
        if not path.exists():
            if check_only:
                errors.append(f"missing frozen baseline: {path.name}")
            continue
        try:
            committed = json.loads(path.read_bytes())
        except (OSError, json.JSONDecodeError) as e:
            errors.append(f"unreadable {path.name}: {e}")
            continue
        if committed != doc and not (
            path == MANIFEST and committed.get("git_rev") != doc.get("git_rev")
        ):
            errors.append(f"stale frozen baseline: {path.name} (regenerate with the script)")
        if path == MANIFEST and committed.get("git_rev") != doc.get("git_rev"):
            errors.append(
                f"baseline pinned to {committed.get('git_rev')}, worktree is {doc.get('git_rev')}: "
                "re-freeze deliberately or stay pinned"
            )

    if errors:
        for e in errors:
            fail(e)
        return 1
    if check_only:
        n_claims = len(dataset["claims"])
        n_abs = len(dataset["abstentions"])
        print(f"baseline check: PASS ({n_claims} claims, {n_abs} abstention shapes)")
        return 0

    MANIFEST.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    CLAIMS.write_text(json.dumps(dataset, indent=2, sort_keys=True) + "\n")
    print(f"wrote {MANIFEST.relative_to(ROOT)} + {CLAIMS.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main("--check" in sys.argv[1:]))
