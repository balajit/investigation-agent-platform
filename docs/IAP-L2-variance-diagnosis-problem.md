# Problem Definition 1/3 — L2 Variance Diagnosis and Fix Design

> Branch context: `L2-D3-fix` (branched from `iap-onboarding-v0.1` @ `5a7eb13`).
> This prompt hands the L2-D3-fix problem to GPT for a first shot.
> Diagnosis and design record. The design below is not an implementation,
> migration, live run, flag change, or verdict change.

## 1. Background

Battery case L2 (`tests/fixtures/battery_cases/L2.json`) asks the callers
question:

```text
symptom: "What calls the order service handler?"
expect:  status=ANSWERED, claim=callers, callee=OrderServiceHandler,
         callee_method=handle, expect_any=[OrderQualifyHelper, OrderServiceRestProxy]
```

The same L2 case flips PASS/FAIL across identical arms and configurations.
Owner classification (2026-10-24, ledger line 501): the L2 divergence is
**variance, not interference** — but the mechanism is unnamed. Naming it is
this task.

## 2. Observed evidence (do not re-run; read these)

| Run set / arm | L2 verdict | Detail |
|---|---|---|
| r1 shadow (`reports/d3-stagec-2026-10-24-r1-shadow.md`) | PASS | COMPLETED, conclusion supported by cited evidence, 15.1s, 3 ev / 2 hyps |
| r2 shadow (`reports/d3-stagec-2026-10-24-r2-shadow.md`) | **FAIL** | COMPLETED, `status=INSUFFICIENT_EVIDENCE, want answered`, 45.2s, 10 ev / 2 hyps |
| r2 control, flag-off (`reports/d3-stagec-2026-10-24-r2-control.md`) | PASS | COMPLETED, conclusion supported by cited evidence, 15.1s, 4 ev / 1 hyp |
| Scratch batteries `dev-shadow-certify-full*.md`, `dev-shadow-certify-L1*.md` | PASS (all) | Same flag-ON configuration as the r2-shadow FAIL |

Key rows (retained in dev DB, do not modify):
- r2-shadow FAIL: investigation `e5392dd6` (16-trace struggle run, 45.2s)
- r2-control PASS: investigation `5469c121` (fresh flag-off run, 15.1s)
- r1-shadow PASS: same symptom, tenant `iap-probe-2026-10-24`
- Freeze context: `docs/IAP-proof-kernel-d3-stageC-freeze-record-v2.md`
  (record hash `ff0cf8e7…`), run set `d3-stagec-2026-10-24-r2`,
  control artifact `artifacts/d3-stagec-2026-10-24-r2/control-artifact.json`
  (pairing verdict for L2: `DIVERGE-STOP`; read its L2 `divergence` block —
  it already records the leading variance hypothesis and history)

## 3. Diagnosis

The primary gap is a **unit-of-proof mismatch at caller enumeration**:

- The reasoner is allowed to emit one aggregate hypothesis such as
  `OrderQualifyHelper and OrderServiceRestProxy call OrderServiceHandler.handle`.
  The structured prompt describes caller enumeration as requiring one
  `CALL_EDGE` claim per caller (`src/investigation_agent_platform/application/investigation/reasoning.py:196-207`).
- The persistence path nevertheless accepts the aggregate hypothesis and only
  persists assessments for the claims the model emitted
  (`src/investigation_agent_platform/application/worker/activities.py:2716-2739,
  5428-5564`). Missing caller claims are not materialized as explicit pending
  obligations.
- The verifier correctly requires every named caller to have its own bound edge
  claim (`src/investigation_agent_platform/application/investigation/verification.py:1987-2066`).
  A graph summary or search hit is insufficient; the cited range must show the
  invocation (`verification.py:1838-1891`).
- The workflow repair seam forces `GET_CODE` when a claimed caller file has not
  been read (`src/investigation_agent_platform/application/worker/workflows.py:531-545,
  1571-1587`), but it does not repair a missing per-caller claim or bind the
  resulting range to a new edge assessment.
