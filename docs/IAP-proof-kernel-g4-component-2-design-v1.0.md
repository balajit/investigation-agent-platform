# Design: Proof-Kernel G4 Component 2 — Typed Rules and Independent Checker v1.0

> Status: draft supplement for separate owner authorization. Component 1 fixture-level exit was signed on 2026-10-24. This document is a code-level design for review only; it authorizes no implementation, rule code, certificate migration, test-file creation, production wiring, database migration, rollout, or authority change.
> Scope: define strict premise Pydantic models, BEHAVIOR/ABSENCE/HISTORY producer validators, independent checker semantics, and the direct `cert/v2` plus `rules/v2` migration required for the rule-level G4 component.

## Context

Component 1 has frozen 44 JSON evidence fixtures and its two fixture-level gates are green: 4 structural tests and 14 existing-fixture integrity tests. Those fixtures validate serialized evidence shape only. They do not construct runtime premise models, prove semantic admissibility, or establish checker outcomes.

The current proof kernel has two registered rules, `definition/v1` and `call_edge/v1`, accepts dictionary-shaped premise artifacts, uses `rules/v1` and `cert/v1`, and routes the current checker through the producer's `apply_rule()` validators. Component 2 replaces that incomplete boundary with strict immutable premise records, three additional versioned rule contracts, rule-specific header validation, typed coverage binding, a separate checker semantic implementation, and a one-way in-repository version migration. Existing DEFINITION and CALL_EDGE behavior must remain pinned by migrated tests.

Component 2 remains non-authoritative. A checker result is a kernel result, not publication authorization. G5 persistence/atomicity, production consumers, live-provider provenance, and rollout remain outside this supplement.

## References

- [`docs/IAP-proof-kernel-g4-phase-design-v1.1.md`](IAP-proof-kernel-g4-phase-design-v1.1.md) — normative G4 sequencing, rule contracts, checker independence, version migration, and Component 2 entry/exit criteria (authoritative parent; v1.0 retained).
- [`docs/IAP-proof-kernel-g4-component-1-design-v1.0.md`](IAP-proof-kernel-g4-component-1-design-v1.0.md) — signed Component 1 fixture corpus, expected classes, mutation paths, and fixture-level boundary.
- [`docs/IAP-proof-kernel-traceability-v1.0.md`](IAP-proof-kernel-traceability-v1.0.md) — current G4 partial disposition and the recorded Component 1 exit.
- [`docs/IAP-proof-kernel-contracts-v1.0.md`](IAP-proof-kernel-contracts-v1.0.md) — certificate, manifest, typed outcome, coverage, and composition contracts.
- [`src/investigation_agent_platform/domain/proof/rules.py`](../src/investigation_agent_platform/domain/proof/rules.py) — current closed registry and producer validator boundary to migrate.
- [`src/investigation_agent_platform/domain/proof/certificate.py`](../src/investigation_agent_platform/domain/proof/certificate.py) — current certificate/checker path to split and migrate.
- [`src/investigation_agent_platform/domain/proof/manifest.py`](../src/investigation_agent_platform/domain/proof/manifest.py) — current manifest shape that needs typed premise and absence coverage extensions.
- [`src/investigation_agent_platform/domain/proof/evaluation.py`](../src/investigation_agent_platform/domain/proof/evaluation.py) — current context derivation that must bind normalized proposition scope in v2.
- [`tests/unit/test_proof_kernel_g4_component1_evidence.py`](../tests/unit/test_proof_kernel_g4_component1_evidence.py) — structural-only Component 1 gate; it must remain separate from semantic rule tests.
- `docs/dev/architecture.md` does not exist in the project. No architecture document is changed by this pure-domain supplement.

## Goals / Non-Goals

**Goals:**

- Define closed, immutable Pydantic v2 premise models for all six declared premise types.
- Define exact producer rule contracts for `behavior/v1`, `absence/v1`, and `history/v1`.
- Define independent semantic checker functions that do not invoke producer validators or trust producer outcomes.
- Migrate maintained kernel inputs directly from `cert/v1`/`rules/v1` to `cert/v2`/`rules/v2` without a permanent compatibility reader.
- Make form, quantifier, target, scope, coverage, manifest, artifact, and canonicalization checks explicit and fail closed.
- Provide exact tests, mutation detectors, source-fingerprint controls, and review evidence for a separate Component 2 exit.
- Keep BEHAVIOR non-discharging unless an independently attested build-to-source mapping contract is approved and represented in the typed evidence.

**Non-Goals:**

- Implementing any rule or checker code under this design-only step.
- Adding a production caller, publication writer, feature flag, workflow behavior, or authority path.
- Adding a database table or migration, persisting premise records, or dual-reading old certificates.
- Inferring `BUILD_SOURCE_MAPPING` from matching strings, timestamps, runtime self-assertion, or hash equality alone.
- Treating a finite fixture suite as population-level provider completeness or universal correctness.
- Implementing CAUSAL, obligation persistence, solver witnesses, composition properties, or Component 3/4 work.

## Decisions

### D1: Use strict immutable Pydantic premise records as the semantic boundary

**Decision:** Add `src/investigation_agent_platform/domain/proof/premises.py` with a closed immutable model hierarchy. Every model uses `ConfigDict(frozen=True, extra="forbid", strict=True)`, literal premise tags, non-empty identity fields, `sha256:` digest formats, and explicit omission/null rules. Models are normalized before rule application and serialized to canonical artifact bytes without hidden coercion.

