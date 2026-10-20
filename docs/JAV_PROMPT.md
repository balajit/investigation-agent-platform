# Review Prompt — Comprehensive Java Code Fact Resolution Across the Java Language

## 1\. Mission

Perform a comprehensive, repository-specific review of IAP's existing Java code-analysis capabilities and define the implementation roadmap for extending deterministic `CodeFactResolution` across the full practical anatomy of the Java language.

The initial Java field-initialization review established the baseline:

- Source retrieval is not semantic resolution.
- Symbol discovery is not proof of a claimed value.
- A verified file, symbol, and line range does not prove that an asserted value is correct.
- Initialization, execution, mutation, configuration, and runtime-effective values are different semantic questions.
- Unsupported or ambiguous analysis must remain explicitly partial or unresolved.

**Your task is to generalize these principles across Java, starting from the existing field-resolution work and expanding systematically into declarations, expressions, types, methods, control flow, exceptions, object construction, inheritance, interfaces, generics, collections, concurrency, frameworks, and runtime behavior.**

The deliverable must identify actual implementation gaps against the current repository, not simply propose a theoretically complete Java analyzer.

This is a review and planning task. Do not modify source code, tests, configuration, database state, migrations, live data, deployment, commits, or releases.

## 2\. Existing IAP architecture to preserve

Review the current implementation of the following components and their actual call paths:

1. Evidence Gateway: authorization, tenant/application scoping, provider selection, source retrieval, revision requirements, sanitization, and provenance.
2. Code-intelligence and source providers: Tree-sitter parsing, symbol discovery, Git-backed source retrieval, revision handling, and line-range evidence.
3. Code analysis and topology: symbol identities, AST representations, call graphs, exception paths, database operations, and structural relationships.
4. Context compilation and investigation reasoning: question-shape classification, code-action selection, evidence banking, code packets, and typed claim generation.
5. Domain models: `CodeSymbol`, `CallGraph`, `ExceptionPath`, `DatabaseOperation`, `DecisionClaim`, `ClaimKind`, `ClaimTarget`, `Evidence`, and `SourceLocation`.
6. Existing `CodeFactResolution` implementation, if now present. Verify its actual contract rather than assuming that the proposed design was implemented.
7. Existing claim verifier: source-range checks, symbol grounding, value verification, behavior verification, scoped-absence handling, confidence, limitations, and final conclusion mapping.
8. Persistence and workflow paths: evidence identifiers, investigation scope, retries, replay determinism, hypothesis assessments, and conclusion generation.
9. Existing tests, fixtures, evaluation scripts, and recommendation/implementation ledgers.

Trace each capability from user question to final conclusion. Identify where deterministic analysis should run, what evidence it consumes, how its result is represented, and how the verifier enforces that result.

Do not rebuild the gateway, add another agent framework, duplicate the verifier, or introduce new infrastructure without concrete evidence that existing abstractions cannot support the requirement.

## 3\. Review the whole practical anatomy of Java

Build a coverage matrix for the language areas below. These categories are mandatory review areas, not a requirement to implement every capability immediately.

### A. Declarations and initialization

- Local variables, parameters, fields, static fields, final fields, and blank final fields.
- Primitive and reference defaults; declaration without initializer.
- Field initializers and static/instance initializer blocks.
- Declaration order and initialization order.
- Constructor execution, constructor chaining using `this(...)`, superclass construction using `super(...)`, and constructor delegation.
- Instance initialization versus class initialization.
- Compile-time constants and constant expressions.
- Definite assignment and effectively-final variables.
- Shadowed fields, inherited fields, and ambiguous symbol resolution.
- Enum constant initialization and initialization of nested classes.
- Records, compact constructors, and record component initialization.

### B. Types and type relationships

- Primitive types, reference types, arrays, and `void`.
- Classes, abstract classes, interfaces, annotations, enums, and records.
- Inheritance, interface implementation, default interface methods, and sealed hierarchies.
- Generic types, type parameters, bounds, wildcards, and type erasure.
- Nested, inner, local, anonymous, and static nested classes.
- Covariant returns, method overloading, method overriding, and field hiding.
- Access modifiers, visibility, package scope, and module boundaries.
- `var` local-variable type inference.
- Boxing, unboxing, widening, narrowing, casts, and conversion rules.
- Nullability implications and reference-type defaults.

### C. Expressions and value computation

