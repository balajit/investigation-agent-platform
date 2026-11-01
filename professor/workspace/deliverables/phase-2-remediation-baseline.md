# Phase 2 Bounded Remediation G0 Baseline

**Baseline date:** 2026-11-01  
**Gate:** G0 — baseline and evidence inventory only  
**G0 decision:** `BASELINE_ACCEPTED_FOR_REVIEW; G1 NOT_STARTED`  
**Inspection scope:** `professor/` and all descendants only  
**Professor-scope Git revision/branch:** `NOT_VERIFIED`; Git metadata for the containing worktree is not treated as Professor-scope evidence.

## Dirty-State Assessment

The Professor directory was inspected as the sole scope. Observed Professor-scope state at G0:

- Untracked bounded prompt: `phase2_bounded_prompt.md`.
- G0/G1/G2 evidence and reports are present under `workspace/`.
- Current Professor scripts and G1 harness changes are present in the scoped directory.

Professor-scope Git clean/dirty attribution is `NOT_VERIFIED`. This report does not inspect or classify paths outside `professor/`.

## Runtime And Provider

| Item | Observed value | Evidence type |
|---|---|---|
| OpenCode | `1.19.3` | Direct CLI observation |
| Provider/model available | `github-copilot/gpt-5.6-luna` listed by `opencode models github-copilot` | Direct CLI observation |
| Project default agent | `professor` | `opencode.json` and discovery |
| Delegation depth | `1` | `opencode.json` |
| Agent discovery | Seven expected project agents; Professor primary, six specialists subagents | `opencode agent list --pure` |
| Web research | `webfetch` previously observed; `websearch` not exposed in prior bounded runtime evidence | Phase 2 evidence/reports |
| Environment | macOS; Python runtime used by local scripts | Prior baseline and current command environment |

No credentials, provider configuration, access controls, or external systems were changed during G0.

## Exact Files Inspected

### Governing instructions and configuration

- `AGENTS.md`
- `README.md`
- `opencode.json`
- `phase2_bounded_prompt.md`
- `phase2_prompt.md`
- `luna_prompt.md`
- `luna_iap_rev.md`

### Agents, workflows, prompts, and schemas

- `.opencode/agents/*.md` — all seven agent definitions
- `.opencode/workflows/research.md`
- `.opencode/workflows/research-and-design.md`
- `.opencode/workflows/code-review.md`
- `.opencode/prompts/research-task.md`
- `.opencode/prompts/repository-review.md`
- `.opencode/prompts/architecture-decision.md`
- `.opencode/prompts/specification-analysis.md`
- `.opencode/schemas/task-package.schema.json`
- `.opencode/schemas/agent-result.schema.json`

### Scripts

- `scripts/validate_project.py`
- `scripts/validate_schemas.py`
- `scripts/acceptance_tests.py`
- `scripts/phase2_qualification.py`
- `scripts/smoke_test.sh`

### Existing reports and evidence

- `workspace/deliverables/phase-1-baseline.md`
- `workspace/deliverables/phase-1-validation-report.md`
- `workspace/deliverables/phase-1-adversarial-review.md`
- `workspace/deliverables/phase-1-completion-report.md`
- `workspace/deliverables/phase-2-validation-report.md`
- `workspace/deliverables/phase-2-adversarial-review.md`
- `workspace/deliverables/phase-2-completion-report.md`
- `workspace/deliverables/phase-2-local-qualification.json`
- `workspace/evidence/phase2-research-session.json`
- `workspace/evidence/phase2-research-design-session.json`
- `workspace/evidence/phase2-research-design-session-retry.json`
- `workspace/evidence/phase2-code-review-session.json`
- `workspace/deliverables.zip` — scanned for credential-shaped URLs; not semantically unpacked for G0.

## Existing Qualification Status

The current Phase 2 completion report sets readiness to `NOT_READY`.

- Research: `PARTIAL`; bounded known-URL retrieval and synthesis observed.
- Research and Design: `NOT_VERIFIED`; attempts did not produce a complete final synthesis.
- Code Review: `PARTIAL / NOT_VERIFIED`; specialist chain and static review recorded, runtime qualification absent.
- Failure and recovery: `NOT_VERIFIED`; local procedural fixtures are not live injection.
- Result contracts: `PARTIAL`; local schema fixtures pass, live delegation enforcement absent.
- Independent qualification: `NOT_MET`; independent adversarial and validation reviews rejected operational qualification.

G0 does not change any of these statuses.

## Accepted Findings And Current Disposition

### P2-001 — Misleading acceptance harness results

**Location:** `scripts/acceptance_tests.py`, especially `result_contract_checks()`.

