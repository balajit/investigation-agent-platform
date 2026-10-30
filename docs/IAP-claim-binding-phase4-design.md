# Design: Claim binding Phase 4 — smallest-fetch forcing + NOT_CHECKABLE denominator

> Supersession (2026-10-30, 2b step 6): the feed-marker mechanism (`claim_gap_feed` entries, `_carry_claim_gap_feed`, `_smallest_fetch_target`) is retired. The verify turn now carries GAP_FILL work orders per claim gap (`_carry_gap_orders`, order identity in `gap_id`/`claim_ref`) and the slot dispatches the first uncovered, dispatchable order per `SMALLEST_FETCH_ORDER` (`_pick_gap_order`) reading only the newest carrier — tombstone suppression, per-kind caps, coverage stamps, and builder-resolved actions are unchanged. Body retained as the build record.

## Context

Phases 1–3 built per-claim retrieval-to-proof binding: authoritative per-`(kind, target)` partitions with demotion (Phase 1), per-claim stored queries with query-only linkage (Phase 2), and misattribution repair behind the `cbv3` sub-gate (Phase 3). Two motion items were deferred in every phase design as Phase 4:

1. **Smallest-fetch forcing rules** — when a typed claim fails for lack of a specific proof kind, force a targeted fetch for the smallest missing piece instead of generic re-search.
2. **`NOT_CHECKABLE` denominator change in `ConclusionGate`** — unfalsifiable/opinion claims excluded from the publish denominator rather than counted as failures.

Research for this design (two read-only passes over the current tree) produced one scope-correcting finding: **`ConclusionGate` has no claim denominator.** It is a six-blocker fail-closed gate (`verification.py:6814-6893`: VERIFIED status, confidence floor, contradictions, causal-chain-or-waiver, non-empty evidence, freshness/provenance) that counts zero claims. The denominator change therefore lands in the metrics and battery layers plus the waiver path that feeds the gate — not in the gate itself, which needs no new blockers. A second finding: `MissingObligation.next_action` is human-readable prose produced in two places and consumed in zero — no forcing rule can act on it, so Phase 4 must add a machine-readable obligation feed. A third: the only "unfalsifiable" hook today is the `checkable` flag, which is `bool(query)` diagnostics-only and almost never false; no opinion detector exists anywhere.

No `docs/dev/architecture.md` exists in the project.

## References

- `docs/Design_Adoptable_from_ClaimLens.md` (§claim-closure algorithm, `smallest_fetch_for_kind()`, adoption items §4) — the Phase 4 sketch this design implements natively.
- `docs/IAP-claim-binding-phase1-design.md` — partitions, demotion, `cbv1/cbv2` pins, non-goal naming Phase 4.
- `docs/IAP-claim-binding-phase2-design.md` — stored queries, ledger linkage, directive trigger, non-goal naming Phase 4.
- `docs/IAP-claim-binding-phase3-design.md` — repair sub-gate, shadow discipline, non-goal naming Phase 4.
- `docs/IAP-L2-variance-diagnosis-problem.md:342` — evaporation with a smallest-fetch `MissingObligation` as the honest terminal state.
- Code anchors: `forcing.py:545-775` (`_callsite_target`, the only claim-kind-keyed rule), `forcing.py:124-147` (cap pattern), `run_investigation.py:653-911` (cascade; tail slot after the `:900-911` replan block), `action_policy.py:137-155` (rule table — cascade-only forces like `log_search`/`replan` have no rows), `verification.py:460-472` (`MissingObligation`), `verification.py:1432-1522` (`select_ledger_requery`), `retrieval_ledger.py:418` (`RETRYABLE_ACTIONS`), `context_compiler.py:386-431` (ordering, inline tier predicate), `worker/activities/lifecycle.py:1330-1445` (waiver block), `scripts/tranche_b_battery.py:327-381` (`score_supported`), `scripts/agent_metrics.py:380-453` (rate table).

## Goals / Non-Goals

**Goals:**

- Five smallest-fetch forcing rules (DEFINITION, HISTORY, BEHAVIOR, ABSENCE, CAUSAL) in `forcing.py`, each capped, deduplicated against history, placed as the last override in the existing cascade (after the replan block).
- A machine-readable obligation feed (`claim_gaps`) from VERIFY to the forcing rules; prose `next_action` strings stay display-only.
- `RETRYABLE_ACTIONS` stays frozen; smallest-fetch directives dispatch through a closed kind→action allowlist with their own validation, counters, and history dedupe — never entering the ledger-requery extraction, marker, or durable-budget lifecycle.
- Compiler pinning so a smallest-fetch banked row survives to the next VERIFY.
- `ClaimKind.NOT_CHECKABLE` as a model-declared kind with prompt, sanitizer, and DecisionClaim support; retrieval skip; exclusion from obligations, directives, binding, and conclusion-sentence mapping; honest abstention preserved.
- Metrics denominator: checkable-only totals plus a `not_checkable_rate` series in `agent_metrics.py`; battery `not_checkable` dimension and a new live probe L13; L8 unchanged.
- Full unit suite green, ruff check + format clean, no new mypy errors on touched paths, scratch-tenant live spot check (conditional on separate owner approval — this design does not authorize the run).

**Non-Goals:**

- Repointing `CLAIM_BINDING_CURRENT` (stays `cbv1`); no new version pin (D8).
- Changing kind-gate semantics (`definition_window_proof`, `history_proof`, `call_site_proof`, value/state/absence gates untouched).
- Exhaustive opinion-language regexes (detection is model-declared, D5).
- Rewriting `ConclusionGate` blockers (no new blockers; the gate counts nothing by design).
- Any freeze, run-set, budget, rollout, authority, or merge decision (separate owner approvals required).

## Decisions

### D1: Five smallest-fetch forcing rules share one parameterized core after the replan block

**Decision:** Add one pure constructor `_smallest_fetch_target()` in `forcing.py`, parameterized by a `KIND_FETCH` table whose values hold direct builder function references (never string names — `globals().get` by string would fail silent on a typo with the caps making the silence permanent), plus per-kind forced counters (cap 2 each, hard-coded `>= 2` per the `forcing.py:564` precedent; `FORCING_CAP_DEFAULT = 2` at `action_policy.py:29` is the value source). One cascade slot in `run_investigation.py` sits **after the `:900-911` replan block** — the last override before the `:912` story-debug and `:951` shadow capture, so no downstream rule can clobber the smallest-fetch override (the replan block is the current last writer). Its guard tests every existing forced flag (`forced_formulate`, `forced_walk_stack`, `forced_floor`, `forced_reread`, `forced_source_value_read`, `forced_requery`, `forced_callsite`, `forced_log_search`, `forced_replan`, plus `_is_defer` and `_absence_forced`). Kinds evaluate in `SMALLEST_FETCH_ORDER`; the first rule returning a validated target wins the turn. CALL_EDGE keeps its existing rule untouched. Each rule fires only when its gap signal is present (D2 feed), the returned directive passes D3 dispatch validation before any counter increments, and its `gap_id` is absent from `action_history` (no repeat fetch for the same gap — D2 stamp contract). Internal wiring checklist for each of the five counters (the `forced_callsite_count` pattern at `:494, :849-857, :999, :1007, :1042, :1062, :1199, :1485`): init beside `:486-495`; cascade `+= 1` + `_reason_data["forced_smallest_fetch_<kind>"] = True`; append to the `_forced_flags` tuple (`:918-932`), the `forcing_used` dict (`:1000-1008`), the post-execution history enrichment (`:1185-1216`, including the three stamp keys for D2 dedupe), and `loop_telemetry` (`:1522-1567`). No `action_policy.py` rows: smallest-fetch is cascade-only like `log_search`/`replan` (which have workflow cascade blocks but no `_FORCING_RULES`/`_TRIGGER_BY_RULE` rows and no `PlannerSnapshot` fields), so no owed computation, snapshot fields, or trigger mapping are specified.

```python
# src/investigation_agent_platform/application/worker/workflows/forcing.py

# Per-kind smallest fetch: what single retrieval closes this kind of gap.
# action is workflow-dispatchable directly (closed allowlist in D3;
# RETRYABLE_ACTIONS stays frozen); builder is a direct function reference
# (never a string — stringly dispatch fails silent under the caps).
KIND_FETCH: dict[str, dict[str, Any]] = {
    # DEFINITION: one range read over the claimed symbol window (cap 1 row
    # per fire; the window, not the file head, is the smallest fetch).
    "DEFINITION": {"action": "GET_CODE", "builder": _definition_fetch_params},
    # HISTORY: one exact-path history read (tip-tree basename resolution
    # happens provider-side; the claim names the file, not the commit).
    "HISTORY": {"action": "GET_CODE_HISTORY", "builder": _history_fetch_params},
    # BEHAVIOR: one log window for the asserted runtime effect; QUERY_STATE
    # is the fallback leg when the gap names state rather than logs.
    "BEHAVIOR": {"action": "SEARCH_LOGS", "builder": _behavior_fetch_params},
    # ABSENCE: one scoped search expected to return match_count 0 — the
    # proof row for absence is the empty result itself.
    "ABSENCE": {"action": "SEARCH_CODE", "builder": _absence_fetch_params},
    # CAUSAL: one range row per uncovered link; the rule fires per step,
    # the per-run cap still bounds total fires.
    "CAUSAL": {"action": "GET_CODE", "builder": _causal_fetch_params},
}

# Fixed evaluation order when several gaps are open. DEFINITION first: a
# missing symbol window blocks every downstream kind; CAUSAL last: links
# presuppose grounded endpoints. CALL_EDGE is absent — its existing rule
# (_callsite_target) keeps its slot and its counter.
SMALLEST_FETCH_ORDER: tuple[str, ...] = (
    "DEFINITION",
    "HISTORY",
    "BEHAVIOR",
    "ABSENCE",
    "CAUSAL",
)

# Agreement invariant (test-pinned): set(KIND_FETCH) == set(SMALLEST_FETCH_ORDER)
# and every KIND_FETCH value's builder is callable. A misspelled kind or
# unresolvable builder fails the unit suite, not the loop silently.
```

