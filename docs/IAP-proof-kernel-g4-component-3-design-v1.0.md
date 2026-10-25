# Design: Proof-Kernel G4 Component 3 — Composition Properties + Mutation Sensitivity v1.0

> Status: supplement for owner review; this is a code-level design for Component 3 authorization. It does not authorize implementation. Component 3 implementation requires separate explicit owner authorization after this supplement is reviewed. It does not close G4, authorize production wiring, or substitute for Component 4.
> Scope: freeze composition-level property vectors and a pre-mapped mutation set over the pure `composition.py` / `publication.py` decision path. No new premise types, no registry changes, no certificate changes, no production behavior.

## Context

Components 1 and 2 froze evidence contracts, rule validators, independent checker semantics, and the `cert/v2` + `rules/v2` cutover with per-rule goldens. What remains at the composition layer is range confidence: the existing suites (`test_composition_invalidation.py`, 18 tests; `test_publication_decision.py`, 11 tests; `test_g5_composition_gate.py`, 7 tests) cover conjunction, alternatives (K5), transitive closure (K6), cycle rejection (K2), duplicate rejection (K14), evidence closure declared/undeclared classes, unknown-dependency rejection (F-C1), and the publication checklist (K8/K13/K15, policy denial). They do not prove the required cross-cutting properties (monotonicity, refutation sensitivity, scope non-escalation, deterministic replay, exhaustive invalidation closure, provenance propagation), and no mutation has ever demonstrated that a weakened invariant fails a named test.

The phase plan (Component 3) requires property-based or deterministic generated tests only where they add value, a mutation for each critical invariant family with pre-mapped detectors, and explicit G5 exclusions. This supplement specifies all three before any implementation run.

## References

- [`docs/IAP-proof-kernel-g4-phase-design-v1.1.md`](IAP-proof-kernel-g4-phase-design-v1.1.md) — parent phase plan (authoritative; v1.0 retained); Component 3 entry/exit criteria, global gate, Testing Philosophy.
- [`docs/IAP-proof-kernel-g4-component-2-design-v1.0.md`](IAP-proof-kernel-g4-component-2-design-v1.0.md) — rule contracts, D4a precedence, D5 dependency rows, D6 checker independence, D7 versioning.
- [`docs/IAP-proof-kernel-traceability-v1.0.md`](IAP-proof-kernel-traceability-v1.0.md) — current statuses; updated with Component 3 evidence after implementation.
- [`src/investigation_agent_platform/domain/proof/composition.py`](../../src/investigation_agent_platform/domain/proof/composition.py) — `make_node`, `RULE_DEPS`, `OPERATIONAL_CLASSES`, `detect_cycle`, `assert_acyclic`, `compose_conjunction`, `compose_alternatives`, `invalidate_derivations`, `invalidate_by_evidence`.
- [`src/investigation_agent_platform/domain/proof/publication.py`](../../src/investigation_agent_platform/domain/proof/publication.py) — `decide_publication`, `build_commit`.

## Goals / Non-Goals

**Goals:**

- Pin the six required properties with deterministic curated vectors plus one exhaustive bounded enumerator, each with named detector tests.
- Freeze a mutation set with one mutation per critical invariant family, each mapped to its expected failing detector test(s) before any mutation runs.
- Extend the global gate suite list (premises suite inclusion) and define the exact Component 3 and global commands.
- Keep every new test pure, deterministic, and replay-trivial; no randomness, no live services.

**Non-Goals:**

- New premise types, registry rows, rule validators, checker semantics, certificate schema, or canonicalization changes.
- Database concurrency, persistence, atomicity, RLS, writer audit, or any G5 claim.
- Production wiring, rollout, migrations, or authority changes.
- Hypothesis or any randomized property framework for the kernel suite.
- Population-accuracy or false-accept-rate claims from finite vectors.

## Decisions

### D1: Deterministic curated generators plus one exhaustive bounded enumerator; no randomized framework

