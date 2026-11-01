# Workflow: Code Review

## 1. Purpose and Activation

Use this workflow for repository code review, defect investigation, security or reliability review, architecture/implementation gap analysis, technical debt assessment, remediation planning, or independent assessment of coding-agent changes. Use Research and Design for proposed new systems or subsystems; use Research for general factual questions without a repository review.

Professor is the sole orchestrator and adjudicator. Code Analyst investigates the implementation; Research Assistant verifies relevant external technical claims; Adversarial Reviewer independently challenges findings; Validation Agent checks the actual final report. Specialists return bounded results and cannot declare the whole review successful.

## 2. Stage 1 — Establish Review Scope

Before analysis, record:

- Repository location, exact revision, and branch when available.
- User objective, review scope, excluded paths/components, and relevant uncommitted changes.
- Applicable requirements, specifications, architecture decisions, and known defects.
- Expected deliverables and acceptance criteria.

Identify the exact source revision. Do not report findings against an unidentified revision. If the worktree is dirty, distinguish relevant uncommitted changes from the base revision. Preserve all user changes; do not discard or modify them during review. If the revision cannot be established, report that limitation and do not claim revision-specific conclusions.

## 3. Stage 2 — Repository Reconnaissance

Assign Code Analyst a bounded read-only reconnaissance and analysis. Inspect relevant project structure, language/framework, dependency configuration, entry points, package/service boundaries, APIs, data models and persistence, configuration, authentication and authorization, logging, errors, background processing, external integrations, tests, and deployment configuration.

Do not call a review exhaustive unless all relevant areas were actually inspected. Record exclusions and their consequences. Code Analyst must trace material paths and callers/callees, not infer behavior from filenames alone.

## 4. Stage 3 — Establish Expected Behavior

Use this evidence order as applicable:

1. User-provided requirements.
2. Approved specifications.
3. Architecture decisions.
4. Existing interfaces and contracts.
5. Tests.
6. Documented invariants.
7. Observable implementation behavior.

Distinguish intended behavior from accidental behavior. Existing implementation is not proof of correctness. Label gaps in expected behavior as unresolved rather than inventing requirements.

## 5. Stage 4 — Trace Execution Paths

For each material path, identify entry point, input validation, authentication/authorization, business logic, service interactions, persistence, external calls, error handling, output, and side effects. Inspect relevant callers/callees. Explain control-flow gaps, incorrect assumptions, missing checks, or unreachable/incomplete behavior in context. Separate static observations from runtime claims.

When a finding requires command execution or a regression test, Code Analyst records the exact command and why it is needed without executing it. Professor owns authorization and may run a narrowly scoped, read-only verification command, or delegate it to an explicitly authorized executor. Record command output, reviewed revision, and limitations as evidence. If no authorized executor is available, classify the finding as a hypothesis or `NOT_VERIFIED`; static inspection is not runtime proof.

## 6. Stage 5 — Analyze Applicable Risks

Investigate categories that are relevant; do not force a finding into every category:

- **Functional:** business logic, missing requirements, state transitions, defaults, incomplete workflows, boundary conditions.
- **Reliability:** exceptions, retries, timeouts, resource leaks, races, partial writes, failure propagation, idempotency.
- **Security:** authentication, authorization, tenant isolation, secrets, injection, deserialization, untrusted input, sensitive logging, auditability.
- **Data integrity:** transactions, lost updates, inconsistent state, duplicate processing, uniqueness, validation, schema/contract drift.
- **Architecture:** responsibility leakage, circular dependencies, coupling, boundaries, hidden dependencies, scaling constraints.
- **Testing:** coverage, assertion strength, failure-path and integration gaps, tests that do not establish claimed behavior.
- **Operations:** configuration, observability, diagnostics, deployment, recovery.

Do not report hypotheses as confirmed defects. Unreproduced or incompletely established issues must be labeled as hypotheses or unverified risks.

