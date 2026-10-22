## Review verdict: strong draft, but not ready to freeze

The draft is substantially better than a typical early agent design. It preserves the read-only principle, separates analysis from workflow authority, makes evidence traceability enforceable, and explicitly protects Phase 0R from scope creep.

I would approve the direction, not yet approve the contract as written. The main concern is that some provisional design choices have been turned into binding language before their owners have approved them. There are also a few inconsistencies between the boundary model, the proposed database integration, and the status register.

The goal of the next revision should be to make the contracts precise enough to prevent rework without accidentally committing the platform to premature implementation decisions.

## 1. Issues to resolve before the next revision

### 1. DEP-02 risks expanding Phase 0R

Blocking inconsistency§0 says the agent design must not expand Phase 0R, and that the actual-role PostgreSQL proof remains the priority. But DEP-02 and §11.1 make both `support_registry` and `coverage_reports` tables prerequisites for 0R exit.

Those statements cannot safely coexist without confirmation from the Phase 0R owner.

Recommendation: Separate the decision to define the registry/report contracts from the decision to create their database tables in the 0R migration wave. Keep the latter proposed until the implementation-plan owner explicitly confirms it fits the existing scope and exit criteria. If it does not, assign the tables to a later migration before their consuming phase needs them.

### 2. D-6 reverses the ownership of platform contracts

Blocking inconsistencyThe proposed rule says Phase C imports coverage-linkage and disposition types from the agent contracts package. But the disposition vocabulary is already a platform-owned contract, and the draft explicitly says the agent design does not redefine platform contracts.

Recommendation: The agent contracts should consume the canonical platform-owned disposition types, not become their authority. Coverage linkage should be defined in an approved shared contract, with the Phase C owner agreeing to its shape. Divergence should fail review only after that ownership and contract are approved.

Do not add D-6 to the rulebook as an unconditional requirement yet.

### 3. The pure-function definition is too broad

Contract clarificationThe declared function accepts sniffer outputs, catalog, registry, and snapshot, but §7 also describes a watchdog that reads control-store state, while §5.2 describes Q&A over a report.

These are different operations with different authorization boundaries. Also, the agent cannot execute a bridge that runs third-party code if all agent-invoked tools must be read-only.

Recommendation: Define separate stateless invocation contracts for:

- Coverage analysis: pinned snapshot and deterministic evidence in, coverage report out.
- Watchdog diagnosis: authorized, tenant-scoped state snapshot in, diagnosis out.
- Report Q&A: validated report and resolvable evidence in, cited answer out.

Bridge execution, where permitted, belongs to a separately authorized workflow activity and sandbox—not to the agent itself.

### 4. The Coverage Report lacks its complete linkage contract

Required before Phase 1B§5.1 says the report is linked by `(request_id, revision)` and has its own content hash, but the field table does not define these fields. It also lacks an explicit report identity and a generation/run identity for repeat analyses of the same revision.

Recommendation: Specify at least:

- `report_id` or an explicitly approved equivalent.
- `request_id` and `revision`.
- `generation` or `analysis_run_id`, with its uniqueness and retry semantics.
- `report_content_hash`, including exactly which canonical bytes are hashed.
- An input-manifest hash covering the pinned evidence and relevant versions.

Define whether timestamps and other volatile metadata are excluded from the content hash. Otherwise, identical analysis inputs may yield different hashes simply because the report was produced at a different time.

The report linkage contract needs Phase 1B owner approval; the full JSON Schema can still follow later.

## 2. Additional contract defects worth correcting now

These are not reasons to abandon the design. They are specific ambiguities that could become implementation bugs if the current language is treated as binding.

### Sniffer contracts

- `census.loc` is required for every regular file. That is not meaningful for binary blobs. Define file classification, byte size, text eligibility, and a precise LOC counting rule. LOC should be nullable or inapplicable for non-text files.
- `census.files[].parse` mixes different concepts. `symbol-only` describes a capability tier, not necessarily a parser outcome. Separate parser status—such as parsed, syntax errors, grammar unavailable, and skipped—from extracted capabilities and downstream support tier.
- `buildmap` source-root validation is inconsistent. Source roots are usually directories, while `census.files[]` inventories files. Validate roots against an explicit directory inventory or the directories implied by file paths.
- “Unknown inputs fail closed” needs qualification. An unsupported build system should produce an explicit gap finding; malformed or unsafe input should fail validation. These are different outcomes.
- Determinism needs a precise definition. Specify canonical serialization and ordering, stable input hashes, and which environment-dependent fields may vary. `produced_at` cannot be treated as deterministic output unless it is excluded from the deterministic content hash.

### Evidence, confidence, and trust

- The report should define evidence pointers as stable references into versioned outputs—for example, an output hash plus a structured field path—not merely a line number that can shift.
- The Entry 6 policy requires every `PROJECTED` and `SPECULATIVE` claim to name a confirming probe. The schema currently explicitly requires one only for `SPECULATIVE`. Align the two.
- Confidence should be attached at the level of individual claims or clearly scoped verdicts. A service can have verified facts alongside speculative conclusions; one confidence label for the entire service may obscure that distinction.
- Define `TRUSTED` bridge status separately from coverage confidence. A trusted conversion mechanism does not automatically prove that every converted output is correct for a particular estate.
- Make it explicit that evidence from static analysis cannot support claims about runtime behavior unless the report identifies the actual runtime evidence.

### Security and authority

