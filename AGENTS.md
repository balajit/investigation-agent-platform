# AGENTS.md — investigation-agent-platform (repo scope)

This file augments the global operator contract; it does not replace it.

## tmp/ is scratch — never reference it from durable artifacts

- `tmp/` holds scratch, spikes, and working notes. It is gitignored and never ships.
- No document in `tmp/` shall be referenced in any form — no file paths, no file names,
  no "see tmp/..." pointers — in documentation (`docs/`), code, configuration, or tests.
- Whatever is needed from a `tmp/` document must be extracted and written into the actual
  artifact (doc section, code comment, test, config value) with no reference to the `tmp/` item.
- This rule applies to all new and touched content effective immediately (2026-10-21).

## No legacy logic for backward compatibility — IAP is not live

- IAP has no live deployment and no external consumers of its stored data or APIs yet. Do not carry legacy branches, compatibility shims, deprecated-flag paths, or dual-schema readers "just in case."
- When a model, schema, or behavior changes, migrate the code and its tests to the new shape directly. Prefer deleting the old path over branching around it.
- The only exception is a deliberate, time-boxed migration aid explicitly requested by the owner (e.g., a one-way backfill script), which must be removed once it has served its purpose — never left as a permanent alternate path.

## Version-pinned behavior, not env-switched legacy

- When engine semantics evolve, pin the version on the entity at creation (e.g., `metadata["verifier_version"]`) and route on the recorded version: in-flight rows keep the semantics they started with, new rows follow the new version. Unmarked rows resolve to current semantics.
- Never gate old-vs-new behavior on environment variables. Env-switched legacy branches are the exact complication this contract forbids; recorded versions are the mechanism.
