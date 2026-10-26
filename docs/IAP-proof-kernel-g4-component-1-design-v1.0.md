# Design: Proof-Kernel G4 Component 1 — Evidence Fixtures v1.0

> Status: fixture-level exit claimed by owner review on 2026-10-24 for the pre-amendment corpus. The owner-approved exit is limited to the checked-in fixture corpus, JSON Schema, duplicate-key protection, inventory/mutation assertions, and the two fixture-level commands recorded below. Any later Component 2 contract amendment requires a new validation run and does not retroactively extend this exit. It does not close G4, authorize Component 2, or authorize rule/checker implementation.
> Scope: freeze positive-candidate and negative evidence fixtures for BEHAVIOR, ABSENCE, and HISTORY. Component 1 does not implement runtime premise classes, proof rules, canonicalization, certificate changes, checker logic, or production behavior.

## Context

The G4 phase plan divides kernel assurance into evidence, rules, composition, and pure end-to-end decision components. Component 1 establishes adjudicated evidence inputs before Component 2 implements premise records and rule/checker behavior. Current kernel types do not include runtime-observation, build-source-mapping, absence-coverage, or history proof premises; therefore, this supplement describes fixture envelopes and semantics, not current executable acceptance.

Existing fixtures under `tests/fixtures/proof_kernel/` use deterministic IDs, JSON payloads, expectation labels, requirement mappings, and fixture-integrity checks. The existing fixture ledger intentionally distinguishes structural validation from semantic verdicts. Component 1 follows those conventions while assigning a G4-specific namespace and expectation vocabulary. The authorized corpus remains in its separate G4 namespace; it is not added to the existing v1 fixture ledger because ledger integration requires a separate review.

The principal unresolved design input is the provenance source for `BUILD_SOURCE_MAPPING`. A mapping cannot establish its own truth by repeating a build ID, repository, and revision in a record. This supplement makes provenance adjudication a prerequisite rather than assuming a source.

## References

- [`docs/IAP-proof-kernel-g4-phase-design-v1.1.md`](IAP-proof-kernel-g4-phase-design-v1.1.md) — parent phase plan (authoritative; v1.0 retained) defining component gates, typed fields, and the senior review requirement for build-to-source mapping.
- [`docs/IAP-proof-kernel-verification-v1.0.md`](IAP-proof-kernel-verification-v1.0.md) — defines evidence-level requirements, K1–K16, A–T, and finite-suite limits.
- [`docs/IAP-proof-kernel-traceability-v1.0.md`](IAP-proof-kernel-traceability-v1.0.md) — current kernel scope and evidence gaps, including K3 and unmapped A–T cases.
- [`tests/unit/test_proof_kernel_fixture_integrity.py`](../tests/unit/test_proof_kernel_fixture_integrity.py) — current conventions for deterministic IDs, inventory coverage, and fixture integrity.
- [`tests/fixtures/proof_kernel/definition_golden_vectors.json`](../tests/fixtures/proof_kernel/definition_golden_vectors.json) and [`tests/fixtures/proof_kernel/definition_invalid_vectors.json`](../tests/fixtures/proof_kernel/definition_invalid_vectors.json) — examples of positive-candidate and negative-vector envelopes.
- `docs/dev/architecture.md` does not exist. The archived architecture copy is not changed by this fixture-only supplement.

## Goals / Non-Goals

**Goals:**

- Define one versioned JSON fixture envelope for Component 1 evidence cases.
- Enumerate exact candidate and negative cases for BEHAVIOR, ABSENCE, and HISTORY.
- Make expected outcomes explicit while separating fixture expectation from any future observed checker result.
- Require one-variable-at-a-time negative variants and stable case IDs.
- Gate BEHAVIOR discharge fixtures on senior review of an admissible BUILD_SOURCE_MAPPING provenance source.
- Keep Component 1 restricted to fixture data, inventory, and fixture-integrity design.

**Non-Goals:**

- Implementing validators, Pydantic premise classes, rules, certificates, checkers, or serializers.
- Claiming a candidate fixture proves real runtime behavior, complete provider coverage, or historical truth.
- Selecting an authoritative BUILD_SOURCE_MAPPING source without owner/senior technical adjudication.
- Writing fixture files, updating a manifest/ledger, adding tests, or changing application behavior in this design step.
- Testing composition, invalidation, publication, or concurrency; those belong to Components 3–4 and G5.

