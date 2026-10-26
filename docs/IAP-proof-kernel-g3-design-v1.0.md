# Design: Proof-Kernel G3 Retrieval Closure v1.0

> Status: as-built contract. Owner APPROVED this design and separately authorized G3 implementation (2026-10-25; see recommendations log); Steps 1–7 are implemented and the first live battery sample has run (traceability G3 row). The G3 gate remains PARTIAL pending a measurable live sample and explicit owner sign-off — no numeric result closes it alone. It does not close G4, authorize production behavior changes, or permit repair-by-assumption.
> Scope: per-claim retrieval binding, bounded re-retrieval directives, provider-total exposure, and a benefit battery with a pass bar. Everything is diagnostics-first: no verdict, tier, status, or confidence change may be caused by ledger data.

## Context

WP-A3 built the action-level retrieval attempt ledger (`src/investigation_agent_platform/application/evidence/retrieval_ledger.py`): frozen `RetrievalOutcome` vocabulary (`ROWS_RETURNED`, `PARTIAL_ROWS`, `SCOPED_EMPTY`, `UNSCOPED_EMPTY`, `SEARCH_FAILED`), ID-based novelty, empty-to-rows lift, recorded-not-enforced per-query attempt bound, ordered partial signals, and a scope-aware query extractor. One fail-open wiring point attaches the attempt dict to `ActionOutcome.retrieval_attempt`. Seventeen gate tests pin the vocabulary, the mixed-signal precedence, novelty/lift/bound semantics, and live `QUERY_STATE` wiring.

What G3 still lacks, per the traceability matrix and the gate definition ("query/attempt ledger + novelty + scoped-empty tests show benefit or defensible diagnostic gain; else keep telemetry, no repair-by-assumption"): the ledger is recorded but never consumed. Live-stack diagnostic benefit is unproven, and gateway `total_count`-vs-banked divergence remains telemetry-only. Attempts travel next to the evidence IDs they describe, but nothing joins a claim to the attempts behind its citations — so evaporated citations, failed searches, and truncated runs stay unexplained at verify time.

No `docs/dev/architecture.md` exists in the project. `docs/archive/dev/architecture.md` is archived system context (authority-vs-projection map, Temporal execution authority, Postgres system of record) and is not changed by this diagnostics-plus-test-harness phase.

## References

- [`docs/IAP-proof-kernel-plan-v1.0.md`](IAP-proof-kernel-plan-v1.0.md) — WP-A3 definition and the G3 gate with its keep-telemetry fail action.
- [`docs/IAP-proof-kernel-traceability-v1.0.md`](IAP-proof-kernel-traceability-v1.0.md) — G3 PARTIAL row and residuals.
- [`docs/IAP-QUERY-TO-VERIFY-IMPROVED-DESIGN.md`](IAP-QUERY-TO-VERIFY-IMPROVED-DESIGN.md) — §D2 per-claim retrieval ledger sketch this design adapts to the WP-A3 vocabulary; the no-new-tables posture is kept (persisted `VERIFY_EVALUATION` rows in the existing `timeline_events` table — recomputed at the verify tail, read back by the battery, never recomputed from scratch downstream).
- [`docs/IAP-QUERY-TO-VERIFY-IMPROVED-DESIGN-luna.md`](IAP-QUERY-TO-VERIFY-IMPROVED-DESIGN-luna.md) — Luna G3 row: per-claim retrieval measurement with sample/uncertainty, repair only on measured justification.
- [`src/investigation_agent_platform/application/evidence/retrieval_ledger.py`](../../src/investigation_agent_platform/application/evidence/retrieval_ledger.py) — the WP-A3 module this design extends without changing existing behavior.
- [`src/investigation_agent_platform/domain/hypothesis/models.py`](../../src/investigation_agent_platform/domain/hypothesis/models.py) — `Hypothesis` (statement, supporting/refuting IDs, assessments carrying `claim_kind`/`claim_target`), the claim side of the join.
- [`scripts/agent_metrics.py`](../../scripts/agent_metrics.py) — read-Postgres-no-writes battery pattern the D5 harness follows.

## Goals / Non-Goals

**Goals:**

- Per-claim retrieval binding: every checkable claim resolves its cited IDs against its claim-local retrieved set first, the investigation bank second, recording claim-local / bank-fallback / unknown partitions in verify-time diagnostics.
- Bounded re-retrieval directives: one bounded re-query per unsatisfied claim (failed search under budget, or unknown citations with budget left), issued as an explicit directive through the existing obligation path, recorded by the existing ledger so lift-to-recovery is measurable.
- Provider-total exposure: attach provider `total_count` and banked counts to attempts so truncation is joinable to claims; flag empty-after-truncation in claim diagnostics without making any proof claim.
- Benefit battery with a pass bar: a read-only script measuring attempt coverage, evaporation attribution, lift-to-recovery chains, and truncation visibility over live runs, with sample sizes reported; G3 passes on demonstrated recovery or defensible diagnostic gain, else telemetry stays.

**Non-Goals:**

- Bound enforcement, blocking, or auto-retry loops — a later stage behind its own explicit policy, not this design.
- Any verdict, tier, status, confidence, or explanation change caused by ledger data (repair-by-assumption stays forbidden).
- Verdict-level demotion on evaporation (diagnostics record evaporations; demotion of conclusions is a separate decision).
- Verbatim/range text grounding of citations (separate work; ID-level binding only here).
- New database tables or migrations — the ledger rides existing JSON diagnostic fields.
- New environment-switched behavior — policy parameters are explicit arguments recorded on the records they govern, per the version-pinned-behavior rule.

## Decisions

### D1: Per-claim retrieval ledger, adapted to the WP-A3 vocabulary

**Decision:** Add a pure per-claim ledger layer to `application/evidence/retrieval_ledger.py` (same module as WP-A3, not the `application/investigation/` path sketched in the older design, keeping one ledger vocabulary). A claim key is the normalized statement plus claim kind; attempts link to claims by banked-ID overlap (primary) or derived-query equality (secondary), both recorded. Citation partitions are computed per claim with an explicit `unmapped` fallback for legacy rows that carry no attempts.

