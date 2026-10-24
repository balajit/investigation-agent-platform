# D3-Family[definition] Phase 0 Approval Package v1.6

> Status: PROPOSED — pending owner review. No implementation authorized.
> This document is a planning artifact prepared under the 2026-10-24
> planning-only authorization. It changes no source files, runs no
> tests, and authorizes no code, commits, authoritative consumption,
> verdict changes, or rollout. Every file/line reference below was
> verified read-only against the working tree on 2026-10-24; line
> numbers refer to that tree and must be re-verified at
> implementation time.
>
> Parent design: `docs/IAP-proof-kernel-d3-definition-plan-v1.3.md`
> (conditional owner design approval 2026-10-24, all 8 §6 items
> decided, Option B semantic boundary). This package is the
> implementation-planning companion the approval conditions require.
> Normative contract language lives in v1.3 §§1–8; this package
> specifies placement, plumbing, verification, tests, flags, and
> measurement against the actual repository. Where they conflict on
> placement or procedure, this package governs the *how*; v1.3
> governs the *what*.
>
> v1.1 revises v1.0 per owner review 2026-10-24 (revision items
> R1–R5). Change log, condensed:
> R1 — normative outcome contract: shadow success is
> `OBSERVATION_ADMISSIBLE`, never proof authorization; the Phase 0
> vocabulary contains no `AUTHORIZED` outcome; G02/G05 updated
> and N13b/N19/N20/N21 added. R2 — currency is a validated/unknown/
> mismatch state derived from same-turn native values, never a
> producer assertion; resident-evidence boundary and
> expected-difference classes E1–E4 defined. R3 — observation
> lifecycle reconciled with no-new-persistence-behavior: same
> write, same transaction, additive bounded keys, differential
> payload tests. R4 — flag tests split into runtime, startup,
> authorized-mode-negative, and rollback categories (non-runnable
> combinations are startup failures, not differentials); sample
> taxonomy frozen to seven classes with per-cell minimums and
> join-violation handling. R5 — §7 traces every owner condition
> to concrete checks. Owner decisions applied: T3 conclude
> mirror in scope, copy-per-flag strict accessors, Option B with
> constrained matching claims.
>
> v1.2 revises v1.1 per owner review 2026-10-24 (findings
> RV01–RV10). Change log, condensed:
> RV01 — association-only admissibility made explicit in the
> observation schema and rule trace (`association_scope:
> "source-range-only"`, machine-readable
> `declaration_requirement_satisfied: false`); `RESOLVED_TARGET_
> BINDING` qualified everywhere as a bound source-range
> association, never declaration resolution.
> RV06 — two-stage evaluation order fixed: stage 1 (association
> admissibility) and stage 2 (declaration-requirement check)
> defined with exact per-stage outputs; dispatch match cannot
> satisfy the declaration requirement by construction.
> RV02/RV08 — authoritative correlation record with execution
> identity, early-return tap-coverage rule, retry/superseded-
> attempt handling, and content-based dedup fallback.
> RV03 — normative per-flag lifecycle table; persistence language
> corrected (additive metadata changes explicitly authorized in
> shadow mode; no persistence-invariance claimed there);
> stamper-no-op counting defined.
> RV04 — trust claim narrowed to deployment-identity pinning;
> executing-byte identity explicitly not claimed from path
> hashing; detectable-vs-outside-guarantee table; static read-set
> proof named as the unallowlisted-read mechanism.
> RV05 — 20-turn protocol labeled a feasibility/coverage gate;
> subcategory presence rule with restricted-conclusion fallback;
> gate-interpretation discipline normative.
> RV09 — per-tap comparison tuples; expected divergences reported
> separately from parity; E1 reworded.
> RV07 — scope statement reconciled with the interface-change
> inventory. RV10 — complete-regression inventory with suites
> and prerequisites identified before implementation. §11 adds
> the finding-to-resolution matrix. Option B, strict flag
> parsing, T3 scope, and fail-closed behavior preserved; no
> AST-based declaration semantics introduced.
>
> v1.3 revises v1.2 per adversarial review 2026-10-24 (findings
> F-A1–A3 blocking, F-B1–B8 precision). Change log, condensed:
> F-A1 — §2.5 currency redefined over audit-bound sources only:
> derivation rule plus candidate bindings labeled unaudited, any
> unbound property defaults to `unknown` with abstention
> semantics (new D3P-N23), and a §9 schema-audit prerequisite
> binds every cell to a field path or declares it
> unknown-by-default. F-A2 — split stamping specified: activity
> stamps execution identity + content, workflow stamps iteration
> on receipt with 1:1 receipt-order join (N22 extended).
> F-A3 — join source must carry a producing-path label;
> unattributable path is an ambiguous join and voids the run set
> (new D3P-N24). F-B1 — bundle uses `commit_id`, aligned with
> native `ClaimTarget`; provenance `revision` recorded alongside
> with equality required. F-B2 — `policy_id` replaced by the
> actually-loaded `triage_route`. F-B3 — `telemetry_omissions`
> registered in the T4 key list and §6 capture-health. F-B4 —
> T2-note threading specified (activity-side list, closure
> capture, merge at return). F-B5 — "set of persistence calls"
> precision. F-B6 — Stage 2 named as an explicit stub with
> pinned constant. F-B7 — §9.0 applicability hedged as presumed
> pending confirmation. F-B8 — retired F03/F04 gap annotated.
> §7 traceability and §11 matrix extended (v1.2→v1.3 section).
>
> v1.4 revises v1.3 per owner review 2026-10-24 (findings C1–C10).
> Change log, condensed:
> C1 — two-layer identity: activity-created `ValidatedDefinitionBundle`
> (no iteration; `bundle_digest` computed once at assembly) vs
> workflow-created correlation envelope (stamps iteration +
> native-outcome snapshot; `envelope_digest` covers both); no
> retroactive mutation; identity stability pinned (new D3P-N25).
> C2 — exact SDK identity tuple from the pinned SDK
> (`temporalio.activity.Info`: namespace, workflow_id,
> workflow_run_id, activity_id, attempt; task_token recorded not
> keyed); accepted-attempt rule pinned to those fields; N22
> requires SDK-contract-shaped records plus a mirror-conformance
> test. C3 — receipt-time pairing is the authoritative join;
> persisted hypothesis rows are consistency cross-checks only
> (mutable current-state, no iteration/path fields); additive
> `producing_path` label listed in §1.6; per-tap join-source
> table; N18/N24 updated. C4 — N13/N13b state exact output
> tuples: association ADMITTED, satisfaction refused; a
> harness-side reference requirement predicate must return
> unsatisfied. C5 — omission counting via in-band flag on the
> conclude result plus harness-side absence detection; the
> harness-loss residual explicitly stated as unmeasurable;
> N17 updated. C6 — §9 split into Stage A (implementation
> authorization: schema audit attached with real field paths,
> baseline commands, feasibility prerequisites), Stage B
> (merge/acceptance: implementation tests, differentials, ledger,
> applicable suites), Stage C (rollout/authority: live evidence +
> separate decision). C7 — prefix retention defined (first 256,
> running counts complete, dropped detail turns excluded as
> truncation events; new D3P-N26). C8 — analyzer specified
> exactly (AST import allowlist + literal-read checks + denylist
> scan) with documented limitations. C9 — Stage 2 described as a
> non-authoritative placeholder and negative guard. C10 —
> "compiled out" replaced with activation language. §7
> traceability, §10, and §11.3 added.
>
> v1.5 revises v1.4 per owner authorization review 2026-10-24
> (provisional CONDITIONAL GO; blocking finding D3P-AUTH-01 plus
> consistency verification). Change log, condensed:
> AUTH-01 — Stage A baseline EXECUTED (not merely documented):
> exact pre-change command run in the implementation
> environment, output attached as §9.4 with counts, environment,
> and exit status; zero failures, so no pre-existing-failure
> carve-outs needed. Consistency verification of §§0–4 against
> §§5–11: F08 harness scope made explicit (startup harness vs
> in-process manipulation; F08 never boots governed paths);
> N13/N13b enforcement language aligned to the named stub +
> consumer-absence + harness predicate (no vague "enforcement
> point"); §10 and §11.3 added (AUTH-01 closure row). No
> architectural change; no new tests; no implementation.
>
> v1.6 is the as-built clarification and acceptance basis for the
> Phase 0 implementation authorized under v1.5 (owner GO
> 2026-10-24). v1.5 remains the authorization basis that governed
> the implementation; nothing below re-opens it. Change log,
> condensed:
> R-IMPL-01 — canonical deduplication identity: duplicate key is
> (activity identity tuple, canonical observation-bytes digest);
> keep-first deterministic; duplicates counted; semantically
> distinct observations (different tap, input set, reason, stage,
> or outcome) can never merge; new D3P negative test pins
> non-merging; static allowlist test extended to all three new
> modules. R-IMPL-02 — T3 conclude-result channel documented:
> T3 observations travel co-located with the conclude native
> outcome in the conclude activity result (durable via workflow
> history); receipt-time pairing applies analysis-side with the
> actual conclude result; verify-turn loop telemetry and T3
> observations have distinct, explicit accounting; new
> `hypothesis_id` binding check makes candidate-mismatched joins
> void (new D3P negative test); N13/N13b/F08 wording reconciled
> with the named triple and harness scope. Compatibility:
> neither refinement adds flags, persistence, native behavior,
> resolvers, migrations, or authority — verified by the unchanged
> §7 gate mapping plus new traceability (§11.4) and the evidence
> below. Evidence: uncommitted working tree at 1389471 (no commit
> per standing instruction); full unit suite, flag-matrix
> differentials, ledger/baseline checks, touched-path integration
> — commands and outputs in §12.3.

## 0. Scope, vocabulary, and assumption register

### 0.1 What Phase 0 is and is not

Phase 0 runs a read-only shadow definition evaluator beside the
native definition gate, records observations, and measures
parity — with zero authority. Concretely out of scope: any
change to native branches, verdicts, or routing; any new
database migration; any resolver invocation; any evidence fetch
the native path did not already perform; any change to activity
*input* schemas. The output- and telemetry-shape changes Phase 0
does make — one additive activity-result key, workflow-local
trace and summary plumbing, additive telemetry keys — apply in
shadow mode only, are each listed in §1.6, and are each covered
by a compatibility test. Contracts affected: internal (activity
result dict, workflow locals, telemetry summary) and one
externally persisted surface (`investigation.metadata
["loop_telemetry"]`, additive keys only; existing consumers must
ignore unknown keys — pinned by D3P-N17/F02). No other
externally consumed contract changes.

### 0.2 Vocabulary (frozen for this package)

- `VERIFY_TURN`: one shadow observation from a per-turn verify
  activity execution (primary tap T1, §1.2).
- `CONCLUDE_GATE`: one shadow observation from the conclude-time
  gate re-evaluation (tap T3, §1.2) — same gate functions, separate
  code path, separately labeled. `CONCLUDE_GATE` is the single
  canonical representation everywhere in this package (observation
  class, bundle `input_set`, telemetry labels); no alternate
  spelling exists and there is no legacy value to migrate, because
  all of it is created by the later implementation, not read from
  existing data.
- `BOUND_FULL`: observation whose input set is the full bound
  supporting set (mirrors site-2 gate, §1.1).
- `CITED_LOCATION`: observation whose input set is the cited
  location-evidence subset (mirrors site-1 gate, §1.1).
- *Eligible turn* (measurement sense, frozen in §6): a turn with a
  recorded native outcome AND a recorded typed observation under
  the frozen comparison rule, `verifier_version == v2`, on a
  definition-claim candidate.
- Shadow outcome vocabulary (normative, Phase 0 — see §2.3):
  `OBSERVATION_ADMISSIBLE` (the observation is well-formed,
  well-bound, and recorded — nothing more), `ABSTAINED` (with a
  named reason code), `UNAVAILABLE` (with named missing inputs).
  The Phase 0 vocabulary contains no `AUTHORIZED` outcome: proof
  authorization does not exist in Phase 0 code, so it cannot be
  produced, consumed, or confused with admissibility.
- Naming qualification (normative, RV01): `RESOLVED_TARGET_BINDING`
  denotes a *bound source-range association* — a bound endpoint
  (symbol + file + range + revision, §2.2 of the parent design) —
  never declaration resolution, declaration semantics, or proof
  sufficiency. Every observation schema and rule trace carries
  `association_scope: "source-range-only"` and the
  machine-readable `declaration_requirement_satisfied: false`
  (§2.3). Wherever this package says "resolved", "bound", or
  "binding" without qualification, it means this association-only
  sense.

### 0.3 Unresolved assumptions (register)

Numbered assumptions with the evidence needed to close each one.
Nothing in this package depends on an assumption being resolved a
particular way — each has a stated fallback. Full register in §8.
Resolved by owner decision in this revision: A-2 (T3 in scope),
A-3 (copy-per-flag accessors), A-5 (Option B constrained by E1–E4
+ no-consumer enforcement, pending owner acceptance as the
constraint evidence). Still open: the load-bearing three are:

- **A-7 (input residency):** only banked `content_snippet` bytes
  plus provenance windows are resident at verify time; full source
  artifacts are not. The bundle (§2) therefore attests resident
  bytes only; anything else is `unavailable-input`, never fetched.
- **A-8 (sample source):** the frozen L1–L12 battery cases carry no
  pre-typed definition claims (verified: no `DEFINITION` /
  `defined in` / `declared in` / `implemented in` /
  `definition is in` / `claim_kind` in any battery file), so turn
  eligibility is emergent. §6 therefore requires a feasibility
  probe before formal collection.
- **A-1 (version coverage):** the legacy v1 path also reaches the
  kind gate. Observations are version-tagged; the eligible set is
  v2-only unless the owner directs otherwise at implementation
  authorization.

## 1. Hook placement and data flow

### 1.1 The authoritative chain (as found)

Per-turn claim verification runs inside
`RunInvestigationWorkflow._run_inner`
(`src/investigation_agent_platform/application/worker/workflows.py:1168`),
once per loop iteration:

```text
workflows.py:1895-1905
  verify_root_cause_activity(VerifyRootCauseInput(tenant_id, investigation_id, triage_route))
```

The activity input carries exactly three scalars — no claim,
hypothesis, evidence, source-byte, or lineage fields
(`activities.py:1027-1032`). Everything else is fetched inside the
activity
(`src/investigation_agent_platform/application/worker/activities.py:5842`):

1. `evidence_repo.find_by_investigation_id` (`activities.py:5874`),
   `hypothesis_repo.find_by_investigation_id` (`activities.py:5891`),
   `investigation_repo.get_by_id` + `resolve_verifier_version`
   (`activities.py:5937,5950`),
   `contradiction_repo.find_by_investigation_id`
   (`activities.py:5962`, fail-open to `[]`).
2. Index (`activities.py:5967`), rank (`activities.py:5972`),
   select candidate (`activities.py:5976-5978`), `_resolve` IDs to
   rows (`activities.py:5985-5995`).
3. `bound` / `refuting` built (`activities.py:6023-6024`);
   statement-grounded range-read join (`activities.py:6029-6039`).
4. Fast-path ladder: code-value (`activities.py:6040`),
   history (`activities.py:6045`), location (`activities.py:6062`),
   doc (`activities.py:6083`), state (`activities.py:6123`).
5. Causal chain (`activities.py:6171-6180`), blocker gate
   (`activities.py:6182-6200`), general path
   `engine.evaluate_hypothesis` (`activities.py:6234-6243`).
6. Telemetry (`activities.py:6245`), legacy-divergence log
   (`activities.py:6259-6281`, observation-only), verdict
   (`activities.py:6282`), persistence (`activities.py:6290-6294`),
   return (`activities.py:6307-6317`).

The native definition gate is
`VerificationEngine.check_claim_kind`
(`src/investigation_agent_platform/application/investigation/verification.py:1958`),
routing `DEFINITION` to `definition_window_proof`
(`verification.py:1779`: path ⊇ line ⊇ symbol, else `(False,
reason)`). It has exactly two production call sites, passing
**different evidence sets**:

| Site | Location | Enclosing function | Evidence set passed |
|---|---|---|---|
| 1 | `verification.py:2678` | `validate_location_claim` (def `verification.py:2610`) | `cited` = `_cited_location_evidence` rows only |
| 2 | `verification.py:3923` | `_evaluate_hypothesis_inner` (def `verification.py:3828`) | full `bound` supporting set |

A second definition backstop follows site 2
(`verification.py:3953-3972`): any `claims_definition` statement
without a range read is `INCONCLUSIVE` regardless of gate outcome.

The conclude path re-runs the same gate functions through a
separate code path: `_gate_conclusion_or_fail`
(`activities.py:6890`; mirror comment `activities.py:7186`;
engine calls `activities.py:7188-7236`; `ConclusionGate.evaluate`
`activities.py:7297-7299`), invoked from
`conclude_investigation_activity` (`activities.py:7587`) at
`activities.py:7633`.

### 1.2 Shadow taps (all read-only, additive-only)

| Tap | Insertion point (anchor, not patch) | Observes | Recorded as |
|---|---|---|---|
| **T1 primary** | `verify_root_cause_activity`, after bound/refuting resolution, before the fast-path ladder (anchor region `activities.py:6023-6052`) | `candidate` (typed `claim_kind`/`claim_target`/statement), `bound`, `refuting`, `triage_route`, `verifier_version`, tenant/investigation/hypothesis ids | `VERIFY_TURN` + `BOUND_FULL` observation |
| T2a mirror | beside site 1 (`verification.py:2671-2680`) | `hypothesis`, `cited`, `kind_gate` tuple | `VERIFY_TURN` + `CITED_LOCATION` note |
| T2b mirror | beside site 2 (`verification.py:3923-3952`, incl. backstop `3953-3972`) | `hypothesis`, `evidence_items`, `kind_gate`, backstop trigger | `VERIFY_TURN` + `BOUND_FULL` note |
| T3 conclude | inside `_gate_conclusion_or_fail` engine-call region (anchor `activities.py:7188-7299`) | same-shape inputs/outputs at conclude time | `CONCLUDE_GATE` observation |
| T4 telemetry | `_derive_loop_telemetry` (`activities.py:1157-1260`) allowlist region (`activities.py:1215-1255`) | shadow-definition summary counts only | telemetry keys (counts/labels/digests, §1.4) |

Tap mechanics, fixed for all taps:

- Each tap copies — never mutates — the observed locals, calls
  only pure functions, and appends to a per-execution observation
list. No tap alters any branch, return value, exception path,
or the set of persistence calls (same calls, same
transactions; payloads gain only the listed additive keys per
§1.4).
- T2a/T2b enter through a new optional parameter
  `observer: Callable | None = None` on the enclosing engine
  methods. When `None` (all existing callers, unchanged), the
  methods satisfy a testable behavioral contract, not a prose
  claim of "identical logic": for the same inputs, D3P-N16
  compares and requires equality of (a) return values, (b)
  serialized outputs, (c) persistence-write call counts and
  payloads, (d) raised exceptions, and (e) observable side
  effects, observer-absent vs observer-present-but-unused. Any
  divergence fails the build. When present (verify activity
  only), the observer is a closure appending to the activity's
  per-execution observation list (F-B4 threading: activity
  creates the list, engine methods only append through the
  closure, activity merges the list into the additive key at
  return); engine methods never own, merge, or return the
  notes.
- T1 runs its pure evaluator inside `try/except`: any exception,
  oversized input, or validation failure records a
  `SHADOW_FAILED` marker and the native path continues exactly as
  today (same isolation shape as the G6 shadow block,
  `workflows.py:1735-1736`).
- The engine methods involved (`check_claim_kind`,
  `effective_claim_kind`, `definition_window_proof`) are already
  pure functions of their arguments; the shadow evaluator reuses
  them as an oracle comparator, never as an authority.

### 1.3 Non-interference argument (to be proven, not asserted)

What shadow code reads: the locals listed in §1.2, the reviewed
dispatch rows loaded at startup (§3), the flag configuration (§5).
What shadow code never touches: `result`, `verified`, the
`hypothesis_repo.save` call, the return payload except one
additive key (§1.4), workflow dispatch, transitions, checkpoint,
or conclude inputs. The workflow never branches on the new
observation lists — the same invariant the G6 block carries
("the workflow never branches on its output",
`workflows.py:89-98,1347-1349`). §4 (D3P-N15–N21) and §5
(D3P-F01/F02 runtime, D3P-F05–F09 startup/negative/rollback)
convert each clause of this argument into a failing-
until-proven test.

### 1.4 Observation channel and persistence lifecycle (R3)

Stated precisely, because "no persistence change" was ambiguous in
v1.0. Phase 0 makes **no new persistence behavior**: no new table,
no new save call, no new write transaction. It does add bounded,
additive keys to payloads that are already persisted today. The
exact lifecycle of one observation:

1. *Residence before summarization*: the per-turn observation
   lives in memory only — first in the activity-return dict key
   `data["shadow_definitions"]`, then appended to the
   workflow-local list `definition_shadow_trace`. Neither is read
   by any branch, dispatch, transition, or verdict path.
2. *Summarization*: the pure `summarize_definition_shadow`
   (mirroring `summarize_trace`) reduces the trace to counts,
   labels, digests, and reason codes. Full per-turn entries are
   carried forward only inside the existing bounded trace shape
   (ids, digests, labels, reason codes — no snippets, no source
   text, same redaction rule as the G6 capture).
3. *Stamping*: summary + bounded trace flow through the existing
   conclude-time path (`workflows.py:1985-2006` →
   `_derive_loop_telemetry` → `_stamp_loop_telemetry` at
   `activities.py:7723-7732`) into the already-persisted
   `investigation.metadata["loop_telemetry"]`. The write is the
   same save call on the same transaction as today
   (`activities.py:1069-1071,7714-7722`); shadow adds zero new
   save calls and shares no new transaction — it rides the normal
   save path or is omitted by the existing fail-open guards.
4. *Overflow* (C7 — retention policy defined, not assumed):
   per-entry size capped; per-turn detail retains the FIRST 256
   entries (prefix, matching the existing `trace[:256]` slice
   pattern at `activities.py:1228`, which keeps the oldest and
   drops the newest). Later entries are dropped with a
   monotonic `dropped_count` plus a `trace_truncated: true`
   marker (never silent). Running counts are computed over ALL
   turns before truncation, so the summary stays complete while
   per-turn detail is prefix-bounded. Effect on correlation:
   dropped turns have no retained observation → they cannot
   join → they are excluded as *truncation exclusions*
   (capture-health class, counted separately); if truncation
   removes turns the floor needs, the run set fails the floor
   (fail closed, never back-filled). Dropped detail is never
   reconstructed from counts. Actual overflow behavior —
   prefix order, counters, marker, and exclusion effect — is
   pinned by D3P-N26.
5. *Telemetry/persistence failure*: the three-layer isolation
   already in `_derive_loop_telemetry` / `_stamp_loop_telemetry`
   / count helpers (`activities.py:1157-1260`, fail-open to `None`
   / zeros with warnings) applies unchanged to the new keys. A
   derivation throw or a stamper no-op yields omission, never a
   native-path effect — pinned by D3P-N17, which asserts the
   native path is unaffected AND the omission is counted.
6. *Rollback*: with both flags off, the additive key is absent,
   the new lists are never created, and `loop_telemetry` contains
   no shadow-definition keys — the persisted payload and the
   write-call sequence are identical to the pre-Phase-0 shape.
   Pinned by D3P-F09, which compares persisted metadata payloads
   and save-call counts flag-off vs the pre-implementation
   baseline, not just returned verdicts.
7. *Live-sample detail*: per-turn detail for §6 analysis is
   retained *only* inside the bounded metadata trace above. No
   other store is written. If detail exceeds the bound, the
   excess is dropped-with-counter (a capture-health event, §6),
   never spilled elsewhere.

Normative per-flag lifecycle (RV03 — the exact activation
boundary for each behavior; "persistence-invariant" is claimed
only where the table says so):

| Behavior | Off/Off | On/Off (shadow mode) |
|---|---|---|
| Shadow list allocation (`definition_shadow_trace`, per-turn observation lists) | never allocated | allocated; workflow-local only, never branched on |
| Activity result key `data["shadow_definitions"]` | absent (D3P-N21) | present, additive; ignored by all native readers |
| Summary construction (`summarize_definition_shadow`) | never runs | runs (pure); output flows only to telemetry |
| Telemetry keys for shadow definitions | absent | present, allowlisted counts/labels/digests only (§1.4 step 3) |
| `investigation.metadata` payload shape | pre-implementation shape, byte-identical | pre-implementation shape PLUS the listed additive keys; every non-shadow field and every native outcome identical (differential-pinned by D3P-F02) |
| Save calls and write transactions | exact legacy sequence | same save calls, same transactions; shadow adds zero new save calls and shares no new transaction — it rides the normal save path or is omitted by the existing fail-open guards |
| Native verdicts, tiers, explanations, confidence, persistence writes, publication, side effects, error semantics | baseline | identical to baseline in every field (D3P-F02 differential) |

Persistence language corrected: in shadow mode the package
does NOT claim persistence-invariance — it explicitly
authorizes the listed additive metadata changes and requires
everything else identical. In Off/Off it requires the old
payload shape and the exact old write sequence (D3P-F09).

Telemetry-omission accounting (C5 — the omitted payload cannot
count itself, so counting is defined through two independent
paths plus a stated residual):

- Path A (in-band flag): if `_derive_loop_telemetry` returns
  `None` (its existing fail-open), the conclude activity sets
  the additive result flag `loop_telemetry_omitted: true`
  (§1.6) instead of telemetry keys. The workflow records the
  flag in run-health (workflow-local, counted even though no
  metadata key was written) and the flag propagates into the
  workflow's terminal accounting read by the collection
  harness. An omission never voids a run set by itself, but a
  run set with zero usable telemetry joins is INSUFFICIENT
  EVIDENCE by the same rule as missing joins.
- Path B (harness-side absence detection): the collection
  harness reconciles completed runs against telemetry-key
  presence. A completed run with no shadow telemetry keys and
  no in-band omission flag is a capture-health event (missing
  telemetry with no accounted cause) and voids the run set
  under the zero-capture-error rule.
- Explicitly unmeasurable residual: if the harness loses the
  terminal run record itself, or the conclude activity result
  carrying the Path A flag is itself lost, no in-system
  mechanism counts the omission. This residual is bounded by
  the harness store's own reliability and is stated here, not
  solved here: the package claims complete accounting of
  omissions detectable through A or B, and names harness-loss
  as outside the guarantee.

If the underlying save itself fails, that is native behavior,
unchanged and out of shadow accounting.

Compatibility consequence (explicit interface-change list, also
§1.6): the `observer` optional parameters, the additive result
key, and the new telemetry keys are backward-compatible shape
changes — old callers pass nothing new, old telemetry consumers
ignore unknown keys — and each is covered by a compatibility
test (D3P-N16 for `observer=None` behavior, D3P-N21 for
key-absent-when-off, D3P-N17 for the Path A omission flag +
Path B absence rule, D3P-N25 for envelope stability). They are
listed in the implementation diff for review; "backward
compatible" is proven per-change, not assumed.

Authoritative correlation record (RV02/RV08; F-A2 split
stamping): the T1 observation itself is the correlation record.
It is emitted before the verdict exists; the native outcome is
joined offline. No single tap site knows every key component —
`VerifyRootCauseInput` carries no iteration (workflow-known)
and execution identity lives only in the activity context —
so the key is assembled in two specified halves:

```text
activity-stamped (at tap time, in-activity):
  (investigation_id, hypothesis_id, input_set, tap,
   namespace, workflow_id, workflow_run_id, activity_id, attempt,
   bundle_digest)
workflow-stamped (on result receipt, in-workflow):
  iteration, receipt_index
full join key (analysis time): all of the above
```

- Execution identity is the exact Temporal SDK contract (C2),
  not an undefined "execution identity": `temporalio.activity.Info`
  (pinned SDK `temporalio>=1.33.0`; field names verified against
  the installed 1.33.0 distribution), read at tap time from the
  activity context (read-only; no behavior change). Correlation
  key components from `Info`: `namespace`, `workflow_id`,
  `workflow_run_id`, `activity_id`, `attempt`. Semantics relied
  upon: `activity_id` is stable across retries of one scheduled
  activity; `attempt` is 1-based and increments per retry;
  `task_token` is unique per delivery (recorded in the
  observation for forensics, NOT part of the key);
  `activity_run_id` is recorded, not keyed (its run-scoping
  semantics are not relied upon). No activity in the worker reads
  execution identity today (verified: no `activity.info` /
  `task_token` use in `application/worker/`), so this is a new
  read-only context access: the implementation cites
  `temporalio.activity.info()` + the five key fields against the
  pinned SDK version and pins them with a unit test.
- Accepted-attempt rule, pinned to those fields: observations
  are accepted iff they arrive in the workflow-accepted activity
  result. Analysis groups observations by
  `(activity_id, attempt)`; the group present in the accepted
  result is the accepted attempt — no inference needed, because
  superseded attempts' outputs never reach the workflow (only
  the final delivered result is received). Any other
  `(activity_id, attempt)` group bearing observations is
  reachable only via test-harness injection and is classified
  `superseded-attempt`: excluded from the denominator, counted
  separately, never joined to another execution's native
  result. D3P-N22 drives this logic with SDK-contract-shaped
  records — constructed from the real `temporalio.activity.Info`
  field tuple (or a pinned mirror of exactly those five fields),
  with varying `attempt`/`activity_id` covering retries,
  interleaved runs, repeated delivery, and superseded attempts —
  plus a mirror-conformance test asserting the mirror matches
  the installed SDK's `Info` field names, so an SDK upgrade
  fails the test for review instead of silently changing
  semantics. If the context is unavailable (unit-test
  harnesses), the fallback is content-based dedup on
  `(bundle_digest, tap)` with keep-first + a monotonic
  `duplicate_count` (never silent).