- Graph retrieval does attempt bounded caller-file reads
  (`src/investigation_agent_platform/application/worker/activities.py:4979-5061,
  5102-5147`), but those rows remain discovery/candidate material until the
  reasoner binds them to the corresponding edge claim. Thus more retrieval can
  leave the verifier with the same incomplete claim set.

The first reliable divergence class is therefore **formulation/binding**, with
verification demotion as the deterministic consequence. The FAIL run's
`ABSTAINED x8` plus `CITED_LOCATION-mirror x8` trace is consistent with this
path, but the unavailable report rows and failed database authentication mean
the exact first failing turn cannot be recovered here. The frozen r2 outcome
remains `DIVERGE-STOP` and `INSUFFICIENT`; this design does not reinterpret it.

### Classification

The underlying gap is a **deterministic product defect** that exposes itself as
sampling variance. The LLM varies in whether it emits complete per-caller
claims and bindings; the verifier then deterministically rejects incomplete
enumerations. This is not a reason to weaken `CALL_EDGE` proof or to count a
call graph as a call site.

The diagnosis would be falsified if retained turn records show that every named
caller had an independently bound `CALL_EDGE` assessment with a range-read row
showing the invocation, and the FAIL instead originates in retrieval failure,
evidence freshness, contradiction handling, or a conclude-only gate blocker.

## 4. Fix Design

### 4.1 Contract

Introduce a normalized **caller-edge obligation set** for a CALL_EDGE
enumeration. The set is derived before hypothesis persistence and is the unit
that the reasoner, workflow, verifier, and grader all observe:

```text
CallerEnumeration {
  callee: OrderServiceHandler.handle,
  edges: [
    { caller: OrderQualifyHelper, evidence_ids: [...], state: PROVEN },
    { caller: OrderServiceRestProxy, evidence_ids: [...], state: PROVEN }
  ],
  required_callers: [...],
  coverage: 2/2
}
```

The persisted hypothesis may retain the human-readable aggregate statement,
but each edge must be represented by its own typed assessment:

```text
claim_kind = CALL_EDGE
claim_target.symbol = <callee or callee.method>
claim_target.counterpart = <one caller only>
evidence_ids = <that caller's own range-read call-site rows>
```

No aggregate caller name may satisfy an individual edge obligation. A graph
edge, declaration, constructor, comment, or search hit remains discovery only.

### 4.2 Normalization and repair pipeline

1. **Recognize the question shape.** When the request is caller enumeration,
   select the CALL_EDGE enumeration contract from the existing structured
   claim kind. Do not infer this from arbitrary prose at persistence time.
2. **Extract candidate callers.** Normalize caller symbols from call-graph
   edge metadata and from the model's aggregate statement. Preserve qualified
   names and map display stems only for matching; never discard the canonical
   symbol.
3. **Build obligations.** For each candidate caller, create one obligation key:
   `(callee symbol, caller symbol, file path, revision)`. Deduplicate by this
   key and cap the set at the existing bounded caller limit. If the model names
   a caller not present in the graph/search evidence, retain it as
   `UNRESOLVED`, not as a proven edge.
4. **Bind available evidence.** Match only same-investigation, revision-aware
   range reads whose content shows the callee invocation. Use the existing
   `_call_shown` and instance-call join rules; do not expand them into
   declaration or graph-summary acceptance.
5. **Repair missing bindings.** If an obligation has a known caller file/line
   but no range read, force one `GET_CODE` for that obligation. The repair result
   must be attached to that obligation's evidence set, not merely added to the
   hypothesis-wide corpus. Bound repair attempts remain capped and are recorded
   with a typed reason when they cannot complete.
6. **Persist explicit status.** Persist assessments for every obligation,
   including unresolved ones, with a stable reason code such as
   `CALL_EDGE_UNRESOLVED`, `CALL_EDGE_NO_CALLSITE`, or
   `CALL_EDGE_REVISION_MISMATCH`. Only `PROVEN` obligations contribute to a
   verified result; unresolved obligations force `INCONCLUSIVE`.
