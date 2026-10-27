# Design: Claim-proof gaps — HISTORY absence, extraction wording, construction-vs-invocation, control-loop routing

## Context

Claim binding Phases 1–4 are merged into `iap-full-1025` (merge `98840fb`). Four `PLANNED` ledger items from the October battery era remain genuinely open, all touching the same verifier/binding surface in `src/investigation_agent_platform/application/investigation/verification.py`:

- The HISTORY verifier proves positive statements ("commit X touched file Y") but a negative finding ("no such commit") falls through every validator and yields no verdict. The ABSENCE kind already has a five-state outcome (`absence_outcome`: `NO_SEARCH` / `DISCOVERY_ONLY` / `EMPTY_SEARCH_PROVED` / `PRESENCE_CONFLICT` / `SCOPE_MISMATCH`); HISTORY has no equivalent. The current empty-history path banks discovery-only `SOURCE_CODE` rows (`_absence_proof_row` in `execution.py`, with an explicit comment explaining why a `COMMIT_HISTORY`-typed absence row was avoided: it carried no file/revision and could never satisfy proof). No producer emits a proof-capable history empty row today, so D1 includes that narrow producer change — the row it adds carries file+revision, the exact property the old comment found missing.
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

- No new retrieval contracts beyond one row shape. D1 adds a single producer: a `COMMIT_HISTORY`-typed, non-discovery zero-match row carrying file+revision on the GET_CODE_HISTORY empty path (alongside, not replacing, the existing discovery-only row). Detecting capped/errored history search is explicitly deferred; D1 v1 proves only the inspected-empty case.
- No prompt-version changes. D2/D3 are parser/proof tightenings behind the existing version pins, not new pins.
- No control-loop execution changes in this design beyond directive selection (D4). Retry *policy* (what to re-run) is follow-on work.
- No new persisted models, no migrations, no user-facing interfaces.

## Decisions

### D1: HISTORY absence proof with inspected-empty VERIFIED and fall-through preservation

**Decision:** Add `history_absence_outcome(evidence_items, target)` mirroring `absence_outcome`, consuming `COMMIT_HISTORY` rows, **plus the one producer that makes its acceptance case reachable**: a `COMMIT_HISTORY`-typed, non-discovery zero-match row banked on the GET_CODE_HISTORY empty path. Three states: `INSPECTED_EMPTY` (≥1 qualifying row on the target file, commit-scoped when `target.commit_id` is set) → `validate_history_claim` returns `VERIFIED` with a `HISTORY_ABSENCE_PROVED:` explanation; `SEARCH_INCOMPLETE` (rows exist for the file but none qualifies — including the mixed case where a non-discovery positive row exists for the file, which blocks absence exactly like `PRESENCE_CONFLICT`) → `None`, today's fall-through; `NO_SEARCH` (no history rows at all) → `None`, today's fall-through. A valid empty search is: zero matches, non-discovery, same file (and commit prefix-match when the target names a commit). Positive-match logic above it is untouched and runs first. New verdict path is active under `cbv2`/`cbv3`; `cbv1` keeps today's fall-through byte-identical (version-pinned behavior, never env-switched).

The proved claim is bounded by the searched scope — never broader. When the target names a commit, the empty search must be commit-scoped (revision prefix-match); when neither statement nor target names one, the verdict proves only "no history for file F in the searched scope" and the explanation states exactly that. No overclaim.

