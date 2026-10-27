# Design: Claim binding Phase 3 — misattribution repair behind its sub-gate

## Context

Phases 1–2 (unmerged on `iap-full-1025-claim-binding`) made claim-local resolution authoritative and the per-claim retrieval hint stored: a `DEFINITION` claim verifies on its claim-local range read, bank-fallback-only citations verify with `UNMAPPED_CITATION`, zero kept citations yield `INCONCLUSIVE` with named `evaporated_ids`, and a never-retrieved claim dispatches via trigger (c). What still fails honestly: the model cites the right file at the right revision but the wrong row — typically a `SEARCH_CODE` hit row instead of the `GET_CODE` range read that shows the symbol. Both rows are banked, so no evaporation fires; the kind gate (`definition_window_proof`, `history_proof`) fails on the cited hit row while a sibling donor row in the bank would pass. Today that claim is `INCONCLUSIVE` and the turn is spent.

Phase 1 landed `repair_citation()` + `_paths_match_for_repair()` as dead code with a no-production-callers pin test: same file path, same revision, kind-grounded (`DEFINITION` via `definition_window_proof`, `HISTORY` via `history_proof`, all else `False`), fail-closed on any exception, with content equality explicitly left to the caller. Phase 3 wires that predicate as gate-failure recovery behind its own sub-gate: a new `cbv3` pin value enables applied repair, `cbv2` rows get shadow-only computation (decisions logged, verdicts unchanged) to prove zero over-rebind before any row is ever stamped `cbv3`, and `cbv1` rows are untouched.

No `docs/dev/architecture.md` exists in the project.

## References

- [IAP-claim-binding-phase1-design.md](./IAP-claim-binding-phase1-design.md) — Phase 1 authoritative partitions, `cbv1`/`cbv2` pin, dead `repair_citation` contract (§D4, implementation step 7), and the explicit Phase 3 non-goal this design implements.
- [IAP-claim-binding-phase2-design.md](./IAP-claim-binding-phase2-design.md) — Phase 2 stored query pair, `CLAIM_UNRESOLVED` coverage, directive trigger (c), `query_source`/`link_source` closed vocabularies whose series this phase extends with repair outcomes.
- [IAP-proof-kernel-g3-design-v1.0.md](./IAP-proof-kernel-g3-design-v1.0.md) — G3 `VERIFY_EVALUATION` persistence and directive state machine whose event shapes this phase reuses without new event types.
- [IAP-L2-D3-remediation-plan.md](./IAP-L2-D3-remediation-plan.md) — Remediation operating rules (review + explicit permission every step; no freeze/run-set/budget/authority/merge without owner approval) and the Phase 1–2 records this builds on without reopening.
- Current tree sources read during design: `application/investigation/verification.py` (`repair_citation` :822-851, `_paths_match_for_repair` :854-879, `ClaimBindingEnforcement` :723, `bound_kept_evidence_only` :793-819, `_claim_binding_partition` :5006-5180, `evaluate_hypothesis` :5214-5297, `enforce_claim_binding` :5322-5492, `_evaluate_hypothesis_inner` :5493, `resolve_claim_binding_version` :1367 with constants :1358-1364, `collect_verification_diagnostics` :537 onward (evaporation/demotion/bindings :639-716), `history_proof` :3209 with pin default :3212 and strict rule :3224, `_paths_match` normalized suffix matcher :3684-3699 vs raw `_item_file` :2323-2325, `ConclusionGate.evaluate` :5925, `LimitationCode` :369, `EvaporatedId` :424, `Demotion` :437, `MissingObligation` :453, `VerificationDiagnostics` :468), `application/evidence/retrieval_ledger.py` (`build_claim_ledger` :632, `re_retrieval_directive` :852 with eligibility :884-899, `directive_consumed`, `recorded_retrieval_attempts` :913), `application/worker/activities/verification.py` (fast-path enforce :858-876, directive producer :1019-1069) + `lifecycle.py` (conclude filter :1111-1128, waiver block :1299-1326, fast-path enforce :1386-1402), `ports/observability/telemetry.py` (:108-129), `infrastructure/observability/telemetry.py`, `application/worker/activities/common.py` (`_KNOWN_LIMITATION_CODES` :439-452, `_emit_verify_telemetry` :507), `scripts/retrieval_benefit.py` (:121, :601), `tests/unit/test_claim_binding_phase1.py` (`TestRepairCitationContract` :211, no-production-callers pin test :260-276), stamp sites (`intake.py` :407-412, `dev_seed.py` :130, `services.py` :245).

## Goals / Non-Goals

**Goals:**

- Repair misattributed citations (right file@revision, wrong row) as gate-failure recovery, behind a dedicated `cbv3` pin value
- Shadow-first rollout: `cbv2` rows compute repair decisions diagnostics-only; applied repair requires `cbv3`, and no row is stamped `cbv3` until shadow proves zero over-rebind
- Caller-side content-equality rule (the gap Phase 1 left open), fail-closed
- `RepairedBinding` diagnostics records + `REPAIRED_CITATION` limitation + closed-vocabulary repair telemetry and battery series
- Full unit suite green, ruff clean, no new mypy errors on touched paths, scratch-tenant live spot check (conditional on separate owner approval per the remediation plan — this design does not authorize the run)

**Non-Goals:**

- Repairing evaporated ghost citations (unknown IDs with no banked row stay `INCONCLUSIVE` + requery directive — there is nothing to prove what a ghost meant)
- Repair for `CALL_EDGE`/`BEHAVIOR`/`ABSENCE`/`CAUSAL`/untyped claims (predicate returns `False`; unchanged)
- Cross-file, cross-revision, or unscoped repair (predicate refuses; unchanged)
- Smallest-fetch forcing rules and `NOT_CHECKABLE` denominator change in `ConclusionGate` (Phase 4)
- Repointing `CLAIM_BINDING_CURRENT` (stays `cbv1`) or changing creation stamps (stay `cbv1` except the D4 owner-approved one-way shadow backfill, which stamps named investigations `cbv2` and is deleted after use)
- Any freeze, run-set, budget, rollout, authority, or merge decision (separate owner approvals required)
- Changes to served-cached conclusion handling or historical artifact semantics

## Decisions

### D1: Repair is gate-failure recovery for banked wrong-row citations only

