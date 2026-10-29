# docs/requirements — purpose and rules

This folder holds **problem statements, goals, constraints, and acceptance criteria**. It is technology-agnostic: it says what must be true and how we will know, never how to build it.

## Conventions

- Filename: `REQ-<kebab-topic>-v<major>.<minor>.md`.
- Lifecycle: `DRAFT → REVIEW → ACCEPTED → SUPERSEDED`. Status is recorded in the document header.
- Traceability: every design in `../design` names the requirement sections it satisfies; every architecture decision in `../architecture` names the requirements that motivated it.
- Versions: minor edits (clarifications, typo fixes) bump minor; scope or acceptance changes bump major and supersede.
- No customer identifiers, no operator paths, no `tmp/` references — findings are stated generically with counts and outcomes.