**Decision:** All Component 3 properties use hand-written curated vectors with pinned expected outcomes, except invalidation closure, which additionally runs over an exhaustive enumerator of all labeled DAGs up to 4 nodes (1 + 3 + 25 + 543 = 572 graphs, edgeless included, fixed lexicographic order). No Hypothesis or randomized generation.

**Rationale:** The domain is closed and small (5 rules, fixed dep-class vocabulary); the required properties are enumerable by hand with exact attribution, matching the phase Testing Philosophy (one property per vector, pinned goldens). Mutation detectors must be stable across runs — a discovered random counterexample would still have to be frozen as a curated vector, so randomization adds a step without adding assurance. Strict closed parsing (`extra="forbid"`, `strict=True`) rejects most random draws at the boundary, making fuzzing low-signal per maintenance cost.

**Alternatives considered:** Hypothesis strategies for scope/graph inputs. Rejected: custom strategies are themselves design surface to review, failures need manual minimization-to-intent, and the strict boundary already pins rejection behavior with C2 negatives.

**Trade-offs:** Exhaustiveness is bounded (n ≤ 4 topologies; larger graphs covered by curated chains plus the transitive-closure argument). Unknown-unknown input shapes outside the closed vocabulary are rejected by construction, not explored.

### D2: Property inventory with named detectors

Each property below names its statement, generator, and detector tests to be created in `tests/unit/test_proof_kernel_g4_component3_properties.py` (test names normative; implementation must use exactly these names so the mutation report references resolve).

**P1 — Monotonicity (no strengthening without discharge), per rule.** Statement: no premise multiset outside a rule's exact contract discharges. The contract differs by rule, so the property is stated per rule and per path — producer and checker intentionally diverge on CALL_EDGE extras (below), and the property must expose that divergence, not paper over it.

- Exact-cardinality rules (`definition/v1`, `behavior/v1`, `absence/v1`, `history/v1`): producer and checker both enforce exact premise sets (producer length guards plus `_duplicate_evidence_id`; checker length guards plus `_duplicate_evidence_id`). Any superset — duplicate ID, wrong closed type, or second valid record of a satisfied role — rejects in both paths. Detectors: `test_monotonicity_extra_premise_rejects_<rule>` × 4, each asserting the documented per-path outcome (producer `INADMISSIBLE_PREMISE`, except definition's count guard which reports `MISSING_PREMISE`; checker `INADMISSIBLE_PREMISE`, except definition's `MISSING_PREMISE`).
- `call_edge/v1` (one-or-more contract): an additional *valid* `NORMALIZED_CALLSITE_FACT` binding the asserted caller/callee preserves discharge in both paths. Detectors: `test_monotonicity_extra_valid_callsite_preserves_discharge` (producer `DISCHARGED`, checker `VALID`).
- CALL_EDGE parity divergence (classified, not decided here): a wrong-type extra row is silently filtered by the producer (which may still discharge) but rejected with `INADMISSIBLE_PREMISE` by the checker; a duplicate evidence ID is unchecked by the producer but rejected with `INADMISSIBLE_PREMISE` by the checker. Detectors pin both behaviors exactly: `test_callsite_wrong_type_producer_tolerates_checker_rejects` and `test_callsite_duplicate_id_producer_tolerates_checker_rejects`, each asserting the producer outcome and the checker outcome separately. Open question: whether the producer should be tightened to match the checker. That is a Component 2 contract change requiring a supplement amendment and owner decision — explicitly out of Component 3 scope, recorded as a residual either way.