## Decisions

### D1: Use a fixture envelope that separates expectation from observation

**Decision:** Each fixture record carries a stable ID, component/rule family, scenario, expectation class, mapping to requirements, normalized fixture inputs, and a short rationale. `expected` records the adjudicated expectation; `observed` is omitted from the design fixtures and is not filled until a later authorized test run. The fixture corpus therefore cannot accidentally imply that an unimplemented checker passed.

**Fixture envelope (normative JSON shape):**

```json
{
  "schema_version": "g4-evidence-fixture/v1",
  "fixture_id": "G4E-BEH-001",
  "component": "G4-C1",
  "family": "BEHAVIOR",
    "scenario": "valid_candidate_test_only_provenance",
    "maps_to": ["BEHAVIOR", "K16"],
    "expectation": {
      "class": "CANDIDATE_INPUT",
      "layer": "EVIDENCE",
      "invariant": "BUILD_SOURCE_PROVENANCE",
      "reason": "test-only mapping authority approved"
    },
  "inputs": {},
  "rationale": "Fixture is structurally complete; semantic discharge is not asserted here."
}
```

`maps_to` is required in every row of the expected-outcome matrices below. Its exact value is frozen per fixture and must not be inferred by the test harness. `inputs` is exactly one family shape defined in D2. The schema rejects any `observed` property in frozen expectation files. `fixture_id` values are globally unique within the G4 Component 1 inventory. Corpus layout and structural validation are frozen below.

**Expectation vocabulary:** `CANDIDATE_INPUT` means a structurally complete positive candidate, not a proven or accepted certificate. `REJECT_INPUT` means the fixture violates a frozen evidence invariant and must later be rejected. `UNSUPPORTED_INPUT` means evidence is insufficient or outside the declared contract and must not discharge. `OWNER_ADJUDICATION_REQUIRED` means no semantic pass/fail expectation may be assigned until an owner decision resolves the named dependency. `PREMISE_UNAVAILABLE` is reserved for a referenced artifact that is absent or hash-mismatched. Component 2 maps these expectation classes to concrete typed checker outcomes; Component 1 must not invent checker error codes.

**Alternatives considered:** Store expected and observed checker results in the same committed fixture record. Rejected because it risks treating a future test run as the oracle and turns an expectation fixture into an unverifiable result log.

**Rationale:** The project’s current kernel fixture ledger already distinguishes structural facts from semantic claims. Component 1 preserves that boundary and keeps unimplemented checks explicitly pending.

**Trade-offs:** A later authorized harness must keep observed run evidence separately and bind it to fixture IDs and the tested revision.

---

### D2: Freeze family-specific input shapes and one-property negative variants

**Decision:** Use these family input shapes as the required fixture fields. They are design-level JSON shapes, not runtime Pydantic classes. Missing, extra, malformed, or null values are tested only where the case explicitly names that mutation; the default fixture contract rejects unknown fields and distinguishes omitted from explicit null.

**BEHAVIOR input shape:**

```json
{
  "proposition": {
    "form": "OBSERVATION",
    "quantifier": "OBSERVATIONAL",
    "relation": "RUNTIME_OBSERVATION",
    "modality": "RUNTIME",
    "predicate": "returns_status(target)",
    "asserted_value": 200,
    "target": {"symbol": "target", "file_path": "src/service.py"},
    "scope": {
      "repository": "synthetic/service",
      "revision": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      "build_identity": "build-001",
      "execution_id": "exec-001",
      "environment": "test"
    }
  },
  "code_premise": {
    "evidence_id": "ev-code-001",
    "content_hash": "sha256:<64-lowercase-hex>",
    "premise_type": "RANGE_READ_FACT",
    "repository": "synthetic/service",
    "file_path": "src/service.py",
    "revision": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "line_start": 10,
    "line_end": 14,
    "target": "target"
  },
  "runtime_premise": {
    "evidence_id": "ev-runtime-001",
    "content_hash": "sha256:<64-lowercase-hex>",
    "premise_type": "RUNTIME_OBSERVATION_FACT",
    "repository": "synthetic/service",
    "symbol": "target",
    "predicate": "returns_status(target)",
    "observed_value": 200,
    "execution_id": "exec-001",
    "build_identity": "build-001",
    "environment": "test",
    "source_revision": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  },
  "build_source_mapping": {
    "evidence_id": "ev-build-map-001",
    "content_hash": "sha256:<64-lowercase-hex>",
    "premise_type": "BUILD_SOURCE_MAPPING",
    "build_identity": "build-001",
    "repository": "synthetic/service",
    "source_revision": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "provenance_ref": "<owner-approved immutable provenance reference>",
    "mapping_digest": "sha256:<64-lowercase-hex>"
  }
}
```