```python
# src/investigation_agent_platform/application/investigation/verification.py
@staticmethod
def history_absence_outcome(
    evidence_items: list[Any], target: ClaimTarget | None
) -> tuple[str, str, list[Any]]:
    """Negative HISTORY finding: inspected-empty vs incomplete vs no search.

    Returns (outcome, reason, qualifying_rows). INSPECTED_EMPTY means a
    scoped history search ran and matched nothing: >=1 non-discovery
    COMMIT_HISTORY row with match_count == 0 on the target file (commit
    prefix-matched when the target names one). A non-discovery positive
    row for the same file blocks absence (SEARCH_INCOMPLETE): an empty
    search never overrides existing positive history evidence. NO_SEARCH
    covers no history retrieved at all. Both non-proof states carry the
    reason for the caller to fall through, never to conclude from silence.
    """
    try:
        if target is None or not getattr(target, "file_path", ""):
            return ("NO_SEARCH", "history absence needs a file target", [])
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
            return ("NO_SEARCH", "no COMMIT_HISTORY rows retrieved; do not conclude absence", [])
        for item in file_hits:
            try:
                attrs = getattr(item, "attributes", None) or {}
                count = attrs.get("match_count") if isinstance(attrs, dict) else None
            except Exception:
                count = None
            if isinstance(count, int) and not isinstance(count, bool) and count != 0:
                return (
                    "SEARCH_INCOMPLETE",
                    "positive history evidence exists for the file; absence blocked",
                    [],
                )
        qualifying: list[Any] = []
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
            qualifying.append(item)
        if qualifying:
            return ("INSPECTED_EMPTY", "", qualifying)
        return (
            "SEARCH_INCOMPLETE",
            "history rows exist for the file but no inspected-empty search covers the claimed scope",
            [],
        )
    except Exception:
        return ("NO_SEARCH", "history absence evaluation failed closed", [])
```

Wiring in `validate_history_claim`, on the no-positive-match path (where `if not matching or not path_commits: return None` sits today — i.e., *after* the concession-language and unresolved-contradiction refusals already above it, which therefore apply unchanged):

```python
# src/investigation_agent_platform/application/investigation/verification.py
# (inside validate_history_claim, replacing the bare `return None`
# on the no-positive-match path)
if not matching or not path_commits:
    if self.claim_binding_version in (CLAIM_BINDING_CBV2, CLAIM_BINDING_CBV3):
        outcome, _, qualifying = VerificationEngine.history_absence_outcome(
            evidence_items, target
        )
        if outcome == "INSPECTED_EMPTY":
            return VerificationResult(
                hypothesis_id=hypothesis.id,
                status=VerificationStatus.VERIFIED,
                corroborating_evidence_ids=[
                    item.evidence_id for item in qualifying
                ],
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
                        detail="Negative proven by scoped empty search over the cited rows.",
                    )
                ],
            )
    return None
```

Verdict trace, end to end: the qualifying rows are banked evidence with IDs, cited by the hypothesis's `supporting_evidence_ids` like any proof row — so claim-local binding enforcement sees a cited HISTORY group (no empty-corroboration special case anywhere downstream), confidence/corroboration flow through the ordinary `VerificationResult` fields, and ConclusionGate treats the result as any VERIFIED. Reachability condition: in production the hypothesis must cite the absence-row IDs; synthetic tests construct that linkage directly. The e2e test (Testing Philosophy) asserts the final disposition through evaluate+conclude, not just the validator return.

Producer contract (the narrow retrieval change), in `execution.py` on the GET_CODE_HISTORY empty path, alongside — not replacing — the existing discovery-only `SOURCE_CODE` row:

```python
# New proof-capable row: COMMIT_HISTORY-typed, NON-discovery, file+revision.
# Scope carries the revision (unlike _code_absence_scope's history branch,
# which is revision-free by design for the discovery row).
_proof_scope = f"{repo}@{rev}:history={path}"[:512]
await evidence_repo.save(
    tenant_id,
    _with_fetch_reason(
        Evidence(
            tenant_id=tenant_id,
            investigation_id=investigation_id,
            evidence_type=_EvidenceType.COMMIT_HISTORY,
            provider="code-intelligence",
            source=f"history://{path}",
            title=f"No history in scope: {_proof_scope[:160]}",
            summary=f"Scoped history search over {_proof_scope} returned zero matches.",
            content_snippet=f"No history in scope: {_proof_scope}",
            attributes={"search_scope": _proof_scope, "match_count": 0},
            observed_at=observed_at,
            retrieved_at=observed_at,
            provenance=EvidenceProvenance(
                tenant_id=tenant_id,
                investigation_id=investigation_id,
                provider_type="search",
                requested_provider_id="code-intelligence",
                actual_provider_id="code-intelligence",
                source_system="code-intelligence",
                retrieval_timestamp=observed_at,
                query_fingerprint=QueryFingerprint(
                    provider_type="search",
                    operation="get_code_history",
                    normalized_query_hash=_hashlib.sha256(
                        _proof_scope.encode()
                    ).hexdigest(),
                ),
                source_location=SourceLocation(
                    system="search",
                    identifier=f"history:{path[:256]}",
                    file_path=path,
                    revision=rev,
                ),
            ),
            freshness=EvidenceFreshness(observed_at=observed_at, retrieved_at=observed_at),
            fingerprint=_hashlib.sha256(
                f"history-absence:{tenant_id}:{investigation_id}:{_proof_scope}".encode()
            ).hexdigest()[:64],
        ),
        _fetch_reason,
    ),
    investigation_id,
)
```

