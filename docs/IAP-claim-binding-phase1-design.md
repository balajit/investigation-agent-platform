# Design: Claim binding Phase 1 — authoritative claim-local resolution, evaporated demotion, L3 subject field

## Context

The investigation loop formulates typed claims (`ClaimKind` + `ClaimTarget` on `HypothesisEvidenceAssessment`), banks evidence rows globally per investigation, then verifies each hypothesis in `VerificationEngine.evaluate_hypothesis` with kind-specific gates and publishes through `ConclusionGate.evaluate`. Steps 2 through 9 of the L2/D3 remediation hardened the gates (exact file/range/revision binding, caller-edge obligations, revision-aware reads, shared `evaluate_caller_edge` seam, D3 identity tags).

What is still diagnostics-only: `VerificationDiagnostics` already records `evaporated_ids`, `demotions`, `missing_obligations`, `claim_bindings`, and `claim_ledgers` (G3 persisted `ClaimRetrievalLedger` / `VERIFY_EVALUATION` rows), but nothing authoritative reads them. A hypothesis whose citations resolve to zero bound rows still flows through the same generic `INCONCLUSIVE` path as a hypothesis that was never retrieved for, and HISTORY content-grounding relies on exact-substring match of the stored commit subject inside freeform model prose (`subject.casefold() in statement.casefold()`), which fails on trivial rewordings such as "with a STANDARD default" versus "with STANDARD default".

Phase 1 makes claim-local resolution and evaporated demotion authoritative, adds a structured `commit_subject` field that ends the substring fragility, and introduces the version pin that later phases reuse for bounded repair. Repair itself stays off in Phase 1. No new table, no new migration, no prompt change, no API change.

No `docs/dev/architecture.md` exists in the project.

## References

- [IAP-L2-D3-remediation-plan.md](./IAP-L2-D3-remediation-plan.md) — Remediation operating rules (review + explicit permission every step; no re-freeze or formal collection without amendment, freeze record, run-set id, budget) and the Step 12 End-A close-out that this work builds on without reopening.
- [IAP-L2-variance-diagnosis-problem.md](./IAP-L2-variance-diagnosis-problem.md) — Caller-edge obligation design (Steps 5.3–5.8) whose exact binding, revision-aware reads, and shared evaluation seam Phase 1 reuses unchanged.
- [IAP-D3-definition-convergence-problem.md](./IAP-D3-definition-convergence-problem.md) — D3 identity and freeze-provenance work (Step 8) whose behavior/protocol/amendment tagging pattern Phase 1 follows for the new pin.
- Current tree sources read during design: `domain/hypothesis/models.py` (`ClaimKind`, `ClaimTarget`, `HypothesisEvidenceAssessment`), `application/evidence/retrieval_ledger.py` (`ClaimRetrievalLedger`, `ClaimRetrievalBinding`, `partition_citations`), `application/investigation/verification.py` (`history_proof`, `validate_history_claim`, `evaluate_caller_edge`, `ConclusionGate.evaluate`, `resolve_verifier_version`), `application/worker/activities/verification.py` and `lifecycle.py` (verify/conclude resolution paths).

## Goals / Non-Goals

**Goals:**

- Authoritative claim-local citation resolution: each typed claim resolves against its own ledger attempts first, global bank second with an explicit `UNMAPPED` limitation, unknown IDs fail closed with typed blockers
- Evaporated-evidence demotion that blocks `VERIFIED`: zero kept citations with a non-empty cited set yields `INCONCLUSIVE` with named `evaporated_ids` and a `MissingObligation` smallest-fetch next action
- L3 HISTORY content-grounding on a structured `commit_subject` field with deterministic normalized comparison, newest-commit rule preserved
- Version pin for claim-binding behavior recorded on the entity at creation; unmarked rows resolve to current semantics per the version-pinned-behavior contract
- Repair code path structured but pinned OFF (no-op in Phase 1); Phase 3 flips it under its own gate
- Full unit suite green, ruff clean, no new mypy errors on touched paths, scratch-tenant live spot check

**Non-Goals:**

- Per-claim `search_query` prompting and ledger plumbing (Phase 2)
- Misattribution repair turned on (Phase 3, behind the pin introduced here)
- Smallest-fetch forcing rules and `NOT_CHECKABLE` denominator change in `ConclusionGate` (Phase 4)
- Any freeze, run-set, budget, rollout, authority, or merge decision (explicitly out of scope; separate owner approvals required)
- Changes to served-cached conclusion handling or historical artifact semantics

## Decisions

### D1: Authoritative claim-local resolution with UNMAPPED fallback

**Decision:** Every distinct typed claim `(kind, target)` partitions its own cited IDs against (a) its own linked attempt IDs — the union of banked IDs from timeline attempts that banked at least one of the claim's cited IDs (identity overlap only; query-text matching never confers locality) — and (b) the investigation bank. A fetch for one claim never makes another claim's citation appear claim-local, not even for two targets of the same kind (no cross-claim borrowing). The hypothesis kept set is the union of per-claim kept sets plus banked causal-step IDs (steps cite by construction). Per-claim locality is recorded as `local_groups` / `cited_groups` (`KIND|target_key`) with kind-level rollups (`local_kinds`, `cited_kinds`, `unmapped_kinds` = kinds with a cited group lacking local hits); the CALL_EDGE/HISTORY refusal fires when the verified kind is unmapped (`unmapped_kinds`) under observed linkage — including a second target of an otherwise-local kind. Claim-local hits verify normally. Bank-fallback-only citations verify only with an explicit `UNMAPPED_CITATION` limitation and can never satisfy CALL_EDGE completeness or HISTORY newest-commit matching. Unknown IDs (in neither set) fail closed: the claim is `INCONCLUSIVE` with `EvaporatedId` entries and a `MissingObligation`, and the hypothesis cannot `VERIFY` on that claim. The existing silent-drop behavior in verify/conclude `_resolve` helpers is replaced by `bound_kept_evidence_only` filtering plus demotion records; nothing is dropped without a named entry. All five activity-site `validate_*` fast paths in both activities (plus engine-internal java-fact dispatch via the code-value input) consume the filtered set. Faults fail closed at every choke point: a fault enforcement yields `INCONCLUSIVE` in `evaluate_hypothesis`, in `enforce_claim_binding` (covering all fast paths), in the conclude waiver block, and in the `None`-enforcement guards (which now construct fault enforcements instead of degrading).

