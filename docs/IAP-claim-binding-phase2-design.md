# Design: Claim binding Phase 2 — per-claim search_query prompting and ledger plumbing

## Context

Phase 1 (`docs/IAP-claim-binding-phase1-design.md`, unmerged on `iap-full-1025-claim-binding`) made claim-local resolution authoritative: every distinct typed claim `(kind, target)` partitions cited IDs against its own linked attempts plus the bank, with identity-overlap-only locality, zero-kept `INCONCLUSIVE`, `UNMAPPED_CITATION`/`EVAPORATED_CITATION` limitations, structured HISTORY `commit_subject`, the `cbv1`/`cbv2` pin (`CLAIM_BINDING_CURRENT = cbv1` during rollout), and closed-vocabulary telemetry. Repair stays dead code for Phase 3.

What is still heuristic: no claim carries its own retrieval query. `search_query` is never model-supplied — the prompt (`reasoning.py` output-contract) has no `search_query` instruction, `DecisionClaim`/`ClaimTarget`/`HypothesisEvidenceAssessment` have no query field, and `FORMULATE` only validates cited IDs against already-banked rows. At verify time `derive_claim_query()` guesses `(query, scope)` from `symbol → file_path → counterpart → commit_id` else `statement[:64]`, with scope only from `target.scope`. The guess rarely equals the real action query, so `build_claim_ledger()` query-match linkage degrades to id-overlap-only or `unmapped`, and `select_ledger_requery()`/`_match_recent_action()` can skip a directive even when the claim is unsatisfied because no recent retryable action re-derives to the guessed pair. Authoritative enforcement deliberately ignores query text entirely.

Phase 2 closes that gap: the model emits a scoped `search_query` per claim with its own validator, the query rides the claim from `DecisionClaim` through `FORMULATE` into persisted assessments, and the ledger prefers the stored pair over the derived guess at join and directive time. No new retrieval action is issued at `FORMULATE`; no new table, migration, prompt version bump beyond the claim-instruction text, API change, or pin change.

No `docs/dev/architecture.md` exists in the project.

## References

- [IAP-claim-binding-phase1-design.md](./IAP-claim-binding-phase1-design.md) — Phase 1 authoritative partitions, demotion, structured subject, `cbv1`/`cbv2` pin, telemetry, and the explicit Phase 2 non-goal this design implements.
- [IAP-proof-kernel-g3-design-v1.0.md](./IAP-proof-kernel-g3-design-v1.0.md) — G3 persisted `ClaimRetrievalLedger`/`VERIFY_EVALUATION` rows, bounded directive state machine, and provider-total exposure whose join this phase makes query-exact.
- [IAP-L2-D3-remediation-plan.md](./IAP-L2-D3-remediation-plan.md) — Remediation operating rules (review + explicit permission every step; no freeze/run-set/budget/authority/merge without owner approval) and the Phase 1 record this builds on without reopening.
- Current tree sources read during design: `ports/reasoning/reasoner.py` (`DecisionClaim` :26-44, `CallerEdgeClaim` :17-23), `domain/hypothesis/models.py` (`ClaimTarget` :56-82, `HypothesisEvidenceAssessment` :85-119), `application/investigation/reasoning.py` (prompt sections :55, output-contract :250-262, `_sanitize_decision_dict` :58-237 with claim allowlist :78 and edge rebuild :210-230, `_validate_llm_decision` :510-536, top-level strip :537-546, `_clean_target` :96-126), `application/worker/activities/execution.py` (pre-dispatch variant fan-out :610-650 with edge fan-out :621-635, `_persist_proposed_hypotheses` :3443-3566 with typed binding :3552-3566, assessment construction :3658-3684, CALL_EDGE obligations :3685-3731 with UNRESOLVED :3703-3714), `application/evidence/retrieval_ledger.py` (`_normalize_key` :186-187, `extract_search_query` :296-348, `derive_claim_query` :558-586, `build_claim_ledger` :609-698 with byte-exact match :643-646, `target_group_key` :701-720, `_distinct_typed_claims` group shape :723-782, `re_retrieval_directive` :785-832, `count_persisted_attempts` strip :944-947), `application/investigation/verification.py` (`_distinct_typed_claims` :723-782 in ledger module, diagnostics assembly :541-618 with ledger calls :610-618, `_match_recent_action` raw compare :1074-1086, `select_ledger_requery` :1098-1148 with ledger calls :1141-1149, pin constants/resolver :1311-1356, enforcement bank fallback :4929-4950), `application/worker/activities/verification.py` + `lifecycle.py` (verify/conclude recorded-attempt reads), `application/worker/workflows/forcing.py` (directive carry/force), `scripts/retrieval_benefit.py` (`link_source == "query_match"` predicate :260), `application/investigation/context_compiler.py` (`REASONER_PROMPT_VERSION` :26, persisted :876-882).

## Goals / Non-Goals

**Goals:**

- Model emits a scoped `search_query` per claim (`search_query` + `search_scope`) with prompt instructions and a dedicated validator
- Additive persisted fields carry the stored pair from `DecisionClaim` through `FORMULATE` into `HypothesisEvidenceAssessment` assessments_json (old rows validate unchanged)
- Ledger join prefers the stored pair over the derived heuristic, with `link_source` recording which basis linked (closed vocabulary)
- Re-retrieval directive and forcing consume the stored pair, so an unsatisfied claim with a stored query maps to a typed retryable action
- Normalization fixed: stored and attempt queries compare normalized (strip + truncate), never byte-exact on raw model text
- Claims with no usable evidence persist a `CLAIM_UNRESOLVED` assessment carrying the stored pair, so the query survives to the ledger and the new directive trigger (c)
- Full unit suite green, ruff clean, no new mypy errors on touched paths, scratch-tenant live spot check (conditional on separate owner approval per the remediation plan — this design does not authorize the run)

