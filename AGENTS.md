# AGENTS.md — investigation-agent-platform (repo scope)

This file augments the global operator contract; it does not replace it.

## tmp/ is scratch — never reference it from durable artifacts

- `tmp/` holds scratch, spikes, and working notes. It is gitignored and never ships.
- No document in `tmp/` shall be referenced in any form — no file paths, no file names,
  no "see tmp/..." pointers — in documentation (`docs/`), code, configuration, or tests.
- Whatever is needed from a `tmp/` document must be extracted and written into the actual
  artifact (doc section, code comment, test, config value) with no reference to the `tmp/` item.
- This rule applies to all new and touched content effective immediately (2026-10-21).
