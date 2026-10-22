# IAP Onboarding Sniffers — Workstream Proposal (DRAFT)

**Status:** DRAFT proposal, unapproved. Nothing here authorizes implementation. Review and owner approval are required before any sniffer code is written.

**Parent contract:** `docs/IAP-onboarding-agent-design.md` §4 (sniffer output contracts, already implemented in `application/onboarding_agent/contracts/sniffer.py`). This proposal covers the *measurement* side; the contracts stay authoritative and unchanged unless a gap is found and approved.

**Blocking dependency:** DEP-06 (redaction rules aligned with `SecretReference` shapes) must resolve before the hygiene slice (S4) starts. All other slices may proceed in parallel once this proposal is approved.

## 1. Objective

Build deterministic, read-only measurement tools that produce the §4 sniffer outputs (census, buildmap, cimapper, depscan, hygscan, sizer) from a pinned estate snapshot. Tools do all the work; the agent judges — sniffers measure, they never verdict, gate, or remediate.

## 2. Non-goals (binding on this workstream)

- No verdicts, tiers, or dispositions: extracted capabilities carry provenance only. Mapping to registry strategies is the agent's `draft_dispositions` join, not sniffer logic.
- No execution with side effects: no builds, no test runs, no network fetches, no dependency installs. Lockfiles and manifests are *read*; never resolved.
- No raw secret material downstream: redaction at the sniffer boundary, enforced again by contract validators (`HygieneFinding` already rejects unredacted secret shapes — defense in depth, not the primary control).
- No changes to approved contract shapes without a separate approved amendment. If a sniffer cannot express a finding in the current shape, that is a contract-gap finding, not license to extend the shape.

## 3. Placement (lane recommendation)

Sniffers live in a **new `application/sniffers/` package, never in `application/onboarding_agent/`**. Rationale:

- The agent package's invariant is pure functions with no I/O; sniffers are filesystem readers by nature. Co-location would break the invariant and the import-safety property (`contracts` importable by intake schemas and workflow activities without DB/Temporal/filesystem weight).
- Sniffers may depend on agent *contracts* for output shapes only — the same sanctioned direction as intake (`platform → contracts`).
- Alternative considered: `integrations/profile_gen/` (shares the embryonic `detect.py`). Rejected: profile generation is a different product surface with its own idioms; sniffers are onboarding-platform measurement tools and should evolve on the platform's cadence.

## 4. Slices

### S1 — Census (foundation; first)

File inventory + parse census → `CensusOutput`. Every other slice consumes the file inventory, so this leads.

- Parse via the already-declared `tree-sitter` + `language-pack` dependencies; grammar-unavailable languages yield `ParseStatus.GRAMMAR_UNAVAILABLE` with an explicit gap-compatible record, never silent skip.
- `totals_by_language` must reconcile with file rows and always include the `unknown` bucket (contract-enforced). Unparseable and skipped inputs are findings with reasons.
- Resource bounds (caps on file size read, per-file timeout, total output cap) are explicit constants, and hitting a cap is a recorded finding, not a silent truncation.
- Adversarial inputs are first-class test cases: symlink loops, null bytes, non-UTF-8 encodings, path-traversal-shaped names, permission-denied directories, multi-GB files.

### S2 — Buildmap, CI map, dependency scan (parallelizable after S1)

- **Buildmap** → `BuildMapOutput`. Reuse-or-replace decision required for the embryonic signal-weighted detection in `integrations/profile_gen/detect.py`: either adopt its pure detection core or document why it diverges. `tool="unrecognized"` is a finding. Source roots must resolve to enumerated directories (contract-enforced).
- **CI map** → `CiMapOutput`. Parse CI configs as text; never execute pipeline steps to discover them.
- **Dependency scan** → `DepScanOutput`. Read manifests and lockfiles; `declared_but_unlocked` / `locked_but_undeclared` / `divergent` are findings, not errors.

### S3 — Sizer

Scale and exclusion signals → `SizerOutput`. Service attribution follows buildmap modules; generated/vendor/binary paths are excluded only with a recorded `exclusion_rationale` per path class. No exclusion without a rationale entry.

### S4 — Hygiene scan (gated on DEP-06)

Secret and hygiene findings → `HygieneOutput`, read-only and redacted.

- Redaction rules come from DEP-06; until it resolves, S4 does not start. The contract validator is the backstop, not the design.
- Redaction failure fails the run, not the finding (§4.5). Uncertainty excludes the input from model context and records the exclusion as a limitation — an un-redactable input never flows downstream on the assumption it is clean.
- Fixtures use synthetic fake secrets only, clearly marked, git-ignored; never real credentials in any form.

## 5. Cross-cutting rules (all slices)

- **Determinism:** sorted collections, canonical field ordering, no timestamps inside hashed content (`produced_at` is envelope metadata only). Byte-identical reruns over identical snapshots are the acceptance test.
- **Failure taxonomy (§4):** *unsupported* input (unknown system, missing grammar) yields an explicit gap finding; *malformed or unsafe* input fails validation with a named code. Partial outputs are labeled partial, never silently completed.
- **Staging:** outputs land in designated scratch areas per the agent design's isolated-scratch rule; platform-state mutation and customer-visible side effects are prohibited.
- **No network, no subprocess execution of repo code.** If a slice cannot be built within these bounds, it returns an explicit limitation, not a workaround.

## 6. Fixtures and tests

- Extend the synthetic-checkout pattern (`scripts/build-sample-checkout.py`, git-ignored `data/`): onboarding-shaped fixtures — polyglot service, monorepo with multiple build systems, unrecognized build tool, secret-seeded hygiene cases, adversarial inputs from S1.
- Golden committed outputs per fixture; determinism tests (rerun → byte-identical); adversarial tests; redaction tests asserting no secret-shaped material in any output.
- New tests live with the sniffer package; `ruff check` clean; strict `mypy` clean on new source (following the agent-package precedent, which exceeds the repo's `mypy src` gate).

## 7. Open decisions (resolve before or during; owners in parentheses)

1. DEP-06 redaction rules and `SecretReference` alignment (agent design owner + Security) — gates S4.
2. Census v1 grammar coverage: which tree-sitter grammars ship in v1 vs. `GRAMMAR_UNAVAILABLE` (proposal owner + platform owners).
3. Resource caps: max file bytes read, per-file timeout, output row caps (proposal owner).
4. Symlink policy: follow-with-depth-cap vs. record-and-skip (proposal owner; Security consulted).
5. Runner identity: which workflow activity executes sniffers and under which grant (Phase 2 DEP-07; record the assumption, do not decide it here).
6. Reuse-vs-replace for `profile_gen/detect.py` in buildmap (proposal owner at S2 kickoff).
7. Scratch location and retention conventions for sniffer outputs (platform owners).

## 8. Acceptance and review gate

This proposal is the reviewable unit: approve the slices, placement, and decision list before any code. Per-slice acceptance is: contract-validated outputs on all fixtures, golden outputs committed, determinism + adversarial + (S4) redaction tests green, `ruff` + strict `mypy` clean, and an explicit limitations list for what the slice does not cover. Owner review pauses before S1, before S4 (DEP-06 check), and at workstream close.
