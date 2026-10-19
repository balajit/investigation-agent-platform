# src/investigation_agent_platform/application/investigation/verification.py
import re
from datetime import UTC, datetime
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from investigation_agent_platform.domain.evidence.models import Evidence
from investigation_agent_platform.domain.hypothesis.models import (
    ClaimKind,
    ClaimTarget,
    Hypothesis,
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


def _prefer_discriminating(
    first_match: Any, eligible: list, evidence_by_id: dict
) -> Any:
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
    """

    RIVAL_UNTESTED = "RIVAL_UNTESTED"
    SINGLE_PROVIDER = "SINGLE_PROVIDER"
    CONCLUSION_UNMAPPED = "CONCLUSION_UNMAPPED"


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
            if attributes.get("read_mode") == "path_only" or attributes.get(
                "discovery_only"
            ):
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
            if re.search(
                rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])", text or ""
            ):
                return True
        return False

    @staticmethod
    def _call_shown(callee: str, snippet: str) -> bool:
        """True when the snippet contains a call expression for ``callee``.

        A declaration line (``def callee(``) matches ``callee(`` too, so
        declarations are excluded: a definition is not a call site.
        """
        names = {callee}
        for sep in ("::", ".", "#"):
            if sep in callee:
                names.add(callee.rsplit(sep, 1)[-1])
        for raw in (snippet or "").splitlines():
            line = raw.split("| ", 1)[1] if "| " in raw and raw.split("|", 1)[0].strip().isdigit() else raw
            for name in names:
                if not name:
                    continue
                for match in re.finditer(
                    rf"(?<![A-Za-z0-9_]){re.escape(name)}\s*\(", line
                ):
                    if VerificationEngine._DECLARATION_PREFIX.search(line[: match.start()]):
                        continue
                    return True
        return False

    @staticmethod
    def _extract_call_edge(statement: str) -> tuple[str | None, str | None]:
        """(callee, caller) from untyped prose; (None, None) when unclear."""
        text = statement or ""
        ident = r"[`'\"]?([A-Za-z_][\w.:#]*)[`'\"]?"
        m = re.search(rf"callers?\s+of\s+{ident}", text, re.IGNORECASE)
        if m:
            return (m.group(1), None)
        m = re.search(
            rf"{ident}\s+(?:is\s+|are\s+)?called\s+by\s+{ident}", text, re.IGNORECASE
        )
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
            return (False, "no range read with recorded start/end lines (a path-only or head read is discovery)")
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
        shown = [
            i
            for i in rows
            if VerificationEngine._call_shown(
                callee, str(getattr(i, "content_snippet", "") or "")
            )
        ]
        if not shown:
            return (False, f"no cited row shows a call to '{callee}' (a search hit or graph row that does not show the call is not a call site)")
        if caller:
            shown = [
                i
                for i in shown
                if VerificationEngine._symbol_in_text(
                    caller, str(getattr(i, "content_snippet", "") or "")
                )
                or VerificationEngine._symbol_in_text(
                    caller, VerificationEngine._item_file(i)
                )
            ]
            if not shown:
                return (False, f"call to '{callee}' is shown but not from '{caller}'")
        return (True, "")

    @staticmethod
    def behavior_proof(evidence_items: list[Any]) -> tuple[bool, str]:
        """Code (range-read) AND runtime evidence — code alone is structure."""
        has_code = any(
            VerificationEngine._item_type(i) in VerificationEngine.CODE_EVIDENCE_TYPES
            and VerificationEngine._is_range_read(i)
            for i in evidence_items or []
        )
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
    def absence_proof(
        evidence_items: list[Any], target: ClaimTarget | None
    ) -> tuple[bool, str]:
        """Scoped empty search: states its scope and records zero matches.

        Accepted proof shape (produced by typed no-match outcomes): an
        evidence row with ``attributes.search_scope`` and
        ``attributes.match_count == 0`` whose scope covers the claimed one.
        """
        scope = (target.scope or "").strip() if target is not None else ""
        if not scope:
            return (False, "absence claim states no search scope (set target.scope)")
        for item in evidence_items or []:
            attributes = getattr(item, "attributes", None) or {}
            if not isinstance(attributes, dict):
                continue
            proof_scope = str(attributes.get("search_scope", "") or "").strip()
            if attributes.get("match_count") != 0 or not proof_scope:
                continue
            if proof_scope.lower() == scope.lower() or scope.lower() in proof_scope.lower():
                return (True, "")
        return (False, f"no scoped empty-search evidence covers '{scope}'; an empty result alone is not proof of absence")

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
            return VerificationEngine.call_site_proof(evidence_items, target, statement)
        if kind == ClaimKind.BEHAVIOR:
            return VerificationEngine.behavior_proof(evidence_items)
        if kind == ClaimKind.ABSENCE:
            return VerificationEngine.absence_proof(evidence_items, target)
        return None

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
            "the", "a", "an", "this", "that", "these", "those",
            "module", "file", "code", "class", "function", "method",
            "service", "correct", "actual",
            # Verb-family and claim scaffolding: structural words, never
            # subjects. Without these, "defined" in "X is defined in ..."
            # grounds against any row containing the word, defeating the
            # substitution check (quantum-module fabrication cited a file
            # whose rows mention "defined").
            "defined", "implemented", "declared", "located", "found",
            "lives", "history", "commits", "called", "does",
            "exist", "exists", "not",
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
            return any(
                VerificationEngine._stem_token(c) in corpus_stems for c in candidates
            )
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
            return (
                first == second
                or first.endswith("/" + second)
                or second.endswith("/" + first)
            )
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
            if not statement_paths:
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
                    if not any(
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
        # Phase 2.2: a typed behavior/absence/causal claim is never a
        # location answer, whatever its phrasing ("found in" inside a
        # behavior claim must not take the citations-only fast path).
        claim_kind = VerificationEngine.effective_claim_kind(hypothesis)
        if claim_kind in (ClaimKind.BEHAVIOR, ClaimKind.ABSENCE, ClaimKind.CAUSAL):
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
            and str(getattr(getattr(c, "severity", ""), "value", getattr(c, "severity", ""))).upper()
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
            and str(getattr(getattr(c, "severity", ""), "value", getattr(c, "severity", ""))).upper()
            in ("HIGH", "CRITICAL")
        ]
        if unresolved:
            return None
        cited = []
        for item in evidence_items or []:
            try:
                etype = str(
                    getattr(getattr(item, "evidence_type", ""), "value", getattr(item, "evidence_type", ""))
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
            and str(getattr(getattr(c, "severity", ""), "value", getattr(c, "severity", ""))).upper()
            in ("HIGH", "CRITICAL")
        ]
        if unresolved:
            return None
        cited = []
        for item in evidence_items or []:
            try:
                etype = str(
                    getattr(getattr(item, "evidence_type", ""), "value", getattr(item, "evidence_type", ""))
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
            except Exception:
                continue
        return texts, paths

    @staticmethod
    def sentence_mapped(
        sentence: str, value_texts: list[Any], file_paths: list[Any]
    ) -> bool:
        """One conclusion sentence maps to a cited path, a grounded subject,
        or a distinctive value in the bound rows — else it is prose."""
        text = (sentence or "").strip()
        if not text:
            return True
        try:
            for path in VerificationEngine._statement_paths(text):
                if any(
                    VerificationEngine._paths_match(path, str(fp)) for fp in file_paths
                ):
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
        overturn the winner), then uncited sentences, then corroboration.
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
        if "CONCLUSION_UNMAPPED" in codes and sentences:
            return f"Cite evidence for each conclusion sentence, then re-verify: {sentences[0][:300]}"
        if "RIVAL_UNTESTED" in codes:
            return "Evaluate every rival hypothesis against evidence, then re-verify."
        if "CONCLUSION_UNMAPPED" in codes:
            return "Cite evidence for each conclusion sentence, then re-verify."
        if "SINGLE_PROVIDER" in codes:
            return "Corroborate with an independent evidence provider, then re-verify."
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
        kind_gate = self.check_claim_kind(hypothesis, evidence_items)
        if kind_gate is not None and not kind_gate[0]:
            kind = VerificationEngine.effective_claim_kind(hypothesis)
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
        if policy.require_causal_relationship and not causal_chain and not location_validated and not doc_validated and not state_validated:
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
