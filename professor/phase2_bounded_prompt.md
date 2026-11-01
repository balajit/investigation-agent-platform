# Professor — Phase 2 Bounded Remediation and Requalification
## Implementation Authorization and Execution Contract for Luna / OpenCode

**Mission:** Remediate the accepted Phase 2 defects, complete the unfinished Phase 2 qualification work, and independently determine whether Professor meets the Phase 2 operational-readiness criteria.

**Execution mode:** Bounded remediation, evidence-driven requalification.

**Starting decision:** `NOT_READY`

**Authorization:** Authorized to inspect, modify, test, and validate the existing Professor project strictly within the scope defined below. This authorization does not extend to Phase 3, new platform capabilities, architectural expansion, or unrelated refactoring.

---

# 1. Role and operating principles

Act as a principal engineer responsible for the technical remediation and qualification of Professor.

You are not being asked to make the project appear complete. You are being asked to establish whether its actual behavior is reliable, reproducible, and supported by verifiable evidence.

Apply the following principles:

1. Evidence over assertion.
2. Runtime behavior over documentation claims.
3. Independent validation over self-certification.
4. Explicit failure over silent degradation.
5. Bounded changes over architectural expansion.
6. Reproducibility over one-time successful execution.
7. Preserve failed attempts and negative evidence.
8. Never convert an unverified result into a PASS.
9. Do not weaken acceptance criteria to achieve a passing result.
10. Do not modify the qualification criteria after observing test outcomes without explicit authorization.

Every conclusion must distinguish:

- `OBSERVED`: directly observed during execution.
- `SPECIALIST_REPORTED`: claimed by a delegated specialist but not independently verified.
- `LOCAL_TEST_OBSERVED`: demonstrated by a local test or script.
- `INDEPENDENTLY_VERIFIED`: reproduced or checked by a separate validation activity.
- `INFERRED`: derived from available evidence.
- `NOT_VERIFIED`: not demonstrated.
- `NOT_APPLICABLE`: explicitly justified as outside the test's scope.

Do not present specialist-reported results as independently verified.

---

# 2. Fixed scope and boundaries

## 2.1 Authorized work packages

The remediation effort is limited to these work packages:

| Work package | Scope |
|---|---|
| WP2-R1 | Repair Phase 2 local qualification harness defects |
| WP2-R2 | Establish reproducible per-session evidence manifests |
| WP2-R3 | Complete Research workflow qualification |
| WP2-R4 | Complete Research & Design workflow qualification |
| WP2-R5 | Complete Code Review workflow qualification |
| WP2-R6 | Demonstrate live failure handling and recovery |
| WP2-R7 | Demonstrate actual task/result contract enforcement |
| WP2-R8 | Independent adversarial review and final qualification |

Do not add work packages unless a concrete blocking dependency is discovered. Document the dependency and request authorization before expanding scope.

## 2.2 Prohibited work

Do not:

- Add new agents.
- Introduce a database, queue, scheduler, daemon, or external orchestrator.
- Change Professor's primary-orchestrator role.
- Increase or redesign `subagent_depth`.
- Introduce recursive delegation.
- Redesign the overall agent architecture.
- Add unrelated product capabilities.
- Perform broad refactoring.
- Change the workflow acceptance criteria merely to obtain PASS.
- Remove or rewrite failed evidence.
- Delete historical session records.
- claim live qualification based on static inspection or procedural fixtures.
- execute production application changes or external side effects.
- expose credentials, tokens, private URLs containing secrets, or other sensitive data in reports.

Existing architecture and security boundaries remain authoritative.

If a required capability cannot be demonstrated with the existing architecture, document the limitation rather than silently introducing a new architecture.

---

# 3. Phase 0 — Establish the authoritative baseline

Before modifying anything, inspect the current repository and establish the actual state.

## 3.1 Repository inspection

Record:

- Git revision and branch.
- Working-tree status, including staged and unstaged changes.
- Untracked files.
- OpenCode version.
- Provider and model.
- Agent configuration and discovery results.
- Workflow and prompt files.
- Task/result schemas.
- Qualification and validation scripts.
- Existing tests.
- Existing Phase 2 reports and evidence.
- Any relevant Phase 1 deliverables.

Do not assume that the repository still matches a previously reported revision.

Do not discard or overwrite user changes.

If the working tree is dirty, distinguish pre-existing changes from your own changes. Do not reset, clean, or stash changes without explicit authorization.

## 3.2 Baseline evidence

Create:

`workspace/deliverables/phase-2-remediation-baseline.md`

Include:

1. Repository identity and revision.
2. Dirty-state assessment.
3. Runtime and provider information.
4. Relevant project files.
5. Existing qualification status.
6. Previously accepted findings.
7. Previously unresolved findings.
8. Security concerns.
9. Known environmental limitations.
10. Exact files proposed for modification.

Create a SHA-256 manifest for relevant source, configuration, schema, script, and workflow files.

The manifest must make it possible to determine which exact source revision was tested.

## 3.3 Previously established starting findings

Treat the following as accepted findings requiring remediation or explicit disposition.

### P2-001 — MEDIUM: Misleading acceptance harness results

Known location:

`scripts/acceptance_tests.py`

The previously reviewed `result_contract_checks()` implementation returned four PASS results unconditionally. It did not load the actual schemas or validate result fixtures.

Required remediation:

- Separate policy-presence checks from actual schema validation.
- Do not report a schema-validation PASS unless the actual validator was executed.
- Validate representative valid and invalid task/result fixtures.
- Ensure failures propagate to the process exit code.
- Report the exact checks executed and their results.
- Prevent the output from implying live agent behavior was tested when it was not.

### P2-002 — MEDIUM: Validation command has a report-writing side effect

Known location:

`scripts/acceptance_tests.py`

The previously reviewed implementation unconditionally wrote:

`workspace/deliverables/phase-1-validation-report.md`

Required remediation:

- Separate read-only validation from report generation.
- A validation or smoke-test command must not silently overwrite an existing report.
- Report generation must be an explicit operation.
- If the destination already exists, require an explicit overwrite option or use a new versioned output path.
- Add tests proving that read-only validation does not modify durable deliverables.

### P2-003 — LOW: Failure-policy checks are not independent

Known location:

`scripts/acceptance_tests.py`

The eight failure-policy checks previously shared one `policy_present` predicate. The checks could pass or fail together even when individual failure-handling requirements were missing.

Required remediation:

- Define the specific expected behavior for each failure scenario.
- Make each static policy check independently meaningful.
- Where behavior cannot be tested statically, label the check as a static policy check rather than a behavioral qualification.
- Do not claim eight independently tested failure behaviors if only one shared predicate was evaluated.

### Previously accepted Phase 2 qualification gaps

Also address:

- Research & Design workflow did not complete its end-to-end execution.
- Code Review had a specialist chain but was not operationally qualified.
- Eight failure checks were procedural fixtures, not live failure injections.
- Live delegation-boundary enforcement was not demonstrated.
- Actual task/result contract enforcement at the orchestration boundary was not demonstrated.
- Per-session evidence manifests were incomplete.
- Independent operational qualification was not achieved.

### Security finding

Three credential-shaped remote URLs were previously identified and sanitized from:

`workspace/evidence/phase2-code-review-session.json`

The file was reported to parse correctly after sanitization.

Required handling:

- Do not restore or reproduce the sensitive values.
- Re-scan current evidence and deliverables for credential-shaped URLs and other secret patterns.
- Preserve the sanitized evidence.
- Do not claim the underlying credential has been revoked or rotated.
- Record owner-side revocation/rotation as an external dependency until independently confirmed by the credential owner.
- Do not attempt credential use or external account access.

---

# 4. WP2-R1 — Repair the local qualification harness

Repair the harness before attempting operational requalification.

## 4.1 Required design

Clearly separate these categories:

1. Static project/policy checks.
2. JSON/schema validation.
3. Local workflow logic tests.
4. Live orchestration tests.
5. Independent operational qualification.

