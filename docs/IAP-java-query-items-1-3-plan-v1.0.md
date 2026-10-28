# Java Query Items 1-3 Plan v1.0

Status: IMPLEMENTED 2026-10-28. This document covers only Item 1 (Q4 migration completion), Item 2 (closed vocabulary semantics), and Item 3 (A7 bucket boundary). It does not authorize Items 4-10 or any unrelated Java-analysis changes.

## Goals

1. Complete the Q4 migration sequence through the dataflow engine without weakening its bounded, fail-closed behavior.
2. Make `PARTIAL`, `UNRESOLVED`, absent records, syntax-error coverage, and evaluator limitations explicit in the standalone vocabulary specification.
3. Freeze the model boundary so new query capability requires an explicit vocabulary amendment and promoting question.

## Non-goals

- No use-def/constant propagation beyond the existing dataflow resolver contract.
- No guard/barrier predicate family.
- No framework marker externalization.
- No lvalue/rvalue model changes.
- No Tree-sitter query-substrate rewrite.
- No model-build benchmark or performance optimization.

## Item 1: Finish Q4 Through Dataflow

### Phase 1: Inventory and contract freeze

- Enumerate every helper and branch in `dataflow.py`: method selection, local/parameter hosting, concurrency and stream abstentions, declaration/use collection, assignment/update handling, rebound detection, out-of-window references, early returns, reaching-definition references, and definite-assignment outcomes.
- Identify every data shape already available in `JavaModel`: methods, locals, params, calls, spans, and syntax status.
- Record the dataflow-specific facts that are not currently in the model as explicit candidate records before implementation. At minimum: assignment/update events, identifier uses, return sites, and scope/binding identity.
- Freeze the existing result contract: status, limitation codes, structured fact keys, `data_flow_refs`, declaration location, subject identity, and parser/version provenance.

### Phase 2: Model discovery additions

- Add typed records only for facts required by the existing resolver, using the same frozen Pydantic conventions as the current model.
- Add class-qualified method identity to every method-scoped dataflow record; method name alone is not a binding identity.
- Preserve source spans and revision/file provenance on every record.
- Keep concurrency and stream marker checks as resolver-level/window-level safety rules unless parity evidence demonstrates that a typed model record is required. Do not silently convert them into ordinary declaration features.
- Add query accessors for dataflow records only after documenting their truth conditions in the vocabulary.

### Phase 3: Parallel discovery and parity harness

- Extract the current tree-bound discovery into a clearly named legacy/reference function without changing its behavior.
- Implement model-backed discovery beside it.
- Build an automated parity harness over every dataflow case in the Java fixture manifest and all dedicated dataflow tests.
- Compare decision-relevant output, not incidental serialization: status, limitation-code set/order policy, structured-fact kind and definitions/uses/early-returns, data-flow references, subject identity, and declaration location.
- Include explicit counterexamples for nested methods, same-name bindings in separate methods, assignment versus compound assignment, update expressions, use-before-definition, method arguments that imply aliasing, stream markers, concurrency markers, and out-of-window references.
- Treat any mismatch as a migration blocker. Do not rely on manual spot comparison.

### Phase 4: Switchover and cleanup

- Switch `resolve_java_data_flow` to model-backed discovery only after the parity harness is green.
- Retain the legacy function temporarily as a test oracle for the migration suite; do not expose it as production behavior.
- Remove dead tree-bound rebound and discovery helpers only after the parity test no longer depends on implementation-only details.
- Update the ledger and migration documentation with exact test counts and known residual limitations.

### Item 1 exit gate

- All existing dataflow tests pass.
- Automated legacy-vs-model parity passes across every dataflow fixture and limitation path.
- No resolver behavior changes are accepted without a new, explicit contract entry.
- Full unit suite, Ruff, formatting, and diff checks pass.

## Item 2: Closed Vocabulary Semantics

### Phase 1: Define outcome classes

Add a normative section to `docs/IAP-java-query-vocabulary-v1.0.md` defining:

- `RESOLVED`: the requested record exists in the analyzed windows and satisfies the predicate's exact truth conditions; no execution or runtime claim is implied.
- `PARTIAL`: a relevant record exists, but the model or bounded analysis cannot establish complete coverage or a required relationship. A predicate may expose the record and its limitation, but must not upgrade the result to a complete positive claim.
- `UNRESOLVED`: no authoritative matching record or decision exists in the analyzed scope, or the analysis cannot safely produce one. This is not a negative claim.
- Missing record: absence from the supplied model/window only; never evidence that the source lacks the entity globally.
- `has_syntax_error`: records from an erroneous window may be queryable, but coverage is explicitly incomplete and no absence predicate may infer false.
- `limitation:*`: a closed evaluator/analysis limitation attached to a record or result; it is not equivalent to a negative predicate result.

### Phase 2: Add exact truth tables

