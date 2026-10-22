The latest revision closes the previous DEP-02b uncertainty and adds meaningful traceability for the decisions made on 2026-10-22. The next review should focus less on restating the design principles and more on internal consistency, approval-state accuracy, database invariants, and whether the contracts are sufficiently precise to implement without conflicting interpretations.

The most important issues I would send back to the agent are:

- AG-02b remains unresolved, and that affects the Coverage Report acceptance criterion.
- AG-06a is marked ACCEPTED while its required Security sign-off is still outstanding. The row needs to distinguish acceptance of the data-only design from approval to execute bridges.
- The report identity, generation, retry, and hash rules need a tighter specification, particularly the relationship between `report_id`, tenant-scoped uniqueness, and deterministic retries.
- The acceptance criteria and traceability matrix need a mechanical consistency check, including whether every required approval and deferred decision is represented accurately.
- The claim that 0R adds exactly two tables needs to be checked against the confirmed migration and existing platform schema, without reopening or expanding the PostgreSQL proof gate.

Here is a ready-to-send review prompt that directs the agent to address these issues without silently changing the agreed scope.

Writing

# Review Request — IAP Onboarding Agent Provisional Design Contract, Latest Revision

## Objective

Review the latest version of the **IAP Onboarding Agent — Provisional Design Contract** as a critical design reviewer. Identify remaining contradictions, underspecified contracts, incorrect approval states, and implementation ambiguities.

The goal is to establish whether the document is genuinely review-complete under §13, not merely whether the latest feedback has been incorporated.

Do not rewrite the document wholesale. Return findings first, with precise section and row references, proposed corrections, and the approval or evidence required to close each issue.

## 1\. Preserve the authority and scope boundaries

Treat the following as non-negotiable:

- The document remains `PROVISIONAL — FOR REVIEW ONLY` until its acceptance conditions and required approvals are satisfied.
- Platform-owned contracts take precedence over this document.
- The agent remains a read-only advisor: it may change minds, never onboarding control state.
- Workflow activities, repositories, deterministic validators, and authorized humans retain their respective responsibilities.
- The actual-role PostgreSQL proof remains the priority Phase 0R gate.
- DEP-02b is recorded as confirmed on 2026-10-22: the two registry/report tables land in migration 024, with RLS, policies, and grants, and without hypothetical seed rows or grant seeding.
- Do not reopen or expand Phase 0R, invent approval decisions, or mark a contract approved based on the existence of review feedback.
- Do not update the ledger. No ledger change is authorized by this review.

If an assertion depends on a referenced repository document, migration, database constraint, or owner decision, distinguish verified evidence from claims in the supplied design. Do not pretend to have inspected sources that are unavailable.

## 2\. Audit the approval and status register

Review every AG-01a through AG-16 entry in §10 against its corresponding contract, decision register, open question, and §13 acceptance criteria.

Pay particular attention to:

1. **AG-02b:** It remains PROPOSED and requires Phase 1B owner approval. Identify exactly which linkage fields, canonicalization rules, generation semantics, and consuming API decisions remain open. Determine whether §5.1 describes them as established requirements more strongly than their approval status permits.
2. **AG-03:** The Coverage Report schema remains PROPOSED, despite incorporating owner markup. Verify that the document clearly distinguishes incorporated feedback from formal approval.
3. **AG-06a:** It is ACCEPTED, but its required sign-off column names Security for executing bridges and sandbox policy. Determine whether the row correctly limits acceptance to bridge trust states, data-only defaults, and per-use verification. Recommend wording that prevents ACCEPTED from being interpreted as authorization to execute bridges.
4. **AG-05b and DEP-02b:** Confirm that acceptance of table placement does not implicitly approve all registry governance, seed-data, promotion, or authorization policies.
5. **AG-10:** It is ACCEPTED as a model-agnostic default, but its reopening trigger remains in §11.2. Determine whether this is a valid standing decision with a future review trigger or a status inconsistency.
6. **AG-01a and AG-01b:** Verify that acceptance of the read-only principle does not imply approval of the detailed grant implementation.
7. **AG-04 and AG-05a:** Confirm that the accepted design contracts do not falsely imply that formal JSON Schemas, production sniffers, registry storage, or seed data are already implemented or approved.

For every finding, state the current status, the specific problem, the minimum correction, the decision owner, and the required sign-off.

Do not downgrade valid accepted principles merely because implementation work remains. Conversely, do not let acceptance of a principle imply acceptance of unapproved implementation details.

## 3\. Review Coverage Report identity, idempotency, and hashing

Perform a detailed contract-level review of §5.1.

### Identity and uniqueness

Check the consistency of:

- `report_id` as both report identity and storage row primary key.
- `tenant_id` as a required component of the database uniqueness constraint but not currently listed in the report's field table.
- `(tenant_id, request_id, revision, generation)` uniqueness.
- One report per analysis generation.
- Re-analysis versus retry semantics.
- Report identity reuse on retries.

Determine whether the contract unambiguously specifies the following cases:

- Two tenants use otherwise identical request identifiers.
- A retry follows a successful report filing but the caller did not receive confirmation.
- Two workers concurrently attempt the same generation.
- A generation is retried with changed evidence or changed catalog/registry pins.
- A new generation is created while an older report remains referenced by an existing decision.

Identify whether tenant identity belongs in the report body, the storage envelope, or both. Do not invent a platform answer where the shared contract must decide.

### Hash canonicalization

Check whether the `input_manifest_hash` and `report_content_hash` definitions are implementable deterministically.

In particular, establish whether the document specifies or must defer:

- Canonical JSON serialization and character encoding.
- Hash input boundaries and exact field inclusion.
- Ordering of arrays and collections.
- Treatment of nulls, absent fields, and numeric representations.
- Inclusion or exclusion of tenant and report identity fields.
- Whether `report_id`, `generation`, `analyzer_version`, and `model_version` belong in the report content hash.
- How the workflow's audit/filing envelope is separated from the hashed report body.
- How a retry distinguishes an identical request from a conflicting attempt using the same idempotency key.

Flag any ambiguity that could produce different hashes for semantically identical inputs, or identical hashes for materially different report bodies.

Propose a minimal canonicalization contract or a clearly bounded list of decisions for the Phase 1B owner. Do not claim that a hash is a signature or proof of authenticity.

## 4\. Check report linkage against platform ownership

Review the relationship between:

- AG-02a and AG-02b.
- DEP-02a and DEP-02b.
- §5.1's link-not-embed rule.
- §9's phase dependencies.
- §11.1's rework-critical decisions.
- §12's open question 1.
- §13's acceptance criterion 2.

Determine whether the design consistently distinguishes:

1. The accepted principle that reports are platform-derived evidence linked to intake rather than embedded in it.
2. The proposed shared linkage contract.
3. The approved storage and migration placement.
4. The consuming API and readiness/activation reference semantics.
5. The policy decision on whether coverage may gate activation.

These are separate decisions. Identify duplicated, conflicting, or misleading language and propose the smallest edits that preserve the ownership boundaries.

## 5\. Verify Phase 0R scope and migration claims

Review DEP-01 through DEP-03, DEP-02b, §8, §9, §11.1, and §13.5.

The latest design states that migration 024 creates two tables with RLS, policies, and grants, without seed rows or grant seeding.

Check the available implementation plan and migration artifacts if repository access exists. Establish whether the following are actually confirmed:

- Exactly which two tables are created.
- Their ownership and relationship to the existing control store.
- Tenant isolation and RLS enforcement.
- The intended read/write grant separation.
- Whether migration 024 introduces any additional objects or authorization paths.
- Whether the grant design remains compatible with the existing actual-role PostgreSQL proof.
- Whether report filing under workflow authority is consistent with the intended table grants.

Do not infer successful RLS enforcement merely from the presence of RLS policies or grants. Distinguish schema inspection from tests executed under the actual roles.

If evidence is unavailable, mark the relevant item **unverified**, specify the artifact or test needed, and leave the recorded plan-owner decision intact unless contradictory evidence is found.

Do not propose moving unrelated database work into 0R.

## 6\. Audit the three invocation contracts and trust boundaries

Check that the three stateless operations in §2 have sufficiently clear and distinct authorization boundaries:

1. Coverage analysis.
2. Watchdog diagnosis.
3. Report Q&A.

Test whether the design prevents:

- Coverage analysis from independently querying control state.
- Watchdog diagnosis from reading state outside its authorized tenant-scoped snapshot.
- Q&A from using unvalidated reports or unresolved evidence pointers.
- Agent-generated commands from being executed automatically.
- Agent outputs from being treated as approvals or state-transition authority.
- Scratch writes from becoming an undeclared state-mutation path.
- Bridge execution through an agent-invoked tool.

Review §3, §6, §7, and §8 for any conflicts in the assignment of analysis, filing, audit, verification, and transition responsibilities.

Check that `VERIFIED`, `PROJECTED`, and `SPECULATIVE` are applied at claim level, that PROJECTED and SPECULATIVE claims require confirming probes, and that static evidence cannot establish runtime behavior without actual runtime evidence.

Identify any place where bridge trust is incorrectly conflated with per-estate conversion correctness or activation eligibility.

## 7\. Review the sniffer and evidence contracts for implementability

Review §4 and §5.3 for missing invariants, contradictions, and unsafe defaults.

Focus on:

- Census completeness, skipped paths, text/binary classification, LOC counting, and totals reconciliation.
- Build-directory validation and module source-root resolution.
- Explicit distinction between unsupported input, incomplete evidence, malformed input, and execution failure.
- Stable hashes and deterministic serialization.
- Redaction failure and uncertain-redaction exclusions.
- Dependency inventory and SBOM cross-check limitations.
- Evidence-pointer resolution against immutable, content-addressed sniffer outputs.
- The relationship between partial outputs and report generation.
- Validator behavior when a required sniffer or evidence source is missing.

Check whether every claim can be traced to evidence and whether the contract explains what happens when that evidence becomes unavailable or no longer matches the pinned estate.

Do not invent capabilities or claim that pattern-based secret detection guarantees complete secret discovery.

## 8\. Validate the status matrix and open-question rollup

Mechanically cross-check §10, §11, and §12.

Verify that:

- Every traceability row has exactly one valid status.
- Every decision has an accountable owner and the appropriate sign-off.
- Every deferred decision has an explicit reopening event and a safe default.
- Every §11.2 parked decision is represented in §10.
- Open questions do not accidentally contradict accepted decisions.
- Repeated decisions use consistent terminology and dates.
- No open question is incorrectly treated as authorization to implement.
- No accepted contract is described as both finalized and awaiting the same approval.

Review open questions 1–12 individually. Recommend closing an item only when its decision is supported by the supplied evidence or an authoritative source. Otherwise retain it with a named owner and trigger.

## 9\. Test the five acceptance criteria in §13

For each criterion, return one of:

- **PASS** — supported by the document and available evidence.
- **FAIL** — a concrete contradiction or missing requirement exists.
- **UNVERIFIED** — repository evidence or a required owner decision is unavailable.
- **BLOCKED** — a prerequisite decision or approval remains outstanding.

Provide the evidence and exact section references supporting each result.

Pay particular attention to criterion 2: the report contract must not be declared complete while AG-02b still requires Phase 1B owner approval.

Also determine whether criterion 4 is satisfied by the current status matrix, including the distinction between acceptance of a design principle and the approval needed before implementation or execution.

A document can be substantially improved and ready for review without being review-complete. Preserve that distinction.

## 10\. Required response format

Return the review in this order:

### A. Executive verdict

Choose one:

- Ready for whiteboard review.
- Reviewable, with blocking issues.
- Not ready for review.

Separately state whether the document meets its own §13 review-complete criteria. Do not conflate review readiness with implementation authorization.

### B. Findings by severity

Use these categories:

- **BLOCKER:** A contradiction or missing decision that prevents the relevant acceptance criterion or phase gate from being satisfied.
- **HIGH:** A material security, integrity, ownership, or implementation ambiguity.
- **MEDIUM:** A contract clarification or traceability defect likely to cause rework.
- **LOW:** Editorial or maintainability improvement.

For each finding, include:

1. Finding ID.
2. Severity.
3. Exact section/table row.
4. Evidence from the supplied document or inspected authoritative artifact.
5. Why the issue matters.
6. Required correction.
7. Decision owner and sign-off.
8. Whether it blocks review completion, a phase gate, implementation, or only a later decision.

Avoid hypothetical objections unless they expose a concrete ambiguity or failure mode.

### C. Acceptance-criteria scorecard

Provide a five-row table for §13 criteria 1–5 with status, evidence, and closure requirements.

### D. Approval and decision ledger

Provide a concise list of:

- Decisions confirmed by authoritative evidence.
- Decisions accepted at the design level but awaiting implementation sign-off.
- Proposed decisions awaiting owner approval.
- Deferred decisions with triggers.
- Claims that remain unverified.

Do not update the actual project ledger.

### E. Minimal correction plan

List only the changes necessary to resolve identified issues. Separate:

- Document edits.
- Phase 1B owner decisions.
- Security or platform sign-offs.
- Repository/migration verification.
- Later-phase policy decisions.

For each change, identify the target section and the expected resulting status.

### F. Proposed patch

If corrections are needed, provide minimal replacement wording or a focused diff for the affected paragraphs and rows. Preserve the existing document structure and accepted decisions. Do not silently rewrite the entire contract.

### G. Final disposition

End with an explicit statement of:

- Whether the document is review-complete.
- Which approvals remain outstanding.
- Whether any phase gate is affected.
- Whether implementation remains unauthorized.

## Review discipline

Be exacting but proportionate. Do not manufacture defects just to produce findings. Distinguish actual contradictions from decisions intentionally deferred to later phases.

Prefer a small, actionable set of evidence-backed findings over a broad speculative redesign.

**The task is to review and report, not to implement, change database state, update the ledger, or declare unapproved decisions accepted.**


