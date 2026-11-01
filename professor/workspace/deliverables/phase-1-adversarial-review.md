# Phase 1 Adversarial Review

**Review date:** 2026-11-01  
**Reviewed scope:** Current `professor/` project artifacts after remediation.  
**Review basis:** Independent artifact review against `luna_prompt.md`, local validation output, and the bounded runtime smoke evidence recorded in `phase-1-validation-report.md`.  
**Review mode:** Read-only. No files or external systems were changed by the reviewer.

## Overall Assessment

**ACCEPT WITH CONDITIONS**

The remediation establishes a coherent structural MVP: Professor is the only project primary, specialists are non-recursive and read-only, the three workflows contain explicit stages and gates, typed result payloads are represented, and local validation passes. The bounded runtime smoke test also demonstrates one successful Professor-to-Research Assistant delegation with `webfetch` retrieval using `github-copilot/gpt-5.6-luna`.

Phase 1 remains limited by the absence of end-to-end Research and Design and Code Review executions, unavailable `websearch`, lack of automatic result-schema enforcement at the OpenCode delegation boundary, and the fact that the local fault checks are policy-presence checks rather than live fault injection. These are documented limitations, not silently accepted as runtime success.

## Findings And Adjudication

### AR-001 — HIGH — Full workflow and fault-path runtime coverage is not demonstrated

- **Affected components:** `scripts/acceptance_tests.py`, OpenCode runtime, all three workflows.
- **Evidence:** `workspace/deliverables/phase-1-validation-report.md` records Research and `webfetch` as verified only for one bounded smoke test; Research and Design, Code Review, full specialist chains, fault injection, and `websearch` remain `NOT_VERIFIED`.
- **Impact:** The project cannot claim that every workflow selects the correct specialists or that blocked, malformed, contradictory, and rejected results are handled in a live session.
- **Adjudication:** `ACCEPTED AS RESIDUAL RISK`.
- **Remediation:** Preserve explicit `NOT_VERIFIED` status. Run provider-backed workflow and fault-path tests when an authorized runtime is available.
- **Verification criteria:** Each workflow runs with its required specialists; eight failure conditions are injected independently; outputs retain `PARTIAL`, `BLOCKED`, or `FAILED` status instead of being promoted to success.

### AR-002 — MEDIUM — Local fault checks do not inject specialist failures

- **Affected component:** `scripts/acceptance_tests.py`.
- **Evidence:** The static policy checks inspect Professor/workflow text. The local result-contract checks document decisions and rely on schema fixtures; no live Professor session is invoked.
- **Impact:** The checks can detect policy-text drift but cannot prove runtime failure propagation.
- **Adjudication:** `RECLASSIFIED` from “fault-injection test” to “static policy and local contract check.”
- **Remediation:** The script output and validation report now distinguish static/local checks from runtime checks. Add actual injected sessions only when provider-backed execution is available.
- **Verification criteria:** The report must never describe these checks as live fault injection; a future runtime harness must provide independent inputs and assertions for all eight cases.

### AR-003 — MEDIUM — Result schemas are not automatically enforced at delegation time

- **Affected components:** `.opencode/schemas/agent-result.schema.json`, `.opencode/agents/professor.md`, runtime delegation.
- **Evidence:** Professor instructions and README state that schemas are intended contracts and are not automatically enforced during an OpenCode session. The bounded smoke test did not establish schema-valid runtime output.
- **Impact:** A malformed specialist response still requires Professor’s procedural inspection and may not be rejected by an automatic boundary.
- **Adjudication:** `ACCEPTED AS DOCUMENTED LIMITATION`.
- **Remediation:** Continue using the local schema validator and explicit Professor result-evaluation procedure. Do not claim automatic enforcement without a demonstrated runtime hook.
- **Verification criteria:** A future runtime test feeds malformed, blocked, partial, and valid typed results through the actual delegation path and records the observed disposition.

### AR-004 — MEDIUM — Ordered code line ranges are enforced procedurally, not by JSON Schema alone

