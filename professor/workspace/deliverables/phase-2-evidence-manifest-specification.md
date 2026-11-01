# Phase 2 Evidence Manifest Specification

**Gate:** G2 / WP2-R2  
**Status:** `PASS WITH GAPS` for existing-session manifest coverage; future live qualification remains subject to manifest creation before execution.

## Manifest Schema

The reusable schema is:

`workspace/evidence/session-manifest.schema.json`

Required groups:

- Identity: session ID, workflow, qualification test ID, operator identity, timestamps, status.
- Repository: identity, revision, branch, dirty-state status, relevant changes, SHA-256 file hashes.
- Runtime: OpenCode version, provider, model, discovered agents, capabilities.
- Execution: scenario, acceptance criteria, delegated specialists, task IDs, result statuses, failed attempts, timeouts/blocks, final disposition.
- Evidence integrity: transcript path/hash, classification, direct observations, specialist-reported claims, independent verification, sanitization.
- Qualification: expected outcome, actual outcome, status, explanation, limitations.

## Existing Session Manifests

| Manifest | Session | Workflow | Status |
|---|---|---|---|
| `phase2-research-session.manifest.json` | `ses_e6a64efcfffeGqWeJ3LHqtvpr9` | Research | `PARTIAL` |
| `phase2-research-design-session.manifest.json` | `ses_e6a63a6efffegFpTsMVIKTcmq8` | Research and Design | `NOT_VERIFIED` |
| `phase2-research-design-session-retry.manifest.json` | `ses_e6a5ed4ccffePmUFL3WbaIDc3A` | Research and Design retry | `NOT_VERIFIED` |
| `phase2-code-review-session.manifest.json` | `ses_e6a63a6edffe1Rp5eDLLAxcNwm` | Code Review | `PARTIAL` |

Failed and incomplete attempts are preserved and explicitly classified. No transcript was rewritten or deleted during G2.

## Validation Results

- Manifest schema validation: **4 manifest sets passed, 0 failures**.
- Transcript SHA-256 validation: **PASS** for all four manifests.
- Relevant Professor configuration/workflow/schema/harness hash validation: **PASS** for all manifest entries.
- Manifest JSON parsing: **PASS**.
- Original transcript JSONL parsing: **PASS**.
- Credential-shaped URL scan across evidence JSON: **0 matches**.
- Evidence sanitization status: preserved from prior approved sanitization.

The original session transcripts are JSONL event logs. The manifest files are standard multiline JSON documents; they must not be parsed as JSONL.

## Reproducibility Limitations

- Manifests were created after the sessions, so session-start dirty state and exact session-time file hashes were not captured live.
- Professor-scope Git clean/dirty state is `NOT_VERIFIED`.
- The hashes in these manifests are working-tree hashes captured at G2 and are perishable. For G3 and later live sessions, prefer committed blob hashes for Professor-scoped files when available, or re-verify all manifest hashes at gate close before accepting the gate.
- Provider/model identity is recorded from the current qualification evidence, but provider internals remain external runtime state.
- Nested specialist task events prove delegation records and returned text, not every nested specialist action. Such claims are classified `SPECIALIST_REPORTED` unless directly observed.
- No manifest changes the existing workflow qualification statuses.

## G2 Decision

**`REPRODUCIBILITY_ARTIFACTS_ESTABLISHED_WITH_LIMITATIONS; G3 NOT_STARTED`**

All existing Phase 2 qualification attempts now have resolvable manifests, transcript hashes, relevant project-file hashes, explicit statuses, and evidence classifications. The artifacts support reproducibility review but do not retroactively convert incomplete or partial sessions into PASS.
