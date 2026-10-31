# src/investigation_agent_platform/application/investigation/verification.py
import re
from collections.abc import Callable
from datetime import UTC, datetime
from enum import Enum
from typing import TYPE_CHECKING, Any, Literal, cast
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from investigation_agent_platform.application.evidence.retrieval_ledger import (
    RETRYABLE_ACTIONS,
    WP_A3_DEFAULT_MAX_ATTEMPTS,
    ClaimRetrievalBinding,
    ClaimRetrievalLedger,
    RetrievalOutcome,
    _distinct_typed_claims,
    _normalize_scope,
    build_claim_ledger,
    claim_key,
    claim_ref,
    count_persisted_attempts,
    extract_search_query,
    linked_scope_attempted,
    re_retrieval_directive,
    target_group_key,
)
from investigation_agent_platform.application.evidence.retrieval_ledger import (
    all_typed_claims_not_checkable as _all_typed_claims_not_checkable,
)
from investigation_agent_platform.domain.code.models import (
    CODE_FACT_RESOLUTION_V2_KEY,
    CODE_FACT_RESOLUTIONS_KEY,
    WINDOW_ANALYSIS_KEY,
    CodeFactResolution,
    CodeFactResolutionV2,
    CodeFactStatus,
    CodeValueSemantics,
    JavaFactCategory,
    detect_dual_resolution_conflict,
)
from investigation_agent_platform.domain.evidence.models import Evidence
from investigation_agent_platform.domain.hypothesis.models import (
    ClaimKind,
    ClaimTarget,
    Hypothesis,
    HypothesisStatus,
)
from investigation_agent_platform.infrastructure.observability.tracing import traced

if TYPE_CHECKING:
    from investigation_agent_platform.domain.finding.models import EliminatedRival


def _record_gate_note(observer: Callable[[dict], None] | None, note: dict[str, Any]) -> None:
    """D3 Phase 0 mirror tap: record-only, never feeds back (package §1.2).

    The observer appends a note to the activity's per-execution list.
    Any observer failure is swallowed: a shadow-recording fault must
    never alter the native verdict, branch, or exception path.
    """
    if observer is None:
        return
    try:
        observer(dict(note))
    except Exception:
        pass