**Decision:** Repair fires only when all of the following hold for one `(kind, target)` group: the group kind is `DEFINITION` or `HISTORY`; the group's kind gate explicitly fails — the `(False, reason)` result from `definition_window_proof`/`history_proof` run on that group's cited rows for that group's target (never inferred from verdict status alone); at least one cited banked ID of that group (the candidate) fails the gate; a donor row exists in the investigation bank such that `repair_citation(kind, target, candidate, donor)` is `True` (same file, same revision, donor passes the kind gate); and the caller-side content rule (D2) holds between candidate and donor. Candidates are defined by kind-gate failure, NOT by the partition's kept status: a banked hit citation is typically already kept (via claim-local or admitted bank-fallback — `kept = local | fallback | step_kept`), and the motivating wrong-row citation is exactly such a cited-but-kept row. "Banked but not kept" from the earlier draft is therefore wrong and is replaced everywhere by "cited, banked, and kind-gate-failing for its own group's target". Evaporated citations (unknown IDs, no banked row) are explicitly out of scope: with no candidate row there is nothing to compare, and guessing what a ghost citation meant would launder fabrication into verification. Those keep the Phase 1 path (`INCONCLUSIVE` + `MissingObligation` + requery directive via trigger (a)).

```python
# src/investigation_agent_platform/application/investigation/verification.py
# (proposed; called from the repair sites in D6, never from kind gates)
def attempt_claim_repair(
    claim_kind: str | None,
    claim_key_value: str,
    target: Any | None,
    candidate_ids: list[str],
    cited_ids: set[str],
    evidence_by_id: dict[str, Any],
    binding_version: str,
    gate_reason: str = "",
    max_repairs: int = 3,
) -> tuple[list[str], list["RepairedBinding"]]:
    """Try to rebind misattributed citations to donor rows (pure).

    candidate_ids are cited, banked IDs that failed THIS group's kind
    gate for THIS group's target (computed by the caller running the
    kind gate per group — kept status is irrelevant; the motivating
    candidate is kept via bank-fallback). claim_key_value is the
    caller's existing claim_key() over the group's statement+kind
    (from the _distinct_typed_claims group entry); target is the
    group's ClaimTarget/mapping for kind-grounding. Scans banked rows
    cited by NO group of the hypothesis as donors in sorted
    evidence-ID order (deterministic, not insertion order); the first
    donor passing repair_citation() under binding_version plus
    _content_matches_for_repair() rebinds that citation. Returns
    (rebound_ids, repairs). Never raises; never touches unknown
    (evaporated) IDs; caps at max_repairs rebinds per call. Pure
    function; callers own records and verdicts. binding_version is a
    pure argument (never persisted entity state) selecting the HISTORY
    strictness inside the predicate (cbv2 AND cbv3 enforce the strict
    rule — see D3) and stamping each repair's outcome/binding_version
    (cbv2→shadow, cbv3→applied). Each repair's claim_ref is derived
    via the existing claim_ref() over claim_key_value; target_ref and
    the inline target mapping via the existing target_group_key()
    over target; gate_reason is recorded as passed. Review-only fields
    all, none used in any decision.
    """
```

Repair iterates `_distinct_typed_claims(hypothesis)` groups — one `(kind, target)` group at a time, groups with kind `DEFINITION`/`HISTORY` only — and never uses hypothesis-level kind. `effective_claim_kind` reads singular `hypothesis.claim_kind` with phrasing-inference fallback: hypothesis-level granularity that would apply one claim's kind/target to another claim's cited IDs. The repair loop instead takes each group's own `kind_value`, `target_dict`, and `cited_ids`; a donor that grounds group A may also serve group B, but only through B's own gate run on B's target with B's record (reuse rule below). Multi-claim same-hypothesis fixtures pin the authorization boundary: a donor passing B's gate but failing A's gate never repairs A, and A's candidates are never evaluated under B's kind/target.

The scan order is deterministic (sorted evidence-ID order, not dict-insertion order) and the first qualifying donor wins — no scoring, no model judgment. `max_repairs=3` bounds the blast radius per group; the fourth misattributed citation stays failed. Donors are banked rows cited by NO group of the hypothesis — never cited IDs of the same group (no self-repair loops), and never rows cited by another group (no citation borrowing: a repair never re-points at evidence another claim already relies on as cited). Cross-group donor REUSE is explicitly allowed and does not violate isolation, because the isolation guarantee is about authorization, not row identity: one uncited range read may ground two DEFINITION claims (different symbols, same file), and each use is independently gated on that group's own kind/target/gate with its own `RepairedBinding`. What never crosses groups is authorization — group A's candidates are never evaluated under group B's kind, target, or gate result, and a donor passing B's gate but failing A's gate never repairs A. Groups are processed in sorted group-ID order so multi-group repair sets are reproducible for shadow review. A Tranche-B-joined uncited range row IS an eligible donor — it is the exact sibling-row shape of the motivating example (hit cited, range read banked but uncited). This does not violate the no-borrowing rule: that rule governs locality of citations ACROSS claims (Phase 1 `_claim_binding_partition` identity-overlap), while donor eligibility is WITHIN one group over same-file rows.

The pin threads into the predicate: `repair_citation` gains a `binding_version` argument forwarded to `history_proof` (which defaults to `CURRENT` and only enforces the strict subject rule under `cbv2`), so the donor faces the same HISTORY strictness the verdict enforces — a donor passing only the lax legacy path can never repair under `cbv2`-shadow/`cbv3`. `DEFINITION` needs no threading (`definition_window_proof` has no pin branch). The Phase 1 predicate body is otherwise unchanged; `_paths_match_for_repair` is amended from raw `_item_file` equality to the normalized `_paths_match` suffix matcher the kind gates use (locator-qualified vs repo-relative pairs must agree with the gate, or repair systematically misses exactly the rows the gate accepts). Revision comparison stays exact string equality (SHAs/tags compare exactly). Both amendments are pinned by tests, including a locator-qualified-vs-repo-relative repair vector.

The trigger is narrow: repair is attempted only on an explicit kind-gate failure result for the group — the caller runs `definition_window_proof`/`history_proof` over the group's cited banked rows for the group's target and proceeds only on `(False, reason)`, recording the reason onto the repair. `INCONCLUSIVE` with corroborating empty is necessary but NOT sufficient: concession-only abstentions, orientation-only abstentions, and any other upstream failure mode never produce a kind-gate `(False, reason)` for the group and therefore never reach the helper. Contradictions, refuting rows, and rival blocks additionally veto even a gate-failed group (checked before the helper runs). The shadow list stays review-sized by construction: only groups whose own kind gate named its failure.

