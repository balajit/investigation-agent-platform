# IAP S1-S7 Remediation Baseline

## Evidence boundary

This is the Phase 0 baseline for source-level revalidation only. No production code, tests, migrations, public contracts, configuration, or external systems were changed. Runtime conclusions are not implied by static source inspection or unit-test results.

## Repository baseline

| Item | Observed value |
| --- | --- |
| Repository | `investigation-agent-platform` workspace |
| Branch | `iap-full-1025` |
| HEAD | `8d2c13fd004e746b765c681b4481b58f90247b9d` |
| HEAD subject | `T-E r2 grader + comparability harness + fixture keys + baseline regen, docs §110` |
| HEAD timestamp | `2026-11-01T01:16:11-04:00` |
| Working tree | Dirty; existing tracked modifications and untracked files were preserved |
| Python | `3.13.15` |
| OpenCode | `1.19.3` |
| Project configuration | `professor/opencode.json`: default agent `professor`, `subagent_depth: 1` |

The working tree included modifications to `docs/IAP_Arch_Eng_Recommendations.log`, battery scripts, verification/lifecycle source, tests, and an untracked `professor/` project tree. These were treated as pre-existing user work. The Phase 0 audit artifacts are new files under `docs/audits/`.

## Governing documents read

- `AGENTS.md`
- `professor/AGENTS.md`
- `professor/luna_iap_rev.md`
- `professor/luna_prompt.md`
- `professor/phase2_prompt.md`
- `docs/design/DES-S6-binding-v1.0.md`
- `docs/design/DES-distill-brief-v1.0.md`

## Runtime and security baseline

- OpenCode `1.19.3` is installed.
- `opencode agent list --pure` confirmed the seven project agents and their declared project modes; ambient agents were also present and are not project evidence.
- `opencode.json` does not pin a provider or model. Provider/model availability remains external runtime state.
- A safe key-name scan found credential-shaped assignments in tracked `.env.local` and `.env.example`. Values were not read or exposed. No rotation, deletion, or history rewrite was performed. The presence of `.env.local` and the category of possible credentials require owner-approved secret review and rotation/history remediation outside this Phase 0 audit.
- No live API, database, Temporal worker, provider, or end-to-end application run was started.

## Phase 0 classification summary

| WP | Classification | Release impact |
| --- | --- | --- |
| WP-01 | IMPLEMENTED; LIVE ACCEPTANCE PARTIALLY UNVERIFIED | Production composition now installs the PostgreSQL transactional UoW; fresh-context and tenant-isolation runtime evidence remain outstanding |
| WP-02 | IMPLEMENTED; LIVE REPLAY PARTIALLY UNVERIFIED | Typed qualification outcomes now fail closed; live Temporal/replay evidence remains outstanding |
| WP-03 | IMPLEMENTED; LIVE ACCEPTANCE PARTIALLY UNVERIFIED | P1: state-aware validation and classifier fail-closed handling pass focused tests; live workflow/database evidence remains outstanding |
| WP-04 | IMPLEMENTED; LIVE ACCEPTANCE UNVERIFIED | P1: v1.4 typed continuation, persistence, API, Temporal, lifecycle, and archive-then-replace paths are implemented; live database/Temporal acceptance remains outstanding |
| WP-05 | IMPLEMENTED; LIVE ACCEPTANCE UNVERIFIED | P2: cbv4 Delta, Contrast, and data-gated Mechanism-shift contracts are implemented; live provider/telemetry acceptance remains outstanding |
| WP-06 | IMPLEMENTED SLICE; LIVE ACCEPTANCE UNVERIFIED | P2: lv14 frontier selection is wired into the locatable smallest-fetch work-order slot; full forcing migration and live frontier acceptance remain open |
| WP-07 | IMPLEMENTED SLICE; QUALITY ACCEPTANCE UNVERIFIED | P2: structural EvidenceBrief projection is implemented; reranking, pruning, semantic faithfulness, recall calibration, and feedback remain open |
| WP-08 | IMPLEMENTED; LIVE ACCEPTANCE UNVERIFIED | P1 security fix: production default policy rejects instruction-like prompt injection markers and missing policies fail closed; live composition/provider proof remains open |
| WP-09 | ACCEPTANCE HARNESS FOUNDATION; LIVE SCENARIOS UNVERIFIED | P1 acceptance gap: A-I registry and explicit NOT_VERIFIED baseline exist, but no actual end-to-end S1-S7 scenario has been established as passed |

## Test evidence

The focused WP-01 through WP-06 source review ran:

```text
pytest -q tests/unit/test_h14_conclusion_unit.py tests/unit/test_h14_finding_persist_order.py tests/unit/test_s4_qualification.py tests/unit/test_s7_priced_handoff.py tests/unit/test_buffered_persistence.py tests/unit/test_f8_forcing_rules.py tests/unit/test_synthesis_forcing.py
257 passed in 1.59s

pytest -q tests/unit/test_s6_binding.py tests/unit/test_j5_newer_constructs.py tests/unit/test_frontier.py tests/unit/test_j10_frontier.py
76 passed in 1.50s
```

These are focused unit/domain/composition tests. They do not prove production-equivalent composition, live persistence, Temporal execution, provider behavior, or full S1-S7 acceptance.

## Phase 0 disposition

Phase 0 is `PARTIALLY COMPLETE` as an investigation deliverable: the requested source revalidation and evidence package are complete, but runtime qualification remains outside the evidence obtained. The project is `NOT READY` for implementation authorization because multiple P1 findings remain and WP-09 is unverified.

The mandatory stop applies. No WP-01 implementation or later phase may begin without explicit user authorization.