- `iteration` is stamped by the workflow when it reads the
  additive result key — the workflow owns the loop counter and
  the receipt point. Receipt order is the join: one activity
  execution produces one result, which is received once, so
  activity-stamped observations and the workflow-stamped
  iteration pair 1:1 by receipt. A result whose observations
  cannot be paired to exactly one receipt iteration is an
  ambiguous join → voids the run set. Receipt-order pairing is
  pinned by D3P-N22 (extended: iteration-stamp correctness
  under interleaved and retried executions).
- Cardinality is 0..n per turn: T1 emits at most one
  `VERIFY_TURN` per candidate evaluation and always runs (it
  sits before the fast-path ladder), so every evaluated
  candidate yields at least a T1 coverage record declaring
  `sites_expected: [site1, site2]`; T2a/T2b append
  `sites_reached` only where their sites execute. Where an
  earlier fast path returns first, no mirror note exists — and
  none is fabricated in-band. Instead, coverage is determined
  at join time: the native outcome record used for the join
  must carry a producing-path label identifying which
  fast-path or general path produced it (bound to concrete
  native fields at implementation — candidates:
  `answer_mode`, `code_value_validated`, limitation reason
  codes — or a dedicated label added with the taps, listed in
  §1.6). A turn with T1 but no T2 note and a fast-path native
  outcome is classified `mirror-not-reached
  (reason: <fast-path-name>)` offline — counted and reported,
  never mistaken for agreement and never suppressed. If the
  producing path is not recoverable from the join source, the
  turn is an ambiguous join and voids the run set (F-A3;
  pinned by D3P-N24) — attribution is never guessed.
