# IAP L2/D3 Remediation Plan

> Status: PROPOSED. This document is a plan only. It grants no implementation,
> test, live-run, freeze, commit, merge, push, authority, or rollout permission.
> Each step is sequential and requires explicit user review and permission before
> the next step begins. A step may produce documentation, code, tests, or runtime
> evidence only within the permission granted for that step.

## 1. Basis and scope

This plan consolidates the gaps reviewed against:

- `docs/IAP-L2-variance-diagnosis-problem.md`
- `docs/IAP-D3-definition-convergence-problem.md`
- `docs/IAP-L2-D3-fix-test-procedure.md`
- `docs/IAP_Arch_Eng_Recommendations.log`

The current branch remains `L2-D3-fix`. The r3 D3 result remains a parity-only
NO-GO: it does not establish L2 convergence or prove the caller-edge fix. The
r2 freeze and verdict remain historical evidence and are not reinterpreted.

The remediation has two boundaries:

1. Native investigation behavior may be changed only to close verified L2 and
   abstention-flow gaps, with strict proof standards preserved.
2. D3 remains observational. Capture, receipt pairing, telemetry, freeze, and
   formal collection changes require the specific D3 gates described below.

## 2. Operating rules

- Review and permission are required after every numbered step.
- No step implicitly authorizes the next step.
- No commit, merge, push, worker restart, live run, tenant write, migration,
  freeze, re-freeze, or formal collection occurs without explicit permission.
- Existing frozen artifacts are not edited in place.
- Any change touching D3 capture, verify/conclude tap sites, receipt pairing,
  telemetry, or conclude wiring must re-run the D3 shadow suite and applicable
  flag differentials before it can be accepted as certified.
- Tests must distinguish retrieval, formulation, proof construction, policy,
  and genuine insufficiency; a passing unit test alone cannot close a runtime
  convergence gate.
- Complete caller enumeration remains strict unless a separate product decision
  explicitly authorizes a labeled partial-result contract.
- No historical verifier behavior is restored as an implicit fallback. Any
  comparison with historical behavior is offline, controlled, and non-authoritative.

## 3. Step 0 — Confirm the baseline and approval boundary

### Objective

Establish the exact starting tree and convert the reviewed gaps into an approved
work list without changing application behavior.

### Work

- Confirm branch, commit, worktree status, and existing user changes.
- Reconcile the current L2/D3 documents, ledger entries, and r3 NO-GO record.
- Build a gap matrix with one row per issue:
  - evidence status;
  - affected contract;
  - proposed change;
  - test required;
  - live/freeze implications;
  - explicit non-goal.
- Mark each proposed behavior change as either engineering remediation or product
  decision.
- Record that this plan itself authorizes no code or runtime action.

### Exit evidence

- Approved gap matrix.
- Explicit list of product decisions still required.
- Clean separation between offline validation, scratch validation, and formal
  D3 collection.

### Pause

Stop and request review of the baseline, scope, and gap matrix before Step 1.

### Step 0 baseline record

- Branch: `L2-D3-fix`.
- Current commit: `2484d9888a7ba1dbcc753990314c20e95bbb2747`.
- Remote comparison: `origin/L2-D3-fix` at `0b7df3c52ed7a53e7f7d93b1c4eca0a1a4b5963f`;
  the branch is five commits ahead.
- Worktree state at review: the recommendations ledger is modified and this plan
  is untracked; no other worktree changes were introduced by Step 0.
- Frozen/runtime interpretation: r3 has exact shadow/control parity for the
  recorded cases, but L1-L5, including L2, abstain in both arms. The run log
  records a probe-level NO-GO, not convergence and not proof that the caller-edge
  change succeeded.
- Existing freeze interpretation: r2 remains historical INSUFFICIENT evidence;
  r3 artifacts and the signed v3 documents are not treated as a successful fix
  validation set.

### Step 0 gap matrix

| Issue | Evidence status | Affected contract | Proposed engineering work | Decision or authorization still required | Test/live implication | Non-goal |
|---|---|---|---|---|---|---|
| 1. Formulation can select ABSENCE for presence evidence | Plausible and statically supported; r3 does not prove sole causation | Claim-kind selection and conclusion routing | Add evidence-aware claim disposition and deterministic presence/absence conflict handling | Approve exact conflict precedence; no automatic positive answer from a conflict | Offline mixed-evidence cases first | Do not weaken strict proof or infer causality from claim kind |
| 2. ABSENCE prompt lacks restraint | Prompt gap confirmed; same-scope impact needs tests | ABSENCE claim contract | Define same-scope positive evidence as disconfirming only when repository/revision/capability/scope match | Approve canonical scope semantics | Offline scope matrix; no live run yet | Do not make any positive row globally defeat absence |
| 3. Empty searches can be citable despite discovery-only marking | Source gap confirmed: rows are marked `discovery_only`, absence proof does not reject that marker | Evidence role and absence proof | Separate discovery-only empties from proof-eligible complete empties | Approve persisted/derived representation without migration if possible | Offline verifier and persistence tests | Do not delete historical evidence or reinterpret frozen rows |
| 4. ABSENCE has no disconfirmation repair forcing | Source gap confirmed for `_callsite_target`; causal impact unproven | Workflow repair and next-action selection | Add bounded ABSENCE disconfirmation obligations and targeted reads | Approve repair budget and terminal behavior | Offline workflow E2E; shared workflow files trigger D3 gates | Do not create an unbounded search loop |
| 5. Formulation assessments are uniform | Source behavior confirmed; rank impact unproven | Evidence assessment and ranking | Prefer typed evidence roles/eligibility over raw weight; only then consider ranking | Approve whether ranking changes are needed after role tests | Offline ranking regression tests | Do not treat evidence type alone as proof |
| 6. Hypotheses are mandatory with any visible evidence | Prompt behavior confirmed | Reasoner decision contract and workflow state machine | Add bounded `DEFER_GATHERING` without hypothesis persistence | Approve disposition vocabulary and defer budget | Offline state-transition tests | Do not allow indefinite deferral |
| 7. Partial caller enumeration abstains | Intentional strict behavior, not a defect | Product result contract and L2 verifier | Keep complete enumeration strict; expose structured proven/unresolved coverage | Product decision required for any `PARTIAL` result type | Offline API/grader tests; formal D3 eligibility must remain explicit | Do not label incomplete enumeration `ANSWERED` |
| 8. Unified-dev removed historical routing | Behavior change confirmed; load-bearing effect unproven | Verifier version policy | Run controlled offline discriminator; no silent legacy fallback | Decide whether an explicit `LIKELY`/`PARTIAL` tier is desired | Offline comparison only before any live validation | Do not restore hidden v1/v2 branches |
| Review-added L2 binding gaps | Static review found independent-obligation, repair-update, evidence-borrowing, and revision-binding concerns | Persistence, workflow, verification, conclusion | Add end-to-end obligation tests and close only verified failures | Approve strict per-edge/revision contract | Offline E2E before live L2 runs | Do not weaken call-site proof |
| Review-added D3 identity/provenance gaps | A2 fields absent from inspected envelope; r3 freeze object revision disagrees with signed docs | D3 envelope identity and freeze builder | Reconcile approved A2 identity and require explicit revision input/assertion | New amendment/freeze/run-set/budget authorization required for formal collection | D3 suites and provenance checks; no formal collection yet | Do not edit old frozen artifacts in place |

