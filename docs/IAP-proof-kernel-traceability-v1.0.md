# IAP Proof-Kernel Traceability and Current-State Gaps v1.0

> Companion to the design and contracts docs. IDs are stable across the package: `REQ-*` (requirements), `GAP-*` (review gaps), `GATE-*` (release gates), `T-*` (tests), `WP-*` (work packages, defined in the plan doc).

## REQ matrix (abridged normative set; full ledger in package review)

| ID | Source location | Requirement | Current implementation evidence | Target | Verify | Phase | Status |
|---|---|---|---|---|---|---|---|
| REQ-KERNEL-001 | v6 §0 central rule | No component turns uncertainty into proof by passing output onward | Partial: kind gates + `_resolve` drops + refuting withhold exist; no checker boundary | Checker + gate as sole authorities | K1–K16, A–T | 2 | Preserved |
| REQ-KERNEL-002 | v6 §0 four questions | Keep identity / relevance / sufficiency / composition separate | Partial: `identity_valid`/`claim_relevant`/`proof_sufficient` split in diagnostics; single `_resolve` path | Three predicates enforced in different code paths | Unit + K-suite | 1–2 | Preserved |
| REQ-KERNEL-003 | v6 §§0.2/0.8/0.14 | Three results + Layer A/B + snapshot consistency | Partial: `apply_verification_outcome` writes SUPPORTED; `ConclusionGate.evaluate` (`verification.py:3909-3981`) enforces 7 checks; no context IDs, no CAS on hypotheses | Four IDs + atomic commit | Race tests K8/K15 | 3 | Preserved |
| REQ-KERNEL-004 | v6 §0.3 | Versioned `PROOF_RULES` per kind, executable | Partial: `check_claim_kind` + `CATEGORY_PROOF_RULES` + `required_evidence_families` | Closed registry with digests | Rule goldens | 2 | Preserved |
| REQ-KERNEL-005 | v6 §0.5/0.10 | Normative premises + inference; closed registry; semantic validation | Partial: Java/Python resolvers with `CodeFactResolutionV2` + limitation codes | Registry + checker rejects label forgery | K1/K9 | 2 | Preserved |
| REQ-KERNEL-006 | v6 §0.6/0.15 | Composition + `Deps(R_i)` closure | Missing: no dependency graph, no invalidation closure | Derivation graph + closure | K5/K6 + mutation | 3 | Preserved |
| REQ-KERNEL-007 | v6 §0.7/0.17 | Canonical proposition + quantifiers + four IDs | Partial: `ClaimKind`/`ClaimTarget` routing only | Full schema + fixtures | Identity fixtures K7 | 1–2 | Clarified |
| REQ-KERNEL-008 | v6 §0.4/0.12 | Reconciliation + route/proposition split | Partial: contradiction penalty + rival demotion; no typed refutation objects | Typed refutations + table | K5/K11 | 3 | Preserved |
| REQ-KERNEL-009 | v6 §0.9 | Certificates + independent checker; `P=Evaluate(C,E,R,M)` | Missing: no certificates, no checker | Serializer + checker | K1–K4,K9,K10 | 2 | Preserved |
| REQ-KERNEL-010 | v6 §0.11 | Coverage premise; typing; lineage/cycles; retention | Partial: absence needs scoped empty proof; lineage fields exist on evidence | Coverage premise + cycle reject | K3/K14 | 2–3 | Preserved |
| REQ-KERNEL-011 | v6 §0.13 | Single path to `VERIFIED`; `*_validated` as reference | Gap: multiple fast paths + LWW hypothesis/conclusion writes | Transition API + audit | K13 | 3 | Preserved |
| REQ-KERNEL-012 | v6 §§5–6 | Stages A–E; Steps 1–7; C1–C6; K1–K16; A–T | Partial: battery fixtures L1–L12 + bound-verification tests; no kernel suite | Gated rollout + suites | Plan/verification docs | All | Preserved |
| REQ-WIT-001 | v6 P2/P4 | Witnesses optional, shadow-gated, generic contracts | Compliant today (no analyzer/solver authority) | Keep; pilots only on bars | Pilot bars | 5 | Preserved |
| REQ-OBS-001 | v6 §6.7 | Cost metrics before optimization | Partial: `agent_metrics.py`, loop telemetry, OTel closed vocab | Add proof metrics | Dashboards | 1–3 | Clarified |

