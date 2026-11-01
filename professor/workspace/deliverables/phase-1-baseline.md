# Phase 1 Baseline Assessment

**Assessment date:** 2026-11-01  
**Repository revision:** `09d954a182575e6b45cfd032044576e5ac745174`  
**Environment:** macOS; Python 3.9.6; OpenCode 1.19.3  
**Baseline status:** Captured before Phase 1 remediation. This file is a historical record and is not to be edited to reflect later changes.

## Inventory and initial configuration

The requested inventory was checked in the Professor project. The project includes `AGENTS.md`, `README.md`, `opencode.json`, seven agent Markdown files, three workflow Markdown files, four prompt Markdown files, two JSON schemas, and workspace directories for decisions, deliverables, evidence, research, and specifications. No `scripts/` directory or existing test suite was present. A `.opencode/package.json` and lockfile exist; the package manifest declares `@opencode-ai/plugin` version `1.19.3`.

Initial file sizes, as observed before edits:

| File/group | Baseline size |
|---|---:|
| `AGENTS.md` | 3,460 bytes |
| `README.md` | 8,778 bytes |
| `opencode.json` | 107 bytes |
| `.opencode/agents/adversarial-reviewer.md` | 5,453 bytes |
| `.opencode/agents/architecture-agent.md` | 0 bytes |
| `.opencode/agents/code-analyst.md` | 10,666 bytes |
| `.opencode/agents/professor.md` | 13,126 bytes |
| `.opencode/agents/prompt-gen.md` | 4,984 bytes |
| `.opencode/agents/research-assistant.md` | 5,089 bytes |
| `.opencode/agents/validation-agent.md` | 5,304 bytes |
| `.opencode/workflows/code-review.md` | 0 bytes |
| `.opencode/workflows/research-and-design.md` | 10,206 bytes |
| `.opencode/workflows/research.md` | 8,319 bytes |
| `.opencode/prompts/architecture-decision.md` | 4,699 bytes |
| `.opencode/prompts/repository-review.md` | 4,475 bytes |
| `.opencode/prompts/research-task.md` | 2,431 bytes |
| `.opencode/prompts/specification-analysis.md` | 0 bytes |
| `.opencode/schemas/agent-result.schema.json` | 3,529 bytes |
| `.opencode/schemas/task-package.schema.json` | 2,611 bytes |

`opencode.json` declares `default_agent: professor` and `subagent_depth: 1`. It does not declare a provider or model. The checked environment exposed `OPENCODE_PID` but no `OPENCODE_*`, `EXA_*`, or `PARALLEL_*` capability-enabling variables.

## Findings