No new partition model is introduced. The authoritative partition IS the persisted `ClaimRetrievalBinding` built by the existing `build_claim_ledger()` in `src/investigation_agent_platform/application/evidence/retrieval_ledger.py`, which already carries `claim_key`, `claim_ref` (via `claim_ref()`), `target_ref` (target-distinct fingerprint, never the colliding statement+kind key alone), `claim_local` / `bank_fallback` / `unknown` each capped at `MAX_PARTITION_IDS` with `omitted_local` / `omitted_bank` / `omitted_unknown` counters, and `unmapped=True` exactly when no recorded attempt linked to the claim. Phase 1 reuses it unchanged; no parallel model, no second join surface.

Causal steps cite by construction: `causal_step_grounding` resolves each step's `evidence_ids` against banked rows, so a step can only ground via its cited IDs, and those IDs join the hypothesis cited set consumed by the partition. An "uncited row" therefore cannot ground a causal proof — steps with uncited or unknown IDs fail the grounding gate before the verdict layer is reached. Target-declared gates (`DEFINITION` window proof, location validation) may verify without citations; that is the existing vocabulary for declaration-shaped claims, explicitly out of Phase 1 scope (empty cited sets follow the existing missing-obligation path, never the evaporation branch).

`linkage_observed` distinguishes "timeline read, nothing linked" from "no timeline read". Locality refusal (CALL_EDGE/HISTORY bank-fallback-only denial) and the unmapped-only gate-waiver block fire only on observed absence; unobserved linkage degrades to the unmapped bank path with its `UNMAPPED_CITATION` limitation. Faults fail closed: a partitioning fault under `cbv2` yields an `INCONCLUSIVE` fault verdict, never a legacy bank-path decision (see `ClaimBindingEnforcement.fault`).

`has_linked_attempts` is defined precisely as: the paired `ClaimRetrievalLedger.attempt_indexes` is non-empty. "Attempts recorded in the investigation but none linked to this claim" yields an empty index list and therefore `unmapped=True` — identical to the no-history case, never a silent third state. Empty cited sets yield empty partitions with `unmapped` as computed; callers distinguish "no citations" from "evaporated" by checking the cited set, never the partition alone.

Verdict enforcement (not diagnostics-only): immediately after the partition is built in verify AND conclude, the working evidence set is filtered down to kept IDs before any kind gate runs:

```python
# src/investigation_agent_platform/application/worker/activities/verification.py
# (proposed new helper; conclude path in lifecycle.py calls the same helper,
# never a forked copy)
def bound_kept_evidence_only(
    evidence_items: list[Any], kept_ids: set[str], cited_ids: set[str]
) -> tuple[list[Any], list[str], int]:
    """Filter the verify/conclude working set down to kept citations.

    Returns (kept_items, evaporated_ids, cited_kept_count). Rows whose IDs
    are not cited pass through untouched (orientation/context, never proof)
    and never count toward cited_kept_count. Cited rows outside kept_ids
    are dropped here AND returned as evaporated so the caller records them
    — never silently omitted. Pure function; callers own the
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
```

`kept_ids` is the union of the partition's `claim_local` plus explicitly-cited bank-fallback rows the caller admits with an `UNMAPPED_CITATION` limitation (never for CALL_EDGE completeness or HISTORY newest-commit matching). The verdict-enforcing branch is keyed on **cited-kept survival, not raw `kept` emptiness**: `cited_kept_count == 0 while cited != empty` yields `INCONCLUSIVE` with a `Demotion` + `MissingObligation`, and no `*_validated` flag is set — even when uncited orientation/context rows (including statement-matched range rows) are present in `kept`. Every `validate_*` fast path receives the filtered set in BOTH activities (five activity call sites — `validate_code_value_claim` dispatches to `validate_java_fact_claim` engine-internally, so filtering the code-value input covers it transitively; the entry-site enumeration test asserts this explicitly); the existing silent `continue` / `if item is not None` drops in the `_resolve` helpers are replaced by calls to this helper.

Tranche B `statement_range_reads` gate: the statement-path/stem inclusion (`verification.py statement_range_reads`) runs AFTER the partition and may only ADD rows that are claim-local-linked or explicitly cited. It can never substitute for zero cited-kept survival: when `cited_kept_count == 0`, the claim takes the evaporation branch even if a statement-named banked read sits uncited in `kept`. Negative control: cite an unknown ID against an otherwise-passing bank plus a statement-matching range row — cited-kept is zero, so the claim must still go `INCONCLUSIVE` with the unknown ID named in `evaporated_ids`.

**Alternative considered:** Keep resolution diagnostics-only and add a parallel authoritative table. Rejected — the G3 persisted ledger already carries the join identity; a second table doubles the join surface and invites drift between the path that logs and the path that decides.

---

### D2: Structured commit_subject ends substring fragility (L3 merge point)

**Decision:** `ClaimTarget` gains `commit_subject: str | None` (max 512, frozen model, additive field). `HypothesisEvidenceAssessment` gains a denormalized `commit_subject` mirror so readers need not chase the target object. `history_proof` and `validate_history_claim` require the structured subject and compare it against the `COMMIT_HISTORY` row `summary` with a deterministic canonical comparison: exact match after canonicalization, else token-set Jaccard at or above 0.70, else fail. The freeform `subject.casefold() in statement.casefold()` check is removed. The newest-commit rule (newest path row must be among matching rows) is preserved unchanged.

Canonicalization order is fixed and pinned: casefold, trim, balanced outer-quote strip (one layer, matching kinds only), conventional-commit type-prefix strip, whitespace collapse. Token-set comparison is Jaccard over alphanumeric tokens; the 0.70 threshold and the prefix list are frozen constants with unit pins. Operating point (measured during implementation): the motivating rewording scores 0.75 (passes); one-word insertions in <=3-token subjects score <=0.667 (fail, including the generic "Update README" collision); any single-token drift scores <=0.5 (fails).