Amendment A1 (authorized under the Component 2 conditional GO, 2026-10-24): `asserted_value` (canonical scalar `string | integer | boolean | null`, floats invalid) and `mapping_digest` (`sha256:`) are required BEHAVIOR contract fields. The baseline value above (`200`) equals the runtime observed value. This amendment changes fixture data and schema only; it does not approve any provenance source or assert any discharge.

`provenance_ref` is intentionally not assigned a concrete source format here. A senior technical reviewer must identify an admissible, immutable source and specify how the mapping artifact is authenticated, scoped, and independently checked. If that decision is not made, the valid-candidate case remains `OWNER_ADJUDICATION_REQUIRED`, and every behavior case expects non-discharge. A candidate fixture cannot promote a mapping merely because the three tuple fields agree.

**ABSENCE input shape:**

```json
{
  "proposition": {
    "form": "ABSENCE",
    "quantifier": "UNIVERSAL",
    "modality": "STATIC",
    "completeness": "CLOSED_WORLD",
    "predicate": "contains_symbol(target)",
    "target": {"universe": "repository revision path prefix"},
    "scope": {
      "repository": "synthetic/service",
      "revision": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
      "path_scope": "src/"
    }
  },
  "coverage_evidence_id": "ev-coverage-001",
  "coverage_evidence": {
    "premise_type": "ABSENCE_COVERAGE_FACT",
    "universe": "repository revision path prefix",
    "predicate": "contains_symbol(target)",
    "search_status": "COMPLETE_EMPTY",
    "search_config_digest": "sha256:<64-lowercase-hex>",
    "scope": {
      "repository": "synthetic/service",
      "revision": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
      "path_scope": "src/"
    },
    "exclusions": [],
    "coverage_basis": {"kind": "<approved coverage basis>", "value": "<fixture value>"},
    "match_count": 0
  },
  "manifest": {
    "evidence_ids": ["ev-coverage-001"],
    "content_hashes": {"ev-coverage-001": "sha256:<64-lowercase-hex>"},
    "premise_types": {"ev-coverage-001": "ABSENCE_COVERAGE_FACT"},
    "retrieval_config": {"scope": "repository revision path prefix"},
    "exclusions": [],
    "coverage": "COMPLETE_EMPTY"
  }
}
```

The JSON shape includes the coverage record separately from its manifest ID/hash/type binding to avoid self-referential hashing. Exact digest values are generated only when concrete fixture files are authorized; angle-bracket notation above is explanatory schema notation and is not valid fixture data. The coverage-basis value remains subject to Component 2 contract review; Component 1 may freeze only a synthetic basis shape and cannot assert a real provider’s completeness.

**HISTORY input shape:**

```json
{
  "proposition": {
    "form": "HISTORICAL",
    "quantifier": "EXISTENTIAL",
    "modality": "HISTORY",
    "predicate": "changed_at_commit(file, commit)",
    "target": {
      "repository": "synthetic/service",
      "commit_id": "cccccccccccccccccccccccccccccccccccccccc",
      "file_path": "src/service.py",
      "subject": "Update target handling"
    },
    "scope": {"repository_revision": "dddddddddddddddddddddddddddddddddddddddd"}
  },
  "history_premise": {
    "evidence_id": "ev-history-001",
    "content_hash": "sha256:<64-lowercase-hex>",
    "premise_type": "COMMIT_HISTORY",
    "repository": "synthetic/service",
    "repository_revision": "dddddddddddddddddddddddddddddddddddddddd",
    "commit_id": "cccccccccccccccccccccccccccccccccccccccc",
    "file_path": "src/service.py",
    "subject": "Update target handling"
  }
}
```

The HISTORY header omits `relation` and `completeness`. Component 1 pins the present/omitted representation in fixtures; Component 2 owns serializer behavior and must separately prove explicit null rejection and deterministic canonical bytes.