**Current disposition:** Confirmed at G0. The function returns four unconditional `PASS` checks and does not load or validate actual result fixtures. The separate `scripts/validate_schemas.py` does perform schema validation, but the acceptance harness does not connect its result-contract labels to that validator.

**Required future scope:** WP2-R1/G1 only. Separate static policy checks from schema validation and make actual validator outcomes drive status and exit code.

### P2-002 — Validation command has a report-writing side effect

**Location:** `scripts/acceptance_tests.py:142-176`.

**Current disposition:** Confirmed at G0. Running the acceptance script unconditionally writes `workspace/deliverables/phase-1-validation-report.md`, potentially overwriting an existing durable report.

**Required future scope:** WP2-R1/G1 only. Read-only validation must not overwrite reports; report generation must be explicit or versioned.

### P2-003 — Failure-policy checks are not independent

**Location:** `scripts/acceptance_tests.py:72-96`.

**Current disposition:** Confirmed at G0. The eight checks share one `policy_present` predicate based on three text phrases, so they do not independently verify their named scenarios.

**Required future scope:** WP2-R1/G1 only. Define independent static/local checks and label them accurately; live behavior remains WP2-R6/G6.

## Security Concerns

- Three credential-shaped URLs were previously found in `workspace/evidence/phase2-code-review-session.json` and sanitized with explicit approval.
- Current evidence scan found zero credential-shaped URLs across `workspace/evidence/*.json`.
- `workspace/deliverables.zip` scan found zero credential-shaped URLs, but G0 did not semantically inspect all archive contents.
- The underlying credential has not been independently confirmed revoked or rotated. That remains an owner-side external dependency.
- G0 did not access credentials or external accounts.

## Existing Tests And Actual Scope

| Command | G0 interpretation |
|---|---|
| `python3 scripts/validate_project.py` | Structural/configuration/schema fixture/discovery validation; not live workflow qualification. |
| `python3 scripts/validate_schemas.py` | Local Draft 2020-12 fixture validation with procedural code-line-range check. |
| `python3 scripts/acceptance_tests.py` | Static workflow/discovery/policy checks plus report-writing side effect; not live failure injection. |
| `python3 scripts/phase2_qualification.py` | Provider-independent expected-status fixtures plus schema validator; not live Professor behavior. |
| `sh scripts/smoke_test.sh` | Runs structural validation and acceptance checks; not an operational qualification test. |
| Phase 2 OpenCode transcripts | Bounded Research and partial/incomplete Research and Design/Code Review evidence; not complete qualification. |

## Existing Evidence And Missing Evidence

### Existing

- Four JSONL session transcripts with session IDs and tool/task events.
- Phase 1 and Phase 2 reports.
- Local schema and procedural qualification output.
- Current agent discovery output.
- Sanitized evidence-directory state.

### Missing or not verified

- Uniform per-session manifests with dirty state, file hashes, runtime capability inventory, and evidence hashes.
- Complete Research and Design final deliverable with adversarial and validation closure.
- Runtime-qualified Code Review with known revision correspondence.
- Live execution of all eight failure scenarios through the actual Professor boundary.
- Live task/result contract enforcement and malformed-result rejection.
- Independent confirmation of credential revocation/rotation.

## SHA-256 Manifest

The following manifest covers relevant Professor instructions, configuration, agent/workflow/prompt/schema files, scripts, and Phase 2 reports at G0. Hashes are computed from current Professor working-tree files, not inferred from the containing Git revision. Working-tree hashes are perishable: future edits invalidate them. For G3 and later live sessions, prefer committed blob hashes for Professor-scoped files when available, or re-verify all hashes at gate close before accepting the gate.