Each category must have a distinct result classification.

A local fixture must never be labeled as a live orchestration test.

## 4.2 Failure-policy checks

Define independent checks for at least the following conditions:

| Scenario | Required behavior to verify |
|---|---|
| No supporting evidence | Professor must preserve the evidence limitation and not fabricate a conclusion |
| Incomplete specialist findings | Professor must not treat incomplete findings as complete |
| Conflicting specialist conclusions | Professor must identify and resolve or explicitly retain the conflict |
| Adversarial reviewer rejection | Rejected work must not be presented as accepted |
| Validation failure | Failed validation must prevent an accepted completion status |
| Research capability unavailable | Professor must disclose the unavailable capability and limit the result |
| Specialist blocked | Professor must preserve the blocked disposition and explain the dependency |
| Malformed specialist response | Professor must reject or quarantine the invalid result and not treat it as completed |

For every case, define:

- Input condition.
- Expected behavior.
- Observable evidence.
- Failure condition.
- Test method.
- Whether the test is static, local, or live.

A shared boolean predicate is not sufficient.

## 4.3 Schema validation

Use the actual task/result schema files.

Test:

- Valid task package.
- Invalid task package.
- Missing required fields.
- Incorrect field types.
- Invalid status values.
- Invalid evidence classification.
- Missing acceptance criteria.
- Malformed specialist result.
- Unexpected or unsupported fields where the schema prohibits them.
- Valid partial and blocked results.
- Valid completed result with evidence references.

Verify that:

- The validator actually loads the intended schema.
- Invalid inputs fail.
- Valid inputs pass.
- Failures produce non-zero process exit status.
- Validation output identifies the failed fixture and reason.

Do not duplicate schema rules in a second hand-maintained implementation unless technically necessary.

## 4.4 Command behavior

Define and document unambiguous commands for:

- Read-only validation.
- Schema validation.
- Local qualification.
- Report generation.

Tests must prove that read-only commands do not alter reports or other durable project artifacts.

Test existing-output behavior explicitly.

## 4.5 WP2-R1 exit criteria

WP2-R1 is complete only when:

- P2-001 is remediated.
- P2-002 is remediated.
- P2-003 is remediated or explicitly reclassified with justification.
- Tests cover the remediations.
- Validation commands have correct exit codes.
- No silent report overwrite is possible.
- Local qualification output accurately describes what was tested.

Produce:

`workspace/deliverables/phase-2-harness-remediation.md`

---

# 5. WP2-R2 — Establish reproducible session evidence

Every live workflow qualification session must have an evidence manifest.

Create a reusable format, for example:

`workspace/evidence/session-manifest.schema.json`

and a manifest for each qualification session.

## 5.1 Required manifest fields

Each manifest must capture:

### Identity

- Session identifier.
- Workflow name.
- Qualification test identifier.
- Start and end timestamps with timezone.
- Operator or execution identity, using non-sensitive identifiers.

### Repository

- Repository identity.
- Git revision.
- Branch.
- Dirty-state status.
- Relevant staged, unstaged, and untracked changes.
- SHA-256 hashes of relevant files.

### Runtime

- OpenCode version.
- Provider.
- Model.
- Agent configuration revision.
- Discovered agents and their modes.
- Relevant runtime capabilities and tool availability.

### Execution

- User task or test scenario.
- Expected acceptance criteria.
- Actual delegated specialists.
- Task identifiers where available.
- Specialist result statuses.
- Evidence references.
- Failed attempts and retries.
- Timeouts and blocked conditions.
- Final Professor disposition.

### Evidence integrity

- Transcript or event-log references.
- SHA-256 hashes of evidence files.
- Evidence classification.
- Whether each result was directly observed or merely reported.
- Independent verification references.

### Qualification

- Expected outcome.
- Actual outcome.
- PASS / FAIL / PARTIAL / BLOCKED / NOT_VERIFIED.
- Explanation.
- Unresolved limitations.

## 5.2 Evidence requirements