**Alternatives considered:** Use free-form evidence prose with only a type label. Rejected because strings cannot independently express or check repository, revision, target, coverage, and execution identity.

**Rationale:** Explicit fields support mutation-by-one-field test cases and keep evidence-bound identity separate from claim identity.

**Trade-offs:** Some candidate records may remain owner-adjudication-only until Component 2 establishes precise validators and accepted provenance formats.

---

### D3: The BUILD_SOURCE_MAPPING provenance decision gates BEHAVIOR candidate status

**Decision:** Component 1 records the following senior-review question before marking a BEHAVIOR fixture `CANDIDATE_INPUT`: what external or independently attested source binds an immutable `build_identity` to `(repository, source_revision)` for the exact runtime execution? The chosen source must be independently addressable, content-bound in the fixture evidence, and not derived solely from the runtime premise being validated. It must define the trust owner, identity stability, scope, failure behavior, and how a conflicting or unavailable mapping is represented.

If no qualifying source is approved, freeze only BEHAVIOR negative/unsupported fixtures and mark positive candidate expectations `OWNER_ADJUDICATION_REQUIRED`. This preserves testable abstention behavior but does not satisfy BEHAVIOR discharge coverage; the gate owner must explicitly decide whether G4 can pass with that rule unsupported. No fixture may fabricate an accepted map or treat `provenance_ref` as proof by itself.

**Alternatives considered:** Accept mapping tuple equality as sufficient. Rejected because the runtime producer could self-assert the build/source relation. Assume build ID embeds a revision. Rejected because no such encoding contract is established in the current kernel.

**Rationale:** The mapping is the critical trust bridge between static code and an observed execution; without independent provenance, matching values alone do not establish that the observation ran the cited source.

**Trade-offs:** BEHAVIOR candidate coverage is conditional on an owner decision and a viable provenance source. The safe default is explicit unsupported behavior.

---

### D4: Component 1 does not assign checker-specific error codes

**Decision:** Fixture expectations use only the expectation classes in D1 plus a stable invariant identifier. Component 1 records the property being tested and the violated field; Component 2 maps it to `CheckerOutcome` and precise reason strings. For example, a wrong repository can be specified as `REJECT_INPUT` with invariant `REPOSITORY_SCOPE_MATCH`, without prematurely choosing whether implementation maps it to `SCOPE_MISMATCH` or `INVALID_INFERENCE`.

**Alternatives considered:** Pin anticipated checker codes now. Rejected because outcome mapping belongs to rule/checker implementation and may need a typed distinction review.

**Rationale:** Keeps evidence expectation adjudication stable while preserving Component 2 ownership of the executable failure taxonomy.

**Trade-offs:** Component 2 must add a complete mapping from each invariant ID to outcome and diagnostic code before tests are authored.

## Data Storage

No persistent storage or database objects are introduced. The authorized fixture JSON is stored at the frozen paths under `tests/fixtures/proof_kernel/g4_component1/`. The Component 1 test validates this G4 corpus separately; changes to the existing fixture ledger remain out of scope.

## Data Structures

### Common fixture envelope

The fixture envelope in D1 is closed and versioned. Required fields are `schema_version`, `fixture_id`, `component`, `family`, `scenario`, `maps_to`, `expectation`, `inputs`, and `rationale`. The schema forbids an `observed` property. `fixture_id` is unique across all Component 1 files and matches `^G4E-(BEH|ABS|HIS)-[0-9]{3}$`. `maps_to` contains one or more of `BEHAVIOR`, `ABSENCE`, `HISTORY`, `K3`, `K4`, `K9`, `K10`, `K11`, `K16`, or an A–T letter explicitly applicable to the fixture. Every expected-outcome row supplies its exact `maps_to`; no fallback or inferred default is permitted.

`expectation.class` is one of `CANDIDATE_INPUT`, `REJECT_INPUT`, `UNSUPPORTED_INPUT`, `OWNER_ADJUDICATION_REQUIRED`, or `PREMISE_UNAVAILABLE`. `expectation.layer` is `EVIDENCE`; `expectation.invariant` is a stable uppercase snake-case ID; `expectation.reason` is concise, deterministic context. A `CANDIDATE_INPUT` is never equivalent to checker `VALID` or a publication decision.

### Fixture validation ownership