### Step 0 scope decision

The proposed next step is engineering-only contract definition and offline test
design. Product/protocol decisions are not silently assumed. The currently open
decisions are:

1. Exact canonical evidence-scope fields and containment semantics.
2. Whether `DEFER_GATHERING` is a persisted workflow disposition or an ephemeral
   action response, and its maximum budget.
3. Whether the product remains strict-only for caller enumeration or adds a
   labeled `PARTIAL` result.
4. Whether an explicit non-authoritative `LIKELY` tier is needed after the
   unified-versus-historical discriminator.
5. The A2 amendment and telemetry-field representation required for formal joins.
6. The owner-authorized sequence for any new freeze, run set, budget, worker
   restart, scratch validation, or formal collection.

Step 0 authorizes none of these changes or runtime actions. It establishes only
the baseline and the proposed work boundary.

## 4. Step 1 — Define canonical evidence scope and roles

### Objective

Prevent empty-search rows, search hits, graph rows, range reads, and runtime rows
from being treated as interchangeable proof.

### Work

- Define a typed canonical evidence scope containing repository, revision,
  capability, path/symbol/query selector, and completeness.
- Define scope equality and path-containment rules without arbitrary substring
  matching.
- Define evidence roles, at minimum:
  - `DISCOVERY_ONLY`;
  - `PROOF_ELIGIBLE`;
  - `DISCONFIRMING`;
  - `CONTEXT_ONLY`.
- Make absence proof require an exact, complete, revision-matched scope and a
  proof-eligible empty result.
- Ensure `discovery_only` rows cannot satisfy an absence proof.
- Preserve existing evidence IDs and persisted data shape unless a migration is
  explicitly approved; prefer derived/validated fields where possible.

### Tests

- Exact scope passes.
- Broad, narrow, sibling, and substring-only scopes fail appropriately.
- Different repository, revision, capability, and selector fail closed.
- Discovery-only empty rows cannot prove absence.
- Complete empty results remain usable for an explicitly scoped absence claim.

### Exit evidence

- Contract specification and test vectors.
- Unit tests for scope canonicalization, roles, equality, and containment.
- No changed verdict path beyond the approved scope contract.

### Pause

Stop and request review of the scope and evidence-role contract before Step 2.

### Step 1 contract record

Step 1 is documentation-only. It defines the smallest compatible contract over
the existing `Evidence`, provenance, attributes, request, claim-target, and
retrieval fields. It does not add a model, migration, runtime branch, or new
telemetry key.

#### Derived canonical evidence scope

```text
EvidenceScope {
  binding: {
    tenant: normalized tenant identity
    investigation: normalized investigation identity
  }
  repository: normalized repository identity
  revision: exact immutable revision; unknown is not equal to any revision
  capability: normalized operation/capability
  selector_kind: path_prefix | file_path | symbol | query | call_graph | history | state
  selector: normalized selector value(s)
  completeness: COMPLETE | PARTIAL | FAILED | UNKNOWN
}
```

The scope is derived at decision time from existing request fields, provenance,
`ClaimTarget`, retrieval metadata, and evidence attributes. Binding is explicit
and mandatory for proof decisions: tenant and investigation identities must both
be normalized and present. Missing binding fields are `UNKNOWN`, never
wildcards, and cannot match another scope. Missing scope fields are also
`UNKNOWN`, never wildcards. `HEAD`, absent revisions, and synthetic query-only
identities are not immutable proof revisions.

For state queries, the selector includes the template ID and canonically sorted
bind parameters. For code/history queries, it includes repository, revision,
capability, selector kind, path/symbol/query, and any direction or language
dimension that changes the result universe.

#### Equality and containment

Proof scope equality requires repository, exact revision, capability, selector
kind, selector value, and investigation/tenant binding to match after approved
normalization. Case folding is not applied indiscriminately to the serialized
scope; the case policy for each component must be explicit.

Path containment is segment-aware and is available for discovery and
disconfirmation only. A path prefix may contain a child path, but sibling paths,
`src/app` versus `src/application`, and arbitrary serialized substrings do not
match. An absence proof requires exact scope equality unless a future explicit
contract proves that a complete broader universe entails the narrower claim.

#### Derived evidence roles

Roles are contextual classifications, not replacements for `EvidenceType`:

| Role | Contract |
|---|---|
| `DISCOVERY_ONLY` | Path-only/search/symbol/graph material, explicit discovery marker, incomplete/failed retrieval, or unpinned revision; never proof by itself |
| `PROOF_ELIGIBLE` | Claim-kind predicate, exact scope/revision, and completeness all pass; evidence type alone cannot grant this role |
| `DISCONFIRMING` | Positive evidence or a candidate in the exact claimed scope that prevents an absence claim from being established, without automatically proving the positive claim |
| `CONTEXT_ONLY` | Orientation, documentation, metadata, or modality not accepted by the current claim-specific proof rule |

The invariant is:

```text
DISCOVERY_ONLY is never PROOF_ELIGIBLE.
```

For absence, a row is proof-eligible only when it has exact canonical scope,
exact immutable revision, capability match, explicit `COMPLETE` coverage,
numeric `match_count == 0`, and no discovery-only/failure/partial marker.

#### Step 1 test-vector matrix

| Vector | Expected result |
|---|---|
| Exact scope equality | Passes scope check |
| Normalized path separators/trailing separator | Passes only if approved normalization makes them equivalent |
| Broad proof path covering a child claim path | Discovery/disconfirmation containment only; absence proof remains exact by default |
| Narrow proof path for a broad claim | Fails |
| Sibling or segment-collision path | Fails |
| Different repository, revision, capability, selector kind, query, symbol, direction, or state parameter | Fails closed |
| Different tenant or investigation binding | Fails closed for proof and disconfirmation |
| Missing revision or completeness | Fails closed for proof |
| Partial, failed, or unknown retrieval | Fails closed for proof |
| Discovery-only empty row | `DISCOVERY_ONLY`; cannot prove absence |
| Complete exact empty row | `PROOF_ELIGIBLE`; may support absence before Step 2 conflict checks |
| Search/graph row with no claim-specific proof predicate | `DISCOVERY_ONLY` or `CONTEXT_ONLY` |
| Range read with matching file/revision/window | Potentially `PROOF_ELIGIBLE` for applicable code claims, never automatically for absence |
| Positive same-scope range row | `DISCONFIRMING` for absence; Step 2 decides the conflict outcome |
| Positive row from another revision | Does not disconfirm the current-revision absence claim |

#### Step 1 implementation boundary

The following are confirmed implementation gaps, but are deferred to later
steps: verifier enforcement of roles and completeness, structured scope
construction, presence/absence conflict handling, and producer-to-verifier
integration. Step 1 defines their contract and test vectors only.

