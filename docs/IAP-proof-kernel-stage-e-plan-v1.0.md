# Stage E Implementation Plan — DRAFT v1.0 (planning only)

Status: DRAFT for owner review. This document plans the work; it does not
authorize it. No code, no fixtures, no runs, no adapter changes follow from
this plan. Per the BCD closeout plan, Stage E stays deferred by design and
each adapter needs its own graduation decision after D-stable.

## 1. Scope (traceability-anchored)

| ID | Item | Current state |
|---|---|---|
| H | Solver wrong abstraction | N/A — solver pilots deferred |
| I | Unrealizable solver model | N/A — solver pilots deferred |
| R | Abstract solver witness → no concrete claim | N/A — solver pilots deferred |
| G8 | Witnesses | NOT STARTED — Stage E deferred by plan |

Non-goals (normative, from ops-review): no witness-gated `VERIFIED`; no
solver beyond admitted value/branch predicates; `UNKNOWN`/timeout/
unavailable stays advisory-only; no workflow/confidence-as-proof; no
planner-fed derivation; no persisted deficiencies.

## 2. Entry criteria (all must hold before any E1 pilot)

1. **D-stable**: Wave 3 D1/D2 complete, native track live and authorized.
   Current status: NOT MET (native track blocked on B+C live).
2. **Native baseline**: frozen native measurements for the claim kinds each
   candidate adapter would witness, so pilot benefit is computable.
3. **Admitted predicate set**: the value/branch predicates a solver may
   encode, reviewed and pinned. Anything outside is out of scope by rule.

## 3. Adapter inventory (graduation units, from current tree)

- `application/analysis/python/` (dispatch, literals, repo_version, control, framework_facts)
- `application/analysis/java/` (incl. provenance)
- `application/analysis/repository_analyzer.py`, `sync_merge.py`

Each graduates independently. No adapter rides another's decision.

## 4. Phases

### E0 — Frozen witness contracts (docs + fixtures, no production contact)
For each candidate adapter: generic path-witness record schema
(nodes/edges, roles, order, completeness, truncation, source revision,
analysis config, limitations) and, for solver families, solver manifest
schema (encoded predicate, assumptions, omissions, status, model mapping,
independent check). Fixtures are synthetic; mappings follow the
test-only authority rule (cf. C2-R1). Exit: owner accepts the schemas.

### E1 — Shadow pilots (compute + log only, never authoritative)
One adapter at a time: run the witness producer in shadow on live inputs,
record agreement/divergence vs the authoritative path, zero persistence,
zero control-flow influence — the G6-measure pattern applied per adapter.
Exit per adapter: a run-set report with counts only.

### E2 — Graduation gate (per adapter, owner decision)
Bars (predeclared before the pilot, never tuned retrospectively):
accuracy/coverage/latency/cost vs the §2 native baseline; encoder fidelity
for solver families; trust-boundary binding + generic consumption + Test N
for analyzers. Pass → E3 candidacy. Fail → adapter stays out, with the
negative result recorded (a failed pilot is evidence, not waste).

### E3 — Advisory consumption (never authoritative)
Graduated witnesses feed retrieval prioritization and diagnostic context
only. They cannot satisfy a declaration-proof requirement, gate a verdict,
or enter a certificate as a premise. Any proposal to promote a witness
beyond advisory restarts at E2 with new bars — never an in-place upgrade.

## 5. Acceptance of this plan

Owner marks: (a) accept plan direction (E0 may be scheduled once §2
entry criteria are met); (b) amend; (c) keep deferred without a plan on
file. Implementation authorization, if ever granted, is per adapter at E2
and names the adapter, the bars, and the reviewer explicitly.
