# Professor Project — Phase 1 Remediation
## Implementation Specification and Execution Instructions for Luna / OpenCode

**Assignment Type:** Architecture and Agent-Orchestration Remediation  
**Priority:** High  
**Role:** Principal Engineer / Agentic Systems Architect  
**Target Project:** Professor  
**Execution Mode:** Controlled, phased implementation  
**Primary Objective:** Bring the existing Professor project to a verified, operationally coherent MVP without unnecessary architectural expansion.

---

# 1. Mission

You are acting as the Principal Engineer responsible for remediating the Professor agentic orchestration project.

Professor is a session-first, multi-agent investigation and research platform implemented using OpenCode.

The existing project has a sound architectural foundation, but several implementation gaps prevent it from being considered operationally complete.

Your responsibility is to:

1. Inspect the entire existing project.
2. Independently verify the previously identified defects.
3. Correct incomplete and inconsistent agent definitions.
4. Complete all three workflow definitions.
5. Reconcile task and result contracts.
6. Establish reliable agent discovery and delegation.
7. Verify external research-tool availability.
8. Implement executable acceptance and smoke tests.
9. Perform an independent adversarial review.
10. Produce a final engineering report demonstrating what works, what does not, and what remains unverified.

This is an implementation assignment, not a request for another architecture proposal.

**You are authorized to modify the Professor project files within the scope defined in this specification.**

Do not modify unrelated application repositories, external systems, global OpenCode configuration, or user-level settings.

Do not introduce infrastructure that is inconsistent with the project's session-first architecture.

---

# 2. Architectural Context and Non-Negotiable Principles

## 2.1 Intended architecture

Professor is the primary orchestrator.

The intended responsibilities are:

| Agent | Responsibility |
|---|---|
| Professor | Intent interpretation, workflow selection, orchestration, synthesis, decision authority |
| Prompt-gen | Convert objectives into bounded, implementation-ready task specifications |
| Research Assistant | External technical research and source verification |
| Code Analyst | Repository inspection, code-path analysis, defect identification |
| Architecture Agent | Architecture decomposition, design alternatives, interfaces and technical decisions |
| Adversarial Reviewer | Independent challenge of findings, reasoning, assumptions and designs |
| Validation Agent | Independent acceptance and deliverable validation |

Professor owns the overall investigation.

Specialist agents must not independently declare the entire investigation successful.

## 2.2 Existing architecture must be preserved

The following principles are mandatory:

- Professor remains the only primary orchestrator.
- Specialists remain bounded subagents.
- `subagent_depth: 1` remains the default unless a documented compatibility requirement proves otherwise.
- Specialists are read-only by default.
- Professor owns final synthesis and adjudication.
- Adversarial review remains independent from validation.
- Research findings must be evidence-based.
- Repository findings must identify source locations.
- Unknowns must remain explicit.
- Retrieved content is untrusted input.
- User authorization is required for consequential actions.
- Investigation must not silently become implementation.
- Session-first orchestration remains the operating model.

## 2.3 Explicitly prohibited scope expansion

Do not introduce:

- An orchestration database.
- A task queue.
- A background scheduler.
- A persistent orchestration daemon.
- An external workflow engine.
- Recursive agent delegation.
- A new application backend.
- A new authentication system.
- Unnecessary abstraction frameworks.

Do not rewrite functioning agents merely to impose a different writing style.

Prefer targeted corrections over wholesale replacement.

---

# 3. Initial Repository Inspection

Before modifying anything, perform a complete inventory.

Inspect:

```text
professor/
├── AGENTS.md
├── README.md
├── opencode.json
├── .opencode/
│   ├── agents/
│   │   ├── professor.md
│   │   ├── prompt-gen.md
│   │   ├── research-assistant.md
│   │   ├── code-analyst.md
│   │   ├── architecture-agent.md
│   │   ├── adversarial-reviewer.md
│   │   └── validation-agent.md
│   ├── workflows/
│   │   ├── research.md
│   │   ├── research-and-design.md
│   │   └── code-review.md
│   ├── prompts/
│   │   ├── specification-analysis.md
│   │   ├── architecture-decision.md
│   │   ├── repository-review.md
│   │   └── research-task.md
│   └── schemas/
│       ├── task-package.schema.json
│       └── agent-result.schema.json
└── workspace/
    ├── research/
    ├── specifications/
    ├── evidence/
    ├── decisions/
    └── deliverables/
```

The inventory above is the expected structure, not an assumption that every file currently exists or is correctly implemented.

Verify the actual repository.

## 3.1 Establish the baseline

Record:

- Git revision.
- OpenCode version.
- Operating environment.
- Existing configuration.
- Agent discovery output.
- Available tools.
- Existing validation scripts and tests.
- Existing project documentation.
- Existing file contents and sizes.

Create a baseline report:

`workspace/deliverables/phase-1-baseline.md`

Classify each finding as:

- CONFIRMED_DEFECT
- POTENTIAL_DEFECT
- RUNTIME_UNVERIFIED
- ACCEPTABLE_DESIGN
- OUT_OF_SCOPE

Do not assume that every finding in the previous review is correct.

Independently verify it.

Do not modify the baseline after remediation to make it appear that the original state was correct.