```python
# src/investigation_agent_platform/domain/hypothesis/models.py (additive fields only)
class ClaimTarget(BaseModel):
    model_config = ConfigDict(frozen=True)

    symbol: str | None = Field(default=None, max_length=256)
    counterpart: str | None = Field(default=None, max_length=256)
    file_path: str | None = Field(default=None, max_length=512)
    line_start: int | None = Field(default=None, ge=1)
    line_end: int | None = Field(default=None, ge=1)
    scope: str | None = Field(default=None, max_length=512)
    declaring_type: str | None = Field(default=None, max_length=512)
    commit_id: str | None = Field(default=None, max_length=64)
    commit_subject: str | None = Field(
        default=None,
        max_length=512,
        description="Structured commit subject the HISTORY claim asserts. Compared "
        "deterministically against the cited COMMIT_HISTORY row summary; never "
        "matched by substring against freeform model prose.",
    )
    value_semantics: str | None = Field(default=None, max_length=64)
    claimed_value: str | None = Field(default=None, max_length=512)
    fact_category: JavaFactCategory | None = Field(default=None)
```

```python
# src/investigation_agent_platform/application/investigation/verification.py
# (proposed; new symbols, no existing code touched)
from __future__ import annotations

import re

_COMMIT_TYPE_PREFIX = re.compile(r"^[a-z][a-z0-9_-]*(\([^)]*\))?(!)?:\s+")

_SUBJECT_TOKEN_THRESHOLD = 0.70


def _canonical_subject(text: str) -> str:
    """Canonicalize a commit subject for deterministic comparison.

    Fixed order, pinned by unit vectors: casefold, trim, balanced
    outer-quote strip (one layer, matching quote kinds only),
    conventional-commit type-prefix strip, whitespace collapse.
    """
    lowered = str(text or "").casefold().strip()
    if len(lowered) >= 2 and lowered[0] == lowered[-1] and lowered[0] in {"'", '"'}:
        lowered = lowered[1:-1].strip()
    lowered = _COMMIT_TYPE_PREFIX.sub("", lowered)
    return re.sub(r"\s+", " ", lowered).strip()


def _subject_tokens(canonical: str) -> frozenset[str]:
    return frozenset(re.findall(r"[a-z0-9]+", canonical or ""))


def subjects_match(claimed: str | None, stored: str | None) -> bool:
    """Deterministic structured-subject comparison for HISTORY grounding.

    Exact match after canonicalization wins. Otherwise token-set Jaccard
    at or above the frozen threshold wins. Empty either side never matches.
    Pure function; no model prose is consulted.
    """
    left = _canonical_subject(str(claimed or ""))
    right = _canonical_subject(str(stored or ""))
    if not left or not right:
        return False
    if left == right:
        return True
    left_tokens = _subject_tokens(left)
    right_tokens = _subject_tokens(right)
    if not left_tokens or not right_tokens:
        return False
    intersection = len(left_tokens & right_tokens)
    union = len(left_tokens | right_tokens)
    if union == 0:
        return False
    return (intersection / union) >= _SUBJECT_TOKEN_THRESHOLD
```

Legacy-vs-strict routing (no silent behavior change): rows evaluated under pin `cbv1` (or unmarked, which resolves to current — see D4) keep the byte-identical legacy HISTORY predicate (path + commit prefix + non-empty description, plus the existing statement-substring check in `validate_history_claim`). Rows under pin `cbv2` require a non-empty `target.commit_subject` (fail closed otherwise — an empty subject never matches) and gate matching rows on `subjects_match(target.commit_subject, row.summary)`; the freeform statement-substring check is removed on the `cbv2` path only. The newest-commit rule (newest path row must be among matching rows) is preserved on both paths.

Rows whose summary matches the structured subject rewording ("with STANDARD default" versus "with a STANDARD default", Jaccard 0.75) now pass via token-set containment; rows with genuinely different subjects still fail closed. Frozen threshold vectors pin: exact-after-canonical match; one-token subjects (single-token drift fails); two-to-three-token subjects (one-token drift fails at 0.70 — scores <=0.667); generic-subject collision ("Update README" versus "Update README twice", 0.667, fails); longer-subject one-word insertion (motivating rewording, 0.75, passes); conventional-prefix strip (`fix(parser): ...` equals `...`); balanced-quote canonical order (unbalanced quotes retained in canonical form; token comparison itself is punctuation-insensitive).

**Alternative considered:** Token-set comparison against the freeform hypothesis statement instead of a structured field. Rejected — that preserves the exact fragility this phase removes (prose is unversioned and untestable); the structured field makes the asserted subject explicit, reviewable, and pinned in tests.

---

### D3: Evaporated demotion blocks VERIFIED (authoritative, not advisory)

**Decision:** When a typed claim cites a non-empty ID set and the authoritative partition keeps zero IDs, the claim records a `Demotion` with `evaporated_ids` and a `MissingObligation{claim, claim_kind, next_action}` and the hypothesis evaluates `INCONCLUSIVE` on that claim. `SUPPORTED` with zero kept citations is impossible. The next action reuses the frozen per-kind directives already present (`_MISSING_OBLIGATION_DIRECTIVES`) and feeds the existing `next_check_for` priority (rival, then relation, then unmapped/evaporated, then provider) without changing that priority order.

Two new frozen `LimitationCode` values carry the verdict-visible caveat: `UNMAPPED_CITATION` (claim verified only on bank-fallback rows with no claim-local retrieval) and `EVAPORATED_CITATION` (some cited IDs resolved to no bound row). Both are additive enum members; no existing code is renamed or removed.

