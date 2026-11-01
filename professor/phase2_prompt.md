# 4. Phase 2 — Operational assurance and workflow qualification

I recommend defining Phase 2 as:

Prove that Professor can reliably execute its workflows, coordinate specialists, preserve evidence, handle failure, and produce trustworthy deliverables in actual OpenCode sessions.

Phase 2 should not be another broad prompt-writing exercise. The architecture and contracts now exist. The next step is to challenge them through execution.

## Phase 2 work packages

1. WP-2.1 — Establish a reproducible execution baseline

   Capture the exact project revision, OpenCode version, provider/model, agent discovery, available tools, and environment assumptions. Preserve session transcripts and outputs as evidence.
2. WP-2.2 — Qualify the Research workflow

   Execute a realistic research task involving source discovery, source retrieval, evidence extraction, conflicting claims, citations, and a final synthesized report.

   Explicitly test unavailable search capability and distinguish known-URL retrieval from source discovery.
3. WP-2.3 — Qualify Research and Design

   Execute a realistic engineering design task. Verify that Professor delegates research, codebase analysis, architecture design, adversarial review, and validation appropriately.

   Require alternatives, requirements traceability, architecture decisions, implementation phases, and testable acceptance criteria.
4. WP-2.4 — Qualify Code Review

   Execute an adversarial review against a real repository or controlled test repository.

   Verify source locations, execution paths, severity, evidence, root causes, remediation, regression tests, and the separation of confirmed defects from hypotheses.
5. WP-2.5 — Exercise failure and recovery

   Inject eight independent conditions:
   - No supporting evidence.
   - Incomplete specialist findings.
   - Conflicting specialist conclusions.
   - Adversarial reviewer rejection.
   - Validation failure.
   - Unavailable research capability.
   - Blocked specialist.
   - Malformed specialist response.
   Verify that Professor does not silently convert any of these into successful completion.
6. WP-2.6 — Verify result-contract enforcement

   Test whether Professor reliably validates task and result objects against the JSON schemas, preserves typed fields, identifies missing information, and rejects malformed results.

   Determine whether procedural enforcement is sufficient or whether a lightweight validation mechanism is justified. Do not introduce a persistent orchestration service without evidence that one is necessary.
7. WP-2.7 — Independent qualification and release decision

   Have the Adversarial Reviewer challenge the execution evidence. Have the Validation Agent independently evaluate acceptance criteria. Professor makes the final readiness decision.

## 5. Phase 2 exit criteria

I would require the following before calling Professor operationally qualified:

| Qualification       | Required evidence                                                       |
| ------------------- | ----------------------------------------------------------------------- |
| Research            | Successful end-to-end execution and evidence-backed deliverable         |
| Research and Design | Successful specialist chain and complete design deliverable             |
| Code Review         | Successful repository review with source-grounded findings              |
| Failure handling    | All eight failure cases independently exercised                         |
| Result integrity    | Malformed and incomplete outputs detected and correctly handled         |
| Evidence integrity  | Claims traceable to actual sources, files, commands, or session outputs |
| Adversarial review  | Independent challenge with findings adjudicated                         |
| Validation          | Independent acceptance results                                          |
| Reproducibility     | Revision, environment, model, and execution evidence recorded           |

A workflow that cannot be tested because a provider capability is unavailable must remain `NOT_VERIFIED` or `BLOCKED`, not be marked PASS.

## 6. What I would not change in Phase 2

Preserve these architectural decisions unless execution evidence demonstrates a concrete problem:

- Professor remains the sole primary agent.
- Specialists remain non-recursive.
- `subagent_depth: 1` remains.
- Prompt-gen remains a task-contract compiler, not an orchestrator.
- Code Analyst remains read-only.
- Adversarial Reviewer and Validation Agent remain independent roles.
- Session-first orchestration remains the default.
- No database, queue, daemon, or external orchestration service.
- No production-code modifications without explicit authorization.

These boundaries are valuable. Phase 2 should prove them, not casually weaken them.

