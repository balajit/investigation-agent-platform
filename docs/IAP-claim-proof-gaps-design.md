# Design: Claim-proof gaps — HISTORY absence, extraction wording, construction-vs-invocation, control-loop routing

## Context

Claim binding Phases 1–4 are merged into `iap-full-1025` (merge `98840fb`). Four `PLANNED` ledger items from the October battery era remain genuinely open, all touching the same verifier/binding surface in `src/investigation_agent_platform/application/investigation/verification.py`:

- The HISTORY verifier proves positive statements ("commit X touched file Y") but a negative finding ("no such commit") falls through every validator and yields no verdict. The ABSENCE kind already has a five-state outcome (`absence_outcome`: `NO_SEARCH` / `DISCOVERY_ONLY` / `EMPTY_SEARCH_PROVED` / `PRESENCE_CONFLICT` / `SCOPE_MISMATCH`); HISTORY has no equivalent.
- `_extract_call_edge` recognizes two phrasings ("callers of X", "X called by Y"). Live L2 asks "What calls the order service handler" — unrecognized, so no CALL_EDGE target is formulated from the statement.
- `call_site_proof` already excludes `new X(` construction for bare-class callees (`exclude_construction=bare`, with `_instance_call_rows` as the declaration+invocation join fallback), and every CALL_EDGE check path funnels through it — except the caller-enumeration scan, which calls `_call_shown` without the flag, so a construction line counts as "shown" for enumeration.
- `next_check_for_abstention` already maps specific gate reasons to targeted directives, and `_MISSING_OBLIGATION_DIRECTIVES` covers per-kind fallbacks. What is missing is the loop-terminal case: when the workflow ends `verification_failed_or_limit_reached` with thin reasons, the directive degrades to the generic bind-and-re-verify string instead of a diagnosed cause.

No `docs/dev/architecture.md` exists in the project. The operative architecture sources are the four phase design docs (`docs/IAP-claim-binding-phase1/2/3/4-design.md`) and this file extends them; no changes to those files are required by this design.

## References

- `docs/IAP-claim-binding-phase1-design.md` — Authoritative claim-local resolution, structured commit_subject (D2), shared predicates over forked copies. Deltas for A/B/C land here.
- `docs/IAP-claim-binding-phase2-design.md` — Stored-vs-derived ledger preference. Unchanged by this design; cited for the no-new-persistence constraint.
- `docs/IAP-claim-binding-phase3-design.md` — Repair gating, donor sharing, shadow disposition. Cited for the "independent gating per use" precedent reused in D1.
- `docs/IAP-claim-binding-phase4-design.md` — `claim_gaps` feed, NC skip semantics, denominator rules, live battery composition. D4 builds on its failure vocabulary.
- `reports/live_battery_20261027_review_fix_L2r3.md` and `reports/live_battery_20261027_base_same_tenant.md` — The L2 variance pair: identical failure shape across trees, attributed to model variance. Parity gates below are calibrated against these.
- Ledger entries `[2026-10-21] (PLANNED) Commit-history ABSENCE path`, `Call-edge extraction wording gap`, `Construction is not invocation`, `Investigation control-loop diagnosis` — the four items this design closes.

## Goals / Non-Goals

**Goals:**

- A HISTORY absence proof: scoped zero-match history search yields a VERIFIED negative verdict; incomplete search preserves today's fall-through exactly.
- A wording matrix for call-edge extraction with a synthetic-harness-first gate (test-only extraction matrix is a hard precondition for touching `_extract_call_edge`).
- Construction excluded at every `_call_shown` call site, with a test pinning each site's flag.
- Loop-terminal failure cause diagnosed from the existing bucket vocabulary, selecting a targeted `next_check` when reasons are thin.
- Battery parity: every live PASS row stays PASS; the injected-claim driver (`scripts/live_inject_not_checkable.py`) stays green.

**Non-Goals:**

- No new retrieval contracts. Detecting capped/errored history search (a retrieval-schema change) is explicitly deferred; D1 v1 proves only the inspected-empty case.
- No prompt-version changes. D2/D3 are parser/proof tightenings behind the existing version pins, not new pins.
- No control-loop execution changes in this design beyond directive selection (D4). Retry *policy* (what to re-run) is follow-on work.
- No new persisted models, no migrations, no user-facing interfaces.

## Decisions

### D1: HISTORY absence proof with inspected-empty VERIFIED and fall-through preservation