```python
# src/investigation_agent_platform/application/evidence/retrieval_ledger.py
"""... (existing module docstring unchanged) ..."""
from __future__ import annotations

import re

#: Caps for per-claim structures (diagnostics stay bounded).
MAX_CLAIM_ATTEMPTS = 10
MAX_PARTITION_IDS = 50


def claim_key(statement: str, claim_kind: str | None) -> str:
    """Deterministic claim key: normalized statement plus claim kind.

    Normalization is strip plus internal-whitespace collapse, case-sensitive;
    the kind suffix is the ClaimKind value or "UNTYPED" for legacy rows.
    """
    collapsed = re.sub(r"\s+", " ", str(statement or "").strip())
    kind = str(claim_kind or "").strip() or "UNTYPED"
    return f"{collapsed[:512]}|{kind[:32]}"


def claim_ref(claim_key_value: str) -> str:
    """Bounded claim reference for length-capped fields.

    16 hex chars of sha256 over the full claim key: fits
    ``EvaporatedId.cited_by`` (max 64) and any other capped slot with
    room to spare. The full ``claim_key`` stays the human-readable
    identity inside ledger records; ``claim_ref`` is the only form that
    enters capped fields. Deterministic for identical inputs.
    """
    import hashlib

    return hashlib.sha256(str(claim_key_value or "").encode("utf-8")).hexdigest()[:16]


class ClaimRetrievalLedger(BaseModel):
    """Retrieval history for one claim: attempts behind its citations."""

    model_config = ConfigDict(frozen=True)

    claim_key: str = Field(description="claim_key() for the owning claim.")
    search_query: str = Field(default="", max_length=1024)
    scope: str | None = Field(
        default=None,
        max_length=512,
        description="Normalized retrieval scope paired with search_query.",
    )
    checkable: bool = Field(
        default=True,
        description="False for unfalsifiable claims, which never dispatch retrieval.",
    )
    attempt_indexes: list[int] = Field(
        default_factory=list,
        description="Positions into the investigation attempt list, oldest first.",
    )
    link_source: str = Field(
        default="id_overlap",
        description="How attempts were linked: id_overlap or query_match.",
    )
    target_ref: str = Field(
        default="",
        max_length=32,
        description="Target fingerprint pairing with claim_key as the unique "
        "per-binding join identity (claim_key alone collides across "
        "target-distinct claims).",
    )


class ClaimRetrievalBinding(BaseModel):
    """Partition of one claim's cited IDs against what was retrieved.

    Lists are capped at ``MAX_PARTITION_IDS`` entries; the ``omitted_*``
    counters record exactly how many IDs were dropped per list so bounded
    diagnostics never silently omit citations. The full (uncapped) unknown
    list drives evaporation up to the diagnostics cap; leftovers are
    counted in ``omitted_unknown``.
    """

    model_config = ConfigDict(frozen=True)

    claim_key: str = Field(description="Full human-readable claim identity.")
    claim_ref: str = Field(
        description="16-hex bounded reference; the only claim form that "
        "enters length-capped fields such as EvaporatedId.cited_by."
    )
    claim_local: list[str] = Field(
        default_factory=list,
        description="Cited IDs retrieved for this claim (attempt-linked).",
    )
    bank_fallback: list[str] = Field(
        default_factory=list,
        description="Cited IDs found only in the investigation bank.",
    )
    unknown: list[str] = Field(
        default_factory=list,
        description="Cited IDs in neither set: evaporated, never silent.",
    )
    omitted_local: int = Field(default=0, ge=0)
    omitted_bank: int = Field(default=0, ge=0)
    omitted_unknown: int = Field(default=0, ge=0)
    unmapped: bool = Field(
        default=False,
        description="True when the claim has no linked attempts (legacy rows).",
    )
    bounded_search: bool = Field(
        default=False,
        description="True when the claim's only basis is a scoped empty that "
        "followed a truncated search of the same scope (diagnostic flag, "
        "never proof).",
    )
    target_ref: str = Field(
        default="",
        max_length=32,
        description="Target fingerprint pairing with claim_key as the unique "
        "per-binding join identity (see ClaimRetrievalLedger).",
    )


def partition_citations(
    cited_ids: list[str],
    local_ids: set[str],
    bank_ids: set[str],
) -> tuple[list[str], list[str], list[str]]:
    """Split cited IDs into claim-local, bank-fallback, and unknown sets.

    Deterministic sorted output. Unknown IDs are returned (never dropped)
    so the caller records them as evaporated with an explicit reason.
    """
    cited = [str(i or "") for i in (cited_ids or []) if str(i or "")]
    local = {str(i or "") for i in (local_ids or set()) if str(i or "")}
    bank = {str(i or "") for i in (bank_ids or set()) if str(i or "")}
    claim_local = sorted(set(cited) & local)
    bank_fallback = sorted((set(cited) - local) & bank)
    unknown = sorted(set(cited) - local - bank)
    return claim_local, bank_fallback, unknown


def derive_claim_query(
    statement: str,
    claim_kind: str | None,
    claim_target: dict[str, object] | None,
) -> tuple[str, str | None]:
    """Best-effort (query, scope) for a claim, mirroring extract_search_query.

    Uses the claim target's symbol/file_path/scope with the same field
    priority as action parameters; returns ("", None) when nothing is
    derivable rather than guessing. Uncheckable statements (taste,
    preference, value judgement with no code anchor) yield ("", None)
    and set checkable=False downstream.
    """
    target = claim_target if isinstance(claim_target, dict) else {}
    query = ""
    for key in ("symbol", "file_path", "counterpart", "commit_id"):
        try:
            candidate = str(target.get(key) or "").strip()
        except Exception:
            continue
        if candidate:
            query = candidate[:1024]
            break
    if not query:
        try:
            query = str(statement or "").strip()[:64]
        except Exception:
            query = ""
    scope: str | None = None
    try:
        explicit = str(target.get("scope") or "").strip()
        if explicit:
            scope = explicit[:512]
    except Exception:
        scope = None
    return query, scope
```

**Alternative considered:** A new `application/investigation/retrieval_ledger.py` module per the older sketch. Rejected — two ledger modules with near-identical names already caused confusion (the harness `AttemptLedger` precedent); one vocabulary lives in one module.

**Rationale:** The claim, not the action, is the unit the verifier judges. Joining attempts to claims makes every later diagnostic (evaporation, lift, truncation) attributable to the claim it affects.

**Trade-offs:** Query-match linking is heuristic where ID overlap is absent; both link sources are recorded so the battery can report their mix, and `unmapped` keeps legacy rows honest instead of forcing a link.

---

### D2: Verify-time claim-local resolution, diagnostics only

**Decision:** At the existing verification finalization site (`application/investigation/verification.py`, where `evaporated`/`demotions`/`missing` are assembled into `VerificationDiagnostics`), build one `ClaimRetrievalBinding` per distinct typed claim from its cited IDs, the investigation bank, and recorded attempts; attach the list as a new `claim_bindings` field on `VerificationDiagnostics` (default empty). One binding per (hypothesis, claim-kind, claim-target) triple — not per hypothesis — because a hypothesis may carry assessments for distinct typed claims, and merging them would misattribute citations. The bounded `claim_ref` (16 hex chars) is the only claim form that enters length-capped fields such as `EvaporatedId.cited_by` (max 64); the full `claim_key` never enters a capped field. Unknown citations are additionally appended as `EvaporatedId` rows (existing mechanism, `cited_by=claim_ref`). No status, tier, confidence, explanation, or corroborating ID may change as a result — enforced by a guardrail test, not just a comment.

Concrete data path (no new tables, no new migrations; source-level persistence surfaces are present in tree, while deployed RLS coverage is a required pre-write attestation):

| Step | Source (exists today) | Consumer |
|---|---|---|
| Attempt recorded | Finalizer builds the attempt dict into `ActionOutcome.retrieval_attempt` (`activities.py` finalization) | — |
| Attempt persisted | NEW fail-open write beside the finalizer: one `TimelineEvent` per recorded attempt into the existing `timeline_events` table (`event_type=RETRIEVAL_ATTEMPT`, new enum member — additive only; `attributes` carries the attempt dict + `evidence_ids[:25]` + `truncated_rows` + counts + `action_execution_id` (the `action_executions.action_id` of the dispatch — the per-action coverage join key; rows predating the key carry none and count as missing), all JSON-safe strings; `evidence_ids` column left empty since ledger IDs are strings, not UUIDs). The ORM model, migration, tenant RLS policy, `append` (fail-open at the call site via try/except — the repository itself raises and logs), and `find_by_investigation_id` read all exist; only the call is new | Battery + verify-time join read it back |
| Evaluation persisted | NEW fail-open write in the verify activity: one `TimelineEvent` per verify turn into the existing `timeline_events` table (`event_type=VERIFY_EVALUATION`, additive enum member — 18 chars fit `String(64)`; `attributes` carries the turn, hypothesis id, verifier version, the recomputed `claim_bindings`, their `claim_ledgers` paired by the `(claim_key, target_ref)` identity (`target_ref` keeps target-distinct claims with the same statement+kind separate), and `missing_obligations` as claim identity only. Every current-version turn persists a row, even with empty bindings/ledgers/missing — a missing row is a reported persistence failure, never an unevaluated turn). The diagnostics are recomputed at the tail with the same timeline-backed join the directive producer used, so every producing path (general, fast-path validators, blocked) emits the same snapshot shape; legacy-pinned runs emit nothing. No new table, no migration, no RLS change | Battery measures changed bindings + missing-obligation events from persisted state |
| Claim cited IDs | `Hypothesis.supporting/refuting_evidence_ids` (already loaded by verify) | D2 hook |
| Bank IDs | `evidence_items` already loaded by `verify_root_cause_activity` | D2 hook |
| Verify inputs | New optional `VerifyRootCauseInput.recent_actions` (default `[]`; legacy callers unaffected) carries the workflow's recent action summaries; extended workflow summary (below) preserves `retrieval_attempt`, `evidence_ids[:25]`, `truncated_rows` for same-turn actions not yet flushed to the timeline table | D2 hook prefers the timeline read and falls back to these summaries per action |
| Diagnostics out | `VerificationDiagnostics.claim_bindings` (new additive field) travels in the existing verify result data dict; no verdict field changes | Battery reads bindings back from persisted `VERIFY_EVALUATION` rows (recomputed once at the verify tail, never recomputed downstream); workflow reads the directive key (D3) |

The `InvestigationToolExecutionORM` table is deliberately NOT used: the model is ORM-declared only, with no migration, no repository, and no established RLS coverage — an ORM declaration is not proof of a persistence path. The `action_executions` table is migrated and RLS-covered but carries no payload column, so it serves only as the battery's action census (denominator), never as the attempt store.

The workflow `action_history` summary update (the `action_history[-1].update({...})` site in `workflows.py`) gains `retrieval_attempt` (dict or None), `evidence_ids` (capped at 25), `truncated_rows` (int, default 0), `provider_total_count`/`banked_count` — all JSON scalars, preserving Temporal replay safety. This covers the forcing-rule budget re-check at cascade time (D3) and same-turn actions missing from the timeline read.

