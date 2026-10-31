# src/investigation_agent_platform/application/analysis/subjects.py
"""Subject matching shared by analyzers and the verifier (J D13).

Moved verbatim from ``VerificationEngine._subject_identifies`` so window
budget allocation orders by the claim subject with the same rule the live
claim flow uses. Exact match wins; otherwise a dot-boundary suffix in
either direction matches — except a bare requested name (no dot) never
suffix-matches a qualified candidate. Never substring.
"""

from __future__ import annotations


def subject_identifies(requested: str | None, candidates: set[str]) -> bool:
    """Exact match, else dot-boundary suffix in either direction (never substring).

    A bare requested name (no dot) never suffix-matches a qualified candidate.
    """
    req = str(requested or "").strip()
    if not req:
        return False
    for candidate in candidates:
        cand = str(candidate or "").strip()
        if not cand:
            continue
        if cand == req or ("." in req and cand.endswith("." + req)) or req.endswith("." + cand):
            return True
    return False