7. **Verify and conclude.** Keep `check_claim_kind` strict. It evaluates each
   edge assessment and then requires `required_callers - proven_callers` to be
   empty before returning success. The conclusion gate consumes the same
   normalized coverage result so a later aggregate prose rewrite cannot hide a
   missing edge.

### 4.3 Minimal code seams

- `ports/reasoning/reasoner.py:16-34`: add an optional structured
  `caller_edges`/obligation payload to `DecisionClaim`, or use a dedicated
  `CallerEdgeClaim` model if the existing claim model becomes overloaded.
  Prefer the dedicated model if it avoids allowing aggregate `counterpart`
  strings in new data.
- `application/worker/activities.py:2716-2739`: normalize and validate the
  claim payload before deduplication; do not collapse two edges that share the
  aggregate statement.
- `application/worker/activities.py:5428-5564`: create one assessment per
  normalized edge and preserve missing obligations with reason codes.
- `application/worker/workflows.py:531-545,1571-1587`: change call-site forcing
  from “unread caller file” to “unresolved caller obligation”; carry the
  obligation identity through the forced `GET_CODE` action and its outcome.
- `application/investigation/verification.py:1838-1891,1987-2076`: consume
  normalized edge obligations, require per-edge call-site proof, and return a
  stable blocker naming the unresolved caller.
- `application/investigation/verification.py:4282-4354`: pass the normalized
  coverage summary into `ConclusionGate`; reject incomplete caller sets before
  publication, without adding causal-chain requirements to this structural
  fact.

Do not change shadow taps or their receipt pairing. The fix belongs on the
native formulation/binding path; D3 remains observational. The dev system has
one active verifier behavior. Historical `v1`/`v2`/`v3` labels may remain on
old audit records or frozen artifacts, but they do not select runtime behavior;
new dev investigations do not create a verifier-version pin.

### 4.4 Failure behavior

- Complete per-caller range evidence: `VERIFIED`, then `ANSWERED` if all other
  gates pass.
- Graph/search rows only: `INCONCLUSIVE` with
  `CALL_EDGE_NO_CALLSITE` and a targeted next action to read each missing
  caller file.
- One caller proven, one caller unresolved: `INCONCLUSIVE` with
  `CALL_EDGE_INCOMPLETE_ENUMERATION`; never publish the partial list as the
  requested answer.
- Caller names in prose but no matching discovery evidence: preserve the
  names as unresolved claims and request a scoped call-graph/search read; do
  not fabricate a caller edge.
- Revision mismatch or cross-investigation evidence: reject the binding with a
  typed reason; do not fall back to same-name rows from another scope.
- More candidates than the bound limit: return an explicit
  `CALL_EDGE_CANDIDATE_LIMIT` limitation and do not claim complete enumeration.

## 5. Implementation Status

The approved implementation is now delivered as the single active dev
verifier behavior. The historical `v3` label is retained only for audit and
frozen-record compatibility.

- `DecisionClaim` accepts structured `caller_edges`; the reasoner sanitizer and
  prompt require one target and evidence binding per edge.
- The unified persistence path expands aggregate caller edges, records one typed assessment
  per edge, stores unresolved obligations with `evidence_id=None` and stable
  reason codes, and stores required edge keys in the existing JSON
  `required_verification` field. No migration or new table was added.
- Workflow call-site forcing now carries `caller_edge_key` and handles
  structured edge targets before the existing definition-read forcing.
- Verification requires every persisted caller obligation to be proven by
  invocation evidence, regardless of any historical label on the row.
- Verify and conclude paths pass complete caller coverage to `ConclusionGate`,
  allowing a structural caller answer without a causal chain while preserving
  fail-closed behavior for incomplete coverage.
- Shadow taps, receipt pairing, and flag behavior were not changed.

Regression coverage covers aggregate and incomplete caller obligations,
version routing, conclusion-gate structural exemption, the existing sibling
repair path, and prompt versioning. The implementation-specific suite passes
(`114 passed`), Ruff passes on touched Python files, and source compilation
passes.

