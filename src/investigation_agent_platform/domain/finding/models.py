# src/investigation_agent_platform/domain/finding/models.py
"""Finding aggregates and terminal conclusion models."""

import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from investigation_agent_platform.domain.hypothesis.models import (
    ClaimKind,
    ClaimTarget,
    HypothesisStatus,
)


class FindingType(StrEnum):
    ROOT_CAUSE = "ROOT_CAUSE"
    CONTRIBUTING_FACTOR = "CONTRIBUTING_FACTOR"
    ANOMALY = "ANOMALY"
    OBSERVATION = "OBSERVATION"


class SeverityLevel(StrEnum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    INFO = "INFO"


class ConclusionStatus(StrEnum):
    ROOT_CAUSE_ESTABLISHED = "ROOT_CAUSE_ESTABLISHED"
    ROOT_CAUSE_LIKELY = "ROOT_CAUSE_LIKELY"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    NO_ROOT_CAUSE_FOUND = "NO_ROOT_CAUSE_FOUND"
    # G1: location answers (where-defined, caller listings, histories) backed
    # by validated code citations — not causal claims, not abstentions.
    ANSWERED = "ANSWERED"


#: Conclusion verdicts. A terminal row states a verdict; overwriting one
#: with a different status is never an update — it is a lost race and
#: must fail loudly. Non-terminal rows state the absence of a verdict
#: and may move forward (or churn among non-verdicts).
TERMINAL_CONCLUSION_STATUSES = frozenset(
    {
        ConclusionStatus.ROOT_CAUSE_ESTABLISHED.value,
        ConclusionStatus.ROOT_CAUSE_LIKELY.value,
        ConclusionStatus.ANSWERED.value,
    }
)


def conclusion_rewrite_allowed(current_status: str | None, new_status: str) -> bool:
    """LWW removal: may a conclusion row be overwritten?

    No row (None) → yes. Same status → yes (idempotent retry/recommit).
    Non-terminal current → yes (forward progress, non-verdict churn).
    Terminal current + different status → no (verdict clobber or race).
    Pure predicate; both conclusion writers enforce it identically.
    """
    if current_status is None or current_status == new_status:
        return True
    return current_status not in TERMINAL_CONCLUSION_STATUSES


#: Model list caps, mirrored as constants so callers bound ids BEFORE building
#: the model (a unit test pins them to the field limits). H14: an unbounded
#: chain used to raise after the hypothesis was already saved VERIFIED.
FINDING_MAX_EVIDENCE_IDS = 100
FINDING_MAX_CAUSAL_IDS = 50
CONCLUSION_MAX_EVIDENCE_IDS = 200
CONCLUSION_MAX_CONTRADICTING_IDS = 200


def dedupe_and_cap_ids(ids: Any, limit: int) -> list[Any]:
    """Order-preserving de-duplication, then keep the leading ``limit`` ids.

    Pure and total: ``None``/empty give ``[]`` and a non-positive limit gives
    ``[]``. Ids are compared by equality, so UUIDs and their string forms are
    treated as distinct; callers pass one representation.
    """
    if not ids or limit <= 0:
        return []
    seen: set[Any] = set()
    out: list[Any] = []
    for item in ids:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
        if len(out) >= limit:
            break
    return out


class Finding(BaseModel):
    """Verified finding aggregate backed by causal evidence."""

    model_config = ConfigDict(frozen=True)

    id: UUID = Field(default_factory=uuid4)
    tenant_id: str = Field(..., max_length=128)
    investigation_id: UUID
    finding_type: FindingType
    title: str = Field(..., max_length=256)
    statement: str = Field(..., max_length=2048)
    evidence_ids: list[UUID] = Field(default_factory=list, max_length=100)
    related_hypothesis_ids: list[UUID] = Field(default_factory=list, max_length=50)
    causal_chain: list[UUID] = Field(default_factory=list, max_length=50)
    severity: SeverityLevel = SeverityLevel.MEDIUM
    remediation_steps: list[str] = Field(default_factory=list, max_length=20)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def validate_root_cause_requirements(self) -> "Finding":
        if self.finding_type == FindingType.ROOT_CAUSE:
            if not self.evidence_ids:
                raise ValueError("ROOT_CAUSE finding must be backed by at least one evidence item.")
            if not self.causal_chain:
                raise ValueError("ROOT_CAUSE finding must specify a non-empty causal_chain.")
        return self


class EliminatedRival(BaseModel):
    """One ruled-out rival: the strongest rival considered plus the
    evidence that ruled it out (S5/S6 rival bar)."""

    model_config = ConfigDict(frozen=True)

    statement: str = Field(default="", max_length=500)
    status: str = Field(default="", max_length=32)
    ruled_out_by_evidence_ids: list[str] = Field(default_factory=list, max_length=20)


HANDOFF_VERSION_CURRENT = "handoff-v1"


class HandoffState(StrEnum):
    """Explicit S7 terminal-handoff states (DES-S7-handoff v1.0)."""

    PROVED = "PROVED"
    BUDGET_DEFERRED = "BUDGET_DEFERRED"
    FRONTIER_EXHAUSTED = "FRONTIER_EXHAUSTED"
    CROSS_BOUNDARY = "CROSS_BOUNDARY"
    TELEMETRY_DEFERRED = "TELEMETRY_DEFERRED"
    #: A reasoning step could not complete; resume reasoning (H7).
    REASONING_DEFERRED = "REASONING_DEFERRED"
    #: An input requirement expired with banked evidence kept (H7d).
    INPUT_DEFERRED = "INPUT_DEFERRED"


# Retrieval actions a next fetch can name (mirrors the investigation action
# vocabulary). Longest names first so GET_CODE_HISTORY is never read as GET_CODE.
_FETCH_ACTION_TOKEN = re.compile(
    r"\b(GET_CODE_HISTORY|FIND_CALL_GRAPH|SEARCH_REF_DOCS|SEARCH_CODE|SEARCH_LOGS|"
    r"FIND_SYMBOL|QUERY_STATE|WALK_STACK|TRACE_LOGIC|PROBE_API|GET_CODE)\b"
)
_FETCH_PATH_TOKEN = re.compile(
    r"[\w./-]*[\w-]\.(?:java|py|kt|xml|ya?ml|properties|sql|json)\b|[\w-]+/[\w./-]+"
)
#: Most tool calls a single priced next fetch may claim.
MAX_FETCH_PRICE = 20.0


def names_concrete_fetch_target(text: str | None) -> bool:
    """True when text names a retrieval action or a file path/name.

    The single definition of "concrete": the handoff builder emits only such
    targets in ``next_fetch`` and the battery's G1 scores against the same
    rule. Template prose ("Bind supporting evidence ...") names neither.
    """
    value = str(text or "")
    return bool(_FETCH_ACTION_TOKEN.search(value) or _FETCH_PATH_TOKEN.search(value))


def price_next_fetch(text: str | None) -> float | None:
    """Estimated cost of a concrete next fetch, in tool-call budget units.

    One unit is one retrieval call against the per-investigation tool-call
    envelope. A fetch naming N distinct retrieval actions costs N; one that
    names only a file path (an implied read) costs 1. Prose that names no
    concrete target is unpriced (None) - a price is never fabricated.
    """
    if not names_concrete_fetch_target(text):
        return None
    actions = set(_FETCH_ACTION_TOKEN.findall(str(text or "")))
    return float(min(max(len(actions), 1), MAX_FETCH_PRICE))


def derive_gap_fetch(claim_kind: ClaimKind | None, target: ClaimTarget | None) -> str | None:
    """The concrete next fetch for an unproven claim, from its typed target.

    Deterministic and pure: it names a retrieval action plus the claim's own
    file/symbol/scope, never prose. Only code-side fetches are named. A
    BEHAVIOR or CAUSAL claim also needs runtime evidence, but a runtime fetch
    is never invented here (a code-only application has none to fetch), so
    those claims get their code half or nothing. Returns None when the claim
    carries no target to name (NOT_CHECKABLE, untyped, or empty target).
    """
    if claim_kind is None or claim_kind == ClaimKind.NOT_CHECKABLE or target is None:
        return None
    path = (target.file_path or "").strip()
    symbol = (target.symbol or "").strip()
    scope = (target.scope or "").strip()
    lines = (
        f" lines {target.line_start}-{target.line_end}"
        if target.line_start and target.line_end
        else ""
    )
    if claim_kind == ClaimKind.HISTORY:
        return f"GET_CODE_HISTORY {path}"[:1024] if path else None
    if claim_kind == ClaimKind.ABSENCE:
        if not symbol:
            return None
        return f"SEARCH_CODE {symbol}" + (f" within {scope}" if scope else "")[:1024]
    if claim_kind == ClaimKind.CALL_EDGE:
        if path:
            return f"GET_CODE {path}{lines} (call site of {symbol or 'the callee'})"[:1024]
        return f"SEARCH_CODE {symbol} call sites"[:1024] if symbol else None
    # DEFINITION, BEHAVIOR, CAUSAL: the code half.
    if path:
        return f"GET_CODE {path}{lines}" + (f" for {symbol}" if symbol else "")[:1024]
    return f"SEARCH_CODE {symbol}"[:1024] if symbol else None


def gap_fetch_for_hypotheses(hypotheses: Any) -> str | None:
    """Concrete next fetch for the strongest hypothesis that is still unproven.

    Considers hypotheses that are not VERIFIED or REJECTED and carry a typed
    claim, highest confidence first. Fail-open: any fault yields None, so a
    handoff is never blocked by gap derivation.
    """
    try:
        open_hyps = [
            h
            for h in (hypotheses or [])
            if getattr(h, "status", None)
            not in (HypothesisStatus.VERIFIED, HypothesisStatus.REJECTED)
        ]
        open_hyps.sort(
            key=lambda h: float(getattr(h, "confidence_score", 0.0) or 0.0), reverse=True
        )
        for hyp in open_hyps:
            fetch = derive_gap_fetch(hyp.claim_kind, hyp.claim_target)
            if fetch:
                return fetch
    except Exception:
        return None
    return None


class S7Handoff(BaseModel):
    """Explicit priced handoff for a terminal investigation.

    ``target`` is the named downstream fetch/recipient target. Abstention
    and refusal handoffs use ``"n/a"``: the handoff names the next fetch
    or the elimination record, but it never asserts a fetch target and
    never mutates the investigation status target.

    ``price`` is the estimated cost of ``next_fetch`` in tool-call budget
    units (see ``price_next_fetch``); None means unpriced. It travels in the
    same JSONB handoff column, so no migration is needed.
    """

    model_config = ConfigDict(frozen=True)

    version: str = Field(default=HANDOFF_VERSION_CURRENT, max_length=16)
    state: HandoffState
    target: str = Field(default="n/a", max_length=1024)
    next_fetch: str | None = Field(default=None, max_length=1024)
    price: float | None = Field(default=None, ge=0.0, le=MAX_FETCH_PRICE)
    resume_pointer: str | None = Field(default=None, max_length=1024)
    recommended_next_steps: list[str] = Field(default_factory=list, max_length=20)


def classify_terminal_handoff(
    *,
    status: ConclusionStatus | str,
    reason: str = "",
    next_check: str | None = None,
    limitations: list[str] | None = None,
    ranked_alternatives: list[str] | None = None,
    eliminated_rivals: list[EliminatedRival | dict[str, Any]] | None = None,
    upstream_identity: str | None = None,
    gap_fetch: str | None = None,
) -> S7Handoff:
    """Map a terminal outcome to its explicit S7 handoff (handoff-v1).

    Pure; never raises. Established/answered verdicts map to PROVED.
    ROOT_CAUSE_LIKELY is provisional, never proved: it falls through so
    its follow-up (next fetch, ranked alternatives, elimination record)
    survives. Non-verdict terminals map to the failure character in
    ``reason`` plus ``limitations`` (provider details live in the
    limitations while callers pass generic reasons): provider/telemetry
    blocks defer with a resume pointer, budget exhaustion defers with the
    priced next fetch, and refuted/frontier endings publish the elimination
    record instead of generic filler.
    """
    try:
        status_text = str(getattr(status, "value", status) if status is not None else "").upper()
    except Exception:
        status_text = ""
    try:
        reason_text = str(reason or "").strip()
        reason_lower = reason_text.lower()
    except Exception:
        reason_text, reason_lower = "", ""
    # ``next_check`` is a prose directive ("..., then re-verify"). ``next_fetch``
    # only ever carries a concrete target (a named action or file): the
    # caller's gap-derived fetch first, else the prose itself when it happens
    # to name one. Prose that names nothing stays in the steps and is never
    # stored as a fetch target (review T2: no filler).
    try:
        fetch = str(next_check or "").strip() or None
    except Exception:
        fetch = None
    try:
        gap = str(gap_fetch or "").strip()
        target: str | None = next(
            (c[:1024] for c in (gap, fetch or "") if c and names_concrete_fetch_target(c)), None
        )
    except Exception:
        target = None
    directive = fetch or target
    try:
        alternatives = [
            str(item or "").strip()
            for item in (ranked_alternatives or [])
            if str(item or "").strip()
        ][:10]
    except Exception:
        alternatives = []
    try:
        eliminated = list(eliminated_rivals or [])
    except Exception:
        eliminated = []
    try:
        upstream = str(upstream_identity or "").strip() or None
    except Exception:
        upstream = None
    try:
        limitation_texts = [
            str(item or "").strip() for item in (limitations or []) if str(item or "").strip()
        ]
    except Exception:
        limitation_texts = []
    # Provider/telemetry details travel in the limitations while callers
    # pass generic reasons ("refuted"/"insufficient"), so signal matching
    # reads both. Budget matching also reads both: the epistemic path
    # restates the reason inside its limitation line.
    signal_text = " ".join([reason_lower, *[t.lower() for t in limitation_texts]])

    if status_text in (
        ConclusionStatus.ANSWERED.value,
        ConclusionStatus.ROOT_CAUSE_ESTABLISHED.value,
    ):
        return S7Handoff(state=HandoffState.PROVED)

    # Steps keep the prose directive; the concrete target is added once when
    # it is not already the directive itself.
    target_step = f"Next fetch: {target[:480]}" if target and target != directive else None

    if upstream is not None:
        steps = [f"Named handoff to {upstream[:200]}: no fetch crosses the tenant boundary."]
        if directive is not None:
            steps.append(f"Localizing evidence/next fetch: {directive[:300]}")
        if target_step is not None:
            steps.append(target_step)
        return S7Handoff(
            state=HandoffState.CROSS_BOUNDARY,
            target=upstream[:1024],
            next_fetch=target,
            price=price_next_fetch(target),
            resume_pointer=None,
            recommended_next_steps=steps[:20],
        )

    _REASONING_MARKERS = (
        "reasoning_provider_blocked", "reasoning_provider_unavailable",
        "reasoning_model_output_invalid", "reasoning_unavailable", "defer_gathering_invalid",
    )
    if any(marker in signal_text for marker in _REASONING_MARKERS):
        resume = directive or (
            "Restore or replace the reasoning provider or model, then resume reasoning and re-verify."
        )
        steps = [resume[:500]] + ([target_step] if target_step else [])
        return S7Handoff(
            state=HandoffState.REASONING_DEFERRED,
            next_fetch=target,
            price=price_next_fetch(target),
            resume_pointer=resume[:1024],
            recommended_next_steps=steps[:20],
        )
    if "input_expired" in signal_text and status_text in ("INSUFFICIENT_EVIDENCE", "COMPLETED"):
        resume = directive or "Supply the requested input, then resume; banked evidence is kept."
        steps = [resume[:500]] + ([target_step] if target_step else [])
        return S7Handoff(
            state=HandoffState.INPUT_DEFERRED,
            next_fetch=target,
            price=price_next_fetch(target),
            resume_pointer=resume[:1024],
            recommended_next_steps=steps[:20],
        )
    if (
        "provider_blocked" in signal_text
        or "auth_failed" in signal_text
        or "telemetry" in signal_text
    ):
        resume = directive or (
            "Restore provider authorization, then resume at the blocked "
            "telemetry fetch and re-verify."
        )
        steps = [resume[:500]] + ([target_step] if target_step else [])
        return S7Handoff(
            state=HandoffState.TELEMETRY_DEFERRED,
            next_fetch=target,
            price=price_next_fetch(target),
            resume_pointer=resume[:1024],
            recommended_next_steps=steps[:20],
        )

    if (
        "budget_exceeded" in signal_text
        or "defer_repair_budget_exhausted" in signal_text
        or "verification_failed_or_limit_reached" in signal_text
        or "limit_reached" in signal_text
    ):
        if directive is not None:
            resume = (
                f"Resume with a larger iteration/evidence budget at the "
                f"blocked fetch: {(target or directive)[:300]}"
            )
            steps = [directive[:500], resume[:500]] + ([target_step] if target_step else [])
            return S7Handoff(
                state=HandoffState.BUDGET_DEFERRED,
                next_fetch=target,
                price=price_next_fetch(target),
                resume_pointer=resume[:1024],
                recommended_next_steps=steps[:20],
            )

    record: list[str] = []
    if eliminated:
        record.append(
            f"Frontier exhausted: {len(eliminated)} rival(s) ruled out; see the elimination record."
        )
    if alternatives:
        record.append(f"Ranked alternatives: {alternatives[0][:300]}")
    if directive is not None:
        record.append(directive[:500])
    if target_step is not None:
        record.append(target_step)
    # Review T2: no filler sentence. When no rival was ruled out, no
    # alternative ranked and no fetch named, the handoff records nothing -
    # a generic line here made every abstention look like a handoff.
    return S7Handoff(
        state=HandoffState.FRONTIER_EXHAUSTED,
        next_fetch=target,
        price=price_next_fetch(target),
        resume_pointer=None,
        recommended_next_steps=record[:20],
    )


class InvestigationConclusion(BaseModel):
    """Terminal investigation report and root cause conclusion."""

    model_config = ConfigDict(frozen=True)

    tenant_id: str = Field(..., max_length=128)
    investigation_id: UUID
    status: ConclusionStatus
    root_cause: str | None = Field(default=None, max_length=2048)
    supporting_evidence_ids: list[UUID] = Field(default_factory=list, max_length=200)
    supporting_hypothesis_ids: list[UUID] = Field(default_factory=list, max_length=50)
    contradicting_evidence_ids: list[UUID] = Field(default_factory=list, max_length=200)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    limitations: list[str] = Field(default_factory=list, max_length=20)
    recommended_next_steps: list[str] = Field(default_factory=list, max_length=20)
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    # D5: in-band (likely) conclusions publish the best supporting reason
    # and citable against-evidence with a provisional marker plus the
    # re-verify directive (what would promote them to established).
    best_reason: str = Field(default="", max_length=2000)
    against_evidence: list[str] = Field(default_factory=list, max_length=20)
    provisional: bool = False
    # F: ranked alternatives (runner-up hypothesis statements with status)
    # so partial answers survive instead of evaporating with abstention.
    ranked_alternatives: list[str] = Field(default_factory=list, max_length=10)
    # Phase 2.4: the single re-verify directive that would promote this
    # conclusion (test the untested rival, cite the uncited sentence,
    # corroborate with another provider). None when established clean.
    next_check: str | None = Field(default=None, max_length=1024)
    # Caller enumeration diagnostics are informational only. Incomplete
    # enumeration remains INSUFFICIENT_EVIDENCE and can never become PARTIAL
    # or ANSWERED through these fields.
    proven_callers: list[str] = Field(default_factory=list, max_length=100)
    unresolved_callers: list[str] = Field(default_factory=list, max_length=100)
    caller_coverage: str | None = Field(default=None, max_length=32)
    # P3 (S6 rival bar): ruled-out rivals with their ruling-out evidence.
    # A causal ESTABLISHED verdict requires a non-empty record; without one
    # the verdict demotes to LIKELY. Empty on legacy rows (never backfilled).
    eliminated_rivals: list["EliminatedRival"] = Field(default_factory=list, max_length=10)
    # P4 (S7 handoff): the structured terminal handoff (version + state +
    # target + fetch + resume pointer). None = legacy/pre-marker row.
    # The operator-facing steps stay duplicated in recommended_next_steps.
    handoff: S7Handoff | None = Field(default=None)
    # 020: verifier provenance. None = legacy/pre-marker historical row.
    verifier_version: str | None = Field(default=None, max_length=16)

    @model_validator(mode="after")
    def validate_conclusion_invariants(self) -> "InvestigationConclusion":
        if self.status == ConclusionStatus.ROOT_CAUSE_ESTABLISHED:
            if not self.root_cause:
                raise ValueError("ROOT_CAUSE_ESTABLISHED requires a detailed root cause statement.")
            if not self.supporting_evidence_ids:
                raise ValueError("ROOT_CAUSE_ESTABLISHED requires supporting evidence IDs.")
        if self.status == ConclusionStatus.ROOT_CAUSE_LIKELY:
            # H13: a provisional verdict is still a verdict; it carries the same
            # evidence invariant as ESTABLISHED.
            if not self.root_cause:
                raise ValueError("ROOT_CAUSE_LIKELY requires a root cause statement.")
            if not self.supporting_evidence_ids:
                raise ValueError("ROOT_CAUSE_LIKELY requires supporting evidence IDs.")
        if self.status == ConclusionStatus.ANSWERED:
            if not self.root_cause:
                raise ValueError("ANSWERED requires the answer statement.")
            if not self.supporting_evidence_ids:
                raise ValueError("ANSWERED requires supporting evidence IDs.")
        return self
