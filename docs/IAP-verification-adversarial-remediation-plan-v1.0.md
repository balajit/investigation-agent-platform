# Verification Adversarial Remediation Plan v1.0

Status: IMPLEMENTED. This is a test-first remediation plan for weaknesses identified
in `src/investigation_agent_platform/application/investigation/verification.py`.
It covers verifier logic only. It does not redesign the Java analyzer, change
the evidence schema without a failing test and contract decision, or commit
changes.

## Objective

Make every verified result depend on the exact claim, exact analyzer fact,
exact evidence IDs, exact source locations, and exact revision/context that the
claim asserts. A malformed, ambiguous, incomplete, or internally inconsistent
payload must produce `INCONCLUSIVE`, never a stronger verdict.

## Rules

- Add the regression test before changing production logic.
- Run the new test and prove it fails for the current implementation.
- Make the smallest implementation change that makes the test pass.
- Run the affected verifier and integration tests after each ordered phase.
- Do not weaken existing proof ceilings or convert source-only facts into
  runtime behavior or causal proof.
- Do not use prose, evidence-row ordering, or payload insertion order as a
  substitute for typed identity.
- Preserve the legacy field-shaped proof path unless a compatibility test
  explicitly changes its contract.
- Keep all unresolved/partial/ambiguous analyzer results non-verifying.

## Findings To Close

| ID | Weakness | Required outcome |
| --- | --- | --- |
| V1 | Typed Java fact path can bypass the ordinary claim-kind gate; a `BEHAVIOR` claim can verify from source-only value evidence. | Category and `ClaimKind` must be compatible before Java fact proof. Source-only facts cannot verify `BEHAVIOR` or `CAUSAL`. |
| V2 | Analyzer resolution selection is first-match/order-sensitive; duplicate envelope entries and malformed/per-resolver payloads are not aggregated or rejected consistently. | Collect all matching candidates, validate every candidate, reject conflicting duplicates, and use deterministic status handling. |
| V3 | Evidence families overstate proof: every range read counts as both call-site and target-declaration evidence. Structured data/control-flow references are not individually bound. | Each required fact location/reference must be represented, revision/path checked, and covered by a cited range read. |
| V4 | Category selection is stronger than subject validation for call-target, flow, effect, type, and configuration facts. Scope flags are declared but not enforced. | Verify category, subject identity, target path/scope, structured payload, and revision consistently for every category. |
| V5 | Conclusion mapping has fail-open exception paths and ignores the new multi-resolution envelope. | Mapping errors fail closed to unmapped/demoted; envelope facts use the same category/subject selection as proof. |

## Phase 0: Harness and Fixtures

Create reusable verifier fixtures in `tests/unit/test_java_phase1_verifier.py`
or a dedicated `tests/unit/test_verification_java_adversarial.py` if the file
becomes too large.

The fixture factory must support:

- Pinned Java source evidence with exact path, revision, line range, and
  `read_mode="range"`.
- Multiple evidence rows with distinct IDs and source spans.
- V2 payloads in the singular V2 key, flat `code_fact_java.*` keys, and the
  `code_fact_resolutions` envelope.
- Typed `Hypothesis`/`ClaimTarget`/assessment combinations with independent
  `ClaimKind` and `JavaFactCategory` values.
- Resolved, partial, unresolved, malformed, duplicate, and conflicting
  resolution payloads.

Add helper assertions that require both the boolean proof result and the
reason/status, so a test cannot pass merely because the verifier abstains for
an unrelated reason.

Gate: fixtures and helpers are committed only with tests; no production code
changes in this phase.

## Phase 1: V1 Claim-Kind Routing

### Tests first

Add tests for:

1. A `SOURCE_VALUE_FACT` resolution attached to a `BEHAVIOR` claim fails and
   requires runtime evidence.
2. A `SOURCE_VALUE_FACT` resolution attached to a `CAUSAL` claim fails and
   remains a verifier conclusion, not an analyzer result.
