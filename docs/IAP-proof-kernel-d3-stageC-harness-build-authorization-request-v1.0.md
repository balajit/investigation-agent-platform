# D3 Stage C Harness — Implementation Authorization Request (DRAFT v1.0)

> Status: **UNBLOCKED — §2 filled by delegation 2026-10-24; §3 items 1–4 and 6 verified (§6).**
> Build may proceed strictly within §1 (dev-phase scope only). Still excluded: Stage C runs,
> Phase 1, authoritative consumption, rollout, merge, push, and any non-`dev-` run set.

## 0. Basis

- Accepted design: `docs/IAP-proof-kernel-d3-stageC-harness-design-proposal-v1.11.md` (registry `iap:d3-stagec:registry/v9`, 30 + 52 + 37 executed gates; owner review ACCEPTED 2026-10-24).
- Controlling protocol: approval package v1.9 + Amendment A1 (hierarchy v1.9+A1 > v1.9). A freeze may bind the resolved rules; it cannot override them.
- ID-01 (DEC-2026-10-24-ID, §12.1) requires this request to name the items in §2 before any build.

## 1. Requested scope

1. Implement the offline analyzer exactly as specified in proposal v1.11, validated against registry v9, with the three versioned validators and their vector suites kept byte-identical and green.
2. Deliver executed tests per §11 (schema/unit, state-ledger integration, fault/restart, read-only adapter contract, static authority scan) with pinned dependencies and recorded evidence.
3. No live tests. No collection execution. No writes to investigation state, telemetry, workflow state, dispatch, certificates, or native outcomes.

## 2. Owner-named prerequisites (ID-01 — all required before build)

| # | Item | Value (dev-phase defaults chosen by chief architect under owner delegation, 2026-10-24) |
|---|---|---|
| 1 | Approval source (authority that signs `ownerAuthorization`) | Dev-only ephemeral Ed25519 keypair generated at first-freeze time; public key pinned in the frozen key list. Valid for `dev-` run sets only. |
| 2 | Runtime attestation source (owner-approved execution wrapper emitting the signed environment manifest) | Local dev execution wrapper: records git SHA, tree hash, process-environment flag states (never run artifacts alone), tenant/database identity, dependency manifest digest; manifest signed with the dev key and bound to the `run_set_id`. |
| 3 | Permissions / access model for build and audit artifacts | Single-developer workstation; repo working tree + `.venv`; no shared credentials; no network fetches at validation time. |
| 4 | Independent audit anchor (write-once location for approvals, manifests, reports) | Local append-only `artifacts/audit/` directory with write-once file permissions. **Known dev limitation:** same-machine, not independent — must be replaced with a truly independent anchor before any non-dev use (see scope note). |
| 5 | Storage and retention location | Local workspace `artifacts/` directory (committed vectors/registries excepted via git; run outputs gitignored). |
| 6 | Retention period (ID-01 suggests study-record life plus one year) | Life of the development phase plus 90 days, then owner review before any extension or deletion. |
| 7 | Frozen key list + `key_list_ref` for the first freeze | Dev key list artifact generated at first freeze, content-hashed into the manifest digest and referenced by `key_list_ref`; rotation = new freeze. |

Dev-phase scope limitation (binding): these defaults are valid **only** for development run sets (`run_set_id` prefixed `dev-`) and for proving the analyzer mechanics. Any non-dev or Stage C use requires replacing this table with production values, re-verifying §3, and re-signing §5 — the dev audit anchor (§2 row 4) is explicitly not independent.

§2 complete by owner delegation; §5 signed 2026-10-24.

## 3. Preconditions (verified at signing)

- [x] Proposal v1.11 + registry v9 + vectors-v1.11 digests match Appendix A (see §6 item 1).
- [x] 30 + 52 + 37 gates rerun green in the build environment with pinned `jsonschema` 4.26.0 / `cryptography` 50.0.1 (see §6 item 2).
- [x] 7 decision digests recompute; A1 and approval-v1.9 byte-identical (see §6 item 4).
- [x] §2 rows 1–7 named (dev defaults by chief-architect delegation, 2026-10-24; dev-only scope limitation applies).

## 4. Explicitly out of scope

Stage C execution (requires its own authorization against a validated v1.9 §6.2i freeze), Phase 1, authoritative consumption, production rollout, merge, push, live tests, and any change to the accepted design (design changes return to the proposal + registry-identity process).

## 5. Decision