**Non-Goals:**

- Issuing new retrieval actions at `FORMULATE` time (claims still validate against banked rows; gathering stays in the gather loop + ledger directive)
- Misattribution repair turned on (Phase 3, behind the pin introduced in Phase 1)
- Smallest-fetch forcing rules and `NOT_CHECKABLE` denominator change in `ConclusionGate` (Phase 4)
- New pin value or `CLAIM_BINDING_CURRENT` repoint (stays `cbv1`; stored pair is additive and version-agnostic — see D6)
- Any freeze, run-set, budget, rollout, authority, or merge decision (separate owner approvals required)
- Changes to served-cached conclusion handling or historical artifact semantics

## Decisions

### D1: Model emits per-claim search_query with its own validator

**Decision:** The reasoning prompt gains a `claim-queries` section instructing the model to emit, per claim, the retrieval query that would find the cited evidence: `search_query` (the query string to run, e.g. symbol, file path, or commit id) and `search_scope` (the scope to run it in, e.g. path prefix or `repository@revision`; empty when unscoped). Both are plain retrieval instructions, never proof — they do not confer locality (Phase 1 identity-overlap rule stands) and are validated as bounded strings, never executed. A dedicated `_clean_claim_query()` sanitizer coerces types, strips whitespace, truncates to caps, and drops empties to `None`; it never raises and never fabricates a query for a claim that has none. Unknown claim-level keys keep the existing sanitize-drop behavior.

```python
# src/investigation_agent_platform/application/investigation/reasoning.py
# (proposed; new prompt section + sanitizer, existing sections untouched)

CLAIM_QUERY_INSTRUCTION = (
    "For each claim, also emit the retrieval that would find its evidence: "
    '"search_query" (the query string to run: symbol, file path, commit id, '
    'or distinctive statement fragment) and "search_scope" (the scope you '
    "searched or would search: path prefix or repository@revision, empty "
    "when unscoped — a plain human scope, NOT the recorded action-scope "
    "form). The query is a retrieval hint only — it never substitutes for "
    "citing evidence IDs, an unlinked query never makes a citation "
    "claim-local, and scope never gates linkage (linkage is on query text; "
    "scope only parameterizes re-retrieval)."
)


def _clean_claim_query(value: object, limit: int) -> str | None:
    """Coerce a model-emitted query/scope to a bounded string or None.

    Pure function; never raises. Non-strings that stringify cleanly
    (numbers, bools) are accepted; dicts/lists/objects yield None.
    Empty-after-strip yields None (absent, never fabricated).
    """
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
    elif isinstance(value, (int, float, bool)):
        text = str(value).strip()
    else:
        return None
    if not text:
        return None
    return text[:limit]
```

The prompt assembly adds the section after `output-contract`, and `_sanitize_decision_dict()` applies `_clean_claim_query(raw.get("search_query"), 1024)` and `_clean_claim_query(raw.get("search_scope"), 512)` per claim item and per `caller_edges[]` item. `known_claim` automatically admits the two new top-level claim keys once they exist on `DecisionClaim` (D2) — no allowlist fork for top-level claims. Edge items are NOT covered by `known_claim`: the edge rebuild at `reasoning.py:210-230` reconstructs each edge as `{"target", "evidence_ids"}` only and must be extended to preserve the cleaned edge-level pair (D3), otherwise edge queries are silently dropped before validation. `_clean_target()` (`reasoning.py:96-126`) MUST NOT gain query keys — the pair lives at claim/edge level only, never inside `target`, so `target_group_key()` fingerprints stay query-free (explicitly forbidden; pinned by test).

**Alternative considered:** Let the server keep deriving queries and skip model prompting entirely. Rejected — the derived guess is exactly the fragility this phase removes (statement truncation rarely equals the real action query); the model knows what it searched for, the server can only guess.

---

### D2: Additive stored-query fields on DecisionClaim and assessments

**Decision:** `DecisionClaim` gains `search_query: str | None` (max 1024) and `search_scope: str | None` (max 512). `HypothesisEvidenceAssessment` gains the same two optional fields with identical caps. `ClaimTarget` is untouched — the query is a retrieval instruction, not target identity, and must never enter `target_group_key()` fingerprints or `claim_key()` joins. `CallerEdgeClaim` gains the same two optional fields so fanned-out per-caller variants (execution.py `621-635`) keep their own query. All fields default `None`; old rows validate unchanged; `extra="forbid"` on `DecisionClaim`/`CallerEdgeClaim` keeps rejecting invented keys.

```python
# src/investigation_agent_platform/ports/reasoning/reasoner.py
# (additive fields only; existing fields untouched)
class DecisionClaim(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    statement: str = Field(..., min_length=1, max_length=2048)
    claim_kind: ClaimKind = ClaimKind.CAUSAL
    evidence_ids: list[str] = Field(default_factory=list, max_length=20)
    target: ClaimTarget | None = None
    # Phase 2 (claim binding): retrieval hint the model emitted for this
    # claim. Never proof, never identity — locality still requires
    # banked-ID overlap. None = legacy/model-omitted (fall back to derived).
    search_query: str | None = Field(default=None, max_length=1024)
    search_scope: str | None = Field(default=None, max_length=512)
    fact_category: JavaFactCategory | None = None
    commit_id: str | None = Field(default=None, max_length=64)
    value_semantics: str | None = Field(default=None, max_length=64)
    claimed_value: str | None = Field(default=None, max_length=512)
    caller_edges: list[CallerEdgeClaim] = Field(default_factory=list, max_length=20)
```