- **Affected component:** `.opencode/schemas/agent-result.schema.json`, `scripts/validate_schemas.py`.
- **Evidence:** The JSON Schema requires positive `line_start` and `line_end` fields but does not compare their values. `validate_schemas.py` adds the cross-field `line_end >= line_start` check.
- **Impact:** A consumer that uses only the JSON Schema can accept a reversed range.
- **Adjudication:** `ACCEPTED AS DOCUMENTED CONTRACT LIMITATION`.
- **Remediation:** Keep the application-level invariant in `validate_schemas.py` and describe it as supplemental validation rather than a Draft 2020-12-only guarantee.
- **Verification criteria:** The local validator rejects reversed ranges and accepts valid single-line and multi-line ranges; documentation does not overstate schema-only enforcement.

### AR-005 — LOW — Restricted frontmatter validation is narrower than full YAML/OpenCode semantic validation

- **Affected component:** `scripts/validate_project.py`.
- **Evidence:** The validator intentionally parses a restricted mapping shape, checks expected current permission keys/actions, and separately confirms effective CLI discovery. It is not a general YAML parser or complete OpenCode configuration validator.
- **Impact:** Future supported OpenCode frontmatter constructs outside this project’s narrow contract may be rejected or not fully type-checked.
- **Adjudication:** `ACCEPTED AS BOUNDED VALIDATION DESIGN`.
- **Remediation:** Retain the narrow contract for this small project and rely on the installed OpenCode discovery command as the compatibility check. Update the validator if project frontmatter expands.
- **Verification criteria:** Current files parse, CLI discovery succeeds, and unsupported or unsafe project permission shapes fail local checks.

### AR-006 — INFO — Code Analyst cannot execute runtime commands

- **Affected component:** `.opencode/agents/code-analyst.md`, `.opencode/workflows/code-review.md`.
- **Evidence:** Code Analyst has `bash: deny`; Code Review now states that Professor or an explicitly authorized executor must run narrowly scoped verification commands when needed.
- **Impact:** Code Analyst reviews are static-only unless Professor supplies separate runtime evidence.
- **Adjudication:** `ACCEPTED DESIGN`.
- **Remediation:** Keep the least-privilege boundary. Classify findings requiring unavailable runtime evidence as hypotheses or `NOT_VERIFIED`.
- **Verification criteria:** No Code Analyst finding claims runtime proof without captured command output and revision evidence.

### AR-007 — INFO — `websearch` is unavailable in the observed runtime

- **Affected component:** `.opencode/agents/research-assistant.md`, Research workflow.
- **Evidence:** The bounded smoke response states that `webfetch` executed successfully and `websearch` was not exposed.
- **Impact:** Research tasks that require discovering an unknown source may be `PARTIAL` or `BLOCKED` unless another authorized discovery mechanism is available.
- **Adjudication:** `ACCEPTED AS EXTERNAL RUNTIME LIMITATION`.
- **Remediation:** Keep the capability gate and never imply search execution from the permission entry alone.
- **Verification criteria:** Known-URL fetch and source-discovery tasks are reported separately; unavailable discovery remains explicit.

## Strongest Opposing Interpretation

The strongest opposing interpretation is that the project should be marked `NOT_READY` until all three workflows and all eight fault cases run end-to-end. That interpretation is valid for a fully runtime-certified release, but it is stricter than the available environment permits. The current artifacts are suitable for an MVP structural checkpoint only because the missing runtime evidence is explicitly reported and not converted into PASS.

## Scope And Limitations

- OpenCode `1.19.3` agent discovery was executed and passed for all seven expected project agents.
- The bounded runtime smoke test used `github-copilot/gpt-5.6-luna`; Professor delegated to Research Assistant, which used `webfetch` for the official agents page.
- `websearch` was not exposed in that runtime and was not used.
- Research and Design, Code Review, full adversarial/validation delegation, and live fault injection were not executed end-to-end.
- The current parent repository revision is `8d2c13fd004e746b765c681b4481b58f90247b9d`; the `professor/` project is uncommitted/untracked in the parent worktree, so no post-change commit revision exists.
- No credentials, provider configuration, or user-level OpenCode files were modified.

## Conditions For Acceptance

1. Keep the two unverified workflow families and unavailable `websearch` explicitly marked `NOT_VERIFIED`.
2. Keep local static policy checks distinct from executable runtime fault injection.
3. Keep the Code Analyst read-only boundary and Professor-owned runtime verification handoff.
4. Re-run the full provider-backed workflow suite before upgrading the readiness decision.