```python
# src/investigation_agent_platform/application/evidence/retrieval_ledger.py
def build_claim_ledger(
    statement: str,
    claim_kind: str | None,
    claim_target: dict[str, object] | None,
    cited_ids: list[str],
    bank_ids: set[str],
    attempts: list[tuple[list[str], dict[str, object] | None]],
) -> tuple[ClaimRetrievalLedger, ClaimRetrievalBinding, list[str]]:
    """Join one claim to the investigation's recorded attempts (pure).

    ``attempts`` is the ordered list of (banked evidence_ids,
    retrieval_attempt dict-or-None) pairs from recorded timeline events
    (plus same-turn workflow summaries). Linking is by banked-ID overlap
    first, derived-query equality second. Returns (ledger, binding,
    full_unknown): the binding carries capped diagnostics-visible lists
    plus exact omitted counts; ``full_unknown`` is the uncapped unknown
    list that drives evaporation up to the diagnostics cap.
    """
    key = claim_key(statement, claim_kind)
    ref = claim_ref(key)
    query, scope = derive_claim_query(statement, claim_kind, claim_target)
    cited = [str(i or "") for i in (cited_ids or []) if str(i or "")]
    linked: list[int] = []
    local_ids: set[str] = set()
    link_source = "id_overlap"
    for index, (banked, attempt) in enumerate(attempts or []):
        banked_set = {str(i or "") for i in (banked or []) if str(i or "")}
        overlap = set(cited) & banked_set
        if overlap:
            linked.append(index)
            local_ids |= overlap
            continue
        if isinstance(attempt, dict) and query:
            if str(attempt.get("search_query") or "") == query:
                linked.append(index)
                link_source = "query_match"
    linked = linked[:MAX_CLAIM_ATTEMPTS]
    bank_set = {str(i or "") for i in (bank_ids or set()) if str(i or "")}
    if not linked:
        bank_fallback = sorted(set(cited) & bank_set)
        unknown = sorted(set(cited) - bank_set)
        return (
            ClaimRetrievalLedger(
                claim_key=key, search_query=query, scope=scope, checkable=bool(query),
                attempt_indexes=[], link_source="id_overlap",
            ),
            ClaimRetrievalBinding(
                claim_key=key, claim_ref=ref,
                claim_local=[],
                bank_fallback=bank_fallback[:MAX_PARTITION_IDS],
                unknown=unknown[:MAX_PARTITION_IDS],
                omitted_bank=max(0, len(bank_fallback) - MAX_PARTITION_IDS),
                omitted_unknown=max(0, len(unknown) - MAX_PARTITION_IDS),
                unmapped=True,
            ),
            unknown,
        )
    claim_local, bank_fallback, unknown = partition_citations(cited, local_ids, bank_ids)
    return (
        ClaimRetrievalLedger(
            claim_key=key, search_query=query, scope=scope, checkable=bool(query),
            attempt_indexes=linked, link_source=link_source,
        ),
        ClaimRetrievalBinding(
            claim_key=key, claim_ref=ref,
            claim_local=claim_local[:MAX_PARTITION_IDS],
            bank_fallback=bank_fallback[:MAX_PARTITION_IDS],
            unknown=unknown[:MAX_PARTITION_IDS],
            omitted_local=max(0, len(claim_local) - MAX_PARTITION_IDS),
            omitted_bank=max(0, len(bank_fallback) - MAX_PARTITION_IDS),
            omitted_unknown=max(0, len(unknown) - MAX_PARTITION_IDS),
        ),
        unknown,
    )


def _distinct_typed_claims(hypothesis: object) -> list[dict[str, object]]:
    """Group one hypothesis's citations by distinct typed claim.

    One output per (claim_kind value or UNTYPED, target fingerprint or
    NO_TARGET) group, where the fingerprint is sha256 of the canonical
    target JSON truncated to 16 hex chars. Assessments sharing kind+target
    union their cited IDs into one claim; a hypothesis with assessments
    for different kinds (or different targets) yields one group each, so
    distinct claims never share a binding. Each group dict carries
    statement, kind_value, target_dict, and cited_ids.
    """
```

Verification hook (exact anchors — the `collect_verification_diagnostics` assembly and the `VerificationDiagnostics(...)` construction in `application/investigation/verification.py`; attempt inputs come from the timeline read (existing `find_by_investigation_id` filtered to `RETRIEVAL_ATTEMPT` in code), falling back to the extended `recent_actions` passed into verify for same-turn actions not yet flushed):

```python
# application/investigation/verification.py, at the existing assembly site:
# attempts: ordered list of (banked evidence_ids, retrieval_attempt dict)
#   loaded via the timeline read (find_by_investigation_id filtered to
#   RETRIEVAL_ATTEMPT events in code), merged with
#   any same-turn pairs from the extended recent_actions input.
# One binding per DISTINCT typed claim: group the hypothesis's cited IDs
# by (claim_kind value or UNTYPED, target fingerprint or NO_TARGET) where
# the target fingerprint is sha256(canonical target JSON)[:16]; cited IDs
# on assessments sharing kind+target are unioned into one claim.
claim_bindings: list[ClaimRetrievalBinding] = []
for claim_group in _distinct_typed_claims(hypothesis):
    _, binding, full_unknown = build_claim_ledger(
        statement=claim_group.statement,
        claim_kind=claim_group.kind_value,
        claim_target=claim_group.target_dict,
        cited_ids=claim_group.cited_ids,
        bank_ids=set(bank_evidence_ids),
        attempts=recorded_attempts,
    )
    claim_bindings.append(binding)
    remaining_budget = 100 - len(evaporated)
    for unknown_id in full_unknown[:remaining_budget]:
        evaporated.append(EvaporatedId(cited_id=unknown_id, cited_by=binding.claim_ref))
# ... existing VerificationDiagnostics(...) call gains:
#     claim_bindings=claim_bindings[:100],
#     claim_ledgers=claim_ledgers[:100],
```

`build_claim_ledger` returns `(ledger, binding, full_unknown)`: the binding carries capped diagnostics-visible lists plus exact omitted counts, and `full_unknown` is the uncapped unknown list that drives evaporation up to the diagnostics cap. `cited_by` carries only `claim_ref` (16 chars, fits the 64-char bound with room to spare); the full `claim_key` never enters a capped field.

`VerificationDiagnostics` gains two fields (additive, default empty — legacy rows validate unchanged):

```python
# application/investigation/verification.py
class VerificationDiagnostics(BaseModel):
    # ... existing fields unchanged ...
    claim_bindings: list[ClaimRetrievalBinding] = Field(default_factory=list, max_length=100)
    # The ledgers behind claim_bindings, paired by claim_key (same order,
    # same cap). Persisted so the battery measures changed bindings from
    # recorded state instead of recomputing the join.
    claim_ledgers: list[ClaimRetrievalLedger] = Field(default_factory=list, max_length=100)
```

**Alternative considered:** Resolving citations against claim-local sets inside the verdict path (demoting conclusions with evaporated citations). Rejected for G3 — that couples diagnostics to verdicts without a measured justification, exactly the repair-by-assumption the gate forbids. Demotion stays a separate decision.

**Rationale:** No new tables: bindings ride the existing diagnostics payload and the full ledger is recomputed from recorded attempts at verify time, then persisted as `VERIFY_EVALUATION` rows — the battery reads those rows back and never recomputes the join itself. Attempt records themselves persist (new write path, existing table) because the join and the battery need a durable source. Legacy rows degrade to `unmapped`, never fail.

**Trade-offs:** Diagnostics grow per hypothesis (bounded by existing caps: 100 bindings max, 50 IDs each). The verdict path is untouched, so G3 cannot fix verification — only explain it.

---

### D3: Bounded re-retrieval directives, single retry, explicit budget

**Decision:** One bounded re-query per unsatisfied claim, issued as a typed machine directive that travels verify-result → workflow history → forcing rule → dispatched action, with consumed-marking that holds exactly once. The directive carries the normalized `(query, scope)` key end to end; scope is never reconstructed from action parameters after issue time. The directive is never prose in `MissingObligation.next_action` (which stays human-readable text and is not executed by the dispatcher); `reason_activity`'s `{next_action, action_parameters}` contract is unchanged — the forcing rule overrides those fields post-reason, following the existing override pattern, so no contract change is required.

Directive lifecycle (normative state machine; workflow-level tests pin every transition):

