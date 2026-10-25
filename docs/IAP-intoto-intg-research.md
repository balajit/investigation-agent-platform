1. Role and Engineering Standard

Act as a Principal Engineer, investigation-agent-platform(iap), Attestation/Provenance Specialist, and Adversarial Code Reviewer.


## 5. Phase Two: Adversarial Review of the IAP Codebase

After completing the research document, inspect IAP and cross-reference every applicable requirement in `docs/in-toto-attestation-feature-assessment.md`.

Create:

`docs/iap-attestation-architecture-decision.md`

### 5.1 Repository inventory

Identify the real packages, files, classes, functions, interfaces, schemas, persistence models, and tests involved in the investigation lifecycle.

Trace the actual flow from:

1. Evidence ingestion.
2. Evidence normalization and storage.
3. Evidence retrieval and correlation.
4. Agent/tool execution.
5. Hypothesis generation.
6. Verification and validation.
7. Finding and conclusion creation.
8. Audit and historical retrieval.

Also examine existing MCP interfaces, application services, evidence ports, and backend adapters where present.

Determine whether attestation belongs in the domain model, an application service, an infrastructure adapter, a dedicated bounded context, or a combination of these.

Do not assume the current architecture matches this description. Verify it.

### 5.2 Adversarial gap analysis

Review the code for gaps involving:

- Missing or inconsistent evidence identifiers.
- Missing content digests or incorrect digest lifecycle.
- Provenance lost during normalization or transformation.
- Evidence references that can silently change meaning.
- Agent claims not bound to the evidence they cite.
- Conclusions without explicit supporting or contradicting evidence.
- Verification results that cannot be independently reconstructed.
- Trust decisions mixed with business logic.
- Untrusted metadata accepted as authoritative.
- Missing tenant boundaries.
- Authorization bypasses in evidence or attestation retrieval.
- Unbounded payloads or unsafe serialization.
- Insecure signature or key handling.
- Replay, rollback, and duplicate-submission risks.
- Incomplete audit history.
- Race conditions between evidence mutation and attestation creation.
- Inconsistent transaction or asynchronous processing semantics.
- Missing idempotency and retry handling.
- Error handling that incorrectly reports unverified data as verified.
- Tests that rely on mocks without testing cryptographic or semantic verification.
- APIs or UI surfaces that overstate the assurance provided by an attestation.

Assess whether each issue is confirmed, partially supported, or requires additional evidence.

### 5.3 Finding format

Give every finding a stable identifier and use this structure:

| Field | Required content |
|---|---|
| Finding ID | For example, `IAP-ATT-001` |
| Severity | Critical, High, Medium, Low, or Informational |
| Package | Actual repository package |
| Source location | File, symbol, and relevant line range when available |
| Existing behavior | What the code actually does |
| Expected behavior | Requirement from the research artifact |
| Evidence | Code, tests, contracts, or documented behavior |
| Failure scenario | Concrete adversarial or operational example |
| Impact | Security, correctness, auditability, reliability, or maintainability |
| Root cause | Architectural or implementation defect |
| Remediation | Concrete change required |
| Test strategy | Tests needed to establish the fix |
| Dependency | Whether in-toto integration is required |
| Confidence | Confirmed, probable, or unresolved |

Prioritize security and correctness defects before optimization.

If no issue is found in a category, do not manufacture a finding. Record the coverage and evidence reviewed.

## 6. Architecture Decision: Build, Integrate, or Hybrid

The architecture decision must explicitly compare four alternatives.

### Option A: Integrate the upstream ecosystem

Use existing libraries, data models, signing formats, and verification facilities wherever they genuinely meet IAP's requirements.

Identify the precise packages and APIs, their versions, supported runtime, integration points, limitations, and additional implementation required.

### Option B: Build native IAP attestation logic

Define the minimum internal capabilities required to meet the investigation use case without adopting the upstream framework.

Explain the cost of owning custom schemas, signing, verification, compatibility, maintenance, and interoperability.

Do not recommend custom cryptography. Use established cryptographic libraries and standards if a native implementation is justified.

### Option C: Hybrid integration

Evaluate whether IAP should reuse upstream attestation formats and verification libraries while implementing its own domain-specific predicates, policies, orchestration, persistence, and evidence lifecycle.

Explicitly identify the boundary between upstream code and IAP-owned logic.

### Option D: Defer or reject adoption

Determine whether any part of the framework introduces complexity or a security model mismatch that outweighs its benefits.

Identify which requirements would still need to be met if adoption is deferred.