The remaining validation is empirical: a newly authorized freeze/run set must
measure repeated L2 convergence and flag-off control parity. The full local
suite is currently blocked during collection by unrelated missing secret-store
modules in `tests/unit/test_datasources.py` and
`tests/unit/test_secret_stores.py`; those failures are not caused by this
change.

## 6. Verification Plan

### Unit and integration coverage

- Aggregate statement with two callers and two independently bound range reads
  verifies with two `CALL_EDGE` assessments.
- Aggregate statement with one assessment fails with
  `CALL_EDGE_INCOMPLETE_ENUMERATION` and names the missing caller.
- A graph summary without a range-read call site fails closed.
- A constructor-only row (`new OrderServiceHandler()`) fails as a caller edge.
- A declaration without an invocation fails; same-file declaration plus
  instance invocation passes through the existing instance-call join.
- A range read for the correct caller file that is not linked to that caller's
  obligation is not silently borrowed by a sibling edge.
- Wrong revision, foreign investigation, duplicate edge, and candidate-limit
  cases are rejected deterministically.
- The workflow forces `GET_CODE` once per unresolved caller obligation, carries
  the obligation key, and binds the returned row to the correct assessment.
- The conclusion gate rejects incomplete caller coverage and approves complete
  coverage without requiring a causal chain for the structural caller answer.
- Existing legacy `v1` rows and current `v2` rows retain their recorded
  semantics; the new normalized behavior is tested under the new version only.
- Shadow-on and shadow-off differential tests prove native verdicts, persisted
  non-shadow fields, and save-call sequences are unchanged by the observational
  capture path.

### Live convergence measurement

After implementation and its unit gates, use a new freeze/run set authorized by
the owner. Run repeated identical flag-on L2 executions plus a fresh flag-off
control. Record, per run:

- final verdict and conclusion status;
- number of caller obligations, proven obligations, unresolved obligations;
- per-obligation evidence IDs, file/revision/range, and gate reason;
- retrieval turns, repair turns, and whether a call-site read was forced;
- verifier and conclude-gate blockers;
- shadow capture health and native/control parity.

The design is converging when no run publishes an incomplete enumeration and
the observed PASS/FAIL sequence is stable across the pre-authorized repeat
budget. Report a binomial pass-rate confidence interval; do not treat one PASS
as convergence. A fresh control divergence still stops the probe under the
existing rule.

## 7. Updated Acceptance Criteria

- [x] Primary gap named: aggregate caller formulation is not normalized into
      per-caller proof obligations before strict verification.
- [x] Existing evidence and exact code seams identified.
- [x] Fix design preserves strict call-site proof and shadow/native isolation.
- [x] Failure modes, unified dev rollout, tests, falsifier, and convergence
      measurement are specified.
- [x] Obligation normalization, workflow repair identity, conclusion coverage,
      version routing, regression tests, Ruff, and compilation are complete.
- [ ] A newly authorized live convergence run set remains outstanding.
- [ ] Full-suite collection remains blocked by the unrelated missing
      secret-store modules named above.

## 8. Remaining Frozen Constraints (do not violate)

- No changes to `config/`, migrations, flags, or env as part of this design.
- No live runs, no worker restarts, no tenant writes, and no new freeze/run set
  without owner authorization.
- No commits in this session. No references to `tmp/` scratch in any output.
- The r2 freeze (`ff0cf8e7…`) and r2 verdict (INSUFFICIENT, kept open)
  stand; this diagnosis must not reinterpret them.

## 9. Original Diagnosis Acceptance Criteria

- [x] A single named mechanism with file:line evidence and a stated falsifier.
- [x] Divergence class identified as formulation/binding followed by
      deterministic verification abstention; exact first turn remains
      unavailable because the named reports and database rows were not
      accessible in this workspace.
- [x] Classification and follow-up supplied: deterministic product gap exposed
      as sampling variance, with a fix design and convergence measurement plan.
- [x] Output retained in this existing problem document.

## 10. Non-goals

- No full obligation-normalization implementation in this step. No re-freeze,
  no new run set, and no verdict changes to r1/r2 records.
- No verdict changes to r1/r2 records.
