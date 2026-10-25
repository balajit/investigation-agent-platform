# Principal Engineer Research Assignment: Deep Technical and Specification Review of LTMS

**Target repository:** https://github.com/pisanuw/ltms/tree/main

**Primary deliverable:** `docs/ltms-concepts-capabilities-and-specifications.md`

**Role:** Principal Engineer, Knowledge Representation and Automated Reasoning Architect, Formal Methods Reviewer, Python Runtime Specialist, and Adversarial Code Reviewer.

## 1. Mission

Perform an exhaustive, technically rigorous investigation of the LTMS repository.

The objective is to produce an authoritative engineering document describing:

1. The concepts and capabilities of the system.
2. The theoretical foundations and prior work on which it is based.
3. The specifications, algorithms, contracts, and invariants that govern its behavior.
4. The key entities, data structures, relationships, and state transitions defined by those specifications.
5. The precise goals, assumptions, guarantees, and limitations of each specification.
6. The relationship between the documented design and the actual implementation.
7. The engineering principles that make the system correct, composable, explainable, maintainable, and testable.
8. The areas where implementation behavior, documentation, tests, or stated goals differ.
9. The applicability of the system to an agent-driven investigation platform, including `investigation-agent-platform` (IAP), without presuming that integration is necessarily appropriate.

This is not a README summary, a module-by-module paraphrase, or a superficial architecture review.

**You must investigate the underlying reasoning algorithms and software engineering decisions deeply enough that another Principal Engineer could use the resulting document to evaluate the implementation, reproduce its semantics, identify correctness defects, and design a safe integration into another system.**

Do not modify production code or implement integration changes. This assignment is research, documentation, and technical verification.

## 2. Mandatory Repository Investigation

Start with the actual repository and inspect the complete relevant source tree, not just the README or documentation website.

Repository:

[ ~/workspace/repos/pisanuw/ltms ]
https://github.com/pisanuw/ltms/tree/main

Inspect, where present and relevant:

- `README.md`
- `PLAN.md`
- `STUDY-NOTES.md`
- `BRIEFING.md`
- `CHANGES.md`
- `NOTICE`
- `LICENSE`
- `pyproject.toml`
- The complete `docs/` tree.
- The complete `src/ltms/` tree.
- The complete `tests/` tree.
- The complete `examples/` tree.
- The complete `exercises/` tree.
- CI workflows and development tooling.
- Package exports and public APIs.
- Any documented reference material and upstream theoretical sources.

The list is a starting point, not a substitute for repository discovery.

Use the actual files and symbols found in the repository. Do not assume every file listed exists or that documentation is current.

### 2.1 Inspect source code at implementation depth

For every major subsystem, trace:

1. Public entry points.
2. Internal data structures.
3. State mutation.
4. Algorithm execution.
5. Dependencies on other subsystems.
6. Return values and error conditions.
7. Invariants maintained before and after operations.
8. Tests establishing expected behavior.
9. Known limitations or unimplemented capabilities.
10. Potential correctness and integration risks.

Follow execution paths across module boundaries. A description of a function's purpose is insufficient if its correctness depends on state maintained by another module.

### 2.2 Establish evidence before conclusions

Every substantive technical statement must be classified as one of:

- **Formal specification:** A normative contract or formal definition.
- **Published theoretical foundation:** A concept or algorithm grounded in identifiable research or a reference work.
- **Documented design:** A statement in the repository's documentation or implementation plan.
- **Implemented behavior:** Behavior confirmed by inspecting executable source.
- **Tested behavior:** Behavior supported by an actual test and its assertions.
- **Experimentally verified behavior:** Behavior confirmed by executing a reproducible experiment.
- **Engineering interpretation:** A reasoned conclusion derived from the above.
- **Unverified assumption:** A proposition that cannot yet be established.

Do not conflate these categories.

For example, a design-plan statement does not establish that the implementation is complete. A passing unit test does not establish a general theorem. A successful example does not prove an algorithm is complete or correct for every valid input.

For every significant finding, provide the repository path, relevant symbol, and line range where available. Cite external references with canonical URLs and identify the relevant section, chapter, or definition.