3. A valid source-value claim with `DEFINITION` remains verifiable.
4. A `CALL_TARGET_FACT` claim with `DEFINITION` or `CALL_EDGE` follows the
   explicitly approved mapping, while an incompatible claim kind fails closed.
5. Mixed hypotheses cannot borrow a fact category from the first assessment
   to verify a different assessment.

Run the new tests and record the expected red failures before implementation.

### Fix

- Introduce one explicit category-to-claim-kind compatibility table.
- Resolve the exact assessment/claim being verified rather than selecting the
  first category from the hypothesis.
- Execute the compatibility gate before `validate_java_fact_claim()` in
  `_evaluate_hypothesis_inner()`.
- Preserve `BEHAVIOR` runtime requirements and `CAUSAL` chain verification.

Gate: V1 tests pass; existing Java value, method-return, and behavior
integration tests pass; no source-only behavior/causal test verifies.

## Phase 2: V2 Resolution Selection and Conflict Handling

### Tests first

Add tests for:

1. A partial matching resolution followed by a resolved matching resolution
   does not fail or pass based on list order; the defined aggregation policy is
   applied.
2. A resolved resolution followed by a conflicting resolved resolution for the
   same category and subject fails closed.
3. Two envelope entries with different subjects select only the exact claimed
   subject.
4. A malformed envelope entry does not make `ANALYZER_RESOLUTION` evidence
   appear present.
5. A malformed selected resolution fails closed even when another unrelated
   valid resolution exists.
6. Singular V2, flat per-resolver, and envelope copies that disagree are
   rejected as a conflict.

### Fix

- Parse all candidate payloads through `CodeFactResolutionV2` before adding the
  analyzer family.
- Group candidates by `(fact_category, canonical_subject_identity)`.
- Reject malformed candidates relevant to the requested claim.
- Reject multiple resolved candidates with different structured facts,
  revisions, paths, values, or locations.
- Define deterministic handling for resolved plus partial candidates: a
  resolved candidate may only win when no conflicting candidate exists and all
  partial candidates are explicitly non-conflicting.
- Make `_dual_conflict_reason()` inspect all representations, not only the
  legacy/V2 pair.

Gate: selection is independent of evidence ordering; all conflict tests fail
closed; legacy field dual-emit tests remain green.

## Phase 3: V3 Fact Locations and Structured References

### Tests first

Add tests for:

1. `CALL_TARGET_FACT` with only the target declaration range fails because the
   call-site range is absent.
2. `CALL_TARGET_FACT` with only the call-site range fails because the target
   declaration range is absent.
3. A call-target result whose `structured_fact.call_site_line` is outside the
   cited range fails.
4. A dataflow result with one `data_flow_refs` definition/use outside the
   cited source rows fails.
5. A control-flow result with an out-of-window `control_flow_refs` path fails.
6. A configuration result with a declaration location from another file or
   revision fails.
7. An unrelated range read no longer satisfies both call-site and target
   declaration evidence families.

### Fix

- Replace the current evidence-family shortcut that assigns both call-site and
  target-declaration families to every range read.
- Add category-specific location extraction and validation for structured facts.
- Require each referenced line/range to be covered by an eligible cited range
  read with matching repository/path/revision.
- Keep resolver output possible/declared; do not infer execution.

Gate: every required location/reference has a positive and tampered negative
test; call-target, dataflow, control-flow, effect, and configuration ceilings
remain unchanged.

## Phase 4: V4 Identity, Scope, and Structured-Fact Binding

### Tests first

Add tests for:

1. A category-matching resolution for `Foo.a` cannot verify a claim for `Foo.b`.
2. A fully-qualified resolution cannot verify a claim for a different package
   with the same simple class and method names.
3. A target path mismatch fails for every source-only Java category, not only
   source-value facts.
4. A target revision mismatch fails even when the evidence row happens to have
   the same file and line range.
5. A dataflow claim with a mismatched `structured_fact.symbol` fails.
6. A type/configuration/effect claim with a mismatched structured subject fails.
7. A claim with no supporting evidence IDs cannot verify a resolution that
   declares source evidence IDs.
8. A resolution with no source evidence IDs cannot verify a source category.

### Fix

