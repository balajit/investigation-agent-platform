# IAP S1–S7 re-engineering — implementation plan v1.0

- Status: DRAFT
- Satisfies: `docs/requirements/REQ-investigation-loop-reengineering-v1.0.md` (G1–G4)
- Implements: `docs/architecture/IAP-investigation-architecture-v2.0.md`, `DES-measurement-v1.0.md` §4 (chunk gates)
- Codebase anchor: Temporal `RunInvestigationWorkflow` (`application/worker/workflows/run_investigation.py`), not an S1–S7 pipeline — no stage modules exist; stages are built as new domain/application components wired into the workflow's caller edges.

## 1. Dependency graph (what blocks what)

```text
P0 measurement baseline (no deps)
 └─> P1 S1 anchors (onset T / last-good G / spread budget)
      ├─> P2 S4 qualification (needs windows for runtime/contrast pinning)
      │    └─> P3 S6 binding + S5 rival bar (needs qualified rows)
      │         └─> P4 S7 handoff (needs binding outcomes; extends InvestigationConclusion.next_check)
      ├─> P5 S2 dormant templates (needs suspect path + windows; feeds frontier)
      └─> P6 collect frontier + work orders (needs failure character from P3, budget from P1, seeds from P5; replaces forcing cascade)
           └─> P7 distill brief + brief-only reason + escalation (needs running loop + usage signals)
P8 composition decision — parallel track, research-gated on live-telemetry inputs (trace attribution, retention reach, parent-set cost)
P9 telemetry Phase B/C — interleaved; BLOCKED_ON_TELEMETRY machinery needs P6 resume/work-order hooks
```

Layer rule for every phase: new semantics live in `domain/` (pure) → orchestration in `application/` → wire-up in `bootstrap/` + workflow caller edges; `ports/` protocols change only where a new capability crosses a boundary; `infrastructure/` adapters change only when a provider must supply new data. Version-pin every behavior change (claim-binding-version precedent: `resolve_*_version()`, unmarked rows keep current semantics); never env-switch, never legacy-branch.

Chunk acceptance (per `DES-measurement` §4): record current-code-shape battery baseline before the chunk; accept on re-grade movement on the targeted stages plus a negative-probe regression guard (absence behavior must not regress).

## 2. Phases

### P0 — Honest signal (enabler, no code deps)

- Fix rubric absence-awareness: pipeline-correct `INSUFFICIENT_EVIDENCE` with scoped absence proof must grade PASS (currently scores FAIL "want answered").
- Fix dispatch create-race (`CANCELLED status=missing`, ~13% of dispatches): idempotent create or client retry before battery tallies.
- Record the 105-case baseline per shape (`--shape history` precedent: 105/105 banked) that every later phase re-grades against.
- Touches: `harness/` battery + rubric, dispatch path. No domain changes.

### P1 — S1 intake anchors (`DES-intake-anchors`)

- `domain/investigation/models.py`: onset `T`, last-known-good `G` (timestamp or explicit `unknown`; defaults forbidden), spread budget on the investigation aggregate; window derivation `[G, T]` (assumed bounds marked).
- `application/knowledge/intake.py` (`ErrorIntakeService._create_investigation`, currently `time_window=(now,now)`), `application/investigation/services.py:169` (manual-create window): stamp T/G/budget at creation.
- Downstream contract: window logic treats explicit-unknown differently from assumed-now; every later phase consumes this.
- Battery movement: S1 acceptance (T/G recorded or explicit unknown; budget priced).

### P2 — S4 shape-aware qualification (`DES-S4-qualification`)

- Depends on P1 (runtime/contrast qualification needs pinned windows).
- Code rows: all four pinned fields (path, line-start, line-end, revision); start-line-only no longer ready.
- History rows: type + path + revision + scope relevance; complete-empty proofs on a separate branch (walk-completeness, searched tip, structured scope, zero matches); prose-only snippets no longer qualify.
- Runtime rows: window pinning + source identity (uses P1 windows + tenant-telemetry Phase A routing).
- Contrast pairs qualify jointly (old half ≤ G, new half ≥ T).
- Where: qualification predicates in `domain/` (new), enforced at bank time in gateway/projection path (`application/evidence/gateway.py`, `infrastructure/evidence/runtime/projection.py` mint platform IDs on qualification — keep that seam); qualification judges readiness only, never answer quality.
- Battery movement: S4 readiness passes iff qualifying evidence banked per shape.

### P3 — S6 binding + S5 rival bar (`DES-S6-binding`)

- Depends on P2 (binding consumes qualified rows + retrieval ledger, both then real).
- Binding checks: range (region in cited pinned row at cited revision), delta (change in hydrated diff/statistics), contrast (halves window-ordered, code row encodes broken assumption), mechanism-shift (metric conditional shifts across T while parents hold, where telemetry supports it).
- History verdicts: ANSWERED only on verifier confirmation (+recency proof where claimed); proven-absence INSUFFICIENT passes; unbound INSUFFICIENT routes into spread, never stop. No auto-pass (close the S6 auto-pass gap).
- Rival bar: causal conclusions name strongest rival + ruling-out evidence (`domain/hypothesis` + `VerificationEngine.evaluate_hypothesis`; `InvestigationConclusion` gains elimination record).
- Version-pin as CBV-next alongside existing `CLAIM_BINDING_*` versions.
- Battery movement: S5/S6 — history-answered only on confirmation, absence INSUFFICIENT passes, rivals named.