```text
b0be65c7de7668e54462d1ddbcd32c02fb47c68a05edbea55031093484667b82  AGENTS.md
18fe63f4f50835b5e35a6c65c18b8035c51c56b5edbe498d2dcdaffbe3c3f121  README.md
77bde2d92260c2da99218e2787f0049fdc4de1b527032249805ec05158055981  opencode.json
cfad3b23b1334ea06e9e56fbbff36d107eccf122373801a62b9dcbccd7fc0b5a  phase2_prompt.md
2e4cfa76f17c2e23a92d62290432d5fcb98bce3f9ee293ba193222b653807811  phase2_bounded_prompt.md
ed1eab921409e5e27101c9b5da86ebaf4f189af766698f0c6e94439463288e12  .opencode/agents/architecture-agent.md
2c1f5536fb6b8b9e48382c334b61fedb649829a326d91aec87c2bf6ffbb09acb  .opencode/agents/code-analyst.md
0fa39dfe341dd62a073ca26922aef1aa396e91f71e8faf3b710ca7341a41b3a4  .opencode/agents/professor.md
2fe5fbce890552b96244624729be0234e2a80acf67f6d59ea14f5bc4a9ebaea8  .opencode/agents/prompt-gen.md
04862f9ce805ddcad1b13f082314b801fd1f66c3400e25840204e7bcb380f744  .opencode/agents/research-assistant.md
7a7cc0f790e3ebe8d52be902d3648693cff6c0e1d85ae8f7dd8ef2153eb484e2  .opencode/agents/validation-agent.md
37d4c190b1f9726014a6627ded33ac85f5774f0b3736e21a2ef6c3fa4d8a5d4d  .opencode/workflows/code-review.md
353c3f90321b76a75e506052a8bb8ad29b347bb1d1e021f9ab2791c261178f9e  .opencode/workflows/research-and-design.md
0beb6016fa1b76d9462d9d4c507e91313b31ed74e73c3dd35d94b4bfdb55ce89  .opencode/workflows/research.md
26c9ef00ea140c792fe9a8a7c7115c82df4e8eddfb0a6aed41f3b77b55ef6313  .opencode/prompts/architecture-decision.md
9dcf2f8e2ee4ce3250f8d5a26dffd367689a30591ab80035c8916f33ca811  .opencode/prompts/repository-review.md
65c53b3b91c075308b0447f7cfe66285750de6512a0fb6d6b1028b735af09b62  .opencode/prompts/research-task.md
8240f3cd2307f9146152423e44bf3b3f4b4567ba6b8359e41b45515521e32e41  .opencode/prompts/specification-analysis.md
dab51751bfacadc4f3feca4281485ae7397b5d34ae51ecfe2cd0eeff92f4942c  .opencode/schemas/agent-result.schema.json
36b27c2433d57481a198d6171439e385ac4cfc83bf41b2d4cf666f7a9901f198  .opencode/schemas/task-package.schema.json
c481805578f67b6faa2cb3e26a1d497759f252be2624615549c173120b533ea5  scripts/acceptance_tests.py
127d8e1e7e85a096684b0d4e1baecae9ef3421c31f18d366194a6bc50829eac6  scripts/phase2_qualification.py
8ff5caf723c780e51ac789e044312954391e844c4d19967fba5b12a3a1300a20  scripts/validate_project.py
444e722f61c22ece11db73e001642c9471e0320f2f84ccf9bb8e982aa47ae2ab  scripts/validate_schemas.py
c8990abe71cfa2c38b38e8381f58beb1b5a721275bb8828bd2964860edc1071c  scripts/smoke_test.sh
34c830f59c19aa2ce614fc8547f4634f73f4e1af828f8c30745ec7732bebb664  workspace/deliverables/phase-2-adversarial-review.md
69eb30232421882fccd7627f23c9c061f16c60beb1604481f295a0c08a14b432  workspace/deliverables/phase-2-completion-report.md
980a71a373fe4e15ba4982b1e96cac9c6a840ee344311433335a935d365d3b28  workspace/deliverables/phase-2-validation-report.md
fe3b482ca934fbf1e329bd12266bdba8bc5749d4d6cace2ddf867c9abe6c5f05  workspace/deliverables/phase-2-local-qualification.json
```

## Proposed Bounded File Changes

No files are proposed for modification by G0 itself. G1 candidates, requiring the G0 baseline decision before execution, are:

- `scripts/acceptance_tests.py` — separate read-only validation from report generation; remove unconditional result-contract PASS labels; make policy checks independent.
- `scripts/phase2_qualification.py` — accurately classify local fixtures and connect schema results to actual validator outcomes without claiming live behavior.
- `scripts/validate_schemas.py` — only if needed to close a G1 fixture/exit-code gap discovered after baseline acceptance.
- `workspace/deliverables/phase-2-harness-remediation.md` — G1 remediation report.
- `workspace/deliverables/phase-2-remediation-change-log.md` — required change log.

G0 does not authorize or begin WP2-R2 through WP2-R8. No workflow, agent, schema, or evidence file was modified during this gate.

## Dependencies And Environmental Limitations

- The underlying credential owner must independently revoke or rotate the previously exposed credential.
- Live `websearch` remains unavailable or unverified.
- OpenCode does not provide demonstrated automatic task/result schema enforcement at the delegation boundary.
- Provider-backed Research and Design, Code Review runtime qualification, live failure injection, and per-session manifests remain future gates.
- Git state outside `professor/` was not considered; no reset, clean, stash, commit, or push was performed.

## G0 Acceptance Recommendation

**`BASELINE_ACCEPTED_FOR_REVIEW; G1 NOT_STARTED`**

The current state is sufficiently inventoried to begin G1 after an explicit gate decision. The baseline confirms the three accepted harness findings and the unresolved operational gaps. G1 should begin only after the baseline and its SHA-256 manifest are accepted; no later gate should be inferred from this report.
