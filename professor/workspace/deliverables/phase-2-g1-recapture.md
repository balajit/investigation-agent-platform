# Phase 2 Bounded Remediation G1 Recapture

**Recapture date:** 2026-11-01  
**Gate:** G1 start recapture only  
**G1 remediation status:** `NOT_STARTED`  
**Professor-scope Git revision/branch:** `NOT_VERIFIED`; this recapture considers only `professor/` and descendants.  
**OpenCode:** `1.19.3`

## Purpose

This artifact refreshes the G0 baseline for the Professor directory after the accepted G0 review verdict. It does not remediate P2-001, P2-002, or P2-003 and does not start any later gate.

## Current Professor-Scope State

- Current Professor-scope files include the bounded prompt, G0/G1/G2 artifacts, harness changes, tests, manifests, and validation scripts.
- Scope-local Git clean/dirty attribution is `NOT_VERIFIED`.

### Professor-scope untracked artifacts observed

- `phase2_bounded_prompt.md`
- `workspace/deliverables/phase-2-remediation-baseline.md`
- `workspace/evidence/phase-2-g0-sha256sums.txt`

The G1 recapture itself added no source or harness remediation changes. All statements here are limited to Professor paths.

## Runtime And Discovery

- OpenCode version: `1.19.3`.
- `opencode.json`: default agent `professor`; `subagent_depth: 1`.
- Pure discovery was executed; the expected Professor project agents remain discoverable with Professor as primary and specialists as subagents.
- Provider/model availability remains external runtime state; the previously observed model is `github-copilot/gpt-5.6-luna`.

## Evidence And Security Status

- Four Phase 2 JSONL session transcripts remain under `workspace/evidence/`.
- `phase-2-g0-sha256sums.txt` is the G0 manifest; it validates against its listed current files.
- `workspace/evidence/phase2-code-review-session.json` remains sanitized.
- Current credential-shaped URL scan: zero matches in evidence JSON files.
- `workspace/deliverables.zip` previously scanned with zero credential-shaped URL matches.
- Underlying credential owner revocation/rotation remains unverified and external to this project.

## Current Qualification Status

G0 recapture does not change the existing Phase 2 statuses:

- P2-001/P2-002/P2-003: confirmed accepted findings; remediation not started.
- Research: `PARTIAL`.
- Research and Design: `NOT_VERIFIED`.
- Code Review: `PARTIAL / NOT_VERIFIED`.
- Live failure handling: `NOT_VERIFIED`.
- Live delegation-boundary contract enforcement: `NOT_VERIFIED`.
- Phase 2 readiness: `NOT_READY`.

## Current Professor-Scope Hashes

The current working-tree SHA-256 values for the G0 artifacts are. These hashes are perishable; G3+ must prefer committed blob hashes where available or re-verify them at gate close:

```text
d596c2bc8ff74d9f27df811389a875778c40b77899aa537e370f9095293b4c8b  workspace/deliverables/phase-2-remediation-baseline.md
4716693a843494b53a36a40832ad97b885e9c7c04ab1eb766675351483d27206  workspace/evidence/phase-2-g0-sha256sums.txt
```

## G1 Start Decision

**`RECAPTURE_ACCEPTED; G1 REMEDIATION MAY START`**

This recapture satisfies the bookkeeping precondition for G1. It does not itself accept the harness for qualification, remediate any finding, or advance to G2. The next authorized action is WP2-R1/G1 harness remediation only.
