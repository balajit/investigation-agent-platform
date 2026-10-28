# Obligation-Layer Design Proposal v1.2 (DRAFT — for owner review, no code)

Amends v1.1 per owner direction 2026-10-28 (three remaining contract gaps:
trusted allowlist source, bound checker outcome, status genesis). Sections
changed from v1.1 are marked **[v1.2]**. Still design only; implementation
needs a separate explicit GO after design acceptance.

## Amendments in v1.2 (owner direction 2026-10-28)

1. **Trusted allowlist.** The adjudication authority allowlist moves out
   of the caller request into a trusted `DecisionPolicy`, constructed by
   the operator (construction site owned by B2 wiring) and passed
   separately from the caller evidence. The gate binds evidence to
   policy (repository scope) and checks adjudications against the
   policy allowlist. Tenant-to-policy selection stays a B2-wiring
   responsibility and is named as such. (§Policy, §Adjudication, §Gate)
2. **Bound checker outcome.** The guarded promotion entry runs
   `check_certificate` itself from the evidence inputs and promotes on
   its own internally produced outcome — there is no caller-supplied
   `checker_outcome` field to mismatch, so the mismatch class is
   eliminated structurally rather than detected. The scalar
   `decide_publication` is demoted to an internal linearization
   sub-step operating on gate-derived values; its existing tests keep
   pinning it as a unit. (§Gate)
3. **Status genesis.** Each non-SATISFIED status gets its mechanical
   producer: UNSUPPORTED from pre-checker expansion of unsupported
   kinds (CAUSAL ordering fixed: denied without consulting the
   checker); OPEN from evaluator abstention where the checker contract
   permits absence (concrete case: revision-unpinned definition/
   call_edge certificates, checker-VALID today, promotion waits);
   CONFLICTED from dual-evaluator disagreement (primary +
   independent second evaluator, same diversity rationale as the
   verdict conjunction); INVALIDATED from evidence change between
   internal check and evaluation (re-verify closes the TOCTOU class
   by construction) and from forked lineage / stale adjudications.
   Evaluation runs only on checker-VALID inputs; otherwise the checker
   reason stands and obligations are not evaluated. (§Genesis, §Gate)

v1.1 amendments (required checklist with OBLIGATIONS_ABSENT, exact
promotion-state table, Adjudication five-binding verification,
derivation-bound IDs with digest-chained lineage, trusted in-kernel
derivation with the caller checklist withdrawn) are retained except
where v1.2 explicitly supersedes them (allowlist location, outcome
transport, evaluation cardinality).

## Problem

K13 ("fast-path/confidence bypass of open obligations denied") is PARTIAL.
The decision-path half is exhaustively pinned. The promotion half has no
objects to gate on. The A–T F row (wrong witness) is blocked on the same
missing object.

## Contract

### ProofObligation (new frozen model, `domain/proof/obligations.py`)

Unchanged from v1.1: `obligation_id` =
`obl:<derivation-id>:<rule-id>:<ordinal>`; `kind`; `statement`;
closed `status` vocabulary (`OPEN`, `SATISFIED`, `CONFLICTED`,
`UNSUPPORTED`, `INVALIDATED`); `evidence`; `derived_from`;
`supersedes` (previous record digest or null). Records immutable; no
timestamps, no confidence scores.

### Kind table (`expand_obligations`)

Unchanged from v1.1 (five rule rows; unknown kinds expand to a single
`UNSUPPORTED` obligation). Signature gains the derivation binding:
`expand_obligations(rule_id, claim, premises, derivation_id)`.

### DecisionPolicy — trusted configuration **[v1.2]**

The v1.1 request-carried `adjudication_authorities` field is WITHDRAWN
(a caller choosing both the adjudication and the allowlist proves
nothing). Replaced by a separately passed trusted object:

- `DecisionPolicy`: `policy_id` (stable string, e.g.
  `policy:<tenant>:<name>:<version>`); `repositories` (list of repository
  roots this policy speaks for); `approved_adjudication_authorities`
  (list of strings, default empty — deny-by-default preserved).