- Add tables for record presence versus coverage state.
- Define the output behavior of every predicate family under complete, partial, unresolved, syntax-error, limitation, and missing-index inputs.
- Explicitly distinguish `false` from `unknown/withheld` where the current Boolean API cannot express the distinction. If a predicate remains Boolean, document that it returns `false` only because the requested record does not satisfy the local record condition, not because the global proposition is disproven.
- Define candidate-set semantics: empty means no same-scope definition record exists in the supplied model, while multiple candidates are surfaced and never silently resolved.
- Define attribution semantics for `in_library`, including ambiguity under same-name/same-arity collisions.

### Phase 3: Contract tests and drift guard

- Add table-driven tests for each predicate family across complete, partial, syntax-error, limitation, and missing-record cases.
- Add a vocabulary completeness test that imports every public predicate in `query.py` and requires a documented entry or an explicit private/internal exemption.
- Add a closed-kind test so new `ArgSketch.kind` prefixes require a vocabulary amendment.
- Correct the current documentation indentation and keep the A7 bucket list synchronized with the predicate catalog.

### Item 2 exit gate

- Every public predicate has exact truth conditions and explicit partial/absence semantics.
- Tests fail when a new public predicate or kind is added without vocabulary documentation.
- No documentation says that absence or `PARTIAL` is a negative claim.

## Item 3: Freeze A7 Buckets

### Phase 1: Reconcile the inventory

- Produce a definitive inventory of every `JavaModel` field and nested record.
- Classify each field as `IN_MODEL`, `ANNOTATED`, `OUT_OF_MODEL`, or `DEFERRED`.
- Define `OUT_OF_MODEL` explicitly as facts requiring cross-window, repository-wide, runtime, framework-version, or semantic authority not carried by the current window model. This category is distinct from `DEFERRED`: out-of-model describes the boundary; deferred describes a capability intentionally awaiting a promoting question and implementation decision.
- Ensure every annotated field states that it is diagnostic/context text and cannot independently satisfy a proof or query claim.

### Phase 2: Promoting questions

Give every deferred capability one exact question whose answer would justify promotion. Initial entries:

- Cross-window call graph: “Which callers of method X exist across the repository at revision R?”
- Constructor/init ordering: “Which constructor or initializer establishes field F before method M reads it?”
- Framework boundaries: “Which framework endpoints does this revision expose, with mapping annotation and path evidence?”
- Generic type arguments: “Which declarations use parameterized type T<U>, and what are the declared arguments?”
- Annotation entities: “Which declarations carry annotation @A, with its source arguments?”
- Exception flow: “Which exceptions can escape method M along each bounded path?”
- Lambda capture/use-def: “Which variables does lambda L capture, and where are they assigned before capture?”
- Use-def links: “Where is local V assigned, read, and invalidated within method M?”
- Comments/Javadoc: “Which public declarations lack a documentation comment in this window?”
- Whole-subtree type features not represented by the current records: retire this generic placeholder and replace it with the specific feature question that motivates the next record.

### Phase 3: Boundary enforcement

- Add a documentation table mapping every model field and public query predicate to one A7 bucket.
- Add tests that assert deferred/out-of-model capabilities are not accidentally exposed through existing predicates.
- Require a vocabulary/spec change before adding a new public model field or predicate.
- Record any intentional exception as a ledger item rather than silently expanding Q3.

### Item 3 exit gate

- Every current model field, record, and public predicate has exactly one documented bucket.
- Every deferred capability has one promoting question.
- The generic type-feature placeholder is removed or split into concrete questions.
- The bucket inventory and vocabulary completeness tests pass.

## Execution Order

1. Freeze the Item 1 dataflow contract and Item 3 inventory in documentation.
2. Add Item 2 outcome semantics and vocabulary drift tests.
3. Add model-backed dataflow discovery and the automated parity harness.
4. Run all three exit gates together.
5. Only then switch the production dataflow resolver and remove dead helpers.

## Final Acceptance

Acceptance requires all three item gates, the full Java unit suite, Ruff checks, formatting checks, and `git diff --check`. Any parity mismatch, undocumented public predicate/kind, or unclassified model field leaves the plan incomplete.

## Implementation Evidence

- Item 1: `dataflow.py` now resolves from model-backed binding/event records; the legacy tree-bound implementation remains private as the automated parity oracle. Dedicated dataflow tests cover reaching definitions, early returns, use-before-definition, aliasing, stream/concurrency abstentions, out-of-window references, and missing symbols.
- Item 2: the vocabulary now defines `RESOLVED`, `PARTIAL`, `UNRESOLVED`, missing-record, syntax-error, limitation, and candidate-set semantics. Contract tests cover public predicate documentation, model-field documentation, closed kind prefixes, and outcome wording.
- Item 3: the vocabulary records in-model, annotated, out-of-model, and deferred boundaries; deferred promotion questions remain in this plan and must be reflected in the vocabulary before promotion.
- Verification: full unit suite, Ruff check, Ruff format check, and `git diff --check` passed. No commit or push was performed.
