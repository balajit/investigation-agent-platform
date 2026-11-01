
# Workflow: Research and Design

## Requirements-to-Design Traceability

Maintain an explicit chain from user objective to requirements, engineering constraints, architecture decisions, component specifications, implementation tasks, and verification criteria. Assign stable IDs to requirements and decisions. Every implementation task must cite an explicit requirement, an approved design decision, and any necessary engineering dependency; do not generate unjustified implementation tasks. Distinguish approved requirements from derived and proposed items.

## 1. Purpose

Investigate a problem and produce an evidence-backed technical design.

This workflow combines discovery, requirements analysis, technical research, candidate evaluation, architecture design, adversarial review, and validation.

The goal is not merely to describe a possible solution. It is to produce a design with explicit requirements, assumptions, trade-offs, interfaces, failure behavior, and verification criteria.

## 2. Activation Conditions

Professor selects this workflow when the user requests:
- Architecture or system design.
- Technical solution evaluation.
- Design of a new component, service, API, integration, or subsystem.
- A migration, modernization, or redesign plan.
- A technical specification intended for implementation.
- Investigation followed by a concrete design recommendation.

For research without a design deliverable, use `research.md`.

For an existing implementation's defects, use `code-review.md` as the primary workflow and this workflow for redesign when necessary.

## 3. Inputs

Capture:

- Problem statement and desired outcome.
- Current system or repository context.
- Functional requirements.
- Non-functional requirements.
- Known constraints and exclusions.
- Relevant technology and platform requirements.
- Security, privacy, reliability, and operational expectations.
- Compatibility and migration constraints.
- Required design artifacts and their destination.
- Decision criteria and acceptance criteria.

Identify missing critical inputs. State reasonable assumptions for noncritical gaps.

Do not invent requirements and present them as user-approved facts.

## 4. Roles

### Professor
Owns scope, requirements adjudication, delegation, design selection, decision records, and final synthesis.

### Prompt-gen
Structures complex assignments and defines explicit deliverable and acceptance contracts.

### Research Assistant
Gathers authoritative technical evidence and evaluates external platform, framework, protocol, and dependency behavior.

### Code Analyst
Examines existing implementation and repository constraints when the design depends on current code or established behavior.

### Adversarial Reviewer
Challenges the proposed design, alternatives, assumptions, failure modes, and operational consequences.

### Validation Agent
Checks that the design answers the original requirements and meets the deliverable contract.

## 5. Execution Procedure

### Stage 1 — Frame the Problem

Professor must establish:

1. The problem to solve.
2. The intended outcome.
3. The stakeholders or system boundaries relevant to the request.
4. Functional requirements.
5. Non-functional requirements.
6. Explicit constraints and exclusions.
7. Acceptance criteria.
8. Known facts, assumptions, and unresolved questions.

Classify requirements as:
- USER-STATED — explicitly provided by the user.
- OBSERVED — established from inspected artifacts.
- DERIVED — logically necessary to satisfy stated requirements.
- PROPOSED — recommended but not yet approved.
- UNRESOLVED — requires further evidence or a decision.

### Stage 2 — Establish the Current State

When a repository or existing system is available, inspect relevant code, configuration, documentation, tests, and interfaces.

Document:
- Current architecture and execution paths.
- Existing components and responsibilities.
- Integration boundaries.
- Data ownership and lifecycle.
- Configuration and deployment assumptions.
- Existing controls and constraints.
- Known limitations and technical debt.

When no implementation evidence is available, label the current-state description as unknown rather than fabricating an architecture.

### Stage 3 — Research Technical Constraints

Use Research Assistant to investigate material external questions.

Prioritize:
1. Official specifications and standards.
2. Official framework and platform documentation.
3. Official security advisories and release notes.
4. Reproducible examples and authoritative technical references.
5. Secondary sources for additional context.

Verify version-sensitive claims against the relevant version where possible.

Record source provenance, relevant dates, compatibility requirements, and limitations.

Do not treat a recommendation from a vendor or community source as proof that a design satisfies the user's requirements.

### Stage 4 — Derive Design Requirements

Translate the problem into explicit engineering requirements.

Address relevant concerns such as:
- Component responsibilities and ownership.
- Interfaces and contracts.
- Data models and state transitions.
- Authentication and authorization.
- Trust boundaries and untrusted input.
- Error handling and partial failures.
- Timeouts, retries, idempotency, and cancellation.
- Concurrency and consistency.
- Observability and auditability.
- Performance and scaling.
- Configuration and secrets management.
- Compatibility and versioning.
- Testing, rollout, and rollback.
- Operational support and recovery.

Only include concerns relevant to the system, but explicitly record material exclusions.

