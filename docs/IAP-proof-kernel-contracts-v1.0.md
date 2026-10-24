# IAP Proof-Kernel Contracts: Certificates, Composition, Publication v1.0

> Companion to `docs/IAP-proof-kernel-design-v1.0.md`. Normative for kernel scope. `MUST`/`MUST NOT` are release gates.

## 3. Canonical contracts and proof kernel (Parts 3–4)

### 3.1 Canonical serialization

- JSON, UTF-8, sorted keys, no whitespace, float rejection, RFC-8785-style. `canon_version` in every hashed preimage; bump invalidates prior hashes by design. Byte-level details still open (RFC 8785 conformance, Unicode/path normalization, null/omission treatment, array-order semantics) — golden vectors required before Stage B; see design §1.2.
- `PropositionID = SHA256(canonical_bytes)`. Fixtures pin: predicate strengthening changes ID; line move keeps proposition ID + changes context ID; rename without validated normalization creates a new ID; quantifier variants never collide.

### 3.2 Rule registry (closed, versioned)

Each `PROOF_RULES` row is a normative `ProofRule`:

```text
ProofRule {
  rule_id: "call_edge/v1" | "definition/v1" | "behavior/v1" | "absence/v1" | "history/v1" | "causal/v1"
  input_types: [NORMALIZED_CALLSITE_FACT, ...]   # typed premises only
  preconditions: [...]        # mechanically checked
  assumptions: [...]          # explicit, non-mechanical
  soundness: SOUND | CONSERVATIVE | HEURISTIC
  unsupported_cases: [...]    # must abstain
  implementation_digest: sha256(registry contract: validator name + rule fields)  # K9 OPEN 2026-10-24: binds the registry contract, NOT executable bytes; no release tooling verifies executable identity yet, so executable-drift detection is explicitly not claimed (see remediation plan Phase 2)
}
```

- `SOUND`: may discharge when premises + assumptions validate. `CONSERVATIVE`: may discharge only if documented inference holds (abstention permitted). `HEURISTIC`: never a proof-producing inference in a publishable certificate (prioritize/annotate only).
- Initial kernel: `DEFINITION` (one direct fact) + `CALL_EDGE` (multi-premise, normalized call-site premise) only. `BEHAVIOR` (structure + runtime) next; then `ABSENCE`, `HISTORY`, `CAUSAL` only with contracts + independent goldens.
- Semantic premise validation: the checker validates the inference instance against the declared resolver/validator contract, never a producer label. A comment row relabeled as a call site MUST reject even with valid hashes (K1).
- Current anchor: `VerificationEngine.check_claim_kind` in `application/investigation/verification.py:1688` plus `definition_window_proof`, `call_site_proof`, `behavior_proof`, `absence_outcome`, `history_proof` become the first rule implementations behind the registry interface. `CATEGORY_PROOF_RULES` + `required_evidence_families` (`verification.py:440-502`) become the machine-readable `unsupported_cases`.

Minimal kernel operations (C5): validate premise type + provenance; apply named versioned rule; verify premise/conclusion match rule contract; check scope/assumptions/completeness; compose mandatory premises conjunctively; evaluate alternatives independently; detect cycles + invalidation deps; return typed outcomes + rejection reasons.

### 3.3 Certificate, manifest, context schemas

```json
{
  "schema_version": "cert/v1",
  "proposition_id": "sha256:…",
  "canonical_proposition": { "predicate": "calls(caller,callee)", "quantifier": "EXISTENTIAL", "canon_version": "canon/v1" },
  "rule_id": "call_edge/v1",
  "rule_version": "v1",
  "implementation_digest": "sha256:…",
  "premises": [{ "evidence_id": "uuid", "content_hash": "sha256:…", "premise_type": "NORMALIZED_CALLSITE_FACT", "scope": {} }],
  "inference_instance": { "rule": "call_edge/v1", "bindings": {} },
  "assumptions": ["static-model-only"],
  "scope": {},
  "coverage": { "universe": "…", "exclusions": [], "result": "COMPLETE_PATH" },
  "context_id": "sha256:…",
  "input_manifest_digest": "sha256:…",
  "derivation_id": "sha256:…",
  "proof_classification": "SOUND",
  "validation": { "result": "ACCEPT", "reasons": [] }
}
```