1. **Issue (verify time).** `collect_verification_diagnostics` (or its verify-activity caller) computes `re_retrieval_directive(...)` per unsatisfied claim from recorded attempts (timeline read via `recorded_retrieval_attempts`, history-summary fallback for same-turn actions) and places the typed dict in the verify result data under `ledger_requery`. The directive query and scope come from the claim ledger's normalized `(search_query, scope)` pair. Triggers are mutually explicit: (a) the claim's latest linked attempt is `SEARCH_FAILED` and the durable count for that normalized key is below budget; or (b) the claim binding has unknown citations and the durable count for that normalized key is zero. An unknown-citation binding with a prior attempt does not independently trigger another directive. The attempt index is derived at verify time as the count of persisted `RETRIEVAL_ATTEMPT` payloads for the normalized `(query, scope)` key, oldest first; the process-local recorder `attempt_index` is telemetry only and is never used for eligibility or budget enforcement. Each persisted event carries a deterministic `attempt_id` derived from the investigation, loop iteration, action type, normalized query, and normalized scope; same-turn summaries carry the same ID before persistence. Same-turn summaries are included only when no persisted row with that `attempt_id` exists, and duplicate same-turn/persisted pairs are de-duplicated by that ID before counting. This durable count holds across workers and replays.
2. **Carry (workflow — exact creation site).** Immediately after `verify_res` returns in the `workflows.py` loop (after the `verify_root_cause_activity` call at the current `iteration`, before the next loop turn's `reason_activity` call), the workflow checks `verify_res.data.get("ledger_requery")`. If it is a dict with a non-empty query, a valid action type, and a normalized scope field, the workflow appends ONE new `action_history` entry (never merges into `action_history[-1]`, which the post-execution update owns): the marker below with `iteration` = current `iteration` (int, the loop's own turn counter), `turn_issued` = same int, `scope` = `_normalize_scope(directive.get("scope"))`, and `consumed: False`. `turn_issued` numbering is the loop `iteration` — the same key every `action_history` entry carries — so "strictly later" means `entry["iteration"] > marker["turn_issued"]` with no clock. The marker rides the existing `action_history` list (Temporal workflow locals, deterministic under replay); the next turn's `reason_activity` input (`action_history[-10:]`) carries it with no new transport. If `action_history[-10:]` truncation ever drops an unconsumed marker, the directive is treated as expired (never re-created from the truncated view; a fresh evaluation may issue a new one only if the recorded budget allows).
3. **Convert (next cascade — exact insertion point).** New `_should_force_ledger_requery(action_history, recorded_summaries)` runs in the post-reason cascade immediately after the `_should_force_walk_stack` block (`workflows.py` D2 block) and before the floor/formulate/synthesis/reread/callsite/log-search overrides. It reads the FULL `action_history` locals (never the truncated `recent_actions` view), plus the same-turn summaries, so a consumed stamp cannot be hidden by truncation. Stack-walk has strict precedence: when `_should_force_walk_stack(...)` returns parameters and sets `forced_walk_stack`, ledger forcing is not evaluated and cannot replace that action. Otherwise it fires only when ALL hold: an unconsumed marker exists (`kind == "ledger_requery_directive"`, `consumed` false, and `directive_consumed(marker, action_history, recorded)` is false); the directive validates (action_type in `RETRYABLE_ACTIONS`, parameters a JSON-scalar mapping, non-empty query — otherwise it is ignored with a loud log and the marker is marked consumed-as-invalid in place); and the budget still holds, recomputed at cascade time from recorded state (count of persisted attempts with the same normalized query+scope key < max_attempts). On fire, the workflow overrides `_reason_data["next_action"]`/`_reason_data["action_parameters"]` with the directive's typed action and params, sets `_reason_data["forced_requery"] = True` (same flag family as `forced_walk_stack`), and stamps the dispatched turn's history entry at post-execution update time with `forced_requery: {claim_ref, query, scope, turn, outcome_status}` (see schema below) while marking the marker `consumed: True` in place. Every later forcing block (floor/formulate/synthesis/reread/callsite/log-search/replan) must also stand down when `forced_requery` is set. On no-fire, the turn proceeds exactly as today.

```python
# workflows.py loop, immediately after verify_res (current iteration):
directive = (verify_res.data or {}).get("ledger_requery")
if isinstance(directive, dict) and str(directive.get("query") or "").strip():
    action_history.append({
        "kind": "ledger_requery_directive",
        "iteration": iteration,
        "claim_ref": str(directive.get("claim_ref") or ""),
        "query": str(directive.get("query") or "").strip(),
        "scope": _normalize_scope(directive.get("scope")),
        "action_type": str(directive.get("action_type") or "").upper(),
        "action_parameters": dict(directive.get("action_parameters") or {}),
        "attempt_index": int(directive.get("attempt_index", 0)),
        "max_attempts": int(directive.get("max_attempts", WP_A3_DEFAULT_MAX_ATTEMPTS)),
        "turn_issued": iteration,
        "consumed": False,
    })

# workflows.py post-reason cascade, after the _should_force_walk_stack block:
# Stack-walk is higher priority and blocks this helper on the same turn.
ledger_override = None
if not _reason_data.get("forced_walk_stack"):
    ledger_override = _should_force_ledger_requery(action_history, recorded_summaries)
if ledger_override is not None:
    _reason_data = dict(_reason_data)
    _reason_data["next_action"] = ledger_override["action_type"]
    _reason_data["action_parameters"] = ledger_override["action_parameters"]
    _reason_data["forced_requery"] = True
```

4. **Hold (subsequent turns).** A consumed marker never fires again (`directive_consumed` true forever once the stamp exists or the budget is reached). A fresh evaluation that still finds the claim unsatisfied issues a *new* directive only if the durable count of persisted attempts for the normalized `(query, scope)` key is below `max_attempts` — the single-retry-per-evaluation rule. The retired marker plus the new recorded attempt make "exactly once per evaluation, bounded overall" auditable from history alone. Execution failure does NOT reopen the marker: a failed dispatch still carries the `forced_requery` stamp (with its `outcome_status`, e.g. `FAILED`/`DEGRADED`), so the marker stays consumed; recovery waits for a fresh evaluation and a fresh budget check, never a tight retry loop.

Priority rationale: intake frames (stack-walk) outrank everything and suppress ledger forcing on the same cascade; a ledger requery then outranks the remaining gathering overrides; value-target and other overrides keep their relative order otherwise, but all later overrides stand down once `forced_requery` is set. The forcing helper reads only workflow history (Temporal-durable, replay-safe) plus the directive marker — never the process-local ledger, never mutable singletons.

`re_retrieval_directive` returns the typed dict below (never prose). Validation is all-or-nothing: any non-scalar parameter value, unknown action type, or empty query returns None for the whole directive — fields are never silently dropped to "fix" it into dispatchability.

```python
# src/investigation_agent_platform/application/evidence/retrieval_ledger.py
RETRYABLE_ACTIONS = frozenset(
    {"SEARCH_CODE", "FIND_SYMBOL", "FIND_CALL_GRAPH", "GET_CODE_HISTORY"}
)
"""Action types the ledger directive may re-issue: exactly the set
extract_search_query() understands. Anything else is rejected."""

# Normative marker + dispatch-stamp schema (single source of truth; plain JSON scalars only).
# Marker (one action_history entry, appended at verify time):
# {"kind": "ledger_requery_directive", "iteration": int, "claim_ref": str,
#  "query": str (stripped, non-empty), "scope": str (normalized; None -> ""),
#  "action_type": str (uppercased member of RETRYABLE_ACTIONS),
#  "action_parameters": dict[str, scalar], "attempt_index": int,
#  "max_attempts": int, "turn_issued": int (== iteration at issue),
#  "consumed": bool}
# Dispatch stamp (merged into the dispatched turn's action_history entry at the
# existing post-execution update site, alongside forced_walk_stack etc.):
# {"forced_requery": {"claim_ref": str, "query": str, "scope": str,
#  "turn": int (dispatch iteration), "outcome_status": str | None}}
# Invariants: turn_issued == marker iteration; stamp.turn > marker.turn_issued;
# query/scope compared normalized (strip; scope None -> ""); consumed flips
# False -> True exactly once (on dispatch, on budget-exhaustion, or on
# consumed-as-invalid for a malformed directive with a loud log).


def _is_scalar(value: object) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def re_retrieval_directive(
    binding: "ClaimRetrievalBinding",
    ledger: "ClaimRetrievalLedger",
    latest_outcome: str | None,
    recorded_attempts: list[tuple[list[str], dict[str, object] | None]],
    action_type: str,
    parameters: dict[str, object] | None,
    max_attempts: int = WP_A3_DEFAULT_MAX_ATTEMPTS,
) -> dict[str, object] | None:
    """Bounded re-query directive for one unsatisfied claim, or None.

    All-or-nothing validation: unknown action type, non-mapping
    parameters, ANY non-scalar parameter value, or empty query returns
    None for the whole directive. Fields are never silently dropped to
    make a directive dispatchable; a malformed directive is a refusal,
    not a repair.
    """
    try:
        budget = max(1, int(max_attempts))
    except (TypeError, ValueError):
        budget = WP_A3_DEFAULT_MAX_ATTEMPTS
    query = str(ledger.search_query or "").strip()
    scope = _normalize_scope(ledger.scope)
    attempt_index = count_persisted_attempts(recorded_attempts, query, scope)
    if attempt_index >= budget:
        return None
    if not query:
        return None
    if str(action_type or "").upper() not in RETRYABLE_ACTIONS:
        return None
    if not isinstance(parameters, dict):
        return None
    if not all(isinstance(k, str) and _is_scalar(v) for k, v in parameters.items()):
        return None
    needs_retry = (attempt_index == 0 and bool(binding.unknown)) or (
        str(latest_outcome or "") == RetrievalOutcome.SEARCH_FAILED.value
    )
    if not needs_retry:
        return None
    return {
        "action": "RETRIEVE",
        "action_type": str(action_type).upper(),
        "action_parameters": {str(k): v for k, v in parameters.items()},
        "query": query,
        "scope": scope,
        "claim_ref": binding.claim_ref,
        "attempt_index": attempt_index,
        "max_attempts": budget,
    }


def _normalize_scope(value: object) -> str:
    """Scope normalization shared by marker, stamp, and budget count."""
    return str(value or "").strip()


def count_persisted_attempts(
    recorded_attempts: list[tuple[list[str], dict[str, object] | None]],
    query: str,
    scope: object,
) -> int:
    """Count durable attempts for one normalized query/scope key.

    The count is authoritative for directive eligibility and budget. The
    process-local ``RetrievalAttempt.attempt_index`` is not authoritative
    because worker restarts can reset that in-memory counter.
    """
    normalized_query = str(query or "").strip()
    normalized_scope = _normalize_scope(scope)
    return sum(
        1
        for _banked, attempt in recorded_attempts or []
        if isinstance(attempt, dict)
        and str(attempt.get("search_query") or "").strip() == normalized_query
        and _normalize_scope(attempt.get("scope")) == normalized_scope
    )


def directive_consumed(
    marker: dict[str, object],
    history_entries: list[dict[str, object]],
    recorded_attempts: list[tuple[list[str], dict[str, object] | None]],
) -> bool:
    """True only when the directive's requery already happened or budgeted out.

    A marker counts as consumed ONLY when (a) a strictly-later history entry
    (`entry["iteration"] > marker["turn_issued"]`) carries a matching
    `forced_requery` dispatch stamp (same claim_ref, same normalized query,
    same normalized scope) — REGARDLESS of the stamped outcome_status
    (FAILED/DEGRADED still counts: a failed dispatch does not reopen the
    marker, bounding retries to one per evaluation); or (b) recorded attempts
    for the same normalized (query, scope) key have reached max_attempts.
    The marker's own presence never counts — a directive is not consumed by
    being issued. All inputs are recorded state (workflow history, persisted
    attempts), so the verdict is deterministic under replay.
    """
    claim_ref = str(marker.get("claim_ref") or "")
    query = str(marker.get("query") or "").strip()
    scope = _normalize_scope(marker.get("scope"))
    issued_turn = marker.get("turn_issued", -1)
    try:
        issued_turn = int(issued_turn)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        issued_turn = -1
    budget = marker.get("max_attempts", WP_A3_DEFAULT_MAX_ATTEMPTS)
    try:
        budget = max(1, int(budget))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        budget = WP_A3_DEFAULT_MAX_ATTEMPTS
    same_key_attempts = 0
    for banked, attempt in recorded_attempts or []:
        if not isinstance(attempt, dict):
            continue
        if str(attempt.get("search_query") or "").strip() != query:
            continue
        if _normalize_scope(attempt.get("scope")) != scope:
            continue
        same_key_attempts += 1
    if same_key_attempts >= budget:
        return True
    for entry in history_entries or []:
        if not isinstance(entry, dict):
            continue
        try:
            entry_turn = int(entry.get("iteration", entry.get("turn", -1)))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if entry_turn <= issued_turn:
            continue
        stamp = entry.get("forced_requery")
        if not isinstance(stamp, dict):
            continue
        if (
            str(stamp.get("claim_ref") or "") == claim_ref
            and str(stamp.get("query") or "").strip() == query
            and _normalize_scope(stamp.get("scope")) == scope
        ):
            return True
    return False
```