A manifest must not claim a session ran against a clean revision if the working tree was dirty.

Do not infer nested specialist execution from a top-level task event.

Do not treat a transcript summary as proof of tool execution.

Preserve original logs and failed attempts. Sanitization must be documented and must not erase the ability to understand the test outcome.

## 5.3 WP2-R2 exit criteria

- Manifest schema exists.
- Example manifests validate against it.
- All subsequent qualification sessions have manifests.
- Source revision and runtime identity are recorded.
- Evidence references are resolvable.
- Evidence hashes are reproducible.
- Missing evidence is explicitly identified.

Produce:

`workspace/deliverables/phase-2-evidence-manifest-specification.md`

---

# 6. WP2-R3 — Complete Research workflow qualification

The Research workflow must demonstrate that Professor can perform a bounded research task, delegate appropriately, inspect the returned evidence, and produce a traceable synthesis.

## 6.1 Test objective

Execute a real, bounded research assignment using the existing Professor and Research Assistant agents.

Choose a subject that:

- Has authoritative public documentation.
- Has a bounded set of questions.
- Can be researched without credentials.
- Does not require modifying external systems.
- Allows source claims to be independently checked.

Do not choose a topic merely because the result is easy to obtain.

## 6.2 Required execution

1. Submit the task to Professor.
2. Professor defines the task and acceptance criteria.
3. Professor delegates to Research Assistant.
4. Research Assistant returns findings with source references.
5. Professor inspects the returned source evidence.
6. Professor distinguishes facts, inferences, and unknowns.
7. Professor produces a final synthesis.
8. Independent validation checks the cited sources and material claims.

If a research tool is unavailable, Professor must report that limitation and either use an explicitly permitted alternative or return a bounded partial result.

Do not silently claim a tool was used if it was not available.

## 6.3 Acceptance criteria

- The task is bounded and has explicit acceptance criteria.
- Delegation actually occurs.
- The specialist returns source-backed findings.
- Material claims can be traced to sources.
- Professor independently inspects the evidence used in the final answer.
- Version-sensitive statements identify their applicable versions.
- Unsupported claims are removed or labeled.
- Tool limitations are disclosed.
- The final report is complete enough to answer the original task.

A specialist saying that it retrieved a source is not sufficient. The source must be inspectable.

## 6.4 Output

Produce:

`workspace/deliverables/phase-2-research-qualification.md`

Include the manifest reference and evidence references.

---

# 7. WP2-R4 — Complete Research & Design workflow qualification

This workflow must demonstrate a complete research-to-design process, not just research collection or an incomplete design draft.

## 7.1 Test objective

Execute a bounded technical design task using the existing Professor, Research Assistant, Architecture Agent, Adversarial Reviewer, and Validation Agent as appropriate.

The task should be sufficiently substantive to exercise:

- Requirements extraction.
- Source and repository inspection where applicable.
- Design alternatives.
- Trade-off analysis.
- Architecture decisions.
- Failure and security analysis.
- Implementation sequencing.
- Independent adversarial review.
- Final validation.

Do not expand the task into a full product redesign.

## 7.2 Required deliverable sections

The design deliverable must include:

1. Problem statement.
2. Goals and non-goals.
3. Requirements and acceptance criteria.
4. Existing-state assessment.
5. Evidence and assumptions.
6. Constraints.
7. Candidate designs.
8. Trade-off analysis.
9. Recommended design and rationale.
10. Component responsibilities.
11. Interfaces and contracts.
12. Data and state transitions, where relevant.
13. Security and trust boundaries.
14. Failure modes and recovery.
15. Observability and diagnostics.
16. Test strategy.
17. Implementation phases.
18. Migration or compatibility considerations, if relevant.
19. Risks and unresolved questions.

For each major recommendation, distinguish evidence from inference and design judgment.

## 7.3 Required execution

1. Professor decomposes the assignment.
2. Research Assistant gathers evidence.
3. Architecture Agent produces a design.
4. Adversarial Reviewer challenges the design.
5. Professor resolves or explicitly retains review findings.
6. Validation Agent evaluates the revised deliverable against acceptance criteria.
7. Professor produces the final result and status.