Param builders — one per kind, each `(gap) -> dict | None` (pure; `None` = unbuildable gap, no fire, no count). Keys are exactly what each `_handle_*` reads (`execution.py:1074-1525, :2225-2420, :3274-3312`) — pure provider schema, no identity keys: gap identity travels to the bank boundary exclusively via the workflow-only `smallest_fetch_context` (D4), never inside params (this keeps the `QUERY_STATE` whole-dict passthrough at `execution.py:1094` safe).

- `_definition_fetch_params(gap)`: `{"file_path": target.file_path, "start_line": target.line_start, "end_line": target.line_end, "revision": "HEAD"}` (all but `file_path` omitted when the target lacks them; `revision` honors a gap `scope` revision when present, else `"HEAD"`). `None` when `file_path` is empty.
- `_history_fetch_params(gap)`: `{"file_path": target.file_path, "symbol": target.symbol, "limit": 10}`. `None` when `file_path` is empty (no revision key exists — `_build()` takes none and the fallback hardcodes `HEAD`).
- `_behavior_fetch_params(gap)`: primary leg `SEARCH_LOGS` with `{"keywords": [...from gap query/target symbol...], "limit": 10}`; fallback leg `QUERY_STATE` with `{"template_id": "default", "limit": 10}` when the gap target names state rather than logs (target carries `scope` with a state/environment marker and no log-derivable keywords) — leg selection is explicit in the builder, and the returned directive's `next_action` follows the leg (`SMALLEST_FETCH_ACTIONS_BY_KIND["BEHAVIOR"]` admits both). `None` when neither leg has inputs.
- `_absence_fetch_params(gap)`: `{"query": <gap query or target symbol>, "path_prefix": <gap scope prefix or "">, "revision": "HEAD", "limit": 20}` — expected to return `match_count == 0`; the empty result is the proof row. `None` when no query/symbol is derivable.
- `_causal_fetch_params(gap)`: `{"file_path": ..., "start_line": ..., "end_line": ...}` for the uncovered link's target window (same shape as DEFINITION; the gap target names the link endpoint). `None` when the window is unbuildable.

Directive key discipline (test-pinned): the returned directive's top-level keys are exactly `{next_action, action_parameters, gap_id, claim_ref, fetch_kind}`; `action_parameters` keys are a subset of the handler's provider schema — in particular `action_parameters` never carries `claim_ref`, `target_key`, `fetch_reason`, or any `ledger_requery`-shaped key. Gap identity travels beside params (directive top level) and to the bank via `smallest_fetch_context` (D4), never inside them (the `_callsite_target` `caller_edge_*` precedent at `forcing.py:649-682` travels the other way — those keys ARE consumed by reconciliation at `execution.py:4166-4193`; smallest-fetch keys are consumed by no provider, hence the subset rule).


def _smallest_fetch_target(
    action_history: list[dict[str, Any]] | None,
    forced_count: int,
    gaps: list[dict[str, Any]] | None,
    *,
    kind: str,
    tenant_id: str = "",
    investigation_id: str = "",
) -> dict[str, Any] | None:
    """One smallest-fetch override for a single claim kind, or None.

    Fires when the D2 gap feed names an open gap of this kind. `gaps`
    is the latest `claim_gap_feed` history entry's list (D2 carrier),
    passed in by the cascade — never read from `reason_data`, which
    carries no gap signal. Bounded (max 2 per run per kind); pure.
    Content-free by design: gap identity + history coverage decide.
    Never raises.
    """
    try:
        if int(forced_count or 0) >= 2:
            return None
        spec = KIND_FETCH.get(str(kind or ""))
        if not spec:
            return None
        feed = [g for g in (gaps or []) if isinstance(g, dict)]
        gap = None
        try:
            for entry in feed:
                try:
                    if str(entry.get("kind") or "") != str(kind):
                        continue
                    if not str(entry.get("gap_id") or ""):
                        continue
                    # Covered gaps fall through to the next open gap of
                    # the same kind — a stale feed never suppresses its
                    # siblings. History dedupe is gap_id-keyed (D2 stamp
                    # contract): no (action, params) comparison — gap
                    # identity already determines both.
                    if _smallest_fetch_already_covered(
                        action_history, str(entry.get("gap_id") or "")
                    ):
                        continue
                    gap = entry
                    break
                except Exception:
                    return None
        except Exception:
            return None
        if gap is None:
            return None
        try:
            builder = (spec or {}).get("builder")
            params = builder(gap) if callable(builder) else None
        except Exception:
            return None
        if not params:
            return None
        try:
            action = _smallest_fetch_action_for(str(kind), dict(params))
            params = {k: v for k, v in dict(params).items() if not str(k).startswith("_")}
        except Exception:
            return None
        if not action:
            return None
        return {
            "next_action": action,
            "action_parameters": dict(params),
            "gap_id": str(gap.get("gap_id") or ""),
            "claim_ref": str(gap.get("claim_ref") or ""),
            "fetch_kind": str(kind),
        }
    except Exception:
        return None
```

The cascade slot mirrors the call-site block exactly (own counters, same guard shape — one forced override wins per turn, and the ledger-requery suppression above keeps its precedence):

```python
# src/investigation_agent_platform/application/worker/workflows/run_investigation.py
# (new block immediately after the :900-911 forced_replan block — the last
# override before the :912 story-debug and :951 shadow capture, so no later
# rule can clobber the smallest-fetch override; counters initialized beside
# :486-495 as forced_smallest_fetch_<kind>_count = 0)
            if (
                not _is_defer
                and not _absence_forced
                and not _reason_data.get("forced_formulate")
                and not _reason_data.get("forced_walk_stack")
                and not _reason_data.get("forced_floor")
                and not _reason_data.get("forced_reread")
                and not _reason_data.get("forced_source_value_read")
                and not _reason_data.get("forced_requery")
                and not _reason_data.get("forced_callsite")
                and not _reason_data.get("forced_log_search")
                and not _reason_data.get("forced_replan")
            ):
                # Latest claim-gap feed (D2 carrier). Newest
                # claim_gap_feed entry wins; older feeds are superseded,
                # never accumulated. No VERIFY_HYPOTHESIS action entry
                # exists in history — the feed arrives only via this
                # carrier marker, appended post-verify beside the
                # ledger-directive marker.
                _gap_feed: list[dict[str, Any]] = []
                try:
                    for _evt in reversed(action_history or []):
                        if isinstance(_evt, dict) and _evt.get("kind") == "claim_gap_feed":
                            _raw_gaps = _evt.get("gaps")
                            if isinstance(_raw_gaps, list):
                                _gap_feed = [g for g in _raw_gaps if isinstance(g, dict)]
                            break
                except Exception:
                    _gap_feed = []
                _smallest_fetch = None
                for _fetch_kind in SMALLEST_FETCH_ORDER:
                    try:
                        _counter = {
                            "DEFINITION": forced_smallest_fetch_definition_count,
                            "HISTORY": forced_smallest_fetch_history_count,
                            "BEHAVIOR": forced_smallest_fetch_behavior_count,
                            "ABSENCE": forced_smallest_fetch_absence_count,
                            "CAUSAL": forced_smallest_fetch_causal_count,
                        }[_fetch_kind]
                    except Exception:
                        continue
                    try:
                        _smallest_fetch = _smallest_fetch_target(
                            action_history,
                            _counter,
                            _gap_feed,
                            kind=_fetch_kind,
                            tenant_id=tenant_id,
                            investigation_id=str(inv_id),
                        )
                    except Exception:
                        _smallest_fetch = None
                    # D3 dispatch validation BEFORE any counter or
                    # override: an unmapped (kind, action) pair or empty
                    # params never fires, never counts, and falls through
                    # to the next kind (fail closed, no override).
                    try:
                        if _smallest_fetch and not _smallest_fetch_dispatchable(
                            _fetch_kind,
                            str((_smallest_fetch or {}).get("next_action") or ""),
                            (_smallest_fetch or {}).get("action_parameters") or {},
                        ):
                            _smallest_fetch = None
                    except Exception:
                        _smallest_fetch = None
                    if _smallest_fetch:
                        try:
                            if _fetch_kind == "DEFINITION":
                                forced_smallest_fetch_definition_count += 1
                            elif _fetch_kind == "HISTORY":
                                forced_smallest_fetch_history_count += 1
                            elif _fetch_kind == "BEHAVIOR":
                                forced_smallest_fetch_behavior_count += 1
                            elif _fetch_kind == "ABSENCE":
                                forced_smallest_fetch_absence_count += 1
                            else:
                                forced_smallest_fetch_causal_count += 1
                        except Exception:
                            pass
                        _reason_data = dict(_reason_data)
                        _reason_data["next_action"] = _smallest_fetch["next_action"]
                        _reason_data["action_parameters"] = _smallest_fetch[
                            "action_parameters"
                        ]
                        _reason_data["forced_smallest_fetch"] = True
                        _reason_data["forced_smallest_fetch_kind"] = _fetch_kind
                        # Gap identity stamp (D2/B4 contract): the
                        # post-execution enrichment records
                        # forced_smallest_fetch_gap_id beside the
                        # existing forced_* stamps, so history dedupe
                        # recovers the gap_id without parsing params.
                        _reason_data["forced_smallest_fetch_gap_id"] = _smallest_fetch[
                            "gap_id"
                        ]
                        _reason_data["smallest_fetch_context"] = {
                            "kind": _fetch_kind,
                            "gap_id": _smallest_fetch["gap_id"],
                            "claim_ref": _smallest_fetch.get("claim_ref", ""),
                        }
                        break