**P2 — Refutation sensitivity + removal recomputation.** Statement: removing a required premise role from a discharging set yields the documented typed non-discharge for that rule (producer `MISSING_PREMISE` when no candidate rows remain; producer `INVALID_INFERENCE` for call_edge leftovers; checker `INADMISSIBLE_PREMISE` on cardinality guards, `MISSING_PREMISE` on empty premise lists, `PREMISE_UNAVAILABLE` on withdrawn artifacts); recomputation over the reduced manifest yields new derivation IDs, never mutated ones. Generator: per rule, drop each required premise role in turn (5 rules × roles = 1 + 1 + 3 + 1 + 1 = 7 removal vectors) through both `apply_rule` and `certify`→`check_certificate`, plus a call_edge variant with a non-binding row left over. Detectors: `test_removal_<rule>_<role>_typed` × 7 asserting the per-rule outcome above, plus `test_removal_leftover_row_invalid_inference` (call_edge), plus `test_removal_recomputes_derivation_ids` asserting `derivation_id`/`manifest_digest` differ from the full-set derivation and no input mapping was mutated. Pass criterion: exact per-rule outcomes; digests differ; inputs immutable.

**P3 — Scope non-escalation.** Statement: widening or altering scope (revision bump, repository swap, added scope key, dropped scope key) on an otherwise discharging application never upgrades a rejection to discharge; at composition, a scope-class change invalidates dependent derivations via `RULE_DEPS`. Generator: per rule, one scope mutation each (revision bump; repository swap where the rule carries `scope.repository`; extra unknown scope key → canonical rejection; dropped scope key → missing). Detectors: `test_scope_change_never_discharges_<rule>` × 5 (producer level) plus `test_scope_revision_change_invalidates_dependents` and `test_scope_repository_change_invalidates_behavior_only` (composition level: definition/call_edge nodes sharing the evidence survive a repository-class change; behavior/absence/history nodes invalidate). Pass criterion: no `DISCHARGED`/`VALID`; invalidation sets exactly as specified.

**P4 — Deterministic replay.** Statement: identical inputs yield identical `proposition_id`, `context_id`, `manifest_digest`, and `derivation_id` across repeated runs; composition invalidation is invariant under node/edge mapping insertion order; publication decisions are invariant under request-mapping key order. Generator: rerun goldens (all 5 rules) plus insertion-order permutations of a fixed 6-node graph (8 fixed permutations covering reversed, rotated, and interleaved insertion) plus `PublicationRequest` mappings constructed with rotated key insertion orders. Detectors: `test_replay_digests_stable_<rule>` × 5, `test_invalidation_insertion_order_invariant` (same node/edge mappings, different insertion orders → identical invalidated sets), `test_decision_request_key_order_invariant` (same `PublicationRequest` fields, different mapping key insertion orders → identical decision and reason; `decide_publication` takes a single request, so this pins key-order independence of parsing, not graph traversal). Pass criterion: byte-identical digests, identical invalidated sets, identical decisions/reasons.

**P5 — Invalidation closure, exhaustive to 4 nodes.** Statement: for every labeled DAG (n ≤ 4, 572 graphs), `invalidate_by_evidence` and `invalidate_derivations` return exactly the transitive dependents of directly-hit nodes — no more, no fewer. Frozen enumerator conventions (normative for implementation and audit):

- Node labels `d0..dn-1`; rules assigned round-robin in fixed order `definition/v1`, `call_edge/v1`, `behavior/v1`, `absence/v1`, `history/v1` (node `di` gets rule `i mod 5`).
- Edge storage follows the code: node `depends_on` lists its dependencies (edge stored as dependent → dependency, e.g. `d2.depends_on == ["d1"]` means d2 depends on d1). Invalidation flows the reverse direction: from a changed node to its transitive dependents. Graphs enumerated in fixed lexicographic edge order; graph index assigned sequentially starting at 0.
- Premise IDs: node `di` owns premises `ev-t3-<i>-<j>` for `j` in `0..(i mod 3)` (1–3 premises per node, deterministic).
- Changed evidence per scenario: (a) all premises of root nodes (nodes with empty `depends_on`); (b)/(c) same evidence set with non-proof classes; (d) all premises of all nodes. Changed classes: (a) `{"evidence.content"}`; (b) `{"budget"}` (operational); (c) `{"telemetry.retention"}` (undeclared); (d) `{"scope.repository"}` only, so rule-declared dep filtering alone decides the direct set.
- Independent oracle, two explicit stages (coded separately in the test, never calling the implementation): stage 1 computes directly-hit nodes as `{nid : premises(nid) ∩ changed_evidence ≠ ∅ AND changed_classes ∩ RULE_DEPS[rule(nid)] ≠ ∅}`; stage 2 computes transitive dependents by BFS over reverse `depends_on` edges from the stage-1 set. The oracle covers the full `invalidate_by_evidence` contract (membership + dep filtering + closure), not closure alone.