Step 1 exit evidence is this contract record plus the referenced current-tree
inspection. No runtime verdict has changed, and no live or frozen artifact is
affected.

## 5. Step 2 — Add deterministic presence-over-absence conflict handling

### Objective

Prevent a presence claim from being reframed as a valid absence claim when
proof-eligible positive evidence exists in the same scope.

### Work

- Add a pure conflict evaluator before `absence_proof` succeeds.
- Require positive evidence to match the same canonical scope, repository,
  revision, and capability before it can contradict absence.
- Add stable reasons such as:
  - `ABSENCE_CONTRADICTED_BY_PRESENCE`;
  - `ABSENCE_SCOPE_INCOMPLETE`;
  - `ABSENCE_PROOF_DISCOVERY_ONLY`.
- Keep the verifier fail-closed: conflict means absence is not established, not
  that the positive claim is automatically proven.
- Add telemetry/diagnostic detail only where it is already permitted; do not
  silently add D3 telemetry keys.

### Tests

- Same-scope positive range evidence blocks absence.
- Positive evidence from another revision does not block absence.
- Search/graph-only presence does not prove a positive claim but can trigger
  disconfirmation work.
- Empty and positive rows from unrelated scopes do not cross-contaminate.

### Exit evidence

- Deterministic conflict matrix.
- Regression tests proving absence cannot win over same-scope proof-eligible
  presence.

### Pause

Stop and request review of the conflict policy and reason vocabulary before Step 3.

### Step 2 implementation record

Implemented the narrow Step 2 verifier guard in
`application/investigation/verification.py`:

- discovery-only zero-result rows return `DISCOVERY_ONLY` and cannot prove
  absence;
- malformed match counts are not treated as numeric zero;
- a same-tenant, same-investigation, same-serialized-scope positive row returns
  `PRESENCE_CONFLICT` with stable reason
  `ABSENCE_CONTRADICTED_BY_PRESENCE`;
- a positive row from another investigation does not disconfirm the current
  absence claim;
- absence remains fail-closed when no eligible empty row exists.

Focused verification passed:

```text
uv run pytest tests/unit/test_phase2_remaining.py tests/unit/test_rev_battery_goldens.py -q
28 passed
```

This step does not yet construct structured repository/revision/capability
scopes or change the empty-row producers. Those remain Step 4 work, where the
canonical scope can be enforced consistently from request through evidence
banking and verification. No D3 telemetry or capture path was changed.

## 6. Step 3 — Introduce explicit defer-before-hypothesis semantics

### Objective

Stop forcing a committed hypothesis when evidence is mixed, incomplete, or
requires disconfirmation.

### Work

- Define a non-hypothesis disposition such as `DEFER_GATHERING`.
- Require a defer decision to contain:
  - reason;
  - missing proof kind;
  - bounded next action;
  - target/scope;
  - remaining repair budget.
- Ensure deferred decisions do not create hypotheses, assessments, or conclusion
  candidates.
- Route the workflow to the bounded next action.
- Define a maximum defer/repair count and a typed terminal insufficiency result.
- Keep model readiness advisory; the workflow remains authoritative.

### Tests

- Mixed positive/empty evidence produces defer rather than a persisted ABSENCE
  hypothesis.
- Defer executes the selected action exactly once within the budget.
- Defer exhaustion produces `INSUFFICIENT_EVIDENCE` with a targeted blocker.
- Malformed or unbounded defer requests fail closed.
- Existing direct formulation and verification paths retain their contracts.

### Exit evidence

- Decision schema and workflow state-transition table.
- Offline end-to-end test from mixed evidence through defer exhaustion.

### Pause

Stop and request review of the defer contract and budget semantics before Step 4.

### Step 3 implementation record

Implemented bounded `DEFER_GATHERING` semantics:

- `InvestigationDecision` now has optional typed `disposition` and
  `defer_gathering` fields; defer remains outside executable `ActionType` and
  `ActionOutcomeStatus` vocabularies.
- Sanitization validates bounded reason/proof/target/scope fields, object-shaped
  action parameters, integer budget `0..2`, and rejects terminal next actions
  (`FORMULATE_HYPOTHESIS`, `VERIFY_HYPOTHESIS`, `CONCLUDE`).
- The workflow owns a two-slot defer budget; the model-reported remaining budget
  is informational and does not control enforcement.
- A valid defer turn clears hypotheses and claims, dispatches exactly one
  gathering action, records disposition and defer counters in action history,
  retrieves the resulting evidence, and skips same-turn formulation,
  verification, conclusion, and ordinary forcing overrides.
- Exhaustion routes through `defer_repair_budget_exhausted`, which is treated as
  epistemic failure and persists `INSUFFICIENT_EVIDENCE` with a targeted next
  check through the existing conclusion path.
- Invalid defer payloads fail closed; they do not fall back to `QUERY_STATE`.

Verification:

```text
ruff check <Step 3 touched files>
All checks passed!

uv run pytest tests/unit/test_reason_requery.py tests/unit/test_phase2_remaining.py tests/unit/test_rev_battery_goldens.py -q
35 passed
```

The implementation intentionally does not yet add structured repository,
revision, capability, or canonical scope validation to defer targets. That
remains part of the later producer/scope work and does not weaken the workflow
budget or hypothesis-suppression guarantees in this step.

#### Step 3 review correction

The defer sanitizer now validates `next_action` against the existing gathering
`ActionType` set, excluding `FORMULATE_HYPOTHESIS`, `VERIFY_HYPOTHESIS`, and
`CONCLUDE`. Unknown values such as `FOO` fail at the decision gate with the same
`defer_gathering_invalid` terminal behavior and cannot consume a repair slot.
The focused suite now reports `36 passed`; the revised sanitizer test also passes
Ruff.

## 7. Step 4 — Add ABSENCE disconfirmation forcing

### Objective

Make ABSENCE verification symmetric with definition and caller-edge verification:
positive discovery candidates must receive bounded reads before absence may
conclude.

### Work

- Extend the workflow forcing seam for ABSENCE claims.
- Identify positive search/symbol/graph rows in the claimed scope that have not
  received a revision-matched range read.
- Force bounded `GET_CODE` or the appropriate capability-specific read.
- Carry a stable disconfirmation obligation key through the action and result.
- Rebind returned evidence to the exact scope and investigation.
- Re-run the presence/absence conflict evaluator after repair.
- Record budget exhaustion and unresolved candidates explicitly.

### Tests

- Positive discovery row forces one targeted range read.
- Different-scope candidate does not force or contradict the claim.
- Returned range evidence is bound to the disconfirmation obligation.
- Repair failure yields typed insufficiency, not absence.
- Repeated turns do not loop after the repair budget is exhausted.

### Exit evidence

- Workflow/action contract.
- Offline end-to-end repair tests.
- No D3 capture-path changes unless separately approved; if shared workflow code
  is touched, the D3 protection suite becomes an exit gate.

### Pause

Stop and request review of the forcing behavior before Step 5.

### Step 4 implementation record

Implemented bounded ABSENCE disconfirmation forcing:

- The workflow reads a compact, sanitized evidence view returned by
  `reason_activity`; it does not perform repository I/O from workflow code.
- A candidate is eligible only when it is cited by an ABSENCE claim, has a
  provider-recorded file path and line range, comes from `SEARCH_CODE`,
  `FIND_SYMBOL`, or `FIND_CALL_GRAPH`, and matches the claimed absence scope.
- Locationless graph summaries, prose-derived paths, empty rows, unrelated
  scopes, and already-read files are rejected.
- The forced action is `GET_CODE` with the exact provider path/range/revision and
  an `ABSENCE_DISCONFIRMATION|<sha256>` obligation key covering tenant,
  investigation, scope, candidate evidence ID, path, revision, and range.
- The forcing budget is bounded at two reads and the action is recorded in
  workflow history with `forced_absence_disconfirmation`.
- A forced disconfirmation read consumes the turn and returns to reasoning; the
  stale ABSENCE hypothesis is not verified on that same turn.
- The returned range remains discovery/proof input for the next reasoner turn;
  this step does not automatically promote it to a positive claim.

Verification:

```text
ruff check <Step 4 touched files>
All checks passed!

uv run pytest tests/unit/test_synthesis_forcing.py tests/unit/test_phase2_remaining.py tests/unit/test_reason_requery.py -q
66 passed
```

No D3 capture path, migration, live run, worker restart, or frozen artifact was
changed.

#### Step 4 audit correction

The Step 4 forcing contract was tightened after audit:

- Every downstream forcing family now checks the disconfirmation flag, so
  formulation, synthesis, reread, call-site, log-search, replan, and other
  forcing paths cannot replace a forced disconfirmation read or burn its budget.
- Candidate scope matching is exact case-insensitive serialized-scope equality;
  substring containment is not used.
- Candidate eligibility requires a positive integer `match_count > 0`; booleans,
  zero, negative, missing, and malformed counts are rejected.
- Candidate rows must have a non-empty tenant binding. Investigation identity is
  retained in the obligation material but remains nullable at this step; stronger
  run-binding equality is deferred to Step 5.
- Candidate access handles both model objects and the dict-shaped evidence view
  returned by `reason_activity`, so `attributes`, scope, and discovery metadata
  are actually enforced in the workflow path.

Additional regression vectors cover downstream override precedence, segment
collision, empty/boolean match counts, empty tenant binding, and dict-shaped
evidence. The focused suite now reports `67 passed`; Ruff remains clean. The D3
shadow suite remains a Step 9 gate because shared workflow code changed.

## 8. Step 5 — Close L2 caller-edge obligation integrity

### Objective

Make caller enumeration complete, edge-local, revision-pinned, and repairable
without weakening the anti-fabrication rule.

### Work

- Derive candidate caller obligations from graph/search evidence independently of
  model wording.
- Preserve model-named callers absent from discovery as unresolved obligations.
- Use one canonical obligation identity containing callee, caller, normalized path,
  repository/investigation scope, and revision.
- Prevent qualified caller names from being split into false obligations.
- Bind each edge only to its own evidence IDs and matching file/revision.
- Update unresolved assessments after forced reads instead of skipping already
  known statements.
- Reject foreign-investigation, wrong-revision, path-mismatch, and borrowed-sibling
  evidence.
- Keep `CALL_EDGE_INCOMPLETE_ENUMERATION` for incomplete answers.

### Tests

- Omitted graph caller becomes unresolved.
- Aggregate model statement creates one obligation per caller.
- Qualified caller identity remains one caller.
- One edge cannot borrow a sibling edge’s range read.
- Wrong revision fails binding.
- Forced read updates the same obligation and can discharge it.
- Complete enumeration reaches the structural conclusion path without a causal
  chain; incomplete enumeration remains non-answerable.

### Exit evidence

- Persistence → workflow → retrieval → verification → conclusion end-to-end test.
- Adversarial tests for omission, borrowing, revision mismatch, duplicate, and
  candidate-limit cases.
- Focused tests, Ruff, compilation, and relevant full-suite results.

### Pause

Stop and request review of the L2 obligation contract and proof that strict
complete enumeration remains intact before Step 6.

### Step 5 inspection record

The current implementation does not close caller-edge obligations end to end.
The confirmed gaps are:

- graph/search-discovered callers are not independently materialized as durable
  unresolved obligations when the model omits them;
- aggregate counterpart tokenization splits qualified callers and drops target
  identity fields such as revision/scope;
- the canonical key is path/commit based but does not explicitly carry the full
  repository/investigation/scope identity;
- persistence and verification bind primarily by path and non-null evidence ID,
  not exact edge ownership, revision, or target range;
- forced caller reads omit the target revision, and a wrong-revision read can
  suppress a later read of the same path;
- forced reads do not reconcile the existing unresolved assessment;
- unresolved `SUPPORTS` assessments with `evidence_id=None` conflict with the
  current hypothesis consistency validator and can fail persistence;
- verify and conclude depend on duplicated vulnerable caller-edge evaluation;
- `direction=BOTH` call-graph depth expansion currently expands only one side.

### Proposed minimal implementation order

1. Fix qualified aggregate parsing and preserve complete target identity.
2. Fix bounded `BOTH` graph-depth expansion and expose candidate completeness.
3. Normalize provider graph/search candidates independently of model claims and
   union them with model-named callers before building obligations.
4. Centralize exact edge binding: tenant/investigation, repository/path,
   revision, range, eligible range-read operation, and candidate ownership.
5. Carry revision-aware obligation identity through forced caller reads and track
   prior reads by obligation identity, not path alone.
6. Make unresolved obligation assessments persistable without fabricating support
   evidence, then reconcile the same hypothesis after a forced read using OCC.
7. Extract shared caller-edge evaluation for verify and conclude.
8. Add adversarial persistence-to-conclusion tests before any live validation.

This scope preserves strict complete enumeration and does not authorize partial
answers, D3 changes, live runs, worker restarts, migrations, commits, or freezes.

#### Step 5 scope review clarification

The dependency order is accepted unchanged. Implementation must specifically
preserve unresolved caller obligations without fabricated evidence IDs. Because
the current `Hypothesis` consistency validator rejects `SUPPORTS` assessments
whose `evidence_id` is `None`, item 6 must choose an explicit unresolved-obligation
representation or validator exemption. It must not use a placeholder evidence ID
or silently drop the unresolved assessment. F4/F5/A2 remain within scope; strict
complete enumeration remains unchanged.

### Step 5.3 implementation record

- Provider-located `CALL_GRAPH` edge rows are projected by `reason_activity` as
  bounded discovery candidates, including candidate evidence ID, caller, callee,
  file, line range, revision, and an explicit incomplete/discovery marker.
- The workflow carries those candidates through `ExecuteActionInput`; hypothesis
  materialization unions them with model claims before building caller obligations.
- Discovered candidates omitted by the model become unresolved `CALL_EDGE`
  obligations rather than disappearing.
- `Hypothesis` consistency permits the explicit `CALL_EDGE_UNRESOLVED` assessment
  with `evidence_id=None`; no placeholder evidence ID is fabricated.