```python
# src/investigation_agent_platform/domain/proof/premises.py
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field

Sha256 = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
FullSha = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
NonEmpty = Annotated[str, Field(min_length=1)]


class PremiseBase(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)
    evidence_id: NonEmpty
    content_hash: Sha256
    premise_type: str


class RangeReadFact(PremiseBase):
    premise_type: Literal["RANGE_READ_FACT"]
    repository: NonEmpty
    file_path: NonEmpty
    revision: NonEmpty
    line_start: Annotated[int, Field(gt=0)]
    line_end: Annotated[int, Field(gt=0)]
    target: NonEmpty
    window_text: NonEmpty


class NormalizedCallsiteFact(PremiseBase):
    premise_type: Literal["NORMALIZED_CALLSITE_FACT"]
    repository: NonEmpty
    file_path: NonEmpty
    revision: NonEmpty
    line_start: Annotated[int, Field(gt=0)]
    line_end: Annotated[int, Field(gt=0)]
    caller: NonEmpty
    callee: NonEmpty
    call_text: NonEmpty


class BehaviorScope(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)
    repository: NonEmpty
    revision: FullSha
    build_identity: NonEmpty
    execution_id: NonEmpty
    environment: NonEmpty


class RuntimeObservationFact(PremiseBase):
    premise_type: Literal["RUNTIME_OBSERVATION_FACT"]
    repository: NonEmpty
    symbol: NonEmpty
    predicate: NonEmpty
    observed_value: str | int | bool | None
    execution_id: NonEmpty
    build_identity: NonEmpty
    environment: NonEmpty
    source_revision: FullSha


class BuildSourceMapping(PremiseBase):
    premise_type: Literal["BUILD_SOURCE_MAPPING"]
    build_identity: NonEmpty
    repository: NonEmpty
    source_revision: FullSha
    provenance_ref: NonEmpty
    mapping_digest: Sha256


class AbsenceScope(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)
    repository: NonEmpty
    revision: FullSha
    path_scope: NonEmpty


class CoverageBasis(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)
    kind: NonEmpty
    value: NonEmpty


class AbsenceCoverageFact(PremiseBase):
    premise_type: Literal["ABSENCE_COVERAGE_FACT"]
    universe: NonEmpty
    predicate: NonEmpty
    search_status: Literal["COMPLETE_EMPTY", "MATCHES_FOUND", "INCOMPLETE", "FAILED"]
    search_config_digest: Sha256
    scope: AbsenceScope
    exclusions: list[NonEmpty]
    coverage_basis: CoverageBasis
    match_count: Annotated[int, Field(ge=0)]


class CommitHistory(PremiseBase):
    premise_type: Literal["COMMIT_HISTORY"]
    repository: NonEmpty
    repository_revision: FullSha
    commit_id: FullSha
    file_path: NonEmpty
    subject: NonEmpty
```

The implementation must add a model-level check that `line_start <= line_end`, reject floats in `observed_value` even though Python `bool` is an `int` subtype, and require exact closed keys for every nested scope and coverage model. `BehaviorScope`, `AbsenceScope`, and `CoverageBasis` above are closed models, not open dictionaries. Any normalized artifact envelope also uses a named closed model or an explicitly reviewed `extra="forbid"` model. `RuntimeObservationFact` uses a proposition-level `observed_value` field selected by the contract amendment described in D2; Component 2 must not infer an asserted value from free-form predicate text.

**Alternative considered:** Continue passing dictionaries and validate fields inside each rule. Rejected because dictionary validators permit accidental coercion, unknown fields, inconsistent null behavior, and unbound artifact shapes.

**Rationale:** Pydantic provides the project-standard typed boundary while `strict=True`, `extra="forbid"`, and frozen models make malformed evidence fail before rule semantics run.

**Trade-offs:** The model layer duplicates some JSON Schema shape checks. That duplication is intentional: Component 1 protects fixture serialization, while Component 2 protects runtime semantic inputs. The two layers must be tested for agreement but must not be treated as one validator.

---

### D2: Resolve BEHAVIOR value and mapping authority before any discharge

**Decision:** Amend the Component 1 BEHAVIOR contract before implementation authorization with an explicit proposition `asserted_value` field whose type is the canonical scalar union `string | integer | boolean | null`; floats are invalid. `behavior/v1` compares proposition predicate and asserted value exactly to the runtime premise predicate and observed value. The proposition scope must exactly match code repository/revision, runtime repository/build/execution/environment/source revision, and the mapping tuple.

`BUILD_SOURCE_MAPPING` admits only an approved independent provenance authority. Real-world mapping authority remains OPEN (G5/G7 or operations item); no production build attestation is approved. A test-only synthetic authority was approved 2026-10-24 (Amendment A2): the frozen `provenance_ref` + `mapping_digest` contract admits Component 2 synthetic vectors only, with trust assumptions documented as fictional. The admitted record must carry `provenance_ref` and `mapping_digest`; the provenance reference must identify an immutable, independently addressable attestation whose trust owner, authentication, build identity, repository, source revision, scope, mismatch behavior, and unavailable behavior are documented. Runtime observation content cannot be the sole source of that attestation. If no approved authority covers a candidate, the producer returns `UNSUPPORTED_CONSTRUCT` and the checker returns the same typed non-discharge outcome for every BEHAVIOR candidate.

The Component 1 fixture currently has no explicit asserted value in its proposition and uses `provenance_ref` without `mapping_digest`. This is a pre-implementation contract amendment, not an implementation choice hidden in rule code. The owner must approve the amended fixture contract and mapping authority separately before BEHAVIOR positive tests can be classified as discharging.

**Alternative considered:** Treat `predicate` text as the asserted value carrier or accept matching build/revision tuple fields as sufficient. Rejected because prose parsing is ambiguous and tuple equality permits runtime self-assertion.

**Rationale:** A behavior proof joins static source to an exact execution; the asserted scalar and independent mapping are separate trust requirements.

**Trade-offs:** BEHAVIOR may remain explicitly unsupported after Component 2 if no admissible mapping authority is approved. That is an honest gate result, not a reason to weaken the rule.

---

### D3: Make ABSENCE a typed coverage proof, never an empty-premise proof