**Alternative considered:** Repair evaporated ghost IDs by matching the stored query against banked rows. Rejected — a ghost ID carries no content, so any donor "match" is a guess about the model's intent; the honest response to a ghost citation is re-retrieval (Phase 2 trigger (a)), never silent substitution.

---

### D2: Content equality is normalized snippet containment or line overlap, fail-closed

**Decision:** The caller-side rule Phase 1 left open is defined as follows (pure, never raises, any exception → no match): the donor's normalized snippet must contain the candidate's normalized snippet (non-empty) — containment is REQUIRED in all cases. Line-range overlap is a secondary signal, never sufficient alone: a shared line or two broad ranges over different code does not establish the donor contains the candidate's content. When both rows carry ranges AND they overlap AND containment holds, the basis records `overlap_and_containment`; containment without usable ranges records `snippet_containment`. Normalization is case-sensitive strip + single-space collapse (code symbols are case-sensitive; no casefold); ranges come from `source_location.line_start/line_end` when both rows carry them.

```python
# src/investigation_agent_platform/application/investigation/verification.py
# (proposed; the "content equality checked by the caller" from Phase 1)
def _content_matches_for_repair(
    candidate_row: Any, donor_row: Any
) -> Literal["snippet_containment", "overlap_and_containment"] | None:
    """Donor shows (at least) what the candidate showed (pure).

    Returns "snippet_containment" when the normalized donor snippet
    contains the normalized candidate snippet (non-empty);
    "overlap_and_containment" when containment holds AND both rows
    carry line ranges that overlap. Otherwise None (including
    overlap-without-containment — overlap alone never matches).
    Never raises.
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
```

Rationale: a `SEARCH_CODE` hit row's snippet (the matched lines) is contained in the `GET_CODE` range read's window in the normal case, and both rows name the same file region when the provider stamps ranges. Containment is the content check; overlap corroborates it but proves nothing alone. Hit rows frequently carry snippets but no `_item_window` ranges — for exactly the motivating hit-vs-read pair the overlap signal never fires, so `snippet_containment` is the primary basis; tests pin a windowless-hit repair via containment and an overlap-without-containment refusal (shared single line, disjoint snippets). Truncation direction matters and is asymmetric: a truncated DONOR contains less, so donor truncation can only cause a miss (fail-closed). A truncated CANDIDATE is shorter and hence easier to contain — the containment signal weakens. This is contained, not fatal, because containment operates only among kind-grounded donors: the donor must independently pass `definition_window_proof`/`history_proof` for the target in the same file@rev, so a spurious containment still requires a grounding row. Tests pin both-truncated, candidate-truncated-only, and windowless-hit vectors explicitly.

**Alternative considered:** Exact snippet equality. Rejected — hit rows and range rows legitimately differ in window (a 3-line match vs a 20-line read); exact equality would repair almost nothing while adding no safety over containment (containment is already gated by same-file+same-revision+kind-grounded).

---

### D3: `cbv3` pin enables applied repair; `cbv2` runs shadow-only; `cbv1` untouched

**Decision:** A third pin value `CLAIM_BINDING_CBV3 = "cbv3"` (repair-enabled) joins `KNOWN_CLAIM_BINDING_VERSIONS`; `CLAIM_BINDING_CURRENT` stays `cbv1` and creation stamps stay `cbv1` except for the owner-approved one-way shadow backfill (D4) — no creation path ever emits `cbv3`. Behavior by pin: `cbv1`/unmarked — byte-identical legacy bank path (repair code never runs, not even shadow); `cbv2` — authoritative Phase 1+2 semantics plus shadow repair computation whose decisions are recorded but never applied (verdicts identical with and without the shadow); `cbv3` — exactly the `cbv2` semantics (including the strict HISTORY subject rule — the `history_proof` pin check becomes `in (CBV2, CBV3)`, never `== CBV2`) plus applied repair at the five sites. Unambiguous: a donor passing only the lax HISTORY path can never repair under `cbv3`, pinned by a dedicated regression test; the implementation plan enumerates every pin-equality touch surface so no `== CBV2` gate is left behind.

```python
# src/investigation_agent_platform/application/investigation/verification.py
# (proposed; mirrors the Phase 1 resolver pattern, no env reads)
CLAIM_BINDING_CBV1 = "cbv1"
CLAIM_BINDING_CBV2 = "cbv2"
CLAIM_BINDING_CBV3 = "cbv3"  # Phase 3: cbv2 semantics + applied repair
CLAIM_BINDING_CURRENT = CLAIM_BINDING_CBV1  # unchanged; repoint needs owner amendment
KNOWN_CLAIM_BINDING_VERSIONS = frozenset(
    {CLAIM_BINDING_CBV1, CLAIM_BINDING_CBV2, CLAIM_BINDING_CBV3}
)
```

Verify AND conclude resolve the pin at every entry path (the Phase 1 enumeration test extends to `cbv3`); unknown pins keep failing closed with the existing typed errors. The no-repository degradation stays `cbv1`. Shadow computation runs inside the same choke (`enforce_claim_binding` / conclude waiver block) so the shadow sees the exact production inputs; a `repair_shadow` flag on the enforcement selects record-only vs apply.

**Alternative considered:** A separate `repair_enabled` boolean beside the version pin. Rejected — the repo contract versions behavior on the entity, never on side flags; a boolean is an env-switch in waiting. The pin already exists for exactly this evolution.

---

### D4: Shadow must prove zero over-rebind before any `cbv3` row exists