If a statement cannot be verified, say so explicitly.

## 3. Determine What "Specification" Means in This Repository

Before writing the detailed analysis, establish whether LTMS has any formal or normative specifications.

Investigate the following categories separately.

### 3.1 Formal or normative specifications

Look for:

- Mathematical definitions.
- Formal operational semantics.
- Inference rules.
- Preconditions and postconditions.
- State-transition definitions.
- Soundness or completeness claims.
- Formal grammars.
- Explicitly versioned schemas.
- Public API contracts.
- Compatibility guarantees.
- Normative conformance requirements.

Do not invent a formal specification if the repository contains only explanatory prose and executable code.

### 3.2 Theoretical specifications and algorithm definitions

Determine which behavior is derived from the underlying reasoning literature, especially Forbus and de Kleer's *Building Problem Solvers* (MIT Press, 1993).

Identify the relevant chapters, algorithms, concepts, and definitions from authoritative references where accessible.

Explain which algorithms are intended to be faithful implementations of the reference material and which are adaptations introduced for Python, simplified representations, or implementation convenience.

### 3.3 Repository-specific specifications

Inspect whether the following define de facto contracts:

- `PLAN.md`
- `STUDY-NOTES.md`
- Architecture documentation.
- Public APIs.
- Type signatures.
- Tests and conformance examples.
- `.kb` file syntax.
- Error and exception behavior.
- State and lifecycle conventions.
- CI quality gates.

Distinguish normative contracts from implementation details.

### 3.4 Produce a specification inventory

Create a table with these columns:

| Field | Description |
|---|---|
| Specification ID | Stable ID, such as `SPEC-LTMS-001` |
| Name | Specification or contract name |
| Category | Formal, theoretical, API, DSL, algorithmic, behavioral, or engineering |
| Source | Canonical source or repository file |
| Version or revision | Verified version, revision, or commit |
| Goal | Problem the specification is intended to solve |
| Scope | Included and excluded behavior |
| Key entities | Concepts, objects, or structures defined |
| Invariants | Conditions that must hold |
| Operations | Required behavior and state transitions |
| Guarantees | What the specification establishes |
| Limitations | What it does not establish |
| Implementation mapping | Relevant modules, classes, and functions |
| Conformance evidence | Tests, proofs, or experiments |
| Status | Implemented, partial, planned, absent, or unresolved |

If there is no formal specification for a subsystem, create a clearly labeled **derived behavioral contract** based on the theoretical source, implementation, and tests. Mark every derived requirement as proposed rather than upstream-normative.

## 4. Theoretical Foundations and Reasoning Paradigms

Document the underlying concepts before explaining implementation details.

At minimum, investigate the following where supported by the repository.

### 4.1 Knowledge representation

Explain:

- Facts and propositions.
- Ground and compound terms.
- Symbols and variables.
- Structured terms and s-expressions.
- Positive and negative propositions.
- Knowledge bases and world models.
- Pattern matching and unification.
- Logical formulas and connectives.
- The distinction between a fact being present and a proposition being believed.

Explain why the distinction between presence and belief matters when combining a rule engine with a truth maintenance system.

### 4.2 Truth maintenance systems

Explain the purpose of a Truth Maintenance System (TMS).

Investigate and distinguish:

- Logic-based TMS (LTMS).
- Justification-based TMS (JTMS).
- Assumption-based TMS (ATMS), including whether it is in scope or explicitly excluded.
- The relationship between a TMS and a conventional forward-chaining inference engine.
- The difference between maintaining conclusions and maintaining explanations for conclusions.
- The difference between retracting a fact, retracting an assumption, and revising a belief.

Do not assume that these systems have identical semantics.

Explain the problems each paradigm addresses and the trade-offs it introduces.

### 4.3 Boolean logic and constraint propagation

Explain:

- Propositional variables.
- Literals and clauses.
- Disjunctive clauses.
- Conjunctive normal form (CNF).
- Unit clauses and unit propagation.
- Contradictions and inconsistent assignments.
- Nogoods.
- Implications and their clause representation.
- Formula normalization.
- The relationship between logical entailment and propagated belief.

