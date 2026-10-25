The strongest approach is to make this a two-phase, evidence-driven engineering assignment for Luna GPT:

1. Research and document in-toto attestation — establish what the open-source project actually provides, its security model, its APIs and data formats, its limitations, and which features are relevant to `investigation-agent-platform` (IAP).
2. Cross-reference the research against the actual IAP codebase — map attestation capabilities to existing packages, data models, workflows, and tests, then produce an architecture decision on whether IAP should integrate in-toto, implement selected logic internally, or use a hybrid approach.

The critical requirement is to prevent the coding agent from jumping directly to implementation. It must distinguish what in-toto already provides from what IAP must build, and support every integration recommendation with repository evidence.

Below is an implementation-grade Markdown prompt you can give to Luna GPT. It defines the research deliverable, code-review deliverable, architectural alternatives, security requirements, and acceptance criteria.

# Luna GPT Engineering Prompt: Research in-toto Attestation and Design Its Integration into Investigation-Agent-Platform

## 1. Role and Engineering Standard

Act as a **Principal Engineer, Software Supply Chain Security Architect, Attestation/Provenance Specialist, and Adversarial Code Reviewer**.

Your assignment is to research the open-source in-toto attestation ecosystem, document its verified capabilities, inspect the actual `investigation-agent-platform` (IAP) codebase, and produce an evidence-backed architecture design determining whether IAP should:

1. Integrate an existing in-toto library or framework.
2. Implement selected attestation capabilities natively.
3. Adopt a hybrid architecture, using in-toto standards and libraries while implementing IAP-specific orchestration and policies.
4. Defer adoption or reject in-toto where its abstractions do not fit the problem.

This is a research, architecture, and design assignment. **Do not implement production code or modify existing application behavior unless separately authorized.**

The output must be rigorous enough for a coding agent to implement the approved design in a subsequent engineering task.

## 2. Project Context

The target system is `investigation-agent-platform` (IAP), an agent-driven application-issue investigation and triage platform.

IAP investigates application failures and operational problems using evidence from sources such as:

- Application logs and runtime telemetry.
- Distributed traces and correlated events.
- Application and infrastructure configuration.
- Database records and query results.
- Repository code and application behavior.
- Existing investigations, hypotheses, findings, timelines, and conclusions.
- Agent-generated analysis, tool execution results, and verification outcomes.

The platform needs a trustworthy way to establish and verify the relationship between evidence, agent actions, derived claims, verification results, and investigation conclusions.

Examples of questions the attestation architecture should help answer:

- Which evidence was used to reach a conclusion?
- Where did the evidence originate?
- Was the evidence altered after collection?
- Which agent, service, tool, or principal produced a claim or result?
- Which procedure or policy was used?
- Which claims were directly observed, inferred, or independently verified?
- Can another investigator reproduce or audit the reasoning?
- Was the conclusion based on incomplete, stale, conflicting, or untrusted evidence?
- Can a verifier detect unauthorized modification, substitution, or replay?
- Can the system establish an auditable chain from original evidence to derived findings?

These are target capabilities to evaluate, not assertions that the existing implementation already supports them.

### Important distinction

Do not assume that software supply-chain attestation and application-incident investigation attestation are interchangeable.

in-toto originated in the software supply-chain security domain. Determine which of its concepts, formats, libraries, and verification mechanisms can be reused directly for IAP, which require an adapter or additional policy, and which do not fit the investigation domain.

Do not force IAP's domain model into a supply-chain model merely because the terminology appears similar.

## 3. Mandatory Operating Rules

### 3.1 Inspect before designing

Start by inspecting the repository, its documentation, dependency manifests, architecture, tests, and existing contracts.

Discover the actual:

- Repository root and package layout.
- Programming language and runtime versions.
- Dependency-management and build tooling.
- Application entry points and service boundaries.
- Domain models and persistence mechanisms.
- Evidence ingestion, retrieval, transformation, and storage paths.
- Agent orchestration and tool-execution workflows.
- Existing provenance, audit, hashing, signing, verification, or event-recording logic.
- Authentication, authorization, tenant isolation, and secrets management.
- Existing APIs, MCP interfaces, background jobs, and event-processing mechanisms.
- Testing, CI, packaging, deployment, and observability conventions.

Use the repository as the source of truth. If an expected component does not exist, explicitly report it rather than inventing its implementation.

### 3.2 Research authoritative sources

Research the upstream projects and specifications using their current documentation and source repositories.

At minimum, investigate:

- The official in-toto project and its architecture.
- The official in-toto attestation specifications.
- The applicable in-toto Python implementation, if relevant to the repository's technology stack.
- The Statement and predicate models used by in-toto attestations.
- DSSE (Dead Simple Signing Envelope), where applicable.
- SLSA provenance and other relevant predicate types.
- Related signing, identity, trust-root, verification, and key-management mechanisms.
- Relevant specifications for digest-based subjects, signatures, verification, and trust policies.

Start with these sources and verify the current canonical URLs and repository locations before relying on them:

- ~/workspace/repos/in-toto/attestation
- [https://in-toto.io/](https://in-toto.io/)
- [https://github.com/in-toto](https://github.com/in-toto)
- [https://github.com/in-toto/attestation](https://github.com/in-toto/attestation)
- [https://github.com/secure-systems-lab/dsse](https://github.com/secure-systems-lab/dsse)
- [https://slsa.dev/spec/](https://slsa.dev/spec/)

Do not assume that every repository, API, specification version, or feature listed above is current, maintained, compatible, or suitable. Verify the status of each relevant component.

For every material capability, record its authoritative source, version or specification revision, and verification status.

Prefer official specifications, upstream documentation, source code, release notes, and maintained test suites over secondary articles.

### 3.3 Separate facts from decisions

Label findings where appropriate as:

- **Verified upstream capability:** Confirmed by authoritative documentation, specifications, or source code.
- **Verified IAP capability:** Confirmed by actual repository implementation and tests.
- **Repository gap:** Required behavior is absent, incomplete, or insufficiently verified.
- **Design proposal:** A recommended future architecture, not existing behavior.
- **Unverified assumption:** A point requiring further investigation.

Do not claim a capability exists simply because a dependency is installed, a class is named appropriately, or a test contains a mock.

### 3.4 Preserve existing behavior and security boundaries

Do not introduce changes to existing IAP contracts during this assignment.

Do not:

- Modify production code.
- Change API or MCP contracts.
- Change authorization or tenant-isolation behavior.
- Introduce migrations.
- Add dependencies.
- Change the meaning or authority of existing investigation findings.
- Replace existing evidence records or audit mechanisms.
- Treat a successful signature verification as proof that a claim is factually true.
- Treat an agent-generated assertion as trusted solely because it has been signed.
- Invent test results, source references, or repository behavior.

Create only the explicitly requested Markdown deliverables, unless the repository's existing documentation conventions require an additional design artifact. Report any necessary deviation.

## 4. Phase One: Research the in-toto Attestation Ecosystem

Create the following file:

`docs/in-toto-attestation-feature-assessment.md`

This document is the canonical research artifact. The subsequent architecture decision must reference it rather than duplicate or reinterpret the research independently.

### 4.1 Executive summary

Explain:

- What in-toto is.
- What attestation means in its ecosystem.
- Which related projects and specifications provide complementary functionality.
- The security problems the ecosystem is designed to address.
- The main differences between software supply-chain attestations and investigation evidence.
- The initial applicability assessment for IAP.
- The principal limitations and unresolved questions.

Keep the summary understandable to a principal engineer who has not previously worked with in-toto.

### 4.2 Ecosystem and component inventory

Create a component inventory covering the relevant upstream projects, specifications, libraries, and supporting technologies.

Include these fields:

| Field               | Required content                                       |
| ------------------- | ------------------------------------------------------ |
| Component           | Project, library, or specification name                |
| Purpose             | Problem it addresses                                   |
| Current status      | Verified maintenance and release status                |
| Version             | Relevant release or specification revision             |
| Language/runtime    | Supported languages and runtime constraints            |
| Core abstractions   | Important objects, records, APIs, or concepts          |
| Security guarantees | What is actually guaranteed                            |
| Limitations         | What it does not guarantee                             |
| IAP applicability   | Direct, adapter required, complementary, or unsuitable |
| Evidence            | Canonical source URLs and supporting references        |

Investigate the relationship between the in-toto framework, the attestation data model, signing envelopes, predicate specifications, and verification tooling.

Avoid treating all projects under a related organization as one interchangeable library.

### 4.3 Detailed feature catalog

Document the features that are relevant to IAP, including the following categories.

#### A. Attestation data model

Investigate:

- Statement structure and schema.
- Subjects and their identifiers.
- Content digests and digest algorithms.
- Predicate types and predicate payloads.
- Predicate type identification and schema evolution.
- Issuer, producer, signer, and subject semantics.
- Serialization and canonicalization requirements.
- Extensibility and interoperability.
- Validation behavior for malformed or unknown data.

Explain which parts are standardized and which parts must be defined by a domain-specific predicate.

#### B. Evidence integrity and provenance

Investigate:

- Content-addressed identification.
- Cryptographic digest generation and verification.
- Evidence-to-attestation binding.
- Provenance and producer attribution.
- Evidence transformation and derived artifacts.
- Multiple subjects and related artifacts.
- Relationships between source evidence, intermediate artifacts, and conclusions.
- Evidence replacement, mutation, deletion, and retention.
- The difference between data integrity and provenance authenticity.

Explain what can be established cryptographically and what requires trusted collection procedures or external evidence.

#### C. Signing, identity, and trust

Investigate:

- Signature creation and verification.
- Signing envelopes and their security properties.
- Key-based and identity-based trust models.
- Certificate or identity verification, where supported.
- Trust roots and trust policies.
- Key rotation, revocation, and expiration.
- Verification of the intended signer and permitted identity.
- Separation of the attestation producer from the party that verifies it.
- Hardware-backed or external signing integrations, where relevant.

Explain what IAP must supply or configure in addition to the upstream library.

Do not assume that every signing method is built into in-toto itself.

#### D. Verification and policy enforcement

Investigate:

- Structural and schema validation.
- Subject and digest matching.
- Signature verification.
- Signer identity constraints.
- Predicate-specific validation.
- Trust-policy enforcement.
- Verification failure reporting.
- Unknown predicate handling.
- Compatibility and schema evolution.
- Whether verification results are reproducible and independently auditable.

Distinguish cryptographic verification, semantic validation, authorization, and factual correctness.

#### E. Lifecycle and workflow

Investigate the applicable concepts for:

- Creating attestations.
- Collecting inputs.
- Recording execution or transformation metadata.
- Producing signed outputs.
- Verifying outputs and relationships.
- Maintaining an audit trail.
- Consuming attestations from another service.
- Offline verification and archival use.

Where traditional in-toto layouts, link metadata, or supply-chain workflows are relevant, explain their original purpose before proposing any adaptation to incident investigations.

#### F. Operational and enterprise concerns

Assess:

- Python/runtime compatibility with the actual IAP repository.
- Dependency footprint and transitive dependencies.
- API stability and maintenance.
- Performance and large evidence payloads.
- Storage and indexing requirements.
- Distributed operation.
- Multi-tenant operation.
- Offline and restricted-network operation.
- Observability and diagnostics.
- Backward compatibility.
- Data privacy and sensitive evidence.
- Licensing and organizational adoption constraints.
- Supply-chain security of the dependency itself.

#### G. Security limitations and threat model

Explicitly assess whether the ecosystem, by itself, prevents or detects:

- Forged or unauthorized attestations.
- Modification of signed content.
- Substitution of one evidence artifact for another.
- Replay of old attestations.
- Signing by a compromised but otherwise authorized identity.
- False claims signed by a legitimate producer.
- Fabricated source evidence.
- Incomplete evidence collection.
- Compromised agent tools or collectors.
- Malicious or incorrect predicate generation.
- Cross-tenant evidence substitution.
- Unauthorized disclosure of evidence.
- Deletion or rollback of previously recorded records.
- Compromised verification policies or trust roots.
- Conflicting attestations and disputed conclusions.

For each threat, document the relevant mechanism, required assumptions, residual risk, and any additional control IAP would need.

### 4.4 Capability-to-IAP mapping

Create a detailed feature matrix.

Use the following columns:

| Column                      | Meaning                                                   |
| --------------------------- | --------------------------------------------------------- |
| Capability ID               | Stable identifier such as `ATT-001`                       |
| Capability                  | Attestation feature                                       |
| Upstream support            | Exact in-toto or related specification/library capability |
| Upstream evidence           | Source URL and supporting location                        |
| IAP requirement             | Why the capability matters to an investigation            |
| Existing IAP implementation | Verified package, file, symbol, or contract               |
| Gap                         | Missing, partial, incompatible, or unknown                |
| Adaptation                  | Required IAP-specific behavior                            |
| Recommendation              | Integrate, build, hybrid, defer, or reject                |
| Verification                | Test or evidence required to establish correctness        |

Include at least:

- Evidence integrity.
- Evidence provenance.
- Agent/tool attribution.
- Claim and conclusion attribution.
- Evidence-to-finding linkage.
- Transformation provenance.
- Signature verification.
- Trust-policy enforcement.
- Tenant isolation.
- Temporal validity and replay controls.
- Immutable or tamper-evident audit history.
- Independent verification.
- Attestation persistence and retrieval.
- Schema evolution.
- Error handling and verification status.
- Privacy, redaction, and access control.

The matrix must be sufficiently detailed to serve as the traceability baseline for the second phase.

### 4.5 Domain-specific attestation examples

Provide conceptual examples for these IAP scenarios:

1. A collector retrieves an application log record.
2. A trace investigation produces a correlated set of spans.
3. An agent generates a hypothesis based on multiple evidence artifacts.
4. A verifier evaluates whether the evidence supports the hypothesis.
5. A finding is accepted, rejected, or marked inconclusive.
6. A final investigation conclusion references its supporting findings.
7. An investigation is reopened because new evidence conflicts with the original conclusion.

For each scenario, specify:

- Subject.
- Producer or asserted actor.
- Evidence references and digests.
- Predicate fields.
- Relevant timestamps and temporal semantics.
- Verification obligations.
- Trust requirements.
- What the attestation establishes.
- What it does not establish.

Include illustrative JSON only where it clarifies the design. Clearly label proposed IAP-specific schemas as examples, not official in-toto schemas.

Use synthetic data. Do not include real credentials, secrets, customer data, or fabricated source claims.

### 4.6 Findings and research conclusion

End the document with:

- A prioritized list of upstream capabilities to reuse.
- A list of capabilities IAP must implement itself.
- A list of unsuitable or unnecessary upstream concepts.
- A dependency and compatibility assessment.
- Unresolved questions.
- A preliminary integration recommendation.
- Explicit confidence and evidence supporting that recommendation.

Do not make the final architectural decision until the actual IAP codebase has been examined.

