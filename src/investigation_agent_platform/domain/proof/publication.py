# src/investigation_agent_platform/domain/proof/publication.py
"""WP-C2 atomic publication decision (frozen, pure, deterministic).

The linearization checklist (contracts §5.1), evaluated in order:

1. A pinned evaluation context exists (expected_context_id is set).
2. The certificate is checker-accepted (VALID, never a lesser outcome).
3. Reconciliation is adjudicated (no unadjudicated material conflict).
4. The context is still current at the commit point
   (certificate context == pinned context, else STALE → reevaluate, deny).
5. Policy (Layer B) permits publication for this kind.

The commit binds the exact certificate + context IDs (derivation_id,
context_id): a bare ``VERIFIED`` without a valid current derivation is
never authorized (K13). Infrastructure failure (PREMISE_UNAVAILABLE,
CHECKER_FAILURE) never becomes a negative proof verdict — it denies
publication as retryable, not as refutation.

No I/O, no environment, no clock. Denial reasons are stable codes.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from investigation_agent_platform.domain.proof.certificate import CheckerOutcome


class PublicationDecision(str, Enum):
    """Commit or deny (frozen; Layer A validity + Layer B policy)."""

    COMMIT = "COMMIT"
    DENY = "DENY"


class DenyReason(str, Enum):
    """Stable denial codes (frozen; append-only)."""

    NO_PINNED_CONTEXT = "NO_PINNED_CONTEXT"
    NOT_CHECKER_ACCEPTED = "NOT_CHECKER_ACCEPTED"
    CONFLICT_UNRESOLVED = "CONFLICT_UNRESOLVED"
    STALE_CONTEXT = "STALE_CONTEXT"
    POLICY_DENIED = "POLICY_DENIED"
    OBLIGATIONS_ABSENT = "OBLIGATIONS_ABSENT"
    OPEN_OBLIGATION = "OPEN_OBLIGATION"
    UNSUPPORTED_OBLIGATION = "UNSUPPORTED_OBLIGATION"
    STALE_OBLIGATION = "STALE_OBLIGATION"


class PublicationCommit(BaseModel):
    """The authorized commit: exact certificate + context binding (never bare)."""

    model_config = ConfigDict(frozen=True)

    derivation_id: str = Field(..., max_length=256)
    context_id: str = Field(..., max_length=256)


def _decide_linearization(
    checker_outcome: CheckerOutcome,
    certificate_context_id: str,
    expected_context_id: str | None,
    reconciliation_adjudicated: bool,
    policy_eligible: bool,
    policy_reason: str = "",
) -> tuple[PublicationDecision, str]:
    """Linearization checklist sub-step (private; not a promotion entry).

    Applies the ordered checklist to gate-derived values only. Callable
    only by ``decide_guarded_promotion`` (obligations layer); no caller
    can reach a COMMIT through this helper with a supplied checker
    outcome, because the helper takes the outcome the guarded entry
    produced internally. Tests pin the ordering through this private
    seam; the public promotion contract lives on the guarded entry.
    """
    if not expected_context_id:
        return PublicationDecision.DENY, DenyReason.NO_PINNED_CONTEXT.value
    if checker_outcome != CheckerOutcome.VALID:
        return (
            PublicationDecision.DENY,
            f"{DenyReason.NOT_CHECKER_ACCEPTED.value}:{checker_outcome.value}",
        )
    if not reconciliation_adjudicated:
        return PublicationDecision.DENY, DenyReason.CONFLICT_UNRESOLVED.value
    if certificate_context_id != expected_context_id:
        return PublicationDecision.DENY, DenyReason.STALE_CONTEXT.value
    if not policy_eligible:
        reason = policy_reason.strip() or "layer-B policy denies"
        return PublicationDecision.DENY, f"{DenyReason.POLICY_DENIED.value}:{reason}"
    return PublicationDecision.COMMIT, "linearization checklist satisfied"


def build_commit(
    derivation_id: str,
    context_id: str,
    expected_context_id: str | None,
) -> PublicationCommit:
    """Bind the exact derivation + context IDs; bare VERIFIED refuses.

    The commit is only constructible when a pinned context exists and
    matches the certificate context — a context move must reevaluate,
    never mutate. An unpinned commit is refused (F-C2): without a pin
    there is no freshness to enforce.
    """
    der = str(derivation_id or "")
    ctx = str(context_id or "")
    if not der or not ctx:
        raise ValueError(
            "BARE_VERDICT: commit needs derivation_id + context_id, never bare VERIFIED"
        )
    if expected_context_id is None or ctx != str(expected_context_id):
        raise ValueError("STALE_CONTEXT: context moved since evaluation; reevaluate, never mutate")
    return PublicationCommit(derivation_id=der, context_id=ctx)