- Candidate changes: if the native path evaluates a different
  candidate than T1 observed (re-selection, rival promotion),
  the hypothesis_id component differs and the join fails by
  construction → protocol violation handling (§6), never a
  forced pairing.
- Retries and repeated execution: the verify activity runs
  under `maximum_attempts=3` (`workflows.py:1169-1177`,
  applied at `:1904`). Each attempt stamps its own
  `(activity_id, attempt)`; only the accepted attempt's result —
  and therefore only its observations — reaches the workflow.
  Superseded attempts leave no observations in the accepted
  result; any observation bearing a non-accepted
  `(activity_id, attempt)` group is classified
  `superseded-attempt`: excluded from the denominator, counted
  separately, never joined to another execution's native result.
  Duplicate delivery within one accepted execution is caught by
  the `((namespace, workflow_id, workflow_run_id, activity_id,
  attempt), bundle_digest, tap)` dedup rule (keep-first +
  `duplicate_count`). Both rules are pinned by D3P-N22
  (SDK-contract-shaped records + mirror-conformance test;
  extended: iteration-stamp correctness and receipt-order pairing
  under interleaved and retried executions).
- The authoritative join is receipt-time pairing (C3), not
  offline reconstruction: when the workflow receives an
  activity result, it pairs that turn's native outcome (from
  the result data, including the additive `producing_path`
  label, §1.6) with that turn's shadow observations (from the
  additive key) into the envelope (§2.1) — in memory, at
  receipt. The workflow already reads the native result at
  receipt (`verify_res.data`, `workflows.py:1930-1932`);
  pairing adds no branch, only the envelope record. Persisted
  hypothesis rows are consistency cross-checks ONLY, never the
  primary join source: rows are mutable current-state
  (`Hypothesis`: statement/status/supporting_evidence_ids/
  version — no iteration field, no producing-path field,
  status overwritten by later turns), so a row cannot identify
  which turn or path produced an outcome. Cross-check rule:
  where the row is still representative (same hypothesis_id,
  no later evaluation has overwritten it), its status must be
  consistent with the paired native outcome; inconsistency is
  flagged and investigated, never silently resolved in either
  direction. Offline analysis therefore verifies (a) envelope
  digest recomputation (tamper-evident pairing, D3P-N25),
  (b) row cross-check consistency, (c) producing-path label
  presence (absent → ambiguous join → void, D3P-N24), and
  (d) harness-side completeness (every completed run has its
  paired records; missing → void). A missing or ambiguous join
  is a protocol violation that voids the run set (§6), never a
  silent exclusion.
- Per-tap join source (each tap's native outcome comes from a
  named record with exact fields):

| Tap | Native outcome source | Shadow source | Pairing point |
|---|---|---|---|
| T1 | activity result at receipt: `verified, status, confidence, hypothesis_id` + additive `producing_path` label (§1.6) | additive-key observations (T1 note) | workflow receipt → one envelope |
| T2a | gate outcome recorded by the mirror note itself at site 1 (path known: site reached ⟹ location path) | observer note | same envelope, per-site section |
| T2b | gate outcome + backstop trigger state at site 2 (path known: site reached ⟹ general path) | observer note | same envelope, per-site section |
| T3 | conclude activity result at receipt (same fields + `producing_path`) | conclude additive key | workflow receipt → `CONCLUDE_GATE` envelope |
| Fast-path early return (no site reached) | activity result at receipt + `producing_path` label | T1 note only | envelope marked `mirror-not-reached (reason: <fast-path-name>)`; label absent → ambiguous-join void (D3P-N24) |

### 1.5 Sequence diagram

```mermaid
sequenceDiagram
    participant W as Workflow (_run_inner)
    participant V as verify_root_cause_activity
    participant E as VerificationEngine
    participant S as Shadow evaluator (pure)
    participant L as Observation list (workflow-local)

    W->>V: VerifyRootCauseInput(tenant_id, investigation_id, triage_route)
    V->>V: fetch evidence/hypothesis/version/contradictions
    V->>V: rank, select candidate, _resolve → bound/refuting
    V->>S: T1: copy(candidate, bound, refuting, triage_route, version)
    S->>S: assemble ValidatedDefinitionBundle (no I/O)
    S->>S: evaluate vs dispatch rows (pure)
    S-->>V: observation (or SHADOW_FAILED)
    V->>V: append to data["shadow_definitions"] (additive key)
    V->>E: fast-path ladder / evaluate_hypothesis (native, unchanged)
    E->>E: check_claim_kind site 1 (cited) / site 2 (bound)
    E-->>V: T2a/T2b mirror notes (observer param)
    V->>V: verdict → save → return (native paths unchanged; result carries producing_path)
    V-->>W: GenericActivityResult + additive key
    W->>W: receipt pairing: envelope(bundle_digest, iteration, receipt_index, native snapshot) → envelope_digest
    W->>L: append envelope to definition_shadow_trace (never branched on)
    Note over W,L: conclude-time: summary counts → loop_telemetry (T4)
```

### 1.6 Files and functions to change on later implementation

TO MODIFY (anchors; exact hunks at implementation time):

1. `application/worker/activities.py` —
   `verify_root_cause_activity` (anchor `6023-6052`): insert T1
   tap (create per-execution observer list → copy → assemble →
   pure-evaluate → append; pass an `observer` closure capturing
   the same list into the engine calls so T2a/T2b notes land in
   it; merge the list into the additive key at return).
   No other line in the function changes.
2. `application/investigation/verification.py` —
   `validate_location_claim` (anchor `2671-2680`): add optional
   `observer` parameter; record-only call beside the gate.
3. `application/investigation/verification.py` —
   `_evaluate_hypothesis_inner` (anchor `3923-3972`): same
   `observer` parameter; record gate + backstop trigger.
4. `application/worker/activities.py` —
   `_gate_conclusion_or_fail` (anchor `7188-7299`): T3 mirror,
   `CONCLUDE_GATE` class.
5. `application/worker/activities.py` — `_derive_loop_telemetry`
   (anchor `1215-1255`): allowlist extension for the new summary
   keys (counts only), same shape-checking and caps. Planned key
   inventory (F-B3; frozen here, not expandable without review):
   per-outcome counts (`shadow_definition_admissible`,
   `shadow_definition_abstained`, `shadow_definition_unavailable`
   with reason-code breakdown), `telemetry_omissions`,
   `dropped_count`, `duplicate_count`, per-input-set counts, and
   the flag configuration snapshot. No other keys may be added
   by the implementation.
6. `application/worker/workflows.py` — `_run_inner` verify region
   (anchor `1895-1905`): read the additive key AND the native
   result (including `producing_path`) at receipt; build the
   correlation envelope (C1: copy observation, stamp iteration +
   receipt_index, attach native-outcome snapshot, compute
   `envelope_digest`); append the envelope to
   `definition_shadow_trace`. `ConcludeInvestigationInput`
   construction (`1985-2006`): pass the summary. No dispatch,
   transition, or break-condition line changes. Same receipt
   pairing for the conclude activity result (T3 envelope).
7. `application/worker/activities.py` — verify + conclude
   result payloads: additive `producing_path` label naming the
   path that produced the native outcome (fast-path name or
   `general-path`; set at each return point; C3 join source).
   Additive `loop_telemetry_omitted: bool` on the conclude
   result (C5 in-band omission flag). Both additive-only;
   existing readers ignore unknown keys.

TO CREATE (planned paths; names follow the D1/D2 precedent):

8. `domain/proof/definition_observation.py` — `definition/v1`
   record per v1.3 §2.2 (frozen dataclasses, closed vocabularies).
9. `domain/proof/definition_dispatch.py` — strict-`"1"` flag
   accessors, `authorize_definition_use`, `verify_definition_binding`
   (reuses the `DispatchTable` machinery shape from `dispatch.py`;
   new rows, new fixture — no change to D1 rows).
10. `application/investigation/definition_shadow.py` — bundle
    assembly from loaded rows + pure shadow evaluator + observation
    schema; imports nothing from infrastructure, repos, or clients
    (statically enforced, §2.2).
11. `tests/fixtures/proof_kernel/definition_dispatch_mapping.json`
    + golden/invalid vector files (pattern:
    `dispatch_mapping.json`, `canon_v1_vectors.json`).
12. `tests/unit/test_d3_*.py` — the §4/§5 suites.

Explicit non-goals: no change to any native branch or return
shape; no migration; no resolver code; no activity-input schema
change (assembly reuses the native fetches in-activity, which is
exactly why T1 sits where it does); no full-artifact acquisition
layer (A-7: resident bytes only).

Interface and output-shape changes (explicit list for diff
review — each is backward-compatible AND compatibility-tested,
none is assumed safe): (i) optional `observer` parameters with
`None` default on two engine methods (D3P-N16 behavioral
contract); (ii) one additive dict key
`data["shadow_definitions"]` on the activity result (absent
unless the shadow flag is on; D3P-N21); (iii) additive
allowlisted keys in the telemetry summary (ignored by existing
consumers; D3P-N17); (iv) additive `producing_path` label on
verify + conclude activity results (C3 join source; ignored by
existing readers; missing label → ambiguous-join void, never
inferred); (v) additive `loop_telemetry_omitted` flag on the
conclude result (C5 in-band omission signal); (vi) two new
modules + fixture/test files that no existing code imports.
Unreviewed activity/output contract changes beyond this list
are out of scope for any implementation authorization under
this package.

## 2. Validated-bundle plumbing

### 2.1 `ValidatedDefinitionBundle` schema + correlation envelope (normative; C1 two-layer identity)

Frozen, digest-bound, assembled at T1 from already-loaded locals
only. The bundle carries NO iteration: iteration is
workflow-known, and the activity must not include a value it has
not received. Identity is therefore two-layer — the activity
creates the observation identity, the workflow creates the
correlation envelope — with one digest per layer, each computed
once, never retroactively mutated:

```text
# Layer 1 — activity-created, immutable at assembly time:
bundle_version: "definition-bundle/v1"
investigation_id, hypothesis_id: str
verifier_version: str            # recorded; eligibility is v2-only per A-1
triage_route: str                # F-B2: the actually-loaded native input (VerifyRootCauseInput.triage_route); no derived policy_id, whose stability was never verified
input_set: "BOUND_FULL" | "CITED_LOCATION" | "CONCLUDE_GATE"
claim_kind_snapshot: "DEFINITION"
claim_target_snapshot: {symbol, file_path, line_start, line_end,
                        scope, declaring_type | omitted, commit_id | omitted}
                        # F-B1: commit_id mirrors native ClaimTarget.commit_id; SourceLocation.revision recorded alongside in bound_rows; where both exist they must agree (else mismatch → abstain)
bound_rows: [{evidence_id, resident_content_digest, provenance_window (file_path, line_start, line_end, revision),
              lineage_link} ...]   # resident bytes only (A-7)
refuting_ids: [str ...]
resident_window_bytes_ref: digest  # digest of the banked snippet bytes used
context_anchor: {investigation_id, application_id}
                     # F-A1/C6 audit: the only typed context-side values proven loaded (Investigation.application_id + ids); repo/revision/scope have no typed context-side source → those properties have no context side (§2.5)
currency: {repository: validated | unknown | mismatch,
           revision: validated | unknown | mismatch,
           scope: validated | unknown | mismatch}
           # R2: state derived from same-turn native values (§2.5);
           # never a producer assertion; unknown/mismatch → abstain
unavailable: [str ...]             # anything not resident, named — never fetched
bundle_digest: "sha256:..."        # canon/v1 over all of the above; computed once at assembly
```

```text
# Layer 2 — workflow-created correlation envelope, stamped once at receipt:
observation: <layer-1 record incl. bundle_digest>   # copied, never mutated
iteration: int                         # workflow loop counter, stamped on receipt (F-A2)
receipt_index: int                     # workflow-monotonic per-receipt sequence (replay determinism)
native_outcome_snapshot: {verified, status, confidence, hypothesis_id,
                          producing_path}  # from the activity result at receipt (C3)
envelope_digest: "sha256:..."          # canon/v1 over (bundle_digest, iteration, receipt_index, native_outcome_snapshot); computed once at receipt
```

Rules: the activity never sees `iteration`; the workflow never
mutates the bundle (it copies the observation into the
envelope — hash-before/hash-after equality is pinned by
D3P-N25). `bundle_digest` covers the activity fields only;
`envelope_digest` covers the correlation. Replay of the same
receipt sequence reproduces both digests exactly (D3P-N25).

`unavailable` non-empty forces evaluator abstention
(`unavailable-input`); it is a first-class outcome, counted in
§6, never a silent skip.

### 2.2 Producer rules and static enforcement

Allowed producer inputs (exhaustive): `candidate`, `bound`,
`refuting`, `evidence_by_id` (read-only), triage_route, version
string, the three scalar ids. Forbidden: any repository object,
client, resolver, network, clock-for-semantics, or the native
`result`. Enforcement is a static import test on the new module
(same technique as the D1-G suite,
`tests/unit/test_d1_flag_equivalence.py`, and the G6-harness
static proof): the shadow module's import closure must not
contain infrastructure, repo, client, resolver, or workflow
modules. Presence-in-memory is the admission criterion: assembly
asserts each referenced row is in the passed locals; anything
else lands in `unavailable`.

### 2.3 Consumer: pure shadow evaluator (R1; RV01/RV06 two-stage order)

`evaluate_definition_shadow(bundle, dispatch_table,
registry_snapshot) -> ShadowDefinitionObservation`, deterministic,
no I/O. Evaluation runs in two strictly ordered stages with
distinct outputs — association evaluation first, declaration-
requirement check second — so a dispatch match cannot be
interpreted as declaration satisfaction by construction:

- Stage 1 — association admissibility: schema/validity checks →
  canonical digest recompute → dispatch exact lookup →
  lineage/provenance/currency checks against the bundle →
  `stage1: ASSOCIATION_ADMISSIBLE | ABSTAINED | UNAVAILABLE`
  with reason code + rule trace. The dispatch lookup enforces
  row match only; it does not evaluate, satisfy, or imply the
  declaration requirement.
- Stage 2 — declaration-requirement placeholder and negative
  guard (C9): this stage does NOT evaluate whether the
  observation satisfies a declaration-proof requirement —
  Phase 0 cannot demonstrate that a future proof requirement
  has been correctly evaluated, and the package claims no such
  thing. It is implemented as an explicitly named stub
  (`STUB_PHASE0_DECLARATION_CHECK_UNIMPLEMENTED`), which
  executes, returns `declaration_requirement_satisfied: false`,
  and does nothing else (F-B6: a named stub with a pinned
  constant, not a plausible-looking evaluation — a unit test
  pins the stub identity and the constant, so any future logic
  beyond the constant fails the test rather than silently
  passing). The stub exists so the check has a code path that
  Phase 1 must deliberately replace; the constant output is
  independent of how strong the Stage 1 association is: even a
  fully valid association yields `false` here (D3P-N13b). What
  is "rejected" by N13 is declaration satisfaction and
  downstream use as proof — never the Stage 1 association
  itself, which is admitted as observed.