### 6.1 Decision matrix

Score the alternatives using a documented scale, for example 1–5, and justify the score for each dimension.

| Dimension | Required evaluation |
|---|---|
| Security assurance | Correctness of integrity, signing, identity, and verification |
| Domain fit | Suitability for incident investigation evidence and conclusions |
| Interoperability | Compatibility with established attestation formats |
| Implementation effort | Work needed to reach the required assurance level |
| Maintenance burden | Long-term ownership and upgrade costs |
| Flexibility | Ability to represent investigation-specific semantics |
| Performance | Creation, storage, retrieval, and verification overhead |
| Extensibility | Schema evolution and new predicate types |
| Operational complexity | Key management, trust configuration, and deployment |
| Compatibility | Fit with IAP's runtime and existing architecture |
| Testability | Ability to test security properties independently |
| Auditability | Reproducibility and clarity of verification outcomes |

Document weights if dimensions have different importance. Do not use an unexplained aggregate score to make the decision.

### 6.2 Target architecture

Provide a component-level design that identifies the responsibilities and dependencies of:

- Evidence and artifact identity.
- Attestation creation.
- Predicate/schema definitions.
- Cryptographic signing or envelope creation.
- Attestation persistence and retrieval.
- Trust configuration.
- Attestation verification.
- Investigation-specific policy evaluation.
- Claim, finding, and conclusion linkage.
- Audit and historical reconstruction.
- API/MCP exposure, if justified by the current architecture.

For each component, identify:

- Responsibility.
- Public interface.
- Inputs and outputs.
- Security invariants.
- Dependencies.
- Failure behavior.
- Test boundary.

Include a Mermaid component diagram or sequence diagram if supported by the repository's documentation conventions.

The architecture should preserve existing service boundaries and avoid turning an MCP server or external API adapter into the attestation authority.

### 6.3 Define the attestation lifecycle

Specify a proposed lifecycle with explicit state transitions, such as:

`DRAFT → CREATED → SIGNED → VERIFIED`

Treat this as an illustrative starting point, not a mandated state machine. Add rejection, expiration, revocation, supersession, or invalidation states only where the actual requirements and trust model justify them.

Distinguish:

- Attestation creation.
- Signature verification.
- Trust-policy acceptance.
- Semantic validation.
- Evidence sufficiency assessment.
- Investigation conclusion acceptance.

A cryptographically valid attestation must not automatically cause an investigation conclusion to be accepted.

Specify what happens when evidence is missing, stale, modified, inaccessible, or contradictory.

## 7. Required IAP-Specific Design Decisions

The architecture document must resolve or explicitly defer the following questions.

### 7.1 What exactly is being attested?

Distinguish among:

- Original evidence.
- Evidence collection events.
- Evidence transformations.
- Agent/tool execution events.
- Agent-generated claims.
- Verification results.
- Findings.
- Final conclusions.

Define whether these are separate attestations, related predicates, or references in a larger attestation graph.

Avoid embedding entire log files, traces, database extracts, or secrets inside attestations. Evaluate references and cryptographic digests instead.

### 7.2 Who is trusted to make each assertion?

Define the distinction among:

- Evidence source.
- Collector.
- Agent.
- Tool.
- Attestation signer.
- Verification service.
- Policy authority.
- Human reviewer, if applicable.

Specify which actors can create attestations, which identities can sign them, and which principals can verify or approve them.

An agent must not be allowed to manufacture trusted provenance merely by signing its own unsupported assertion.

### 7.3 How is the evidence chain represented?

Define how a verifier can trace a conclusion through findings and claims to the underlying evidence.

Consider:

- Stable artifact identifiers.
- Cryptographic digests.
- Parent or dependency references.
- Transformation records.
- Timestamps and source-time semantics.
- Conflicting and superseding attestations.
- Partial or missing evidence.
- Independent verification.
- Long-term archival and reconstruction.

Explain whether the design uses a graph, append-only records, relational references, or another structure, and why.

### 7.4 How are trust and freshness established?

Specify the controls required for:

- Identity verification.
- Authorized signer policies.
- Trust-root management.
- Key rotation and revocation.
- Replay resistance.
- Clock skew and timestamp interpretation.
- Evidence expiration or staleness.
- Duplicate submissions.
- Cross-tenant isolation.
- Verification-policy changes over time.

A digest alone does not establish the origin or freshness of evidence. A timestamp alone does not prove when an event actually occurred.

### 7.5 What must remain immutable?

Identify which data must be immutable or tamper-evident, which metadata can change, and how corrections are represented.

