# Obligation-Layer Design Proposal v1.1 (DRAFT — for owner review, no code)

Amends v1.0 per owner direction 2026-10-28 (five points: required
checklist, exact promotion states, adjudication contract,
derivation-bound identity, trusted in-kernel evaluation). Sections
changed from v1.0 are marked **[v1.1]**. Still design only; implementation
needs a separate explicit GO after design acceptance.

## Amendments in v1.1 (owner direction 2026-10-28)

1. **Required checklist.** `obligations` input is REQUIRED; omission denies
   (`OBLIGATIONS_ABSENT`). No unguarded legacy route is preserved — all
   in-tree callers and tests are updated at implementation. (§Gate, §Test plan)
2. **Exact promotion states.** Only `SATISFIED`, and `CONFLICTED` paired
   with a valid `PROCEED` adjudication, promote. `OPEN` denies
   (`OPEN_OBLIGATION`); `UNSUPPORTED` denies (`UNSUPPORTED_OBLIGATION`,
   acknowledgment does not promote); `INVALIDATED` denies
   (`STALE_OBLIGATION`, requires fresh expansion). "Terminal" is
   redefined: promotion-terminal vs dead-end vs open. (§States, §Gate)
3. **Adjudication contract.** New frozen `Adjudication` object
   (`adjudication_id`, `obligation_id`, `derivation_id`, `context_id`,
   `authority`, `ruling` ∈ {`PROCEED`, `UPHOLD_DENIAL`}, `rationale`);
   the gate verifies all five bindings plus authority-allowlist
   membership. `ADJUDICATED` is not a status — `CONFLICTED` stays
   `CONFLICTED`; promotion reads the pair. (§Adjudication, §Gate)
4. **Derivation-bound identity.** `obligation_id` =
   `obl:<derivation-id>:<rule-id>:<ordinal>`; status records are
   immutable and chain by digest (`supersedes`); the gate reads the
   chain head per ID and denies on fork (`STALE_OBLIGATION`, re-expand).
   (§Identity)
5. **Trusted evaluation.** The gate derives the obligation set and
   evaluates statuses itself, in-kernel, from the certified inputs —
   no caller-asserted checklist is ever read. `PublicationRequest`
   carries the evidence the derivation needs (certificate + manifest +
   artifacts + authority allowlists), not a verdict. Adjudications are
   caller-supplied but VERIFIED (all bindings + allowlist), never
   trusted. (§Evaluation, §Gate)

## Problem

K13 ("fast-path/confidence bypass of open obligations denied") is PARTIAL.
The decision-path half is exhaustively pinned (all 9 non-VALID outcomes
deny; CAUSAL abstention can never reach COMMIT). The promotion half has no
objects to gate on: there is no obligation model, no per-kind obligation
expansion, and `decide_publication` takes no obligation input — so
`VALID + open obligation` cannot even be expressed, let alone denied.
The A–T F row (wrong witness) is blocked on the same missing object.

## Contract

### ProofObligation (new frozen model, `domain/proof/obligations.py`)

- `obligation_id`: `obl:<derivation-id>:<rule-id>:<ordinal>` (see §Identity).
- `kind`: the rule ID that created it.
- `statement`: the checkable restatement.
- `status`: closed vocabulary — `OPEN`, `SATISFIED`, `CONFLICTED`,
  `UNSUPPORTED`, `INVALIDATED` (frozen on acceptance; append-only after).
