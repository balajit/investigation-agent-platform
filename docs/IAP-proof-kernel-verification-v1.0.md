# IAP Proof-Kernel Verification and Adversarial Suite v1.0

> Every test states certificate disposition, rejection reason, invalidation closure, reconciliation state, publication decision, and observable events where relevant. Fixtures fixed before implementation runs; ambiguous cases independently adjudicated.

## Golden layers (four, independent)

1. Evidence-level: source identity, locations, execution scope, what each record demonstrates.
2. Rule-level: premises entail the canonical proposition.
3. Composition-level: individually valid premises jointly establish the conclusion (scope/quantifier preserved).
4. End-to-end publication: published conclusion supported by expected derivation + current context.

## K1–K16 kernel acceptance (Stage B/C milestone with A–T)

| Test | Fault | Required outcome |
|---|---|---|
| K1 | Valid hash, false semantic type (comment as call site) | Certificate rejected |
| K2 | Circular derivation | Rejected |
| K3 | Empty search, excluded relevant universe | No absence proof |
| K4 | One execution as universal proof | Rejected |
| K5 | One route fails, another valid route survives | Supported via survivor |
| K6 | Root premise invalidated transitively | All dependents reevaluated |
| K7 | Two claims normalize to one ID | No silent transfer; migration adjudicated |
| K8 | Evidence change between validation and publication | Atomic commit or deny |
| K9 | Implementation changes, declared identity unchanged | Release control detects |
| K10 | Referenced premise artifact missing | No acceptance (`PREMISE_UNAVAILABLE`) |
| K11 | Support/refute different scopes | No false conflict/composition |
| K12 | Policy changes, proof unchanged | Proof preserved; publication reevaluated |
| K13 | Fast path / confidence tries open obligation | Verification + publication denied |
| K14 | Same evidence duplicated | No corroboration inflation |
| K15 | Stale certificate/context for publication | Rejected |
| K16 | Same declared inputs, different derivations | Fail unless explicitly excluded field |

Plus pinned fixtures: location movement (same proposition, new context); scope broadening (new obligations); over-invalidation from undeclared dep (must not invalidate); `UNSUPPORTED` after recovery (must reevaluate); older consistent snapshot publishable only where per-kind policy permits.

## A–T regression + M–T additions

A speculative laundering; B comment/construction as call; C repair across boundary; D right rows wrong proposition; E refutation evaporation; F wrong witness; G truncated path; H solver wrong abstraction; I unrealizable solver model; J obligation survives claim change; K merged obligations; L workflow ≠ proof progress. M high-score missing proof → deny; N model/config drift → invalidate; O contradiction retraction → recompute, no gain; P runtime vs structure → runtime obligation stays open; Q provider recovery → reevaluate; R abstract solver witness → no concrete claim until mapped; S scope movement → invalidate/rebind; T legacy rows → discovery only without revalidation.

Metamorphic: change one property (revision, tier, refutation add/remove, predicate strength, truncation, solver on/off, replay) and assert the single expected delta. Three-outcome reporting always: false-accept rate / correct-verification rate / correct-abstention rate, plus recovery, action count, latency, breakdowns by kind/family/tier. `PARTIAL` fails exit where the protocol requires it.

## Property, mutation, concurrency, integration

- Properties: monotonicity (accumulation never raises strength without rule satisfaction); refutation sensitivity; invalidation closure; reproducibility; scope non-escalation; provenance non-independence; publication consistency; recovery correctness.
- Mutation: remove a required premise / weaken a scope check / bypass an invalidation edge / drop a semantic validator — suite MUST fail (proves sensitivity, not just passage).
- Concurrency: controlled interleavings of evidence updates, reconciliation changes, publication commits; hypothesis-CAS + context-check behavior; no mixed-snapshot publication.
- Integration/E2E: battery L1–L12 shapes plus offline-only duplicate-symbol/revision/failure/absence fixtures; solver-off reproduces native baseline; replay compatibility with recorded Temporal histories.

## Environment and sensitivity

K-suite + A–T + property/mutation/concurrency run together at every authoritative gate. Report invariants exercised, attacks detected, untested scenarios, and injected-defect detection. Zero new false accepts on fixtures is a regression gate, never a population claim; reason-tagged false-accept deltas prevent a binding fix from hiding an edge-composition regression.
