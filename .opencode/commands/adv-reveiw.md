---
description: Perform an adversarial architecture and detailed-design review on uncommitted changes
agent: plan
---
Review the following uncommitted git changes for `investigation-agent-platform` (IAP) using the adversarial review framework below:

!`git diff`

---

# Adversarial Architecture and Detailed-Design Review Prompt for IAP
Muse is the chief architect of IAP and is the lead engineer for IAP under the guidance of the Owner. For all practical purposes, Balaji is responsible for the design and decisions.
Balaji is performing the role of senior engineer for IAP and thereby the review will assume Balaji as the responsible person for the repo.

## Independent Red-Team Review of Architecture Update and Implementation Design

**Review target:** `investigation-agent-platform` (IAP)\
**Primary baseline:** `IAP Architecture Decision Synthesis — Joern, CodeQL, ClaimLens, Z3, LangGraph (v6 — enforceable kernel semantics)`\
**Design author under review:** Balaji \
**Reviewer role:** Independent Principal/Distinguished Engineer, adversarial software architect, proof-system reviewer, and implementation-readiness assessor\
**Review objective:** Determine whether Balaji's proposed architecture and detailed implementation design are correct, complete, consistent with v6, grounded in the actual repository, and safe to implement.

---

# 1. Mission: independently challenge the design

You are the independent reviewer. You did not author Balaji's design, and you must not behave as its advocate.

Your responsibility is to determine whether Balaji's work can withstand a rigorous, evidence-driven architecture review.

You must independently inspect:

1. The complete original v6 architecture synthesis.
2. Balaji's complete architecture update and detailed design.
3. Balaji's requirements traceability matrix.
4. Balaji's repository findings and current-state assessment.
5. Balaji's proposed schemas, contracts, algorithms, diagrams, and state transitions.
6. Balaji's implementation phases, work packages, dependencies, entry/exit criteria, and release gates.
7. The actual IAP repository, including implementation, tests, configuration, persistence, and every route capable of producing or publishing a verification result.
8. Referenced source reports and prior review documents, if available.

**Do not accept Balaji's statements as facts merely because they are detailed, confident, or accompanied by diagrams.**

Treat the original v6 document as the primary source of architectural intent and the actual repository as the source of truth for current implementation behavior.

Your job is to find what Balaji missed, misunderstood, weakened, invented, oversimplified, contradicted, or failed to make implementable.

This is an architecture and design review, not a code-generation task. Do not implement the proposed changes. Do not silently rewrite Balaji's design. Produce a rigorous review and a concrete remediation specification that can be handed back to Balaji or another coding engine.

# 2. Mandatory review principles

## 2.1 Independent reconstruction
Before evaluating Balaji's conclusions, independently reconstruct the important requirements from v6.

## 2.2 Evidence over assertion
Every material finding must distinguish between verified fact, derived conclusion, design risk, unverified assumption, and architectural recommendation. Cite exact source sections and actual repository paths.

## 2.3 Adversarial interpretation
Challenge contracts for weak interpretations, malformed inputs, concurrency issues, bypass scenarios, and failure modes.

## 2.4 Preserve established guarantees
Do not recommend simplification that weakens v6's proof semantics or existing safety gates.

# 3. Review the complete source, not a curated summary
Establish a source inventory and inspect the complete v6 document, including schemas, test cases (K1–K16, A–T), stages A–E, and gates G1–G8. Provide the source coverage matrix.

# 4. Repository-grounded design review
Independently inspect the actual repository implementation, current claims, obligations, evidence ingestion, rules, persistence, concurrency, and hard gates.

# 5. High-risk adversarial review areas
Focus deep review on:
5.1 Proposition identity and canonicalization
5.2 Evidence admissibility and provenance
5.3 Proof certificates and kernel soundness
5.4 Coverage and absence proofs
5.5 Composition and conflict semantics
5.6 Invalidation and reconciliation
5.7 Atomic publication and concurrency
5.8 Lifecycle, legacy paths, and authority bypass
5.9 Planner and workflow semantics
5.10 External witnesses and optional tools
5.11 Schema evolution and operational integrity
5.12 Performance, scale, and observability

# 6. Mandatory acceptance-suite audit
Audit Kernel suite K1–K16, A–T regression suite, three-outcome reporting, and feature-flag compatibility.

# 7. Audit Balaji's phase plan as an engineering dependency graph
Check inputs/outputs, entry/exit criteria, real prerequisites, sequencing, and conflicting edits.

# 8. Required finding format
Use the structured finding template (`ARCH-CRIT-001`, severity, confidence, category, source requirement, repo evidence, defect, failure scenario, impact, required correction, regression protection, blocking status, resolution criteria).

# 9. Severity definitions and release-blocking policy
Classify into Critical, High, Medium, Low, Informational with strict release-blocking rules.

# 10. Mandatory independent consistency checks
Perform source-to-design consistency, cross-document consistency, implementation feasibility, test sensitivity, and completeness checks.

# 11. Required outputs
Produce the full review package in the tmp directory under investigation-agent-platform:

- Deliverable A — Independent executive verdict
- Deliverable B — Source fidelity and traceability report
- Deliverable C — Prioritized defect register
- Deliverable D — Corrected target architecture recommendations
- Deliverable E — Corrected phase and dependency plan
- Deliverable F — Acceptance-suite audit table
- Deliverable G — Remediation prompt for Balaji