- `BOTH` call-graph depth expansion and qualified caller identity were corrected
  in Steps 5.1 and 5.2.
- Projection coverage now drives `_caller_edge_candidates_from_evidence` with
  provider-shaped `CALL_GRAPH` rows and asserts the complete candidate payload.
  A row missing `caller_symbol` is skipped while a subsequent valid row still
  projects, covering the fail-closed filtering behavior.

### Step 5.4 implementation record

- Step 5.4 is scoped to `CALL_EDGE` proof only. The binding helper requires a
  non-empty exact revision, explicit target file and line range, one consistent
  tenant/investigation binding across the bound set and candidate row, matching
  repository identity, an eligible code range read, target-range containment,
  and caller/callee identity.
- Unknown revision, foreign binding, sibling search rows, cross-revision rows,
  and out-of-range reads fail closed.
- All caller-edge verification branches use the helper: per-edge typed
  assessments anchor on their cited row, while aggregate and single-edge paths
  anchor on the hypothesis bound set.
- Definition proof remains unchanged and is outside this step's scope.
- The helper returns only rows whose windows contain the target range. A
  non-containing same-file/same-revision row cannot be passed to
  `call_site_proof` as a fallback. Regression coverage includes containing plus
  non-containing rows and a non-containing-only `CALL_EDGE_INCOMPLETE_ENUMERATION`
  result.

Verification for the materialization slice:

```text
ruff check <Step 5.1-5.3 touched files>
All checks passed!

uv run pytest tests/unit/test_phase2_remaining.py tests/unit/test_caller_edge_key_parity.py tests/unit/test_evidence_bound_verification.py -q
83 passed

uv run pytest tests/unit/test_evidence_bound_verification.py tests/unit/test_caller_edge_key_parity.py tests/unit/test_rev_battery_goldens.py tests/unit/test_synthesis_forcing.py tests/unit/test_phase2_remaining.py tests/unit/test_reason_requery.py tests/unit/test_code_recall_gateway.py -q
163 passed, 11 warnings

ruff check <Step 5.4 touched files>
All checks passed!

python -m compileall -q <Step 5.4 touched files>
Passed
```

### Step 5.5 implementation record

- Forced caller reads now carry the canonical edge identity plus caller, callee,
  normalized path, target revision, target line range, scope, tenant, and
  investigation parameters.
- Successful historical `GET_CODE` reads suppress only the same full obligation;
  same-path reads at another revision or range, and reads from another binding,
  do not suppress the required read.
- The workflow passes the current tenant and investigation binding into the
  forcing helper. Definition forcing retains its existing path behavior.
- Regression coverage includes same-path wrong-revision, wrong-range, and
  foreign-binding reads.

Verification for the Step 5.5 slice:

```text
uv run pytest tests/unit/test_synthesis_forcing.py tests/unit/test_evidence_bound_verification.py tests/unit/test_caller_edge_key_parity.py tests/unit/test_rev_battery_goldens.py tests/unit/test_phase2_remaining.py tests/unit/test_reason_requery.py tests/unit/test_code_recall_gateway.py -q
166 passed, 11 warnings

ruff check <Step 5.5 touched files>
All checks passed!

python -m compileall -q <Step 5.5 touched files>
Passed
```

### Step 5.6 implementation record

- Unresolved `CALL_EDGE_UNRESOLVED` assessments remain persisted with
  `evidence_id=None` until an exact forced range read is available.
- After a successful caller-edge `GET_CODE`, the activity reloads evidence and
  hypotheses for the same tenant/investigation, validates the returned row
  through the Step 5.4 binding helper, and matches unresolved assessments by
  canonical edge identity.
- Matching assessments are replaced with `CALL_EDGE_BOUND`, their exact
  evidence ID is added to `supporting_evidence_ids`, and the same hypothesis is
  saved with its current version. A concurrency conflict re-reads and retries
  once; unresolved or ineligible reads do not fabricate support.
- The activity reports the reconciliation count as diagnostics; strict
  complete enumeration and answer semantics are unchanged.

Verification for the Step 5.6 slice:

```text
uv run pytest tests/unit/test_synthesis_forcing.py tests/unit/test_evidence_bound_verification.py tests/unit/test_caller_edge_key_parity.py tests/unit/test_rev_battery_goldens.py tests/unit/test_phase2_remaining.py tests/unit/test_reason_requery.py tests/unit/test_code_recall_gateway.py -q
167 passed, 11 warnings

ruff check <Step 5.6 touched files>
All checks passed!

python -m compileall -q <Step 5.6 touched files>
Passed
```

### Step 5.7 implementation record

- Added `VerificationEngine.evaluate_caller_edge()` as the shared caller-edge
  decision seam. Non-CALL_EDGE claims retain the existing `None` result; caller
  claims use the exact binding, revision, range, and enumeration logic.
- Both `verify_root_cause_activity` and `conclude_investigation_activity` now
  use this evaluator for `caller_edge_validated`, so unresolved assessments and
  exact-binding failures produce the same result in both paths.
- Persisted `CALL_EDGE_UNRESOLVED` assessments are explicitly incomplete until
  Step 5.6 reconciliation changes them to `CALL_EDGE_BOUND`.

Verification for the Step 5.7 slice:

```text
uv run pytest tests/unit/test_synthesis_forcing.py tests/unit/test_evidence_bound_verification.py tests/unit/test_caller_edge_key_parity.py tests/unit/test_rev_battery_goldens.py tests/unit/test_phase2_remaining.py tests/unit/test_reason_requery.py tests/unit/test_code_recall_gateway.py -q
168 passed, 11 warnings

ruff check <Step 5.7 touched files>
All checks passed!

python -m compileall -q <Step 5.7 touched files>
Passed
```

### Step 5.8 implementation record

- Added composed persistence-to-conclusion coverage for unresolved caller-edge
  assessments: `evidence_id=None` remains incomplete, the shared evaluator
  returns `CALL_EDGE_INCOMPLETE_ENUMERATION`, and the conclusion gate denies
  approval without relabeling the result as partial or answered.
- Added exact-binding adversarial coverage proving a same-revision, same-range
  caller row can pass the caller-edge kind gate while a wrong-revision sibling
  cannot be borrowed as proof.
- Existing omission, sibling-repair, constructor-only, cross-range,
  cross-binding, and OCC reconciliation tests remain in the Step 5 battery.

Verification for the complete Step 5 slice:

```text
uv run pytest tests/integration/test_verify_conclude_path.py tests/unit/test_evidence_bound_verification.py tests/unit/test_phase2_remaining.py tests/unit/test_synthesis_forcing.py tests/unit/test_caller_edge_key_parity.py tests/unit/test_rev_battery_goldens.py tests/unit/test_reason_requery.py tests/unit/test_code_recall_gateway.py -q
176 passed, 11 warnings

ruff check <Step 5 touched files>
All checks passed!

python -m compileall -q <Step 5 touched files>
Passed
```

Step 5 is complete. Step 6 remains pending approval.

### Step 6 implementation record