A reviewer rejection must not disappear from the final record.

A validation failure must not be converted to PASS through wording changes.

If a specialist is not invoked, explain why.

## 7.4 Acceptance criteria

- Research and design both complete.
- The architecture is supported by evidence and explicit assumptions.
- Alternatives and trade-offs are documented.
- Security, failure, and operational concerns are addressed.
- Adversarial review actually occurs.
- Review findings have explicit dispositions.
- Independent validation actually occurs.
- All required deliverable sections are present.
- No unresolved blocking finding is hidden.
- The final result is complete and traceable.

## 7.5 Output

Produce:

`workspace/deliverables/phase-2-research-and-design-qualification.md`

Include the actual design artifact, review report, validation report, manifest, and evidence references.

---

# 8. WP2-R5 — Complete Code Review workflow qualification

The Code Review workflow must demonstrate that Professor can coordinate a meaningful code review, challenge findings, and ensure that the final findings are evidence-backed and correctly qualified.

## 8.1 Test objective

Select a bounded, non-production code scope with enough complexity to test the workflow.

Do not make production changes.

The test must permit independent inspection of source files and, where appropriate, safe local reproduction.

## 8.2 Required execution

1. Professor establishes review scope and criteria.
2. Code Analyst inspects the code and identifies findings.
3. Each finding identifies:
   - Finding ID.
   - Severity.
   - Package and source file.
   - Relevant function/class or line range.
   - Observed behavior.
   - Expected behavior.
   - Evidence.
   - Impact.
   - Proposed remediation.
   - Confidence.
4. Adversarial Reviewer challenges the findings.
5. Professor resolves disputes and corrects unsupported claims.
6. Validation Agent checks the final review against its acceptance criteria.
7. Professor produces the final review.

Where safe and authorized, run targeted tests or reproduce a finding locally. If not performed, label the finding as static-analysis-only or unverified.

## 8.3 Acceptance criteria

- Code scope and revision are recorded.
- Review findings are tied to actual source.
- Severity is justified.
- Findings distinguish confirmed defects from risks and hypotheses.
- Adversarial review occurs.
- Validation occurs.
- Reviewer and validator results are preserved.
- Unsupported findings are corrected or withdrawn.
- No production modification occurs.
- Final findings are traceable to evidence.

## 8.4 Output

Produce:

`workspace/deliverables/phase-2-code-review-qualification.md`

Include the reviewed revision, file hashes, findings, specialist outputs, adversarial dispositions, validation results, and evidence manifest.

---

# 9. WP2-R6 — Live failure handling and recovery qualification

This is a critical work package.

The objective is to test Professor's behavior through the actual orchestration path.

A Python script that feeds an expected status into a predicate is not a live failure injection.

A static instruction check is not a live failure injection.

A simulated specialist response is not evidence of live behavior unless it is injected into the actual Professor execution boundary and Professor processes it.

## 9.1 Safety requirements

Perform tests only in an isolated, non-production session.

Do not expose credentials or invoke external side effects.

Use controlled test inputs and clearly marked synthetic responses.

Do not corrupt persistent project state.

Preserve all test inputs and outputs.

## 9.2 Required live scenarios

Execute at least these scenarios:

### F1 — No supporting evidence

Give Professor a task for which the supplied evidence is insufficient.

Verify that it:

- Does not fabricate facts.
- Does not claim completion.
- Identifies the missing evidence.
- Returns a partial or blocked disposition as appropriate.

### F2 — Incomplete specialist result

Have a specialist return an explicitly incomplete result.

Verify that Professor:

- Detects the incomplete result.
- Does not treat it as completed.
- Identifies the missing acceptance criteria.
- Requests additional work or returns PARTIAL.

### F3 — Conflicting specialist results

Provide conflicting findings from two specialist results.

Verify that Professor:

- Identifies the conflict.
- Does not silently choose one.
- Requests evidence or explains the unresolved conflict.
- Preserves the conflict in its final disposition if unresolved.