**Alternative considered:** Worker-side auto-retry inside `record_retrieval_attempt` or the finalizer, and routing `MissingObligation.next_action` text straight into the dispatcher. Both rejected — the first breaks the ledger's fail-open diagnostics-only contract and bypasses the obligation path; the second cannot work because the dispatcher requires a typed action plus validated parameters, which prose does not supply, and silently "interpreting" prose into actions would be assumption-by-construction.

**Rationale:** Recovery must be evidence (new rows re-verified), never assumption. A single bounded, explicitly-typed re-issue with consumed-marking is the smallest behavior change that can demonstrate recovery, and the ledger already measures exactly this shape (lift). The marker lives in workflow state (Temporal-durable, replay-safe); the budget is recomputed from recorded attempts at every decision point, so no single store is trusted alone.

**Trade-offs:** One retry per claim per evaluation; repeated failures wait for a new turn. No blocking, no queue, no priority beyond the documented cascade position. A later routing stage may consume the directive directly (out of scope here).

---

### D4: Provider-total exposure plus empty-after-truncation flag

**Decision:** Add two additive optional fields to `RetrievalAttempt` (`provider_total_count`, `banked_count`, default `None` = unknown/legacy — existing rows validate unchanged, replay-safe JSON scalars) populated from the gateway result at the existing finalizer wiring point; mark `bounded_search` on a claim binding whose only basis is a scoped empty that followed a truncated search of the same scope. All diagnostic-only; `total_count` never enters a proof, and `bounded_search` never upgrades or downgrades a verdict.

```python
# src/investigation_agent_platform/application/evidence/retrieval_ledger.py
class RetrievalAttempt(BaseModel):
    # ... existing fields unchanged ...
    provider_total_count: int | None = Field(
        default=None, ge=0,
        description="Provider-reported total before budget/dedup (gateway "
        "total_count); None when the provider reports none (legacy rows).",
    )
    banked_count: int | None = Field(
        default=None, ge=0,
        description="Rows actually banked for this attempt; None for legacy rows.",
    )
```

Population and persistence (existing finalizer site in `application/worker/activities.py`, which already forwards `truncated_rows` and source statuses): pass `provider_total_count` from the gateway `total_count` and `banked_count` from `len(banked)` through `record_retrieval_attempt` (two new optional parameters, default `None`, fail-open preserved). Persist every recorded attempt beside the finalizer as a `TimelineEvent` (`event_type=RETRIEVAL_ATTEMPT`) into the existing `timeline_events` table (persistence contract pinned below; already read in-activities): `attributes` carries the attempt dict plus `evidence_ids[:25]`, `truncated_rows`, and counts (all JSON-safe strings; the UUID-typed `evidence_ids` column stays empty since ledger IDs are strings). The write goes through the existing `SqlAlchemyTimelineRepository.append` inside a fail-open try/except at the call site — never blocks the action. Persistence contract (normative — no migration, no RLS change): `TimelineEventType.RETRIEVAL_ATTEMPT = "RETRIEVAL_ATTEMPT"` is additive only in `domain/timeline/models.py`; `TimelineEventORM.event_type` is `String(64)` so the 17-char value fits with no DDL (code-only deploy, no Alembic revision). `timeline_events` is RLS-covered by `migrations/versions/001_add_rls.py` (`tenant_isolation` + `FORCE ROW LEVEL SECURITY`) and retained in `migrations/versions/3354e515779a_core_retention_classes_and_rls_coverage.py`. The same change makes `timeline_repository._from_orm` tolerant (`try: TimelineEventType(row.event_type) except ValueError: UNKNOWN`) so one new row can never break hydration and rollback stays safe; the first `RETRIEVAL_ATTEMPT` write happens only after that deploy. Verify-time and battery reads use the existing `find_by_investigation_id` plus the pure `recorded_retrieval_attempts(events)` code-side filter (`event_type == RETRIEVAL_ATTEMPT`, oldest first); no new repository method, no new repository class, no migration, no RLS change. The verify activity obtains the read through the same ctx-provided repository pattern it already uses for evidence/hypotheses; any read failure degrades to `unmapped` bindings (never a verdict change, never an exception surfacing). If the write path is unavailable in some deployment, attempts stay workflow-local and the battery reports the coverage gap honestly — degraded measurement, never silent invention. Code-side filter (pure, tested):

```python
def recorded_retrieval_attempts(events: list) -> list[tuple[list[str], dict]]:
    """(banked evidence_ids, attempt dict) for RETRIEVAL_ATTEMPT events only, oldest first."""
    out: list[tuple[list[str], dict]] = []
    for event in events or []:
        event_type = getattr(event, "event_type", None)
        if str(getattr(event_type, "value", event_type)) != "RETRIEVAL_ATTEMPT":
            continue
        attributes = getattr(event, "attributes", None) or {}
        attempt = attributes.get("attempt") if isinstance(attributes, dict) else None
        banked = attributes.get("evidence_ids") if isinstance(attributes, dict) else []
        out.append(
            ([str(i) for i in (banked or []) if str(i)], dict(attempt) if isinstance(attempt, dict) else {})
        )
    return out
```

Claim-side flag, computed in the D2 hook: `bounded_search=True` when the binding's claim-local and bank-fallback lists are empty, the latest linked attempt is `SCOPED_EMPTY`, and a same-scope prior attempt for the claim query carried `truncated_rows > 0` or `provider_total_count > banked_count`.

#### Deployment and rollback constraint