Component 1 owns structural validation of the common fixture envelope and the three family input shapes in this supplement. The validator is test tooling only and checks JSON shape, enums, exact required fields, allowed optional fields, identifier syntax, unique IDs, and family-to-input correspondence. Component 2 owns domain premise Pydantic models and semantic rule/checker validation. Component 1's fixture validator must not duplicate or claim to implement those premise validators; it checks fixture serialization only.

### Frozen fixture file inventory

After authorization, the corpus consists of exactly these files:

- `tests/fixtures/proof_kernel/g4_component1/fixture_schema_v1.json` — JSON Schema Draft 2020-12, root object for the common envelope, closed properties, and discriminated family input variants. The schema validates only fixture structure; it does not assert premise admissibility or checker outcomes.
- `tests/fixtures/proof_kernel/g4_component1/behavior_evidence_v1.json` — common envelopes with BEHAVIOR shape, including mapping-gated positive candidate and explicit abstention/rejection variants.
- `tests/fixtures/proof_kernel/g4_component1/absence_evidence_v1.json` — common envelopes with ABSENCE proposition, coverage record, and manifest binding.
- `tests/fixtures/proof_kernel/g4_component1/history_evidence_v1.json` — common envelopes with HISTORY proposition and full-SHA commit-history premise.

The stable case IDs and expected outcomes are listed in the three expected-outcome matrices in Implementation Detail. Component 1 must not create additional fixtures without adding an expectation row and unique ID here first. The exact structural test file is `tests/unit/test_proof_kernel_g4_component1_evidence.py`. The exact fixture/schema validation command is `uv run python -m pytest -q -p no:cacheprovider tests/unit/test_proof_kernel_g4_component1_evidence.py`; it loads the checked-in JSON Schema and validates every envelope and inventory entry. The exact existing-fixture regression command is `uv run python -m pytest -q -p no:cacheprovider tests/unit/test_proof_kernel_fixture_integrity.py`; it ensures the existing proof-kernel inventory remains intact. The pre-existing `fixture_ledger.json` is not amended by Component 1.

## Interfaces

No user-facing interface, runtime API, CLI, workflow, or database operation is introduced by this supplement. The structural fixture test is `tests/unit/test_proof_kernel_g4_component1_evidence.py` and its exact command is `uv run python -m pytest -q -p no:cacheprovider tests/unit/test_proof_kernel_g4_component1_evidence.py`. Its owner-approved result is recorded in the status block and traceability matrix; it does not authorize Component 2 execution.

## Accessibility

No REST, CLI, MCP, TUI, or GUI surface is introduced. Accessibility requirements are not applicable to fixture design.

## Implementation Detail

This section records the completed Component 1 fixture scope and its bounded exit. It is not permission to add semantic validators, rules, checker logic, or ledger integration.

### Fixture inventory and common expectations

Every fixture must vary one material field from its paired baseline unless the case is explicitly a multi-defect structural-malformation fixture. For each negative case, record the baseline fixture ID, mutated field path, invariant ID, expected expectation class, and why the field affects admissibility. Do not store expected checker codes or observed verdicts.

### BEHAVIOR expected-outcome matrix