```

No `action_policy.py` mirror is specified: smallest-fetch is cascade-only like `log_search`/`replan` (workflow blocks, no `_FORCING_RULES`/`_TRIGGER_BY_RULE` rows, no `PlannerSnapshot` fields, no owed computation) — the internal wiring checklist in the Decision paragraph above is the complete shadow/telemetry wiring.

**Alternative considered:** one shared counter (cap 2) for all five kinds. Rejected — a DEFINITION-heavy investigation would starve HISTORY/BEHAVIOR gaps; per-kind caps preserve the existing per-rule isolation (every rule in `forcing.py` owns its counter).

**Rationale:** Mirrors the only proven claim-kind rule (`_callsite_target`, which fixed L2-class deaths) in shape, caps, dedupe, and cascade placement, so review burden is limited to the five param builders and the gap feed.

---

### D2: The forcing rules consume a machine-readable `claim_gaps` feed, not prose directives

**Decision:** VERIFY produces a compact `claim_gaps` feed that the workflow carries as an opaque history marker to the next turn's cascade (exact carrier below — the `_carry_ledger_directive` precedent: one new history entry appended post-verify, never merged into an existing entry, so replay stays deterministic). Each gap names one typed group with zero citations in its hypothesis. Gap identity is the group identity — `gap_id = kind + "|" + target_key`, exactly the enforcement `group_id` shape (`verification.py:5885`) and the `cited_groups` membership key — because `_distinct_typed_claims()` groups by (kind, target) and merges statements: two same-kind targets are two groups (their `target_key`s differ), while two statements over one target are one group. `claim_ref` (reconstructed from the group's statement+kind via `claim_key()`/`claim_ref()`) rides along as informational context for marker joins and debug only — never as identity, never in dedupe. This makes dedupe rephrase-stable: a model rewording across turns changes the statement hash but not the group, so the same logical target never refires. Dedupe, joins, and history-cover checks all key on `gap_id`. Prose `MissingObligation.next_action` strings are unchanged and stay display-only. Gap openness is decided from the group's own citation presence — never from kept rows: an uncited group has nothing kept by construction, so no kept join is needed or performed (this corrects the earlier draft, which read a `kept_ids` field that `ClaimRetrievalBinding` does not have — `retrieval_ledger.py:487-527` carries only cited-ID partitions `claim_local`/`bank_fallback`/`unknown`, and the enforcement exposes hypothesis-level `kept_ids` plus group identity sets `local_groups`/`cited_groups` only). Cited groups are repair/obligation territory (Phase 3 directives, `MissingObligation`, `next_check`), never smallest-fetch — even when their gate failed. The feed is built from `_distinct_typed_claims` groups minus the enforcement's `cited_groups` — never from verdict status text.

```python
# src/investigation_agent_platform/application/investigation/verification.py
# (called inside verify_root_cause_activity, beside
# collect_verification_diagnostics — the only place the enforcement's
# cited_groups and the typed-group inventory coexist)


def build_claim_gaps(
    hypothesis: Any,
    cited_group_ids: set[str] | None,
) -> dict[str, Any]:
    """Machine-readable smallest-fetch obligations for one hypothesis (pure).

    Returns {"gaps": [...], "not_checkable_groups": ["KIND|target_key", ...]}.
    On ledger-import failure returns the error envelope {"error": ...}
    (never a silent empty feed); the activity sets no data["claim_gaps"].

    A gap names one typed group (kind, target) with NO citation in this
    hypothesis: openness is membership in `cited_group_ids` (the
    enforcement's `cited_groups`, "KIND|target_key" strings at
    verification.py:843-847) — uncited groups have no kept rows by
    construction, so no kept join exists or is needed. Cited groups are
    repair/obligation territory even when their gate failed, never
    smallest-fetch. NOT_CHECKABLE groups (D5) never appear as gaps;
    they are inventoried into not_checkable_groups for the D7
    denominator instead. Kinds outside KIND_FETCH (CALL_EDGE and any
    future kind), UNTYPED groups, and NO_TARGET/empty-target groups
    never appear (a builder cannot fetch without a target; skipping
    here avoids a silent per-turn no-fire). Identity is the group
    identity (kind + target_key); two same-kind targets on one
    hypothesis produce two gaps, two statements over one target
    produce one. Sorted in SMALLEST_FETCH evaluation order (D1)
    for determinism. Never raises.
    """
    try:
        from investigation_agent_platform.application.evidence.retrieval_ledger import (
            _distinct_typed_claims,
            claim_key,
            claim_ref,
            target_group_key,
        )
    except Exception:
        return {"gaps": [], "not_checkable_groups": []}
    order = {"DEFINITION": 0, "HISTORY": 1, "BEHAVIOR": 2, "ABSENCE": 3, "CAUSAL": 4}
    cited = set(cited_group_ids or [])
    gaps: list[dict[str, Any]] = []
    inventoried: list[str] = []
    try:
        groups = [g for g in list(_distinct_typed_claims(hypothesis) or []) if isinstance(g, dict)]
    except Exception:
        return {"gaps": [], "not_checkable_groups": []}
    for group in groups:
        try:
            kind_value = str(group.get("kind_value") or "")
            target = group.get("target_dict") if isinstance(group.get("target_dict"), dict) else {}
            ref = ""
            try:
                # _distinct_typed_claims intentionally exposes the normalized
                # statement/kind parts, not claim_key. Reconstruct the exact
                # Phase 2 identity before hashing; never invent a second key.
                key = claim_key(str(group.get("statement") or ""), kind_value)
                ref = claim_ref(key)
            except Exception:
                ref = ""
            tkey = ""
            try:
                tkey, _ = target_group_key(target)
            except Exception:
                tkey = ""
            if not ref or not tkey or tkey == "NO_TARGET":
                continue
            gid = str(kind_value) + "|" + tkey
            if kind_value == "NOT_CHECKABLE":
                inventoried.append(gid)
                continue
            if kind_value not in order or kind_value == "UNTYPED":
                continue
            # Per-group citation presence decides. Cited groups —
            # kept or not, gate passed or failed — belong to repair
            # (Phase 3), MissingObligation, and next_check, never here.
            if gid in cited:
                continue
            gaps.append(
                {
                    # Group identity, not statement identity: stable
                    # across rephrases; claim_ref is informational.
                    "gap_id": gid,
                    "claim_ref": ref,
                    "kind": kind_value,
                    "target_key": tkey,
                    "target": dict(target),
                    "query": str(group.get("stored_query") or ""),
                    "scope": str(group.get("stored_scope") or ""),
                }
            )
        except Exception:
            continue
    try:
        gaps.sort(key=lambda g: (order.get(str(g.get("kind") or ""), 9), str(g.get("target_key") or "")))
    except Exception:
        pass
    return {"gaps": gaps, "not_checkable_groups": sorted(set(inventoried))}
```

Carrier contract (Temporal replay determinism): the VERIFY activity result `data` gains exactly one key, `"claim_gaps"`, holding the gaps list above (JSON mappings only — no row objects). The workflow carries it forward with a pure carrier mirroring `_carry_ledger_directive` (`forcing.py:281-324`) — one new history entry, never a merge into an existing entry. A valid empty list is significant: it appends a tombstone feed and clears any older gaps. Only malformed or missing `claim_gaps` returns `None` and leaves the prior feed untouched. The `dropped` counters (`unknown_kind`/`missing_gap_id`/`duplicate_gap_id`) are debug-only post-mortem payload on the history entry — read from `action_history` during incident review, consumed by unit tests, never by a production path (they fire only on producer/consumer mismatch, which should be zero).

```python
# src/investigation_agent_platform/application/worker/workflows/forcing.py
# (beside _carry_ledger_directive — same pure-carrier precedent)
def _carry_claim_gap_feed(
    verify_data: dict[str, Any] | None, iteration: int
) -> dict[str, Any] | None:
    """Claim-gap feed marker to append to action_history, or None.

    Pure carry: validates verify result data["claim_gaps"] is a list. A
    non-empty list must contain dicts with a non-empty gap_id and known
    KIND_FETCH kind. An empty list returns a valid tombstone entry, which
    supersedes older feeds. Only malformed or missing input returns None,
    so a malformed feed never breaks the loop. Feed entries are deduped
    by gap_id (first wins); drops are counted, not silent.
    Workflow-safe (no wall-clock, no randomness). Never raises.
    """
    try:
        data = verify_data if isinstance(verify_data, dict) else {}
        raw = data.get("claim_gaps")
        if not isinstance(raw, list):
            return None
        gaps: list[dict[str, Any]] = []
        seen: set[str] = set()
        dropped_unknown_kind = dropped_missing_gap_id = dropped_duplicate = 0
        for entry in raw:
            try:
                if not isinstance(entry, dict):
                    dropped_missing_gap_id += 1
                    continue
                gid = str(entry.get("gap_id") or "")
                if not gid:
                    dropped_missing_gap_id += 1
                    continue
                if str(entry.get("kind") or "") not in KIND_FETCH:
                    dropped_unknown_kind += 1
                    continue
                if gid in seen:
                    dropped_duplicate += 1
                    continue
                seen.add(gid)
                gaps.append(dict(entry))
            except Exception:
                dropped_missing_gap_id += 1
                continue
        # A non-empty input with zero valid entries is malformed, not an
        # empty tombstone. Only raw=[] clears an older feed.
        if raw and not gaps:
            return None
        return {
            "kind": "claim_gap_feed",
            "iteration": int(iteration),
            "turn_issued": int(iteration),
            "gaps": gaps,
            "dropped": {
                "unknown_kind": dropped_unknown_kind,
                "missing_gap_id": dropped_missing_gap_id,
                "duplicate_gap_id": dropped_duplicate,
            },
        }
    except Exception:
        return None
```

```python
# src/investigation_agent_platform/application/worker/workflows/run_investigation.py
# (beside the ledger-marker append at :1317-1322 — verify_res is in scope,
# action_history append is the only mutation, replay-safe)
            _gap_marker = _carry_claim_gap_feed(verify_res.data, iteration)
            if _gap_marker is not None:
                # Append even when gaps=[]: the empty marker is the explicit
                # tombstone that prevents a prior feed from firing again.
                action_history.append(_gap_marker)
