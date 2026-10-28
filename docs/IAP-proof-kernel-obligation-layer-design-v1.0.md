# Obligation-Layer Design Proposal v1.0 (DRAFT — for owner review, no code)

## Problem

K13 ("fast-path/confidence bypass of open obligations denied") is PARTIAL.
The decision-path half is now exhaustively pinned (all 9 non-VALID outcomes
deny; CAUSAL abstention can never reach COMMIT). The promotion half has no
objects to gate on: there is no obligation model, no per-kind obligation
expansion, and `decide_publication` takes no obligation input — so
`VALID + open obligation` cannot even be expressed, let alone denied.
The A–T F row (wrong witness) is blocked on the same missing object.

This proposal defines the closed obligation contract. It authorizes no code;
implementation needs a separate explicit GO after design acceptance.

## Contract

### ProofObligation (new frozen model, `domain/proof/obligations.py`)

- `obligation_id`: stable string, `kind:<rule-id>:<ordinal>` (e.g.
  `kind:behavior/v1:0`).
- `kind`: the rule ID that created it (`definition/v1`, `call_edge/v1`,
  `behavior/v1`, `absence/v1`, `history/v1`).
- `statement`: the checkable restatement (e.g. "every premise scope
  revision equals the certificate revision").
- `status`: closed vocabulary — `OPEN`, `SATISFIED`, `CONFLICTED`,
  `UNSUPPORTED`, `INVALIDATED` (frozen on acceptance; append-only after).
- `evidence`: evidence IDs that satisfy it (empty unless SATISFIED).
- `derived_from`: derivation ID that produced it (lineage, never edited).

No timestamps, no confidence scores, no free text beyond `statement`:
obligations are checkable predicates with a status, not commentary.

### Kind table (`expand_obligations`)

| Rule | Obligations (each must reach a terminal status before promotion) |
|---|---|
| definition/v1 | O1 single RANGE_READ premise bound; O2 symbol shown in cited text; O3 premise revision joins claim scope (when pinned) |
| call_edge/v1 | O1 all premises NORMALIZED_CALLSITE, unique IDs; O2 caller/callee bound in cited text; O3 premise revisions join claim scope (when pinned) |
| behavior/v1 | O1 code/runtime/mapping triple present, unique; O2 joins hold (target, repo, revision, build, execution, environment); O3 mapping authority approved |
| absence/v1 | O1 complete-empty coverage; O2 zero matches; O3 scope/config/exclusion agreement; O4 no relevant exclusion |
| history/v1 | O1 single commit-history premise; O2 full-SHA pins; O3 repo/revision/commit/file/subject equality; no locators |

`expand_obligations(rule_id, claim, premises) -> list[ProofObligation]`
is pure (frozen inputs, new objects, deterministic). Unknown rule kind
(CAUSAL and anything unregistered) expands to a single `UNSUPPORTED`
obligation — abstention, never discharge.

### Status transitions (closed)

- `OPEN -> SATISFIED`: the obligated check passed on the certified inputs.
- `OPEN -> CONFLICTED`: contradictory evidence on the same obligation
  (requires adjudication input to leave; adjudication is a new object,
  never a mutation).
- `OPEN -> UNSUPPORTED`: the obligation names a construct the kernel
  cannot establish (e.g. CAUSAL expansion, unapproved authority).
- `OPEN -> INVALIDATED`: the inputs the obligation was derived from were
  superseded (new revision, new search); a fresh expansion replaces it.
- Terminal states (`SATISFIED`, `CONFLICTED` adjudicated, `UNSUPPORTED`
  acknowledged, `INVALIDATED` replaced) are the only ones promotion reads.
  No transition ever mutates: status changes are new obligation records
  superseding the old ID lineage.

### Gate extension (`decide_publication`, additive only)

- `PublicationRequest` gains an optional `obligations` checklist input
  (absent = legacy behavior, unchanged verdicts: no existing test moves).
- New `DenyReason` member `OPEN_OBLIGATION` (frozen enum is append-only;
  no existing member changes meaning).
- Checklist order: after `NOT_CHECKER_ACCEPTED`, before
  `CONFLICT_UNRESOLVED` — an unchecked certificate denies before any
  conflict or policy reasoning.
- Rule: `VALID + any obligation not terminally resolved -> DENY
  (OPEN_OBLIGATION:...)`; `VALID + all SATISFIED/adjudicated -> COMMIT`.
- Confidence, scores, and policy inputs can never override the gate: the
  obligation check runs before `POLICY_DENIED` and takes no score input.
  (K13's "confidence bypass" clause, closed by construction.)

## Test plan (for the implementation GO, not this proposal)

- New `tests/unit/test_obligation_gating_k13.py`: expansion vectors per
  kind (obligation count/IDs/statements), transition closure (each
  transition legal/illegal pair), CAUSAL expansion to UNSUPPORTED.
- Extend `tests/unit/test_publication_decision.py`: open-obligation
  denial, satisfied-obligation commit, conflict-unadjudicated denial,
  confidence-does-not-override vector (high policy eligibility +
  VALID + open obligation still DENYs).
- Fingerprint addition: `obligations.py` joins the fingerprinted module
  set (reviewed change, manifest re-record).
- No live tests: the layer is pure (no production callers by construction,
  same as the rest of `domain/proof`).

## Explicit non-goals

- No obligation persistence (no tables, no migration): obligations are
  derived per decision from the certificate + manifest, like derivations.
  Durable obligation tracking is G5/publication-service scope, not kernel.
- No legacy-path closure (K13-B3): writer/caller audit and LWW/fast-path
  removal ride the B2/C1 rollout decisions, not this contract.
- No real-world authority (K13-B4): BEHAVIOR O3 stays unsatisfiable in
  production until the B3 reopen trigger fires; the design records the
  unsatisfiability honestly instead of discharging it.
- No second-path duplication of the expansion table in v1: the expansion
  is a single pure function (not a verdict), covered by vectors; diverse
  re-implementation applies to verdicts, and is recorded as follow-up if
  the owner wants it.

## Decision requested

1. Accept the model + kind table + status vocabulary as stated, or amend.
2. On acceptance: separate GO for the implementation (new module +
   gate extension + tests + fingerprint addition, pure domain only).