## 7. Stage 6 — Finding Contract and Severity

Every confirmed finding must include:

| Field | Required content |
|---|---|
| Finding ID | Unique stable identifier. |
| Title | Concise defect statement. |
| Severity | CRITICAL, HIGH, MEDIUM, LOW, or INFORMATIONAL. |
| Category | Functional, Security, Reliability, Data, Architecture, Testing, or Operations. |
| Package/module | Affected package or module. |
| File and lines | Exact source location and line range where available. |
| Revision | Reviewed revision. |
| Observed behavior | What the code actually does. |
| Expected behavior | Required behavior or violated invariant and its source. |
| Trigger conditions | Conditions required for the failure. |
| Failure mechanism | Causal chain. |
| Root cause | Underlying cause, if established. |
| Impact | Concrete functional/system consequence. |
| Evidence | Source locators, tests, or reproduced observations. |
| Remediation | Concrete proposed correction. |
| Regression test | Test scenario and expected assertion. |
| Confidence | HIGH, MEDIUM, or LOW with basis. |
| Limitations | Missing verification or relevant uncertainty. |

Severity definitions:

- **CRITICAL:** exploitable security issue, major data corruption, or systemic outage with severe consequences.
- **HIGH:** significant functional failure, security weakness, or reliability issue affecting important behavior.
- **MEDIUM:** meaningful bounded defect or a viable workaround exists.
- **LOW:** localized, limited-impact defect or maintainability concern.
- **INFORMATIONAL:** observation that does not establish a defect.

Explain severity using supported impact, not subjective wording. Severity and confidence are independent.

## 8. Stage 7 — Independent Adversarial Review

Give Adversarial Reviewer the review scope, source revision, requirements, candidate findings with evidence, and exclusions. Ask it to independently challenge:

- Whether each significant finding is real and evidence-supported.
- Severity, trigger, impact, and root-cause accuracy.
- Whether remediation addresses the cause or introduces regressions.
- False positives and plausible alternative explanations.
- Missing defect classes and scope gaps.

Do not ask it merely to rewrite findings. Record which checks were performed and which were not.

## 9. Stage 8 — Professor Adjudication

Professor adjudicates each significant finding as `ACCEPTED`, `REJECTED`, `NEEDS_MORE_EVIDENCE`, `RECLASSIFIED`, or `DUPLICATE`. Compare underlying evidence, scope, assumptions, and reasoning. Preserve disagreements; do not silently suppress disputed findings or decide by confidence/majority alone. Report remaining uncertainty as such.

## 10. Stage 9 — Independent Validation

Give Validation Agent the actual completed report, original requirements, scope, finding evidence, and adversarial dispositions. It checks evidence and source locations, severity, scope coverage, requirement traceability, remediation feasibility, regression-test guidance, and report completeness. Validation must inspect the artifact itself and must not silently correct it. Record the returned gate result and unresolved gaps.

## 11. Deliverables

Create `workspace/deliverables/code-review-report.md` with:

1. Executive Summary.
2. Repository and Revision.
3. Review Scope and Exclusions.
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

When the user requests an implementation prompt, also create `workspace/deliverables/code-review-remediation-prompt.md`. Organize actionable changes by priority and dependency; identify affected packages/files, expected behavior, implementation guidance, and regression tests. Do not implement review fixes unless implementation is explicitly authorized.

## 12. Acceptance Criteria

- Reviewed revision is identified, or revision-specific conclusions are marked not verified.
- Scope, exclusions, dirty changes, and limitations are explicit.
- Findings are source-grounded and severity is justified by impact.
- Hypotheses are not reported as confirmed defects.
- Significant findings are adversarially challenged; false positives are considered.
- Professor adjudicates findings and preserves material disagreements.
- Remediation and regression tests are actionable.
- Validation checks the actual deliverable.
- No unverified claim or unrun test is presented as fact.
- No source change is made unless separately and explicitly authorized.