```

The cascade reads only the newest `kind == "claim_gap_feed"` entry; older feeds are superseded, never accumulated. An empty newest marker yields `[]` and therefore clears the feed. No `VERIFY_HYPOTHESIS` action entry exists in history (`run_investigation.py:1069-1090` pre-appends the proposed action, `1185-1216` enriches it post-execution — verification runs later at `:1300-1311` and its result is never appended as its own turn), so the carrier marker is the only way the feed reaches the next turn. `reason_data` is never merged — the cascade passes the feed list directly, keeping `recent_actions` and the reason payload untouched.

Producer side (verify activity, beside the existing `recorded_attempts` threading): after enforcement is built (post-repair, `:926` — repairs rebind rows but never alter citation presence, so pre/post-repair `cited_groups` agree; the post-repair read keeps one join site), call `build_claim_gaps(candidate, set(_binding_enforcement.cited_groups))` — the activity scope where both the candidate hypothesis and its enforcement are available (`:466` onward through the marker sites at `:1176-1191`; the engine-internal `collect_verification_diagnostics` at `verification.py:598` receives no enforcement and is deliberately not extended). Set `data["claim_gaps"]` to the returned gaps; the returned `not_checkable_groups` go to the `VERIFY_EVALUATION` marker extras (D7 pattern at `activities/verification.py:1196-1209`), not into the history feed. A hypothesis whose enforcement faulted (`fault=True`) produces no feed — fail closed, like the gate. A ledger-import failure inside the builder returns `{"gaps": [], "not_checkable_groups": [], "error": "ledger_import_failed"}`; the activity treats an error-bearing result as no-feed (no `data["claim_gaps"]` key) plus a warning story-log — an empty feed and a broken import must never look identical.

Gap-identity stamp contract (history dedupe): the cascade stamps the fired directive's `gap_id` into `_reason_data["forced_smallest_fetch_gap_id"]` (D1 snippet), and the execution call copies `_reason_data["smallest_fetch_context"]` into `ExecuteActionInput.smallest_fetch_context`. The post-execution history enrichment records `forced_smallest_fetch`, `forced_smallest_fetch_kind`, `forced_smallest_fetch_gap_id`, and the normalized context in `action_history[-1].update({...})` beside the existing `forced_*` stamps (`run_investigation.py:1185-1216`). Recovery needs no param parsing — the dedupe helper reads the stamp:

```python
# run_investigation.py, ExecuteActionInput construction and enrichment
smallest_fetch_context = _reason_data.get("smallest_fetch_context")
...
smallest_fetch_context=smallest_fetch_context
...
action_history[-1].update(
    {
        "forced_smallest_fetch": bool(_reason_data.get("forced_smallest_fetch")),
        "forced_smallest_fetch_kind": str(
            _reason_data.get("forced_smallest_fetch_kind") or ""
        ),
        "forced_smallest_fetch_gap_id": str(
            _reason_data.get("forced_smallest_fetch_gap_id") or ""
        ),
    }
)
```

```python
# src/investigation_agent_platform/application/worker/workflows/forcing.py
def _smallest_fetch_already_covered(
    action_history: list[dict[str, Any]] | None,
    gap_id: str,
) -> bool:
    """True when gap_id already fired a smallest-fetch turn (pure).

    Scans action_history newest-first for a
    forced_smallest_fetch_gap_id stamp equal to gap_id. Same gap never
    refires; two same-kind targets (different gap_ids) never suppress
    each other. Never raises.
    """
    try:
        want = str(gap_id or "")
        if not want:
            return False
        for evt in reversed(action_history or []):
            try:
                if not isinstance(evt, dict):
                    continue
                if str(evt.get("forced_smallest_fetch_gap_id") or "") == want:
                    return True
            except Exception:
                continue
        return False
    except Exception:
        return False
```

This replaces the earlier draft's `_fetch_already_covered(action_history, action, params)`, which does not exist anywhere in the tree — all existing dedupe is inline per-helper history scans (`forcing.py:264-271, 821-834, 1300-1307`), and no history entry carries a `gap_id` today, hence the explicit stamp above.

**Alternative considered:** reusing the `ledger_requery_directive` marker round-trip for smallest-fetch. Rejected — that path is built for cited/failed claims with attempt counting (`durable >= max_attempts`), while smallest-fetch targets uncited gaps; shoehorning it would tangle two lifecycles. The dedicated `claim_gap_feed` carrier mirrors `_carry_ledger_directive` exactly (one appended entry, validated, never merged), so the new lifecycle stays beside — not inside — the ledger one.

**Rationale:** Separates the machine signal (structured gaps) from the human signal (prose `next_action`), which the research proved no consumer reads. Composite `gap_id` keeps same-kind targets distinct end to end; citation presence (not kept rows, which uncited groups cannot have) keeps the openness test trivially auditable; `claim_ref`/`target_key` reuse the existing ledger identity functions, so gaps join to markers and bindings without new IDs. The `gap_id` stamp makes history dedupe a string equality scan instead of param-shape matching.

---

### D3: Smallest-fetch directives dispatch through a closed kind→action map — RETRYABLE_ACTIONS stays frozen

**Decision:** A workflow-local allowlist maps each kind to its dispatchable actions. `RETRYABLE_ACTIONS` (`retrieval_ledger.py:418`: `SEARCH_CODE, FIND_SYMBOL, FIND_CALL_GRAPH, GET_CODE_HISTORY`) is **not** extended — grounding confirmed that adding actions there would entangle them with the ledger-requery lifecycle (`re_retrieval_directive` rejects unknown actions at `:878-879`; `_should_force_ledger_requery` drops unknown-action markers as invalid; `directive_consumed` budgets per `(query, scope)`; `verification.py:1400,1409` skips non-retryable claim-to-prior-action rescans). Smallest-fetch needs none of that: its actions (`GET_CODE`, `SEARCH_LOGS`, `QUERY_STATE`, `SEARCH_CODE`, `GET_CODE_HISTORY`) are all natively dispatchable workflow actions today (`execution.py` handles each; `QUERY_STATE` is the default `next_action` at `reasoning.py:720` and `run_investigation.py:1108`), so no extraction, marker, or budget changes are required. The closed mapping is checked at dispatch, never inferred from prose:

```python
# src/investigation_agent_platform/application/worker/workflows/forcing.py
# Phase 4 (smallest-fetch): closed kind→action dispatch allowlist.
# Workflow-local — RETRYABLE_ACTIONS is untouched (see above).
SMALLEST_FETCH_ACTIONS_BY_KIND: dict[str, frozenset[str]] = {
    "DEFINITION": frozenset({"GET_CODE"}),
    "HISTORY": frozenset({"GET_CODE_HISTORY"}),
    "BEHAVIOR": frozenset({"SEARCH_LOGS", "QUERY_STATE"}),
    "ABSENCE": frozenset({"SEARCH_CODE"}),
    "CAUSAL": frozenset({"GET_CODE"}),
}
```

Dispatch validation: a forced smallest-fetch turn whose `(kind, action)` pair is not in this table falls back to no override (fail closed — the turn proceeds unforced rather than dispatching an unmapped action). The check lives in the D1 cascade snippet, called after the target is built and before any counter increments or `_reason_data` override — an unmapped pair or empty params never fires, never counts, and falls through to the next kind.

Extraction and attempt counting are deliberately separate from the ledger-requery lifecycle. Smallest-fetch directives never enter `extract_search_query()`, never mint `ledger_requery` markers, and never consume `durable`/`max_attempts` budget: `RETRYABLE_ACTIONS` (the exact set `extract_search_query()` understands) is unchanged, and the new actions are dispatchable only through the smallest-fetch path, which counts attempts its own way — per-kind `forced_*` counters (cap 2) plus `gap_id`-keyed history dedupe (`_smallest_fetch_already_covered`). The gap's stored query/scope, when present, rides the directive as a hint for param building only; when absent, the param builder derives from the gap target. Concretely, dispatch checks three gates in order, all must pass:

```python
# src/investigation_agent_platform/application/worker/workflows/forcing.py
# (beside _smallest_fetch_target; called by the D1 cascade slot before
# counting the fire)
def _smallest_fetch_dispatchable(kind: str, action: str, params: dict[str, Any]) -> bool:
    """Separate validation from ledger-requery eligibility (pure)."""
    try:
        allowed = SMALLEST_FETCH_ACTIONS_BY_KIND.get(str(kind or ""), frozenset())
        if str(action or "") not in allowed:
            return False
        if not isinstance(params, dict) or not params:
            return False
        # No ledger keys are fabricated here: no claim_ref/target_key pair
        # below may create a ledger_requery marker or consume durable budget.
        return True
    except Exception:
        return False