### F4 — Adversarial rejection

Have the Adversarial Reviewer reject a deliverable with a concrete finding.

Verify that Professor:

- Preserves the rejection.
- Does not claim acceptance.
- Addresses the finding or reports the unresolved blocker.

### F5 — Validation failure

Have the Validation Agent return FAIL or REVISE.

Verify that Professor:

- Preserves the failure.
- Does not mark the deliverable complete.
- Either performs authorized remediation and revalidation or returns a non-accepted status.

### F6 — Unavailable capability

Run a bounded task requiring a capability known to be unavailable in the environment.

Verify that Professor:

- Detects or accurately receives the unavailability.
- Does not fabricate a tool result.
- Does not silently substitute an unapproved capability.
- Discloses the limitation.

### F7 — Blocked specialist

Make a required specialist unavailable or provide a controlled blocked result through the actual execution path.

Verify that Professor:

- Preserves BLOCKED.
- Identifies the dependency.
- Does not claim the delegated work completed.

### F8 — Malformed specialist response

Inject a malformed response at the actual specialist-result boundary.

Examples:

- Missing required result fields.
- Invalid status.
- Wrong field types.
- Missing evidence references.
- Contradictory status and completion claims.

Verify that Professor:

- Detects invalid structure.
- Does not accept the malformed result as completed.
- Rejects, quarantines, or requests a corrected result.
- Preserves the failure evidence.

## 9.3 Test execution record

For every scenario record:

- Test ID.
- Session ID.
- Test input.
- Injection method.
- Actual orchestration boundary exercised.
- Expected behavior.
- Actual behavior.
- Transcript/event evidence.
- Specialist identities.
- Professor's final disposition.
- Independent verification.
- PASS / FAIL / BLOCKED / NOT_VERIFIED.

Do not mark a scenario PASS based solely on a specialist's self-description.

If the current OpenCode environment cannot inject or observe a particular failure at the required boundary, record it as NOT_VERIFIED and explain the technical limitation.

Do not implement an elaborate test framework solely to force these scenarios to pass. Prefer a small, explicit, reproducible qualification mechanism.

## 9.4 WP2-R6 exit criteria

All eight scenarios must have been attempted through the actual orchestration path.

Every PASS must be supported by observable evidence.

Any failed or unverified scenario prevents a full operational qualification PASS.

Produce:

`workspace/deliverables/phase-2-live-failure-qualification.md`

---

# 10. WP2-R7 — Demonstrate actual task/result contract enforcement

Local JSON Schema validation is necessary but does not prove that Professor enforces contracts when delegating and receiving specialist results.

Demonstrate the actual enforcement behavior.

## 10.1 Required checks

Verify:

1. Professor produces or delegates a task conforming to the task contract.
2. The delegated task contains explicit acceptance criteria.
3. The receiving specialist receives the intended task and constraints.
4. Specialist results conform to the result contract.
5. Invalid results are not accepted as successful completion.
6. PARTIAL, BLOCKED, FAILED, and REVISE are preserved.
7. Evidence references are retained.
8. Missing required fields are detected.
9. Status and completion claims are consistent.
10. Professor's final result reflects actual specialist outcomes.

## 10.2 Enforcement boundary

Determine exactly where validation occurs:

- Before delegation.
- At the specialist boundary.
- When the specialist result is received.
- Before final synthesis.
- At final result production.

Document which boundaries are actually enforced by runtime behavior and which are only documented conventions.

Do not claim enforcement merely because the schema files exist.

If OpenCode does not provide a native enforcement hook at a particular boundary, document the actual mechanism used by Professor and its limitations.

Do not invent capabilities or assume that prompt instructions are equivalent to hard runtime enforcement.

## 10.3 Acceptance criteria

- Valid and invalid task/result cases are exercised.
- At least one malformed result is rejected through the actual orchestration path.
- At least one partial result is correctly preserved.
- At least one blocked result is correctly preserved.
- Professor's final status is consistent with the delegated outcomes.
- Evidence proves the behavior.