Use small mathematical examples and walk through the actual implementation's behavior.

Explicitly distinguish sound inference from logical completeness.

### 4.4 Dependency-directed reasoning

Explain:

- Dependency graphs.
- Assumptions and their identifiers or reasons.
- Support for a derived proposition.
- Alternative support.
- Retraction propagation.
- Contradiction handling.
- Dependency-directed backtracking.
- Reuse of previously computed information.
- The difference between chronological backtracking and dependency-directed search.

Trace a concrete example through the actual code.

### 4.5 Explanation and well-founded support

Investigate how the system answers questions such as:

- Why is this proposition believed?
- Which premises support it?
- Which assumptions are necessary?
- What changes if an assumption is withdrawn?
- How does the implementation prevent an explanation from depending on circular or ill-founded support?

Explain what constitutes a valid explanation in this implementation and what the implementation does not guarantee.

### 4.6 Rule-based reasoning and unification

Explain:

- Pattern-directed forward chaining.
- Rule triggers and rule bodies.
- Variable bindings.
- Unification.
- Occurs-check behavior.
- Fact indexing.
- Incremental matching.
- Rule registration and activation.
- Nested rules and captured environments.
- Rule execution ordering.
- Duplicate facts and activation behavior.
- The interaction between rule execution and changes in belief.

Distinguish the plain rule engine from the reasoning engine integrated with the LTMS.

### 4.7 Advanced facilities

Investigate and document the actual semantics, algorithms, assumptions, and limitations of:

- Indirect proof.
- Closed-world assumptions.
- Dependency-directed search.
- Optional logical completeness.
- Prime implicates and their computation.
- Watched literals and the associated Boolean constraint propagation implementation.
- Optional SAT solver integration or differential testing.

For each facility, explain why it exists, how it works, when it is useful, and which limitations or complexity costs it introduces.

If a facility is planned rather than implemented, say so.

## 5. Detailed Specification and Key Entity Analysis

Create a comprehensive entity catalog covering the important concepts in the repository.

Potential entities include, subject to actual source verification:

- `Symbol`
- `Var`
- Term
- Binding environment
- Rule
- Fact or datum
- Database class or index bucket
- LTMS node
- Clause
- Assumption
- Informant or assumption reason
- Support
- Justification
- Nogood
- Contradiction handler
- Belief label
- Engine instance
- Trigger
- Rule activation
- Predicate or formula
- Explanation
- Knowledge-base file
- Completeness-related structures
- Watched-literal structures

For every actual entity, document:

1. Stable entity ID.
2. Exact implementation name.
3. Purpose and conceptual meaning.
4. Relevant theoretical definition.
5. Source file and defining symbol.
6. Fields and field semantics.
7. Identity and equality behavior.
8. Relationships with other entities.
9. Ownership and lifecycle.
10. Valid and invalid states.
11. Creation and mutation operations.
12. Invariants.
13. Error conditions.
14. Serialization or persistence behavior, if any.
15. Complexity implications where relevant.
16. Relevant tests.
17. Security or correctness implications.
18. Whether it is public API, internal implementation, or a proposed concept.

Do not describe a proposed entity as an existing class.

### 5.1 Entity relationship model

Create an entity relationship diagram using Mermaid if appropriate.

Show:

- Entity relationships.
- Cardinalities where meaningful.
- Ownership and lifecycle boundaries.
- Support and dependency relationships.
- Links between facts, nodes, clauses, assumptions, and explanations.
- The relationship between rule execution and belief updates.

Do not use a generic class diagram that ignores the semantic differences between presence, support, and belief.

### 5.2 State-transition models

Where stateful behavior exists, provide explicit state-transition tables or diagrams.

At minimum, investigate:

- Belief-state transitions.
- Assumption activation and retraction.
- Support establishment and invalidation.
- Contradiction creation and resolution.
- Rule activation and execution.
- Explanation traversal.
- Completeness-mode behavior, if implemented.

For each transition, specify:

- Initial state.
- Trigger.
- Preconditions.
- State changes.
- Postconditions.
- Possible exceptions.
- Relevant invariant.
- Evidence that the transition is implemented and tested.

## 6. Subsystem-by-Subsystem Technical Analysis

Inspect every relevant source module and document its role and algorithm.

Use the actual repository layout. The following categories are expected to be investigated if present:

### 6.1 Terms and unification

Explain the term representation, variable representation, binding semantics, occurs check, substitution, and failure handling.

Evaluate whether empty bindings can be confused with failure, whether chained substitutions are resolved correctly, and whether cyclic terms are rejected where required.

### 6.2 TRE: pattern-directed reasoning engine

Document indexing, rule registration, matching, activation queues, forward chaining, and execution-order semantics.

Determine whether behavior is independent of the order in which facts and rules are introduced where the implementation claims it should be.

### 6.3 JTMS and JTRE

Document belief labels, justifications, two-phase retraction, and the relationship between justification maintenance and rule execution.

Explain how JTMS behavior differs from LTMS behavior and why both exist.

### 6.4 LTMS core

Document the node model, clause representation, Boolean constraint propagation, support representation, contradiction detection, nogoods, and assumption handling.

Trace the full execution path from adding a formula to deriving a belief.

### 6.5 LTRE

Explain how the rule engine and LTMS are connected.

Document the datum-to-node relationship, formula translation, belief-conditioned rule triggers, assumption guards, retraction, and the propagation of belief changes into rule execution.

Identify the invariants required for the two subsystems to remain consistent.

### 6.6 Retraction, contradiction handling, and explanation

Trace retraction and contradiction recovery end to end.

Determine how the implementation preserves well-founded support, invalidates dependent beliefs, handles alternative support, and avoids leaving stale derived beliefs.

### 6.7 Indirect proof, CWA, and dependency-directed search

Explain the algorithm and control flow of each facility.

Investigate handler composition, temporary assumptions, exception-safe cleanup, nested search, and restoration of engine state.

### 6.8 Logical completeness and watched literals

Establish exactly which capabilities are implemented, optional, planned, or experimental.

Explain the relationship between the primary propagation engine, any completeness extension, and any SAT-backed verification.

Do not conflate faster propagation with stronger logical completeness.

### 6.9 DSL and world-model loading

Document the `.kb` language, grammar, parsing, evaluation, supported operations, error reporting, and expectations.

Investigate whether malformed inputs, unsupported constructs, or unexpected types can leave the engine in a partially updated state.

### 6.10 Testing, packaging, and CI

Inspect:

- Unit tests.
- Conformance tests.
- Property-based tests.
- Differential tests.
- Integration tests.
- Example programs.
- Type checking.
- Linting.
- Supported Python versions.
- Optional dependencies.
- CI execution.

Explain what each testing layer proves and what it leaves untested.

## 7. Algorithm-Level Analysis

For every central algorithm, provide an implementation-grounded explanation.

Include at least:

- Unification.
- Occurs checking.
- Substitution.
- Formula normalization and CNF conversion.
- Boolean constraint propagation.
- Assumption enabling and retraction.
- Support selection and alternative support.
- Contradiction detection and nogood installation.
- Rule matching and activation.
- Dependency-directed search.
- Closed-world reasoning.
- Indirect proof.
- Explanation construction.
- Prime implicate generation, if implemented.
- Watched-literal propagation, if implemented.

For each algorithm, document:

| Dimension | Required analysis |
|---|---|
| Algorithm ID | Stable identifier |
| Goal | Problem solved |
| Inputs | Required values and preconditions |
| Outputs | Return values and state changes |
| Core idea | Mathematical or algorithmic explanation |
| Step-by-step behavior | Execution sequence |
| Data structures | Relevant structures and invariants |
| Correctness argument | Why the algorithm should be correct under stated assumptions |
| Complexity | Time and space estimates with stated assumptions |
| Failure modes | Contradictions, exceptions, nontermination, invalid inputs |
| Implementation mapping | Actual functions and source lines |
| Tests | Existing and missing coverage |
| Limitations | Known restrictions and unresolved concerns |