Prefer explicit supersession or correction records over silently rewriting the historical basis of an investigation.

Do not assume that a signed record alone guarantees durable storage, completeness, or protection against deletion.

### 7.6 What is exposed through the application and MCP?

If the existing IAP interfaces justify exposing attestation functionality, propose the minimum necessary operations.

Potential operations to evaluate include:

- Create an attestation for a supported artifact or event.
- Retrieve an attestation by identifier.
- Verify an attestation against an explicit trust policy.
- Retrieve verification results.
- Trace the attestations supporting a finding or conclusion.

Do not automatically expose all these operations as MCP tools.

Determine which operations belong in application services, which are read-only, which require authorization, and which should remain internal.

Specify JSON schemas and error contracts only when the actual architecture supports and requires them. Follow existing MCP and API conventions rather than inventing a parallel contract.

## 8. Proposed Package and File Layout

Recommend a concrete package and file layout based on the actual repository.

Use this conceptual separation as a starting point, adapting names and boundaries to existing conventions:

```text
src/
  <existing_iap_package>/
    attestation/
      models.py
      schemas.py
      ports.py
      service.py
      verification.py
      policy.py
      provenance.py
      errors.py
    infrastructure/
      attestation/
        <upstream_adapter>.py
        <persistence_adapter>.py
        <signing_adapter>.py
        <trust_adapter>.py

tests/
  unit/
    attestation/
  integration/
    attestation/
  security/
    attestation/
  contract/
    attestation/

docs/
  in-toto-attestation-feature-assessment.md
  iap-attestation-architecture-decision.md
```

This is illustrative, not an instruction to create empty directories or force an artificial package structure.

For every proposed file, explain:

- Its responsibility.
- The interfaces or classes it contains.
- Its dependencies.
- The invariants it enforces.
- Its unit and integration tests.
- Whether it is new, an adaptation of an existing module, or unnecessary.

Avoid duplicate models and unnecessary abstraction layers.

## 9. Test and Verification Strategy

Define a test plan that verifies security properties rather than just successful execution.

Include:

### Unit tests

- Valid and invalid statement structures.
- Digest generation and mismatch detection.
- Correct subject-to-evidence binding.
- Valid and invalid signatures.
- Unauthorized signer rejection.
- Predicate-specific semantic validation.
- Unsupported schema and predicate behavior.
- Error classification.

### Integration tests

- Attestation creation and persistence.
- Retrieval and independent verification.
- Evidence-to-finding-to-conclusion linkage.
- Verification after application restart.
- Trust configuration and key rotation.
- Tenant-scoped retrieval and authorization.
- Failure and retry handling.
- Concurrent evidence updates and attestation creation.

### Adversarial tests

- Evidence substitution.
- Tampered payloads.
- Replay of previously valid attestations.
- Signing with an untrusted key.
- Valid signatures from unauthorized identities.
- Malformed or oversized payloads.
- Missing source evidence.
- Conflicting attestations.
- Stale evidence.
- Compromised producer assertions.
- Unauthorized cross-tenant access.
- Deleted or unavailable historical artifacts.
- Attempts to bypass verification through API or MCP entry points.

### Compatibility tests

- Existing IAP records and workflows.
- Existing API/MCP contracts.
- Schema-version evolution.
- Backward compatibility where required.
- Verification of archived records.

For every test category, specify the expected result and the security invariant being protected.

Do not claim that tests pass unless they were actually executed. In this assignment, report proposed tests separately from tests already present in the repository.

## 10. Implementation Roadmap

Provide a phased implementation plan without implementing the changes.

For each phase, include:

- Objectives.
- Repository packages and files affected.
- Dependencies and prerequisites.
- Design decisions that must be finalized.
- Security considerations.
- Tests and verification evidence.
- Acceptance criteria.
- Rollback or compatibility considerations.

A reasonable sequence to evaluate is:

1. Finalize the attestation threat model and domain contract.
2. Define versioned predicates and stable evidence references.
3. Introduce a narrow attestation service boundary.
4. Integrate the selected upstream libraries or native adapters.
5. Implement trust configuration and verification policy.
6. Integrate attestation with evidence, claims, findings, and conclusions.
7. Add persistence, auditability, and independent verification.
8. Add adversarial tests and operational observability.
9. Consider API/MCP exposure only after the underlying service contracts are stable.

Adjust this sequence based on dependencies discovered in the repository.

Do not introduce migrations, production callers, or behavior-changing integrations as part of this research and design assignment.

## 11. Required Traceability