Hand-worked example (3 nodes: d0 definition, d1 behavior with `depends_on == ["d0"]`, d2 history with `depends_on == ["d1"]`; premises `ev-t3-0-0`, `ev-t3-1-0`, `ev-t3-2-0`): (a) change `{ev-t3-0-0}` × `{evidence.content}` → direct `{d0}` (only d0 holds the premise; all rules declare the class) → closure `{d0, d1, d2}`; (b)/(c) same evidence × `{budget}` / `{telemetry.retention}` → `{}`; (d) change all three premises × `{scope.repository}` → direct `{d1, d2}` → closure `{d1, d2}`. Dep-row basis, verifiable in `composition.py::RULE_DEPS` without re-deriving the design: the `behavior/v1` and `history/v1` rows list `scope.repository`; the `definition/v1` and `call_edge/v1` rows do not. A drift-pin detector, `test_exhaustive_oracle_assumes_declared_deps`, asserts exactly this membership, so any future dep-row change fails loudly instead of silently invalidating the example and the (d) scenario expectations.

Detectors: `test_exhaustive_closure_content`, `test_exhaustive_closure_operational_empty`, `test_exhaustive_closure_undeclared_empty`, `test_exhaustive_closure_repository_filtered`, `test_exhaustive_oracle_assumes_declared_deps`. Failure attribution: graph index + edge list + scenario. Pass criterion: implementation set == oracle set on all 572 × 4 cases.

**P6 — Provenance propagation (non-independence).** Statement: provenance-bearing input changes propagate through evidence closure to all transitive dependents; provenance failure is never contained at the changed node. Generator: (a) mapping-premise byte change on a behavior derivation with a composed dependent → both invalidate via `evidence.content`; (b) exclusions-list addition covering an absence scope → absence node invalidates via `coverage.exclusions` class; (c) unapproved mapping authority is a checker input, not a dep class: withdrawing approval turns a `VALID` behavior check into `UNSUPPORTED_CONSTRUCT` on recheck (no graph change needed). Detectors: `test_mapping_change_propagates_to_dependents`, `test_exclusion_addition_invalidates_absence`, `test_authority_withdrawal_rechecks_unsupported`. Pass criterion: exact invalidated sets / checker outcomes.

### D3: Mutation set, pre-mapped (not yet run)

Each mutation states the exact edit anchor (file, current behavior), the edit, and the expected failing detector test name(s) from D2 or the existing suites. Mutations never touch the developer's live worktree — which carries uncommitted Component 2 changes that a revert could mis-restore — and are never committed. Per-mutation procedure (normative; the implementer follows it exactly, with the report fields filled per run):