```

So the closed map cannot authorize a malformed or uncounted retry: unmapped pairs fall back to no override, and every fire is counted exactly once against its kind counter with history dedupe preventing repeats. A test pins `RETRYABLE_ACTIONS` to its four existing members — the smallest-fetch path must keep working without touching that set.

**Alternative considered:** opening all gather actions for all kinds. Rejected — the closed mapping is the point: a BEHAVIOR gap must never trigger a code-range read that then grounds nothing, burning budget.

---

### D4: Compiler pins smallest-fetch rows Tier 1 while uncited

**Decision:** Forcing stamps banked smallest-fetch rows with `attributes["fetch_reason"] = "smallest_fetch:<KIND>:<gap_id>"` at bank time. The gap identity reaches the bank boundary through a workflow-only execution context, never through provider parameters: `ExecuteActionInput` gains an optional `smallest_fetch_context` mapping containing only `{"kind", "gap_id", "claim_ref"}`; the workflow copies the D1 directive fields into it, and `execute_action_activity` passes it to the common bank helper. The helper adds the `fetch_reason` attribute to every row produced by that forced action immediately before `save`/`save_batch`; direct singleton bank paths use the same helper. The context is not included in `extract_search_query()`, retrieval-attempt parameters, durable ledger keys, or provider requests, and malformed context causes no stamp rather than a provider-visible parameter.

The compiler treats these rows three ways: (a) Tier-1 exemption — never downgraded to Tier 2 while uncited, so the snippet body survives; (b) priority partition — fetch rows sort into their own partition between `bound_items` and `code_items`, so they are ordered before uncited code and logs instead of competing with them; (c) pool-cut carry — fetch rows from beyond the `MAX_EVIDENCE_POOL` slice are appended to the pool (bounded ≤10 by the forcing caps; duplicates impossible since they come from beyond the cut). Code bodies use an explicit queue, not the general excerpt order: `bound_code = bound_items ∩ code`, `fetch_code = fetch_items ∩ code`, and `unbound_code = code_items`; concatenate those queues in that order, emit at most `MAX_CODE_EXCERPTS` bodies, and only then allow the remaining non-code excerpts to consume ordinary excerpt slots. A fetch row can displace an unbound code body, never a bound code body. Once the model cites the row, the existing bound pin takes over and `fetch_reason` is ignored. Grounding for (c): hydration (`find_by_investigation_id`, `evidence_repository.py:343-376`) is an unordered UNION — owned rows plus shared bank-interest rows with no `ORDER BY` (`:420`'s `order_by(created_at)` belongs to `purge_expired`, not hydration — the earlier draft miscited it). The raw `pool = list(evidence_items[:MAX_EVIDENCE_POOL])` cut (`context_compiler.py:391`) therefore keeps an arbitrary 500, so any fetch row past the cut may shed regardless of age. The carry is required, not merely protective. The budget enforcer's bound-only guarantee (`:754-784`) is untouched — fetch rows remain shrinkable everywhere else, which is correct: they are hints, not citations.

```python
# src/investigation_agent_platform/application/investigation/context_compiler.py
# (in compile_evidence_excerpts — extend the INLINE tier predicate at
# :421-431, which has no _is_tier1 helper; additive clause before the
# existing relevance/recency clauses)
    # Phase 4 (smallest-fetch): a row fetched to close a named gap stays
    # visible until the model cites it (bound pin takes over) — otherwise
    # the fetch the loop paid budget for can be trimmed before VERIFY.
    try:
        attrs = getattr(item, "attributes", None) or {}
        if isinstance(attrs, dict) and str(attrs.get("fetch_reason") or "").startswith(
            "smallest_fetch:"
        ):
            tier = 1
    except Exception:
        pass
    # ... existing relevance/recency clauses unchanged ...

# (in compile_evidence_excerpts, selection order — fetch partition + carry)
    pool = list(evidence_items[:MAX_EVIDENCE_POOL])   # first 500 input order, unsorted
    # Phase 4: carry beyond-cut fetch rows (≤10 by forcing caps). Hydration
    # is unordered (no ORDER BY), so without this any fetch row past the
    # cut may shed regardless of age.
    try:
        _seen = {str(getattr(r, "evidence_id", "") or "") for r in pool}
        for r in evidence_items[MAX_EVIDENCE_POOL:]:
            try:
                _ra = getattr(r, "attributes", None) or {}
                if isinstance(_ra, dict) and str(_ra.get("fetch_reason") or "").startswith("smallest_fetch:"):
                    _rid = str(getattr(r, "evidence_id", "") or "")
                    if _rid and _rid not in _seen:
                        pool.append(r)
                        _seen.add(_rid)
            except Exception:
                continue
    except Exception:
        pass
    bound_items = sorted(... in bound ..., key=_sort_key, reverse=True)
    fetch_items = sorted(... fetch_reason and not bound ..., key=_sort_key, reverse=True)
    code_items  = sorted(... is_code and not bound and not fetch ..., key=_code_sort_key, reverse=True)
    rest        = sorted(... remainder ..., key=_sort_key, reverse=True)
    items = (bound_items + fetch_items + code_items + rest)[:MAX_VISIBLE_EXCERPTS]

# (code-body budget — explicit queue; the shared counter is consumed only
# from bound_code + fetch_code + unbound_code, in that order)
    def _is_code_item(e: Any) -> bool:
        return _is_code_type(str(_get(_get(e, "evidence_type", ""), "value", e) or ""))

    bound_code = [e for e in bound_items if _is_code_item(e)]
    fetch_code = [e for e in fetch_items if _is_code_item(e)]
    unbound_code = [e for e in code_items if _is_code_item(e)]
    code_queue = bound_code + fetch_code + unbound_code
    code_body_ids = {str(getattr(e, "evidence_id", "") or "") for e in code_queue[:MAX_CODE_EXCERPTS]}
    # The existing excerpt loop emits body content only for code_body_ids;
    # all other code excerpts remain Tier 2/metadata-only. Bound rows are
    # selected first, so a fetch row can never evict a cited body.
```

**Alternative considered:** relying on existing bound/recency/relevance surfacing. Rejected — the research shows a fetched-but-uncited row survives only via recency or `relevance >= 0.8`, so the paid fetch can be trimmed before the VERIFY that would cite it; that failure mode wastes the forcing budget silently.

---

### D5: `NOT_CHECKABLE` is a model-declared kind with contained skip semantics

**Decision:** Add `ClaimKind.NOT_CHECKABLE`. The model declares it under a tightened prompt rule (below). The sanitizer accepts the new vocab member (still coercing anything else off-vocab to CAUSAL). `effective_claim_kind` passes a declared `NOT_CHECKABLE` through (no inference applies). Every kind-dispatched path gains an early skip; nothing infers, grounds, or obligates these claims. Scope adopts the ClaimLens observable-outcome test (§3.2) while replacing its `OPINION_CUES` regex with model declaration: **predictions ARE checkable; only taste/preference/value judgments — and unfalsifiable claims no observable could ever settle — are not.** A forecast that names an observable outcome stays checkable even with no code anchor today ("will fail under load" → BEHAVIOR; "latency will exceed 2s after deploy" → checkable forecast) — "no code anchor" alone never disqualifies, because verification can still be attempted against logs, history, or future observation. `NOT_CHECKABLE` is reserved for claims where no possible evidence counts for or against: taste/aesthetic judgments ("best practice", "should be cleaner") and value judgments ("users prefer X" with no measurable criterion). This distinction is why the earlier draft's "predictions without a code anchor" phrasing is dropped — it would have skipped checkable forecasts:

- `check_claim_kind` (`verification.py:3230`): `NOT_CHECKABLE` returns `(False, "NOT_CHECKABLE_SKIPPED")` before any kind branch — the tuple shape callers test (`if kind_gate is not None and not kind_gate[0]`, at `:4164` and `:6493`); `(True, ...)` would VERIFY an opinion claim and is explicitly forbidden. This arm is defense-in-depth only: the primary skip is the evaluate-level projection below, so no opinion group reaches a gate.
- Binding enforcement partition: `NOT_CHECKABLE` groups are excluded from cited/kept/evaporated sets (invisible to binding, like untyped claims).
- `evaluate_hypothesis` (`:6008`) / `_evaluate_hypothesis_inner` (`:6396`): partition assessments by `(claim_kind, target_key)` before binding and verification; discard `NOT_CHECKABLE` groups from the working hypothesis and run the existing verifier on the projected checkable subset (original hypothesis ID preserved). If the projection is empty, return `INCONCLUSIVE` with the new `NOT_CHECKABLE_SKIPPED` limitation (additive `LimitationCode` member at `verification.py:372`, same frozen-enum discipline as Phase 3's `REPAIRED_CITATION` at `:407-410`) — no gates run, no obligations emit. `Hypothesis.claim_kind` remains unchanged for legacy callers, but no mixed current-version row reaches a kind gate without this projection. The original assessment set remains attached to diagnostics/marker inventory so the skipped groups are measurable.
- Abstention-limitations threading (D7 scorer input): at the `_gate_conclusion_or_fail` reasons assembly (`worker/activities/lifecycle.py:1399-1421`), append `"NOT_CHECKABLE_SKIPPED"` when the candidate carries ≥1 assessment and every assessment's `claim_kind` is `NOT_CHECKABLE` (hydrated-enum comparison in that scope — `candidate.assessments` loops at `:730, :889, :1075` prove availability; the marker is not). `_persist_abstention` caps at 20 (`:932`) — one additive entry cannot overflow it. This is the only path that puts the limitation on a persisted conclusion, and the D7 battery assertion pins exactly this string.
- `collect_verification_diagnostics`: no `MissingObligation` for these groups.
- `select_ledger_requery` / `build_claim_gaps`: groups skipped (D2 inventories them instead).
- Marker extras: the verify activity emits `not_checkable_count` (exact int) plus `not_checkable_groups` (the `"KIND|target_key"` identities, capped at 10 with a `not_checkable_truncated` flag mirroring the `repaired` precedent) into the `VERIFY_EVALUATION` marker beside `evaporated_count` — the D7 denominator input. Counts stay exact when identities truncate.
- Conclusion-sentence mapping (`map_conclusion_sentences`/`apply_conclusion_mapping`): opinion sentences are not flagged `CONCLUSION_UNMAPPED`.
- `next_check_for`: excluded claims generate no "cite evidence" directives.

The skip must also be applied at the evidence-write seam: a `NOT_CHECKABLE`
claim never contributes a search query, so no smallest-fetch directive can be
constructed for it; if a defensive caller supplies one, the kind allowlist
rejects it before dispatch. The opinion-only conclusion is therefore a
workflow abstention, not a verifier `VERIFIED`/`ANSWERED` result.

```python
# src/investigation_agent_platform/domain/hypothesis/models.py
class ClaimKind(StrEnum):
    DEFINITION = "DEFINITION"
    CALL_EDGE = "CALL_EDGE"
    BEHAVIOR = "BEHAVIOR"
    ABSENCE = "ABSENCE"
    HISTORY = "HISTORY"
    CAUSAL = "CAUSAL"
    # Phase 4: model-declared unfalsifiable (taste/preference/value
    # judgment, or a claim no observable could ever settle). Predictions
    # that name an observable outcome stay checkable even without a
    # current code anchor. Never retrieves, never grounds, never
    # obligates; excluded from checkable denominators (D7).
    NOT_CHECKABLE = "NOT_CHECKABLE"