- Constructed by the operator (file/config, owned by the B2 wiring
  work: the wiring selects the tenant's policy and passes it alongside
  the tenant's evidence). Pure-domain v1 constructs it explicitly in
  tests — no ambient config, no environment lookup, no singleton.
- Trust boundary, exact: the caller supplies evidence + adjudications
  (transport, verified, never trusted); the operator supplies the policy
  (trusted); the gate binds them (§Gate, step 1). Tenant-to-policy
  selection is OUTSIDE the kernel (B2 wiring): the kernel binds
  evidence repositories to the selected policy's `repositories` list,
  and denies `POLICY_DENIED` (reason `policy-scope:<repo>`) on mismatch.
  Kernel inputs carry no tenant marker by design (purity); the wiring
  is where tenant isolation lives, and the design says so instead of
  pretending otherwise.

### States and genesis **[v1.2]**

The v1.1 promotion table stands; each row now names its mechanical
producer:

| Latest status | Producer (how evaluation reaches it) | Promotion |
|---|---|---|
| `SATISFIED` | Both evaluators agree all joins hold on the certified inputs | permits |
| `CONFLICTED` + valid `PROCEED` adjudication | Primary and independent second evaluator DISAGREE on an obligation's resolution (§Evaluation); pair read per §Adjudication | permits |
| `CONFLICTED` without valid adjudication | (same producer) | denies `CONFLICT_UNRESOLVED` (`obligation-conflict:` prefix) |
| `OPEN` | An evaluator ABSTAINS: a required agreement input is absent where the checker contract permits absence. Concrete live case: revision-unpinned definition/call_edge certificates — checker-VALID today (join skipped on both paths when unpinned), promotion waits because O3 (revision join) cannot be established. The checker accepts (its contract); the promotion bar is deliberately stricter. | denies `OPEN_OBLIGATION` |
| `UNSUPPORTED` | Pre-checker expansion of an unsupported kind (CAUSAL and anything unregistered). Produced BEFORE the checker runs — the abstention path denies explicitly instead of relying on the checker's separate denial. A VALID certificate can never accompany UNSUPPORTED (the checker would deny too); the row exists so abstention is a verdict, not a gap. | denies `UNSUPPORTED_OBLIGATION`, always |
| `INVALIDATED` | (a) Evidence changed between the internal check and evaluation: the gate recomputes manifest digest + artifact hashes at evaluate time and compares to check-time values; mismatch denies. In the atomic guarded entry the inputs are frozen, so the re-verify always passes — which is exactly the closure proof for the check-then-decide TOCTOU class. (b) Forked lineage (forward-compat, v1.1). | denies `STALE_OBLIGATION` (a, b) |

Evaluation runs ONLY on checker-VALID inputs: if the internal check
denies, the checker reason stands and obligations are not evaluated
(there is nothing to promote). The v1.1 question "can a VALID
certificate reach non-SATISFIED" is therefore answered per row above:
yes for OPEN (unpinned revision), yes for CONFLICTED (evaluator
disagreement on VALID-checker inputs — the differential suite's mutant
vectors are exactly this shape), no for UNSUPPORTED (pre-checker by
construction), and INVALIDATED is the freshness guard around the
decision itself.

"Terminal" (v1.1): promotion-terminal = `SATISFIED`, `CONFLICTED` +
valid `PROCEED`. Dead-end = `UNSUPPORTED`, `INVALIDATED`. Open = `OPEN`.

### Adjudication **[v1.2: allowlist source changed]**

Shape unchanged from v1.1 (`adjudication_id`, `obligation_id`,
`derivation_id`, `context_id`, `authority`, `ruling` ∈
{`PROCEED`, `UPHOLD_DENIAL`}, `rationale`). Verification unchanged
(five bindings, in order) EXCEPT step 3: membership is checked against
the TRUSTED POLICY allowlist (`policy.approved_adjudication_authorities`),
not a request field. The initial allowlist is EMPTY (deny-by-default;
the first accepted authority needs an explicit owner decision).
No `ADJUDICATED` status exists; `CONFLICTED` stays `CONFLICTED`.
An adjudication naming the right obligation but bound to a DIFFERENT
derivation is stale evidence: ignored for pairing (the obligation
stays unpaired → `CONFLICT_UNRESOLVED` with the stale binding named).
Rulings do not transfer across decisions.

### Identity and lineage

Unchanged from v1.1 (derivation-bound IDs, digest-chained immutable
records, chain-head reads, fork denies `STALE_OBLIGATION` as a
forward-compat rule for the persisted layer).

### Evaluation — trusted, dual, in-kernel **[v1.2]**

- `expand_obligations` + `evaluate_obligations` (primary) run
  in-kernel from the certified inputs, as v1.1. ADDED: an independent
  second evaluator `evaluate_obligations_independent`, written to the
  same diversity discipline as `second_path` (no shared modules with
  the primary evaluator; restated closed sets; hand-rolled checks).
  Per-obligation resolution: both agree → that status; either
  abstains while the other resolves → `OPEN` (abstention is
  contagious — promotion must not outrun the weaker reader);
  disagree on resolved statuses → `CONFLICTED` (standing protocol
  for adjudication, never silent).
- The v1.0 caller checklist stays withdrawn (v1.1). The v1.1
  `adjudication_authorities` request field is now withdrawn too
  (allowlist lives in the policy). Caller inputs to the guarded entry
  are evidence + adjudications + reconciliation/policy-eligibility
  scalars (post-gate only, unchanged semantics) — nothing the caller
  supplies can omit an obligation, assert a status, or approve an
  authority.

### Gate — guarded promotion entry **[v1.2]**

New public function (pure domain, no production contact):

```
decide_guarded_promotion(
    policy, rule_id, proposition, certificate | None,
    manifest, artifacts, adjudications,
    expected_context_id, reconciliation_adjudicated,
    policy_eligible, policy_reason,
) -> (PublicationDecision, reason)
```

Order, exact:

1. **Bind evidence to policy.** Collect repositories from the
   proposition scope and every premise artifact; at least one must be
   present (all five premise models require `repository`), and every
   member must fall within `policy.repositories`; else deny
   `POLICY_DENIED` (`policy-scope:<repo>` or `policy-scope:
   no-repository`). No policy (None) denies `POLICY_DENIED`
   (`no-policy`). Tenant-to-policy selection is the B2 wiring's job;
   this step binds evidence to the selected policy.
2. **Expand.** `expand_obligations(rule_id, ...)`; any `UNSUPPORTED`
   in the set denies `UNSUPPORTED_OBLIGATION` immediately —
   the checker is not consulted (CAUSAL ordering fixed; abstention
   is explicit). `certificate=None` with a registered rule denies
   `OBLIGATIONS_ABSENT` (nothing to evaluate).
3. **Check, internally.** Run `check_certificate` from the evidence
   inputs (constructed `CheckRequest` inside the gate). Non-VALID →
   deny with the scalar mapping (`NOT_CHECKER_ACCEPTED:<outcome>:
   <first-reason>`), obligations unevaluated — assert evaluation was
   not reached. **[v1.2: the supplied outcome class is eliminated —
   there is no `checker_outcome` request field on this entry, so
   binding is structural, not asserted.]**
4. **Re-verify freshness of evidence.** Recompute manifest digest +
   artifact hashes; mismatch vs step 3 denies `STALE_OBLIGATION`
   (TOCTOU closure proof — always passes on frozen inputs).
5. **Dual-evaluate.** Primary + independent evaluators over the
   checked inputs; per-obligation agreement protocol above.
6. **Promotion rule (v1.1 exact):** COMMIT iff every obligation is
   `SATISFIED`, or `CONFLICTED` with a valid `PROCEED` adjudication;
   else the row's deny reason. Then run the scalar linearization
   sub-step with gate-derived values (VALID outcome, reconciliation
   and policy-eligibility scalars as passed — evaluated LAST, cannot
   override a deny); COMMIT only if the sub-step also commits.

The scalar `decide_publication` is DEMOTED to the internal
linearization sub-step: same function, same tests (they keep pinning
it as a unit), but it is no longer the promotion contract — the
guarded entry is. Existing scalar tests do not move; new tests target
the guarded entry.

### Test plan (for the implementation GO, not this proposal)

v1.1 plan retained, plus:

- Policy binding: wrong-repository evidence denies `POLICY_DENIED`
  (`policy-scope:`); None policy denies; tenant selection is a B2
  wiring test, not a kernel test (say so in the test).
- Outcome binding (structural): request-shape test asserting the
  guarded entry has NO `checker_outcome` field (like the
  checker import-separation test); internal-check-denies vector
  (defective evidence denies with the checker's reason, obligations
  unevaluated — assert evaluation was not reached, e.g. via a
  reason prefix or a sentinel).
- Genesis vectors: OPEN via revision-unpinned VALID definition cert
  (checker VALID, obligation O3 OPEN → `OPEN_OBLIGATION`;
  then the pinned-revision twin promotes — the pair proves the
  promotion bar is strictly above the checker bar); CONFLICTED via
  evaluator-disagreement injection (monkeypatch one evaluator on a
  fixed input → pair requires adjudication; unpaired denies);
  UNSUPPORTED via CAUSAL rule_id without consulting the checker
  (assert the checker was not called — sentinel or call-count);
  STALE via forked lineage fixtures and via stale-derivation
  adjudication (reason names the stale binding).
- Dual-evaluator agreement: full valid corpus (per rule) resolves
  all-SATISFIED on both evaluators.
- Fingerprint addition: `obligations.py` (+ second evaluator module
  if split) joins the fingerprinted set.

### Explicit non-goals

- v1.1 non-goals retained (no persistence; no legacy-writer closure;
  no real-world authority — BEHAVIOR O3 stays unsatisfiable in
  production until the B3 trigger; no second-path duplication of
  expansion in v1 beyond the dual evaluators specified here).
- Mapping-authority allowlist (`mapping_authority` request input to
  `check_certificate`, membership in a hardcoded set) has the SAME
  caller-supplied shape as the v1.1 adjudication allowlist this
  proposal fixes. Deliberately out of v1.2 scope (it is checker
  contract, frozen, shared by both verdict paths): tracked as
  follow-up — either carry it into the DecisionPolicy at the checker
  contract's next amendment, or justify why mapping provenance
  differs from adjudication authority. Not silently dropped; named here.

## Decision requested (REVIEW GATE R2, second round)

1. Accept the v1.2 contract as stated, amend it, or reject.
2. On acceptance: separate GO for the implementation (new module(s)
   + guarded entry + scalar demotion + tests + fingerprint addition,
   pure domain only). No code starts before that GO.