Output schema: `observation_id`, `bundle_digest`, `outcome:
OBSERVATION_ADMISSIBLE | ABSTAINED | UNAVAILABLE`, `reason_code`
(closed vocabulary), `rule_trace` (names `association_scope:
"source-range-only"` on every record), `association_scope:
"source-range-only"` (constant, schema-pinned),
`declaration_requirement_satisfied: false` (constant in Phase 0,
machine-readable, schema-pinned), `input_set`,
`flag_config_snapshot`.

Normative distinctions (blocking contract, not test coverage):

- `OBSERVATION_ADMISSIBLE` means exactly: the Stage 1
  source-range association is well-formed, well-bound, and
  recorded. It is admissible *for shadow observation*. It is not
  proof authorization, does not satisfy any declaration
  requirement, and cannot be consumed as a verified claim.
- The Phase 0 outcome vocabulary contains no `AUTHORIZED` value.
  Proof authorization does not exist in Phase 0 code: there is no
  code path that produces it, no consumer that reads it, and no
  telemetry field that carries it. This is enforced, not
  asserted, by three checks: (a) a static test that the
  `AUTHORIZED` literal (and any alias) appears nowhere in the new
  shadow modules (D3P-N19a); (b) a consumer-absence test proving
  no verdict, persistence, publication, or gate code imports or
  reads the observation lists (D3P-N19b); (c) the outcome enum
  itself, which cannot represent authorization.
- A missing declaration therefore remains insufficient *even when
  every digest, provenance, and lineage check passes*: the
  evaluator returns `OBSERVATION_ADMISSIBLE` for the association
  and the Stage 2 check independently records
  `declaration_requirement_satisfied: false` (D3P-N13, D3P-N13b).
  A green admissibility test must never read as proof
  satisfaction — §4 states each golden's exact non-implication.

### 2.4 No-independent-acquisition audit

D3P-N-series tests pin: rows absent from locals are named in
`unavailable` (never fetched); assembly with an empty `bound`
yields `unavailable-input`, not an error and not a fetch; the
static test fails the build if the shadow module gains a fetch
capability. Live-sample analysis (§6) cross-checks: every
`VERIFY_TURN` observation must join to rows the native path
fetched in the same turn (join key evidence ids); an observation
referencing unfetched rows is a protocol violation and voids the
run set.

### 2.5 Currency contract (R2; F-A1 audit-gated sources)

Each `currency` property (`repository`, `revision`, `scope`) is a
three-state value — `validated`, `unknown`, or `mismatch` —
derived only from same-turn native values already in memory. No
extra read, fetch, or acquisition is performed to establish it.

Derivation rule (normative, source-independent): a property is
`validated` iff two independently-loaded native values — one
from the claim/row side, one from the investigation-context
side — are both present and byte-equal; any side missing →
`unknown`; present but unequal → `mismatch`. `unknown` or
`mismatch` on any property forces evaluator abstention with
reason `stale-or-unknown-context` — never a weakened pass. If
the native locals cannot establish currency either way, the
correct Phase 0 result is abstention (D3P-N05, D3P-N14); the
contract is never weakened to make the observation appear
complete.

Source bindings (F-A1): the v1.2 table named sources that do
not exist under those names — `SourceLocation` carries no
repository field, and no expected-context repository/revision
exists on the investigation row (closest existing concept is
the proof domain's `expected_context_id`, a context identity
string). The table below therefore gives *candidate* bindings,
explicitly labeled unaudited; each cell is bound — or declared
unknown-by-default — by the §9 schema audit before
implementation. Any property whose cell has no bound source at
audit time defaults to `unknown` for every observation, with
abstention semantics (pinned by D3P-N23: an unbound source
yields `unknown`, never a default `validated`).

| Property | Value A (claim/row side, candidate) | Value B (context side, candidate) | Audit task |
|---|---|---|---|
| `repository` | row provider/source-system identity (candidate; `SourceLocation` has no repository field) | investigation/application context repository, if such a natively-loaded field is proven to exist | bind both sides to field paths, or declare the property unknown-by-default |
| `revision` | `SourceLocation.revision` (Optional) and/or `ClaimTarget.commit_id` (Optional; F-B1 alignment) | context-side revision, if proven | bind both sides; where both A-values exist they must agree with each other and with B |
| `scope` | row/investigation scope as loaded on the bound rows | scope from the loaded investigation row | bind both sides to field paths |

Stale-context detection needs no extra acquisition *once cells
are bound*: a resident row-side value that disagrees with the
resident context-side value *is* the staleness signal, detected
by in-memory comparison. Until a cell is bound, there is no
signal — only `unknown`.

### 2.6 Resident-evidence boundary and expected-difference classes (R2)

Documented separately because a digest of observed bytes proves
*which bytes were observed* — not that they are the complete
range or the current file content:

- *Exact bytes hashed*: the UTF-8 bytes of the resident banked
  snippet (`content_snippet` as loaded on the evidence row),
  recorded as `resident_content_digest` per row.
- *Provenance association*: `evidence_id` → banked row →
  `provenance.source_location` (file, lines, revision) — all
  resident, all named in the bundle.
- *Completeness*: the bundle records whether resident bytes cover
  the entire claimed range (`bytes_present: full | partial` with
  the missing line spans named). Partial coverage is not a
  degraded pass — uncovered range portions are `unavailable`
  (§2.1) and force abstention for any check that needs them.
- *Coordinates and revision*: the claimed range's coordinates come
  from the resident provenance window; the declaration identity
  carries `commit_id` (native `ClaimTarget.commit_id`) with
  `SourceLocation.revision` recorded alongside in the bound
  rows — where both exist they must agree, else `mismatch` →
  abstain (§2.5). The evaluator checks claim-range ⊆
  provenance-window ⊆ resident-coverage; any failure is
  `unavailable-input`.
- *Beyond-resident or incomplete mapping*: the range extends past
  resident content, or the provenance mapping is incomplete →
  `unavailable-input`, never a fetch, never a best-effort fill.
- *No symbol-match upgrade (prohibition)*: a matching symbol or
  substring never upgrades incomplete provenance into a resolved
  declaration. Native substring tolerance
  (`verification.py:1645-1656`) is a native behavior the shadow
  mirrors-or-narrows; where the shadow narrows, the difference is
  a labeled expected difference (E2 below), not a stronger claim.

Expected-difference classes (frozen; each has a reporting
treatment in §6 and at least one pinning test):

- E1 *stricter binding*: native accepts an unpinned or loosely
  scoped range the typed path must reject → native-accept /
  shadow-abstain is recorded as *expected divergence by design*
  (RV09), counted in the divergence column — never as parity,
  never as agreement (pinned by D3P-G02 negatives + §4.1/§7 of
  v1.3).
- E2 *match-tolerance narrowing*: native substring tolerance
  accepts where the shadow requires the exact bound symbol →
  recorded as tolerance difference, never as shadow superiority
  (pinned by D3P-N18).
- E3 *version/policy pinning*: observations version-tagged;
  v1-vs-v2 or row-version differences route to exclusions, never
  to silent agreement (pinned by D3P-N11).
- E4 *input-set difference*: `BOUND_FULL` vs `CITED_LOCATION`
  vs `CONCLUDE_GATE` outcomes on the same candidate are joined
  per-tap (§1.4); cross-tap disagreement is attribution data, not
  contradiction (pinned by D3P-N18).

Together with the Phase 0 vocabulary (§2.3: no `AUTHORIZED`
outcome, no consumer, telemetry-carried admissibility only),
these classes are the demonstrable constraint the owner
condition requires: no downstream gate can interpret a shadow
match as satisfying `requires_target_declaration=true`,
because in Phase 0 there is no downstream gate and no
authorization value to interpret.

## 3. Resolver trust-anchor verification

### 3.1 Anchored artifacts

Reviewed definition dispatch rows (new fixture
`definition_dispatch_mapping.json`): per-row `contract_digest`
(normalizer contract) and `artifact_digest` (normalizer
executable); dependency/configuration manifest digest;
`bundle_version`; `table_version`; table `policy_digest`. The row
set is reviewed; digests are recomputed at runtime, never trusted
from records (same shape as `dispatch.py:118-134,155-158`).

### 3.2 Verification points

| Point | Check | On mismatch |
|---|---|---|
| Startup | loaded row contract digests == reviewed; loaded normalizer bytes + dependency manifest + enumerated config surface == approved digests | refuse the shadow path entirely, loudly (fail closed at load) |
| Invocation (per construction) | embedded `(contract_digest, artifact_digest)` re-verified against loaded values | single-observation refusal (`SHADOW_FAILED`-class marker, native untouched) |
| Reload | re-derive manifest + re-verify before any further construction | refuse further construction until digests match again |
| Record-carried digest | accepted only as a claim to check against loaded values | rejected if it matches nothing approved |

### 3.3 Manifest derivation (normative procedure for implementation)

Walk loaded package bytes (exact bytes hashed); read the
dependency lockfile when present else installed-distribution
metadata (`name==pinned-version` entries); canonicalize the
explicitly enumerated configuration surface (allowlisted env
vars, config files, command-line surfaces declared in the mapping
artifact) as sorted `KEY=value` UTF-8 lines with absent values
omitted, never defaulted. Combine the three digests by
canonical-JSON hashing in fixed order. The derivation procedure
is part of the reviewed contract: two builds of the same approved
inputs produce byte-identical manifests.

Runtime introspection mechanics (how "actual" values are
obtained — no self-report): the normalizer module is resolved
through the import system to its loaded file
(`module.__file__` of the actually imported module object, no
re-import, no path search); the exact bytes at that path are
hashed. Dependency metadata comes from the lockfile bytes when
the deployment carries one, else from installed-distribution
metadata read at runtime. Configuration values are read only
through the allowlisted names; any read outside the allowlist is
itself the violation in §3.4. When a component cannot be
measured reliably — no file bytes (e.g., namespace packages
without a backing file), no lockfile and no installed metadata,
or an unreadable artifact — the result is not a guess and not a
skip: the component is named `unmeasurable-input` and
construction is refused (D3P-N20). Unmeasurable fails closed,
exactly like mismatch.

Explicit non-claim (RV04): hashing bytes read from
`module.__file__` establishes the content at that path *when
read*. It does not by itself prove the bytes currently
executing are those bytes — a file can change after import,
bytecode may load from a different artifact, dependencies may
already be imported, and configuration may change after the
manifest is calculated. The package does not claim
executing-byte identity from path hashing. The Phase 0 trust
boundary is therefore *deployment-identity pinning before first
evaluation*: the loaded artifact identity (module bytes +
manifest + complete allowlisted inputs, §3.2 startup row) is
verified before shadow evaluation is enabled at all, and any
failure to establish that identity refuses the shadow path —
refusing evaluation when identity cannot be established is the
safe default, not an edge case.

Detectable vs outside the guarantee (frozen; do not extend the
claim without a new enforcement mechanism):

| Drift class | Detectable in Phase 0? | Mechanism |
|---|---|---|
| Normalizer byte change (file) | yes | load-time hash vs reviewed digest; reload re-derivation |
| Dependency change (lockfile/installed metadata) | yes | manifest digest compare |
| Allowlisted config change | yes | configuration-surface digest compare |
| Unallowlisted behavior-affecting input | yes *at build/test time only* — by the static analyzer specified below (C8); NOT by runtime interception | static test + allowlist review; a module that gains a new read fails the static test before it can run |
| File change after the load-time read | no | outside the guarantee; mitigated by reload re-verification, not eliminated |
| Already-imported dependency differing from the hashed artifact | no | outside the guarantee; mitigated by deployment-identity pinning (single pinned deployment), not eliminated |
| Config change after manifest calculation | no | outside the guarantee; mitigated by reload re-verification on the reload path, not eliminated |

Runtime detection of arbitrary unallowlisted reads is explicitly
NOT claimed: there is no runtime interception mechanism, and
the package does not pretend a manifest proves what code *will*
read. The guarantee rests on three legs — static analysis
(what the code *can* read), load-time manifest (what was
*approved*), reload re-verification (what *changed*) — and names
its gaps above.

Static analyzer specification (C8 — exact mechanism, scope,
and limits; grounded in the existing D1-G technique,
`tests/unit/test_d1_flag_equivalence.py:93-109`, which enforces
an AST top-level-import allowlist plus a literal
`text.count("os.environ") == 1` check):

- What the analyzer checks: (a) AST walk over the module source
  (all scopes, including function-level imports) collecting
  import top-module names, required to be a subset of the
  reviewed allowlist; (b) literal/direct reads of
  behavior-affecting inputs — `os.environ`, `os.getenv`,
  `sys.argv`, `open(`/config-path patterns — each must resolve
  to an allowlisted name or the test fails; (c) denylist scan
  for dynamic-access patterns that defeat (a)/(b):
  `getattr` with `'environ'`/`'getenv'` string arguments,
  `importlib` with non-literal module names, `__import__`,
  `eval(`, `exec(`, `subprocess`, `os.system`, `os.popen`,
  `os.exec*`, `os.fork`. Any denylist hit fails the test.
- Limitations stated plainly: the scan cannot see through
  dynamically constructed strings outside the denylist
  patterns, C-extension reads, inherited file descriptors, or
  `/proc`-style side channels; and subprocess environment
  inheritance means a child process would receive the full
  environment — therefore subprocess use is forbidden in the
  shadow/normalizer modules by the same scan (denylist
  includes it), not merely unreviewed.
- Mitigations for the gaps: denylist + subprocess ban (scan),
  allowlist review (human, at mapping-artifact review),
  load-time manifest as the second leg (what was approved),
  reload re-verification as the third (what changed). The
  analyzer proves the module *as written* stays within the
  allowlist; it is not a proof about what arbitrary code
  *could* do. Scope the claim accordingly in every report.

### 3.4 Mismatch and unrecognized-input table

| Event | Required behavior |
|---|---|
| Row contract digest mismatch at load | refuse shadow path, loud log |
| Normalizer byte mismatch | refuse construction |
| Dependency-only drift | refuse (distinct test from executable drift) |
| Configuration-surface drift | refuse (distinct test) |
| Normalizer reads env/config/file outside the allowlist | contract violation: refuse, do not ignore-and-continue |
| Malformed flag value | treated as off + loud startup log (visible misconfiguration, zero authority risk) |
| `"1"` in authority position without Phase 1 authorization | hard startup failure: refuse governed paths, no silent native fallback |

### 3.5 Why self-attestation is insufficient (stated for the record)

Expected values come from the reviewed row; actual values come
from runtime introspection of loaded code; the record under
evaluation contributes neither. A manifest supplied by the
deployment being checked authenticates nothing.

## 4. Golden and negative test matrix

Naming follows the D1 precedent (`test_d1_*`,
`test_proof_kernel_fixture_integrity.py`). Proposed files:
`tests/unit/test_d3_definition_observation.py` (schema/canonical),
`tests/unit/test_d3_definition_authorize.py` (dispatch + authorize),
`tests/unit/test_d3_definition_trust.py` (digests/manifest/reload),
`tests/unit/test_d3_shadow_isolation.py` (non-interference,
flags-off, telemetry failure), `tests/unit/test_d3_live_sample.py`
(protocol accounting: denominator, cells, exclusions — unit-
testable on synthetic observation sets). Fixtures:
`tests/fixtures/proof_kernel/definition_dispatch_mapping.json`,
`definition_golden_vectors.json`,
`definition_invalid_vectors.json`. Ledger integration reuses the
`proof_kernel_ledger.py`/`--check` pattern
(`tests/unit/test_proof_kernel_fixture_integrity.py` shape).