```python
# src/investigation_agent_platform/domain/hypothesis/models.py
# (additive fields only; existing fields untouched)
class HypothesisEvidenceAssessment(BaseModel):
    model_config = ConfigDict(frozen=True)
    # ... existing fields ...
    # Phase 2 (claim binding): stored retrieval hint carried from
    # DecisionClaim at FORMULATE. None = legacy/omitted (derived fallback).
    search_query: str | None = Field(default=None, max_length=1024)
    search_scope: str | None = Field(default=None, max_length=512)
```

`_validate_llm_decision()` needs no change beyond the model (sanitize-first, then `model_validate`, truncates via `_clean_claim_query` before validation so overlong model text can never fail the whole reason step). Assessment persistence (`assessments_json`) round-trips the two new optionals with no migration (JSON columns, Pydantic defaults).

**Alternative considered:** Store the query on `ClaimTarget`. Rejected — `ClaimTarget` feeds identity fingerprints (`target_group_key`, `claim_key`); retrieval hints must not perturb claim identity, or two identical claims with different phrasings of the same query become distinct claims.

---

### D3: FORMULATE carries the stored pair, issues nothing

**Decision:** `FORMULATE` (execution.py claim handling `639-682` + assessment construction `3658-3684`, CALL_EDGE fan-out `3643-3731`) copies `search_query`/`search_scope` from each `DecisionClaim` (or per-caller `CallerEdgeClaim` variant) onto every `HypothesisEvidenceAssessment` built from it. No retrieval is issued at `FORMULATE`: cited IDs still validate against banked rows exactly as today (typed claims drop the batch list, never merge). A claim without a stored query formulates identically to today (fields `None`). Malformed typing still skips the claim per existing behavior.

```python
# src/investigation_agent_platform/application/worker/activities/execution.py
# (proposed; inside _persist_proposed_hypotheses assessment construction
# at :3658-3684 and CALL_EDGE obligations at :3685-3731 — NOT the
# pre-dispatch fan-out at :610-650, which builds variants but no assessments)
def _stored_claim_query(raw_claim: dict[str, object]) -> tuple[str | None, str | None]:
    """Extract the stored retrieval hint from a FORMULATE claim dict.

    Pure function; never raises. Returns (search_query, search_scope),
    each bounded or None. Operates on the already-sanitized claim mapping.
    """
    try:
        query = raw_claim.get("search_query")
        scope = raw_claim.get("search_scope")
    except Exception:
        return (None, None)
    query_text = str(query).strip()[:1024] if isinstance(query, str) and str(query).strip() else None
    scope_text = str(scope).strip()[:512] if isinstance(scope, str) and str(scope).strip() else None
    return (query_text, scope_text)
```

Two fan-out sites must preserve the pair (both currently drop it):

- Pre-dispatch variant fan-out (`execution.py:621-635`): `claim_variants` spreads `**raw_claim` but rebuilds each caller variant from `edge.get("target")` / `edge.get("evidence_ids")` only. Extend the variant construction to copy the edge-level pair when present, falling back to the top-level claim pair when the edge omits it.
- Reason-side edge sanitizer (`reasoning.py:210-230`): extend the edge rebuild to preserve the cleaned edge-level pair alongside `target`/`evidence_ids`.

Callers pass the per-variant raw claim mapping (post-fan-out, so each caller variant keeps its own pair) and set the two fields on the constructed assessment. `CALL_EDGE_UNRESOLVED` assessments (new behavior at current `execution.py:3703-3714`, which today builds them without query fields) carry the pair — the unresolved caller still knows what to search for, which is exactly what the directive consumes.

**Coverage for claims with no usable evidence (all kinds, not just CALL_EDGE):** today the assessment loop (`execution.py:3658-3684`) iterates `claim_binding_ids.get(claim_key, [])`, so a non-CALL_EDGE claim whose `evidence_ids` are all malformed or unbanked persists NOTHING — no assessment, no `_distinct_typed_claims` group, query lost, ledger never built. Generalize the unresolved path: for every typed claim with zero valid cited IDs, persist exactly one assessment with `evidence_id=None` carrying kind/target/stored pair, `reason="claim unresolved: no usable evidence"`, `reason_code="CLAIM_UNRESOLVED"` (new additive code on the free-form `reason_code` field — no enum change), `schema_version="v2"`, no `fact_category` (nothing validated, so none claimed). This mirrors `CALL_EDGE_UNRESOLVED` (`:3702-3716`) for all kinds, including its domain-validator treatment (`validate_assessment_consistency` exempts both codes — the only domain-model change in this phase). The group then exists with empty `cited_ids`: enforcement skips it for locality (nothing to partition — correct), while the ledger builds an unmapped, checkable row from the stored pair that is directive-eligible under the new trigger (c) in D5. A dedicated test pins a non-CALL_EDGE claim with no usable evidence IDs: one `CLAIM_UNRESOLVED` assessment, group present, ledger checkable with stored query, directive fires.

**Alternative considered:** Fire a retrieval per claim at `FORMULATE` when the query is present but no cited rows are banked. Rejected — gathering budget, ordering, and dedup belong to the gather loop and the ledger directive state machine; `FORMULATE` firing retrievals would bypass attempt budgets and the `RETRYABLE_ACTIONS` gate.

---

### D4: Ledger prefers stored over derived, records the basis

**Decision:** `build_claim_ledger()` gains two optional parameters, `stored_query: str | None` and `stored_scope: str | None` (default `None` = legacy caller). The effective `(query, scope)` is the normalized stored pair when a non-empty stored query is present, else the existing `derive_claim_query()` output (unchanged fallback for legacy rows). Attempt linkage tries, in order: (1) banked-ID overlap (unchanged, still the only basis that confers locality); (2) normalized stored-query match; (3) legacy derived query-match (retained so old rows behave byte-identically when no stored query exists). `ClaimRetrievalLedger` gains `query_source` (`stored`|`derived`, default `derived`) recording where the ledger's effective pair came from — set to `stored` whenever a non-empty stored query is present EVEN IF nothing linked (a stored-backed miss is still `query_source="stored"` with empty `attempt_indexes`; this is the computable denominator the battery needs) — and `link_source` gains one closed-vocabulary value, `stored_query_match`, keeping `id_overlap`/`query_match`. Source and linkage are orthogonal by construction: source says where the query came from, linkage says what linked.