**Decision:** Extend the frozen manifest contract with `premise_types`, `coverage_evidence_id`, and `coverage_evidence`. `absence/v1` requires exactly one `AbsenceCoverageFact`, its ID in all manifest maps, `search_status=COMPLETE_EMPTY`, `match_count=0`, a non-empty bounded universe, matching predicate and repository/revision/path scope, explicit exclusions, matching search-config digest, and an approved non-empty `coverage_basis`. An empty evidence list, an incomplete/failed search, a nonzero match count, or relevant scope excluded from the universe never discharges absence.

The checker independently reconstructs and validates the typed coverage artifact. `manifest.coverage` remains the coarse retrieval result, while `coverage_evidence` is the semantic proof premise. `COMPLETE_EMPTY` maps to certificate `COMPLETE_PATH` only after the typed coverage checks pass.

**Alternative considered:** Reuse the generic certificate coverage block or accept zero search rows as absence. Rejected because neither proves what universe was searched or whether retrieval succeeded.

**Rationale:** Absence is a bounded universal claim. Coverage is part of the proof, not metadata about an empty result.

**Trade-offs:** The manifest becomes more explicit and larger. The added fields are required for soundness and are not optional compatibility fields.

---

### D4: HISTORY matches exact immutable history identity and preserves omission semantics

**Decision:** `history/v1` requires exactly one `CommitHistory` record. Repository, full lowercase commit ID, file path, subject, and queried repository revision must exactly match the proposition target and scope. Abbreviated SHAs, uppercase SHAs, branch names, time ranges, file mentions without commit identity, and causal interpretations return typed unsupported or invalid outcomes according to the explicit precedence contract below.

HISTORY proposition headers omit `relation` and `completeness`. Omission is valid; explicit `null` is invalid; an unexpected non-null value is unsupported. Producer serialization and independent checker parsing use the same byte-level vectors but separate semantic code. No serializer may convert explicit null into omission.

**Alternative considered:** Normalize optional header nulls to omission. Rejected because null and omission carry different contract meanings and normalization would conceal malformed callers.

**Rationale:** Full commit identity and exact subject binding establish a historical fact without laundering it into a causal claim.

**Trade-offs:** Upstream callers that supplied abbreviated history locators must resolve them before entering the kernel. The kernel does not perform repository I/O.

### D4a: Freeze first-failure outcome precedence

**Decision:** Producer and checker functions use the following ordered precedence. They stop at the first applicable failure and return the listed typed outcome; later checks must not overwrite an earlier diagnosis. `CHECKER_FAILURE` is reserved for malformed checker inputs or unexpected checker exceptions, while unavailable artifacts are `PREMISE_UNAVAILABLE`.

| Order | Condition | Producer outcome | Checker outcome |
|---:|---|---|---|
| 1 | Request, proposition, certificate, or typed record cannot be parsed under the closed schema | `INADMISSIBLE_PREMISE` | `CHECKER_FAILURE` for checker-envelope/schema failure; `INADMISSIBLE_PREMISE` for a parsed premise contract violation |
| 2 | Required premise ID, manifest key, coverage record, or artifact is absent | `MISSING_PREMISE` | `MISSING_PREMISE` when the certificate is structurally complete but the required proof input is absent |
| 3 | Artifact bytes are unavailable or do not match the declared content hash | Rule layer reports missing bindings as `MISSING_PREMISE` and mismatched bytes as `INADMISSIBLE_PREMISE` (the frozen producer vocabulary has no unavailability member); `certify()` and the checker report the same conditions as `PREMISE_UNAVAILABLE` | `PREMISE_UNAVAILABLE` |
| 4 | Premise type, literal tag, scalar, digest, line range, or canonical field shape is invalid | `INADMISSIBLE_PREMISE` | `INADMISSIBLE_PREMISE` |
| 5 | Rule header form, quantifier, relation, modality, or completeness is not an admitted combination | `UNSUPPORTED_CONSTRUCT` | `UNSUPPORTED_CONSTRUCT` |
| 6 | Rule-specific cardinality, target, value, predicate, repository, revision, build, execution, environment, coverage, or history binding does not match | `INVALID_INFERENCE` | `INVALID_INFERENCE` |
| 7 | A required scope equality fails across proposition, premise, manifest, or mapping | `INVALID_INFERENCE` (producer `RuleOutcome` has no scope-specific member) | `SCOPE_MISMATCH` |
| 8 | A required external authority is absent or the declared construct is outside the approved rule contract | `UNSUPPORTED_CONSTRUCT` | `UNSUPPORTED_CONSTRUCT` |
| 9 | Certificate context is stale or derivation dependencies contain a cycle | not applicable to producer rule application | `STALE_CERTIFICATE` or `INVALID_INFERENCE` |

For HISTORY specifically, after closed parsing and artifact availability, evaluate in this order: exact header combination; exactly one `COMMIT_HISTORY`; full lowercase commit syntax; repository equality; queried repository-revision equality; commit equality; file-path equality; subject equality; causal/unsupported interpretation rejection; then discharge. A malformed optional header is handled at order 1 or 4 and is never normalized into omission. This table is normative for diagnostic precedence and is covered by Component 2 vectors.

**Alternative considered:** Let each validator choose its own check order. Rejected because equivalent mutations would produce unstable diagnostics and differential tests could pass for the wrong reason.

**Rationale:** A fixed first-failure order makes rejection attribution deterministic and prevents a later semantic check from masking unavailable or malformed evidence.

**Trade-offs:** Some combined-defect inputs report the earliest contract failure rather than every failure. The mutation suite supplies one-defect cases for each invariant.

---

### D5: Register rules under rules/v2 with explicit contracts and dependencies