```python
# src/investigation_agent_platform/application/investigation/verification.py
class LimitationCode(str, Enum):
    RIVAL_UNTESTED = "RIVAL_UNTESTED"
    SINGLE_PROVIDER = "SINGLE_PROVIDER"
    CONCLUSION_UNMAPPED = "CONCLUSION_UNMAPPED"
    RELATION_UNDEMONSTRATED = "RELATION_UNDEMONSTRATED"
    NO_RUNTIME_OBSERVATION = "NO_RUNTIME_OBSERVATION"
    RIVAL_SHARED_EVIDENCE = "RIVAL_SHARED_EVIDENCE"
    # Phase 1 (claim binding): bank-fallback-only citations are not proof
    # of claim-local retrieval; evaporated citations are named, not silent.
    UNMAPPED_CITATION = "UNMAPPED_CITATION"
    EVAPORATED_CITATION = "EVAPORATED_CITATION"
```

Verify-time pseudo-flow inside `evaluate_hypothesis` (narrative, not a new orchestrator): for each distinct typed claim `(kind, target)`, build the authoritative partition from that claim's own ledger attempts plus the investigation bank; drop unknown IDs into `EvaporatedId` records with `CITED_ID_EVAPORATED`; run the existing kind gates only over kept IDs; if kept is empty while cited is non-empty, mark the claim `INCONCLUSIVE`, attach the `Demotion` + `MissingObligation`, and continue (never `VERIFIED` on that claim). A fault enforcement short-circuits the whole hypothesis to `INCONCLUSIVE`. Claims with empty cited sets follow the existing missing-obligation path unchanged.

**Alternative considered:** Demote to `LIKELY` instead of `INCONCLUSIVE` so partial answers survive. Rejected — `LIKELY` still publishes through the gate on the in-band path; a claim with zero grounded citations has no publishable basis and must not enter any publish tier.

---

### D4: Version pin for claim-binding behavior (no env switch)

**Decision:** Claim-binding strictness is versioned on the entity, not the environment. A new investigation metadata pin `claim_binding_version` takes values `cbv1` (bank path decides, diagnostics-only — the pre-Phase-1 behavior) and `cbv2` (Phase 1: authoritative partitions + demotion + structured HISTORY subject). The `cb`-prefixed values are deliberately distinct from `VERIFIER_VERSION_*` (`v1`/`v2`/`v3`) so dashboards, log queries, and telemetry labels never conflate the two pins. Resolution mirrors `resolve_verifier_version` exactly: a `CLAIM_BINDING_CURRENT` constant names the current semantics, unmarked rows resolve to it, unknown pins fail closed with the existing typed errors (verify yields an established-error result, conclude denies). During Phase 1 rollout `CLAIM_BINDING_CURRENT` is `cbv1`, so unmarked historical rows keep byte-identical behavior; repointing it to `cbv2` afterward requires a separate owner amendment, never a silent flip. No environment variable gates the version decision (the `IAP_WP_A3_MAX_ATTEMPTS` bound is an orthogonal attempt-budget control, out of scope here). The repair consumer introduced in Phase 3 reads the same pin and stays a no-op while the pin resolves to `cbv1` or while its own repair sub-pin is off.

No-repository degradation (mirrors the verifier contract): when no investigation repository is wired (tests, dev), there is no persisted row to misread, so resolution yields `CLAIM_BINDING_CURRENT` with an explicit `no-repository` source — loud in story logs, never a silent default. This matches `resolve_verifier_version`'s no-repository branch exactly; it is not an exception to fail-closed (there is no pin to misread), and the provenance reads `no-repository`, never `pinned`.

```python
# src/investigation_agent_platform/application/investigation/verification.py
# (proposed; mirrors resolve_verifier_version at verification.py:1142-1169)
CLAIM_BINDING_CBV1 = "cbv1"
CLAIM_BINDING_CBV2 = "cbv2"
CLAIM_BINDING_CURRENT = CLAIM_BINDING_CBV1  # Phase 1 rollout value; repoint needs owner amendment
KNOWN_CLAIM_BINDING_VERSIONS = frozenset({CLAIM_BINDING_CBV1, CLAIM_BINDING_CBV2})


def resolve_claim_binding_version(metadata: Any) -> tuple[str, str]:
    """Resolve the claim-binding behavior version for one investigation.

    Returns (version, source) where source is "pinned" or "unmarked".
    Unmarked rows resolve to CLAIM_BINDING_CURRENT, preserving the
    historical default path per the version-pinned-behavior contract.
    Unknown pins raise UnknownVerifierVersion; unreadable metadata raises
    VerifierVersionUnavailable — the same typed fail-closed shapes the
    verify/conclude activities already catch. Pure function; no I/O, no
    environment reads.
    """
    from investigation_agent_platform.application.investigation.verification import (
        UnknownVerifierVersion,
        VerifierVersionUnavailable,
    )

    try:
        if hasattr(metadata, "model_dump"):
            mapping = dict(metadata.model_dump() or {})
        elif isinstance(metadata, dict):
            mapping = dict(metadata)
        else:
            raise VerifierVersionUnavailable("claim binding metadata unreadable")
    except (UnknownVerifierVersion, VerifierVersionUnavailable):
        raise
    except Exception as exc:
        raise VerifierVersionUnavailable(f"claim binding metadata unreadable: {exc}") from exc
    pinned = str(mapping.get("claim_binding_version") or "").strip()
    if not pinned:
        return (CLAIM_BINDING_CURRENT, "unmarked")
    if pinned not in KNOWN_CLAIM_BINDING_VERSIONS:
        raise UnknownVerifierVersion(f"unknown claim_binding_version: {pinned!r}")
    return (pinned, "pinned")
```

Verify AND conclude both resolve the pin at entry (conclude today hardcodes its version and must be extended — a test enumerates every verify/conclude entry path and proves each resolves). Creation paths stamp `claim_binding_version: cbv1` on new investigations during Phase 1 rollout so every row carries an explicit mark; the Phase 3 gate flips new rows to the repair-enabled sub-state only after shadow proves zero over-rebind.

**Alternative considered:** Environment variable to enable strict binding per deployment. Rejected — the repo contract forbids env-switched legacy branches; recorded versions keep in-flight rows on the semantics they started with.

---

### D5: First-class telemetry for every new Phase 1 path (closed vocabularies, fire-and-forget)

