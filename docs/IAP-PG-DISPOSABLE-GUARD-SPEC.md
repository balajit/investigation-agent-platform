# Disposable-PostgreSQL Guard — Specification (APPROVED and IMPLEMENTED)

**Status:** Approved by owner 2026-10-22 and implemented the same day: `infrastructure/persistence/pg_guard.py` (pure verdict + per-command override with stderr audit), `migrations/env.py` write-command hook (unknown commands gated, reads/offline exempt), `tests/integration/conftest.py` autouse fixture for opt-in onboarding suites (protected URL fails loud, unreachable keeps skip), `tests/unit/test_pg_guard.py` (33 tests). Verified live: stamp refused on protected dev DB with version untouched, read commands exempt, override accepted with reason echo, real chain upgrade on disposable, full unit suite green. Implements the structural hardening agreed after the 026-stamp incident (migration verification ran against the persistent dev database behind `localhost:5452` because its URL was inherited).

**Scope:** all Alembic write commands repo-wide (`upgrade`, `downgrade`, `stamp`) plus the onboarding opt-in actual-role test suites. Out of scope: raw `psql`/`docker exec` usage (cannot be gated in code — covered by documented discipline only), non-PostgreSQL backends.

## 1. Problem

Alembic resolves its target from `-x url=` or the inherited `IAP_DATABASE_URI`; opt-in suites resolve from inherited `IAP_TEST_PG_URL`. Nothing distinguishes a disposable verification container from a persistent database (dev `:5452`, default `:5432`, deploy targets). A stamp writes version truth; an upgrade writes DDL. Both happened or could happen against the wrong database with a single inherited variable. Human discipline already failed once.

## 2. Goals / non-goals

- Goals: make accidental use structurally impossible (deny by default); keep sanctioned use working (deploy pipelines run `upgrade head` for real — see `migrations/env.py` header); give every denial an actionable message; one shared verdict implementation for both call sites.
- Non-goals: secret management; replacing the disposable-container practice (the guard enforces it, it does not create containers); gating read-only commands (`current`, `heads`, `history`, `show`, offline `--sql` rendering, `check`); gating raw CLIs.

## 3. Design

### 3.1 Single guard module

New dependency-free module, e.g. `src/investigation_agent_platform/infrastructure/persistence/pg_guard.py` (stdlib + SQLAlchemy URL parsing only — no DB imports, no I/O):

- `verdict_for_url(url: str) -> Verdict` — pure function returning `ALLOW`, `DENY_PROTECTED`, or `DENY_REMOTE` with a reason string. Pure so it is unit-testable without a database.
- `require_disposable(url: str, *, purpose: str) -> None` — raises a descriptive `RuntimeError` (alembic path) on deny; test call sites map denial to a hard failure, never a skip (skip would hide misconfiguration; unreachability keeps its existing skip behavior).

### 3.2 Call sites

1. **`migrations/env.py`, `run_migrations_online()` only** (offline mode never connects — exempt by construction): after `_resolve_url()`, before engine creation, determine the invoked command and call the guard for write commands (`upgrade`, `downgrade`, `stamp`). Read commands are exempt. Implementation note: resolve the command from the alembic config command options at env time; if the command cannot be determined, default to gating (fail closed).
2. **Onboarding opt-in suites** (all `test_*_postgres.py` files): a single autouse fixture in `tests/integration/conftest.py` calls the shared guard at fixture setup, before any engine connects (verified live: protected URL fails the run, unreachable disposable keeps the existing skip). Implemented as one shared hook rather than per-file edits so future onboarding suites are covered automatically and no in-flight test files needed touching.

### 3.3 Deny rules (evaluated in order)

1. **Unparseable or non-PostgreSQL URL → deny** (fail closed; the message names the URL sans password).
2. **Non-loopback host → deny** (`DENY_REMOTE`). Loopback = `localhost`, `127.0.0.0/8`, `::1`. Rationale: deploy targets and remote dev databases must never be reachable by accident.
3. **Loopback host with a protected port → deny** (`DENY_PROTECTED`). Initial protected set: `5432` (default), `5452` (dev). Rationale: these are the ports a disposable is least likely to use and a persistent database most likely to use.
4. **Otherwise → allow.** Any other loopback port is treated as disposable-by-convention (established practice uses 5433/5434/5544+).

### 3.4 Explicit override (mandatory, not optional)

Sanctioned flows (deploy pipelines, deliberate dev-database migration, CI service containers) must keep working. Override is a single environment variable carrying a free-text reason, e.g. `IAP_PG_GUARD_OVERRIDE="<reason>"`. Rules:

- The guard enforces non-empty reason and echoes it to stderr on every overridden invocation (audit trail in CI logs and terminals).
- The override is per-command (inline on the command line), never exported into shell profiles — documented as a hard rule; an exported override re-creates the exact hazard being removed.
- Override bypasses rules 2 and 3 but never rule 1 (garbage URLs still fail).

### 3.5 Denial message contract

Every denial prints: the refused endpoint (host:port, no credentials), which rule fired and why, the one-liner disposable flow (docker run + stamp/upgrade pattern per the 0R precedent), and the override form for sanctioned use. No stack trace as the primary signal; the `RuntimeError` carries the same text.

## 4. Protected-endpoint registry

The port set `{5432, 5452}` lives as a named constant in the guard module with a comment citing this spec. Amendments (new dev ports, CI service ports) are reviewed code changes with a test update, never silent. Host coverage beyond loopback is intentionally absent: remote hosts are denied outright, so no registry is needed for them.

## 5. Tests (no database required)

- Pure-function battery for `verdict_for_url`: disposable ports allowed; protected ports denied; remote hosts (including non-loopback IPs and hostnames) denied; unparseable/non-PG URLs denied; passwords never appear in messages.
- Override battery: empty reason rejected; non-empty reason allowed with stderr echo.
- Wiring tests: env.py hook invokes the guard for write commands and skips it for read/offline paths (mock the guard, assert call/no-call); one opt-in fixture test asserting denial surfaces as failure, not skip.
- New tests follow the repo gate: `ruff check` clean, strict `mypy` clean on the new module.

## 6. Rollout and backward compatibility

- Lands as: guard module + env.py hook + opt-in fixture hooks + this spec marked approved + ledger entry. No migration, no config change.
- Known behavior change (intended): any workflow that currently runs `alembic upgrade` against dev `:5452` (or relies on inherited URLs for verification) will now fail closed. The sanctioned replacement is the inline override with a reason, or a disposable container. CI jobs using loopback service containers on protected ports must move ports or set the override with the job name as reason.
- The existing `localhost:5452` dev database needs no change; after rollout, stamping it by accident becomes structurally impossible.

## 7. Open questions for owner review

1. Protected-port list: seed with `{5432, 5452}` as specified, or add ports now (CI service ports, other local projects)?
2. Override form: free-text reason as specified, or a ticket/approval-ID format?
3. Scope: onboarding opt-in suites in this slice and generalize the fixture hook to all PG-backed suites later, or wire every suite now?
4. Should the disposable-container flow itself be scripted (single command that starts a task-named container and exports the URL), or is the documented `docker run` pattern plus the guard sufficient?