**Decision:** Set `RULE_BUNDLE_VERSION = "rules/v2"` and retain existing rule IDs at `definition/v1` and `call_edge/v1` while adding exactly `behavior/v1`, `absence/v1`, and `history/v1`. Every registry row declares exact premise cardinality, input types, header contract, binding projection, scope fields, assumptions, soundness, unsupported cases, dependency classes, and validator qualified name. `CAUSAL` remains unregistered.

| Rule | Required premises | Header | Soundness | Key dependencies |
|---|---|---|---|---|
| `definition/v1` | exactly one `RANGE_READ_FACT` | EXISTENCE / EXISTENTIAL | SOUND | evidence content, availability, revision, rule bundle |
| `call_edge/v1` | one or more `NORMALIZED_CALLSITE_FACT` | EXISTENCE / EXISTENTIAL | SOUND | evidence content, availability, revision, rule bundle |
| `behavior/v1` | one `RANGE_READ_FACT`, one `RUNTIME_OBSERVATION_FACT`, one `BUILD_SOURCE_MAPPING` | OBSERVATION / OBSERVATIONAL / RUNTIME_OBSERVATION / RUNTIME | SOUND | evidence content/availability, repository, revision, execution, environment, build identity, rule bundle |
| `absence/v1` | exactly one `ABSENCE_COVERAGE_FACT`, no match-result premises | ABSENCE / UNIVERSAL / STATIC / CLOSED_WORLD | SOUND within declared universe | evidence content/availability, repository, revision, path scope, universe, exclusions, search config, rule bundle |
| `history/v1` | exactly one `COMMIT_HISTORY` | HISTORICAL / EXISTENTIAL / HISTORY | SOUND for exact history fact | evidence content/availability, repository, queried revision, commit, file, subject, rule bundle |

Producer validators may return `DISCHARGED`, `MISSING_PREMISE`, `INADMISSIBLE_PREMISE`, `INVALID_INFERENCE`, or `UNSUPPORTED_CONSTRUCT`. They never return a publishable result by label alone. Unknown rule IDs and unlisted header combinations fail closed.

**Alternative considered:** Reinterpret `rules/v1` to include new rules or add a second registry fallback. Rejected by the repository's no-legacy-logic rule and because semantic versioning must bind certificate and rule-bundle identity.

**Rationale:** The registry is the closed authority for what the kernel can evaluate; explicit dependencies make future invalidation reviewable.

**Trade-offs:** Existing test fixtures and call sites require a direct migration. There is no permanent dual-version reader.

---

### D6: Implement checker semantics independently from producer validators

**Decision:** Add `src/investigation_agent_platform/domain/proof/checker_semantics.py` containing separate functions: `check_definition_semantics`, `check_call_edge_semantics`, `check_behavior_semantics`, `check_absence_semantics`, and `check_history_semantics`. Move the closed `CheckerOutcome` enum to `src/investigation_agent_platform/domain/proof/outcomes.py` so `certificate.py` can call checker semantics without an import cycle, then re-export that enum from `certificate.py` as a compatibility surface. This preserves the eight current importer paths (`certificate.py`, `composition.py`, `publication.py`, `application/proof/publication_commit_service.py`, and four unit tests) without importer churn; the application publication service's behavior remains unchanged. `certificate.py` must not import or invoke `apply_rule`, `_VALIDATORS`, or any producer validator. The checker treats `validation`, `proof_classification`, producer outcomes, and producer reasons as untrusted fields.

```python
# src/investigation_agent_platform/domain/proof/checker_semantics.py
from collections.abc import Mapping
from typing import Any

from investigation_agent_platform.domain.proof.outcomes import CheckerOutcome


def check_behavior_semantics(
    proposition: Mapping[str, Any],
    premises: list[Mapping[str, Any]],
    artifacts: Mapping[str, Mapping[str, Any]],
    mapping_authority: Mapping[str, Any] | None,
) -> tuple[CheckerOutcome, str]:
    """Independently check behavior joins; never call producer validators."""
    if mapping_authority is None:
        return CheckerOutcome.UNSUPPORTED_CONSTRUCT, "BUILD_SOURCE_PROVENANCE unavailable"
    # Complete semantic checks are specified below and are authorized only
    # after this supplement is approved; this excerpt shows the boundary.
    return CheckerOutcome.UNSUPPORTED_CONSTRUCT, "mapping authority not approved"
```

The snippet shows the required module boundary, not authorized implementation. The final implementation must be complete, pure, and contain no `NotImplementedError`. Shared trusted primitives are limited to canonical JSON bytes, digest recomputation, Pydantic parsing schemas, and closed enums. Semantic admissibility, cardinality, header gates, target/scope joins, coverage, and outcome precedence are checker-owned logic.

### D6a: Freeze ordered producer and checker check sequences

**Decision:** The implementation must follow these ordered sequences. Each step is a distinct named check; a failure returns immediately using D4a precedence. The sequences are the minimum prose-precise implementation contract for the three producer validators and five checker semantic functions.

**Producer `validate_behavior_premises`:**

1. Parse the proposition header and require `OBSERVATION`, `OBSERVATIONAL`, `RUNTIME_OBSERVATION`, and `RUNTIME`, plus target, scope, predicate, and `asserted_value`.
2. Require exactly three premises with one each of `RANGE_READ_FACT`, `RUNTIME_OBSERVATION_FACT`, and `BUILD_SOURCE_MAPPING`; reject duplicate evidence IDs and extra types.
3. Parse every premise through its strict Pydantic model and verify artifact availability/content hashes against the manifest.
4. Require code target and file path to equal the proposition target and code repository/revision to equal proposition scope.
5. Require runtime predicate/value/symbol and repository/source revision/build identity/execution ID/environment to equal the proposition.
6. Require mapping build identity/repository/source revision to equal the code and runtime join, then require approved independent provenance authority and mapping digest.
7. Return `DISCHARGED`; otherwise return the first typed failure.

