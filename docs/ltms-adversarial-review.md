# LTMS Adversarial Review

**Review date:** 2026-10-25  
**Target revision:** `738161f2bdc2a938d97c40f974f51766f45d4c0c`  
**Method:** source and tests inspected read-only; no production code changed. The full suite was subsequently run at this revision with `uv run --extra dev --extra sat pytest -ra -o addopts=''` under CPython 3.13.15: **169 passed in 1.17s**. The specific finding repro scenarios below were not added or separately run; their stated confidence is based on source-path tracing unless otherwise noted.

## 1. Executive assessment

The code has several good, correctness-oriented choices: explicit `FAIL` sentinel, occurs-check, incremental clause counters, deferred contradiction dispatch, two-phase retraction, per-instance state, and property/differential tests. The strongest review concerns are at semantic seams, not the isolated BCP hot path: temporary assumption cleanup must include rule activations; LTRE assumption ownership differs for simple and compound propositions; and DSL loading has no strict malformed-input or transaction contract.

No claim is made here that the algorithms are formally proven. The full test suite passed, but targeted regression tests for the findings were not added or separately executed. Candidate issues whose status depends on intended semantics are listed separately as observations, not defects.

## 2. Findings

### FIND-LTMS-001 — Indirect proof may execute stale activations from the refuted branch

- **Severity:** Medium
- **Confidence:** High that the code path exists; minimal case not separately exercised by a targeted test.
- **Package/source:** `src/ltms/indirect.py::try_indirect_proof`, `src/ltms/core.py::LTMS.set_truth`, `src/ltms/ltre.py::LTRE.run_rules`; contrast `src/ltms/dds.py::_dds`.
- **Expected semantics:** Once a temporary opposite assumption has been retracted as part of failed-branch recovery, work whose only trigger was that assumption must not be executed as though its premise still held, unless rule activations are expressly defined as durable events.
- **Actual behavior:** LTMS label changes enqueue parked true/false rule activations via `set_truth` (`core.py:334–355`). Indirect proof enables the temporary assumption and then calls `ltre.run_rules()` (`indirect.py:40–44`). When its contradiction handler finds a contradiction dependent on the assumption, it retracts the node and installs a nogood (`indirect.py:28–37`), but does not clear or restore `ltre.queue`. The subsequent `run_rules()` loop can therefore drain activations queued by the failed temporary premise. The analogous DDS failed branch explicitly calls `ltre.queue.clear()` (`dds.py:61–72`).
- **Minimal counterexample:** Assert `(p∨q)`, `(¬p∨r)`, and `(¬q∨r)`, so `r` is entailed by cases but remains UNKNOWN under base BCP. Register a `Trigger.FALSE` rule on `r` whose callback asserts an unrelated marker. Call `try_indirect_proof(r)`: the temporary `r=FALSE` assumption forces `p=FALSE` and `q=FALSE`, violating `(p∨q)`; the handler retracts the temporary assumption and the nogood derives `r=TRUE`, but the queued FALSE-trigger callback remains and `run_rules()` executes it while `r` is TRUE.
- **Why:** LTMS truth-maintenance tracks clauses/support, while LTRE callbacks are a separate queue with no activation support/cancellation identity. Indirect proof handles the LTMS assumption but not the rule queue.
- **Consequence:** State may contain permanent assertions derived by execution under a premise that no longer holds. Later reasoning/explanations can present this as unrelated asserted state.
- **Recommended correction:** Define activation semantics first. A minimal safe policy is to snapshot queue state at branch entry and discard only activations introduced by that branch on failed refutation, then verify behavior when nested rules enqueue more work. Better long-term design attaches activation provenance/branch scope or makes callback effects retractable when they are intended as logical consequences. Do not copy `queue.clear()` blindly: it may discard unrelated pre-existing queued work.
- **Regression test:** Build the minimal model above; assert that no callback whose only trigger is the refuted assumption executes, that unrelated pre-existing queue items follow a documented policy, and that successful indirect proof still executes appropriate callbacks.
- **Already documented?:** Not found in docs/PLAN during this review; DDS cleanup makes the missing counterpart notable.

### FIND-LTMS-002 — Compound assumptions omit informant ownership conflict validation