Each row: contract reference → input → expected outcome →
acceptance gate. "Gate: unit" runs offline; "gate: ledger" must
reproduce through the fixture ledger; "gate: live" is counted in
§6, never asserted from unit runs.

### 4.1 Golden (valid) tests

| ID | Contract (v1.3) | Input | Expected | Gate |
|---|---|---|---|---|
| D3P-G01 | §2.1 canonical target | golden vectors, all variants incl. omitted `declaring_type` | byte-stable digests; all parse | unit + ledger |
| D3P-G02 | §2.2/§2.4 bound record | valid `RESOLVED_TARGET_BINDING` (bound source-range association only — the name denotes a bound endpoint, not a resolved declaration): zero/unique-candidate selection, all lineage kinds, all ref shapes | Stage 1 `ASSOCIATION_ADMISSIBLE` with rule trace naming `association_scope: "source-range-only"`; Stage 2 records `declaration_requirement_satisfied: false`; asserts nothing about declaration proof (R1/RV01) | unit + ledger |
| D3P-G03 | §2.2 `UNRESOLVED` | ref-only record, no bound fields | well-formed; binding refused by construction (inexpressible) | unit |
| D3P-G04 | §2.2 `PARTIAL` | missing target / missing lineage source | well-formed; `missing` names exactly the absent fields | unit |
| D3P-G05 | §3 dispatch + §2.3 two stages | exact row match + policy digest; `requires_target_declaration=true` | Stage 1 admissibility gate passes on row match; null/absent declaration never passes Stage 1; Stage 2 records `declaration_requirement_satisfied: false` in all cases — dispatch match and declaration satisfaction are separate outputs, never conflated (RV06) | unit + ledger |
| D3P-G06 | §4.0a bundle | fully resident bundle, digests verify | shadow evaluates to a recorded observation | unit |
| D3P-G07 | §3.2 trust happy path | matching bytes + manifest + config | construction permitted | unit |

### 4.2 Negative tests (all must yield typed unavailability or abstention — never a positive)

| ID | Contract | Input | Expected | Gate |
|---|---|---|---|---|
| D3P-N01 | §2.2 schema | malformed record, unknown variant/field, duplicate set entries | reject at validation | unit |
| D3P-N02 | canon/v1 | bad path, reversed range, float, non-NFC, null field | `CanonicalizationError`, no digest | unit |
| D3P-N03 | §2.4 selection | zero / ambiguous candidates | abstain (no best-effort) | unit + live |
| D3P-N04 | §2.3/§2.4a bytes | resident bytes changed after digest | digest mismatch → unavailable | unit + live |
| D3P-N05 | §2.5 currency | bound cell sides present but unequal | `mismatch` → abstain (`stale-or-unknown-context`) | unit + live |
| D3P-N06 | §3 digests | record-carried digest ≠ recomputed | reject substitution | unit |
| D3P-N07 | §2.3 lineage | incomplete chain (missing link) | abstain, names the gap | unit |
| D3P-N08 | §3 dispatch | wrong profile/version/row; unknown row | no match, no fallback, no inheritance | unit |
| D3P-N09a–d | §3.2 drift | contract-only change / executable substitution / dependency drift / config drift (four distinct tests) | refuse in each case | unit |
| D3P-N10 | §3.2 allowlist | behavior-affecting input outside allowlist | contract violation → refuse | unit |
| D3P-N11 | cross-policy replay | v1 observation under v2 policy and reverse; cross-investigation; cross-scope | reject (version/scope pin) | unit |
| D3P-N12 | §2.2 partiality | `PARTIAL`/missing target presented for authorization | abstain; never binds | unit |
| D3P-N13 | Owner condition 1 + Option B | source-range association presented where declaration proof is required | exact output tuple: Stage 1 `ASSOCIATION_ADMISSIBLE`, observation outcome `OBSERVATION_ADMISSIBLE`, Stage 2 stub returns `declaration_requirement_satisfied: false`. The association is ADMITTED; what is refused is declaration satisfaction and any downstream use as proof. A harness-side reference requirement predicate (requires-declaration-proof → reads the machine-readable field) must return unsatisfied for this tuple; assert no path from the tuple to any satisfied/authorized representation | unit |
| D3P-N13b | R1/RV01 sufficiency | fully digest-valid, provenance-complete association with declaration evidence still missing | same exact tuple as N13; assert `declaration_requirement_satisfied is False` machine-readable; green admissibility never counted as satisfaction | unit |
| D3P-N14 | §2.4a currency | bundle digests no longer verify at use | stale by definition → reject, never refresh implicitly | unit |
| D3P-N15 | §1.2 isolation | evaluator exception / oversized input | evaluator-failure marker captured (`SHADOW_FAILED` class, §6 taxonomy); native verdict identical (differential) | unit |
| D3P-N16 | §1.2 `observer=None` | full native suite with observer absent vs present-but-unused | behavioral contract: equal return values, serialized outputs, persistence-write calls + payloads, exceptions, side effects | unit |
| D3P-N17 | §1.4 telemetry/omission | telemetry derivation throws / returns `None` | native path unaffected; Path A: in-band `loop_telemetry_omitted` flag set on the conclude result and counted in run-health; Path B: harness reconciles completed runs vs key presence, missing-unexplained voids the run set; harness-loss residual stated as unmeasurable | unit |
| D3P-N18 | §1.1 two sets | same candidate under `BOUND_FULL` vs `CITED_LOCATION` | observations labeled; differing outcomes both recorded, neither suppressed | unit + live |
| D3P-N19a | §2.3 vocabulary | static scan of new shadow modules | `AUTHORIZED` literal (and aliases) absent; the outcome enum cannot represent proof authorization | unit |
| D3P-N19b | §2.3 no-consumer | import/reference scan over verdict, persistence, publication, gate code | no consumer reads the observation lists; admissibility cannot flow into any proof decision | unit |
| D3P-N20 | §3.3 unmeasurable | dependency/runtime component that cannot be measured reliably (no file bytes, no lockfile, no installed metadata) | `unmeasurable-input` → refuse construction (fail closed) | unit |
| D3P-N21 | §1.4 additive key | shadow flag off, full native suite | `data["shadow_definitions"]` absent; result schema identical to pre-implementation shape | unit |
| D3P-N22 | §1.4 correlation/retries | synthetic duplicate delivery within one execution; superseded-attempt observations presented for join; interleaved/retried executions | duplicates deduped keep-first with `duplicate_count`; superseded-attempt observations excluded from denominator and counted separately, never joined to another execution's native result; iteration stamped on receipt pairs 1:1 with the accepted execution | unit |
| D3P-N23 | §2.5 unbound source (F-A1) | currency cell with no audit-bound source on either side | property defaults to `unknown` (never `validated`); evaluator abstains `stale-or-unknown-context` | unit |
| D3P-N24 | §1.4 producing-path label (F-A3) | native outcome record without a recoverable producing-path label | turn classified ambiguous-join; voids the run set; never attributed by guess | unit |
| D3P-N25 | §2.1 two-layer identity (C1) | re-serialize a received observation; replay a receipt sequence; mutate one covered field | bundle_digest recomputes equal (identity stable across stamping); replay reproduces both digests exactly; any covered-field mutation breaks verification; hash-before/hash-after receipt equality proves no bundle mutation | unit |
| D3P-N26 | §1.4 step 4 overflow (C7) | 300-turn trace through summarization + capping | first 256 detail entries retained in order; later entries dropped with `dropped_count` + `trace_truncated: true`; running counts cover all 300; dropped turns excluded as truncation exclusions, never reconstructed | unit |

D3P-N13/N13b deserve emphasis: together they test the
code-level enforcement the owner conditions require — the
named stub recording `declaration_requirement_satisfied: false`
(N13), the absence of any consumer that could read it as
satisfied (N19b), and the harness-side reference predicate
returning unsatisfied for the tuple — plus the positive
statement that even a fully valid association leaves a missing
declaration insufficient (N13b). A green G02 must never read
as proof satisfaction. If the stub, the consumer-absence, or
the harness predicate is missing at implementation time, the
implementation is incomplete regardless of other greens.

## 5. Four-combination feature-flag matrix

### 5.1 Parsing specification (for implementation)

Two new accessors in the new `definition_dispatch.py` module,
copying the exact D1 shape (`dispatch.py:41-47`) — one
`os.environ.get(NAME, "") == "1"` each, no stripping, no
synonyms, off by default. Copy-per-flag (not a shared helper)
preserves the D1-G invariant (single `os.environ` read per
module, pinned by `tests/unit/test_d1_flag_equivalence.py:104-109`;
the same invariant test is added for the new module). Proposed
names: `shadow_typed_definitions_enabled()` for
`IAP_SHADOW_TYPED_DEFINITIONS`, `typed_definitions_enabled()`
for `IAP_TYPED_DEFINITIONS`. Neither flag exists in code today
(verified: zero `src/` hits) — both are created by the later
implementation, not by this package.

### 5.2 Required behavior matrix (normative)

| Shadow flag | Authority flag | Required Phase 0 behavior |
|---|---|---|
| Off | Off | exact legacy behavior; shadow tap not activated: no shadow evaluation, acquisition, or shadow persistence effects (no build-time removal is claimed or needed — the tap code path is gated off at runtime and provably inert per D3P-F01/N21) — the only runnable baseline for differentials |
| On | Off | isolated shadow observation only; every native verdict, tier, explanation, confidence value, persistence write, publication decision, side effect, and error semantic identical to flag-off — the only runnable shadow mode in Phase 0 |
| Off | On | NOT RUNNABLE in Phase 0: authority flag without Phase 1 authorization is a hard startup failure (§3.4, D3P-F06), not a comparable run — never differentially compared as an ordinary mode |
| On | On | NOT RUNNABLE in Phase 0: same hard failure while Phase 1 is unauthorized; authoritative consumption is prohibited regardless of flag manipulation (D3P-F08) |

All parsing uses the exact enabling value `"1"`. The authority
flag cannot override the Phase 0 authority boundary under any
combination. Telemetry records the flag configuration of every
measured run (flag config is part of run identity).

Startup-failure scope (explicit decision): the hard failure is
scoped to the governed typed-definition component, which refuses
to initialize loudly. It does not take down the whole
application — but no governed path runs either. There is no
silent native fallback that would mask the misconfiguration
(D3P-F06 asserts refusal is loud; D3P-F07 asserts the blast
radius stops at the governed component).

### 5.3 Flag tests (four categories — startup and runtime separated)

ID note (F-B8): F03/F04 were the v1.1 four-combination
differentials, retired by the runnable/non-runnable split;
numbering is preserved (not reused) for traceability across
revisions.

Harness scope (startup vs in-process — normative): startup
tests (F05–F07) boot the governed component init in an
isolated harness (subprocess or equivalent) and expect refusal
(F06) or off-behavior (F05) with blast-radius assertions
(F07). Runtime tests (F01/F02/F09, all N-series) NEVER boot
governed paths: flags are manipulated in-process post-startup
(`monkeypatch.setenv` or direct accessor override) so the
hard startup failure cannot trigger mid-test. F08 in
particular forces the authority flag in-process WITHOUT
governed-path initialization — booting governed code with the
flag set is F06's test, not F08's. "Config tampering" in F08
means in-process config override, never a deployment config
change. A test that boots governed paths with authority
enabled and compares outputs as an ordinary mode is an
invalid test under this package.

