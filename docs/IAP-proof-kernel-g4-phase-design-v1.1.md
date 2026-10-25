# Design: Proof-Kernel G4 Closure Phase v1.1

> Revision v1.1 (2026-10-26): sole change from v1.0 is Global G4 exit criterion 6 — the gate suite is now 14 files, adding `tests/unit/test_proof_kernel_premises.py` (C3 §D5's premises-suite addition, recorded under owner authority 2026-10-26; the standalone C4 gate and the global G4 gate are the same ruled command). No other text changed. v1.0 retained as the reviewed artifact; v1.1 is the authoritative text for the gate command.

> Status: phase-gating plan for owner review; this is not an implementer-ready code design. Before implementation authorization for any component, publish and review that component's code-level design supplement. This plan does not itself authorize implementation. Passing G4 permits consideration of a later authority decision; it does not wire the kernel into production or close G5.
> Scope: close the G4 kernel assurance gap across the four golden layers—evidence, rules, composition, and end-to-end checked decision—while keeping the proof kernel non-authoritative.

## Context

The proof kernel currently exists as a set of pure domain modules under `src/investigation_agent_platform/domain/proof/`. The current closed registry supports `definition/v1` and `call_edge/v1`; other claim kinds abstain. Certificate construction and checking cover identity recomputation, premise hashes and types, scope, coverage, cycles, and freshness for the implemented subset. Composition and publication-decision modules have scoped tests, but no authoritative production publication path consumes the kernel.

The G4 traceability matrix identifies remaining closure work: executable BEHAVIOR, ABSENCE, and HISTORY rule coverage; a diverse checker validation boundary; stronger property and mutation evidence; and owner sign-off on the explicit A–T mapping. Current K/A–T statuses and known limits remain governed by `docs/IAP-proof-kernel-traceability-v1.0.md`. G5 remains responsible for persisted composition, live-database interleavings, and atomic publication commit.

This design interprets “four components” as the four golden layers named in `docs/IAP-proof-kernel-verification-v1.0.md`: evidence-level, rule-level, composition-level, and end-to-end publication. The fourth component is an in-memory certificate-to-decision integration boundary for G4; it does not claim persisted or atomic publication.

## References

- [`docs/IAP-proof-kernel-design-v1.0.md`](IAP-proof-kernel-design-v1.0.md) — defines the proposition/context model, trust boundaries, invariants, and target architecture.
- [`docs/IAP-proof-kernel-contracts-v1.0.md`](IAP-proof-kernel-contracts-v1.0.md) — normative certificate, rule, composition, coverage, and publication contracts.
- [`docs/IAP-proof-kernel-verification-v1.0.md`](IAP-proof-kernel-verification-v1.0.md) — defines the four golden layers, K1–K16, A–T, and property/mutation expectations.
- [`docs/IAP-proof-kernel-traceability-v1.0.md`](IAP-proof-kernel-traceability-v1.0.md) — current status and exact evidence inventory; G4 remains PARTIAL.
- [`docs/IAP-proof-kernel-forward-plan-v1.0.md`](IAP-proof-kernel-forward-plan-v1.0.md) — dependency ordering and requirement for per-step owner authorization.
- [`docs/IAP-proof-kernel-publication-commit-design-v1.0.md`](IAP-proof-kernel-publication-commit-design-v1.0.md) — records the separate persistence/commit design and its residuals; it does not make G4 authoritative.
- `docs/dev/architecture.md` does not exist. `docs/archive/dev/architecture.md` is archived system context and is not changed by this pure-kernel, non-authoritative phase.

## Goals / Non-Goals

**Goals:**

- Specify a gated closure path for all four golden layers, with component-level entry and exit criteria and an explicit code-level supplement requirement.
- Add only proof rules whose evidence contracts can be checked deterministically and whose unsupported cases fail closed.
- Make checker independence meaningful by ensuring it does not call the producer's rule validators or trust producer conclusions.
- Require property and mutation evidence that demonstrates the suite detects weakened invariants, not just that it passes current code.
- Exercise the pure certificate → independent checker → composition/decision path without adding a production consumer or claiming database atomicity.
- Refresh the traceability matrix and record residuals honestly before owner review of G4 disposition.

**Non-Goals:**

- Production wiring, authoritative state transitions, rollout, deployment, or behavior changes to existing verification paths.
- Closing G5's persisted composition, database concurrency, RLS, or atomic publication requirements.
- Implementing CAUSAL or other unsupported rule kinds, obligation persistence, or a witness/solver authority path.
- Claiming population-level accuracy or absence of false accepts from a finite fixture suite.
- Re-freezing unrelated baselines or changing their revision-pin policy as part of G4.

## Decisions

### D1: G4 is split into four independently reviewable golden-layer components

**Decision:** Implement and review the evidence, rules, composition, and pure end-to-end decision components in that order. Each component has its own owner confirmation point. A component may be marked complete only when its specified artifacts and tests pass; G4 stays PARTIAL until all four components and the global exit criteria pass.

**Alternatives considered:** Treat G4 as one combined implementation batch. Rejected because it obscures which trust boundary failed and makes review and rollback less attributable.

**Rationale:** The existing verification contract already defines four distinct layers. Preserving that decomposition keeps evidence admissibility, inference soundness, graph composition, and decision integration from being conflated.

**Trade-offs:** Component sequencing introduces review pauses and may require revisiting earlier layers when downstream negative tests expose a contract defect.

---

### D2: Rule additions are narrow, typed, versioned, and fail closed

**Decision:** Add `behavior/v1`, `absence/v1`, and `history/v1` only after their evidence inputs, bindings, scope, and unsupported cases are frozen in tests. Do not broaden `definition/v1` or `call_edge/v1` to cover these cases. A rule that cannot establish its proposition under the declared evidence contract returns a typed non-discharge outcome; the registry must not map it to a successful proof.

**Rule contracts:**

- **BEHAVIOR:** Requires a `RANGE_READ_FACT`, `RUNTIME_OBSERVATION_FACT`, and `BUILD_SOURCE_MAPPING`. The proposition target `(symbol,file_path)` equals the code premise target. Proposition scope `(revision,build_identity,execution_id,environment)` must equal the code premise revision, runtime premise fields, and mapping. The mapping must exactly bind `(build_identity,repository,source_revision)` and its repository/revision must match the code premise. The runtime predicate/value must equal the proposition's asserted behavior. Because this mapping record does not exist yet, the rule is unsupported-by-construction until Components 1–2 add and validate it; code alone, unproven mapping, runtime text, mismatched builds, or unbound results abstain.
- **ABSENCE:** Requires exactly one `ABSENCE_COVERAGE_FACT` in the manifest, a successful complete-empty search, a bounded universe and predicate matching the proposition, zero matches, and recorded exclusions. The canonical proposition uses `form=ABSENCE`, `quantifier=UNIVERSAL`, `modality=STATIC`, `completeness=CLOSED_WORLD`; the certificate coverage result is `COMPLETE_PATH`, mapped from manifest `COMPLETE_EMPTY`. The checker requires the coverage record and manifest to agree on universe, predicate, scope, exclusions, and search-config digest. Failed/incomplete search, missing coverage, unbounded universe, nonzero match count, or exclusion of relevant scope rejects. The checker enforces the recorded coverage contract; it does not claim provider completeness beyond that boundary.
- **HISTORY:** Requires exactly one `COMMIT_HISTORY` premise whose repository, full 40-character lowercase commit ID, file path, and subject exactly match the proposition target. `scope.repository_revision` identifies the queried source snapshot, not the target commit. Prefix SHAs are not accepted by the kernel. A file mention, branch label, vague time range, or causal interpretation does not discharge a historical-change claim.

**Checker form and quantifier gate:** The current `check_certificate()` final gate admits only `form == EXISTENCE` and rejects `quantifier == UNIVERSAL`; it must be replaced by a rule-specific contract check before any new rule can be accepted. The exact accepted header combinations are: `definition/v1` and `call_edge/v1`: `form=EXISTENCE`, `quantifier=EXISTENTIAL`; `behavior/v1`: `form=OBSERVATION`, `quantifier=OBSERVATIONAL`, `relation=RUNTIME_OBSERVATION`, `modality=RUNTIME`; `absence/v1`: `form=ABSENCE`, `quantifier=UNIVERSAL`, `modality=STATIC`, `completeness=CLOSED_WORLD`, plus valid complete-coverage evidence; `history/v1`: `form=HISTORICAL`, `quantifier=EXISTENTIAL`, `modality=HISTORY`. `relation` and `completeness` are omitted for HISTORY unless a later approved contract explicitly requires them. No rule accepts `form=UNIVERSAL`; ABSENCE is the only G4 rule authorized to accept the `UNIVERSAL` quantifier, and only within its declared universe. Any other combination—including any omitted required field—returns `UNSUPPORTED_CONSTRUCT`; a matching combination with malformed or insufficient premises returns its more specific typed rejection. For ABSENCE, a `COMPLETE_EMPTY` manifest coverage maps to certificate coverage `COMPLETE_PATH`, and both the manifest coverage and the `ABSENCE_COVERAGE_FACT` must agree on zero matches, universe, scope, exclusions, and search configuration.

For HISTORY, omitted `relation` and `completeness` keys are the canonical representation; explicit `null` values are invalid and must reject. Component 2 pins this rule with byte-level canonicalization vectors for (a) both keys absent, (b) either key explicitly null, and (c) an unexpected non-null value. The serializer and checker must produce the same deterministic result without normalizing null into omission.

Every registry row declares the exact input premise types, bindings, scope requirements, assumptions, soundness, version, and unsupported cases. Rule-version changes are explicit semantic changes; old rule IDs are not silently reinterpreted. Adding these rules requires `RULE_BUNDLE_VERSION = rules/v2` and a direct certificate contract update to `cert/v2`. Because IAP has no live external consumers, migrate existing kernel fixtures and tests directly to `cert/v2`; remove `cert/v1` acceptance rather than keeping a permanent dual-version reader. Any `cert/v1` input after the change returns `CHECKER_FAILURE` (unsupported contract version). `CheckRequest.rule_bundle_version` and `EvaluationInputs.rule_bundle_version` must be supplied explicitly and exactly as `rules/v2`; the existing empty-string default is removed. Missing or mismatched bundle/schema pairs fail closed, with no fallback to another registry.

**Claim bindings and canonical header:** `cert/v2.canonical_proposition` uses the existing header fields `predicate`, `quantifier`, `canon_version`, `form`, `relation`, `modality`, `completeness`, `assumptions`, and `target`. The certificate also carries `scope` at top level, matching `EvaluationInputs.proposition`.

Scope is context-only: it is excluded from `proposition_id` but included in `context_id`. Current `_freeze_context()` omits proposition scope. Component 2 must add normalized scope to the context preimage as `proposition_scope`, pin it with a context golden, and have the checker reject omitted or divergent scope.

The exact target projection is:

- DEFINITION: `symbol`, `file_path`.
- CALL_EDGE: `caller`, `callee`; normalize existing `counterpart`/`symbol` aliases before canonicalization under ClaimTarget.
- BEHAVIOR: `symbol`, `file_path`.
- ABSENCE: `universe`; `predicate` remains the canonical proposition root field.
- HISTORY: `repository`, `commit_id`, `file_path`, `subject`.

Context values live under `scope`, not `target`: DEFINITION and CALL_EDGE use `revision`; BEHAVIOR uses `revision`, `build_identity`, `execution_id`, `environment`; ABSENCE uses `repository`, `revision`, `path_scope`; HISTORY uses `repository_revision` for the queried snapshot while target `commit_id` identifies the historical commit.

Component 2 extends `canonical.py` allowlists, classifies target fields as proposition identity and scope fields as evaluation context, and pins both identity and context vectors. No accepted field may be silently projected away. `certify()` derives the full `inference_instance.bindings` map from canonical header, target, and scope. The checker independently derives the expected map and compares it byte-for-byte; unknown, missing, extra, or contradictory fields reject. Producer and checker must not share the binding-projection implementation.

**Premise types, validation, and manifest mapping:** Add the closed G4 premise vocabulary and strict immutable records in `src/investigation_agent_platform/domain/proof/premises.py`: `RANGE_READ_FACT`, `NORMALIZED_CALLSITE_FACT`, `RUNTIME_OBSERVATION_FACT`, `BUILD_SOURCE_MAPPING`, `ABSENCE_COVERAGE_FACT`, and `COMMIT_HISTORY`. Each record validates required non-empty identity/provenance fields, strict positive line coordinates where applicable, closed type tags, and rejects unknown fields. `RUNTIME_OBSERVATION_FACT` includes `symbol`, `execution_id`, `build_identity`, `environment`, `predicate`, `observed_value`, and `source_revision`; `observed_value` is a canonical JSON scalar (string, integer, boolean, or null; floats are forbidden), and its predicate/value must exactly match the asserted proposition. `BUILD_SOURCE_MAPPING` includes its premise type and binds `build_identity`, `repository`, and `source_revision`; no such mapping currently exists in the G4 kernel, so BEHAVIOR is unsupported-by-construction until Component 1 freezes this evidence contract and Component 2 validates and pins it as an admitted premise. No implicit mapping from timestamps or matching text is allowed.

**Manifest mapping:** `EvidenceManifest.evidence_ids`, `content_hashes`, and a required `premise_types: dict[str, PremiseType]` bind each artifact by ID, full digest, and closed type. All three mappings have exactly the same key set. `CheckRequest.artifacts` contains normalized record bytes for those entries.

For ABSENCE, add frozen `coverage_evidence: AbsenceCoverage | None` and `coverage_evidence_id: str | None` fields. The ID must exist in all three manifest mappings with type `ABSENCE_COVERAGE_FACT`, and `CheckRequest.artifacts[coverage_evidence_id]` contains its normalized record. `AbsenceCoverage` does not embed its own ID or content hash. Its universe, predicate, scope, exclusions, `search_config_digest`, `coverage_basis`, `search_status`, and `match_count` are bound by `manifest_digest`.

`retrieval_config` may carry retrieval parameters and matching scope/config references, but cannot substitute for coverage evidence. The checker validates agreement between coverage, canonical proposition, and manifest scope/config identities. Producer `_validator_premise()` and the independent checker each validate the closed artifact first, then require its type tag to match `manifest.premise_types[evidence_id]`. `rule.input_types` is the permitted-type set; the checker also enforces required premise conjunctions.

Complete Pydantic definitions, validators, field limits, and error enums belong in the Component 1/2 supplements. These phase-plan fields are normative inventories, not implementation-ready schemas or code.

**History commit matching:** Require a full 40-character lowercase hexadecimal commit ID in both proposition target and premise; do not accept abbreviated SHA prefixes. The repository/history retrieval layer may resolve a user-entered prefix before it becomes a G4 premise, but G4 validates only the resolved full identifier. This avoids inheriting the application's bidirectional startswith matching as a proof rule.

**Composition dependencies:** Extend `RULE_DEPS` with explicit rows: `behavior/v1` depends on `evidence.content`, `evidence.availability`, `scope.revision`, `scope.execution`, `scope.environment`, `scope.build_identity`, and `rule.bundle`; `absence/v1` depends on `evidence.content`, `evidence.availability`, `scope.repository`, `scope.revision`, `scope.path`, `coverage.universe`, `coverage.exclusions`, `coverage.search_config`, and `rule.bundle`; `history/v1` depends on `evidence.content`, `evidence.availability`, `scope.repository`, `scope.revision`, `history.commit`, `history.file_path`, `history.subject`, and `rule.bundle`. These are the complete proof-relevant change classes for G4. Operational classes remain non-invalidating. A missing registry dependency row is a test failure and blocks rule registration.

**Rule input sets:** `definition/v1` requires exactly one `RANGE_READ_FACT`; `call_edge/v1` requires one or more `NORMALIZED_CALLSITE_FACT` records under its existing route contract; `behavior/v1` requires one `RANGE_READ_FACT`, one `RUNTIME_OBSERVATION_FACT`, and one `BUILD_SOURCE_MAPPING`; `absence/v1` requires exactly one `ABSENCE_COVERAGE_FACT`, no match-result records, and non-null `EvidenceManifest.coverage_evidence` whose `search_status` is `COMPLETE_EMPTY` and `match_count` is zero; `history/v1` requires exactly one `COMMIT_HISTORY` record. Duplicate IDs are rejected before rule application. Extra premise types reject unless the component supplement explicitly documents an allowed optional premise and its semantics. This makes absence non-vacuous while preserving the rule that zero matches is data in a coverage proof, not an empty certificate premise list.

**Alternatives considered:** Add broad generic rules over arbitrary evidence types. Rejected because that weakens premise semantics and makes valid hashes appear more authoritative than the evidence they bind.

**Rationale:** The proposed rules address current G4 gaps while keeping evidence identity, proof meaning, and context freshness separate.

**Trade-offs:** These initial contracts intentionally abstain on runtime/build joins and coverage claims that cannot be proven from available typed records. Additional evidence types or broader rules require a later contract and version.

---

### D3: Checker independence is semantic, not merely a separate call path

**Decision:** The checker must independently implement semantic admissibility checks for every G4-supported rule: DEFINITION, CALL_EDGE, BEHAVIOR, ABSENCE, and HISTORY. It may share frozen serialization schemas and canonical identity primitives, but it must not call `apply_rule`, `_VALIDATORS`, or any producer per-rule validator to reach its semantic verdict. Producer-supplied result, soundness, and validation fields remain untrusted.

The independent checker receives the certificate, canonical proposition, evidence manifest, available normalized artifacts, registry contract, expected context, and dependency graph as explicit immutable inputs. It recomputes identities and content hashes, independently validates each premise against the rule contract, then checks coverage, scope, dependencies, and freshness. Any malformed input or checker exception fails closed with a typed outcome; infrastructure/checker failure is never a negative proof.

Rule contract identity and executable identity are distinct. The prescribed executable-drift control is a checked-in SHA-256 fingerprint manifest at `tests/fixtures/proof_kernel/validator_fingerprints_v1.json`, generated and checked by `scripts/proof_kernel_validator_fingerprints.py`. Component 2 fingerprints exact source bytes for the producer/checker modules and the shared proof-boundary modules `premises.py`, `manifest.py`, `evaluation.py`, and `canonical.py`; its supplement names the complete six-module set and reasons. The exact check command is `uv run python scripts/proof_kernel_validator_fingerprints.py --check`; it recomputes every pinned hash and fails with the changed relative path and expected/actual digest when they differ. The named detector test is `tests/unit/test_proof_kernel_validator_fingerprints.py::test_validator_fingerprint_check_detects_unreviewed_source_change`, which modifies a copied module in a temporary directory and asserts a nonzero check result; the unmodified repository copy must pass. Updating a fingerprint requires review of the changed producer/checker semantics or shared proof-boundary contract and associated version decision; it is not an automatic bless-on-test behavior. The fingerprint detects source changes, not the bytes of an installed or deployed interpreter image, and G4 must not claim more than this test/repository control establishes.

**Alternatives considered:** Invoke the producer validator twice from a separate checker function. Rejected because common semantic defects would be repeated, not independently detected.

**Rationale:** A diverse validation path directly addresses the declared G4-3 closeout requirement while allowing stable low-level canonicalization code to remain shared.

**Trade-offs:** Independent validators duplicate some logic and can drift. Differential goldens and explicit rule-contract versioning are required to manage this risk.

---

### D4: G4 end-to-end means pure decision integration; persisted atomicity remains G5

**Decision:** G4's fourth component exercises a complete in-memory path from evidence artifacts through rule evaluation, certificate serialization, independent checking, composition, and publication decision. It verifies that only a checker-valid current derivation can receive a COMMIT decision and that policy denial does not mutate or erase the proof. It does not write authoritative status or claim a live snapshot is atomically bound at database commit.

**Alternatives considered:** Require G4 to prove production database atomicity. Rejected because G5 owns the persistent commit protocol, and the traceability record explicitly says publication-commit atomicity remains open.

**Rationale:** G4 can close kernel correctness and pure integration without falsely upgrading the existing non-authoritative boundary.

**Trade-offs:** G4 PASS will mean the kernel's declared pure contracts pass their finite evidence suite; it will not mean that existing production paths are protected from bypass or that publication is safe to enable.

---

### D5: G4 completion does not authorize implementation or authority

**Decision:** This document is a phase plan, not a code-level design. Before authorizing each component, publish and review a design supplement named `docs/IAP-proof-kernel-g4-component-N-design-v1.0.md` (replace N with 1–4). Each supplement must include full Pydantic record definitions, registry rows, producer and independent checker function signatures and complete logic, error/outcome codes, fixture schema and examples, exact tests/commands, file list, and component-specific migration/version effects. For Components 1–3, the supplement also maps each fixture and mutation to the named gate; Component 4 includes the full harness and report schema. This phase plan supplies architectural constraints but deliberately does not substitute prose sketches for those implementation details. Each component requires separate explicit owner authorization after its supplement is reviewed. After component completion, update the traceability matrix with exact evidence and residuals. G4 PASS requires explicit owner review of that evidence; any production integration, migration, rollout, or authority change requires a separate decision and the applicable G5/G7 gates.

**Alternatives considered:** Treat acceptance of this phase plan as a code-ready design or blanket implementation authorization. Rejected because field-level contracts and complete implementations must be frozen in component supplements, and the forward plan requires explicit owner go-ahead per step.

**Rationale:** Keeps design approval, implementation approval, gate disposition, and rollout authority distinct.

**Trade-offs:** More design/approval checkpoints, but each implementation authorization can be bounded to a complete, reviewable specification.

## Data Storage

No persistent model, database table, migration, or external data contract is introduced by G4. Certificates and evidence manifests remain immutable in-memory test inputs for this phase. Existing test fixtures may be added or versioned under `tests/fixtures/proof_kernel/`; fixture changes must be explicit and reviewable.

## Data Structures

G4 migrates the current kernel certificate contract directly to `cert/v2` and uses the existing `EvidenceManifest` as the base for the explicitly typed `coverage_evidence` addition. New rule-specific artifacts must be strict immutable Pydantic records validated at the domain boundary, content-hashed, and bound to evidence IDs in the manifest. The shapes below describe required semantic fields only; complete executable model definitions belong in the component design supplements.

### BEHAVIOR evidence inputs

```text
BehaviorCodePremise {
  evidence_id: string
  content_hash: sha256
  premise_type: RANGE_READ_FACT
  repository: string
  file_path: string
  revision: string
  line_start: positive_integer
  line_end: positive_integer
  target: string
}

BehaviorRuntimePremise {
  evidence_id: string
  content_hash: sha256
  premise_type: RUNTIME_OBSERVATION_FACT
  repository: string
  target: string
  predicate: string
  observed_value: string | integer | boolean | null
  execution_id: string
  build_identity: string
  environment: string
  observed_at: timestamp_or_recorded_event_time
  source_revision: string
}

BuildSourceMapping {
  evidence_id: string
  content_hash: sha256
  premise_type: BUILD_SOURCE_MAPPING
  build_identity: string
  repository: string
  source_revision: string
  mapping_digest: sha256
}
```

The BEHAVIOR binding compares proposition target `(symbol, file_path)` to the code premise target and proposition scope `(repository, revision, build_identity, execution_id, environment)` to code repository/revision and runtime repository/build/execution/environment fields. The `BUILD_SOURCE_MAPPING` record must establish the exact tuple `(build_identity, repository, source_revision)`; its repository/revision must equal the code premise repository/revision. This mapping record does not exist in the current G4 kernel. Until Component 1 freezes this evidence contract and Component 2 validates and admits it, BEHAVIOR is unsupported-by-construction and all attempts abstain; no implementation may infer the mapping from matching names, timestamps, or the runtime event itself.

### ABSENCE coverage input

```text
AbsenceCoverage {
  universe: non_empty_string
  predicate: canonical_predicate
  search_status: COMPLETE_EMPTY
  search_config_digest: sha256
  scope: { repository, revision, path_or_resource_scope }
  exclusions: [explicit_exclusion]
  coverage_basis: non_empty_typed_record
  match_count: 0
}
```

The rule rejects missing or inconsistent coverage fields. `COMPLETE_EMPTY` is admissible only within the declared universe; it does not establish global absence outside it.

### HISTORY evidence input

```text
HistoryPremise {
  evidence_id: string
  content_hash: sha256
  premise_type: COMMIT_HISTORY
  repository: string
  repository_revision: string
  commit_id: full lowercase 40-hex SHA
  file_path: string
  subject: string
}
```

The proposition target must identify the same repository, full commit ID, file, and exact subject; `scope.repository_revision` identifies the source snapshot against which the history result was retrieved. The rule does not infer causation or runtime behavior from commit metadata.

## Interfaces

G4 introduces no REST, CLI, MCP, workflow, or user-facing interface. The implementation boundary remains pure domain functions and test harnesses.

### Domain-level contracts

- Registry lookup: an exact registered rule ID resolves to one immutable `ProofRule`; unknown IDs abstain or reject with a typed unsupported outcome.
- Producer rule application: consumes a canonical claim and content-hash-bound normalized premises; returns a typed outcome and reason without persistence or side effects.
- Independent checker: consumes explicit certificate, proposition, manifest, artifacts, registry version, expected context, and dependency inputs; returns a typed `CheckerOutcome` and reason codes.
- G4 integration harness: runs evaluation → certificate → independent check → composition → decision using frozen fixtures; emits a deterministic report with input fixture IDs, expected/actual outcomes, and failure attribution.

The report is test output, not a new production artifact or operational API.

## Accessibility

No REST, CLI, MCP, TUI, or GUI interface is introduced by G4. This section is not applicable to the pure domain and test-harness scope.

## Implementation Detail

Implementation proceeds component-by-component. Before each component, the owner confirms the exact scope. A failed exit criterion blocks the next component; repair work is limited to the failing component unless the owner authorizes a changed scope.

### Component 1 — Evidence-level goldens and admissibility

Freeze positive and negative evidence records for BEHAVIOR, ABSENCE, and HISTORY before writing rule implementations. Include valid, malformed, missing-artifact, hash-mismatch, wrong-type, wrong-target, wrong-revision/build, and scope-mismatch cases. For ABSENCE, include bounded complete-empty, incomplete, failed, unbounded, relevant-scope-excluded, and nonzero-match cases. For HISTORY, include exact match and each repository/commit/file/subject mismatch independently. Add cross-layer identity cases where a content hash is valid but the semantic record is inadmissible. This component defines fixture schemas and expected outcomes only; it does not implement proof-premise classes, the rule registry, canonicalization, certificates, checker logic, or runtime behavior.

Preserve the rule that hashes prove byte identity only. Evidence source identity, provenance, semantics, scope, and coverage each receive independent assertions. Store fixture provenance and expected rejection reason alongside each case. Do not update the baseline manifest or claim live-stack evidence as a part of this component.

**Entry criteria:** Component 1 supplement exists and is reviewed; owner explicitly authorizes Component 1; current traceability matrix and fixture inventory are reviewed; contract fields and unsupported cases above are accepted. The authorization package must include a `BUILD_SOURCE_MAPPING` provenance-contract assessment for senior technical review: admissible source of the mapping, exact build-to-source identity fields, mismatch and missing-data outcomes, and how the mapping avoids self-assertion by the runtime observation. Component 1 freezes the evidence contract and its fixtures only; runtime premise classes and checker enforcement are Component 2 work. If no admissible mapping source can be identified, Component 1 records BEHAVIOR as unsupported and escalates whether that limitation blocks G4 PASS.

**Exit criteria:** every new premise shape has valid and invalid golden fixtures; hashes and scope bindings are independently pinned; fixture-integrity checks pass; no fixture grants acceptance based only on a producer label or matching hash. Until a valid `BUILD_SOURCE_MAPPING` contract is defined and positively tested, all BEHAVIOR examples are expected to abstain; the component may not label BEHAVIOR discharge as covered.

### Component 2 — Rule-level rules and diverse checker

Add registry entries and producer validators for `behavior/v1`, `absence/v1`, and `history/v1`, preserving existing rule behavior. Update the checker with distinct semantic validation functions that do not invoke producer validators. Keep shared schema parsing and canonical hash primitives explicit in the documented trusted computing base. Confirm checker failure, unavailable evidence, unsupported constructs, and invalid inference remain distinguishable outcomes. Component 2 owns premise Pydantic types, canonical field allowlists and identity vectors, evidence-manifest coverage typing, claim-binding projection, certificate/rule-bundle versioning, rule form/quantifier checks, producer and independent validators, and the executable-fingerprint mechanism.

Add differential tests that feed identical frozen inputs to producer and checker paths and assert agreement for valid certificates and principled disagreement for tampered producer assertions. Add a K9-oriented release/test control that detects executable-validator changes not accompanied by the required contract review/version change. Update rule registries and fixtures together; no implicit version migration is allowed.

**Entry criteria:** Component 1 passes; Component 2 supplement specifies exact Pydantic types, rule rows, binding projections, form/quantifier gate, v2 certificate header, registry version, dependency rows, checker code, fingerprint fixture, and HISTORY canonicalization vectors proving absent optional header fields remain absent, explicit null is rejected, and unsupported non-null values are rejected; owner confirms Component 2 scope.

**Exit criteria:** each new rule has positive, negative, and unsupported-case tests; only explicitly admissible evidence discharges; producer and independent checker agree on all valid goldens; every invalid golden is rejected with attributable typed outcome; mutations to every producer validator cannot make the checker accept invalid evidence; validator drift without an approved version/contract change is detected; `cert/v1` fails closed and all maintained kernel fixtures use `cert/v2`; existing DEFINITION/CALL_EDGE semantics remain pinned by migrated fixtures. If `BUILD_SOURCE_MAPPING` cannot be supported with a validated provenance contract, BEHAVIOR remains explicitly unsupported and G4 cannot claim BEHAVIOR discharge coverage; gate owner must decide whether that limitation blocks G4 PASS before Component 2 authorization. HISTORY omission/null canonicalization vectors must pass for both producer serialization and independent checker parsing.

### Component 3 — Composition-level properties and mutation sensitivity

Exercise the rules through the existing pure composition and invalidation APIs. Cover conjunction (all mandatory premises required), alternatives (one valid route survives a failed route), dependency closure (changed premises invalidate declared dependents transitively), cycle rejection, unrelated-route preservation, duplicate-premise rejection, policy-change reevaluation, and recovery-triggered reevaluation where covered by the declared pure contracts. Keep publication concurrency and persisted graph behavior in G5.

Add property-based or deterministic generated tests only where they add value beyond the current curated goldens. Required properties include monotonicity (additional evidence cannot strengthen a result without a rule discharge), refutation sensitivity and removal recomputation, scope non-escalation, deterministic replay, invalidation closure, and provenance non-independence. Maintain a mutation set with at least one mutation for each critical invariant family: remove a mandatory premise check, weaken scope/revision equality, bypass semantic validation, drop a dependency edge, accept incomplete absence coverage, and accept stale context. Each mutation must cause a named test to fail; mutation results are evidence of test sensitivity, not a population accuracy estimate.

**Entry criteria:** Component 2 passes; Component 3 supplement specifies property generators, dependency-class mutations, exact detector tests, report format, and G5 exclusions; owner confirms Component 3 scope; mutations are mapped to named invariants and expected failing tests before running them.

**Exit criteria:** all declared composition goldens and required properties pass; each required mutation is detected by its mapped test; valid alternatives and unrelated routes remain valid; no undeclared dependency is inferred; no database concurrency or atomicity claim is made.

### Component 4 — Pure end-to-end certificate-to-decision integration

Build a test-only orchestration harness that executes the frozen evidence → rule → certificate → independent checker → composition → publication-decision path. Use the real pure domain APIs, not mocked acceptance decisions. Include one valid case per supported rule, checker rejection cases, stale context, missing premise, policy denial, alternative survival, and combined multi-premise behavior. Assert that a denied decision preserves the proof artifact and that the decision references the exact accepted derivation/context. Verify no production workflow, application publication writer, or feature flag consumes the G4 harness as authority.

This component terminates at a pure decision result. It does not insert publication records, update hypothesis/finding status, or simulate database atomicity as if that were evidence. Database-level commit and interleaving tests remain G5 requirements.

**Entry criteria:** Components 1–3 pass; Component 4 supplement defines harness modules, complete report schema, exact fixtures and commands, static consumer-scan contract, and all decision outcomes; owner confirms Component 4 scope; G5 residuals are explicitly carried forward.

**Exit criteria:** end-to-end pure cases pass with deterministic result and complete failure attribution; no invalid/stale certificate yields COMMIT; policy changes recompute the decision without mutating proof; static boundary checks confirm no new authoritative consumer; test report distinguishes G4 pure decision coverage from G5 persistence/atomicity.

### Global G4 exit criteria

G4 may be proposed as PASS only when all of the following are true:

1. All four component exits are recorded with exact commands, fixture identities, test results, and owner approvals.
2. K1–K16 and A–T statuses are reconciled against executable evidence; `NOT MAPPED`, `N/A`, `PENDING`, and partial states retain explicit rationale and are not relabeled as PASS to force closure. Scenarios outside G4's scope, especially database atomicity, remain owned by G5 and are named as such.
3. The rule registry remains closed and versioned; DEFINITION, CALL_EDGE, BEHAVIOR, ABSENCE, and HISTORY have explicit premise contracts, negative cases, and unsupported boundaries. CAUSAL remains unsupported unless separately designed and authorized.
4. The independent checker does not invoke producer semantic validators; its shared trusted primitives are inventoried; executable drift detection is demonstrated.
5. The full relevant kernel suite, fixture-integrity suite, property suite, mutation suite, and pure integration suite pass. Skips, xfails, and known failures are reported separately and are never counted as passes. No unexplained failure is accepted as pre-existing without named evidence and gate-owner acceptance.
6. There are zero new false accepts in the frozen adversarial suite, with results attributed by reason and rule family. The suite is exactly `tests/unit/test_canon_identity.py`, `tests/unit/test_manifest_evaluation.py`, `tests/unit/test_typed_rules.py`, `tests/unit/test_certificate_checker.py`, `tests/unit/test_composition_invalidation.py`, `tests/unit/test_publication_decision.py`, `tests/unit/test_g5_composition_gate.py`, `tests/unit/test_proof_kernel_fixture_integrity.py`, `tests/unit/test_proof_kernel_validator_fingerprints.py`, `tests/unit/test_proof_kernel_g4_component1_evidence.py`, `tests/unit/test_proof_kernel_g4_component2_rules.py`, `tests/unit/test_proof_kernel_g4_component3_properties.py`, `tests/unit/test_proof_kernel_g4_component4_integration.py`, and `tests/unit/test_proof_kernel_premises.py`. Component supplements must use these exact G4 test filenames or revise the suite list before their implementation authorization. Run this exact command from the repository root:

   `uv run python scripts/proof_kernel_validator_fingerprints.py --check && uv run python -m pytest -q -p no:cacheprovider tests/unit/test_canon_identity.py tests/unit/test_manifest_evaluation.py tests/unit/test_typed_rules.py tests/unit/test_certificate_checker.py tests/unit/test_composition_invalidation.py tests/unit/test_publication_decision.py tests/unit/test_g5_composition_gate.py tests/unit/test_proof_kernel_fixture_integrity.py tests/unit/test_proof_kernel_validator_fingerprints.py tests/unit/test_proof_kernel_g4_component1_evidence.py tests/unit/test_proof_kernel_g4_component2_rules.py tests/unit/test_proof_kernel_g4_component3_properties.py tests/unit/test_proof_kernel_g4_component4_integration.py tests/unit/test_proof_kernel_premises.py`

   This command becomes executable only after all four supplements' test files exist; component-specific commands must be provided in each supplement. The unrelated revision-pinned baseline test is not in this G4 suite and must be reported separately in the full-suite result. This is a regression threshold only, not a claim about all inputs or production populations.
7. The G4 traceability matrix, verification document, and this design's implementation record agree on scope and residuals; matrix owner sign-off is recorded.
8. G4 PASS is recorded as kernel assurance only. It does not authorize production wiring, rollout, migrations, or publication authority; G5/G7 and separate owner decisions remain required.

## Migrations

No database migration is planned or authorized for G4. The approved design direction is a direct in-repository kernel contract migration from `cert/v1` to `cert/v2` and from `rules/v1` to `rules/v2`, with tests and fixtures migrated to the new version and old certificate inputs rejected. Component 2's supplement must provide the complete schema and fixture migration inventory before authorization; if it finds a need to persist or dual-read old certificate records, stop and request a separate owner decision. G5's persisted publication schema is outside this phase.

## Testing Philosophy

### Evidence-level goldens

Each evidence contract is tested independently for identity, bytes, provenance, scope, semantic type, target binding, and coverage. Negative fixtures vary one relevant property at a time so a rejection is attributable. Valid hashes never substitute for semantic checks.

### Rule and checker tests

For each rule, test the complete minimal valid premise set, every missing or mismatched premise, explicit unsupported cases, hash and artifact availability failures, and checker exception behavior. Producer and checker paths use separate semantic implementations and separately adjudicated expected outcomes. Differential agreement is not sufficient by itself; each expected outcome is pinned in a golden. A mutation to any one producer validator must leave the independent checker's verdict unchanged for fixed certificate inputs; a mutation to the checker must be detected by the checker-specific goldens or source fingerprint gate.

### Composition and mutation tests

Composition tests check the declared graph only and distinguish route status from proposition status. Mutation tests deliberately weaken critical checks and must fail at least one named test each. Record the mutation, expected detector, and actual detector so a passing mutation run cannot be mistaken for a passing implementation suite.

### Pure end-to-end tests

Run all supported rule kinds through the actual pure pipeline and assert the exact certificate identity, checker result, composition result, decision, and reason codes. Explicitly keep database transactions, persistence races, writer routing, and production behavior in G5 or later gates.

### Regression and scope reporting

Run the project-prescribed kernel checks and relevant full unit suite. Report pass, fail, skip, and xfail counts independently, alongside the exact revision and command. A pre-existing failure may be carried only with an exact failure identity, proof it is unchanged, and gate-owner acceptance; it is never included in a green count.

## Documentation Plan

### `docs/IAP-proof-kernel-g4-phase-design-v1.0.md`

**Audience:** Kernel implementers and gate reviewers. This is a phase-gating plan, not implementer-ready code design. Its current version defines the four G4 components, implementation boundaries, component confirmations, and global exit criteria. Each component's code-level contract must be authored in its own reviewed supplement before that component is authorized.

### `docs/IAP-proof-kernel-traceability-v1.0.md`

**Audience:** Gate owner and reviewers. After implementation, update exact K/A–T states, rule coverage, checker-independence evidence, property/mutation results, pure integration evidence, and residual G5 work. Never mark an entire K/A–T scenario PASS when only a narrower scope is tested.

### `docs/IAP-proof-kernel-verification-v1.0.md`

**Audience:** Test authors and assurance reviewers. Add the new rule-level vectors and named property/mutation detector mapping if the existing verification contract does not already state them precisely. Keep the four golden layers and finite-suite limitation explicit.

### `docs/IAP-proof-kernel-contracts-v1.0.md`

**Audience:** Kernel implementers and contract reviewers. Component 2 must directly migrate the certificate contract and schema references from `cert/v1`/`rules/v1` to `cert/v2`/`rules/v2`, update rule form/quantifier contracts, and reconcile premise/binding definitions with this approved plan. Do not retain a permanent `cert/v1` compatibility reader.

### `docs/archive/dev/architecture.md` changes

No changes are required. The G4 work adds pure proof-domain behavior and a test-only harness; it does not change system authority, deployment, or data flow. No `docs/dev/architecture.md` exists in the project.

## Risks / Trade-offs

### Rule contracts exceed available provenance

**Risk:** Runtime observations may not carry enough build/source mapping to soundly join them to code; search providers may not establish complete coverage for ABSENCE; history rows may omit exact commit/file metadata.

**Mitigation:** Treat missing or ambiguous provenance as unsupported and keep the relevant rule partial. Do not invent joins or relax evidence requirements to obtain a passing case.

### Independent validators drift

**Risk:** Separate semantic implementations may disagree over edge cases or become inconsistent as rule contracts evolve.

**Mitigation:** Share only explicitly inventoried canonical/schema primitives, pin expected results independently, run differential goldens, and require rule-version review when contracts change.

### Finite tests create false confidence

**Risk:** Passing fixtures and mutations may be mistaken for universal correctness or production safety.

**Mitigation:** Preserve bounded claims in the traceability matrix, report untested inputs, and prohibit G4 PASS from authorizing production integration or atomicity claims.

### Scope leakage into G5 or rollout

**Risk:** A pure publication-decision test could be described as proving persistent publication safety or used to justify authoritative wiring.

**Mitigation:** Keep G4 harness test-only, add a boundary check for production consumers, label the result “pure decision integration,” and carry database commit, concurrency, RLS, and writer-audit criteria as explicit G5/G7 work.

### Baseline revision pin adds unrelated maintenance

**Risk:** A repository revision pin may fail after unrelated commits and obscure whether G4 fixtures or contracts changed.

**Mitigation:** Do not modify the baseline pin policy in G4. Report the current baseline failure separately; propose any change to provenance-vs-gate semantics as a distinct owner-reviewed task.