- **Severity:** Medium
- **Confidence:** High source-path confidence; minimal case not separately exercised by a targeted test.
- **Package/source:** `src/ltms/ltre.py::LTRE.assume` (`143–171`), `LTRE.retract` (`173–179`).
- **Expected semantics:** A datum already assumed by informant A should either reject informant B or implement explicitly shared ownership. Simple and compound assumptions should follow the same rule.
- **Actual behavior:** The simple branch raises when `datum.assumption` is non-None and differs from `informant` (`150–155`). The compound branch only performs setup when `datum.assumption is None`; for an existing owner, it falls through and returns the datum without checking that owner (`161–171`). Retraction only succeeds when the supplied informant equals the stored one (`177–179`).
- **Minimal counterexample:** `e.assume(("implies", ("a",), ("b",)), "owner-1"); e.assume(("implies", ("a",), ("b",)), "owner-2")`. The second assume silently returns while the stored owner remains `owner-1`; `e.retract(("implies", ("a",), ("b",)), "owner-2")` then does nothing. The setup of the compound assumption does not depend on whether its formula is currently satisfied.
- **Why:** Compound guard handling lacks the `elif datum.assumption != informant: raise` branch used for simple facts.
- **Consequence:** Callers can believe they established/removed an assumption when they did not; ownership-sensitive contexts can leak assumptions.
- **Recommended correction:** Enforce the same owner policy for both paths, or define multi-owner semantics with explicit reference tracking. Do not silently accept a no-op ownership claim.
- **Regression test:** Parameterize simple and compound formulas across same-owner re-assumption and different-owner re-assumption; assert consistent outcomes and verify that only the actual owner can retract.
- **Already documented?:** No matching limitation or test found.

- **Documentation discrepancy:** `STUDY-NOTES.md:213–217` states that a different informant errors for compound assumptions; this conflicts with the implementation branch at `src/ltms/ltre.py:157–171`. This makes the defect more than an undocumented edge case: narrative docs promise a guard that the implementation does not perform.

### FIND-LTMS-003 — KB DSL has permissive directive parsing and unspecified partial-mutation behavior

- **Severity:** Medium
- **Confidence:** High source-path confidence; targeted cases not separately tested.
- **Package/source:** `src/ltms/dsl.py::load_kb` (`197–254`), especially `line.partition(" ")` (`205`), `complete` (`230–234`), and sequential execution loop (`201–253`).
- **Expected semantics:** A malformed directive should be rejected, not interpreted as a different proposition; extra arguments should be rejected; loader mutation behavior should be explicitly specified (incremental or transactional).
- **Actual behavior:** `line.partition(" ")` splits only on a literal space. A line such as `query\train` does not enter the query branch and falls through to a bare proposition assertion. The `complete` branch never inspects `rest`, so `complete unexpected` silently runs completion. `load_kb` mutates the engine line by line and does not roll back if a later parse, contradiction or `expect` error occurs; whether this is a defect is unresolved because the loader's atomicity contract is undocumented.
- **Minimal counterexamples:** A line containing `query<TAB>rain` is treated as an assertion rather than a query; `load_kb("complete unexpected")` completes; with an existing engine, `load_kb("rain\nexpect rain false", engine)` raises but leaves `rain` asserted.
- **Why:** Statement dispatch tokenization and validation are weaker than expression parsing; loading is an interpreter loop over mutable state.
- **Consequence:** Typos can silently alter model semantics. Callers may also reuse a partially changed engine after an exception; this is dangerous unless incremental mutation-on-error is an explicit contract.
- **Recommended correction:** Parse directive tokens with a whitespace-aware grammar and reject trailing directive text. Choose and document atomic-load versus incremental-load semantics. If atomicity is chosen, parse/validate all statements first and apply via a transaction/snapshot strategy with exception-safe rollback. Note that individual engine formula installation also mutates before final propagation, so statement-level validation alone does not guarantee transactionality.
- **Regression test:** Cover tab/multiple-whitespace directive separators, extra `complete` tokens, malformed inputs after an earlier mutation, and error during formula/expect processing. Assert exact engine state after failure under the chosen contract.
- **Already documented?:** `dsl.py` documents statements but no strictness/atomicity guarantee; current tests cover malformed expressions/expect missing status but not these cases (`tests/test_dsl.py:30–57,131–146`).

## 3. Observations and unresolved contracts (not defects absent an intent decision)

### OBS-LTMS-001 — LTRE/JTRE activations are one-shot per parked match

`LTRE` clears `node.true_rules`/`false_rules` when `LTMS.set_truth` fires them (`core.py:334–355`); JTRE similarly clears parked IN/OUT lists (`jtms.py:243–262`). Neither path re-registers the activation when the node later changes state. Existing tests assert initial activation only (`tests/test_ltre.py:31–50`, `tests/test_jtre.py:14–25`). If rules are transition listeners, this is a lifecycle defect; if rule matching creates one-shot activations, behavior may be intentional. Decide and test before adoption.

### OBS-LTMS-002 — CWA ownership collision is possible if LTRE owner state is stale

`close_predicate` records any UNKNOWN datum it attempts to assume, and the context manager retracts every recorded form (`cwa.py:22–46`). LTRE accepts a repeated same-informant simple assumption without distinguishing whether this scope created it (`ltre.py:143–155`). Under ordinary API use, an already-owned assumption is normally known and therefore skipped by CWA; the risky case requires a datum owner field to remain set while its LTMS node has become UNKNOWN (for example, after internal node-only retraction). The source supports a conditional ownership hazard, but this session did not construct or execute the required state. Existing context test covers only a newly created closure (`tests/test_advanced.py:55–60`). Treat as a test gap/hypothesis until a minimal reachable path is demonstrated; document CWA ownership preconditions meanwhile.

