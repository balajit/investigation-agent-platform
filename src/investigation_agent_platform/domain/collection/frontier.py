# src/investigation_agent_platform/domain/collection/frontier.py
"""Collect frontier (DES-collect-frontier v1.0): the retained implementation.

Best-first ordering, seed translation and the three-exit rule, tested and pure. As of the
advisory-layer decision (review W1) there is no production caller: the loop's pivot,
requery and smallest-fetch slots do not read it. It must NOT be imported from workflow
code: ``FrontierCandidate.seeded_at`` defaults to the wall clock and ids default to uuid4,
neither of which is replay-safe. A consumer (a future frontier-driven slot) must pass
explicit ids and timestamps. See the rebuild notes in the design document.

"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from investigation_agent_platform.domain.hypothesis.templates import (
    DormantTemplate,
    SuspectPath,
)

#: Version pin for every candidate this module emits.
FRONTIER_VERSION_CURRENT = "frontier-v1"

#: Depth cap: bounds call-graph and history-walk radius (DES §3).
MAX_FRONTIER_DEPTH = 4

#: Default time-to-live in loop rounds for a seed. Uncalibrated chunk-1
#: default; battery calibration may move it (version bump, never a branch).
DEFAULT_CANDIDATE_TTL_ROUNDS = 10

#: Fixpoint floor: ordered candidates scoring at or below this are
#: indistinguishable from exhausted (DES §3).
FIXPOINT_SCORE_FLOOR = 0.0


class FrontierDimension(StrEnum):
    """Spread posture for collection (DES §2), keyed to S6 failure character."""

    LAYER_SPREAD = "LAYER_SPREAD"
    ASSUMPTION_SPREAD = "ASSUMPTION_SPREAD"
    RIVAL_SPREAD = "RIVAL_SPREAD"
    TIME_SPREAD = "TIME_SPREAD"


class EvidenceState(StrEnum):
    """Collection-side facts the dimension mapping needs (computed by the
    loop; passed in explicitly here)."""

    NO_ROWS = "NO_ROWS"
    ROWS_WITHOUT_MATCH = "ROWS_WITHOUT_MATCH"
    STALE_MATCH = "STALE_MATCH"
    LOCAL_COMPLETE = "LOCAL_COMPLETE"


class FailureCharacter(BaseModel):
    """The S6 failure character a work order addresses (DES-reason-workorders
    §2): which failure the next fetch is expected to discriminate.

    Deliberately minimal in chunk 1 — a value object over signals the loop
    already computes (triage route, dominant claim kind, rival state), never
    new inference.
    """

    model_config = ConfigDict(frozen=True)

    triage_route: str = Field(..., min_length=1, max_length=32)
    claim_kind: str = Field(default="untyped", max_length=64)
    has_unbeaten_rival: bool = False

    def spread_dimension(self, evidence_state: EvidenceState) -> FrontierDimension | None:
        """Map (character, evidence) to a spread posture (DES §2).

        Precedence is explicit: an unbeaten rival always means rival
        spread, regardless of row state; LOCAL_COMPLETE means there is
        nothing to collect (None — the caller observes an empty frontier
        and exits fixpoint). Cross-boundary termination is per-template,
        not per-character, and is handled at seed time.
        """
        if self.has_unbeaten_rival:
            return FrontierDimension.RIVAL_SPREAD
        if evidence_state == EvidenceState.NO_ROWS:
            return FrontierDimension.LAYER_SPREAD
        if evidence_state == EvidenceState.ROWS_WITHOUT_MATCH:
            return FrontierDimension.ASSUMPTION_SPREAD
        if evidence_state == EvidenceState.STALE_MATCH:
            return FrontierDimension.TIME_SPREAD
        return None


class CandidateStatus(StrEnum):
    """Lifecycle of one frontier candidate.

    Chunk 1 drives PENDING only. The P9 telemetry-deferred states are
    declared so the loop contract is visible, but nothing here produces
    or consumes them — dispatchable() is True for PENDING alone.
    """

    PENDING = "PENDING"
    IN_FLIGHT = "IN_FLIGHT"
    EXHAUSTED = "EXHAUSTED"
    BLOCKED_ON_TELEMETRY = "BLOCKED_ON_TELEMETRY"
    WAITING = "WAITING"
    DEFERRED = "DEFERRED"


class SeedFetch(BaseModel):
    """The discriminating fetch copied from the seeding template at seed
    time. A value copy, not a reference: candidates outlive template
    revisions and must never alias them."""

    model_config = ConfigDict(frozen=True)

    fetch_kind: str = Field(..., min_length=1, max_length=64)
    target: str = Field(..., min_length=1, max_length=1024)


class FrontierCandidate(BaseModel):
    """One best-first collection target (DES §1)."""

    model_config = ConfigDict(frozen=True)

    candidate_id: UUID = Field(default_factory=uuid4)
    version: str = Field(default=FRONTIER_VERSION_CURRENT, max_length=16)
    service: str | None = Field(default=None, max_length=256)
    file_path: str | None = Field(default=None, max_length=1024)
    commit: str | None = Field(default=None, max_length=128)
    span: str | None = Field(default=None, max_length=128)
    config: str | None = Field(default=None, max_length=256)
    dimension: FrontierDimension
    seed_fetch: SeedFetch
    addresses: FailureCharacter
    cost_estimate: int = Field(..., ge=0)
    depth: int = Field(default=0, ge=0)
    ttl_rounds: int = Field(default=DEFAULT_CANDIDATE_TTL_ROUNDS, ge=0)
    status: CandidateStatus = CandidateStatus.PENDING
    seed_source: str = Field(default="", max_length=256)
    seeded_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def _require_identity(self) -> FrontierCandidate:
        if not any([self.service, self.file_path, self.commit, self.span, self.config]):
            raise ValueError(
                "a candidate must name at least one of service/file/commit/span/config"
            )
        return self

    @property
    def dispatchable(self) -> bool:
        """True only for PENDING. P9 states are declared but never selected
        here; the loop owns their lifecycle."""
        return self.status == CandidateStatus.PENDING

    def admissible(self, max_depth: int = MAX_FRONTIER_DEPTH) -> bool:
        """Depth/TTL caps (DES §3): over-deep or expired candidates never
        order, even if scored."""
        return self.depth <= max_depth and self.ttl_rounds > 0

    @property
    def base_score(self) -> float:
        """Ordinal value density for best-first ranking: cheaper seeds rank
        higher at equal novelty. Ordinal per DES §1 — calibrated to
        cardinal later from fetch→outcome logs, never here."""
        return 1.0 / (1.0 + float(self.cost_estimate))


def _novelty(candidate: FrontierCandidate, collected: list[tuple[str | None, str | None]]) -> float:
    """One-minus-max-similarity against collected (file, span) evidence.

    Ordinal starter, deliberately coarse: same file and span is exhausted
    (0.0), same file is half-novel (0.5), unseen files are fully novel
    (1.0). Span-level overlap and content similarity are chunk-2 work.
    """
    best = 1.0
    for file_path, span in collected:
        if candidate.file_path is not None and candidate.file_path == file_path:
            if candidate.span is not None and candidate.span == span:
                return 0.0
            best = min(best, 0.5)
    return best


def order_candidates(
    candidates: list[FrontierCandidate],
    collected: list[tuple[str | None, str | None]],
    *,
    max_depth: int = MAX_FRONTIER_DEPTH,
) -> list[FrontierCandidate]:
    """Best-first ordering (DES §1): dispatchable and admissible only,
    ranked by base_score × novelty, ties broken by depth then identity so
    the order is deterministic across input orderings.

    Exhausted-by-capability candidates (score at/below the fixpoint floor)
    are dropped here — the exit signal treats an empty ordering as the
    fixpoint (DES §3).
    """
    scored: list[tuple[float, FrontierCandidate]] = []
    for candidate in candidates:
        if not candidate.dispatchable or not candidate.admissible(max_depth):
            continue
        score = candidate.base_score * _novelty(candidate, collected)
        if score <= FIXPOINT_SCORE_FLOOR:
            continue
        scored.append((score, candidate))
    scored.sort(key=lambda item: (-item[0], item[1].depth, item[1].candidate_id.hex))
    return [candidate for _, candidate in scored]


def seed_from_dormant_templates(
    paths_with_templates: list[tuple[SuspectPath, DormantTemplate]],
    character: FailureCharacter,
    evidence_state: EvidenceState,
    *,
    ttl_rounds: int = DEFAULT_CANDIDATE_TTL_ROUNDS,
) -> tuple[list[FrontierCandidate], list[str]]:
    """Seed the frontier from S2 dormant templates (DES §4: append-only).

    Takes (suspect path, template) pairs because templates carry site
    node IDs but not path identity — identity resolves against the path.
    Returns (candidates, refused_upstream_identities). Cross-boundary
    templates refuse at seed time — they terminate as named handoffs per
    ADR-BOUNDARY-001 and never become collection targets. LOCAL_COMPLETE
    seeds nothing: the loop observes the empty frontier and exits
    fixpoint. Over-budget templates still seed; the budget exit (not the
    seeder) stops the run, so pricing stays in one place.
    """
    dimension = character.spread_dimension(evidence_state)
    if dimension is None:
        return [], []
    seeded: list[FrontierCandidate] = []
    refused: list[str] = []
    now = datetime.now(UTC)
    for path, template in paths_with_templates or []:
        if template.handoff_candidate is not None:
            refused.append(template.handoff_candidate.upstream_identity)
            continue
        fetch = template.discriminating_fetch
        if fetch is None:
            continue
        sites_by_id = {site.node_id: site for site in path.sites}
        sites = [sites_by_id[nid] for nid in template.site_node_ids if nid in sites_by_id]
        if not sites:
            continue
        lines = sorted(site.line for site in sites)
        try:
            seeded.append(
                FrontierCandidate(
                    file_path=sites[0].file_path,
                    span=f"{lines[0]}-{lines[-1]}",
                    dimension=dimension,
                    seed_fetch=SeedFetch(fetch_kind=fetch.fetch_kind.value, target=fetch.target),
                    addresses=character,
                    cost_estimate=template.total_cost,
                    depth=0,
                    ttl_rounds=ttl_rounds,
                    seed_source=(f"dormant-template:{template.kind.value}/{template.template_id}"),
                    seeded_at=now,
                )
            )
        except ValueError:
            continue
    return seeded, refused


class FrontierExit(StrEnum):
    """The three exits, evaluated in precedence order (DES §3)."""

    CONTINUE = "CONTINUE"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    FIXPOINT = "FIXPOINT"
    DIMINISHING_RETURNS = "DIMINISHING_RETURNS"
    CONFIDENCE_CORROBORATED = "CONFIDENCE_CORROBORATED"


def evaluate_exit(
    *,
    spread_budget_remaining: int,
    ordered_pending: list[FrontierCandidate],
    top_gain_history: list[float],
    independent_artifact_types: int,
    posterior: float,
    min_gain: float = 0.01,
    patience_k: int = 3,
    posterior_threshold: float = 0.9,
) -> FrontierExit:
    """Pure three-exit stopping rule (DES §3).

    Thresholds are explicit parameters with uncalibrated chunk-1 defaults;
    battery calibration moves the defaults (with a version bump), never
    the precedence: budget first, then fixpoint, then diminishing
    returns, then corroborated confidence.
    """
    if spread_budget_remaining <= 0:
        return FrontierExit.BUDGET_EXHAUSTED
    if not ordered_pending:
        return FrontierExit.FIXPOINT
    recent = list(top_gain_history or [])[-patience_k:]
    if len(recent) >= patience_k and all(gain < min_gain for gain in recent):
        return FrontierExit.DIMINISHING_RETURNS
    if posterior >= posterior_threshold and independent_artifact_types >= 2:
        return FrontierExit.CONFIDENCE_CORROBORATED
    return FrontierExit.CONTINUE
