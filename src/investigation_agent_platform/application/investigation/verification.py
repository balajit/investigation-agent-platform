# src/investigation_agent_platform/application/investigation/verification.py
import re
from datetime import UTC, datetime
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from investigation_agent_platform.domain.evidence.models import Evidence
from investigation_agent_platform.domain.hypothesis.models import Hypothesis
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
                return candidate
    return eligible[0] if eligible else None


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


class VerificationResult(BaseModel):
    hypothesis_id: UUID
    status: VerificationStatus
    corroborating_evidence_ids: list[UUID] = Field(default_factory=list)
    explanation: str
    confidence: InvestigationConfidence
    # G1: location-answer validation bypasses causal verification; the gate
    # and conclude paths branch on this flag (never on status text).
    location_validated: bool = False


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
    def has_range_read(evidence_items: list) -> bool:
        """At least one citation comes from a range-read operation."""
        try:
            for item in evidence_items or []:
                provenance = getattr(item, "provenance", None)
                fingerprint = getattr(provenance, "query_fingerprint", None)
                operation = str(getattr(fingerprint, "operation", "") or "").upper()
                if operation in VerificationEngine.GET_OPERATIONS:
                    return True
            return False
        except Exception:
            return False

    @staticmethod
    def claims_location(statement: str) -> bool:
        """Location/listing claim eligible for the answer path (never causal)."""
        try:
            lowered = f" {(statement or '').lower()} "
            patterns = (
                VerificationEngine.DEFINITION_PATTERNS
                + VerificationEngine.LISTING_PATTERNS
            )
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
                    if (
                        path == cited_path
                        or cited_path.endswith("/" + path)
                        or path.endswith("/" + cited_path)
                    ):
                        return path
            return None
        except Exception:
            return None

    @staticmethod
    def _cited_location_evidence(evidence_items: list) -> list:
        """Code citations carrying file+line+revision provenance."""
        cited = []
        for item in evidence_items or []:
            etype = str(
                getattr(getattr(item, "evidence_type", ""), "value", getattr(item, "evidence_type", ""))
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
        # G9 correction fallback: phrasing patterns miss ("the correct file
        # is X"), so a statement naming a cited evidence path qualifies as
        # a location claim on evidence grounds. Phrase path unchanged.
        by_phrase = VerificationEngine.claims_location(statement)
        if not by_phrase:
            cited_pre = VerificationEngine._cited_location_evidence(evidence_items)
            corrected_path = VerificationEngine._names_cited_path(statement, cited_pre)
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
            and str(getattr(getattr(c, "severity", ""), "value", getattr(c, "severity", ""))).upper()
            in ("HIGH", "CRITICAL")
        ]
        if unresolved:
            return None
        cited = VerificationEngine._cited_location_evidence(evidence_items)
        if not cited:
            return None
        if VerificationEngine.claims_definition(statement) and not VerificationEngine.has_range_read(
            cited
        ):
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
    @traced("reason", require_scope=False, extra_from=_verification_extra)
    def evaluate_hypothesis(
        self,
        hypothesis: Hypothesis,
        evidence_items: list[Evidence],
        contradictions: list[Contradiction],
        causal_chain: list[CausalRelationship],
        policy: RootCauseVerificationPolicy,
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
        return VerificationResult(
            hypothesis_id=hypothesis.id,
            status=status,
            corroborating_evidence_ids=corroborating_ids,
            explanation=(
                "Heuristic verification score "
                f"(coverage={coverage:.2f} from {len(corroborating_ids)} items across "
                f"{len(providers)} providers, reliability={reliability:.2f}, causal={causal:.2f}). "
                "Heuristic only — final conclusions require ConclusionGate approval."
            ),
            confidence=confidence,
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
        if policy.require_causal_relationship and not causal_chain and not location_validated:
            # G1: location answers have no causal chain by nature — the
            # location validation (cited code with provenance) stands in.
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
