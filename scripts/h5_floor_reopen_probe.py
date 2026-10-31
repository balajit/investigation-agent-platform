"""H5 reopen detector, layer 2: would verification have succeeded (read-only inquiry aid).

Layer 1 is a read-only SQL rate over persisted assessments (see the H5 design note).
This module holds the pure parts: re-typing NC-with-identifier assessments as
DEFINITION over the run's banked rows and asking the location validator, plus
the threshold verdict. The thresholds are PROPOSALS for the owner, not decisions.
Running any of this against a database is an owner-approved step.
"""

from __future__ import annotations

from typing import Any


def nc_with_identifier_assessments(hypothesis: Any) -> list[Any]:
    """NC assessments whose claim names code (target symbol/file or identifier-like statement)."""
    try:
        from investigation_agent_platform.application.investigation.reasoning import (
            nc_names_identifier,
        )
    except Exception:
        return []
    out = []
    try:
        for asm in list(getattr(hypothesis, "assessments", None) or []):
            try:
                if str(getattr(asm, "claim_kind", "") or "") != "NOT_CHECKABLE":
                    continue
                target = getattr(asm, "claim_target", None)
                target_map = (
                    dict(target.model_dump()) if hasattr(target, "model_dump") else {}
                )
                if nc_names_identifier(str(getattr(asm, "statement", "") or ""), target_map):
                    out.append(asm)
            except Exception:
                continue
    except Exception:
        return []
    return out


def would_verify_if_retyped(hypothesis: Any, evidence_rows: list[Any]) -> bool:
    """Re-type NC-with-identifier assessments as DEFINITION and ask the location validator."""
    try:
        retyped = [
            a.model_copy(update={"claim_kind": "DEFINITION"})
            for a in nc_with_identifier_assessments(hypothesis)
        ]
        if not retyped:
            return False
        from investigation_agent_platform.application.investigation.verification import (
            VerificationEngine,
            VerificationStatus,
        )

        view = hypothesis.model_copy(update={"assessments": retyped})
        result = VerificationEngine().validate_location_claim(view, list(evidence_rows or []), [])
        return bool(result is not None and result.status == VerificationStatus.VERIFIED)
    except Exception:
        return False


def reopen_verdict(rows: list[dict], *, min_runs: int = 30, min_rate: float = 0.10, min_would_verify: float = 0.30) -> dict:
    """Threshold verdict over per-run layer-1 rows. Pure; thresholds are proposals."""
    runs = len(rows)
    nc_id = sum(int(r.get("nc_with_identifier", 0) or 0) for r in rows)
    typed = sum(int(r.get("typed", 0) or 0) for r in rows)
    verified = sum(int(r.get("would_verify", 0) or 0) for r in rows)
    rate = (nc_id / typed) if typed else 0.0
    wv = (verified / nc_id) if nc_id else 0.0
    return {
        "runs": runs,
        "nc_id_rate": rate,
        "would_verify_rate": wv,
        "reopen": runs >= min_runs and rate >= min_rate and wv >= min_would_verify,
    }
