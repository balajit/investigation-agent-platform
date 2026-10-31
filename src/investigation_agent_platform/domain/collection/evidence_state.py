# src/investigation_agent_platform/domain/collection/evidence_state.py
"""Evidence-state derivation from analyzer status (J10 D21, pure domain).

``AnalysisStamp`` parses a history ``analysis`` dict (or nothing, for old
entries); ``derive_evidence_state`` folds recent analyzed turns into an
``EvidenceState``. ``claims_verified`` and ``match_predates_onset`` are
producers for LOCAL_COMPLETE and STALE_MATCH that need claim-level facts
and onset anchors the cascade does not hold today: production passes
``None`` for both until the owner schedules that wiring, so neither state
is produced yet.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, fields

from investigation_agent_platform.domain.collection.frontier import EvidenceState

RECENT_ANALYZED_TURNS = 2  # a widen-and-retry must be able to supersede an earlier refusal


@dataclass(frozen=True)
class AnalysisStamp:
    resolved: int = 0
    partial: int = 0
    unresolved: int = 0
    window: int = 0
    runtime: int = 0
    unsupported: int = 0
    failure: int = 0
    provenance: int = 0

    @classmethod
    def parse(cls, raw: object) -> AnalysisStamp | None:
        if not isinstance(raw, dict):
            return None
        try:
            return cls(**{f.name: int(raw.get(f.name, 0) or 0) for f in fields(cls)})
        except (TypeError, ValueError):
            return None


def derive_evidence_state(
    *,
    banked_total: int,
    stamps: Sequence[AnalysisStamp],
    claims_verified: bool | None = None,
    match_predates_onset: bool | None = None,
) -> EvidenceState:
    """Pure. ``stamps`` are in turn order; only the most recent ones count.

    ``claims_verified`` and ``match_predates_onset`` are producers for LOCAL_COMPLETE
    and STALE_MATCH that need claim-level and onset-anchor facts the cascade does not
    hold today; production passes ``None`` for both until the owner schedules that
    wiring, so neither state is produced yet (an analyzer-RESOLVED row for one
    selector cannot show that the claim needs nothing further).
    """
    if banked_total <= 0:
        return EvidenceState.NO_ROWS
    recent = list(stamps)[-RECENT_ANALYZED_TURNS:]
    if not recent:
        return EvidenceState.ROWS_WITHOUT_MATCH
    # A clean analyzed turn supersedes earlier refusals (widen-and-retry):
    # only refusals after the last clean stamp count as gave up.
    effective = recent
    for index in range(len(recent) - 1, -1, -1):
        if not (recent[index].partial or recent[index].unresolved):
            effective = recent[index + 1 :]
            break
    gave_up = [s for s in effective if s.partial or s.unresolved]
    if any(s.runtime or s.failure for s in gave_up):
        return EvidenceState.ANALYSIS_BLOCKED
    if gave_up:
        return EvidenceState.ANALYSIS_INCOMPLETE
    if match_predates_onset:
        return EvidenceState.STALE_MATCH
    if claims_verified:
        return EvidenceState.LOCAL_COMPLETE
    return EvidenceState.ROWS_WITHOUT_MATCH
