# iap-restricted-verify

Verify IAP gaps using only IAP API surfaces. Testing and gap identification only — never a build session.

## Scope

- Purpose is restricted-mode verification: file investigations through the platform API, score the outcomes, record misses as ledger gaps.
- Zero product changes during the run: no edits to `src/`, `config/`, `tests/`, `migrations/`, `scripts/`, or `docs/` (architecture/ACQUIRED_JUDGEMENT). The sole writable file is `docs/IAP_Arch_Eng_Recommendations.log`, append-only, one line per gap.

## Allowed surfaces

- `curl` against `http://localhost:8080/api/v1` with header `X-Tenant-ID: n-a`.
- Environment preamble before any API call: `set -a && source .env.local && source ~/bin/.iap.env.local && set +a`, plus `IAP_INTAKE_SERVICE_PRINCIPALS=dev-principal` for intake calls.
- Polling only via `python3 /tmp/iap_poll.py <id> [timeout]` — no sleeps.
- Readable sub-resources: health/live, profiles, investigations CRUD + start, conclusion, findings, evidence, hypotheses, timeline, intake/errors.
- `python3` may import and run any part of the IAP codebase to aid analysis (replay a validator against banked rows, run a classifier over a question, exercise a pure helper) — strictly read-only toward the system: no repo writes, no DB mutations, no secret material in outputs.

## Forbidden in-run

`grep`/`rg`, `sed`/`awk`, `read`/`edit`/`write` on platform paths, direct database access (`psql` and friends), `docker exec`, echoing secrets or credentials. Short read-only `bash` (`ls`, `git status --short`, `curl`) is fine. Prefix shell commands with `snip` per repo convention.

## Battery + rubric

File one investigation per case (`POST /api/v1/investigations` with `application_id`, `problem_description`, `initial_context`, then `POST .../start`, poll to terminal):

- Tranche B (app `spring-sample-service`): L1 symbol lookup, L2 caller enumeration, L3 commit history, L4 typo-path correction, L5 multi-hop synthesis, L8 unanswerable (must abstain).
- Platform probes: S7 Oracle version banner, S8 self-code location, S9 hub question, S10 runtime intake with `frames` key (never `stack_frames`).

Score each: PASS (ANSWERED + expected content), PARTIAL (content banked, unverified), FAIL (absent or fabricated), ABSTAIN-OK (honest INSUFFICIENT/NO_ROOT_CAUSE on unanswerable). Close with a scoreboard: totals plus per-case `verdict — detail [short-id]`.

## Gap duty

Every miss becomes one ledger line in `docs/IAP_Arch_Eng_Recommendations.log`:

```text
[YYYY-MM-DD] (OPEN) Title — one-line detail with run id. Source: restricted-verify session.
```

Preserve run IDs, dates, counts, outcomes. Diagnose only from what API responses show; never open platform files to explain a result mid-run (python3 analysis of already-fetched data is allowed).