Current-state correction: today there is no normalization at join time — `build_claim_ledger` (`retrieval_ledger.py:631-646`) uses the derived output verbatim and compares `str(attempt.get("search_query") or "") == query` byte-exact (no strip, no casefold), QUERY-ONLY (scope is not consulted in the join at all); scope enters only later in `_match_recent_action` (`verification.py:1074-1086`), which compares query raw with `_normalize_scope` only on scope; only `count_persisted_attempts` (`:944-947`) strips both sides; `_normalize_key` (`:186-187`) serves in-memory counting only. The canonical normalization for the new comparison is strip + truncate to extraction caps (query 1024, scope 512), case-SENSITIVE (queries are code symbols — no casefold).

Scope and linkage are separated (this is deliberate, not an omission):

- Attempt LINKAGE in `build_claim_ledger` is query-only for BOTH bases, before and after Phase 2. The legacy derived join stays query-only (plus the strip-parity fix) for byte-compatibility; the stored join is likewise query-only (normalized strip + truncate, case-sensitive). Scope NEVER gates linkage — this is what makes the model scope usable at all, because recorded attempt scopes are synthesized action-specific forms (`repo@rev:prefix:query=…`, `symbol=…`, `callgraph=…`, `history=…` per `extract_search_query` `:328-347`) that a model-emitted `path prefix` or `repository@revision` will never equal. A scope-shape mismatch therefore cannot break linkage — only query text links.
- Scope travels as dispatch input: the ledger records the effective scope, and `_match_recent_action` / directive parameterization prefers the matched recent action's own recorded scope (which already has the exact synthesized shape), falling back to the stored scope when reconstructing params. The model scope is a dispatch hint, never a join key.

`link_source` precedence (single label per ledger, explicit): `stored_query_match` if any attempt linked via the stored basis, else `query_match` if any linked via the derived basis, else `id_overlap` (the default — every ledger whose attempts linked by banked-ID overlap only). `attempt_indexes` order is unchanged (index order, capped at `MAX_CLAIM_ATTEMPTS`); the label records the strongest query basis present, and ID overlap remains the SOLE basis for locality regardless of label (see locality rule below).

**Locality rule (unchanged from Phase 1, restated to remove ambiguity):** stored queries affect ONLY `ClaimRetrievalLedger` linkage (`attempt_indexes`/`link_source`) and therefore directive eligibility. They NEVER affect the authoritative partition: `ClaimBindingEnforcement.per_claim_local` is computed in `_claim_binding_partition` (`verification.py:5035-5079`) purely from banked-ID overlap (`banked_set & set(group_cited)`, `:5060-5061`, with the "query-text never confers locality" comment at `:5045-5049`), independently of `build_claim_ledger`'s query matching under EITHER basis. A query match — stored or derived — never makes a citation claim-local, under `cbv1` or `cbv2`. Tests prove it: same banked IDs with and without a stored query yield identical `ClaimBindingEnforcement` records.

```python
# src/investigation_agent_platform/application/evidence/retrieval_ledger.py
# (proposed signature + join core; partition/return shapes unchanged)
def build_claim_ledger(
    statement: str,
    claim_kind: str | None,
    claim_target: dict[str, object] | None,
    cited_ids: list[str],
    bank_ids: set[str],
    attempts: list[tuple[list[str], dict[str, object] | None]],
    target_ref: str | None = None,
    stored_query: str | None = None,
    stored_scope: str | None = None,
) -> tuple[ClaimRetrievalLedger, ClaimRetrievalBinding, list[str]]:
    """Join one claim to recorded attempts (pure).

    ``stored_query``/``stored_scope`` is the Phase 2 per-claim hint
    carried from FORMULATE (None = legacy/omitted). When present and
    non-empty after normalization, it replaces the derived guess as the
    ledger's (query, scope); otherwise derive_claim_query() output is
    used unchanged. ID overlap still exclusively confers locality;
    query matches only link attempts for directive purposes.
    """
```

All existing callers (`collect_verification_diagnostics`, `claim_binding_enforcement`, battery joins) pass the assessment-carried pair through; callers without grouping pass `None` and get byte-identical legacy behavior (proven by a regression test that calls with and without `None` on legacy-shaped inputs and asserts identical ledgers/bindings).

**Alternative considered:** Drop the derived fallback once stored queries exist. Rejected — legacy rows (pre-Phase-2 assessments, auto CAUSAL claims from `_ensure_claim_coverage`) never carry a stored query; removing the fallback would unmap every historical claim.

---

### D5: Directive and forcing consume the stored pair

**Decision:** `_distinct_typed_claims` groups gain `stored_query`/`stored_scope` (group shape change is REQUIRED — groups today carry only statement/kind/target/cited IDs at `retrieval_ledger.py:723-782` / `verification.py:723-782`). Precedence is deterministic and pinned: first non-empty pair in assessment order wins; divergent pairs within one `(kind, target)` group do not fan the group out (group identity is unchanged — query is not identity). `collect_verification_diagnostics` (`verification.py:541-618`, ledger calls `:610-618`), `claim_binding_enforcement`, and `select_ledger_requery` (ledger calls `:1141-1149`) pass the group pair into `build_claim_ledger(stored_query=…, stored_scope=…)`.

