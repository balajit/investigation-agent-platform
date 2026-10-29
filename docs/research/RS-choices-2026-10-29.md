# RS — Choices on the table (2026-10-29)

Records the options considered during the re-engineering sessions and what was selected, deferred, or ruled out — with reasons — so later readers see why, not just what.

## Ruled out

- Reasoner-driven fetching (status quo): couples recall to token spend, no feedback edge. Rejected by measurement (84 honest abstentions concentrated where proof contracts don't exist).
- Full causal discovery per incident (PC/FCI/GES/NOTEARS): sample-starved on short windows, faithfulness routinely violated. Rejected by RS-causal survey.
- Commit-subject binding: certifies agreement between derived texts while the code sits unconsulted; verifiability tracked message quality instead of repository contents. Rejected by structure, replaced by ADR-PROOF-001.
- Same-service HA standby and cross-endpoint fan-out: deferred, not rejected — no tenant needs them yet.

## Selected (with leans where measurement is pending)

- Composition order: SBFL-style trace ranking first, mechanism-shift confirmation second, propagation walk for the human path — data-gated, not merit-gated (runtime layers activate on telemetry presence).
- Hydration backing: recompute-live via the existing diff port (no new tier-2 writes), pending verify-time cost measurement.
- Hydrated content visible to the verifier only (lean), not the reasoner's formulation prompts — matches identify-not-forensics scope.
- Loud-fail = ERROR-level structured logs (not warnings, not pagers) + BLOCKED/DEFERRED lifecycle.
- Layout discovery writes default mappings directly (`managed_by: auto`, never clobbers hand edits); no certification workflow in v1.
- Tenant telemetry: service-homed endpoints, one home per service, uniqueness enforced at configure time.

## Open (need measurement, not opinion)

- Trace→range attribution coverage and retention reach per tenant (gates runtime layers).
- Metric parent-set curation cost (gates mechanism-shift ordering).
- Escalation thresholds and distill operating point (need labeled replays; battery verdicts alone are insufficient since they lack fetch histories).
- Whether hydrated hunks should reach formulation prompts (revisit if verifier-only binding proves too weak in battery re-grades).