---

# 4. Work Package 1 — Agent Configuration and Discovery

**Priority: P1**

Target:

```text
.opencode/agents/
opencode.json
AGENTS.md
```

## 4.1 Normalize agent frontmatter

Inspect every agent definition.

Verify:

- Frontmatter is valid.
- Opening YAML delimiter is at the beginning of the file.
- Agent names are consistent with filenames and delegation references.
- Modes are correct.
- Descriptions clearly establish delegation purpose.
- Model configuration does not depend on undocumented assumptions.
- Permissions match responsibilities.
- Tool permissions correspond to tools actually available.
- Specialist responsibilities do not overlap unnecessarily.

Normalize formatting consistently.

Do not assume whitespace is necessarily rejected by OpenCode. Verify using the actual installed version and its supported configuration format.

## 4.2 Verify agent modes

The intended configuration is:

| Agent | Expected mode |
|---|---|
| professor | primary |
| prompt-gen | subagent |
| research-assistant | subagent |
| code-analyst | subagent |
| architecture-agent | subagent |
| adversarial-reviewer | subagent |
| validation-agent | subagent |

Confirm that the configuration is supported by the installed OpenCode version.

Verify actual agent discovery using the CLI.

Use the supported commands for the installed version, including:

```bash
opencode --version
opencode agent list
```

Do not assume that the presence of a Markdown file proves agent registration.

## 4.3 Verify delegation

Inspect Professor's delegation instructions.

Confirm that Professor:

1. Selects an appropriate workflow.
2. Identifies required specialists.
3. Defines bounded assignments.
4. Supplies necessary context.
5. Specifies deliverables and acceptance criteria.
6. Tracks completion and partial results.
7. Handles failures and blocked agents.
8. Resolves contradictory findings.
9. Performs final synthesis.

Verify that specialist agents cannot create recursive delegation chains.

## 4.4 Permissions and safety

Review all agent permissions.

Use least privilege.

In particular:

- Research Assistant should have only the tools required for research.
- Code Analyst should be read-only during investigations.
- Architecture Agent should not modify production code.
- Adversarial Reviewer should challenge conclusions without changing the original deliverable.
- Validation Agent should inspect artifacts without silently correcting them.
- Prompt-gen should produce specifications without executing the tasks it describes.

Professor must ask before consequential actions.

Do not grant broad shell or write access simply because it is convenient.

### Work Package 1 exit criteria

- All expected agents have valid definitions.
- All agent modes are verified.
- Discovery output matches the expected configuration.
- Delegation references resolve correctly.
- Permission boundaries are documented.
- No unexplained recursive delegation exists.

---

# 5. Work Package 2 — Complete Architecture Agent

**Priority: P1**

Target:

`.opencode/agents/architecture-agent.md`

The existing file is empty.

Create a complete specialist definition.

## 5.1 Role

Architecture Agent is responsible for technical architecture analysis and design.

It is not another orchestrator.

It must provide Professor with evidence-backed architecture recommendations.

## 5.2 Responsibilities

The agent must be capable of:

1. Decomposing complex systems into components.
2. Identifying component boundaries and ownership.
3. Defining interfaces and contracts.
4. Analyzing data models and relationships.
5. Identifying state transitions.
6. Evaluating architecture alternatives.
7. Identifying architectural dependencies.
8. Assessing failure propagation.
9. Analyzing security and trust boundaries.
10. Evaluating scalability and performance.
11. Assessing operational complexity.
12. Identifying migration and compatibility concerns.
13. Producing implementation-ready architecture specifications.

## 5.3 Required engineering considerations

For relevant designs, address:

- Component responsibilities.
- API and interface contracts.
- Data ownership.
- Persistence requirements.
- Authentication and authorization.
- Trust boundaries.
- Error handling.
- Timeouts and retries.
- Idempotency.
- Concurrency.
- Consistency.
- Observability.
- Auditability.
- Configuration.
- Secrets management.
- Deployment.
- Recovery.
- Testing.
- Rollback.

Do not mechanically include irrelevant concerns.

Explain why material concerns are excluded.

## 5.4 Architecture alternatives

For consequential decisions:

- Identify at least two materially distinct approaches when feasible.
- Define decision criteria.
- Compare advantages and disadvantages.
- Explain implementation and operational costs.
- Identify failure modes.
- Recommend one approach.
- Document rejected alternatives.

Do not manufacture alternatives simply to satisfy a numeric requirement.

## 5.5 Output contract

Return:

1. Architecture summary.
2. Problem and scope.
3. Requirements.
4. Current-state findings.
5. Component decomposition.
6. Interfaces and contracts.
7. Data flows.
8. State transitions.
9. Security analysis.
10. Reliability and failure analysis.
11. Alternatives and trade-offs.
12. Recommendation.
13. Implementation considerations.
14. Open decisions.
15. Evidence and limitations.

All claims must be appropriately classified as observed, derived, proposed, or unknown.

---

# 6. Work Package 3 — Complete Specification Analysis Prompt

**Priority: P1**

Target:

`.opencode/prompts/specification-analysis.md`

The existing file is empty.

Create a reusable specification-analysis prompt.

Its purpose is to transform incomplete business or technical requirements into an explicit engineering specification.