**Decision:** Over-rebind is defined as a shadow repair whose donor a reviewer judges wrong: the donor does not ground the claim, or the candidate was actually the right row. The Phase 3 gate is operational, not aspirational: the battery gains a `shadow_repair_review` series listing every shadow repair (claim, target, candidate, donor, basis, outcome, version, gate reason) from `VERIFY_EVALUATION` rows; a human reviews every candidate in the shadow window; the gate to stamp `cbv3` opens only on a window with zero over-rebinds across at least the battery's full live-case set. The design does not fix the window size — the owner sets it at gate time — but no smaller than every live-runnable case once each, and any over-rebind resets the window. Completeness is a gate precondition and fails closed: the gate query blocks opening when any cohort marker has `repaired_truncated == true` or `len(repaired) != repaired_count + repair_shadow_count`, and every counted candidate must resolve to a recorded per-candidate disposition keyed by (hypothesis_id, turn, claim_ref, cited_id, donor_id, binding_version) — a counted-but-unlisted candidate blocks the gate exactly like an over-rebind. Recovery for a truncated marker is deterministic recompute: re-run the pure `attempt_claim_repair` per group over that turn's persisted inputs (bank rows, cited IDs, pin — all persisted state) via a battery subcommand and diff against the listed candidates; the recomputed full list is then reviewed. If recompute cannot reproduce the turn's inputs, the owner excludes that investigation from the window with sign-off (the window minimum still applies).

Shadow observations need `cbv2` rows, but creation paths stamp `cbv1` and `CURRENT` stays `cbv1` — so the design defines the one approved cohort mechanism. The shadow cohort is a bounded, owner-named set of investigations (scratch tenant plus the selected live-runnable battery cases), stamped `cbv2` by a one-way backfill script run once under explicit owner approval and deleted immediately after use — the repo contract's allowed time-boxed migration-aid exception (never a permanent path, never env-switched). The script takes explicit investigation IDs, stamps only `cbv1`/unmarked rows, refuses `cbv3`, and logs every stamp; a test pins that no creation path emits `cbv2` or `cbv3` through the normal flow (the backfill is outside the creation paths by construction). Until the gate opens, `cbv3` is unstampable in practice: no creation path, no backfill, and no script emits it, pinned by test. Flipping any stamp to `cbv3` is a separate owner amendment with its own entry/exit evidence — Phase 3 must not smuggle it, exactly as Phase 1 did not smuggle the `cbv2` repoint.

**Alternative considered:** Ship applied repair immediately behind `cbv3` with review after. Rejected — applied repair changes verdicts (`INCONCLUSIVE` → `VERIFIED`); an over-rebinding repair is a false accept, the exact failure the whole program exists to close. Shadow-first is the only order that keeps the false-accept count at zero during evaluation.

---

### D5: Repaired verdicts carry records, a limitation, and closed-vocabulary telemetry

**Decision:** Every applied or shadow repair emits a `RepairedBinding` diagnostic (claim ref, cited ID, donor ID, basis) and every applied repair attaches a `REPAIRED_CITATION` limitation to the verdict — a repaired `VERIFIED` never looks identical to a clean one. A repaired claim can still only reach `LIKELY` when other limitations demand it; repair never upgrades a tier, only rebinds the evidence the existing tier logic then judges.

```python
# src/investigation_agent_platform/application/investigation/verification.py
# (proposed; additive diagnostics, existing shapes untouched)
from typing import Any, Literal

class RepairedBinding(BaseModel):
    """One citation rebound from a misattributed row to a donor row.

    basis, outcome, and binding_version are Literal-typed (not str):
    out-of-vocabulary construction raises ValidationError. No separate
    validator enforces them; the types are the enforcement.
    """

    model_config = ConfigDict(frozen=True)

    claim_ref: str = Field(description="16-hex claim reference (never the full key).")
    claim_key: str = Field(
        default="",
        max_length=560,
        description="Full human-readable claim identity (statement|kind, "
        "same shape as ClaimRetrievalBinding.claim_key) so reviewers read "
        "the claim without joining. Redundant with the marker's bindings "
        "by design — review must not depend on a join that omission caps "
        "could break.",
    )
    target_ref: str = Field(
        default="",
        max_length=32,
        description="Target-distinctness fingerprint pairing with claim_key "
        "to join the exact (kind, target) group (same shape as "
        "ClaimRetrievalBinding.target_ref). Join key only — reviewers "
        "read the target dict below.",
    )
    target: dict[str, Any] = Field(
        default_factory=dict,
        description="The group's normalized target mapping (the target_dict "
        "from target_group_key: symbol, counterpart, file_path, line "
        "range, scope, commit_id/commit_subject for HISTORY, etc.). "
        "Model-bounded by ClaimTarget's own max_lengths; reviewers assess "
        "donor grounding against THIS, never against a fingerprint.",
    )
    cited_id: str = Field(description="The misattributed cited evidence ID.")
    donor_id: str = Field(description="The banked donor row that grounds the claim.")
    basis: Literal["snippet_containment", "overlap_and_containment"] = Field(
        description="How the donor matched the candidate (overlap alone "
        "never matches — D2)."
    )
    outcome: Literal["applied", "shadow"] = Field(
        description='"applied" (cbv3, verdict rests on the donor) or '
        '"shadow" (cbv2, recorded only, verdict unchanged). Per-record, '
        "so a mixed marker's candidates are individually identifiable "
        "and dispositions keyed by (hypothesis_id, turn, claim_ref, "
        "cited_id, donor_id, binding_version) match unambiguously.",
    )
    binding_version: Literal["cbv2", "cbv3"] = Field(
        description="Repair never runs under cbv1, so it never appears. "
        "Redundant with outcome by design (shadow⇔cbv2, applied⇔cbv3) — "
        "the version keys the disposition, the outcome states what the "
        "reviewer is looking at.",
    )
    gate_reason: str = Field(
        default="",
        max_length=512,
        description="The kind-gate (False, reason) that authorized this "
        "repair attempt. Review input, never a decision input.",
    )
```

```python
# LimitationCode gains exactly one member (additive, frozen enum discipline):
# Phase 3 (claim binding): a cited row was rebound to a same-file,
# same-revision donor that grounds the claim; the verdict rests on the
# donor, not on the cited row as written.
REPAIRED_CITATION = "REPAIRED_CITATION"
```