| Fixture ID | Scenario / single mutation | Expected class | Invariant | `maps_to` |
|---|---|---|---|---|
| G4E-BEH-001 | Complete code, runtime, and test-only-approved mapping candidate | `CANDIDATE_INPUT` | `BUILD_SOURCE_PROVENANCE` | `BEHAVIOR`, `K16` |
| G4E-BEH-002 | Missing runtime premise | `UNSUPPORTED_INPUT` | `BEHAVIOR_REQUIRES_RUNTIME` | `BEHAVIOR`, `P` |
| G4E-BEH-003 | Missing code range premise | `UNSUPPORTED_INPUT` | `BEHAVIOR_REQUIRES_CODE_RANGE` | `BEHAVIOR`, `P` |
| G4E-BEH-004 | Missing build-source mapping | `UNSUPPORTED_INPUT` | `BUILD_SOURCE_PROVENANCE` | `BEHAVIOR`, `K16` |
| G4E-BEH-005 | Mapping build identity differs from proposition/runtime | `REJECT_INPUT` | `BUILD_ID_MATCH` | `BEHAVIOR`, `N` |
| G4E-BEH-006 | Mapping repository differs from code premise | `REJECT_INPUT` | `REPOSITORY_MATCH` | `BEHAVIOR`, `K11` |
| G4E-BEH-007 | Mapping revision differs from code premise | `REJECT_INPUT` | `SOURCE_REVISION_MATCH` | `BEHAVIOR`, `K11` |
| G4E-BEH-008 | Runtime execution ID differs from proposition scope | `REJECT_INPUT` | `EXECUTION_ID_MATCH` | `BEHAVIOR`, `K11` |
| G4E-BEH-009 | Runtime environment differs from proposition scope | `REJECT_INPUT` | `ENVIRONMENT_MATCH` | `BEHAVIOR`, `K11` |
| G4E-BEH-010 | Runtime predicate differs from proposition | `REJECT_INPUT` | `RUNTIME_PREDICATE_MATCH` | `BEHAVIOR`, `D` |
| G4E-BEH-011 | Runtime value differs from asserted value | `REJECT_INPUT` | `RUNTIME_VALUE_MATCH` | `BEHAVIOR`, `D` |
| G4E-BEH-012 | Runtime artifact is unavailable from the manifest | `PREMISE_UNAVAILABLE` | `RUNTIME_ARTIFACT_AVAILABLE` | `BEHAVIOR`, `K10` |
| G4E-BEH-013 | Runtime artifact hash differs from manifest hash | `PREMISE_UNAVAILABLE` | `RUNTIME_ARTIFACT_HASH_MATCH` | `BEHAVIOR`, `K10` |
| G4E-BEH-014 | Duplicate evidence ID used for code and runtime records | `REJECT_INPUT` | `EVIDENCE_ID_UNIQUENESS` | `BEHAVIOR`, `K14` |
| G4E-BEH-015 | Runtime observed value contains a float | `REJECT_INPUT` | `CANONICAL_SCALAR_ONLY` | `BEHAVIOR`, `K9` |

`G4E-BEH-001` is frozen as `CANDIDATE_INPUT` under Amendment A2 (test-only mapping authority approved 2026-10-24; real-world authority remains OPEN). It may revert to `OWNER_ADJUDICATION_REQUIRED` only through an explicitly reviewed amendment. `G4E-BEH-004` (mapping premise absent) is `UNSUPPORTED_INPUT` per the matrix otherwise-clause recorded at authorization; it does not change without a reviewed supplement amendment.

### ABSENCE expected-outcome matrix

| Fixture ID | Scenario / single mutation | Expected class | Invariant | `maps_to` |
|---|---|---|---|---|
| G4E-ABS-001 | Bounded complete-empty search, matching scope, zero matches, no exclusions | `CANDIDATE_INPUT` | `COMPLETE_EMPTY_WITHIN_UNIVERSE` | `ABSENCE`, `K3` |
| G4E-ABS-002 | Manifest coverage is `INCOMPLETE` | `UNSUPPORTED_INPUT` | `COMPLETE_COVERAGE_REQUIRED` | `ABSENCE`, `K3` |
| G4E-ABS-003 | Search status is `FAILED` | `UNSUPPORTED_INPUT` | `SUCCESSFUL_SEARCH_REQUIRED` | `ABSENCE`, `K3` |
| G4E-ABS-004 | Universe omitted or empty | `UNSUPPORTED_INPUT` | `BOUNDED_UNIVERSE_REQUIRED` | `ABSENCE`, `K3` |
| G4E-ABS-005 | Match count is nonzero | `REJECT_INPUT` | `ZERO_MATCHES_REQUIRED` | `ABSENCE`, `K3` |
| G4E-ABS-006 | Relevant path is excluded from declared universe | `UNSUPPORTED_INPUT` | `RELEVANT_SCOPE_NOT_EXCLUDED` | `ABSENCE`, `K3` |
| G4E-ABS-007 | Coverage scope revision differs from proposition scope | `REJECT_INPUT` | `COVERAGE_REVISION_MATCH` | `ABSENCE`, `K11` |
| G4E-ABS-008 | Coverage repository/path scope differs from proposition | `REJECT_INPUT` | `COVERAGE_SCOPE_MATCH` | `ABSENCE`, `K11` |
| G4E-ABS-009 | Coverage predicate differs from proposition predicate | `REJECT_INPUT` | `COVERAGE_PREDICATE_MATCH` | `ABSENCE`, `D` |
| G4E-ABS-010 | Coverage ID is absent from evidence IDs | `REJECT_INPUT` | `COVERAGE_ID_BOUND` | `ABSENCE`, `K10` |
| G4E-ABS-011 | Coverage content hash mismatches manifest | `PREMISE_UNAVAILABLE` | `COVERAGE_HASH_MATCH` | `ABSENCE`, `K10` |
| G4E-ABS-012 | Coverage premise type is absent or not `ABSENCE_COVERAGE_FACT` | `REJECT_INPUT` | `COVERAGE_TYPE_BOUND` | `ABSENCE`, `K1` |
| G4E-ABS-013 | Search-config digest differs from retrieval-config binding | `REJECT_INPUT` | `SEARCH_CONFIG_MATCH` | `ABSENCE`, `N` |
| G4E-ABS-014 | Manifest exclusions differ from coverage-record exclusions | `REJECT_INPUT` | `EXCLUSIONS_MATCH` | `ABSENCE`, `K3` |
| G4E-ABS-015 | Coverage record is absent but manifest claims complete-empty | `REJECT_INPUT` | `COVERAGE_RECORD_REQUIRED` | `ABSENCE`, `K3` |
| G4E-ABS-016 | Empty premise list without coverage record | `UNSUPPORTED_INPUT` | `EMPTY_IS_NOT_ABSENCE_PROOF` | `ABSENCE`, `K3` |