**Decision:** Add `history_absence_outcome(evidence_items, target)` mirroring `absence_outcome`, consuming `COMMIT_HISTORY` rows. Three states: `INSPECTED_EMPTY` (≥1 non-discovery row with `match_count == 0` on the target file, commit-scoped when `target.commit_id` is set) → `validate_history_claim` returns `VERIFIED` with a `HISTORY_ABSENCE_PROVED:` explanation; `SEARCH_INCOMPLETE` (rows exist for the file but none qualifies as an inspected-empty search) → `None`, exactly today's fall-through; `NO_SEARCH` (no history rows at all) → `None`, exactly today's fall-through. A valid empty search is: zero matches, non-discovery, same file (and commit prefix-match when the target names a commit). Positive-match logic above it is untouched and runs first. New verdict path is active under `cbv2`/`cbv3`; `cbv1` keeps today's fall-through byte-identical (version-pinned behavior, never env-switched).

```python
# src/investigation_agent_platform/application/investigation/verification.py
@staticmethod
def history_absence_outcome(
    evidence_items: list[Any], target: ClaimTarget | None
) -> tuple[str, str]:
    """Negative HISTORY finding: inspected-empty vs incomplete vs no search.

    Returns (outcome, reason). INSPECTED_EMPTY means a scoped history
    search ran and matched nothing: >=1 non-discovery COMMIT_HISTORY row
    with match_count == 0 on the target file (commit prefix-matched when
    the target names one). Anything else is not proof: SEARCH_INCOMPLETE
    when history rows exist but none qualifies, NO_SEARCH when no history
    was retrieved at all. Both non-proof states carry the reason for the
    caller to fall through, never to conclude from silence.
    """
    try:
        if target is None or not getattr(target, "file_path", ""):
            return ("NO_SEARCH", "history absence needs a file target")
        commit_id = str(getattr(target, "commit_id", "") or "").lower()
        file_hits: list[Any] = []
        for item in evidence_items or []:
            if VerificationEngine._item_type(item) != "COMMIT_HISTORY":
                continue
            try:
                attrs = getattr(item, "attributes", None) or {}
                if not isinstance(attrs, dict):
                    attrs = {}
                if attrs.get("discovery_only") is True:
                    continue
            except Exception:
                continue
            if not VerificationEngine._paths_match(
                str(target.file_path or ""), VerificationEngine._item_file(item)
            ):
                continue
            file_hits.append(item)
        if not file_hits:
            return ("NO_SEARCH", "no COMMIT_HISTORY rows retrieved; do not conclude absence")
        for item in file_hits:
            try:
                attrs = getattr(item, "attributes", None) or {}
                count = attrs.get("match_count") if isinstance(attrs, dict) else None
            except Exception:
                count = None
            if not (isinstance(count, int) and not isinstance(count, bool) and count == 0):
                continue
            if commit_id:
                revision = str(
                    getattr(
                        getattr(getattr(item, "provenance", None), "source_location", None),
                        "revision",
                        "",
                    )
                    or ""
                ).lower()
                if not revision or (
                    not revision.startswith(commit_id) and not commit_id.startswith(revision)
                ):
                    continue
            return ("INSPECTED_EMPTY", "")
        return (
            "SEARCH_INCOMPLETE",
            "history rows exist for the file but no inspected-empty search covers the claimed scope",
        )
    except Exception:
        return ("NO_SEARCH", "history absence evaluation failed closed")
```

Wiring in `validate_history_claim`, on the no-positive-match path (where `if not matching or not path_commits: return None` sits today):

```python
# src/investigation_agent_platform/application/investigation/verification.py
# (inside validate_history_claim, replacing the bare `return None`
# on the no-positive-match path)
if not matching or not path_commits:
    if self.claim_binding_version in (CLAIM_BINDING_CBV2, CLAIM_BINDING_CBV3):
        outcome, _ = VerificationEngine.history_absence_outcome(evidence_items, target)
        if outcome == "INSPECTED_EMPTY":
            return VerificationResult(
                hypothesis_id=hypothesis.id,
                status=VerificationStatus.VERIFIED,
                corroborating_evidence_ids=[],
                explanation=(
                    "HISTORY_ABSENCE_PROVED: scoped history search of "
                    f"{target.file_path} matched no commit; the negative "
                    "recent-change claim is verified by the empty search."
                ),
                confidence=confidence,
                location_validated=True,
                limitations=[
                    VerificationLimitation(
                        code=LimitationCode.HISTORY_ABSENCE_PROVED,
                        detail="Negative proven by scoped empty search, not by a cited row.",
                    )
                ],
            )
    return None
```