**Decision:** Every new Phase 1 behavior emits OTel counters plus `loop_telemetry` keys plus battery-measurable series. No new code path ships without all three. Label vocabularies are closed by construction (claim kinds, partition dispositions, limitation codes, pin versions, booleans); high-cardinality values (file paths, line ranges, revision SHAs, subjects, statements) never appear as labels — they belong in structured span/log fields only. All emissions are fire-and-forget: adapters never raise into an investigation.

New port methods on `ObservabilityPort` (with matching `OpenTelemetryObservabilityAdapter` counters):

```python
# src/investigation_agent_platform/ports/observability/telemetry.py
def record_claim_binding(
    self,
    context: SpanContext,
    claim_kind: str,
    disposition: str,
    binding_version: str,
) -> None: ...
```

`claim_kind` is one of `DEFINITION|CALL_EDGE|BEHAVIOR|ABSENCE|HISTORY|CAUSAL|UNTYPED|unspecified`. `disposition` is one of `claim_local|bank_fallback|evaporated|unmapped|no_citations`. `binding_version` is one of `cbv1|cbv2` (never bare `v1`/`v2`, which collide with the verifier pin). (`UNTYPED` covers untargeted/ungrouped claims; closed, never high-cardinality.)

```python
def record_history_subject(
    self, context: SpanContext, outcome: str, newest_rule: str
) -> None: ...
```

`outcome` is one of `matched_exact|matched_token_set|mismatched_subject|missing_subject|missing_row`. `newest_rule` is one of `satisfied|violated|not_applicable`.

```python
def record_binding_version(
    self, context: SpanContext, version: str, source: str
) -> None: ...
```

`version` is one of `cbv1|cbv2`. `source` is one of `pinned|unmarked`.

Counters (`infrastructure/observability/telemetry.py`): `claim.binding.decisions` (by kind/disposition/version), `history.subject.checks` (by outcome/newest_rule), `claim.binding.version` (by version/source). Each `add(1, attrs)` guarded by `self._enabled` and wrapped in try/except at call sites via the existing `_emit_telemetry` helper.

`loop_telemetry` additive keys (stamped on verify/conclude telemetry, bounded scalars only): `claim_binding_kept`, `claim_binding_evaporated`, `claim_binding_unmapped`, `claim_binding_version`. No per-claim text enters `loop_telemetry`; counts only.

`scripts/agent_metrics.py` series (read-only battery analysis over persisted `VERIFY_EVALUATION` rows): `evaporated_rate`, `unbound_rate`, per-kind `VERIFIED/LIKELY/INCONCLUSIVE` split, `history_subject_match_rate` (exact vs token-set vs mismatch). The battery report prints numerator plus denominator, never a bare percentage.

Call sites: `_emit_verify_telemetry` in `application/worker/activities/common.py` emits `record_claim_binding` per evaluated typed claim, `record_history_subject` per HISTORY claim evaluated, and `record_binding_version` once per verify/conclude turn. Emission failures are swallowed (existing `_emit_telemetry` contract).

**Alternative considered:** Reuse the generic `record_metric` escape hatch per call site. Rejected — ad-hoc metric names break the closed-vocabulary guarantee and cannot be gated in review; named methods with frozen label sets are enforceable.

---

### D6: CALL_EDGE and HISTORY completeness ride the same partition (no gate weakening)

**Decision:** The Step 5 caller-edge completeness rule and the HISTORY newest-commit rule consume the authoritative partition, not the raw cited set. An aggregate caller-enumeration claim verifies only when every named caller has its own claim-local kept edge; bank-fallback-only callers yield `MissingObligation(GET_CODE caller window)` and the claim stays `INCONCLUSIVE`. HISTORY newest-commit matching considers only rows that pass `subjects_match` on the `cbv2` path; a newest row with a non-matching subject cannot satisfy the rule. `evaluate_caller_edge` remains the single shared seam for verify and conclude; both callers pass kept IDs only.

Gate-waiver block: the `ConclusionGate` `*_validated` waiver (`location_validated`, `doc_validated`, `state_validated`, `code_value_validated`, `history_validated`, `caller_edges_validated`) cannot be satisfied by a claim with zero cited-kept survival (`cited_kept_count == 0`), nor — when linkage was observed and no causal chain exists — by a verification that rested only on bank-fallback rows with `UNMAPPED_CITATION`. With a causal chain present the gate does not need the waiver and the limitation rides the published conclusion instead. The waiver inputs are computed from kept-rows-only validations in both activities; a blocked claim forces the `no validated causal chain` blocker path even when a `*_validated` flag would otherwise be set. Refusal and the unmapped-only prong both require `linkage_observed`: unobserved linkage (no timeline) keeps the legacy bank path with its limitation instead of abstaining on missing telemetry.

Enumerated entry sites (five activity call sites plus engine-internal dispatch, in verify AND conclude): `validate_code_value_claim` (which dispatches to `validate_java_fact_claim` engine-internally — filtering the code-value input covers it transitively), `validate_history_claim`, `validate_location_claim`, `validate_doc_claim`, `validate_state_claim`, plus the `evaluate_caller_edge` seam. A test enumerates these call sites and proves each receives the filtered set.

**Alternative considered:** Let bank-fallback rows satisfy completeness when claim-local rows are absent (lenient migration). Rejected — that reintroduces the exact L2 pattern (nine banked rows, zero bound) this phase exists to close.

---

## Data Storage

No new table and no migration in Phase 1. All Phase 1 state rides existing JSON columns:

- `hypotheses.assessments_json`: gains optional `commit_subject` (string, max 512) mirroring `ClaimTarget.commit_subject`. Old rows omit it and validate unchanged; readers treat absence as "unstructured legacy subject" and route HISTORY claims through the unmapped path, never by guessing.
- `investigations.metadata` (JSON): gains optional `claim_binding_version` (`cbv1` stamped at creation during rollout; `cbv2` for strict rows). Readers resolve via `resolve_claim_binding_version`; unmarked resolves to `CLAIM_BINDING_CURRENT` (`cbv1` during rollout); unknown values fail closed with typed errors.
- `VERIFIED`/`INCONCLUSIVE` results: existing `VerificationResult.diagnostics` (`evaporated_ids`, `demotions`, `missing_obligations`, `claim_bindings`, `claim_ledgers`) is now populated on the authoritative path, not only the shadow path. No schema change; the diagnostics payload version stays at diagnostics-`v1` (unchanged; unrelated to the `cbv1`/`cbv2` binding pin).
- `timeline_events` (`VERIFY_EVALUATION` rows): no new event type; the existing persisted-evaluation rows carry the authoritative partitions for battery measurement.

