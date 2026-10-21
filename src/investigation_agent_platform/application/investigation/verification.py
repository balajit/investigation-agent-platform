# src/investigation_agent_platform/application/investigation/verification.py
import re
from datetime import UTC, datetime
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from investigation_agent_platform.domain.code.models import (
    CODE_FACT_RESOLUTION_V2_KEY,
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


def select_verifiable_candidate(
    ranked: list,
    evidence_by_id: dict,
    exclude_statuses: tuple = ("REJECTED", "CONTRADICTED"),
) -> Any | None:
    """First eligible candidate whose citations resolve, else first eligible.

    G9: reprocess copies hypotheses with stale supporting IDs (cited rows
    purged); verifying the stale copy first concludes INCONCLUSIVE while a
    verifiable twin waits. Prefer candidates with >=1 supporting ID
    present in evidence_by_id; fall back to the head eligible so the
    honest-abstention path is unchanged when nothing resolves. Pure and
    deterministic. ID comparison tolerates str/UUID mixing (same coercion
    as the activities' _resolve).
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


class RootCauseVerificationPolicy(BaseModel):
    min_confidence_threshold: float = Field(default=0.80, ge=0.0, le=1.0)
    require_cross_layer_evidence: bool = Field(default=True)
    max_allowed_unresolved_contradictions: int = Field(default=0, ge=0)
    require_causal_relationship: bool = Field(default=True)
    # D5: publish band. [likely_floor, established_floor) concludes
    # ROOT_CAUSE_LIKELY with best-reason/against-evidence; at or above
    # established_floor concludes ROOT_CAUSE_ESTABLISHED. Below likely
    # floor abstains. Policy-owned (not magic constants) so runtime and
    # code profiles can tune the band independently.
    likely_floor: float = Field(default=0.80, ge=0.0, le=1.0)
    established_floor: float = Field(default=0.90, ge=0.0, le=1.0)

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


class VerificationLimitation(BaseModel):
    code: LimitationCode
    detail: str = Field(default="", max_length=500)


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
        frozenset({"SOURCE_RANGE"}), **_VERIFIED_SOURCE  # type: ignore[arg-type]
    ),
    JavaFactCategory.SOURCE_VALUE_FACT: CategoryProofRule(
        frozenset({"SOURCE_RANGE", "ANALYZER_RESOLUTION"}), **_VERIFIED_SOURCE  # type: ignore[arg-type]
    ),
    JavaFactCategory.LANGUAGE_SEMANTIC_FACT: CategoryProofRule(
        frozenset({"SOURCE_RANGE", "LANGUAGE_RULE_VERSION", "ANALYZER_RESOLUTION"}),
        **_VERIFIED_SOURCE,  # type: ignore[arg-type]
    ),
    JavaFactCategory.CONTROL_FLOW_FACT: CategoryProofRule(
        frozenset({"SOURCE_RANGE", "ANALYZER_RESOLUTION"}), **_LIKELY_SOURCE  # type: ignore[arg-type]
    ),
    JavaFactCategory.DATA_FLOW_FACT: CategoryProofRule(
        frozenset({"SOURCE_RANGE", "ANALYZER_RESOLUTION"}), **_LIKELY_SOURCE  # type: ignore[arg-type]
    ),
    JavaFactCategory.CALL_TARGET_FACT: CategoryProofRule(
        frozenset({"CALL_SITE_RANGE", "TARGET_DECLARATION_RANGE", "ANALYZER_RESOLUTION"}),
        **_LIKELY_SOURCE,  # type: ignore[arg-type]
    ),
    JavaFactCategory.POSSIBLE_EFFECT_FACT: CategoryProofRule(
        frozenset({"SOURCE_RANGE", "ANALYZER_RESOLUTION"}), **_LIKELY_SOURCE  # type: ignore[arg-type]
    ),
    JavaFactCategory.CONFIGURATION_FACT: CategoryProofRule(
        frozenset({"SOURCE_RANGE", "CONFIGURATION_SOURCE"}), **_LIKELY_SOURCE  # type: ignore[arg-type]
    ),
    JavaFactCategory.RUNTIME_OBSERVATION_FACT: CategoryProofRule(
        frozenset({"RUNTIME_OBSERVATION"}), True, True, False, True, False, "LIKELY"
    ),
    JavaFactCategory.CAUSAL_CLAIM: CategoryProofRule(
        frozenset({"SOURCE_RANGE", "RUNTIME_OBSERVATION", "CAUSAL_STEP_BINDINGS"}),
        True, True, True, True, False, "LIKELY",
    ),
}


def category_proof_rule(category: JavaFactCategory) -> CategoryProofRule | None:
    """Return the proof policy for a known category, else None (fail closed)."""
    try:
        return CATEGORY_PROOF_RULES.get(category)
    except Exception:
        return None


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
                        source
                        if isinstance(source, JavaFactCategory)
                        else JavaFactCategory(source)
                    )
                except ValueError:
                    return None
        return None

    @staticmethod
    def _v2_any_from_evidence(evidence_items: list[Any]) -> CodeFactResolutionV2 | None:
        """First well-formed v2 payload regardless of status (for PARTIAL wiring)."""
        for item in evidence_items or []:
            attrs = getattr(item, "attributes", None) or {}
            if not isinstance(attrs, dict):
                continue
            raw = attrs.get(CODE_FACT_RESOLUTION_V2_KEY)
            if not isinstance(raw, dict):
                continue
            try:
                return CodeFactResolutionV2.model_validate(raw)
            except Exception:
                continue
        return None

    @staticmethod
    def _v2_resolution_from_evidence(evidence_items: list[Any]) -> CodeFactResolutionV2 | None:
        """Load only well-formed RESOLVED v2 analyzer output."""
        resolution = VerificationEngine._v2_any_from_evidence(evidence_items)
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
                    families.add("CALL_SITE_RANGE")
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
        try:
            rule = category_proof_rule(category)
        except Exception:
            rule = None
        if rule is None:
            return (False, f"unknown fact category {category!r}: fail closed")
        v2_any = VerificationEngine._v2_any_from_evidence(evidence_items)
        if v2_any is not None and v2_any.status != CodeFactStatus.RESOLVED:
            codes = ",".join(v2_any.limitation_codes) or "untyped_partial"
            return (False, f"resolution is {v2_any.status.value}: {codes}")
        resolution = VerificationEngine._v2_resolution_from_evidence(evidence_items)
        if resolution is None:
            if v2_any is None:
                return (False, "no resolved CodeFactResolutionV2 is bound to the cited evidence")
            return (False, f"resolution is {v2_any.status.value}: non-verifying status")
        if resolution.fact_category != category:
            return (False, "claim category does not match the resolved fact category")
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
                return (False, "causal claims require per-step bound evidence; never analyzer-verified")
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
        if rule.requires_source_window_for_each_location and resolution.declaration_location is not None:
            matched = []
            expected_ids = {str(eid) for eid in resolution.source_evidence_ids}
            for item in evidence_items or []:
                if expected_ids and str(getattr(item, "evidence_id", "")) not in expected_ids:
                    continue
                loc = getattr(
                    getattr(getattr(item, "provenance", None), "source_location", None),
                    "revision",
                    None,
                )
                if str(loc or "") != (resolution.revision or ""):
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
                if start is not None and end is not None and window[0] <= start and end <= window[1]:
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
                if str(claimed_value) != str(resolution.resolved_value):
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
                subject = (
                    str(getattr(target, "symbol", "") or "")
                    or str(getattr(target, "subject_identity", "") or "")
                )
                expected_subjects = {
                    str(resolution.subject_identity or ""),
                    str(resolution.field_name or ""),
                    f"{resolution.declaring_type}.{resolution.field_name}"
                    if resolution.declaring_type and resolution.field_name
                    else "",
                }
                if subject and subject not in expected_subjects:
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
        provider priority in next_check_for applies unchanged.
        """
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
        if "runtime" in lowered or "runtime" in codes_text.lower():
            return VerificationLimitation(
                code=LimitationCode.NO_RUNTIME_OBSERVATION,
                detail=(f"Java fact abstained ({codes_text or reason}); runtime evidence required.")[:500],
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
            # single dotted identifier; nothing else may intervene.
            match = re.search(
                r"(?i)(?:default\s+value|initial\s+value|field\s+value|value)"
                r"\s+of\s+[A-Za-z_][\w.]*\s+is\s+"
                r"(['\"][^'\"]+['\"]|[-+]?\d+(?:\.\d+)?(?:[fFdDlL])?|true|false|null)",
                statement or "",
            )
        if match is None:
            return None
        value = match.group(1).strip()
        if len(value) >= 2 and value[0] in {"'", '"'} and value[-1] == value[0]:
            return value[1:-1]
        return value

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
        v2_any = self._v2_any_from_evidence(evidence_items)
        ok, reason = self.java_fact_proof(hypothesis, evidence_items)
        if not ok:
            return None
        resolution = self._v2_resolution_from_evidence(evidence_items)
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
        if self._fact_category_of(hypothesis) is None:
            return None
        v2_any = self._v2_any_from_evidence(evidence_items)
        if v2_any is None or v2_any.status == CodeFactStatus.RESOLVED:
            return None
        ok, reason = self.java_fact_proof(hypothesis, evidence_items)
        if ok:
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
            if attributes.get("read_mode") == "path_only" or attributes.get("discovery_only"):
                return False
            return VerificationEngine._item_window(item) is not None
        except Exception:
            return False

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
    def _call_shown(
        callee: str, snippet: str, *, exclude_construction: bool = False
    ) -> bool:
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
                    if exclude_construction and re.search(
                        r"\bnew\s*$", line[: match.start()]
                    ):
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
            if not callee_class or any(
                sep in callee_class for sep in ("::", ".", "#")
            ):
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

    @staticmethod
    def _extract_call_edge(statement: str) -> tuple[str | None, str | None]:
        """(callee, caller) from untyped prose; (None, None) when unclear."""
        text = statement or ""
        ident = r"[`'\"]?([A-Za-z_][\w.:#]*)[`'\"]?"
        m = re.search(rf"callers?\s+of\s+{ident}", text, re.IGNORECASE)
        if m:
            return (m.group(1), None)
        m = re.search(rf"{ident}\s+(?:is\s+|are\s+)?called\s+by\s+{ident}", text, re.IGNORECASE)
        if m:
            return (m.group(1), m.group(2))
        return (None, None)

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
        evidence_items: list[Any], target: ClaimTarget | None, statement: str
    ) -> tuple[bool, str]:
        """A cited code row that shows the call expression itself."""
        callee = target.symbol if target is not None else None
        caller = target.counterpart if target is not None else None
        if not callee:
            callee, extracted_caller = VerificationEngine._extract_call_edge(statement)
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
    def absence_outcome(
        evidence_items: list[Any], target: ClaimTarget | None
    ) -> tuple[str, str]:
        """Four-outcome absence classification (Phase 2a, pure, deterministic).

        - NO_SCOPE: claim states no search scope (target-resolution failure).
        - EMPTY_SEARCH_PROVED: scoped match_count==0 row covers the claim.
        - NO_SEARCH: bound set holds no empty-search rows at all (retrieval
          failure — never invent "nothing changed" from this).
        - SCOPE_MISMATCH: empty-search rows exist but none covers the claim
          (target-resolution failure).
        """
        scope = (
            (getattr(target, "scope", "") or "").strip() if target is not None else ""
        )
        if not scope:
            return ("NO_SCOPE", "absence claim states no search scope (set target.scope)")
        scoped_rows: list[str] = []
        for item in evidence_items or []:
            attributes = getattr(item, "attributes", None) or {}
            if not isinstance(attributes, dict):
                continue
            proof_scope = str(attributes.get("search_scope", "") or "").strip()
            if attributes.get("match_count") != 0 or not proof_scope:
                continue
            scoped_rows.append(proof_scope)
        if not scoped_rows:
            return (
                "NO_SEARCH",
                "no empty-search evidence bound; do not conclude absence from retrieval silence",
            )
        lowered = scope.lower()
        for proof_scope in scoped_rows:
            if proof_scope.lower() == lowered or lowered in proof_scope.lower():
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
        kind = VerificationEngine.effective_claim_kind(hypothesis)
        if kind is None or kind == ClaimKind.CAUSAL:
            return None
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
                evidence_by_id = {
                    str(getattr(item, "evidence_id", "")): item
                    for item in evidence_items or []
                }
                for assessment in edge_assessments:
                    row = evidence_by_id.get(str(assessment.evidence_id))
                    if row is None:
                        return (False, "a caller edge's bound call-site evidence is unavailable")
                    # Same-claim sibling repair (live L2 035908): the model
                    # cited a sibling (comment/search row) while the showing
                    # call-site range sits bound but unlinked. The claim
                    # stays bound to showing evidence — set membership in
                    # the bound set — rather than failing on the pointer.
                    # Tight: only same-file range rows for this target.
                    _rows: list[Any] = [row]
                    _tfile = str(
                        getattr(assessment.claim_target, "file_path", "") or ""
                    )
                    if _tfile:
                        for _item in evidence_items or []:
                            if _item is row:
                                continue
                            try:
                                if not VerificationEngine._paths_match(
                                    _tfile, VerificationEngine._item_file(_item)
                                ):
                                    continue
                            except Exception:
                                continue
                            if _item not in _rows:
                                _rows.append(_item)
                    edge_ok, edge_reason = VerificationEngine.call_site_proof(
                        _rows, assessment.claim_target, statement
                    )
                    if not edge_ok:
                        return (False, edge_reason)
                named_callers: list[str] = []
                for item in evidence_items or []:
                    if VerificationEngine._item_type(item) not in VerificationEngine.CODE_EVIDENCE_TYPES:
                        continue
                    if VerificationEngine._item_window(item) is None:
                        continue
                    if not VerificationEngine._call_shown(
                        str(getattr(target, "symbol", "") or ""),
                        str(getattr(item, "content_snippet", "") or ""),
                    ):
                        continue
                    file_stem = (
                        VerificationEngine._item_file(item)
                        .rsplit("/", 1)[-1]
                        .rsplit(".", 1)[0]
                    )
                    if file_stem and re.search(
                        rf"(?<![A-Za-z0-9_]){re.escape(file_stem)}(?![A-Za-z0-9_])",
                        statement,
                    ):
                        named_callers.append(file_stem.casefold())
                bound_callers: set[str] = set()
                for a in edge_assessments:
                    _ct = getattr(a, "claim_target", None)
                    if _ct is None:
                        continue
                    _cp = str(getattr(_ct, "counterpart", "") or "")
                    for _sep in ("::", "#"):
                        _cp = _cp.replace(_sep, ".")
                    for _part in _cp.split("."):
                        _part = _part.strip()
                        if _part:
                            bound_callers.add(_part.casefold())
                if set(named_callers) - bound_callers:
                    return (False, "caller enumeration names a caller without its own CALL_EDGE claim")
                return (True, "")
            aggregate_edges = VerificationEngine._aggregate_call_edges(hypothesis)
            if aggregate_edges:
                for edge in aggregate_edges:
                    edge_ok, edge_reason = VerificationEngine.call_site_proof(
                        evidence_items, edge, statement
                    )
                    if not edge_ok:
                        return (False, edge_reason)
                return (True, "")
            return VerificationEngine.call_site_proof(evidence_items, target, statement)
        if kind == ClaimKind.BEHAVIOR:
            return VerificationEngine.behavior_proof(evidence_items)
        if kind == ClaimKind.ABSENCE:
            return VerificationEngine.absence_proof(evidence_items, target)
        if kind == ClaimKind.HISTORY:
            return VerificationEngine.history_proof(evidence_items, target)
        return None

    @staticmethod
    def _aggregate_call_edges(hypothesis: Any) -> list[ClaimTarget]:
        """Detect claims whose one target actually asserts several callers."""
        target = getattr(hypothesis, "claim_target", None)
        if target is None or not getattr(target, "counterpart", None):
            return []
        counterpart = str(target.counterpart)
        names = re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*\b", counterpart)
        names = [name for name in names if name.casefold() not in {"and", "or"}]
        if len(names) < 2:
            return []
        return [
            ClaimTarget(
                symbol=target.symbol,
                counterpart=name,
                file_path=target.file_path,
                line_start=target.line_start,
                line_end=target.line_end,
            )
            for name in names
        ]

    @staticmethod
    def history_proof(
        evidence_items: list[Any], target: ClaimTarget | None
    ) -> tuple[bool, str]:
        """A cited commit row must identify the claimed file and commit."""
        if target is None or not target.file_path or not target.commit_id:
            return (False, "history claim must identify target.file_path and target.commit_id")
        path_rows: list[Any] = []
        matching_rows: list[Any] = []
        for item in evidence_items or []:
            if VerificationEngine._item_type(item) != "COMMIT_HISTORY":
                continue
            if not VerificationEngine._paths_match(target.file_path, VerificationEngine._item_file(item)):
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
            description = str(getattr(item, "summary", "") or "").strip()
            if description and content.strip():
                matching_rows.append(item)
        if path_rows and matching_rows:
            newest = max(
                path_rows,
                key=lambda item: getattr(item, "observed_at", None)
                or getattr(item, "retrieved_at", None)
                or datetime.min.replace(tzinfo=UTC),
            )
            if newest in matching_rows:
                return (True, "")
        return (
            False,
            f"no cited COMMIT_HISTORY row matches {target.file_path} at commit {target.commit_id}",
        )

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
            and str(getattr(getattr(c, "severity", ""), "value", getattr(c, "severity", ""))).upper()
            in {"HIGH", "CRITICAL"}
            for c in contradictions or []
        ):
            return None
        target = getattr(hypothesis, "claim_target", None)
        proof_ok, _ = VerificationEngine.history_proof(evidence_items, target)
        if not proof_ok or target is None:
            return None
        matching: list[Any] = []
        path_commits: list[Any] = []
        for item in evidence_items or []:
            if VerificationEngine._item_type(item) != "COMMIT_HISTORY":
                continue
            if not VerificationEngine._paths_match(target.file_path or "", VerificationEngine._item_file(item)):
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
            if (
                (revision.startswith(commit_id) or commit_id.startswith(revision))
                and subject
                and subject.casefold() in statement.casefold()
            ):
                matching.append(item)
        if not matching or not path_commits:
            return None
        newest = max(
            path_commits,
            key=lambda item: getattr(item, "observed_at", None)
            or getattr(item, "retrieved_at", None)
            or datetime.min.replace(tzinfo=UTC),
        )
        if newest not in matching:
            return None
        confidence = InvestigationConfidence(
            coverage_score=1.0,
            reliability_score=1.0,
            causal_score=0.5,
            contradiction_penalty=0.0,
        )
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
            for ids in (
                getattr(hypothesis, "supporting_evidence_ids", None) or [],
            ):
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
            return (False, "")
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
                return (False, "")
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
            payload = attrs.get("code_fact_resolution_v2") or attrs.get(
                "code_fact_resolution"
            )
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
                payload = attrs.get("code_fact_resolution_v2")
                if payload is None:
                    continue
                if isinstance(payload, dict):
                    status = str(payload.get("status") or "")
                    subject = str(payload.get("subject_identity") or "")
                    value = payload.get("resolved_value")
                else:
                    status = str(getattr(payload, "status", "") or "")
                    subject = str(getattr(payload, "subject_identity", "") or "")
                    value = getattr(payload, "resolved_value", None)
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
            # Fail-open on analysis errors: never demote on a crash.
            return True

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
                if not VerificationEngine.sentence_mapped(sentence, value_texts, file_paths):
                    unmapped.append(sentence)
        except Exception:
            return []
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
                return (
                    "Narrow or re-scope the empty-search proof to the claimed scope, then re-verify."
                )
            if any(
                t == "no validated causal chain" or "no causal chain established" in t
                for t in lowered
            ):
                return "Bind a causal chain with cited evidence for each link, then re-verify."
            return f"Resolve '{texts[0][:300]}', then re-verify."
        except Exception:
            return None

    @traced("reason", require_scope=False, extra_from=_verification_extra)
    def evaluate_hypothesis(
        self,
        hypothesis: Hypothesis,
        evidence_items: list[Evidence],
        contradictions: list[Contradiction],
        causal_chain: list[CausalRelationship],
        policy: RootCauseVerificationPolicy,
        sibling_hypotheses: list[Any] | None = None,
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
        if VerificationEngine.claims_field_value(
            getattr(hypothesis, "statement", "")
        ) or VerificationEngine._fact_category_of(hypothesis) is not None:
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

        confidence = InvestigationConfidence(
            coverage_score=coverage,
            reliability_score=reliability,
            causal_score=causal,
            contradiction_penalty=0.0,
        )

        status = (
            VerificationStatus.VERIFIED
            if confidence.overall_confidence >= policy.min_confidence_threshold
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
                if likely <= overall < established:
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
                for contradiction in contradictions or []:
                    try:
                        if getattr(contradiction, "resolution_status", "") == "UNRESOLVED":
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
    ) -> ConclusionGateDecision:
        blockers: list[str] = []
        if verification.status != VerificationStatus.VERIFIED:
            blockers.append(f"hypothesis status is {verification.status.value}, not VERIFIED")
        if verification.confidence.overall_confidence < policy.min_confidence_threshold:
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