`_match_recent_action()` and `select_ledger_requery()` then operate on the ledger's effective `(query, scope)` — the stored pair when present — with no signature change at the directive layer (the ledger already carries the effective pair). `_carry_ledger_directive()`/`_should_force_ledger_requery()` consume the ledger pair unchanged. Battery join MUST be updated in the same change: `scripts/retrieval_benefit.py:260` predicates on `link_source == "query_match"` and will silently miss the new basis — widen it to `{"query_match", "stored_query_match"}` — and `scripts/agent_metrics.py` gains the `stored_query_rate` series (neither exists there today). The only other change at this layer is normalization parity: `_match_recent_action` strips + truncates the query on both sides with the same caps the ledger uses. Parity intentionally changes legacy derived-only matching too (a derived query with surrounding whitespace now matches where raw `!=` previously missed) — ship it as a named bug fix with its own regression test, not smuggled inside stored-query tests. Scope comparison in `_match_recent_action` is two-pass for stored ledgers: first pass query + scope-compatible (normalized equality), second pass query-only — and on the query-only hit the stored scope is injected into the dispatched parameters (`params["scope"]`), so the retry runs under the same scope the ledger's durable budget key counts (review fix: without injection the retry could run under the provider-synthesized scope while accounting under the stored one); derived ledgers keep the existing single-pass behavior plus the strip fix. `RETRYABLE_ACTIONS`, budgets (`IAP_WP_A3_MAX_ATTEMPTS`), durable attempt counting, and consumed-only-after-dispatch semantics are untouched.

New directive trigger (c) for never-retrieved claims: `select_ledger_requery` today fires only for unsatisfied bindings — unknown citations with zero durable attempts (a), or latest linked `SEARCH_FAILED` under budget (b). A `CLAIM_UNRESOLVED` claim (empty cited set, stored query present) satisfies neither vacuously, so without a third trigger the D3 coverage fix would persist queries that never dispatch. Add trigger (c): claim has empty cited set AND a non-empty stored query AND durable attempt count for the stored pair is zero AND the stored query maps to a typed retryable recent action (or to action reconstruction from the stored pair) — yields a directive parameterized from the stored pair. Triggers (a) and (b) are unchanged. The eligibility itself lives in `re_retrieval_directive` as a third arm (`never_retrieved`: stored-sourced ledger, empty `attempt_indexes`, durable zero) alongside `failed_retry`/`unknown_retry` — `select_ledger_requery` gates on durability/mapping, the directive function on eligibility; both must agree or no directive issues.

**Alternative considered:** A separate stored-query directive path parallel to the ledger directive. Rejected — one directive state machine (G3) with a better query source; a parallel path doubles the budget-accounting surface and invites double-dispatch of the same claim.

---

### D6: No new pin; CURRENT stays cbv1; legacy fallback is identical

**Decision:** Phase 2 introduces no new `claim_binding_version` value and does not repoint `CLAIM_BINDING_CURRENT` (stays `cbv1` during rollout, same as Phase 1; resolver at `verification.py:1311-1356` is pure with no env reads — no new env gate is introduced; the pre-existing `IAP_WP_A3_MAX_ATTEMPTS` read at `retrieval_ledger.py:274-278` is out of scope). Pin-gating for the new paths is explicit: `build_claim_ledger(stored_…)` prefers stored whenever present (diagnostics recompute runs under every pin), but authoritative enforcement still follows the pin — under `cbv1` `claim_binding_enforcement` returns `None` and the bank path decides (`verification.py:4929-4948`), so verdicts are unchanged and stored linkage is diagnostics-only; under `cbv2` the AUTHORITATIVE partition is still computed exclusively from banked-ID overlap (`_claim_binding_partition`, `:5035-5079`) and is therefore IDENTICAL with or without a stored query — what "prefers stored" changes is only the ledger's `attempt_indexes`/`link_source` (which attempts the claim is linked to for directive and battery purposes), never `per_claim_local`. Rows without a stored query behave byte-identically to today under both pins (derived fallback). Tests pin all four cells — stored-present × {cbv1, cbv2}, stored-absent × {cbv1, cbv2} — and in every cell assert the `ClaimBindingEnforcement` record is identical with and without the stored pair given the same banked IDs (query match never confers locality, both bases, both pins). Repointing `CLAIM_BINDING_CURRENT` to `cbv2` remains a separate owner amendment; Phase 2 must not smuggle it.

**Alternative considered:** New `cbv3` pin for stored-query semantics. Rejected — there is no behavior fork to pin: stored-when-present plus derived-fallback is a strict improvement with identical legacy output, not a semantic branch. A third pin value would triple the test matrix for zero behavioral distinction.

---

### D7: Telemetry distinguishes stored from derived linkage

**Decision:** `ClaimRetrievalLedger` gains `query_source` (`stored`|`derived`) and `link_source` gains `stored_query_match` (see D4); `record_claim_binding` disposition labels are unchanged (`claim_local|bank_fallback|evaporated|unmapped|no_citations`) — basis and source are visible in persisted ledgers, not in counter labels, keeping the Phase 1 closed-vocabulary guarantee intact. `agent_metrics.py` gains a `stored_query_rate` series with a COMPUTABLE denominator: stored-linked ledgers (`query_source="stored"` AND `link_source="stored_query_match"`) over stored-sourced ledgers (`query_source="stored"`, linked or not) — numerator-plus-denominator like all battery series. The denominator needs no guessing because `query_source` is persisted per ledger (a stored-backed miss is still `query_source="stored"` with empty `attempt_indexes`). No file path, query text, scope text, or statement ever enters a label — the series counts rows, never strings.

**Alternative considered:** Per-query-text counter labels. Rejected — unbounded cardinality; the repo contract forbids high-cardinality values in labels.

## Data Storage