**Producer `validate_absence_premises`:**

1. Parse the proposition header and require `ABSENCE`, `UNIVERSAL`, `STATIC`, `CLOSED_WORLD`, predicate, universe, and repository/revision/path scope.
2. Require exactly one `ABSENCE_COVERAGE_FACT`, no match-result premise, and a non-null coverage record and ID.
3. Parse the coverage model and require manifest ID/hash/type key sets to match the coverage artifact.
4. Require `COMPLETE_EMPTY`, `match_count == 0`, non-empty universe, non-empty coverage basis, and explicit exclusions.
5. Require coverage predicate, universe, repository, revision, path scope, exclusions, and search-config digest to match proposition and manifest retrieval configuration.
6. Reject relevant scope exclusions and any unbounded, incomplete, failed, or nonzero-match search.
7. Return `DISCHARGED`; otherwise return the first typed failure.

**Producer `validate_history_premises`:**

1. Parse the proposition with `HISTORICAL`, `EXISTENTIAL`, and `HISTORY`; require relation and completeness to be omitted, not null.
2. Require exactly one `COMMIT_HISTORY` premise and reject extra premise types or duplicate IDs.
3. Parse the premise and verify artifact availability/content hash and manifest type binding.
4. Require full lowercase 40-hex commit IDs in proposition and premise.
5. Compare repository, queried repository revision, commit ID, file path, and subject exactly.
6. Reject branch/time-range locators and causal interpretations as unsupported.
7. Return `DISCHARGED`; otherwise return the first typed failure.

**Checker `check_definition_semantics`:**

1. Parse the certificate/proposition boundary and require the exact DEFINITION header combination.
2. Recompute premise IDs, content hashes, manifest bindings, and exact one-premise cardinality.
3. Parse the artifact as `RANGE_READ_FACT`, verify file/revision scope, and require the asserted symbol in the cited range text.
4. Recompute the rule binding and compare it byte-for-byte with the certificate inference instance.
5. Return `VALID` or the first typed failure; ignore producer validation fields.

**Checker `check_call_edge_semantics`:**

1. Parse the certificate/proposition boundary and require the exact CALL_EDGE header combination.
2. Recompute IDs, hashes, manifest bindings, and require one or more call-site premises with no duplicate IDs.
3. Parse every premise as `NORMALIZED_CALLSITE_FACT`, verify revision scope, and reject relabeled search/comment rows.
4. Require at least one cited call text to show the exact asserted caller and callee.
5. Recompute bindings and derivation identity, then return `VALID` or the first typed failure.

**Checker `check_behavior_semantics`:**

1. Validate v2 schema/version, exact rule bundle, exact behavior header, and certificate/proposition field parity.
2. Recompute proposition/context/manifest identities including `proposition_scope` and verify expected context freshness.
3. Require exactly the three permitted premise types, unique IDs, manifest key-set equality, available artifacts, and content-hash equality.
4. Independently parse code, runtime, and mapping artifacts through closed models; reject floats, unknown nested keys, malformed ranges, and unapproved provenance.
5. Independently compare target, predicate, asserted value, repository, revision, build, execution, environment, and mapping tuple.
6. Verify inference bindings, derivation identity, dependency acyclicity, and non-staleness.
7. Return `VALID` or the first typed failure; never call producer validators.

**Checker `check_absence_semantics`:**

1. Validate v2 schema/version, exact rule bundle, exact absence header, and certificate/proposition field parity.
2. Recompute proposition/context/manifest identities and freshness.
3. Require exactly one typed coverage premise, matching manifest ID/hash/type maps, available artifact bytes, and hash equality.
4. Independently validate complete-empty status, zero matches, bounded universe, predicate/scope/config/exclusion agreement, and coverage basis.
5. Reject relevant exclusions, extra premise types, empty premise lists, incomplete/failed searches, and nonzero matches.
6. Verify bindings, derivation identity, and acyclicity, then return `VALID` or the first typed failure.

**Checker `check_history_semantics`:**

1. Validate v2 schema/version, exact rule bundle, exact history header, and absence of relation/completeness keys.
2. Recompute proposition/context/manifest identities and freshness.
3. Require exactly one typed history premise, available artifact bytes, hash equality, and manifest binding.
4. Independently validate full lowercase SHA syntax and compare repository, queried revision, commit, file, and subject.
5. Reject branch/time-range and causal interpretations.
6. Verify bindings, derivation identity, and acyclicity, then return `VALID` or the first typed failure.

These sequences are covered by one-defect vectors and producer/checker differential tests. Reordering a sequence is a contract change requiring a reviewed supplement amendment and version decision.

**Alternative considered:** Call the producer validator from a second public checker function. Rejected because a separate call path does not detect correlated semantic defects and is not independent assurance.

**Rationale:** Independent semantic code is the G4-3 trust-boundary requirement.

**Trade-offs:** Some semantic logic is intentionally duplicated. Differential goldens and source fingerprints control drift; shared code is not allowed to erase independence.

---

### D7: Migrate directly to cert/v2 and reject cert/v1

**Decision:** Set `CERT_SCHEMA_VERSION = "cert/v2"`; require `CheckRequest.rule_bundle_version` and `EvaluationInputs.rule_bundle_version` to be explicitly supplied as `rules/v2`; remove empty-string defaults. Add `proposition_scope` to the context preimage and pin it in context goldens. `cert/v2.canonical_proposition` includes the rule-approved header fields, while top-level certificate `scope` equals the normalized proposition scope. The checker derives and compares bindings byte-for-byte.

The direct migration inventory is:

- `src/investigation_agent_platform/domain/proof/certificate.py`: schema version, v2 header/coverage/manifest checks, explicit bundle version, independent semantic calls, and `cert/v1` rejection.
- `src/investigation_agent_platform/domain/proof/outcomes.py`: shared closed checker-outcome enum with no producer-validator dependency; `certificate.py` re-exports it for the current importers.
- `src/investigation_agent_platform/domain/proof/premises.py`: strict immutable premise models and nested closed scope/coverage models.
- `src/investigation_agent_platform/domain/proof/checker_semantics.py`: independent semantic checker functions with no producer-validator dependency.
- `src/investigation_agent_platform/domain/proof/certificate.py`: compatibility re-export of `CheckerOutcome`; current domain/application/test imports remain valid and `publication_commit_service.py` behavior is unchanged.
- `src/investigation_agent_platform/domain/proof/rules.py`: registry rows, `rules/v2`, typed premise intake, and new rule dispatch.
- `src/investigation_agent_platform/domain/proof/manifest.py`: `premise_types`, typed coverage fields, exact key-set binding, and v2 digest vectors.
- `src/investigation_agent_platform/domain/proof/evaluation.py`: required bundle version and normalized proposition scope in context identity.
- `src/investigation_agent_platform/domain/proof/canonical.py`: v2 target allowlists and rule-specific proposition/scope projection without silent field loss.
- `tests/fixtures/proof_kernel/cert_schema_v2.json`: new closed certificate schema; no v1 schema acceptance.
- `tests/fixtures/proof_kernel/*`: migrate maintained certificate fixtures and expected bundle values directly to v2.
- `tests/unit/test_typed_rules.py`, `tests/unit/test_certificate_checker.py`, `tests/unit/test_manifest_evaluation.py`, and `tests/unit/test_canon_identity.py`: migrate existing expectations and add v2 rule/checker cases.
- `tests/unit/test_proof_kernel_g4_component2_rules.py`: new exact Component 2 suite.
- `src/investigation_agent_platform/domain/proof/validator_fingerprints.py`, `scripts/proof_kernel_validator_fingerprints.py`, and `tests/fixtures/proof_kernel/validator_fingerprints_v1.json`: executable drift control described in D8 for the six reviewed semantic-boundary modules.

Any certificate with `schema_version=cert/v1`, any missing bundle version, or any mismatched schema/rule pair returns `CHECKER_FAILURE`; it is not reinterpreted. No database migration is included. If persisted v1 data is discovered, stop and request a separate migration decision.

The Component 1 exit recorded on 2026-10-24 applies to the pre-amendment corpus. Step 1 below proposes a reviewed amendment to BEHAVIOR fixtures (`asserted_value` and `mapping_digest`); that amendment is not covered by the existing exit. After an authorized amendment, rerun both Component 1 commands, require the same 4-test structural gate and 14-test fixture-integrity regression (or record an explicitly reviewed count change), and record a new fixture-level validation result before relying on the amended corpus in Component 2.

**Alternative considered:** Keep a tolerant v1 reader or dual-emit both versions. Rejected because IAP has no live external consumers and the repository explicitly forbids permanent compatibility branches.

**Rationale:** A direct versioned cutover prevents old semantics from being silently accepted under new rule names.

**Trade-offs:** The migration touches existing kernel fixtures and tests. This is intentional and reviewable; it is not a production data migration.

---

### D8: Detect executable validator drift with a reviewed fingerprint manifest

**Decision:** Fingerprint exact source bytes for `premises.py`, `rules.py`, `checker_semantics.py`, `manifest.py`, `evaluation.py`, and `canonical.py` in `tests/fixtures/proof_kernel/validator_fingerprints_v1.json`. `premises.py` is included because strict parsing, literal tags, nested closed keys, scalar restrictions, and null/omission behavior are semantic gates. `manifest.py` and `evaluation.py` are included because manifest key-set binding and context construction are part of the accepted proof boundary. `canonical.py` is included because identity and byte normalization are shared trusted primitives whose changes alter the v2 contract. `scripts/proof_kernel_validator_fingerprints.py --check` recomputes all six SHA-256 values and fails with the relative path plus expected/actual digest on mismatch. Updating the fingerprint requires a reviewed contract/version change and associated semantic tests; the command never blesses changes automatically.

The named detector test is `tests/unit/test_proof_kernel_validator_fingerprints.py::test_validator_fingerprint_check_detects_unreviewed_source_change`. It modifies a copied module under a temporary directory and asserts a nonzero check result, while the repository copy must pass unchanged. The fingerprint controls checked-in source bytes only; it is not a deployment-image or interpreter attestation.

**Alternative considered:** Rely only on the registry's validator qualified name and contract digest. Rejected because unchanged names and fields can hide executable semantic changes.

**Rationale:** The control directly addresses K9's executable-drift gap without pretending to certify a deployed artifact.

**Trade-offs:** Legitimate semantic changes require explicit fingerprint review. That friction is part of the assurance boundary.

## Data Storage

No database table, persistent model, migration, or external storage is introduced. Premise records, manifests, certificates, and checker requests remain immutable in-memory domain values and test fixtures. If implementation discovers persisted `cert/v1` data or a required database compatibility path, it must stop and obtain a separate owner decision rather than adding a dual reader.

## Data Structures

### Common premise envelope

Every artifact is a closed object with `evidence_id`, `content_hash`, and one literal `premise_type`. The manifest has exactly matching key sets for `evidence_ids`, `content_hashes`, and `premise_types`; artifacts supplied to certification/checking must cover the same IDs and hash to the manifest values. Duplicate IDs, unknown premise types, unknown fields, malformed hashes, null required fields, and missing artifacts fail closed.

Hash-preimage contract (frozen): the hashed artifact payload is the normalized premise record minus the identity envelope — `evidence_id`, `content_hash`, and `premise_type` are never part of the hashed bytes. Hashing `content_hash` would make the preimage self-referential; hashing `evidence_id`/`premise_type` would couple the content digest to inventory addressing. Producer (`certificate._artifact_hash`) and checker (`checker_semantics._artifact_hash`) hash the envelope-free bytes independently; the envelope is verified against the manifest separately on both sides.

