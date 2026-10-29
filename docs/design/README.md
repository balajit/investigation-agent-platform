# docs/design — purpose and rules

This folder holds **component designs** that implement the architecture in `../architecture` to satisfy the requirements in `../requirements`. One design per separable piece.

## Conventions

- Filename: `DES-<kebab-topic>-v<major>.<minor>.md`.
- Lifecycle: `DRAFT → REVIEW → ACCEPTED → SUPERSEDED`. Status is recorded in the document header.
- Traceability: the header of every design lists `Satisfies:` (requirement sections) and `Implements:` (architecture decisions). A design without both links is not marked ACCEPTED.
- Versions: minor edits bump minor; contract or behavior changes bump major and supersede.
- No legacy branches, no env-switched behavior: version-pinned semantics only, per the repository contract.
- No customer identifiers, no operator paths, no `tmp/` references.