```

Prompt rule (added to the per-kind evidence rules, `reasoning.py:327` area): `NOT_CHECKABLE: taste/preference/value/aesthetic judgments, or claims no observable could ever settle ("best practice", "should be cleaner") — emit no search_query, cite no evidence; these claims are skipped, never verified. Predictions that name an observable outcome are checkable even without a code anchor ("will fail under load" stays BEHAVIOR) and must never use this kind. Deontic "should" with a checkable action stays checkable ("the handler should be called once per submit" is BEHAVIOR).` The sanitizer normalizes case before membership (`str(kind).strip().upper()`, `reasoning.py:230-232` — matching is case-exact today, so a model-emitted `"not_checkable"` would otherwise silently coerce to CAUSAL); closed vocabulary becomes `"DEFINITION|CALL_EDGE|BEHAVIOR|ABSENCE|HISTORY|CAUSAL|NOT_CHECKABLE"`.

**Alternative considered:** prose-regex detection (`OPINION_CUES`) in the style of `CONCESSION_PATTERNS`. Rejected — concession patterns work because inability language is narrow and closed; opinion language is open-ended ("should", "best", "will", "I think") and a regex would false-positive on legitimate claims ("the handler should be called once per submit" is a checkable BEHAVIOR claim). Model declaration with prompt grounding is auditable per row; the sanitizer still coerces garbage to CAUSAL.

**Rationale:** Keeps the closed-vocabulary discipline of the whole series: new behavior enters through the taxonomy, not through string matching, so every downstream switch has an explicit case.

---

### D6: Opinion-only probes skip retrieval; mixed investigations exclude cleanly

**Decision:** A new first-position cascade check `_should_force_conclude_not_checkable` fires when every evaluated statement in the current `reason_data` is matched by one or more `NOT_CHECKABLE` claim dicts. It is checked before the `_absence_disconfirmation_target` block (the current first rule at `:653`) because nothing downstream can change the outcome. The cascade sets `next_action = "CONCLUDE"`, `conclusion_readiness = 1.0`, and a workflow-local `_reason_data["forced_not_checkable"] = True` flag (recorded in the history enrichment beside the other `forced_*` stamps for observability), and performs no retrieval. The loop gains one deterministic early exit checked before `execute_action_activity` (`:1110`): `if _reason_data.get("forced_not_checkable"): break` — skipping execute/retrieve/verify for the turn and falling to the existing CONCLUDING handling (`:1492-1503`), which requests COMPLETED and routes through `_gate_conclusion_or_fail`. There the D5 evaluate early-exit yields `INCONCLUSIVE` + `NOT_CHECKABLE_SKIPPED`, the gate denies VERIFIED, and `_persist_abstention` persists the honest abstention with the limitation (D5 threading). No `conclude_investigation_activity` status/reason change is specified — in particular the epistemic-reason tuple (`lifecycle.py:1943-1947`) is untouched, because this path never presents FAILED: the COMPLETED request routes through the gate, not the failure branch. Implementation note (close-out fix): the concluding block maps every unverified run to FAILED, so the break alone does NOT reach the gate — a workflow-local `_d6_concluded` flag is set at the break and concluding maps it to a COMPLETED request (cancelled still wins). Without the flag the gate's NC-marker branch is unreachable in production; the injected live test (opt 2, `reports/live_battery_20261027_phase4_inject.md`) caught this — unit tests cover the gate in isolation with requested-COMPLETED and missed the wiring. Structural pin `test_d6_concluded_requests_completed` guards the mapping. Mixed investigations proceed normally with D5 exclusions — checkable claims fetch, verify, and conclude exactly as today; the opinion claims ride along as skips in binding, obligations, directives, and sentence mapping, and never appear in any denominator (D7).

Inputs are cascade-scope only: `reason_data["claims"]` (DecisionClaim dicts — `reasoner.py:30-53` — each carrying `statement` + `claim_kind` + optional `target`) and `reason_data["hypotheses_evaluated"]` (strings or `{statement,text}` dicts, parsed at `forcing.py:1289-1296`). The join is the statement: the reasoner contract requires a claim's `statement` to match an entry in `hypotheses_evaluated` (`reasoner.py:34-35`), so matching is by normalized (strip + casefold) statement text. No hydrated hypothesis objects exist at workflow scope (repos hydrate them inside activities; `VerifyRootCauseInput` carries only `recent_actions`, `iteration`, IDs — `inputs.py:130-143`), and none are needed: the reason-output dicts already carry the kind. Two further guards fall out of existing shapes: `DecisionClaim.claim_kind` defaults to `CAUSAL` (`reasoner.py:41`), so a model that omits kinds can never satisfy the check; and an "untyped" hypothesis at this layer is exactly an evaluated statement with no matching claim dict — which disables the fast path, since such a hypothesis may simply predate typing.

Truth table for the fast path and mixed semantics:

| Hypotheses evaluated | Claim dicts | Fast path? | Conclusion semantics |
|---|---|---|---|
| Empty set | — | No | Normal loop; nothing to conclude (no opinions asserted) |
| ≥1 evaluated statement with no matching claim dict | any | No | Untyped statements default to CAUSAL-shaped handling per the `DecisionClaim` default — a potentially checkable claim must never be skipped |
| ≥1 claim dict with `claim_kind != NOT_CHECKABLE` | mixed | No | Checkable subset evaluates normally; opinion claims excluded per D5; verdict and limitations describe the checkable subset |
| Every evaluated statement matched, every claim `NOT_CHECKABLE` | all opinion | **Yes** | Force CONCLUDE with honest-abstain guidance; no retrieval burned |

```python
# src/investigation_agent_platform/application/worker/workflows/forcing.py

def _norm_statement(text: Any) -> str:
    """Normalized statement for claim↔evaluation joins (pure)."""
    try:
        return str(text or "").strip().casefold()
    except Exception:
        return ""


def _should_force_conclude_not_checkable(
    reason_data: dict[str, Any] | None,
) -> bool:
    """True when honest abstention is the only possible outcome (pure).

    Every evaluated statement has ≥1 matching claim dict and every
    claim dict is NOT_CHECKABLE. Empty sets, unmatched statements, and
    any checkable claim return False. Never raises.
    """
    try:
        data = reason_data if isinstance(reason_data, dict) else {}
        raw_hyps = data.get("hypotheses_evaluated") or []
        if not isinstance(raw_hyps, list) or not raw_hyps:
            return False
        stmts: list[str] = []
        for h in raw_hyps:
            try:
                if isinstance(h, dict):
                    s = _norm_statement(h.get("statement") or h.get("text"))
                else:
                    s = _norm_statement(h)
                if s:
                    stmts.append(s)
            except Exception:
                continue
        if not stmts:
            return False
        raw_claims = data.get("claims") or []
        if not isinstance(raw_claims, list) or not raw_claims:
            return False
        by_stmt: dict[str, list[str]] = {}
        for c in raw_claims:
            try:
                if not isinstance(c, dict):
                    continue
                s = _norm_statement(c.get("statement"))
                if not s:
                    continue
                by_stmt.setdefault(s, []).append(str(c.get("claim_kind") or "").upper())
            except Exception:
                continue
        if not by_stmt:
            return False
        for s in stmts:
            kinds = by_stmt.get(s)
            if not kinds:
                return False
            for k in kinds:
                if k != "NOT_CHECKABLE":
                    return False
        return True
    except Exception:
        return False
