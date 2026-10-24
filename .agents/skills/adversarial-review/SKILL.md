# Skill: adversarial-review

---
name: adversarial-review
description: Use when finishing a work package, before claiming a gate or milestone complete, or when asked to review design work adversarially. Don't use for routine code review, style feedback, or test-only changes.
license: MIT
metadata:
  source: https://github.com/balajit/investigation-agent-platform
  author: Thummala
---

## Quick start

Run this review against any work package *before* declaring it done. Attach the repo, the session report, and the executable test results. Trust nothing that cannot be executed or quoted. A claim is established only by an executable test asserting that exact property for the stated inputs and boundary.

## Workflow

Copy this checklist and check off items as each section produces findings or an explicit no-finding with evidence:

```text
Review progress:
- [ ] 1. Verdict calibration
- [ ] 2. Constraint-first audit
- [ ] 3. Dual-representation hunt
- [ ] 4. Mechanism-vs-property tests
- [ ] 5. Silent-fallback hunt
- [ ] 6. Forgery, tamper, boundary suite
- [ ] 7. Legacy and flag residue
- [ ] 8. Report-vs-code diff
- [ ] 9. Crack journal updated
```

1. **Verdict calibration.** Separate (a) implemented, (b) tested, (c) acceptance gate closed. Quote every report sentence using gate language (PASS, complete, verified, established) for work that is merely implemented, and downgrade it precisely.
2. **Constraint-first audit.** List every contract, schema, fixture, and interface governing the changed code. For each, determine whether it was read before implementation or reconstructed after. Quote the governing clause next to the code that must satisfy it. Treat contract-forbidden-but-code-allowed behavior as P1 even without test coverage.
3. **Dual-representation hunt.** Enumerate every fact now existing in two places (two ID computations, header vs full object, counts in docs vs suite, validation in two layers). For each pair, name the executable check forcing agreement. Agreement by proximity is not agreement.
4. **Mechanism-vs-property tests.** State each cited test's required property in one sentence, then apply the mutation test mentally: if the implementation violated exactly that property and nothing else, would this test fail? If it would still pass, mark the requirement PENDING and name the missing assertion.
5. **Silent-fallback hunt.** Grep for uncertainty becoming a value: bare `except: pass`, `or {}` / `or []` / `or ""` defaults, unmarked-to-default resolutions, `getattr(x, y, None)` chains that proceed on None, classifiers where one signal beats all others. Carve-out (narrow): fail-open is permitted ONLY for diagnostics and telemetry that cannot alter a verdict, change a persisted decision, or select a code path. When in doubt whether an output can alter a verdict, treat it as verdict-influencing. Everything else: name the uncertain state and the substituted value, justify or file.
6. **Forgery, tamper, boundary suite.** Demand adversarial twins for every new check: forged labels with valid hashes, tampered IDs, missing artifacts, empty inputs, duplicates, unknown future enum values, reordered inputs, version skew between producer and checker. Missing twin means PENDING.
7. **Legacy and flag residue.** List every env switch, kill-switch, tolerance branch, and default kept for compatibility or rollout. Each must cite a live consumer that requires it or a removal entry. Version-pinned routing on a recorded entity is allowed; env-switched legacy is not. Dead flags in docs, tests, or comments count as residue.
8. **Report-vs-code diff.** Take the three strongest claims in the session report. Quote each claim, quote the code or test allegedly establishing it, and state the gap in one sentence. Unlocatable code means fiction — say so plainly.

## Output format

No other shape is accepted:

- Verdict: one paragraph. Progress acknowledged, gates stated exactly.
- Findings table: ID | Severity (P1/P2) | Crack class (workflow §1–5) | Evidence (file:line or test name) | Required resolution (executable, checkable) | Disposition.
- Explicit non-findings: one line per checked-but-clean section stating what was checked.
- Hard rule: no finding closes on "tests pass" alone. Every closure names the test that would fail if the defect were reintroduced.

## Crack journal

Maintain the per-project journal at `tmp/adversarial-review-crack-journal.md` relative to the active workspace root (gitignored scratch, never referenced from durable artifacts). Before finishing a review, append any new failure shape discovered, one line each:

```text
- <short name>: <one-line pattern>. First seen: <session/task>.
```

Seed it from the existing journal on first use; check every new work package against every journal line. The journal is what makes this skill compound instead of going stale.