## 6.1 Requirements extraction

Extract and classify:

- Functional requirements.
- Non-functional requirements.
- Business rules.
- Technical constraints.
- Security requirements.
- Data requirements.
- Integration requirements.
- Operational requirements.
- Performance expectations.
- Compatibility requirements.
- Acceptance criteria.

## 6.2 Requirement classification

Use these classifications:

| Classification | Meaning |
|---|---|
| USER_STATED | Explicitly stated by the user |
| OBSERVED | Established from inspected evidence |
| DERIVED | Necessary to satisfy another requirement |
| PROPOSED | Recommended but not approved |
| UNRESOLVED | Requires clarification or evidence |

Never silently convert a proposed requirement into an approved requirement.

## 6.3 Specification analysis

Identify:

- Ambiguous language.
- Contradictory requirements.
- Missing requirements.
- Unverifiable requirements.
- Hidden dependencies.
- Unspecified error behavior.
- Unspecified authorization behavior.
- Missing boundary conditions.
- Missing data lifecycle definitions.
- Missing state transitions.
- Incomplete acceptance criteria.

## 6.4 Traceability

Every derived requirement must reference its originating requirement.

Every proposed design decision must identify the requirements it addresses.

Every acceptance criterion must be objectively testable.

## 6.5 Output

Produce a Markdown specification with:

1. Executive summary.
2. Problem statement.
3. Scope and exclusions.
4. Functional requirements.
5. Non-functional requirements.
6. Business rules.
7. Data requirements.
8. Interface requirements.
9. Security requirements.
10. Operational requirements.
11. Assumptions.
12. Gaps and ambiguities.
13. Requirements traceability.
14. Acceptance criteria.
15. Open questions.

The prompt must explicitly instruct the receiving agent to distinguish missing information from reasonable assumptions.

---

# 7. Work Package 4 — Complete Code Review Workflow

**Priority: P1 — Critical workflow gap**

Target:

`.opencode/workflows/code-review.md`

The existing file is empty.

This is one of the most important deliverables.

The workflow must provide a rigorous, adversarial software engineering review.

It must not be a generic instruction to inspect code and list bugs.

## 7.1 Activation conditions

Professor selects Code Review when the user requests:

- Repository code review.
- Defect investigation.
- Security review.
- Reliability review.
- Architecture and implementation gap analysis.
- Technical debt assessment.
- Remediation planning.
- Independent assessment of a coding agent's changes.

For a proposed new system or subsystem, use Research and Design.

For a general technical question without a repository review, use Research.

## 7.2 Stage 1 — Establish review scope

Capture:

- Repository location.
- Repository revision.
- Branch.
- User objective.
- Review scope.
- Excluded directories or components.
- Relevant architecture documents.
- Existing requirements.
- Known defects.
- Expected deliverables.

Establish the exact revision being reviewed.

Never report findings against an unidentified source revision.

If the repository is dirty, identify relevant uncommitted changes.

Do not discard, overwrite, or modify user changes.

## 7.3 Stage 2 — Repository reconnaissance

Code Analyst must inspect:

- Project structure.
- Language and framework.
- Dependency configuration.
- Application entry points.
- Package boundaries.
- Service boundaries.
- API endpoints.
- Data models.
- Persistence.
- Configuration.
- Authentication.
- Authorization.
- Logging.
- Error handling.
- Background processing.
- External integrations.
- Existing tests.
- Deployment configuration.

Do not claim exhaustive review when only a subset was inspected.

Record excluded areas and their consequences.

## 7.4 Stage 3 — Understand expected behavior

Identify the expected behavior from:

1. User-provided requirements.
2. Approved specifications.
3. Architecture decisions.
4. Existing contracts.
5. Tests.
6. Documented invariants.
7. Observable implementation behavior.

Distinguish intended behavior from accidental behavior.

Do not assume existing implementation is correct merely because it is already implemented.

## 7.5 Stage 4 — Analyze execution paths

Trace material execution paths end to end.

For each important path, identify:

- Entry point.
- Input validation.
- Authentication and authorization.
- Business logic.
- Service interactions.
- Persistence operations.
- External calls.
- Error handling.
- Output.
- Side effects.

Identify control-flow gaps, incorrect assumptions, missing checks, and unreachable or incomplete behavior.

Inspect callers and callees where needed.

Avoid isolated line-level observations that lack execution context.

## 7.6 Stage 5 — Defect and risk analysis

Inspect applicable categories:

### Functional correctness
- Incorrect business logic.
- Missing requirements.
- Invalid state transitions.
- Incorrect defaults.
- Incomplete workflows.
- Boundary-condition failures.

### Reliability
- Unhandled exceptions.
- Incorrect retry behavior.
- Missing timeouts.
- Resource leaks.
- Race conditions.
- Partial-write problems.
- Failure propagation.
- Non-idempotent operations.

### Security
- Authentication gaps.
- Authorization bypass.
- Tenant isolation failures.
- Secret exposure.
- Injection risks.
- Unsafe deserialization.
- Untrusted input handling.
- Sensitive-data logging.
- Inadequate auditability.