```

**Alternative considered:** letting the normal loop discover unanswerability over several turns. Rejected — each turn burns model calls and budget against claims the design already knows cannot verify; L8 exists precisely because honest abstention should be immediate.

**Rationale:** First-position check because no downstream rule can change the outcome; the truth table makes the unmatched-statement guard explicit so the shortcut can never skip a potentially checkable claim.

Terminal control-flow contract: the workflow checks this predicate immediately after reason output and before any gathering helper. When true, it sets the CONCLUDE/readiness/flag triple above and breaks out before `execute_action_activity` — no provider is ever called, no verify runs, and the turn's history entry records `forced_not_checkable: True` for audit. Concluding follows the existing path unchanged (COMPLETED request → gate → abstention persist), so the persisted conclusion limitations are exactly the `_persist_abstention` shape the D7 battery assertion pins (`NOT_CHECKABLE_SKIPPED` present, zero supporting evidence/hypothesis IDs attached beyond the candidate). A mixed or unmatched turn follows the existing loop unchanged. The loop-harness test pins the whole path: opinion-only input concludes in one turn with zero execute/retrieve/verify activity calls.

---

### D7: Denominators count checkable typed groups; the waiver path is unchanged

**Decision:** The metric unit is the **typed claim group** `(kind, target)` as emitted into `VERIFY_EVALUATION` `bindings`/`ledgers` rows — not hypotheses, investigations, or turns, which the existing series already cover at their own grains. Per marker:

- `checkable_total` = number of `bindings` rows. `NOT_CHECKABLE` groups are excluded from binding by D5, so surviving rows are checkable by construction — no per-row kind filter is needed or performed.
- `not_checkable_count` = the marker extra `not_checkable_count` emitted by the verify activity (D5: exact int beside `evaporated_count`; identities in `not_checkable_groups`, capped at 10 with `not_checkable_truncated`). It cannot be derived from `bindings`/`ledgers` rows precisely because D5 excludes these groups from binding — the count rides the marker extras, not the row lists.
- `not_checkable_rate` (printed) = Σ`not_checkable_count` / Σ(`checkable_total` + `not_checkable_count`) across evaluated markers; a marker with zero typed groups contributes to neither sum (never 0/0).
- Existing binding-kind rates (`evaporated_rate`, `unmapped_rate`, `claim_local_rate`) keep their denominators untouched; the printed line `checkable binding groups: <checkable_total>` (bindings minus `NOT_CHECKABLE` — equal to `checkable_total`) is printed alongside `not_checkable_rate` so reviewers can read either cut (`scripts/agent_metrics.py:440-459`; the script is print-only, there is no emitted `checkable_binding_total` series). No existing series is renamed or redefined.
- Mixed hypotheses contribute group-wise: each binding row counts once under its own kind; `not_checkable_groups` identities carry their own `"KIND|target_key"` prefixes for per-kind review. Untyped assessments (no `claim_kind`) still appear in bindings — only `NOT_CHECKABLE` is excluded — so they count as checkable, matching the D6 truth table.

Battery: `score_supported` gains a `not_checkable` claim dimension, enforced on the inputs the scorer actually receives (`tranche_b_battery.py:327-349`: conclusion status, conclusion limitations, supporting rows via `conclusion.supporting_evidence_ids` + hypotheses `supporting_evidence_ids` — `_supporting_rows:101-113`). The hypothesis API items expose no assessments, so the check is made on the conclusion surface, which the verify activity controls. The check must be invoked before the generic expected-`ABSTAIN` return, not merely added to the answered-claim checks map: when `expect.claim == "not_checkable"`, dispatch to `_check_not_checkable(...)` immediately after loading `expect`/`concl`. That helper owns the complete status matrix and returns `PASS`, `PARTIAL`, or `FAIL`; the generic L8 abstention branch remains unchanged for cases without that claim dimension. The fixture-integrity test must add `L13` to its expected case-ID set.

```python
# scripts/tranche_b_battery.py — new checks entry:
#   "not_checkable": _check_not_checkable(case, expect, concl, hyps, by_id)
def _check_not_checkable(case, expect, concl, hyps, by_id):
    """Opinion probe: abstain honestly with nothing cited (pure)."""
    want = str((expect or {}).get("status") or "ABSTAIN")
    cstatus = str((concl or {}).get("status") or "")
    if want != "ABSTAIN":
        return ("FAIL", "opinion probe misconfigured: want_status must be ABSTAIN")
    if cstatus in ANSWERED_SET:
        return ("FAIL", "answered an opinion probe")
    if cstatus not in ABSTAIN_SET:
        return ("FAIL", f"status={cstatus or 'missing'}")
    rows = _supporting_rows(concl, hyps, by_id)
    if rows:
        return ("PARTIAL", f"cites {len(rows)} supporting rows for an opinion claim")
    limitations = list((concl or {}).get("limitations") or [])
    if "NOT_CHECKABLE_SKIPPED" not in limitations:
        return ("PARTIAL", "abstained without NOT_CHECKABLE_SKIPPED limitation")
    ok, reason = _check_no_failure(rows)   # vacuous on [], kept for symmetry
    if not ok:
        return ("PARTIAL", reason)
    return ("PASS", f"honest abstention ({cstatus}), retrieval skipped")