For each derived requirement, cite its originating requirement. For each proposed design decision, list the requirements it addresses and mark approval status. Do not convert a proposal into an approved decision.

### Stage 5 — Develop Candidate Designs

Develop at least two materially distinct candidates for consequential architectural decisions when feasible.

For each candidate, assess:
- Requirement coverage.
- Architectural complexity.
- Implementation effort.
- Reliability and failure behavior.
- Security and privacy.
- Operational burden.
- Compatibility and migration.
- Performance and scalability.
- Testability.
- Reversibility and future change cost.

If only one feasible design exists, explain why alternatives were excluded.

Use decision criteria derived from the requirements rather than selecting an approach solely because it is familiar.

### Stage 6 — Recommend the Design

Professor synthesizes the research and selects or recommends a design.

Document:
- Recommended architecture.
- Component responsibilities.
- Interfaces and contracts.
- Key data flows.
- Important state transitions.
- Error and recovery behavior.
- Security controls.
- Operational and observability model.
- Deployment and configuration assumptions.
- Trade-offs and rejected alternatives.
- Unresolved decisions.

Distinguish mandatory design requirements from implementation suggestions.

Do not claim a design has been implemented or tested when only a specification has been produced.

### Stage 7 — Adversarial Design Review

Require Adversarial Reviewer for material architecture decisions, security-sensitive designs, migrations, and high-impact recommendations.

The reviewer must challenge:
- Missing or contradictory requirements.
- Unsupported assumptions.
- Failure modes and degraded operation.
- Trust boundaries and authorization.
- Concurrency and consistency.
- Data loss or corruption risks.
- Operational complexity.
- Hidden dependencies and vendor lock-in.
- Migration and rollback feasibility.
- Testability and observability.
- Simpler or more reversible alternatives.

Professor must address material findings or record them as residual risks.

### Stage 8 — Produce an Implementation Plan

When implementation planning is requested, include:
- Phases and dependency ordering.
- Package and file layout where applicable.
- Interface and schema definitions.
- Required configuration changes.
- Security and compatibility requirements.
- Unit, integration, and system tests.
- Migration and rollout steps.
- Rollback strategy.
- Acceptance criteria for each phase.

Use a phase table with these fields: Phase, Objective, Dependencies, Components, Files, Contracts, Implementation, Tests, Exit Criteria, and Risks. Every task must trace to requirements, approved decisions, and dependencies. A phase is not complete merely because files were created.

Do not modify code unless implementation has been explicitly authorized.

### Stage 9 — Validate the Design

Invoke Validation Agent to check the actual design against the original requirements.

Verify:
- Requirement-to-design traceability.
- Internal consistency.
- Evidence for important external technical claims.
- Coverage of failure and security scenarios.
- Treatment of alternatives and trade-offs.
- Explicit handling of unresolved decisions.
- Completeness of implementation and testing guidance, when requested.

## 6. Required Deliverable

Produce a Markdown design document containing:

1. Executive Summary and Recommendation.
2. Problem Statement and Scope.
3. Requirements and Constraints.
4. Current-State Assessment.
5. Assumptions and Derived Requirements.
6. Research Evidence and Technical Constraints.
7. Candidate Designs and Trade-Offs.
8. Recommended Architecture.
9. Component Responsibilities and Interfaces.
10. Data Flows and State Transitions.
11. Security, Reliability, and Failure Analysis.
12. Operational and Observability Considerations.
13. Migration, Rollout, and Rollback Plan, when applicable.
14. Testing and Acceptance Criteria.
15. Adversarial Review Findings.
16. Open Decisions and Residual Risks.
17. Validation Results.

For substantial decisions, create an architecture decision record under `workspace/decisions/` when requested or consistent with the project's artifact conventions.

## 7. Acceptance Criteria

- The recommendation addresses the user's stated objective.
- Requirements are traceable to the design.
- Facts, assumptions, derived requirements, and proposals are distinguished.
- Material external claims have appropriate evidence.
- Alternatives and trade-offs are evaluated.
- Interfaces, component boundaries, and failure behavior are sufficiently concrete.
- Security and operational concerns are addressed where relevant.
- Unresolved decisions and residual risks are explicit.
- Implementation guidance is actionable when requested.
- Adversarial review and validation are completed when required, or their absence is disclosed.

## 8. Stop Conditions

Stop when the design meets the acceptance criteria and remaining uncertainties are documented.

Do not continue researching low-impact questions unlikely to change the recommendation.

Ask the user when a material decision depends on an unstated preference, constraint, or authorization that cannot reasonably be assumed.

## 9. Final Response

Professor provides:
- The recommended design and rationale.
- The most important trade-offs.
- Key implementation and operational risks.
- Unresolved decisions.
- The deliverable location.
- Review and validation outcomes.