- [x] **APPROVED as stated** — owner, date: 2026-10-24. Effect: framework accepted; build may proceed strictly within §1 only after §2/§3 complete.
- [ ] **REJECTED / ALTERNATIVE** — substitute terms: ________. Effect: build stays blocked.

The 2026-10-24 date is accurate (decision recorded the same UTC day) and is retained as the approval record.

## 6. Signing-time verification record (2026-10-24, build environment)

Commands were run in the workspace root with `.venv/bin/python` (Python 3.13.15).

1. Artifact identities — all match the recorded identities (registry digest and `$id` match proposal Appendix A; vectors digest matches the ledgered v1.11 value):
   - proposal v1.11: SHA-256 `a7f105039595be56c939eef543c24a4ab4b2bc3b7ca0b28f21b4b0f819b6d74b`
   - registry `schemas-v1.10.json`: SHA-256 `4e3d6d5e82645fe3ae4dec6179badbd8d8d27c025d4d71b9f40f0f5689d1bd96`, `$id` `iap:d3-stagec:registry/v9`
   - vectors `schema-vectors-v1.11.json`: SHA-256 `4ad6672d5d06d7e1df1666a8174397599604daa63fd6384c7c4566710c5b7666`, registry `iap:d3-stagec:registry/v9`, 30 schema + 37 report cases
2. Validators rerun — output preserved verbatim:
   - `scripts/check_d3_stagec_schema_vectors.py …schemas-v1.10.json …vectors-v1.11.json` → `30 passed, 0 failed, validator=jsonschema Draft2020-12`
   - `scripts/check_d3_stagec_signatures.py` (with `-W error::SyntaxWarning`) → `52 passed, 0 failed, validator=cryptography Ed25519 ephemeral keypairs`
   - `scripts/check_d3_stagec_report.py …schemas-v1.10.json …vectors-v1.11.json` → `37 passed, 0 failed, semantic report validator R1-R8 + terminal accounting`
   - pinned versions: `jsonschema` 4.26.0, `cryptography` 50.0.1
3. Double rebuild — `scripts/build_d3_stagec_vectors.py` run twice; both outputs SHA-256 `4ad6672d5d06d7e1df1666a8174397599604daa63fd6384c7c4566710c5b7666` (byte-identical).
4. Decisions + parents — all 7 §12.1 decision digests recompute OK; Amendment A1 SHA-256 `c96ea6c8d5a97f7d75d664d4f1e109840c7a2b2316805dbbd2f629f6d4d7302f` and approval-v1.9 SHA-256 `547aee50446c0fcc674a67e5f239e15de6dcf5f73de52474f3f133ce2be965c3` both byte-identical to pinned identities.
5. §2 completeness — rows 1–7 named 2026-10-24 (dev defaults by delegation, dev-only scope). Complete; build unblocked within §1 dev scope.
6. §5 record — the checked box dated 2026-10-24 is the intended, accurate approval record and is retained.

## 7. Re-point to v1.14 (owner-signed 2026-10-24) — SUPERSEDED by §8

The owner signed the re-point of this request's artifact basis to proposal
v1.14 / registry v12 / vectors v1.14 on 2026-10-24; that basis is superseded
by the v1.15 control order-agreement delta below. §§0–7 are retained
unaltered as the signing-time record; §8 states the new basis prospectively
and awaits owner sign-off — the §7 signature does not carry forward to it.
§2 dev-phase defaults (rows 1–7) are unchanged and still apply; scope (§1
dev-only) is unchanged.

Verification rerun 2026-10-24 in the build environment
(`.venv/bin/python`, Python 3.13.15):

1. Artifact identities — registry digest and `$id` match proposal v1.14 Appendix A:
   - proposal v1.14: SHA-256 `4367eebeba1cbc0ff20db716791cf56e8f5a303693b32536642c1937f08baf90`
   - registry `schemas-v1.13.json`: SHA-256 `b1142ed9db57625762667685a416f95c9cd5014993bb025cfa8652df16093546`, `$id` `iap:d3-stagec:registry/v12`
   - vectors `schema-vectors-v1.14.json`: SHA-256 `94208d606458ef0b4de5a4e6c7a38dbc2d24a3b549a8a27b457e05d5a1e3f30b`, registry `iap:d3-stagec:registry/v12`, 32 schema + 43 report cases