### Data integrity
- Incorrect transactions.
- Lost updates.
- Inconsistent state.
- Duplicate processing.
- Incorrect uniqueness assumptions.
- Missing validation.
- Schema and contract drift.

### Architecture
- Responsibility leakage.
- Circular dependencies.
- Tight coupling.
- Incorrect abstraction boundaries.
- Inconsistent interfaces.
- Hidden dependencies.
- Scalability constraints.

### Testing
- Missing coverage.
- Weak assertions.
- Untested failure paths.
- Inadequate integration tests.
- Tests that do not establish claimed behavior.

### Operations
- Configuration errors.
- Insufficient observability.
- Inadequate diagnostics.
- Deployment risks.
- Recovery limitations.

These categories are investigation areas, not a requirement to invent a finding in every category.

## 7.7 Stage 6 — Finding contract

Every confirmed finding must include:

| Field | Requirement |
|---|---|
| Finding ID | Unique stable identifier |
| Title | Concise defect description |
| Severity | CRITICAL, HIGH, MEDIUM, LOW, INFORMATIONAL |
| Category | Functional, Security, Reliability, Data, Architecture, Testing, Operations |
| Package/module | Affected package |
| File | Exact source file |
| Lines | Exact line range where available |
| Revision | Reviewed source revision |
| Observed behavior | What the code actually does |
| Expected behavior | What should happen |
| Trigger conditions | Conditions under which the defect occurs |
| Failure mechanism | Why the defect occurs |
| Root cause | Underlying cause |
| Impact | Functional or system consequences |
| Evidence | Source locations, tests, or reproducible observations |
| Remediation | Concrete proposed correction |
| Regression test | Test required to prevent recurrence |
| Confidence | HIGH, MEDIUM, LOW |
| Limitations | Anything not verified |

Do not report a hypothesis as a confirmed defect.

If a finding cannot be reproduced or fully established, label it accordingly.

## 7.8 Severity definitions

Use consistent severity definitions:

- CRITICAL: Exploitable security issue, major data corruption, or systemic outage with severe consequences.
- HIGH: Significant functional failure, security weakness, or reliability issue affecting important system behavior.
- MEDIUM: Meaningful defect with limited scope or a viable workaround.
- LOW: Localized defect, maintainability concern, or limited-impact weakness.
- INFORMATIONAL: Observation that does not establish a defect.

Explain severity using actual impact, not subjective wording.

## 7.9 Stage 7 — Adversarial review

Invoke Adversarial Reviewer.

The reviewer must independently challenge:

- Whether each significant finding is real.
- Whether evidence supports the claim.
- Whether severity is justified.
- Whether the root cause is correct.
- Whether remediation addresses the actual cause.
- Whether the proposed fix creates regressions.
- Whether important defect classes were missed.
- Whether the review scope was sufficient.

The reviewer must also identify potential false positives.

Do not merely ask the reviewer to rewrite the findings.

## 7.10 Stage 8 — Finding adjudication

Professor owns final adjudication.

For every significant finding, record:

- ACCEPTED.
- REJECTED.
- NEEDS_MORE_EVIDENCE.
- RECLASSIFIED.
- DUPLICATE.

Preserve disagreements between Code Analyst and Adversarial Reviewer.

Do not silently suppress disputed findings.

## 7.11 Stage 9 — Validation

Validation Agent must independently check:

- Finding evidence.
- Source locations.
- Severity.
- Scope coverage.
- Requirement traceability.
- Remediation feasibility.
- Regression test recommendations.
- Report completeness.

Validation must operate on the actual deliverable.

## 7.12 Stage 10 — Deliverables

Produce:

`workspace/deliverables/code-review-report.md`

Required sections:

1. Executive Summary.
2. Repository and Revision.
3. Review Scope.
4. Review Methodology.
5. Architecture Overview.
6. Findings Summary.
7. Detailed Findings.
8. Security Assessment.
9. Reliability Assessment.
10. Testing Gaps.
11. False Positives and Disputed Findings.
12. Remediation Priorities.
13. Residual Risks.
14. Validation Results.
15. Limitations.

When the user requests an implementation prompt, also produce:

`workspace/deliverables/code-review-remediation-prompt.md`

The remediation prompt must contain actionable changes organized by priority and dependency.

It must identify affected packages, files, expected behavior, implementation guidance, and regression tests.

Do not implement fixes unless explicitly authorized.

## 7.13 Code Review acceptance criteria

- Review revision is identified.
- Scope and exclusions are explicit.
- Findings are source-grounded.
- Severity is justified.
- False positives are challenged.
- Significant findings receive adversarial review.
- Remediation is actionable.
- Regression tests are identified.
- Validation results are reported.
- No unverified claim is presented as fact.

---

# 8. Work Package 5 — Strengthen Research Workflow

**Priority: P2**

Target:

`.opencode/workflows/research.md`

The existing workflow is reasonably complete.

Do not rewrite it unnecessarily.

Review it for consistency with the following requirements.

## 8.1 Research execution

Professor must:

1. Define the research question.
2. Establish scope.
3. Identify subquestions.
4. Delegate bounded research tasks.
5. Gather authoritative evidence.
6. Reconcile contradictory sources.
7. Identify unsupported assumptions.
8. Synthesize findings.
9. Validate the final deliverable.