## Data Structures

Verify-time inputs reuse the persisted G3 shapes (never a parallel model). `build_claim_ledger()` output — the `(ClaimRetrievalLedger, ClaimRetrievalBinding, unknown)` triple — is the authoritative partition; `evaluate_hypothesis` and the conclude path consume it directly with caps, `omitted_*` counters, `claim_ref`, and `target_ref` intact. The only new Phase 1 structure is the HISTORY subject input (separate input model, never reused as output):

```python
# src/investigation_agent_platform/application/investigation/verification.py
# (proposed; HISTORY subject input only — partitions reuse ClaimRetrievalBinding)
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class HistorySubjectInput(BaseModel):
    """HISTORY subject comparison input (structured, never freeform prose)."""

    model_config = ConfigDict(frozen=True)

    claimed_subject: str | None = Field(default=None, max_length=512)
    stored_summary: str | None = Field(default=None, max_length=1024)
    target_file: str = Field(max_length=512)
    target_commit: str = Field(max_length=64)
    investigation_id: UUID
```

## Interfaces

No user-facing interface changes in Phase 1. REST, CLI, MCP, and report shapes are unchanged. Operator-visible effects are confined to diagnostics already surfaced in `loop_telemetry` (new `evaporated`/`repaired` counts stay zero for repair in Phase 1, `unmapped` appears on legacy rows) and to sharper `next_check` directives (rival, then relation, then unmapped/evaporated, then provider).

| Method | Path | Request | Response | Description |
| ------ | ---- | ------- | -------- | ----------- |
| — | — | — | — | No API change in Phase 1 |

| Command | Arguments | Description |
| ------- | --------- | ----------- |
| — | — | No CLI change in Phase 1 |

## Implementation Detail

The implementer applies, in order:

1. `domain/hypothesis/models.py`: add `ClaimTarget.commit_subject` and `HypothesisEvidenceAssessment.commit_subject` as additive optional fields with max lengths above. No validator changes; frozen models stay frozen.
2. `application/investigation/verification.py`: add `_COMMIT_TYPE_PREFIX`, `_SUBJECT_TOKEN_THRESHOLD` (0.70), `_canonical_subject`, `_subject_tokens`, `subjects_match`, `subject_match_outcome`, `HistorySubjectInput`, `resolve_claim_binding_version` plus `CLAIM_BINDING_CBV1/CBV2/CURRENT` constants (per D4 with typed errors), `UNMAPPED_CITATION` + `EVAPORATED_CITATION` `LimitationCode` members, `ClaimBindingEnforcement` (with per-group `local_groups`/`cited_groups`, kind rollups `local_kinds`/`cited_kinds`/`unmapped_kinds`, `linkage_observed`, `fault`), `bound_kept_evidence_only`, `repair_citation` + `_paths_match_for_repair` (dead code, Phase 3-owned contract, no production callers by test), `_fault_inconclusive`, and `target_group_key` in `retrieval_ledger.py` (shared by grouping and verification). Branch `history_proof` and `validate_history_claim` on the resolved pin: `cbv1`/unmarked keeps the byte-identical legacy HISTORY predicate; `cbv2` requires non-empty `target.commit_subject` and gates matching rows on `subjects_match`. Partition per distinct typed claim via `_distinct_typed_claims` (whose groups now carry assessment `evidence_id`s) plus per-group `build_claim_ledger` linkage; thread the kept set through `evaluate_hypothesis` (filter before inner evaluation, kind-scoped refusal, UNMAPPED limitation, zero-kept INCONCLUSIVE, fault INCONCLUSIVE), `check_claim_kind` dispatch, the five activity-site `validate_*` fast paths (plus engine-internal java-fact dispatch), `causal_step_grounding` (which runs over the filtered set), and the shared `evaluate_caller_edge` seam so kept IDs are the only IDs kind gates see. Emit `EvaporatedId`, `Demotion`, and `MissingObligation` on the authoritative path and route `next_check_for` after the existing rival/relation checks.
3. `application/worker/activities/verification.py` and `lifecycle.py`: resolve via `resolve_claim_binding_version` at EVERY verify and conclude entry path (conclude gains the resolution it lacks today; no-repository degrades to `cbv1` with loud provenance); fail closed on unknown/unreadable pins with the typed errors; replace the silent `_resolve` drops with `bound_kept_evidence_only`; gate Tranche B `statement_range_reads` inclusion to claim-local-linked or explicitly cited rows under `cbv2`; pass kept IDs (not raw cited sets) into every `validate_*` call, `evaluate_caller_edge`, and causal grounding; route taken fast-path verdicts through `enforce_claim_binding` (single choke point per activity); block the `ConclusionGate` path on zero kept, on fault enforcement, and on unmapped-only without a causal chain; construct fault enforcements (never `None`) when the filter step itself faults. `application/investigation/services.py` (disposition create), `application/knowledge/intake.py`, and `api/dev_seed.py` stamp `claim_binding_version: cbv1` at creation.
4. `infrastructure/persistence` repositories: no column change; confirm `assessments_json` round-trips the new optional `commit_subject` and investigation metadata round-trips `claim_binding_version`.
5. `scripts/agent_metrics.py`: add `evaporated_rate`, `unbound_rate`, per-kind `VERIFIED/LIKELY/INCONCLUSIVE` split, and `history_subject_match_rate` (exact vs token-set vs mismatch) series from persisted `VERIFY_EVALUATION` rows (read-only battery analysis, no runtime behavior).
6. Telemetry wiring (D5): add `record_claim_binding`, `record_history_subject`, and `record_binding_version` to `ObservabilityPort` plus matching OTel counters in `OpenTelemetryObservabilityAdapter`; extend `_KNOWN_LIMITATION_CODES` in `application/worker/activities/common.py` with `NO_RUNTIME_OBSERVATION`, `RIVAL_SHARED_EVIDENCE`, `UNMAPPED_CITATION`, and `EVAPORATED_CITATION` (coerce-and-count `other`, never drop the signal); extend `_emit_verify_telemetry` to emit per-claim binding dispositions, per-HISTORY subject outcomes, and per-turn binding versions via the existing `_emit_telemetry` fire-and-forget helper; stamp bounded `claim_binding_kept/evaporated/unmapped/version` counts into `loop_telemetry`. No file path, line range, SHA, subject, or statement text ever enters a label.