class ContradictionSeverity(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class Contradiction(BaseModel):
    contradiction_id: UUID = Field(default_factory=uuid4)
    statement: str
    evidence_ids: list[UUID] = Field(default_factory=list)
    hypothesis_ids: list[UUID] = Field(default_factory=list)
    severity: ContradictionSeverity
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    # F-052: durable contradiction lifecycle — every contradiction carries its
    # resolution state plus who resolved it, why, and when. Unresolved
    # HIGH/CRITICAL contradictions block conclusion via ConclusionGate.
    resolution_status: str = Field(default="UNRESOLVED")
    resolved_by: str | None = None
    resolution_rationale: str | None = None
    resolved_at: datetime | None = None
    superseded_by: UUID | None = None

    def resolve(self, resolved_by: str, rationale: str) -> "Contradiction":
        if self.resolution_status not in ("UNRESOLVED", "INVESTIGATING"):
            raise ValueError(
                f"Contradiction {self.contradiction_id} is already {self.resolution_status}"
            )
        return self.model_copy(
            update={
                "resolution_status": "RESOLVED",
                "resolved_by": resolved_by,
                "resolution_rationale": rationale,
                "resolved_at": datetime.now(UTC),
            }
        )

    def accept_risk(self, resolved_by: str, rationale: str) -> "Contradiction":
        """Explicit risk acceptance — recorded, attributed, and auditable."""
        if self.resolution_status not in ("UNRESOLVED", "INVESTIGATING"):
            raise ValueError(
                f"Contradiction {self.contradiction_id} is already {self.resolution_status}"
            )
        return self.model_copy(
            update={
                "resolution_status": "ACCEPTED_RISK",
                "resolved_by": resolved_by,
                "resolution_rationale": rationale,
                "resolved_at": datetime.now(UTC),
            }
        )


class VerificationStatus(str, Enum):
    VERIFIED = "VERIFIED"
    SUPPORTED = "SUPPORTED"
    CONTRADICTED = "CONTRADICTED"
    INCONCLUSIVE = "INCONCLUSIVE"


# P0-3: deterministic candidate ranking — verification must evaluate the
# strongest candidate, never whatever row order the database returned.
_STATUS_RANK = {
    "VERIFIED": 0,
    "SUPPORTED": 1,
    "UNDER_INVESTIGATION": 2,
    "PROPOSED": 3,
    "CONTRADICTED": 4,
    "REJECTED": 5,
}


@traced("reason", require_scope=False)
def rank_hypotheses(hypotheses: list[Hypothesis]) -> list[Hypothesis]:
    """Order candidates by lifecycle status, then support/confidence, then age."""

    def _key(h: Hypothesis) -> tuple[int, float, float, str, str]:
        status = str(getattr(getattr(h, "status", ""), "value", getattr(h, "status", "")))
        return (
            _STATUS_RANK.get(status, 9),
            -(float(getattr(h, "support_score", 0.5) or 0.0)),
            -(float(getattr(h, "confidence_score", 0.5) or 0.0)),
            str(getattr(h, "created_at", "")),
            str(getattr(h, "id", "")),
        )

    return sorted(hypotheses, key=_key)


def reconcile_contradictions(contradictions: list[Any], hypotheses: list[Any]) -> list[Any]:
    """Contradictions to re-save as RESOLVED. Pure; never raises.

    H11b-D2: a deterministic, attributed resolution with no human in the
    loop and no fake attribution. An UNRESOLVED contradiction linked only
    to REJECTED hypotheses is reconciled by ``system:contradiction-reconciler``.
    ``accept_risk`` keeps its human caller (no automatic callers anywhere).
    """
    try:
        status_by_id = {str(getattr(h, "id", "")): str(getattr(getattr(h, "status", ""), "value", getattr(h, "status", "")) or "") for h in (hypotheses or [])}
    except Exception:
        return []
    out: list[Any] = []
    try:
        for c in contradictions or []:
            try:
                if getattr(c, "resolution_status", "") != "UNRESOLVED" or not getattr(c, "hypothesis_ids", None):
                    continue
                linked = [str(h) for h in (getattr(c, "hypothesis_ids", None) or [])]
                if linked and all(status_by_id.get(hid) in ("REJECTED",) for hid in linked):
                    out.append(c.resolve("system:contradiction-reconciler", "every hypothesis it linked was rejected"))
            except Exception:
                continue
    except Exception:
        return []
    return out


def select_verifiable_candidate(
    ranked: list,
    evidence_by_id: dict,
    exclude_statuses: tuple = ("REJECTED", "CONTRADICTED"),
    prefer_id: Any | None = None,
) -> Any | None:
    """First eligible candidate whose citations resolve, else first eligible.

    G9: reprocess copies hypotheses with stale supporting IDs (cited rows
    purged); verifying the stale copy first concludes INCONCLUSIVE while a
    verifiable twin waits. Prefer candidates with >=1 supporting ID
    present in evidence_by_id; fall back to the head eligible so the
    honest-abstention path is unchanged when nothing resolves. H17c: when
    ``prefer_id`` names an eligible candidate (the one the last verify turn
    judged), return it first so verify and conclude do not pick different
    candidates. Pure and deterministic. ID comparison tolerates str/UUID
    mixing (same coercion as the activities' _resolve).
    """
    try:
        eligible = [
            h
            for h in (ranked or [])
            if str(getattr(getattr(h, "status", ""), "value", getattr(h, "status", "")))
            not in exclude_statuses
        ]
    except Exception:
        return None
    if prefer_id:
        try:
            for candidate in eligible:
                if str(getattr(candidate, "id", "")) == str(prefer_id):
                    return candidate
        except Exception:
            pass
    for candidate in eligible:
        try:
            raw_ids = getattr(candidate, "supporting_evidence_ids", None) or []
        except Exception:
            continue
        for raw in raw_ids:
            try:
                key = raw if not isinstance(raw, str) else UUID(raw)
            except (ValueError, TypeError, AttributeError):
                continue
            if key in (evidence_by_id or {}):
                return _prefer_discriminating(candidate, eligible, evidence_by_id)
    return eligible[0] if eligible else None


def apply_verification_outcome(candidate: Hypothesis, result: "VerificationResult") -> Hypothesis:
    """Persist a verification verdict back onto the candidate.

    Phase 1 determinism item: `rank_hypotheses` sorts on `support_score` /
    `confidence_score`, but nothing ever wrote them — every row kept the 0.5
    defaults and ranking degraded to status-then-oldest. This helper writes
    what the verifier computed so the next ranking is over maintained fields:
    corroborating IDs union into `supporting_evidence_ids`, parsed
    `against_evidence` into `refuting_evidence_ids`, `support_score` from the
    coverage score, `confidence_score` from the overall confidence. Lifecycle
    advances with valid-transition guards (a PROPOSED row reaches
    CONTRADICTED via UNDER_INVESTIGATION; an invalid edge leaves the status
    untouched). Pure and deterministic; never raises.
    """
    updated = candidate
    try:
        conf = result.confidence
        support = max(0.0, min(1.0, float(conf.coverage_score)))
        confidence = max(0.0, min(1.0, float(conf.overall_confidence)))
    except Exception:
        return candidate
    try:
        merged_support = list(getattr(updated, "supporting_evidence_ids", []) or [])
        for eid in result.corroborating_evidence_ids or []:
            if eid not in merged_support:
                merged_support.append(eid)
        merged_support = merged_support[:200]
        merged_refuting = list(getattr(updated, "refuting_evidence_ids", []) or [])
        for raw in result.against_evidence or []:
            try:
                parsed_id = raw if isinstance(raw, UUID) else UUID(str(raw))
            except (ValueError, TypeError, AttributeError):
                continue
            if parsed_id not in merged_refuting:
                merged_refuting.append(parsed_id)
        merged_refuting = merged_refuting[:200]
        if (
            merged_support != list(getattr(updated, "supporting_evidence_ids", []) or [])
            or merged_refuting != list(getattr(updated, "refuting_evidence_ids", []) or [])
            or float(getattr(updated, "support_score", 0.5) or 0.0) != support
            or float(getattr(updated, "confidence_score", 0.5) or 0.0) != confidence
        ):
            updated = updated.model_copy(
                update={
                    "supporting_evidence_ids": merged_support,
                    "refuting_evidence_ids": merged_refuting,
                    "support_score": support,
                    "confidence_score": confidence,
                }
            )
    except Exception:
        return candidate
    verified = str(getattr(result.status, "value", result.status)) == "VERIFIED"
    contradicted = str(getattr(result.status, "value", result.status)) == "CONTRADICTED"
    try:
        current = updated.status
        # NOTE: PROPOSED→SUPPORTED is not a valid direct edge in the
        # lifecycle graph — both promotion paths chain through
        # UNDER_INVESTIGATION. (The pre-helper inline block attempted the
        # direct edge inside a blanket except and silently never fired.)
        if verified and current in (
            HypothesisStatus.PROPOSED,
            HypothesisStatus.UNDER_INVESTIGATION,
        ):
            if current == HypothesisStatus.PROPOSED:
                updated = updated.transition_status(HypothesisStatus.UNDER_INVESTIGATION)
            updated = updated.transition_status(HypothesisStatus.SUPPORTED)
        elif contradicted and current in (
            HypothesisStatus.PROPOSED,
            HypothesisStatus.UNDER_INVESTIGATION,
            HypothesisStatus.SUPPORTED,
        ):
            if current == HypothesisStatus.PROPOSED:
                updated = updated.transition_status(HypothesisStatus.UNDER_INVESTIGATION)
            updated = updated.transition_status(HypothesisStatus.CONTRADICTED)
        elif not verified and not contradicted and current == HypothesisStatus.PROPOSED:
            updated = updated.transition_status(HypothesisStatus.UNDER_INVESTIGATION)
    except ValueError:
        pass
    return updated


def _prefer_discriminating(first_match: Any, eligible: list, evidence_by_id: dict) -> Any:
    """Prefer the resolvable candidate with the most unique support (E2).

    Counts supporting IDs (resolvable) that no other eligible candidate
    cites — discriminating evidence. Stable: ties keep rank order, and a
    zero-unique field never loses to absence (falls back to first_match).
    Pure and deterministic.
    """
    try:
        resolved: list[tuple[Any, set]] = []
        for candidate in eligible:
            cited: set = set()
            try:
                for raw in getattr(candidate, "supporting_evidence_ids", None) or []:
                    try:
                        key = raw if not isinstance(raw, str) else UUID(raw)
                    except (ValueError, TypeError, AttributeError):
                        continue
                    if key in (evidence_by_id or {}):
                        cited.add(key)
            except Exception:
                pass
            if cited:
                resolved.append((candidate, cited))
        if not resolved:
            return first_match
        counts: dict[int, int] = {}
        for index, (_, cited) in enumerate(resolved):
            others: set = set()
            for other_index, (_, other_cited) in enumerate(resolved):
                if other_index != index:
                    others |= other_cited
            counts[index] = len(cited - others)
        best = max(counts.values())
        if best <= 0:
            return first_match
        for index, (_, _) in enumerate(resolved):
            if counts[index] == best:
                return resolved[index][0]
        return first_match
    except Exception:
        return first_match


class CausalRelationship(BaseModel):
    cause: str = Field(..., description="Root trigger event or condition")
    effect: str = Field(..., description="Observed symptom or failure")
    evidence_ids: list[UUID] = Field(default_factory=list)
    mechanism: str = Field(..., description="Explicit step-by-step mechanism")
    confidence: float = Field(..., ge=0.0, le=1.0)


class InvestigationConfidence(BaseModel):
    coverage_score: float = Field(..., ge=0.0, le=1.0)
    reliability_score: float = Field(..., ge=0.0, le=1.0)
    causal_score: float = Field(..., ge=0.0, le=1.0)
    contradiction_penalty: float = Field(..., ge=0.0, le=1.0)

    @property
    def overall_confidence(self) -> float:
        score = (
            0.35 * self.coverage_score
            + 0.25 * self.reliability_score
            + 0.20 * self.causal_score
            + 0.20 * (1.0 - self.contradiction_penalty)
        )
        return max(0.0, min(1.0, score))


_BAND_EPSILON = 1e-9


def band_at_least(score: float, floor: float, *, tolerant: bool) -> bool:
    """Whether ``score`` meets ``floor``. Tolerant (LV_H) absorbs float rounding."""
    return (score + _BAND_EPSILON >= floor) if tolerant else (score >= floor)


class RootCauseVerificationPolicy(BaseModel):
    min_confidence_threshold: float = Field(default=0.80, ge=0.0, le=1.0)
    require_cross_layer_evidence: bool = Field(default=True)
    max_allowed_unresolved_contradictions: int = Field(default=0, ge=0)
    # H11 (lv5): when True the general path counts only unresolved HIGH/CRITICAL
    # contradictions against the limit, as ConclusionGate and the specialized
    # validators already do. Model-reported contradictions are persisted as
    # MEDIUM with no evidence link and nothing resolves them, so counting them
    # let one stray prose contradiction force CONTRADICTED. The activities set
    # this from the recorded loop version; the default keeps the old counting.
    blocking_contradictions_only: bool = Field(default=False)
    require_causal_relationship: bool = Field(default=True)
    # D5: publish band. [likely_floor, established_floor) concludes
    # ROOT_CAUSE_LIKELY with best-reason/against-evidence; at or above
    # established_floor concludes ROOT_CAUSE_ESTABLISHED. Below likely
    # floor abstains. Policy-owned (not magic constants) so runtime and
    # code profiles can tune the band independently.
    likely_floor: float = Field(default=0.80, ge=0.0, le=1.0)
    established_floor: float = Field(default=0.90, ge=0.0, le=1.0)
    # H15 (LV_H): compare scores to the band floors with a 1e-9 tolerance and require a
    # causal step for a CAUSAL claim. Set from the recorded pin by _policy_for_loop_version
    # in the verify and conclude activities; the defaults keep the old behavior.
    tolerant_band: bool = Field(default=False)
    require_causal_step_for_causal_claims: bool = Field(default=False)
    # H15 (LV_H, owner decision): one step is likely by design, not by accident.
    established_requires_steps: int = Field(default=2, ge=0)
    # H17b (LV_H): check claim groups against each other after the per-group
    # conjunction. Blocking conflicts withdraw; mixed revisions demote to likely.
    cross_claim_checks: bool = Field(default=False)

    @classmethod
    def runtime(cls) -> "RootCauseVerificationPolicy":
        """Policy preset for runtime failure claims (D5).

        Same 0.80 band floor: trace rows + log-group rows + owning-service
        topology attribution satisfy cross-layer as distinct providers and
        types. Returned as an explicit preset (not inline construction) so
        runtime tuning has one home.
        """
        return cls(
            min_confidence_threshold=0.80,
            require_cross_layer_evidence=True,
            max_allowed_unresolved_contradictions=0,
            require_causal_relationship=True,
            likely_floor=0.80,
            established_floor=0.90,
        )


class LimitationCode(str, Enum):
    """Typed caveats on a VERIFIED result (Phase 2.3).

    RIVAL_UNTESTED: a sibling hypothesis was never evaluated against
        evidence — the winner was not contrasted, so it publishes as
        likely, never established.
    SINGLE_PROVIDER: every corroborating row comes from one provider —
        no independent corroboration.
    CONCLUSION_UNMAPPED: a material sentence of the published conclusion
        maps to no cited claim or evidence row (Phase 2.4).
    RELATION_UNDEMONSTRATED: a causal step is type-grounded (it cites code
        or runtime rows) but no single cited row shows the asserted
        relation — the mechanism is supported, not demonstrated, so the
        claim publishes as likely, never established.
    NO_RUNTIME_OBSERVATION: a behavior claim has its code path and a
        tested rival but no runtime observation — routine in code-only
        scopes. The mechanism is code-plausible, not observed: likely,
        never established.
    """

    RIVAL_UNTESTED = "RIVAL_UNTESTED"
    SINGLE_PROVIDER = "SINGLE_PROVIDER"
    CONCLUSION_UNMAPPED = "CONCLUSION_UNMAPPED"
    RELATION_UNDEMONSTRATED = "RELATION_UNDEMONSTRATED"
    NO_RUNTIME_OBSERVATION = "NO_RUNTIME_OBSERVATION"
    # Phase 2a: part of the winner's corroborating rows also supports a live
    # rival — the same rows establish two stories, so confidence pays a
    # rival-shared penalty and the conclusion publishes as likely at best.
    RIVAL_SHARED_EVIDENCE = "RIVAL_SHARED_EVIDENCE"
    # H17b: cross-claim conflict inside one hypothesis.
    CLAIM_CONFLICT = "CLAIM_CONFLICT"
    # H17b: claims pinned to different revisions (non-blocking; demotes to likely).
    REVISION_MIXED = "REVISION_MIXED"
    # Phase 1 (claim binding): a claim verified only on bank-fallback rows
    # with no claim-local retrieval — not proof of claim-local retrieval.
    UNMAPPED_CITATION = "UNMAPPED_CITATION"
    # Phase 1 (claim binding): some cited IDs resolved to no bound row;
    # the evaporation is named here instead of silently dropped.
    EVAPORATED_CITATION = "EVAPORATED_CITATION"
    # Phase 3 (claim binding): a cited row was rebound to a same-file,
    # same-revision donor that grounds the claim; the verdict rests on
    # the donor, not on the cited row as written.
    REPAIRED_CITATION = "REPAIRED_CITATION"
    # Phase 4 (claim binding): an opinion-only claim never verifies —
    # taste/preference/value judgments and unfalsifiable claims are
    # skipped, never grounded. Additive member, same frozen-enum
    # discipline as REPAIRED_CITATION.
    NOT_CHECKABLE_SKIPPED = "NOT_CHECKABLE_SKIPPED"
    # Claim-proof gaps (D1): a negative recent-change claim verified by a
    # scoped empty history search. Additive member, same frozen-enum
    # discipline as REPAIRED_CITATION.
    HISTORY_ABSENCE_PROVED = "HISTORY_ABSENCE_PROVED"
    # P3 (S5/S6 rival bar): the winner has no ruled-out rival with
    # ruling-out evidence — the causal verdict publishes as likely, never
    # established. Additive member, same frozen-enum discipline.
    RIVAL_UNBEATEN = "RIVAL_UNBEATEN"
    # Review H3: a verdict published on one claim group while another
    # checkable (kind, target) group behind the same hypothesis did not
    # verify over the rows it cites. The verdict is withdrawn (INCONCLUSIVE).
    UNVERIFIED_CLAIM_GROUP = "UNVERIFIED_CLAIM_GROUP"
    # Review H4: NOT_CHECKABLE claims behind a verified hypothesis were
    # excluded from verification. They ship as this limitation and are never
    # part of what the verdict certifies.
    NOT_CHECKABLE_EXCLUDED = "NOT_CHECKABLE_EXCLUDED"


class VerificationLimitation(BaseModel):
    code: LimitationCode
    detail: str = Field(default="", max_length=500)


class DiagnosticReasonCode(str, Enum):
    """Stable reason-code vocabulary for WP-A2 verification diagnostics.

    Values are frozen API: never rename or remove a code, only append new
    ones. Persisted diagnostics and assessments store the string value so
    old rows stay readable when new codes are added.
    """

    CITED_ID_EVAPORATED = "CITED_ID_EVAPORATED"
    TIER_DEMOTED = "TIER_DEMOTED"
    OBLIGATION_MISSING = "OBLIGATION_MISSING"


class EvaporatedId(BaseModel):
    """A cited evidence ID that resolves to no bound evidence row.

    The citation is preserved here instead of being silently dropped, so
    per-claim retrieved/bound IDs stay auditable (I2: no silent
    evaporation). Diagnostics-only: never affects the verdict.
    """

    cited_id: str = Field(..., max_length=64)
    cited_by: str = Field(default="", max_length=64)
    reason_code: DiagnosticReasonCode = DiagnosticReasonCode.CITED_ID_EVAPORATED


class Demotion(BaseModel):
    """An established-to-likely tier demotion with its causes.

    ``cause_codes`` holds the LimitationCode values that forced the
    demotion (empty when the heuristic score alone fell below the
    established floor). Diagnostics-only: recorded, never re-decided.
    """

    hypothesis_id: UUID
    from_tier: str = Field(default="established", max_length=16)
    to_tier: str = Field(default="likely", max_length=16)
    reason_code: DiagnosticReasonCode = DiagnosticReasonCode.TIER_DEMOTED
    cause_codes: list[str] = Field(default_factory=list, max_length=10)
    detail: str = Field(default="", max_length=500)


class MissingObligation(BaseModel):
    """A claim that formed no dischargeable obligation.

    ``claim`` is the hypothesis statement (truncated), ``claim_kind`` the
    effective kind value when typed, and ``next_action`` the single
    re-verify directive that would discharge it. Diagnostics-only:
    ambiguous claims stay exactly as evaluated.
    """

    claim: str = Field(..., max_length=1024)
    claim_kind: str | None = Field(default=None, max_length=32)
    reason_code: DiagnosticReasonCode = DiagnosticReasonCode.OBLIGATION_MISSING
    next_action: str | None = Field(default=None, max_length=1024)


class RepairedBinding(BaseModel):
    """Phase 3 (claim binding): one citation rebound to a donor row.

    Output-only diagnostics. ``basis``, ``outcome``, and
    ``binding_version`` are Literal-typed: out-of-vocabulary
    construction raises ValidationError.
    """

    model_config = ConfigDict(frozen=True)

    claim_ref: str = Field(description="16-hex claim reference (never the full key).")
    claim_key: str = Field(
        default="",
        max_length=560,
        description="Full human-readable claim identity (statement|kind).",
    )
    target_ref: str = Field(
        default="",
        max_length=32,
        description="Target-distinctness fingerprint pairing with claim_key "
        "to join the exact (kind, target) group. Join key only.",
    )
    target: dict[str, Any] = Field(
        default_factory=dict,
        description="The group's normalized target mapping (symbol, "
        "file_path, line range, commit_id/commit_subject, ...).",
    )
    cited_id: str = Field(description="The misattributed cited evidence ID.")
    donor_id: str = Field(description="The banked donor row that grounds the claim.")
    basis: Literal["snippet_containment", "overlap_and_containment"] = Field(
        description="How the donor matched the candidate (overlap alone never matches)."
    )
    outcome: Literal["applied", "shadow"] = Field(
        description='"applied" (cbv3, verdict rests on the donor) or '
        '"shadow" (cbv2, recorded only, verdict unchanged).',
    )
    binding_version: Literal["cbv2", "cbv3"] = Field(
        description="Pin that produced this record (redundant with outcome "
        "by design: shadow<->cbv2, applied<->cbv3)."
    )
    gate_reason: str = Field(
        default="",
        max_length=512,
        description="The kind-gate (False, reason) that authorized this "
        "attempt. Review input, never a decision input.",
    )
    donor_snippet: str = Field(
        default="",
        max_length=2000,
        description="Truncated donor row content_snippet at repair time. "
        "Review rendering only (finding G): the reviewer judges match "
        "quality from the snippet, never a bare ID. Never a decision input.",
    )


class VerificationDiagnostics(BaseModel):
    """Diagnostics-only annotations attached to a VerificationResult.

    Never alters status, tier, explanation, confidence, or corroborating
    IDs. ``schema_version`` versions the diagnostics payload itself;
    ``reason_codes`` is the sorted unique snapshot of codes present.
    """

    schema_version: str = Field(default="v1", max_length=16)
    evaporated_ids: list[EvaporatedId] = Field(default_factory=list, max_length=100)
    demotions: list[Demotion] = Field(default_factory=list, max_length=10)
    missing_obligations: list[MissingObligation] = Field(default_factory=list, max_length=10)
    reason_codes: list[str] = Field(default_factory=list, max_length=20)
    # G3: per-distinct-claim retrieval partitions (diagnostics-only, default
    # empty — legacy rows validate unchanged). Never alters verdicts.
    claim_bindings: list[ClaimRetrievalBinding] = Field(default_factory=list, max_length=100)
    # G3: the ledgers behind claim_bindings, paired by claim_key (same
    # order, same cap). Persisted so the benefit battery measures changed
    # bindings from recorded state instead of recomputing the join.
    claim_ledgers: list[ClaimRetrievalLedger] = Field(default_factory=list, max_length=100)
    # Phase 3: per-group misattribution repairs (applied or shadow).
    # Additive, default empty — legacy rows validate unchanged.
    # Keep the full in-memory repair set until marker serialization applies
    # its explicit review cap and truncation flag. A pre-marker cap would
    # make omitted repairs indistinguishable from a complete marker.
    repaired: list[RepairedBinding] = Field(default_factory=list)


class VerificationResult(BaseModel):
    hypothesis_id: UUID
    status: VerificationStatus
    corroborating_evidence_ids: list[UUID] = Field(default_factory=list)
    explanation: str
    confidence: InvestigationConfidence
    # Phase 2.3: typed caveats. The explanation names these codes — never
    # a bare item count — so a reader knows exactly what "likely" means.
    limitations: list[VerificationLimitation] = Field(default_factory=list)
    # G1: location-answer validation bypasses causal verification; the gate
    # and conclude paths branch on this flag (never on status text).
    location_validated: bool = False
    # T1-1: doc-answer validation parallel to location_validated.
    doc_validated: bool = False
    # #65: state-answer validation parallel to doc_validated (banked
    # DATABASE_STATE rows answer value questions without causal machinery).
    state_validated: bool = False
    code_value_validated: bool = False
    # D5: publish tier for VERIFIED results — "established" (at/above the
    # established floor) or "likely" (in-band). Conclude maps likely to
    # ROOT_CAUSE_LIKELY with best-reason/against-evidence attached.
    tier: str = "established"
    # D5: best supporting reason (highest-confidence chain/link or top
    # corroborating summary) and citable against-evidence (bound refuting
    # IDs as strings + unresolved non-blocking contradiction statements).
    best_reason: str = ""
    against_evidence: list[str] = Field(default_factory=list)
    # WP-A2: diagnostics-only annotations (evaporated IDs, demotions,
    # missing obligations). None = clean evaluation or flag off. Never
    # influences verdicts, gates, or conclusions.
    diagnostics: "VerificationDiagnostics | None" = None


# WP-A2: stable per-kind re-verify directives for missing obligations.
# Wording mirrors next_check_for_abstention (unblock-neutral, never
# "promote"); the mapping is frozen alongside DiagnosticReasonCode.
_MISSING_OBLIGATION_DIRECTIVES = {
    "DEFINITION": "Read the file (range read) showing the defined symbol, then re-verify.",
    "CALL_EDGE": "Bind the range-read row showing the call expression, then re-verify.",
    "BEHAVIOR": "Observe the behavior at runtime (log, trace, state, or probe), then re-verify.",
    "ABSENCE": "Narrow or re-scope the empty-search proof to the claimed scope, then re-verify.",
    "HISTORY": "Bind the commit-history row matching the claimed file and commit, then re-verify.",
    "CAUSAL": "Bind supporting evidence to each causal step, then re-verify.",
}
_DEFAULT_MISSING_OBLIGATION_DIRECTIVE = "Bind supporting evidence to the claim, then re-verify."

# Phase 4 (claim binding): kinds eligible for smallest-fetch forcing.
_SMALLEST_FETCH_ORDER = ("DEFINITION", "HISTORY", "BEHAVIOR", "ABSENCE", "CAUSAL")


def build_claim_gaps(
    hypothesis: Any,
    cited_group_ids: set[str] | None,
    failed_cited_group_ids: set[str] | None = None,
    include_no_target: bool = False,
) -> dict[str, Any]:
    """Machine-readable smallest-fetch obligations for one hypothesis (pure).

    Returns {"gaps": [...], "not_checkable_groups": ["KIND|target_key", ...]}.

    A gap names one typed group (kind, target) with NO citation in this
    hypothesis: openness is membership in ``cited_group_ids`` (the
    enforcement's ``cited_groups``, "KIND|target_key" strings) — uncited
    groups have no kept rows by construction, so no kept join exists or
    is needed. Cited groups are repair/obligation territory even when
    their gate failed, never smallest-fetch - except (review H8, lv3) a cited
    group named in ``failed_cited_group_ids``: it failed its own gate over the
    rows it cites, which left it no route to a better fetch, so it becomes a
    gap tagged ``reason="cited_gate_failed"``. NOT_CHECKABLE groups never
    appear as gaps; they are inventoried into not_checkable_groups.
    Kinds outside KIND_FETCH (CALL_EDGE, UNTYPED, NO_TARGET/empty
    targets) never appear. Identity is the group identity
    (kind + target_key); claim_ref is informational only. Import failure
    returns an "error"-bearing envelope (never a silent empty feed).
    Sorted in SMALLEST_FETCH evaluation order. Never raises.
    """
    try:
        order = {k: i for i, k in enumerate(_SMALLEST_FETCH_ORDER)}
        cited = set(cited_group_ids or [])
        failed_cited = set(failed_cited_group_ids or [])
        gaps: list[dict[str, Any]] = []
        inventoried: list[str] = []
        try:
            groups = [
                g for g in list(_distinct_typed_claims(hypothesis) or []) if isinstance(g, dict)
            ]
        except Exception:
            return {
                "gaps": [],
                "not_checkable_groups": [],
                "error": "ledger_import_failed",
            }
        for group in groups:
            try:
                kind_value = str(group.get("kind_value") or "")
                _raw_target = group.get("target_dict")
                target: dict[str, Any] = _raw_target if isinstance(_raw_target, dict) else {}
                ref = ""
                try:
                    key = claim_key(str(group.get("statement") or ""), kind_value)
                    ref = claim_ref(key)
                except Exception:
                    ref = ""
                tkey = ""
                try:
                    tkey, _ = target_group_key(target)
                except Exception:
                    tkey = ""
                no_target = tkey == "NO_TARGET"
                if not ref or not tkey or (
                    no_target
                    and not (
                        include_no_target
                        and kind_value in ("BEHAVIOR", "CAUSAL")
                    )
                ):
                    continue
                base = str(kind_value) + "|" + tkey
                # H8b: a targetless gap is keyed by claim reference so two
                # hypotheses (or two claims) never share one gap id.
                gid = f"{base}|{ref}" if no_target else base
                if kind_value == ClaimKind.NOT_CHECKABLE.value:
                    inventoried.append(gid)
                    continue
                if kind_value not in order:
                    continue
                refetch = base in cited and base in failed_cited
                if base in cited and not refetch:
                    continue
                gap: dict[str, Any] = {
                    "gap_id": gid,
                    "claim_ref": ref,
                    "kind": kind_value,
                    "target_key": tkey,
                    "target": dict(target),
                    "query": str(group.get("stored_query") or ""),
                    "scope": str(group.get("stored_scope") or ""),
                }
                if refetch:
                    gap["reason"] = "cited_gate_failed"
                if no_target:
                    gap["no_target"] = True
                    if not gap["query"]:
                        from investigation_agent_platform.application.evidence.retrieval_ledger import (
                            derive_claim_query,
                        )

                        try:
                            gap["query"] = derive_claim_query(
                                str(group.get("statement") or ""), kind_value, target
                            )[0]
                        except Exception:
                            pass
                gaps.append(gap)
            except Exception:
                continue
        try:
            gaps.sort(
                key=lambda g: (
                    order.get(str(g.get("kind") or ""), 9),
                    str(g.get("target_key") or ""),
                )
            )
        except Exception:
            pass
        return {"gaps": gaps, "not_checkable_groups": sorted(set(inventoried))}
    except Exception:
        return {
            "gaps": [],
            "not_checkable_groups": [],
            "error": "ledger_import_failed",
        }


def collect_verification_diagnostics(
    hypothesis: Any,
    evidence_items: list[Any],
    result: "VerificationResult",
    recorded_attempts: list[tuple[list[str], dict[str, Any] | None]] | None = None,
) -> "VerificationDiagnostics | None":
    """Build diagnostics-only annotations for an evaluated hypothesis.

    Pure function of (hypothesis citations, bound evidence, result): it
    reads IDs and verdict fields but never mutates them. Returns None
    when there is nothing to report. Fail-open by contract — callers
    must additionally guard with try/except so persistence never blocks
    on diagnostics.

    ``recorded_attempts`` is the timeline-backed (banked IDs, attempt
    dict) list, oldest first; ``None`` (legacy callers) degrades claim
    bindings to ``unmapped`` without failing.
    """
    try:
        bound: set[str] = set()
        for item in evidence_items or []:
            try:
                key = str(getattr(item, "evidence_id", "") or "").strip()
            except Exception:
                continue
            if key:
                bound.add(key)
        cited: list[str] = []
        seen: set[str] = set()

        def _cite(raw: Any) -> None:
            try:
                key = str(raw or "").strip()
            except Exception:
                return
            if key and key not in seen:
                seen.add(key)
                cited.append(key)

        try:
            for raw in getattr(hypothesis, "supporting_evidence_ids", []) or []:
                _cite(raw)
            for raw in getattr(hypothesis, "refuting_evidence_ids", []) or []:
                _cite(raw)
            for asm in getattr(hypothesis, "assessments", []) or []:
                _cite(getattr(asm, "evidence_id", None))
            for step in getattr(hypothesis, "causal_steps", []) or []:
                for raw in getattr(step, "evidence_ids", []) or []:
                    _cite(raw)
        except Exception:
            pass
        # G3: one retrieval binding per distinct typed claim. Legacy rows
        # (recorded_attempts None) degrade to unmapped, never fail.
        recorded: list[tuple[list[str], dict[str, Any] | None]] = list(recorded_attempts or [])
        claim_bindings: list[ClaimRetrievalBinding] = []
        claim_ledgers: list[ClaimRetrievalLedger] = []
        unknown_to_ref: dict[str, str] = {}
        try:
            try:
                statement = str(getattr(hypothesis, "statement", "") or "")
            except Exception:
                statement = ""
            for group in _distinct_typed_claims(hypothesis):
                try:
                    kind_value = group.get("kind_value")
                    target_dict = group.get("target_dict")
                    group_cited = group.get("cited_ids")
                    ledger, binding, full_unknown = build_claim_ledger(
                        statement=str(group.get("statement", "") or statement),
                        claim_kind=str(kind_value) if kind_value else None,
                        claim_target=dict(target_dict) if isinstance(target_dict, dict) else None,
                        cited_ids=list(group_cited) if isinstance(group_cited, list) else [],
                        bank_ids=set(bound),
                        attempts=recorded,
                        target_ref=str(group.get("target_key") or "") or None,
                        stored_query=str(group.get("stored_query") or "") or None,
                        stored_scope=str(group.get("stored_scope") or "") or None,
                    )
                except Exception:
                    continue
                if _claim_bounded_search(binding, ledger, recorded):
                    try:
                        binding = binding.model_copy(update={"bounded_search": True})
                    except Exception:
                        pass
                claim_bindings.append(binding)
                try:
                    claim_ledgers.append(ledger)
                except Exception:
                    pass
                # Uncapped mapping: IDs omitted from the 50-ID binding
                # limit still resolve to the claim ref in evaporation.
                for unknown_id in full_unknown:
                    unknown_to_ref.setdefault(str(unknown_id), binding.claim_ref)
        except Exception:
            claim_bindings = []
            claim_ledgers = []
            unknown_to_ref = {}
        try:
            cited_by = str(getattr(hypothesis, "id", "") or "")
        except Exception:
            cited_by = ""
        evaporated = [
            EvaporatedId(cited_id=c, cited_by=(unknown_to_ref.get(c) or cited_by)[:64])
            for c in cited
            if c not in bound
        ]
        demotions: list[Demotion] = []
        try:
            tier = str(getattr(result, "tier", "") or "")
            if tier == "likely":
                cause_codes: list[str] = []
                details: list[str] = []
                for lim in getattr(result, "limitations", []) or []:
                    try:
                        code = getattr(getattr(lim, "code", ""), "value", getattr(lim, "code", ""))
                        if str(code or "").strip():
                            cause_codes.append(str(code))
                    except Exception:
                        continue
                    try:
                        detail = str(getattr(lim, "detail", "") or "").strip()
                        if detail:
                            details.append(detail)
                    except Exception:
                        continue
                demotions.append(
                    Demotion(
                        hypothesis_id=hypothesis.id,
                        cause_codes=cause_codes[:10],
                        detail=(
                            "; ".join(details)[:500]
                            if details
                            else "heuristic score below the established floor"
                        ),
                    )
                )
        except Exception:
            pass
        missing: list[MissingObligation] = []
        try:
            status = getattr(result, "status", None)
            corroborating = getattr(result, "corroborating_evidence_ids", []) or []
            if status == VerificationStatus.INCONCLUSIVE and not corroborating:
                kind_value: str | None = None
                try:
                    kind = VerificationEngine.effective_claim_kind(hypothesis)
                    if kind is not None:
                        kind_value = str(getattr(kind, "value", kind) or "").strip() or None
                except Exception:
                    kind_value = None
                try:
                    statement = str(getattr(hypothesis, "statement", "") or "").strip()
                except Exception:
                    statement = ""
                missing.append(
                    MissingObligation(
                        claim=statement[:1024],
                        claim_kind=kind_value,
                        next_action=(
                            _MISSING_OBLIGATION_DIRECTIVES.get(kind_value or "")
                            or _DEFAULT_MISSING_OBLIGATION_DIRECTIVE
                        ),
                    )
                )
        except Exception:
            pass
        codes = sorted(
            {e.reason_code.value for e in evaporated}
            | {d.reason_code.value for d in demotions}
            | {m.reason_code.value for m in missing}
        )
        if not (evaporated or demotions or missing or claim_bindings):
            return None
        return VerificationDiagnostics(
            evaporated_ids=evaporated[:100],
            demotions=demotions[:10],
            missing_obligations=missing[:10],
            reason_codes=codes[:20],
            claim_bindings=claim_bindings[:100],
            claim_ledgers=claim_ledgers[:100],
        )
    except Exception:
        return None


# Phase 1 (claim binding): version pin for authoritative claim-local
# resolution. The ``cb``-prefixed values are deliberately distinct from
# VERIFIER_VERSION_* (v1/v2/v3) so dashboards, log queries, and telemetry
# labels never conflate the two pins. Defined ahead of first use because
# repair_citation's default pin references CURRENT at def time.
CLAIM_BINDING_CBV1 = "cbv1"
CLAIM_BINDING_CBV2 = "cbv2"
# Phase 3 (claim binding): cbv2 semantics plus applied misattribution
# repair. Stamped only via a separate owner amendment after the
# shadow-first gate; no creation path emits it.
CLAIM_BINDING_CBV3 = "cbv3"
# Rollout value: unmarked rows resolve here, preserving byte-identical
# pre-Phase-1 behavior. Repointing to CLAIM_BINDING_CBV2 requires a
# separate owner amendment, never a silent constant change.
CLAIM_BINDING_CURRENT = CLAIM_BINDING_CBV1
KNOWN_CLAIM_BINDING_VERSIONS = frozenset(
    {CLAIM_BINDING_CBV1, CLAIM_BINDING_CBV2, CLAIM_BINDING_CBV3}
)


class ClaimBindingEnforcement(BaseModel):
    """Authoritative Phase 1 partition outcome for one hypothesis.

    ``kept_ids`` is the union of claim-local hits plus explicitly admitted
    bank-fallback rows (admitted with UNMAPPED_CITATION, never for
    CALL_EDGE completeness or HISTORY newest-commit matching).
    ``cited_kept_count`` counts cited IDs surviving in kept — the verdict
    trigger is defined over it, never over raw kept emptiness, so uncited
    orientation/context rows (including statement-matched range rows) can
    never substitute for zero cited survival. ``unmapped`` marks
    hypotheses with no claim-local linkage (no timeline-backed attempts,
    or attempts that retrieved nothing cited); their bank-fallback rows
    verify only with UNMAPPED_CITATION, never for CALL_EDGE completeness
    or HISTORY newest-commit matching.
    """

    model_config = ConfigDict(frozen=True)

    cited_ids: list[str] = Field(default_factory=list, max_length=200)
    kept_ids: list[str] = Field(default_factory=list, max_length=200)
    evaporated_ids: list[str] = Field(default_factory=list, max_length=100)
    local_kinds: list[str] = Field(
        default_factory=list,
        max_length=20,
        description="Claim kinds with at least one cited ID retrieved by that "
        "claim's own linked attempts (per-claim locality, never borrowed).",
    )
    cited_kinds: list[str] = Field(
        default_factory=list,
        max_length=20,
        description="Claim kinds with at least one cited ID.",
    )
    local_groups: list[str] = Field(
        default_factory=list,
        max_length=100,
        description="Per-(kind, target) group identities (KIND|target_key) "
        "with at least one claim-local kept ID. Locality never crosses "
        "groups — not even between two targets of the same kind.",
    )
    cited_groups: list[str] = Field(
        default_factory=list,
        max_length=100,
        description="Per-(kind, target) group identities with at least one cited ID.",
    )
    unmapped_kinds: list[str] = Field(
        default_factory=list,
        max_length=20,
        description="Claim kinds with at least one cited group lacking "
        "claim-local hits. Drives the CALL_EDGE/HISTORY refusal and the "
        "UNMAPPED limitation.",
    )
    linkage_observed: bool = Field(
        default=False,
        description="True when timeline-backed attempts existed to join "
        "against. Locality enforcement (claim-local vs bank-fallback) "
        "applies only when observed; without it the partition degrades "
        "to unmapped bank-path.",
    )
    fault: bool = Field(
        default=False,
        description="True when partitioning itself faulted under cbv2. "
        "Fail closed: the claim is INCONCLUSIVE, never legacy-decided.",
    )
    cited_kept_count: int = Field(default=0, ge=0)
    unmapped: bool = Field(default=True)
    has_claim_local: bool = Field(default=False)
    limitation_codes: list[str] = Field(default_factory=list, max_length=10)
    next_action: str | None = Field(default=None, max_length=1024)
    # Phase 3 (claim binding): rebound donor IDs (applied, cbv3) and
    # shadow-recorded donor IDs (record-only, cbv2). Bounded, additive,
    # default empty — verify/conclude telemetry and the conclude waiver
    # block observe repairs without recomputing them.
    repaired_ids: list[str] = Field(default_factory=list, max_length=30)
    repair_shadow_ids: list[str] = Field(default_factory=list, max_length=30)


def bound_kept_evidence_only(
    evidence_items: list[Any], kept_ids: set[str], cited_ids: set[str]
) -> tuple[list[Any], list[str], int]:
    """Filter a verify/conclude working set down to kept citations.

    Returns ``(kept_items, evaporated_ids, cited_kept_count)``. Rows whose
    IDs are not cited pass through untouched (orientation/context, never
    proof) and never count toward ``cited_kept_count``. Cited rows outside
    ``kept_ids`` are dropped here AND returned as evaporated so the caller
    records them — never silently omitted. Pure function; callers own the
    Demotion/MissingObligation records.
    """
    kept: list[Any] = []
    evaporated: list[str] = []
    cited_kept_count = 0
    for item in evidence_items or []:
        try:
            eid = str(getattr(item, "evidence_id", "") or "")
        except Exception:
            continue
        if eid and eid in cited_ids:
            if eid not in kept_ids:
                evaporated.append(eid)
                continue
            cited_kept_count += 1
        kept.append(item)
    return kept, evaporated, cited_kept_count


def repair_citation(
    claim_kind: str | None,
    target: Any | None,
    candidate_row: Any,
    donor_row: Any,
    binding_version: str = CLAIM_BINDING_CURRENT,
) -> bool | None:
    """Phase 3 repair predicate (live behind the cbv3/shadow gate).

    Returns True only when the donor row may stand in for a misattributed
    citation: same file path and revision as the cited row (normalized
    suffix match, exact revision), and kind-grounded for the claim
    target. Content equality is checked by the caller. The HISTORY
    strict-subject rule follows binding_version (cbv2 AND cbv3 enforce
    it); DEFINITION has no pin branch.
    """
    try:
        if not _paths_match_for_repair(candidate_row, donor_row):
            return False
        try:
            kind = ClaimKind(str(claim_kind)) if claim_kind else None
        except Exception:
            return None
        if kind == ClaimKind.DEFINITION:
            ok, _ = VerificationEngine.definition_window_proof([donor_row], target)
            return bool(ok)
        if kind == ClaimKind.HISTORY:
            ok, _ = VerificationEngine.history_proof([donor_row], target, binding_version)
            return bool(ok)
        return False
    except Exception:
        return False


def _same_pinned_revision(first: Any, second: Any) -> bool:
    """True only when both sides name the same established revision.

    Missing revision never matches: unknown is not equal to unknown,
    and never equal to a pinned revision. Guards same-revision joins
    against revision-less rows, which carry no evidence of sameness.
    """
    try:
        left = str(first or "").strip()
        right = str(second or "").strip()
    except Exception:
        return False
    return bool(left and right and left == right)


def _paths_match_for_repair(candidate_row: Any, donor_row: Any) -> bool:
    """Same file path (normalized suffix match) and revision on both rows.

    Pure. Uses the same ``_paths_match`` the kind gates use so
    locator-qualified vs repo-relative pairs agree with the gate;
    revision comparison stays exact string equality.
    """
    try:
        candidate_file = VerificationEngine._item_file(candidate_row)
        donor_file = VerificationEngine._item_file(donor_row)
        if not VerificationEngine._paths_match(candidate_file, donor_file):
            return False
        candidate_rev = str(
            getattr(
                getattr(getattr(candidate_row, "provenance", None), "source_location", None),
                "revision",
                "",
            )
            or ""
        )
        donor_rev = str(
            getattr(
                getattr(getattr(donor_row, "provenance", None), "source_location", None),
                "revision",
                "",
            )
            or ""
        )
        return _same_pinned_revision(candidate_rev, donor_rev)
    except Exception:
        return False


def _content_matches_for_repair(
    candidate_row: Any, donor_row: Any
) -> Literal["snippet_containment", "overlap_and_containment"] | None:
    """Donor shows (at least) what the candidate showed (pure).

    Returns "snippet_containment" when the normalized donor snippet
    contains the normalized candidate snippet (non-empty);
    "overlap_and_containment" when containment holds AND both rows
    carry line ranges that overlap. Otherwise None (including
    overlap-without-containment — overlap alone never matches).
    Never raises. Normalization is case-sensitive strip +
    single-space collapse.
    """
    try:
        candidate_snippet = str(getattr(candidate_row, "content_snippet", "") or "")
        donor_snippet = str(getattr(donor_row, "content_snippet", "") or "")

        def _normalize(text: str) -> str:
            return re.sub(r"\s+", " ", str(text or "")).strip()

        norm_candidate = _normalize(candidate_snippet)
        norm_donor = _normalize(donor_snippet)
        if not norm_candidate or norm_candidate not in norm_donor:
            return None
        try:
            candidate_location = getattr(
                getattr(candidate_row, "provenance", None), "source_location", None
            )
            donor_location = getattr(
                getattr(donor_row, "provenance", None), "source_location", None
            )
            candidate_start = int(getattr(candidate_location, "line_start", 0) or 0)
            candidate_end = int(getattr(candidate_location, "line_end", 0) or 0)
            donor_start = int(getattr(donor_location, "line_start", 0) or 0)
            donor_end = int(getattr(donor_location, "line_end", 0) or 0)
        except (TypeError, ValueError, AttributeError):
            return "snippet_containment"
        if min(candidate_start, candidate_end, donor_start, donor_end) < 1:
            return "snippet_containment"
        if max(candidate_start, donor_start) <= min(candidate_end, donor_end):
            return "overlap_and_containment"
        return "snippet_containment"
    except Exception:
        return None


def attempt_claim_repair(
    claim_kind: str | None,
    claim_key_value: str,
    target: Any | None,
    candidate_ids: list[str],
    cited_ids: set[str],
    evidence_by_id: dict[str, Any],
    binding_version: Literal["cbv2", "cbv3"],
    gate_reason: str = "",
    max_repairs: int = 3,
) -> tuple[list[str], list[RepairedBinding]]:
    """Try to rebind misattributed citations to donor rows (pure).

    candidate_ids are cited, banked IDs that failed THIS group's kind
    gate for THIS group's target; claim_key_value is the caller's
    existing claim_key() over the group's statement+kind. Donor pool is
    banked rows cited by NO group of the hypothesis, scanned in sorted
    evidence-ID order; the first donor passing repair_citation() under
    binding_version plus _content_matches_for_repair() rebinds that
    citation. Returns (rebound_ids, repairs). Never raises; never
    touches unknown (evaporated) IDs; caps at max_repairs rebinds.
    binding_version stamps each repair's outcome/binding_version
    (cbv2->shadow, cbv3->applied). claim_ref/target_ref/inline target
    derive via the existing claim_ref()/target_group_key(); gate_reason
    is recorded as passed. Review-only fields, none used in decisions.
    """
    rebound: list[str] = []
    repairs: list[RepairedBinding] = []
    # One uncited donor may ground several citations of the same
    # group (each use independently gated and recorded); the
    # per-group cap counts performed rebinds, not unique donors.
    done = 0
    try:
        cap = max(1, int(max_repairs or 3))
    except Exception:
        cap = 3
    try:
        kind_value = str(claim_kind) if claim_kind else ""
    except Exception:
        kind_value = ""
    if kind_value not in ("DEFINITION", "HISTORY"):
        return [], []
    try:
        ref = claim_ref(str(claim_key_value or ""))
    except Exception:
        return [], []
    try:
        target_key, _target_dict = target_group_key(target)
    except Exception:
        return [], []
    try:
        target_dict: dict[str, Any] = dict(_target_dict or {})
    except Exception:
        return [], []
    try:
        target_model = ClaimTarget(**{k: v for k, v in target_dict.items()})
    except Exception:
        return [], []
    try:
        outcome: Literal["applied", "shadow"] = (
            "applied" if binding_version == CLAIM_BINDING_CBV3 else "shadow"
        )
    except Exception:
        outcome = "shadow"
    try:
        pool = sorted(
            str(eid or "")
            for eid in (evidence_by_id or {})
            if str(eid or "") and str(eid or "") not in (cited_ids or set())
        )
    except Exception:
        return [], []
    for candidate_id in list(candidate_ids or []):
        if done >= cap:
            break
        try:
            cid = str(candidate_id or "").strip()
        except Exception:
            continue
        if not cid:
            continue
        candidate_row = (evidence_by_id or {}).get(cid)
        if candidate_row is None:
            continue
        for donor_id in pool:
            donor_row = (evidence_by_id or {}).get(donor_id)
            if donor_row is None:
                continue
            try:
                if not repair_citation(
                    kind_value,
                    target_model,
                    candidate_row,
                    donor_row,
                    binding_version,
                ):
                    continue
                basis = _content_matches_for_repair(candidate_row, donor_row)
            except Exception:
                continue
            if not basis:
                continue
            try:
                repairs.append(
                    RepairedBinding(
                        claim_ref=ref,
                        claim_key=str(claim_key_value or "")[:560],
                        target_ref=str(target_key or "")[:32],
                        target=dict(target_dict),
                        cited_id=cid,
                        donor_id=str(donor_id),
                        basis=basis,
                        outcome=outcome,
                        binding_version=binding_version,
                        gate_reason=str(gate_reason or "")[:512],
                        donor_snippet=str(getattr(donor_row, "content_snippet", "") or "")[:500],
                    )
                )
            except Exception:
                continue
            done += 1
            if str(donor_id) not in rebound:
                rebound.append(str(donor_id))
            break
    return rebound, repairs


def _latest_linked_outcome(
    ledger: Any, recorded: list[tuple[list[str], dict[str, Any] | None]]
) -> str | None:
    """Outcome string of the claim's latest linked attempt, or None."""
    try:
        indexes = list(getattr(ledger, "attempt_indexes", None) or [])
        if not indexes:
            return None
        _banked, attempt = recorded[int(indexes[-1])]
        if isinstance(attempt, dict):
            return str(attempt.get("outcome") or "") or None
    except Exception:
        return None
    return None


def _claim_bounded_search(
    binding: ClaimRetrievalBinding,
    ledger: Any,
    recorded: list[tuple[list[str], dict[str, Any] | None]],
) -> bool:
    """True when the claim's only basis is a truncated scoped empty.

    The truncation must come from a strictly prior attempt for the same
    normalized (query, scope) key — the latest linked empty itself never
    counts, even if it carries truncation signals.
    """
    try:
        if list(getattr(binding, "claim_local", None) or []) or list(
            getattr(binding, "bank_fallback", None) or []
        ):
            return False
        if _latest_linked_outcome(ledger, recorded) != RetrievalOutcome.SCOPED_EMPTY.value:
            return False
        try:
            positions = [int(p) for p in (getattr(ledger, "attempt_indexes", None) or [])]
            latest_pos = max(positions) if positions else -1
        except Exception:
            return False
        try:
            query = str(getattr(ledger, "search_query", "") or "")
            scope = _normalize_scope(getattr(ledger, "scope", None))
        except Exception:
            return False
        for pos, (_banked, attempt) in enumerate(recorded or []):
            if pos >= latest_pos:
                continue
            if not isinstance(attempt, dict):
                continue
            if str(attempt.get("search_query") or "") != query:
                continue
            if _normalize_scope(attempt.get("scope")) != scope:
                continue
            try:
                truncated = int(attempt.get("truncated_rows") or 0)
            except (TypeError, ValueError):
                truncated = 0
            signals = attempt.get("signals") or []
            signaled = (
                any(str(s).startswith("truncated:") for s in signals)
                if isinstance(signals, list)
                else False
            )
            try:
                provider_total = attempt.get("provider_total_count")
                banked_count = attempt.get("banked_count")
                totaled = (
                    provider_total is not None
                    and banked_count is not None
                    and int(provider_total) > int(banked_count)
                )
            except (TypeError, ValueError):
                totaled = False
            if truncated > 0 or signaled or totaled:
                return True
    except Exception:
        return False
    return False


def build_verify_evaluation_attributes(
    diagnostics: Any | None,
    *,
    turn: int | None,
    hypothesis_id: Any,
    verifier_version: str,
) -> dict[str, Any] | None:
    """Attributes for one VERIFY_EVALUATION timeline marker, or None.

    Pure (diagnostics-only): serializes the recomputed claim bindings,
    their ledgers, and the missing obligations (claim identity only) so
    the benefit battery measures changed bindings and missing-obligation
    events from persisted state instead of recomputing the join. A None
    diagnostics (empty or otherwise unrepresented evaluation) yields a
    skeleton with empty lists — every current-version turn persists a
    marker, so the battery never mistakes a missing row for an unevaluated
    turn. None is returned only on internal error; the caller then skips
    the write and reports the persistence failure. Sizes are bounded by
    the diagnostics caps (100 bindings, 50 IDs each, 10 missing), so the
    marker stays a small JSON object.
    """
    try:
        try:
            turn_value = int(turn) if turn is not None else None
        except (TypeError, ValueError):
            turn_value = None
        skeleton = {
            "turn": turn_value,
            "hypothesis_id": str(hypothesis_id or ""),
            "verifier_version": str(verifier_version or ""),
            "bindings": [],
            "ledgers": [],
            "missing_obligations": [],
            "evaporated_count": 0,
            # Phase 3 (claim binding): bounded repair dumps plus uncapped
            # counts. A truncated list sets repaired_truncated so the
            # Phase 3 gate fails closed instead of reviewing a partial set.
            "repaired": [],
            "repaired_truncated": False,
            "repaired_count": 0,
            "repair_shadow_count": 0,
        }
        if diagnostics is None:
            return skeleton
        bindings = list(getattr(diagnostics, "claim_bindings", None) or [])
        ledgers = list(getattr(diagnostics, "claim_ledgers", None) or [])
        missing = list(getattr(diagnostics, "missing_obligations", None) or [])
        evaporated = list(getattr(diagnostics, "evaporated_ids", None) or [])
        repaired_all = list(getattr(diagnostics, "repaired", None) or [])
        binding_dicts: list[dict[str, Any]] = []
        for binding in bindings[:100]:
            try:
                dumped = binding.model_dump(mode="json")
                binding_dicts.append(dict(dumped) if isinstance(dumped, dict) else {})
            except Exception:
                continue
        ledger_dicts: list[dict[str, Any]] = []
        for ledger in ledgers[:100]:
            try:
                dumped = ledger.model_dump(mode="json")
                ledger_dicts.append(dict(dumped) if isinstance(dumped, dict) else {})
            except Exception:
                continue
        missing_dicts: list[dict[str, Any]] = []
        for item in missing[:10]:
            try:
                missing_dicts.append(
                    {
                        "claim": str(getattr(item, "claim", "") or "")[:1024],
                        "claim_kind": getattr(item, "claim_kind", None),
                        "reason_code": str(
                            getattr(getattr(item, "reason_code", ""), "value", "") or ""
                        ),
                    }
                )
            except Exception:
                continue
        repaired_dicts: list[dict[str, Any]] = []
        repaired_truncated = False
        try:
            ordered = sorted(
                repaired_all,
                key=lambda r: (
                    str(getattr(r, "claim_ref", "") or ""),
                    str(getattr(r, "cited_id", "") or ""),
                    str(getattr(r, "donor_id", "") or ""),
                ),
            )
        except Exception:
            ordered = list(repaired_all)
        for item in ordered:
            if len(repaired_dicts) >= 10:
                repaired_truncated = True
                break
            try:
                dumped = item.model_dump(mode="json") if hasattr(item, "model_dump") else {}
                repaired_dicts.append(dict(dumped) if isinstance(dumped, dict) else {})
            except Exception:
                continue
        repaired_count = 0
        repair_shadow_count = 0
        for item in repaired_all:
            try:
                if str(getattr(item, "outcome", "") or "") == "applied":
                    repaired_count += 1
                else:
                    repair_shadow_count += 1
            except Exception:
                repair_shadow_count += 1
        skeleton["bindings"] = binding_dicts
        skeleton["ledgers"] = ledger_dicts
        skeleton["missing_obligations"] = missing_dicts
        skeleton["evaporated_count"] = len(evaporated)
        skeleton["repaired"] = repaired_dicts
        skeleton["repaired_truncated"] = repaired_truncated
        skeleton["repaired_count"] = repaired_count
        skeleton["repair_shadow_count"] = repair_shadow_count
        return skeleton
    except Exception:
        return None


def _match_recent_action(
    ledger: Any, recent_actions: list[Any]
) -> tuple[str, dict[str, Any]] | tuple[None, None]:
    """Find the recent typed action behind a claim's ledger query.

    Prefers a history summary carrying the same retrieval attempt
    (normalized query + normalized scope); falls back to a retryable
    action whose parameters derive to the same key. Stored ledgers
    (Phase 2) get a second query-only pass: scope never gates the
    match, it only parameterizes dispatch — the second pass reuses the
    query-matching action and lets the caller parameterize scope from
    the ledger's stored scope. Returns (None, None) when no typed,
    validated action matches — the directive is then skipped.
    """
    try:

        def _norm_query(value: object) -> str:
            try:
                return str(value or "").strip()[:1024]
            except Exception:
                return ""

        query = _norm_query(getattr(ledger, "search_query", ""))
        scope = _normalize_scope(getattr(ledger, "scope", None))
        stored = str(getattr(ledger, "query_source", "derived") or "") == "stored"
    except Exception:
        return None, None
    if not query:
        return None, None
    try:
        entries = list(recent_actions or [])
    except Exception:
        return None, None

    def _scan(
        require_scope: bool,
    ) -> tuple[str, dict[str, Any]] | tuple[None, None]:
        for entry in reversed(entries):
            if not isinstance(entry, dict):
                continue
            try:
                attempt = entry.get("retrieval_attempt")
                if isinstance(attempt, dict):
                    if _norm_query(attempt.get("search_query")) != query:
                        continue
                    if require_scope and _normalize_scope(attempt.get("scope")) != scope:
                        continue
                else:
                    action = str(entry.get("action") or "").strip().upper()
                    params = entry.get("parameters")
                    if action not in RETRYABLE_ACTIONS or not isinstance(params, dict):
                        continue
                    derived_query, derived_scope, _provider = extract_search_query(action, params)
                    if _norm_query(derived_query) != query:
                        continue
                    if require_scope and _normalize_scope(derived_scope) != scope:
                        continue
                action = str(entry.get("action") or "").strip().upper()
                params = entry.get("parameters")
                if action not in RETRYABLE_ACTIONS or not isinstance(params, dict):
                    continue
                return action, {str(k): v for k, v in params.items()}
            except Exception:
                continue
        return None, None

    action_type, params = _scan(True)
    if (action_type is None or params is None) and stored:
        action_type, params = _scan(False)
    if action_type is None or params is None:
        return None, None
    if stored:
        # The query-only fallback may find an action recorded with a
        # provider-generated scope. Re-dispatch it using the stored claim
        # scope so the action and the ledger's durable budget key agree.
        stored_scope = _normalize_scope(getattr(ledger, "scope", None))
        if stored_scope:
            params = dict(params)
            params["scope"] = stored_scope
    return action_type, params


def select_ledger_requery(
    hypothesis: Any,
    evidence_items: list[Any],
    recorded_attempts: list[tuple[list[str], dict[str, Any] | None]] | None,
    recent_actions: list[Any] | None,
    max_attempts: int = WP_A3_DEFAULT_MAX_ATTEMPTS,
) -> dict[str, Any] | None:
    """First dispatchable bounded re-retrieval directive, or None (pure).

    Walks the hypothesis's distinct typed claims in order; the first
    claim whose binding is unsatisfied (unknown citations with zero
    durable attempts, or a failed latest attempt under budget) and whose
    query maps to a typed, validated recent action yields the directive.
    Fail-closed on unavailable history: ``recorded_attempts=None``
    (attempt history unavailable, distinct from confirmed-empty) yields
    None — an unknown-citation retry is never authorized without durable
    data. Fail-open on internal error: returns None, never a directive.
    """
    try:
        if recorded_attempts is None:
            return None
        recorded = list(recorded_attempts)
        bound: set[str] = set()
        for item in evidence_items or []:
            try:
                key = str(getattr(item, "evidence_id", "") or "").strip()
            except Exception:
                continue
            if key:
                bound.add(key)
        try:
            statement = str(getattr(hypothesis, "statement", "") or "")
        except Exception:
            statement = ""
        try:
            budget = max(1, int(max_attempts))
        except (TypeError, ValueError):
            budget = WP_A3_DEFAULT_MAX_ATTEMPTS
        for group in _distinct_typed_claims(hypothesis):
            try:
                kind_value = group.get("kind_value")
                target_dict = group.get("target_dict")
                group_cited = group.get("cited_ids")
                ledger, binding, _full_unknown = build_claim_ledger(
                    statement=str(group.get("statement", "") or statement),
                    claim_kind=str(kind_value) if kind_value else None,
                    claim_target=dict(target_dict) if isinstance(target_dict, dict) else None,
                    cited_ids=list(group_cited) if isinstance(group_cited, list) else [],
                    bank_ids=set(bound),
                    attempts=recorded,
                    target_ref=str(group.get("target_key") or "") or None,
                    stored_query=str(group.get("stored_query") or "") or None,
                    stored_scope=str(group.get("stored_scope") or "") or None,
                )
            except Exception:
                continue
            try:
                latest = _latest_linked_outcome(ledger, recorded)
            except Exception:
                latest = None
            group_cited_ids = list(group_cited) if isinstance(group_cited, list) else []
            durable = count_persisted_attempts(recorded, ledger.search_query, ledger.scope)
            # Finding B: linkage is query-only, so a same-query attempt at
            # a different scope still lands in attempt_indexes. Only a
            # scope-matching linked attempt suppresses the never-retrieved
            # trigger — the durable budget already counts (query, scope).
            # Shared with re_retrieval_directive via linked_scope_attempted.
            try:
                _scope_hit = linked_scope_attempted(ledger, recorded)
            except Exception:
                _scope_hit = True
            never_retrieved = (
                not group_cited_ids
                and not binding.unknown
                and getattr(ledger, "query_source", "derived") == "stored"
                and not _scope_hit
                and durable == 0
            )
            failed_retry = latest == RetrievalOutcome.SEARCH_FAILED.value
            if not (binding.unknown or never_retrieved or failed_retry):
                continue
            if durable >= budget:
                continue
            try:
                action_type, params = _match_recent_action(ledger, recent_actions or [])
            except Exception:
                continue
            if action_type is None or params is None:
                continue
            try:
                directive = re_retrieval_directive(
                    binding, ledger, latest, recorded, action_type, params, budget
                )
            except Exception:
                continue
            if directive is not None:
                return directive
        return None
    except Exception:
        return None


def _verification_extra(result: Any) -> dict[str, Any]:
    """Verdict + counts from VerificationResult (never explanations)."""
    try:
        extra: dict[str, Any] = {}
        status = getattr(result, "status", None)
        if status is not None:
            extra["iap.verdict"] = str(getattr(status, "value", status))
        corroborating = getattr(result, "corroborating_evidence_ids", None) or []
        extra["iap.corroborating_count"] = len(corroborating)
        confidence = getattr(getattr(result, "confidence", None), "overall_confidence", None)
        if isinstance(confidence, (int, float)):
            extra["iap.confidence"] = confidence
        return extra
    except Exception:
        return {}


def _gate_extra(decision: Any) -> dict[str, Any]:
    """Gate outcome counts (never blocker text — may quote evidence)."""
    try:
        extra: dict[str, Any] = {}
        if getattr(decision, "approved", None) is not None:
            extra["iap.gate_approved"] = bool(decision.approved)
        blockers = getattr(decision, "blockers", None) or []
        extra["iap.blockers_count"] = len(blockers)
        return extra
    except Exception:
        return {}


from dataclasses import dataclass
from typing import Literal as _Literal


@dataclass(frozen=True)
class CategoryProofRule:
    """Per-category proof policy (Java D7).

    required_evidence_families names the evidence families that must be present;
    source_only_verifiable False means source evidence alone can never verify;
    max_verdict caps the final tier (possible effects and call targets cap at
    LIKELY; runtime/causal never verify from source alone).
    """

    required_evidence_families: frozenset[str]
    requires_immutable_source_or_observation_identity: bool
    requires_exact_scope_match: bool
    requires_source_window_for_each_location: bool
    requires_structured_fact_match: bool
    source_only_verifiable: bool
    max_verdict: _Literal["INSUFFICIENT", "LIKELY", "VERIFIED"]


_VERIFIED_SOURCE = {
    "requires_immutable_source_or_observation_identity": True,
    "requires_exact_scope_match": True,
    "requires_source_window_for_each_location": True,
    "requires_structured_fact_match": True,
    "source_only_verifiable": True,
    "max_verdict": "VERIFIED",
}
_LIKELY_SOURCE = {**_VERIFIED_SOURCE, "max_verdict": "LIKELY"}

CATEGORY_PROOF_RULES: dict[JavaFactCategory, CategoryProofRule] = {
    JavaFactCategory.DECLARATION_FACT: CategoryProofRule(
        frozenset({"SOURCE_RANGE"}),
        **_VERIFIED_SOURCE,  # type: ignore[arg-type]
    ),
    JavaFactCategory.SOURCE_VALUE_FACT: CategoryProofRule(
        frozenset({"SOURCE_RANGE", "ANALYZER_RESOLUTION"}),
        **_VERIFIED_SOURCE,  # type: ignore[arg-type]
    ),
    JavaFactCategory.LANGUAGE_SEMANTIC_FACT: CategoryProofRule(
        frozenset({"SOURCE_RANGE", "LANGUAGE_RULE_VERSION", "ANALYZER_RESOLUTION"}),
        **_VERIFIED_SOURCE,  # type: ignore[arg-type]
    ),
    JavaFactCategory.CONTROL_FLOW_FACT: CategoryProofRule(
        frozenset({"SOURCE_RANGE", "ANALYZER_RESOLUTION"}),
        **_LIKELY_SOURCE,  # type: ignore[arg-type]
    ),
    JavaFactCategory.DATA_FLOW_FACT: CategoryProofRule(
        frozenset({"SOURCE_RANGE", "ANALYZER_RESOLUTION"}),
        **_LIKELY_SOURCE,  # type: ignore[arg-type]
    ),
    JavaFactCategory.CALL_TARGET_FACT: CategoryProofRule(
        frozenset({"CALL_SITE_RANGE", "TARGET_DECLARATION_RANGE", "ANALYZER_RESOLUTION"}),
        **_LIKELY_SOURCE,  # type: ignore[arg-type]
    ),
    JavaFactCategory.POSSIBLE_EFFECT_FACT: CategoryProofRule(
        frozenset({"SOURCE_RANGE", "ANALYZER_RESOLUTION"}),
        **_LIKELY_SOURCE,  # type: ignore[arg-type]
    ),
    JavaFactCategory.CONFIGURATION_FACT: CategoryProofRule(
        frozenset({"SOURCE_RANGE", "CONFIGURATION_SOURCE"}),
        **_LIKELY_SOURCE,  # type: ignore[arg-type]
    ),
    JavaFactCategory.RUNTIME_OBSERVATION_FACT: CategoryProofRule(
        frozenset({"RUNTIME_OBSERVATION"}), True, True, False, True, False, "LIKELY"
    ),
    JavaFactCategory.CAUSAL_CLAIM: CategoryProofRule(
        frozenset({"SOURCE_RANGE", "RUNTIME_OBSERVATION", "CAUSAL_STEP_BINDINGS"}),
        True,
        True,
        True,
        True,
        False,
        "LIKELY",
    ),
}


def category_proof_rule(category: JavaFactCategory) -> CategoryProofRule | None:
    """Return the proof policy for a known category, else None (fail closed)."""
    try:
        return CATEGORY_PROOF_RULES.get(category)
    except Exception:
        return None


# The dev system has one active verifier behavior. The ``v2`` label is retained
# only because the frozen D3 protocol names it and existing persisted
# conclusions may expose it. Historical v1/v3 labels do not select behavior.
VERIFIER_VERSION_CURRENT = "v2"
VERIFIER_VERSION_LEGACY = "v1"  # historical audit label only
VERIFIER_VERSION_V2 = VERIFIER_VERSION_CURRENT
VERIFIER_VERSION_V3 = "v3"  # historical audit label only
KNOWN_VERIFIER_VERSIONS = frozenset(
    {VERIFIER_VERSION_LEGACY, VERIFIER_VERSION_CURRENT, VERIFIER_VERSION_V3}
)


class UnknownVerifierVersion(ValueError):
    """A pinned verifier version with no defined semantics (fail closed)."""


class VerifierVersionUnavailable(ValueError):
    """Version information could not be obtained (fail closed)."""


def resolve_claim_binding_version(metadata: Any) -> tuple[str, str]:
    """Resolve the claim-binding behavior version for one investigation.

    Returns ``(version, source)`` where source is ``"pinned"`` or
    ``"unmarked"``. Unmarked rows resolve to CLAIM_BINDING_CURRENT,
    preserving the historical default path per the version-pinned-behavior
    contract. Raises :class:`UnknownVerifierVersion` for pins outside
    :data:`KNOWN_CLAIM_BINDING_VERSIONS` and
    :class:`VerifierVersionUnavailable` when version information could not
    be obtained at all. Callers must fail closed on either exception.
    Pure function; no I/O, no environment reads.
    """
    if metadata is None:
        raise VerifierVersionUnavailable(
            f"no claim binding version metadata: {type(metadata).__name__}"
        )
    try:
        if hasattr(metadata, "model_dump"):
            mapping = dict(metadata.model_dump() or {})
        elif isinstance(metadata, dict):
            mapping = dict(metadata)
        else:
            raise VerifierVersionUnavailable(
                f"no claim binding version metadata: {type(metadata).__name__}"
            )
    except (UnknownVerifierVersion, VerifierVersionUnavailable):
        raise
    except Exception as exc:
        raise VerifierVersionUnavailable(
            f"claim binding version metadata unreadable: {exc}"
        ) from exc
    pinned = str(mapping.get("claim_binding_version") or "").strip()
    if not pinned:
        return CLAIM_BINDING_CURRENT, "unmarked"
    if pinned not in KNOWN_CLAIM_BINDING_VERSIONS:
        raise UnknownVerifierVersion(f"unrecognized claim binding pin: {pinned!r}")
    return pinned, "pinned"


class HistorySubjectInput(BaseModel):
    """HISTORY subject comparison input (structured, never freeform prose)."""

    model_config = ConfigDict(frozen=True)

    claimed_subject: str | None = Field(default=None, max_length=512)
    stored_summary: str | None = Field(default=None, max_length=1024)
    target_file: str = Field(max_length=512)
    target_commit: str = Field(max_length=64)
    investigation_id: UUID


def resolve_verifier_version(metadata: Any) -> tuple[str, str]:
    """Resolve the recorded verifier version for an investigation.

    Returns ``(version, source)`` where source is ``"pinned"`` (row
    carries an explicit pin) or ``"unmarked"`` (successfully loaded row
    or row-equivalent metadata without a pin — these ran current-path
    semantics by default under the documented migration policy, so they
    stay on current). Raises :class:`UnknownVerifierVersion` for pins
    outside :data:`KNOWN_VERIFIER_VERSIONS` and
    :class:`VerifierVersionUnavailable` when version information could
    not be obtained at all (missing row, missing repository read,
    malformed metadata). Callers must fail closed on either exception:
    never guess a contract.

    Version-contained routing (g4-closure + L2-D3-fix agreement): the
    recorded version decides semantics for that row — pinned v1 rows keep
    their exact v1 shape; unmarked rows resolve to current. Never env-switched.
    """
    if metadata is None or not isinstance(metadata, dict):
        raise VerifierVersionUnavailable(f"no version metadata: {type(metadata).__name__}")
    pinned = str(metadata.get("verifier_version") or "").strip()
    if not pinned:
        return VERIFIER_VERSION_CURRENT, "unmarked"
    if pinned not in KNOWN_VERIFIER_VERSIONS:
        raise UnknownVerifierVersion(f"unrecognized pin: {pinned!r}")
    return pinned, "pinned"


class VerificationEngine:
    """Evaluates hypothesis validity against root cause policy requirements.

    G4: a hypothesis whose own statement concedes the claim (inability
    language) can never VERIFY on math alone — the gate never reads claim
    text, so the engine rejects such statements as INCONCLUSIVE up front.
    Patterns are deliberately narrow (inability semantics only): legitimate
    eliminations like "not caused by X" must keep verifying.
    """

    CONCESSION_PATTERNS = (
        "not available",
        "unavailable",
        "unable to",
        "could not determine",
        "could not establish",
        "could not verify",
        "could not retrieve",
        "could not find",
        "cannot determine",
        "cannot verify",
        "cannot establish",
        "no data available",
        "no data to",
        "unknown cause",
        "cause unknown",
        "incomplete extraction",
        "missing extraction",
    )

    def __init__(
        self,
        verifier_version: str = VERIFIER_VERSION_CURRENT,
        claim_binding_version: str = CLAIM_BINDING_CURRENT,
    ) -> None:
        # Kept as API-compatible arguments for shadow/protocol callers. The
        # verifier version does not select behavior in the unified dev
        # runtime; the claim binding version selects Phase 1 strictness
        # (cbv1 bank-path legacy vs cbv2 authoritative partitions).
        self.verifier_version = VERIFIER_VERSION_CURRENT
        if claim_binding_version not in KNOWN_CLAIM_BINDING_VERSIONS:
            raise UnknownVerifierVersion(
                f"unrecognized claim binding pin: {claim_binding_version!r}"
            )
        self.claim_binding_version = claim_binding_version
        # Phase 3 (claim binding): counts repair evaluations actually
        # executed (per-group helper calls). Tests assert cbv1 runs
        # execute zero repair paths via this counter plus the absence
        # of records — absence alone also matches a skipped path.
        self._repair_calls = 0

    @staticmethod
    def canonical_caller_edge_key(
        symbol: Any, counterpart: Any, file_path: Any, commit_id: Any
    ) -> str:
        """Single canonical obligation key for caller edges.

        Shared by verification coverage and workflow repair forcing so both
        sides join on the same identity. `|` is escaped as `%7C` in each
        component to prevent delimiter collisions; file paths are normalized
        identically on both sides (backslash → slash, strip whitespace and
        leading/trailing slashes).
        """

        def _escape(value: Any) -> str:
            return str(value or "").strip().replace("|", "%7C")

        normalized_path = (
            str(file_path or "").replace("\\", "/").strip().strip("/").lstrip("./")
        ).replace("|", "%7C")
        return "|".join(
            (
                _escape(symbol),
                _escape(counterpart),
                normalized_path.strip(),
                _escape(commit_id),
            )
        )

    @staticmethod
    def caller_edge_key(target: ClaimTarget) -> str:
        """Stable obligation key carried through reason/workflow repair."""
        return VerificationEngine.canonical_caller_edge_key(
            getattr(target, "symbol", ""),
            getattr(target, "counterpart", ""),
            getattr(target, "file_path", ""),
            getattr(target, "commit_id", ""),
        )

    @staticmethod
    def required_caller_edge_keys(hypothesis: Any) -> set[str]:
        """Read v3 caller obligations persisted in required_verification."""
        return {
            str(item)[len("CALL_EDGE|") :]
            for item in (getattr(hypothesis, "required_verification", None) or [])
            if str(item).startswith("CALL_EDGE|")
        }

    @staticmethod
    def caller_edge_diagnostics(hypothesis: Any) -> tuple[list[str], list[str], str | None]:
        """Return caller coverage diagnostics without changing proof semantics."""
        assessments = [
            assessment
            for assessment in (getattr(hypothesis, "assessments", None) or [])
            if getattr(assessment, "claim_kind", None) == ClaimKind.CALL_EDGE
            and getattr(assessment, "claim_target", None) is not None
        ]
        proven: set[str] = set()
        unresolved: set[str] = set()
        for assessment in assessments:
            targets = VerificationEngine._aggregate_call_edge_targets(
                getattr(assessment, "claim_target", None)
            ) or [getattr(assessment, "claim_target", None)]
            for target in targets:
                if target is None:
                    continue
                caller = str(getattr(target, "counterpart", "") or "").strip()
                if not caller:
                    continue
                (
                    proven if getattr(assessment, "evidence_id", None) is not None else unresolved
                ).add(caller)
        proven_callers = sorted(proven, key=str.casefold)
        unresolved_callers = sorted(unresolved - proven, key=str.casefold)
        required = VerificationEngine.required_caller_edge_keys(hypothesis)
        required_count = max(len(required), len(proven) + len(unresolved))
        coverage = f"{len(proven)}/{required_count}" if required_count else None
        return proven_callers, unresolved_callers, coverage

    # G3: definition-strength claims ("defined/implemented/declared in X")
    # require at least one range-read (GET_SOURCE) citation — SEARCH matches
    # establish presence, never definition. Provenance operation is the
    # discriminator (intelligence.py SEARCH_CODE vs git.py GET_SOURCE).
    DEFINITION_PATTERNS = (
        "defined in",
        "implemented in",
        "declared in",
        "definition is in",
    )
    GET_OPERATIONS = frozenset({"GET_SOURCE", "GET_CODE", "READ_FILE"})

    # G1: listing claims answerable from code citations (caller lists,
    # histories) — same location-answer path as definitions.
    LISTING_PATTERNS = (
        "located in",
        "located at",
        "found in",
        "lives in",
        "history of",
        "commits touching",
        "callers of",
        "called by",
    )
    LISTING_EVIDENCE_TYPES = frozenset(
        {"SOURCE_CODE", "CALL_GRAPH", "COMMIT_HISTORY", "EXCEPTION_PATH"}
    )

    # G9: path-token pattern for correction claims ("the correct file is
    # X", "actual path is Y", "typo for Z"). Tokens must contain a slash
    # so bare words/versions never match; trailing punctuation stripped
    # at extraction time.
    _PATH_TOKEN_RE = r"[A-Za-z0-9_.\-~/][A-Za-z0-9_.\-/]*\.[A-Za-z0-9]{1,10}"

    @staticmethod
    def concedes_claim(statement: str) -> bool:
        """Deterministic concession detector over the claim text."""
        try:
            lowered = f" {(statement or '').lower()} "
            return any(pattern in lowered for pattern in VerificationEngine.CONCESSION_PATTERNS)
        except Exception:
            return False

    @staticmethod
    def claims_definition(statement: str) -> bool:
        """Whether the claim asserts a definition site (needs a range read)."""
        try:
            lowered = f" {(statement or '').lower()} "
            return any(pattern in lowered for pattern in VerificationEngine.DEFINITION_PATTERNS)
        except Exception:
            return False

    @staticmethod
    def claims_field_value(statement: str) -> bool:
        """Routing aid only: does prose look like a field-value claim?

        Never supplies a structured target, category, or value. Callers must
        read ClaimTarget/HypothesisEvidenceAssessment/DecisionClaim fields for
        the typed claim; prose detection only routes to the proof dispatch.
        """
        lowered = f" {(statement or '').lower()} "
        if any(
            phrase in lowered
            for phrase in (
                "default value",
                "initializer is",
                "initializes to",
                "initialized to",
                "initial value",
                "field value",
                "value of field",
                "value of the field",
                "field defaults to",
                "defaults to",
                "default is",
            )
        ):
            return True
        return (
            re.search(
                r"\b(?:default(?:s)? to|initialized to|initializes to|assigned to|set to|equals?|is)\s+"
                r"(?:['\"][^'\"]+['\"]|[-+]?\d+(?:\.\d+)?(?:[fFdDlL])?|true|false|null)\b",
                lowered,
            )
            is not None
        )

    @staticmethod
    def claims_java_fact(statement: str) -> bool:
        """Routing aid only: generalize claims_field_value without inference.

        Same prose signal as claims_field_value today (no new resolvers in
        Phase 1). Must not supply a structured target, category, or value;
        typed fields on the claim carry those. Unknown phrasing fails closed
        downstream in java_fact_proof.
        """
        try:
            return VerificationEngine.claims_field_value(statement)
        except Exception:
            return False

    @staticmethod
    def _fact_category_of(hypothesis: Any) -> JavaFactCategory | None:
        """Typed fact_category carried by the claim, else None (legacy/untyped).

        Reads hypothesis.fact_category, then claim_target.fact_category, then
        the first assessment carrying one. Never infers from ClaimKind or prose.
        Unknown strings fail closed (return None so only legacy paths apply).
        """
        for source in (
            getattr(hypothesis, "fact_category", None),
            getattr(getattr(hypothesis, "claim_target", None), "fact_category", None),
            getattr(getattr(hypothesis, "target", None), "fact_category", None),
        ):
            if source is None:
                continue
            try:
                return source if isinstance(source, JavaFactCategory) else JavaFactCategory(source)
            except ValueError:
                return None
        try:
            assessments = getattr(hypothesis, "assessments", None) or []
        except Exception:
            assessments = []
        for asm in assessments:
            for source in (
                getattr(asm, "fact_category", None),
                getattr(getattr(asm, "claim_target", None), "fact_category", None),
            ):
                if source is None:
                    continue
                try:
                    return (
                        source if isinstance(source, JavaFactCategory) else JavaFactCategory(source)
                    )
                except ValueError:
                    return None
        return None

    @staticmethod
    def _subject_identifies(requested: str | None, candidates: set[str]) -> bool:
        """Subject match across qualification levels (pure).

        Claims carry short symbols (``OrderLookupServiceImpl.findOrder``)
        while analyzer resolutions carry fully-qualified identities
        (``com.example.retail.order.OrderLookupServiceImpl.findOrder``), or the
        reverse for method-anchored windows. Exact match wins; otherwise a
        dot-boundary suffix in either direction matches — except a bare
        requested name (no dot) never suffix-matches a qualified
        candidate: an unscoped method name must not select across classes.
        Never substring: ``OtherImpl.findOrder`` matches nothing.
        Selection-only — revision, path, window, and value checks below
        still bind the evidence.
        """
        req = str(requested or "").strip()
        if not req:
            return False
        for candidate in candidates:
            cand = str(candidate or "").strip()
            if not cand:
                continue
            if cand == req:
                return True
            if "." in req and cand.endswith("." + req):
                return True
            if req.endswith("." + cand):
                return True
        return False

    @staticmethod
    def _v2_any_from_evidence(
        evidence_items: list[Any],
        category: JavaFactCategory | None = None,
        subject: str | None = None,
    ) -> CodeFactResolutionV2 | None:
        """Select a v2 payload by category/subject, never by insertion order."""
        candidates, malformed = VerificationEngine._v2_candidates_from_evidence(
            evidence_items, category, subject
        )
        if malformed or not candidates:
            return None
        resolved = [item for item in candidates if item.status == CodeFactStatus.RESOLVED]
        # J2: two RESOLVED field-value facts for one subject that disagree
        # are an ambiguity, not a tie to break by insertion order. Picking
        # the first would verify or refute a claim on an arbitrary one.
        # (Guarded-return facts legitimately yield several values per
        # subject - one per guard - so only field values are compared.)
        field_values: dict[str, set[str]] = {}
        for item in resolved:
            if item.field_name is not None and item.subject_identity:
                field_values.setdefault(str(item.subject_identity), set()).add(
                    str(item.resolved_value)
                )
        if any(len(values) > 1 for values in field_values.values()):
            return None
        return resolved[0] if resolved else candidates[0]

    @staticmethod
    def _v2_candidates_from_evidence(
        evidence_items: list[Any],
        category: JavaFactCategory | None = None,
        subject: str | None = None,
    ) -> tuple[list[CodeFactResolutionV2], bool]:
        """Collect all relevant payloads and flag malformed matching entries."""
        requested_subject = str(subject or "").strip()
        candidates: list[CodeFactResolutionV2] = []
        malformed = False
        for item in evidence_items or []:
            attrs = getattr(item, "attributes", None) or {}
            if not isinstance(attrs, dict):
                continue
            raws: list[Any] = []
            for key in (CODE_FACT_RESOLUTION_V2_KEY, "code_fact_resolutions"):
                value = attrs.get(key)
                if key == "code_fact_resolutions" and isinstance(value, list):
                    raws.extend(
                        entry.get("resolution")
                        for entry in value
                        if isinstance(entry, dict) and isinstance(entry.get("resolution"), dict)
                    )
                elif isinstance(value, dict):
                    raws.append(value)
            raws.extend(
                value
                for key, value in attrs.items()
                if key.startswith("code_fact_java.") and isinstance(value, dict)
            )
            for raw in raws:
                raw_category = raw.get("fact_category") if isinstance(raw, dict) else None
                raw_subjects = {
                    str(raw.get(name) or "")
                    for name in (
                        "subject_identity",
                        "declaring_type",
                        "field_name",
                        "call_target",
                        "target_identity",
                    )
                    if isinstance(raw, dict)
                }
                if category is not None and raw_category not in {
                    category,
                    category.value,
                    None,
                }:
                    continue
                if (
                    requested_subject
                    and raw_subjects
                    and not VerificationEngine._subject_identifies(requested_subject, raw_subjects)
                ):
                    continue
                try:
                    resolution = CodeFactResolutionV2.model_validate(raw)
                except Exception:
                    malformed = True
                    continue
                if category is not None and resolution.fact_category != category:
                    continue
                if requested_subject:
                    resolution_subjects = {
                        str(resolution.subject_identity or ""),
                        str(resolution.declaring_type or ""),
                        str(resolution.field_name or ""),
                        str(resolution.call_target or ""),
                        str(resolution.target_identity or ""),
                    }
                    if not VerificationEngine._subject_identifies(
                        requested_subject, resolution_subjects
                    ):
                        continue
                candidates.append(resolution)
        return candidates, malformed

    @staticmethod
    def _window_analysis_of(evidence_items: list[Any]) -> dict[str, Any] | None:
        """Return the non-ok window analysis stamped on a backfilled row, else None."""
        for item in evidence_items or []:
            attrs = getattr(item, "attributes", None)
            raw = attrs.get(WINDOW_ANALYSIS_KEY) if isinstance(attrs, dict) else None
            if isinstance(raw, dict) and raw.get("status") in {"failed", "degraded", "refused"}:
                return raw
        return None

    @staticmethod
    def _window_analysis_reason(evidence_items: list[Any]) -> str | None:
        """Human form of the stamped analysis: `window analysis <status>: <codes>`."""
        raw = VerificationEngine._window_analysis_of(evidence_items)
        if raw is None:
            return None
        codes = ",".join(raw.get("limitations") or [])
        return f"window analysis {raw.get('status')}: {codes}"

    @staticmethod
    def _with_window_backfill(
        evidence_items: list[Any], subject: str | None = None
    ) -> list[Any]:
        """Splice bank-time-enumerated records into rows lacking analysis (pure).

        Covers shared/reused rows banked before enumeration existed: runs
        the window enumerator over numbered, pinned SOURCE_CODE range rows
        that carry no v2 payload and returns in-memory copies with
        RESOLVED records appended to the envelope. Banked rows are never
        mutated; the enumerator is deterministic, so verification stays a
        pure function of (hypothesis, evidence).

        The spliced copy also drops the discovery_only label: that label
        records that no method/field selector was passed, and enumeration
        takes no selectors — it resolves every cleanly-declared value in
        the window. The row itself is a numbered, pinned range read, and
        soundness rests on the proof's own checks (revision, path, window
        containment, subject, exact value), which all still bind every
        backfilled record. Sibling proof paths (definition, caller) keep
        the label and their confirming-read doctrine — only this
        analyzer-bound value path treats the enumerated row as
        proof-grade. Rows whose analysis copy still refuses (unnumbered,
        unpinned, truncated) contribute nothing.
        """
        try:
            from investigation_agent_platform.application.analysis.java.window_facts import (
                enumerate_java_window_facts,
            )
        except Exception:
            return evidence_items
        out: list[Any] = []
        for item in evidence_items or []:
            try:
                attrs = getattr(item, "attributes", None)
                if not isinstance(attrs, dict):
                    out.append(item)
                    continue
                if isinstance(attrs.get(CODE_FACT_RESOLUTION_V2_KEY), dict):
                    out.append(item)
                    continue
                if any(
                    key.startswith("code_fact_java.") and isinstance(value, dict)
                    for key, value in attrs.items()
                ):
                    out.append(item)
                    continue
                envelope = attrs.get("code_fact_resolutions")
                if isinstance(envelope, list) and any(
                    isinstance(entry, dict) and isinstance(entry.get("resolution"), dict)
                    for entry in envelope
                ):
                    out.append(item)
                    continue
                copier = getattr(item, "model_copy", None)
                if not callable(copier):
                    out.append(item)
                    continue
                probe_attrs = dict(attrs)
                probe_attrs.pop("discovery_only", None)
                probe = copier(update={"attributes": probe_attrs})
                analysis = enumerate_java_window_facts(probe, subject_hint=subject)
                records = list(analysis.records)
                merged = list(envelope) if isinstance(envelope, list) else []
                for record in records:
                    payload = record.model_dump(mode="json")
                    if any(
                        isinstance(entry, dict)
                        and isinstance(entry.get("resolution"), dict)
                        and entry.get("resolution", {}).get("subject_identity")
                        == record.subject_identity
                        and entry.get("resolution", {}).get("resolved_value")
                        == record.resolved_value
                        for entry in merged
                    ):
                        continue
                    merged.append(
                        {
                            "resolver": "window_enumeration",
                            "selector": record.subject_identity,
                            "selector_name": "window",
                            "resolution": payload,
                        }
                    )
                sense = dict(attrs)
                if analysis.status.value != "ok":
                    # The reason "nothing was enumerated" is stamped even when
                    # no record was produced; refusing/provenance rows keep
                    # their silence otherwise.
                    sense[WINDOW_ANALYSIS_KEY] = analysis.to_attribute()
                if not merged and WINDOW_ANALYSIS_KEY not in sense:
                    out.append(item)
                    continue
                if merged:
                    sense[CODE_FACT_RESOLUTIONS_KEY] = merged
                sense.pop("discovery_only", None)
                out.append(copier(update={"attributes": sense}))
            except Exception:
                out.append(item)
        return out

    @staticmethod
    def _v2_resolution_from_evidence(
        evidence_items: list[Any],
        category: JavaFactCategory | None = None,
        subject: str | None = None,
    ) -> CodeFactResolutionV2 | None:
        """Load only well-formed RESOLVED v2 analyzer output."""
        resolution = VerificationEngine._v2_any_from_evidence(evidence_items, category, subject)
        if resolution is not None and resolution.status == CodeFactStatus.RESOLVED:
            return resolution
        return None

    @staticmethod
    def _dual_conflict_reason(evidence_items: list[Any]) -> str | None:
        """Conflict reason when an item banks disagreeing dual keys, else None."""
        for item in evidence_items or []:
            attrs = getattr(item, "attributes", None) or {}
            if not isinstance(attrs, dict):
                continue
            legacy_raw = attrs.get("code_fact_resolution")
            v2_raw = attrs.get(CODE_FACT_RESOLUTION_V2_KEY)
            if isinstance(legacy_raw, dict) and isinstance(v2_raw, dict):
                reason = detect_dual_resolution_conflict(legacy_raw, v2_raw)
                if reason is not None:
                    return reason
        return None

    @staticmethod
    def _node_ids_match(hypothesis: Any, resolution: Any) -> tuple[bool, str]:
        """Optional verifier match (Model D5): when both sides carry node_id, match."""
        resolution_node = getattr(resolution, "node_id", None)
        if resolution_node is None:
            return (True, "")
        candidates: list[Any] = [
            getattr(hypothesis, "node_id", None),
            getattr(getattr(hypothesis, "claim_target", None), "node_id", None),
            getattr(getattr(hypothesis, "target", None), "node_id", None),
        ]
        try:
            for asm in getattr(hypothesis, "assessments", None) or []:
                candidates.append(getattr(asm, "node_id", None))
        except Exception:
            pass
        for candidate in candidates:
            if candidate is None:
                continue
            try:
                if str(candidate) != str(resolution_node):
                    return (False, "claim node_id does not match the resolved symbol node_id")
            except Exception:
                continue
            return (True, "")
        return (True, "")

    @staticmethod
    def _java_evidence_families(
        evidence_items: list[Any], resolution: CodeFactResolutionV2 | None
    ) -> set[str]:
        """Evidence families present for the category proof table."""
        families: set[str] = set()
        for item in evidence_items or []:
            try:
                if VerificationEngine._is_range_read(item) and VerificationEngine._item_type(
                    item
                ) in {"SOURCE_CODE", "EXCEPTION_PATH"}:
                    families.add("SOURCE_RANGE")
                    if resolution is not None:
                        structured = resolution.structured_fact or {}
                        if (
                            resolution.fact_category == JavaFactCategory.CALL_TARGET_FACT
                            and isinstance(structured, dict)
                            and structured.get("call_site_line") is not None
                        ):
                            families.add("CALL_SITE_RANGE")
                        if resolution.declaration_location is not None:
                            families.add("TARGET_DECLARATION_RANGE")
                if VerificationEngine._item_type(item) in VerificationEngine.RUNTIME_EVIDENCE_TYPES:
                    families.add("RUNTIME_OBSERVATION")
                attrs = getattr(item, "attributes", None) or {}
                if isinstance(attrs, dict):
                    if isinstance(attrs.get(CODE_FACT_RESOLUTION_V2_KEY), dict):
                        try:
                            CodeFactResolutionV2.model_validate(
                                attrs.get(CODE_FACT_RESOLUTION_V2_KEY)
                            )
                            families.add("ANALYZER_RESOLUTION")
                        except Exception:
                            pass
                    envelope = attrs.get("code_fact_resolutions")
                    if isinstance(envelope, list) and any(
                        isinstance(entry, dict) and isinstance(entry.get("resolution"), dict)
                        for entry in envelope
                    ):
                        families.add("ANALYZER_RESOLUTION")
                    for key, value in attrs.items():
                        if not (key.startswith("code_fact_java.") and isinstance(value, dict)):
                            continue
                        try:
                            named = CodeFactResolutionV2.model_validate(value)
                        except Exception:
                            continue
                        if named.status == CodeFactStatus.RESOLVED:
                            families.add("ANALYZER_RESOLUTION")
                            break
                    if attrs.get("search_scope") is not None:
                        families.add("CAUSAL_STEP_BINDINGS")
                    if attrs.get("configuration_source") or attrs.get("config_source"):
                        families.add("CONFIGURATION_SOURCE")
            except Exception:
                continue
        if resolution is not None and resolution.language_version:
            families.add("LANGUAGE_RULE_VERSION")
        if resolution is not None and resolution.runtime_source_identity:
            families.add("RUNTIME_OBSERVATION")
        return families

    @staticmethod
    def _resolution_from_evidence(evidence_items: list[Any]) -> CodeFactResolution | None:
        """Load only well-formed analyzer output attached to source evidence."""
        for item in evidence_items or []:
            attrs = getattr(item, "attributes", None) or {}
            if not isinstance(attrs, dict):
                continue
            raw = attrs.get("code_fact_resolution")
            if not isinstance(raw, dict):
                continue
            try:
                resolution = CodeFactResolution.model_validate(raw)
            except Exception:
                continue
            if resolution.status == CodeFactStatus.RESOLVED:
                return resolution
        return None

    @staticmethod
    def code_value_proof(hypothesis: Any, evidence_items: list[Any]) -> tuple[bool, str]:
        """Require analyzer output and exact claim/evidence provenance match."""
        resolution = VerificationEngine._resolution_from_evidence(evidence_items)
        if resolution is None:
            return (False, "no resolved CodeFactResolution is bound to the cited evidence")
        semantics = str(
            getattr(hypothesis, "claim_value_semantics", None)
            or getattr(getattr(hypothesis, "claim_target", None), "value_semantics", None)
            or ""
        ).upper()
        claimed_value = getattr(hypothesis, "claimed_value", None) or getattr(
            getattr(hypothesis, "claim_target", None), "claimed_value", None
        )
        target = getattr(hypothesis, "claim_target", None)
        if semantics not in {
            CodeValueSemantics.EXPLICIT_INITIALIZER.value,
            CodeValueSemantics.LANGUAGE_DEFAULT.value,
        }:
            return (False, "value claim does not declare supported source-value semantics")
        if claimed_value is None:
            return (False, "value claim has no structured claimed_value")
        if semantics != resolution.value_semantics.value:
            return (False, "claim semantics do not match the resolved source fact")
        if str(claimed_value) != str(resolution.resolved_value):
            return (
                False,
                f"claimed value {claimed_value!r} conflicts with resolved source value {resolution.resolved_value!r}",
            )
        statement_value = VerificationEngine._claimed_literal_from_statement(
            str(getattr(hypothesis, "statement", "") or "")
        )
        if statement_value is not None and statement_value != str(resolution.resolved_value):
            return (
                False,
                f"statement value {statement_value!r} conflicts with resolved source value {resolution.resolved_value!r}",
            )
        if target is None or str(getattr(target, "symbol", "") or "") != resolution.field_name:
            return (False, "claim target does not identify the resolved field")
        if (
            getattr(target, "declaring_type", None)
            and str(target.declaring_type) != resolution.declaring_type
        ):
            return (False, "claim declaring type does not match the resolved field")
        bound_ids = {
            str(value) for value in (getattr(hypothesis, "supporting_evidence_ids", None) or [])
        }
        if not bound_ids.intersection(str(eid) for eid in resolution.source_evidence_ids):
            return (False, "resolution source evidence is not bound to the claim")
        if getattr(target, "file_path", None) and not VerificationEngine._paths_match(
            str(target.file_path), str(resolution.declaration_location.file_path or "")
        ):
            return (False, "claim path does not match the resolved field declaration")
        matched_evidence = []
        expected_ids = {str(eid) for eid in resolution.source_evidence_ids}
        for item in evidence_items or []:
            if str(getattr(item, "evidence_id", "")) not in expected_ids:
                continue
            loc = getattr(
                getattr(getattr(item, "provenance", None), "source_location", None),
                "revision",
                None,
            )
            if str(loc or "") != resolution.revision:
                continue
            if not VerificationEngine._paths_match(
                str(resolution.declaration_location.file_path or ""),
                VerificationEngine._item_file(item),
            ):
                continue
            matched_evidence.append(item)
        if not matched_evidence:
            return (
                False,
                "resolution has no matching cited evidence at the same revision and path",
            )
        if not any(VerificationEngine._is_range_read(item) for item in matched_evidence):
            return (False, "resolved field has no matching source range read")
        range_evidence = next(
            (item for item in matched_evidence if VerificationEngine._is_range_read(item)),
            None,
        )
        declared_range = VerificationEngine._item_window(range_evidence) if range_evidence else None
        declaration_start = resolution.declaration_location.line_start
        declaration_end = resolution.declaration_location.line_end
        target_line = getattr(target, "line_start", None)
        if declared_range is None or not (
            declaration_start is not None
            and declaration_end is not None
            and declared_range[0] <= declaration_start
            and declaration_end <= declared_range[1]
        ):
            return (False, "source read window does not contain the resolved declaration")
        if target_line is not None and not (declared_range[0] <= target_line <= declared_range[1]):
            return (False, "source read window does not contain the claimed target line")
        node_ok, node_reason = VerificationEngine._node_ids_match(hypothesis, resolution)
        if not node_ok:
            return (False, node_reason)
        return (True, "")

    @staticmethod
    def java_fact_proof(hypothesis: Any, evidence_items: list[Any]) -> tuple[bool, str]:
        """Per-category proof dispatch (Java D7).

        Each category declares required provenance, permissible statuses,
        comparison operator, and abstention conditions. PARTIAL/UNRESOLVED are
        never verifying; RUNTIME_OBSERVATION_FACT/CAUSAL_CLAIM never verify
        from source evidence alone; unknown categories fail closed.
        """
        conflict = VerificationEngine._dual_conflict_reason(evidence_items)
        if conflict is not None:
            return (False, f"conflicting dual code-fact payloads: {conflict}")
        category = VerificationEngine._fact_category_of(hypothesis)
        if category is None:
            return (False, "claim carries no typed fact_category; legacy proof paths only")
        claim_kind = VerificationEngine.effective_claim_kind(hypothesis)
        allowed_kinds = {
            JavaFactCategory.DECLARATION_FACT: {ClaimKind.DEFINITION},
            JavaFactCategory.SOURCE_VALUE_FACT: {ClaimKind.DEFINITION},
            JavaFactCategory.LANGUAGE_SEMANTIC_FACT: {ClaimKind.DEFINITION},
            JavaFactCategory.CONTROL_FLOW_FACT: {ClaimKind.DEFINITION},
            JavaFactCategory.DATA_FLOW_FACT: {ClaimKind.DEFINITION},
            JavaFactCategory.CALL_TARGET_FACT: {ClaimKind.CALL_EDGE},
            JavaFactCategory.POSSIBLE_EFFECT_FACT: {ClaimKind.DEFINITION},
            JavaFactCategory.CONFIGURATION_FACT: {ClaimKind.DEFINITION},
            JavaFactCategory.RUNTIME_OBSERVATION_FACT: {ClaimKind.BEHAVIOR},
            JavaFactCategory.CAUSAL_CLAIM: {ClaimKind.CAUSAL},
        }.get(category, set())
        if claim_kind is not None and claim_kind not in allowed_kinds:
            return (
                False,
                f"fact category {category.value} is incompatible with claim kind {claim_kind.value}",
            )
        if category == JavaFactCategory.CAUSAL_CLAIM:
            return (False, "causal claims are verifier conclusions, never analyzer facts")
        try:
            rule = category_proof_rule(category)
        except Exception:
            rule = None
        if rule is None:
            return (False, f"unknown fact category {category!r}: fail closed")
        target = getattr(hypothesis, "claim_target", None) or getattr(hypothesis, "target", None)
        subject = str(getattr(target, "symbol", "") or "").strip() or None
        candidates, malformed = VerificationEngine._v2_candidates_from_evidence(
            evidence_items, category, subject
        )
        if malformed:
            return (False, "malformed matching analyzer resolution")
        resolved_candidates = [
            item for item in candidates if item.status == CodeFactStatus.RESOLVED
        ]
        if len(resolved_candidates) > 1:
            # Content fingerprint: binding IDs (which row the fact came
            # from) and per-run node IDs are excluded — the same true
            # fact banked twice (shared + owned rows, overlapping reads)
            # must not read as a conflict. Only genuine divergence in
            # the resolved content fails the proof.
            fingerprints = {
                item.model_dump_json(
                    exclude={
                        "assumptions",
                        "source_evidence_ids",
                        "conflicting_evidence_ids",
                        "node_id",
                    }
                )
                for item in resolved_candidates
            }
            if len(fingerprints) > 1:
                return (False, "conflicting matching analyzer resolutions")
        v2_any = (
            resolved_candidates[0]
            if resolved_candidates
            else (candidates[0] if candidates else None)
        )
        if v2_any is not None and v2_any.status != CodeFactStatus.RESOLVED:
            codes = ",".join(v2_any.limitation_codes) or "untyped_partial"
            return (False, f"resolution is {v2_any.status.value}: {codes}")
        resolution = VerificationEngine._v2_resolution_from_evidence(
            evidence_items, category, subject
        )
        if resolution is None:
            if v2_any is None:
                window_reason = VerificationEngine._window_analysis_reason(evidence_items)
                if window_reason is not None:
                    return (False, window_reason)
                return (False, "no resolved CodeFactResolutionV2 is bound to the cited evidence")
            return (False, f"resolution is {v2_any.status.value}: non-verifying status")
        if resolution.fact_category != category:
            return (False, "claim category does not match the resolved fact category")
        if target is not None:
            target_subject = str(getattr(target, "symbol", "") or "").strip()
            resolution_subjects = {
                str(getattr(resolution, name, "") or "").strip()
                for name in (
                    "subject_identity",
                    "target_identity",
                    "call_target",
                    "field_name",
                    "declaring_type",
                )
            }
            structured = resolution.structured_fact or {}
            if isinstance(structured, dict):
                for name in ("symbol", "method", "target"):
                    value = structured.get(name)
                    if value:
                        structured_subject = str(value).strip()
                        if not VerificationEngine._subject_identifies(
                            target_subject, {structured_subject}
                        ):
                            return (False, "claim target does not identify the structured fact")
                        resolution_subjects.add(structured_subject)
            if target_subject and not VerificationEngine._subject_identifies(
                target_subject, resolution_subjects
            ):
                return (False, "claim target does not identify the resolved fact")
        families = VerificationEngine._java_evidence_families(evidence_items, resolution)
        missing = set(rule.required_evidence_families) - families
        if missing:
            return (
                False,
                f"missing required evidence families: {sorted(missing)}",
            )
        if not rule.source_only_verifiable:
            has_runtime = "RUNTIME_OBSERVATION" in families
            if not has_runtime:
                return (
                    False,
                    f"{category.value} is never source-verifiable without runtime evidence",
                )
            if category == JavaFactCategory.CAUSAL_CLAIM:
                return (
                    False,
                    "causal claims require per-step bound evidence; never analyzer-verified",
                )
        if rule.requires_immutable_source_or_observation_identity:
            if category == JavaFactCategory.RUNTIME_OBSERVATION_FACT:
                if not resolution.runtime_source_identity or resolution.runtime_observed_at is None:
                    return (False, "runtime observation lacks source identity/time")
            else:
                if (
                    not resolution.revision
                    or resolution.revision in {"HEAD", "UNPINNED"}
                    or resolution.declaration_location is None
                    or not resolution.declaration_location.file_path
                    or not resolution.declaration_location.revision
                ):
                    return (False, "resolution lacks immutable source provenance")
        bound_ids = {
            str(value) for value in (getattr(hypothesis, "supporting_evidence_ids", None) or [])
        }
        if bound_ids and not bound_ids.intersection(
            str(eid) for eid in resolution.source_evidence_ids
        ):
            return (False, "resolution source evidence is not bound to the claim")
        if (
            rule.requires_source_window_for_each_location
            and resolution.declaration_location is not None
        ):
            matched = []
            expected_ids = {str(eid) for eid in resolution.source_evidence_ids}
            for item in evidence_items or []:
                if expected_ids and str(getattr(item, "evidence_id", "")) not in expected_ids:
                    continue
                loc_rev = str(
                    getattr(
                        getattr(getattr(item, "provenance", None), "source_location", None),
                        "revision",
                        None,
                    )
                    or ""
                )
                res_rev = str(getattr(resolution, "revision", "") or "")
                # Missing revision never matches (see _same_pinned_revision):
                # unknown is not equal to unknown, and never equal to a
                # pinned revision.
                if not _same_pinned_revision(loc_rev, res_rev):
                    continue
                if not VerificationEngine._paths_match(
                    str(resolution.declaration_location.file_path or ""),
                    VerificationEngine._item_file(item),
                ):
                    continue
                matched.append(item)
            if not matched:
                return (
                    False,
                    "resolution has no matching cited evidence at the same revision and path",
                )
            if not any(VerificationEngine._is_range_read(item) for item in matched):
                return (False, "resolved fact has no matching source range read")
            windowed = [i for i in matched if VerificationEngine._is_range_read(i)]
            contained = False
            for item in windowed:
                window = VerificationEngine._item_window(item)
                if window is None:
                    continue
                start = resolution.declaration_location.line_start
                end = resolution.declaration_location.line_end
                if (
                    start is not None
                    and end is not None
                    and window[0] <= start
                    and end <= window[1]
                ):
                    contained = True
                    break
            if not contained:
                return (False, "source read window does not contain the resolved declaration")
        if category in {
            JavaFactCategory.SOURCE_VALUE_FACT,
            JavaFactCategory.LANGUAGE_SEMANTIC_FACT,
            JavaFactCategory.DECLARATION_FACT,
        }:
            claimed_value = getattr(hypothesis, "claimed_value", None) or getattr(
                getattr(hypothesis, "claim_target", None), "claimed_value", None
            )
            target = getattr(hypothesis, "claim_target", None) or getattr(
                hypothesis, "target", None
            )
            if category == JavaFactCategory.SOURCE_VALUE_FACT:
                if claimed_value is None:
                    return (False, "source-value claim has no structured claimed_value")
                if resolution.resolved_value is None:
                    return (False, "resolved source fact has no comparable value")
                if VerificationEngine._bare_literal(
                    claimed_value
                ) != VerificationEngine._bare_literal(resolution.resolved_value):
                    return (
                        False,
                        f"claimed value {claimed_value!r} conflicts with resolved source value {resolution.resolved_value!r}",
                    )
                statement_value = VerificationEngine._claimed_literal_from_statement(
                    str(getattr(hypothesis, "statement", "") or "")
                )
                if statement_value is not None and statement_value != str(
                    resolution.resolved_value
                ):
                    return (
                        False,
                        f"claim statement conflicts with resolved source value {resolution.resolved_value!r}",
                    )
                subject = str(getattr(target, "symbol", "") or "") or str(
                    getattr(target, "subject_identity", "") or ""
                )
                expected_subjects = {
                    str(resolution.subject_identity or ""),
                    str(resolution.declaring_type or ""),
                    str(resolution.field_name or ""),
                    f"{resolution.declaring_type}.{resolution.field_name}"
                    if resolution.declaring_type and resolution.field_name
                    else "",
                }
                if subject and not VerificationEngine._subject_identifies(
                    subject, expected_subjects
                ):
                    return (False, "claim target does not identify the resolved subject")
        node_ok, node_reason = VerificationEngine._node_ids_match(hypothesis, resolution)
        if not node_ok:
            return (False, node_reason)
        return (True, "")

    @staticmethod
    def _java_partial_limitation(
        resolution: CodeFactResolutionV2 | None, reason: str
    ) -> VerificationLimitation:
        """Map PARTIAL/UNRESOLVED to a typed limitation (existing code set).

        Runtime-shaped abstentions map to NO_RUNTIME_OBSERVATION so next_check
        names the runtime observation; all other partials map to
        RELATION_UNDEMONSTRATED so the existing rival > relation > unmapped >
        provider priority in next_check_for applies unchanged. The class comes
        from the limitation registry, never substring sniffing.
        """
        from investigation_agent_platform.application.analysis.limitations import (
            LimitationClass,
            limitation_class,
        )

        lowered = f" {(reason or '').lower()} "
        codes_text = ",".join(resolution.limitation_codes) if resolution is not None else ""
        if resolution is not None and resolution.fact_category in {
            JavaFactCategory.RUNTIME_OBSERVATION_FACT,
            JavaFactCategory.CAUSAL_CLAIM,
        }:
            return VerificationLimitation(
                code=LimitationCode.NO_RUNTIME_OBSERVATION,
                detail=(
                    f"Java {resolution.fact_category.value} abstained ({codes_text or reason}); "
                    "runtime observation required."
                )[:500],
            )
        codes = [c for c in codes_text.split(",") if c] or ([reason] if reason else [])
        if any(limitation_class(code) is LimitationClass.RUNTIME for code in codes):
            return VerificationLimitation(
                code=LimitationCode.NO_RUNTIME_OBSERVATION,
                detail=(
                    f"Java fact abstained ({codes_text or reason}); runtime evidence required."
                )[:500],
            )
        if "runtime" in lowered and not codes_text:
            return VerificationLimitation(
                code=LimitationCode.NO_RUNTIME_OBSERVATION,
                detail=(
                    f"Java fact abstained ({codes_text or reason}); runtime evidence required."
                )[:500],
            )
        return VerificationLimitation(
            code=LimitationCode.RELATION_UNDEMONSTRATED,
            detail=(f"Java fact non-verifying ({codes_text or reason}).")[:500],
        )

    @staticmethod
    def _claimed_literal_from_statement(statement: str) -> str | None:
        match = re.search(
            r"(?i)(?:default\s+value|initial\s+value|field\s+value|value\s+of\s+(?:the\s+)?field|"
            r"initializer|initializes?\s+to|initialized\s+to|defaults?\s+to|assigned\s+to)"
            r"\s*(?:is|of|to|=)?\s*(['\"][^'\"]+['\"]|[-+]?\d+(?:\.\d+)?(?:[fFdDlL])?|true|false|null)",
            statement or "",
        )
        if match is None:
            # Confident fallback for "<trigger> of <symbol> is <literal>",
            # e.g. "The default value of x is 999". The symbol slot is a
            # single dotted identifier, optionally introduced by "the"
            # and/or "field" ("the initial value of the field X is ...").
            # A repo-relative path may sit between the symbol and "is"
            # ("of the field X in src/.../F.java is ...") — models cite
            # paths everywhere. Nothing else may intervene.
            match = re.search(
                r"(?i)(?:default\s+value|initial\s+value|field\s+value|value)"
                r"\s+of\s+(?:the\s+)?(?:field\s+)?[A-Za-z_][\w.]*"
                r"(?:\s+in\s+[A-Za-z0-9_.\-/]+)?\s+is\s+"
                r"(['\"][^'\"]+['\"]|[-+]?\d+(?:\.\d+)?(?:[fFdDlL])?|true|false|null)",
                statement or "",
            )
        if match is None:
            return None
        value = match.group(1).strip()
        if len(value) >= 2 and value[0] in {"'", '"'} and value[-1] == value[0]:
            return value[1:-1]
        return value

    @staticmethod
    def _bare_literal(value: Any) -> str:
        """Literal with one layer of surrounding quotes removed (pure).

        Models commit claimed_value with the quotes they see in the
        statement; the analyzer resolves the bare value. Both denote the
        same source literal — compare canonical forms, once, here.
        """
        try:
            text = str(value or "").strip()
            if len(text) >= 2 and text[0] in {"'", '"'} and text[-1] == text[0]:
                return text[1:-1]
            return text
        except Exception:
            return str(value)

    def validate_code_value_claim(
        self, hypothesis: Any, evidence_items: list[Any], contradictions: list[Any]
    ) -> VerificationResult | None:
        """Verify typed source-value claims against trusted analyzer output."""
        statement = str(getattr(hypothesis, "statement", "") or "")
        if not (
            self.claims_field_value(statement)
            or self._fact_category_of(hypothesis) == JavaFactCategory.SOURCE_VALUE_FACT
        ) or self.concedes_claim(statement):
            return None
        if any(
            getattr(c, "resolution_status", "") == "UNRESOLVED"
            and str(
                getattr(getattr(c, "severity", ""), "value", getattr(c, "severity", ""))
            ).upper()
            in {"HIGH", "CRITICAL"}
            for c in (contradictions or [])
        ):
            return None
        fact_category = self._fact_category_of(hypothesis)
        if fact_category is not None:
            return self.validate_java_fact_claim(hypothesis, evidence_items, contradictions)
        ok, reason = self.code_value_proof(hypothesis, evidence_items)
        resolution = self._resolution_from_evidence(evidence_items)
        if not ok or resolution is None:
            return None
        ids = [
            item.evidence_id
            for item in evidence_items
            if str(getattr(item, "evidence_id", ""))
            in {str(value) for value in resolution.source_evidence_ids}
        ]
        confidence = InvestigationConfidence(
            coverage_score=1.0,
            reliability_score=1.0,
            causal_score=0.5,
            contradiction_penalty=0.0,
        )
        return VerificationResult(
            hypothesis_id=hypothesis.id,
            status=VerificationStatus.VERIFIED,
            corroborating_evidence_ids=ids,
            explanation=(
                f"Source value {resolution.resolved_value!r} validated as "
                f"{resolution.value_semantics.value} for {resolution.declaring_type}."
            ),
            confidence=confidence,
            location_validated=True,
            code_value_validated=True,
        )

    def validate_java_fact_claim(
        self, hypothesis: Any, evidence_items: list[Any], contradictions: list[Any]
    ) -> VerificationResult | None:
        """Generalization of validate_code_value_claim (Java Phase 1, D7/D8).

        Typed fact_category claims use java_fact_proof with verdict ceilings;
        legacy claims without a category return None so only existing proof
        paths apply. PARTIAL/UNRESOLVED map to typed limitations (existing
        LimitationCode set) so next_check_for priority applies unchanged.
        Possible effects and call targets cap at LIKELY; runtime/causal never
        verify from source alone; unknown categories fail closed.
        """
        category = self._fact_category_of(hypothesis)
        if category is None:
            return None
        statement = str(getattr(hypothesis, "statement", "") or "")
        if self.concedes_claim(statement):
            return None
        if any(
            getattr(c, "resolution_status", "") == "UNRESOLVED"
            and str(
                getattr(getattr(c, "severity", ""), "value", getattr(c, "severity", ""))
            ).upper()
            in {"HIGH", "CRITICAL"}
            for c in (contradictions or [])
        ):
            return None
        target = getattr(hypothesis, "claim_target", None) or getattr(hypothesis, "target", None)
        claim_subject = str(getattr(target, "symbol", "") or "").strip() or None
        evidence_items = self._with_window_backfill(evidence_items, subject=claim_subject)
        subject = claim_subject
        v2_any = self._v2_any_from_evidence(evidence_items, category, subject)
        ok, reason = self.java_fact_proof(hypothesis, evidence_items)
        if not ok:
            return None
        resolution = self._v2_resolution_from_evidence(evidence_items, category, subject)
        if resolution is None or v2_any is None:
            return None
        rule = category_proof_rule(category)
        tier: str = "established"
        try:
            if rule is not None and rule.max_verdict == "LIKELY":
                tier = "likely"
        except Exception:
            tier = "likely"
        ids = [
            item.evidence_id
            for item in evidence_items
            if str(getattr(item, "evidence_id", ""))
            in {str(value) for value in resolution.source_evidence_ids}
        ]
        confidence = InvestigationConfidence(
            coverage_score=1.0,
            reliability_score=1.0,
            causal_score=0.5,
            contradiction_penalty=0.0,
        )
        return VerificationResult(
            hypothesis_id=hypothesis.id,
            status=VerificationStatus.VERIFIED,
            corroborating_evidence_ids=ids,
            explanation=(
                f"Java {category.value} validated "
                f"({resolution.value_semantics.value}) for "
                f"{resolution.subject_identity or resolution.field_name}."
            ),
            confidence=confidence,
            location_validated=True,
            code_value_validated=True,
            tier=tier,
        )

    def java_partial_result(
        self, hypothesis: Any, evidence_items: list[Any]
    ) -> VerificationResult | None:
        """INCONCLUSIVE with typed limitation for PARTIAL/UNRESOLVED v2 rows."""
        category = self._fact_category_of(hypothesis)
        if category is None:
            return None
        target = getattr(hypothesis, "claim_target", None) or getattr(hypothesis, "target", None)
        subject = str(getattr(target, "symbol", "") or "").strip() or None
        evidence_items = self._with_window_backfill(evidence_items, subject=subject)
        v2_any = self._v2_any_from_evidence(evidence_items, category, subject)
        if v2_any is not None and v2_any.status == CodeFactStatus.RESOLVED:
            return None
        ok, reason = self.java_fact_proof(hypothesis, evidence_items)
        if ok:
            return None
        if v2_any is None and self._window_analysis_of(evidence_items) is None:
            return None
        limitation = self._java_partial_limitation(v2_any, reason)
        try:
            hypothesis_id = hypothesis.id
        except Exception:
            return None
        return VerificationResult(
            hypothesis_id=hypothesis_id,
            status=VerificationStatus.INCONCLUSIVE,
            corroborating_evidence_ids=[],
            explanation=f"Java fact not established: {reason}.",
            confidence=InvestigationConfidence(
                coverage_score=0.0,
                reliability_score=0.0,
                causal_score=0.0,
                contradiction_penalty=0.0,
            ),
            limitations=[limitation],
        )

    @staticmethod
    def _item_window(item: Any) -> tuple[int, int] | None:
        """Actual (start_line, end_line) recorded in provenance, else None."""
        location = getattr(getattr(item, "provenance", None), "source_location", None)
        start = getattr(location, "line_start", None)
        end = getattr(location, "line_end", None)
        if isinstance(start, bool) or isinstance(end, bool):
            return None
        if not isinstance(start, int) or not isinstance(end, int):
            return None
        if start < 1 or end < start:
            return None
        return (start, end)

    @staticmethod
    def _item_file(item: Any) -> str:
        location = getattr(getattr(item, "provenance", None), "source_location", None)
        return str(getattr(location, "file_path", "") or "")

    @staticmethod
    def _item_type(item: Any) -> str:
        etype = getattr(item, "evidence_type", "")
        return str(getattr(etype, "value", etype) or "").upper()

    @staticmethod
    def _is_range_read(item: Any) -> bool:
        """One citation that is a real line-range read.

        Phase 0.4 labeled path-only reads; Phase 2.2 additionally requires
        the provenance to record an actual start/end line — the operation
        name alone (GET_CODE) is not evidence of a range.
        """
        try:
            provenance = getattr(item, "provenance", None)
            fingerprint = getattr(provenance, "query_fingerprint", None)
            operation = str(getattr(fingerprint, "operation", "") or "").upper()
            if operation not in VerificationEngine.GET_OPERATIONS:
                return False
            attributes = getattr(item, "attributes", None) or {}
            if not isinstance(attributes, dict):
                attributes = {}
            # J7 residual: a provenance window alone proves nothing - a
            # provider records the ACTUAL lines it returned even for an
            # un-ranged read (the bounded file head is lines 1..N). Only a
            # row whose requester labeled it ``read_mode == "range"`` is a
            # range read; unlabeled, path-only and discovery rows are not.
            if attributes.get("read_mode") != "range" or attributes.get("discovery_only"):
                return False
            return VerificationEngine._item_window(item) is not None
        except Exception:
            return False

    @staticmethod
    def caller_edge_binding_rows(
        evidence_items: list[Any],
        target: ClaimTarget | None,
        bound_set: list[Any],
    ) -> list[Any]:
        """Return exact, eligible range rows for one bound caller edge.

        ``bound_set`` is the evidence already bound to this claim. Its
        tenant/investigation and repository anchor the repair. A discovery row
        may identify the edge, but only a same-binding, same-revision range
        read whose window contains the target range can prove it. The
        revision pins from the claim target, else from the single bound
        citation (models rarely echo the revision into the claim target).
        """
        if target is None or not target.symbol or not target.counterpart:
            return []
        target_path = str(target.file_path or "").strip()
        target_revision = str(target.commit_id or "").strip()
        if not target_revision:
            # Live L2: a BOUND edge with null commit_id rejected an exact
            # same-revision range read. The claim is about the row it cites,
            # so the bound citation pins the revision. Still fail closed
            # when the bound rows carry no single revision.
            bound_revisions = {
                str(
                    getattr(
                        getattr(getattr(item, "provenance", None), "source_location", None),
                        "revision",
                        "",
                    )
                    or ""
                ).strip()
                for item in bound_set or []
            }
            bound_revisions.discard("")
            if len(bound_revisions) == 1:
                target_revision = next(iter(bound_revisions))
        target_start = target.line_start
        target_end = target.line_end
        if (
            not target_path
            or not target_revision
            or not isinstance(target_start, int)
            or isinstance(target_start, bool)
            or not isinstance(target_end, int)
            or isinstance(target_end, bool)
            or target_end < target_start
        ):
            return []

        def _binding(item: Any) -> tuple[str, str] | None:
            provenance = getattr(item, "provenance", None)
            tenant = str(getattr(item, "tenant_id", "") or "").strip()
            investigation = str(getattr(item, "investigation_id", "") or "").strip()
            provenance_tenant = str(getattr(provenance, "tenant_id", "") or "").strip()
            provenance_investigation = str(
                getattr(provenance, "investigation_id", "") or ""
            ).strip()
            if not tenant or not investigation:
                return None
            if (tenant, investigation) != (provenance_tenant, provenance_investigation):
                return None
            return (tenant, investigation)

        anchors = [_binding(item) for item in bound_set or []]
        if not anchors or any(binding is None for binding in anchors):
            return []
        if len(set(anchors)) != 1:
            return []
        binding = anchors[0]

        def _repository(item: Any) -> str:
            location = getattr(
                getattr(getattr(item, "provenance", None), "source_location", None),
                "file_path",
                "",
            )
            path = str(location or "").replace("\\", "/").strip().strip("/")
            for raw in (
                getattr(item, "source", None),
                getattr(
                    getattr(getattr(item, "provenance", None), "source_location", None),
                    "identifier",
                    None,
                ),
            ):
                value = str(raw or "").strip()
                if "://" in value:
                    value = value.split("://", 1)[1]
                value = value.split("@", 1)[0].strip("/")
                suffix = "/" + path
                if path and value.endswith(suffix):
                    return value[: -len(suffix)].strip("/").casefold()
                if value:
                    return value.casefold()
            return ""

        anchor_repositories = {_repository(item) for item in bound_set if _repository(item)}
        target_scope = str(target.scope or "").strip()
        target_repository = (
            target_scope.split("@", 1)[0].strip().casefold() if "@" in target_scope else ""
        )
        if target_repository:
            anchor_repositories.add(target_repository)
        if len(anchor_repositories) > 1:
            return []

        eligible: list[Any] = []
        contains_target: list[Any] = []
        for item in evidence_items or []:
            if _binding(item) != binding:
                continue
            location = getattr(
                getattr(getattr(item, "provenance", None), "source_location", None),
                "file_path",
                "",
            )
            if not VerificationEngine._paths_match(target_path, str(location or "")):
                continue
            revision = str(
                getattr(
                    getattr(getattr(item, "provenance", None), "source_location", None),
                    "revision",
                    "",
                )
                or ""
            ).strip()
            if not revision or revision.casefold() != target_revision.casefold():
                continue
            if anchor_repositories and _repository(item) not in anchor_repositories:
                continue
            if VerificationEngine._item_type(item) not in VerificationEngine.CODE_EVIDENCE_TYPES:
                continue
            if not VerificationEngine._is_range_read(item):
                continue
            attributes = getattr(item, "attributes", None) or {}
            if isinstance(attributes, dict):
                caller = str(attributes.get("caller_symbol", "") or "").strip()
                callee = str(attributes.get("callee_symbol", "") or "").strip()
                if caller and caller.casefold() != str(target.counterpart).casefold():
                    continue
                if callee and callee.casefold() != str(target.symbol).casefold():
                    continue
            eligible.append(item)
            window = VerificationEngine._item_window(item)
            if window is not None and window[0] <= target_start and window[1] >= target_end:
                contains_target.append(item)
        return contains_target if contains_target else []

    @staticmethod
    def has_range_read(evidence_items: list) -> bool:
        """At least one citation comes from an actual line-range read."""
        try:
            return any(VerificationEngine._is_range_read(i) for i in evidence_items or [])
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Phase 2.2: claim-kind verification. Each rule answers one question:
    # "does the CITED evidence demonstrate THIS kind of claim?" — never
    # "does evidence exist". Each returns (ok, reason); reason is the
    # operator-facing explanation when ok is False.
    # ------------------------------------------------------------------
    CALL_EDGE_PATTERNS = ("callers of", "caller of", "called by")
    RUNTIME_EVIDENCE_TYPES = frozenset(
        {
            "RUNTIME_LOG",
            "RUNTIME_TRACE",
            "LOG",
            "TRACE",
            "DATABASE_STATE",
            "METRIC",
            "SYSTEM_EVENT",
            "API_PROBE",
            "DIVERGENCE",
        }
    )
    CODE_EVIDENCE_TYPES = frozenset({"SOURCE_CODE", "EXCEPTION_PATH"})
    _DECLARATION_PREFIX = re.compile(
        r"(?:^|\s)(?:async\s+)?(?:def|class|function|func|fn|void|public|private|protected|static)\s+[\w.<>\[\], ]*$"
    )

    @staticmethod
    def effective_claim_kind(hypothesis: Any) -> ClaimKind | None:
        """Typed kind when recorded; otherwise inferred for the two kinds
        that have unambiguous phrasing (call edge, definition).

        BEHAVIOR and ABSENCE are never inferred from prose: guessing would
        reclassify legitimate causal statements ("the lock is never
        released") and block them. They apply only when typed.
        """
        typed = getattr(hypothesis, "claim_kind", None)
        if typed is not None:
            try:
                return ClaimKind(typed)
            except ValueError:
                return None
        statement = str(getattr(hypothesis, "statement", "") or "")
        lowered = f" {statement.lower()} "
        if any(pattern in lowered for pattern in VerificationEngine.CALL_EDGE_PATTERNS):
            return ClaimKind.CALL_EDGE
        if VerificationEngine.claims_definition(statement):
            return ClaimKind.DEFINITION
        return None

    @staticmethod
    def _symbol_in_text(symbol: str, text: str) -> bool:
        """Whole-identifier match (also the last segment of a qualified name)."""
        candidates = {symbol}
        for sep in ("::", ".", "#"):
            if sep in symbol:
                candidates.add(symbol.rsplit(sep, 1)[-1])
        for name in candidates:
            if not name:
                continue
            if re.search(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])", text or ""):
                return True
        return False

    @staticmethod
    def _call_shown(callee: str, snippet: str, *, exclude_construction: bool = False) -> bool:
        """True when the snippet contains a call expression for ``callee``.

        A declaration line (``def callee(``) matches ``callee(`` too, so
        declarations are excluded: a definition is not a call site.
        With ``exclude_construction``, ``new Callee(`` construction sites are
        also excluded: instantiating a class is not invoking it (live L2#2
        answered a "who calls" claim on field-construction lines alone).
        """
        names = {callee}
        for sep in ("::", ".", "#"):
            if sep in callee:
                names.add(callee.rsplit(sep, 1)[-1])
        for raw in (snippet or "").splitlines():
            line = (
                raw.split("| ", 1)[1]
                if "| " in raw and raw.split("|", 1)[0].strip().isdigit()
                else raw
            )
            for name in names:
                if not name:
                    continue
                for match in re.finditer(rf"(?<![A-Za-z0-9_]){re.escape(name)}\s*\(", line):
                    if VerificationEngine._DECLARATION_PREFIX.search(line[: match.start()]):
                        continue
                    if exclude_construction and re.search(r"\bnew\s*$", line[: match.start()]):
                        continue
                    return True
        return False

    @staticmethod
    def _instance_call_rows(callee_class: str, rows: list[Any]) -> list[Any]:
        """Rows proving invocation through a declared instance of ``callee_class``.

        A bare-class callee ("OrderServiceHandler") never names its method,
        so ``handler.handle(`` cannot match the class name. Instead join two
        same-file facts: a row declaring ``CalleeClass <var>`` and a row
        showing ``<var>.<method>(``. Both halves must be bound rows of one
        file — a declaration without a shown invocation proves nothing.
        """
        try:
            if not callee_class or any(sep in callee_class for sep in ("::", ".", "#")):
                return []
            # Declaration and invocation must come from one file — a field
            # typed X in file A never proves a call in file B.
            decl: dict[tuple[str, str], list[Any]] = {}
            for item in rows or []:
                try:
                    path = VerificationEngine._item_file(item)
                    text = str(getattr(item, "content_snippet", "") or "")
                    for raw in text.splitlines():
                        line = (
                            raw.split("| ", 1)[1]
                            if "| " in raw and raw.split("|", 1)[0].strip().isdigit()
                            else raw
                        )
                        for var_match in re.finditer(
                            rf"(?<![A-Za-z0-9_]){re.escape(callee_class)}\s+([A-Za-z_]\w*)\s*[=;]",
                            line,
                        ):
                            decl.setdefault((path, var_match.group(1)), []).append(item)
                except Exception:
                    continue
            if not decl:
                return []
            out: list[Any] = []
            seen: set[int] = set()
            for item in rows or []:
                try:
                    path = VerificationEngine._item_file(item)
                    text = str(getattr(item, "content_snippet", "") or "")
                    used: set[str] = set()
                    for raw in text.splitlines():
                        line = (
                            raw.split("| ", 1)[1]
                            if "| " in raw and raw.split("|", 1)[0].strip().isdigit()
                            else raw
                        )
                        for dpath, var in decl:
                            if dpath != path:
                                continue
                            if re.search(
                                rf"(?<![A-Za-z0-9_]){re.escape(var)}\s*\.\s*[A-Za-z_]\w*\s*\(",
                                line,
                            ):
                                used.add(var)
                    for var in used:
                        if id(item) not in seen:
                            seen.add(id(item))
                            out.append(item)
                        for row in decl.get((path, var), []):
                            if id(row) not in seen:
                                seen.add(id(row))
                                out.append(row)
                except Exception:
                    continue
            return out
        except Exception:
            return []

    _CALL_EDGE_PATTERNS_ALL: tuple[tuple[str, str], ...] = (
        # (regex, shape): shape "callee" returns (callee, None);
        # shape "edge" returns (callee, caller). Ordered most-specific first.
        # Strict table only: an optional "the" may precede either side
        # ("what calls the OrderService", "invoked by the schedule").
        # Only "the" — "a"/"an" stay ambiguous with single-letter names
        # ("called by A and B") and keep legacy behavior.
        # Legacy table below is untouched.
        (r"callers?\s+of\s+(?:the\s+)?{ident}", "callee"),
        (r"calls?\s+sites?\s+of\s+(?:the\s+)?{ident}", "callee"),
        (r"invocations?\s+of\s+(?:the\s+)?{ident}", "callee"),
        (r"what\s+(?:calls?|invokes?)\s+(?:the\s+)?{ident}", "callee"),
        (r"who\s+(?:calls?|invokes?)\s+(?:the\s+)?{ident}", "callee"),
        (r"where\s+is\s+(?:the\s+)?{ident}\s+(?:called|invoked)", "callee"),
        (
            (
                r"{ident}\s+(?:is\s+|are\s+)?(?:called|invoked)\s+by\s+"
                r"(?:the\s+)?{ident}"
            ),
            "edge",
        ),
    )
    # cbv1 legacy: preserved outputs for the pinned legacy cases
    # (I/O equivalence on the pinned list, not regex identity).
    _CALL_EDGE_PATTERNS_LEGACY: tuple[tuple[str, str], ...] = (
        (r"callers?\s+of\s+{ident}", "callee"),
        (r"{ident}\s+(?:is\s+|are\s+)?called\s+by\s+{ident}", "edge"),
    )

    # Conservative regex inference for every new D2 extraction form (D2).
    CALL_EDGE_INFERENCE_PATTERNS: tuple[str, ...] = (
        r"what\s+(?:calls?|invokes?)\s+[A-Za-z_][\w.:#]*",
        r"who\s+(?:calls?|invokes?)\s+[A-Za-z_][\w.:#]*",
        r"where\s+is\s+[A-Za-z_][\w.:#]*\s+(?:called|invoked)",
        r"calls?\s+sites?\s+of\s+[A-Za-z_][\w.:#]*",
        r"invocations?\s+of\s+[A-Za-z_][\w.:#]*",
        r"[A-Za-z_][\w.:#]*\s+(?:is\s+|are\s+)?invoked\s+by\s+[A-Za-z_][\w.:#]*",
    )

    @staticmethod
    def infers_call_edge(statement: str) -> bool:
        """True when untyped prose unambiguously asks a call-edge question."""
        lowered = f" {statement.lower()} "
        return any(
            re.search(pattern, lowered)
            for pattern in VerificationEngine.CALL_EDGE_INFERENCE_PATTERNS
        )

    @staticmethod
    def _looks_like_symbol(token: str, raw_match: str) -> bool:
        """Guard against extracting prose words as callees.

        D2 strict path only (legacy is untouched). Multi-word prose
        ("what calls the order service handler") must yield (None, None)
        — the typed path owns those — rather than a wrong callee like
        "the". Quoted, qualified, CamelCase, or snake_case tokens pass.
        """
        if not token:
            return False
        if any(f"{q}{token}{q}" in raw_match for q in ("`", "'", '"')):
            return True
        return any(c.isupper() or c in ".:#_" for c in token)

    @staticmethod
    def _extract_call_edge(
        statement: str, *, legacy: bool = False
    ) -> tuple[str | None, str | None]:
        """(callee, caller) from untyped prose; (None, None) when unclear."""
        text = statement or ""
        ident = r"[`'\"]?([A-Za-z_][\w.:#]*)[`'\"]?"
        if legacy:
            rows = VerificationEngine._CALL_EDGE_PATTERNS_LEGACY
            for raw, shape in rows:
                m = re.search(raw.format(ident=ident), text, re.IGNORECASE)
                if not m:
                    continue
                if shape == "edge":
                    return (m.group(1), m.group(2))
                return (m.group(1), None)
            return (None, None)
        for raw, shape in VerificationEngine._CALL_EDGE_PATTERNS_ALL:
            m = re.search(raw.format(ident=ident), text, re.IGNORECASE)
            if not m:
                continue
            if shape == "edge":
                if not VerificationEngine._looks_like_symbol(m.group(1), m.group(0)):
                    continue
                return (m.group(1), m.group(2))
            if not VerificationEngine._looks_like_symbol(m.group(1), m.group(0)):
                continue
            return (m.group(1), None)
        return (None, None)

    @staticmethod
    def resolve_claim_kind(hypothesis: Any, *, strict: bool) -> ClaimKind | None:
        """Version-aware kind resolution.

        strict=False reproduces effective_claim_kind exactly (legacy
        inference only). strict=True additionally applies infers_call_edge.
        effective_claim_kind itself stays byte-identical forever (legacy
        inference only); the seam wraps it.
        """
        kind = VerificationEngine.effective_claim_kind(hypothesis)
        if kind is not None:
            return kind
        if not strict:
            return None
        statement = str(getattr(hypothesis, "statement", "") or "")
        if VerificationEngine.infers_call_edge(statement):
            return ClaimKind.CALL_EDGE
        return None

    @staticmethod
    def _item_revision(item: Any) -> str:
        location = getattr(getattr(item, "provenance", None), "source_location", None)
        return str(getattr(location, "revision", "") or "")

    @staticmethod
    def _revision_pinned(revision: Any) -> bool:
        """A row is pinned iff its revision names an immutable commit.

        Empty, HEAD, and UNPINNED reads assert nothing (Phase 4.2) — they
        can orient but never bind.
        """
        rev = str(revision or "").strip()
        return bool(rev) and rev not in {"HEAD", "UNPINNED"}

    @staticmethod
    def definition_window_proof(
        evidence_items: list[Any], target: ClaimTarget | None
    ) -> tuple[bool, str]:
        """A real range read whose window contains the defined symbol."""
        reads = [i for i in evidence_items or [] if VerificationEngine._is_range_read(i)]
        if not reads:
            return (
                False,
                "no range read with recorded start/end lines (a path-only or head read is discovery)",
            )
        # P3 (S6 binding): the claimed region must sit in a cited pinned
        # row at the cited revision. An unpinned read (HEAD/empty) asserts
        # nothing — it orients, never binds.
        pinned = [
            i
            for i in reads
            if VerificationEngine._revision_pinned(VerificationEngine._item_revision(i))
        ]
        if not pinned:
            return (
                False,
                "no pinned range read (HEAD/empty revision asserts nothing)",
            )
        reads = pinned
        if target is not None and target.commit_id:
            want = str(target.commit_id).strip().casefold()
            reads = [
                i for i in reads if VerificationEngine._item_revision(i).strip().casefold() == want
            ]
            if not reads:
                return (False, f"no range read at cited revision {target.commit_id}")
        if target is not None and target.file_path:
            reads = [
                i
                for i in reads
                if VerificationEngine._paths_match(
                    target.file_path, VerificationEngine._item_file(i)
                )
            ]
            if not reads:
                return (False, f"no range read of {target.file_path}")
        if target is not None and target.line_start:
            line = target.line_start
            reads = [
                i
                for i in reads
                if (w := VerificationEngine._item_window(i)) and w[0] <= line <= w[1]
            ]
            if not reads:
                return (False, f"no read window covers line {line}")
        if target is not None and target.symbol:
            reads = [
                i
                for i in reads
                if VerificationEngine._symbol_in_text(
                    target.symbol, str(getattr(i, "content_snippet", "") or "")
                )
            ]
            if not reads:
                return (False, f"no read window contains symbol '{target.symbol}'")
        return (True, "")

    @staticmethod
    def call_site_proof(
        evidence_items: list[Any],
        target: ClaimTarget | None,
        statement: str,
        *,
        legacy_extraction: bool,
    ) -> tuple[bool, str]:
        """A cited code row that shows the call expression itself."""
        callee = target.symbol if target is not None else None
        caller = target.counterpart if target is not None else None
        if not callee:
            callee, extracted_caller = VerificationEngine._extract_call_edge(
                statement, legacy=legacy_extraction
            )
            caller = caller or extracted_caller
        if not callee:
            return (False, "call edge names no identifiable callee (set target.symbol)")
        rows = [
            i
            for i in VerificationEngine._cited_location_evidence(evidence_items)
            if VerificationEngine._item_window(i) is not None
        ]
        if not rows:
            return (False, "no cited code row with file, line range and revision provenance")
        bare = not any(sep in (callee or "") for sep in ("::", ".", "#"))
        shown = [
            i
            for i in rows
            if VerificationEngine._call_shown(
                callee,
                str(getattr(i, "content_snippet", "") or ""),
                exclude_construction=bare,
            )
        ]
        if not shown and bare:
            # Bare-class callee ("OrderServiceHandler") never names its
            # method, so constructor lines alone prove nothing — but an
            # invocation through a declared instance does. Join the
            # declaration row with the invocation row (same file).
            shown = VerificationEngine._instance_call_rows(callee, rows)
        if not shown:
            return (
                False,
                f"no cited row shows a call to '{callee}' (a search hit or graph row that does not show the call is not a call site)",
            )
        if caller:
            shown = [
                i
                for i in shown
                if VerificationEngine._symbol_in_text(
                    caller.rsplit(".", 1)[-1], str(getattr(i, "content_snippet", "") or "")
                )
                or VerificationEngine._symbol_in_text(
                    caller.rsplit(".", 1)[-1], VerificationEngine._item_file(i)
                )
            ]
            if not shown:
                return (False, f"call to '{callee}' is shown but not from '{caller}'")
        return (True, "")

    @staticmethod
    def behavior_proof(evidence_items: list[Any]) -> tuple[bool, str]:
        """Code (range-read) AND runtime evidence — code alone is structure."""
        has_code = VerificationEngine.behavior_has_code_path(evidence_items)
        has_runtime = any(
            VerificationEngine._item_type(i) in VerificationEngine.RUNTIME_EVIDENCE_TYPES
            for i in evidence_items or []
        )
        if has_code and has_runtime:
            return (True, "")
        missing = []
        if not has_code:
            missing.append("a code range read")
        if not has_runtime:
            missing.append("runtime evidence (log, trace, state, metric or probe)")
        return (False, "behavior claim lacks " + " and ".join(missing))

    @staticmethod
    def behavior_has_code_path(evidence_items: list[Any]) -> bool:
        """A real code range read exists (the code half of a behavior claim)."""
        try:
            return any(
                VerificationEngine._item_type(i) in VerificationEngine.CODE_EVIDENCE_TYPES
                and VerificationEngine._is_range_read(i)
                for i in evidence_items or []
            )
        except Exception:
            return False

    @staticmethod
    def absence_outcome(evidence_items: list[Any], target: ClaimTarget | None) -> tuple[str, str]:
        """Classify whether bound evidence can support an absence claim.

        - NO_SCOPE: claim states no search scope (target-resolution failure).
        - EMPTY_SEARCH_PROVED: eligible scoped match_count==0 row covers the claim.
        - NO_SEARCH: bound set holds no empty-search rows at all (retrieval
          failure — never invent "nothing changed" from this).
        - SCOPE_MISMATCH: empty-search rows exist but none covers the claim
          (target-resolution failure).
        - DISCOVERY_ONLY: matching empty rows exist, but they are discovery
          material and cannot establish absence.
        - PRESENCE_CONFLICT: same-scope, same-binding positive evidence defeats
          the absence claim without proving the positive claim by itself.
        """
        scope = (getattr(target, "scope", "") or "").strip() if target is not None else ""
        if not scope:
            return ("NO_SCOPE", "absence claim states no search scope (set target.scope)")

        def _scope(item: Any) -> str:
            attributes = getattr(item, "attributes", None) or {}
            if not isinstance(attributes, dict):
                return ""
            return str(attributes.get("search_scope", "") or "").strip()

        def _binding(item: Any) -> tuple[str, str] | None:
            tenant = str(getattr(item, "tenant_id", "") or "").strip()
            investigation = str(getattr(item, "investigation_id", "") or "").strip()
            provenance = getattr(item, "provenance", None)
            if not tenant:
                tenant = str(getattr(provenance, "tenant_id", "") or "").strip()
            if not investigation:
                investigation = str(getattr(provenance, "investigation_id", "") or "").strip()
            if not tenant or not investigation:
                return None
            return (tenant, investigation)

        def _same_binding(left: Any, right: Any) -> bool:
            left_binding = _binding(left)
            right_binding = _binding(right)
            return left_binding is not None and left_binding == right_binding

        def _attributes(item: Any) -> dict[str, Any]:
            attributes = getattr(item, "attributes", None) or {}
            return attributes if isinstance(attributes, dict) else {}

        def _is_zero(attributes: dict[str, Any]) -> bool:
            count = attributes.get("match_count")
            return isinstance(count, int) and not isinstance(count, bool) and count == 0

        def _is_positive(attributes: dict[str, Any]) -> bool:
            count = attributes.get("match_count")
            return isinstance(count, int) and not isinstance(count, bool) and count > 0

        scoped_rows: list[Any] = []
        discovery_rows: list[Any] = []
        for item in evidence_items or []:
            attributes = _attributes(item)
            proof_scope = _scope(item)
            if not _is_zero(attributes) or not proof_scope:
                continue
            if attributes.get("discovery_only"):
                discovery_rows.append(item)
            else:
                scoped_rows.append(item)
        if not scoped_rows:
            if discovery_rows and any(
                _scope(item).casefold() == scope.casefold() for item in discovery_rows
            ):
                return (
                    "DISCOVERY_ONLY",
                    "ABSENCE_PROOF_DISCOVERY_ONLY: matching empty-search evidence is discovery-only and cannot prove absence",
                )
            return (
                "NO_SEARCH",
                "no empty-search evidence bound; do not conclude absence from retrieval silence",
            )

        for empty_row in scoped_rows:
            if _scope(empty_row).casefold() != scope.casefold():
                continue
            for positive_row in evidence_items or []:
                if positive_row is empty_row or not _same_binding(empty_row, positive_row):
                    continue
                positive_attributes = _attributes(positive_row)
                if (
                    _scope(positive_row).casefold() == scope.casefold()
                    and _is_positive(positive_attributes)
                    and not positive_attributes.get("discovery_only")
                ):
                    return (
                        "PRESENCE_CONFLICT",
                        "ABSENCE_CONTRADICTED_BY_PRESENCE: same-scope positive evidence contradicts the absence claim",
                    )
            return ("EMPTY_SEARCH_PROVED", "")
        return (
            "SCOPE_MISMATCH",
            f"no scoped empty-search evidence covers '{scope}'; an empty result alone is not proof of absence",
        )

    @staticmethod
    def absence_proof(evidence_items: list[Any], target: ClaimTarget | None) -> tuple[bool, str]:
        """Scoped empty search: states its scope and records zero matches.

        Accepted proof shape (produced by typed no-match outcomes): an
        evidence row with ``attributes.search_scope`` and
        ``attributes.match_count == 0`` whose scope covers the claimed one.
        """
        outcome, reason = VerificationEngine.absence_outcome(evidence_items, target)
        if outcome == "EMPTY_SEARCH_PROVED":
            return (True, "")
        return (False, reason)

    def check_claim_kind(
        self, hypothesis: Any, evidence_items: list[Any]
    ) -> tuple[bool, str] | None:
        """Claim-kind gate. None means no kind-specific rule applies."""
        strict = self.claim_binding_version in (
            CLAIM_BINDING_CBV2,
            CLAIM_BINDING_CBV3,
        )
        kind = VerificationEngine.resolve_claim_kind(hypothesis, strict=strict)
        legacy_extraction = not strict
        if kind is None or kind == ClaimKind.CAUSAL:
            return None
        # Phase 4 (defense in depth): NOT_CHECKABLE never verifies. The
        # primary skip is the evaluate-level projection, so no opinion
        # group should reach a gate; a (False, ...) return routes to the
        # existing gate-fail INCONCLUSIVE path. (True, ...) is forbidden
        # here — it would VERIFY an opinion claim.
        if kind == ClaimKind.NOT_CHECKABLE:
            return (False, LimitationCode.NOT_CHECKABLE_SKIPPED.value)
        target = getattr(hypothesis, "claim_target", None)
        statement = str(getattr(hypothesis, "statement", "") or "")
        if kind == ClaimKind.DEFINITION:
            return VerificationEngine.definition_window_proof(evidence_items, target)
        if kind == ClaimKind.CALL_EDGE:
            edge_assessments = [
                assessment
                for assessment in (getattr(hypothesis, "assessments", None) or [])
                if getattr(assessment, "claim_kind", None) == ClaimKind.CALL_EDGE
                and getattr(assessment, "claim_target", None) is not None
            ]
            if edge_assessments:
                unresolved = [
                    assessment
                    for assessment in edge_assessments
                    if getattr(assessment, "evidence_id", None) is None
                ]
                if unresolved:
                    first_target = getattr(unresolved[0], "claim_target", None)
                    missing = (
                        self.caller_edge_key(first_target)
                        if first_target is not None
                        else "unknown"
                    )
                    return (
                        False,
                        f"CALL_EDGE_INCOMPLETE_ENUMERATION: unresolved edge {missing}",
                    )
                evidence_by_id = {
                    str(getattr(item, "evidence_id", "")): item for item in evidence_items or []
                }
                proven_callers: set[str] = set()
                for assessment in edge_assessments:
                    if getattr(assessment, "evidence_id", None) is None:
                        continue
                    row = evidence_by_id.get(str(assessment.evidence_id))
                    if row is None:
                        return (False, "a caller edge's bound call-site evidence is unavailable")
                    edge_targets = VerificationEngine._aggregate_call_edge_targets(
                        assessment.claim_target
                    ) or [assessment.claim_target]
                    matched_targets: list[ClaimTarget] = []
                    for edge_target in edge_targets:
                        _rows = VerificationEngine.caller_edge_binding_rows(
                            evidence_items,
                            edge_target,
                            [row],
                        )
                        if not _rows:
                            return (
                                False,
                                "CALL_EDGE_INCOMPLETE_ENUMERATION: no exact bound range read for edge",
                            )
                        # A same-file sibling range read can repair a
                        # cited discovery row, but it must still prove this
                        # specific caller edge at the same binding, revision,
                        # repository, and target range.
                        edge_ok, edge_reason = VerificationEngine.call_site_proof(
                            _rows,
                            edge_target,
                            statement,
                            legacy_extraction=legacy_extraction,
                        )
                        if not edge_ok:
                            return (
                                False,
                                edge_reason,
                            )
                        matched_targets.append(edge_target)
                    for matched_target in matched_targets:
                        _cp = str(getattr(matched_target, "counterpart", "") or "")
                        for _sep in ("::", "#"):
                            _cp = _cp.replace(_sep, ".")
                        for _part in _cp.split("."):
                            _part = _part.strip()
                            if _part:
                                proven_callers.add(_part.casefold())
                named_callers: list[str] = []
                for item in evidence_items or []:
                    if (
                        VerificationEngine._item_type(item)
                        not in VerificationEngine.CODE_EVIDENCE_TYPES
                    ):
                        continue
                    if VerificationEngine._item_window(item) is None:
                        continue
                    # D3 (claim-proof gaps): construction lines never name a
                    # caller for enumeration — the single site where "shown"
                    # meant something weaker than call_site_proof. Under cbv1
                    # the current enumeration result is preserved.
                    bare_symbol = not any(
                        sep in (str(getattr(target, "symbol", "") or ""))
                        for sep in ("::", ".", "#")
                    )
                    if not VerificationEngine._call_shown(
                        str(getattr(target, "symbol", "") or ""),
                        str(getattr(item, "content_snippet", "") or ""),
                        exclude_construction=(bare_symbol and strict),
                    ):
                        continue
                    file_stem = (
                        VerificationEngine._item_file(item).rsplit("/", 1)[-1].rsplit(".", 1)[0]
                    )
                    if file_stem and re.search(
                        rf"(?<![A-Za-z0-9_]){re.escape(file_stem)}(?![A-Za-z0-9_])",
                        statement,
                    ):
                        named_callers.append(file_stem.casefold())
                if set(named_callers) - proven_callers:
                    return (
                        False,
                        "caller enumeration names a caller without its own CALL_EDGE claim",
                    )
                required = self.required_caller_edge_keys(hypothesis)
                proven_keys = {
                    self.caller_edge_key(a.claim_target)
                    for a in edge_assessments
                    if getattr(a, "evidence_id", None) is not None
                    and getattr(a, "claim_target", None) is not None
                }
                if required - proven_keys:
                    missing = min(required - proven_keys)
                    return (False, f"CALL_EDGE_INCOMPLETE_ENUMERATION: unresolved edge {missing}")
                return (True, "")
            aggregate_edges = VerificationEngine._aggregate_call_edges(hypothesis)
            if target is None:
                return VerificationEngine.call_site_proof(
                    evidence_items,
                    target,
                    statement,
                    legacy_extraction=legacy_extraction,
                )
            anchor_ids = {
                str(value)
                for value in (getattr(hypothesis, "supporting_evidence_ids", None) or [])
                if str(value or "").strip()
            }
            if aggregate_edges:
                for edge in aggregate_edges:
                    rows = VerificationEngine.caller_edge_binding_rows(
                        evidence_items,
                        edge,
                        [
                            item
                            for item in evidence_items
                            if str(getattr(item, "evidence_id", "") or "") in anchor_ids
                        ],
                    )
                    if not rows:
                        return (
                            False,
                            "CALL_EDGE_INCOMPLETE_ENUMERATION: no exact bound range read for edge",
                        )
                    edge_ok, edge_reason = VerificationEngine.call_site_proof(
                        rows, edge, statement, legacy_extraction=legacy_extraction
                    )
                    if not edge_ok:
                        return (False, edge_reason)
                return (True, "")
            rows = VerificationEngine.caller_edge_binding_rows(
                evidence_items,
                target,
                [
                    item
                    for item in evidence_items
                    if str(getattr(item, "evidence_id", "") or "") in anchor_ids
                ],
            )
            if not rows:
                return (
                    False,
                    "CALL_EDGE_INCOMPLETE_ENUMERATION: no exact bound range read for edge",
                )
            return VerificationEngine.call_site_proof(
                rows, target, statement, legacy_extraction=legacy_extraction
            )
        if kind == ClaimKind.BEHAVIOR:
            return VerificationEngine.behavior_proof(evidence_items)
        if kind == ClaimKind.ABSENCE:
            return VerificationEngine.absence_proof(evidence_items, target)
        if kind == ClaimKind.HISTORY:
            return VerificationEngine.history_proof(
                evidence_items, target, self.claim_binding_version
            )
        return None

    def evaluate_caller_edge(
        self, hypothesis: Any, evidence_items: list[Any]
    ) -> tuple[bool, str] | None:
        """Evaluate one caller-edge claim for both verify and conclude.

        This is the single caller-edge decision seam. It preserves the
        existing ``None`` contract for non-caller claims while ensuring both
        lifecycle paths consume the same exact-binding and enumeration result.
        """
        if (
            VerificationEngine.resolve_claim_kind(
                hypothesis,
                strict=self.claim_binding_version
                in (
                    CLAIM_BINDING_CBV2,
                    CLAIM_BINDING_CBV3,
                ),
            )
            != ClaimKind.CALL_EDGE
        ):
            return None
        return self.check_claim_kind(hypothesis, evidence_items)

    @staticmethod
    def _aggregate_call_edges(hypothesis: Any) -> list[ClaimTarget]:
        """Detect claims whose one target actually asserts several callers."""
        target = getattr(hypothesis, "claim_target", None)
        return VerificationEngine._aggregate_call_edge_targets(target)

    @staticmethod
    def _aggregate_call_edge_targets(target: ClaimTarget | None) -> list[ClaimTarget]:
        """Split an aggregate caller target into independently checked edges."""
        if target is None or not getattr(target, "counterpart", None):
            return []
        counterpart = str(target.counterpart)
        names = [
            segment.strip(" ,;:")
            for segment in re.split(r"\s+(?:and|or)\s+|[,;]", counterpart, flags=re.IGNORECASE)
        ]
        names = [name for name in names if name]
        if len(names) < 2:
            return []
        return [target.model_copy(update={"counterpart": name}) for name in names]

    # Phase 1 (claim binding): frozen HISTORY subject comparison.
    _COMMIT_TYPE_PREFIX = re.compile(r"^[a-z][a-z0-9_-]*(\([^)]*\))?(!)?:\s+")
    # 0.70 operating point, pinned by unit vectors: one-word insertions in
    # >=4-token subjects pass (motivating rewording scores 0.75); one-word
    # insertions in <=3-token subjects fail (generic collision scores
    # 0.667); any single-token drift fails (<=0.5).
    _SUBJECT_TOKEN_THRESHOLD = 0.70

    @staticmethod
    def _canonical_subject(text: Any) -> str:
        """Canonicalize a commit subject for deterministic comparison.

        Fixed order, pinned by unit vectors: casefold, trim, balanced
        outer-quote strip (one layer, matching quote kinds only),
        conventional-commit type-prefix strip, whitespace collapse.
        """
        lowered = str(text or "").casefold().strip()
        if len(lowered) >= 2 and lowered[0] == lowered[-1] and lowered[0] in {"'", '"'}:
            lowered = lowered[1:-1].strip()
        lowered = VerificationEngine._COMMIT_TYPE_PREFIX.sub("", lowered)
        return re.sub(r"\s+", " ", lowered).strip()

    @staticmethod
    def _subject_tokens(canonical: str) -> frozenset[str]:
        return frozenset(re.findall(r"[a-z0-9]+", canonical or ""))

    @staticmethod
    def subject_match_outcome(claimed: Any | None, stored: Any | None) -> str:
        """Closed-vocabulary HISTORY subject outcome for telemetry.

        One of matched_exact | matched_token_set | mismatched_subject |
        missing_subject | missing_row. Pure function; mirrors
        subjects_match so counters and gates can never disagree.
        """
        try:
            left = VerificationEngine._canonical_subject(claimed)
            right = VerificationEngine._canonical_subject(stored)
        except Exception:
            return "mismatched_subject"
        if not left:
            return "missing_subject"
        if not right:
            return "missing_row"
        if left == right:
            return "matched_exact"
        if VerificationEngine.subjects_match(claimed, stored):
            return "matched_token_set"
        return "mismatched_subject"

    @staticmethod
    def subjects_match(claimed: Any | None, stored: Any | None) -> bool:
        """Deterministic structured-subject comparison for HISTORY grounding.

        Exact match after canonicalization wins. Otherwise token-set
        Jaccard at or above the frozen threshold wins. Empty either side
        never matches. Pure function; no model prose is consulted.
        """
        left = VerificationEngine._canonical_subject(claimed)
        right = VerificationEngine._canonical_subject(stored)
        if not left or not right:
            return False
        if left == right:
            return True
        left_tokens = VerificationEngine._subject_tokens(left)
        right_tokens = VerificationEngine._subject_tokens(right)
        if not left_tokens or not right_tokens:
            return False
        union = left_tokens | right_tokens
        if not union:
            return False
        return (len(left_tokens & right_tokens) / len(union)) >= (
            VerificationEngine._SUBJECT_TOKEN_THRESHOLD
        )

    @staticmethod
    def history_proof(
        evidence_items: list[Any],
        target: ClaimTarget | None,
        claim_binding_version: str = CLAIM_BINDING_CURRENT,
    ) -> tuple[bool, str]:
        """A cited commit row must identify the claimed file and commit.

        Under pin ``cbv2`` the row must additionally match the structured
        ``target.commit_subject`` (deterministic comparison, never
        freeform-prose substring); an empty structured subject never
        matches. Under ``cbv1``/unmarked the legacy predicate applies
        byte-identically (path + commit prefix + non-empty description).
        """
        if target is None or not target.file_path or not target.commit_id:
            return (False, "history claim must identify target.file_path and target.commit_id")
        strict_subject = claim_binding_version in (CLAIM_BINDING_CBV2, CLAIM_BINDING_CBV3)
        if strict_subject and not str(getattr(target, "commit_subject", "") or "").strip():
            return (False, "history claim must identify target.commit_subject")
        path_rows: list[Any] = []
        matching_rows: list[Any] = []
        for item in evidence_items or []:
            if VerificationEngine._item_type(item) != "COMMIT_HISTORY":
                continue
            try:
                _attrs = getattr(item, "attributes", None) or {}
                if not isinstance(_attrs, dict):
                    _attrs = {}
                if _attrs.get("discovery_only") is True:
                    continue
                if _attrs.get("match_count") == 0:
                    continue
            except Exception:
                pass
            if not VerificationEngine._paths_match(
                target.file_path, VerificationEngine._item_file(item)
            ):
                continue
            path_rows.append(item)
            revision = str(
                getattr(
                    getattr(getattr(item, "provenance", None), "source_location", None),
                    "revision",
                    "",
                )
                or ""
            )
            commit_id = target.commit_id.lower()
            revision_lower = revision.lower()
            if not (revision_lower.startswith(commit_id) or commit_id.startswith(revision_lower)):
                continue
            content = "\n".join(
                str(getattr(item, field, "") or "")
                for field in ("title", "summary", "content_snippet", "source")
            )
            # Require a meaningful commit description in the same row;
            # matching a path and hash alone does not ground "what changed".
            # Under cbv2 the description must match the structured subject.
            description = str(getattr(item, "summary", "") or "").strip()
            if strict_subject and not VerificationEngine.subjects_match(
                getattr(target, "commit_subject", ""), description
            ):
                continue
            if description and content.strip():
                matching_rows.append(item)
        if path_rows and matching_rows:
            newest = max(
                path_rows,
                key=lambda item: (
                    getattr(item, "observed_at", None)
                    or getattr(item, "retrieved_at", None)
                    or datetime.min.replace(tzinfo=UTC)
                ),
            )
            if newest in matching_rows:
                return (True, "")
        return (
            False,
            f"no cited COMMIT_HISTORY row matches {target.file_path} at commit {target.commit_id}",
        )

    @staticmethod
    def history_absence_outcome(
        evidence_items: list[Any], target: ClaimTarget | None
    ) -> tuple[str, str, list[Any]]:
        """Negative HISTORY finding: inspected-empty vs incomplete vs no search.

        Returns (outcome, reason, qualifying_rows). INSPECTED_EMPTY means a
        scoped history search ran and matched nothing: >=1 non-discovery
        COMMIT_HISTORY row with match_count == 0 on the target file and a
        non-empty searched-tip revision. A non-discovery positive row for
        the same file blocks absence (SEARCH_INCOMPLETE): an empty search
        never overrides existing positive history evidence. NO_SEARCH
        covers no history retrieved at all. Both non-proof states carry the
        reason for the caller to fall through, never to conclude from silence.
        """
        try:
            if target is None or not getattr(target, "file_path", ""):
                return ("NO_SEARCH", "history absence needs a file target", [])
            if getattr(target, "commit_id", ""):
                return (
                    "SEARCH_INCOMPLETE",
                    "commit-scoped absence needs range proof (deferred); file-level only",
                    [],
                )
            file_hits: list[Any] = []
            saw_file_rows = False
            for item in evidence_items or []:
                if VerificationEngine._item_type(item) != "COMMIT_HISTORY":
                    continue
                if not VerificationEngine._paths_match(
                    str(target.file_path or ""), VerificationEngine._item_file(item)
                ):
                    continue
                saw_file_rows = True
                try:
                    attrs = getattr(item, "attributes", None) or {}
                    if not isinstance(attrs, dict):
                        attrs = {}
                    if attrs.get("discovery_only") is True:
                        continue
                except Exception:
                    continue
                file_hits.append(item)
            if not file_hits:
                if saw_file_rows:
                    return (
                        "SEARCH_INCOMPLETE",
                        (
                            "only discovery history rows exist for the file; "
                            "no inspected-empty search covers the claimed scope"
                        ),
                        [],
                    )
                return (
                    "NO_SEARCH",
                    "no COMMIT_HISTORY rows retrieved; do not conclude absence",
                    [],
                )
            for item in file_hits:
                try:
                    attrs = getattr(item, "attributes", None) or {}
                    count = attrs.get("match_count") if isinstance(attrs, dict) else None
                except Exception:
                    count = None
                if isinstance(count, int) and not isinstance(count, bool) and count != 0:
                    return (
                        "SEARCH_INCOMPLETE",
                        "positive history evidence exists for the file; absence blocked",
                        [],
                    )
            qualifying: list[Any] = []
            for item in file_hits:
                try:
                    attrs = getattr(item, "attributes", None) or {}
                    count = attrs.get("match_count") if isinstance(attrs, dict) else None
                except Exception:
                    count = None
                if not (isinstance(count, int) and not isinstance(count, bool) and count == 0):
                    continue
                try:
                    row_revision = str(
                        getattr(
                            getattr(getattr(item, "provenance", None), "source_location", None),
                            "revision",
                            "",
                        )
                        or ""
                    )
                except Exception:
                    row_revision = ""
                if not row_revision:
                    continue
                qualifying.append(item)
            if qualifying:
                return ("INSPECTED_EMPTY", "", qualifying)
            return (
                "SEARCH_INCOMPLETE",
                (
                    "history rows exist for the file but no inspected-empty search "
                    "covers the claimed scope"
                ),
                [],
            )
        except Exception:
            return ("NO_SEARCH", "history absence evaluation failed closed", [])

    def validate_history_claim(
        self, hypothesis: Any, evidence_items: list[Any], contradictions: list[Any]
    ) -> VerificationResult | None:
        """Verify a recent-change statement against its exact commit subject."""
        if VerificationEngine.effective_claim_kind(hypothesis) != ClaimKind.HISTORY:
            return None
        statement = str(getattr(hypothesis, "statement", "") or "")
        if self.concedes_claim(statement):
            return None
        if any(
            getattr(c, "resolution_status", "") == "UNRESOLVED"
            and str(
                getattr(getattr(c, "severity", ""), "value", getattr(c, "severity", ""))
            ).upper()
            in {"HIGH", "CRITICAL"}
            for c in contradictions or []
        ):
            return None
        target = getattr(hypothesis, "claim_target", None)
        confidence = InvestigationConfidence(
            coverage_score=1.0,
            reliability_score=1.0,
            causal_score=0.5,
            contradiction_penalty=0.0,
        )
        # D1 (claim-proof gaps): file-level history absence is evaluated
        # BEFORE the positive history_proof prerequisite, which rejects
        # commit-less targets — placing absence after it would make the
        # new verdict unreachable. Commit-naming targets skip absence and
        # continue to the positive path, where they may still verify a
        # positive commit claim. cbv1 keeps today's fall-through.
        if self.claim_binding_version in (CLAIM_BINDING_CBV2, CLAIM_BINDING_CBV3) and not getattr(
            target, "commit_id", ""
        ):
            outcome, _, qualifying = VerificationEngine.history_absence_outcome(
                evidence_items, target
            )
            if outcome == "INSPECTED_EMPTY":
                try:
                    _tip = str(
                        getattr(
                            getattr(
                                getattr(qualifying[0], "provenance", None),
                                "source_location",
                                None,
                            ),
                            "revision",
                            "",
                        )
                        or ""
                    )
                except Exception:
                    _tip = ""
                return VerificationResult(
                    hypothesis_id=hypothesis.id,
                    status=VerificationStatus.VERIFIED,
                    corroborating_evidence_ids=[item.evidence_id for item in qualifying],
                    explanation=(
                        "HISTORY_ABSENCE_PROVED: scoped history search of "
                        f"{target.file_path} from tip {_tip[:8]} matched no commit; "
                        "the negative recent-change claim is verified by the "
                        "empty search over the stated window only."
                    ),
                    confidence=confidence,
                    location_validated=True,
                    limitations=[
                        VerificationLimitation(
                            code=LimitationCode.HISTORY_ABSENCE_PROVED,
                            detail=("Negative proven by scoped empty search over the cited rows."),
                        )
                    ],
                )
        proof_ok, _ = VerificationEngine.history_proof(
            evidence_items, target, self.claim_binding_version
        )
        if not proof_ok or target is None:
            return None
        strict_subject = self.claim_binding_version in (
            CLAIM_BINDING_CBV2,
            CLAIM_BINDING_CBV3,
        )
        # Under cbv2/cbv3 the structured subject decides; the claim text is
        # never consulted. Under cbv1 the legacy statement-substring
        # check applies byte-identically (see history_proof branch).
        claimed_subject = (
            str(getattr(target, "commit_subject", "") or "") if strict_subject else None
        )
        matching: list[Any] = []
        path_commits: list[Any] = []
        for item in evidence_items or []:
            if VerificationEngine._item_type(item) != "COMMIT_HISTORY":
                continue
            try:
                _vattrs = getattr(item, "attributes", None) or {}
                if not isinstance(_vattrs, dict):
                    _vattrs = {}
                if _vattrs.get("discovery_only") is True:
                    continue
                if _vattrs.get("match_count") == 0:
                    continue
            except Exception:
                pass
            if not VerificationEngine._paths_match(
                target.file_path or "", VerificationEngine._item_file(item)
            ):
                continue
            revision = str(
                getattr(
                    getattr(getattr(item, "provenance", None), "source_location", None),
                    "revision",
                    "",
                )
                or ""
            ).lower()
            if not revision:
                continue
            path_commits.append(item)
            commit_id = str(target.commit_id or "").lower()
            subject = str(getattr(item, "summary", "") or "").strip()
            if strict_subject:
                if (
                    revision.startswith(commit_id) or commit_id.startswith(revision)
                ) and VerificationEngine.subjects_match(claimed_subject, subject):
                    matching.append(item)
            elif (
                (revision.startswith(commit_id) or commit_id.startswith(revision))
                and subject
                and subject.casefold() in statement.casefold()
            ):
                matching.append(item)
        if not matching or not path_commits:
            return None
        newest = max(
            path_commits,
            key=lambda item: (
                getattr(item, "observed_at", None)
                or getattr(item, "retrieved_at", None)
                or datetime.min.replace(tzinfo=UTC)
            ),
        )
        if newest not in matching:
            return None
        return VerificationResult(
            hypothesis_id=hypothesis.id,
            status=VerificationStatus.VERIFIED,
            corroborating_evidence_ids=[item.evidence_id for item in matching],
            explanation="Recent-change claim matches the cited file, commit, and commit subject.",
            confidence=confidence,
            location_validated=True,
        )

    @staticmethod
    def claims_location(statement: str) -> bool:
        """Location/listing claim eligible for the answer path (never causal)."""
        try:
            lowered = f" {(statement or '').lower()} "
            patterns = VerificationEngine.DEFINITION_PATTERNS + VerificationEngine.LISTING_PATTERNS
            return any(pattern in lowered for pattern in patterns)
        except Exception:
            return False

    @staticmethod
    def _statement_paths(statement: str) -> list:
        """Repo-relative path tokens named in a claim (pure, deterministic).

        G9: correction claims name paths without location phrasing, so the
        tokens themselves are extracted. Requires a slash (bare filenames
        and versions never match); trailing punctuation/quotes stripped;
        leading ./ stripped. No filesystem access — pure text.
        """
        try:
            paths = []
            for match in re.finditer(VerificationEngine._PATH_TOKEN_RE, statement or ""):
                token = match.group(0).strip().strip(".,;:)]}'\"").lstrip("./")
                if "/" in token and len(token) >= 3 and token not in paths:
                    paths.append(token)
            return paths
        except Exception:
            return []

    @staticmethod
    def _names_cited_path(statement: str, cited: list) -> str | None:
        """First statement path matching a cited evidence file_path (pure).

        G9: the location content of a correction claim is the path the
        citations prove. Match is exact on normalized paths, or
        segment-aligned suffix in either direction — code providers cite
        locator-qualified paths (`org/repo/src/...`) while claims name
        repo-relative ones (`src/...`), and vice versa is possible. A
        typo'd path can never match cited provenance, so the wrong path
        self-excludes; no fuzzy matching (edit distance would admit
        near-miss typos as verified locations).
        """
        try:
            cited_paths = set()
            for item in cited or []:
                provenance = getattr(item, "provenance", None)
                location = getattr(provenance, "source_location", None)
                file_path = getattr(location, "file_path", None)
                if file_path:
                    cited_paths.add(str(file_path).strip().strip("/").lstrip("./"))
            for path in VerificationEngine._statement_paths(statement):
                for cited_path in cited_paths:
                    if VerificationEngine._paths_match(path, cited_path):
                        return path
            return None
        except Exception:
            return None

    @staticmethod
    def _names_cited_stem(statement: str, cited: list) -> str | None:
        """First bare filename stem in a claim matching a cited evidence file (pure).

        Caller-enumeration answers state "X and Y call Z" with bare class
        names and no slash-paths, so the G9 path-token fallback cannot see
        them. A stem matches only when it equals a cited file's basename
        stem (case-insensitive, extension stripped): naming the actual
        cited file qualifies; a corpus-foreign subject (quantum-module)
        never matches. No edit-distance fuzz (near-miss typos stay misses).
        """
        try:
            import re as _re

            stems: set[str] = set()
            for item in cited or []:
                try:
                    provenance = getattr(item, "provenance", None)
                    location = getattr(provenance, "source_location", None)
                    file_path = str(getattr(location, "file_path", "") or "")
                    base = file_path.strip().strip("/").split("/")[-1]
                    if "." in base:
                        base = base.rsplit(".", 1)[0]
                    if base:
                        stems.add(base.casefold())
                except Exception:
                    continue
            if not stems:
                return None
            for match in _re.finditer(r"[A-Za-z][A-Za-z0-9_]*", statement or ""):
                token = match.group(0)
                if len(token) < 4 or token.lower() in VerificationEngine._SUBJECT_STOPWORDS:
                    continue
                if token.casefold() in stems:
                    return token
            return None
        except Exception:
            return None

    # T1-2: verbs splitting a location claim into subject | location.
    # Correction phrasing included ("correct file is", "actual path is").
    _SUBJECT_SPLIT_VERBS = (
        "implemented as",
        "implemented in",
        "defined in",
        "declared in",
        "located in",
        "located at",
        "found in",
        "lives in",
        "correct file is",
        "actual path is",
        "actual file is",
    )
    _SUBJECT_STOPWORDS = frozenset(
        {
            "the",
            "a",
            "an",
            "this",
            "that",
            "these",
            "those",
            "module",
            "file",
            "code",
            "class",
            "function",
            "method",
            "service",
            "correct",
            "actual",
            # Verb-family and claim scaffolding: structural words, never
            # subjects. Without these, "defined" in "X is defined in ..."
            # grounds against any row containing the word, defeating the
            # substitution check (quantum-module fabrication cited a file
            # whose rows mention "defined").
            "defined",
            "implemented",
            "declared",
            "located",
            "found",
            "lives",
            "history",
            "commits",
            "called",
            "does",
            "exist",
            "exists",
            "not",
        }
    )

    @staticmethod
    def _stem_token(token: str) -> str:
        """Porter-lite stem for subject matching (pure).

        Handles the plural/code-inflection cases that break naive
        substring checks (activities→activity, callers→caller,
        handlers→handler). Comparison-only, never stored.
        """
        try:
            word = str(token or "").lower()
            if len(word) <= 3:
                return word
            if word.endswith("ies") and len(word) > 4:
                return word[:-3] + "y"
            if word.endswith("es") and len(word) > 4:
                return word[:-2]
            if word.endswith("s") and not word.endswith("ss"):
                return word[:-1]
            return word
        except Exception:
            return str(token or "").lower()

    @staticmethod
    def _claim_subject_tokens(statement: str, extra_verbs: tuple = ()) -> list:
        """Subject-region tokens of a location claim (pure).

        Text before the first location verb; tokens are Capitalized
        words, quoted phrases, and path basenames (len>=4, stopwords
        dropped, stemmed). Empty when no verb splits the statement
        (listing shapes bypass subject grounding). extra_verbs extends
        the split set for non-location shapes (state claims split on
        "runs"/"uses"/etc.) without changing location behavior.
        """
        try:
            lowered = f" {(statement or '').lower()} "
            split_at = -1
            for verb in (*VerificationEngine._SUBJECT_SPLIT_VERBS, *extra_verbs):
                index = lowered.find(f" {verb} ")
                if index != -1 and (split_at == -1 or index < split_at):
                    split_at = index
            if split_at == -1:
                return []
            subject_region = (statement or "")[:split_at]
            import re as _re

            tokens: list[str] = []
            for match in _re.finditer(r"[A-Za-z][A-Za-z0-9_.\-/]*", subject_region):
                raw = match.group(0).strip(".,;:)]}'\"")
                if "/" in raw:
                    raw = raw.rsplit("/", 1)[-1]
                if "." in raw and " " not in raw:
                    raw = raw.split(".")[0]
                lowered_raw = raw.lower()
                if len(raw) < 4 or lowered_raw in VerificationEngine._SUBJECT_STOPWORDS:
                    continue
                stemmed = VerificationEngine._stem_token(raw)
                if stemmed and stemmed not in tokens:
                    tokens.append(stemmed)
            return tokens
        except Exception:
            return []

    @staticmethod
    def claim_subject_grounded(statement: str, corpus_texts: list) -> bool:
        """Whether the claim subject appears in the evidence corpus (pure).

        T1-2: catches fabrication-by-substitution ("the quantum module is
        X" where quantum appears in no row) while any-match keeps legit
        claims passing. Listing shapes without a verb split pass
        (nothing to ground). Case-insensitive stemmed matching.
        """
        try:
            subjects = VerificationEngine._claim_subject_tokens(statement)
            if not subjects:
                return True
            corpus = " ".join(str(text or "") for text in (corpus_texts or [])).lower()
            if not corpus:
                return False
            import re as _re

            corpus_stems = {
                VerificationEngine._stem_token(word)
                for word in _re.split(r"[^a-z0-9]+", corpus)
                if len(word) >= 3
            }
            return any(token in corpus_stems for token in subjects)
        except Exception:
            return True

    @staticmethod
    def _state_value_grounded(statement: str, value_texts: list) -> bool:
        """Whether a distinctive answer token occurs in row values (pure).

        Distinctive = quoted phrases, tokens containing digits (19c, v2),
        and Capitalized words (Oracle, Enterprise), len>=4, stopwords
        dropped. any-match: the claimed value is either in the rows or not.
        """
        try:
            import re as _re

            strong: list[str] = []
            weak: list[str] = []
            for match in _re.finditer(r"'([^']+)'|\"([^\"]+)\"", statement or ""):
                phrase = match.group(1) or match.group(2) or ""
                for word in _re.split(r"[^A-Za-z0-9]+", phrase):
                    if len(word) >= 4 and word not in strong:
                        strong.append(word)
            for match in _re.finditer(r"[A-Za-z0-9]+(?:[.\-/][A-Za-z0-9]+)*", statement or ""):
                raw = match.group(0).strip(".,;:)]}'\"")
                lowered = raw.lower()
                if len(raw) < 2:
                    continue
                has_digit = any(ch.isdigit() for ch in raw)
                # Version tokens (19c, v2) are short by nature; words keep
                # the len>=4 bar so scaffolding never becomes distinctive.
                if len(raw) < 4 and not has_digit:
                    continue
                if lowered in VerificationEngine._SUBJECT_STOPWORDS:
                    continue
                is_capitalized = raw[0].isalpha() and raw[0].isupper()
                if has_digit and raw not in strong:
                    strong.append(raw)
                elif is_capitalized and raw not in weak:
                    weak.append(raw)
            # High-specificity tokens (quoted, versioned) decide when
            # present: "21c" must occur, not merely "Database". Generic
            # capitalized words only carry claims with no strong tokens.
            candidates = strong if strong else weak
            if not candidates:
                return False
            corpus = " ".join(str(t or "") for t in (value_texts or [])).lower()
            if not corpus:
                return False
            corpus_stems = {
                VerificationEngine._stem_token(w)
                for w in _re.split(r"[^a-z0-9]+", corpus)
                if len(w) >= 3
            }
            return any(VerificationEngine._stem_token(c) in corpus_stems for c in candidates)
        except Exception:
            return False

    @staticmethod
    def _paths_match(first: str, second: str) -> bool:
        """Exact or segment-aligned suffix match either direction (pure).

        Shared by the G9 correction fallback and the Tranche B
        statement-matched range-read inclusion: locator-qualified
        (`org/repo/src/...`) vs repo-relative (`src/...`) paths meet
        without fuzzy matching.
        """
        try:
            first = str(first or "").strip().strip("/").lstrip("./")
            second = str(second or "").strip().strip("/").lstrip("./")
            if not first or not second:
                return False
            return first == second or first.endswith("/" + second) or second.endswith("/" + first)
        except Exception:
            return False

    @staticmethod
    def statement_range_reads(statement: str, evidence_items: list) -> list:
        """Range-read rows for files named in a claim (pure, deterministic).

        Tranche B: a definition claim answered from a SEARCH row while its
        range-read sits banked but uncited (the model cited the sibling
        that found the file, not the read itself). The read is part of
        this claim's lineage — same file, file+revision provenance,
        range-read operation — so it joins the bound set instead of
        forcing an honest answer into INCONCLUSIVE. Concession,
        contradiction, and provenance gates still apply downstream.
        """
        try:
            statement_paths = VerificationEngine._statement_paths(statement or "")
            # REV_BATTERY Phase 1b: caller-enumeration claims name bare class
            # stems ("X calls Z") with no slash-path, so the path-token
            # fallback sees nothing and the banked call-site range never
            # joins the bound set (live L2#2). Fall back to bare-stem
            # matching with the same file+revision + range-read gates.
            use_stem_fallback = not statement_paths
            statement_stems: set[str] = set()
            if use_stem_fallback:
                for _m in re.finditer(r"[A-Za-z][A-Za-z0-9_]*", statement or ""):
                    _tok = _m.group(0)
                    if len(_tok) < 4:
                        continue
                    if _tok.lower() in VerificationEngine._SUBJECT_STOPWORDS:
                        continue
                    statement_stems.add(_tok.casefold())
                if not statement_stems:
                    return []
            matched = []
            seen = set()
            for item in evidence_items or []:
                try:
                    provenance = getattr(item, "provenance", None)
                    fingerprint = getattr(provenance, "query_fingerprint", None)
                    operation = str(getattr(fingerprint, "operation", "") or "").upper()
                    if operation not in VerificationEngine.GET_OPERATIONS:
                        continue
                    location = getattr(provenance, "source_location", None)
                    file_path = getattr(location, "file_path", None)
                    revision = getattr(location, "revision", None)
                    if not file_path or not revision:
                        continue
                    if use_stem_fallback:
                        _base = str(file_path).strip().strip("/").split("/")[-1]
                        if "." in _base:
                            _base = _base.rsplit(".", 1)[0]
                        if not _base or _base.casefold() not in statement_stems:
                            continue
                    elif not any(
                        VerificationEngine._paths_match(path, str(file_path))
                        for path in statement_paths
                    ):
                        continue
                    key = getattr(item, "evidence_id", None)
                    if key is not None and key in seen:
                        continue
                    if key is not None:
                        seen.add(key)
                    matched.append(item)
                except Exception:
                    continue
            return matched
        except Exception:
            return []

    @staticmethod
    def _cited_location_evidence(evidence_items: list) -> list:
        """Code citations carrying file+line+revision provenance."""
        cited = []
        for item in evidence_items or []:
            etype = str(
                getattr(
                    getattr(item, "evidence_type", ""), "value", getattr(item, "evidence_type", "")
                )
                or ""
            ).upper()
            if etype not in VerificationEngine.LISTING_EVIDENCE_TYPES:
                continue
            provenance = getattr(item, "provenance", None)
            location = getattr(provenance, "source_location", None)
            file_path = getattr(location, "file_path", None)
            revision = getattr(location, "revision", None)
            if file_path and revision:
                cited.append(item)
        return cited

    def validate_location_claim(
        self,
        hypothesis: Any,
        evidence_items: list,
        contradictions: list,
        observer: Callable[[dict[str, Any]], None] | None = None,
    ) -> Any | None:
        """G1 location-answer validation. Returns a VERIFIED result with
        location_validated=True when eligible, else None (normal path).

        Eligible: location/listing language, no concession, no unresolved
        HIGH/CRITICAL, ≥1 code citation with file+line+revision provenance,
        and — for definition-strength claims — a range-read citation.
        Confidence is an audited heuristic (base 0.80 + 0.05 per extra
        independent citation, cap 0.95), labeled as such in the explanation.
        """
        statement = str(getattr(hypothesis, "statement", "") or "")
        # Value assertions are authorized in evaluate_hypothesis only after
        # the structured resolution/value proof succeeds. This method remains
        # a location/listing proof and must never independently certify a value.
        if VerificationEngine.claims_field_value(statement):
            return None
        # Phase 2.2: a typed behavior/absence/causal claim is never a
        # location answer, whatever its phrasing ("found in" inside a
        # behavior claim must not take the citations-only fast path).
        claim_kind = VerificationEngine.effective_claim_kind(hypothesis)
        if claim_kind in (
            ClaimKind.BEHAVIOR,
            ClaimKind.ABSENCE,
            ClaimKind.HISTORY,
            ClaimKind.CAUSAL,
        ):
            return None
        # G9 correction fallback: phrasing patterns miss ("the correct file
        # is X"), so a statement naming a cited evidence path qualifies as
        # a location claim on evidence grounds. Phrase path unchanged.
        by_phrase = VerificationEngine.claims_location(statement) or claim_kind in (
            ClaimKind.DEFINITION,
            ClaimKind.CALL_EDGE,
        )
        if not by_phrase:
            cited_pre = VerificationEngine._cited_location_evidence(evidence_items)
            corrected_path = VerificationEngine._names_cited_path(statement, cited_pre)
            if not corrected_path:
                corrected_path = VerificationEngine._names_cited_stem(statement, cited_pre)
            if not corrected_path:
                return None
        else:
            corrected_path = None
        if VerificationEngine.concedes_claim(statement):
            return None
        unresolved = [
            c
            for c in (contradictions or [])
            if getattr(c, "resolution_status", "") == "UNRESOLVED"
            and str(
                getattr(getattr(c, "severity", ""), "value", getattr(c, "severity", ""))
            ).upper()
            in ("HIGH", "CRITICAL")
        ]
        if unresolved:
            return None
        cited = VerificationEngine._cited_location_evidence(evidence_items)
        if not cited:
            return None
        # Phase 2.2: the cited rows must demonstrate THIS kind of claim —
        # a definition needs a range window containing the symbol; a call
        # edge needs a row showing the call. Existence of a location-bearing
        # row (including a search hit) is no longer enough.
        kind_gate = self.check_claim_kind(hypothesis, cited)
        _record_gate_note(
            observer,
            {
                "tap": "T2a",
                "input_set": "CITED_LOCATION",
                "kind": str(getattr(kind, "value", kind))
                if (kind := VerificationEngine.effective_claim_kind(hypothesis)) is not None
                else "UNKNOWN",
                "ok": None if kind_gate is None else bool(kind_gate[0]),
                "reason": "" if kind_gate is None else str(kind_gate[1]),
            },
        )
        if kind_gate is not None and not kind_gate[0]:
            return None
        # T1-2: subject grounding — the claim must be ABOUT the cited
        # rows. Satisfied either by a statement path matching cited
        # provenance (correction claims name the file) or by a subject
        # token appearing in the cited rows. Listing shapes without a
        # verb split pass through. Catches substitution fabrications
        # ("the quantum module is X" where the subject is corpus-foreign).
        try:
            _path_grounded = (
                VerificationEngine._names_cited_path(statement, cited) is not None
                or VerificationEngine._names_cited_stem(statement, cited) is not None
            )
        except Exception:
            _path_grounded = False

        # T1-2: subject grounding helper shared below.
        def _cited_texts() -> list:
            texts = []
            for item in cited:
                try:
                    provenance = getattr(item, "provenance", None)
                    location = getattr(provenance, "source_location", None)
                    texts.append(str(getattr(location, "file_path", "") or ""))
                except Exception:
                    pass
                for field in ("title", "summary", "content_snippet", "source"):
                    try:
                        texts.append(str(getattr(item, field, "") or ""))
                    except Exception:
                        continue
            return texts

        if not _path_grounded:
            try:
                if not VerificationEngine.claim_subject_grounded(statement, _cited_texts()):
                    return None
            except Exception:
                pass
        elif VerificationEngine.claims_definition(statement):
            # Definition claims ("X is implemented in Y") must ground BOTH
            # sides: naming the cited file is not enough when the subject
            # itself is corpus-foreign (quantum-module fabrication). Listing
            # and correction shapes keep the OR semantics above.
            try:
                if not VerificationEngine.claim_subject_grounded(statement, _cited_texts()):
                    return None
            except Exception:
                pass
        if VerificationEngine.claims_definition(
            statement
        ) and not VerificationEngine.has_range_read(cited):
            return None
        providers = {str(getattr(e, "provider", "unknown")) for e in cited}
        # Scoring for location answers: coverage is binary (the location is
        # either observed with provenance or not — 1.0 from the first
        # citation); causal is N/A-neutral (0.5: no causal content is
        # asserted, so it neither supports nor detracts); reliability 1.0
        # (provenance required for every cited row). Overall lands at 0.90:
        # above the 0.80 bar with an honest margin, below certainty.
        confidence = InvestigationConfidence(
            coverage_score=1.0,
            reliability_score=1.0,
            causal_score=0.5,
            contradiction_penalty=0.0,
        )
        return VerificationResult(
            hypothesis_id=hypothesis.id,
            status=VerificationStatus.VERIFIED,
            corroborating_evidence_ids=[e.evidence_id for e in cited],
            explanation=(
                f"Location answer validated against {len(cited)} code citation(s) "
                f"from {len(providers)} provider(s) with file+line+revision "
                f"provenance; no causal claim made."
                + (
                    f" Validated as a corrected-path claim naming {corrected_path}."
                    if corrected_path
                    else ""
                )
            ),
            confidence=confidence,
            location_validated=True,
        )

    def validate_doc_claim(
        self,
        hypothesis: Any,
        evidence_items: list,
        contradictions: list,
    ) -> Any | None:
        """T1-1 doc-answer validation. Returns a VERIFIED result with
        doc_validated=True when eligible, else None (normal path).

        Eligible: statement names a document path matching a DOCUMENTATION
        row with revision provenance, no concession, no unresolved
        HIGH/CRITICAL, and the claim subject grounded in the cited rows
        (same substitution guard as location claims). Same 0.90 shape:
        the document is either cited with provenance or not.
        """
        statement = str(getattr(hypothesis, "statement", "") or "")
        if VerificationEngine.concedes_claim(statement):
            return None
        # T1-1: the claim must name a document path (vague "somewhere in
        # the docs" claims verify nothing even when a doc row is cited).
        if not VerificationEngine._statement_paths(statement):
            return None
        unresolved = [
            c
            for c in (contradictions or [])
            if getattr(c, "resolution_status", "") == "UNRESOLVED"
            and str(
                getattr(getattr(c, "severity", ""), "value", getattr(c, "severity", ""))
            ).upper()
            in ("HIGH", "CRITICAL")
        ]
        if unresolved:
            return None
        cited = []
        for item in evidence_items or []:
            try:
                etype = str(
                    getattr(
                        getattr(item, "evidence_type", ""),
                        "value",
                        getattr(item, "evidence_type", ""),
                    )
                    or ""
                ).upper()
                if etype != "DOCUMENTATION":
                    continue
                provenance = getattr(item, "provenance", None)
                location = getattr(provenance, "source_location", None)
                if getattr(location, "file_path", None) and getattr(location, "revision", None):
                    cited.append(item)
            except Exception:
                continue
        if not cited:
            return None
        try:
            if VerificationEngine._names_cited_path(statement, cited) is None:
                cited_texts = []
                for item in cited:
                    for field in ("title", "summary", "content_snippet", "source"):
                        try:
                            cited_texts.append(str(getattr(item, field, "") or ""))
                        except Exception:
                            continue
                if not VerificationEngine.claim_subject_grounded(statement, cited_texts):
                    return None
        except Exception:
            pass
        providers = {str(getattr(e, "provider", "unknown")) for e in cited}
        confidence = InvestigationConfidence(
            coverage_score=1.0,
            reliability_score=1.0,
            causal_score=0.5,
            contradiction_penalty=0.0,
        )
        return VerificationResult(
            hypothesis_id=hypothesis.id,
            status=VerificationStatus.VERIFIED,
            corroborating_evidence_ids=[e.evidence_id for e in cited],
            explanation=(
                f"Document answer validated against {len(cited)} reference citation(s) "
                f"from {len(providers)} provider(s) with path+revision "
                f"provenance; no causal claim made."
            ),
            confidence=confidence,
            doc_validated=True,
        )

    @traced("reason", require_scope=False, extra_from=_verification_extra)
    def validate_state_claim(
        self,
        hypothesis: Any,
        evidence_items: list,
        contradictions: list,
        hub_names: list[str] | None = None,
    ) -> Any | None:
        """#65 state-answer validation. Returns a VERIFIED result with
        state_validated=True when eligible, else None (normal path).

        Eligible: statement names a value grounded in cited DATABASE_STATE
        rows (same substitution guard as doc claims), no concession, no
        unresolved HIGH/CRITICAL, rows carry connection provenance. Same
        0.90 shape: the value is either in the cited rows or not.
        """
        statement = str(getattr(hypothesis, "statement", "") or "")
        if VerificationEngine.concedes_claim(statement):
            return None
        unresolved = [
            c
            for c in (contradictions or [])
            if getattr(c, "resolution_status", "") == "UNRESOLVED"
            and str(
                getattr(getattr(c, "severity", ""), "value", getattr(c, "severity", ""))
            ).upper()
            in ("HIGH", "CRITICAL")
        ]
        if unresolved:
            return None
        cited = []
        for item in evidence_items or []:
            try:
                etype = str(
                    getattr(
                        getattr(item, "evidence_type", ""),
                        "value",
                        getattr(item, "evidence_type", ""),
                    )
                    or ""
                ).upper()
                if etype != "DATABASE_STATE":
                    continue
                cited.append(item)
            except Exception:
                continue
        if not cited:
            return None
        # #65 AND-rule: the SUBJECT anchor must ground in row values ∪
        # provenance (which database), and a distinctive VALUE token must
        # occur in row values (which answer). Either alone admits
        # fabrication: value-only verifies "quantum ... 19c"; subject-only
        # verifies wrong versions. No vacuous pass: a state claim with no
        # extractable subject anchors nothing and is rejected.
        _STATE_VERBS = ("runs", "uses", "backs", "returns", "shows", "reports", "is", "are")
        subjects = VerificationEngine._claim_subject_tokens(statement, _STATE_VERBS)
        if not subjects:
            return None
        value_texts: list[str] = []
        anchor_texts: list[str] = []
        for item in cited:
            for field in ("title", "summary", "content_snippet", "source"):
                try:
                    value_texts.append(str(getattr(item, field, "") or ""))
                except Exception:
                    continue
            try:
                provenance = getattr(item, "provenance", None)
                anchor_texts.append(str(getattr(provenance, "source_system", "") or ""))
            except Exception:
                pass
            for field in ("provider", "source"):
                try:
                    anchor_texts.append(str(getattr(item, field, "") or ""))
                except Exception:
                    continue
        import re as _re

        anchor_corpus = {
            VerificationEngine._stem_token(w)
            for w in _re.split(r"[^a-z0-9]+", " ".join(value_texts + anchor_texts).lower())
            if len(w) >= 3
        }
        if not any(s in anchor_corpus for s in subjects):
            return None
        if not VerificationEngine._state_value_grounded(statement, value_texts):
            return None
        providers = {str(getattr(e, "provider", "unknown")) for e in cited}
        hub_note = ""
        try:
            if hub_names:
                from investigation_agent_platform.domain.analysis.hub_memory import (
                    hub_membership_corroborates,
                )

                cited_tables = []
                for e in cited:
                    for field in ("title", "summary", "content_snippet"):
                        try:
                            cited_tables.append(str(getattr(e, field, "") or ""))
                        except Exception:
                            continue
                if hub_membership_corroborates(list(hub_names), cited_tables):
                    hub_note = " Hub-membership corroborated (orientation only)."
        except Exception:
            hub_note = ""
        confidence = InvestigationConfidence(
            coverage_score=1.0,
            reliability_score=1.0,
            causal_score=0.5,
            contradiction_penalty=0.0,
        )
        return VerificationResult(
            hypothesis_id=hypothesis.id,
            status=VerificationStatus.VERIFIED,
            corroborating_evidence_ids=[e.evidence_id for e in cited],
            explanation=(
                f"State answer validated against {len(cited)} database-state "
                f"citation(s) from {len(providers)} provider(s); no causal claim made.{hub_note}"
            ),
            confidence=confidence,
            state_validated=True,
        )

    _TESTED_STATUSES = frozenset({"SUPPORTED", "CONTRADICTED", "VERIFIED", "REJECTED"})

    @staticmethod
    def _rival_tested(rival: Any) -> bool:
        """A rival counts as tested when evidence was evaluated against it
        (assessments recorded) or its lifecycle advanced past investigation.
        Fail-closed on unreadable rows: a sibling we cannot read is a live
        alternative, never an evaluated one."""
        try:
            if getattr(rival, "assessments", None):
                return True
            status = str(
                getattr(getattr(rival, "status", ""), "value", getattr(rival, "status", ""))
            ).upper()
            if not status:
                return False
            return status in VerificationEngine._TESTED_STATUSES
        except Exception:
            # Fail-closed on unreadable rows: a sibling we cannot read is a
            # live alternative, never an evaluated one.
            return False

    @staticmethod
    @staticmethod
    def _rival_tested_for(candidate: Any, siblings: list[Any] | None) -> bool:
        """At least one sibling was evaluated against evidence (pure).

        Same tested definition as rival_contrast, inverted: used to earn
        the code-only behavior demotion path. The candidate itself is
        excluded by id; unreadable siblings do not count.
        """
        try:
            candidate_id = getattr(candidate, "id", None)
            for sib in siblings or []:
                try:
                    if candidate_id is not None and getattr(sib, "id", None) == candidate_id:
                        continue
                    assessments = getattr(sib, "assessments", None)
                    status = str(
                        getattr(getattr(sib, "status", ""), "value", getattr(sib, "status", ""))
                    ).upper()
                    if assessments or status in VerificationEngine._TESTED_STATUSES:
                        return True
                except Exception:
                    continue
        except Exception:
            pass
        return False

    @staticmethod
    def rival_evidence_overlap(
        candidate: Any, siblings: list[Any] | None, corroborating_ids: list[Any] | None
    ) -> tuple[float, list[str]]:
        """Fraction of the candidate's corroborating rows also supporting a live rival.

        Same rows establishing two stories is genuine ambiguity, not
        corroboration depth: the shared fraction feeds contradiction_penalty
        (Phase 2a), and any share at all demotes the conclusion to likely.
        Only verifier-touched rivals count (UNDER_INVESTIGATION/SUPPORTED/
        VERIFIED): formulate-time SUPPORTS citations are claims, not proof —
        a PROPOSED rival sharing rows is unevaluated, priced instead by the
        RIVAL_UNTESTED demote. The candidate itself (by id), REJECTED rows,
        and CONTRADICTED rows (already lost) are excluded. ID comparison
        tolerates UUID/str mixing. Pure and deterministic; unreadable rows
        fail closed toward no-share.
        """
        try:
            candidate_ids = {str(e) for e in (corroborating_ids or [])}
            if not candidate_ids:
                return (0.0, [])
            candidate_id = getattr(candidate, "id", None)
            shared_statements: list[str] = []
            shared: set[str] = set()
            for sib in siblings or []:
                try:
                    if candidate_id is not None and getattr(sib, "id", None) == candidate_id:
                        continue
                    status = str(
                        getattr(getattr(sib, "status", ""), "value", getattr(sib, "status", ""))
                    ).upper()
                    if status not in ("UNDER_INVESTIGATION", "SUPPORTED", "VERIFIED"):
                        continue
                    rival_ids = {
                        str(e) for e in (getattr(sib, "supporting_evidence_ids", None) or [])
                    }
                    overlap = candidate_ids & rival_ids
                    if overlap:
                        shared |= overlap
                        shared_statements.append(str(getattr(sib, "statement", "") or "")[:200])
                except Exception:
                    continue
            if not shared:
                return (0.0, [])
            return (min(1.0, len(shared) / len(candidate_ids)), shared_statements)
        except Exception:
            return (0.0, [])

    @staticmethod
    def rival_contrast(
        candidate: Any, siblings: list[Any] | None
    ) -> tuple[bool, list[VerificationLimitation]]:
        """(demote_to_likely, limitations) from untested rival hypotheses.

        A sibling the loop never evaluated is a live alternative the winner
        was not contrasted against — the claim publishes as likely at best.
        The candidate itself is excluded by id.
        """
        try:
            candidate_id = getattr(candidate, "id", None)
            untested: list[str] = []
            for sib in siblings or []:
                try:
                    if candidate_id is not None and getattr(sib, "id", None) == candidate_id:
                        continue
                    if not VerificationEngine._rival_tested(sib):
                        untested.append(str(getattr(sib, "statement", "") or "")[:200])
                except Exception:
                    continue
            if not untested:
                return (False, [])
            return (
                True,
                [
                    VerificationLimitation(
                        code=LimitationCode.RIVAL_UNTESTED,
                        detail=(
                            f"{len(untested)} rival hypothesis(es) never evaluated "
                            f"against evidence (e.g. {untested[0][:160]}). "
                            "Winner not contrasted: likely, not established."
                        )[:500],
                    )
                ],
            )
        except Exception:
            return (False, [])

    _CAUSAL_SPLIT_VERBS = (
        "causes",
        "caused",
        "causing",
        "leads to",
        "led to",
        "triggers",
        "triggered",
        "results in",
        "resulted in",
        "due to",
        "because of",
    )

    @staticmethod
    def untested_rival_statements(candidate: Any, siblings: list[Any] | None) -> list[str]:
        """Statements of live rivals never evaluated against evidence."""
        out: list[str] = []
        try:
            candidate_id = getattr(candidate, "id", None)
            for sib in siblings or []:
                try:
                    if candidate_id is not None and getattr(sib, "id", None) == candidate_id:
                        continue
                    if not VerificationEngine._rival_tested(sib):
                        text = str(getattr(sib, "statement", "") or "").strip()
                        if text:
                            out.append(text[:500])
                except Exception:
                    continue
        except Exception:
            pass
        return out

    @staticmethod
    def elimination_record(
        candidate: Any,
        siblings: list[Any] | None,
        evidence_bank: list[Any] | None = None,
    ) -> list["EliminatedRival"]:
        """Ruled-out rivals with their ruling-out evidence (S5/S6 rival bar).

        A REJECTED or CONTRADICTED sibling counts only with non-empty
        refuting evidence IDs that resolve against the banked evidence —
        a stale or unresolved ID satisfies nothing. Without a bank to
        check against the record stays empty (fail closed: a ruling-out
        claim that cannot be grounded is not recorded). The candidate
        itself is excluded. Never raises.
        """
        from investigation_agent_platform.domain.finding.models import EliminatedRival

        out: list[EliminatedRival] = []
        try:
            banked: set[str] = set()
            if evidence_bank is not None:
                for row in evidence_bank:
                    try:
                        if isinstance(row, dict):
                            eid = row.get("evidence_id", "")
                        else:
                            eid = getattr(row, "evidence_id", "")
                        eid = str(eid or "").strip()
                        if eid:
                            banked.add(eid)
                    except Exception:
                        continue
            else:
                return []
            candidate_id = getattr(candidate, "id", None)
            for sib in siblings or []:
                try:
                    if candidate_id is not None and getattr(sib, "id", None) == candidate_id:
                        continue
                    status = str(
                        getattr(getattr(sib, "status", ""), "value", getattr(sib, "status", ""))
                    ).upper()
                    if status not in ("REJECTED", "CONTRADICTED"):
                        continue
                    ruled_out_by = [
                        str(e)
                        for e in (getattr(sib, "refuting_evidence_ids", None) or [])[:20]
                        if str(e or "").strip() and str(e) in banked
                    ]
                    if not ruled_out_by:
                        continue
                    out.append(
                        EliminatedRival(
                            statement=str(getattr(sib, "statement", "") or "")[:500],
                            status=status,
                            ruled_out_by_evidence_ids=ruled_out_by,
                        )
                    )
                except Exception:
                    continue
        except Exception:
            pass
        return out

    # Evidence types that ground a causal step: source text or a runtime
    # observation. Orientation, docs, graph summaries, and commit titles
    # are direction, never grounding. DIVERGENCE is a machine-checked
    # diff — grounding, not prose.
    _STEP_GROUNDING_TYPES = frozenset(
        {
            "SOURCE_CODE",
            "EXCEPTION_PATH",
            "RUNTIME_LOG",
            "RUNTIME_TRACE",
            "LOG",
            "TRACE",
            "DATABASE_STATE",
            "METRIC",
            "SYSTEM_EVENT",
            "API_PROBE",
            "DIVERGENCE",
        }
    )

    @staticmethod
    def _evidence_by_id(evidence_items: list[Any]) -> dict[str, Any]:
        by_id: dict[str, Any] = {}
        for item in evidence_items or []:
            try:
                by_id[str(getattr(item, "evidence_id", ""))] = item
            except Exception:
                continue
        return by_id

    @staticmethod
    def cites_only_orientation(
        hypothesis: Any,
        evidence_items: list[Any],
        causal_chain: list[Any] | None = None,
    ) -> tuple[bool, str]:
        """D4 enforcement: support tracing only to fuzzy orientation never verifies.

        Collects every cited ID (supporting lists, assessments, causal steps)
        and returns True only when at least one cited row is orientation-traced
        and no cited row is real (orientation-untraced, known) evidence.
        Unknown IDs alone are left to the unknown-citation gates — this gate
        fires on the orientation marker, never on docs, prose, or absence.
        """
        from investigation_agent_platform.application.evidence.code_recall import (
            is_orientation_traced_evidence,
        )

        cited: list[str] = []
        try:
            for ids in (getattr(hypothesis, "supporting_evidence_ids", None) or [],):
                for raw in ids:
                    text = str(raw or "")
                    if text and text not in cited:
                        cited.append(text)
            for asm in getattr(hypothesis, "assessments", None) or []:
                raw = getattr(asm, "evidence_id", None)
                text = str(raw or "")
                if text and text not in cited:
                    cited.append(text)
            steps = causal_chain
            if steps is None:
                try:
                    steps = getattr(hypothesis, "causal_steps", None) or []
                except Exception:
                    steps = []
            for step in steps or []:
                for raw in getattr(step, "evidence_ids", None) or []:
                    text = str(raw or "")
                    if text and text not in cited:
                        cited.append(text)
        except Exception:
            return (True, "citation inspection faulted; failing closed")
        if not cited:
            return (False, "")
        by_id = VerificationEngine._evidence_by_id(evidence_items)
        saw_orientation = False
        for candidate in cited:
            row = by_id.get(candidate)
            if row is None:
                continue
            try:
                if is_orientation_traced_evidence(row):
                    saw_orientation = True
                else:
                    return (False, "")
            except Exception:
                return (True, "citation inspection faulted; failing closed")
        if saw_orientation:
            return (
                True,
                "claim cites only fuzzy-orientation support, never code/runtime evidence",
            )
        return (False, "")

    @staticmethod
    def _row_text(item: Any) -> str:
        parts = []
        for field in ("title", "summary", "content_snippet", "source"):
            try:
                parts.append(str(getattr(item, field, "") or ""))
            except Exception:
                continue
        try:
            location = getattr(getattr(item, "provenance", None), "source_location", None)
            parts.append(str(getattr(location, "file_path", "") or ""))
        except Exception:
            pass
        return " ".join(parts)

    @staticmethod
    def _text_stems(text: str) -> set[str]:
        return {
            VerificationEngine._stem_token(w)
            for w in re.split(r"[^a-z0-9]+", (text or "").lower())
            if len(w) >= 3
        }

    @staticmethod
    def causal_step_grounding(
        causal_chain: list[Any], evidence_items: list[Any]
    ) -> tuple[bool, str]:
        """Blocking gate: every causal step cites banked grounding rows.

        A step with no citations, citations to unknown IDs, or citations
        only to orientation/docs rows is unproven — INCONCLUSIVE, not a
        low-confidence VERIFIED. Relation demonstration is a separate,
        demoting check below.
        """
        from investigation_agent_platform.application.evidence.code_recall import (
            is_orientation_traced_evidence,
        )

        if not causal_chain:
            return (True, "")
        by_id = VerificationEngine._evidence_by_id(evidence_items)
        for index, step in enumerate(causal_chain):
            try:
                cited_ids = [str(eid) for eid in (getattr(step, "evidence_ids", None) or [])]
            except Exception:
                cited_ids = []
            if not cited_ids:
                return (False, f"causal step {index} cites no evidence")
            rows = [by_id[eid] for eid in cited_ids if eid in by_id]
            if len(rows) < len(cited_ids):
                return (
                    False,
                    f"causal step {index} cites {len(cited_ids) - len(rows)} unknown id(s)",
                )
            grounded = [
                r
                for r in rows
                if str(
                    getattr(
                        getattr(r, "evidence_type", ""), "value", getattr(r, "evidence_type", "")
                    )
                    or ""
                ).upper()
                in VerificationEngine._STEP_GROUNDING_TYPES
                # D4: orientation-traced rows never ground, whatever type
                # they carry — the marker means "steering hint, not proof".
                and not is_orientation_traced_evidence(r)
            ]
            if not grounded:
                return (
                    False,
                    f"causal step {index} cites only orientation/docs rows, never code/runtime",
                )
        return (True, "")

    @staticmethod
    def causal_relation_demonstrated(
        causal_chain: list[Any], evidence_items: list[Any]
    ) -> tuple[bool, str]:
        """Demoting check: does a cited row show each step's relation?

        A step is demonstrated when one cited row contains subject terms
        from at least two of its {cause, mechanism, effect} fields (fewer
        when a field is empty). Lexical matching cannot prove a mechanism,
        so failure demotes to likely — it never blocks. An undemonstrable
        step (empty fields throughout) fails open: nothing to check.
        """
        if not causal_chain:
            return (True, "")
        by_id = VerificationEngine._evidence_by_id(evidence_items)
        for index, step in enumerate(causal_chain):
            try:
                fields = [
                    VerificationEngine._text_stems(str(getattr(step, name, "") or ""))
                    for name in ("cause", "mechanism", "effect")
                ]
            except Exception:
                continue
            fields = [f for f in fields if f]
            if not fields:
                continue
            needed = min(2, len(fields))
            rows = [
                by_id[str(eid)]
                for eid in (getattr(step, "evidence_ids", None) or [])
                if str(eid) in by_id
            ]
            demonstrated = False
            for row in rows:
                try:
                    row_stems = VerificationEngine._text_stems(VerificationEngine._row_text(row))
                except Exception:
                    continue
                hits = sum(1 for stems in fields if stems & row_stems)
                if hits >= needed:
                    demonstrated = True
                    break
            if not demonstrated:
                return (
                    False,
                    f"causal step {index} cited but no cited row shows its asserted relation",
                )
        return (True, "")

    @staticmethod
    def _bound_corpus(evidence_items: list[Any]) -> tuple[list[str], list[str]]:
        """(value texts, file paths) from bound evidence rows."""
        texts: list[str] = []
        paths: list[str] = []
        for item in evidence_items or []:
            try:
                for field in ("title", "summary", "content_snippet", "source"):
                    texts.append(str(getattr(item, field, "") or ""))
                location = getattr(getattr(item, "provenance", None), "source_location", None)
                file_path = str(getattr(location, "file_path", "") or "")
                if file_path:
                    paths.append(file_path)
                    texts.append(file_path)
                texts.extend(VerificationEngine._bound_resolution_identities(item))
            except Exception:
                continue
        return texts, paths

    @staticmethod
    def _bound_resolution_identities(item: Any) -> list[str]:
        """Subject/value identity strings of a bound RESOLVED code-fact.

        The proof of a source-value claim increasingly lives in the analyzer
        resolution payload (subject_identity, declaring type, resolved value),
        not just the snippet prose — method-anchored windows legitimately
        omit the enclosing class declaration (live L5). Surfacing those
        strings keeps conclusion mapping honest about what the bound rows
        actually establish. Non-RESOLVED or missing payloads contribute
        nothing; fail-open tolerates every payload shape.
        """
        out: list[str] = []
        try:
            attrs = getattr(item, "attributes", None) or {}
            if not isinstance(attrs, dict):
                return out
            payload = attrs.get("code_fact_resolution_v2") or attrs.get("code_fact_resolution")
            if payload is None:
                return out
            if isinstance(payload, dict):
                status = str(payload.get("status") or "")
                fields = [
                    payload.get("subject_identity"),
                    payload.get("declaring_type"),
                    payload.get("field_name"),
                    payload.get("resolved_value"),
                ]
            else:
                status = str(getattr(payload, "status", "") or "")
                fields = [
                    getattr(payload, "subject_identity", None),
                    getattr(payload, "declaring_type", None),
                    getattr(payload, "field_name", None),
                    getattr(payload, "resolved_value", None),
                ]
            if "RESOLVED" not in status.upper():
                return out
            for field in fields:
                text = str(field or "").strip()
                if text and text.upper() not in {"NONE", "NULL"} and text not in out:
                    out.append(text)
        except Exception:
            return []
        return out

    @staticmethod
    def _resolution_grounded(sentence: str, evidence_items: list[Any] | None) -> bool:
        """A sentence restating a bound RESOLVED code fact is mapped.

        Requires both halves of the proven fact in the sentence: the subject
        identity (stem match on any dot-segment) and the resolved literal
        (quoted or plain substring). Value-only or subject-only mentions
        still take the normal text-mapping path, so this can never launder
        an unrelated value into a grounded claim.
        """
        try:
            text = str(sentence or "")
            if not text:
                return False
            lowered = text.lower()
            for item in evidence_items or []:
                attrs = getattr(item, "attributes", None) or {}
                if not isinstance(attrs, dict):
                    continue
                payloads: list[dict[str, Any]] = []
                singular = attrs.get("code_fact_resolution_v2")
                if isinstance(singular, dict):
                    payloads.append(singular)
                envelope = attrs.get("code_fact_resolutions")
                if isinstance(envelope, list):
                    payloads.extend(
                        entry["resolution"]
                        for entry in envelope
                        if isinstance(entry, dict) and isinstance(entry.get("resolution"), dict)
                    )
                payloads.extend(
                    value
                    for key, value in attrs.items()
                    if key.startswith("code_fact_java.") and isinstance(value, dict)
                )
                for payload in payloads:
                    status = str(payload.get("status") or "")
                    subject = str(payload.get("subject_identity") or "")
                    value = payload.get("resolved_value")
                    if "RESOLVED" not in status.upper() or not subject or value is None:
                        continue
                    subject_hit = any(
                        len(part) >= 3 and part.lower() in lowered
                        for part in re.split(r"[.:#]", subject)
                    )
                    value_hit = str(value).strip() != "" and str(value).lower() in lowered
                    if subject_hit and value_hit:
                        return True
            return False
        except Exception:
            return False

    @staticmethod
    def sentence_mapped(sentence: str, value_texts: list[Any], file_paths: list[Any]) -> bool:
        """One conclusion sentence maps to a cited path, a grounded subject,
        or a distinctive value in the bound rows — else it is prose."""
        text = (sentence or "").strip()
        if not text:
            return True
        try:
            # A file citation maps a location, not a value. For source-value
            # claims require a distinctive literal to occur in the bound text.
            if VerificationEngine.claims_field_value(text):
                corpus_text = " ".join(str(t or "") for t in value_texts).lower()
                values = re.findall(
                    r"(?i)(?:=\s*|defaults?\s+to\s+|initialized?\s+to\s+)"
                    r"(['\"][^'\"]+['\"]|[-+]?\d+(?:\.\d+)?(?:[fdl])?|true|false|null)",
                    text,
                )
                if values:
                    return all(value.lower().strip("'\"") in corpus_text for value in values)
                return False
            for path in VerificationEngine._statement_paths(text):
                if any(VerificationEngine._paths_match(path, str(fp)) for fp in file_paths):
                    return True
            subjects = VerificationEngine._claim_subject_tokens(
                text, VerificationEngine._CAUSAL_SPLIT_VERBS
            )
            if subjects:
                corpus = " ".join(str(t or "") for t in value_texts).lower()
                stems = {
                    VerificationEngine._stem_token(w)
                    for w in re.split(r"[^a-z0-9]+", corpus)
                    if len(w) >= 3
                }
                if any(token in stems for token in subjects):
                    return True
            # Short numeric literals ("20", "v2") never survive the stem
            # corpus above; match them as substrings — state answers hinge
            # on values, and a claimed value either occurs or it does not.
            corpus_text = " ".join(str(t or "") for t in value_texts).lower()
            for match in re.finditer(r"[A-Za-z0-9]*[0-9][A-Za-z0-9./-]*", text):
                raw = match.group(0).strip(".,;:)]}'\"").lower()
                if len(raw) >= 2 and raw in corpus_text:
                    return True
            return VerificationEngine._state_value_grounded(text, list(value_texts))
        except Exception:
            return False

    @staticmethod
    def _region_tokens(region: str, min_len: int = 4) -> list[str]:
        """Subject-shaped tokens of an arbitrary text region (pure).

        Same token rules as _claim_subject_tokens (Capitalized words, quoted
        phrases, path basenames, stopwords dropped, stemmed) without
        the verb split — shared by the cause side (pre-verb region) and the
        effect side (post-verb region) of a causal sentence. The effect side
        passes min_len=3: effects are often acronyms or codes (NPE, OOM)
        that the cause-side floor would discard.
        """
        try:
            tokens: list[str] = []
            for match in re.finditer(r"[A-Za-z][A-Za-z0-9_.\-/]*", region or ""):
                raw = match.group(0).strip(".,;:)]}'\"")
                if "/" in raw:
                    raw = raw.rsplit("/", 1)[-1]
                if "." in raw and " " not in raw:
                    raw = raw.split(".")[0]
                lowered_raw = raw.lower()
                if len(raw) < min_len or lowered_raw in VerificationEngine._SUBJECT_STOPWORDS:
                    continue
                stemmed = VerificationEngine._stem_token(raw)
                if stemmed and stemmed not in tokens:
                    tokens.append(stemmed)
            return tokens
        except Exception:
            return []

    @staticmethod
    def _causal_verb_span(sentence: str) -> tuple[str, int] | None:
        """(verb, original-coord start index) of the first causal verb, else None.

        Multi-word verbs ("results in", "due to") match space-padded so
        "led" never fires inside "failed". Pure.
        """
        try:
            lowered = f" {(sentence or '').lower()} "
            first: tuple[str, int] | None = None
            for verb in VerificationEngine._CAUSAL_SPLIT_VERBS:
                index = lowered.find(f" {verb} ")
                if index != -1 and (first is None or index < first[1]):
                    first = (verb, index)
            return first
        except Exception:
            return False

    @staticmethod
    def causal_sentence_entailed(
        sentence: str, evidence_items: list[Any] | None, causal_texts: list[Any] | None = None
    ) -> bool | None:
        """Does one bound row (or chain link) show the asserted causal relation?

        Returns None when the sentence makes no causal claim with tokens on
        both sides (non-causal sentences and vague effects keep the existing
        mapping path). Returns True when a single row blob or causal-link
        text contains cause-side AND effect-side stems — the relation is
        entailed, not merely consistent across scattered rows. Returns False
        when the sentence asserts causation but no single row shows both
        sides: cause in row 3 + effect in row 7 is consistency, not proof.
        Analysis errors return conservative non-entailment rather than proving
        a causal relation.
        """
        try:
            span = VerificationEngine._causal_verb_span(sentence)
            if span is None:
                return None
            verb, index = span
            cause_tokens = VerificationEngine._region_tokens((sentence or "")[:index])
            effect_tokens = VerificationEngine._region_tokens(
                (sentence or "")[index + len(verb) + 1 :], min_len=3
            )
            if not cause_tokens or not effect_tokens:
                return None
            blobs: list[str] = []
            for item in evidence_items or []:
                try:
                    texts, _paths = VerificationEngine._bound_corpus([item])
                    blob = " ".join(str(t or "") for t in texts).strip()
                    if blob:
                        blobs.append(blob)
                except Exception:
                    continue
            for text in causal_texts or []:
                if str(text or "").strip():
                    blobs.append(str(text))
            for blob in blobs:
                stems = VerificationEngine._text_stems(blob)
                if any(t in stems for t in cause_tokens) and any(t in stems for t in effect_tokens):
                    return True
            return False
        except Exception:
            return False

    @staticmethod
    def map_conclusion_sentences(
        root_cause: str,
        evidence_items: list[Any],
        causal_texts: list[Any] | None = None,
    ) -> list[str]:
        """Conclusion sentences mapping to no cited claim or evidence row.

        Pure and deterministic: split on sentence boundaries, then require
        each sentence to name a cited path, ground its causal subject in the
        bound rows, or state a distinctive value the rows contain.
        """
        value_texts, file_paths = VerificationEngine._bound_corpus(evidence_items)
        value_texts.extend(str(t or "") for t in (causal_texts or []))
        unmapped: list[str] = []
        try:
            for raw in re.split(r"(?<=[.!?])\s+", (root_cause or "").strip()):
                sentence = raw.strip().strip(".,;:!?\"'")
                if not sentence:
                    continue
                if VerificationEngine._resolution_grounded(sentence, evidence_items):
                    continue
                # Phase 2b: entailment for causal claims. A sentence asserting
                # causation ("X causes Y") maps only when one bound row (or
                # chain link) shows both sides — cause in row 3 + effect in
                # row 7 is consistency, not proof. Non-causal sentences and
                # vague effects keep the existing mapping path.
                entailed = VerificationEngine.causal_sentence_entailed(
                    sentence, evidence_items, causal_texts
                )
                if entailed is False:
                    unmapped.append(sentence)
                    continue
                if not VerificationEngine.sentence_mapped(sentence, value_texts, file_paths):
                    unmapped.append(sentence)
        except Exception:
            return [
                sentence
                for sentence in re.split(r"(?<=[.!?])\s+", (root_cause or "").strip())
                if sentence
            ]
        return unmapped

    @staticmethod
    def apply_conclusion_mapping(verification: Any, unmapped: list[Any]) -> Any:
        """Demote a conclusion whose sentences outrun its citations to likely."""
        if not unmapped:
            return verification
        try:
            first = str(unmapped[0] or "")[:160]
            limitations = list(getattr(verification, "limitations", None) or [])
            limitations.append(
                VerificationLimitation(
                    code=LimitationCode.CONCLUSION_UNMAPPED,
                    detail=(
                        f"{len(unmapped)} conclusion sentence(s) map to no cited "
                        f"claim or evidence row (e.g. {first})."
                    )[:500],
                )
            )
            return verification.model_copy(update={"tier": "likely", "limitations": limitations})
        except Exception:
            return verification

    @staticmethod
    def next_check_for(
        limitations: list[Any],
        untested_rivals: list[Any] | None = None,
        unmapped: list[Any] | None = None,
    ) -> str | None:
        """The single re-verify directive that would promote this conclusion.

        Priority follows information value: an untested rival first (it can
        overturn the winner), then an undemonstrated relation in the
        winner's own steps, then uncited sentences, then corroboration.
        """
        codes: set[str] = set()
        for lim in limitations or []:
            try:
                codes.add(
                    str(
                        getattr(getattr(lim, "code", ""), "value", getattr(lim, "code", ""))
                    ).upper()
                )
            except Exception:
                continue
        rivals = [str(r or "").strip() for r in (untested_rivals or []) if str(r or "").strip()]
        sentences = [str(x or "").strip() for x in (unmapped or []) if str(x or "").strip()]
        if "RIVAL_UNTESTED" in codes and rivals:
            return f"Evaluate the untested rival hypothesis against evidence, then re-verify: {rivals[0][:300]}"
        if "RIVAL_UNTESTED" in codes:
            return "Evaluate every rival hypothesis against evidence, then re-verify."
        if "RIVAL_UNBEATEN" in codes:
            return (
                "Rule out the strongest live rival with ruling-out evidence, "
                "then re-verify: an established causal verdict requires a "
                "recorded elimination."
            )
        if "RELATION_UNDEMONSTRATED" in codes:
            return "Cite the code/log rows showing each causal step's mechanism, then re-verify."
        if "NO_RUNTIME_OBSERVATION" in codes:
            return "Observe the behavior at runtime (log, trace, state, or probe), then re-verify."
        if "CONCLUSION_UNMAPPED" in codes and sentences:
            return (
                f"Cite evidence for each conclusion sentence, then re-verify: {sentences[0][:300]}"
            )
        if "CONCLUSION_UNMAPPED" in codes:
            return "Cite evidence for each conclusion sentence, then re-verify."
        if "SINGLE_PROVIDER" in codes:
            return "Corroborate with an independent evidence provider, then re-verify."
        return None

    @staticmethod
    def next_check_for_abstention(kind: Any, reasons: list[Any] | None) -> str | None:
        """The single re-verify directive for an abstained investigation.

        Pure and deterministic: maps the abstention kind and its reason
        list to one operator check. Specific reason strings precede the
        kind blanket, so "all hypotheses rejected" never receives a
        refuting-evidence directive when no refuting items exist. Wording
        is unblock-neutral ("then re-verify"), never "promote." Fail-open:
        any exception yields None — persistence never blocks on this.
        """
        try:
            texts = [str(r or "").strip() for r in (reasons or []) if str(r or "").strip()]
            if not texts:
                return None
            lowered = [t.lower() for t in texts]
            if any("all hypotheses rejected or contradicted" in t for t in lowered):
                return (
                    "Formulate a hypothesis consistent with the refuting evidence, then re-verify."
                )
            refuting = next(
                (
                    t
                    for t in texts
                    if re.match(r"^\d+ refuting evidence item\(s\) bound", t.lower())
                ),
                "",
            )
            if refuting:
                return f"Resolve the {refuting[:120]}, then re-verify."
            if str(kind or "").strip().lower() == "refuted":
                return "Resolve the refuting evidence, then re-verify."
            if any(t == "no bound supporting evidence" for t in lowered):
                return (
                    "Cite and bind supporting evidence to the candidate hypothesis, then re-verify."
                )
            if any("cited evidence_id" in t and "unknown" in t for t in lowered):
                return (
                    "Rebind supporting IDs within this investigation (a cited key "
                    "resolves elsewhere or nowhere), then re-verify."
                )
            if any("caller enumeration names a caller" in t for t in lowered):
                return (
                    "Bind each named caller to its own CALL_EDGE claim with a "
                    "shown call site, then re-verify."
                )
            if any("no cited row shows a call" in t for t in lowered):
                return (
                    "Bind the range-read row showing the call expression "
                    "(not a search hit or comment), then re-verify."
                )
            if any("runtime" in t and "behavior" in t for t in lowered):
                return (
                    "Observe the behavior at runtime (log, trace, state, or probe), then re-verify."
                )
            if any("java fact" in t for t in lowered):
                return (
                    "Bind a resolved source-value fact (or widen the range window "
                    "past the method-only slice), then re-verify."
                )
            if any("empty-search" in t or "absence" in t for t in lowered):
                return "Narrow or re-scope the empty-search proof to the claimed scope, then re-verify."
            if any(
                t == "no validated causal chain" or "no causal chain established" in t
                for t in lowered
            ):
                return "Bind a causal chain with cited evidence for each link, then re-verify."
            return f"Resolve '{texts[0][:300]}', then re-verify."
        except Exception:
            return None

    def claim_binding_enforcement(
        self,
        hypothesis: Any,
        evidence_items: list[Any],
        recorded_attempts: list[tuple[list[str], dict[str, Any] | None]] | None,
    ) -> ClaimBindingEnforcement | None:
        """Compute the authoritative Phase 1 partition for one hypothesis.

        Returns None when the pin is not cbv2/cbv3 (legacy bank path
        decides, byte-identically). Under cbv2/cbv3 a partitioning fault
        yields a fault enforcement that the caller turns INCONCLUSIVE
        (fail closed, never legacy-decided). When
        timeline-backed attempts are unavailable, every cited row is
        bank-fallback (unmapped): unknown (cited-but-unbanked) IDs still
        evaporate, bank-fallback-only citations verify only with an
        UNMAPPED_CITATION limitation, and CALL_EDGE/HISTORY verification
        without a claim-local kept edge is refused.
        """
        if self.claim_binding_version not in (CLAIM_BINDING_CBV2, CLAIM_BINDING_CBV3):
            return None
        try:
            return self._claim_binding_partition(hypothesis, evidence_items, recorded_attempts)
        except Exception:
            # Fail closed: a partitioning fault under cbv2 never degrades
            # to the legacy bank path (which could VERIFY on evaporated
            # rows). The caller turns a fault enforcement INCONCLUSIVE.
            return ClaimBindingEnforcement(fault=True)

    # -- Phase 3 (claim binding): misattribution repair --------------------

    def _repair_vetoed(
        self,
        hypothesis: Any,
        evidence_items: list[Any] | None,
        contradictions: list[Any] | None,
        sibling_hypotheses: list[Any] | None,
    ) -> bool:
        """True when repair must not run for this hypothesis.

        Vetoes: concession phrasing or orientation-only support
        (abstention-shaped claims never verify), any UNRESOLVED
        HIGH/CRITICAL contradiction, any bound refuting evidence, and an
        already-VERIFIED sibling (a rival block — the answer is taken).
        Fail-closed: any inspection fault vetoes. Pure; never raises.
        """
        try:
            if VerificationEngine.concedes_claim(str(getattr(hypothesis, "statement", "") or "")):
                return True
        except Exception:
            return True
        try:
            orientation_only, _orientation_reason = VerificationEngine.cites_only_orientation(
                hypothesis, list(evidence_items or [])
            )
            if orientation_only:
                return True
        except Exception:
            return True
        try:
            for item in contradictions or []:
                if getattr(item, "resolution_status", "") != "UNRESOLVED":
                    continue
                severity = getattr(
                    getattr(item, "severity", ""), "value", getattr(item, "severity", "")
                )
                if str(severity or "").upper() in ("HIGH", "CRITICAL"):
                    return True
        except Exception:
            return True
        try:
            if list(getattr(hypothesis, "refuting_evidence_ids", None) or []):
                return True
        except Exception:
            return True
        try:
            for sibling in sibling_hypotheses or []:
                try:
                    status = getattr(sibling, "status", None)
                    if str(getattr(status, "value", status) or "") == "VERIFIED":
                        return True
                except Exception:
                    continue
        except Exception:
            return True
        return False

    def repair_failed_groups(
        self,
        hypothesis: Any,
        evidence_items: list[Any],
        contradictions: list[Any] | None = None,
        sibling_hypotheses: list[Any] | None = None,
        binding_version: str | None = None,
        evidence_bank: list[Any] | None = None,
    ) -> tuple[list[str], list[RepairedBinding]]:
        """Per-(kind, target) gate-failure recovery (Phase 3).

        Iterates the hypothesis's _distinct_typed_claims groups (sorted
        group-ID order) with kind DEFINITION/HISTORY. For each group
        whose kind gate explicitly returns (False, reason) over the
        group's cited banked rows, runs attempt_claim_repair over the
        cited banked rows that fail the gate individually. Gate rows and
        the donor pool resolve against the bank (evidence_bank when
        given — the full investigation bank, which the verdict's working
        set may exclude — else evidence_items); donors are bank rows
        cited by NO group. Returns (rebound_ids, repairs); under cbv1
        (or unmarked) returns ([], []) without touching the helper —
        the execution counter stays zero. Vetoed hypotheses
        (concession, orientation-only support, blocking contradiction,
        refuting evidence, verified rival) return ([], []). Never raises.
        """
        try:
            version = binding_version or self.claim_binding_version
        except Exception:
            version = CLAIM_BINDING_CBV1
        if version not in (CLAIM_BINDING_CBV2, CLAIM_BINDING_CBV3):
            return [], []
        try:
            if self._repair_vetoed(hypothesis, evidence_items, contradictions, sibling_hypotheses):
                return [], []
        except Exception:
            return [], []
        try:
            raw_groups = list(_distinct_typed_claims(hypothesis) or [])
            groups: list[dict[str, Any]] = [g for g in raw_groups if isinstance(g, dict)]
        except Exception:
            return [], []
        try:
            bank_items = evidence_bank if evidence_bank is not None else evidence_items
            evidence_by_id: dict[str, Any] = {}
            for item in bank_items or []:
                try:
                    key = str(getattr(item, "evidence_id", "") or "").strip()
                except Exception:
                    continue
                if key and key not in evidence_by_id:
                    evidence_by_id[key] = item
        except Exception:
            return [], []
        try:
            all_cited: set[str] = set()
            for group in groups:
                try:
                    for raw in group.get("cited_ids") or []:
                        cid = str(raw or "").strip()
                        if cid:
                            all_cited.add(cid)
                except Exception:
                    continue
        except Exception:
            return [], []
        rebound_all: list[str] = []
        repairs_all: list[RepairedBinding] = []
        try:
            statement = str(getattr(hypothesis, "statement", "") or "")
        except Exception:
            statement = ""

        def _group_sort_key(group: Any) -> str:
            try:
                return f"{group.get('kind_value') or ''}|{group.get('target_key') or ''}"
            except Exception:
                return ""

        for group in sorted(groups, key=_group_sort_key):
            try:
                kind_value = str(group.get("kind_value") or "")
            except Exception:
                continue
            if kind_value not in ("DEFINITION", "HISTORY"):
                continue
            try:
                target_dict = dict(group.get("target_dict") or {})
            except Exception:
                continue
            try:
                target_model = ClaimTarget(**target_dict)
            except Exception:
                continue
            try:
                group_cited = [
                    str(raw or "").strip()
                    for raw in (group.get("cited_ids") or [])
                    if str(raw or "").strip() in evidence_by_id
                ]
            except Exception:
                continue
            if not group_cited:
                continue
            try:
                rows = [evidence_by_id[cid] for cid in group_cited]
                if kind_value == "DEFINITION":
                    gate_ok, gate_reason = VerificationEngine.definition_window_proof(
                        rows, target_model
                    )
                else:
                    gate_ok, gate_reason = VerificationEngine.history_proof(
                        rows, target_model, version
                    )
            except Exception:
                continue
            if gate_ok:
                continue
            try:
                candidates = []
                for cid in group_cited:
                    try:
                        if kind_value == "DEFINITION":
                            ok, _ = VerificationEngine.definition_window_proof(
                                [evidence_by_id[cid]], target_model
                            )
                        else:
                            ok, _ = VerificationEngine.history_proof(
                                [evidence_by_id[cid]], target_model, version
                            )
                        if not ok:
                            candidates.append(cid)
                    except Exception:
                        continue
            except Exception:
                continue
            if not candidates:
                continue
            try:
                self._repair_calls += 1
            except Exception:
                pass
            try:
                ck = claim_key(statement, kind_value)
            except Exception:
                continue
            try:
                rebound, repairs = attempt_claim_repair(
                    kind_value,
                    ck,
                    target_model,
                    candidates,
                    all_cited,
                    evidence_by_id,
                    cast(Literal["cbv2", "cbv3"], version),
                    str(gate_reason or ""),
                )
            except Exception:
                continue
            rebound_all.extend(rebound)
            repairs_all.extend(repairs)
        return rebound_all, repairs_all

    @staticmethod
    def _bank_index_for(items: list[Any] | None) -> dict[str, Any]:
        """Evidence rows keyed by string evidence ID (pure, never raises)."""
        index: dict[str, Any] = {}
        try:
            for row in items or []:
                try:
                    key = str(getattr(row, "evidence_id", "") or "").strip()
                except Exception:
                    continue
                if key and key not in index:
                    index[key] = row
        except Exception:
            pass
        return index

    def _hypothesis_with_rebound_ids(self, hypothesis: Any, mapping: dict[str, Any]) -> Any | None:
        """Hypothesis copy with candidate cited IDs replaced by donors.

        mapping keys are string cited IDs; values are the donor row's
        evidence_id object verbatim (ID types preserved — UUIDs stay
        UUIDs). Substitutes in supporting_evidence_ids, per-assessment
        evidence_id, and causal-step evidence_ids. Returns None on any
        fault (fail closed — the caller keeps the original verdict).
        """
        try:
            if not mapping:
                return None
            updates: dict[str, Any] = {}
            try:
                supporting = [
                    mapping.get(str(raw), raw)
                    for raw in (getattr(hypothesis, "supporting_evidence_ids", None) or [])
                ]
                updates["supporting_evidence_ids"] = supporting
            except Exception:
                pass
            try:
                rebound_assessments = []
                for asm in getattr(hypothesis, "assessments", None) or []:
                    try:
                        eid = getattr(asm, "evidence_id", None)
                        if eid is not None and str(eid) in mapping:
                            rebound_assessments.append(
                                asm.model_copy(update={"evidence_id": mapping[str(eid)]})
                            )
                        else:
                            rebound_assessments.append(asm)
                    except Exception:
                        rebound_assessments.append(asm)
                updates["assessments"] = rebound_assessments
            except Exception:
                pass
            try:
                rebound_steps = []
                for step in getattr(hypothesis, "causal_steps", None) or []:
                    try:
                        ids = [
                            mapping.get(str(raw), raw)
                            for raw in (getattr(step, "evidence_ids", None) or [])
                        ]
                        rebound_steps.append(step.model_copy(update={"evidence_ids": ids}))
                    except Exception:
                        rebound_steps.append(step)
                updates["causal_steps"] = rebound_steps
            except Exception:
                pass
            return hypothesis.model_copy(update=updates)
        except Exception:
            return None

    @staticmethod
    def _repair_mapping(repairs: list[RepairedBinding]) -> dict[str, Any] | None:
        """Build a citation substitution map, or fail closed on conflicts.

        One evidence ID can be cited by multiple typed groups. Applying two
        different donors through a hypothesis-wide ID map would make the last
        group's authorization rewrite the other group. Conflicting donor
        choices therefore disable applied revalidation for the turn.
        """
        try:
            mapping: dict[str, Any] = {}
            for repair in repairs or []:
                cited = str(getattr(repair, "cited_id", "") or "").strip()
                donor = str(getattr(repair, "donor_id", "") or "").strip()
                if not cited or not donor:
                    continue
                prior = mapping.get(cited)
                if prior is not None and str(prior) != donor:
                    return None
                mapping[cited] = donor
            return mapping
        except Exception:
            return None

    @staticmethod
    def attach_repairs(result: Any, repairs: list[RepairedBinding]) -> Any:
        """Attach repair records to a result's diagnostics (pure-ish).

        Appends to diagnostics.repaired without an in-memory cap; marker
        serialization applies the explicit review cap and truncation flag.
        Attaches REPAIRED_CITATION when the verdict is VERIFIED with applied
        repairs. Returns the (possibly copied) result. Never raises.
        """
        try:
            if not repairs:
                return result
            diagnostics = getattr(result, "diagnostics", None)
            if diagnostics is None:
                try:
                    diagnostics = VerificationDiagnostics()
                except Exception:
                    return result
            existing = list(getattr(diagnostics, "repaired", None) or [])
            merged = [*existing, *[r for r in repairs if r not in existing]]
            try:
                diagnostics = diagnostics.model_copy(update={"repaired": merged})
            except Exception:
                return result
            try:
                result = result.model_copy(update={"diagnostics": diagnostics})
            except Exception:
                return result
            try:
                status = str(
                    getattr(getattr(result, "status", ""), "value", getattr(result, "status", ""))
                )
                applied = [r for r in repairs if str(getattr(r, "outcome", "")) == "applied"]
                if status == VerificationStatus.VERIFIED.value and applied:
                    engine_self = VerificationEngine()
                    result = engine_self._apply_binding_limitations(
                        result, [LimitationCode.REPAIRED_CITATION.value]
                    )
            except Exception:
                pass
            return result
        except Exception:
            return result

    def _failed_proof_for_repair(self, hypothesis: Any, expect_kinds: tuple[Any, ...]) -> bool:
        """True when a None validator result is a failed proof (pure).

        Mirrors the wrapped validator's own kind gate: the hypothesis's
        effective kind must be one the validator handles, and a
        DEFINITION/HISTORY group (repair's own domain) must cite rows.
        Kind mismatch, concession-shaped, vetoed, and uncited hypotheses
        stay ineligible, so the repair trigger does not broaden. Never
        raises.
        """
        try:
            kind = VerificationEngine.resolve_claim_kind(
                hypothesis,
                strict=self.claim_binding_version
                in (
                    CLAIM_BINDING_CBV2,
                    CLAIM_BINDING_CBV3,
                ),
            )
        except Exception:
            return False
        try:
            if kind not in tuple(expect_kinds or ()):
                return False
        except Exception:
            return False
        try:
            groups = _distinct_typed_claims(hypothesis) or []
        except Exception:
            return False
        for group in groups:
            try:
                if not isinstance(group, dict):
                    continue
                if str(group.get("kind_value") or "") not in ("DEFINITION", "HISTORY"):
                    continue
                if list(group.get("cited_ids") or []):
                    return True
            except Exception:
                continue
        return False

    def revalidate_with_repair(
        self,
        run_fn: Any,
        hypothesis: Any,
        working_items: list[Any],
        evidence_bank: list[Any],
        contradictions: list[Any] | None = None,
        sibling_hypotheses: list[Any] | None = None,
        enforcement: Any | None = None,
        expect_kinds: tuple[Any, ...] | None = None,
    ) -> tuple[Any, list[RepairedBinding]]:
        """Run a fast-path validate fn with one repair pass (Phase 3).

        Runs run_fn(hypothesis, working_items) verbatim (identical to
        the unwrapped call, including under cbv1); when the pin is
        cbv2/cbv3 and the outcome is INCONCLUSIVE with empty
        corroborating evidence, runs one per-group repair pass over
        evidence_bank and — under cbv3 with rebinds — re-runs run_fn
        once on the rebound hypothesis with a kept-filtered working set
        (kept + rebound donors, via enforcement_with_repairs). Returns
        (result, repairs); shadow repairs are returned for diagnostics
        attachment, never applied. Never raises.

        Finding D: HISTORY/location validators return None both when
        inapplicable (kind mismatch, concession, veto) and when the
        proof fails on cited rows. A failed proof must reach repair;
        an inapplicable validator must not broaden the trigger.
        ``expect_kinds`` names the kinds the wrapped validator handles
        (history: HISTORY; location: DEFINITION/CALL_EDGE); a None
        result proceeds to repair only when the hypothesis's effective
        kind matches and a DEFINITION/HISTORY group cites rows. Veto and
        pin gating stay inside repair_failed_groups.
        """
        try:
            result = run_fn(hypothesis, working_items)
        except Exception:
            return None, []
        try:
            version = self.claim_binding_version
        except Exception:
            return result, []
        if version not in (CLAIM_BINDING_CBV2, CLAIM_BINDING_CBV3):
            return result, []
        _failed_proof = False
        if result is None:
            # Failed proof (validator declined on cited rows) vs
            # inapplicable (kind mismatch, concession, veto): only the
            # former reaches repair, and only for the wrapped
            # validator's own kinds.
            if expect_kinds is not None and self._failed_proof_for_repair(
                hypothesis, tuple(expect_kinds)
            ):
                _failed_proof = True
            else:
                return None, []
        if not _failed_proof:
            try:
                status = str(
                    getattr(getattr(result, "status", ""), "value", getattr(result, "status", ""))
                )
                corroborating = list(getattr(result, "corroborating_evidence_ids", None) or [])
            except Exception:
                return result, []
            if status != VerificationStatus.INCONCLUSIVE.value or corroborating:
                return result, []
        try:
            rebound, repairs = self.repair_failed_groups(
                hypothesis, evidence_bank, contradictions, sibling_hypotheses
            )
        except Exception:
            return result, []
        if version != CLAIM_BINDING_CBV3 or not rebound or not repairs:
            return result, repairs
        if enforcement is None:
            return result, repairs
        try:
            bank_index = self._bank_index_for(evidence_bank)
            mapping = self._repair_mapping(repairs)
            if mapping is None:
                # The same cited ID can belong to multiple typed groups.
                # Conflicting donor choices cannot be represented by a
                # hypothesis-wide substitution map, so keep the original
                # verdict and retain the repair records for shadow review.
                return result, repairs
            mapped_ids: dict[str, Any] = {}
            for cited_id, donor_id in mapping.items():
                try:
                    donor_row = bank_index.get(str(donor_id))
                    if donor_row is not None:
                        mapped_ids[cited_id] = donor_row.evidence_id
                except Exception:
                    continue
            mapping = mapped_ids
            if not mapping:
                return result, repairs
            rebound_hyp = self._hypothesis_with_rebound_ids(hypothesis, mapping)
            if rebound_hyp is None:
                return result, repairs
            extended = self.enforcement_with_repairs(enforcement, rebound)
            try:
                kept2 = set(extended.kept_ids or [])
                cited2 = {str(mapping.get(str(c), c)) for c in (extended.cited_ids or [])}
                working2, _, _ = bound_kept_evidence_only(list(evidence_bank or []), kept2, cited2)
            except Exception:
                return result, repairs
            result = run_fn(rebound_hyp, working2)
            return self.attach_repairs(result, repairs), repairs
        except Exception:
            return result, repairs

    @staticmethod
    def enforcement_with_repairs(
        enforcement: ClaimBindingEnforcement, rebound_ids: list[str]
    ) -> ClaimBindingEnforcement:
        """Enforcement copy extended with applied rebinds (Phase 3).

        kept_ids gains the rebound donor IDs (bounded at 200) and
        repaired_ids records them; cited_kept_count counts each rebind
        as kept so the conclude waiver treats a rebound citation as
        surviving. Pure; returns the input unchanged on any fault.
        """
        try:
            if not rebound_ids:
                return enforcement
            rebound = [str(rid) for rid in rebound_ids if str(rid or "").strip()]
            if not rebound:
                return enforcement
            kept = list(enforcement.kept_ids or [])
            have = set(kept)
            for rid in rebound:
                if rid not in have:
                    kept.append(rid)
                    have.add(rid)
            prior_repaired = list(enforcement.repaired_ids or [])
            merged_repaired = [*prior_repaired]
            for rid in rebound:
                if rid not in merged_repaired:
                    merged_repaired.append(rid)
            try:
                base_count = int(enforcement.cited_kept_count or 0)
            except Exception:
                base_count = 0
            return enforcement.model_copy(
                update={
                    "kept_ids": kept[:200],
                    "repaired_ids": merged_repaired[:30],
                    "cited_kept_count": base_count + len(rebound),
                }
            )
        except Exception:
            return enforcement

    def _claim_binding_partition(
        self,
        hypothesis: Any,
        evidence_items: list[Any],
        recorded_attempts: list[tuple[list[str], dict[str, Any] | None]] | None,
    ) -> ClaimBindingEnforcement | None:
        """Pure partition computation (raises on fault; wrapper closes it)."""
        bank: set[str] = set()
        for item in evidence_items or []:
            try:
                key = str(getattr(item, "evidence_id", "") or "").strip()
            except Exception:
                continue
            if key:
                bank.add(key)
        cited: list[str] = []
        seen: set[str] = set()

        def _cite(raw: Any) -> None:
            try:
                key = str(raw or "").strip()
            except Exception:
                return
            if key and key not in seen:
                seen.add(key)
                cited.append(key)

        try:
            for raw in getattr(hypothesis, "supporting_evidence_ids", []) or []:
                _cite(raw)
            for raw in getattr(hypothesis, "refuting_evidence_ids", []) or []:
                _cite(raw)
            for asm in getattr(hypothesis, "assessments", []) or []:
                _cite(getattr(asm, "evidence_id", None))
            for step in getattr(hypothesis, "causal_steps", []) or []:
                for raw in getattr(step, "evidence_ids", []) or []:
                    _cite(raw)
        except Exception:
            pass
        recorded = list(recorded_attempts or [])
        # Per-claim locality: each distinct typed claim resolves its cited
        # IDs against ITS OWN linked attempts only. A fetch for one claim
        # never makes another claim's citation appear claim-local — the
        # cross-claim borrowing this phase exists to prevent. Causal-step
        # IDs ride the hypothesis-wide bank path (causal_step_grounding
        # already requires banked grounding rows per step).
        from investigation_agent_platform.application.evidence.retrieval_ledger import (
            partition_citations as _partition_citations,
        )

        step_ids: set[str] = set()
        try:
            for step in getattr(hypothesis, "causal_steps", []) or []:
                for raw in getattr(step, "evidence_ids", []) or []:
                    try:
                        key = str(raw or "").strip()
                    except Exception:
                        continue
                    if key:
                        step_ids.add(key)
        except Exception:
            pass
        per_claim_local: set[str] = set()
        per_claim_fallback: set[str] = set()
        per_claim_unknown: set[str] = set()
        local_kinds: set[str] = set()
        cited_kinds: set[str] = set()
        # Per-(kind, target) group identity: "KIND|target_key". Locality is
        # recorded per group so one claim's retrieval can never satisfy
        # another claim's locality — including two targets of the SAME
        # kind (e.g. two CALL_EDGE callers).
        local_groups: set[str] = set()
        cited_groups: set[str] = set()
        distinct_groups: list[dict[str, Any]] = []
        try:
            distinct_groups = list(_distinct_typed_claims(hypothesis) or [])
        except Exception:
            distinct_groups = []
        for group in distinct_groups or []:
            try:
                group_cited = [str(i or "") for i in (group.get("cited_ids") or []) if str(i or "")]
                if not group_cited:
                    continue
                kind_value = str(group.get("kind_value") or "") or "UNTYPED"
                target_key = str(group.get("target_key") or "") or "NO_TARGET"
                group_id = f"{kind_value}|{target_key}"
                # Identity-overlap linkage only: an attempt links to this
                # claim iff it actually banked one of the claim's cited
                # rows. Query-text matching alone never confers locality —
                # the linked set is drawn from banked IDs, never from the
                # query string.
                linked_g: set[str] = set()
                for banked, _attempt in recorded:
                    try:
                        banked_set = {
                            str(raw or "").strip() for raw in banked or [] if str(raw or "").strip()
                        }
                    except Exception:
                        continue
                    if banked_set & set(group_cited):
                        linked_g |= banked_set
                local_g, fallback_g, unknown_g = _partition_citations(group_cited, linked_g, bank)
                # Linked-but-unbanked IDs (retrieved, since pruned) are
                # evaporated: cited, but no bound row exists.
                for lid in sorted((set(group_cited) & linked_g) - bank):
                    if lid not in unknown_g:
                        unknown_g.append(lid)
                if local_g:
                    local_kinds.add(kind_value)
                    local_groups.add(group_id)
                cited_kinds.add(kind_value)
                cited_groups.add(group_id)
                per_claim_local |= set(local_g)
                per_claim_fallback |= set(fallback_g)
                per_claim_unknown |= set(unknown_g)
            except Exception:
                continue
        cited_set = set(cited)
        step_kept = sorted(step_ids & bank)
        kept = sorted(set(per_claim_local) | set(per_claim_fallback) | set(step_kept))
        # Unknown: cited IDs in neither the bank nor any claim's linked
        # set (plus linked-but-unbanked, computed per claim above).
        linked_all: set[str] = set()
        for banked, _attempt in recorded:
            for raw in banked or []:
                try:
                    key = str(raw or "").strip()
                except Exception:
                    continue
                if key:
                    linked_all.add(key)
        unknown = sorted((cited_set - bank - linked_all) | per_claim_unknown)
        has_local = bool(per_claim_local)
        unmapped = not bool(cited_set & linked_all)
        linkage_observed = bool(recorded)
        # A kind is only partially bound when at least one of its cited
        # groups lacks claim-local hits — that, not kind-level locality,
        # drives the CALL_EDGE/HISTORY refusal and the UNMAPPED limitation.
        unmapped_kinds = sorted(
            {gid.split("|", 1)[0] for gid in cited_groups if gid not in local_groups}
        )
        limitations: list[str] = []
        if cited_set and unmapped_kinds:
            limitations.append(LimitationCode.UNMAPPED_CITATION.value)
        if unknown:
            limitations.append(LimitationCode.EVAPORATED_CITATION.value)
        next_action: str | None = None
        if cited_set and not kept:
            try:
                kind = VerificationEngine.effective_claim_kind(hypothesis)
                kind_value = str(getattr(kind, "value", kind) or "") if kind is not None else ""
            except Exception:
                kind_value = ""
            next_action = (
                _MISSING_OBLIGATION_DIRECTIVES.get(kind_value or "")
                or _DEFAULT_MISSING_OBLIGATION_DIRECTIVE
            )
        return ClaimBindingEnforcement(
            cited_ids=cited[:200],
            kept_ids=kept[:200],
            evaporated_ids=sorted(unknown)[:100],
            local_kinds=sorted(local_kinds)[:20],
            cited_kinds=sorted(cited_kinds)[:20],
            local_groups=sorted(local_groups)[:100],
            cited_groups=sorted(cited_groups)[:100],
            unmapped_kinds=sorted(unmapped_kinds)[:20],
            cited_kept_count=len(set(kept) & cited_set),
            unmapped=unmapped,
            has_claim_local=has_local,
            linkage_observed=linkage_observed,
            limitation_codes=limitations[:10],
            next_action=next_action,
        )

    def _apply_binding_limitations(
        self, result: VerificationResult, codes: list[str]
    ) -> VerificationResult:
        """Attach Phase 1 limitation codes to a VERIFIED result (additive)."""
        try:
            existing = list(getattr(result, "limitations", None) or [])
            have = set()
            for lim in existing:
                try:
                    have.add(
                        str(getattr(getattr(lim, "code", ""), "value", getattr(lim, "code", "")))
                    )
                except Exception:
                    continue
            additions = []
            for code in codes or []:
                if code in have:
                    continue
                try:
                    additions.append(
                        VerificationLimitation(
                            code=LimitationCode(code),
                            detail=f"Phase 1 claim binding: {code}.",
                        )
                    )
                except Exception:
                    continue
            if not additions:
                return result
            return result.model_copy(update={"limitations": [*existing, *additions]})
        except Exception:
            return result

    @traced("reason", require_scope=False, extra_from=_verification_extra)
    def evaluate_hypothesis(
        self,
        hypothesis: Hypothesis,
        evidence_items: list[Evidence],
        contradictions: list[Contradiction],
        causal_chain: list[CausalRelationship],
        policy: RootCauseVerificationPolicy,
        sibling_hypotheses: list[Any] | None = None,
        observer: Callable[[dict[str, Any]], None] | None = None,
        recorded_attempts: list[tuple[list[str], dict[str, Any] | None]] | None = None,
        evidence_bank: list[Any] | None = None,
    ) -> VerificationResult:
        """Public verdict entry point (WP-A2 wrapper + Phase 1 binding).

        Under pin cbv2 the working evidence set is filtered to kept
        citations before the inner evaluator runs, and a hypothesis whose
        cited set keeps zero IDs evaluates INCONCLUSIVE (never VERIFIED).
        Under cbv1/unmarked the legacy bank path decides byte-identically.
        Diagnostics (evaporated IDs, demotions, missing obligations, G3
        claim bindings) are attached afterwards; ``recorded_attempts``
        (timeline-backed, oldest first; ``None`` for legacy callers)
        feeds G3 bindings and Phase 1 linkage. ``evidence_bank`` (the
        full investigation bank; ``None`` for legacy callers) feeds only
        the Phase 3 donor pool and re-run join — the verdict's working
        set stays ``evidence_items``. The optional observer
        carries the T2b mirror note through to the inner gate; ``None``
        (all existing callers) preserves the exact legacy call shape.

        Phase 4: the assessment projection runs first. NOT_CHECKABLE
        groups are discarded from the working hypothesis before binding
        and verification; if the projection is empty the opinion-only
        result returns here and no kind gate ever sees these claims.
        """
        # Phase 4 (claim binding): partition assessments by kind. An
        # assessment-level scan (not hypothesis-level claim_kind) so mixed
        # hypotheses project correctly. NOT_CHECKABLE groups are discarded
        # from the working hypothesis before binding and verification; the
        # original ID is preserved so joins stay stable.
        try:
            _asms = list(getattr(hypothesis, "assessments", None) or [])
        except Exception:
            _asms = []
        try:
            _checkable_asms = [
                a
                for a in _asms
                if str(
                    getattr(getattr(a, "claim_kind", None), "value", getattr(a, "claim_kind", None))
                    or ""
                )
                != ClaimKind.NOT_CHECKABLE.value
            ]
        except Exception:
            _checkable_asms = list(_asms)
        try:
            _typed = [a for a in _asms if getattr(a, "claim_kind", None) is not None]
        except Exception:
            _typed = []
        # Finding N: opinion-only screen is the shared predicate (not an
        # inline re-implementation) so the marker, the activities, and
        # this projection cannot drift apart. Import failure falls back
        # to the pre-N inline condition, never to a pass-through.
        try:
            _opinion_only = bool(_all_typed_claims_not_checkable(hypothesis))
        except Exception:
            try:
                _opinion_only = bool(_typed and len(_checkable_asms) == 0)
            except Exception:
                _opinion_only = False
        if _opinion_only:
            return self._not_checkable_inconclusive(hypothesis)
        if 0 < len(_checkable_asms) < len(_asms):
            try:
                _copy = hypothesis.model_copy(update={"assessments": list(_checkable_asms)})
            except Exception:
                _copy = None
            if _copy is not None:
                hypothesis = _copy
        enforcement = self.claim_binding_enforcement(
            hypothesis, list(evidence_items or []), recorded_attempts
        )
        if enforcement is not None and enforcement.fault:
            # Fail closed: partitioning faulted under cbv2. Never fall
            # through to the legacy bank path (which could VERIFY on
            # evaporated rows).
            return self._fault_inconclusive(hypothesis)
        working_items = list(evidence_items or [])
        if enforcement is not None:
            try:
                kept = set(enforcement.kept_ids)
                cited = set(enforcement.cited_ids)
                working_items, _, _ = bound_kept_evidence_only(working_items, kept, cited)
            except Exception:
                working_items = list(evidence_items or [])
        result = self._evaluate_hypothesis_inner(
            hypothesis,
            working_items,
            contradictions,
            causal_chain,
            policy,
            sibling_hypotheses,
            observer,
        )
        # Phase 3 (claim binding): gate-failure recovery on INCONCLUSIVE
        # with empty corroborating evidence. Shadow (cbv2) records only;
        # applied (cbv3) substitutes donors and re-runs the inner once.
        repair_records: list[RepairedBinding] = []
        if enforcement is not None and self.claim_binding_version in (
            CLAIM_BINDING_CBV2,
            CLAIM_BINDING_CBV3,
        ):
            try:
                _status = str(
                    getattr(
                        getattr(result, "status", ""),
                        "value",
                        getattr(result, "status", ""),
                    )
                )
                _corr = list(getattr(result, "corroborating_evidence_ids", None) or [])
            except Exception:
                _status, _corr = "", [None]
            if _status == VerificationStatus.INCONCLUSIVE.value and not _corr:
                try:
                    _rebound, repair_records = self.repair_failed_groups(
                        hypothesis,
                        list(evidence_items or []),
                        contradictions,
                        sibling_hypotheses,
                        evidence_bank=evidence_bank,
                    )
                except Exception:
                    _rebound, repair_records = [], []
                if _rebound and repair_records and self.claim_binding_version == CLAIM_BINDING_CBV3:
                    try:
                        _bank_index = self._bank_index_for(
                            list(evidence_bank or []) or list(evidence_items or [])
                        )
                        _mapping = self._repair_mapping(repair_records)
                        if _mapping is not None:
                            _mapping = {
                                str(_cited): _bank_index[str(_donor)].evidence_id
                                for _cited, _donor in _mapping.items()
                                if str(_donor) in _bank_index
                            }
                        if _mapping:
                            _rerun_mapping = dict(_mapping)
                            _rebound_hyp = self._hypothesis_with_rebound_ids(hypothesis, _mapping)
                        else:
                            _rerun_mapping = {}
                            _rebound_hyp = None
                    except Exception:
                        _rebound_hyp = None
                    if _rebound_hyp is not None:
                        try:
                            _kept2 = set(enforcement.kept_ids or []) | set(_rebound)
                            _cited2 = {
                                str(_rerun_mapping.get(str(c), c))
                                for c in (enforcement.cited_ids or [])
                            }
                            # Donor rows may live only in the bank (the
                            # verdict's working set can exclude uncited
                            # rows); join them before kept-filtering.
                            _rerun_pool = list(evidence_items or [])
                            try:
                                _have = set()
                                for _wrow in _rerun_pool:
                                    try:
                                        _have.add(str(getattr(_wrow, "evidence_id", "") or ""))
                                    except Exception:
                                        continue
                                for _did in _rebound:
                                    if _did not in _have and _did in _bank_index:
                                        _rerun_pool.append(_bank_index[_did])
                            except Exception:
                                pass
                            _working2, _, _ = bound_kept_evidence_only(_rerun_pool, _kept2, _cited2)
                        except Exception:
                            _working2 = list(evidence_items or [])
                        try:
                            result = self._evaluate_hypothesis_inner(
                                _rebound_hyp,
                                _working2,
                                contradictions,
                                causal_chain,
                                policy,
                                sibling_hypotheses,
                                observer,
                            )
                            enforcement = self.enforcement_with_repairs(enforcement, _rebound)
                        except Exception:
                            pass
        if enforcement is not None:
            result = self.enforce_claim_binding(hypothesis, result, enforcement)
        try:
            diagnostics = collect_verification_diagnostics(
                hypothesis, evidence_items, result, recorded_attempts
            )
            if diagnostics is not None:
                existing = getattr(result, "diagnostics", None)
                if (
                    enforcement is not None
                    and list(enforcement.cited_ids or [])
                    and enforcement.cited_kept_count == 0
                    and existing is not None
                ):
                    # Zero-kept enforcement already recorded authoritative
                    # evaporated/demotion/missing records; merge only the
                    # recomputed bindings/ledgers instead of replacing them.
                    try:
                        result = result.model_copy(
                            update={
                                "diagnostics": existing.model_copy(
                                    update={
                                        "claim_bindings": diagnostics.claim_bindings,
                                        "claim_ledgers": diagnostics.claim_ledgers,
                                    }
                                )
                            }
                        )
                    except Exception:
                        result = result.model_copy(update={"diagnostics": diagnostics})
                else:
                    result = result.model_copy(update={"diagnostics": diagnostics})
        except Exception:
            pass
        # Phase 3: attach repair records (shadow or applied) to the
        # diagnostics collected on the original hypothesis (the audit
        # trail of what was cited), plus REPAIRED_CITATION when an
        # applied repair verifies.
        try:
            if repair_records:
                result = self.attach_repairs(result, repair_records)
        except Exception:
            pass
        return result

    def _not_checkable_inconclusive(self, hypothesis: Any) -> VerificationResult:
        """Opinion-only verdict: every typed claim is NOT_CHECKABLE (pure).

        No gates run, no obligations emit — unfalsifiable claims are
        skipped, never grounded. Mixed hypotheses never reach here (the
        evaluate-level projection filters before this point).
        """
        try:
            hid = getattr(hypothesis, "id", None) or uuid4()
        except Exception:
            hid = uuid4()
        return VerificationResult(
            hypothesis_id=hid,
            status=VerificationStatus.INCONCLUSIVE,
            corroborating_evidence_ids=[],
            explanation=(
                "All typed claims are NOT_CHECKABLE (taste/preference/value "
                "judgments or unfalsifiable claims); skipped, never verified."
            ),
            confidence=InvestigationConfidence(
                coverage_score=0.0,
                reliability_score=0.0,
                causal_score=0.0,
                contradiction_penalty=0.0,
            ),
            limitations=[
                VerificationLimitation(
                    code=LimitationCode.NOT_CHECKABLE_SKIPPED,
                    detail="opinion-only hypothesis: skipped, never verified",
                )
            ],
        )

    @staticmethod
    def _fault_inconclusive(hypothesis: Any) -> VerificationResult:
        """Fail-closed verdict for a binding enforcement fault (pure)."""
        try:
            hid = getattr(hypothesis, "id", None) or uuid4()
        except Exception:
            hid = uuid4()
        return VerificationResult(
            hypothesis_id=hid,
            status=VerificationStatus.INCONCLUSIVE,
            corroborating_evidence_ids=[],
            explanation=(
                "Claim binding enforcement faulted; failing closed to "
                "INCONCLUSIVE rather than deciding on an unpartitioned bank."
            ),
            confidence=InvestigationConfidence(
                coverage_score=0.0,
                reliability_score=0.0,
                causal_score=0.0,
                contradiction_penalty=0.0,
            ),
        )

    @staticmethod
    def typed_claim_groups(hypothesis: Any) -> tuple[dict[tuple[str, str], list[Any]], int]:
        """Checkable assessments grouped by (claim kind, target fingerprint) (pure).

        Returns ``(groups, not_checkable_count)``. NOT_CHECKABLE assessments
        are counted, never grouped; untyped assessments are ignored. The
        target fingerprint is the one verification already uses
        (``target_group_key``), so identities agree across layers.
        """
        groups: dict[tuple[str, str], list[Any]] = {}
        not_checkable = 0
        try:
            assessments = list(getattr(hypothesis, "assessments", None) or [])
        except Exception:
            assessments = []
        for asm in assessments:
            kind = getattr(asm, "claim_kind", None)
            if kind is None:
                continue
            if kind == ClaimKind.NOT_CHECKABLE:
                not_checkable += 1
                continue
            try:
                key = (str(kind.value), target_group_key(getattr(asm, "claim_target", None))[0])
            except Exception:
                key = (str(getattr(kind, "value", kind)), "NO_TARGET")
            groups.setdefault(key, []).append(asm)
        return groups, not_checkable

    def _group_verifies(
        self,
        view: Any,
        kind: str,
        rows: list[Any],
        contradictions: list[Any],
        causal_chain: list[Any],
        policy: RootCauseVerificationPolicy,
    ) -> bool:
        """Verify ONE claim group with the validator for its kind (H3).

        DEFINITION / CALL_EDGE use the location proof, HISTORY the commit
        proof, and BEHAVIOR / ABSENCE / CAUSAL the general verdict - each over
        a hypothesis view holding only this group's assessments.
        """
        try:
            if kind in (ClaimKind.DEFINITION.value, ClaimKind.CALL_EDGE.value):
                verdict = self.validate_location_claim(view, rows, contradictions)
            elif kind == ClaimKind.HISTORY.value:
                verdict = self.validate_history_claim(view, rows, contradictions)
            elif kind in (
                ClaimKind.BEHAVIOR.value,
                ClaimKind.ABSENCE.value,
                ClaimKind.CAUSAL.value,
            ):
                verdict = self.evaluate_hypothesis(view, rows, contradictions, causal_chain, policy)
            else:
                return False
            return verdict is not None and verdict.status == VerificationStatus.VERIFIED
        except Exception:
            return False

    def failed_cited_group_ids(
        self,
        hypothesis: Any,
        result: Any,
        evidence_items: list[Any],
        contradictions: list[Any],
        causal_chain: list[Any],
        policy: RootCauseVerificationPolicy,
        include_no_target: bool = False,
    ) -> list[str]:
        """Cited checkable groups that fail their own gate (review H8, lv3). Pure.

        Returns ``"KIND|target_key"`` ids, sorted. Only meaningful when the taken
        verdict is not VERIFIED (otherwise nothing needs a re-fetch). Each group
        is re-verified over the rows IT cites, exactly as
        ``enforce_group_conjunction`` does. Uncited groups are excluded (they are
        already smallest-fetch gaps), as are NOT_CHECKABLE groups. Targetless
        groups are excluded unless ``include_no_target`` (H8b, LV_H) and the kind
        is BEHAVIOR or CAUSAL. A cited DEFINITION/CALL_EDGE group none of whose
        cited rows is bound counts as failed: its citation evaporated. Never
        raises; fault -> [].
        """
        try:
            if getattr(result, "status", None) == VerificationStatus.VERIFIED:
                return []
            groups, _nc = VerificationEngine.typed_claim_groups(hypothesis)
            by_id = {str(getattr(e, "evidence_id", "")): e for e in evidence_items or []}
            failed: list[str] = []
            for (kind, target_key), asms in groups.items():
                if target_key == "NO_TARGET" and not (
                    include_no_target and kind in ("BEHAVIOR", "CAUSAL")
                ):
                    continue
                cited: list[str] = []
                for asm in asms:
                    evidence_id = getattr(asm, "evidence_id", None)
                    if evidence_id is not None and str(evidence_id) not in cited:
                        cited.append(str(evidence_id))
                if not cited:
                    continue
                own_rows = [by_id[c] for c in cited if c in by_id]
                gid = f"{kind}|{target_key}"
                if not own_rows and kind in (
                    ClaimKind.DEFINITION.value,
                    ClaimKind.CALL_EDGE.value,
                ):
                    failed.append(gid)
                    continue
                rows = own_rows or list(evidence_items or [])
                view = hypothesis.model_copy(
                    update={
                        "assessments": list(asms),
                        "supporting_evidence_ids": [
                            getattr(by_id[c], "evidence_id", None) for c in cited if c in by_id
                        ],
                    }
                )
                if not self._group_verifies(view, kind, rows, contradictions, causal_chain, policy):
                    failed.append(gid)
            return sorted(set(failed))
        except Exception:
            return []

    def enforce_group_conjunction(
        self,
        hypothesis: Any,
        result: VerificationResult,
        evidence_items: list[Any],
        contradictions: list[Any],
        causal_chain: list[Any],
        policy: RootCauseVerificationPolicy,
    ) -> VerificationResult:
        """A VERIFIED verdict must hold for EVERY checkable claim group (H3/H4).

        The fast paths decide from the first assessment's kind and target, so
        a hypothesis like ``[DEFINITION(grounded), CAUSAL(no evidence)]``
        verified on the definition alone and published the causal statement
        with it. Here every distinct checkable (kind, target) group is
        re-verified over the rows IT cites; if any group fails, the verdict is
        withdrawn to INCONCLUSIVE with an UNVERIFIED_CLAIM_GROUP limitation.
        Hypotheses with a single checkable group are returned unchanged (the
        existing proof already covers them). When the verdict stands and
        NOT_CHECKABLE claims were excluded, a NOT_CHECKABLE_EXCLUDED
        limitation records that they are not certified. Pure.
        """
        if getattr(result, "status", None) != VerificationStatus.VERIFIED:
            return result
        groups, not_checkable = VerificationEngine.typed_claim_groups(hypothesis)
        if len(groups) > 1:
            by_id = {str(getattr(e, "evidence_id", "")): e for e in evidence_items or []}
            failures: list[str] = []
            for (kind, target_key), asms in groups.items():
                cited: list[str] = []
                for asm in asms:
                    evidence_id = getattr(asm, "evidence_id", None)
                    if evidence_id is not None and str(evidence_id) not in cited:
                        cited.append(str(evidence_id))
                own_rows = [by_id[c] for c in cited if c in by_id]
                needs_cited_rows = kind in (
                    ClaimKind.DEFINITION.value,
                    ClaimKind.CALL_EDGE.value,
                )
                if own_rows:
                    rows = own_rows
                elif needs_cited_rows:
                    failures.append(f"{kind}|{target_key}: cites no bound row")
                    continue
                else:
                    # Behavior/absence/causal/history proofs can rest on the
                    # causal chain or a scoped empty search rather than a
                    # row cited per assessment; evaluate over the bound set.
                    rows = list(evidence_items or [])
                try:
                    cited_ids = [
                        getattr(by_id[c], "evidence_id", None) for c in cited if c in by_id
                    ]
                    view = hypothesis.model_copy(
                        update={
                            "assessments": list(asms),
                            "supporting_evidence_ids": [e for e in cited_ids if e is not None],
                        }
                    )
                except Exception:
                    failures.append(f"{kind}|{target_key}: group view unavailable")
                    continue
                if not self._group_verifies(view, kind, rows, contradictions, causal_chain, policy):
                    failures.append(f"{kind}|{target_key}: not verified over its cited rows")
            if failures:
                try:
                    hid = getattr(hypothesis, "id", None) or result.hypothesis_id
                except Exception:
                    hid = result.hypothesis_id
                return VerificationResult(
                    hypothesis_id=hid,
                    status=VerificationStatus.INCONCLUSIVE,
                    corroborating_evidence_ids=[],
                    explanation=(
                        "Verified on one claim, but "
                        f"{len(failures)} other checkable claim group(s) did not "
                        "verify over the rows they cite; the verdict is withdrawn."
                    ),
                    confidence=InvestigationConfidence(
                        coverage_score=0.0,
                        reliability_score=0.0,
                        causal_score=0.0,
                        contradiction_penalty=0.0,
                    ),
                    limitations=[
                        VerificationLimitation(
                            code=LimitationCode.UNVERIFIED_CLAIM_GROUP,
                            detail="; ".join(failures)[:500],
                        )
                    ],
                )
        if getattr(policy, "cross_claim_checks", False) and len(groups) > 1:
            try:
                from investigation_agent_platform.application.investigation.cross_claim import (
                    cross_claim_conflicts,
                )

                conflicts = cross_claim_conflicts(groups)
                blocking = [c for c in conflicts if c.blocking]
                mixed = [c for c in conflicts if not c.blocking]
            except Exception:
                try:
                    hid = getattr(hypothesis, "id", None) or result.hypothesis_id
                except Exception:
                    hid = result.hypothesis_id
                return VerificationResult(
                    hypothesis_id=hid,
                    status=VerificationStatus.INCONCLUSIVE,
                    corroborating_evidence_ids=[],
                    explanation="Cross-claim inspection faulted; failing closed.",
                    confidence=InvestigationConfidence(
                        coverage_score=0.0,
                        reliability_score=0.0,
                        causal_score=0.0,
                        contradiction_penalty=0.0,
                    ),
                    limitations=[
                        VerificationLimitation(
                            code=LimitationCode.CLAIM_CONFLICT,
                            detail="cross-claim inspection faulted",
                        )
                    ],
                )
            if blocking:
                try:
                    hid = getattr(hypothesis, "id", None) or result.hypothesis_id
                except Exception:
                    hid = result.hypothesis_id
                return VerificationResult(
                    hypothesis_id=hid,
                    status=VerificationStatus.INCONCLUSIVE,
                    corroborating_evidence_ids=[],
                    explanation=(
                        "Verified per group, but the groups contradict each other: "
                        + "; ".join(c.detail for c in blocking)[:400]
                    ),
                    confidence=InvestigationConfidence(
                        coverage_score=0.0,
                        reliability_score=0.0,
                        causal_score=0.0,
                        contradiction_penalty=0.0,
                    ),
                    limitations=[
                        VerificationLimitation(
                            code=LimitationCode.CLAIM_CONFLICT,
                            detail="; ".join(c.detail for c in blocking)[:500],
                        )
                    ],
                )
            if mixed:
                existing = list(getattr(result, "limitations", None) or [])
                existing.append(
                    VerificationLimitation(
                        code=LimitationCode.REVISION_MIXED,
                        detail=mixed[0].detail,
                    )
                )
                result = result.model_copy(update={"tier": "likely", "limitations": existing})
        if not_checkable > 0:
            try:
                existing = list(getattr(result, "limitations", None) or [])
                if not any(
                    getattr(lim, "code", None) == LimitationCode.NOT_CHECKABLE_EXCLUDED
                    for lim in existing
                ):
                    existing.append(
                        VerificationLimitation(
                            code=LimitationCode.NOT_CHECKABLE_EXCLUDED,
                            detail=(
                                f"{not_checkable} NOT_CHECKABLE claim(s) were excluded from "
                                "verification; this verdict does not certify them."
                            ),
                        )
                    )
                    result = result.model_copy(update={"limitations": existing})
            except Exception:
                pass
        return result

    def enforce_claim_binding(
        self,
        hypothesis: Any,
        result: VerificationResult,
        enforcement: ClaimBindingEnforcement,
    ) -> VerificationResult:
        """Apply the authoritative verdict branch for a cbv2 partition.

        Cited-but-kept-zero can never VERIFY; bank-fallback-only
        CALL_EDGE/HISTORY verification is refused; otherwise limitations
        are attached additively and corroborating IDs are filtered to
        kept citations. A fault enforcement fails closed to INCONCLUSIVE
        (this is the single choke point, so fast paths are covered).
        """
        try:
            if getattr(enforcement, "fault", False):
                return self._fault_inconclusive(hypothesis)
            cited = list(enforcement.cited_ids or [])
            kept = set(enforcement.kept_ids or [])
            if cited and enforcement.cited_kept_count == 0:
                try:
                    kind = VerificationEngine.effective_claim_kind(hypothesis)
                    kind_value = (
                        str(getattr(kind, "value", kind) or "") if kind is not None else None
                    )
                except Exception:
                    kind_value = None
                try:
                    statement = str(getattr(hypothesis, "statement", "") or "").strip()
                except Exception:
                    statement = ""
                missing = MissingObligation(
                    claim=statement[:1024],
                    claim_kind=kind_value,
                    next_action=(enforcement.next_action or _DEFAULT_MISSING_OBLIGATION_DIRECTIVE),
                )
                evaporated = [
                    EvaporatedId(cited_id=c, cited_by=str(getattr(hypothesis, "id", "") or "")[:64])
                    for c in enforcement.evaporated_ids[:100]
                ]
                try:
                    demotion_hyp_id = getattr(hypothesis, "id", None) or result.hypothesis_id
                except Exception:
                    demotion_hyp_id = result.hypothesis_id
                demotion = Demotion(
                    hypothesis_id=demotion_hyp_id,
                    from_tier="established",
                    to_tier="likely",
                    reason_code=DiagnosticReasonCode.CITED_ID_EVAPORATED,
                    cause_codes=list(enforcement.limitation_codes or [])[:10],
                    detail=(
                        "Phase 1 claim binding: cited evidence kept zero "
                        "bound rows; claim is INCONCLUSIVE with a smallest-fetch "
                        f"next action ({missing.next_action or 're-verify'})."
                    )[:500],
                )
                diagnostics = VerificationDiagnostics(
                    evaporated_ids=evaporated,
                    demotions=[demotion],
                    missing_obligations=[missing],
                    reason_codes=sorted(
                        {e.reason_code.value for e in evaporated}
                        | {demotion.reason_code.value, missing.reason_code.value}
                    ),
                )
                return VerificationResult(
                    hypothesis_id=getattr(hypothesis, "id", None) or result.hypothesis_id,
                    status=VerificationStatus.INCONCLUSIVE,
                    corroborating_evidence_ids=[],
                    explanation=(
                        "Claim cites evidence but zero cited IDs resolve to "
                        "bound rows (evaporated); INCONCLUSIVE pending the "
                        "smallest-fetch re-retrieval."
                    ),
                    confidence=InvestigationConfidence(
                        coverage_score=0.0,
                        reliability_score=0.0,
                        causal_score=0.0,
                        contradiction_penalty=0.0,
                    ),
                    diagnostics=diagnostics,
                )
            # Corroborating IDs are filtered to kept citations — but only
            # for claims that cite evidence. Uncited claims (target-declared
            # proof, the existing vocabulary) keep the gate's corroborating
            # set untouched; the zero-kept branch above already handled
            # cited-but-unbound.
            if cited:
                # Phase 3 (claim binding): rebound donor IDs survive the
                # kept filter — a successful rebind stands in for its
                # citation. repaired_ids is empty outside applied repair.
                try:
                    _repaired = set(enforcement.repaired_ids or [])
                except Exception:
                    _repaired = set()
                try:
                    corroborating = [
                        eid
                        for eid in (getattr(result, "corroborating_evidence_ids", None) or [])
                        if str(eid) in kept or str(eid) in _repaired
                    ]
                except Exception:
                    corroborating = list(getattr(result, "corroborating_evidence_ids", None) or [])
                try:
                    result = result.model_copy(update={"corroborating_evidence_ids": corroborating})
                except Exception:
                    pass
            # Locality refusal is per (kind, target) group and applies only
            # when linkage was observed: without timeline attempts there
            # is no linkage to be absent from, so unmapped bank-path rows
            # verify with the UNMAPPED limitation instead of being refused
            # for missing telemetry. A CALL_EDGE/HISTORY claim is refused
            # when any cited group of that kind retrieved nothing
            # claim-local — including a second target of the same kind
            # whose sibling target is local (no cross-claim borrowing).
            _refuse_locality = False
            if cited and enforcement.linkage_observed:
                try:
                    kind = VerificationEngine.effective_claim_kind(hypothesis)
                    kind_value = str(getattr(kind, "value", kind) or "") if kind is not None else ""
                except Exception:
                    kind = None
                    kind_value = ""
                _refuse_locality = kind in (
                    ClaimKind.CALL_EDGE,
                    ClaimKind.HISTORY,
                ) and kind_value in (enforcement.unmapped_kinds or [])
            if _refuse_locality:
                try:
                    _status_value = str(
                        getattr(
                            getattr(result, "status", ""), "value", getattr(result, "status", "")
                        )
                    )
                except Exception:
                    _status_value = ""
                if _status_value == VerificationStatus.VERIFIED.value:
                    # Bank-fallback-only CALL_EDGE/HISTORY verification is
                    # refused: the verified claim's own group retrieved
                    # nothing claim-local.
                    return self.enforce_claim_binding(
                        hypothesis,
                        result.model_copy(
                            update={
                                "status": VerificationStatus.INCONCLUSIVE,
                                "corroborating_evidence_ids": [],
                            }
                        ),
                        enforcement.model_copy(update={"cited_kept_count": 0, "kept_ids": []}),
                    )
            result = self._apply_binding_limitations(
                result,
                [LimitationCode.UNMAPPED_CITATION.value]
                if LimitationCode.UNMAPPED_CITATION.value in (enforcement.limitation_codes or [])
                else [],
            )
            if enforcement.limitation_codes:
                result = self._apply_binding_limitations(result, enforcement.limitation_codes)
            return result
        except Exception:
            # Fail closed: an enforcement fault never returns the
            # pre-enforcement (possibly VERIFIED) result.
            try:
                return self._fault_inconclusive(hypothesis)
            except Exception:
                return VerificationResult(
                    hypothesis_id=result.hypothesis_id,
                    status=VerificationStatus.INCONCLUSIVE,
                    corroborating_evidence_ids=[],
                    explanation=("Claim binding enforcement faulted; failing closed."),
                    confidence=InvestigationConfidence(
                        coverage_score=0.0,
                        reliability_score=0.0,
                        causal_score=0.0,
                        contradiction_penalty=0.0,
                    ),
                )

    def _evaluate_hypothesis_inner(
        self,
        hypothesis: Hypothesis,
        evidence_items: list[Evidence],
        contradictions: list[Contradiction],
        causal_chain: list[CausalRelationship],
        policy: RootCauseVerificationPolicy,
        sibling_hypotheses: list[Any] | None = None,
        observer: Callable[[dict[str, Any]], None] | None = None,
    ) -> VerificationResult:
        if VerificationEngine.concedes_claim(getattr(hypothesis, "statement", "")):
            confidence = InvestigationConfidence(
                coverage_score=0.0,
                reliability_score=0.0,
                causal_score=0.0,
                contradiction_penalty=0.0,
            )
            return VerificationResult(
                hypothesis_id=hypothesis.id,
                status=VerificationStatus.INCONCLUSIVE,
                corroborating_evidence_ids=[],
                explanation=(
                    "Hypothesis statement concedes its own claim (inability "
                    "language); abstention-shaped claims never verify."
                ),
                confidence=confidence,
            )
        # D4 enforcement: a claim whose only support traces to fuzzy
        # orientation never verifies — orientation steers deterministic reads,
        # it is never proof. Runs before every kind-specific path so no claim
        # shape can slip past on an untyped or docs-routed proof.
        orientation_only, orientation_reason = VerificationEngine.cites_only_orientation(
            hypothesis, evidence_items, causal_chain
        )
        if orientation_only:
            return VerificationResult(
                hypothesis_id=hypothesis.id,
                status=VerificationStatus.INCONCLUSIVE,
                corroborating_evidence_ids=[],
                explanation=(
                    "Orientation-only claim cannot verify: "
                    f"{orientation_reason}. Fuzzy orientation steers "
                    "deterministic reads; cite the range-read evidence those "
                    "reads bank, never the hint."
                ),
                confidence=InvestigationConfidence(
                    coverage_score=0.0,
                    reliability_score=0.0,
                    causal_score=0.0,
                    contradiction_penalty=0.0,
                ),
            )
        if (
            VerificationEngine.claims_field_value(getattr(hypothesis, "statement", ""))
            or VerificationEngine._fact_category_of(hypothesis) is not None
        ):
            java_category = VerificationEngine._fact_category_of(hypothesis)
            if java_category is not None:
                java_result = self.validate_java_fact_claim(
                    hypothesis, evidence_items, contradictions
                )
                if java_result is not None:
                    return java_result
                partial_result = self.java_partial_result(hypothesis, evidence_items)
                if partial_result is not None:
                    return partial_result
                _java_ok, java_reason = self.java_fact_proof(hypothesis, evidence_items)
                return VerificationResult(
                    hypothesis_id=hypothesis.id,
                    status=VerificationStatus.INCONCLUSIVE,
                    corroborating_evidence_ids=[],
                    explanation=f"Java fact claim is not established: {java_reason}.",
                    confidence=InvestigationConfidence(
                        coverage_score=0.0,
                        reliability_score=0.0,
                        causal_score=0.0,
                        contradiction_penalty=0.0,
                    ),
                )
            value_result = self.validate_code_value_claim(
                hypothesis, evidence_items, contradictions
            )
            if value_result is not None:
                return value_result
            _value_ok, value_reason = self.code_value_proof(hypothesis, evidence_items)
            return VerificationResult(
                hypothesis_id=hypothesis.id,
                status=VerificationStatus.INCONCLUSIVE,
                corroborating_evidence_ids=[],
                explanation=f"Code value claim is not established: {value_reason}.",
                confidence=InvestigationConfidence(
                    coverage_score=0.0,
                    reliability_score=0.0,
                    causal_score=0.0,
                    contradiction_penalty=0.0,
                ),
            )
        kind_gate = self.check_claim_kind(hypothesis, evidence_items)
        behavior_likely_only = False
        _gate_kind = VerificationEngine.effective_claim_kind(hypothesis)
        _record_gate_note(
            observer,
            {
                "tap": "T2b",
                "input_set": "BOUND_FULL",
                "kind": str(getattr(_gate_kind, "value", _gate_kind))
                if _gate_kind is not None
                else "UNKNOWN",
                "ok": None if kind_gate is None else bool(kind_gate[0]),
                "reason": "" if kind_gate is None else str(kind_gate[1]),
                "behavior_likely_only": False,
            },
        )
        if kind_gate is not None and not kind_gate[0]:
            kind = VerificationEngine.effective_claim_kind(hypothesis)
            if (
                kind == ClaimKind.BEHAVIOR
                and VerificationEngine.behavior_has_code_path(evidence_items)
                and self._rival_tested_for(hypothesis, sibling_hypotheses)
            ):
                # Code-complete behavior with a tested rival but no runtime
                # observation (routine in code-only scopes): proceed to
                # scoring, then cap at likely with NO_RUNTIME_OBSERVATION.
                # Demote-only — never established, never blocked.
                behavior_likely_only = True
                _record_gate_note(
                    observer,
                    {
                        "tap": "T2b",
                        "input_set": "BOUND_FULL",
                        "kind": "BEHAVIOR",
                        "ok": False,
                        "reason": "behavior-likely-only",
                        "behavior_likely_only": True,
                    },
                )
            else:
                return VerificationResult(
                    hypothesis_id=hypothesis.id,
                    status=VerificationStatus.INCONCLUSIVE,
                    corroborating_evidence_ids=[],
                    explanation=(
                        f"{kind.value if kind else 'Claim'} claim not demonstrated by "
                        f"its cited evidence: {kind_gate[1]}."
                    ),
                    confidence=InvestigationConfidence(
                        coverage_score=0.3,
                        reliability_score=0.5,
                        causal_score=0.0,
                        contradiction_penalty=0.0,
                    ),
                )
        if VerificationEngine.claims_definition(
            getattr(hypothesis, "statement", "")
        ) and not VerificationEngine.has_range_read(evidence_items):
            confidence = InvestigationConfidence(
                coverage_score=0.3,
                reliability_score=0.5,
                causal_score=0.0,
                contradiction_penalty=0.0,
            )
            _record_gate_note(
                observer,
                {
                    "tap": "T2b",
                    "input_set": "BOUND_FULL",
                    "kind": "DEFINITION",
                    "ok": False,
                    "reason": "definition-backstop-no-range-read",
                    "backstop_triggered": True,
                },
            )
            return VerificationResult(
                hypothesis_id=hypothesis.id,
                status=VerificationStatus.INCONCLUSIVE,
                corroborating_evidence_ids=[],
                explanation=(
                    "Definition-strength claim without a range-read citation; "
                    "SEARCH matches establish presence, never definition — "
                    "read the file (GET_CODE) and re-verify."
                ),
                confidence=confidence,
            )
        unresolved = [c for c in contradictions if c.resolution_status == "UNRESOLVED"]
        if getattr(policy, "blocking_contradictions_only", False):
            unresolved = [
                c
                for c in unresolved
                if c.severity in (ContradictionSeverity.HIGH, ContradictionSeverity.CRITICAL)
            ]
        if len(unresolved) > policy.max_allowed_unresolved_contradictions:
            confidence = InvestigationConfidence(
                coverage_score=0.4,
                reliability_score=0.5,
                causal_score=0.2,
                contradiction_penalty=1.0,
            )
            return VerificationResult(
                hypothesis_id=hypothesis.id,
                status=VerificationStatus.CONTRADICTED,
                corroborating_evidence_ids=[],
                explanation=f"Unresolved contradictions ({len(unresolved)}) exceed limit.",
                confidence=confidence,
            )

        # P3 (S6 shape branch): an untyped claim never verifies. kind_gate
        # is None for typed CAUSAL (causal machinery + heuristic scoring
        # below) and for claims with no resolvable kind. The latter used to
        # fall through to heuristic scoring and could VERIFY on uncited
        # similarity alone — the S6 auto-pass. Placed after the
        # kind-independent safety checks (orientation, contradictions), so
        # those still adjudicate untyped claims; it guards entry to causal
        # grounding and heuristic scoring only. Untyped claims route to
        # spread (INCONCLUSIVE), never to a stop.
        if kind_gate is None:
            _typed_kind = getattr(hypothesis, "claim_kind", None)
            try:
                _typed_kind = ClaimKind(_typed_kind) if _typed_kind is not None else None
            except ValueError:
                _typed_kind = None
            if _typed_kind is None:
                return VerificationResult(
                    hypothesis_id=hypothesis.id,
                    status=VerificationStatus.INCONCLUSIVE,
                    corroborating_evidence_ids=[],
                    explanation=(
                        "Claim has no verifiable kind (untyped and matching no "
                        "definition/call-edge phrasing): type the claim or route "
                        "to spread. Untyped claims never verify."
                    ),
                    confidence=InvestigationConfidence(
                        coverage_score=0.0,
                        reliability_score=0.0,
                        causal_score=0.0,
                        contradiction_penalty=0.0,
                    ),
                )
        # H15-D2: a CAUSAL claim with no causal step does not verify (LV_H).
        # Only the effective CAUSAL kind is gated; BEHAVIOR and ABSENCE claims
        # legitimately carry no chain and have their own kind gates.
        if (
            policy.require_causal_step_for_causal_claims
            and not causal_chain
            and VerificationEngine.effective_claim_kind(hypothesis) == ClaimKind.CAUSAL
        ):
            return VerificationResult(
                hypothesis_id=hypothesis.id,
                status=VerificationStatus.INCONCLUSIVE,
                corroborating_evidence_ids=[],
                explanation=(
                    "Causal claim has no causal step: volume of evidence alone does not establish "
                    "a mechanism. Propose a cited cause-to-effect step, then re-verify."
                ),
                confidence=InvestigationConfidence(
                    coverage_score=0.3,
                    reliability_score=0.5,
                    causal_score=0.0,
                    contradiction_penalty=0.0,
                ),
            )
        # Causal steps must cite banked grounding rows (code or runtime).
        # A step citing nothing, unknown IDs, or only orientation/docs is
        # unproven — INCONCLUSIVE. Relation demonstration demotes (below),
        # it never blocks.
        step_grounded, step_reason = VerificationEngine.causal_step_grounding(
            causal_chain, evidence_items
        )
        if not step_grounded:
            return VerificationResult(
                hypothesis_id=hypothesis.id,
                status=VerificationStatus.INCONCLUSIVE,
                corroborating_evidence_ids=[],
                explanation=(f"Causal claim not grounded in cited evidence: {step_reason}."),
                confidence=InvestigationConfidence(
                    coverage_score=0.3,
                    reliability_score=0.5,
                    causal_score=0.0,
                    contradiction_penalty=0.0,
                ),
            )

        # F-050: cross-layer corroboration requires INDEPENDENT sources — distinct
        # providers/systems, not merely distinct evidence_type values. Two rows
        # from the same derived dataset must never count as corroboration.
        # Proven in production (self-investigation d16f044f): ten lines from
        # one log file previously counted as cross-layer via a source-count
        # escape hatch, capping nothing — confidence hit exactly 0.80 and a
        # single-capability conclusion was published as VERIFIED without the
        # agent ever checking state or code. Distinct source URIs from one
        # provider are volume, not corroboration.
        providers = {getattr(e, "provider", "unknown") for e in evidence_items}
        types = {e.evidence_type for e in evidence_items}
        cross_layer = (
            (len(providers) >= 2 and len(types) >= 2)
            if policy.require_cross_layer_evidence
            else True
        )

        # F-049: explicit, auditable scoring inputs instead of magic constants.
        # Coverage = corroborating evidence breadth capped by hypothesis needs;
        # reliability = fraction of evidence from sanctioned providers with
        # provenance; causal = chain completeness. Scores are labeled heuristic
        # in the explanation and never constitute proof by themselves.
        corroborating_ids = [e.evidence_id for e in evidence_items]
        coverage = (
            min(1.0, len(corroborating_ids) / 4.0)
            if cross_layer
            else min(0.5, len(corroborating_ids) / 4.0)
        )
        with_provenance = sum(
            1 for e in evidence_items if getattr(e, "provenance", None) is not None
        )
        reliability = (with_provenance / len(evidence_items)) if evidence_items else 0.0
        causal = min(1.0, len(causal_chain) / 2.0) if causal_chain else 0.0
        # Phase 2a: rival-shared rows are ambiguity, not corroboration
        # depth. The shared fraction feeds contradiction_penalty (0.20
        # weight in overall_confidence) so a winner splitting its evidence
        # with a live rival must clear the threshold on the remainder.
        # Scope: main causal path only; the specialized value/history/state
        # paths build their own confidence objects (follow-up).
        rival_shared = 0.0
        rival_shared_statements: list[str] = []
        try:
            rival_shared, rival_shared_statements = VerificationEngine.rival_evidence_overlap(
                hypothesis, sibling_hypotheses, corroborating_ids
            )
        except Exception:
            rival_shared, rival_shared_statements = 0.0, []

        confidence = InvestigationConfidence(
            coverage_score=coverage,
            reliability_score=reliability,
            causal_score=causal,
            contradiction_penalty=min(1.0, rival_shared),
        )

        status = (
            VerificationStatus.VERIFIED
            if band_at_least(
                confidence.overall_confidence,
                policy.min_confidence_threshold,
                tolerant=policy.tolerant_band,
            )
            else VerificationStatus.INCONCLUSIVE
        )
        # D5: tier + reason/against packaging for VERIFIED results. Best
        # reason is the first causal link (highest-confidence mechanism) or
        # the top corroborating item; against-evidence quotes unresolved
        # non-blocking contradictions (HIGH/CRITICAL would have blocked).
        # Phase 2.3: rival contrast demotes to likely, and typed limitations
        # name the caveats.
        tier = "established"
        best_reason = ""
        against_evidence: list[str] = []
        limitations: list[VerificationLimitation] = []
        if status == VerificationStatus.VERIFIED:
            try:
                overall = float(confidence.overall_confidence)
                likely = float(policy.likely_floor)
                established = float(policy.established_floor)
                if band_at_least(overall, likely, tolerant=policy.tolerant_band) and not band_at_least(
                    overall, established, tolerant=policy.tolerant_band
                ):
                    tier = "likely"
                elif (
                    policy.tolerant_band
                    and VerificationEngine.effective_claim_kind(hypothesis) == ClaimKind.CAUSAL
                    and len(causal_chain or []) < policy.established_requires_steps
                ):
                    # owner decision: one step is likely by design, not by accident
                    tier = "likely"
            except (TypeError, ValueError):
                pass
            try:
                if causal_chain:
                    first = causal_chain[0]
                    best_reason = (
                        f"{getattr(first, 'cause', '')} → "
                        f"{getattr(first, 'effect', '')} "
                        f"via {getattr(first, 'mechanism', '')}"
                    ).strip()[:500]
                if not best_reason and corroborating_ids:
                    best_reason = f"corroborated by {len(corroborating_ids)} evidence item(s)"
                _linked_only = bool(getattr(policy, "tolerant_band", False))
                for contradiction in contradictions or []:
                    try:
                        # H11b (LV_H): only unresolved LINKED rows count; notes
                        # (no evidence) and resolved rows never ride a verdict.
                        # Older pins keep the old selection.
                        if getattr(contradiction, "resolution_status", "") != "UNRESOLVED":
                            continue
                        if _linked_only and not getattr(contradiction, "evidence_ids", None):
                            continue
                        statement = str(getattr(contradiction, "statement", "") or "")
                        if statement:
                            against_evidence.append(statement[:300])
                    except Exception:
                        continue
                against_evidence = against_evidence[:5]
            except Exception:
                pass
        if status == VerificationStatus.VERIFIED:
            try:
                demote, rival_limits = VerificationEngine.rival_contrast(
                    hypothesis, sibling_hypotheses
                )
                limitations.extend(rival_limits)
                if demote:
                    tier = "likely"
                if rival_shared > 0.0:
                    limitations.append(
                        VerificationLimitation(
                            code=LimitationCode.RIVAL_SHARED_EVIDENCE,
                            detail=(
                                f"{rival_shared:.0%} of corroborating rows also support a "
                                f"live rival (e.g. {rival_shared_statements[0][:160] if rival_shared_statements else 'unknown'}). "
                                "Same rows, two stories: likely, not established."
                            )[:500],
                        )
                    )
                    tier = "likely"
                relation_ok, relation_reason = VerificationEngine.causal_relation_demonstrated(
                    causal_chain, evidence_items
                )
                if not relation_ok:
                    limitations.append(
                        VerificationLimitation(
                            code=LimitationCode.RELATION_UNDEMONSTRATED,
                            detail=(
                                f"{relation_reason}; the mechanism is "
                                "supported, not demonstrated: likely, not "
                                "established."
                            )[:500],
                        )
                    )
                    tier = "likely"
                if behavior_likely_only:
                    limitations.append(
                        VerificationLimitation(
                            code=LimitationCode.NO_RUNTIME_OBSERVATION,
                            detail=(
                                "Behavior established from code path with a "
                                "tested rival but no runtime observation; "
                                "code-plausible, not observed: likely, not "
                                "established."
                            )[:500],
                        )
                    )
                    tier = "likely"
                if len(providers) < 2:
                    limitations.append(
                        VerificationLimitation(
                            code=LimitationCode.SINGLE_PROVIDER,
                            detail=(
                                "All corroborating rows come from one provider "
                                f"({min(providers) if providers else 'unknown'}); "
                                "no independent corroboration."
                            )[:500],
                        )
                    )
            except Exception:
                pass
        if status == VerificationStatus.VERIFIED:
            shown = (
                f"{len(causal_chain)} causal link(s) across "
                f"{len(providers)} provider(s) and {len(types)} evidence type(s)"
            )
            caveats = (
                " Limitations: "
                + "; ".join(f"{lim.code.value}: {lim.detail}" for lim in limitations)
                if limitations
                else " No caveats beyond the heuristic score."
            )
            explanation = (
                f"Verified: {shown}. "
                f"Heuristic score (coverage={coverage:.2f}, reliability={reliability:.2f}, "
                f"causal={causal:.2f}).{caveats} "
                "Final conclusions require ConclusionGate approval."
            )
        else:
            explanation = (
                "Heuristic verification score "
                f"(coverage={coverage:.2f} from {len(corroborating_ids)} items across "
                f"{len(providers)} providers, reliability={reliability:.2f}, causal={causal:.2f}). "
                "Heuristic only — final conclusions require ConclusionGate approval."
            )
        return VerificationResult(
            hypothesis_id=hypothesis.id,
            status=status,
            corroborating_evidence_ids=corroborating_ids,
            explanation=explanation,
            confidence=confidence,
            tier=tier,
            best_reason=best_reason,
            against_evidence=against_evidence,
            limitations=limitations,
        )