- Adopted the strict-only caller result contract. Incomplete caller enumeration
  remains `INSUFFICIENT_EVIDENCE`/`INCONCLUSIVE`; no `PARTIAL` conclusion status
  was added and no `ANSWERED` path was relaxed.
- Added persisted caller diagnostics to conclusions: `proven_callers`,
  `unresolved_callers`, and `caller_coverage` (`proven/required`). The fields
  are diagnostic coverage, not proof or D3 authority.
- Populated diagnostics for caller-edge abstentions and approved conclusions,
  and carried them through ORM, normal repository, proof-commit projection,
  and migration `035_caller_coverage_diagnostics`.

Verification for Step 6:

```text
uv run pytest tests/integration/test_verify_conclude_path.py tests/unit/test_evidence_bound_verification.py tests/unit/test_phase2_completion.py tests/unit/test_tenant_onboarding_control_store_migration.py -q
126 passed, 6 xfailed

uv run pytest tests/integration/test_verify_conclude_path.py tests/unit/test_evidence_bound_verification.py tests/unit/test_phase2_remaining.py tests/unit/test_synthesis_forcing.py tests/unit/test_caller_edge_key_parity.py tests/unit/test_rev_battery_goldens.py tests/unit/test_reason_requery.py tests/unit/test_code_recall_gateway.py -q
178 passed, 11 warnings

ruff check <Step 6 touched files>
All checks passed!

git diff --check
Passed
```

Step 6 is complete. Step 7 remains pending approval.

### Step 7 implementation record

- Added an offline verifier discriminator with identical hypothesis, evidence,
  policy, and statement inputs across four caller-edge cases: complete proof,
  wrong revision, wrong range, and foreign tenant binding.
- The unified verifier accepts only the complete case. The historical
  pre-remediation comparator accepts all four because it used file-local sibling
  repair without exact revision, range, or binding enforcement.
- The historical comparator is test-only. No legacy routing, environment switch,
  or runtime fallback was added.

Verification for Step 7:

```text
uv run pytest tests/unit/test_verifier_discriminator.py tests/unit/test_verifier_version.py tests/integration/test_verify_conclude_path.py tests/unit/test_evidence_bound_verification.py tests/unit/test_phase2_remaining.py tests/unit/test_synthesis_forcing.py tests/unit/test_caller_edge_key_parity.py tests/unit/test_rev_battery_goldens.py tests/unit/test_reason_requery.py tests/unit/test_code_recall_gateway.py -q
194 passed, 11 warnings

ruff check <Step 7 touched files>
All checks passed!

python -m compileall -q <Step 7 touched files>
Passed

git diff --check
Passed
```

Step 7 is complete. Step 8 remains pending approval.

### Step 8 implementation record

- Added A2 identity fields to shadow observations: `active_behavior_version`,
  audit `verifier_version`, `protocol_id`, and `amendment_id`; all are included
  in the content identity hash.
- Added protocol and amendment identity to receipt envelopes and their digest
  preimage. Envelope joins now fail closed for protocol, amendment, behavior,
  candidate, path, or superseded-attempt mismatches.
- Added `verify_provenance_consistency()` for exact manifest/freeze/attestation/
  governed-tree revision equality.
- Changed the Stage C run-set builder to require an explicit code revision,
  match it against the signed manifest, archive that revision, and reject
  governed-tree modifications. Invocation without the required revision fails
  before creating artifacts.
- Historical freeze records, manifests, and run sets were not modified. No
  Stage C execution, live validation, authority, rollout, or worker restart was
  performed.

Verification for Step 8:

```text
uv run pytest tests/unit/test_d3_shadow_runtime.py tests/unit/test_d3_shadow_bundle.py tests/unit/test_d3_definition_observation.py tests/unit/test_d3_definition_authorize.py tests/unit/test_d3_tap_isolation.py tests/unit/test_harness_freeze.py tests/unit/test_harness_capture_control.py tests/unit/test_verifier_version.py -q
157 passed

ruff check <Step 8 touched files>
All checks passed!

python -m compileall -q <Step 8 touched files>
Passed

git diff --check
Passed

uv run python scripts/build_d3_stagec_runset.py
Rejected: explicit governed code revision required.
```

Step 8 is complete. Step 9 remains pending approval.

### Step 9 implementation record

- Ran the focused L2/verifier/offline conclusion battery, including the
  persistence-to-conclusion integration path, caller-edge discriminator, and
  historical verifier label tests: `199 passed, 11 warnings`.
- Ran the D3 shadow/runtime, definition observation/authorization, tap
  isolation, harness capture/control, freeze, canonical, metrics, CLI, ledger,
  and registry suites: `253 passed`.
- Ran the complete offline evaluation suite: `11 passed`.
- Ran the full unit suite: `3362 passed, 4 skipped, 6 xfailed, 47 warnings`.
  The only two failures are the intentional proof-kernel baseline checks,
  which require the worktree to equal the older pinned baseline commit. No
  baseline re-freeze or artifact mutation was performed.
- Updated stale migration-chain expectations for the approved migration 035;
  the migration subset passes `159 passed, 2 warnings`.
- Ruff, compilation, and `git diff --check` pass on all Step 9 touched paths.
- No unexplained flag-dependent native behavior, no D3 shadow capture failure,
  and no live execution, worker restart, freeze, authority, or rollout action
  occurred.

Residual limitation: the repository-wide baseline gate remains intentionally
red until a separately authorized re-freeze or baseline decision. Step 9 does
not authorize that action.

Step 9 is complete. Step 10 remains pending approval.

### Step 10 implementation record

- Confirmed runtime prerequisites without exposing credentials: API readiness
  returned HTTP 200; layered database identity was `localhost:5452/iap`; the
  approved scratch tenant `dev-shadow-certify` is distinct from the frozen probe
  tenant `iap-probe-2026-10-26-r3`.
- The pre-run scratch dry-run was clean: zero existing investigation, evidence,
  hypothesis, finding, conclusion, session, tool-execution, contradiction, or
  transition rows; no cleanup was executed.
- Restarted the worker under the approved shadow runtime boundary and verified a
  single worker tree. The API remained ready.
- Ran exactly one L2 scratch battery case with no prior cleanup. It stopped on
  the first failure as required: `FAILED`, reason
  `defer_repair_budget_exhausted`, three path-only source rows, zero hypotheses,
  and zero conclusions.
- The post-run read-only audit found one scratch investigation, three evidence
  rows, zero hypotheses, zero conclusions, and eight transitions. No D3 trace was
  available because the run failed before hypothesis formulation.
- The control arm was not run because the procedure requires stopping on the
  first scratch failure. Scratch rows were not deleted.

Step 10 is incomplete and does not support a convergence claim. The required
follow-up is an offline diagnosis/fix of the defer/retrieval budget path before
any further live run. Step 11 remains blocked.

### Step 10 re-run record (2026-10-26, explicit owner permission)

The defer/retrieval follow-up from the record above is complete (missing
calibration checkout rebuilt; hypothesis persistence for all action types;
CALL_EDGE revision pin; call-edge causal synthesis — all verified live).
With fresh explicit permission, Step 10 was re-executed per the test
procedure, both arms:

- Offline gates: full unit suite 3642 passed + 2 documented baseline pins;
  ruff clean on all touched files (7 pre-existing ruff findings in untouched
  files left alone).
- Restarted the worker with `IAP_SHADOW_TYPED_DEFINITIONS=1`, verified a
  single poller tree, API READY. Cleaned the scratch scope via
  `clean_battery.py --execute` (1 prior PASS row removed; its report kept).
- Shadow arm: `live_battery --tenant dev-shadow-certify --only L2
  --no-cleanup-prior` → **PASS (COMPLETED)** in 14.2s. Root cause
  "OrderServiceRestProxy calls the order service handler via its handler
  field and submit method"; 1 VERIFIED + 1 PROPOSED hypothesis; 3
  SOURCE_CODE rows (2 discovery + 1 range read at `0d9982f6`); 2
  CALL_EDGE_BOUND assessments; conclusion `ROOT_CAUSE_ESTABLISHED`;
  zero defer/repair turns fired. Report
  `reports/dev-shadow-L2-20261026T125657.md`.
- Capture verification (§3.2): 149-record trace, 0 ERROR/CRITICAL, 0
  capture-error/INVALID_FLOAT/pairing-failure signatures. D3 tap armed
  (flag on, dispatch table loads, mode=shadow): verify-turn T1
  observations reproduced as `UNAVAILABLE / not-a-definition-claim`
  (correct abstention for caller-enumeration; zero capture_error markers).
  T2 mirror-note sink is wired as engine observer; no drops evidenced.
- Control arm: restarted worker without the flag, single poller, API READY;
  re-ran L2 (battery cleanup-prior) → **PASS (COMPLETED)** in 13.1s with
  the identical root cause, evidence profile (3 SOURCE_CODE, range read at
  `0d9982f6`), 149-record trace, 0 errors, conclusion
  `ROOT_CAUSE_ESTABLISHED` and a 1-step causal-chain finding. Native parity
  holds; D3 keys absent by construction (flag off). Report
  `reports/dev-shadow-L2-control-20261026T130157.md`.
- Post-run: scratch holds 1 investigation (control arm); probe
  `iap-probe-2026-10-26-r3` untouched at 12 rows; `n-batt` untouched.

Engineering signal only: both Step 10 arms pass with native parity and a
clean capture bill. No formal convergence claim is made; Step 11 (protocol
amendment, freeze, run-set, budget, formal authorization) remains blocked
pending owner approval.

## 9. Step 6 — Decide and, if approved, implement partial-result semantics

### Objective

Resolve whether users need useful partial caller information without relabeling
an incomplete enumeration as `ANSWERED`.

### Decision

The default recommendation is **do not relax the strict answer contract**.
Expose structured diagnostics instead:

```text
status: INCONCLUSIVE
proven_callers: [...]
unresolved_callers: [...]
coverage: proven/required
next_action: GET_CODE
```

If a partial product result is approved, define it explicitly as `PARTIAL`, keep
it ineligible for formal D3 PASS, and require the unresolved set and coverage in
the response and persisted conclusion.

### Exit evidence

- Explicit product decision: strict-only or labeled partial.
- Contract, API, persistence, grader, and battery expectations aligned.
- Tests for no accidental `ANSWERED` result from partial coverage.

### Pause

Stop and request the product decision before any partial-result implementation.

## 10. Step 7 — Run a controlled verifier-behavior discriminator

### Objective

Determine whether verifier consolidation contributes to the abstention change
without restoring silent legacy behavior.

### Work

- Build an offline comparison harness using identical fixture, evidence, model
  output, workflow budget, and configuration inputs.
- Compare strict unified behavior with the historical behavior only as a test
  comparator, never as a runtime fallback.
- Record claim kind, persisted hypotheses, bound evidence, verification result,
  gate blockers, and final disposition.
- Separate prompt, evidence, model, and verifier-version factors.
- Classify differences as contract changes, not automatically as regressions.

### Exit evidence

- Reproducible comparison matrix.
- Decision on whether any explicit `LIKELY`/`PARTIAL` tier is needed.
- No legacy branch restored without separate approval.

### Pause

Stop and request review of the discriminator results and any proposed behavior
policy before Step 8.

## 11. Step 8 — Repair D3 A2 identity and freeze provenance

### Objective

Ensure formal evidence can be joined to the exact behavior, protocol, amendment,
and code revision that produced it.

### Work

- Resolve the A2 contract conflict between required observation identity and the
  approved telemetry allowlist.
- Under an approved amendment, add and test the required identity fields:
  `active_behavior_version`, audit verifier label, protocol/amendment identity,
  and receipt binding as applicable.
- Ensure empty-trace absence and mirror-note/capture-error semantics remain
  unchanged.
- Change the run-set builder to accept an explicit code revision and fail if it
  silently substitutes `HEAD`.
- Assert identical revision metadata across freeze record, manifest, freeze object,
  runtime attestation, and governed tree hash.
- Reject dirty governed trees and mismatched signed metadata.

### Gates

- D3 shadow runtime and bundle suite.
- Flag-off differential and D3-key absence tests.
- Full unit suite with known baseline exceptions explicitly recorded.
- Ruff and static checks on touched paths.
- Scratch telemetry only if runtime capture changes: envelopes present,
  `capture_errors == 0`, and required receipt identity present.

### Exit evidence

- Reconciled A2 amendment/protocol.
- Provenance consistency report with exact revision equality.
- No formal collection under a mismatched or superseded freeze.

### Pause

Stop and request review of the D3 identity and provenance evidence before Step 9.

## 12. Step 9 — Offline acceptance battery

### Objective

Prove the native contracts before spending live budget.

### Work

- Run the complete focused L2/D3 unit and offline end-to-end suites.
- Run adversarial cases for all eight reviewed issues plus the prior caller-edge
  review findings.
- Run D3 flag matrix and tap-isolation tests if shared files changed.
- Confirm no shadow capture, verifier, or conclusion regression.
- Produce a review report listing every pass, failure, skipped gate, and residual
  limitation.

### Exit bar

- No unresolved high-severity offline contract failure.
- No unexplained flag-dependent native behavior.
- No test that passes only by injecting a final validation boolean without testing
  the production path that computes it.

### Pause

Stop and request review of the offline acceptance report before Step 10.

## 13. Step 10 — Scratch validation only

### Objective

Validate behavior on a non-frozen scratch tenant without making formal claims.

### Work

- Obtain explicit permission for worker restart and scratch tenant writes.
- Run the existing L2-focused procedure only after all offline gates pass.
- Record per run:
  - claim kind and disposition;
  - obligations/proven/unresolved counts;
  - evidence scope, roles, IDs, files, revisions, and ranges;
  - retrieval, defer, and repair turns;
  - verifier and conclusion blockers;
  - shadow trace, mirror notes, capture errors, and native parity.
- Stop immediately on capture error, fresh control divergence, or provenance
  mismatch.
- Classify results as engineering signal only.

### Exit evidence