### HISTORY expected-outcome matrix

| Fixture ID | Scenario / single mutation | Expected class | Invariant | `maps_to` |
|---|---|---|---|---|
| G4E-HIS-001 | Exact repository, queried revision, full commit SHA, file, and subject | `CANDIDATE_INPUT` | `HISTORY_EXACT_BINDING` | `HISTORY`, `K16` |
| G4E-HIS-002 | Repository differs | `REJECT_INPUT` | `HISTORY_REPOSITORY_MATCH` | `HISTORY`, `K11` |
| G4E-HIS-003 | Commit SHA differs by one hex character | `REJECT_INPUT` | `HISTORY_FULL_SHA_MATCH` | `HISTORY`, `K9` |
| G4E-HIS-004 | Commit SHA is abbreviated | `REJECT_INPUT` | `HISTORY_FULL_SHA_REQUIRED` | `HISTORY`, `K9` |
| G4E-HIS-005 | Commit SHA has uppercase hex | `REJECT_INPUT` | `HISTORY_SHA_CANONICAL_CASE` | `HISTORY`, `K9` |
| G4E-HIS-006 | File path differs | `REJECT_INPUT` | `HISTORY_FILE_MATCH` | `HISTORY`, `D` |
| G4E-HIS-007 | Subject differs | `REJECT_INPUT` | `HISTORY_SUBJECT_MATCH` | `HISTORY`, `D` |
| G4E-HIS-008 | Queried repository revision differs from proposition context | `REJECT_INPUT` | `HISTORY_QUERY_REVISION_MATCH` | `HISTORY`, `K11` |
| G4E-HIS-009 | History artifact unavailable | `PREMISE_UNAVAILABLE` | `HISTORY_ARTIFACT_AVAILABLE` | `HISTORY`, `K10` |
| G4E-HIS-010 | History artifact content hash mismatch | `PREMISE_UNAVAILABLE` | `HISTORY_ARTIFACT_HASH_MATCH` | `HISTORY`, `K10` |
| G4E-HIS-011 | Branch name or time range supplied instead of full commit ID | `UNSUPPORTED_INPUT` | `HISTORY_REQUIRES_COMMIT_ID` | `HISTORY`, `K7` |
| G4E-HIS-012 | Claim asks for causal effect inferred from commit subject | `UNSUPPORTED_INPUT` | `HISTORY_DOES_NOT_PROVE_CAUSATION` | `HISTORY`, `A` |
| G4E-HIS-013 | Relation and completeness omitted | `CANDIDATE_INPUT` | `HISTORY_OPTIONAL_HEADER_FIELDS_OMITTED` | `HISTORY`, `K16` |

Expected classes above are fixture-level expectations, not checker verdicts. For a candidate case, Component 2 later determines whether the premise evidence actually discharges the named rule. Negative fixtures may require more than one lower-level rejection (e.g., syntax and semantic checks); `invariant` identifies the intended property and does not prescribe a reason-code precedence order.

## Migrations

No database migration is applicable. No fixture schema or data migration is performed as part of authoring this supplement. If Component 1 is authorized, it creates only the new G4 fixture namespace and then proposes an explicit integrity-ledger integration change for review; existing `cert/v1` artifacts and fixture ledgers are not silently modified here.