Gaps G1–G12 (sixth review) map to WP/GATE in the plan doc; none dropped. Highest-critical: G1 kernel spec, G2 canonicalization, G3 unsound-rule review, G4 invalidation, G5 atomic publication, G6 legacy bypass inventory.

## Current-state evidence matrix (verified symbols only)

| Capability | Implementation | Evidence | Gap | Target change | Verify |
|---|---|---|---|---|---|
| Typed claims | `ClaimKind` (6) + `ClaimTarget` | `domain/hypothesis/models.py:35-76` | No quantifier/scope/assumption schema | Extend per contracts §1.2 | Identity fixtures |
| Per-claim binding | Assessments carry kind/target; `_resolve` drops stale/foreign | `activities.py:5859-5869,6821-6831`; `verification.py:850-881` | No ledger of attempts; drops silent outside diagnostics | Retrieval ledger + dispositions | Closure harness |
| Kind proofs | `check_claim_kind` + definition/call/behavior/absence/history + Java/Python facts | `verification.py:1509-1938,2340-2702` | No registry versioning/digests; fast-path breadth | Registry + semantic validators | K1/K4 goldens |
| Confidence vs proof | Separate fields; threshold ≥0.80; fast-path 0.90 | `verification.py:3761-3765,3909-3981` | High-score bypass risk without obligation check | Obligation-gated promotion | Test M |
| Contradictions | Penalty + rival demotion; `max_allowed_unresolved=0` → CONTRADICTED | `verification.py:3669-3683`; `activities.py:5911-6032` | No typed refutation scope; write-only lifecycle fields | Typed refutations + reconciliation | K5/K11/O |
| Publication | `ConclusionGate` 7 checks + `_gate_conclusion_or_fail` | `verification.py:3909-3981`; `activities.py:6759-6768` | No context IDs; hypothesis LWW; conclusion LWW | Atomic commit on context | K8/K12/K15 + race tests |
| Concurrency | Investigation CAS; hypothesis/conclusion LWW; evidence DO NOTHING | `investigation_repository.py:149-183`; `hypothesis_repository.py:97-130`; `finding_repository.py:160-249` | Hypothesis CAS missing (Stage C entry) | Add CAS + context check | Concurrency suite |
| Workflow | Temporal loop, 10-iter default, forcing caps 2/run, version pin `metadata["verifier_version"]` (current `v2`; the `IAP_VERIFIER_V2` variable no longer exists in code) | `workflows.py:1157-1789`; `activities.py:5831` | No typed obligations; planner vestigial; no shadow policy | Pure `InvestigationActionPolicy` shadow | Shadow parity G6 |
| Resolvers | Java 10 resolvers + Python mirror; `CodeFactResolutionV2` | `domain/code/models.py`; `application/analysis/java|python/` | No registry digest binding; some `id()`-style identity risks | Bind digests; audit Node identity | Encoder goldens |
| Tests | 2798 unit green; battery L1–L12; bound-verification + phase2/3 suites | `tests/unit|fixtures|benchmarks` | No K1–K16, no property/mutation/concurrency kernel suites | Verification doc suites | Mutation cover |
| Observability | Loop telemetry, agent metrics, OTel closed vocab | `activities.py:1060-1214`; `scripts/agent_metrics.py`; `infrastructure/observability/` | No proof metrics (obligations/certs/invalidation cost) | Add per §6.7 | Dashboards |

## Legacy bypass inventory (must be centralized before authoritative rollout)

`validate_code_value_claim` (`verification.py:1121`), `validate_java_fact_claim` (`:1172`), `validate_history_claim` (`:1869`), `validate_location_claim` (`:2340`), `validate_doc_claim` (`:2493`), `validate_state_claim` (`:2580`); `_resolve` + `statement_range_reads` repair (`activities.py:5859-5910,6977-6988`); `_gate_conclusion_or_fail` fast-path chain (`:6969-7125`); `SUPPORTED→VERIFIED` promotion (`:7172-7174`); finding/conclusion publish (`:7175-7337`); abstention publishes (`:6833-7081`); cached/banked reuse across revisions (bank-interest rows). Each gets a WP line item: route through the transition API or gate behind the kernel flag.