2. Validators rerun — output preserved verbatim:
   - `scripts/check_d3_stagec_schema_vectors.py …schemas-v1.13.json …vectors-v1.14.json` → `32 passed, 0 failed, validator=jsonschema Draft2020-12`
   - `scripts/check_d3_stagec_signatures.py` → `52 passed, 0 failed, validator=cryptography Ed25519 ephemeral keypairs`
   - `scripts/check_d3_stagec_report.py …schemas-v1.13.json …vectors-v1.14.json` → `43 passed, 0 failed, semantic report validator R1-R8 + terminal accounting`
   - package CLI `check-case --all` over the same pair → `43 passed, 0 failed`
   - `tests/unit/test_harness_*.py` (7 files) → `146 passed`
   - pinned versions: `jsonschema` 4.26.0, `cryptography` 50.0.1
3. Double rebuild — `scripts/build_d3_stagec_vectors.py` run twice; both outputs SHA-256 `94208d606458ef0b4de5a4e6c7a38dbc2d24a3b549a8a27b457e05d5a1e3f30b` (byte-identical).
4. Parents — Amendment A1 SHA-256 `c96ea6c8d5a97f7d75d664d4f1e109840c7a2b2316805dbbd2f629f6d4d7302f` and approval-v1.9 SHA-256 `547aee50446c0fcc674a67e5f239e15de6dcf5f73de52474f3f133ce2be965c3` both byte-identical to pinned identities.

Decision:

- [x] **RE-POINT APPROVED as stated** — owner, date: 2026-10-24. Effect: the artifact basis of this request moves to proposal v1.14 / registry v12 / vectors v1.14; build may proceed strictly within §1 (dev-phase scope only) with §2/§5 of the original record intact. (Superseded by the v1.15 delta in §8; re-sign required.)

## 8. Re-point to v1.15 (pending owner sign-off)

Control order-agreement tightening (`compare_pair` and both mirrors require
the two `control_order` values to agree; `RC-control-order-mismatch` added).
No registry change (registry v12 already carries the counterpart fields).
Accepted design is now
`docs/IAP-proof-kernel-d3-stageC-harness-design-proposal-v1.15.md`,
registry `iap:d3-stagec:registry/v12`, vectors v1.15. §2 dev-phase defaults
(rows 1–7) are unchanged and still apply; scope (§1 dev-only) is unchanged.

Verification rerun 2026-10-24 in the build environment
(`.venv/bin/python`, Python 3.13.15):

1. Artifact identities — registry digest and `$id` match proposal v1.15 Appendix A:
   - registry `schemas-v1.13.json`: SHA-256 `b1142ed9db57625762667685a416f95c9cd5014993bb025cfa8652df16093546`, `$id` `iap:d3-stagec:registry/v12`
   - vectors `schema-vectors-v1.15.json`: SHA-256 `810125cc87198a7f68eb88bd8ac959b6b77264d581c2f34c1362136ada2bfcf2`, registry `iap:d3-stagec:registry/v12`, 32 schema + 44 report cases
2. Validators rerun — output preserved verbatim:
   - `scripts/check_d3_stagec_schema_vectors.py …schemas-v1.13.json …vectors-v1.15.json` → `32 passed, 0 failed, validator=jsonschema Draft2020-12`
   - `scripts/check_d3_stagec_signatures.py` → `52 passed, 0 failed, validator=cryptography Ed25519 ephemeral keypairs`
   - `scripts/check_d3_stagec_report.py …schemas-v1.13.json …vectors-v1.15.json` → `44 passed, 0 failed, semantic report validator R1-R8 + terminal accounting`
   - package CLI `check-case --all` over the same pair → `44 passed, 0 failed`
   - `tests/unit/test_harness_*.py` (7 files) → `147 passed`
   - pinned versions: `jsonschema` 4.26.0, `cryptography` 50.0.1
3. Double rebuild — `scripts/build_d3_stagec_vectors.py` run twice; both outputs SHA-256 `810125cc87198a7f68eb88bd8ac959b6b77264d581c2f34c1362136ada2bfcf2` (byte-identical).
4. Parents — Amendment A1 SHA-256 `c96ea6c8d5a97f7d75d664d4f1e109840c7a2b2316805dbbd2f629f6d4d7302f` and approval-v1.9 SHA-256 `547aee50446c0fcc674a67e5f239e15de6dcf5f73de52474f3f133ce2be965c3` both byte-identical to pinned identities.

Decision:

- [x] **RE-POINT APPROVED as stated** — owner, date: 2026-10-24. Effect: the artifact basis of this request moves to proposal v1.15 / registry v12 / vectors v1.15; build may proceed strictly within §1 (dev-phase scope only) with §2/§5 of the original record intact.