1. Record baseline: live `git status --porcelain` output and the sha256 of the 6 fingerprinted modules. Define the run journal destination: a transient, uncommitted location selected by the operator at run time, outside every scratch dir and outside version control. The supplement deliberately does not name it — committed artifacts must not name transient destinations. Final tallies are transcribed to traceability per D6; only reviewable results enter durable documentation.
2. Create the scratch dir with `SCRATCH=$(mktemp -d "${TMPDIR:-/tmp}/iap-mut-<M#>.XXXXXX")` (atomic creation; aborts on collision, parallel-run safe). Fixed paths are forbidden — never reuse a directory name across runs.
3. Install the trap (`EXIT`, `INT`, `TERM`) immediately after `mktemp` succeeds, before any copying or mutation, so setup-phase failures and interruptions are covered, not just the test run. The trap tolerates partial state via a `PHASE` variable (`init` → `copy` → `verify` → `mutate` → `run` → `done`). On every exit path the trap must: (a) compare live `git status --porcelain` to the recorded baseline; (b) remove the scratch dir if it exists and confirm its absence; (c) append one report line to the journal (`mutation`, `phase reached`, `expected detector`, `actual outcome or setup-failure reason`, `live-tree-unchanged yes/no`, `scratch-removed yes/no`). A copy failure, hash mismatch, or interrupt during setup is recorded with its phase — never silent, never skipping verification.
4. `PHASE=copy`: copy `pyproject.toml`, `src/`, `tests/`, `scripts/` into the scratch dir (virtualenv, version-control metadata, project scratch, and documentation directories excluded). `PHASE=verify`: confirm the copy's fingerprinted module hashes equal the live baseline; abort the run on mismatch (the trap records the `verify` failure). `PHASE=mutate`: apply the mutation only in the scratch copy.
5. `PHASE=run`: run the Component 3 standalone command in the scratch copy via `<live-repo>/.venv/bin/python -m pytest` with cwd set to the copy (pytest `pythonpath=["src"]` plus rootdir discovery resolve imports to the copy's sources, shadowing the editable install). Capture the pytest exit code separately: an expected detector firing (test failure) is the mutation-run *success* signal, not a procedure failure — the trap's exit status reflects procedure integrity only (live tree unchanged, scratch removed), never the test outcome. Detector-vs-expected comparison is done by the author against the D3 mapping and recorded in the report. `PHASE=done` on clean completion.
6. A trap firing with `live-tree-unchanged != yes` or `scratch-removed != yes` is a procedure failure: stop the protocol, investigate, and do not proceed to the next mutation until the worktree state is explained and owner-accepted.

Post-run verification is checksum equality in the scratch copy plus an untouched live tree — a fingerprint `--check` alone cannot restore a dirty worktree, so it is necessary but not sufficient. The trap is installed before any filesystem mutation in the procedure, so verification is not skipped on setup failures, interruptions, or expected detector failures alike.

| ID | Target invariant | Edit (file + change) | Expected detector(s) |
|---|---|---|---|
| M1 | Absence exact-one-coverage | `rules.py::validate_absence_premises`: delete the `len(covers) > 1 or len(rows) != 1` guard (accept any non-empty row set) | `test_absence_multi_coverage_rejects` (C3 detector: two coverage premises → `INADMISSIBLE_PREMISE`); existing `test_absence_empty_premises_is_missing` must still pass |
| M2 | Scope/revision equality | `checker_semantics.py::_revision_scope_check`: replace `!=` with `==` in the revision comparison (accept only mismatches) | `test_scope_swap_rejected` (now `SCOPE_MISMATCH`), K11 vectors, `test_scope_change_never_discharges_*` |
| M3 | Semantic validation load-bearing | `rules.py::validate_behavior_premises`: return `(DISCHARGED, "neutered")` unconditionally | All behavior invalid goldens (`test_behavior_*_mismatch/invalid`), `test_checker_rejects_behavior_predicate_mismatch`, `test_checker_independent_of_monkeypatched_producer` |
| M4 | Dependency edge completeness | `composition.py::RULE_DEPS`: remove `"scope.repository"` from `behavior/v1` | `test_scope_repository_change_invalidates_behavior_only`, `test_composition_accepts_new_rule_nodes_and_deps` (pins the dep) |
| M5 | Complete-empty admission | `rules.py::validate_absence_premises`: accept `search_status == "INCOMPLETE"` as discharging | `test_absence_incomplete_search_unsupported`, `test_absence_failed_search_unsupported` |
| M6 | Freshness pin load-bearing | `certificate.py::check_certificate`: ignore `expected_context_id` (treat as `None`) | `test_k8_k15_stale_context_rejected`, `test_checker_stale_context_rejected_for_behavior_certificate`, decision-level `test_stale_context_denies_k8_k15` |

**Survivor protocol:** if a mutation passes the full gate, implementation stops and the author must either (a) add the missing detector (named, pinned, with rationale) and re-run the mutation to a fail, or (b) record owner-accepted justification that the mutated behavior is contract-equivalent. Silence is not an option; an undetected mutation blocks Component 3 exit.

### D4: Dependency-class mutation discipline + over-invalidation guard

Mutations M4-style (dep edits) must also assert the over-invalidation guard still holds: after reverting, operational-only and undeclared-class change scenarios across the 572-graph enumerator still yield empty invalidation sets (P5 detectors double as the guard). No mutation may broaden invalidation; broadening is a failure of the mutation run, investigated as strictly as narrowing.

### D5: Suite list revision + exact commands

The phase global suite list is revised by this supplement to include the premises suite (outstanding recommendation from the Component 2 gate): the G4 suite is the 13 phase-listed files plus `tests/unit/test_proof_kernel_premises.py`. `test_proof_kernel_g4_component4_integration.py` is parent-required inventory with no supplement or test file yet — the global command below is blocked until Component 4 exists, and no partial-file variant of it represents the G4 gate.

Component 3 standalone command (repo root):

```text
uv run python scripts/proof_kernel_validator_fingerprints.py --check && uv run python -m pytest -q -p no:cacheprovider tests/unit/test_composition_invalidation.py tests/unit/test_publication_decision.py tests/unit/test_g5_composition_gate.py tests/unit/test_proof_kernel_g4_component3_properties.py
```

Global gate command (repo root): the parent phase inventory is preserved intact, including `tests/unit/test_proof_kernel_g4_component4_integration.py`, which does not exist yet — the global command is BLOCKED until Component 4's supplement and test file exist, and no partial-file variant of it may be presented as the G4 gate:

```text
uv run python scripts/proof_kernel_validator_fingerprints.py --check && uv run python -m pytest -q -p no:cacheprovider tests/unit/test_canon_identity.py tests/unit/test_manifest_evaluation.py tests/unit/test_typed_rules.py tests/unit/test_certificate_checker.py tests/unit/test_composition_invalidation.py tests/unit/test_publication_decision.py tests/unit/test_g5_composition_gate.py tests/unit/test_proof_kernel_fixture_integrity.py tests/unit/test_proof_kernel_validator_fingerprints.py tests/unit/test_proof_kernel_g4_component1_evidence.py tests/unit/test_proof_kernel_g4_component2_rules.py tests/unit/test_proof_kernel_g4_component3_properties.py tests/unit/test_proof_kernel_g4_component4_integration.py tests/unit/test_proof_kernel_premises.py
```

Skips, xfails, and the unrelated revision-pinned baseline failure are reported separately per the phase reporting rule and never counted as passes.

### D6: Report format

Results are recorded in two durable places: (1) the detector-mapping table in the `test_proof_kernel_g4_component3_properties.py` module docstring (mutation → edit anchor → expected detector → actual result → verdict), updated at implementation time; (2) a Component 3 section in `docs/IAP-proof-kernel-traceability-v1.0.md` with the mutation table outcomes, enumerator counts (572 × 4), and residuals. Raw per-run journal lines live only at the operator-selected transient destination from D3 and are transcribed, never referenced, into the durable record. A passing mutation run must never be mistaken for a passing implementation suite: the report distinguishes "mutation detected" from "implementation green."

### D7: No source changes, no G5, no production surface

Component 3 implementation adds exactly one test file plus docstring/ledger/traceability updates. No `src/` changes are expected; any `src/` change forced by a survivor is a contract change requiring supplement amendment and owner re-approval before landing. Mutations run only in disposable scratch copies per the D3 procedure and are deleted same-session; the live worktree is never mutated. No migrations, no persistence tests, no live services, no production wiring, no authority changes. The application `rules/v1` default remains untouched G5 territory.

## Interfaces

No new interfaces. The executable surface is pure test code calling existing pure domain functions (`make_node`, `assert_acyclic`, `compose_conjunction`, `compose_alternatives`, `invalidate_derivations`, `invalidate_by_evidence`, `decide_publication`, `build_commit`, rule validators, `certify`, `check_certificate`). The new test module exposes no imports for other suites.

## File list

- New (implementation phase, not this supplement): `tests/unit/test_proof_kernel_g4_component3_properties.py` — P1–P6 vectors, 572-graph enumerator, detector-mapping table in module docstring.
- Touched (implementation phase): `docs/IAP-proof-kernel-traceability-v1.0.md` (Component 3 section + requirement-to-test index counts), `docs/IAP_Arch_Eng_Recommendations.log` (mutation report ledger entries).
- This supplement only: `docs/IAP-proof-kernel-g4-component-3-design-v1.0.md` (this file).
- Explicitly not touched: `src/` (unless a survivor forces an amended contract change), fixtures, migrations, application code.

## Data Storage / Migrations

None. No persistent model, table, migration, or external contract. Test fixtures, if any small ones are needed for graph shapes, are constructed inline in code (node IDs `d0…`, premise IDs `ev-t3-*`) so the enumerator is self-describing; no new fixture files.

## Entry criteria

1. This supplement is reviewed; owner explicitly authorizes Component 3 implementation (including the enumerator bound n ≤ 4 and the six-mutation set).
2. Component 2 is formally accepted (owner disposition recorded). Component 3 implementation begins only after that acceptance, per the parent phase plan's Component 3 entry criteria. An earlier start requires amending the parent phase plan and explicit owner approval of the changed gate — this supplement cannot relax that prerequisite on its own.
3. Mutation-to-detector mapping above is accepted as the pre-run contract; detectors are named tests that do not yet exist (except where existing tests are cited).

## Exit criteria

1. All P1–P6 detectors pass; enumerator completes 572 × 4 cases with zero mismatches (exact count reported).
2. Each of M1–M6 is applied singly in a scratch copy per the D3 procedure, detected by its mapped test(s), recorded, and the copy deleted; the live working tree is never modified (verified by identical `git status --porcelain` before/after plus scratch checksum equality), and live fingerprint `--check` stays clean since no live `src/` bytes change.
3. Survivors, if any, resolved via the survivor protocol with owner ruling recorded.
4. Traceability Component 3 section + index counts + ledger updated; global gate green except the pre-existing baseline pins (reported separately).
5. No `src/`, fixture, migration, or application change in the Component 3 diff (or an amended supplement if a survivor forced one).

## Testing Philosophy alignment

One property per vector; pinned golden outcomes; differential agreement is necessary but never sufficient (each expected outcome pinned independently); mutation run ≠ implementation suite (D6 report separation); finite-suite limits stated, not laundered into population claims.

## Documentation Plan

After implementation: traceability Component 3 section (mutation table outcomes, enumerator counts, residuals), ledger entries per mutation, requirement-to-test index counts for the new file. No changes to `docs/archive/*`. If a survivor forces a contract change, this supplement is amended (versioned) before the code lands.

## Risks / Trade-offs

- **Enumerator cost:** 572 graphs × 4 scenarios ≈ 2.3k pure-function evaluations, each microseconds — negligible. Bound is documented; larger graphs add no new edge shapes (transitivity is already covered inductively by chains at n ≤ 4 plus curated 6-node orders in P4).
- **Mutation discipline:** temporary edits to `src/` risk residue. Mitigated by single-mutation-at-a-time, immediate revert, `git status` + fingerprint check after each run, and never committing mid-protocol.
- **False confidence:** passing mutations prove detectors fire, not that invariants hold universally. The supplement claims detector sensitivity only, consistent with the phase plan.