## Testing Philosophy

### Fixture structure and inventory

After authorization, validate every fixture against the closed common envelope and one family-specific input schema in `tests/fixtures/proof_kernel/g4_component1/fixture_schema_v1.json`. Reject duplicate IDs, unknown fields, missing fixture references, orphaned files, invalid hash syntax, malformed full-SHA values, and expectation classes outside the closed enum. Structural validity does not claim semantic acceptance. Component 1's JSON Schema is the source of truth for serialized fixture shape; Component 2's Pydantic premise models remain the source of truth for normalized runtime premise validation. Execute `uv run python -m pytest -q -p no:cacheprovider tests/unit/test_proof_kernel_g4_component1_evidence.py` for the Component 1 structural gate, then `uv run python -m pytest -q -p no:cacheprovider tests/unit/test_proof_kernel_fixture_integrity.py` as the existing-fixture regression gate.

### One-field mutation discipline

Each negative fixture must identify its baseline and mutate one field or one explicit structural condition. Verify that the expected invariant changes while all unrelated values remain byte-identical. If one fixture intentionally contains multiple defects, label it `multi_defect`, list each defect, and do not use it as sole evidence for any individual invariant.

### BUILD_SOURCE_MAPPING provenance review

No BEHAVIOR fixture beyond G4E-BEH-001 is designated `CANDIDATE_INPUT`. BEH-001 holds that status only under Amendment A2's test-only authority (synthetic vectors; real-world authority OPEN). Prior to approval, all positive-candidate cases were frozen as pending; a fixture containing a syntactically complete but unapproved mapping remains `OWNER_ADJUDICATION_REQUIRED` and is excluded from any positive-candidate count.

### Expected versus observed outcomes

Fixture files pin only expectation class, invariant, and rationale. A later authorized test harness stores actual observed outcomes separately and joins by `fixture_id`, tested revision, and harness version. No `CANDIDATE_INPUT` value is counted as a checker pass, false-accept rate, or proof of provider quality.

## Documentation Plan

### `docs/IAP-proof-kernel-g4-component-1-design-v1.0.md`

**Audience:** Gate owner, senior technical reviewer, and Component 1 fixture authors. This supplement freezes fixture paths, JSON Schema location, validation ownership, commands, scenario IDs, exact mappings, expected-outcome classes, and provenance-review gates. Component implementation remains blocked on separate owner authorization.

### `docs/IAP-proof-kernel-g4-phase-design-v1.0.md`

**Audience:** Kernel implementers and gate reviewers. Keep the parent phase plan aligned with this supplement’s fixture IDs, Component 1 scope, and BUILD_SOURCE_MAPPING senior-review prerequisite if the design is approved.

### `tests/fixtures/proof_kernel/`

**Audience:** Test authors and reviewers. No files are created by this supplement. After authorization, add the three family fixture files and schema at the frozen paths in Data Structures, then validate them with the exact commands recorded there.

### `docs/archive/dev/architecture.md` changes

No changes are required. This supplement defines test inputs only and changes no system architecture, data flow, authority, or deployment. No `docs/dev/architecture.md` exists in the project.

## Risks / Trade-offs

### BUILD_SOURCE_MAPPING lacks an approved evidence authority

**Risk:** A syntactically valid build/revision tuple could be self-asserted by the runtime record, making the code/runtime join circular and unsound.

**Mitigation:** Require a senior technical review of an independently addressable provenance source before any positive candidate fixture is classified as such. Keep BEHAVIOR unsupported and escalate the G4 gate disposition if no acceptable source is available.

### Fixture expectations are mistaken for checker results

**Risk:** A `CANDIDATE_INPUT` may be reported as a passing proof before Component 2 implements and independently checks it.

**Mitigation:** Keep expected and observed fields separate, use fixture expectation classes only, and prohibit candidate counts from entering gate pass metrics.

### Provider coverage is assumed from an empty result

**Risk:** A `COMPLETE_EMPTY` sample may be taken to establish completeness beyond the fixture’s declared universe or provider contract.

**Mitigation:** Bind every absence candidate to a bounded synthetic universe, config digest, explicit exclusions, and zero-match count. Component 2 must separately validate the declared contract; these fixtures make no population claim.