### P4 — S7 priced handoff (`DES-S7-handoff`)

- Depends on P3 (handoff content derives from binding/failure character).
- Terminal states: proved (+elimination record), budget-deferred (single highest-value next fetch, priced), frontier-exhausted (elimination record), cross-boundary handoff (upstream identity + localizing evidence per `ADR-BOUNDARY-001`), telemetry-deferred (resume pointer at blocked fetch).
- Discriminating-check construction from the actual gap (no generic filler); extend existing `InvestigationConclusion.next_check`; budget/telemetry-deferred states are resumable work orders re-entering at the blocked fetch.
- Battery movement: S7 — every terminal state carries next fetch or elimination record.

### P5 — S2 dormant templates (`DES-S2-dormant-templates`)

- Depends on P1 (windows for gate-state/payload/trend discriminators); feeds P6 as frontier seeds.
- Four templates per suspect path (flag-gated, data-gated, contract drift, regime crossing), each naming its discriminating fetch at birth; assumption enumeration renders every branch/guard checkable and priced against the spread budget.
- Cross-boundary templates terminate at template time as named-handoff candidates (never fetches).
- Where: `domain/` templates + hypothesis models; consumes code-layer structures (existing Tree-sitter/graph providers).

### P6 — Collect frontier + reason work orders (`DES-collect-frontier`, `DES-reason-workorders`)

- Depends on P1 (budget), P3 (failure character → spread dimensions), P5 (template seeds).
- Frontier entity (new — today `frontier` is only a loop-local BFS variable in four places): best-first over (service, file, commit, span, config) with VOI scoring, spread keyed to S6 failure character, three-exit stopping (budget/fixpoint/diminishing-returns/confidence-with-corroboration), depth/TTL caps.
- Replaces the forcing cascade in `run_investigation.py:683-1023` (`workflows/forcing.py` helpers retire as frontier subsumes each override).
- Work orders: reasoner output becomes verdicts-with-citations or hypothesis-plus-discriminating-fetch seeds back into the frontier (replaces today's `action_history` marker handoffs `_carry_ledger_directive` / `_carry_claim_gap_feed` with typed entities).
- P9 hook: `BLOCKED_ON_TELEMETRY`/`WAITING`/`DEFERRED` + backoff/heartbeat/resume land here (Phase A already logs ERROR + typed errors).
- Battery movement: G1 bound-or-priced-handoff rate; forcing-pile deletion must not regress targeted stages.

### P7 — Distill brief + brief-only reason + escalation (`DES-distill-brief`)

- Depends on P6 (needs running loop producing usage signals) — biggest architectural cut, goes last.
- Brief pipeline: rerank → sentence prune → extractive brief with citations/corroboration/dissent; operating point recall ≥ 0.95 at 5–10% tokens; faithfulness hard gate. (Today `distill` is only post-conclusion knowledge capture in `application/knowledge/capture.py` — that stays; the brief is a new in-loop component.)
- Reason plane cutover: `ReasoningCoordinator` reads brief + drill-down pointers only (today full-context); cheap-tier drafts, frontier-model escalation below confidence/above stakes, calibrated on labeled replays; tier diversity for real headroom.
- Feedback edges: brief usage signals (used/unused/wrong) reweight collector/distiller scoring.
- Battery movement: G2 recall-at-brief vs compression curve; G3 end-to-end quality-per-cost.

### P8 — Composition decision (parallel, research-gated)

- `DES-composition-decision` stays DRAFT until the three inputs are measured against live customer telemetry: trace→range attribution coverage (do spans carry filepath/line — early evidence: code location absent, trace ids present), retention reach for pre-onset estimation (~12d IAP-only retention noted), parent-set curation cost per metric family.
- Data-gating first: layers whose telemetry is absent are specified data-gated, not chosen/rejected. Code-side layers build unconditionally (P1–P6 need no telemetry); runtime layers unlock per telemetry contract.

### P9 — Telemetry Phase B/C (interleaved)

- Phase B run-log study (identifier scoreboard → readiness join-key coverage; absence inventory → ingest prerequisite spec) feeds P2 runtime qualification and P5 discriminators.
- Phase C readiness probe + `BLOCKED_ON_TELEMETRY` machinery plugs into P6 hooks. Blocked now on ops: family-wide read grant + operator mapping placement.

## 3. Optimal-path rationale

Bottom-up along the data dependency, thinnest-first: windows (P1) are consumed by everything; qualification (P2) makes readiness honest before binding judges anything; binding (P3) produces the failure character that handoff (P4) and frontier spread (P6) key on; templates (P5) are seeds the frontier (P6) consumes; the brief (P7) needs a working loop to compress and grade. Nothing is built before its consumer contract exists; every phase moves its stages on the 105-case battery or it does not land.