The `history-absence:` fingerprint prefix keeps the proof row distinct from the discovery row's `absence:` fingerprint (no dedupe collision). The existing `history_proof`/`validate_history_claim` positive-match skips (`match_count == 0` → skip) ignore this row exactly as they ignore any zero-match row, so the positive path is unaffected.

**Alternative considered:** A new `ClaimKind` (e.g., `HISTORY_ABSENCE`) with its own partition and telemetry vocabulary. Rejected — doubles the kind surface for one verdict path, and the ABSENCE precedent proves a negative statement verifies under its existing kind. Also considered: reusing the existing discovery-only row as the proof row. Rejected — the verifier's absence gate deliberately excludes `discovery_only` rows, and the `execution.py` comment records why a file/revision-less history row can never satisfy proof; promoting discovery rows to proof would weaken every absence gate at once. Also considered: revising D1 to keep history absence non-verifying until the contract exists. Rejected — the contract change above is one row shape in one empty-path branch, small enough to ship with the verdict path so the acceptance case has a producer from day one.

**Rationale:** Producer and consumer are designed together so the acceptance case is reachable in production, not just in synthetic tests; the integration test exercises the true GET_CODE_HISTORY empty path rather than hand-built rows. The positive path already assembles `path_commits`; reusing that assembly for the negative case keeps one retrieval contract and one file-scope definition. Version-gating follows the Phase 1 D4 pin convention: in-flight `cbv1` rows keep the semantics they started with.

**Trade-offs:** Capped/errored history search is undetectable in the current row schema, so `SEARCH_INCOMPLETE` falls through instead of producing a `NOT_CHECKABLE`-with-blocker verdict. Closing that needs a retrieval-contract change (deferred non-goal). Production reachability depends on the hypothesis citing the absence-row IDs — the design guarantees the verdict *can* be reached and proved, not that any given model will cite correctly.

---

### D2: Call-edge extraction wording matrix behind a synthetic harness

**Decision:** Extend `_extract_call_edge` with the missing interrogative and nominal forms **and** extend untyped kind inference with the matching conservative phrases — the motivating live case ("What calls the order service handler?") arrives untyped, so extraction alone would never fire. Both seams are version-gated (`cbv2`/`cbv3` strict, `cbv1` legacy), and the compatibility claim is pinned I/O equivalence on explicit case lists, not regex identity.

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
# cbv1 legacy: preserves prior outputs for the pinned legacy cases
# (I/O equivalence on the pinned list, not regex identity).
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

Callers pass `legacy=(version == CLAIM_BINDING_CBV1)` following the existing `strict_subject` convention at the `validate_history_claim` call site (`self.claim_binding_version in (CLAIM_BINDING_CBV2, CLAIM_BINDING_CBV3)` → `legacy=False`). Compatibility claim: the legacy table preserves the prior outputs for the pinned legacy cases (see Testing Philosophy) — "byte-identical regexes" is not claimed, since the shared edge row admits `invoked by` where the old one did not. `called by`/`invoked by` share one edge row.

The inference seam is in scope, not just extraction. Untyped kind inference (`effective_claim_kind` via `CALL_EDGE_PATTERNS`) runs before any gate: an untyped "What calls X?" never enters the CALL_EDGE branch even with perfect extraction. D2 therefore extends inference too, with its own version gate — the existing three-substring tuple is untouched, and a new-phrasing matcher applies only on the strict path:

```python
# src/investigation_agent_platform/application/investigation/verification.py
CALL_EDGE_PATTERNS = ("callers of", "caller of", "called by")  # unchanged legacy

# Conservative exact-phrase inference for the new D2 forms. Substring
# style mirrors CALL_EDGE_PATTERNS; entries are full phrases (never bare
# "where is") so they cannot misfire on unrelated prose.
CALL_EDGE_INFERENCE_PHRASES = (
    "what calls",
    "what invokes",
    "who calls",
    "who invokes",
    "call sites of",
    "invocations of",
    "invoked by",
)


@staticmethod
def infers_call_edge(statement: str) -> bool:
    """True when untyped prose unambiguously asks a call-edge question."""
    lowered = f" {statement.lower()} "
    return any(phrase in lowered for phrase in VerificationEngine.CALL_EDGE_INFERENCE_PHRASES)
```

Hook in `check_claim_kind`, where the version is in scope (no signature changes anywhere — `effective_claim_kind` and its existing callers are untouched):

```python
kind = VerificationEngine.effective_claim_kind(hypothesis)
if (
    kind is None
    and self.claim_binding_version in (CLAIM_BINDING_CBV2, CLAIM_BINDING_CBV3)
    and VerificationEngine.infers_call_edge(statement)
):
    kind = ClaimKind.CALL_EDGE
```

So D2 covers typed claims (extraction within the gate) *and* untyped inference (gate entry), both pinned per-version: `cbv1` preserves the current enumeration result on both seams.

**Alternative considered:** Routing extraction through the model (a typed `claim_target` for every CALL_EDGE statement). Rejected — the untyped-prose fallback exists precisely for statements the model did not type; model-routing would move the failure, not remove it.

**Rationale:** Extraction is pure string matching with no retrieval contact, so widening it cannot fabricate evidence — every new edge still faces `call_site_proof`. The risk is a *missed* edge becoming a *wrong* edge only if a pattern misparses; the harness pins each form.

**Trade-offs:** English-only, same as the existing patterns. Non-English statements still yield `(None, None)`.

---

### D3: Construction excluded at every `_call_shown` call site

**Decision:** The enumeration scan passes `exclude_construction` for bare-class symbols, matching `call_site_proof` semantics: a `new X(` line never names a caller for enumeration purposes. All three `call_site_proof` entry points already carry the bare-aware flag, so the enumeration scan is the only behavior change. A test enumerates every `_call_shown` call site and pins its flag, so future call sites cannot silently regress. The flag is threaded under `cbv2`/`cbv3`; `cbv1` preserves the current enumeration result (described as preserved behavior, not as "unflagged in general" — the audit test pins the explicit argument *and* the per-pin outcome).

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

### D4: Reuse the abstention reason router on the terminal FAILED path (built last)

**Decision:** The terminal `verification_failed_or_limit_reached` branch in `conclude_investigation_activity` (`lifecycle.py`, epistemic sub-branch) hardcodes its directive inline and never calls `next_check_for_abstention`; neither gate-fail reason strings nor bucket signals are available at the selection point (loop telemetry is stamped later). D4 v1 therefore does **not** infer the bucket taxonomy from aggregate counters — coarse run-level totals cannot safely distinguish "retrieved but unbound" from "bound but wrong type". Instead, the conclude path loads the investigation's hypotheses through the existing activity-context repositories (the same source the verify path reads), collects their diagnostic reason strings, and calls `next_check_for_abstention` with them — reusing the reason-row routing the abstention path already proves. Fail-closed on every axis: any load error, empty reasons, or router `None` keeps the current hardcoded string byte-identical. No new telemetry fields, no retry-policy change — directive selection only.

```python
# src/investigation_agent_platform/application/worker/activities/lifecycle.py
# (inside the epistemic sub-branch, replacing the fixed _epistemic_check
# assignment for the verification_failed_or_limit_reached reason only;
# budget_exceeded and defer_repair_budget_exhausted keep their strings)
_epistemic_check = (
    "Bind supporting evidence and a cited causal "
    "chain to a candidate hypothesis, then re-verify."
)
try:
    _hyp_reasons: list[str] = _collect_hypothesis_gate_reasons(
        ctx, tenant_id, investigation_id
    )
    _routed = VerificationEngine.next_check_for_abstention(
        "unverified", _hyp_reasons
    )
    _epistemic_check = _routed or _epistemic_check
except Exception:
    pass
```