### Proposition and scope projections

The v2 proposition identity projection is rule-specific and closed. BEHAVIOR identity is `predicate`, header fields, and target `symbol/file_path`; its scope is repository, revision, build identity, execution ID, and environment. ABSENCE identity includes predicate and target universe; its scope is repository, revision, and path scope. HISTORY identity includes repository, commit ID, file path, and subject; its scope contains queried repository revision. Scope is excluded from `proposition_id` and included under `proposition_scope` in `context_id`.

### Certificate v2

`cert/v2` contains `schema_version`, `proposition_id`, `canonical_proposition`, `rule_id`, `rule_version`, `implementation_digest`, `premises`, `inference_instance`, `assumptions`, `scope`, `coverage` where required, `context_id`, `input_manifest_digest`, `derivation_id`, `proof_classification`, and an untrusted `validation` envelope. The checker recomputes all trusted values and ignores producer acceptance claims.

### Typed outcomes

The existing `CheckerOutcome` vocabulary remains closed: `VALID`, `MISSING_PREMISE`, `INADMISSIBLE_PREMISE`, `INVALID_INFERENCE`, `SCOPE_MISMATCH`, `UNSUPPORTED_CONSTRUCT`, `STALE_CERTIFICATE`, `CONFLICTING_EVIDENCE`, `CHECKER_FAILURE`, and `PREMISE_UNAVAILABLE`. Component 2 adds stable invariant-to-outcome mappings in the design/test vectors without changing the public outcome vocabulary. Checker exceptions map to `CHECKER_FAILURE`; provider unavailability maps to `PREMISE_UNAVAILABLE` only when the artifact is missing or hash-mismatched, never to a negative proof.

## Interfaces

Component 2 introduces no REST, CLI, MCP, workflow, TUI, GUI, or production interface. The only executable interfaces after authorization are pure Python domain functions and test tooling:

| Interface | Inputs | Output | Boundary |
|---|---|---|---|
| `premises.py` models | strict mappings | frozen typed premise | rejects unknown/coerced input |
| producer rule application | typed proposition and premise records | `RuleOutcome`, reason | no I/O or persistence |
| `certify()` | v2 evaluation inputs, typed premises, artifacts | `cert/v2` mapping | refuses non-discharging producer result |
| `check_certificate()` | v2 certificate, proposition, manifest, artifacts, context | `CheckerOutcome`, reasons | independent semantic path |
| fingerprint checker | source paths and fingerprint manifest | exit status/report | test/release control only |

## Accessibility

No user-facing interface is introduced. Test and fingerprint failures must remain readable without color, identify the exact file/case and invariant, and return nonzero exit status for automation. Structured JSON diagnostics may be emitted by test tooling, but no secrets or artifact payloads are logged.

## Implementation Detail

Implementation is authorized only after a separate owner approval explicitly names this supplement and its scope. Once authorized, implement in this order:

1. Amend the Component 1 BEHAVIOR fixture contract for `asserted_value` and reconcile `provenance_ref`/`mapping_digest`; obtain the required provenance adjudication or record BEHAVIOR as unsupported.
2. Add strict premise models and model-level tests, including extra-field rejection, float/bool handling, null-vs-omitted behavior, line ordering, full-SHA rules, and canonical artifact serialization.
3. Extend `EvidenceManifest` and `EvaluationInputs` for typed premise/coverage maps and explicit `rules/v2`; add context scope vectors before changing certificates.
4. Add producer validators and registry rows while preserving existing definition/call-edge semantics through migrated tests.
5. Add independent checker semantic functions, remove checker imports/calls to producer validators, and add static import/consumer tests proving the separation.
6. Add `cert_schema_v2.json`, migrate maintained fixtures/tests, and add explicit v1 rejection vectors.
7. Add fingerprint generation/checking and source-mutation detector tests.
8. Run the exact Component 2 command, the Component 1 gates, relevant existing kernel suites, the fingerprint check, and the repository's relevant lint/type checks. Record pass/fail/skip/xfail counts separately.

The implementation must not add production callers, modify publication authority, change database schemas, add environment-switched legacy behavior, or claim BEHAVIOR discharge without approved mapping provenance.

## Migrations

No database migration is authorized. The only migration is a direct source/test contract cutover from `cert/v1` to `cert/v2` and from `rules/v1` to `rules/v2`. Update all maintained fixture and test inputs in one reviewed change. Remove v1 acceptance from the checker. Missing or mismatched v2 bundle/schema inputs fail closed. Do not create a compatibility branch, dual-schema reader, environment switch, or persisted-data backfill under Component 2.

Step 6 scoping note (recorded at implementation): the v1 Layer-1 example corpora (`cert_valid_examples.json`, `cert_invalid_examples.json`, `cert_layer1_invalid_examples.json`) and `cert_schema_v1.json` are retained unchanged as frozen structural-shape history. Their pinned digests cannot be recomputed under v2 semantics (no stored manifests/artifacts to re-derive from), so field-migrating them would produce stale-`id` "valid" certificates — worse than retention. The checker itself rejects `cert/v1` mechanically; the v1 schema file is test tooling, not an acceptance path. `cert_schema_v2.json` is the only schema new certificates are validated against.

## Testing Philosophy

### Premise model tests

Test each six-model positive shape and every closed-boundary mutation: missing required field, explicit null, unknown field, wrong literal tag, malformed digest, invalid line range, uppercase/short history SHA, float observed value, bool/int ambiguity, and canonical serialization. Test that model instances are frozen and artifact bytes are deterministic.

### Rule tests