Evidence manifest: ordered evidence IDs + content hashes + retrieval config + exclusions + coverage result. Context: manifest digest + proposition ID + rule-bundle version + model/encoding config + reconciliation version. Evaluation is pure: `P = Evaluate(C,E,R,M)`; new evidence means a new evaluation, never in-place mutation. Lifecycle history stays as audit trail, not proof state.

Evaluator/checker pseudocode:

```text
evaluate(C, E_manifest, R_bundle, M_config):
  premises = validate_each_premise_types_and_provenance(E_manifest)  # typed, else INADMISSIBLE
  derivation = apply_rule(R_bundle.rule_for(C.kind), premises, C.scope, C.assumptions)
  return (derivation, certificate(derivation, context_id(C,E,R,M)))

check(certificate, ledger, registry):
  reject if schema_version unknown
  reject if rule_id/version not in closed registry or digest mismatch (K9)
  reject if any premise content-hash mismatch or artifact unavailable (K10 / PREMISE_UNAVAILABLE)
  reject if premise types do not satisfy rule contract (K1) or scope incompatible (K11)
  reject if coverage premise missing for ABSENCE/universal (K3/K4)
  reject if cycle detected (K2) or contexts mixed
  reject if context_id stale at publication time (K8/K15)
  else ACCEPT
```

Checker and evaluator MUST be independently reviewable (separate validation logic + separate fixtures); running the same implementation twice is not independence.

Three validation layers (normative): Layer 1 — structural schema validation (presence, types, formats, enums; `cert/v1` JSON Schema, full-length 64-hex digests). Layer 2 — semantic certificate validation (rule-specific premise types, scope, coverage, assumptions, quantifiers, graph integrity, conclusion consistency). Layer 3 — independent kernel checking (identity recomputation, rule applicability, premise admissibility). A schema-valid certificate is never an accepted proof; producer-supplied `validation` and `proof_classification` fields are untrusted assertions the checker ignores.

### 3.4 Typed outcomes (never collapsed before diagnostics)

`VALID | MISSING_PREMISE | INADMISSIBLE_PREMISE | INVALID_INFERENCE | SCOPE_MISMATCH | UNSUPPORTED_CONSTRUCT | STALE_CERTIFICATE | CONFLICTING_EVIDENCE | CHECKER_FAILURE | PREMISE_UNAVAILABLE`. Mapping: retrieve / reject-replace / reject / do-not-compose / different-rule / retry (never a proof verdict) / restore-or-reevaluate / adjudicate / retry. Infrastructure failure never becomes a negative proof verdict.

## 4. Coverage, composition, invalidation (Part 4)

### 4.1 Composition

- Conjunction: every mandatory premise satisfied (e.g. behavior = structure ∧ runtime ∧ scope; shape per rule, not prose).
- Alternatives: one complete admissible derivation suffices; failed alternative never taints success, never counts as refutation.
- Dependency: downstream valid only while premises remain valid; invalidation closure is transitive and cycle-free.
- Contradiction: refutation affects the proposition or route it challenges only (see §4.2).
- Shared evidence: one record may support many obligations, each evaluated independently; lineage decides corroboration (duplicates never count twice, K14).

### 4.2 Route / proposition / reconciliation split

| Status | Question |
|---|---|
| Derivation (route) status | Is this individual proof route valid? |
| Proposition status | Does a complete admissible route establish it, defeat it, or leave it insufficient? |
| Reconciliation status | Are apparent conflicts compatible, genuinely inconsistent, or unadjudicated? |

Transition table (normative): refutation killing one of two independent valid routes → that route invalid, proposition stays supported; admissible counterexample to a universal → proposition defeated, publication denied; cross-scope/config/execution evidence → recorded, not a conflict; unadjudicated material conflict → proposition not established, publication denied. `CONFLICTED` (obligation: admissible refutation challenges a live route/proposition) vs `CONTRADICTED` (lifecycle: unresolved-contradiction overflow) linked explicitly; invalidating the sole refutation recomputes with no strength gain.

