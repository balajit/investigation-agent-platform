# Design: Query-to-Verify Improved Proof Core

Native proof-and-evidence core for IAP: claim-local retrieval binding, typed relation edges, a shared proof-obligation vocabulary, and deterministic obligation-directed routing inside the existing workflow, with optional analyzer path witnesses and narrow constraint-solver checks gated behind shadow evaluation. No new runtime dependency, no authoritative role for any external witness.

## Context

IAP investigates source code by decomposing questions into typed claims (`ClaimKind`: `DEFINITION | CALL_EDGE | BEHAVIOR | ABSENCE | HISTORY | CAUSAL` in `src/investigation_agent_platform/domain/hypothesis/models.py:35-54`) with revision-aware targets (`ClaimTarget` in `domain/hypothesis/models.py:56-76`), banking evidence rows with provenance (`domain/evidence/models.py:118-151`, `domain/provenance/models.py:10-51`), and verifying deterministically in `VerificationEngine` (`src/investigation_agent_platform/application/investigation/verification.py:513-521`) under `ConclusionGate` (`verification.py:3909-3981`). Live battery runs show a banked-but-unverified signature: evidence rows exist yet hypotheses stay `INCONCLUSIVE` and conclusions persist `INSUFFICIENT_EVIDENCE`, with zero recorded causal chains and generic blockers that do not say which proof obligation failed. The verified mechanisms behind that signature are binding-scoped, not retrieval-scoped: formulate validates citations against rows existing at that turn, per-claim IDs stay claim-local (`application/worker/activities.py:5265-5332`), verify/conclude re-resolution drops stale or foreign IDs (`activities.py:5859-5869,6821-6831`), range repair joins only revision-matching range reads (`verification.py:2252-2317`), any bound refuting evidence withholds fast paths (`activities.py:5911-6032`), and causal steps attach only on `VERIFY_HYPOTHESIS` with resolvable IDs (`activities.py:5466-5533`). This design closes that hole natively first, then adds optional witnesses only where measured gaps remain.

### Changes to architecture documentation

No `docs/dev/architecture.md` exists in the project. On implementation, update `docs/IAP-architecture-v1.0.md` with a new section covering the per-claim retrieval ledger, the typed relation edge and resolution tiers, the shared obligation vocabulary, and the deterministic routing policy, plus the shadow-evaluation and graduation bars for the optional analyzer and solver pilots. Record each rollout flag and its default in the same section so operators can reason about behavior without reading code.

## References