`_collect_hypothesis_gate_reasons` is a new small helper next to the conclude activity: same-process repository read, returns the persisted per-hypothesis diagnostic reason strings (the same strings the abstention path routes on), `[]` on any failure. Full bucket-taxonomy classification waits on a diagnostic payload produced at the failing gate itself — explicitly deferred follow-on work, not smuggled in through counters.

**Alternative considered:** Inferring buckets from stamped `loop_telemetry` counters plus a bucket→directive map. Rejected — verified against the tree: the counters are stamped after the conclusion is saved, and aggregates cannot distinguish the buckets that matter. Also considered: a learned/model-chosen next check. Rejected — directives must stay deterministic and auditable.

**Rationale:** The reason router already exists and is tested; the terminal path's defect is that it cannot reach it, not that routing logic is missing. Threading the reasons is a wiring change at the actual workflow-to-conclude handoff, which is what the finding requires.

**Trade-offs:** When hypotheses carry no diagnostic reasons (older rows, load failure), the directive stays generic — today's behavior is the floor, never worse. The bucket vocabulary remains forensics-only until the gate-side diagnostic payload exists.

## Data Storage

No new persisted models, no schema changes, no new telemetry fields. D1 adds one producer (a `COMMIT_HISTORY`-typed, non-discovery zero-match row carrying file+revision, banked on the GET_CODE_HISTORY empty path) and appends one `LimitationCode` enum member (`HISTORY_ABSENCE_PROVED`; the enum is append-only by contract). D2 is pure string matching plus module-level pattern tables and one inference predicate. D3 changes a call-site flag. D4 adds one small same-process read helper in the conclude activity. The claim-binding ledger, partitions, and pins are untouched.

## Data Structures

Input and output shapes, all internal to `verification.py`:

```python
# Outcome vocabulary for history_absence_outcome (D1).
# "INSPECTED_EMPTY" -> caller emits VERIFIED negative verdict, citing rows.
# "SEARCH_INCOMPLETE" | "NO_SEARCH" -> caller falls through (returns None).
# Third tuple element: the qualifying rows (empty unless INSPECTED_EMPTY).
HistoryAbsenceOutcome = tuple[str, str, list]  # (outcome, reason, rows)

# Pattern tables for _extract_call_edge (D2).
# Each row: (regex with {ident} slot, "callee" | "edge").
_CALL_EDGE_PATTERNS_ALL: tuple[tuple[str, str], ...]
_CALL_EDGE_PATTERNS_LEGACY: tuple[tuple[str, str], ...]

# Conservative exact-phrase inference for the new D2 forms (D2).
CALL_EDGE_INFERENCE_PHRASES: tuple[str, ...]
```

`VerificationResult` construction for the D1 negative verdict reuses the positive path's fields verbatim (`hypothesis_id`, `status=VERIFIED`, the surrounding `confidence`, `location_validated=True`) with `corroborating_evidence_ids` set to the cited absence-row IDs, the `HISTORY_ABSENCE_PROVED:` explanation prefix, and a single `VerificationLimitation(code=HISTORY_ABSENCE_PROVED)`.

## Interfaces

No user-facing interfaces change: no REST, CLI, MCP, or TUI surface is added or altered. The observable differences are verdict explanations (new `HISTORY_ABSENCE_PROVED:` prefix), `next_check` strings on loop-terminal conclusions, and battery report lines — all existing channels with closed vocabularies.

## Implementation Detail

Wiring order and call sites (the Decisions carry the full code; this section pins where it lands):

0. Producer row in `execution.py` GET_CODE_HISTORY empty path (D1). Inert until consumers exist: positive-match skips ignore zero-match rows.
1. `_CALL_EDGE_PATTERNS_ALL` / `_CALL_EDGE_PATTERNS_LEGACY` constants, rewritten `_extract_call_edge` with `legacy` kwarg, `CALL_EDGE_INFERENCE_PHRASES` + `infers_call_edge`, and the `check_claim_kind` hook (D2). The `call_site_proof` call site passes `legacy=(version == CLAIM_BINDING_CBV1)` following the existing `strict_subject` convention.
2. `history_absence_outcome` static + the `validate_history_claim` no-match branch (D1). The positive-match block above it is byte-untouched and runs first. `LimitationCode.HISTORY_ABSENCE_PROVED` appended to the enum.
3. Enumeration-scan flag (D3). The three `call_site_proof` entry points (aggregate assessments, aggregate edges, single target) are untouched.
4. `_collect_hypothesis_gate_reasons` + conclude-path selection reusing `next_check_for_abstention` (D4), after D1–D3 are merged and live-proved.