### OBS-LTMS-003 — `substitute` trusts externally supplied bindings are acyclic

Bindings generated by `unify` are guarded by the occurs-check, but public `substitute(term, bindings)` recursively follows arbitrary maps (`unify.py:91–103`). A caller-created self-cycle such as `{X: X}` can recurse indefinitely. This may be a documented precondition; add validation or document it if untrusted maps are in scope.

### OBS-LTMS-004 — Rule callbacks are not truth-maintained conclusions

LTRE executes ordinary Python callbacks from a separate activation queue (`ltre.py:287–294`). If a callback asserts a conclusion, that assertion is permanent unless it is itself represented as a retractable assumption or guarded formula. Rule-trigger withdrawal does not automatically retract callback output. This is an architectural semantic boundary; users must not assume LTRE is a RETE/TMS rule-support unification without explicit modeling.

### OBS-LTMS-005 — DDS choice-set cardinality is caller-supplied

The search chooses in order but does not install mutual-exclusion or exactly-one clauses (`dds.py:44–58`). The coloring example supplies inter-vertex conflicts and extracts the first TRUE color; it does not define a generic cardinality contract for arbitrary choice sets (`tests/test_advanced.py:66–101`). Clarify whether choice sets represent ordered candidates or logical exclusive domains.

### OBS-LTMS-006 — Optional algorithm validation has scope gaps

The watched-engine Hypothesis differential covers clause additions and forward assumptions but not randomized retraction/handler histories (`tests/test_watched.py:84–129`). SAT tests establish that reported conflicts/forced labels are sound, not that every consequence/conflict is found (`test_differential_sat.py:1–9,42–75`). CLTMS tests cover small examples but no SAT-oracle completion check was found (`test_cltms.py`). These are test coverage gaps, not demonstrated defects.

### OBS-LTMS-007 — Documentation status and test-count drift

README feature table (`README.md:33–46`) lists watched propagation and completeness as implemented, while its later future-work section (`README.md:94–99`) calls them future work. `docs/index.md:62–64`, `BRIEFING.md:4`, and `CHANGES.md:85` report different test counts. Treat source and CI config as implementation/configuration evidence; do not reuse these stale counts or roadmap statements as current status without revalidation.

## 4. Areas reviewed without a confirmed defect

- **Empty unification success:** `{}` is distinct from `FAIL`; callers test identity. `unify.py:43–65`; tests in `test_unify.py`.
- **Occurs-check and chained substitution:** `_occurs` recursively traverses terms/bindings and substitution resolves chains. `unify.py:68–103`; tests cover generated acyclic chains and cycles rejected by unify.
- **Clause counters and BCP incompleteness:** counter definitions and updates align in inspected code; tests explicitly pin known incompleteness (`core.py:295–376`; `test_core_bcp.py:52–72`; `test_core_properties.py`). This is not a proof over all operation sequences.
- **Two-phase retraction and alternate support:** phase separation is explicit and representative alternative/circular support tests exist (`core.py:462–498`; `test_core_assumptions.py:19–48`).
- **Tautology/duplicate clause simplification:** duplicate literals collapse and a complementary pair drops the clause (`core.py:201–219`; `test_core_bcp.py:75–88`).
- **Handler stack cleanup:** context manager removes a handler in `finally` (`core.py:400–409`). Advanced facility state cleanup remains more complex and is captured above.

## 5. Reproducibility and validation record

### Environment and commands

- Target revision inspected: `738161f2bdc2a938d97c40f974f51766f45d4c0c`.
- Target working tree was clean; no target files modified.
- Project declares Python `>=3.10`; an initial attempt under system Python 3.9.6 failed because pytest was unavailable and `typing.TypeGuard` is not provided by that runtime.
- After the owner noted `uv` availability, ran `uv run --extra dev --extra sat pytest -ra -o addopts=''`: **169 passed in 1.17s**, CPython 3.13.15. The first `uv run --extra dev --extra sat pytest -ra` passed but emitted no count under configured `-q`, so it was repeated with the project addopts disabled.
- `uv` generated an untracked `uv.lock`; it was removed. The target repository working tree was clean afterward. No source/test files in the target were modified.
- The full pass confirms the checked-in test suite, not the specific new counterexamples. Add and run focused regression cases before treating those effects as experimentally verified.

## 6. Relationship to the companion documents

- [Concepts, capabilities, and specifications](ltms-concepts-capabilities-and-specifications.md) documents architecture, theory, entity model, algorithm boundaries, tests, and IAP fit.
- [Derived behavioral specification](ltms-derived-behavioral-specification.md) states proposed contracts and marks observed, intended, proposed, and unresolved semantics.
- Findings here are implementation-review conclusions against those contracts; no defect is silently converted into intended behavior.
