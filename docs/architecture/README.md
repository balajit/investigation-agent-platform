# docs/architecture — purpose and rules

This folder holds **system structure**: the architecture overview and Architecture Decision Records (ADRs). It says what the system is and why it is shaped that way.

## Conventions

- ADRs keep the existing `ADR-<TOPIC>-<NNN>.md` form and record context, decision, consequences, and alternatives considered.
- The versioned overview (`IAP-investigation-architecture-vX.Y.md`) is the map; ADRs are the log of turns.
- Every ADR names the requirements that motivated it (`../requirements`); every design in `../design` names the ADRs it implements.
- No customer identifiers, no operator paths, no `tmp/` references.