Do not invent precise complexity bounds where the implementation or algorithm does not justify them. State the assumptions needed for each estimate.

Where useful, provide pseudocode, logical formulas, small execution traces, and counterexamples.

## 8. Engineering Principles and Invariants

Identify the principles that govern the correctness of the system.

Investigate, where relevant:

- Explicit engine-instance state instead of unsafe global mutable state.
- Consistent equality and identity semantics.
- Occurs-check correctness.
- Distinguishing successful empty bindings from failure.
- Separation of term representation, rule matching, and logical reasoning.
- Separation of database presence from belief.
- Consistency between LTMS nodes and LTRE data.
- Well-founded support and avoidance of circular justifications.
- Correct propagation of belief changes.
- Two-phase retraction.
- Safe contradiction-handler composition.
- Exception-safe restoration of temporary assumptions.
- Deterministic behavior where required.
- Isolation between engine instances.
- Incremental computation and reuse.
- Soundness versus completeness.
- Explicit ownership of mutable state.
- Stable public contracts.
- Strict input validation.
- Testability of logical invariants.

For every identified invariant, record:

- Invariant ID.
- Formal or operational statement.
- Why it is necessary.
- Source locations that maintain it.
- Operations that can violate it.
- Tests that protect it.
- Whether the invariant is proven, tested, assumed, or unverified.

Pay special attention to invariants that span multiple modules. A local unit test may not detect a cross-module consistency defect.

## 9. Adversarial Correctness Review

Perform a skeptical review of the actual implementation.

Do not merely restate known limitations already documented by the author. Independently inspect whether the implementation satisfies its own stated goals.

Investigate:

- Incorrect inference or belief propagation.
- Unsound support relationships.
- Circular or ill-founded explanations.
- Stale conclusions after retraction.
- Incomplete contradiction recovery.
- Incorrect assumption identity or ownership.
- Binding corruption.
- Unification edge cases.
- CNF normalization errors.
- Tautology handling.
- Empty formula and empty clause semantics.
- Rule activation ordering.
- Duplicate activation and recursion risks.
- Infinite forward-chaining loops.
- Inconsistent state after exceptions.
- Temporary assumption leakage.
- Incorrect nesting of contradiction handlers.
- State leakage across engine instances.
- Mutation during iteration.
- Incorrect alternative-support selection.
- Incorrect nogood installation order.
- False completeness claims.
- Divergence between the primary engine and optional SAT-backed checks.
- Parser ambiguity and malformed DSL inputs.
- Type-safety and public API defects.
- Unhandled pathological inputs and resource exhaustion.

For every finding, provide:

- Finding ID.
- Severity.
- Package and source file.
- Function or class.
- Line range where available.
- Expected semantics.
- Actual behavior.
- Minimal reproducible example or counterexample.
- Why the issue occurs.
- Consequences.
- Recommended correction.
- Regression test.
- Confidence level.
- Whether the issue is already documented.

Do not report a defect based solely on suspicion. If a behavior is uncertain, construct a targeted experiment or test where feasible, and distinguish confirmed defects from hypotheses.

Do not claim a formal proof of correctness when the analysis establishes only empirical behavior.

## 10. Formal and Behavioral Specification Reconstruction

If no comprehensive formal specification exists, derive a proposed specification from the theory, source, and tests.

Create:

`docs/ltms-derived-behavioral-specification.md`

This document should define the essential behavior independently of implementation details.

Include:

1. Core concepts and terminology.
2. Supported logical language.
3. Term and variable semantics.
4. Rule semantics.
5. Fact insertion and retrieval semantics.
6. Belief-state semantics.
7. Clause and formula semantics.
8. Assumption lifecycle.
9. Retraction semantics.
10. Contradiction semantics.
11. Support and explanation semantics.
12. Closed-world and indirect-proof semantics.
13. Dependency-directed search semantics.
14. Error and exception semantics.
15. Complexity and completeness limitations.
16. Public API contracts.
17. Required invariants.
18. Conformance examples.
19. Explicit non-goals.
20. Open questions.

Use mathematical notation where it improves precision.