No new table and no migration in Phase 2. All Phase 2 state rides existing JSON columns:

- `hypotheses.assessments_json`: gains optional `search_query` (string, max 1024) and `search_scope` (string, max 512) per assessment. Old rows omit both and validate unchanged; readers treat absence as "model-omitted" and use the derived fallback, never guess.
- `investigations.metadata` (JSON): unchanged (`claim_binding_version` values `cbv1`/`cbv2`, `CURRENT = cbv1`; no new value).
- `timeline_events` (`RETRIEVAL_ATTEMPT`, `VERIFY_EVALUATION` rows): no new event type; ledger rows gain the two persisted closed-vocabulary fields (`query_source`, widened `link_source`) and carry stored-backed `(search_query, scope)` where the claim had a stored pair. Old ledger rows without `query_source` read as `"derived"` (same defaulting rule as the model).
- `ActionOutcome.retrieval_attempt`: unchanged shape (attempt dict + `attempt_id`).

## Data Structures

Inputs and outputs stay separated. The only new input fields are the two optional strings on claim-shaped inputs; ledger/binding outputs keep their shapes with one widened enum:

```python
# src/investigation_agent_platform/ports/reasoning/reasoner.py
# (input side: DecisionClaim + CallerEdgeClaim gain the same pair)
class CallerEdgeClaim(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    target: ClaimTarget
    evidence_ids: list[str] = Field(default_factory=list, max_length=20)
    # Phase 2: per-caller retrieval hint (None = omitted; derived fallback).
    search_query: str | None = Field(default=None, max_length=1024)
    search_scope: str | None = Field(default=None, max_length=512)
```

```python
# src/investigation_agent_platform/application/evidence/retrieval_ledger.py
# (output side: one new provenance field + one widened enum)
# ClaimRetrievalLedger.query_source: one of "stored" | "derived"
# (default "derived"; "stored" whenever the effective pair came from the
# stored hint — even when nothing linked)
# ClaimRetrievalLedger.link_source: one of
#   "id_overlap" | "query_match" | "stored_query_match"
# (label precedence when different attempts link through different bases:
# stored_query_match > query_match > id_overlap-label; the label records
# the strongest query basis present. Locality never follows the label:
# only banked-ID overlap confers locality in the authoritative partition.)
```

`HistorySubjectInput`, `DeferGathering`, `InvestigationDecision`, `VerificationDiagnostics`, `ClaimRetrievalBinding`, and `MissingObligation` shapes are unchanged.

## Interfaces

No user-facing interface changes in Phase 2. REST, CLI, MCP, and report shapes are unchanged. Operator-visible effects are confined to diagnostics already surfaced: ledgers in `VERIFY_EVALUATION` rows carry stored-backed queries, and the battery prints the `stored_query_rate` series. Prompt text changes (the `claim-queries` instruction) are model-facing, not operator-facing, and ride the existing `REASONING_PROMPT_SECTIONS_VERSION` mechanism (bump the trailing counter per the established convention, no separate versioning scheme).

| Method | Path | Request | Response | Description |
| ------ | ---- | ------- | -------- | ----------- |
| — | — | — | — | No API change in Phase 2 |

| Command | Arguments | Description |
| ------- | --------- | ----------- |
| — | — | No CLI change in Phase 2 |

## Implementation Detail

The implementer applies, in order:

1. `ports/reasoning/reasoner.py`: add `search_query`/`search_scope` optionals to `DecisionClaim` and `CallerEdgeClaim` with the caps above. No validator changes; frozen models stay frozen.
2. `domain/hypothesis/models.py`: add the same two optionals to `HypothesisEvidenceAssessment`. No validator changes.
3. `application/investigation/reasoning.py`: add `CLAIM_QUERY_INSTRUCTION` + `_clean_claim_query()`; include the section in `build_system_prompt()` after `output-contract`; apply the cleaner per claim and per `caller_edges[]` item in `_sanitize_decision_dict()`; extend the edge rebuild (`:210-230`) to preserve the cleaned edge pair; do NOT touch `_clean_target()` allowlisting (query keys are forbidden there — pinned by test). Bump `REASONING_PROMPT_SECTIONS_VERSION` trailing counter (`v3-claim-grounded-016` → next) per the sections convention; check `REASONER_PROMPT_VERSION` (`context_compiler.py:26`, persisted `:876-882`) during implementation and bump it too if its convention requires (it versions the compiled prompt, not the sections) — record the decision in the implementation log either way.
4. `application/worker/activities/execution.py`: extend the pre-dispatch fan-out (`:621-635`) to propagate edge-level pairs (fallback to top-level); add `_stored_claim_query()`; thread the pair from each post-fan-out raw claim mapping onto every constructed assessment (`:3658-3684`) including `CALL_EDGE_UNRESOLVED` (`:3685-3731`); generalize the unresolved path to ALL kinds — every typed claim with zero valid cited IDs persists one `evidence_id=None` assessment with `reason_code="CLAIM_UNRESOLVED"`, `schema_version="v2"`, kind/target/stored pair (new behavior; today only CALL_EDGE does this at `:3702-3716`); no retrieval call added.
5. `application/evidence/retrieval_ledger.py`: add `query_source` (`stored`|`derived`) to `ClaimRetrievalLedger` (set `stored` whenever the effective pair came from the hint, even on a miss); extend `_distinct_typed_claims` groups with `stored_query`/`stored_scope` (first pair wins atomically: the first assessment's pair is kept whole and a later pair fills in only when the group has no stored query yet — query and scope are never mixed across assessments; pinned by test); extend `build_claim_ledger()` with `stored_query`/`stored_scope` (default `None`); normalize stored pair with strip + truncate (query 1024, scope 512) and `_normalize_scope` for scope, case-sensitive; prefer stored when non-empty else `derive_claim_query()` (unchanged legacy fallback); link attempts query-only in the order ID overlap, then normalized stored match (`stored_query_match`), then legacy derived match (`query_match`); scope never gates linkage; `link_source` label precedence stored > derived > overlap-label; cap and return shapes otherwise unchanged.
6. `application/investigation/verification.py`: thread the group pair into every `build_claim_ledger()` call (`collect_verification_diagnostics` `:610-618`, enforcement, directive producer `:1141-1149`); strip + truncate the query on both sides in `_match_recent_action()` (named bug fix with its own legacy-whitespace regression test) with two-pass scope handling for stored ledgers (query + scope-compatible first, query-only with stored-scope parameterization second); derived ledgers keep single-pass + strip fix; add directive trigger (c) in `select_ledger_requery` (empty cited set + non-empty stored query + zero durable attempts + retryable mapping); `derive_claim_query()` itself unchanged (legacy fallback).
7. `application/worker/activities/verification.py` + `lifecycle.py` + `workflows/forcing.py`: no logic change beyond passing the stored pair into the ledger calls they already make and the normalization-parity fix in matching; budgets, `RETRYABLE_ACTIONS`, consumed-only-after-dispatch, and fail-closed `None` handling untouched.
8. `scripts/agent_metrics.py` + `scripts/retrieval_benefit.py`: widen the `link_source == "query_match"` predicate (`retrieval_benefit.py:260`) to `{"query_match", "stored_query_match"}` (REQUIRED — otherwise stored-backed ledgers silently miss the battery join); add `stored_query_rate` series defined as `query_source="stored"` + `link_source="stored_query_match"` over `query_source="stored"` (numerator + denominator; zero-denominator safe); reuse the same strip + truncate normalization for battery joins.
9. Telemetry (`ports` + `infrastructure` + `common.py`): no new port method; `_KNOWN_LIMITATION_CODES` unchanged; `_emit_verify_telemetry` unchanged except ledgers it already emits now carry stored-backed queries. The new `stored_query_match` value is asserted closed-vocabulary by test.

## Migrations

No migration in Phase 2. Creation order is code-only: reasoner port models, domain models, reasoning prompt + sanitizer, execution FORMULATE threading, ledger join, verification directive threading, metrics. Compatibility notes: old `assessments_json` rows without the pair validate unchanged and take the derived path byte-identically; old `VERIFY_EVALUATION` ledger rows keep their derived queries and remain joinable; mixed investigations (some claims stored, some legacy) resolve per-claim with no cross-claim borrowing (Phase 1 rule preserved — a stored query on one claim never links another claim's attempts).

## Testing Philosophy

### Prompt and sanitizer

Pin the new prompt section text and the sanitizer matrix: overlong queries truncate (never fail the reason step), empty/whitespace yields `None`, non-string scalars coerce or drop per type, nested unknown keys still drop, off-vocabulary kinds still default CAUSAL. Prove a claim with an invented `search_query` key shape but garbage value formulates with `None` (never fabricated, never fatal). Prove placement: per-claim `search_query` survives while a top-level (decision-level) `search_query` is stripped by the top-level unknown-key filter with only the field name logged. Prove `_clean_target` rejects query keys (adding `search_query` inside `target` never survives sanitizing, so identity fingerprints stay query-free).

### FORMULATE carry-through

Prove each constructed assessment carries the exact stored pair from its source claim variant, including per-caller fan-out (two callers, distinct queries, each variant keeps its own; edge-omitted falls back to top-level) and `CALL_EDGE_UNRESOLVED` (pair present despite `evidence_id=None`). Prove the generalized unresolved path: a non-CALL_EDGE claim with no usable evidence IDs (all malformed or unbanked) persists exactly one `evidence_id=None` assessment with `reason_code="CLAIM_UNRESOLVED"`, kind/target/stored pair intact — and that this claim then appears in `_distinct_typed_claims` groups and yields a checkable stored-backed ledger. Prove a claim without the pair formulates with both fields `None` and verifies exactly as today. Fixture order is load-bearing: ledger-preference tests fabricate the new assessment fields first (FORMULATE threading lands before ledger tests can run against persisted rows); `assessments_json` round-trip of the pair is asserted with old rows (fields absent) validating unchanged.

### Ledger preference and fallback

Prove the preference ladder: stored present + attempt matching stored (but not derived) links via `stored_query_match` with `query_source="stored"`; stored present + attempt matching both links via `stored_query_match` with ID-overlap precedence unchanged; stored present + NOTHING matching still records `query_source="stored"` with empty `attempt_indexes` (the miss is countable, not silent); stored absent behaves byte-identically to the pre-Phase-2 join with `query_source="derived"` (regression test calls with `stored=None` and asserts ledger+binding equality with the old call shape). Prove normalization: stored `"  Widget  "` matches attempt `"Widget"` (strip + truncate, case-sensitive — `"widget"` lowercase does NOT match `"Widget"`); scope shapes never gate linkage (stored scope `repo@HEAD` vs attempt scope `repo@HEAD:/:query=Widget` still links on the query). Prove mixed-basis label precedence: attempts linking via stored on one index and derived on another yield `link_source="stored_query_match"`. Prove group precedence: one `(kind, target)` group aggregating assessments with divergent stored pairs keeps the first pair atomically (a later complete pair does not replace an earlier partial one; query and scope never mix across assessments — pinned). Prove scope injection: a stored ledger matching a recent action query-only dispatches with the stored scope in parameters. Prove trigger (c) coexists with the failed-attempt path: a citation-less claim with a prior SEARCH_FAILED under budget retries via the failed path, not blocked by the never-retrieved durable-zero gate. Prove no cross-claim borrowing with stored queries: claim A stored query matching claim B's attempt does not link B's rows to A. Prove the four pin cells: stored-present under `cbv1` changes diagnostics only (verdict identical to bank path), stored-present under `cbv2` changes ledger linkage only (authoritative `ClaimBindingEnforcement` identical with and without the pair given the same banked IDs — query match never confers locality, both bases, both pins), stored-absent under both pins is byte-identical to today.

### Directive end to end

Prove an unsatisfied claim (unknown citations, zero durable attempts) with a stored query mapping to a retryable recent action now yields a directive where the derived guess previously yielded `None` (the motivating gap, pinned as a regression test). Prove trigger (c): a `CLAIM_UNRESOLVED` claim (empty cited set, stored query, zero durable attempts for the stored pair) yields a directive parameterized from the stored pair; zero durable but no retryable mapping yields `None`; durable count above zero without failure yields `None` (no churn on already-tried queries). Prove budgets still gate: durable count at max yields no directive even with a stored query. Prove unretryable action types still yield no directive. Prove forcing consumes the stored-backed marker exactly once (consumed-only-after-dispatch, duplicate markers deduped).

### Telemetry and battery

Prove `link_source` and `query_source` vocabularies are closed: both are `Literal` types so out-of-vocabulary construction raises `ValidationError` (pinned by direct construction test), and producers only ever emit closed values. Prove no query/scope/statement text reaches any counter label. Prove the `stored_query_rate` series reports numerator + denominator computed SOLELY from persisted fields (`query_source`/`link_source`, never by re-deriving or by inspecting query text), and handles zero stored-sourced ledgers without division failure. Prove a stored-backed miss is countable: ledger with `query_source="stored"` and empty `attempt_indexes` appears in the denominator.

### Regression and invariants

Run the focused caller-edge, verification, G3, and Phase 1 binding suites plus the full unit suite. Prove `SUPPORTED` with zero kept citations remains unreachable and the Phase 1 negative controls (unknown ID + statement-matching range row, cross-claim borrowing, same-kind second target, query-text borrowing) still hold with stored queries present — stored queries must never confer locality.

## Documentation Plan

### `docs/IAP-L2-D3-remediation-plan.md`

**Audience:** Owner and auditors

Append a Phase 2 record under the remediation log: scope (per-claim stored query + `CLAIM_UNRESOLVED` coverage + ledger preference + trigger (c) + directive fix), entry evidence (this design plus its adversarial review and the six finding responses), exit evidence (test tallies, ruff, mypy, scratch-tenant spot check only if separately approved), and the explicit statement that no freeze, run-set, budget, authority, or merge was consumed.

### `docs/IAP-proof-kernel-g3-design-v1.0.md`

**Audience:** Engineers working the retrieval and battery line

Add an as-built note in the ledger section: ledgers now prefer the stored per-claim pair (`query_source="stored"`, `stored_query_match` basis, scope never gating linkage) with derived retained as fallback; directive matching normalizes the query on both sides with two-pass scope handling, plus trigger (c) for `CLAIM_UNRESOLVED` never-retrieved claims; battery predicates widened to the stored basis. No change to the directive state machine, budget accounting, or event types (ledger rows gain two closed-vocabulary fields).

### `docs/IAP-claim-binding-phase1-design.md`

**Audience:** Engineers working the claim-binding line

Add a forward pointer (no edits to the Phase 1 record itself): Phase 2 supersedes `derive_claim_query` as the primary query source while retaining it as the legacy fallback; locality rules, pins, and telemetry vocabularies from Phase 1 are unchanged.

## Risks / Trade-offs

### Model emits plausible-but-wrong queries

**Risk:** The model may emit a tidy query that retrieves nothing (wrong symbol spelling, wrong scope), converting a previously bank-linked claim into directive churn against a dead query.

**Mitigation:** Stored queries never confer locality and never remove ID-overlap linkage — worst case is a wasted bounded retry, not a lost verification. Attempt budgets (`IAP_WP_A3_MAX_ATTEMPTS`) and durable counting cap the churn; the battery `stored_query_rate` series measures hit rate so a bad-prompt regression is visible before any enforcement widening.

### Scope mismatch on stored queries

**Risk:** Model scopes (path prefixes, `repository@revision`) will essentially never equal the provider's recorded synthesized scopes (`repo@rev:prefix:query=…`, `symbol=…`, `callgraph=…`, `history=…`), so any design that gates linkage on scope equality fails linkage systematically.

**Mitigation:** Scope never gates linkage by construction (D4) — only normalized query text links attempts, so scope-shape mismatch cannot break linkage, only (at worst) leave dispatch parameterized from the recent action's own recorded scope. The model is explicitly instructed to emit a plain human scope, not the recorded form, and the prompt states exact recorded forms are not required. A query-text miss degrades to the exact pre-Phase-2 behavior (bank fallback with limitation), never to a new failure mode. Tests pin scope-shape mismatch still linking on the query.

### Prompt bloat and version churn

**Risk:** Every claim now carries two more fields through prompt, reason payload, workflow input, and assessments, growing token and storage footprints.

**Mitigation:** Both fields are optional and capped (1024/512); omission is free and behaves exactly as today. The prompt addition is one short section, not per-kind boilerplate. Storage is two nullable JSON keys per assessment — no column, no index, no migration.

### Stored query as a covert channel

**Risk:** Model-controlled query text flows into persisted ledgers and battery joins; a hostile or buggy model could stuff the query with exfiltrated content or break joins with adversarial strings.

**Mitigation:** Queries are truncated, stripped, and never executed as code — they parameterize existing typed retrieval actions through the `RETRYABLE_ACTIONS` gate. They never enter telemetry labels, never enter fingerprints, and overlong content is cut before validation. The validator test pins truncation and type coercion.