- Literals, operators, precedence, and associativity.
- Arithmetic, comparisons, boolean logic, bitwise operations, and shifts.
- Conditional expressions and the ternary operator.
- String concatenation and string templates where supported by the target Java version.
- Assignments, compound assignments, increment/decrement, and side effects.
- Casts, `instanceof`, pattern matching, and switch expressions.
- Method invocations, chained calls, field access, array access, and constructor expressions.
- Compile-time constant evaluation versus runtime evaluation.
- Integer overflow, floating-point behavior, short-circuit evaluation, and evaluation order.
- Unsupported or side-effect-dependent expressions.

For each expression category, define which cases can be evaluated deterministically without executing application code and which require runtime evidence.

### D. Methods and dispatch

- Method declarations, signatures, return types, and parameter binding.
- Overloaded-method resolution.
- Overridden-method dispatch and interface default methods.
- Static methods, instance methods, private methods, and final methods.
- `this`, `super`, and qualified member access.
- Varargs, recursion, lambdas, and method references.
- Return paths, early returns, and unreachable code.
- Side effects on fields, parameters, collections, and external resources.
- Calls across module and dependency boundaries.
- Reflection, dynamic proxies, generated methods, and runtime dispatch.

Distinguish a statically identifiable method target from proof that the method executes in a particular investigation.

### E. Control flow and data flow

- `if/else`, nested conditions, loops, enhanced `for`, and `while`.
- `switch` statements and expressions.
- `break`, `continue`, labelled statements, and `return`.
- `try/catch/finally`, try-with-resources, and suppressed exceptions.
- Definite assignment, reaching definitions, and variable use-before-assignment.
- Conditional assignments and path-dependent values.
- Branch predicates, short-circuit logic, and null checks.
- Data-flow dependencies between inputs, transformations, and outputs.
- Bounded loops and recursion versus unbounded or path-explosive analysis.

Determine which analyses are suitable for a narrow deterministic implementation and where conservative unresolved outcomes are required.

### F. Exceptions and failure propagation

- Checked and unchecked exceptions.
- Explicit throws, implicit exceptions, and exception propagation.
- Catch-block selection and exception hierarchy.
- `finally` behavior, resource cleanup, and suppressed exceptions.
- Exception wrapping and cause chains.
- Declared exceptions versus actual possible failures.
- Retry handling, exception translation, fallback behavior, and error swallowing.
- Transaction rollback or partial completion when application/framework semantics are involved.

Do not infer that a catch handler executes simply because it exists.

### G. Collections, arrays, and object state

- Arrays and array initialization.
- Lists, sets, maps, queues, and other standard collection interfaces.
- Common collection implementations and their documented semantics.
- Mutation through collection methods, iterators, aliases, and shared references.
- `equals`, `hashCode`, comparators, ordering, and duplicate behavior.
- Immutable collections, unmodifiable views, and mutable backing collections.
- Streams, intermediate operations, terminal operations, and laziness.
- Optional values and null-handling conventions.
- Object identity, aliasing, and escape of references.
- Mutable state across method and object boundaries.

Do not assume collection contents or object state from a declaration alone.

### H. Lambdas, functional interfaces, and asynchronous execution

- Lambda expressions and captured variables.
- Functional interfaces and method references.
- Streams and deferred execution.
- `CompletableFuture`, executors, tasks, callbacks, and completion handlers.
- `Future`, cancellation, timeouts, and exception completion.
- Ordering guarantees, happens-before relationships, and thread visibility.
- Synchronization, locks, volatile fields, atomic variables, and concurrent collections.
- Race conditions and non-deterministic scheduling.
- Reactive libraries and framework-managed asynchronous execution.

Static analysis must distinguish a possible execution path from a proven execution history.

### I. Annotations, reflection, and code generation

- Built-in and custom annotations.
- Retention and target metadata.
- Annotation processing and generated sources.
- Reflection-based field and method access.
- Dynamic proxies and service discovery.
- Dependency injection and framework-managed initialization.
- Lombok and other compile-time code-generation tools.
- Compiler plugins and build-generated classes.

Identify the boundary between source-verifiable facts and behavior that requires generated-source, build, configuration, or runtime evidence.

### J. Resources, modules, and external interactions

- `AutoCloseable`, try-with-resources, and resource lifetime.
- File and network I/O.
- JDBC and persistence APIs.
- Transactions, commit/rollback, and isolation semantics.
- HTTP clients, message brokers, caches, and external services.
- Configuration properties, environment variables, system properties, and command-line arguments.
- Java modules, packages, classpaths, dependencies, and service loading.
- Serialization, deserialization, and validation.
- Time, locale, randomness, and environment-dependent values.

For each category, specify whether source alone can establish the fact, whether configuration or dependency metadata is needed, or whether observed runtime evidence is required.

### K. Build and language-version semantics