For each requirement, identify whether it is:

- Derived from established theory.
- Required by documented repository intent.
- Confirmed by existing behavior.
- A proposed clarification or change.
- Unresolved.

Do not silently turn implementation quirks into intended semantics. Flag discrepancies for adjudication.

## 11. Applicability to Investigation-Agent-Platform

Provide a separate section evaluating the potential use of LTMS in an agent-driven investigation platform.

Do not assume LTMS should be adopted merely because it supports explanations or belief revision.

Evaluate concrete use cases such as:

- Representing investigation hypotheses.
- Expressing relationships between observed symptoms and possible causes.
- Maintaining dependencies between claims.
- Tracking assumptions used during diagnosis.
- Retracting conclusions when assumptions become invalid.
- Explaining why a hypothesis is currently believed.
- Handling contradictory evidence.
- Exploring alternative explanations.
- Evaluating rules over structured incident facts.
- Separating observed facts from inferred claims.

For each use case, distinguish:

1. What LTMS can provide directly.
2. What requires IAP-specific modeling.
3. What requires additional temporal, probabilistic, causal, or distributed reasoning.
4. What must remain outside the reasoning engine.
5. What risks arise from treating logical belief as real-world truth.

Explicitly assess the limitations of propositional reasoning for:

- Time-dependent observations.
- Confidence scores and probabilistic evidence.
- Causal inference.
- Incomplete telemetry.
- Source reliability.
- Conflicting evidence.
- Distributed investigation state.
- Large evidence graphs.
- Long-running investigations.
- Human review and approval.

A logical system's belief state must not be presented as a calibrated probability or factual guarantee.

Recommend an integration only if the required semantics fit the actual IAP architecture and the cost of maintaining the reasoning model is justified.

## 12. Required Deliverables

Create the following Markdown files, following existing repository conventions:

### Deliverable A

`docs/ltms-concepts-capabilities-and-specifications.md`

The primary comprehensive reference document covering:

- Project scope and architecture.
- Theoretical foundations.
- Specification inventory.
- Key entities and relationships.
- Subsystem behavior.
- Algorithm descriptions.
- Engineering principles and invariants.
- Feature and capability matrix.
- Testing evidence.
- Limitations.
- Open questions.
- Applicability to IAP.

### Deliverable B

`docs/ltms-derived-behavioral-specification.md`

The reconstructed behavioral specification, including contracts, invariants, state transitions, conformance examples, and explicitly labeled unresolved semantics.

### Deliverable C

`docs/ltms-adversarial-review.md`

A detailed code review containing reproducible correctness findings, evidence, severity, and remediation recommendations.

If the repository already has authoritative documents covering these areas, cross-reference them rather than unnecessarily duplicating content. Preserve the requested deliverables unless repository constraints make this impossible; document any deviation.

## 13. Required Structure of the Primary Reference Document

Use this outline, adapting it to verified repository contents:

1. Executive summary.
2. Project goals and non-goals.
3. Scope and terminology.
4. Historical and theoretical foundations.
5. Specification inventory.
6. Architecture and dependency layers.
7. Entity catalog.
8. Entity relationship diagrams.
9. State-transition models.
10. Subsystem specifications.
11. Algorithm-level analysis.
12. Engineering principles and invariants.
13. Capability matrix.
14. Public API and DSL contracts.
15. Test strategy and verified evidence.
16. Adversarial findings summary.
17. Known limitations.
18. Applicability to IAP.
19. Open questions and unresolved semantics.
20. Glossary.
21. References and source traceability.

Use tables where they improve comparison and traceability. Use diagrams where they clarify relationships or control flow. Avoid diagrams that merely decorate the document.

## 14. Source and Evidence Traceability

Maintain stable identifiers across the documents:

- `SPEC-LTMS-*` for specifications.
- `ENT-LTMS-*` for entities.
- `ALG-LTMS-*` for algorithms.
- `INV-LTMS-*` for invariants.
- `CAP-LTMS-*` for capabilities.
- `TEST-LTMS-*` for test evidence.
- `FIND-LTMS-*` for review findings.
- `IAP-LTMS-*` for potential IAP integration requirements.