`corroborating_evidence_ids` is empty by construction: an absence is proven by the search, not by a row, exactly as `absence_proof` proves without a positive citation. `HISTORY_ABSENCE_PROVED` is appended to `LimitationCode` (append-only enum: never rename or remove, only append). The explanation prefix lets battery forensics and `next_check` routing match it deterministically. `confidence` reuses the surrounding function's existing construction verbatim.

**Alternative considered:** A new `ClaimKind` (e.g., `HISTORY_ABSENCE`) with its own partition and telemetry vocabulary. Rejected — doubles the kind surface for one verdict path, and the ABSENCE precedent proves a negative statement verifies under its existing kind.

**Rationale:** The positive path already assembles `path_commits`; reusing that assembly for the negative case keeps one retrieval contract and one file-scope definition. Version-gating follows the Phase 1 D4 pin convention: in-flight `cbv1` rows keep the semantics they started with.

**Trade-offs:** Capped/errored history search is undetectable in the current row schema, so `SEARCH_INCOMPLETE` falls through instead of producing a `NOT_CHECKABLE`-with-blocker verdict. Closing that needs a retrieval-contract change (deferred non-goal).

---

### D2: Call-edge extraction wording matrix behind a synthetic harness

**Decision:** Extend `_extract_call_edge` with the missing interrogative and nominal forms, ordered most-specific first so `cbv1` legacy behavior is byte-identical for inputs the current patterns already match. The pattern table is a module-level constant so the harness and the implementation share one source. New forms: "what calls X" / "what invokes X", "who calls X" / "who invokes X", "where is X called/invoked", "X is invoked by Y", "call sites of X" / "invocations of X". New patterns are active under `cbv2`/`cbv3`; `cbv1` uses the two legacy rows only.

```python
# src/investigation_agent_platform/application/investigation/verification.py
_CALL_EDGE_PATTERNS_ALL: tuple[tuple[str, str], ...] = (
    # (regex, shape): shape "callee" returns (callee, None);
    # shape "edge" returns (callee, caller). Ordered most-specific first.
    (r"callers?\s+of\s+{ident}", "callee"),
    (r"calls?\s+sites?\s+of\s+{ident}", "callee"),
    (r"invocations?\s+of\s+{ident}", "callee"),
    (r"what\s+(?:calls?|invokes?)\s+{ident}", "callee"),
    (r"who\s+(?:calls?|invokes?)\s+{ident}", "callee"),
    (r"where\s+is\s+{ident}\s+(?:called|invoked)", "callee"),
    (r"{ident}\s+(?:is\s+|are\s+)?(?:called|invoked)\s+by\s+{ident}", "edge"),
)
# cbv1 legacy: byte-identical to the pre-change implementation.
_CALL_EDGE_PATTERNS_LEGACY: tuple[tuple[str, str], ...] = (
    (r"callers?\s+of\s+{ident}", "callee"),
    (r"{ident}\s+(?:is\s+|are\s+)?called\s+by\s+{ident}", "edge"),
)


@staticmethod
def _extract_call_edge(
    statement: str, *, legacy: bool = False
) -> tuple[str | None, str | None]:
    """(callee, caller) from untyped prose; (None, None) when unclear."""
    text = statement or ""
    ident = r"[`'\"]?([A-Za-z_][\w.:#]*)[`'\"]?"
    rows = (
        VerificationEngine._CALL_EDGE_PATTERNS_LEGACY
        if legacy
        else VerificationEngine._CALL_EDGE_PATTERNS_ALL
    )
    for raw, shape in rows:
        m = re.search(raw.format(ident=ident), text, re.IGNORECASE)
        if not m:
            continue
        if shape == "edge":
            return (m.group(1), m.group(2))
        return (m.group(1), None)
    return (None, None)