- `evidence`: evidence IDs that satisfy it (empty unless SATISFIED).
- `derived_from`: derivation ID (equals the ID's derivation segment).
- `supersedes`: digest of the previous status record for this obligation
  ID, or null for the genesis record.

No timestamps, no confidence scores, no free text beyond `statement`.

### Kind table (`expand_obligations`)

Unchanged from v1.0:

| Rule | Obligations (each must reach a promotable state before promotion) |
|---|---|
| definition/v1 | O1 single RANGE_READ premise bound; O2 symbol shown in cited text; O3 premise revision joins claim scope (when pinned) |
| call_edge/v1 | O1 all premises NORMALIZED_CALLSITE, unique IDs; O2 caller/callee bound in cited text; O3 premise revisions join claim scope (when pinned) |
| behavior/v1 | O1 code/runtime/mapping triple present, unique; O2 joins hold (target, repo, revision, build, execution, environment); O3 mapping authority approved |
| absence/v1 | O1 complete-empty coverage; O2 zero matches; O3 scope/config/exclusion agreement; O4 no relevant exclusion |
| history/v1 | O1 single commit-history premise; O2 full-SHA pins; O3 repo/revision/commit/file/subject equality; no locators |

`expand_obligations(rule_id, claim, premises, derivation_id)`
-> `list[ProofObligation]` is pure (frozen inputs, new objects,
deterministic). Unknown rule kind (CAUSAL and anything unregistered)
expands to a single `UNSUPPORTED` obligation — abstention, never
discharge. Ordinals assign in kind-table order (stable).

### States **[v1.1]**

The vocabulary is unchanged; the PROMOTION reading is now exact:

| Latest status | Promotion | Reason on deny |
|---|---|---|
| `SATISFIED` | permits (with all others promotable) | — |
| `CONFLICTED` + valid `PROCEED` adjudication | permits (pair read, §Adjudication) | — |
| `CONFLICTED` without valid adjudication | denies | `CONFLICT_UNRESOLVED` (existing member) |
| `OPEN` | denies | `OPEN_OBLIGATION` |
| `UNSUPPORTED` | denies, always | `UNSUPPORTED_OBLIGATION` — acknowledgment, repetition, or seniority never promotes |
| `INVALIDATED` | denies, always | `STALE_OBLIGATION` — requires a fresh expansion; the dead set never promotes |

"Terminal" now means two distinct things, not one:

- **Promotion-terminal**: `SATISFIED`, and `CONFLICTED` paired with a
  valid `PROCEED` adjudication. The gate may promote on these and only these.
- **Dead-end**: `UNSUPPORTED`, `INVALIDATED`. No transition out promotes
  this set; the remedy is outside the set (authority approval for
  UNSUPPORTED, fresh expansion for INVALIDATED).
- **Open**: `OPEN` is the only status with further evaluation to do.

Status records are immutable. A status change is a NEW record with the
same `obligation_id`, a new digest, and `supersedes` pointing at the
previous record's digest. No record is ever edited.

### Adjudication **[v1.1]**

`CONFLICTED` obligations do not become "adjudicated" — there is no
`ADJUDICATED` status. Instead, promotion reads a PAIR: the (still
`CONFLICTED`) obligation plus a separate frozen `Adjudication` object:

- `adjudication_id`: `adj:<derivation-id>:<obligation-ordinal>:<n>`.
- `obligation_id`: must equal a CONFLICTED obligation in the derived set.
- `derivation_id`, `context_id`: must equal the decision's derivation
  and expected context (binds the ruling to this decision, not to the
  obligation in general).
- `authority`: the ruling party, as an opaque string.
- `ruling`: closed — `PROCEED` or `UPHOLD_DENIAL`.
- `rationale`: non-empty string (audit, never evaluated).

Gate verification order for each CONFLICTED obligation: (1) an
adjudication names its `obligation_id`, else deny `CONFLICT_UNRESOLVED`;
(2) `derivation_id` and `context_id` match the decision, else deny
`CONFLICT_UNRESOLVED` (cross-decision rulings do not transfer);
(3) `authority` is a member of the approved adjudication authorities
carried with the request (explicit allowlist, default empty — with an
empty set no `PROCEED` is accepted, and the gate says so);
(4) `ruling == PROCEED` promotes the pair, `UPHOLD_DENIAL` denies
(`CONFLICT_UNRESOLVED`).

Adjudication authority values are owner-approved strings (e.g. role
names); the initial allowlist is EMPTY — the implementation ships
deny-by-default, and the first accepted authority needs an explicit
owner decision. This keeps the conflict path closed until the owner
opens it deliberately.

### Identity and lineage **[v1.1]**

- `obligation_id = obl:<derivation-id>:<rule-id>:<ordinal>`.
  Derivation-binding removes the cross-derivation collision class:
  two decisions never mint the same ID because derivation IDs are
  unique per (context, rule, premises, inference).
- Each status record carries `derivation_id`, `context_id`,
  `supersedes` (previous record digest or null), and its own digest
  (canonical bytes over the record, same layout discipline as
  derivations).
- Lineage of one obligation = records sharing its ID ordered by the
  `supersedes` chain. The gate reads the CHAIN HEAD per ID.
- Fork (two records superseding the same parent) denies the whole
  decision with `STALE_OBLIGATION` — a forked lineage is not readable
  and the remedy is the same: a fresh expansion. Note: the v1
  in-kernel evaluator emits genesis records only (`supersedes=null`)
  and therefore cannot fork; the fork rule is specified now so the
  future persisted layer (G5) inherits it, and is covered by a vector
  at implementation.
- The immutable record chain is the AUDIT trail, not the decision
  input: the gate re-derives per decision (§Evaluation) and returns
  the derived obligation report inside the denial/commit reason
  (counts + IDs; reason strings stay bounded). No stored record is
  ever trusted for promotion.

### Evaluation — trusted, in-kernel **[v1.1]**

The v1.0 caller-supplied checklist is WITHDRAWN — it was the bypass the
owner identified (a caller could omit obligations or assert SATISFIED
without evidence, and the gate could not tell).

- `expand_obligations(rule_id, claim, premises, derivation_id)` and
  `evaluate_obligations(obligations, certificate, manifest, artifacts)`
  are kernel-pure functions. `evaluate_obligations` returns each
  obligation with a derived status (OPEN/SATISFIED/CONFLICTED/
  UNSUPPORTED/INVALIDATED) computed from the certified inputs —
  the same inputs `check_certificate` verified.
- `decide_publication` derives the set itself on every call: it takes
  the certificate, manifest, artifacts, and authority allowlists as
  REQUIRED inputs (see §Gate) and never reads a caller-asserted
  status. There is no parameter through which a caller can omit an
  obligation or assert SATISFIED.
- Caller-supplied `Adjudication` objects ARE accepted as inputs but
  VERIFIED per §Adjudication (all five bindings + allowlist) —
  verification, not trust. An adjudication that fails any check is
  ignored (denial proceeds on the CONFLICTED obligation), and the
  reason names the failed binding.
- Cost note: derivation runs the same joins the checker ran (same
  inputs, same purity). No I/O, no production contact, no new
  subsystems — the layer stays inside `domain/proof`.

### Gate extension **[v1.1]** (`decide_publication`, contract change)

- `PublicationRequest` changes shape (reviewed contract change):
  scalar context/derivation IDs stay, and three evidence inputs are
  added — `certificate`, `manifest`, `artifacts` — plus `adjudications`
  (default empty list) and `adjudication_authorities` (default empty
  list). The evidence inputs are Optional-with-default-None and
  None denies with `OBLIGATIONS_ABSENT` (the gate stays a total
  function — it denies on shape instead of raising — but there is no
  shape that reaches the old unguarded verdict). **[v1.1: the unguarded
  legacy route is removed, not preserved.]**
- New `DenyReason` members (frozen enum is append-only; existing
  members unchanged): `OBLIGATIONS_ABSENT`, `OPEN_OBLIGATION`,
  `UNSUPPORTED_OBLIGATION`, `STALE_OBLIGATION`. `CONFLICT_UNRESOLVED`
  is reused for unpaired or invalidly-paired CONFLICTED obligations,
  with reason strings prefixed `obligation-conflict:` to distinguish
  them from the reconciliation conflict (which keeps its exact existing
  reason string, so the current reconciliation test does not move).
- Checklist order: `NO_PINNED_CONTEXT` -> `NOT_CHECKER_ACCEPTED` ->
  obligation derivation + evaluation (all deny classes above) ->
  `CONFLICT_UNRESOLVED` (reconciliation) -> `STALE_CONTEXT` ->
  `POLICY_DENIED`. The obligation gate runs before conflict and
  policy reasoning, takes no score input, and confidence can never
  override it (there is no parameter for it to arrive through).
- Promotion rule, exact: COMMIT iff checker VALID AND every derived
  obligation's latest status is `SATISFIED`, or `CONFLICTED` with a
  valid `PROCEED` adjudication. Anything else denies with the
  row's reason from the §States table.

## Test plan (for the implementation GO, not this proposal)

- New `tests/unit/test_obligation_gating_k13.py`: expansion vectors per
  kind (obligation count/IDs/statements, CAUSAL -> single UNSUPPORTED);
  transition-closure vectors (legal/illegal record chains, fork denies
  STALE_OBLIGATION); adjudication vectors (valid PROCEED pair promotes;
  wrong derivation/context/authority/ruling denies with the binding
  named; empty allowlist accepts no PROCEED).
- Update `tests/unit/test_publication_decision.py`: every existing
  vector gains derived evidence inputs (contract change — the
  scalar-only shape is gone); new vectors for each deny class
  (OBLIGATIONS_ABSENT, OPEN, UNSUPPORTED, STALE, unpaired CONFLICTED);
  confidence-does-not-override vector (VALID + policy-eligible +
  open obligation still DENYs).
- Bypass vectors (the v1.0 hole, now closed): caller-shaped inputs
  that omit an obligation or assert SATISFIED are not representable —
  a test asserts the request shape has no status-assertion field
  (structural pin, like the checker import-separation test).
- Fingerprint addition: `obligations.py` joins the fingerprinted module
  set (reviewed change, manifest re-record).
- No live tests: pure domain, no production callers by construction.

## Explicit non-goals

- No obligation persistence (no tables, no migration): obligations are
  derived per decision from the certificate + manifest + artifacts.
  Durable obligation tracking is G5/publication-service scope.
- No legacy-path closure (K13-B3): writer/caller audit and LWW/fast-path
  removal ride the B2/C1 rollout decisions, not this contract.
- No real-world authority (K13-B4): BEHAVIOR O3 stays unsatisfiable in
  production until the B3 reopen trigger fires; UNSUPPORTED denies
  rather than discharging.
- No second-path duplication of `expand/evaluate_obligations` in v1:
  expansion/evaluation is a single pure derivation (not a verdict),
  covered by vectors; verdict diversity stays where it is (checker +
  second path). Recorded as follow-up if the owner wants it.

## Decision requested (REVIEW GATE R2)

1. Accept the v1.1 contract as stated, amend it, or reject.
2. On acceptance: separate GO for the implementation (new module +
   gate contract change + tests + fingerprint addition, pure domain
   only). No code starts before that GO.
