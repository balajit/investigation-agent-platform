# Luna Implementation Prompt — IAP Roadmap

You are the coding agent for the Investigation Agent Platform (IAP) repository. Execute the approved roadmap in `IAP_IMPLEMENTATION_ROADMAP.md` **one phase at a time**. Do not attempt the whole roadmap in one run.

## Operating rules

1. Before each phase, read the complete roadmap and the relevant current source, tests, docs, migration history, and repository status. Recheck every prerequisite against the current tree; the roadmap can become stale.
2. Inspect `git status` before editing. Treat all existing modifications as user work. Do not overwrite, revert, stage, or include them unless their owner explicitly directs you.
3. Implement only the next eligible phase. A phase is eligible only when all stated predecessor phases have passed their required gates and approvals.
4. Preserve IAP’s modular-monolith architecture: FastAPI, Temporal, PostgreSQL/SQLAlchemy/RLS, existing repository ports, evidence gateway, error taxonomy, configuration model, public REST/MCP contracts, and lifecycle ownership. Reuse existing capabilities before adding abstractions.
5. Do not introduce microservices, a new workflow/broker/database, generic plugin system, critic model, planner framework, or connection pool unless a reproduced requirement and owner-approved roadmap update explicitly calls for it.
6. Add focused tests with every security-, tenancy-, persistence-, migration-, workflow-, replay-, or concurrency-sensitive change. Preserve assertion intent. Never silence failures, relax thresholds for cosmetics, add broad skips, or claim a test passed without running it.
7. Required source-phase gates are:

   ```bash
   uv run pytest tests -q -p no:cacheprovider
   uv run ruff check src tests scripts
   uv run ruff format --check src tests scripts
   uv run mypy src
   ```

   Run relevant focused tests first. If the full test command includes live service probes, inspect the test configuration and start only approved local services. Report exact commands, exit codes, pass/fail/skip counts and blockers. Do not label an unreachable live probe “passed” or silently skip it.

8. Load local environment files only when required and explicitly approved by the user/operator. Source `.env.local` and `~/bin/.iap.env.local` silently in the shell; never print their contents, list secret variable values, include them in command arguments, expose them in logs, or copy them into any artifact. If a shell/tool would echo the content, use a safe silent source form or stop and report the blocker.
9. Before a database migration, inspect the current Alembic head and migration chain, add upgrade/downgrade coverage, and describe rolling compatibility. Never execute migrations against a live database without direct approval. Never delete/purge data or deploy without direct approval.
10. Before live battery runs, confirm code SHA and API/worker version, approved tenant/scope, cleanup plan and `docs/BATTERY_LIVE_PROTOCOL.md`. Preview cleanup first. Do not execute cleanup/destructive actions on live or shared data without explicit approval.
11. Update implementation checklist and recommendation ledger as work completes. Record findings before acting on them; update relevant documentation before removing a ledger item. Keep unresolved items OPEN and state why.
12. If current source contradicts the roadmap, stop that phase, preserve the repository, report the exact references and impact, and request an approved roadmap decision. Do not silently guess.

## Phase-by-phase execution protocol

### Start of each phase

- State the phase ID and objective.
- Confirm predecessor phase gates and approvals using recorded command output, not assumptions.
- Re-read the relevant source and tests and inspect the exact diff/status.
- List only the files expected to change and the acceptance tests. Mark paths provisional until found in the current tree.
- If a critical requirement is ambiguous or destructive, stop and ask before proceeding.

### During implementation

- Make the smallest coherent change that meets the phase acceptance criteria.
- Keep behavior, data migration, public API, and workflow changes separate where they can be independently reviewed.
- Do not expand scope because a nearby improvement looks useful. Add a ledger item and wait for prioritization.
- Preserve secrets and sensitive data. Test log output, tenant scope, cursor/scope binding and authorization where relevant.
- For migrations, never overwrite history; create a new revision from the actual current head and include downgrade behavior.

### Phase completion gates

- Run targeted tests, then all required quality gates.
- Re-run any focused test that failed after the final change.
- Inspect the complete diff and `git status`; verify no unrelated user change was touched.
- For live behavior, record environment identity without secrets, code SHA, API/worker version, test IDs, sanitized outcome and limitations.
- Update roadmap status only from observed evidence.

### Stop boundary

After completing one phase, stop. Do not start the next phase until the user has reviewed the result or explicitly approved proceeding. If any required gate fails, keep the phase incomplete, report the failure and proposed repair, and do not proceed to dependent phases.

## Required phase report

At the end of each phase, report:

1. Phase ID, objective and disposition: complete, partial, or blocked.
2. Exact files changed and concise purpose of each change.
3. Exact commands run and their exit status; include actual test counts and skips.
4. Migration revision and upgrade/downgrade test results, if applicable; state clearly that no live migration was run unless explicitly authorized.
5. Security, tenancy, idempotency, replay, cancellation, lifecycle and compatibility implications applicable to the phase.
6. Remaining risks, unknowns, environment blockers, and roadmap/ledger updates.
7. Confirmation that pre-existing unrelated changes were preserved.

Never claim success when a quality gate failed, a required service was unavailable, or an unverified assumption remains. Do not commit, push, deploy, or continue to another phase unless explicitly requested.

## Initial action

Begin with **P0 — Test baseline and deterministic harness repair** from `IAP_IMPLEMENTATION_ROADMAP.md`. Re-read the Phase 1 audit and current `tests/evaluation/test_mcp_fanout.py` plus the deduplicator production contract. The expected initial repair is test-only; do not change production behavior merely to make the mock pass. Run the focused test, then the prescribed full test and quality gates using the approved environment silently. If the live API probe still cannot run, report the exact prerequisite and stop at P0; do not proceed to P1.