The migration files are repository evidence, not a deployed-schema attestation. `001_add_rls.py` declares `timeline_events` with the `tenant_isolation` policy and `FORCE ROW LEVEL SECURITY`; `3354e515779a_core_retention_classes_and_rls_coverage.py` adds the retention column for that table but does not itself add the RLS policy. Before the first `RETRIEVAL_ATTEMPT` write, deployment must verify the target catalog has the expected `timeline_events.event_type` column/type, RLS enabled and forced, and the tenant-isolation policy. The battery fails closed with `schema_not_verified` when this attestation is absent. This paragraph supersedes any earlier shorthand in this document that described migration-source coverage as proof of deployed coverage.

The tolerant `_from_orm` reader is forward-compatible only. Deploy the reader before enabling producer writes. Once a new event may exist, rollback to a binary that directly constructs `TimelineEventType` is prohibited unless an explicitly approved recovery procedure first blocks new writes and removes or quarantines the new rows. The design does not claim safe rollback to an older reader; any earlier wording that said rollback was safe is superseded.

**Alternative considered:** Promoting provider totals into completeness reasoning (e.g., treating large totals as incompleteness proof). Rejected — provider counts are unaudited estimates; G3 uses them for visibility only.

**Rationale:** The documented limitation ("total_count-vs-banked remains telemetry-only") stays true, but the telemetry becomes joinable: truncation is visible at the claim it affects instead of floating in action logs.

**Trade-offs:** Provider totals vary in meaning across providers (pre-dedup, pre-budget); the design records them verbatim with no normalization and no semantic claim.

---

### D5: Benefit battery with a pass bar

**Decision:** A read-only battery script, `scripts/retrieval_benefit.py`, following the `scripts/agent_metrics.py` pattern (reads Postgres directly with SQLAlchemy model classes for table/column names, never hand-guessed schema; no writes, no schema change), measuring four metrics over trailing live runs plus operator CLI flags `--tenant` and `--days`. G3 passes on demonstrated recovery or defensible diagnostic gain with sample sizes reported; otherwise telemetry stays and no behavior ships — the gate's own fail action.

```python
# scripts/retrieval_benefit.py
"""G3 benefit battery: does the retrieval ledger earn its keep?

Read-only. Follows scripts/agent_metrics.py conventions: SQLAlchemy model
classes for table/column names, no hand-guessed schema, no writes.
"""
from __future__ import annotations

import argparse
import sys

_REPO_ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant", default=None, help="Tenant ID to scope the battery.")
    parser.add_argument("--days", type=int, default=30, help="Trailing window in days.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = run_battery(tenant_id=args.tenant, days=args.days)
    print(format_report(report))
    return 0 if report["pass"] else 1


def load_cohort(tenant_id: str, days: int) -> list[str]:
    """Relevant investigation IDs for the window (denominator source).

    Distinct investigation_ids from action_executions with at least one
    dispatched retrieval-type action in range. Runs with no recorded
    attempt remain in the cohort: coverage divides recorded attempts by
    this set, so missing telemetry lowers the rate instead of vanishing.
    RLS + window (normative): ActionExecutionORM (`action_executions`,
    migrated in 002_add_phase2_tables with tenant_isolation + FORCE RLS
    and idx_action_exec_tenant_inv) filtered by tenant_id and
    created_at >= now - days; action_type in RETRYABLE_ACTIONS. Uses the
    model class for table/column names (never hand-guessed schema).
    """


def run_battery(tenant_id: str, days: int) -> dict:
    """Compute the four G3 metrics over persisted records.

    Tenant scope is mandatory: every read runs inside the application's
    own rls_session (SET LOCAL app.tenant_id) with an explicit tenant
    predicate; unscoped runs are refused, never executed.
    cohort: distinct investigations with >= 1 dispatched retrieval action
      in the window, from the action_executions census (verifiably
      migrated + RLS-covered), each carrying its eligible action_ids.
      Runs WITHOUT any recorded attempt stay in the denominator:
      missing attempts must show up as missing.
    attempts: recorded attempt payloads from timeline_events
      (RETRIEVAL_ATTEMPT events via TimelineEventORM, same tenant + window
      on timestamp, event_type equality in code), each carrying its
      action_execution_id join key. Same-turn in-memory workflow
      summaries are a verify-time convenience only, never a battery
      input — the battery reads persisted rows so every counted
      attempt is durable.
    evaluations: VERIFY_EVALUATION events via TimelineEventORM (same
      tenant + window, event_type equality in code, oldest first),
      each carrying the turn, persisted claim bindings, their ledgers
      paired by claim_key, and missing obligations as claim identity.
      Attribution and recovery read these rows — never a recompute —
      so measured bindings always agree with what verification saw.
    """
    cohort = load_cohort(tenant_id, days)
    attempts, evaluations = load_records(tenant_id, days)
    coverage = attempt_coverage(attempts, cohort)
    attribution = evaporation_attribution(evaluations, attempts)
    recovery = lift_recovery_chains(attempts, evaluations)
    truncation = truncation_visibility(evaluations, attempts)
    verdict_guard = no_ledger_verdict_changes()
    n_runs = len(cohort)
    evaluable_runs = sum(1 for inv in cohort if evaluations.get(inv))
    n_evaporation_events = attribution["events"]
    branch_r = (
        verdict_guard["clean"]
        and coverage["eligible_rate"] == 1.0
        and evaluable_runs >= 5
        and recovery["chains"] >= 3
    )
    branch_d = (
        verdict_guard["clean"]
        and coverage["eligible_rate"] == 1.0
        and evaluable_runs >= 5
        and n_evaporation_events >= 10
        and attribution["rate"] >= 0.80
    )
    inconclusive = evaluable_runs < 5 or (
        not branch_r and not branch_d and n_evaporation_events < 10
    )
    return {
        "n_runs": n_runs,
        "n_attempts": len(attempts),
        "n_claims": len(bindings),
        "n_evaporation_events": n_evaporation_events,
        "coverage": coverage,
        "attribution": attribution,
        "recovery": recovery,
        "truncation": truncation,
        "verdict_guard": verdict_guard,
        "branch_recovery": branch_r,
        "branch_diagnostic": branch_d,
        "inconclusive": inconclusive,
        "pass": bool((branch_r or branch_d) and not inconclusive),
    }
```