## 8.2 Source requirements

Prioritize:

1. Official specifications.
2. Official documentation.
3. Standards.
4. Original research.
5. Primary technical sources.
6. Authoritative secondary analysis.

Record:

- Source title.
- URL or repository locator.
- Publisher.
- Publication or revision date, when available.
- Retrieval date.
- Relevant evidence.
- Limitations.

Do not fabricate citations.

Do not treat model knowledge as a retrieved source.

## 8.3 Research completeness

The final output must distinguish:

- Verified facts.
- Supported conclusions.
- Inferences.
- Recommendations.
- Hypotheses.
- Unknowns.

The Research Assistant must not declare completion merely because it has gathered a certain number of sources.

Research completeness depends on answering the research questions with sufficient evidence.

## 8.4 Research acceptance criteria

- Research questions are answered.
- Material claims are supported.
- Sources are traceable.
- Conflicts are addressed.
- Uncertainties are disclosed.
- Final recommendations are distinguishable from established facts.
- Validation checks the actual deliverable.

---

# 9. Work Package 6 — Strengthen Research and Design Workflow

**Priority: P2**

Target:

`.opencode/workflows/research-and-design.md`

The existing workflow has a strong foundation.

Preserve its overall structure and improve requirements traceability.

## 9.1 Requirements-to-design traceability

Establish the following chain:

```text
User Objective
    |
    v
Requirements
    |
    v
Engineering Constraints
    |
    v
Architecture Decisions
    |
    v
Component Specifications
    |
    v
Implementation Tasks
    |
    v
Verification Criteria
```

Each implementation task must trace to:

- An explicit requirement.
- An approved design decision.
- A necessary engineering dependency.

Do not create implementation tasks with no documented justification.

## 9.2 Design requirements

Ensure the workflow explicitly covers:

- Functional requirements.
- Non-functional requirements.
- Component boundaries.
- Interface contracts.
- Data models.
- State transitions.
- Security boundaries.
- Failure behavior.
- Retry and idempotency requirements.
- Observability.
- Deployment considerations.
- Compatibility.
- Migration.
- Rollback.
- Testing.

## 9.3 Phase-based implementation planning

Every substantial implementation plan must include:

| Field | Description |
|---|---|
| Phase | Sequential implementation phase |
| Objective | What the phase accomplishes |
| Dependencies | Prerequisite work |
| Components | Affected components |
| Files | Expected files or packages |
| Contracts | Interfaces or schema changes |
| Implementation | Concrete engineering tasks |
| Tests | Required tests |
| Exit criteria | Conditions required to complete the phase |
| Risks | Remaining risks |

Do not mark a phase complete simply because implementation files were created.

## 9.4 Design acceptance criteria

- Requirements are traceable.
- Design decisions are explicit.
- Interfaces are sufficiently detailed.
- Alternatives and trade-offs are documented.
- Failure and security behavior are addressed.
- Implementation phases have exit criteria.
- Validation identifies unresolved design gaps.

---

# 10. Work Package 7 — Reconcile Task and Agent Result Schemas

**Priority: P1**

Targets:

```text
.opencode/schemas/task-package.schema.json
.opencode/schemas/agent-result.schema.json
```

The existing schemas provide a useful foundation.

The problem is that the result schema does not sufficiently represent specialist-specific outputs.

For example, a Code Analyst finding requires substantially more detail than a generic claim, confidence, rationale, and source identifier.

## 10.1 Preserve the common result envelope

Retain the existing common fields where practical:

- task_id
- agent
- status
- summary
- findings
- evidence
- limitations
- acceptance_criteria_check
- recommended_next_steps

Preserve the existing status semantics:

- COMPLETED
- PARTIAL
- BLOCKED
- FAILED

Do not casually rename existing fields.

## 10.2 Introduce typed specialist payloads

Design a schema that supports the following result categories:

- RESEARCH
- CODE_REVIEW
- ARCHITECTURE
- ADVERSARIAL_REVIEW
- VALIDATION

The implementation must retain a common result envelope while supporting specialized finding structures.

Recommended approach:

```text
AgentResult
├── Common Metadata
├── Summary
├── Findings
│   ├── Common Finding
│   ├── Research Finding
│   ├── Code Review Finding
│   ├── Architecture Finding
│   ├── Adversarial Finding
│   └── Validation Finding
├── Evidence
├── Limitations
├── Acceptance Criteria Check
└── Recommended Next Steps
```

This is a conceptual structure, not a requirement to implement these exact JSON property names.

## 10.3 Code review result requirements

Support:

- Finding ID.
- Severity.
- Category.
- Package/module.
- File.
- Line range.
- Reviewed revision.
- Observed behavior.
- Expected behavior.
- Trigger conditions.
- Failure mechanism.
- Root cause.
- Impact.
- Evidence references.
- Remediation.
- Regression test.
- Confidence.
- Limitations.

## 10.4 Research result requirements

Support:

- Research question.
- Claim.
- Source references.
- Source quality.
- Publication date.
- Retrieval date.
- Evidence classification.
- Contradictions.
- Confidence.
- Limitations.

## 10.5 Architecture result requirements

Support:

- Design decision.
- Requirement references.
- Component responsibilities.
- Interface definitions.
- Alternatives.
- Trade-offs.
- Failure behavior.
- Security considerations.
- Open decisions.

## 10.6 Validation result requirements

Support:

- Acceptance criterion.
- Validation method.
- Observed result.
- Status.
- Evidence.
- Defects identified.
- Remaining limitations.

## 10.7 Schema engineering requirements

Use JSON Schema Draft 2020-12 unless the existing OpenCode integration requires another documented format.

Requirements:

- Strict validation.
- Explicit enumerations.
- Appropriate required fields.
- No accidental additional properties.
- Correct conditional validation.
- Reusable definitions where useful.
- Valid JSON.
- Test fixtures for valid and invalid cases.

Do not make schemas so restrictive that legitimate partial or blocked results cannot be represented.

Do not silently discard specialist information during serialization.

## 10.8 Task package review

Review the existing task-package schema for completeness.

Ensure task specifications represent:

- Objective.
- Context.
- Scope.
- Exclusions.
- Questions.
- Execution steps.
- Evidence requirements.
- Deliverable contract.
- Acceptance criteria.
- Safety constraints.
- Assumptions.

Ensure NEEDS_CONTEXT tasks cannot proceed as READY tasks without satisfying the required context conditions.

## 10.9 Schema compatibility

Inspect all existing references to both schemas.

Update the corresponding:

- Agent definitions.
- Workflow definitions.
- Prompt templates.
- README.
- Validation scripts.
- Test fixtures.

Do not maintain multiple conflicting definitions of the same result contract.

---

# 11. Work Package 8 — External Research Tool Verification

**Priority: P1**

Target:

`.opencode/agents/research-assistant.md`

The current Research Assistant configuration allows:

```yaml
websearch: allow
webfetch: allow
```

Permission does not prove availability.

Verify that the tools exist and function with the actual OpenCode and GitHub Copilot configuration.

## 11.1 Capability verification

Determine:

- OpenCode version.
- Active provider.
- Selected model.
- Available built-in tools.
- Whether websearch is available.
- Whether webfetch is available.
- Whether the provider/model combination supports the required interaction.
- Whether additional environment configuration is required.

Use current official OpenCode documentation for version-sensitive configuration.

Do not assume that documentation for another OpenCode version applies.

## 11.2 Fallback behavior

If websearch is unavailable:

- Identify the supported alternative.
- Document required configuration.
- Do not silently pretend research has been performed.
- Allow the Research Assistant to return BLOCKED or PARTIAL.
- Explain the missing capability to Professor.

Do not expose API keys or credentials in project files.

Do not add an external search service without documenting why the existing supported mechanism is insufficient.

## 11.3 Research smoke test

Perform a small research task using an authoritative technical subject.

Verify:

1. Search actually executes.
2. Results are retrieved.
3. A source can be opened.
4. The relevant content is examined.
5. The Research Assistant identifies its source.
6. The result is returned in the expected contract.
7. Unsupported claims are not presented as retrieved facts.

Record the actual tool and provider behavior.

---

# 12. Work Package 9 — Documentation Synchronization

**Priority: P2**

Target:

`README.md`

The README must accurately describe the implemented project.

Update:

- Project mission.
- Architecture.
- Agent inventory.
- Workflow inventory.
- Prompt inventory.
- Schema inventory.
- Workspace structure.
- Agent discovery instructions.
- Configuration requirements.
- Tool requirements.
- Permission model.
- Delegation model.
- Validation procedure.
- Known limitations.

Include `code-analyst.md`, `architecture-agent.md`, `specification-analysis.md`, and all three workflow files in the correct locations.

Document which capabilities have been:

- Implemented.
- Structurally validated.
- Runtime verified.
- Not yet verified.

Do not claim runtime capabilities based solely on Markdown configuration.

Update AGENTS.md only where needed to align it with the completed implementation.

Do not create conflicting instructions between README, AGENTS.md, Professor, and the workflows.

---

# 13. Work Package 10 — Automated Validation and Acceptance Tests

**Priority: P1**

The project needs repeatable validation.

Create a lightweight validation suite using Python's standard library wherever practical.

Suggested location:

```text
scripts/
├── validate_project.py
├── validate_schemas.py
└── smoke_test.sh
```

Adapt the structure to the repository's existing conventions if appropriate.

Do not introduce a large test framework for this small configuration project.

## 13.1 Structural validation

Validate:

- Expected files exist.
- Required files are non-empty.
- JSON configuration parses.
- JSON schemas parse.
- YAML frontmatter is valid.
- Agent names and modes are consistent.
- Workflow references resolve.
- Prompt references resolve.
- README paths exist.
- Workspace directories exist.
- No duplicate or conflicting agent definitions exist.

Do not merely check that a Markdown file contains text.

Check the required sections and relevant structural contracts.

## 13.2 Schema validation

Validate fixtures against both schemas.

Cover:

- Valid task package.
- Invalid task package.
- NEEDS_CONTEXT without questions.
- Valid generic agent result.
- Valid code-review result.
- Valid research result.
- Valid architecture result.
- Valid adversarial result.
- Valid validation result.
- Invalid severity.
- Missing mandatory fields.
- Unexpected properties.
- Invalid acceptance status.