| ID | Category | Covers |
|---|---|---|
| D3P-F01 | Runtime differential | Off/Off baseline: exact legacy behavior; additive key absent (with D3P-N21), no shadow acquisition or persistence |
| D3P-F02 | Runtime differential | On/Off: isolated shadow observation only; full native differential (verdicts, tiers, explanations, confidence, persistence writes + payloads, publication, side effects, error semantics) identical to F01 |
| D3P-F05 | Startup/config | default-off (missing/empty), malformed values (`"true"`, `"yes"`, `" 1 "`, `"2"`, alternate case) in either position → off + loud startup log; nothing activates |
| D3P-F06 | Startup/config | `"1"` in authority position without Phase 1 authorization → hard startup failure: governed paths refuse, loudly, with no silent native fallback |
| D3P-F07 | Startup/config | failure-blast-radius: the hard failure stops at the governed typed-definition component; asserted by startup test (governed init refuses; unrelated paths unaffected; nothing governed runs) |
| D3P-F08 | Authorized-mode negative (in-process only — never boots governed paths; that is F06's test) | authority flag forced on in-process post-startup (`monkeypatch`/accessor override) or config overridden in-process: no code path consumes typed-definition observations — verdicts, persistence, and publication identical to F01; manipulation cannot create a consumer that does not exist (with D3P-N19b); governed-path initialization is never invoked in this harness |
| D3P-F09 | Rollback | disable shadow after enabled runs: new runs produce legacy-shaped payloads and legacy write-call sequences; no shadow-only acquisition or persistence effects remain; asserted by persisted-payload + save-call differential vs baseline, not by verdict comparison |

## 6. Pre-declared live-sample protocol (frozen before collection)

### 6.1 Frozen definitions and outcome taxonomy

- *Eligible turn*: native outcome recorded AND typed observation
  recorded under the frozen comparison rule, `verifier_version ==
  v2`, definition-claim candidate, input-set labeled
  (`BOUND_FULL` / `CITED_LOCATION` / `CONCLUDE_GATE`).
- *Run terminal states*: PASS / FAIL per the existing run
  vocabulary.
- *Attribution*: every counted turn carries
  `(investigation_id, hypothesis_id, iteration, input-set, tap,
  snapshot identity)`.
- *Snapshot identity*: exact code revision, configuration,
  resolver/contract/artifact digests, dispatch-row versions, and
  run-case ids — recorded before collection starts.

Frozen comparison taxonomy (every observed turn lands in exactly
one class; the class determines treatment — fixed before
collection, never re-cut after seeing data):

| Outcome class | Definition | Treatment |
|---|---|---|
| Native verified | gate outcome `VERIFIED` on the observed candidate | native acceptance |
| Native explicit non-verification | explicit rejection outcome on the observed candidate (non-accepting terminal gate result that is a decision, not an error or abstention) | separate non-acceptance class |
| Native inconclusive / abstention | `INCONCLUSIVE`, abstention, or non-acceptance without explicit rejection | separate abstention class |
| Native execution error | activity/engine exception, timeout, malformed native state | excluded from the primary comparison; counted and reported separately |
| Shadow evaluator failure | the evaluator ran and produced a failure marker that was successfully captured (`SHADOW_FAILED` class) | reported separately; never treated as agreement or as ordinary abstention |
| Observation capture/transport failure | the observation was lost, truncated past the bound, or never arrived (counter-example: `dropped_count` increments, additive key missing) | invalidates the run set under the zero-capture-error rule (§6.3) |
| Missing or ambiguous native-to-shadow join | observation that cannot be joined to its tap-specific native outcome by the §1.4 key | protocol violation: voids the run set; never silently excluded |

`SHADOW_FAILED` is an evaluator failure, not a capture error:
the evaluator ran, failed safely, and the failure marker itself
was captured — that is successful failure handling, counted in
shadow-health. Losing the marker (or the observation) is the
capture error. The protocol preserves this distinction in
reporting: evaluator-failure count and capture-error count are
separate required fields, and only the second voids run sets.

Coverage floor — minimum feasibility/coverage gate, NOT a
statistically validated parity gate (RV05, proposal): at least
20 eligible turns total, with a minimum of 3 eligible turns in
each of the four decision-7 cells (native accept / native
non-accept × PASS / FAIL run terminals), where "native
non-accept" for cell purposes groups explicit non-verification
with inconclusive/abstention (reported separately per the
taxonomy above, grouped only for the floor). Twenty
observations with an empty or single-observation critical cell
is INSUFFICIENT EVIDENCE, not a pass — the floor is conjunctive
(total AND per-cell), and the protocol never lowers it to fit
collected data. Meeting this floor establishes only that a
larger-sample measurement is warranted; it establishes no
statistical confidence, no production safety, and no parity
claim (see gate-interpretation discipline, §6.4).

Subcategory presence rule: because the grouped non-acceptance
cell can otherwise hide an unevaluated subcategory, each run
terminal's non-accept floor additionally requires at least one
explicit-rejection turn AND at least one
inconclusive/abstention turn. If either subcategory is
unpopulatable in a terminal, conclusions are restricted to
grouped non-acceptance for that terminal, and claiming either
subcategory adequately evaluated is prohibited — any
subcategory-specific conclusion in that case is INSUFFICIENT
EVIDENCE by construction.

### 6.2 Feasibility probe (required by finding A-8)

Because turn eligibility is emergent (the frozen battery carries
no pre-typed definition claims), formal collection begins with a
bounded probe: pilot runs shaped to produce definition claims
(formulate-definition prompts against code-bearing fixtures).
Go/no-go: the probe must demonstrate all four decision-7 cells
are populatable (native accept / native non-accept × PASS /
FAIL). If any cell cannot be populated, the outcome is
INSUFFICIENT EVIDENCE by construction —
the protocol never lowers the floor to fit the data.

### 6.3 Collection, stop conditions, and reporting

Minimum: at least 20 eligible turns with at least 3 per
decision-7 cell (native accept / native non-accept × PASS / FAIL;
§6.1 floor is conjunctive — total AND per-cell); zero capture or
transport errors (any such error voids the run set: recover or
rerun, never trim); native errors, shadow evaluator failures,
and exclusions each reported in their own taxonomy class,
separate from the denominator. A missing or ambiguous join is a
protocol violation that voids the run set, never a silent
exclusion. Stop only when floor + per-cell coverage +
zero-capture-errors all hold; otherwise the verdict is
INSUFFICIENT EVIDENCE or HOLD, never a pass. Report: the
denominator, each matrix cell count, the full seven-class
accounting of every observed turn, capture-health (evaluator-
failure count, capture-error count, and telemetry-omission
count as three separate fields), and the frozen snapshot
identity. The protocol
document itself is hashed and frozen before the first collection
run; any change restarts collection.

Per-tap comparison tuples (RV09 — the comparison baseline
differs per tap, so parity is computed per tap, never pooled):

- T1 (`BOUND_FULL`): (native verify verdict on the candidate,
  shadow Stage 1 outcome, E-class or none).
- T2a (`CITED_LOCATION`): (native location-path gate result,
  shadow mirror note, E-class or none) — only where the site
  executed; else `mirror-not-reached` (§1.4).
- T2b (`BOUND_FULL`): (native general-path gate result +
  backstop trigger state, shadow mirror note, E-class or none).
- T3 (`CONCLUDE_GATE`): (native conclude gate outcome, shadow
  observation, E-class or none).

Expected divergences (any tuple carrying E1–E4) are reported in
a dedicated divergence section with their class labels —
separately from parity counts. A native-accept/shadow-abstain
tuple under E1 is an expected divergence, never ordinary parity
and never agreement. Cross-tap disagreement (E4) is attribution
data: the taps observed different evidence sets by construction
(§1.1), so their tuples are reported side by side, not
reconciled into one verdict.

### 6.4 Gate-interpretation discipline (normative)

The package's gate language stays disciplined in every report,
summary, and future decision — these distinctions are preserved
in test names, result summaries, gate reports, and owner
decisions:

- Unit tests passing establishes only that the tested contracts
  passed in the tested environment.
- G5-unit green is not G5-formal approval.
- A 20-turn sample with the required cell counts establishes
  minimum coverage, not statistical confidence, production
  safety, or parity. The best live outcome of Phase 0 is
  "feasibility demonstrated; larger-sample measurement
  warranted" — never "parity proven".
- A matching digest establishes a match against the digest's
  stated preimage; it does not independently establish that the
  preimage is complete, current, or the code actually executing.
- `OBSERVATION_ADMISSIBLE` establishes only the limited
  association contract. It is not proof sufficiency, declaration
  resolution, verification, or publication eligibility.
- Shadow non-interference tests compare the actual native
  behavior and persistence effects — not just final verdicts or
  selected output fields (D3P-F02/F09 differentials).

## 7. Mandatory acceptance criteria — demonstration mapping

| Criterion | Demonstrated by |
|---|---|
| Non-interference | D3P-N15, N16 (behavioral contract), N17, N21; D3P-F01/F02 differentials (verdicts, tiers, explanations, confidence, persistence writes + payloads, publication, side effects, error semantics identical); persisted-payload + save-call comparison in F09 |
| Fail-closed behavior | D3P-N01–N12, N14 (every miss/mismatch/staleness class → unavailability or abstention); D3P-N20 (unmeasurable → refuse); D3P-N23 (unbound currency source → `unknown` → abstain); §3.4 startup/reload refusals |
| Semantic boundary | D3P-G02/G05 (admissibility recorded within contract, never proof) + D3P-N12/N13/N13b (exact output tuples: association ADMITTED, satisfaction refused, C4) + harness reference predicate unsatisfied; D3P-N19a/b (no authorization value, no consumer); v1.3 §5 boundary demonstration carried into the implementation evidence |
| Reproducibility | Ledger-gated goldens (D3P-G01–G07); identity stability across stamping and replay (D3P-N25); snapshot identity (§6.1); fixture ledger `--check` reproduction; §9 command outputs attached |
| Observability | T4 telemetry keys (frozen inventory, §1.6); evaluator-failure vs capture-error vs telemetry-omission separate counting (D3P-N15/N17, §1.4 step 7 lifecycle, §6.1; Path A flag + Path B harness rule; residual stated); `mirror-not-reached` coverage with producing-path labels (§1.4, D3P-N18, D3P-N24); correlation/dedup accounting with split stamping and SDK-shaped records (D3P-N22); truncation exclusions with running-count completeness (D3P-N26); §6 seven-class reporting (denominator, per-tap tuples, cells, capture-health) |
| Rollback | D3P-F01 + D3P-F09: disabling shadow restores exact legacy payloads and write-call sequences including absence of shadow acquisition and persistence; verified by persisted-state differential, not by verdict comparison |
| Gate integrity | §6 verdict vocabulary (INSUFFICIENT EVIDENCE / HOLD / feasibility-demonstrated) + §6.4 interpretation discipline + subcategory presence rule distinguish unit success from gate passage; the best live outcome is "larger-sample measurement warranted", never "parity proven"; per-cell floor blocks passes on unpopulated cells; staged gates (§9: Stage A audit attached + executed baseline (§9.4) gate implementation; Stage B artifacts gate merge; Stage C evidence gates rollout); a green unit suite alone authorizes nothing |

Traceability to owner conditions (design approval + package
review — every condition has a concrete, executable check):

| Owner condition | Concrete checks | Unambiguous expected result |
|---|---|---|
| `RESOLVED_TARGET_BINDING` cannot satisfy a declaration-proof requirement (code-level) | D3P-N13, D3P-N13b, D3P-N19a/b; Stage 2 named stub with pinned constant (F-B6/C9) | stub records `false` (machine-readable); harness reference predicate returns unsatisfied; no authorization literal; no consumer; stub cannot grow logic silently |
| Reproducible binding/byte-to-line/digest/lineage/stale-artifact tests | D3P-G01/G02/G06, D3P-N02/N04–N07/N14, ledger-gated goldens | green goldens + named abstentions; `--check` reproduction |
| Validated-bundle-only consumption, no independent acquisition | §2.2 static import test, D3P-N-series `unavailable` cases, §2.4 live join audit | no fetch capability in module; unfetched references void the run set |
| Loaded-artifact trust-anchor verification | D3P-G07, D3P-N09a–d, D3P-N10, D3P-N20, §3.2/§3.4 | refuse on every drift/unmeasurable class; startup refusal loud |
| Dispatch/authorization/cross-policy/stale-context negatives | D3P-G05 (two-stage outputs), D3P-N08, D3P-N11, D3P-N05, D3P-N23 (unbound source → unknown) | no match/no fallback; cross-policy and stale rejected; unbound currency never validates |
| Currency sources bound by attached schema audit (F-A1/C6) | §9.2 audit report (verified field paths; repository + undecidable cells unknown-by-default); D3P-N23 | revision row-internal agreement validates; repository never validates in Phase 0; scope single-sided stated; missing audit cells default unknown |
| Correlation identity and path attribution (F-A2/F-A3 → C2/C3) | split stamping with exact SDK tuple (§1.4) pinned by D3P-N22 (+ mirror-conformance test); receipt-time pairing as authoritative join with additive `producing_path` (§1.4/§1.6); persisted rows cross-check only; D3P-N18/N24 | 1:1 receipt-order pairing; envelope digests tamper-evident (N25); unattributable turns void run sets |
| Four flag combinations + exact flag-off equivalence | D3P-F01/F02 (runnable differentials), D3P-F05–F07 (startup), D3P-F08 (authorized-mode negative), D3P-F09 (rollback payload differential) | differentials identical; non-runnable combos fail loud at startup; rollback restores legacy payloads |
| Prerequisite gate artifact versions attached | §9 command outputs (§6 snapshot identity for live legs) | versions recorded; missing versions block authorization |
| Outcome taxonomy frozen before collection | §6.1 seven-class table; D3P-N-series protocol-accounting in `test_d3_live_sample.py` | every turn classified; per-cell floor enforced; violations void run sets |

Traceability to package-review findings RV01–RV10 (each finding
has a concrete, executable resolution in this revision):

| Finding | Resolution location | Executable check |
|---|---|---|
| RV01 association-only admissibility | §0.2 naming note; §2.3 schema + rule trace; §2.6 constraint paragraph | D3P-G02/G05 (scope named), D3P-N13/N13b (Stage 2 `false` machine-readable), schema test pins `association_scope` + `declaration_requirement_satisfied: false` |
| RV06 dispatch/declaration separation | §2.3 two-stage order | D3P-G05 (separate per-stage outputs); dispatch match never yields declaration satisfaction by construction |
| RV02 correlation record + early returns | §1.4 correlation contract | D3P-N18 (per-tap join + `mirror-not-reached` classification); join failures void run sets |
| RV08 execution identity + retries | §1.4 key + retry rules | D3P-N22 (duplicate injection + superseded-attempt exclusion); SDK accessor pinned by test |
| RV03 per-flag lifecycle + persistence | §1.4 lifecycle table + language correction | D3P-F01/F02 (runnable), F05–F07 (startup), F09 (payload differential); telemetry-omission counting |
| RV04 narrowed trust claim | §3.3 non-claim + detectable table; §3.2 startup pinning | D3P-G07, D3P-N09a–d, D3P-N10, D3P-N20; gaps explicitly outside guarantee |
| RV05 feasibility gate + subcategories | §6.1 floor + subcategory rule; §6.4 discipline | per-cell + subcategory enforcement in `test_d3_live_sample.py`; restricted conclusions when unpopulatable |
| RV07 scope reconciliation | §0.1; §1.6 interface list | D3P-N16/N17/N21 compatibility tests per listed change |
| RV09 per-tap parity + divergence | §6.3 comparison tuples; §2.6 E1–E4 | E1 reworded; divergences reported separately; D3P-N18 pins cross-tap attribution |
| RV10 regression inventory | §9.0 freeze-list | applicable-suite results (or approved evidence-based exception) required before merge/rollout |

## 8. Risks and unresolved assumptions register

| ID | Assumption / risk | Position taken in this package | Evidence needed to close |
|---|---|---|---|
| A-1 | Legacy v1 path reaches the kind gate; is gate coverage version-independent? | observations version-tagged; eligible set v2-only | owner confirm at implementation authorization, or code re-verification that v1/v2 gate behavior is identical for definition claims |
| A-2 | Conclude-time mirror in scope? | DECIDED (owner 2026-10-24): T3 in scope as separately labeled `CONCLUDE_GATE`; observational only — must not change conclusion eligibility, final verdict, persistence, or error behavior | none; position fixed by decision, enforced by §1.2 tap mechanics + D3P-F02 differential |
| A-3 | Flag helper shape | DECIDED (owner 2026-10-24): copy-per-flag strict accessors, preserving the D1-G single-read invariant; both default off, exact `"1"` only; local compatibility decision, not a duplication of authorization logic | implementation-time review of the new module + D3P-F05 missing/empty/malformed/alternate-value tests |
| A-4 | Mapping artifact schema | reuse `DispatchTable` machinery with new definition rows + new fixture file; no change to D1 rows | implementation-time review of the new fixture against v1.3 §3 preimages |
| A-5 | Symbol matching stays native-parity (substring whole-identifier, `verification.py:1645-1656`), no AST node identity on the definition path | CONDITIONAL (owner 2026-10-24): Option B preserved; shadow mirrors-or-narrows native matching and its output is explicitly a source-range association, never declaration proof. This revision supplies the constraint: expected-difference classes E1–E4 (§2.6) plus the no-consumer enforcement (§2.3, D3P-N19) showing no downstream gate can interpret a shadow match as satisfying `requires_target_declaration=true` | owner acceptance of E1–E4 + N19 as the constraint evidence, or require node-identity matching (new capability, out of v1 scope) |
| A-6 | `_callsite_target` vs `_reread_target` overlap for definition-shaped claims | shadow records `forced[]` as-is; no disambiguation beyond existing flags | none required for Phase 0; revisit only if attribution analysis needs it |
| A-7 | Only banked snippet bytes + provenance are resident; full artifacts are not loaded at verify time | bundle attests resident bytes only; all else `unavailable-input`, never fetched (§2.4) | implementation-time residency audit: assert every bundle field maps to a same-turn native local |
| A-8 | Definition-claim turns are emergent; frozen battery has no pre-typed definition claims (verified by grep) | §6.2 feasibility probe with populability go/no-go before formal collection | probe results; if cells unpopulatable, INSUFFICIENT EVIDENCE stands |

## 9. Acceptance commands (documented for authorization time — not executed in planning)

### 9.0 Complete-regression inventory (RV10 — identified before implementation)

Applicable suites for a change touching `application/worker/`
(activities, workflows), `application/investigation/
verification.py`, and `domain/proof/` — inventoried read-only,
not executed. Applicability below is presumed from suite
purpose and file names (F-B7), not verified by running
anything: the freeze-list — not the presumption — is
normative, and each row's applicability is confirmed or
corrected at implementation authorization. Exact connection
requirements and markers are confirmed at the same time.

| Suite | Files | Applicability to Phase 0 | Prerequisites (to confirm) |
|---|---|---|---|
| `tests/unit` | all, incl. new `test_d3_*` | direct: new suites + neighboring suites (activities, verification, telemetry, dispatch, flags) must stay green | none (offline, deterministic) |
| `tests/contract` | `test_mcp_contract.py`, `test_mcp_adversarial.py` | MCP surface untouched by the taps; run to confirm no contract change | per suite markers/environment |
| `tests/integration` | `test_verify_conclude_path.py` | direct: exercises the touched verify/conclude path end to end | live backing services per file |
| `tests/integration` | `test_agent_loop_elastic_oracle.py` | direct: exercises the loop the taps sit beside | elastic + loop fixtures |
| `tests/integration` | `test_tenancy_rls.py` | persistence/RLS behavior around the touched saves | postgres |
| `tests/integration` | remaining files (onboarding/postgres/neo4j/elastic/mcp/knowledge/llm) | unaffected surface; full-regression membership, run unless an evidence-based exception is approved | live services per file |
| `tests/system_api` | `test_route_coverage.py`, `test_contract_conformance.py`, `test_usecase_flows.py` | API surface untouched; run investigation-touching flows as regression | API test environment |
| `tests/evaluation` | `test_incident_replay.py`, `test_acceptance_gates.py` | end-to-end non-interference evidence at acceptance level | replay fixtures, gate environment |
| `tests/evaluation` | `test_mcp_fanout.py` | likely N/A (MCP surface); confirm or except with evidence | per suite markers |
| `tests/benchmarks` | `test_topology_enrichment.py` | likely N/A; confirm or except with evidence | per suite markers |

Rule: before any merge or rollout decision, results are
required for every applicable suite above — or an explicitly
approved, evidence-based exception naming the suite and the
reason. The unit scope (§9 commands 1–4) never stands in for
this inventory.

### 9.1 Staged authorization gates (C6 — each artifact states the gate it unlocks)

No implementation output is required to approve the
implementation itself. Stages:

- **Stage A — gates Phase 0 implementation authorization.**
  Evaluable now, against existing code only: (A1) the schema
  audit below (attached, verified read-only against the tree —
  no new code needed to evaluate it); (A2) baseline unit-suite
  state — EXECUTED 2026-10-24, output attached as §9.4
  (3058 passed / 4 skipped / 6 xfailed, exit 0; no D3 test
  exists yet, so this is the pre-change regression floor);
  (A3) feasibility prerequisites (§6.2 probe plan). Stage A
  contains zero artifacts that require the implementation to
  exist.
- **Stage B — gates merge and Phase 0 acceptance.** Evaluable
  only after implementation: D3P unit suites (incl. N22–N26),
  flag differentials, ledger/baseline `--check`, and the §9.0
  applicable suites (or approved evidence-based exceptions).
- **Stage C — gates any rollout or authority decision.**
  Live-sample evidence per §6 under the §6.4 discipline, plus a
  separate explicit owner decision. Stage C is never satisfied
  by Stage A or B artifacts.

### 9.2 Schema audit report (C6/F-A1 prerequisite — completed read-only, attached)

Verified by direct inspection of the implementation tree on
2026-10-24 (file + line + accessor chain; no execution, no new
code). Each §2.5 currency cell is either bound or declared
unknown-by-default. Unbound cells default their property to
`unknown` with abstention semantics (D3P-N23):

| Property | A-side (claim/row side) | B-side (context side) | Disposition |
|---|---|---|---|
| `revision` | BOUND (either side suffices; both required to agree when both present): `SourceLocation.revision` (`domain/provenance/models.py:20`, `str \| None`) and `ClaimTarget.commit_id` (`domain/hypothesis/models.py:73`, `str \| None`) | UNBOUND — no typed context-side revision on the investigation row (verified: no `expected_context` revision/repo fields on `Investigation`; closest existing concept is the proof domain's `expected_context_id`, a context identity string, not a repo/revision pair) | row-internal consistency only: both A-values present and equal → `validated`; either missing → `unknown`; disagree → `mismatch`. No context-side staleness detectable — stated, not claimed |
| `repository` | UNBOUND — `SourceLocation` has no repository field (verified: `system/identifier/file_path/lines/revision` only); `Evidence.source` is a locator string, not an identity; `attributes` is untyped (no proven stable key) | UNBOUND — no typed context-side repository | unknown-by-default, declared now. Follow-up OUTSIDE Phase 0: a banking-code audit could bind `attributes[<proven-key>]` as the row side if a stable key is proven; until then the property never validates and checks requiring corroborated repository abstain |
| `scope` | BOUND (single-sided): `ClaimTarget.scope` (`domain/hypothesis/models.py:71`, `str \| None`) | UNBOUND — no typed context-side scope | present → `validated-as-stated` (attests the claim states a scope, NOT a corroborated scope); missing → `unknown`. Checks requiring corroborated scope abstain; the single-sided nature is stated, not hidden |

Supporting bindings (same inspection): resident bytes =
`Evidence.content_snippet` (`domain/evidence/models.py:131`);
provenance window = `Evidence.provenance.source_location`
(`domain/evidence/models.py:138` → `SourceLocation`
`file_path/line_start/line_end/revision`, all Optional);
candidate identity = `Hypothesis` row
(`domain/hypothesis/models.py:134-150`:
statement/status/supporting_evidence_ids/version) — note the
row carries NO iteration field and NO producing-path field,
which is why persisted rows are cross-checks only and the
receipt-paired record (§1.4) is the authoritative join (C3);
activity inputs = `VerifyRootCauseInput` three scalars
(`application/worker/activities.py:1027-1032`,
verified read-only); retry policy `maximum_attempts=3`
(`application/worker/workflows.py:1169-1177`, applied at
`:1904`); SDK identity = `temporalio.activity.Info` field
tuple (pinned `temporalio>=1.33.0`, field names verified
against installed 1.33.0); D1-G static technique = AST
top-level import allowlist + `text.count("os.environ") == 1`
(`tests/unit/test_d1_flag_equivalence.py:93-109`, read
directly — exact mechanism and limits feed the C8
specification in §3.3).

### 9.3 Command list (Stage B/C execution; Stage A baseline documented)

To be run in stage order, with outputs attached to the
authorization request for the gate each command serves. Stage A
commands are evaluable now; Stage B/C commands require the
implementation or live runs and gate only their own stages —
never the implementation authorization itself:

```text
# Stage A baseline (evaluated 2026-10-24; gates implementation authorization):
# A2. Pre-change unit-suite floor — EXECUTED, output attached as §9.4
#     (3058 passed / 4 skipped / 6 xfailed, exit 0). Re-run command:
.venv/bin/python -m pytest tests/unit -q

# Stage B (require implementation; gate merge/acceptance only):
# B1. Static and unit gates (offline, deterministic)
.venv/bin/python -m ruff check src/investigation_agent_platform/domain/proof/ tests/unit/test_d3_*.py
.venv/bin/python -m pytest tests/unit/test_d3_definition_observation.py tests/unit/test_d3_definition_authorize.py tests/unit/test_d3_definition_trust.py tests/unit/test_d3_shadow_isolation.py tests/unit/test_d3_live_sample.py -q
.venv/bin/python -m pytest tests/unit/test_d1_flag_equivalence.py tests/unit/test_proof_kernel_fixture_integrity.py tests/unit/test_proof_kernel_baseline.py tests/unit/test_shadow_telemetry.py -q

# B2. Fixture ledger and baseline reproduction (no writes; nonzero exit on drift)
.venv/bin/python scripts/proof_kernel_ledger.py --check
.venv/bin/python scripts/proof_kernel_baseline.py --check

# 3. Unit scope, full suite (Stage B regression; record exact counts including skips/xfails)
.venv/bin/python -m pytest tests/unit -q

# NOTE (scope labeled accurately): the command above covers the unit
# scope only. The repository's complete regression includes contract,
# integration (postgres/neo4j/elastic-backed), system_api, evaluation,
# and benchmark suites with live-infrastructure prerequisites. The
# exact complete-regression command and its environment prerequisites
# are frozen at implementation authorization (D3P-F/§7 evidence),
# with each suite's scope documented alongside its output. No suite
# is silently substituted for another; a green unit scope never
# stands in for the full regression.

# 4. Flag-matrix (Stage B): runnable-combination differentials (native outputs
#    compared flag-off vs shadow-on; D3P-F01/F02)
IAP_SHADOW_TYPED_DEFINITIONS=0 IAP_TYPED_DEFINITIONS=0 .venv/bin/python -m pytest tests/unit/test_d3_shadow_isolation.py -q -k flag_matrix
IAP_SHADOW_TYPED_DEFINITIONS=1 IAP_TYPED_DEFINITIONS=0 .venv/bin/python -m pytest tests/unit/test_d3_shadow_isolation.py -q -k flag_matrix
# Non-runnable combinations are startup-failure tests, not
# differentials (D3P-F06/F07/F08): each must refuse loudly at
# startup; assert refusal + blast-radius scope, never compare
# outputs as an ordinary mode.

# 5. Live-sample collection (Stage C; only after protocol freeze + hash recorded; §6)
#    commands frozen with the protocol; outputs: denominator, four
#    decision-7 cells (≥3 each), full seven-class accounting,
#    evaluator-failure vs capture-error vs telemetry-omission counts,
#    harness-absence rule outcomes, snapshot identity
```

Stage-gate summary: Stage A artifacts (§9.2 audit attached; A2
baseline EXECUTED with output attached as §9.4; §6.2
probe plan) gate the implementation decision. Stage B artifacts
(D3P suites incl. N22–N26, F-differentials, ledger/baseline
`--check`, §9.0 suites or approved exceptions) gate merge and
Phase 0 acceptance. Stage C artifacts (§6 live evidence + the
§6.4 discipline + a separate owner decision) gate any rollout
or authority decision. No stage is satisfied by another stage's
artifacts.

Required outcomes: all green, zero drift, differentials byte-
identical where specified, live verdict per §6 vocabulary. Any
deviation is HOLD or INSUFFICIENT EVIDENCE — never a pass by
narrowing.

### 9.4 Stage A baseline evidence (D3P-AUTH-01 — executed, attached)

Executed 2026-10-24 in the implementation environment (the
working tree at commit `1389471`, uncommitted planning docs
only — no source changes). Exact command, as specified:

```text
.venv/bin/python -m pytest tests/unit -q
```

Result:

```text
3058 passed, 4 skipped, 6 xfailed, 47 warnings in 37.04s
```

Exit status 0. Environment: Python 3.13.15, pytest 9.1.1,
Darwin 27.0.0 arm64, repo commit
`1389471dd566039e5433fab3df2fd226d0ca4c8a`. The 47 warnings
are pre-existing `RuntimeWarning`s in neo4j-adapter tests
(unawaited mocks; unrelated to proof-kernel paths) — recorded
here so they are neither treated as regressions later nor
silently accepted as clean: the implementation baseline is
"3058/4/6 with 47 pre-existing warnings", and any deviation
from exactly these counts after implementation is a
regression signal until proven otherwise. No failures exist,
so no pre-existing-failure carve-outs were needed. Scratch
generated by the run (`.pytest_cache/`) was removed afterward;
`git status` confirms no source or artifact changes from the
run. Stage A baseline: COMPLETE. D3P-AUTH-01: CLOSED.

## 10. Decision restatement (v1.6 as-built status)

v1.5 remains the authorization basis that governed the Phase 0
implementation (owner GO 2026-10-24, Stage A). v1.6 does not
re-authorize anything: it is the as-built clarification and
acceptance basis, recording what was actually implemented
(§12), how the two accepted refinements are enforced and
tested, and the implementation evidence. Nothing in v1.6 widens
the authorized scope: implementation of the Phase 0 shadow
hook and its tests only — behind default-off flags, with no
authority, no verdict changes, no persistence changes beyond
the explicitly listed additive shadow-mode keys (§1.4/§1.6),
and no rollout. Phase 1, authoritative typed-definition
consumption, native-path changes beyond the specified read-only
taps, production use, and (per the GO) merge/acceptance itself
remain subject to separate explicit owner decisions — against
the §7 evidence (including the §9.2 attached audit, the §9.4
executed baseline, and the §9.0 suite results at their stages),
the §6 measurement under the §6.4 interpretation discipline,
the §11 matrix, and the §12 acceptance evidence. The
implementation stays uncommitted until the owner says
otherwise.

## 11. Finding-to-resolution matrix (v1.1 → v1.2)

| Finding | Severity | Changed section(s) | Resolving contract or test | Preserved (not reopened) |
|---|---|---|---|---|
| RV01 association admissibility vs binding authorization | Blocking P1 | §0.2, §2.3, §2.6, §4 (G02/G05/N13/N13b) | `association_scope` + machine-readable `declaration_requirement_satisfied: false`; two-stage outputs; no `AUTHORIZED` value | Option B; no AST semantics |
| RV02 T1/native-outcome correlation | Blocking P1 | §1.4 correlation contract | authoritative record + execution identity; offline `mirror-not-reached` classification; D3P-N18 | no verdict-path modification |
| RV03 persistence vs flag-off baseline | Blocking P1 | §1.4 lifecycle + per-flag table | runnable vs startup vs negative vs rollback split; payload differentials (F02/F09/N21); omission counting | same-write/same-transaction rule |
| RV04 runtime trust vs executing artifact | Blocking P1 | §3.3 non-claim + detectable table | deployment-identity pinning; static read-set as the unallowlisted-read mechanism; named gaps | fail-closed on unmeasurable (N20) |
| RV05 floor/taxonomy compatibility | Blocking P1 | §6.1 floor + subcategory rule; §6.4 | feasibility-gate label; conjunctive floor; restricted conclusions; interpretation discipline | INSUFFICIENT EVIDENCE default |
| RV06 dispatch/declaration conflation | P1 | §2.3 two stages; §4 G05 | per-stage outputs; dispatch match ≠ declaration satisfaction by construction | exact-match dispatch |
| RV07 scope vs interface inventory | P1 | §0.1; §1.6 list | precise input/output/telemetry scope; affected contracts named | no input-schema change |
| RV08 retry/duplicate identity | P1 | §1.4 key + retry rules | execution identity + content dedup fallback; D3P-N22 | maximum_attempts=3 honored |
| RV09 parity operationalization | P2 | §6.3 tuples; §2.6 E1 | per-tap tuples; divergence section; E1 reworded | E1–E4 classes kept |
| RV10 regression completeness | P2 | §9.0 inventory | freeze-list of suites + prerequisites; results-or-exception rule | unit scope honestly labeled |

## 11.1 Finding-to-resolution matrix (v1.2 → v1.3, adversarial findings)

| Finding | Severity | Changed section(s) | Resolving contract or test | Preserved (not reopened) |
|---|---|---|---|---|
| F-A1 currency sources ungrounded | Blocking | §2.5 (audit-gated table + default-unknown); §9 (schema-audit prerequisite); §4 (D3P-N23); §7 traceability | derivation rule + unaudited candidates; unbound → `unknown` → abstain; audit binds or declares unknown-by-default | no-extra-read rule; fail-closed default |
| F-A2 split stamping unspecified | Blocking | §1.4 correlation contract; §4 (D3P-N22 extended) | activity stamps identity + content; workflow stamps iteration on receipt; 1:1 receipt-order join | no input-schema change; no verdict-path change |
| F-A3 path attribution recoverability | Blocking | §1.4 coverage rule; §4 (D3P-N24); §6 taxonomy (ambiguous-join void) | producing-path label required; unattributable → void run set | offline join; never guessed |
| F-B1 commit_id alignment | Precision | §2.1 schema; §2.5/§2.6 references | `commit_id` mirrors native; provenance `revision` alongside with equality required | native model untouched |
| F-B2 policy_id stability | Precision | §2.1 schema; §1.2 tap row/sequence diagram; §2.2 producer inputs | `triage_route` (actually loaded) replaces derived `policy_id` | no new input fields |
| F-B3 omission registration | Precision | §1.6 item 5 (frozen key inventory); §6.3 report list | `telemetry_omissions` named in T4 keys and capture-health | counts-only telemetry rule |
| F-B4 T2 threading | Precision | Tap mechanics; §1.6 item 1 | activity-side list, closure capture, merge at return | engine methods never own notes |
| F-B5 persistence-call wording | Precision | §1.3 | "set of persistence calls" (same calls/transactions; additive payloads only) | — |
| F-B6 Stage 2 stub naming | Precision | §2.3; §4 (N13) | named stub `STUB_PHASE0_DECLARATION_CHECK_UNIMPLEMENTED` with pinned constant | two-stage order kept |
| F-B7 applicability hedge | Precision | §9.0 intro | freeze-list normative; presumption labeled as such | inventory intact |
| F-B8 F-numbering gap | Precision | §5.3 ID note | retired F03/F04 annotated; numbering preserved for traceability | — |

## 11.2 Finding-to-resolution matrix (v1.3 → v1.4, review C1–C10)

| Finding | Severity | Changed section(s) | Resolving contract or test | Preserved (not reopened) |
|---|---|---|---|---|
| C1 bundle identity at two times | Blocking P1 | §2.1 (two-layer identity + digests); §1.4/§1.6 (envelope at receipt); §4 (D3P-N25) | bundle digest (activity, no iteration) vs envelope digest (workflow, iteration + native snapshot); no retroactive mutation; stability pinned | no input-schema change |
| C2 retry identity precision | Blocking P1 | §1.4 (exact SDK tuple + accepted-attempt rule); §4 (N22 SDK-shaped records) | `temporalio.activity.Info` fields (pinned 1.33.0); group-by-(activity_id, attempt); mirror-conformance test | maximum_attempts=3 honored |
| C3 native-to-shadow join source | Blocking P1 | §1.4 (receipt-time pairing authoritative; rows cross-check only; per-tap table); §1.6 items 6–7 (envelope + producing_path) | named record + exact fields per tap; additive path label listed; D3P-N18/N24 | no new persistence behavior |
| C4 N13 contradiction | Blocking P1 | §2.3 (admitted vs refused); §4 (exact N13/N13b tuples + harness predicate) | association ADMITTED, satisfaction refused, machine-readable; consumer-side rejection asserted | two-stage order kept |
| C5 omission counting path | Blocking P1 | §1.4 step 7 (Path A in-band flag + Path B harness absence + unmeasurable residual); §1.6; §4 (N17) | `loop_telemetry_omitted` flag; completed-run reconciliation; harness-loss stated unmeasurable | three-class separation kept |
| C6 staged authorization gates | Blocking P1 | §9 (Stage A/B/C + §9.2 attached audit + §9.4 executed baseline); §7/§10 references | Stage A evaluated (audit attached, baseline 3058/4/6 exit 0, probe plan); B/C gated on implementation/live evidence | no future outputs for implementation approval |
| C7 trace truncation direction | P2 | §1.4 step 4; §4 (D3P-N26); §6 capture-health | prefix-256 retention; running counts complete; dropped turns excluded as truncation exclusions | existing slice pattern kept |
| C8 static analyzer scope | P2 | §3.3 analyzer spec + denylist + limitations | AST import allowlist + literal-read checks + denylist scan; subprocess forbidden; gaps and mitigations named | D1-G technique grounded |
| C9 authority wording | P2 | §2.3 Stage 2 paragraph | non-authoritative placeholder + negative guard; no evaluation claim | stub naming kept |
| C10 "compiled out" wording | P2 | §5.2 Off/Off row | "not activated; no shadow evaluation, acquisition, or shadow persistence effects" | activation model unchanged |

## 11.3 Finding-to-resolution matrix (v1.4 → v1.5, authorization review)

| Finding | Severity | Changed section(s) | Resolving contract or test | Preserved (not reopened) |
|---|---|---|---|---|
| D3P-AUTH-01 Stage A baseline documented, not evidenced | Blocking P1 | §9.1 Stage A text; §9.3 A2 command; new §9.4 attached output; §10 | exact pre-change command EXECUTED 2026-10-24 (3058 passed / 4 skipped / 6 xfailed, exit 0; Python 3.13.15, pytest 9.1.1, Darwin arm64, commit 1389471); 47 pre-existing warnings recorded as baseline, not regressions; scratch removed, tree unchanged | no implementation outputs required for implementation approval |
| Consistency: F08 harness scope | Blocking-adjacent | §5.3 harness-scope rule; F08 row | startup harness (F05–F07, isolated boot, expects refusal) vs in-process manipulation (runtime tests, monkeypatch post-startup, governed paths never booted); booting governed code with authority on is F06's test, never a differential | runnable/non-runnable split kept |
| Consistency: N13 enforcement language | Blocking-adjacent | §2.3 Stage 2 paragraph; §4 N13 emphasis; §7 traceability row | "enforcement point" replaced by the named triple (stub constant + consumer-absence + harness predicate); C9 placeholder framing preserved | two-stage order; stub naming |

## 11.4 Finding-to-resolution matrix (v1.5 → v1.6, implementation review)

| Finding | Severity | Changed section(s) | Resolving contract or test | Preserved (not reopened) |
|---|---|---|---|---|
| R-IMPL-01 content-identity dedup | Accepted P1 | §12.1; `pair_receipt` (definition_shadow.py); D3P-N22 + new non-merge negative test | canonical key (identity tuple + observation-bytes digest); deterministic keep-first; `duplicate_count`; distinct tap/input-set/reason/stage/outcome never merge | no joint validation; no native-path effect |
| R-IMPL-02 T3 conclude-result channel | Accepted P1 | §12.2; conclude result + workflow/T4 wiring; D3P-N24 + candidate-mismatch negative test | T3 co-located with native outcome in conclude result; analysis-side pairing; hypothesis_id binding check; separate accounting | no conclude changes beyond additive keys; no native-branch changes |

## 12. As-built record: R-IMPL-01 / R-IMPL-02 and Stage B evidence

This section describes what was actually implemented (verified
against the working tree 2026-10-24), not what v1.5 wording
alone would imply. Where the implementation is more precise
than v1.5, the precise behavior governs; v1.5's required
invariants are all preserved.

### 12.1 R-IMPL-01 — canonical deduplication identity

Duplicate detection key (implemented in `pair_receipt`):
`(namespace, workflow_id, workflow_run_id, activity_id, attempt)`
plus the canonical-JSON digest of the full observation dict.
Properties, each tested:

- Only genuinely equivalent duplicate observations collapse:
  identical bytes under the same execution identity (N22
  duplicate-injection test; new non-merge test asserts
  semantically distinct observations — different outcome, reason,
  input set, or stage — never merge even in one execution).
- Keep-first is deterministic: first-seen wins by receipt order;
  re-running the same receipt sequence reproduces the same
  kept/dropped partition (N25 envelope stability).
- Every suppressed duplicate increments `duplicate_count`
  (returned alongside envelopes; surfaces into the
  `shadow_definition_summary` allowlist).
- Distinct tap, execution, or semantic outcomes cannot be
  silently merged: any field difference changes the digest, so
  merging requires exact equality (negative test pins this).
- Deduplication never turns a missing or ambiguous attribution
  into a valid join: dedup operates before join audit, and
  `audit_envelope_join` voids ambiguous joins independently
  (N24, including the new candidate-mismatch void).

### 12.2 R-IMPL-02 — T3 conclude-result channel

T3 observations are produced inside `_gate_conclusion_or_fail`
and travel co-located with the conclude native outcome in the
conclude activity result (additive `shadow_definitions` key +
`producing_path: "conclude-gate"` + `loop_telemetry_omitted`
in-band flag). They cannot ride `loop_telemetry` (stamped
before conclude runs). Receipt-time pairing applies
analysis-side using the actual conclude result and its
corresponding execution context (same `pair_receipt` pure
function, tested with T3-shaped inputs). Accounting is separate
by construction:

- Verify-turn observations → workflow envelopes → bounded
  `definition_shadow_trace` + `shadow_definition_summary` in
  `loop_telemetry` (T4 allowlist, frozen key inventory).
- T3 observations → conclude activity result (durable via
  workflow history); joined to the conclude gate outcome by the
  co-located `producing_path` and the bound `hypothesis_id`.
- A T3 observation paired with a different candidate's native
  outcome is a `void:candidate-mismatch` (new
  `hypothesis_id` binding check + negative test) — incorrect
  T3 attribution can never count as parity.
- Missing T3 observations, failed pairing (`capture_error`
  entries), and telemetry omissions are counted, not silently
  absorbed: `capture_errors`, `dropped_count`,
  `telemetry_omissions`, harness-side absence detection (§1.4
  step 7, N17/N24/N26).
- The T3 mirror changes nothing native: no conclude branch,
  verdict, persistence call, or error semantic was altered
  (verify suite + conclude-path integration tests green).

### 12.3 Compatibility statement

Neither refinement expands the approved implementation scope
or changes the authority boundary:

- No new flags, schemas, migrations, resolvers, or evidence
  fetches were introduced; no native branch, verdict, tier,
  confidence, explanation, persistence write, publication
  decision, side effect, or error semantic changed (covered by
  F01/F02 differentials, N15–N17/N21 isolation tests, and the
  unchanged-behavior suites below).
- `typed_definitions_enabled()` has exactly one reader: the
  hard-refusal gate. No verdict, persistence, publication, or
  engine code reads it (N19b scan). No consumer exists that any
  flag value could activate (N19a/b).
- The `AUTHORIZED` value remains unrepresentable: not in any
  shadow-module code path (static scan incl. all three new
  modules) and not in the `ShadowOutcome`/`DefinitionStage`
  enums (N19a, extended in v1.6).
- The v1.3 Option B boundary is intact: every admissible test
  tuple carries `declaration_requirement_satisfied: false`
  (N13/N13b with the harness reference predicate).

### 12.4 Implementation evidence (commands run 2026-10-24)

Implementation state: uncommitted working tree at commit
`1389471` (no commit per standing instruction — therefore no
"implementation commit" exists to cite; the durable diff is the
working tree itself, summarized below; pre-existing tree
modifications unrelated to D3 are untouched).

```text
# D3 unit suites + nearest-neighbor suites, both flag states (F01/F02 differential core)
IAP_SHADOW_TYPED_DEFINITIONS=0 .venv/bin/python -m pytest tests/unit/test_d3_tap_isolation.py tests/unit/test_d3_shadow_runtime.py tests/unit/test_d3_shadow_bundle.py tests/unit/test_d3_definition_authorize.py tests/unit/test_d3_definition_observation.py tests/unit/test_shadow_telemetry.py tests/unit/test_synthesis_forcing.py tests/unit/test_proof_kernel_fixture_integrity.py tests/unit/test_proof_kernel_baseline.py tests/unit/test_verifier_version.py tests/unit/test_d1_flag_equivalence.py -q -p no:cacheprovider
# → 176 passed (off; includes N19 consumer/no-authorization scans, flag contracts, fixture integrity, verifier + D1 regression guards)
IAP_SHADOW_TYPED_DEFINITIONS=1 .venv/bin/python -m pytest <same selection> -q -p no:cacheprovider
# → 176 passed (on; identical — no native behavior change under the shadow flag)
```

Full unit suite, fixture ledger, baseline (Stage A regression floor preserved):

```text
.venv/bin/python -m pytest tests/unit -q -p no:cacheprovider
# → 3146 passed, 4 skipped, 6 xfailed, 47 warnings (baseline 3058 + 88 new D3 tests), exit 0
.venv/bin/python scripts/proof_kernel_ledger.py --check
# → structural check: PASS (67 entries across 7 files)
.venv/bin/python scripts/proof_kernel_baseline.py --check
# → baseline check: PASS
.venv/bin/python -m ruff check <touched files>
# → All checks passed
```

Touched-path integration (available without live infra):

```text
.venv/bin/python -m pytest tests/integration/test_verify_conclude_path.py -q
# → 6 passed
```

Tests exercising each refinement:

- R-IMPL-01: `test_d3p_n22_pairing_dedup_keep_first` (keep-first + counted), `test_d3p_n22_distinct_observations_never_merge` (negative: distinct never merge), `test_d3p_n22_superseded_attempt_distinguished`, `test_d3p_sdk_identity_mirror_conformance`, `test_d3p_n25_envelope_identity_stable_across_replay` (replay determinism; mutation breaks verification).
- R-IMPL-02: `test_d3p_n24_audit_ok_and_voids` family (missing outcome/observation, unattributable path, superseded), `test_d3p_n24_candidate_mismatch_voids` (negative: wrong-candidate pairing voids), `test_d3p_conclude_gate_input_set` (T3 observations carry CONCLUDE_GATE), `test_d3p_n17_derive_carries_definition_keys` + `test_d3p_n17_omission_flag_paths` (in-band flag + harness rule), `test_d3p_n26_prefix_retention_with_running_counts` (truncation accounting).

### 12.5 Stage B evidence checklist (owner decision §3 mapping)

1. Complete diff + change inventory, pre-existing changes intact: new files — `domain/proof/definition_{observation,dispatch,shadow}.py`, `tests/unit/test_d3_*.py` (5 files), `tests/fixtures/proof_kernel/definition_*.json` (3 files); extended — `verification.py` (observer params + record-only notes), `activities.py` (T1/T3 taps, `producing_path`, additive keys, T4 allowlist, hard refusal), `workflows.py` (receipt pairing, trace state, loop_telemetry keys), `scripts/proof_kernel_ledger.py` + fixture-integrity/baseline tests (new entries), `test_d1_flag_equivalence.py` (sanctioned-reuse allowlist), `test_verifier_version.py` (spy signatures). Pre-existing tree modifications (env-switch removals, G6 owed/forcing refactor, onboarding/CLI changes) untouched — verified by inspection of the diff hunks outside D3 paths.
2. §9.0 suite results: unit scope green (above); touched-path integration green (above); remaining suites (contract, system_api, evaluation, benchmarks, infra-backed integration) require CI/live services — see §12.6 for the per-suite execution record in this environment.
3. Refinement evidence: §12.1/§12.2 test lists above; all green in the full-suite run.
4. Flags/consumers/persistence: default-off proven (F05 + flag-off differential); no authoritative consumer exists (N19a/b scans); no unauthorized persistence or native-behavior changes (F01/F02 differentials, N15–N17/N21 isolation, unchanged full-suite behavior on all pre-existing tests).
5. v1.6 delta + traceability: this section + §11.4 below + §7/§10 references.

### 12.6 §9.0 suite execution record (this environment)

Executed 2026-10-24 in the implementation environment
(Python 3.13.15, pytest 9.1.1, Darwin arm64, uncommitted tree at
1389471). Suites that need unavailable live services skip
cleanly with explicit reasons rather than failing:

- `tests/unit` (all): PASS — 3146 passed, 4 skipped, 6 xfailed (baseline 3058 + 88 new D3 tests; see §12.4).
- `tests/integration` (all): 79 passed, 48 skipped, 0 failed —
  includes `test_verify_conclude_path.py` (the touched verify +
  conclude path) and `test_tenancy_rls.py`; skips are suites
  self-gating on live backing services (postgres/neo4j/elastic/LLM)
  unavailable here. No failures.
- `tests/contract` (all): PASS — 17 passed, 0 failed
  (MCP contract + adversarial; no live MCP server required).
- `tests/evaluation` (all): PASS — 11 passed, 0 failed
  (incl. incident replay + acceptance gates runnable offline).
- `tests/benchmarks`: 2 passed, 1 skipped (topology enrichment;
  skip is environment-gated).
- `tests/system_api` (all): 57 skipped, 0 failed — explicitly
  requires a live platform stack (`GET /api/v1/health/ready`;
  harness exits cleanly with "start it first" when absent).
  Deferred to CI where the stack exists. Exception rationale:
  D3 changes add no API routes, no MCP tools, no contract
  shapes; the only externally visible additions are additive
  telemetry keys (covered by unit shape tests) and additive
  activity-result keys (covered by N21/flag-off tests).
