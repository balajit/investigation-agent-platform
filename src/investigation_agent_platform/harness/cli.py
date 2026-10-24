"""Harness command surface (dev scope only; no collection, no writes).

Commands:
  check-case --registry PATH --vectors PATH --case ID [--expect accept|reject]
      Schema-gate one report case against the registry join contract, then
      recompute R1–R8 from its source records. Exits 0 iff the outcome
      matches `--expect` (default: the case's own expectation).
  check-case --registry PATH --vectors PATH --all
      Run every report case the same way; exits 0 iff all match.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from investigation_agent_platform.harness.metrics import verify_report
from investigation_agent_platform.harness.registry import (
    HarnessValidationError,
    load_registry,
    validator_for,
)

#: Pinned SHA-256 of the vectors file bytes
#: (`docs/IAP-proof-kernel-d3-stageC-schema-vectors-v1.15.json`).
#: Every CLI entry point verifies this digest before trusting any
#: vector-defined expectation, so a modified vectors file keeping the
#: same registry id is rejected. Any vectors change requires a new pin
#: and review, exactly like the registry pin.
VECTORS_SHA256 = "810125cc87198a7f68eb88bd8ac959b6b77264d581c2f34c1362136ada2bfcf2"


def _load_vectors(path: str | Path) -> dict[str, Any]:
    from investigation_agent_platform.harness.canonical import (
        sha256_digest,
        strict_parse,
    )

    raw = Path(path).read_bytes()
    if sha256_digest(raw) != f"sha256:{VECTORS_SHA256}":
        raise HarnessValidationError("vectors digest mismatch: file is not the pinned vectors set")
    return strict_parse(raw)


def _inputs_for(case: dict[str, Any]) -> dict[str, Any]:
    inputs = {
        key: case[key]
        for key in (
            "turns",
            "attempts",
            "receipts",
            "controls",
            "cohort_fixtures",
            "capture_events",
            "control_native_divergence",
        )
    }
    if "formulas" in case:
        inputs["formulas"] = case["formulas"]
    return inputs


def check_case(registry: dict[str, Any], case: dict[str, Any]) -> None:
    """Schema-gate one case, then recompute R1–R8; raises on any violation.

    Policy (required set, formula catalog, reason codes) is derived from
    the pinned registry inside `verify_report`, never from caller inputs.
    The report is bound to the case freeze: `report["freeze_hash"]` must
    equal the recomputed freeze digest.
    """
    validator_for(registry, "reportCase").validate(case)
    verify_report(
        inputs=_inputs_for(case),
        report=case["report"],
        registry=registry,
        terminal=case["terminal"],
        freeze=case["freeze"],
    )


def _run_one(
    registry: dict[str, Any],
    vectors: dict[str, Any],
    case_id: str,
    expect: str | None = None,
) -> int:
    """Run one loaded case; return 0 iff the outcome matches the expectation."""
    case = next((c for c in vectors["report_cases"] if c["id"] == case_id), None)
    if case is None:
        print(f"FAIL {case_id}: unknown case id")
        return 1
    want = expect or case["expect"]
    try:
        check_case(registry, case)
        ok = want == "accept"
        detail = "recomputed exactly" if ok else "UNEXPECTEDLY ACCEPTED"
    except HarnessValidationError as exc:
        ok = want == "reject"
        detail = f"rejected as expected ({exc})" if ok else f"UNEXPECTED REJECT ({exc})"
    except Exception as exc:
        print(f"FAIL {case_id}: UNEXPECTED ERROR ({type(exc).__name__}: {exc})")
        return 1
    print(f"{'PASS' if ok else 'FAIL'} {case_id}: {detail}")
    return 0 if ok else 1


def run_case(registry_path: str, vectors_path: str, case_id: str, expect: str | None = None) -> int:
    """Run one case; return 0 iff the outcome matches the expectation."""
    registry = load_registry(registry_path)
    vectors = _load_vectors(vectors_path)
    if vectors.get("registry") != registry.get("$id"):
        raise HarnessValidationError("vectors target a different registry")
    return _run_one(registry, vectors, case_id, expect)


def run_all(registry_path: str, vectors_path: str) -> int:
    """Run every report case; return 0 iff all match their expectations."""
    registry = load_registry(registry_path)
    vectors = _load_vectors(vectors_path)
    if vectors.get("registry") != registry.get("$id"):
        raise HarnessValidationError("vectors target a different registry")
    failed = sum(_run_one(registry, vectors, case["id"]) != 0 for case in vectors["report_cases"])
    print(f"{len(vectors['report_cases']) - failed} passed, {failed} failed")
    return 0 if failed == 0 else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="harness")
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check-case", help="verify report cases by recompute")
    check.add_argument("--registry", required=True)
    check.add_argument("--vectors", required=True)
    check.add_argument("--case", default=None)
    check.add_argument("--all", action="store_true")
    check.add_argument("--expect", choices=["accept", "reject"], default=None)
    args = parser.parse_args(argv)
    if args.command == "check-case":
        if args.all:
            return run_all(args.registry, args.vectors)
        if not args.case:
            print("FAIL: --case ID or --all is required")
            return 2
        return run_case(args.registry, args.vectors, args.case, args.expect)
    return 2


if __name__ == "__main__":
    sys.exit(main())