- Target Java release and compiler flags.
- Language features gated by Java version.
- Annotation processors and generated sources.
- Build profiles, dependency versions, compiler configuration, and conditional source sets.
- Preview features and differences between source and deployed bytecode.
- Source revision versus deployed artifact identity.

Do not silently apply semantics from a newer Java version than the repository targets.

## 4\. Required fact and claim taxonomy

Determine whether existing claim types can represent the following distinctions safely. Reuse the existing taxonomy wherever possible; recommend a new type only if a demonstrated contract gap requires it.

- `DECLARATION_FACT`: declared name, type, signature, modifier, or location.
- `SOURCE_VALUE_FACT`: explicit literal or supported deterministic expression.
- `LANGUAGE_SEMANTIC_FACT`: value or behavior mandated by Java language rules.
- `CONTROL_FLOW_FACT`: a statically established branch, return, or exception path.
- `DATA_FLOW_FACT`: a supported relationship between definitions, assignments, and uses.
- `CALL_TARGET_FACT`: a statically resolved invocation target.
- `POSSIBLE_EFFECT_FACT`: a potential mutation, exception, or side effect.
- `CONFIGURATION_FACT`: a declared configuration mapping or binding.
- `RUNTIME_OBSERVATION_FACT`: behavior demonstrated by trace, logs, database state, or other runtime evidence.
- `CAUSAL_CLAIM`: a claim that a mechanism caused an observed application outcome.

For each category, define the required provenance, permissible resolution statuses, verifier rule, and conditions under which the final answer must abstain.

A possible causal explanation must not be promoted merely because its individual code locations exist.

## 5\. CodeFactResolution contract

Inspect the existing contract and propose the smallest compatible extension needed for the wider Java coverage.

The contract must be able to express:

- Target identity: repository, qualified symbol/member identity, source file, and source range.
- Immutable source revision or content identity.
- Fact category and semantic stage.
- Resolved value or structured fact, when supported.
- Supporting evidence IDs and exact source provenance.
- Relevant declarations, assignments, invocation targets, or control-flow locations.
- Resolution status: resolved, partial, or unresolved.
- Analysis method and applicable Java language version.
- Explicit assumptions and limitations.
- Conflicting evidence and unresolved dynamic behavior.
- Whether the result establishes source semantics, a possible behavior, or an observed runtime fact.

Assess whether this should remain an ephemeral result passed into the existing verifier, be attached to existing evidence, or eventually require persistence. Do not recommend schema or API changes without a concrete consumer and compatibility analysis.

## 6\. Deterministic analysis design

Define a layered approach that can grow incrementally:

1. **Revision-pinned source acquisition:** obtain source evidence through the existing Evidence Gateway and a provider that honors the requested revision.
2. **Parsing and identity normalization:** use the existing parser where suitable; define stable symbol identity across providers and revisions.
3. **AST fact extraction:** extract declarations, types, modifiers, initializers, assignments, methods, calls, and structural relationships.
4. **Local semantic evaluation:** compute supported language defaults, constants, and safe expressions.
5. **Bounded relationship analysis:** follow constructor chains, assignments, overrides, and control-flow relationships within a documented scope.
6. **Resolution assembly:** produce a structured `CodeFactResolution` with provenance and limitations.
7. **Claim verification:** validate the asserted fact against the resolution, not merely the cited file or symbol.
8. **Conclusion enforcement:** preserve partial/unresolved status and prevent unsupported values or causal claims from becoming verified conclusions.

For every layer, identify existing repository components to reuse, missing capabilities, exact candidate files, and regression tests.

Avoid general symbolic execution, arbitrary application execution, or unsandboxed project builds as prerequisites for the initial implementation.

## 7\. Verifier requirements

Review all verifier shortcuts and conclusion-mapping paths that might incorrectly promote a claim.

The verifier must distinguish:

- A location that exists from a value that is correct.
- A declaration from an initialized value.
- An initializer from a post-construction value.
- A possible write from a write that executes.
- A possible method target from the method that actually ran.
- A statically inferred behavior from a runtime observation.
- A configuration declaration from the configuration actually applied.
- A possible causal mechanism from an evidenced root cause.

Inspect numeric and string values, booleans, null, enums, constants, expressions, and structured claims. Avoid brittle substring matching as the sole semantic proof mechanism.

Require consistent repository and revision identity across discovery, source evidence, analysis, and verification. A revision mismatch must fail closed or explicitly invalidate the affected result.

## 8\. Prioritization and implementation phases

Produce a dependency-aware roadmap with acceptance gates.

At minimum, evaluate these phases:

- **Phase 0 — Baseline and contract audit:** verify the actual state of the field-resolution work and existing tests.
- **Phase 1 — Verifier integrity:** prevent unsupported value, behavior, and conclusion-mapping claims from being verified.
- **Phase 2 — Java declarations and expressions:** fields, locals, parameters, defaults, constants, literals, and safe expressions.
- **Phase 3 — Initialization and object construction:** static/instance initialization, constructor chaining, inheritance, and field hiding.
- **Phase 4 — Methods, overrides, and data flow:** overload resolution, dispatch, assignments, returns, and bounded local flow.
- **Phase 5 — Control flow and exceptions:** branch conditions, possible execution paths, exception propagation, and resource handling.
- **Phase 6 — Types and advanced language features:** generics, lambdas, records, sealed types, pattern matching, and supported language-version features.
- **Phase 7 — Frameworks and external boundaries:** dependency injection, configuration binding, generated code, persistence, messaging, and asynchronous execution.
- **Phase 8 — End-to-end investigation evaluation:** validate question routing, evidence retrieval, resolution, verification, and final-answer semantics.

These are candidate phases, not authorization to implement them. Reorder or split them based on actual dependencies and measured value.

For each phase provide:

- Objective and non-goals.
- Existing components and exact source paths.
- Confirmed gaps versus unverified assumptions.
- Required contract changes.
- Regression and integration tests.
- Security, tenant, revision, and compatibility considerations.
- Acceptance criteria and failure recovery.
- Dependencies and explicit go/no-go gate.

## 9\. Evaluation strategy

Create synthetic Java fixtures pinned to exact Git commits. Include positive, negative, ambiguous, inherited, shadowed, and dynamically unresolved examples.

For each supported feature, test both correct and incorrect claims. Include counterexamples where source evidence is valid but the asserted interpretation is false.

Required invariants:

- No claim is verified solely because its source location is valid.
- No deterministic fact combines evidence from different revisions.
- No possible execution path is described as observed execution.
- No source initializer is described as the runtime-effective value without adequate evidence.
- Unsupported Java constructs produce explicit partial/unresolved results.
- Tenant and application boundaries remain enforced.
- Evidence IDs and source ranges survive the full investigation pipeline.
- Final conclusions preserve limitations and conflicting evidence.

Evaluate precision, recall within the declared supported subset, false-verification rate, abstention correctness, provenance correctness, and repeatability. Measure each separately; do not optimize coverage by weakening verification.

Separate deterministic unit tests, database/integration tests, and live LLM or application evaluation. Identify environment prerequisites and report blockers honestly.

## 10\. Mandatory output format

Deliver the review in the following sections:

1. Executive verdict and highest-risk gaps.
2. As-built architecture and verified end-to-end call paths.
3. Java language coverage matrix: implemented, partial, missing, or unverified.
4. Fact taxonomy and existing-claim compatibility assessment.
5. Proposed `CodeFactResolution` contract and provenance invariants.
6. Deterministic analysis architecture and boundaries.
7. Verifier integrity review, including bypasses and false-acceptance risks.
8. Prioritized implementation roadmap with acceptance gates.
9. Java fixture and evaluation matrix.
10. Risks, limitations, open questions, and deferred capabilities.
11. Exact repository files and tests to inspect or change in each future phase.
12. Final recommendation: the smallest next safe implementation increment and the evidence required to authorize it.

## 11\. Evidence and repository discipline

- Inspect current source and tests before making claims about what exists.
- Cite exact repository-relative file paths and line ranges for every important finding.
- Label each finding as confirmed, inferred, or unverified.
- Distinguish observed test results from proposed tests and expected behavior.
- Do not treat historical reports as evidence of current HEAD behavior.
- Inspect the working tree and preserve existing user modifications.
- Do not overwrite operator changes or silently normalize unrelated files.
- Do not run destructive cleanup, database migrations, or live-data operations.
- Do not print or persist secrets or sensitive environment values.
- Do not implement changes during this review.
- Do not expand the work into a new agent framework, generic plugin system, separate reasoning engine, or broad platform rewrite.

## 12\. Final decision standard

The objective is not to make IAP claim it understands all of Java.

The objective is to make IAP **reliably resolve a clearly declared subset of Java facts, prove each answer against the exact source and language semantics, and explicitly identify where execution, framework behavior, or external state prevents a deterministic answer.**

Recommend implementation only when the proposed increment has a narrow semantic contract, a reproducible counterexample or capability gap, and a testable acceptance gate.

The most important outcome is a coverage matrix that separates three levels:

- Deterministic source facts: declarations, literal initializers, constants, and supported language rules.
- Static behavioral analysis: potential assignments, dispatch, control flow, and exception paths, with explicit limits.
- Runtime-dependent facts: actual configuration, executed branches, transaction outcomes, concurrent behavior, and observed production state.