```

So PASS requires all three: ABSTAIN status, zero supporting rows (retrieval was skipped — nothing cited for the opinion claim), and the `NOT_CHECKABLE_SKIPPED` limitation the D5 path emits. An answered status FAILs (fabricated answer to an opinion probe, mirroring the existing unanswerable arm); an abstention that still cites rows or lacks the limitation degrades to PARTIAL with the failing dimension named. The helper is called before the generic ABSTAIN branch so these outcomes are reachable. Unknown claim kinds still hard-FAIL as today. L8 is unchanged (unanswerable-factual abstention, no kind assertion, no limitation requirement). New live probe L13 (`tests/fixtures/battery_cases/L13.json`, `live: true`):

```json
{
    "id": "L13",
    "symptom": "Is the order filtering implementation beautiful?",
    "context": "Phase 4 L13: pure-taste probe — beauty has no observable; must declare NOT_CHECKABLE, skip retrieval, and abstain honestly.",
    "live": true,
    "expect": {"status": "ABSTAIN", "claim": "not_checkable"}
}
```

The symptom is deliberately pure taste (no observable could settle it), so a model answering structurally would be fabricating — the probe holds the model to declaration-or-fabrication with no groundable middle path.

The waiver path (D6 block in `lifecycle.py`, gate clause in `verification.py:6862-6876`) needs no code change: an opinion-only hypothesis never verifies, so the gate's status blocker and the waiver's `cited_kept_count == 0` arm both resolve to honest abstention exactly as today. The design's "denominator change" is therefore metrics + battery + waiver-input exclusion (D5/D6 keep opinion claims out of the inputs), not new gate blockers — this corrects the phase-series shorthand that named `ConclusionGate`, which counts nothing by design.

**Alternative considered:** adding a claim counter and threshold to `ConclusionGate`. Rejected — the gate is a blocker list by architecture; grafting a denominator onto it would create two sources of publish truth. Counting belongs in measurement (metrics/battery), blocking belongs in the gate.

---

### D8: No new version pin; `CURRENT` stays `cbv1`

**Decision:** Phase 4 ships unpinned. The atomic-deploy rationale: (a) forcing rules are additive retrieval with existing cap infrastructure — they follow the precedent of every prior forcing rule (`_callsite_target`, walk-stack, floor), none of which carry pins; kind gates and verdict semantics are untouched, so in-flight rows cannot change meaning mid-loop. (b) `NOT_CHECKABLE` cannot pre-exist in stored rows: the only emission path is the tightened prompt, and the previous sanitizer coerced any such value to CAUSAL — post-deploy rows are read by post-deploy code in the same deployment; handling is additive skip-logic, and every switch it touches defaults to the current behavior for other kinds. (c) No creation stamp changes; `CLAIM_BINDING_CURRENT` stays `cbv1`; the Phase 3 `cbv2`/`cbv3` discipline is untouched. If a future phase changes gate semantics, it pins per the version-pin contract — this phase does not trigger it.

**Alternative considered:** a `forcing_version` pin mirroring `cbv*`. Rejected — pins exist to version verdict-affecting semantics; forcing only adds bounded evidence, and a pin would split the loop's retrieval behavior across concurrent investigations for no safety gain.

Replay direction (Temporal): in-flight runs adopt the new cascade mid-loop — the first post-deploy VERIFY mints a feed and the new slot evaluates it. Benign by precedent (`forcing.py` contains zero version gating; every prior rule shipped the same way): gaps only arise from post-deploy VERIFY output, counters initialize at zero, and no stored shape changes. Rollback direction: pre-`NOT_CHECKABLE` code reading post-deploy rows coerces the unknown kind to CAUSAL in the sanitizer (`reasoning.py:230-232`, valid against the old six-member vocabulary) and `effective_claim_kind` maps it to `None` → untyped/checkable handling (`verification.py:2849-2854`) — never a crash, never a verify. The old-code-tolerance test pins both directions.

## Data Storage

No new table and no migration in Phase 4. All state rides existing structures:

- `ClaimKind` gains the `NOT_CHECKABLE` member (code enum; stored JSON assessment values are forward-compatible strings; pre-deploy rows cannot contain the value per D8).
- `Evidence.attributes` carries `fetch_reason` on smallest-fetch banked rows (existing free-form dict, no schema change).
- VERIFY activity result `data` gains the `"claim_gaps"` key (transient, never persisted as a column; consumed by the `_carry_claim_gap_feed` carrier into the history entry).
- `action_history` gains `claim_gap_feed` entries (one per valid VERIFY result, including empty-feed tombstones: `kind`, `iteration`, `turn_issued`, `gaps` — newest supersedes older feeds, never accumulated).
- `ExecuteActionInput` gains `smallest_fetch_context` (`kind`, `gap_id`, `claim_ref`) as workflow-only bank metadata; it is stripped before provider dispatch and ledger extraction.
- `VERIFY_EVALUATION` marker extras gain `not_checkable_count` (exact int), `not_checkable_groups` (capped identities), and `not_checkable_truncated` (flag) beside `evaporated_count` — the D7 denominator input.
- Post-execution history enrichment gains `forced_smallest_fetch`, `forced_smallest_fetch_kind`, and `forced_smallest_fetch_gap_id` stamps beside the existing `forced_*` stamps (D2 dedupe contract).
- New fixture `tests/fixtures/battery_cases/L13.json`; update the fixture-integrity test's expected case-ID set to include `L13`; no existing fixture is modified.

## Data Structures

Inputs and outputs stay separated. New structures: the `claim_gaps` item (input to forcing, D2 — `gap_id`, `claim_ref`, `kind`, `target_key`, `target`, `query`, `scope`), the `claim_gap_feed` history entry (D2 carrier — `kind`, `iteration`, `turn_issued`, `gaps`, `dropped` counters; control plane, read newest-only, empty list is a tombstone), the smallest-fetch directive (D1 return — `next_action`, `action_parameters`, `gap_id`, `claim_ref`, `fetch_kind`; validated by D3, never persisted as a ledger marker), the workflow-only `smallest_fetch_context` passed to the bank helper, the `not_checkable_groups` marker extras (D5/D7 — measurement plane, never control), and the `NOT_CHECKABLE` assessment shape (standard `HypothesisEvidenceAssessment` with the new kind; no new fields). No shared input/output model is introduced.

## Interfaces

No user-facing interface changes (REST, CLI, MCP, reports unchanged). Operator-visible effects are confined to diagnostics and measurement:

| Surface | Addition | Description |
|---|---|---|
| `VERIFY_EVALUATION` marker | `not_checkable_count/groups/truncated` extras | Checkable-only denominators (D7); `bindings` rows are checkable by construction |
| `action_history` | `claim_gap_feed` entry | Machine feed for the next turn; newest wins, superseded each VERIFY |
| `agent_metrics.py` | `checkable binding groups` printed line, `not_checkable_rate` | Checkable-only denominators; existing series untouched |
| Battery | L13 live probe, `not_checkable` dimension | Opinion abstention with retrieval skipped; L8 unchanged |
| `loop_telemetry` | `forced_smallest_fetch_<kind>` counters | Per-kind fire counts beside existing forcing counters |

No accessibility impact: no interactive, visual, or CLI-output contract changes (battery reports and markers keep their existing shapes with additive keys).

## Testing

### Forcing rule contracts

Test the shared core in isolation with stubbed builders: fires only on a matching open gap; silent when the gap is absent, the kind mismatches, the counter is at cap, the dispatch validation rejects the pair, or the same `gap_id` stamp already appears in `action_history`. Pin the fixed evaluation order with two open gaps of different kinds, and pin same-kind two-target independence (two gaps, same kind, different `gap_id`s — each fires on its own turn; this is the regression test for the group-identity finding). Pin that a `False` from `_smallest_fetch_dispatchable` increments no counter and leaves `_reason_data` untouched. Pin `set(KIND_FETCH) == set(SMALLEST_FETCH_ORDER)` with all builders callable.

### Gap feed and transport

Test `build_claim_gaps` against a two-group hypothesis: a cited group (in `cited_groups`) with a failed gate produces no gap — it belongs to repair/obligations, not smallest-fetch; an uncited group produces a gap keyed by group identity (`gap_id == "KIND|target_key"`, no statement hash); a cited group with no binding record still produces no gap (citation presence, not records, decides); UNTYPED and NO_TARGET groups produce no gaps; `NOT_CHECKABLE` groups land in `not_checkable_groups`, never in gaps; the suite runs through the real `_claim_binding_partition` (`cited_groups` at `verification.py:5963-5964`), never a hand-built set; output sorted deterministically. Identity regression tests: two groups same kind different targets → two gaps with distinct `gap_id`s; one target with a reworded statement across turns → identical `gap_id`, no refire (rephrase-stability). Import-failure test: ledger-import failure returns the error sentinel (never a silent empty feed) and the activity sets no `data["claim_gaps"]` key. Test `_carry_claim_gap_feed` rejects malformed feeds (non-list, all-invalid non-empty list) with `None`, while raw `[]` produces a tombstone; drops are counted (`dropped.unknown_kind/missing_gap_id/duplicate_gap_id`) and duplicates dedupe first-wins — test each rejection arm asserts its counter; test the workflow appends exactly one entry beside the ledger marker even for the tombstone; test the cascade reads only the newest feed and defaults to `[]` on malformed history. Test the `gap_id` stamp: fired directives record `forced_smallest_fetch_gap_id` in the history enrichment, and `_smallest_fetch_already_covered` matches on stamp equality only.

### Dispatch validation

Test `_smallest_fetch_dispatchable` returns `False` (never `None` — the function returns `bool`) for cross-kind pairs (e.g. BEHAVIOR+GET_CODE), empty params, and unknown actions, and that a `False` result increments no counter and mutates no `_reason_data`; test the directive key discipline (`action_parameters` keys ⊆ handler schema, no identity/ledger keys inside); test that a smallest-fetch fire creates no `ledger_requery` marker and consumes no `durable` budget (the extraction-separation regression test); test `RETRYABLE_ACTIONS` still equals its four original members; test `set(KIND_FETCH) == set(SMALLEST_FETCH_ORDER)` and every builder resolves to a callable.

### NOT_CHECKABLE semantics

Test the D6 truth table row by row: empty set, evaluated statement with no matching claim dict (fast path stays off), mixed kinds, all-opinion (fast path fires); statement joins normalize case/whitespace; a claim dict with defaulted (omitted) kind is CAUSAL and never satisfies the check; sanitizer accepts the new vocab member, normalizes case/whitespace variants (`"not_checkable"`, `" Not_Checkable "` → NOT_CHECKABLE), and still coerces other off-vocab values to CAUSAL; `effective_claim_kind` passes it through; kind gates, binding, obligations, directives, and sentence mapping all skip; opinion-only hypothesis yields INCONCLUSIVE with `NOT_CHECKABLE_SKIPPED` and the abstention path persists the limitation; break-helper unit test pins the exit wiring (`forced_not_checkable` → break, otherwise fall through); scratch-gated live L13 run pins the 1-turn conclude with zero gather dispatches (no Temporal harness in unit). Test a checkable forecast without a code anchor ("will fail under load") stays BEHAVIOR and never takes the NOT_CHECKABLE path; test a taste judgment ("best practice") is declarable as NOT_CHECKABLE and skips retrieval. Old-code tolerance test: pre-`NOT_CHECKABLE` sanitizer/engine on NOT_CHECKABLE rows → checkable handling, never crash, never verify.

### Compiler and metrics

Test `fetch_reason` rows land Tier 1 while uncited and fall back to normal rules once cited; test the fetch partition orders before unbound code, the pool carry preserves beyond-cut fetch rows order-independently (regression: >500-row bank with shuffled input keeps every fetch row visible — hydration is unordered, so the test must not assume newest-shed-first), the forcing context reaches the bank helper without appearing in provider parameters or ledger attempts, and code bodies queue bound-first then fetch (a fetch body displaces unbound code, never bound); test metric denominators on a mixed marker (checkable_total equals binding-row count, `not_checkable_rate` arithmetic from marker extras, exact counts with truncated identities, zero-group markers contributing to neither sum); test battery `score_supported` on the L13 shape (the `not_checkable` branch is exercised before the generic ABSTAIN return: PASS with empty supporting rows + limitation; PARTIAL when rows are cited or limitation is absent; FAIL when answered), update the fixture-ID integrity pin for L13, and confirm L8 still passes unchanged.

### Gates

Full unit suite green, ruff check + format clean, no new mypy errors on touched paths, scratch-tenant live spot check with L1/L2/L8/L13 (conditional on separate owner approval — this design does not authorize the run).

## Documentation Plan

### `docs/IAP-claim-binding-phase4-design.md` (this file)

**Audience:** Implementer and adversarial reviewer.

Covers the complete Phase 4 scope: five forcing rules with the shared core and cascade wiring, the `claim_gaps` feed with group identity and transport contract, dispatch validation separated from ledger extraction, compiler pinning, the `NOT_CHECKABLE` kind with skip points and truth table, metric denominators with pinned units, battery L13, and the no-new-pin rationale. An implementer works from this file alone.

### `docs/IAP_Arch_Eng_Recommendations.log`

**Audience:** Owner.

Append the Phase 4 design entry (OPEN → IMPLEMENTED on acceptance) plus any review findings as their own entries; implemented entries are removed from the log per the ledger rule, retaining only decisions future readers need.

### `docs/IAP-claim-binding-phase1-design.md`, phase2, phase3

**Audience:** Reviewers.

Unchanged — historical phase docs are frozen records. Cross-references flow one way (Phase 4 cites them).

## Risks / Trade-offs

### Smallest-fetch burns budget on uncloseable gaps

**Risk:** A gap whose proof does not exist in the sample (genuinely missing code/history) triggers a bounded fetch that returns nothing, twice per kind per run, delaying honest abstention.

**Mitigation:** Per-kind caps (2), history dedupe (never the same gap twice), fixed evaluation order (cheapest gaps first), and the existing loop budgets above all of it. The battery tracks forced-turn counts per kind, so waste is measured per series, not assumed.

### Forcing changes live retrieval behavior without a pin

**Risk:** D8 ships gather-side changes unpinned; a bad param builder could spam useless retrievals across all investigations until reverted.

**Mitigation:** Builders are pure functions over gap targets with contract tests per kind; dispatch validation fails closed; caps bound blast radius to 2 per kind per run; forced counters in telemetry make any spam immediately visible. Rollback is a single revert with no data migration (no stored shape changes).

### Model under-declares NOT_CHECKABLE to look productive

**Risk:** The model learns that opinion claims skip retrieval and abstain, and routes genuinely checkable claims through `NOT_CHECKABLE` to avoid work — silent coverage loss.

**Mitigation:** `not_checkable_rate` is a printed battery series with per-kind review; spikes are visible. The prompt rule requires checkable phrasing to stay checkable ("the handler should be called once per submit" stays BEHAVIOR; checkable forecasts stay checkable even without a code anchor), and the sanitizer still coerces garbage rather than accepting new synonyms. If abuse appears, the remedy is prompt tightening, not architecture.

### Group identity depends on stable target keys

**Risk:** `gap_id` joins assume `target_group_key` is stable across turns for the same logical target; key instability would duplicate gaps or break dedupe.

**Mitigation:** Identity is statement-independent (`gap_id = KIND|target_key`), so model rephrases across turns cannot mint new gaps — only target-key instability can, and the functions are the same ones the Phase 2/3 ledger paths already depend on for linkage and repair identity; any instability would already break those paths and their pin tests. The two-target regression test plus the rephrase-stability test guard the shape directly.

### L13 adds a live dependency on model opinion behavior

**Risk:** The new live probe asserts the model declares `NOT_CHECKABLE` for a taste/prediction prompt; model drift could flip it to flaky.

**Mitigation:** L13 follows the L8 precedent (live since Tranche B) with the same ABSTAIN-first assertion; the `not_checkable` dimension is additive, so a flip degrades to PARTIAL rather than masking other results. If flaky, demote L13 to offline grading like L9/L10–L12.

## Implementation deltas — owner review A–N (2026-10-27, commits 17e9127 + J set + 8386740)

- **J → L13 mitigation, exercised as written:** model drift made L13 flaky, so L13 is demoted to offline grading (`live:false` + `offline_reason` in `tests/fixtures/battery_cases/L13.json`), the default live plan is L13-free, and the injected-claim live proof driver is adopted at `scripts/live_inject_not_checkable.py` (OVERALL PASS 6/6 on the sandbox).
- **F → D7:** `format_repair_rate()` denominators count checkable typed groups only; non-checkable groups never dilute the rate.
- **L → D5:** the NC early-skip is one shared predicate called from both verify and conclude paths.
- **M → D2:** the `claim_gaps` feed is total — an explicit `claim_gaps: []` is emitted when no candidate exists, so absence of gaps is data, not a missing feed.
- **Baseline provenance (merge-prep fix):** the proof-kernel `git_rev` pin compared `HEAD` for equality, which made every freeze commit self-invalidating. `git_rev` is now recorded provenance excluded from `--check`/test equality; content reproducibility is carried by `battery_case_digests`.