- Scratch report with no formal PASS/rollout claim.
- Root-cause attribution for any remaining failure.
- Decision whether another offline fix cycle is required.

### Pause

Stop and request review of scratch results before Step 11.

## 14. Step 11 — Formal D3 amendment, freeze, and convergence collection

### Objective

Only if scratch validation supports it, collect formal evidence under a new,
explicitly authorized protocol and freeze.

### Preconditions

- Owner-approved behavior and protocol amendment.
- A2 identity and eligibility mapping reconciled.
- Exact code revision and governed tree frozen.
- New freeze record and manifest signed/hash-verified.
- New run-set ID and budget authorized.
- Offline and scratch gates accepted.

### Collection

- Repeated identical flag-on L2 executions.
- L1/L4 regression guards as specified by the D3 procedure.
- Fresh flag-off controls with native parity and D3-key absence.
- Stop on any fresh control divergence or capture/provenance violation.
- Report pass-rate confidence intervals and eligible/void/excluded attempts.
- Do not call one PASS, exact parity, or one successful repair convergence.

### Exit evidence

- Formal run-set report.
- Per-run obligation and receipt evidence.
- Capture health and control-parity evidence.
- Owner decision: close as converged, retain as insufficient, or return to a
  named remediation step.

### Pause

Stop and request owner review of the formal collection report before any D3 close,
authority, rollout, merge, or push decision.

## 15. Step 12 — Close-out or carry forward

### Objective

Make the final state explicit without overstating evidence.

### Work

- Update the L2 and D3 problem documents with as-built behavior and evidence.
- Close only ledger items supported by the accepted evidence; retain unresolved
  items as `OPEN` or `PLANNED`.
- Record any product decisions and their contract consequences.
- Record formal verdict, confidence limitations, and remaining risks.
- Keep authority and rollout decisions separate from convergence evidence.

### Exit evidence

- Updated documentation and ledger.
- Explicit final verdict and residual gap list.
- No unreviewed implementation, freeze, or runtime claim remains implied.

### Pause

Stop for final user review and explicit permission for any close, merge, push,
authority, or rollout action.

## 15b. Step 12 close-out record (End A — 2026-10-26)

Verdict: retain Step 11 r4 as INSUFFICIENT. No convergence claimed. No D3
close, authority, rollout, merge, or push taken. The plan terminates here;
any future cycle requires fresh authorization (new amendment, freeze record,
run-set id, and budget, in that order).

Formal evidence (unchanged, recorded for reference):

- `reports/d3-stagec-2026-10-26-r4.md`: 10 shadow + 6 control on probe
  `iap-probe-2026-10-26-r4`, exact per-fixture parity (L4 via cached-CONFIRMED),
  capture clean (0 errors, 0 capture/pairing flags), budget spent 10/48.
  RUN_LOG committed as `85aedd4`. Freeze v4 record `6dcae600…`, code rev
  `eeecfae`.
- Pass-rate CIs (Clopper-Pearson 95%, r4 only): L2 5/5 `[0.478, 1.000]`;
  accept pooled 7/9 `[0.400, 0.972]`; overall 8/10 `[0.444, 0.975]`;
  L8 nonaccept 1/1 `[0.025, 1.000]`.

Remediation shipped after the verdict (commit `156c6f5`, offline gates green:
full unit 3645 passed + 2 known pre-existing baseline pins, ruff clean):

- L3 evidence honesty: history misses now bank deterministic discovery-only
  `SOURCE_CODE` absence rows (never `COMMIT_HISTORY`); `history_proof` and
  `validate_history_claim` skip `discovery_only` / `match_count==0` rows.
- L5 pre-claim forcing fallback: a `BEHAVIOR`-typed claim with no
  `fact_category` heading to VERIFY, with a banked `.java`/`.py` `GET_CODE`
  lacking a method/field variant, forces one full-window (1–2000) re-read so
  the model can retype as `SOURCE_VALUE_FACT`. Bounded by the existing
  2-force budget; `BEHAVIOR` bar and profile doctrine untouched.

Scratch validation (native, fix tree, tenant
`dev-remediation-l3l5-20261026`, report
`reports/live_battery_20261026T173731.md` — engineering signal only, no
formal claim):

- L5 PASS (11.5s, VERIFIED via the model-typed `SOURCE_VALUE_FACT` /
  `GUARDED_RETURN_LITERAL` route, `res_v2` on a single 6–19 range read). The
  forcing fallback did not fire (single `GET_CODE` total), so L5 typing
  variance stands unmeasured.
- L3 FAIL (`defer_repair_budget_exhausted`) with honest evidence: 3 real
  stamped `COMMIT_HISTORY` rows + 3 `SOURCE_CODE` search hits, zero
  fake-`COMMIT_HISTORY` absence rows — the L3 honesty fix is verified live.
  Blocker scoped to proof shape, not retrieval: the model cited non-latest
  `ee6b9036` ("Guard null orderType with STANDARD default") while the latest
  path commit is `0d9982f6` ("Tighten filter eligibility window"), and the
  statement inserts "a" ("with a STANDARD default"), breaking the exact
  casefold-substring subject containment.

Residual gaps (retained OPEN, no new cycle authorized):

1. HISTORY content-grounding architecture: `ClaimTarget` carries
   `commit_id`/`file_path` but no structured subject field, so content
   grounding relies on exact-substring matching against freeform prose —
   not a deterministic check for LLM output. Redesign options recorded in
   session (structured `commit_subject` field; token-set normalization;
   dropping the content gate; advisory-only demotion); none selected.
2. Newest-commit rule stands as correct ("most recent" ⊂ "recent change");
   the L3 miss is a model-citation/formulation gap, and the verifier
   behaved correctly in refusing it.
3. L5 typing variance: `BEHAVIOR`-without-category dead-ends in code-only
   scopes; the fallback is unproven insurance. Measuring it needs repeated
   scratch runs, not authorized here.

L1/L2/L4/L8 behavior is recorded as converged and stable across both r4
arms. No authority or rollout decision is implied by this record.

## 16. Approval record

| Step | Scope approved | Reviewer | Date | Evidence | Next-step permission |
|---|---|---|---|---|---|
| 0 | Pending | | | | Pending |
| 1 | Pending | | | | Pending |
| 2 | Pending | | | | Pending |
| 3 | Pending | | | | Pending |
| 4 | Pending | | | | Pending |
| 5 | Pending | | | | Pending |
| 6 | Pending | | | | Pending |
| 7 | Pending | | | | Pending |
| 8 | Pending | | | | Pending |
| 9 | Pending | | | | Pending |
| 10 | Pending | | | | Pending |
| 11 | Formal r4 collection (10 shadow + 6 control) | owner direction (session 2026-10-26) | 2026-10-26 | reports/d3-stagec-2026-10-26-r4.md; RUN_LOG @85aedd4 | Returned to remediation; closed via Step 12 (End A) — no re-collection |
| 12 | Close-out End A (no convergence claim) | owner direction (session 2026-10-26) | 2026-10-26 | §15b close-out record above | None — plan terminates; future work needs fresh authorization |