The two documents must be linked through stable identifiers.

The feature-assessment document must assign IDs such as:

- `ATT-001` — Evidence integrity.
- `ATT-002` — Producer provenance.
- `ATT-003` — Signature verification.
- `ATT-004` — Trust-policy enforcement.
- `ATT-005` — Evidence-to-conclusion linkage.
- `ATT-006` — Replay and freshness controls.
- `ATT-007` — Tenant isolation.
- `ATT-008` — Independent verification.

Extend this inventory as necessary.

The architecture decision must reference these identifiers in its gap analysis, design decisions, and implementation roadmap.

Use a traceability table:

| Requirement ID | Upstream capability | IAP implementation/gap | Decision | Target component | Verification test |
|---|---|---|---|---|---|

Every high- or critical-severity finding must map to a remediation or an explicitly justified deferral.

Every recommended component must map to one or more requirements.

Every security requirement must map to a concrete verification strategy.

## 12. Acceptance Criteria

The assignment is complete only when all applicable criteria below are met.

### Research deliverable

- [ ] `docs/in-toto-attestation-feature-assessment.md` exists.
- [ ] Upstream projects, APIs, specifications, versions, and maintenance status are verified.
- [ ] Features are described accurately, with authoritative references.
- [ ] Security guarantees are distinguished from assumptions and limitations.
- [ ] IAP-specific applicability is assessed.
- [ ] The capability matrix has stable requirement IDs.
- [ ] Domain-specific attestation examples are included.
- [ ] The research conclusion identifies unresolved questions and its evidence basis.

### Architecture deliverable

- [ ] `docs/iap-attestation-architecture-decision.md` exists.
- [ ] The actual IAP repository has been inspected.
- [ ] Relevant packages, files, symbols, and tests are referenced.
- [ ] The review identifies real implementation gaps without inventing defects.
- [ ] Integration, native implementation, hybrid, and defer/reject alternatives are compared.
- [ ] The recommendation is justified by security, domain fit, compatibility, and operational cost.
- [ ] The target architecture and component boundaries are specified.
- [ ] The attestation lifecycle and trust model are defined.
- [ ] Proposed package/file layout is concrete and repository-aware.
- [ ] The test plan includes adversarial security scenarios.
- [ ] The implementation roadmap contains verifiable acceptance criteria.
- [ ] Requirements, findings, components, and tests are traceable across both documents.

### Scope and quality

- [ ] No production code was modified.
- [ ] No dependencies were added.
- [ ] No migrations were created or executed.
- [ ] No API/MCP contracts or production behavior were changed.
- [ ] No unverified capability is presented as an established fact.
- [ ] No test execution or repository inspection is falsely claimed.
- [ ] Security limitations and unresolved assumptions are explicit.

If a criterion cannot be met, document the reason, its impact, and the evidence needed to close the gap.

## 13. Final Response Format

When finished, provide:

1. The two Markdown artifact paths.
2. A concise executive summary of the architectural recommendation.
3. The most important verified in-toto capabilities for IAP.
4. The most important gaps found in the current implementation.
5. The rationale for integrating, building, using a hybrid approach, or deferring adoption.
6. The highest-risk unresolved security questions.
7. A summary of repository files inspected.
8. A list of tests inspected or executed, clearly distinguishing the two.
9. Any acceptance criteria that remain unmet.

Use links or repository-relative paths to the generated documents where supported.

Do not merely describe what should be done. Complete the research and design documents based on actual evidence.

## 14. Final Engineering Principle

The purpose of attestation in IAP is not simply to sign investigation records. It is to establish a verifiable, auditable relationship between evidence, its origin, transformations, agent actions, claims, verification results, and conclusions.

The architecture must preserve the distinction between:

- **Integrity:** the artifact matches the bytes or digest that were recorded.
- **Authenticity:** the artifact or assertion is attributable to an accepted identity under a defined trust policy.
- **Provenance:** the documented origin and transformation history of an artifact.
- **Authorization:** the identity was permitted to perform the action or make the assertion.
- **Validity:** the record conforms to its structural and semantic contract.
- **Evidentiary support:** the evidence actually supports the associated claim.
- **Conclusion correctness:** the investigation conclusion is justified by the evidence and applicable reasoning policy.

These properties are related but not interchangeable.

Recommend the smallest architecture that can establish the required properties with explicit trust assumptions, independently verifiable records, and testable security invariants. Reuse proven standards and libraries where appropriate, and implement IAP-specific semantics only where the upstream ecosystem does not provide them.