Metric definitions (all computed from persisted rows, no new instrumentation): **coverage** — fraction of *eligible* retrieval actions carrying a recorded attempt joined on `action_execution_id` (`action_executions.action_id` equals the marker's `attributes.action_execution_id`), where eligible means dispatched actions whose type is in the retrieval set the ledger instruments, counted from the `action_executions` census (non-retrieval actions never count for or against); bar: 100%. Rows predating the join key carry none and count as missing — missing attempts show up as missing, never vanish. **attribution** — fraction of evaporation events traceable to a concrete recorded signal, measured on the latest `VERIFY_EVALUATION` event per investigation: unknown citations from persisted bindings plus missing-obligation claims from the same event (a hypothesis-level missing obligation expands to one unit per distinct target sharing its `claim_key`, so target-distinct claims never merge). A unit is attributed when its `(claim_key, target_ref)` identity shows a recorded signal *at or before the evaluation*: only earlier attempts count, so a later failure can never explain an earlier evaluation. `query_match` ledgers join by normalized key (D2's basis); `id_overlap` ledgers additionally require the attempt's persisted banked IDs to intersect the binding's cited set (D2's basis; the binding 50-ID caps make this fail-safe). A `bounded_search` flag on the binding signals on its own. Signal-free units are *unexplained*. Bar: ≥ 80% with ≥ 10 events. **recovery** — count of independent end-to-end chains keyed by `(investigation_id, claim_key, target_ref)`: a persisted pre-evaluation binding with empty `claim_local` and nonempty unknown, a strictly later same-key attempt, and a later persisted post-evaluation binding for the same identity whose `claim_local` changed and now contains ≥ 1 novel row durable in the bank. Novelty is proven per row by attempt-listed banked IDs absent from all earlier same-key attempts, or by the row's bank `retrieved_at` falling inside the (pre, post] window while a same-key window attempt returned rows (cap-independent — the 25-ID per-attempt persistence cap cannot hide a chain); only rows the post binding cites count, and incidental durable rows never veto. The changed binding is read from persisted evaluations — observed across turns, not inferred. One chain per investigation; ≥ 3 chains must span ≥ 3 distinct investigations. **truncation visibility** — count of `bounded_search` flags on latest persisted bindings where truncation occurred, counted once per claim on the latest evaluation; when no truncation occurred in the window, truncation is reported as not-applicable (neither pass nor fail contribution). Sample floors apply to *evaluable* runs (investigations with ≥ 1 `VERIFY_EVALUATION` event): branches need ≥ 5 of them. Pre-marker runs contribute to coverage (as misses) but not to attribution or recovery. Uncertainty is reported as raw counts plus sample sizes (runs, evaluable runs, attempts, claims, evaporation events) — no modeled confidence intervals on small samples. The verdict guard is a test, not a battery metric: a differential suite asserting identical statuses/tiers/confidences with ledger fields populated versus stripped.

Pass rule (two explicit branches; prose and pseudocode agree): **Branch R (recovery)** passes when the verdict guard is clean, coverage is 100% of eligible actions, evaluable runs ≥ 5, and ≥ 3 independent recovery chains. **Branch D (diagnostic attribution)** passes when the verdict guard is clean, coverage is 100% of eligible actions, evaluable runs ≥ 5, evaporation events ≥ 10, and attribution ≥ 80%. Below both branches' sample floors the battery reports **inconclusive** and the gate stays PARTIAL — inconclusive is never a pass, and truncation N/A never converts it into one. Either branch still requires explicit owner sign-off on the sample before G3 passes; numbers alone do not close the gate.

**Alternative considered:** Modeled statistics (Wilson intervals, significance tests). Rejected — samples are small and operational; honest counts plus a provisional-sample rule beat false precision.

**Rationale:** The gate demands "demonstrated recovery or defensible diagnostic improvement with sample/uncertainty." Counts, chains, and a guardrail test are auditable by the owner without statistical machinery.

**Trade-offs:** Thresholds (80% attribution, ≥3 recovery chains, ≥5 runs, ≥10 evaporation events) are owner-adjustable defaults proposed here, not derived constants. If live traffic lacks failures or recoveries, the battery reports inconclusive and the gate stays PARTIAL — numbers alone never close G3; either branch still needs explicit owner sign-off on the sample.

---

### D6: No-repair guardrails and failure semantics

**Decision:** The following are forbidden, tested by dedicated negative vectors, and any violation blocks G3 exit: ledger data causing any status/tier/confidence/explanation change; `UNSCOPED_EMPTY` supporting anything; `PARTIAL_ROWS` read as completeness; re-retrieval beyond the single bounded retry without a fresh evaluation; treating `unmapped` legacy rows as failures or as verified-by-default; fabricating versions, codes, queries, or scopes for legacy rows (None/empty propagate as unknown); dispatching a directive that fails validation (unknown action type, non-scalar parameters, empty query) — refused with a loud log, never downgraded to a generic action. Stop conditions from the parent plan apply unchanged: any ledger-attributable `VERIFIED` without valid current derivation, silent drop raising strength, or nondeterministic derivation fails the gate outright.

```python
# tests/unit/test_wp_a3_retrieval_ledger.py (existing suite; G3 vectors appended
# to the new tests/unit/test_g3_retrieval_benefit.py gate file instead — see D7)
def test_no_repair_by_assumption() -> None:
    """Ledger fields must never upgrade an outcome on their own."""
    # Vector shape (implemented in D7): an UNSCOPED_EMPTY attempt plus a
    # PARTIAL_ROWS attempt for the same claim resolve to unknown/bank
    # partitions only; no helper returns DISCHARGED/VALID/VERIFIED.
```

**Alternative considered:** Allowing explicitly-labeled "ledger-supported" strength bumps. Rejected — strength is the verifier's, and the gate's fail action exists precisely to prevent assumption creep.

**Rationale:** A diagnostics system earns trust by refusing to become an oracle. The guardrails are the contract that keeps G3 telemetry honest.

**Trade-offs:** Real recoveries still require the normal verify path to re-run on new rows; the ledger only shortens the loop to the next retrieval, never the proof itself.

---

### D7: Test files and exact commands

**Decision:** G3 vectors live in a new gate file `tests/unit/test_g3_retrieval_benefit.py` (ledger unit vectors for D1/D4 additions incl. `claim_ref` length bound and omitted-count accounting; claim-join goldens for D2 incl. per-distinct-claim splitting and unmapped legacy rows; directive vectors for D3 incl. over-budget refusal, malformed-directive rejection, and consumed-no-repeat; verdict-guard differentials and workflow forcing tests below; guardrail negatives for D6); the existing `tests/unit/test_wp_a3_retrieval_ledger.py` is untouched (frozen Stage A evidence). Workflow forcing coverage (pure, no Temporal run needed): `test_ledger_forcing_emits_typed_action` (unconsumed directive in history → exact `action_type` + validated params, priority position after stack-walk forcing), `test_ledger_verify_result_carried_to_next_cascade` (verify result with `ledger_requery` → marker appended with `turn_issued == iteration` → next-turn cascade fires → dispatched typed action with `forced_requery` stamp at a later `iteration`), `test_ledger_forcing_consumed_no_repeat` (consumed marker → no override), `test_ledger_failed_dispatch_stays_consumed` (FAILED/DEGRADED stamp still consumed — no immediate re-fire), `test_ledger_forcing_budget_exhausted_no_repeat` (recorded same-key attempts at budget → no override), `test_ledger_forcing_rejects_malformed_directive` (unknown action type / non-scalar params / empty query → no dispatch, logged, marker consumed-as-invalid), `test_ledger_directive_never_alters_verdict` (identical inputs with and without directive-bearing diagnostics → identical verification outputs), `test_timeline_unknown_event_type_maps_to_unknown` (new-value row hydrates without raising). The battery script gets smoke coverage from a small fixture-driven test (may fold into the gate file if it stays hermetic — no live DB in unit tests; the script itself runs operator-side against real Postgres).

Component gate command (repo root):

```text
uv run python -m pytest -q -p no:cacheprovider tests/unit/test_wp_a3_retrieval_ledger.py tests/unit/test_g3_retrieval_benefit.py
```

Battery command (operator-side, needs DB env):

```text
set -a && source .env.local && source ~/bin/.iap.env.local && set +a && uv run python scripts/retrieval_benefit.py --tenant TENANT_ID --days 30
```

## Data Storage

No new database table, migration, or external data contract. `ClaimRetrievalBinding` lists ride the existing `VerificationDiagnostics` payload (one additive field, default empty — legacy rows validate unchanged); full per-claim ledgers are recomputed from recorded attempts at verify time and persisted as `VERIFY_EVALUATION` rows (with paired `claim_ledgers`), which the battery reads back. Attempt additions (`provider_total_count`, `banked_count`) are optional JSON scalars on the existing dict. Recorded attempts persist as `TimelineEvent` rows (`event_type=RETRIEVAL_ATTEMPT`) in the existing `timeline_events` table — the repository migration history declares the table's tenant RLS, but deployed RLS must be catalog-attested before writes and battery reads are accepted. The `InvestigationToolExecutionORM` model is deliberately NOT used: it is ORM-declared only, with no migration, no repository, and no established RLS coverage. No new tables, no migrations, no new contracts: the only new persistence is rows in a pre-existing operational table written through a pre-existing repository.

## Data Structures

Input and output schemas are separated throughout. Claim-side inputs are plain mappings (hypothesis fields, outcome dicts, recorded attempt rows); outputs are the frozen Pydantic records above (`ClaimRetrievalLedger`, `ClaimRetrievalBinding` with `claim_ref` + `omitted_*` accounting). `ClaimRetrievalLedger.scope` is the normalized scope paired with `search_query`. `RetrievalAttempt` gains only the two optional count fields from D4 (existing fields frozen, vocabulary untouched). `VerificationDiagnostics` gains only `claim_bindings` (existing fields frozen, diagnostics-only contract preserved). `MissingObligation.next_action` is unchanged in shape and stays human-readable text only — it is never executed. The executable directive is a separate typed dict (`action_type`, validated JSON-scalar `action_parameters`, normalized `query`, normalized `scope`, `claim_ref`, durable attempt count, and budgets) traveling via verify result data into the workflow marker, never via the text field.

## Interfaces

No new REST, MCP, TUI, or GUI surface. The only interface is the operator battery CLI:

| Command | Arguments | Description |
| ------- | --------- | ----------- |
| `scripts/retrieval_benefit.py` | `--tenant ID` (required), `--days N` | Read-only G3 benefit battery; exit 0 on bar met, 1 on bar-missed, 2 on unreadable, report on stdout |

Error behavior: missing `--tenant`, missing DB env, failed catalog attestation, or unreadable rows fail the script with an actionable message and exit 2 (never exit 0 on unread data — an unread battery is not a pass).

## Implementation Detail

Implementation proceeds file by file after owner authorization of this design:

1. `src/investigation_agent_platform/application/evidence/retrieval_ledger.py` — add `claim_key`, `claim_ref`, `ClaimRetrievalLedger`, `ClaimRetrievalBinding` (+ `omitted_*`), `partition_citations`, `derive_claim_query`, `build_claim_ledger` (ledger, binding, full_unknown), `_distinct_typed_claims`, `RETRYABLE_ACTIONS`, `re_retrieval_directive` (typed dict), `_normalize_scope`, `count_persisted_attempts`, `directive_consumed`, `recorded_retrieval_attempts` (pure code-side `RETRIEVAL_ATTEMPT` filter); extend `RetrievalAttempt` with the two optional counts; extend `record_retrieval_attempt` with two optional count parameters (default `None`, fail-open preserved).
2. `src/investigation_agent_platform/application/investigation/verification.py` — add `claim_bindings` to `VerificationDiagnostics`; thread an optional `recorded_attempts` list (default `None` = legacy path, bindings degrade to `unmapped`) from `evaluate_hypothesis` into `collect_verification_diagnostics`; build per-distinct-claim bindings at the existing diagnostics assembly site; emit the typed directive into verify result data (`ledger_requery` key); `MissingObligation.next_action` keeps human-readable text only.
3. `src/investigation_agent_platform/application/worker/activities.py` — forward gateway `total_count` and banked length into the existing finalizer call (two arguments, no logic change); fail-open `TimelineEvent` append beside the finalizer (`event_type=RETRIEVAL_ATTEMPT`, attempt dict + capped IDs + truncation/counts + `action_execution_id` — the dispatch's `action_executions.action_id` — in `attributes`); fail-open `VERIFY_EVALUATION` append per verify turn (recomputed `claim_bindings`, paired `claim_ledgers`, `missing_obligations`, turn in `attributes`; every current-version turn emits a row, with a skeleton payload when the evaluation is empty; skips are warning-logged; legacy-pinned runs emit nothing; `result` never mutated).
4. `src/investigation_agent_platform/domain/timeline/models.py` — add `RETRIEVAL_ATTEMPT = "RETRIEVAL_ATTEMPT"` and `VERIFY_EVALUATION = "VERIFY_EVALUATION"` to `TimelineEventType` (additive only; never rename or remove members). `src/investigation_agent_platform/infrastructure/persistence/timeline_repository.py` — make `_from_orm` tolerant (`except ValueError: UNKNOWN`) in the same change; no migration (String(64) fits both), no RLS change. Deployment is forward-only after the first new event; an older direct-enum reader cannot be restored while new rows remain.
5. `src/investigation_agent_platform/application/worker/workflows.py` — extend the `action_history` summary update with `retrieval_attempt`, capped `evidence_ids`, `truncated_rows`, counts; add `ledger_requery` marker handling plus the `_should_force_ledger_requery` cascade rule with consumed-marking; pass `recent_actions` and `iteration` into the new optional `VerifyRootCauseInput` fields.
6. `src/investigation_agent_platform/application/worker/activities.py` (`VerifyRootCauseInput`) — add optional `recent_actions: list[dict]` (default empty) and `iteration: int | None` (default None); pass the workflow's recent action summaries and loop iteration at the verify call site.
7. `scripts/retrieval_benefit.py` — new battery script per D5 (reads timeline retrieval-attempt events with `action_execution_id` + `VERIFY_EVALUATION` events + `action_executions` census + evidence bank; `--tenant` required; runs inside the app's `rls_session`).
8. `tests/unit/test_g3_retrieval_benefit.py` — new gate file per D7 (incl. workflow forcing state-machine tests, eval-marker producer tests, battery metric/branch/contract tests); existing A3 suite untouched.

## Migrations

None. No database migration is planned or authorized. The JSON payload additions are additive-optional with legacy-tolerant readers (None/empty defaults), following the WP-A2 precedent. If implementation discovers a need for a persisted claim-ledger table, it must stop and request a separate owner decision rather than adding one.

## Testing Philosophy

### Ledger unit vectors

Pin the new pure functions exactly as WP-A3 pinned the classifier: claim-key normalization edge cases, partition splits (local/bank/unknown/unmapped), derived-query fallback to `("", None)`, caps on attempts and ID lists, and the additive count fields defaulting to `None` on legacy-shaped inputs.

### Claim-join goldens

End-to-end goldens through `build_claim_ledger` with frozen action-outcome fixtures: ID-overlap linking, query-match linking, mixed-link sources recorded, legacy row with no attempts resolving to `unmapped` with bank-fallback preserved, and unknown citations surfacing for evaporation. No verdict assertions — these tests pin partitions, not proof.

### Directive and bound vectors

`re_retrieval_directive` returns the exact typed directive dict, including normalized scope and durable `attempt_index`, for failed/under-budget and zero-prior-attempt unknown/under-budget cases, and `None` for satisfied, unmappable, uncheckable, prior-attempt unknown, over-budget, unknown-action-type, non-scalar-parameter, and empty-query cases. One vector walks the full single-retry-then-stop sequence: fail → directive → recorded retry → fail again → `None`. The `MissingObligation.next_action` text form is asserted human-only: no vector may route it to the dispatcher.

### Claim identity and overflow accounting

Pin the 64-char bound: `claim_ref` is always 16 hex chars for arbitrary statements; a 2048-char statement plus kind still yields a fittable ref while the full key stays readable. Multi-kind and multi-target hypotheses produce one binding per distinct typed claim (never merged, never crossed). Overflow vectors load 60 unknown citations and assert exactly 50 kept plus `omitted_unknown == 10`, with evaporation capped by the remaining diagnostics budget and the omitted remainder counted, not silent.

### Forcing-rule vectors

Pure workflow-cascade tests with frozen history fixtures: an unconsumed directive marker yields the exact typed action and validated parameters at the documented cascade position, after stack-walk has declined and before remaining gathering overrides; a simultaneous stack-walk debt suppresses ledger forcing; a consumed marker (flagged entry or post-budget recorded attempt) yields no override; a malformed directive yields no dispatch and a logged refusal; identical verification inputs with and without directive-bearing diagnostics yield identical verification outputs (never a verdict change). State-transition coverage (no Temporal run needed): `test_ledger_verify_result_carried_to_next_cascade` (verify result with `ledger_requery` → marker appended with `turn_issued == iteration` and normalized scope → next-turn `_should_force_ledger_requery` fires → dispatched typed action with `forced_requery` stamp at a later `iteration`), `test_ledger_failed_dispatch_stays_consumed` (FAILED/DEGRADED stamped entry still counts as consumed — no immediate re-fire), `test_ledger_scope_normalization` (None vs "" scope match), and `test_ledger_budget_uses_persisted_count_not_local_index`. Reader compatibility: `test_timeline_unknown_event_type_maps_to_unknown` (new-value row hydrates without raising); deployment tests separately attest the target catalog's RLS state and enforce the forward-only rollout/rollback rule.

### Guardrail negatives

Dedicated tests asserting the forbidden list: ledger inputs cannot produce discharge/valid/verified outcomes from any helper; `UNSCOPED_EMPTY` and `PARTIAL_ROWS` never imply completeness; verdict-differential test asserting identical statuses/tiers/confidences with ledger fields populated versus stripped.

### Battery script

Hermetic smoke coverage with fixture JSON (no live DB in unit tests): the four metric functions over a frozen record set, the exit-code contract (0 pass / 1 bar-missed / 2 unreadable), and the small-sample provisional rule.

## Documentation Plan

### `docs/IAP-proof-kernel-g3-design-v1.0.md`

**Audience:** G3 owner and implementers. This file — the normative G3 contract: decisions, exact code, commands, and the pass bar. Implementation authorization is a separate explicit owner act after review.

### `docs/IAP-proof-kernel-traceability-v1.0.md`

**Audience:** Gate owner and reviewers. After implementation: G3 row updated with battery results (rates, sample sizes, verdict-guard outcome), residuals closed or carried with rationale. Never mark G3 PASS on telemetry alone — only on the D5 bar or explicit owner acceptance of the sample.

### `docs/IAP-proof-kernel-plan-v1.0.md`

**Audience:** Planners. No change required by this design; if the owner adjusts the D5 thresholds during review, the adjusted numbers are recorded here as the accepted bar, not in implementation code.

## Risks / Trade-offs

### Per-claim queries misfire on prose claims

**Risk:** Derived claim queries for vague statements retrieve nothing or the wrong scope, producing `unmapped` bindings or misleading partitions.

**Mitigation:** `derive_claim_query` returns `("", None)` instead of guessing, and `checkable=False` marks unfalsifiable claims so they never dispatch retrieval and never enter the publish denominator (adopted from the query-to-verify design). The battery reports the unmapped rate; a high rate is a finding about claim quality, not a ledger failure.

### Re-retrieval becomes a retry loop

**Risk:** A persistently failing provider turns the single bounded retry into a per-turn tax, or pressure builds to raise the bound instead of fixing the provider.

**Mitigation:** Single retry per evaluation, budget-enforced, recorded on the attempt; repeated failures surface in the battery as failed-recovery counts rather than silent loops. Raising the bound is a policy decision with its own review, not an operator tweak (no env-switched behavior).

### Battery finds no failures to explain

**Risk:** Live traffic may be too clean for recovery chains or truncation flags to appear, leaving the benefit claim on thin samples.

**Mitigation:** Below either branch's sample floors the battery reports inconclusive and G3 stays PARTIAL — thin samples never pass, even on the diagnostic route. Clean traffic that meets the attribution floor with explicit owner sign-off is the only non-recovery path to a pass.

### Ledger data tempts verdict coupling

**Risk:** Future work sees claim-local partitions and wants demotion, blocking, or strength changes driven by them.

**Mitigation:** D6 forbids it with dedicated negative tests, and the verdict guard runs in the gate suite. Any coupling is a new design with its own gate, not a G3 extension.