For BEHAVIOR, test the minimal three-premise conjunction, every identity/scope mismatch, missing mapping, unavailable/hash-mismatched artifact, wrong type, wrong predicate/value, unapproved mapping, and canonical scalar rejection. For ABSENCE, test complete-empty within a bounded universe, incomplete/failed search, nonzero matches, missing coverage, empty premise list, scope/predicate/config/exclusion mismatch, relevant exclusion, and manifest key-set mismatch. For HISTORY, test exact match, each repository/commit/file/subject/revision mismatch, prefix/uppercase commit IDs, causal wording as unsupported, and omitted/null/unexpected optional headers.

### Independent checker tests

Feed the same frozen v2 inputs through producer and checker paths and pin expected outcomes independently. Mutate producer validator behavior in a copied module and prove checker results do not change. Mutate the checker semantic module and prove the checker-specific cases or fingerprint gate fail. Assert producer `validation` and `proof_classification` forgeries are ignored.

### Version and identity tests

Pin v2 proposition, context, manifest, derivation, and certificate vectors byte-for-byte. Verify proposition identity excludes scope while context identity changes when scope changes. Reject `cert/v1`, omitted bundle versions, mismatched `cert/v2`/`rules/v1` pairs, unknown rule IDs, unknown fields, missing/extra bindings, and contradictory target/scope projections.

### Exact Component 2 gate

The new test file is `tests/unit/test_proof_kernel_g4_component2_rules.py`. The standalone Component 2 command is:

```text
uv run python scripts/proof_kernel_validator_fingerprints.py --check && uv run python -m pytest -q -p no:cacheprovider tests/unit/test_canon_identity.py tests/unit/test_manifest_evaluation.py tests/unit/test_typed_rules.py tests/unit/test_certificate_checker.py tests/unit/test_composition_invalidation.py tests/unit/test_publication_decision.py tests/unit/test_g5_composition_gate.py tests/unit/test_proof_kernel_fixture_integrity.py tests/unit/test_proof_kernel_validator_fingerprints.py tests/unit/test_proof_kernel_g4_component1_evidence.py tests/unit/test_proof_kernel_g4_component2_rules.py
```

This standalone command includes the fingerprint check, migrated canonical/manifest/rule/checker suites, composition and decision regressions, both Component 1 fixture gates, the fingerprint detector suite, and the Component 2 suite. The exit is not green unless every command segment passes. Skips, xfails, pre-existing failures, and unsupported BEHAVIOR cases are reported separately and never counted as rule passes. The parent plan's later global command additionally includes `tests/unit/test_proof_kernel_g4_component3_properties.py` and `tests/unit/test_proof_kernel_g4_component4_integration.py` after those supplements exist. After the proposed Component 1 BEHAVIOR amendment, rerun these exact commands independently:

```text
uv run python -m pytest -q -p no:cacheprovider tests/unit/test_proof_kernel_g4_component1_evidence.py
uv run python -m pytest -q -p no:cacheprovider tests/unit/test_proof_kernel_fixture_integrity.py
```

Record those post-amendment results separately from the pre-amendment Component 1 exit.

## Documentation Plan

### `docs/IAP-proof-kernel-g4-component-2-design-v1.0.md`

**Audience:** Component 2 implementers, gate owner, and senior technical reviewers. This supplement is the reviewable contract for typed premise models, three rule validators, independent checker semantics, direct v2 migration, tests, fingerprints, and authorization boundaries.

### `docs/IAP-proof-kernel-g4-component-1-design-v1.0.md`

**Audience:** Component 1 reviewers. Retain the fixture-level exit record and keep its scope separate from premise semantics and rule/checker outcomes. Amend only through a reviewed contract change if BEHAVIOR asserted-value or provenance fields change.

### `docs/IAP-proof-kernel-traceability-v1.0.md`

**Audience:** Gate owner and reviewers. Keep Component 1 recorded as fixture-level evidence, update rule-level rows only after executable Component 2 evidence exists, and retain G4 PARTIAL until all global criteria are met.

### `docs/IAP-proof-kernel-contracts-v1.0.md`

**Audience:** Kernel contract maintainers. Update the normative certificate and rule-bundle references to v2 only in the authorized implementation change, with the complete fixture and test migration inventory.

### `docs/dev/architecture.md` changes

No changes are required. No `docs/dev/architecture.md` exists in the project, and Component 2 adds pure domain validation only.

## Risks / Trade-offs

### BUILD_SOURCE_MAPPING remains unresolved

**Risk:** A complete-looking runtime observation could be incorrectly joined to static source if the mapping is self-attested or lacks immutable authority.

**Mitigation:** Keep BEHAVIOR non-discharging until the owner approves an independently addressable provenance source with authentication, scope, conflict, and unavailable contracts. Record unsupported behavior explicitly rather than weakening the rule.

### Producer and checker can drift

**Risk:** Separate semantic implementations may disagree or one may be changed without corresponding contract review.

**Mitigation:** Use independent goldens, mutation tests, static import checks, and reviewed source fingerprints. Shared code is limited to canonical/schema primitives.

### Direct v2 migration can expose hidden v1 consumers

**Risk:** Existing tests or untracked callers may rely on default `rules/v1` or `cert/v1` behavior.

**Mitigation:** Inventory and migrate maintained repository consumers in the authorized change; fail closed on any remaining v1 input; stop and request a separate decision if persisted v1 data is found.

### Fixture schema and runtime model can diverge

**Risk:** JSON fixtures can remain structurally valid while runtime Pydantic models reject or reinterpret them.

**Mitigation:** Add explicit fixture-to-model loading tests in Component 2, keep Component 1 structural tests separate, and report model rejection as semantic fixture incompatibility rather than silently changing the corpus.

### Finite rule tests can overstate assurance

**Risk:** Green curated tests may be mistaken for universal rule correctness or production safety.

**Mitigation:** Preserve typed unsupported outcomes, mutation sensitivity, exact tested-input scope, G4 non-authority language, and G5/G7 follow-up gates in all exit records.