If an external schema-validation dependency is required, document it and provide a reproducible setup.

## 13.3 Agent discovery test

Execute the supported OpenCode agent-discovery command.

Check:

- All seven expected agents are discovered.
- Exactly one primary orchestrator is configured.
- Specialist modes are correct.
- The default agent is Professor.
- Delegation depth is correct.

If OpenCode is unavailable, mark this test NOT_VERIFIED.

Do not report PASS.

## 13.4 Workflow acceptance tests

Test each workflow independently.

### Test A — Research

Example:

"Research the current official OpenCode agent configuration format and explain how specialist delegation works."

Expected:

- Professor selects research workflow.
- Research Assistant is invoked.
- Authoritative sources are retrieved.
- Claims are evidence-backed.
- Final response includes limitations.

### Test B — Research and Design

Example:

"Design a reliable evidence-management subsystem for an AI investigation platform. Produce component boundaries, interfaces, data models, failure behavior, and a phased implementation plan."

Expected:

- Professor selects research-and-design.
- Requirements are classified.
- Architecture Agent is invoked.
- Alternatives are evaluated.
- Adversarial review is performed.
- Validation checks the actual design.
- Final output is implementation-ready.

### Test C — Code Review

Example:

"Review a small repository containing an intentionally introduced authorization defect. Identify the issue, its trigger conditions, severity, remediation, and regression tests."

Expected:

- Professor selects code-review.
- Code Analyst inspects source.
- Findings identify exact locations.
- Adversarial Reviewer challenges findings.
- Validation Agent checks the report.
- Professor produces the final adjudicated result.

Use a temporary test fixture or an existing safe sample repository.

Do not introduce intentional defects into production or user repositories.

## 13.5 Fault-injection tests

Test orchestration failure behavior.

At minimum:

1. Research Assistant returns no evidence.
2. Code Analyst returns incomplete findings.
3. Two specialists disagree.
4. Adversarial Reviewer rejects a significant finding.
5. Validation Agent identifies a missing acceptance criterion.
6. Required external search tooling is unavailable.
7. A specialist returns BLOCKED.
8. A specialist returns malformed output.

Professor must handle each condition explicitly.

It must not silently convert failure into success.

---

# 14. Work Package 11 — Independent Adversarial Review

**Priority: P1**

After implementation, invoke Adversarial Reviewer with an independent assignment.

The reviewer must not simply inspect whether the implementation matches the authoring instructions.

It must evaluate whether the resulting system is actually coherent and usable.

## 14.1 Review areas

Challenge:

- Agent responsibility boundaries.
- Agent discovery.
- Delegation contracts.
- Workflow selection.
- Task/result schema completeness.
- Evidence integrity.
- Security boundaries.
- Tool availability assumptions.
- Runtime behavior.
- Failure handling.
- Documentation accuracy.
- Test coverage.
- Operational limitations.

## 14.2 Mandatory challenge questions

1. Can Professor actually discover and invoke every specialist?
2. Can Professor distinguish an incomplete result from a completed result?
3. Can a specialist return all information required by its workflow?
4. Can code-review findings be represented without information loss?
5. Can Research Assistant perform real source retrieval?
6. Can Professor handle contradictory specialist conclusions?
7. Can validation identify a defective deliverable?
8. Can an unavailable tool be detected?
9. Can the workflow recover from a blocked specialist?
10. Is any agent accidentally granted excessive authority?
11. Are there duplicated or contradictory instructions?
12. Are acceptance criteria objectively verifiable?

## 14.3 Findings

Every adversarial finding must include:

- Finding ID.
- Severity.
- Affected file or component.
- Evidence.
- Impact.
- Remediation.
- Verification criteria.

Professor must adjudicate every significant finding.

Fix material defects before declaring Phase 1 complete.

---

# 15. Execution Governance

This is a controlled implementation.

Do not execute all work packages as an unreviewed batch.

Use the following phases.

## Phase A — Baseline and contracts

Includes:

- Repository inventory.
- Baseline report.
- Agent configuration inspection.
- Task/result contract design.
- Dependency identification.

**Exit gate:** Baseline is complete and proposed contract changes are understood.

## Phase B — Core agent completion

Includes:

- Frontmatter normalization.
- Agent mode verification.
- Architecture Agent implementation.
- Specification Analysis prompt.
- Permission corrections.

**Exit gate:** Agent definitions are structurally valid and discovery is verified where tooling is available.

## Phase C — Workflow completion

Includes:

- Code Review workflow.
- Research workflow refinement.
- Research and Design refinement.
- Consistent delegation and result requirements.

**Exit gate:** All three workflows have explicit procedures, deliverables, and acceptance criteria.

## Phase D — Schema and documentation

Includes:

- Task/result schema changes.
- Test fixtures.
- README.
- AGENTS.md synchronization.
- Prompt and workflow references.

**Exit gate:** No known contract or documentation inconsistencies remain.

## Phase E — Automated validation

Includes:

- Structural tests.
- Schema tests.
- Agent discovery tests.
- Smoke tests.
- Fault-injection tests.

**Exit gate:** All executable tests pass. Unavailable runtime tests are explicitly marked NOT_VERIFIED.