| ID | Classification | Finding and evidence |
|---|---|---|
| B-01 | CONFIRMED_DEFECT | `.opencode/agents/architecture-agent.md` is empty (0 bytes), so the expected architecture specialist has no project-local instructions. |
| B-02 | CONFIRMED_DEFECT | `.opencode/prompts/specification-analysis.md` is empty (0 bytes), so the requested reusable specification-analysis task prompt is absent. |
| B-03 | CONFIRMED_DEFECT | `.opencode/workflows/code-review.md` is empty (0 bytes), leaving Professor's referenced code-review workflow without stages, finding contract, review gates, or deliverables. |
| B-04 | CONFIRMED_DEFECT | README's project layout omits `code-analyst.md` and `architecture-agent.md`, lists a non-existent `architecture-analysis.md` prompt instead of `architecture-decision.md`, omits `specification-analysis.md`, and does not document the workflow directory. |
| B-05 | CONFIRMED_DEFECT | The baseline CLI command `opencode agent list` completed on version 1.19.3, but classifies `professor`, `prompt-gen`, `research-assistant`, `adversarial-reviewer`, `validation-agent`, and `architecture-agent` as `(all)`. `code-analyst` is `(subagent)`. The project expects one primary orchestrator and six specialists; observed discovery does not conform. The CLI also lists unrelated built-in and ambient agents, so discovery output must be filtered to project expectations. |
| B-06 | POTENTIAL_DEFECT | Agent Markdown files for `professor`, `prompt-gen`, `research-assistant`, `adversarial-reviewer`, and `validation-agent` begin with a blank line before the YAML delimiter. The installed CLI’s `(all)` results correlate with these files but do not alone prove whitespace is the cause. |
| B-07 | CONFIRMED_DEFECT | The result schema's finding object only represents a generic claim, classification, confidence, rationale, impact, and source IDs. It cannot represent the complete code-review, research, architecture, adversarial-review, or validation payloads required by their workflows. |
| B-08 | POTENTIAL_DEFECT | The result schema requires top-level fields regardless of `BLOCKED`/`FAILED` status and has no typed result category or specialist-specific conditional shape; the impact on legitimate failure outputs requires fixture validation. |
| B-09 | POTENTIAL_DEFECT | The task schema requires all context fields even for `NEEDS_CONTEXT`; the current conditional only ensures a non-empty `questions` array. The intended behavior for empty/unresolved context should be tested. |
| B-10 | RUNTIME_UNVERIFIED | CLI version and discovery are observed, but provider/model selected for a running model interaction and actual specialist invocation were not established by static project configuration. No model is pinned in the project config. |
| B-11 | RUNTIME_UNVERIFIED | `research-assistant.md` permits `websearch` and `webfetch`, but the baseline did not execute either tool through the configured Research Assistant/provider. Official OpenCode tools documentation says websearch availability depends on provider or enabling environment variables; permission alone is insufficient. |
| B-12 | RUNTIME_UNVERIFIED | No structural/schema validation scripts or tests existed at baseline. Runtime workflow tests and orchestration fault-injection tests had not been run. |
| B-13 | ACCEPTABLE_DESIGN | Session-first operation, `subagent_depth: 1`, task/result schemas, explicit read-only specialist rules, and separation of adversarial review from validation are present in the starting design. These remain appropriate architectural constraints. |
| B-14 | OUT_OF_SCOPE | At baseline the parent workspace had extensive unrelated pre-existing modifications and untracked files outside the Professor project. They were not inspected as application scope and are preserved untouched. |

## Baseline verification record

| Check | Result | Evidence/limitation |
|---|---|---|
| Git revision | OBSERVED | `git rev-parse HEAD`: `09d954a182575e6b45cfd032044576e5ac745174`. |
| Worktree | OBSERVED | Parent worktree reports numerous pre-existing changes outside the Professor project; `luna_prompt.md` was untracked at baseline. No project-local code changes were present in the captured status. |
| OpenCode version | OBSERVED | `opencode --version`: `1.19.3`. |
| Agent discovery | FAIL | `opencode agent list` ran and returned results, with the mode mismatches described in B-05. |
| Project config and schemas | NOT YET VALIDATED | JSON contents were inspected; automated validation did not yet exist. |
| Available tools | PARTIAL | CLI help and current official tools docs were inspected. `websearch` is conditionally available; `webfetch` is a built-in OpenCode tool. Actual provider-bound invocation was not established. |
| Existing tests/scripts | OBSERVED | None in the project inventory. |
| Workspace structure | OBSERVED | All five expected workspace directories exist. |

## Sources and evidence type

- Project files listed above: direct repository observation at the recorded revision.
- OpenCode CLI output: direct command output from OpenCode 1.19.3 in this environment.
- OpenCode current official documentation: primary source, https://opencode.ai/docs/agents/, https://opencode.ai/docs/tools/, and https://opencode.ai/docs/config/ (retrieved 2026-11-01). The served documentation announces OpenCode v2; exact compatibility with installed 1.19.3 is not assumed without runtime validation.

This baseline records only initial-state evidence. Subsequent implementation and verification results belong in the validation and completion reports.
