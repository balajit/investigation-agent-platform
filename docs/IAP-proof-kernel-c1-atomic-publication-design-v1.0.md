# C1 Atomic-Publication Design Proposal v1.0 (DRAFT — for owner review, no code)

Design-only. No implementation, migration, writer routing, or live run
is authorized by this document. Each needs its own explicit GO (R4
gates below).

## Position relative to G5 (read first)

The G5 atomicity proposal (`docs/IAP-proof-kernel-g5-atomicity-proposal-v1.3.md`,
DRAFT) already defines the interleave protocol end-to-end: D1 recon,
D2 operator interleave legs, D3 strict fixtures, D4 assertion bundle,
and new live tests T13–T16 in the existing live module. This C1 design
does NOT duplicate it — it defines what C1 adds on top, in a world
where the scalar promotion entry is gone:

1. **Guarded service (done, B1).** `publication_commit_service.py` now
   promotes each surviving route through `decide_guarded_promotion`
   with an operator-supplied `DecisionPolicy` and per-route trusted
   `repository_bindings`. Composition/mode logic (`single`/`all`/`any`)
   is unchanged (G5-owned semantics preserved).
2. **New C1 work item: trusted context producer.** The service needs
   `policy`, `repository_bindings`, `expected_context_id`,
   reconciliation, and eligibility from the application boundary. Some
   exist (context, reconciliation, eligibility flow through `commit()`
   today); two are new and operator-owned:
   - `DecisionPolicy` selection per tenant (shared with B2: one policy
     source, two consumers — define it once, reference it here).
   - `repository_bindings` derivation from the provider/profile
     boundary at commit time (the binding the v1.3 trust model rests
     on; forge it and the policy gate is theater).
3. **Writer routing decision (owner).** The service supports
   `single`/`all`/`any` modes; the decision is which modes go live,
   in which order, and with what rollback per mode. Proposal default:
   `single` first (one route per commit; smallest blast radius),
   `all`/`any` after `single` is proven — but this is a decision, not
   a default that activates itself.
4. **Migration decisions (owner).** `033`/`034` exist (commit tables +
   grants). v1.3 obligations need NO new tables (derived per decision,
   no persistence). Confirm: no obligation migration in C1. Any future
   lineage persistence is G5 scope with its own proposal.
5. **Production wiring.** The service has no production callers today.
   Options (decision, not either/or by default):
   - (a) Call it from the conclude path after B2 enforcing (one
     pipeline: conclude → guard → commit atomically).
   - (b) Standalone publication job over VERIFIED hypotheses (decouples
     conclusion from publication; needs idempotency + scheduling, which
     the service already has via `ALREADY_COMMITTED`).
   - (c) Keep test-only until B2 enforcing is proven, then choose.
   Recommended: (c) — the wiring decision is better made with R3b
   shadow evidence in hand.
6. **Live interleave proof.** G5 T13–T16 plus obligation-aware
   assertions: guarded COMMIT under held-lock interleave, denial on
   pre-commit evidence change with `STALE_OBLIGATION`/context reasons,
   cross-tenant policy denial, and recommit idempotency through the
   guarded entry. The D4 bundle gains the guarded-entry reason codes;
   base-module counts keep their recorded assertions.

## Explicit GOs required (R4 decomposition)

- **R4a (this doc):** accept/amend the C1 design.
- **R4b:** implement the trusted context producer + policy source
  (no live contact; unit-tested with fixture policies).
- **R4c:** writer-routing decision (`single` first or otherwise).
- **R4d:** production wiring decision ((a)/(b)/(c) above).
- **R4e:** live interleave run (staging only; G5 operator protocol +
  obligation assertions). No production data, no prod deploy.
- C1 creates no new migrations (confirmed above). If that changes, a
  migration GO is required separately.

## Verification (for the implementation GOs, not this proposal)

- Guarded service unit path: existing live-module builders + fixture
  policy/bindings promote definition routes and deny forged/unbound
  ones without a database (pure-domain subset).
- Trust-boundary test: bindings forged by the caller (wrong digest,
  extra binding, unlisted repo) deny `POLICY_DENIED` before the checker
  runs.
- Mode semantics preserved: `single`/`all`/`any` composition behavior
  unchanged from the G5-recorded baseline (the composition code path
  is untouched by the guarded migration).
- API scan: production publication callers use only the guarded entry
  (extends the C4 scan; the service is the current allowlist member).