## Phase F — Adversarial review

Includes:

- Independent review.
- Finding adjudication.
- Remediation.
- Regression validation.

**Exit gate:** No unresolved critical or high-severity defect remains without explicit documented acceptance.

---

# 16. Change Management Rules

1. Preserve existing functioning behavior unless a specific defect requires change.
2. Do not replace existing prompts without first understanding their contents.
3. Do not delete existing requirements.
4. Do not invent user-approved requirements.
5. Do not add unnecessary dependencies.
6. Do not alter user-level OpenCode configuration.
7. Do not modify unrelated projects.
8. Do not commit, push, deploy, or publish without explicit authorization.
9. Preserve unrelated working-tree changes.
10. Keep changes focused and reviewable.
11. Validate every changed JSON and Markdown configuration artifact.
12. Record any deviations from this specification.
13. Never claim that a test passed unless it actually executed and passed.

If a requirement conflicts with the installed OpenCode version, identify the conflict, consult the relevant official documentation, and propose the smallest supported correction.

Do not silently implement an unsupported configuration.

---

# 17. Final Deliverables

Produce the following artifacts.

| Deliverable | Destination |
|---|---|
| Baseline assessment | `workspace/deliverables/phase-1-baseline.md` |
| Completed Code Review workflow | `.opencode/workflows/code-review.md` |
| Architecture Agent | `.opencode/agents/architecture-agent.md` |
| Specification Analysis prompt | `.opencode/prompts/specification-analysis.md` |
| Revised task schema | `.opencode/schemas/task-package.schema.json` |
| Revised result schema | `.opencode/schemas/agent-result.schema.json` |
| Revised Research workflow | `.opencode/workflows/research.md` |
| Revised Research and Design workflow | `.opencode/workflows/research-and-design.md` |
| Updated documentation | `README.md`, `AGENTS.md` where needed |
| Validation suite | `scripts/` |
| Validation results | `workspace/deliverables/phase-1-validation-report.md` |
| Adversarial review | `workspace/deliverables/phase-1-adversarial-review.md` |
| Final implementation report | `workspace/deliverables/phase-1-completion-report.md` |

Additional supporting files may be created when necessary, but must be justified.

---

# 18. Final Completion Report Requirements

The final report must include:

## 18.1 Executive summary

- What was implemented.
- What was corrected.
- Whether the project is operationally ready.
- Remaining blockers.

## 18.2 Change inventory

For every modified or created file:

- File path.
- Reason for change.
- Summary of implementation.
- Relevant tests.

## 18.3 Architecture status

Report the final responsibility of every agent.

## 18.4 Workflow status

Report the implementation and verification status of:

- Research.
- Research and Design.
- Code Review.

## 18.5 Contract status

Explain the final task and result schemas and how specialist-specific results are represented.

## 18.6 Runtime status

Report actual results for:

- OpenCode version.
- Agent discovery.
- Provider/model configuration.
- Research tooling.
- Delegation.
- Workflow smoke tests.
- Fault-injection tests.

Distinguish:

- PASS.
- FAIL.
- PARTIAL.
- BLOCKED.
- NOT_VERIFIED.

## 18.7 Adversarial review

Include all significant findings, their adjudication, and their remediation status.

## 18.8 Residual risks

Identify anything that remains incomplete, unverified, or dependent on external configuration.

## 18.9 Readiness decision

Select exactly one:

- READY_FOR_MVP_USE
- READY_WITH_DOCUMENTED_LIMITATIONS
- NOT_READY

Provide evidence supporting the decision.

Do not select READY merely because the implementation tasks were completed.

---

# 19. Definition of Done

Phase 1 is complete only when:

- All seven agents have valid definitions.
- Agent discovery has been verified or explicitly marked NOT_VERIFIED.
- Professor remains the sole primary orchestrator.
- All three workflows are complete.
- Architecture Agent is implemented.
- Specification Analysis prompt is implemented.
- Task and result contracts are consistent.
- Specialist outputs are representable without material information loss.
- Research tooling is verified or its limitation is explicitly handled.
- Documentation reflects the actual implementation.
- Structural validation passes.
- Schema tests pass.
- Available runtime tests pass.
- Adversarial review is complete.
- Material findings are resolved or explicitly accepted.
- Final completion report is produced.

**A complete set of Markdown files is not proof of an operational agentic system.**

The final readiness decision must be based on demonstrated behavior.

---

# 20. Instructions to Luna

Operate as a principal engineer, not as a passive code-generation assistant.

Be skeptical of existing implementation, including the previous review findings and this specification itself.

If you discover a better way to achieve an objective without violating the architectural constraints, explain the improvement and implement it when it is a clear, bounded correction.

Do not introduce unnecessary architectural complexity.

Do not skip difficult verification because configuration files appear correct.

Do not report success based on intention.

Maintain a clear distinction between:

- Implemented.
- Structurally valid.
- Tested.
- Runtime verified.
- Not verified.

Use Professor's existing orchestration principles throughout this assignment.

At the end, provide the final completion report and a concise summary of any remaining work.

**The objective is not to make Professor look complete. The objective is to make Professor demonstrably reliable as an agentic research, investigation, architecture, and code-review orchestrator.**