Repair scaffolding lands as dead code behind the pin: a proposed `repair_citation(claim_kind, target, cited_row, donor_row)` helper with the same-file, same-revision, kind-grounded predicate exists but no production caller references it (pinned by test). It never runs while the pin resolves to Phase 1 state. Phase 3 wires it under its own sub-gate. Faults fail closed: a partitioning fault under `cbv2` yields a fault enforcement that callers turn `INCONCLUSIVE` (never the legacy bank path); `enforce_claim_binding` faults yield `INCONCLUSIVE` rather than returning the pre-enforcement verdict.

`loop_telemetry` additive keys (workflow-accumulated from verify result data, then stamped by `_derive_loop_telemetry`): `claim_binding_kept`, `claim_binding_evaporated`, `claim_binding_unmapped` (bounded counts), `claim_binding_version` (closed `cbv1|cbv2` label). Verify result data carries the per-turn counts under `cbv2` only, so legacy result shapes stay byte-identical.

VERIFY_EVALUATION marker additive keys: `verification_status`, `verification_tier`, `claim_binding_version`, and (HISTORY-kind turns only) `history_subject_outcome` (one of `matched_exact|matched_token_set|mismatched_subject|missing_subject|missing_row`, recomputed deterministically from the corroborating rows). The agent-metrics battery reads per-kind `VERIFIED_ESTABLISHED|VERIFIED_LIKELY|...` splits and the subject outcome distribution from these keys with numerator-plus-denominator reporting.

## Migrations

No migration in Phase 1. Creation order is code-only: domain models, then verification engine, then activity plumbing, then metrics. Compatibility notes: old `assessments_json` rows without `commit_subject` validate unchanged; HISTORY claims on such rows take the unmapped bank path exactly like today and cannot reach the structured-subject rule until reformulated. Old investigation metadata without `claim_binding_version` resolves to `CLAIM_BINDING_CURRENT` (`cbv1` during rollout — byte-identical current behavior) under the documented migration policy. Repointing `CLAIM_BINDING_CURRENT` to `cbv2` requires a separate owner amendment with its own entry/exit evidence, never a silent constant change.

## Testing Philosophy

### Authoritative partition and demotion

Prove the verdict follows kept IDs only: a claim citing IDs in neither the ledger set nor the bank yields `INCONCLUSIVE` with `EvaporatedId` records and a `MissingObligation`, and the hypothesis cannot `VERIFY` on that claim. Prove bank-fallback-only citations publish at most with `UNMAPPED_CITATION` and never satisfy caller completeness. Prove empty cited sets take the existing missing-obligation path rather than the evaporation path. Prove the Tranche B gate: an unknown cited ID plus an otherwise-passing bank plus a statement-matching range row still yields `INCONCLUSIVE` with the unknown ID named (the range row never substitutes for zero kept citations). Prove every activity-site `validate_*` fast path in both activities receives the filtered kept-rows-only set (entry-site enumeration test: five activity sites plus engine-internal java-fact dispatch), and that the `*_validated` gate waiver is blocked when cited-kept survival is zero or verification rested only on unmapped bank-fallback rows.

### HISTORY structured subject

Pin the canonicalization order (casefold, trim, balanced outer-quote strip, conventional-commit prefix strip, whitespace collapse) and the token-set threshold with frozen unit vectors: exact-after-canonical match; one-token subjects (single-token drift fails); two-to-three-token subjects (one-token drift fails at 0.70); generic-subject collision ("Update README" versus "Update README twice", 0.667, fails); longer-subject one-word insertion (motivating rewording, 0.75, passes); prefix strip (`fix(parser): ...` equals `...`); balanced-quote canonical order (unbalanced quotes retained in canonical form); the motivating rewording ("with a STANDARD default" versus "with STANDARD default" matches; genuinely different subjects do not). Pin newest-commit-wins against multi-commit fixtures: a matching older commit with a non-matching newest commit fails closed. Prove `cbv1` rows keep the byte-identical legacy predicate (statement-substring check intact) while `cbv2` rows require the structured subject.

### Caller-edge completeness on kept IDs

Extend the Step 5 adversarial battery: aggregate enumeration with one grounded caller and one ungrounded stem mention publishes only the grounded caller and records the missing-caller obligation; wrong-revision sibling rows never satisfy completeness; construction-only and comment-line rows evaporate with the caller-window next action. Prove no cross-claim borrowing: a CALL_EDGE claim citing row A plus a DEFINITION claim citing row B, with only B's fetch recorded, keeps the CALL_EDGE group unmapped (DEFINITION verifies; CALL_EDGE does not borrow B's linkage). Prove the same-kind case: two CALL_EDGE targets sharing the callee, only the first retrieved — the second target is not local and the aggregate cannot verify as complete. Prove no query-text borrowing: same derived query on both targets, attempt banking only the first row — the second target stays unmapped. Prove the positive control: separate linked rows per target verify. Prove the activity choke points: the two-caller shape through the real verify fast path (no VERIFIED, unlinked caller absent) and through the conclude gate (abstention, unlinked caller demoted from proven to unresolved). Prove the kind-scoped refusal: CALL_EDGE/HISTORY bank-fallback with observed linkage and a cited-but-unmapped group is `INCONCLUSIVE`; unobserved linkage keeps the bank path with `UNMAPPED_CITATION`. Prove fault-closure in enforce itself (fault enforcement on a VERIFIED input yields `INCONCLUSIVE` with empty corroborating) and that enforce never raises. Prove the waiver: unmapped-only without a causal chain forces abstention; with a chain the conclusion publishes with the limitation.