Version pins: no new pin, no pin bump. D1/D2/D3 new behavior is active under `cbv2`/`cbv3`; `cbv1` rows keep byte-identical semantics per the Phase 1 D4 convention. If live parity shows any PASS→FAIL flip attributable to D2/D3, the change is reverted and re-designed — parity gates below are hard.

## Migrations

Implementation order is a dependency chain, each step independently committable and revertible:

1. Producer row (`execution.py` GET_CODE_HISTORY empty path) with unit tests on the banked shape (COMMIT_HISTORY-typed, non-discovery, `match_count == 0`, file+revision set, fingerprint distinct from the discovery row) plus an integration test driving the real empty-result path. No consumer reads it yet — it is inert by construction (positive-match skips ignore zero-match rows).
2. Synthetic extraction+inference harness (`tests/unit/test_call_edge_extraction.py`): statement→(callee, caller) matrix covering every `_CALL_EDGE_PATTERNS_ALL` row and every `CALL_EDGE_INFERENCE_PHRASES` entry, plus adversarial non-matches ("callers of" inside unrelated prose, quoted identifiers, `::`/`#` separators, "where is" without a call verb). Red-first: new rows fail before the implementation lands. This harness is a hard precondition for step 3 — it also records the typed-vs-inferred scope decision (both seams covered, per-version).
3. D2 implementation behind the harness, with pinned legacy I/O-equivalence tests (legacy table reproduces old outputs for the pinned case list on both seams; "preserves prior outputs for pinned cases" is the claim, not regex identity).
4. D1 (`history_absence_outcome` + wiring + unit tests: inspected-empty VERIFIED citing the absence rows, mixed positive/empty → incomplete fall-through, incomplete fall-through, no-rows fall-through, positive-path byte-identical, `cbv1` fall-through preserved) + e2e disposition test through evaluate+conclude.
5. D3 (enumeration flag + call-site flag audit test pinning per-pin outcomes).
6. Full suite + sandbox battery parity (all live PASS rows stay PASS; injected-claim driver green) → commit producer+D1–D3 as one unit.
7. D4 implementation, last, against the merged tree (conclude-path wiring + router-reuse tests).

No data migration: frozen baselines regenerate deterministically (provenance-only `git_rev`), and no fixture content changes meaning under this design.

## Testing Philosophy

### Extraction and inference matrix

The harness pins every pattern row independently: each phrasing form maps to its expected (callee, caller), each inference phrase maps to `True` (and near-miss prose maps to `False`), and adversarial inputs (identifiers embedded in unrelated prose, missing callee, empty statement, "where is" without a call verb) map to `(None, None)` / `False`. New phrasings are added as failing rows first. The legacy tables are pinned to prior outputs for an explicit pinned case list on both seams — the compatibility claim is I/O equivalence on those cases, not regex identity. The harness covers typed claims (extraction) and untyped inference (gate entry) because the scope decision is that D2 serves both.

### History absence

Unit tests construct `COMMIT_HISTORY` rows directly: a zero-match non-discovery row on the target file yields `INSPECTED_EMPTY` with the row returned for citation; discovery-only rows yield `SEARCH_INCOMPLETE`; no rows yield `NO_SEARCH`; a non-discovery positive row for the same file alongside a zero-match row yields `SEARCH_INCOMPLETE` (presence blocks absence); a positive match still takes the existing VERIFIED path with the existing explanation byte-identical. A dedicated test asserts the incomplete and no-search states return `None` from `validate_history_claim` (fall-through preservation, not just outcome strings), and a `cbv1` test asserts the negative path still returns `None` under the legacy pin. An integration test drives the real GET_CODE_HISTORY empty path and asserts the banked proof row's shape (type, non-discovery, file+revision, fingerprint distinctness). An end-to-end test runs a synthetic history-empty investigation through evaluate+conclude and asserts the final disposition: VERIFIED negative with `HISTORY_ABSENCE_PROVED`, cited absence-row IDs, normal ConclusionGate passage.

