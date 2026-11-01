# Phase 2 G2 Manifest Validation Report

**Gate:** G2 / WP2-R2  
**Decision:** `REPRODUCIBILITY_ARTIFACTS_ESTABLISHED_WITH_LIMITATIONS; G3 NOT_STARTED`

**Scope:** `professor/` and all descendants only. Git metadata outside this directory was not inspected or used as Professor evidence.

## Checks

| Check | Result |
|---|---|
| `session-manifest.schema.json` parses and validates as Draft 2020-12 | PASS |
| Four manifests discovered | PASS |
| Four manifests validate against schema | PASS |
| Session IDs are unique | PASS |
| Transcript paths resolve | PASS |
| Transcript hashes match manifest values | PASS |
| Relevant file hashes match manifest values | PASS |
| Original session JSONL transcripts parse | PASS |
| Manifest JSON documents parse | PASS |
| Credential-shaped URL scan | PASS; 0 matches |
| Failed/retried sessions preserved | PASS |

Working-tree hashes are perishable. For G3 and later live sessions, manifests must prefer committed blob hashes for Professor-scoped files when available, or re-verify all working-tree hashes at gate close before accepting the gate.

## Limitations

- Manifests were generated after the original sessions.
- Session-time Professor-scope Git state and source hashes were not captured before those sessions ran.
- Git state outside `professor/` was not inspected and is outside this validation scope.
- Working-tree hashes are perishable. G3+ must use committed blob hashes where available or perform gate-close re-verification.
- Live workflow and contract qualification remains outside G2.

## G3 Boundary

G2 did not execute Research, Research and Design, Code Review, live failure handling, or live contract enforcement. Those activities remain unstarted under the bounded gate sequence.