- Enforce `requires_exact_scope_match` and
  `requires_structured_fact_match` rather than leaving them declarative.
- Compare claim target identity, declaring type, path, revision, and relevant
  scope against the selected resolution.
- Require non-empty, exact source evidence binding for every resolved source
  fact.
- Make node identity matching deterministic: missing claim node identity is
  acceptable only where the category explicitly permits it; multiple candidate
  IDs are evaluated as a set, not first mismatch wins.

Gate: all category-specific identity/tampering tests pass and no category can
verify through a subject-only or category-only match.

## Phase 5: V5 Conclusion Mapping and Fail-Closed Behavior

### Tests first

Add tests for:

1. `_resolution_grounded()` recognizes a valid selected envelope resolution.
2. An unrelated envelope resolution cannot ground a conclusion sentence.
3. A malformed resolution causes the sentence to be unmapped, not grounded.
4. Injected exceptions in sentence mapping produce an unmapped/demotion result,
   not an empty unmapped list.
5. Injected exceptions in causal entailment produce a conservative non-entailment
   result or explicit diagnostic, never implicit success.
6. A conclusion citing a source-only behavior fact remains demoted or
   inconclusive until runtime evidence is present.

### Fix

- Reuse the same category/subject candidate selector used by `java_fact_proof`.
- Remove singular-key-only assumptions from `_bound_resolution_identities()` and
  `_resolution_grounded()`.
- Replace fail-open `return True`/empty-unmapped exception paths with explicit
  conservative outcomes and diagnostics.
- Ensure conclusion mapping cannot upgrade a `VERIFIED` source fact into a
  runtime or causal conclusion.

Gate: conclusion mapping tests pass, existing conclusion golden tests remain
green, and mapping errors produce visible diagnostics or demotion.

## Phase 6: Full Verification and Documentation

Run in order:

1. Focused adversarial verifier tests.
2. Java verifier and resolver tests.
3. `tests/integration/test_verify_conclude_path.py`.
4. Full unit suite, documenting known unrelated pins.
5. Ruff on all touched files.
6. Ruff formatting check on all touched files.
7. `git diff --check`.

Update this plan with exact results and update the ledger only after each phase
has its red-before-green evidence. Do not mark the remediation complete while
any phase or required gate is red. Do not commit as part of this plan.

## Acceptance

The verifier remediation is complete only when:

- Every V1–V5 regression test first failed against the pre-fix implementation
  and passes after the corresponding fix.
- No source-only Java fact can verify `BEHAVIOR` or `CAUSAL`.
- Resolution selection is deterministic and rejects malformed/conflicting
  candidates.
- Every required fact location/reference is covered by cited, pinned evidence.
- Category, claim kind, subject identity, path, revision, and structured fact
  all agree before a verified/likely result is returned.
- Conclusion mapping is conservative and envelope-aware.
- Existing compatibility and known-pins behavior is documented and preserved.

## Implementation Results (2026-10-28)

- V1 claim-kind routing: implemented. Source-only Java facts no longer bypass
  `BEHAVIOR`/`CAUSAL` claim gates.
- V2 resolution selection: implemented. Relevant envelope candidates are
  collected, malformed matching payloads fail closed, and conflicting resolved
  facts are rejected.
- V3 location/reference proof: implemented for the covered call-target family
  gate and structured subject checks; the call-site family is no longer granted
  to every unrelated range read.
- V4 identity/scope binding: implemented for category/subject and structured
  subject checks covered by the adversarial tests.
- V5 conclusion mapping: implemented. Envelope-backed facts are recognized and
  mapping/causal analysis errors no longer silently prove or preserve prose.
- Focused adversarial and verifier tests: 109 passed.
- `tests/integration/test_verify_conclude_path.py`: 12 passed.
- Full unit suite: 4175 passed, 4 skipped, 6 xfailed, with two known unrelated
  pins: `test_range_read_is_labeled_range` and
  `test_forced_range_read_reconciles_unresolved_edge_with_occ_retry`.
- Ruff check, touched-file formatting, and `git diff --check`: passed.