- [docs/IAP-architecture-v1.0.md](./IAP-architecture-v1.0.md) — Current IAP architecture, verification states, gate semantics, and profile conventions this design builds on.
- [docs/REV_BATTERY_CLOSE_PLAN.md](./REV_BATTERY_CLOSE_PLAN.md) — Battery failure analysis and proof-obligation tracking plan whose failure buckets this design adopts as the diagnostic vocabulary.
- [docs/DS_REVIEW_v0.1.md](./DS_REVIEW_v0.1.md) — Code-grounding contracts (target, revision, evidence, verification) whose range-containment and revision-agreement rules remain authoritative.
- [docs/BATTERY_LIVE_PROTOCOL.md](./BATTERY_LIVE_PROTOCOL.md) — Live battery execution and grading protocol reused for shadow evaluation and graduation decisions.
- [SMT-LIB standard logics](https://smt-lib.org/logics.shtml) — Defines the `QF_LIA` and `QF_LRA` fragments that bound the optional constraint-solver pilot to decidable linear arithmetic.

## Goals / Non-Goals

**Goals:**

- Claim-local retrieval-to-proof closure: every checkable claim binds its own retrieval attempts, grounds citations strictly, and demotes loudly on evaporation.
- Typed relation edges with resolution tiers and honest unresolved state for call and data-flow relationships.
- One shared proof-obligation vocabulary produced by the verifier and consumed by routing.
- Deterministic smallest-valid-action routing inside the existing durable workflow, shadow-tested before it owns any decision.
- Eval gates that enforce zero-unsatisfied-on-verified by default with a labeled report-only override.
- Optional analyzer path witnesses and narrow constraint-solver checks, each shadow-gated with explicit graduation bars and no gate authority.
- Preservation of honest abstention and all existing fail-closed gates.

**Non-Goals:**

- A second durable runtime, checkpoint store, or orchestration framework — the existing workflow stays.
- A central graph database or analyzer platform — native resolvers and the existing call-graph providers stay primary.
- Lexical or model-judged proof of source-code relationships — deterministic predicates remain the only code proof.
- Solver-based reasoning over call graphs, history, absence, dispatch, or whole-program behavior.
- New persisted tables, caches, or API surfaces before shadow evidence justifies them.
- Threshold or semantics changes to `VERIFIED`, `INCONCLUSIVE`, `CONTRADICTED`, `ANSWERED`, `LIKELY`, or abstention states to improve answer rate.

## Decisions

### D1: Synthetic proof-closure harness gate as a hard precondition

**Decision:** No verifier, prompt, or routing change lands until a test-only proof-closure harness demonstrates that reported battery failures classify into exactly one explicit failure bucket each. The harness builds representative L2 (caller enumeration), L3 (history), L5 (guarded behavior), and L8 (nonexistent target) shapes in the banked-but-unverified, bound-but-wrong-type, wrong-revision, evaporated-ID, missing-causal-step, and genuinely-absent variants, runs the current verifier and battery grader over them, and reports the bucket per fixture. The gate passes when every fixture lands in exactly one bucket, existing honest-abstention fixtures still abstain, and a sampled set of real banked-but-unverified runs classifies the same way. Until the gate passes, only harness fixtures and pure helper functions may be written; no authoritative wiring changes.

```python
# src/investigation_agent_platform/application/investigation/proof_closure.py
"""Test-only proof-closure harness. No production wiring reads this module."""
from __future__ import annotations

from pydantic import BaseModel, Field


FAILURE_BUCKETS: tuple[str, ...] = (
    "never_retrieved",
    "retrieved_but_unbound",
    "bound_but_wrong_type",
    "withheld_by_refuting",
    "provider_or_analyzer_failed",
    "genuinely_insufficient",
    "wrong_revision_or_scope",
)


class ClosureFixture(BaseModel):
    """One synthetic or sampled-real run shape with a known-correct bucket."""

    fixture_id: str = Field(description="Stable fixture identity, e.g. L2-caller-enumeration-evaporated.")
    battery_shape: str = Field(description="Battery shape under test: L1, L2, L3, L4, L5, or L8.")
    expected_bucket: str = Field(description="The single failure bucket this fixture must report.")
    must_abstain: bool = Field(
        default=False,
        description="True when the correct terminal outcome is honest abstention.",
    )


class ClosureReport(BaseModel):
    """Harness outcome for one fixture."""

    fixture_id: str = Field(description="Fixture identity from ClosureFixture.")
    reported_bucket: str | None = Field(
        default=None,
        description="Bucket reported by the current verifier plus grader, or None if unclassifiable.",
    )
    abstained: bool = Field(default=False, description="Whether the run abstained.")
    passed: bool = Field(description="True only when reported_bucket matches expected and abstention matches.")


def grade_closure(fixtures: list[ClosureFixture], reporter) -> list[ClosureReport]:
    """Run each fixture through the supplied reporter and check single-bucket classification.

    The reporter callable wraps the current verifier plus battery grader and returns
    (reported_bucket, abstained) for a fixture. This function performs no I/O itself.
    """
    reports: list[ClosureReport] = []
    for fixture in fixtures:
        reported_bucket, abstained = reporter(fixture)
        bucket_ok = reported_bucket == fixture.expected_bucket
        abstain_ok = abstained == fixture.must_abstain
        reports.append(
            ClosureReport(
                fixture_id=fixture.fixture_id,
                reported_bucket=reported_bucket,
                abstained=abstained,
                passed=bool(bucket_ok and abstain_ok),
            )
        )
    return reports


def gate_passed(reports: list[ClosureReport]) -> bool:
    """Hard gate predicate: every fixture classified exactly once and correctly."""
    if not reports:
        return False
    return all(report.passed and report.reported_bucket is not None for report in reports)
```

**Alternative considered:** Proceeding directly to verifier demotion in parallel with harness construction. Rejected — the demotion paths bake the bucket taxonomy into product behavior, and a wrong taxonomy would misattribute every future failure and then route on it.

**Rationale:** The harness is the cheapest falsification of the shared premise that binding, not retrieval or verifier semantics, dominates the battery failures. It costs fixtures, not rework.

**Trade-offs:** Live signal is deferred by one slice, and synthetic fixtures cannot capture model sampling or retrieval variance, so the gate additionally requires sampled real runs to classify the same way or the taxonomy is fixed instead of proceeding.

---

### D2: Per-claim retrieval ledger

**Decision:** Every checkable claim carries its own retrieval ledger recording the query text that was issued for it and the evidence IDs each attempt returned. Verification resolves a claim's citations against that claim's retrieved set first and the investigation bank second, and records retrieved, bound, grounded, and evaporated ID sets per claim. Uncheckable claims (taste, preference, value judgement with no code anchor) never dispatch retrieval and never enter the publish denominator. Legacy rows without a ledger entry degrade to an explicitly labeled `unmapped` fallback rather than failing or silently verifying.

```python
# src/investigation_agent_platform/application/investigation/retrieval_ledger.py
"""Per-claim retrieval ledger. Pure data helpers; no I/O, no LLM calls."""
from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class RetrievalAttempt(BaseModel):
    """One provider operation issued for one claim."""

    operation: str = Field(description="Provider operation, e.g. GET_CODE, SEARCH_CODE, GET_HISTORY.")
    query_fingerprint: str = Field(description="Provenance query fingerprint of the issued operation.")
    returned_evidence_ids: list[UUID] = Field(
        default_factory=list,
        description="Evidence IDs returned by this attempt in this investigation.",
    )
    attempted_at: datetime = Field(description="When the attempt executed.")


class ClaimRetrievalLedger(BaseModel):
    """Retrieval history for one (statement, claim-kind) claim."""

    claim_key: str = Field(description="Deterministic key: normalized statement plus claim kind.")
    search_query: str = Field(
        default="",
        description="Keywords plus symbol stems issued for this claim, 3-10 terms when present.",
    )
    checkable: bool = Field(
        default=True,
        description="False for unfalsifiable claims, which never dispatch retrieval.",
    )
    attempts: list[RetrievalAttempt] = Field(default_factory=list)


def claim_local_ids(ledger: ClaimRetrievalLedger) -> set[UUID]:
    """Union of IDs retrieved for this claim across all attempts."""
    found: set[UUID] = set()
    for attempt in ledger.attempts:
        found.update(attempt.returned_evidence_ids)
    return found


def partition_citations(
    cited_ids: list[UUID],
    ledger: ClaimRetrievalLedger,
    bank_ids: set[UUID],
) -> tuple[set[UUID], set[UUID], set[UUID]]:
    """Split cited IDs into claim-local, bank-fallback, and unknown sets.

    Returns (claim_local, bank_fallback, unknown). Unknown IDs are dropped by the
    caller and recorded as evaporated with an explicit reason, never silently.
    """
    local = claim_local_ids(ledger)
    cited = set(cited_ids)
    claim_local = cited & local
    bank_fallback = (cited - local) & bank_ids
    unknown = cited - local - bank_ids
    return claim_local, bank_fallback, unknown
```

**Alternative considered:** Keeping the global bank as the sole resolution scope with an evaporation audit. Rejected — without claim-local linkage the loop cannot distinguish never-retrieved-for-this-claim from retrieved-but-wrong-type, so the next action stays generic and the L2/L3/L5 loop keeps re-searching broadly.

**Rationale:** Presence in the bank is not proof. Claim-local resolution makes the binding requirement mechanical and gives routing a named missing proof instead of a generic blocker.

**Trade-offs:** One bounded re-retrieval per unsatisfied claim adds one to two actions on caller-enumeration, history, and behavior shapes. Poor per-claim queries can produce scoped misses, mitigated by the same-file-and-revision repair bound in D3 and the `unmapped` legacy fallback.

---

### D3: Strict grounding with bounded repair and evaporated-evidence demotion

**Decision:** Fuzzy matching may find rows and drive orientation, but only strict kind-specific predicates may prove. Every cited row must satisfy the existing deterministic gate for its claim kind at the same revision and path: range containment with recorded start and end lines for location and value claims, a shown call site for call edges, file plus commit-prefix plus subject match for history, and scoped `match_count == 0` proof rows for absence. Misattributed IDs may be repaired by content match only when the donor row is same-file, same-revision, and kind-grounded for the same claim, with the repair recorded. A claim whose every citation evaporates is `INCONCLUSIVE` with named evaporated IDs and a smallest-fetch next action, never verified and never silent. `SUPPORTED` with zero kept citations is impossible by construction.

```python
# src/investigation_agent_platform/application/investigation/grounding.py
"""Strict grounding helpers. Predicate dispatch reuses existing kind gates."""
from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, Field


SEVEN_BUCKETS: tuple[str, ...] = (
    "never_retrieved",
    "retrieved_but_unbound",
    "bound_but_wrong_type",
    "withheld_by_refuting",
    "provider_or_analyzer_failed",
    "genuinely_insufficient",
    "wrong_revision_or_scope",
)


class GroundingOutcome(BaseModel):
    """Per-claim grounding result with full audit trail."""

    claim_key: str = Field(description="Claim key matching ClaimRetrievalLedger.claim_key.")
    kept_ids: list[UUID] = Field(default_factory=list, description="Citations that grounded strictly.")
    evaporated_ids: list[UUID] = Field(default_factory=list, description="Citations dropped with reasons.")
    repaired_from: dict[str, str] = Field(
        default_factory=dict,
        description="Kept ID to original cited ID for bounded misattribution repairs.",
    )
    demoted: bool = Field(default=False, description="True when citations existed but none grounded.")
    bucket: str = Field(description="Exactly one of the seven failure buckets, or 'grounded'.")


def bounded_repair_allowed(original_id: UUID, donor_id: UUID, *, same_file: bool, same_revision: bool, kind_grounded: bool) -> bool:
    """Repair gate: donor must be same-file, same-revision, and kind-grounded.

    Original and donor content match is checked by the caller. This predicate adds
    the scope bounds so repair cannot rebind across files or revisions.
    """
    _ = (original_id, donor_id)
    return bool(same_file and same_revision and kind_grounded)
```

The seven failure buckets are `never_retrieved`, `retrieved_but_unbound`, `bound_but_wrong_type`, `withheld_by_refuting`, `provider_or_analyzer_failed`, `genuinely_insufficient`, and `wrong_revision_or_scope`. Every `INCONCLUSIVE` claim reports exactly one, and every blocker maps to a smallest-fetch next action in D8.

**Alternative considered:** Demotion without the repair path (drop all unresolvable citations). Rejected — misattributed-ID cases where the right row exists under a different ID would keep failing despite available proof; bounded repair converts those drops into keeps without opening cross-file rebinding.

**Rationale:** This is the smallest change that closes false accepts from construction-as-invocation, comment-line-as-proof, and stale-ID citations while turning silent drops into named, routable blockers.

**Trade-offs:** Telemetry needs a closed label vocabulary to avoid cardinality blowup; reason labels are bucket names, never SHAs or raw text. Over-strictness on dynamic dispatch is bounded by the speculative-hop cap in D4 rather than relaxed grounding.

---

### D4: Typed relation edges with resolution tiers and endpoint certainty

**Decision:** Every material code relation becomes a typed edge object carrying caller, call site, callee, ordered intermediates, one location per hop at the pinned revision, a resolution tier, and provenance. Resolution tiers order `STATIC` (exact symbol match), `DYNAMIC` (hierarchy- or injection-aware dispatch), `SPECULATED` (hint, name-only, or external-stub match), and `UNRESOLVED` (no edge, retained hint). Unlinked calls are `UNRESOLVED` with receiver-type or configuration hints carried into the missing-obligation tool hint, never silence and never a fabricated edge. A speculative hop with certain endpoints caps at `LIKELY`; a speculative endpoint, or construction counted as invocation, fails closed. Path depth is tunable with default 4; longer-than-cap is unsatisfied with the truncated path retained as the hint.

```python
# src/investigation_agent_platform/domain/relations/edges.py
"""Typed relation edge vocabulary. Pure model; resolution order enforced by checkers."""
from __future__ import annotations

from enum import Enum
from uuid import UUID

from pydantic import BaseModel, Field


class ResolutionTier(str, Enum):
    """Ordered resolution strength. Order matters: STATIC > DYNAMIC > SPECULATED > UNRESOLVED."""

    STATIC = "STATIC"
    DYNAMIC = "DYNAMIC"
    SPECULATED = "SPECULATED"
    UNRESOLVED = "UNRESOLVED"


class EdgeHop(BaseModel):
    """One hop in a verified relation path at the pinned revision."""

    ordinal: int = Field(description="Zero-based position along the path.")
    file_path: str = Field(description="Repository-relative file path.")
    line_start: int = Field(description="First line of the hop window, 1-based.")
    line_end: int = Field(description="Last line of the hop window, 1-based.")
    role: str = Field(description="Hop role, e.g. entry, proxy, dispatch, handler.")
    evidence_id: UUID = Field(description="Banked row showing this hop at the pinned revision.")


class RelationEdge(BaseModel):
    """A claimed code relation with its proof state."""

    edge_id: str = Field(description="Deterministic edge identity: caller, site, callee, revision.")
    caller: str = Field(description="Calling symbol or component name.")
    callsite_file: str = Field(description="Repository-relative file containing the call site.")
    callee: str = Field(description="Called symbol or handler name.")
    revision: str = Field(description="Pinned repository revision all hops must share.")
    resolution: ResolutionTier = Field(description="Strongest tier established for this edge.")
    endpoint_certain: bool = Field(
        description="True only when both endpoint identities are certain, regardless of hop speculation.",
    )
    hops: list[EdgeHop] = Field(default_factory=list, description="Ordered intermediates, empty when unresolved.")
    retained_hint: str = Field(
        default="",
        description="Receiver-type or configuration hint retained when UNRESOLVED.",
    )
    decided_by: str = Field(default="", description="Checker tier that decided, for audit.")


def edge_ceiling(edge: RelationEdge) -> str:
    """Maximum publishable outcome for an edge. Fail-closed on uncertain endpoints."""
    if edge.resolution is ResolutionTier.UNRESOLVED:
        return "UNSATISFIED"
    if not edge.endpoint_certain:
        return "UNSATISFIED"
    if edge.resolution is ResolutionTier.SPECULATED:
        return "LIKELY"
    return "VERIFIED_CANDIDATE"
```

**Alternative considered:** A central graph database or external analyzer platform as the relation store. Rejected — substantially larger than the demonstrated need, duplicates existing topology and analysis foundations, and adds storage, indexing, and operational surface for a problem that currently fails at single-hop grounding.

**Rationale:** The battery failure is representational: rows banked, no edge object, so `chains=0` is a mystery. Edges make each hop inspectable and each gap routable.

**Trade-offs:** Edge plumbing across formulate, verify, and conclude plus persistence; tier-ordering review per language; depth-cap tuning per profile. Over-strictness on dynamic dispatch is handled by the `LIKELY` cap for certain endpoints, never by relaxing endpoint certainty.

---

### D5: Dispatch and semantics table in per-tenant profiles

**Decision:** Language and framework dispatch wiring (annotation-to-edge rules, injection equivalents, sanitizer-equivalents for blocked paths) lives in per-tenant profile configuration with explicit versioning. Verifier code holds only the query mechanics and the tier ordering. Unknown wiring yields `UNRESOLVED` with the wiring gap named in the retained hint, never silence and never a speculative pass.

**Alternative considered:** Hardcoding dispatch rules in verifier code. Rejected — rules vary per tenant stack and framework version, and code-embedded rules cannot be tuned without a release.

**Rationale:** Profiles already vary per onboarded tenant, so dispatch knowledge follows the same versioning and review path as other tenant configuration instead of scattering substring heuristics through checkers.

**Trade-offs:** Profile authoring and review cost per tenant; stale profiles can over-narrow resolution until refreshed, which surfaces as `UNRESOLVED` with a named wiring hint rather than a false negative.

---

### D6: Shared proof-obligation vocabulary

**Decision:** The verifier emits one shared typed-obligation vocabulary per claim and causal link, consumed by routing, telemetry, and the battery. Obligation statuses are `OPEN`, `SATISFIED`, `CONFLICTED`, `UNSUPPORTED`, and `NOT_APPLICABLE`. Each unsatisfied obligation carries a stable missing code, the claim or step identity, target and scope, bound evidence IDs, and the verifier generation. A separate next-action record names the obligation it discharges, the validated action type and parameters, the expected discriminator, and attempt counts. These are planning facts; they never redefine `VERIFIED`, `INCONCLUSIVE`, or `CONTRADICTED`.

```python
# src/investigation_agent_platform/application/investigation/obligations.py
"""Shared proof-obligation vocabulary. Derived per verification pass; see D8 for routing."""
from __future__ import annotations

from enum import Enum
from uuid import UUID

from pydantic import BaseModel, Field


class ObligationKind(str, Enum):
    """Stable obligation families mapped from claim kinds and fact categories."""

    LOCATION_RANGE = "LOCATION_RANGE"
    CALL_EDGE = "CALL_EDGE"
    BEHAVIOR_CODE = "BEHAVIOR_CODE"
    BEHAVIOR_RUNTIME = "BEHAVIOR_RUNTIME"
    ABSENCE_SCOPE = "ABSENCE_SCOPE"
    HISTORY_MATCH = "HISTORY_MATCH"
    CAUSAL_LINK = "CAUSAL_LINK"


class ObligationStatus(str, Enum):
    """Per-obligation proof state. Planning facts only."""

    OPEN = "OPEN"
    SATISFIED = "SATISFIED"
    CONFLICTED = "CONFLICTED"
    UNSUPPORTED = "UNSUPPORTED"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class ProofObligation(BaseModel):
    """One required proof unit for one claim or causal link."""

    obligation_id: str = Field(description="Deterministic identity over claim, kind, target, revision, verifier version.")
    claim_key: str = Field(description="Claim key matching ClaimRetrievalLedger.claim_key.")
    kind: ObligationKind = Field(description="Obligation family.")
    target: str = Field(description="Symbol, path, scope, or link reference the obligation is about.")
    repository_revision: str = Field(description="Pinned revision or scope the obligation applies to.")
    status: ObligationStatus = Field(description="Current proof state of this obligation.")
    missing_code: str = Field(default="", description="Stable code when OPEN, e.g. CALLSITE_NOT_SHOWN.")
    bound_evidence_ids: list[UUID] = Field(default_factory=list)
    verifier_version: str = Field(description="Verifier generation that produced this status.")


class NextEvidenceAction(BaseModel):
    """One validated evidence action discharging one obligation."""

    obligation_id: str = Field(description="Obligation this action is expected to discharge.")
    action_type: str = Field(description="Validated action type from the registered capability set.")
    parameters: dict[str, str] = Field(default_factory=dict, description="Validated action parameters.")
    expected_discriminator: str = Field(description="What the action result must show to satisfy the obligation.")
    attempt_count: int = Field(default=0, description="Attempts already spent on this obligation.")
    status: str = Field(default="PROPOSED", description="PROPOSED, DISPATCHED, COMPLETED, or DEGRADED.")


def expand_obligations(claim_key: str, claim_kind: str, fact_category: str, revision: str, verifier_version: str) -> list[ProofObligation]:
    """Table read from (claim kind, fact category) to required obligation rows.

    The full table is data, not prose: each supported (kind, category) pair maps to
    its required obligation kinds. Unknown pairs yield no obligations and the claim
    is marked UNSUPPORTED rather than guessed.
    """
    table: dict[tuple[str, str], list[ObligationKind]] = {
        ("DEFINITION", "SOURCE_FACT"): [ObligationKind.LOCATION_RANGE],
        ("CALL_EDGE", "CALL_FACT"): [ObligationKind.CALL_EDGE],
        ("BEHAVIOR", "SOURCE_FACT"): [ObligationKind.BEHAVIOR_CODE],
        ("BEHAVIOR", "RUNTIME_FACT"): [ObligationKind.BEHAVIOR_CODE, ObligationKind.BEHAVIOR_RUNTIME],
        ("ABSENCE", "ABSENCE_FACT"): [ObligationKind.ABSENCE_SCOPE],
        ("HISTORY", "HISTORY_FACT"): [ObligationKind.HISTORY_MATCH],
        ("CAUSAL", "CAUSAL_FACT"): [ObligationKind.CAUSAL_LINK],
    }
    kinds = table.get((claim_kind, fact_category), [])
    return [
        ProofObligation(
            obligation_id=f"{claim_key}:{kind.value}:{revision}:{verifier_version}",
            claim_key=claim_key,
            kind=kind,
            target=claim_key,
            repository_revision=revision,
            status=ObligationStatus.OPEN,
            verifier_version=verifier_version,
        )
        for kind in kinds
    ]
```

**Alternative considered:** Two parallel vocabularies, one for binding gaps and one for routing. Rejected — duplicate state with reconciliation risk; every divergence becomes a new mystery equivalent to the one being removed.

**Rationale:** One vocabulary lets the verifier say what is missing, the router choose the smallest valid fetch, telemetry count the same codes the battery grades, and reviewers audit a single contract.

**Trade-offs:** Stable identity and versioning discipline; claim mutation and evidence invalidation must recompute obligations, or stale `SATISFIED` rows could linger. Derived-per-pass computation avoids persistence risk until shadow data justifies it.

---

### D7: Obligation table with deterministic-first cascade and a gated behavioral extension

**Decision:** The `(claim kind, fact category)` table in D6 is the single expansion point; the six existing validators plus claim-kind dispatch become rows in that table rather than parallel code paths over time. Deterministic rule kernels run first for every obligation and are the only path for source identity, revision pinning, and range reads. A model-graded entailment extension exists only for `BEHAVIOR` and `CAUSAL` sentences where no deterministic kernel can decide, runs only on fall-through after deterministic abstention, and uses a strict grade contract with a panel vote that withholds on ties. An LLM never establishes that a file, line range, or revision exists; it may only judge whether a cited row says what the claim says it says. Verdict roll-up: all required obligations `SATISFIED` (ignoring `NOT_APPLICABLE`) rolls up to `VERIFIED` subject to existing per-category verdict ceilings; any `CONFLICTED` rolls up to `CONTRADICTED`; otherwise `INCONCLUSIVE` with at least one missing obligation.

**Alternative considered:** Model grading as a peer proof path for all claim kinds. Rejected — lexical overlap cannot prove calls, data flow, defaults, or absence, and peer status would let a fluent judgement override a failed deterministic check.

**Rationale:** Deterministic-first preserves every current fail-closed property while giving the two genuinely judgement-shaped obligations a disciplined, votable, auditable extension instead of an unbounded heuristic.

**Trade-offs:** Panel voting adds latency on the fall-through path only; contested panels withhold to unscored human review rather than blocking permanently, which needs an explicit review queue to avoid silent queues.

---

### D8: Deterministic action policy inside the existing workflow with shadow strangler

**Decision:** A pure policy function maps open obligations plus registered capabilities, action history, and remaining budgets to the smallest valid next action, returning the missing-code-to-action mapping as data. The LLM reasoner may propose or choose among valid actions only when the deterministic mapping does not decide. The policy runs in shadow alongside existing forcing rules first, logging closed-vocabulary divergence; only mapped cases transfer to policy authority behind a flag, and existing forcing helpers are removed only after parity and battery acceptance. The existing durable workflow keeps retries, signals, budgets, checkpoints, and terminal transitions; no second runtime is introduced.

```python
# src/investigation_agent_platform/application/investigation/action_policy.py
"""Deterministic next-action policy. Pure function: no I/O, no LLM, no budget mutation."""
from __future__ import annotations

from pydantic import BaseModel, Field

from .obligations import NextEvidenceAction, ObligationStatus, ProofObligation


class PolicyInput(BaseModel):
    """Everything the policy may read. Anything else is out of scope by construction."""

    obligations: list[ProofObligation] = Field(description="Current derived obligations for the candidate.")
    action_history: list[str] = Field(default_factory=list, description="Action types already dispatched.")
    capabilities: list[str] = Field(default_factory=list, description="Registered allowed action types.")
    budget_remaining: bool = Field(default=True, description="False when no evidence actions remain.")


FETCH_FOR_MISSING: dict[str, str] = {
    "DEFINITION_RANGE_MISSING": "GET_CODE",
    "CALLSITE_NOT_SHOWN": "GET_CODE",
    "RUNTIME_LEG_MISSING": "SEARCH_LOGS",
    "HISTORY_ROW_MISSING": "GET_HISTORY",
    "ABSENCE_SCOPE_UNPROVEN": "SEARCH_CODE",
    "CAUSAL_STEP_UNGROUNDED": "GET_CODE",
}


def choose_next_action(policy_input: PolicyInput) -> NextEvidenceAction | None:
    """Return the smallest valid action for the first open obligation, or None.

    Priority follows obligation order supplied by the verifier. An action type must
    be registered as capable and must not repeat a history entry more than once;
    otherwise the next obligation is tried. None means abstain with missing codes.
    """
    if not policy_input.budget_remaining:
        return None
    seen_counts: dict[str, int] = {}
    for action in policy_input.action_history:
        seen_counts[action] = seen_counts.get(action, 0) + 1
    for obligation in policy_input.obligations:
        if obligation.status is not ObligationStatus.OPEN:
            continue
        if not obligation.missing_code:
            continue
        action_type = FETCH_FOR_MISSING.get(obligation.missing_code)
        if action_type is None:
            continue
        if action_type not in policy_input.capabilities:
            continue
        if seen_counts.get(action_type, 0) >= 2:
            continue
        return NextEvidenceAction(
            obligation_id=obligation.obligation_id,
            action_type=action_type,
            parameters={"target": obligation.target, "revision": obligation.repository_revision},
            expected_discriminator=f"{obligation.kind.value} satisfied at {obligation.target}",
        )
    return None
```

**Alternative considered:** Replacing the workflow runtime with a graph-based orchestration framework. Rejected — it would duplicate replay, retry, signal, and checkpoint semantics without supplying any proof logic, creating dual state authority for zero verification gain.

**Rationale:** The gap is routing generality, not execution machinery: independent forcing heuristics with per-helper caps cannot express obligation precedence, while a testable pure function can, inside the loop that already works.

**Trade-offs:** A deterministic table can false-reject viable paths, so capability-aware fallback plus constrained LLM choice is required; per-turn derivation cost is bounded to the selected candidate; persisted obligations would risk staleness, so persistence waits for shadow evidence.

---

### D9: Eval gates enforce by default with a labeled report-only override

**Decision:** Battery acceptance gates assert per-obligation properties: every `VERIFIED` claim ships zero required non-`NOT_APPLICABLE` unsatisfied obligations; every `INCONCLUSIVE` ships at least one missing obligation whose tool hint names a real registered tool; citation validity holds at the current bar with malformed-response reasons counted as failures; rival-contrast and abstention goldens keep their semantics, asserted per obligation. Gates enforce by default and fail the run on violation; an environment override drops them to report-only for diagnosis runs with the downgrade labeled in the run record. Volatility is handled by repeating graded judgements over epochs and reducing contested outcomes to unscored human review rather than forcing a verdict.

**Alternative considered:** Advisory dashboards without enforcement. Rejected — unenforced gates document the same failures the battery already reports without changing what ships.

**Rationale:** Enforcement is what converts the obligation vocabulary from documentation into a contract: promotion of any claim kind, witness, or solver check is gated on measured behavior, not argument.

**Trade-offs:** Flaky-model variance can fail enforced gates; epochs plus majority reduction absorb sampling noise, and the labeled override preserves a diagnosis path without weakening the default.

---

### D10: Optional analyzer path-witness packet with read-only shadow adapter

**Decision:** A versioned analyzer path packet may be banked as typed evidence and consumed only for structural data-flow and call-path obligations where native providers are demonstrably thin. The packet carries schema version, analyzer and query-pack identity and version, database identity with the exact analyzed revision, path kind, source and sink with node identity plus file and line ranges plus model roles, ordered nodes and edges with ordinals, explicitly marked summarized segments, a truncation flag with omitted counts, and a result hash. Validation rejects revision mismatch, incomplete databases, over-bound payloads, and broken endpoint ordering before banking; truncation never silently satisfies a missing edge. A read-only adapter runs in shadow first comparing witness paths against native obligations; promotion of selected obligations behind a flag follows only on measured usable-path rate, with range reads and runtime checks remaining mandatory and missing results never treated as absence proof.

```python
# src/investigation_agent_platform/application/analysis/path_witness.py
"""Analyzer path-witness validation. Pure checks; ingestion banking stays in the evidence layer."""
from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, Field


class WitnessNode(BaseModel):
    """One ordered path node with location and provenance."""

    ordinal: int = Field(description="Zero-based position along the path.")
    node_identity: str = Field(default="", description="Analyzer node identity when available.")
    role: str = Field(description="Node role, e.g. source, transform, sink.")
    file_path: str = Field(description="Repository-relative file path.")
    line_start: int = Field(description="First line, 1-based.")
    line_end: int = Field(description="Last line, 1-based.")


class WitnessEdge(BaseModel):
    """One ordered path edge between witness nodes."""

    ordinal: int = Field(description="Zero-based position along the path.")
    from_ordinal: int = Field(description="Source node ordinal.")
    to_ordinal: int = Field(description="Destination node ordinal.")
    kind: str = Field(description="Edge kind, e.g. data_flow, call, control_dependence.")


class PathWitnessPacket(BaseModel):
    """Bounded structural path witness for one repository at one revision."""

    schema_version: str = Field(description="Packet contract version, e.g. v1.")
    analyzer_name: str = Field(description="Analyzer name for provenance.")
    analyzer_version: str = Field(description="Analyzer version for provenance and cache identity.")
    query_id: str = Field(description="Query or rule identity that produced the path.")
    database_revision: str = Field(description="Exact commit the analyzed database was built from.")
    target_revision: str = Field(description="Pinned investigation revision the path is offered for.")
    path_kind: str = Field(description="DATA_FLOW, TAINT_FLOW, CONTROL_DEPENDENCE, or CALL_PATH.")
    source_file: str = Field(description="Source node file.")
    sink_file: str = Field(description="Sink node file.")
    nodes: list[WitnessNode] = Field(default_factory=list)
    edges: list[WitnessEdge] = Field(default_factory=list)
    summarized_segment_count: int = Field(default=0, description="Subpaths hidden behind summaries.")
    truncated: bool = Field(default=False, description="True when output was cut to bounds.")
    result_hash: str = Field(description="Content hash binding nodes, edges, and identities.")


MAX_WITNESS_NODES = 64
MAX_WITNESS_EDGES = 128


def validate_witness(packet: PathWitnessPacket, max_nodes: int = MAX_WITNESS_NODES, max_edges: int = MAX_WITNESS_EDGES) -> list[str]:
    """Return a list of rejection reasons; empty means the packet may be banked.

    Rejections are fail-closed: revision mismatch, bound overflow, broken ordering,
    and empty paths are never banked as verifying evidence.
    """
    reasons: list[str] = []
    if packet.database_revision != packet.target_revision:
        reasons.append("revision_mismatch")
    if len(packet.nodes) > max_nodes:
        reasons.append("node_bound_exceeded")
    if len(packet.edges) > max_edges:
        reasons.append("edge_bound_exceeded")
    if not packet.nodes:
        reasons.append("empty_path")
    ordinals = [node.ordinal for node in packet.nodes]
    if ordinals != sorted(ordinals) or len(set(ordinals)) != len(ordinals):
        reasons.append("node_order_broken")
    edge_ordinals = [edge.ordinal for edge in packet.edges]
    if edge_ordinals != sorted(edge_ordinals) or len(set(edge_ordinals)) != len(edge_ordinals):
        reasons.append("edge_order_broken")
    node_set = set(ordinals)
    for edge in packet.edges:
        if edge.from_ordinal not in node_set or edge.to_ordinal not in node_set:
            reasons.append("edge_dangles")
            break
    return reasons
```

**Alternative considered:** Building analyzer coverage first and binding second. Rejected — richer witnesses would merely add more unbound rows, masking the simpler binding failure the harness gate is designed to surface.

**Rationale:** Optional witnesses help exactly one shape: ordered intermediates for modeled relations where native providers cannot produce a repeatable path. Everything else stays native.

**Trade-offs:** Database builds, query authoring, language coverage, and latency are real costs measured in shadow before any promotion; stale or mismatched databases are more misleading than a search miss, hence SHA-equality enforcement end to end.

---

### D11: Narrow constraint-solver pilot for admitted value and branch predicates

**Decision:** An ephemeral in-process check inside value and branch validation encodes the bound resolved source fact plus the negated claim as linear integer or real arithmetic with booleans, restricted to linear comparisons, null-guard booleans, and resolved equalities over a single window. Bit operations, strings, nonlinear arithmetic, quantifiers, loops, cross-file conditions, dispatch, reflection, and runtime configuration are inadmissible and return the native path with an explicit note. Admitted queries run under a per-query timeout and resource cap with no cross-claim solver state; a satisfiable negated claim yields a counterexample model that becomes a fetch target, an unsatisfiable one confirms the predicate within the encoded fragment only, and unknown, timeout, or exception degrades to the current demotion with the solver reason recorded. Solver output never satisfies a gate alone and is disabled-flag-identical to current behavior.

```python
# src/investigation_agent_platform/application/verification/solver_pilot.py
"""Narrow constraint-solver pilot. Ephemeral, diagnostic-only, flag-gated."""
from __future__ import annotations

from pydantic import BaseModel, Field


ADMITTED_THEORIES: tuple[str, ...] = ("QF_LIA", "QF_LRA")
PILOT_TIMEOUT_MS = 2000
PILOT_RLIMIT = 100000
PILOT_MAX_CALLS_PER_INVESTIGATION = 4


class SolverPredicate(BaseModel):
    """Normalized predicate offered to the solver for one claim."""

    claim_key: str = Field(description="Claim key the predicate was derived from.")
    theory: str = Field(description="One of the admitted theories.")
    declarations: list[str] = Field(description="Normalized predicate text for audit.")
    knowledge_base: list[str] = Field(description="Bound resolved-row literals only.")
    negated_claim: str = Field(description="Negation of the claim over the same symbols.")


class SolverVerdict(BaseModel):
    """Solver outcome attached to verification diagnostics, never gate-authoritative."""

    claim_key: str = Field(description="Claim key this verdict annotates.")
    outcome: str = Field(description="SAT, UNSAT, UNKNOWN, or INADMISSIBLE.")
    reason: str = Field(default="ok", description="Solver reason or admission note, closed vocabulary.")
    counterexample: dict[str, str] = Field(default_factory=dict, description="Named-symbol model on SAT.")
    elapsed_ms: int = Field(default=0, description="Wall time for budget accounting.")


def admit_predicate(theory: str, uses_bit_ops: bool, uses_strings: bool, uses_nonlinear: bool, uses_quantifiers: bool, cross_file: bool) -> str:
    """Admission gate. Returns 'ADMIT' or the inadmissibility reason."""
    if theory not in ADMITTED_THEORIES:
        return "theory_not_admitted"
    if uses_bit_ops:
        return "bit_ops_require_width_modeling"
    if uses_strings:
        return "string_content_unstable"
    if uses_nonlinear:
        return "nonlinear_undecidable_fragment"
    if uses_quantifiers:
        return "quantifiers_incomplete"
    if cross_file:
        return "cross_file_out_of_scope"
    return "ADMIT"
```

**Alternative considered:** Solver-based reasoning over call graphs, history, absence, or whole-program behavior. Rejected — encoding fidelity for those relations is unprovable, inapplicable fragments dominate, and the authoritative decision would rest on the least auditable component.

**Rationale:** The pilot tests the single overlap where a checked implication with a witness on each side could upgrade one demote-only heuristic into a check, at zero gate risk because the native path decides regardless.

**Trade-offs:** The admitted share may be small enough to decline the dependency permanently; encoder review is the real cost since any code-to-constraint mismatch is a soundness hole in the annotation, bounded by native primacy, same-revision scoping, and goldens.

---

### D12: Verdict and score hygiene

**Decision:** Scoring stays deterministic in Python with confidence-weighted contributions and a rival-shared-evidence penalty; unfalsifiable claims are excluded from publish denominators; solver and analyzer attributes are never keyed by claim text alone but always by claim identity plus repository, revision, file and line window, predicate or query identity, and theory or model version with timeout; bank-interest shared rows never inherit solver or analyzer verdicts across revisions and are re-checked on reuse or dropped.

**Alternative considered:** Caching verification verdicts by claim text for reuse. Rejected — text-equal claims at different revisions, windows, or evidence sets would inherit proofs that do not apply, silently breaking revision consistency and freshness guarantees.

**Rationale:** Hygiene rules are what let every other decision be adopted without weakening `ConclusionGate`, freshness, or provenance requirements.

**Trade-offs:** Re-checking shared rows costs redundant compute on cross-run reuse; freshness windows can force re-retrieval of still-valid rows, which is the correct price for revision safety.

## Data Storage

Early phases persist nothing new: the retrieval ledger, grounding outcomes, obligations, and solver verdicts ride existing JSON diagnostic fields (`assessments_json`, action-execution records, `loop_telemetry`) with forward-compatible optional keys, and old rows without those keys degrade to explicitly labeled fallbacks. A versioned obligation table is designed now but created only if shadow evidence justifies persistence, with invalidation on claim, revision, verifier-version, or evidence-availability change.

```python
# src/investigation_agent_platform/infrastructure/persistence/models.py (Phase-gated, not created until justified)
"""Future obligation persistence. Structural placeholder for future use. Not populated in this phase."""
from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column


class ProofObligationORM:  # noqa: D101 - placeholder pending shadow evidence
    """Versioned obligation row. Created only after shadow divergence justifies persistence."""

    __tablename__ = "proof_obligations"
    __table_args__ = (
        UniqueConstraint("tenant_id", "obligation_id", "verifier_version", name="uq_obligation_version"),
        Index("ix_obligation_investigation", "tenant_id", "investigation_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    tenant_id: Mapped[str] = mapped_column(String(64))
    investigation_id: Mapped[UUID] = mapped_column()
    obligation_id: Mapped[str] = mapped_column(String(256))
    claim_key: Mapped[str] = mapped_column(String(512))
    kind: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16))
    missing_code: Mapped[str] = mapped_column(String(128), default="")
    bound_evidence_ids: Mapped[list] = mapped_column(JSONB, default=list)
    verifier_version: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
```

## Data Structures

Input and output schemas are separate models. Creation inputs (`ClaimRetrievalLedger`, `RelationEdge`, `ProofObligation` construction parameters) are never reused as read outputs (`GroundingOutcome`, `SolverVerdict`, `ClosureReport`); outputs carry audit fields (kept, evaporated, repaired-from, reasons, elapsed) that inputs must not accept. All schemas use `Field(...)` with descriptions as shown in D2 through D4, D6, D8, D10, and D11. The obligation status alphabet is `OPEN`, `SATISFIED`, `CONFLICTED`, `UNSUPPORTED`, `NOT_APPLICABLE`, and the verdict roll-up in D7 maps those to `VERIFIED`, `CONTRADICTED`, or `INCONCLUSIVE` subject to existing per-category ceilings. The seven failure buckets in D3 are the closed vocabulary for blockers, telemetry, and battery grading.

## Interfaces

### Telemetry counters (closed vocabulary)

| Counter | Labels | Description |
|---|---|---|
| `obligation_total` | `kind`, `status`, `missing_code` | Derived obligations per verification pass |
| `evaporated_total` | `bucket` | Citations dropped with named reasons |
| `repaired_total` | `kind` | Bounded misattribution repairs applied |
| `policy_divergence_total` | `chosen_by` | Shadow policy vs forcing-rule agreement |
| `solver_total` | `outcome`, `reason` | Pilot solver calls with closed-vocabulary reasons |
| `witness_total` | `path_kind`, `result` | Analyzer witness validation outcomes |

### Environment flags

| Flag | Default | Description |
|---|---|---|
| `IAP_OBLIGATION_GATES` | `enforce` | `enforce` fails runs on gate violation; `report` downgrades to labeled report-only |
| `IAP_SOLVER_PILOT` | `off` | Enables the ephemeral solver pilot in shadow diagnostics |
| `IAP_WITNESS_ADAPTER` | `off` | Enables the read-only analyzer witness adapter in shadow |
| `IAP_POLICY_SHADOW` | `on` | Computes policy choice alongside forcing rules without owning routing |

### Battery grader dimensions

Grade conclusions, not corpus contents: answer state, exact cited evidence IDs, claim-local retrieval and binding, source revision and path and range, relation and path completeness, rival handling, absence scope, provider failure exclusion, and abstention correctness. `PARTIAL` must not count as a pass where the required relationship is absent.

## Implementation Detail

Implementation order is the implementation plan document. Engineering rules apply across all phases: verifier output threads through existing `verify_root_cause_activity` and `conclude_investigation_activity` regions in `application/worker/activities.py`; the policy lives in a new pure module `application/investigation/action_policy.py` with no I/O; the obligation expansion lives in `application/investigation/obligations.py` as a table read; witness validation lives in `application/analysis/path_witness.py`; the solver pilot lives in `application/verification/solver_pilot.py` as an ephemeral diagnostic; telemetry uses additive fail-open hooks with closed label vocabularies so a metrics failure can never fail verification; every flag-off path reproduces current behavior bit-for-bit, proven by flag-off-identical tests per phase.

## Migrations

Creation order: no migration in the harness, ledger, demotion, edge, obligation-derivation, or shadow-routing slices — all state rides existing JSON diagnostic fields. The versioned obligation table in Data Storage is created only after shadow divergence justifies persistence, with a forward-compatible Alembic revision, backfill defined as recompute-on-read (no backfill of historical obligations), and old rows without new keys degrading to labeled fallbacks. Analyzer and solver attributes are never migrated into_evidence rows; they remain per-run diagnostics. Compatibility note: served-cached conclusions follow primaries and grade the cached answer, so schema additions must never change cached verdict semantics.

## Testing Philosophy

### Proof-closure harness

Build synthetic and sampled-real fixtures for every representative shape before any product change, asserting single-bucket classification and preserved honest abstention. Treat an unclassifiable fixture as a taxonomy defect, not a test defect, and fix the buckets before proceeding.

### Grounding and repair

Pin the guardrail behaviors with positive, negative, and adversarial cases: near-identical fabrication rejected, right-row-wrong-ID repaired only within same-file and same-revision bounds, over-reach repair refused, and supported-with-evaporated demoted with named IDs. Mirror the strict-vs-fuzzy split so finding stays generous while proof stays exact.

### Edges and enumeration

Test tier ordering explicitly: static exact match, dispatch-aware dynamic, speculative hop with certain endpoints capped at likely, speculative endpoint and construction-as-invocation failing closed, caller-enumeration completeness enforced, and depth-cap truncation retaining the path as a hint with the obligation unsatisfied.

### Policy shado
...[truncated 3881 chars]