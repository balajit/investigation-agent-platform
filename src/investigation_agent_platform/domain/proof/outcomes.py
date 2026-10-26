# src/investigation_agent_platform/domain/proof/outcomes.py
"""Shared closed checker-outcome vocabulary (frozen §3.4).

Split out of ``certificate.py`` so the independent checker semantics
(``checker_semantics.py``) can return checker verdicts without importing
the certificate module — and so ``certificate.py`` keeps a compatibility
re-export for the current importers (composition, publication, the
application publication service, and unit tests).
"""

from __future__ import annotations

from enum import Enum


class CheckerOutcome(str, Enum):
    """Checker verdicts (frozen §3.4 vocabulary)."""

    VALID = "VALID"
    MISSING_PREMISE = "MISSING_PREMISE"
    INADMISSIBLE_PREMISE = "INADMISSIBLE_PREMISE"
    INVALID_INFERENCE = "INVALID_INFERENCE"
    SCOPE_MISMATCH = "SCOPE_MISMATCH"
    UNSUPPORTED_CONSTRUCT = "UNSUPPORTED_CONSTRUCT"
    STALE_CERTIFICATE = "STALE_CERTIFICATE"
    CONFLICTING_EVIDENCE = "CONFLICTING_EVIDENCE"
    CHECKER_FAILURE = "CHECKER_FAILURE"
    PREMISE_UNAVAILABLE = "PREMISE_UNAVAILABLE"
