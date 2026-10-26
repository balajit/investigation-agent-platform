# Design: Proof-Kernel G4 Component 4 — Pure End-to-End Certificate-to-Decision Integration v1.2

> Status: supplement for owner review; this is a code-level design for Component 4 authorization. It does not authorize implementation. Component 4 implementation requires separate explicit owner authorization after this supplement is reviewed. It does not close G4, authorize production wiring, or substitute for G5 persistence/atomicity.
> Scope: freeze the test-only pure orchestration harness (manifest -> rule -> certify -> check_certificate -> compose -> decide_publication -> build_commit where COMMIT), the complete per-case report schema, exact inline fixtures and commands, the static consumer-scan contract, and every asserted decision outcome over the five closed rules. No new premise types, no registry changes, no certificate changes, no production behavior.
> Revision: v1.2 supersedes v1.1 on owner review (2026-10-26). Amendments: 14-file gate ruling RECORDED (owner: global G4 gate is `test_proof_kernel_premises.py` inclusive — provisionality removed, ruled command carried below; this deviates from the parent's verbatim 13-file text under explicit owner authority, noted in D4); R3 report contract made executable — normative assertion-message rule (red runs emit actuals), green-means-pinned semantics, and a new `test_c4_report_table_complete` completeness detector (20 normative tests). v1.0/v1.1 retained as reviewed artifacts. C4 scope confirmed test-only, separate from production wiring and cross-modal diagnosis.

## Context

Components 1–3 froze evidence contracts, rule validators with independent checker semantics and the `cert/v2` + `rules/v2` cutover (C1, C2), and composition-level property vectors with a pre-mapped six-mutation detector set (C3, implemented 2026-10-26 under the owner C3 GO with zero survivors and no `src/` change). The existing suites (`test_composition_invalidation.py`, `test_publication_decision.py`, `test_g5_composition_gate.py`) pin each layer in isolation: composition never touches certificates, and publication-decision tests use synthetic `ctx-1`/`der-1` identifiers rather than real `certify` output. No test currently executes the full pure path from evidence artifacts through rule evaluation, certificate serialization, independent checking, composition, and publication decision using the real pure domain APIs — so a binding break between layers (e.g. a `derivation_id` that `build_commit` would refuse, or a `context_id` that `decide_publication` would call stale) has no detector.

The phase plan (Component 4) requires a test-only harness over the real pure APIs: one valid case per supported rule, checker rejection, stale context, missing premise, policy denial, alternative survival, and combined multi-premise behavior — asserting that denied decisions preserve the proof artifact, that decisions reference the exact accepted `derivation_id`/`context_id`, and that no production consumer treats the harness as authority. This supplement specifies all of that before any implementation run.

## References

- [`docs/IAP-proof-kernel-g4-phase-design-v1.0.md`](IAP-proof-kernel-g4-phase-design-v1.0.md) — parent phase plan; Component 4 entry/exit criteria, global gate, Testing Philosophy.
- [`docs/IAP-proof-kernel-g4-component-2-design-v1.0.md`](IAP-proof-kernel-g4-component-2-design-v1.0.md) — rule contracts, D4a precedence, binding projections, `_PINNED_*_BINDINGS` literals reused here.
- [`docs/IAP-proof-kernel-g4-component-3-design-v1.0.md`](IAP-proof-kernel-g4-component-3-design-v1.0.md) — structural template for this supplement; D5 suite-list revision noted in D4 below.
- [`docs/IAP-proof-kernel-traceability-v1.0.md`](IAP-proof-kernel-traceability-v1.0.md) — current statuses; updated with Component 4 evidence after implementation (C3 recorded as implemented 2026-10-26).
- [`src/investigation_agent_platform/domain/proof/evaluation.py`](../../src/investigation_agent_platform/domain/proof/evaluation.py) — `EvaluationInputs`, `evaluate`.
- [`src/investigation_agent_platform/domain/proof/manifest.py`](../../src/investigation_agent_platform/domain/proof/manifest.py) — `build_manifest`, `ManifestCoverage`, `manifest_digest`.
- [`src/investigation_agent_platform/domain/proof/rules.py`](../../src/investigation_agent_platform/domain/proof/rules.py) — `get_rule`, `apply_rule`, `RuleOutcome`, `RULE_REGISTRY`, `RULE_BUNDLE_VERSION`, `APPROVED_BUILD_MAPPING_AUTHORITIES`.
- [`src/investigation_agent_platform/domain/proof/certificate.py`](../../src/investigation_agent_platform/domain/proof/certificate.py) — `certify`, `CheckRequest`, `check_certificate`, `CertificationError`, `CERT_SCHEMA_VERSION`.
- [`src/investigation_agent_platform/domain/proof/checker_semantics.py`](../../src/investigation_agent_platform/domain/proof/checker_semantics.py) — independent `check_*_semantics` (never producer validators).
- [`src/investigation_agent_platform/domain/proof/outcomes.py`](../../src/investigation_agent_platform/domain/proof/outcomes.py) — `CheckerOutcome`.
- [`src/investigation_agent_platform/domain/proof/composition.py`](../../src/investigation_agent_platform/domain/proof/composition.py) — `make_node`, `compose_conjunction`, `compose_alternatives`, `invalidate_derivations`, `invalidate_by_evidence`.
- [`src/investigation_agent_platform/domain/proof/publication.py`](../../src/investigation_agent_platform/domain/proof/publication.py) — `decide_publication`, `PublicationRequest`, `PublicationDecision`, `DenyReason`, `build_commit`.
- [`src/investigation_agent_platform/domain/proof/premises.py`](../../src/investigation_agent_platform/domain/proof/premises.py) — strict premise records; envelope-free hash-preimage contract.
- [`src/investigation_agent_platform/domain/proof/canonical.py`](../../src/investigation_agent_platform/domain/proof/canonical.py) — `V2_TARGET_KEYS`, `V2_SCOPE_KEYS`, `canonical_dict_bytes` (shared allowed primitive).
- [`src/investigation_agent_platform/application/proof/publication_commit_service.py`](../../src/investigation_agent_platform/application/proof/publication_commit_service.py) — pre-existing G5-owned `decide_publication` consumer (imports `decide_publication` only, not `build_commit`); grandfathered in the D3 allowlist, not a C4 dependency.

## Goals / Non-Goals

**Goals:**

- Pin one deterministic valid certificate-to-COMMIT path per supported rule (`definition/v1`, `call_edge/v1`, `behavior/v1`, `absence/v1`, `history/v1`) through the real pure APIs in fixed call order.
- Pin every negative path: checker rejection, stale context (K8/K15), missing premise, policy denial with proof-preserving recomputation, alternative survival, and combined multi-premise behavior.
- Prove denied/stale decisions preserve the proof artifact byte-for-byte and reference the exact accepted `derivation_id`/`context_id`.
- Freeze the static consumer-scan contract that keeps the harness and the pure decision functions non-authoritative.
- Emit a deterministic report distinguishing G4 pure-decision coverage from G5 persistence/atomicity.

**Non-Goals:**

- New premise types, registry rows, rule validators, checker semantics, certificate schema, or canonicalization changes.
- Publication-record inserts, hypothesis/finding status updates, atomicity/interleaving coverage, writer routing, live-database tests, or any production wiring.
- Fixture files, migrations, application changes, rollout, or authority changes.
- Population-accuracy or false-accept-rate claims from finite end-to-end vectors.

## Decisions

### D1: Harness shape — test-only orchestration calling the real pure APIs in fixed order

**Decision:** The new module `tests/unit/test_proof_kernel_g4_component4_integration.py` contains only test functions and small inline builders (mirroring the `_behavior_claim`-style pinned-builder convention from `tests/unit/test_proof_kernel_g4_component2_rules.py`: `_PINNED_BEHAVIOR_BINDINGS`, `_PINNED_ABSENCE_BINDINGS`, `_PINNED_HISTORY_BINDINGS`, flat `claim.update(over)` overrides). Each end-to-end test executes the same fixed call order with no mocks:

1. `build_manifest(evidence_ids, content_hashes, premise_types, ...)` — artifacts hashed envelope-free as `sha256:` + hex(`canonical_dict_bytes(record)`), per the frozen premises/v1 preimage contract (callers supply artifacts without `evidence_id`/`content_hash`/`premise_type` keys).
2. `EvaluationInputs(proposition=..., manifest=..., rule_bundle_version="rules/v2" (= `RULE_BUNDLE_VERSION`), rule_id, rule_version, implementation_digest, inference={})` (+ optional `evaluate` smoke binding; the derivation under test is the one `certify` constructs).
3. `get_rule(rule_id)` (+ `apply_rule` agreement spot-check on the valid cases: producer `DISCHARGED`).
4. `certify(inputs, premises, artifacts, universe=...)` — `premises` entries carry `evidence_id` + `content_hash` + `premise_type` (+ scope synthesized by `_entry_scope`); `universe` supplied for absence.
5. `CheckRequest(certificate=..., proposition=..., manifest=..., rule_bundle_version="rules/v2", artifacts=..., expected_context_id=..., mapping_authority=..., derivation_deps={})` → `check_certificate(request)` → `(CheckerOutcome, reasons)`.
6. `make_node(derivation_id, rule_id, premise_ids, depends_on)` → `compose_conjunction([outcome])` for single-route cases (every path pins the composition layer, even singletons), `compose_alternatives([...])` for the survival case.
7. `PublicationRequest(checker_outcome=..., certificate_context_id=..., expected_context_id=..., derivation_id=..., reconciliation_adjudicated=True, policy_eligible=..., policy_reason=...)` → `decide_publication(request)`; on `COMMIT`, `build_commit(derivation_id, context_id, expected_context_id)` and assert the returned `PublicationCommit` binds the exact certificate `derivation_id`/`context_id`.

Propositions are built as `(header, target, scope)` triples projecting exactly to the C2 pinned bindings: target keys per `V2_TARGET_KEYS`, scope keys per `V2_SCOPE_KEYS`, header `form`/`quantifier`/`relation`/`modality`/`completeness`/`predicate`/`asserted_value` per rule. Required header combinations: definition/call_edge `EXISTENCE`/`EXISTENTIAL`; behavior `OBSERVATION`/`OBSERVATIONAL`/`RUNTIME_OBSERVATION`/`RUNTIME`; absence `ABSENCE`/`UNIVERSAL`/`STATIC`/`CLOSED_WORLD`; history `HISTORICAL`/`EXISTENTIAL`/`HISTORY` with `relation` and `completeness` keys absent (never null).

Exact valid-case certificate identity assertions (every valid test asserts all of these): `proposition_id`, `context_id`, `derivation_id`, `input_manifest_digest == manifest_digest(manifest)`, `rule_id`, `rule_version`, `implementation_digest == RULE_REGISTRY[rule_id].implementation_digest`, `schema_version == CERT_SCHEMA_VERSION == "cert/v2"`; checker outcome `VALID`; composition outcome `VALID`; decision `COMMIT`; `build_commit` returns the exact `derivation_id`/`context_id` pair.

Exact fixture premise sets (inline, self-describing IDs `ev-c4-<rule>-<n>`; 40-char lowercase-hex revisions `rc4a…` style wherever a `FullSha` field requires it; `mapping_digest`/`search_config_digest` well-formed `sha256:` digests):

- **definition** (`test_e2e_definition_valid_commit`): one `RANGE_READ_FACT` (`ev-c4-definition-0`: `repository`, `file_path`, `revision`, positive `line_start <= line_end`, `target`, `window_text` containing the asserted symbol); proposition target `(symbol, file_path)`, scope `(revision)`.
- **call_edge** (`test_e2e_call_edge_valid_commit`): one `NORMALIZED_CALLSITE_FACT` (`ev-c4-call-edge-0`: `repository`, `file_path`, `revision`, line range, `caller`, `callee`, `call_text` containing both names); proposition target `(caller, callee)` (canonical keys; `counterpart`/`symbol` aliases not used here), scope `(revision)`.
- **behavior** (`test_e2e_behavior_valid_commit`): `RANGE_READ_FACT` (`ev-c4-behavior-0`) + `RUNTIME_OBSERVATION_FACT` (`ev-c4-behavior-1`: `symbol`, `predicate`, canonical-scalar `observed_value` (never float), `execution_id`, `build_identity`, `environment`, `source_revision`) + `BUILD_SOURCE_MAPPING` (`ev-c4-behavior-2`: `build_identity`, `repository`, `source_revision`, `provenance_ref="synthetic-test-only-authority-001"`, `mapping_digest`); `mapping_authority={"approved_provenance_refs": ["synthetic-test-only-authority-001"]}` (the single member of `APPROVED_BUILD_MAPPING_AUTHORITIES`). Proposition target `(symbol, file_path)`, scope `(repository, revision, build_identity, execution_id, environment)`; asserted `predicate`/`asserted_value` equal the runtime record.
- **absence** (`test_e2e_absence_valid_commit`): one `ABSENCE_COVERAGE_FACT` (`ev-c4-absence-0`: `universe`, `predicate`, `search_status="COMPLETE_EMPTY"`, `search_config_digest`, `scope={repository, revision, path_scope}`, `exclusions=[]`, `coverage_basis={kind, value}`, `match_count=0`); manifest carries `coverage_evidence_id` + `coverage_evidence` (the envelope-free `AbsenceCoverage`), `coverage=ManifestCoverage.COMPLETE_EMPTY`, `retrieval_config={"scope": universe, "search_config_digest": ...}` matching the fact, `exclusions=[]`; `universe` passed to `certify`. Proposition target `(universe)`, scope `(repository, revision, path_scope)`; certificate `coverage.result == "COMPLETE_PATH"`.
- **history** (`test_e2e_history_valid_commit`): one `COMMIT_HISTORY` (`ev-c4-history-0`: `repository`, `repository_revision` (full SHA), `commit_id` (full SHA), `file_path`, `subject`); proposition target `(repository, commit_id, file_path, subject)`, scope `(repository_revision)`; `relation`/`completeness` omitted.

Negative cases (each varies exactly one property from its valid base so the denial is attributable):

- `test_e2e_checker_rejection_denies` (definition base): certify valid, then tamper the artifact copy (`window_text` with the symbol removed) → `check_certificate` returns `PREMISE_UNAVAILABLE` (content-hash mismatch) → `PublicationRequest` with that outcome → `DENY`, reason starts with `NOT_CHECKER_ACCEPTED:` → no `build_commit` call; assert no `COMMIT`.
- `test_e2e_checker_stale_rejection_denies` (definition base, R1 checker layer): `CheckRequest(expected_context_id="ctx-moved-c4")` ≠ certificate `context_id` → `check_certificate` returns `STALE_CERTIFICATE` (K8/K15); `PublicationRequest` carrying that checker outcome (with the certificate's own IDs) → `DENY` with reason starting `NOT_CHECKER_ACCEPTED:` — the checker-outcome gate (`publication.py` first-failure order) fires before the staleness comparison, so `STALE_CONTEXT` must NOT appear here. No `build_commit` call.
- `test_e2e_decision_stale_context_denies` (definition base, R1 decision layer): `CheckRequest` with `expected_context_id` equal to the certificate `context_id` → `check_certificate` returns `VALID`; `PublicationRequest(checker_outcome=VALID, certificate_context_id=<cert ctx>, expected_context_id="ctx-moved-c4")` → `DENY STALE_CONTEXT` (exact); `build_commit(der, cert_ctx, "ctx-moved-c4")` raises `ValueError` matching `STALE_CONTEXT`.
- `test_e2e_missing_premise_denies` (call_edge base): `CheckRequest` with one premise's artifact bytes withheld → `PREMISE_UNAVAILABLE` → `DENY NOT_CHECKER_ACCEPTED:*`. (Construction note: dropping a premise before `certify` refuses earlier with `CertificationError("RULE_NOT_DISCHARGED")` — fail-closed at serialization, never reaching decision; the normative test pins the checker/decision path.)
- `test_e2e_policy_denial_recomputes_without_mutating_proof` (behavior base): record `H0 = sha256(canonical_dict_bytes(certificate))`; `decide_publication(policy_eligible=False, policy_reason="c4-policy-freeze")` → `DENY POLICY_DENIED:c4-policy-freeze`; assert `sha256(canonical_dict_bytes(certificate)) == H0`, `derivation_id`/`context_id`/`input_manifest_digest` unchanged; then `PublicationRequest` with identical IDs and `policy_eligible=True` → `COMMIT` → `build_commit` succeeds on the same IDs.
- `test_e2e_alternative_survival_commits` (definition + call_edge bases): `compose_alternatives([INVALID_INFERENCE, VALID])` → `VALID` (K5) → `COMMIT` for the surviving derivation's IDs; assert the failed route's IDs appear nowhere in the `PublicationRequest`/`PublicationCommit`.
- `test_e2e_multi_premise_behavior_commits` (behavior base only, R2 narrowed): the three-premise behavior certificate (code + runtime + mapping premises inside ONE derivation) is the conjunction case — `compose_conjunction([VALID])` over its single mandatory route → `VALID` → `COMMIT` for that derivation's own `derivation_id`/`context_id`; assert the committed derivation's `premise_ids` contain `ev-c4-behavior-0/1/2`. Conjunction necessity is pinned by additionally asserting that dropping any one premise refuses earlier at `certify` with `CertificationError("RULE_NOT_DISCHARGED")` (fail-closed at serialization, never a partial-derivation commit). No cross-derivation conjunction is committed: `compose_conjunction` returns outcome + reason only (no combined derivation/context identity), and `PublicationRequest`/`build_commit` accept exactly one pair — a multi-derivation commit would need a designed combined identity that does not exist, so the design claims none.

**Rationale:** Every layer transition recomputes identities from content (never trusts the prior layer's labels), matching the phase Testing Philosophy (exact certificate identity, checker result, composition result, decision, reason codes). Single-route cases still pass through `compose_conjunction` so a future composition regression cannot hide behind direct checker-to-decision wiring.

**Alternatives considered:** Mock the checker verdict at the decision layer (rejected: would sever the identity binding this component exists to pin); drive negatives only through `apply_rule` without certificates (rejected: producer verdicts are untrusted by design — only `check_certificate` outcomes reach `decide_publication`).

**Trade-offs:** Five hand-built valid routes plus seven targeted negatives cover the contract surface but make no population claim; strict `premises/v1` parsing (`extra="forbid"`, `strict=True`, `FullSha` revisions) couples fixtures to the frozen premise schema — a premise-schema change breaks C4 fixtures loudly, which is intended.

### D2: Denied-decision preservation — proof bytes identical, exact IDs referenced, every DenyReason enumerated

**Decision:** `test_e2e_denied_decision_preserves_proof_artifact` runs the definition valid route to a certificate, snapshots `H0 = sha256(canonical_dict_bytes(certificate))`, then executes one denial per `DenyReason` member with exactly one triggering delta (all earlier checklist layers satisfied, so attribution is exact — checklist-order brittleness is a named risk, not an accident):

| Test (normative) | Triggering fixture delta | Expected reason |
|---|---|---|
| `test_e2e_deny_no_pinned_context` | `expected_context_id=None`, all else COMMIT-ready | `NO_PINNED_CONTEXT` (exact) |
| `test_e2e_deny_not_checker_accepted` | `checker_outcome=INVALID_INFERENCE` (tampered-artifact checker path from D1) | starts with `NOT_CHECKER_ACCEPTED:` |
| `test_e2e_deny_conflict_unresolved` | `reconciliation_adjudicated=False`, all else COMMIT-ready | `CONFLICT_UNRESOLVED` (exact) |
| `test_e2e_deny_stale_context` | `expected_context_id="ctx-moved-c4"` ≠ certificate `context_id` | `STALE_CONTEXT` (exact) |
| `test_e2e_deny_policy_denied` | `policy_eligible=False, policy_reason="c4-deny-pin"` | starts with `POLICY_DENIED:` |

After every denial the test asserts: `sha256(canonical_dict_bytes(certificate)) == H0` (certificate dict deep-equal, not just the hash), the denied `PublicationRequest.certificate_context_id`/`derivation_id` equal the certificate's `context_id`/`derivation_id`, and no `PublicationCommit` was constructed. The stale row additionally asserts `build_commit` raises `ValueError` matching `STALE_CONTEXT`.

**Rationale:** Policy denial and staleness must recompute the decision, never mutate the proof — the frozen `EvaluationInputs`/`EvidenceManifest`/certificate models already make mutation impossible, and this test turns that structural property into an executable detector.

**Alternatives considered:** Assert preservation only for the policy case (rejected: staleness and conflict paths must carry the same guarantee, and the per-reason table is the parent's "all decision outcomes" requirement).

### D3: Static consumer-scan contract — executable, allowlisted, no new authority

**Decision:** Normative test `test_no_production_consumer_of_g4_decision` (in the C4 module) performs a `pathlib`-based source grep at test time and fails on any violation:

- **Searched roots:** `src/` in its entirety (every package: `application/`, `infrastructure/`, interface/workflow layers, services, CLI entry points).
- **Forbidden patterns** (matched against file text, test fails with the offending path + line on any hit outside the allowlist):
  1. `decide_publication` and `build_commit` as import or attribute references (covers `from … import decide_publication`, `publication.decide_publication`, aliased imports).
  2. The harness module path `test_proof_kernel_g4_component4_integration` referenced from any file (import, string, or comment) — the harness exposes no imports for other suites.
  3. The literal substrings `component4_integration` and `g4_component4` under `src/` (feature-flag or config gating of the harness; verified 2026-10-25 that no `feature_flag`/`is_enabled`/`get_flag`/`USE_PROOF_KERNEL`/`proof_kernel_enabled` mechanism exists in `src/`, so any such reference is novel by construction).
- **Allowlist (exact, exhaustive):** (a) `src/investigation_agent_platform/domain/proof/` internal cross-imports; (b) `tests/` imports; (c) the single grandfathered pre-existing consumer `src/investigation_agent_platform/application/proof/publication_commit_service.py`, which may import `decide_publication` but must not import `build_commit` or reference the harness module — pinned by asserting the file's text contains `decide_publication`, contains neither `build_commit` nor `component4_integration`, and remains the only `src/` file outside `domain/proof/` matching pattern 1. Any second match is a failure, not an extension request: adding authority requires a separate owner decision and a supplement amendment, never a test edit.

**Rationale:** The parent requires "static boundary checks confirm no new authoritative consumer." An exact-set assertion (not a count threshold) makes a new consumer fail loudly instead of being absorbed.

**Alternatives considered:** Manual review checklist without an executable test (rejected: reviews drift; the boundary must be a detector); forbidding even the grandfathered service import (rejected: it predates G4, is G5-owned, and removing it is a G5 migration decision, not a C4 test concern).

**Trade-offs / limits:** A grep scan detects references, not runtime authority — dynamic imports via `importlib` with constructed strings, or a copy-pasted reimplementation of the checklist, would evade it. Documented as a limit in Risks; the scan is a tripwire, not a proof of non-consumption.

### D4: Fixtures, commands, and report schema — inline only, ruled 14-file gate

**Decision:** All fixtures are inline builders in the C4 module (no new fixture files, following C3 precedent: node IDs `ev-c4-*`, revisions as named 40-hex constants). The standalone C4 command and the global G4 gate command are the same ruled command — the parent's Global G4 exit criterion 6 plus the owner-recorded 14-file ruling (entry item 4), i.e. the verbatim parent command with `tests/unit/test_proof_kernel_premises.py` appended. This deviates from the parent's 13-file text under explicit owner authority (2026-10-26); the parent text itself is unchanged:

```text
uv run python scripts/proof_kernel_validator_fingerprints.py --check && uv run python -m pytest -q -p no:cacheprovider tests/unit/test_canon_identity.py tests/unit/test_manifest_evaluation.py tests/unit/test_typed_rules.py tests/unit/test_certificate_checker.py tests/unit/test_composition_invalidation.py tests/unit/test_publication_decision.py tests/unit/test_g5_composition_gate.py tests/unit/test_proof_kernel_fixture_integrity.py tests/unit/test_proof_kernel_validator_fingerprints.py tests/unit/test_proof_kernel_g4_component1_evidence.py tests/unit/test_proof_kernel_g4_component2_rules.py tests/unit/test_proof_kernel_g4_component3_properties.py tests/unit/test_proof_kernel_g4_component4_integration.py tests/unit/test_proof_kernel_premises.py
```

Report contract (R3 — executable, closes the `-q` vs actuals gap): the gate runs `pytest -q`, whose green output carries no per-case values — so the contract does not rely on gate verbosity at all. Instead it rests on three executable legs:
1. **Normative assertion-message rule:** every normative test asserts equality against pinned literals with a message embedding the observed values (`f"checker={actual_outcome} reason={actual_reason} ids={...}"` shape). A red run therefore emits the actual outcome, reason, and IDs in the failure output — actuals are captured exactly when they differ.
2. **Green-means-pinned semantics:** every normative assertion is an equality check against a literal pinned in the test body. A green run proves actual == expected by construction — the test source is the record of actuals, and a green gate output plus the pinned source jointly constitute the evidence. No separate emission channel is needed for the passing case, and none is claimed.
3. **Completeness detector `test_c4_report_table_complete` (20th normative test):** parses the module-docstring mapping table at test time and fails unless it holds exactly one row per each of the 19 behavioral normative tests with non-empty `fixture IDs`, `expected`, `actual`, and `verdict` cells. A `PENDING` or drifted table fails the gate — the table cannot silently lag the suite.
The transcribed record (traceability C4 section) carries: the table, the exact gate command, revision, and tallies. Tallies: per-group pass counts and the `20/20` total (exact counts filled at implementation; skips/xfails reported separately per the phase reporting rule, never counted as passes); plus the normative G4-vs-G5 scoping footnote, worded exactly: "G4 pure-decision coverage only: this report pins the in-memory certificate-to-decision path. It claims no publication-record insert, no status update, no atomicity/interleaving behavior, no writer routing, and no production behavior — all owned by G5."

**Rationale:** One command for standalone and global runs removes the "which gate" ambiguity that partial-file variants create; the footnote makes scope-laundering structurally awkward. The report contract binds actuals to executable assertions rather than to log verbosity, so the `-q` gate and the per-case record cannot diverge: red runs emit actuals, green runs prove them equal to pins, and the completeness detector pins the table to the suite.

**Alternatives considered:** A C4-only subset command for iteration (rejected as the gate: developers may run subsets locally, but only the full ruled command counts; no partial-file variant may be presented as the C4 or G4 gate). A JSON emission harness or pytest plugin for actuals (rejected: new mechanism to review for a closed deterministic suite; the assertion-message rule captures actuals with zero new surface). Narrowing the report to "gate green + pins" prose without the table (rejected: per-case fixture/attribution rows are a parent requirement — line 245 — and the completeness detector makes them executable instead).

### D5: G5 carry-forward — explicit non-claims with owners

**Decision:** Component 4 does not claim, test, or imply any of the following; each names its G5 owner document:

- Publication-record insert (`insert_commit`) — owner: G5, `docs/IAP-proof-kernel-publication-commit-design-v1.0.md`.
- Hypothesis/finding/conclusion status updates (`advance_hypothesis_guarded`, finding/conclusion inserts) — owner: G5, publication-commit design.
- Database atomicity, interleaving, live-snapshot binding, CAS/version-guard behavior — owner: G5, `docs/IAP-proof-kernel-g5-formal-protocol-v1.0.md` (the `test_g5_composition_gate.py` suite stays in the gate as a boundary witness, not as a G5 PASS claim).
- Writer routing (`single`/`all`/`any` over live routes), RLS session handling, contradiction listing — owner: G5, `publication_commit_service.py` + G5 protocol.
- Production behavior of any kind, including the grandfathered `publication_commit_service.py` path — owner: G5/G7 separate decisions.

The C4 report footnote (D4) and the exit criteria restate this boundary; traceability records the residuals by name.

## Interfaces

No new interfaces. The executable surface is pure test code calling existing pure domain functions in the D1 order (`build_manifest`, `EvaluationInputs`/`evaluate`, `get_rule`/`apply_rule`, `certify`, `CheckRequest`/`check_certificate`, `make_node`, `compose_conjunction`/`compose_alternatives`, `PublicationRequest`/`decide_publication`, `build_commit`). The new test module exposes no imports for other suites (enforced by D3 pattern 2).

## File list

- New (implementation phase, not this supplement): `tests/unit/test_proof_kernel_g4_component4_integration.py` — 20 normative tests (D1 5 valid + 7 negatives incl. checker-stale, decision-stale, policy-recompute, D2 preservation + 5 per-reason, D3 scan, D4 `test_c4_report_table_complete`), inline fixtures only, report table in module docstring.
- Touched (implementation phase): `docs/IAP-proof-kernel-traceability-v1.0.md` (Component 4 section + requirement-to-test index counts), `docs/IAP_Arch_Eng_Recommendations.log` (C4 entries).
- This supplement only: `docs/IAP-proof-kernel-g4-component-4-design-v1.0.md` (this file).
- Explicitly not touched: `src/`, fixtures (`tests/fixtures/`), migrations, application code, `docs/archive/*`.

## Data Storage / Migrations

None. No persistent model, table, migration, or external contract. All fixtures are inline in code (premise IDs `ev-c4-*`); no new fixture files. Certificates, manifests, and decisions exist only as in-memory test values.

## Entry criteria

1. Components 1–3 pass (C3 landed 2026-10-26 under the owner C3 GO: 39 tests green, M1–M6 all detected, no `src/` change — per traceability; Component 4 begins only after recorded acceptance of all three).
2. This supplement is reviewed; owner explicitly confirms the Component 4 scope (harness order, 20 normative test names, fixture IDs, scan contract, R3 report contract, D5 residuals) — C4 test-only scope confirmed separate from production wiring and cross-modal diagnosis.
3. G5 residuals (D5) are explicitly carried forward and named in the implementation authorization.
4. 13-vs-14 gate reconciliation RULED (owner 2026-10-26): the global G4 gate is 14 files, `tests/unit/test_proof_kernel_premises.py` inclusive — carried as the ruled command in D4. Provisionality is over; C4 gate results are claimed against the ruled 14-file command only.

## Exit criteria

1. All 20 normative tests pass deterministically (19 behavioral + `test_c4_report_table_complete`) with complete per-case failure attribution (fixture IDs, rule, certificate identity, checker outcome, composition outcome, decision, reason, preservation hash; expected vs actual per the R3 report contract); exact counts reported.
2. No invalid or stale certificate yields `COMMIT` across all negative paths (checker tamper, withheld artifact, stale context at both checker and decision layers, unpinned context, unadjudicated conflict, policy denial).
3. Policy change recomputes the decision without mutating the proof (`H_pre == H_post`, identical `derivation_id`/`context_id`/`input_manifest_digest`, `COMMIT` recovered on the same proof with `policy_eligible=True`).
4. Static boundary check green: non-test importer set of `decide_publication`/`build_commit` equals the D3 allowlist exactly; no harness or flag reference under `src/`.
5. Test report carries the normative G4-vs-G5 scoping footnote and distinguishes G4 pure-decision coverage from G5 persistence/atomicity.
6. Traceability Component 4 section + index counts + ledger updated; no `src/`, fixture, migration, or application change in the Component 4 diff.

## Testing Philosophy alignment

One property per test; pinned golden identities and outcomes (certificate fields, `VALID`/`COMMIT`, exact reason strings); differential agreement is necessary but never sufficient (producer `DISCHARGED` spot-checks accompany, never substitute for, independent checker verdicts); implementation suite ≠ sensitivity evidence (no mutations in C4 — detection sensitivity was C3's job); finite-suite limits stated, not laundered into population or production claims. Pure end-to-end tests assert the exact certificate identity, checker result, composition result, decision, and reason codes while keeping database transactions, persistence races, writer routing, and production behavior explicitly in G5.

## Documentation Plan

After implementation: traceability Component 4 section (per-case table with fixture IDs + expected/actual + attribution, 20-test index counts, residuals; 13-vs-14 ruled 14-file, no longer a residual), ledger entries for the C4 run, requirement-to-test index counts for the new file. No changes to `docs/archive/*`. If a contract defect surfaces during C4 implementation, this supplement is amended (versioned) before the code lands — C4 adds no `src/` workaround.

## Risks / Trade-offs

- **Terminates at pure decision:** the harness ends at `build_commit`'s in-memory `PublicationCommit`. Any reading of a green C4 gate as persistence or safety-to-enable is scope leakage; mitigated by the D4 footnote, D5 owners, and the D3 scan.
- **Checklist-order brittleness:** `decide_publication` reports the first failure only, so per-reason tests that accidentally trip an earlier layer misattribute. Mitigated by the D2 construction rule (exactly one delta per test, all earlier layers COMMIT-ready) — a wrong-reason failure is itself a test failure, investigated, never re-scoped silently.
- **Consumer-scan false-confidence limits:** grep detects references, not authority; constructed dynamic imports or reimplemented checklists evade it. The scan is a tripwire with a documented ceiling, consistent with the phase plan's bounded-claims rule.
- **13-vs-14 gate reconciliation (RESOLVED in v1.2):** owner ruled the global G4 gate is 14 files; the ruled command in D4 supersedes the parent's 13-file text under that authority. Residual risk: the parent phase plan text still reads 13 files — a future reader of the parent alone may run the short command. Mitigated by entry item 4 (ruling recorded here) and the ledger entry; amending the parent plan itself is an owner decision, not taken here.
- **Strict-schema fixture coupling:** `extra="forbid"` + `strict=True` + `FullSha` fields mean premise-schema evolution breaks C4 fixtures. Intended: fixtures must break loudly on contract change, never pass vacuously.

(End of file)