```

Callers pass `legacy=(version == CLAIM_BINDING_CBV1)` following the existing `strict_subject` convention at the `validate_history_claim` call site (`self.claim_binding_version in (CLAIM_BINDING_CBV2, CLAIM_BINDING_CBV3)` → `legacy=False`). The legacy rows reproduce current behavior exactly (`callers of X`, `X called by Y`); everything else is additive. `called by`/`invoked by` share one edge row.

**Alternative considered:** Routing extraction through the model (a typed `claim_target` for every CALL_EDGE statement). Rejected — the untyped-prose fallback exists precisely for statements the model did not type; model-routing would move the failure, not remove it.

**Rationale:** Extraction is pure string matching with no retrieval contact, so widening it cannot fabricate evidence — every new edge still faces `call_site_proof`. The risk is a *missed* edge becoming a *wrong* edge only if a pattern misparses; the harness pins each form.

**Trade-offs:** English-only, same as the existing patterns. Non-English statements still yield `(None, None)`.

---

### D3: Construction excluded at every `_call_shown` call site

**Decision:** The enumeration scan passes `exclude_construction` for bare-class symbols, matching `call_site_proof` semantics: a `new X(` line never names a caller for enumeration purposes. All three `call_site_proof` entry points already carry the bare-aware flag, so the enumeration scan is the only behavior change. A test enumerates every `_call_shown` call site and pins its flag, so future call sites cannot silently regress. The flag is threaded under `cbv2`/`cbv3`; `cbv1` keeps the unflagged call byte-identical.

```python
# src/investigation_agent_platform/application/investigation/verification.py
# (caller-enumeration scan inside check_claim_kind; only the flag is new)
bare_symbol = not any(
    sep in (str(getattr(target, "symbol", "") or "")) for sep in ("::", ".", "#")
)
strict = self.claim_binding_version in (CLAIM_BINDING_CBV2, CLAIM_BINDING_CBV3)
if not VerificationEngine._call_shown(
    str(getattr(target, "symbol", "") or ""),
    str(getattr(item, "content_snippet", "") or ""),
    exclude_construction=(bare_symbol and strict),
):
    continue
```

The `_instance_call_rows` declaration+invocation join stays as-is: it is the sanctioned path for proving invocation through a declared instance, and construction lines alone still prove nothing.

**Alternative considered:** Excluding construction for qualified callees too (`Class.method`). Rejected — for a qualified callee, `new Class(` cannot match the `method(` call pattern, so construction lines are already inert there; threading the flag would be dead code asserting a false generality.

**Rationale:** One predicate, one flag, every call site. The enumeration scan was the single site where "shown" meant something weaker than everywhere else.

**Trade-offs:** A statement that genuinely asks "what constructs X" still has no first-class claim kind; it continues to resolve as a CALL_EDGE miss rather than a supported query. That is a kind-vocabulary question, out of scope.

---

### D4: Loop-terminal cause diagnosis from the bucket vocabulary (built last)

**Decision:** When the workflow terminates `verification_failed_or_limit_reached` and `next_check_for_abstention` would fall back past the specific-reason rows to the generic string, the conclude path first classifies the run into the frozen attribution-bucket vocabulary (`never_retrieved`, `retrieved_but_unbound`, `bound_but_wrong_type`, `withheld_by_refuting`, `provider_or_analyzer_failed`, `genuinely_insufficient`, `wrong_revision_or_scope` — the `ATTRIBUTION_BUCKETS` declared in `scripts/proof_kernel_baseline.py`) and selects the matching `_MISSING_OBLIGATION_DIRECTIVES` entry (or its `next_check` reason-row equivalent) instead of the generic fallback. Classification inputs are already stamped: `loop_telemetry` counters (retrieval happened? bindings landed?) plus the gate-fail reason strings. No new telemetry fields, no retry-policy change — directive selection only.

```python
# src/investigation_agent_platform/application/investigation/verification.py
# (sketch: bucket selection from already-stamped signals; full
# implementation after D1–D3 outcomes are known)
_BUCKET_DIRECTIVES: dict[str, str] = {
    "never_retrieved": _MISSING_OBLIGATION_DIRECTIVES["DEFINITION"],
    "retrieved_but_unbound": "Bind the retrieved rows to the claim target, then re-verify.",
    "bound_but_wrong_type": "Rebind to the row type the claim kind requires, then re-verify.",
    "withheld_by_refuting": "Resolve the refuting evidence, then re-verify.",
    "provider_or_analyzer_failed": "Retry the failed provider or analyzer step, then re-verify.",
    "genuinely_insufficient": _DEFAULT_MISSING_OBLIGATION_DIRECTIVE,
    "wrong_revision_or_scope": "Re-scope retrieval to the claimed revision and scope, then re-verify.",
}
```

**Alternative considered:** A learned/model-chosen next check. Rejected — directives must stay deterministic and auditable; the reason-string routing in `next_check_for_abstention` already proves the deterministic approach carries the load.

**Rationale:** The taxonomy exists in two places (battery forensics, attribution buckets) but the loop-terminal path cannot speak it. D4 is a join, not a new invention.

**Trade-offs:** Bucket classification from counters is heuristic at the margins (e.g., partial retrieval). Misclassification degrades to a slightly-off directive, never to a wrong verdict — selection only affects prose.

## Data Storage

No new persisted models, no schema changes, no new telemetry fields. D1 reads existing `COMMIT_HISTORY` rows (`attributes.match_count`, file path, provenance revision) and appends one `LimitationCode` enum member (`HISTORY_ABSENCE_PROVED`; the enum is append-only by contract). D2 is pure string matching plus two module-level pattern tables. D3 changes a call-site flag. D4 reads already-stamped `loop_telemetry` and reason strings plus one module-level map. The claim-binding ledger, partitions, and pins are untouched.

## Data Structures

Input and output shapes, all internal to `verification.py`:

```python
# Outcome vocabulary for history_absence_outcome (D1).
# "INSPECTED_EMPTY" -> caller emits VERIFIED negative verdict.
# "SEARCH_INCOMPLETE" | "NO_SEARCH" -> caller falls through (returns None).
HistoryAbsenceOutcome = str  # Literal["INSPECTED_EMPTY", "SEARCH_INCOMPLETE", "NO_SEARCH"]

# Pattern tables for _extract_call_edge (D2).
# Each row: (regex with {ident} slot, "callee" | "edge").
_CALL_EDGE_PATTERNS_ALL: tuple[tuple[str, str], ...]
_CALL_EDGE_PATTERNS_LEGACY: tuple[tuple[str, str], ...]

# Bucket -> directive map for loop-terminal selection (D4).
_BUCKET_DIRECTIVES: dict[str, str]
```

`VerificationResult` construction for the D1 negative verdict reuses the positive path's fields verbatim (`hypothesis_id`, `status=VERIFIED`, the surrounding `confidence`, `location_validated=True`) with empty `corroborating_evidence_ids`, the `HISTORY_ABSENCE_PROVED:` explanation prefix, and a single `VerificationLimitation(code=HISTORY_ABSENCE_PROVED)`.

## Interfaces

No user-facing interfaces change: no REST, CLI, MCP, or TUI surface is added or altered. The observable differences are verdict explanations (new `HISTORY_ABSENCE_PROVED:` prefix), `next_check` strings on loop-terminal conclusions, and battery report lines — all existing channels with closed vocabularies.

## Implementation Detail

Wiring order and call sites (the Decisions carry the full code; this section pins where it lands):

1. `_CALL_EDGE_PATTERNS_ALL` / `_CALL_EDGE_PATTERNS_LEGACY` constants + rewritten `_extract_call_edge` with `legacy` kwarg (D2). The `call_site_proof` call site passes `legacy=(version == CLAIM_BINDING_CBV1)` following the existing `strict_subject` convention.
2. `history_absence_outcome` static + the `validate_history_claim` no-match branch (D1). The positive-match block above it is byte-untouched and runs first. `LimitationCode.HISTORY_ABSENCE_PROVED` appended to the enum.
3. Enumeration-scan flag (D3). The three `call_site_proof` entry points (aggregate assessments, aggregate edges, single target) are untouched.
4. `_BUCKET_DIRECTIVES` + conclude-path selection (D4), after D1–D3 are merged and live-proved.

Version pins: no new pin, no pin bump. D1/D2/D3 new behavior is active under `cbv2`/`cbv3`; `cbv1` rows keep byte-identical semantics per the Phase 1 D4 convention. If live parity shows any PASS→FAIL flip attributable to D2/D3, the change is reverted and re-designed — parity gates below are hard.

## Migrations

Implementation order is a dependency chain, each step independently committable and revertible:

1. Synthetic extraction harness (`tests/unit/test_call_edge_extraction.py`): statement→(callee, caller) matrix covering every `_CALL_EDGE_PATTERNS_ALL` row plus adversarial non-matches ("callers of" inside unrelated prose, quoted identifiers, `::`/`#` separators). Red-first: new rows fail before the implementation lands. This harness is a hard precondition for step 2.
2. D2 implementation behind the harness, with a `cbv1`-legacy byte-identical test (legacy rows reproduce old outputs exactly).
3. D1 (`history_absence_outcome` + wiring + unit tests: inspected-empty VERIFIED, incomplete fall-through, no-rows fall-through, positive-path byte-identical, `cbv1` fall-through preserved).
4. D3 (enumeration flag + call-site flag audit test).
5. Full suite + sandbox battery parity (all live PASS rows stay PASS; injected-claim driver green) → commit D1–D3 as one unit.
6. D4 implementation, last, against the merged D1–D3 tree.

No data migration: frozen baselines regenerate deterministically (provenance-only `git_rev`), and no fixture content changes meaning under this design.

## Testing Philosophy

### Extraction matrix

The harness pins every pattern row independently: each phrasing form maps to its expected (callee, caller), and adversarial inputs (identifiers embedded in unrelated prose, missing callee, empty statement) map to `(None, None)`. New phrasings are added as failing rows first. The two legacy forms are pinned to their current outputs under both pattern tables so widening cannot silently shift them.

### History absence

Unit tests construct `COMMIT_HISTORY` rows directly: a zero-match non-discovery row on the target file yields `INSPECTED_EMPTY`; discovery-only rows yield `SEARCH_INCOMPLETE`; no rows yield `NO_SEARCH`; a positive match still takes the existing VERIFIED path with the existing explanation byte-identical. A dedicated test asserts the incomplete and no-search states return `None` from `validate_history_claim` (fall-through preservation, not just outcome strings), and a `cbv1` test asserts the negative path still returns `None` under the legacy pin.

### Construction exclusion

A test enumerates `_call_shown` call sites (excluding the definition) and asserts each passes `exclude_construction` explicitly — no default reliance. Behavior tests pin: a `new OrderServiceHandler(` line alone does not satisfy the enumeration scan for bare `OrderServiceHandler` under `cbv2`/`cbv3` but still satisfies it under `cbv1`; declaration + `handler.submit(` same-file join still verifies via `_instance_call_rows`.

### Battery parity

After D1–D3, the sandbox battery must reproduce every current live PASS (L1/L3/L4/L5/L8, injected-claim 6/6) and every current live FAIL must keep its failure *cause* (model-variance shapes, not new gate strings — except intended new `HISTORY_ABSENCE_PROVED` lines on negative-history probes). Any PASS→FAIL flip stops the line.

### Control-loop routing

D4 tests feed canned (bucket-signals, reasons) pairs and assert the selected directive; a test asserts the generic fallback survives only when classification itself fails closed. No live model in these tests.

## Documentation Plan

### `docs/IAP-claim-proof-gaps-design.md` (this file)

**Audience:** Implementer and owner-review follow-up.

The Phase 0 record: decisions D1–D4 with code, ordering, and parity gates. Updated once at implementation close with outcome notes (same delta-section convention as the phase docs).

### Ledger `docs/IAP_Arch_Eng_Recommendations.log`

**Audience:** Session continuity and release review.

On implementation close, the four `PLANNED` entries flip to `IMPLEMENTED` with file-and-commit evidence, matching the October close-out convention. If D4 splits (design now, build later), its entry moves to `PLANNED` with the new scope rather than staying open under the old text.

## Risks / Trade-offs

### Positive history verdicts shift meaning

**Risk:** A negative statement that previously fell through to another validator (and occasionally VERIFIED elsewhere) now VERIFIES as a history absence. If the zero-match row was itself a retrieval artifact (wrong file scoped, capped search presented as complete), we mint a false VERIFIED.

**Mitigation:** `INSPECTED_EMPTY` requires non-discovery rows with explicit `match_count == 0` on the exact target file — discovery-only and unscored rows never qualify. Battery parity pins every current PASS; new `HISTORY_ABSENCE_PROVED` lines appear only on negative-history probes. The capped-search hole is documented (D1 trade-off) rather than papered over.

### Extraction widening misparses prose

**Risk:** A new pattern matches an identifier the statement did not mean as a callee, formulating a wrong CALL_EDGE target.

**Mitigation:** Extraction never verifies — every formulated edge faces `call_site_proof` and binding. The harness adversarial rows pin non-matches. Any live misparse surfaces as a gate-fail reason string, which the battery reports make visible.

### Enumeration tightening drops a legitimate caller

**Risk:** A caller whose only shown evidence is a construction line now fails enumeration where it previously passed.

**Mitigation:** That previous PASS was the exact false-proof the ledger item records (live L2#2 answered on field-construction lines). The `_instance_call_rows` join remains the legitimate path for instance-mediated invocation. Parity watches for flips; a legitimate-caller flip is a re-design trigger, not a test-update trigger. `cbv1` rows are unaffected by construction.

### Bucket classification misfires

**Risk:** Counter-based classification picks the wrong bucket and the operator gets a slightly-off directive.

**Mitigation:** Selection affects prose only, never verdicts. Fail-closed to the existing generic string preserves today's behavior as the floor.