Telemetry (closed vocabularies, fire-and-forget, mirroring D5 of Phase 1): new port method `record_claim_repair(context, claim_kind, basis, outcome, binding_version)` where `claim_kind` reuses the Phase 1 closed set, `basis` is `snippet_containment|overlap_and_containment`, `outcome` is `applied|shadow|refused` (`refused` is counter-only — the helper ran and found no donor, so no record exists; records carry only `applied|shadow`), `binding_version` is `cbv2|cbv3` (repair never runs under `cbv1`, so it never appears — every record carries both fields per D5, so mixed markers disambiguate per candidate); counter `claim.binding.repairs`; `loop_telemetry` additive keys `claim_binding_repaired`, `claim_binding_repair_shadow`; `VERIFY_EVALUATION` marker keys `repaired` (bounded `RepairedBinding` dumps, cap 10 per marker — the review input), `repaired_truncated` (bool, true when repairs exceeded the cap and the list is partial), `repaired_count`, `repair_shadow_count`; battery series `repair_rate` (applied repairs over checkable claims, numerator + denominator) and `shadow_repair_review` (every shadow candidate with claim/candidate/donor/basis/gate_reason for human review, never a bare rate). The cap can hide candidates, so the gate fails closed on it (D4): a truncated marker blocks the gate until its full candidate list is recovered and reviewed.

**Alternative considered:** Silent rebind with no limitation. Rejected — the operator must be able to distinguish "cited correctly" from "cited wrongly but recovered" in every report and battery split; silent repair would corrupt the false-accept accounting the program measures.

---

### D6: Repair is wired at five enumerated sites, not one choke

**Decision:** There is no single enforcement choke: `enforce_claim_binding` only filters already-decided corroborating IDs and never re-executes kind gates, and verify/conclude fast paths bypass the engine entirely (the code says so itself). Repair therefore ships as a re-evaluate loop at each of the five sites that can produce a kind-gate verdict, sharing one pure helper. The Phase 1 pin test (`test_no_production_caller_in_phase1`) is updated, not deleted: it enumerates these five sites and asserts each sits behind the pin gate, with no ungated path.

The five sites:

1. Engine `evaluate_hypothesis` (:5214-5297): after kind-gate evaluation yields `INCONCLUSIVE`, iterate the hypothesis's `_distinct_typed_claims` groups with kind `DEFINITION`/`HISTORY` in sorted group-ID order; for each group whose kind gate explicitly returns `(False, reason)` on the group's cited banked rows (and no contradiction/refuting/rival veto), run `attempt_claim_repair` with that group's kind/target/candidates, substitute donors into the working set, and re-run the kind gate once for that group. One loop per group only — a repaired-then-still-failing group does not loop again. Donor pool excludes every group's cited IDs (no citation borrowing); a donor already used by an earlier group in the same pass MAY serve again, gated afresh on the later group's kind/target with its own record (D1 reuse rule).
2. Verify-activity fast-path enforce (:858-876): fast paths return `VERIFIED` without engine enforcement, so repair wraps the fast-path validate call — per `_distinct_typed_claims` group with kind `DEFINITION`/`HISTORY`, on explicit kind-gate `(False, reason)` with no veto, substitute and re-validate once, behind the same pin/shadow split.
3. Conclude bound-set filter (`lifecycle.py` :1111-1128): same per-group wrap-and-revalidate for conclude's working set.
4. Conclude waiver block (`lifecycle.py` :1299-1326): the waiver predicate is amended so a cited ID with a successful rebind counts as kept — the rebound donor stands in for the citation in `cited_kept_count` and the `unmapped`-only refusal. Without this, every `cbv3` repair still abstains at conclude: the donor is non-cited, so `bound_kept_evidence_only` passes it through as context and the count stays zero. `ClaimBindingEnforcement.repaired_ids` feeds the amended predicate; the amendment is pinned by a conclude-publishes-with-repair test plus a shadow-still-abstains test.
5. Conclude fast-path enforce (`lifecycle.py` :1386-1402): same per-group wrap as (2) for conclude's fast paths.

The pin test asserts: (1) all `repair_citation` references outside its `def` live in these five sites and their direct pure helpers; (2) `attempt_claim_repair` has no caller outside a pin-resolved site; (3) a `cbv1` run over a repairable fixture repairs nothing and emits no repair records (shadow included — absence of records plus an execution counter, since absence alone also matches a silently-skipped path).

**Alternative considered:** Wire repair only into `enforce_claim_binding` and claim the single choke. Rejected — factually false: the choke never re-executes kind gates (status is decided upstream) and fast paths bypass it, so that wiring could never flip a verdict and would leave fast-path verdicts unrepaired.

**Alternative considered:** Keep the zero-caller test and put repair in a new module it doesn't scan. Rejected — gaming the test's scan path instead of its intent; the intent is "no ungated repair", and the test should say that directly.

## Data Storage

No new table and no migration in Phase 3. All Phase 3 state rides existing JSON columns:

- `investigations.metadata` (JSON): gains no new key — `claim_binding_version` takes the new value `cbv3` (stamped only after the Phase 3 gate opens via separate owner amendment; until then creation paths keep stamping `cbv1`). Readers resolve via the existing resolver extended with the new known value; unknown values keep failing closed with the typed errors.
- `hypotheses.assessments_json`: unchanged (no new fields; repairs rebind IDs, they don't add claim metadata).
- `timeline_events` (`VERIFY_EVALUATION` rows): no new event type; the persisted `claim_ledgers`/`claim_bindings` shapes are unchanged; `VerificationDiagnostics` gains the additive `repaired` list (defaults preserve old payloads — no diagnostics version bump, unrelated to the `cbv*` binding pin), serialized into markers as the bounded `repaired` dumps plus `repaired_count`/`repair_shadow_count` keys.
- `ActionOutcome.retrieval_attempt`: unchanged.

## Data Structures

Inputs and outputs stay separated. The only new structures are the `RepairedBinding` diagnostic (output-only, above) and the repair-attempt inputs the pure helper takes (plain mappings, never persisted):

```python
# src/investigation_agent_platform/application/investigation/verification.py
# (proposed; input side — the working-set view the helper joins)
# attempt_claim_repair() takes:
#   claim_kind: str | None        — the GROUP's kind (DEFINITION/HISTORY gate inside;
#                                  never hypothesis-level effective_claim_kind)
#   claim_key_value: str          — the GROUP's claim identity: caller-computed
#                                  via the existing claim_key() over the group
#                                  entry's statement + kind_value
#   target: mapping               — the GROUP's ClaimTarget for kind-grounding
#   candidate_ids: list[str]      — the GROUP's cited banked IDs that failed the
#                                  group's kind gate (kept status irrelevant)
#   cited_ids: set[str]           — every ID cited by ANY group of the hypothesis
#                                  (donor pool = banked rows outside this set)
#   evidence_by_id: dict          — bank rows by ID
#   binding_version: Literal["cbv2", "cbv3"] — pure arg selecting HISTORY
#                                  strictness (D3) AND stamping each repair
#                                  (cbv2→shadow, cbv3→applied)
#   gate_reason: str = ""        — the (False, reason) authorizing this attempt
#   max_repairs: int = 3         — per-group cap
# returns (rebound_ids, repairs: list[RepairedBinding])
# (record identity is fully determined by these inputs: claim_ref via the
#  existing claim_ref() over claim_key_value; target_ref + inline target
#  mapping via the existing target_group_key() over target. Callers pass
#  the group's statement/kind/target — never prebuilt ref strings — so a
#  record can never name a claim its gate never saw.)
```

`VerificationDiagnostics` gains one additive field — `repaired: list[RepairedBinding]` (default empty; existing constructors unaffected) — so the diagnostics object carries repairs end to end. `build_verify_evaluation_attributes` serializes it: skeleton gains `"repaired": []`, `"repaired_truncated": false`, `"repaired_count": 0`, `"repair_shadow_count": 0`, and the repaired dumps ride the existing bounded pattern (`model_dump(mode="json")`, cap 10, skip-on-error, matching bindings/ledgers) — with one addition: when repairs exceed the cap the marker stores the first 10 (sorted by claim_ref, cited_id, donor_id for determinism) and sets `repaired_truncated: true` rather than truncating silently. Counts are uncapped integers covering ALL repairs, so truncation is always detectable. The reviewer retrieval path is existing infrastructure, not new code: each `RepairedBinding` carries `claim_key` + `target_ref` + the group's normalized `target` mapping inline (exact claim text, kind, and the full target — symbol, file, line range, commit_id/commit_subject for HISTORY — no join required to judge grounding), plus claim ref + candidate + donor IDs and per-record `outcome`/`binding_version` (a mixed applied+shadow marker disambiguates every candidate individually); the reviewer resolves full candidate/donor rows (including snippets) through the existing evidence-bank read for that investigation/turn, and may join (`claim_ref`, `target_ref`) to the same marker's `bindings` dumps (which always carry scalar `claim_key`/`claim_ref`/`target_ref`) for the group's partition context. The battery loader gains an opt-in bank-row fetch used ONLY for rendering `shadow_repair_review` rows (claim text, target symbol/file/subject, candidate snippet, donor snippet, basis, outcome, version, gate reason) — snippets appear in review output only, never in counter labels or series keys.

`ClaimRetrievalLedger`, `ClaimRetrievalBinding`, `ClaimTarget`, `HypothesisEvidenceAssessment`, `MissingObligation`, `EvaporatedId`, `Demotion`, `HistorySubjectInput`, and `DeferGathering` shapes are unchanged. `ClaimBindingEnforcement` gains additive `repaired_ids`/`repair_shadow_ids` (bounded lists, default empty) so verify/conclude telemetry and the conclude waiver block observe repairs without recomputing them.

## Interfaces

No user-facing interface changes in Phase 3. REST, CLI, MCP, and report shapes are unchanged. Operator-visible effects are confined to diagnostics already surfaced: `UNMAPPED`/`EVAPORATED` limitation lists gain the `REPAIRED_CITATION` member where applied, `loop_telemetry` gains the two repair counts, and the battery prints `repair_rate` plus the `shadow_repair_review` candidate list. Prompt text is unchanged (the model is never told about repair — it keeps citing what it cited; the platform recovers the binding).

| Method | Path | Request | Response | Description |
| ------ | ---- | ------- | -------- | ----------- |
| — | — | — | — | No API change in Phase 3 |

| Command | Arguments | Description |
| ------- | --------- | ----------- |
| — | — | No CLI change in Phase 3 |

## Implementation Detail

The implementer applies, in order:

1. `application/investigation/verification.py`: add `CLAIM_BINDING_CBV3` + extend `KNOWN_CLAIM_BINDING_VERSIONS` (resolver unchanged — it already validates against the set); amend `history_proof`'s strict-subject pin check from `== CBV2` to `in (CBV2, CBV3)` so `cbv3` retains strict HISTORY semantics (D3); add `_content_matches_for_repair()` returning a basis or `None` (pure, per D2 — overlap alone never matches — with `max_repairs=3` cap and deterministic sorted–evidence-ID donor scan in `attempt_claim_repair`); add `RepairedBinding` (+ `gate_reason`) + `REPAIRED_CITATION` + `repaired` field on `VerificationDiagnostics` + `repair_shadow`/`repaired_ids` plumbing on `ClaimBindingEnforcement`; extend the `repair_citation` docstring (no longer "never called" — now "called only behind the `cbv3`/shadow gate").
2. Five repair sites per D6 (engine `evaluate_hypothesis` re-evaluate loop; verify fast-path wrap; conclude filter wrap; conclude waiver-predicate amendment counting rebinds as kept; conclude fast-path wrap): on narrow-trigger kind-gate failure run `attempt_claim_repair`; under `cbv3` apply (substitute donors, re-run the kind gate once, attach limitation + records), under `cbv2` record shadow only (verdict path identical — proven by test). `cbv1` never reaches the helper. Pin-threading touch surface (all equality checks become `in (CBV2, CBV3)` with the shadow/apply split, no env reads added): `history_proof`/`validate_history_claim` version params, the activity-level `claim_binding_version ==` gates (verify activity :836/:859, `lifecycle.py` :1041/:1067/:1111/:1301/:1387), and the `VerificationEngine.__init__` pin validation (already set-based — unchanged). `repair_shadow` travels as a pure function argument, never as persisted entity state.
3. Ledger/directive staleness is accepted and documented (not fixed with new paths): repaired claims keep their original ledgers/bindings as the audit trail of what was cited; the directive may still fire on a linked `SEARCH_FAILED` after a successful repair — bounded, harmless, and it can only bank more evidence. No new directive path, no ledger rewrite on repair. `re_retrieval_directive` and `select_ledger_requery` are untouched.
4. Served-cached conclusions: the cache key must include `claim_binding_version` — verify at implementation; if absent, add it (one-line key change, pinned by test). Repaired turns persist new conclusion rows, so a stale pre-repair cached conclusion cannot serve a `cbv3` turn once the key carries the pin.
5. `application/worker/activities/verification.py` + `lifecycle.py`: thread pin resolution (already present) into the repair call; stamp `claim_binding_repaired`/`claim_binding_repair_shadow` into verify result data and `loop_telemetry`; persist `repaired_count`/`repair_shadow_count` in `VERIFY_EVALUATION` markers.
6. `ports/observability/telemetry.py` + `infrastructure/observability/telemetry.py` + `application/worker/activities/common.py`: add `record_claim_repair` port method + `claim.binding.repairs` counter + `_emit_verify_telemetry` emission (fire-and-forget, existing helper); extend `_KNOWN_LIMITATION_CODES` with `REPAIRED_CITATION`. These are explicit new operator-visible surfaces (port method, counter, loop keys, marker keys, battery series) — each gets the closed-vocabulary + no-high-cardinality-labels treatment; no REST/CLI/MCP/report-shape change.
7. `scripts/agent_metrics.py` + `scripts/retrieval_benefit.py`: add `repair_rate` series (applied repairs over checkable claims, numerator + denominator) and `shadow_repair_review` candidate listing (every shadow repair with claim/candidate/donor/basis — the human review input, never auto-approved).
8. Creation paths (`application/knowledge/intake.py`, `api/dev_seed.py`, `application/investigation/services.py`): UNCHANGED — keep stamping `cbv1`. A test pins that no creation path emits `cbv2` or `cbv3` through the normal flow (the gate to stamp anything else is a separate owner amendment). The shadow-cohort one-way backfill script (D4: explicit investigation IDs, `cbv1`/unmarked rows only, refuses `cbv3`, logs every stamp) is written, run once under owner approval, and deleted in the same change — a test pins its absence is not required (it must not exist post-run); the cohort's `cbv2` stamps are verified by listing the named investigations.

## Migrations

No migration in Phase 3. Creation order is code-only: pin constant, pure helpers + records, enforcement choke wiring, activity telemetry threading, metrics. Compatibility notes: old rows (`cbv1`/unmarked) never execute repair code — not even shadow; `cbv2` rows gain shadow records only (verdicts byte-identical, proven by a shadow-parity test that runs the suite with shadow on/off and asserts identical verdicts); `cbv3` rows exist only after the gate amendment. `RepairedBinding` validates with defaults on old payloads that omit it.

## Testing Philosophy

### Predicate contract (extended, not replaced)

Keep every Phase 1 `TestRepairCitationContract` vector (same-file/same-revision repair, cross-file refusal, cross-revision refusal, unscoped-kind refusal, no-production-caller — the last updated per D6) and add content-rule vectors: snippet containment repairs (3-line hit inside 20-line read, including a windowless-hit repair via containment, basis `snippet_containment`); overlap-plus-containment repairs (adjacent windows, same region, containment holds — basis `overlap_and_containment`); overlap-without-containment REFUSES (shared single line, disjoint snippets — the finding-3 vector); disjoint same-file rows refuse (different functions, no containment, no overlap); empty-candidate-snippet with overlapping ranges refuses (nothing to confirm); both-truncated and candidate-truncated-only vectors (documenting the asymmetric truncation semantics); locator-qualified vs repo-relative file pairs repair via the normalized matcher; malformed rows fail closed. Prove the rule never raises (fuzz with `None`/unshaped doubles). Prove donor scan order is sorted evidence-ID order (shuffled bank insertion still picks the same donor).

### Gate-failure recovery end to end

Prove the motivating shape at each of the five sites: `DEFINITION` group citing a banked hit row plus a banked uncited range row for the same file@rev — where the hit is KEPT via bank-fallback yet fails the definition gate (the cited-but-kept wrong-row case from finding 2) — without repair the claim is `INCONCLUSIVE`; with `cbv3` it verifies with the donor corroborating and a `REPAIRED_CITATION` limitation (engine path, verify fast path, and conclude paths including the amended waiver, which must publish rather than abstain on a rebound claim); with `cbv2` the verdict stays `INCONCLUSIVE` but the shadow record names the exact donor the applied path would have taken, and conclude still abstains. Prove ghost citations never repair: unknown IDs with no banked row stay `INCONCLUSIVE` under all pins with the requery directive intact. Prove the trigger is narrow: identical fixtures with a contradiction, refuting evidence, a rival block — or with a concession-only / orientation-only abstention that never produced a kind-gate `(False, reason)` — never attempt repair (no shadow records, execution counter stays zero). Prove per-group authorization isolation: a two-`DEFINITION`-group hypothesis (targets X and Y) where an uncited donor grounds group B but fails group A's gate for A's target — group A is NOT repaired (authorization never crosses groups), pinned alongside a same-target control that does repair. Prove reuse is independent: an uncited donor grounding BOTH groups repairs each group with its own gate run and its own `RepairedBinding` (deterministic group order reproduces the set). Prove no citation borrowing: a row cited by group B is never offered as a donor for group A even when same-file@rev and gate-passing. Prove the cap: four misattributed citations repair three and the fourth still fails, with exactly one re-evaluation loop per group (no repair-of-repair). Prove kind scope: identical fixtures under `CALL_EDGE`/`CAUSAL` never repair under any pin.

Implementation correction invariants: a conflicting donor choice for the same cited ID across groups fails closed for applied revalidation rather than applying a hypothesis-wide last-writer-wins substitution; repair diagnostics remain uncapped in memory until marker serialization applies the explicit ten-record review cap and `repaired_truncated` flag; and orientation-only support vetoes repair before the per-group helper runs. These invariants are covered by same-citation conflict, 31-record marker, and orientation-only counter tests.

### Pin matrix and shadow parity

Pin the resolver matrix extended with `cbv3` (absent→`CURRENT`, each value pinned, unknown→`UnknownVerifierVersion`, unreadable→`VerifierVersionUnavailable`), the no-`cbv2`/`cbv3`-stamp creation test, and the verify/conclude entry-path enumeration extended to the repair helper (every path resolves the pin before touching repair, and every pre-existing `claim_binding_version ==` gate is updated to the `cbv2`/`cbv3` split with the shadow/apply distinction). Prove shadow parity: the full focused suite produces byte-identical verdicts with shadow computation enabled vs disabled. Prove `cbv3` retains strict HISTORY semantics: a donor passing only the lax HISTORY path (fails the strict subject rule) never repairs under `cbv3` — the regression test fails if the `history_proof` pin check regresses to `== CBV2`. Prove `cbv1` runs execute zero repair code paths (shadow included — assert via an explicit execution counter on the helper plus the absence of repair records, since absence alone also matches a silently-skipped path). Prove the served-cache key carries the binding pin (add it if absent).

### Telemetry and battery

Prove `basis` (`snippet_containment|overlap_and_containment`)/`outcome`/`version` vocabularies are closed on the new `Literal`-typed models (out-of-vocabulary construction raises `ValidationError` — scoped to those models; limitation strings still degrade to `"other"` via `_limitation_kind` coercion, which is existing behavior, not a violation). Prove no file path, revision, snippet, or statement text reaches any counter label, tested at the `_emit_verify_telemetry` call site (which today takes free-form version/source strings). Prove `repair_rate` reports numerator + denominator and handles zero checkable claims without division failure. Prove `shadow_repair_review` lists every shadow candidate with claim/candidate/donor/basis/gate_reason — the review input the gate consumes. Prove `VERIFY_EVALUATION` marker serialization: a repair-bearing diagnostics object round-trips through `build_verify_evaluation_attributes` with the `repaired` dumps (bounded at 10), counts, and skeleton defaults on repair-free turns; and prove the reviewer path resolves each listed candidate/donor ID to its full bank row (snippets included) via the existing evidence read — the battery loader's bank-row fetch serves review rendering only. Prove the reviewer needs no join for claim identity: each repair carries `claim_key` + `target_ref` + the full normalized `target` mapping inline (symbol, file_path, line range, commit_subject — a HISTORY repair shows the asserted subject without any lookup), and (`claim_ref`, `target_ref`) join to the same marker's `bindings` dumps. Prove per-record disambiguation: a marker with both applied and shadow repairs carries `outcome`/`binding_version` on every record (cbv2→shadow, cbv3→applied, redundant by design), `Literal`-typed so an out-of-vocabulary value raises, and dispositions keyed by (hypothesis_id, turn, claim_ref, cited_id, donor_id, binding_version) match exactly one record each. Prove the gate fails closed on truncation: an 11-repair diagnostics object serializes to 10 dumps with `repaired_truncated == true` and counts at 11, and the gate query blocks on that marker; prove deterministic recompute reproduces the full 11-candidate list from persisted inputs. Prove gate completeness: a counted-but-unlisted candidate (counts exceeding listed dumps without the truncated flag — a corrupt marker) blocks the gate.

### Regression and invariants

Run the focused caller-edge, verification, G3, and Phase 1–2 binding suites plus the full unit suite. Re-prove the Phase 1 negative controls with repair enabled under `cbv3`: cross-file, cross-revision, unscoped-kind, and query-text-borrowing fixtures still refuse; `SUPPORTED` with zero kept citations remains unreachable (repair only rebinds banked rows — it never conjures kept IDs from nothing).

## Documentation Plan

### `docs/IAP-L2-D3-remediation-plan.md`

**Audience:** Owner and auditors

Append a Phase 3 record under the remediation log: scope (applied repair under `cbv3` with `cbv2` shadow-first and the zero-over-rebind gate), entry evidence (this design plus its adversarial review), exit evidence (test tallies, ruff, mypy, shadow-window evidence, scratch-tenant spot check only if separately approved), and the explicit statement that no freeze, run-set, budget, authority, or merge was consumed.

### `docs/IAP-proof-kernel-g3-design-v1.0.md`

**Audience:** Engineers working the retrieval and battery line

Add an as-built note: `VERIFY_EVALUATION` rows now carry repair records and counts; the directive state machine is unchanged (repaired claims stop directing because their bindings are satisfied, not through any new directive path).

### `docs/IAP-claim-binding-phase1-design.md`

**Audience:** Engineers working the claim-binding line

Add a forward pointer (no edits to the Phase 1 record itself): the dead `repair_citation` contract is now live behind the `cbv3`/shadow gate defined here; the predicate itself is unchanged, the caller-side content rule and the gating are new.

## Risks / Trade-offs

### Over-rebind launders a wrong answer into VERIFIED

**Risk:** A donor that passes the predicate but does not actually ground the claim converts an honest `INCONCLUSIVE` into a false `VERIFIED` — the worst failure this program exists to prevent.

**Mitigation:** Four independent gates stand between a citation and a repaired verdict: same file, same revision, kind-grounding by the existing kind gates (not a new matcher), and required snippet containment (line overlap corroborates but never suffices alone) — plus the per-group cap of 3, the `DEFINITION`/`HISTORY`-only scope with per-group isolation, the explicit kind-gate-failure trigger, and the shadow-first rollout that requires a human-reviewed zero-over-rebind window before any `cbv3` row exists. The battery tracks false accepts separately so any future over-rebind is measured, not assumed.

### Shadow review is human labor that can rubber-stamp

**Risk:** A long list of shadow candidates invites skim-approval, which would open the `cbv3` gate on unexamined repairs.

**Mitigation:** The review list carries the full evidence for each candidate (claim text, candidate row, donor row, basis, gate reason) and the gate requires an explicit per-candidate disposition, not a blanket sign-off; the window minimum (full live-case set, owner-set, any over-rebind resets) keeps the list short by construction. Candidates that are obviously right in bulk still require the recorded disposition — the log, not the speed, is the control.

### Repair masks model citation sloppiness

**Risk:** If repair routinely fixes hit-instead-of-read citations, the prompt/model never learns to cite range reads, and the system pays a repair dependency forever.

**Mitigation:** `repair_rate` is a visible battery series precisely so citation quality is tracked; a rising rate is a prompt-work signal, not a success metric. The model is never told about repair (no prompt change), so its citations remain an honest measure of grounding behavior — repair is insurance, not instruction.

### Donor sharing across claims

**Risk:** One banked range read can donor for several claims, concentrating many verdicts on a single row whose later invalidation would cascade.

**Mitigation:** Sharing is provenance, not proof: each group's use records its own `RepairedBinding` with that group's gate reason, and invalidation of the donor row invalidates every dependent verdict through the existing evidence-identity join (same as shared cited rows today). Reuse across groups is allowed precisely because each use is independently gated — a donor grounding two claims is two separately authorized repairs, not one authorization spent twice. No new coupling is introduced beyond what shared cited rows already carry.