Provide traceability tables connecting:

`Theory/specification → Entity → Algorithm → Implementation → Invariant → Test → Finding or limitation`

Every critical correctness claim should have supporting evidence.

Use repository-relative paths and line ranges. If line numbers may change, include the relevant symbol name.

For external sources, provide canonical URLs and precise references to the relevant sections or definitions.

Do not invent citations, source locations, tests, or algorithm guarantees.

## 15. Research Execution and Validation Protocol

Perform the work in the following sequence.

### Step 1: Inventory

Inspect the repository structure, documentation, source, tests, packaging, and CI.

### Step 2: Establish theoretical sources

Identify the applicable theory and authoritative references. Separate established theory from repository-specific decisions.

### Step 3: Build the specification and entity inventories

Establish the terminology, contracts, entities, relationships, and expected state transitions.

### Step 4: Trace algorithms through source code

Follow important operations from public APIs through their complete implementation paths.

### Step 5: Verify behavior

Inspect tests and run appropriate tests or small experiments if the environment permits. Record commands, versions, outcomes, and limitations.

### Step 6: Reconstruct behavioral contracts

Identify where theory, documentation, implementation, and tests agree or disagree.

### Step 7: Perform adversarial review

Challenge the correctness of the core invariants and the assumptions made by the implementation.

### Step 8: Assess IAP applicability

Map actual capabilities and limitations to concrete investigation use cases.

### Step 9: Write and cross-check the deliverables

Ensure all claims have appropriate evidence and the three documents use consistent terminology and identifiers.

If repository access, test execution, or external reference access is unavailable, continue with the evidence that can be obtained and clearly document the remaining limitations.

## 16. Quality Gates

The work is not complete merely because the Markdown files exist.

Verify that:

- Every major subsystem has been examined.
- Every central algorithm has an implementation-grounded explanation.
- Important entities and their relationships are documented.
- The distinction between formal specification and inferred behavior is preserved.
- Theoretical claims are supported by authoritative references.
- Engineering invariants are explicit and testable.
- The difference between soundness and completeness is clear.
- Documented limitations are distinguished from verified defects.
- Adversarial findings contain reproducible evidence where feasible.
- Test coverage claims reflect actual test assertions.
- Planned capabilities are not presented as implemented.
- Integration recommendations are based on IAP requirements rather than enthusiasm for the framework.
- All deliverables cross-reference one another consistently.

### Required final coverage matrix

Include a final matrix showing every major subsystem and whether it has been examined across:

- Documentation.
- Source implementation.
- Tests.
- Algorithm correctness.
- Invariants.
- Known limitations.
- IAP applicability.

Use `Complete`, `Partial`, `Not examined`, or `Not applicable`, with a short explanation for anything other than `Complete`.

## 17. Final Response

When finished, report:

1. The exact paths of the Markdown files created.
2. The repository commit or revision examined, if available.
3. The major concepts and capabilities documented.
4. The principal algorithms and invariants analyzed.
5. Any confirmed correctness defects.
6. The most important documented limitations.
7. The most significant discrepancies between theory, documentation, source, and tests.
8. The assessment of potential applicability to IAP.
9. The tests and experiments actually executed.
10. The areas not fully verified and why.

Do not claim completion if substantial source modules remain unexamined. Identify incomplete coverage explicitly.

## 18. Final Principle

The goal is to understand LTMS as a reasoning system with precise semantics, not merely as a collection of Python modules.

The resulting documents must enable another engineer to answer:

- What problem does each component solve?
- What is its theoretical basis?
- What exactly does it guarantee?
- Under what assumptions does that guarantee hold?
- What entities and invariants make the guarantee possible?
- How does the implementation realize the algorithm?
- How is correctness verified?
- Where can the implementation fail?
- Which limitations are fundamental to the reasoning model?
- Which capabilities could be reused safely in IAP?
- Which additional semantics and engineering controls would IAP need?

Favor technical precision, traceable evidence, reproducible examples, explicit uncertainty, and defensible engineering conclusions over breadth without depth.