- The redaction contract should acknowledge that secret-pattern detection cannot guarantee that every secret has been found. Define what happens when redaction is uncertain, including whether the affected input is excluded from model context.
- Specify that repository excerpts sent to an LLM are bounded, redacted, audited, and governed by the approved data-handling policy. Sniffer JSON is the default input; raw excerpts require an explicit exception.
- Remediation commands should be recommendations, not executable payloads. Require safe parameter handling and operator review, and never let the agent's output become a shell command automatically.
- Clarify that isolated scratch writes are permitted analysis mechanics, whereas platform-state mutation and customer-visible side effects are prohibited. This makes the negative-scope rule precise without contradicting the sniffers' need to produce files.

## 3. Status register: the biggest governance improvement still needed

The status vocabulary is good, but the current matrix sometimes treats an accepted principle as if its implementation details were also accepted.

For example:

- AG-01: The read-only advisor principle can be accepted. The dedicated principal, exact grant scopes, and enforcement details still require the appropriate platform and Security approvals.
- AG-02: Link-not-embed can be accepted as a design constraint. The precise linkage fields, content-hash definition, generation semantics, and consuming API contract remain proposed until the relevant owner approves them.
- AG-05: The strategy vocabulary can be proposed for review independently of the database schema, seed data, registry ownership, and trust-promotion procedure.

I recommend splitting these into separate rows rather than weakening the status definitions.

There are three more corrections to make:

1. Replace every generic `Owner` placeholder with an accountable role, such as Phase 1B design owner, Phase 0R implementation-plan owner, or platform registry owner. A person’s name is unnecessary if the role is unambiguous.
2. Add traceability rows for decisions that appear in §11.2 but not §10, including cross-tenant correction reuse and remediation auto-apply. Otherwise, §13's completeness condition is not met.
3. Keep `REJECTED` for proposals or behaviors explicitly ruled out. For permanent negative constraints, state the prohibited behavior clearly so that a reader cannot mistake the status for rejection of the safety requirement itself.

## 4. The decision register needs one important consistency pass

The current open-question rollup sometimes reopens decisions that the normative sections already make.

The clearest example is question 1:

> Report consumer: operator doc only, or also stored artifact for future gating?

Section 5 already mandates a linked, stored report. Section 11 separately defers activation gating. Those are two different decisions.

I would split them as follows:

- Report persistence and linkage: proposed contract, approval needed before Phase 1B finalizes its schemas.
- Activation gating: deferred until Phase 3 planning, with advisory-only behavior as the provisional default.

Likewise, make the `PROJECTED → VERIFIED` promotion rule explicit: confirming probe evidence is required; human acknowledgment is recorded separately if policy requires it. Replace “tribunal: first promotion” with a concrete review trigger and accountable owner.

For every deferred item, record the event that reopens it, the decision owner, and any required sign-off. A trigger such as “build time” should mean a specific milestone—for example, before the first model-backed implementation is approved.

## 5. Review of the proposed work plan

The six work items are directionally right, but I would change their order and acceptance criteria slightly.

1. D-1 — Refine the field-level contracts.
   Keep formal JSON Schema deferred, but make the nested shapes of verdicts, evidence pointers, bridges, enhancement items, and remediation recommendations explicit. Add the input-manifest and hash semantics. Resolve the census and determinism issues above.
2. D-2 — Define report linkage with the Phase 1B owner.
   Produce the exact linkage proposal and its retry/versioning semantics. Get the consuming phase's approval before calling it locked. Do not make the agent design the unilateral authority for a platform-owned intake contract.
3. D-3 — Establish the registry contract before seeding entries.
   Finalize the proposed strategy and trust-state vocabulary, then derive initial rows from the actual capability catalog and verified platform support. Each row needs provenance and an accountable owner. Do not populate hypothetical capabilities or promote bridges merely to make the registry look complete.
4. D-4 — Propose grant scopes and the activity interface.
   Submit the required read-only scope and workflow-interface requirements to the existing phase owners. Treat them as integration inputs, not permission to modify the Phase 0R scope. The activity interface should specify input references, idempotency, failure behavior, and report filing authority.
5. D-5 — Complete the status and approval matrix.
   Split principle-level acceptance from implementation-level decisions, replace placeholder owners, and ensure every deferred decision appears in the traceability matrix with a trigger and sign-off.
6. D-6 — Revise the Phase C consumption rule.
   Do not add the proposed rule as written. Replace it with a requirement that Phase C consumes approved, platform-owned disposition types and an explicitly approved coverage-linkage contract. Neither side silently redefines the other's contract.

One sequencing note: D-1 and D-2 can progress in parallel, but neither the schema refinement nor the database design work should delay the actual-role PostgreSQL proof. That remains an independent gate.

## 6. What I would consider sufficient for design acceptance

Before approving the document as the review-cycle output, I would require these five conditions:

- The authority boundary is consistent: the agent reasons, the workflow files artifacts, and separately authorized components perform any permitted side effects.
- The Coverage Report has a complete linkage, provenance, hashing, and retry contract, with ownership approved by its consuming phase.
- The sniffer contracts distinguish unsupported inputs, incomplete evidence, and actual execution failures without silently losing information.
- The status matrix distinguishes accepted principles from proposed implementation contracts, and every decision has an accountable owner and appropriate sign-off.
- The Phase 0R checklist is explicitly non-expanding, and any proposed database changes are confirmed against the existing implementation plan rather than assumed.

Bottom line: Keep the document's overall architecture. Do not start a redesign. Resolve the four contract issues in §1, correct the status and decision-register inconsistencies, and then return it for review. The next milestone is a coherent, reviewable contract—not formal JSON Schema, working sniffers, or new migration work.