class ConclusionGateDecision(BaseModel):
    approved: bool
    blockers: list[str] = Field(default_factory=list)


class ConclusionGate:
    """Single mandatory gate for conclusion publication (F-051).

    The LLM may RECOMMEND readiness, but only this domain service authorizes
    finalization — evaluating evidence coverage, contradiction state, causal
    chain, freshness, and provenance. Any blocker fails closed.
    """

    @traced("reason", extra_from=_gate_extra)
    async def evaluate(
        self,
        tenant_id: str,
        investigation_id: UUID,
        verification: VerificationResult,
        contradictions: list[Contradiction],
        causal_chain: list[CausalRelationship],
        evidence_items: list[Evidence],
        policy: RootCauseVerificationPolicy,
        location_validated: bool = False,
        doc_validated: bool = False,
        state_validated: bool = False,
        code_value_validated: bool = False,
        history_validated: bool = False,
        caller_edges_validated: bool = False,
    ) -> ConclusionGateDecision:
        blockers: list[str] = []
        if verification.status != VerificationStatus.VERIFIED:
            blockers.append(f"hypothesis status is {verification.status.value}, not VERIFIED")
        if not band_at_least(
            verification.confidence.overall_confidence,
            policy.min_confidence_threshold,
            tolerant=policy.tolerant_band,
        ):
            blockers.append(
                f"confidence {verification.confidence.overall_confidence:.2f} below threshold "
                f"{policy.min_confidence_threshold:.2f}"
            )
        blocking_contradictions = [
            c
            for c in contradictions
            if c.resolution_status == "UNRESOLVED"
            and c.severity in (ContradictionSeverity.HIGH, ContradictionSeverity.CRITICAL)
        ]
        if blocking_contradictions:
            blockers.append(
                f"{len(blocking_contradictions)} unresolved HIGH/CRITICAL contradiction(s)"
            )
        if (
            policy.require_causal_relationship
            and not causal_chain
            and not location_validated
            and not doc_validated
            and not state_validated
            and not code_value_validated
            and not history_validated
            and not caller_edges_validated
        ):
            # G1: location answers have no causal chain by nature — the
            # location validation (cited code with provenance) stands in.
            # T1-1: same for validated doc answers. #65: same for validated
            # state answers (banked rows stand in).
            blockers.append("no causal chain established")
        if not evidence_items:
            blockers.append("no evidence items bound to the conclusion")
        else:
            # Freshness + provenance: every bound evidence item must carry
            # provenance; stale items (>24h) must be explicitly re-validated.
            from datetime import timedelta

            now = datetime.now(UTC)
            for item in evidence_items:
                if getattr(item, "provenance", None) is None:
                    blockers.append(f"evidence {item.evidence_id} lacks provenance")
                    break
                retrieved = getattr(getattr(item, "freshness", None), "retrieved_at", None)
                if retrieved is not None and (now - retrieved) > timedelta(hours=24):
                    blockers.append(f"evidence {item.evidence_id} is stale (>24h since retrieval)")
                    break
        return ConclusionGateDecision(approved=not blockers, blockers=blockers)
