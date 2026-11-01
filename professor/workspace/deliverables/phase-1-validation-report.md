# Phase 1 Validation Report

**Validation date:** 2026-11-01
**Scope:** deterministic local contract, discovery, static policy checks, and recorded bounded runtime smoke evidence.

## Local Checks

| Check | Status | Evidence |
|---|---|---|
| research.md | PASS | required stages and acceptance contract are present |
| research-and-design.md | PASS | required stages and acceptance contract are present |
| code-review.md | PASS | required stages and acceptance contract are present |
| agent discovery | PASS | all seven project agents discovered with expected modes |
| static policy check: no evidence | PASS | expected PARTIAL; report limitation |
| static policy check: incomplete findings | PASS | expected PARTIAL; preserve missing acceptance criteria |
| static policy check: specialist disagreement | PASS | expected PARTIAL; preserve disagreement for adjudication |
| static policy check: adversarial rejection | PASS | expected PARTIAL; record disposition and re-evaluate |
| static policy check: validation gap | PASS | expected PARTIAL; do not deliver as fully validated |
| static policy check: unavailable search | PASS | expected BLOCKED; explain missing capability |
| static policy check: blocked specialist | PASS | expected BLOCKED; do not convert to success |
| static policy check: malformed output | PASS | expected FAILED; reject result and report failure |
| local result contract: valid completed result | PASS | completed result is accepted |
| local result contract: partial result retains limitations | PASS | partial result is not promoted to completed |
| local result contract: blocked result requires limitations | PASS | blocked result remains blocked |
| local result contract: malformed result is rejected | PASS | missing required result fields are rejected by schema fixtures |

## Runtime Checks

| Check | Status | Limitation |
|---|---|---|
| research workflow delegation | PASS | Professor delegated task RA-001 to Research Assistant using github-copilot/gpt-5.6-luna |
| research-and-design workflow delegation | NOT_VERIFIED | no provider-backed OpenCode session was executed by this local harness |
| code-review workflow delegation | NOT_VERIFIED | no provider-backed OpenCode session was executed by this local harness |
| websearch availability | NOT_VERIFIED | websearch was not exposed in the observed runtime |
| webfetch availability | PASS | Research Assistant retrieved https://opencode.ai/docs/agents/ |

## Bounded Runtime Smoke Evidence

The recorded provider-backed smoke test used `github-copilot/gpt-5.6-luna`; Professor delegated task `RA-001` to Research Assistant, which retrieved `https://opencode.ai/docs/agents/` with `webfetch`. The runtime response stated that `websearch` was not exposed. This evidence is recorded here; the local harness does not re-run the provider session.

## Interpretation

Local PASS results establish workflow structure, static failure-policy guidance, and representative result-contract decisions. They do not establish that a provider-backed model will select workflows, invoke specialists, retrieve web sources, or handle injected failures in a live session.

## Overall Status

**PASS WITH GAPS**

Research delegation and known-URL webfetch are verified only for the recorded bounded smoke test. Research and Design, Code Review, live fault injection, and websearch remain `NOT_VERIFIED`.