Produce:

`workspace/deliverables/phase-2-contract-enforcement-qualification.md`

---

# 11. WP2-R8 — Independent adversarial review and final qualification

The final qualification must not be self-certified by the implementation agent.

## 11.1 Review independence

Use the existing Adversarial Reviewer and Validation Agent.

They must evaluate the actual remediation and qualification evidence, not merely the completion report.

Where possible, provide them with:

- Source changes.
- Test results.
- Session manifests.
- Raw evidence references.
- Workflow deliverables.
- Failure-injection results.
- Contract-enforcement results.
- Previous findings and their dispositions.

Do not ask reviewers to accept claims without evidence.

Do not hide previous failures or failed attempts.

## 11.2 Adversarial review questions

The Adversarial Reviewer must answer:

1. Were all three accepted harness findings actually remediated?
2. Can local qualification still produce misleading PASS results?
3. Can validation overwrite durable deliverables?
4. Are failure-policy checks genuinely independent?
5. Are session manifests sufficient to reproduce the test context?
6. Did the Research workflow actually complete?
7. Did Research & Design actually complete?
8. Did Code Review actually complete?
9. Were the eight failure scenarios tested through live orchestration?
10. Is malformed specialist output rejected?
11. Are task/result contracts enforced at the actual boundaries claimed?
12. Can a specialist falsely report completion and have Professor accept it?
13. Are evidence provenance and integrity adequate?
14. Are there hidden failures, unverified claims, or acceptance-criteria gaps?
15. Does the final evidence justify operational readiness?

Every finding must include severity, evidence, impact, and disposition.

## 11.3 Validation Agent requirements

The Validation Agent must independently assess:

- Scope compliance.
- Acceptance criteria.
- Test completeness.
- Evidence traceability.
- Reproducibility.
- Workflow completion.
- Security handling.
- Outstanding defects.
- Final readiness decision.

The Validation Agent must be permitted to return FAIL or REVISE.

Do not modify validation criteria after receiving its result to obtain PASS.

## 11.4 Outputs

Produce:

- `workspace/deliverables/phase-2-adversarial-review.md`
- `workspace/deliverables/phase-2-validation-report.md`
- `workspace/deliverables/phase-2-completion-report.md`
- `workspace/deliverables/phase-2-qualification-summary.json`

The completion report must link every claim to evidence.

---

# 12. Final qualification decision

Use exactly these readiness classifications:

| Classification | Meaning |
|---|---|
| `READY` | All mandatory Phase 2 operational criteria demonstrated and independently validated |
| `READY_WITH_LIMITATIONS` | All critical criteria pass; only explicitly accepted non-blocking limitations remain |
| `NOT_READY` | One or more mandatory criteria failed or remain unverified |

Do not introduce a weaker status that obscures a failed mandatory criterion.

## 12.1 Mandatory READY criteria

A `READY` decision requires all of the following:

- P2-001 remediated.
- P2-002 remediated.
- P2-003 remediated or correctly reclassified.
- Read-only validation behavior proven.
- Local schema validation proven.
- Per-session evidence manifests complete.
- Research workflow qualified.
- Research & Design workflow qualified.
- Code Review workflow qualified.
- Eight live failure scenarios attempted and passed.
- Actual task/result enforcement demonstrated.
- Malformed specialist result rejected.
- Partial and blocked results preserved.
- Independent adversarial review accepted.
- Independent validation accepted.
- No unresolved critical or high-severity finding.
- Security evidence scan clean, with outstanding credential-owner actions disclosed.
- Scope boundaries respected.

`READY_WITH_LIMITATIONS` may be used only if every critical and mandatory operational criterion passes and the remaining limitations are explicitly non-blocking.

If a required live scenario cannot be exercised, the result is `NOT_READY`, not `READY_WITH_LIMITATIONS`.

## 12.2 Final decision format

The completion report must include:

1. Final classification.
2. Mandatory criteria matrix.
3. Work package dispositions.
4. Findings disposition matrix.
5. Live qualification results.
6. Contract enforcement results.
7. Evidence manifest index.
8. Independent reviewer decisions.
9. Outstanding risks and limitations.
10. Exact reason for the readiness decision.
11. Recommended next action.

Do not recommend Phase 3 unless the Phase 2 exit criteria are met and independently accepted.

---

# 13. Execution sequencing and gates

Execute in this order:

| Gate | Work | Required decision |
|---|---|---|
| G0 | Baseline and evidence inventory | Baseline accepted |
| G1 | WP2-R1 harness remediation | Local qualification trustworthy |
| G2 | WP2-R2 evidence manifests | Reproducibility established |
| G3 | WP2-R3 Research | Research qualified |
| G4 | WP2-R4 Research & Design | R&D qualified |
| G5 | WP2-R5 Code Review | Code Review qualified |
| G6 | WP2-R6 live failure tests | Failure handling demonstrated |
| G7 | WP2-R7 contract enforcement | Runtime boundary behavior demonstrated |
| G8 | WP2-R8 independent review | Final qualification decision |

At each gate:

1. Record work completed.
2. Record changed files.
3. Run relevant tests.
4. Record evidence.
5. Identify unresolved findings.
6. Obtain the required acceptance decision before continuing.

If a gate fails:

- Preserve the failed result.
- Identify the cause.
- Perform only in-scope remediation.
- Repeat the relevant qualification.
- Preserve both the original and retest evidence.

Do not silently bypass a failed gate.

---

# 14. Change control

Before modifying any file:

1. Identify the defect or requirement.
2. Identify the exact file and relevant code.
3. Explain the proposed change.
4. Identify the tests that will demonstrate the change.
5. Confirm the change is within the bounded scope.

Maintain:

`workspace/deliverables/phase-2-remediation-change-log.md`

For each change record:

- Change ID.
- Finding or work package.
- File path.
- Previous behavior.
- New behavior.
- Rationale.
- Tests.
- Evidence.
- Regression risk.

Do not modify unrelated files.

Do not overwrite prior qualification reports or session evidence.

Version new reports or require explicit overwrite authorization.

---

# 15. Definition of Done

This effort is complete only when:

1. All authorized remediation work is completed or explicitly dispositioned.
2. All three accepted harness findings have evidence-backed dispositions.
3. The qualification harness no longer overstates what it tests.
4. Read-only validation has no durable-report overwrite side effect.
5. Evidence manifests support reproducibility.
6. All three workflows have been attempted and their outcomes correctly classified.
7. Live failure scenarios have been attempted through the actual orchestration path.
8. Task/result enforcement has been tested at the actual runtime boundaries claimed.
9. Independent adversarial review is complete.
10. Independent validation is complete.
11. The final readiness classification is evidence-backed.
12. All deliverables are present and cross-referenced.
13. Security limitations and credential-owner responsibilities are disclosed.
14. No Phase 3 work has been introduced.

A successful local test suite is not sufficient to satisfy this Definition of Done.

A successful specialist chain is not sufficient to satisfy this Definition of Done.

A complete-looking report is not sufficient to satisfy this Definition of Done.

Only demonstrated behavior supported by reproducible evidence can establish operational readiness.

---

# 16. Initial instruction to Professor / Luna

Begin with G0 only.

Inspect the current repository, existing Phase 2 evidence, scripts, workflows, schemas, and reports.

Do not immediately start modifying files.

Return a concise but technically complete baseline assessment containing:

- Current revision and dirty state.
- Exact files inspected.
- Confirmed P2-001, P2-002, and P2-003 source locations and current disposition.
- Existing tests and their actual scope.
- Existing live qualification evidence.
- Missing evidence.
- Proposed bounded file changes.
- Dependencies and environmental limitations.
- G0 acceptance recommendation.

Wait for the G0 baseline decision before proceeding to G1.

**Final instruction:** The objective is not to achieve a PASS. The objective is to discover whether Professor deserves a PASS. Preserve that distinction throughout the entire effort.