Reconciliation dispositions: `invalid_or_foreign` (excluded with reason); `stale_needs_revalidation`; `admissible_conflict`; `different_predicate_or_scope` (routed, never counted); `reconciled_under_rule` (rule version + reconciling evidence recorded).

### 4.3 Coverage as premise

For `ABSENCE`, universal, and completeness-sensitive claims the certificate carries: (1) search executed OK; (2) specified universe; (3) adequate coverage for the predicate; (4) zero matches; (5) match semantics supporting absence. Manifest records execution, configuration, scope, exclusions, coverage result. Classifications: `COMPLETE_EMPTY | INCOMPLETE | FAILED | MATCHES_FOUND`. `COMPLETE_EMPTY` satisfies absence only when universe coverage + match semantics hold — never beyond declared universe. Unbounded/inadequately covered universes yield `UNSUPPORTED`/`OPEN`.

Typing + lineage: raw artifact → normalized record → derived fact → proof premise → intermediate conclusion. Every derived record carries parent derivation + lineage; checker rejects cycles and self-dependent derivations; intermediate conclusions never re-ingested as independent primary evidence. Hash proves sameness, not correctness; provenance + semantic admissibility validated separately. Ledger carries retention policy or `PREMISE_UNAVAILABLE`.

### 4.4 Invalidation closure

Each rule declares `Deps(R_i)`: proof-relevant input classes that can affect it. Closure runs over the actual derivation graph, transitive and cycle-free. Root-premise change invalidates every dependent (K6); independent alternatives and unrelated claims preserved; non-declared changes (e.g. logging config) do not invalidate; dependency never inferred from shared file/claim/evaluation. `UNSUPPORTED` is context-scoped; provider recovery permits reevaluation.

## 5. Atomic publication and lifecycle (Part 5)

### 5.1 Linearization point (mandatory)

1. Establish consistent evaluation context. 2. Validate certificate + premises. 3. Verify reconciliation + policy inputs. 4. Confirm context still current at the commit point. 5. Commit referencing exact certificate + context ID (never bare `VERIFIED`).

One atomic operation: conditional update on `(investigation_id, context_id, version)` (database transaction, CAS, or equivalent; technology free, atomicity mandatory). Version-check-then-unguarded-write is prohibited. On context change: reevaluate or deny; never mutate the old certificate (K8/K15).

Dependency classes: proof deps (source, evidence, scope, model, rules); publication deps (conflict state, freshness + policy, audit state); operational deps (capabilities, budgets, provider availability — affect progress only, never truth, never invalidate). Whether an older consistent snapshot may still publish is per-kind policy (immutable historical-source claims may survive unrelated evidence; derivations the new evidence can invalidate cannot).

### 5.2 Single path + legacy migration

Only the publication path authorizes publication, and only a checker-accepted current derivation supports it. Migration: inventory every `VERIFIED`/validated-flag/cached-outcome/migration/fast-path writer (see design §6 gap list), route through the transition API, audit each write path. Any direct `VERIFIED` without a valid current derivation is rejected regardless of score/caller (K13). `*_validated` flags remain certificate references with deriving rule version + evidence set.

### 5.3 Freshness, legacy, cross-modality, lineage (normative deltas from current behavior)

- Freshness vs identity: revision + scope + model decide applicability; `<24h` gate stays a per-type publication-policy dimension, never applicability. Older evidence for immutable revision + stable model stays applicable.
- Legacy tiers (exhaustive): discovery-only (retrieval hints, never proof) / revalidatable (re-checked, admitted as new evidence with current provenance) / historical-context (visible, never satisfies new obligations). Tolerant readers; missing revision/verifier never receives implicit trust.
- Cross-modality joins declare keys: revision + source mapping, build identity, runtime env/config, execution/trace identity, window, symbol relation, causal assumptions. Coexistence is never causation; mismatched builds/traces/envs are adversarial tests.
- Corroboration over lineage: three rows from one analyzer run, solver verdict derived from the fact it supports, cached copies, shared incomplete models, or summaries never count as independent confirmation. Where corroboration is required, the rule defines independence; otherwise one authoritative source suffices.