### Version pin and fail-closed behavior

Pin the resolver matrix (absent resolves to `CLAIM_BINDING_CURRENT` unmarked — `cbv1` during rollout; `cbv1`/`cbv2` resolve pinned; unknown pins raise `UnknownVerifierVersion`; unreadable metadata raises `VerifierVersionUnavailable`), the creation-path `cbv1` stamping (intake, dev-seed, disposition service), the no-repository `cbv1` degradation with its loud provenance, and the verify/conclude fail-closed denial on both typed errors. Enumerate every verify/conclude entry path in a test and prove each resolves the pin. Prove unmarked historical rows keep current behavior byte-identically. Prove fault-closure: a partitioning fault under `cbv2` yields `INCONCLUSIVE` (never the legacy bank path), and the enforce tail is load-bearing (disable it and the zero-kept claim loses its evaporated/demotion records). Prove `linkage_observed` gating: CALL_EDGE/HISTORY refusal and the unmapped-only waiver block fire on observed absence only; unobserved linkage keeps the bank path with its limitation.

### Telemetry completeness

Every new Phase 1 path has a counter, a loop_telemetry key, and a battery series, each asserted by a test. Closed-vocabulary enforcement is tested by passing an out-of-vocabulary label and proving it is coerced or rejected, and by asserting no file path, SHA, subject, or statement text reaches any label. Emission-failure tests prove a raising adapter never breaks verification (fire-and-forget). Content tests assert the emitted closed labels end to end (per-claim dispositions, version stamp with source, HISTORY subject outcome recomputed from corroborating rows). Marker tests assert the additive VERIFY_EVALUATION keys (`verification_status`, `verification_tier`, `claim_binding_version`, `history_subject_outcome`); the battery series tests assert per-kind verdict splits and subject-outcome distributions with numerator-plus-denominator reporting. The repair contract is pinned (same-file/revision/grounded repair, cross-file/cross-revision/unknown-kind refusal) with a no-production-caller test. Causal characterization pins that steps cite by construction (ghost-cited steps fail grounding, then evaporate with the CAUSAL directive).

### Regression and invariants

Run the focused caller-edge, verification, and G3 suites plus the full unit suite. Assert `SUPPORTED` with zero kept citations is unreachable by construction (negative control: disable the tail enforcement call and watch the test fail). Assert no silent drops: every unknown ID appears in `evaporated_ids` with a reason code.

## Documentation Plan

### `docs/IAP-L2-D3-remediation-plan.md`

**Audience:** Owner and auditors

Append a Phase 1 record under the remediation log: scope (authoritative partitions + demotion + `commit_subject` + pin), entry evidence (this design plus its adversarial review), exit evidence (test tallies, ruff, mypy, scratch-tenant spot check), and the explicit statement that no freeze, run-set, budget, authority, or merge was consumed. No tmp/ path or filename appears.

### `docs/IAP-L2-variance-diagnosis-problem.md`

**Audience:** Engineers diagnosing L2 variance

Add an as-built note in the caller-edge section: completeness now consumes kept IDs only, with the missing-caller obligation shape and the bank-fallback limitation. Include the negative controls (wrong-revision, construction-only, comment-line) so future readers know what honest abstention looks like.

### `docs/IAP-D3-definition-convergence-problem.md`

**Audience:** Engineers working the D3 line

Add an as-built note: binding observations now carry the claim-binding version alongside behavior/protocol/amendment identities; receipt envelopes reject mismatches per the pin. No change to the frozen D3 protocol itself.

## Risks / Trade-offs

### Stricter binding converts borderline answers to abstentions

**Risk:** Claims that previously verified on bank-fallback rows now yield `INCONCLUSIVE` with `UNMAPPED_CITATION`, reducing `ANSWERED` counts on shapes that were arguably correct but unbound.

**Mitigation:** The limitation code distinguishes "unbound but plausible" from "wrong"; Phase 2 ledger plumbing and Phase 4 smallest-fetch forcing convert these into targeted recoveries rather than permanent losses. Battery tracks false-accept count separately so the trade is measured, not assumed.

### Structured subject requires reformulation of legacy HISTORY claims

**Risk:** Old hypotheses without `commit_subject` cannot verify HISTORY until reformulated, which looks like a regression on cached shapes.

**Mitigation:** Unmapped path preserves current behavior for legacy rows (limitation attached, no silent breakage); only new strict rows require the field. Prompt changes that populate the field land in Phase 2 with their own validator.

### Threshold tuning in subject comparison

**Risk:** The 0.70 token-set threshold may be too strict (reworded subjects fail) or too loose (different subjects pass) on real commit corpora.

**Mitigation:** Threshold and prefix list are frozen constants with unit pins (operating point measured during implementation: motivating rewording 0.75 passes, generic collision 0.667 fails); the scratch-tenant spot check plus the first shadow battery measure real-subject behavior before any enforcement widening. Any change requires a design amendment, not a quiet constant edit.

### Pin plumbing spread across activities

**Risk:** `claim_binding_version` must be stamped, propagated, and resolved in every verify/conclude entry path; a missed path silently runs the wrong semantics.

**Mitigation:** Single resolver, single stamp site, fail-closed on unknown, and a test that enumerates the entry paths. The pin rides the existing `verifier_version` plumbing rather than inventing a parallel channel.

## Implementation deltas — owner review A–N (2026-10-27, commits 17e9127 + J set)

- **A → D2:** `commit_subject` gained a sanitizer (strip/normalize before match) and the prompt moved to `v3-claim-grounded-018`, which instructs the structured subject form. Substring fragility is addressed at both ends: producer (prompt) and consumer (sanitizer).
- **B → D1:** scope-attempt linkage is a single shared predicate, `linked_scope_attempted()`, called from both the verify and conclude paths — never a forked copy.
- **C → binding scope:** `_scope_path_prefix()` now resolves at the SEARCH_CODE site, so scope prefixes attach where the retrieval action actually runs.