### Construction exclusion

A test enumerates `_call_shown` call sites (excluding the definition) and asserts each passes `exclude_construction` explicitly — no default reliance — and asserts the per-pin outcome trio: a `new OrderServiceHandler(` line alone does not satisfy the enumeration scan for bare `OrderServiceHandler` under `cbv2`/`cbv3`, still satisfies it under `cbv1` (preserved current result), and a real invocation line satisfies it under all pins; declaration + `handler.submit(` same-file join still verifies via `_instance_call_rows`.

### Battery parity

After D1–D3, the sandbox battery must reproduce every current live PASS (L1/L3/L4/L5/L8, injected-claim 6/6) and every current live FAIL must keep its failure *cause* (model-variance shapes, not new gate strings — except intended new `HISTORY_ABSENCE_PROVED` lines on negative-history probes). Any PASS→FAIL flip stops the line.

### Control-loop routing

D4 tests feed canned hypothesis-reason lists into the conclude-path selection and assert the routed directive matches what `next_check_for_abstention` returns for the same reasons; tests assert the current hardcoded strings survive byte-identical when reasons are empty and when the hypothesis load raises. No bucket-counter tests exist by design — counters are not a selection input. No live model in these tests.

## Documentation Plan

### `docs/IAP-claim-proof-gaps-design.md` (this file)

**Audience:** Implementer and owner-review follow-up.

The Phase 0 record: decisions D1–D4 with code, ordering, and parity gates. Updated once at implementation close with outcome notes (same delta-section convention as the phase docs).

### Ledger `docs/IAP_Arch_Eng_Recommendations.log`

**Audience:** Session continuity and release review.

On implementation close, the four `PLANNED` entries flip to `IMPLEMENTED` with file-and-commit evidence, matching the October close-out convention. If D4 splits (design now, build later), its entry moves to `PLANNED` with the new scope rather than staying open under the old text.

## Risks / Trade-offs

### Positive history verdicts shift meaning

**Risk:** A negative statement that previously fell through to another validator (and occasionally VERIFIED elsewhere) now VERIFIES as a history absence. If the zero-match row was itself a retrieval artifact (wrong file scoped, capped search presented as complete), we mint a false VERIFIED; and a commit-less target could prove a narrower claim than the statement makes.

**Mitigation:** Three independent guards. (1) Producer: the proof row carries file+revision and is banked only on a well-formed empty GET_CODE_HISTORY result — discovery-only and unscored rows never qualify. (2) Precedence: any non-discovery positive row for the same file blocks absence (`SEARCH_INCOMPLETE`), so an empty search never overrides existing positive history evidence. (3) Scope binding: a commit-naming target requires a commit-scoped search; a commit-less target yields a verdict whose explanation states exactly the searched file scope. Battery parity pins every current PASS; new `HISTORY_ABSENCE_PROVED` lines appear only on negative-history probes. The capped-search hole is documented (D1 trade-off) rather than papered over.

### Extraction widening misparses prose

**Risk:** A new pattern matches an identifier the statement did not mean as a callee, formulating a wrong CALL_EDGE target.

**Mitigation:** Extraction never verifies — every formulated edge faces `call_site_proof` and binding. The harness adversarial rows pin non-matches. Any live misparse surfaces as a gate-fail reason string, which the battery reports make visible.

### Enumeration tightening drops a legitimate caller

**Risk:** A caller whose only shown evidence is a construction line now fails enumeration where it previously passed.

**Mitigation:** That previous PASS was the exact false-proof the ledger item records (live L2#2 answered on field-construction lines). The `_instance_call_rows` join remains the legitimate path for instance-mediated invocation. Parity watches for flips; a legitimate-caller flip is a re-design trigger, not a test-update trigger. `cbv1` rows are unaffected by construction.

### Terminal reasons unavailable

**Risk:** The conclude path cannot load hypothesis diagnostics (older rows, repository error), so the directive stays generic.

**Mitigation:** That is today's behavior, preserved as the floor — fail-closed keeps the current hardcoded strings byte-identical. Selection affects prose only, never verdicts. Full bucket-taxonomy classification is deferred until a diagnostic payload exists at the failing gate; it is not inferred from counters in the meantime.
