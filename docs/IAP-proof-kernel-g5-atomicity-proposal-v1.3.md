# Design: G5 publication-commit atomicity completion v1.3

> Status: DRAFT proposal, 2026-10-26. Supersedes v1.2. No implementation authorization. The writer-routing decision and any production wiring remain explicit owner decisions; this proposal implements neither. Decision required before any code, migration, or live run under this proposal.

## 0. Revision note (v1.2 → v1.3)

Third read-only review confirmed the key claims against the service, repository, migration, and live-test code, and found three issues (two in the Track A companion, fixed there) plus one stale cross-reference here. Both agreed and fixed: P2 is now explicitly in the §4/D4 assertion-bundle scope (D2 already required it — the scope paragraph caught up); §11 names v1.3 as the execution candidate.

Second read-only review confirmed the v1.1 resolutions (route fixture, post-write lifecycle finding, fixture reuse, rules/v2 pinning, 16-test total) and the lifecycle-denial finding against the service code. Five remaining findings, all agreed and fixed: P1a now advances the version in the raw-SQL leg so B's read is well-defined; the multi-route `POLICY_DENIED` residual is withdrawn (call-scoped params make a later-route-only denial impossible in the current API); RLS acceptance adds `relrowsecurity` and a deployed-revision check; the assertion bundle's scope is narrowed to the new tests + operator legs with the base module's gap stated, and all bundle cross-references corrected to §4/D4; P1b states exact per-round loser assertions.

Read-only review of v1.0 found six blockers plus risks. Dispositions, each verified against the tree before rewriting:

- **Route-fixture incompatibility — DISPUTED with evidence, hardened anyway.** v1.0's artifact (ex-lines 192–200) carries all seven `RangeReadFact` fields (`premises.py:81–93`); the checker merge (`checker_semantics.py:68–84`) binds envelope + artifact into exactly the ten strict fields; the digest uses the same `canonical_dict_bytes` primitive as `_artifact_hash` (`checker_semantics.py:63–65`, `certificate.py:172–181`). The described defect matches the *existing live module's* minimal builder (`{"file_path","symbol","text"}`), not the proposal's. v1.1 still hardens: the builder imports `_artifact_hash` (no hand-rolled digest) and D1 recon runs the existing module first.
- **Unproven concurrency — AGREED.** v1.0's bare `gather` is replaced by a barrier-forced pair plus a raw-SQL held-lock operator leg; each leg states what it proves.
- **P3 missing from live tests — AGREE.** Added as T14.
- **DSN no-op (`or True`) — AGREE.** Replaced by a fail-closed host/port/db allowlist; credentials never printed.
- **P1 repeat contradiction — AGREED, and deeper.** Without lifecycle, a same-expectation replay yields `ALREADY_COMMITTED` (`committed=True`), not `STALE_VERSION` (service: version check passes at line 186, `find_commit` hits at line 199, idempotent return at 208–211). v1.1 pins lifecycle on both concurrent attempts and documents the full replay-semantics table.
- **Zero-partial-rows not operationalized — AGREE.** v1.1 defines a standard assertion bundle over all four tables plus hypothesis version/status and finding/conclusion content.
- **New finding from verification (review did not catch): lifecycle denials occur AFTER persistence.** `UNSUPPORTED_PROJECTION_STATUS` (line 271) and `LIFECYCLE_EDGE_DENIED` (line 274) return after derivation nodes and the commit row are flushed (lines 227–269); the normal return then commits them. T15/T16 encode the designed bar (zero partial rows on any denial) and are pre-registered as likely-red bug vectors with a prescribed fix shape. (v1.2 correction: the multi-route `POLICY_DENIED` sentence formerly here is withdrawn — policy inputs are call-scoped, so no later-route-only denial exists in the current API; see §5.)

## 1. Context (corrected)

G5 Composition is PASS (formal, scoped): 22/22 green via `scripts/g5_formal_driver.py`, but the executed protocol covered only the hypothesis-save CAS path. The publication path exists as reviewed design plus test-only implementation: `docs/IAP-proof-kernel-publication-commit-design-v1.0.md` (AUTHORIZED 2026-10-24, Review Stage C1+C2, guardrails: dev stack only, report before merge, stay non-authoritative), the service (no production callers), the repository helpers, and migrations `033` + `034`.

**Correction of v1.0:** the live suite `tests/integration/test_proof_publication_live.py` EXISTS (12 tests: persist/reload, K6, K5, K12, happy-path with projections, interleaved version-bump denial, evidence-append denial, conflict denial + adjudicated commit, forged/policy denial, idempotent recommit, K14, least-privilege RLS role test). The C1+C2 design §6 names it as the test plan and §5 records the RLS posture verified 2026-10-24 on dev `:5452` (`tenant_isolation` on both tables, RLS enabled + forced, head `034`, enforcement proven under `iap_test1` via `SET ROLE`). v1.0's "suite absent" claim was false. **This proposal therefore extends the existing module (new tests T13–T16 reusing its `live` fixture, `_counts`, `_lifecycle`, RLS helpers) instead of creating a parallel module.** v1.0's parallel-module code is withdrawn in full — it duplicated coverage while regressing fixture isolation, cleanup, and DSN handling the existing module already gets right.

No `docs/dev/architecture.md` exists; architecture notes live in `docs/architecture/` and `docs/IAP-architecture-v1.0.md`; no changes to those files are proposed here.

## 2. References

- `tests/integration/test_proof_publication_live.py` — base module (12 tests); extended, not replaced, by §5.
- `docs/IAP-proof-kernel-publication-commit-design-v1.0.md` — §2 schema, §3 protocol (single-transaction order), §5 RLS baseline, §6 test plan, §7 residuals (loop-evidence race stays open).
- `src/investigation_agent_platform/application/proof/publication_commit_service.py` — exact line order verified: lock (183) → `HYPOTHESIS_NOT_FOUND` (185) → `STALE_VERSION` (187) → `STALE_CONTEXT` (190) → `CONFLICT_UNRESOLVED` (193) → `find_commit` (199) → `ALREADY_COMMITTED` (208) → store/insert (227–269) → lifecycle validation (270–284) → `COMMITTED` (291).
- `src/investigation_agent_platform/domain/proof/checker_semantics.py:63–84` — `_artifact_hash` primitive + `_rebuild_input` envelope/artifact merge (the strict-parse input).
- `src/investigation_agent_platform/domain/proof/premises.py:81–93` — `RangeReadFact` closed contract (seven artifact fields + three envelope fields, `extra="forbid"`).
- `migrations/versions/033_proof_publication_commit.py` + `034_proof_tables_grants.py` — tables, `tenant_isolation` ENABLE+FORCE, least-privilege grants to `iap_app`/`iap_worker`/`iap_test1`.
- `docs/IAP-proof-kernel-g5-formal-protocol-v1.0.md` — Part A preconditions (app role, RLS forced, single-writer grep) reused verbatim; Cases 1–4 shape the operator legs.
- `docs/IAP-L2-D3-fix-test-procedure.md` §2 — canonical staging identity (`localhost:5452/iap`, host/port/db display only, never credentials).
- `docs/IAP-proof-kernel-g4-disposition-package-v1.0.md` — decided G4 PARTIAL; §4 scopes this proposal as G5-owned with the exact non-claim footnote carried in §8.

## 3. Goals / Non-Goals

**Goals:**

- Recon-first verification of the C1+C2 implementation state (§4, zero code).
- Operator interleave protocol for the publication path on staging: held-lock proof (P1a), barrier-forced service pair (P1b), pre-commit evidence-change (P2, renamed — it is not a race), cross-tenant denial (P3).
- Four new live tests T13–T16 in the existing module reusing its fixture and helpers; the new tests and the P1b/P3 operator legs assert the §4/D4 assertion bundle (the base module's 12 tests keep their recorded count assertions — gap stated in §4/D4, not re-litigated here).
- Lifecycle projection verification including content equality and the DRAFT/unsupported-status vectors.
- Exit criteria mapping exactly what closes G5 atomicity and what stays open.

**Non-Goals:**

- Writer routing (`single`/`all`/`any` over live routes), authority routing, production wiring, rollout — separate explicit owner decisions.
- New premise types, rule changes, checker changes, canonicalization changes.
- Serializing loop-evidence appends against commits (C1+C2 §7 residual, restated in §9).
- `mode="all"`/`"any"` multi-route calls (out of scope, §9) — gate stays `mode="single"`.
- G5 PASS declaration itself — the gate owner declares it against the exit criteria, not this document.

## 4. Decisions

### D1: Recon-first verification (read-only; any red item stops the proposal)

Run in order; record outputs. Any deviation from the expected values stops all work until the owner rules.

```bash
# 1. Base module present with 12 tests (corrects v1.0's "absent" claim)
ls tests/integration/test_proof_publication_live.py
grep -c "^async def test_\|^def test_" tests/integration/test_proof_publication_live.py
# expect: 12 (11 async + k14 sync)

# 2. Reason-code coverage of the base module (map each CommitResult site to a test)
grep -n "STALE_VERSION\|STALE_CONTEXT\|CONFLICT_UNRESOLVED\|POLICY_DENIED\|ALREADY_COMMITTED\|HYPOTHESIS_NOT_FOUND\|LIFECYCLE_EDGE\|UNSUPPORTED_PROJECTION\|COMMIT_RACE_LOST" \
  src/investigation_agent_platform/application/proof/publication_commit_service.py
# expect inventory: pre-write denials (183-226) + ALREADY_COMMITTED (208) +
#   post-write lifecycle denials (270-284) + COMMIT_RACE_LOST handler (254-266).
# Map: existing tests cover STALE_VERSION (sequential), STALE_CONTEXT,
#   CONFLICT_UNRESOLVED, POLICY_DENIED (single), EVALUATION_FAILED,
#   ALREADY_COMMITTED (idempotent), HYPOTHESIS_NOT_FOUND (NOT covered -> T14),
#   concurrent STALE_VERSION (NOT covered -> T13), LIFECYCLE_EDGE_DENIED /
#   UNSUPPORTED_PROJECTION_STATUS (NOT covered -> T15/T16, likely red per §0).

# 3. Transaction ownership: one rls_session, commit-on-exit, rollback-on-exception,
#    explicit rollback only on the IntegrityError path
grep -n "rls_session\|session.rollback\|await session.commit" \
  src/investigation_agent_platform/application/proof/publication_commit_service.py \
  src/investigation_agent_platform/infrastructure/persistence/rls.py
# expect: single `async with rls_session(...)` (service line 182); no explicit
#   commit in service (rls_session commits on clean exit, rolls back on
#   exception); one explicit rollback at service line 255 (unique-race path).

# 4. Version check precedes ALL writes (the loser's zero-write guarantee)
# expect order: lock 183, version 186-187, digest 188-190, contradiction
#   191-193, find_commit 199, store 227, insert 245 — reads show no store/insert
#   before line 227.

# 5. Route-shape pin: shared hash primitive + registry-pinned rule fields
grep -n "canonical_dict_bytes" \
  src/investigation_agent_platform/domain/proof/checker_semantics.py \
  src/investigation_agent_platform/domain/proof/certificate.py
# expect: both hash via canonical_dict_bytes (no second primitive).
uv run python -c "from investigation_agent_platform.domain.proof.rules import RULE_REGISTRY; r=RULE_REGISTRY['definition/v1']; print(r.rule_version, r.implementation_digest)"
# record rule_version + digest; §4/D3 pins these from the registry (never hardcoded).

# 6. RLS catalog + role posture + deployed revision on staging (exact queries,
#    acceptance in D5 exit)
# -- relrowsecurity AND relforcerowsecurity true on both tables (enabled AND forced);
# -- policy predicate keys app.tenant_id;
# -- rolbypassrls=false AND rolsuper=false for iap_app, iap_worker, iap_test1;
# -- current_user during the run is the least-privilege role, never a superuser;
# -- alembic_version is 034_proof_tables_grants (deployed head, not just migration
#    source present).

# 7. Base-module baseline: run the existing 12 tests on staging first
IAP_LIVE_TESTS=1 uv run python -m pytest -q -p no:cacheprovider tests/integration/test_proof_publication_live.py
# expect: 12 passed. If the happy-path/K5 tests fail with COMPOSITION_DENIED /
#   INADMISSIBLE_PREMISE, the base module's minimal route builder predates the
#   strict RangeReadFact contract: update its _definition_route to the §4/D3 strict
#   shape as pre-work (owner-notified), then re-run to 12 green before adding T13-T16.
```

**Alternative considered:** Skip recon and write tests directly. Rejected — v1.0 did that and shipped a false "suite absent" claim plus an unproven concurrency test.

**Rationale:** Every line of §5 binds an API recon pins first; a drifted tree fails loudly at D1 instead of mid-run.

### D2: Commit-path interleave operator protocol (staging only)

G5 formal-protocol preconditions (§1.1–1.4) apply unchanged, plus migration head includes 033/034 and the D1 item-6 role checks. One tenant `T`, one investigation `I`, one hypothesis `H` at version `N`, one valid strict definition route, expectation pinned to `(H, N, digest(I))`.

```text
Case P1a — the lock exists (raw SQL, proves serialization; the service cannot
hold a transaction open, so the operator proves the lock here, once).
  Session A and B (app role, SET LOCAL app.tenant_id='T'); both read (H,N,D).
  A: BEGIN; SELECT ... FOR UPDATE on H (holds the row lock; do NOT commit).
  B: same SELECT ... FOR UPDATE -> blocks on the row lock (observed block =
     the serialization mechanism the service relies on).
  A: UPDATE hypotheses SET version = N+1 WHERE id=H AND tenant_id='T'
     (mirrors the service's version advance; WITHOUT this advance B's
     post-commit read is meaningless) then COMMIT.
  B unblocks -> reads version N+1, i.e. the state a service-level winner
  leaves behind; B's own transaction then ROLLBACKs (no write).
  PASS BAR: B demonstrably blocked until A committed (timestamps); B reads
  N+1 after unblocking; no other writer on the proof tables during the window
  (re-grep writer audit). Narrower claim stated honestly: P1a proves lock
  serialization + version visibility, not service semantics — those are P1b's.

Case P1b — barrier-forced service pair (proves the outcome under contention).
  R = 10 pre-registered rounds. Per round: fresh (I,H) fixture + fresh
  expectation (H,N,D); two coroutines rendezvous on asyncio.Barrier(2), then
  each calls service commit WITH the §5 lifecycle projection.
  Per-round bar: exactly one COMMITTED + exactly one STALE_VERSION (order-free;
  the row lock, proven by P1a, forces the loser through the version check
  after the winner's lifecycle advance to N+1).
  Cumulative tally: 10 COMMITTED + 10 STALE_VERSION, zero partial rows.
  Per-round assertions, stated exactly (the contention loser here is
  STALE_VERSION, not a replay — replays are the separate table below):
  winner reason COMMITTED with lifecycle projections; loser reason
  STALE_VERSION; global `_counts == (1,1,1,1)`; hypothesis reads (N+1,
  VERIFIED). The loser's zero-write claim is proven by the totals: exactly
  one COMMITTED plus (1,1,1,1) plus a single version bump entails the loser
  wrote nothing — any loser write would add rows or a second bump. Loser
  status/version is not read separately because the loser holds no identity
  apart from "the non-winner"; the global state is the assertion.
  State R=10 before starting; changing R mid-run voids the run.

Replay-semantics table (fixed expectation, sequential replays — NOT interleaves):
  expectation (H,N,D) + lifecycle,    after winner at N+1 -> STALE_VERSION.
  expectation (H,N,D), NO lifecycle,  same route/derivation, after winner
    -> ALREADY_COMMITTED with committed=True, no new rows (find_commit hit).
  expectation (H,N,D2') where D2' recomputed after evidence change
    -> COMMITTED (fresh context), except same derivation+policy
    -> ALREADY_COMMITTED (K12 covered by existing test_k12_policy_version).
  v1.0's "1 COMMIT + 49 STALE_VERSION" claim is withdrawn: without lifecycle
  rounds 2..50 return ALREADY_COMMITTED, and a replay is not an interleave.

Case P2 — pre-commit evidence-change denial (RENAMED; v1.0's "evidence race"
  overstated: it inserts evidence before calling commit, testing the digest
  check, not the race window).
  1. Read (H,N,D). 2. Insert one evidence row for I. 3. Commit -> STALE_CONTEXT.
  PASS BAR: STALE_CONTEXT; §4/D4 bundle all-zero; H version/status unchanged.
  The concurrent append/commit race REMAINS OPEN (C1+C2 §7 residual): an
  optional operator note may loop concurrent appends against commits and record
  that every outcome is COMMITTED-or-STALE_CONTEXT with no partial rows, but a
  probabilistic loop is NOT a gate and its tally proves nothing about the window.

Case P3 — cross-tenant commit denied (operator + T14).
  Same commit under app.tenant_id='OTHER' -> HYPOTHESIS_NOT_FOUND (row lock
  finds no row under the wrong tenant). Verify under T: §4/D4 bundle all-zero,
  H version/status unchanged. No row touched under T, none created under OTHER.
```

**Alternative considered:** Reuse the hypothesis-path Cases 1–4 as sufficient. Rejected — they cover the premise row, not the commit row, derivation rows, or projections.

**Rationale:** P1a proves the mechanism, P1b proves the service outcome under forced contention, P2/P3 prove the context and tenant denials. Each leg states its own claim so no leg is asked to prove what it cannot.

### D3: Four new live tests extending the existing module (T13–T16)

**Decision:** Extend `tests/integration/test_proof_publication_live.py` in place. Reuse: the `live` fixture (per-test engine, before/after `_clean_tenant` — the fixture-isolation and cleanup v1.0 lacked), `_pg_url()`, `_investigation`, `_hypothesis`, `_evidence`, `_expectation`, `_counts` (4-tuple over commits/nodes/findings/conclusions by investigation), `_lifecycle` (full finding/conclusion content). Add:

```python
import asyncio
from urllib.parse import urlparse

# Fail-closed staging identity (replaces v1.0's no-op `or True` assertion).
# Canonical staging per docs/IAP-L2-D3-fix-test-procedure.md §2.
_ALLOWED_HOSTS = {"localhost", "127.0.0.1"}
_ALLOWED_PORT = 5452
_ALLOWED_DBS = {"iap"}


def _pg_url() -> str:  # extends the existing helper; keep its name/override order
    url = os.environ.get("IAP_PROOF_TEST_PG_URL") or os.environ.get("IAP_DATABASE_URI", "")
    if not url:
        pytest.skip("no Postgres URL (IAP_DATABASE_URI / IAP_PROOF_TEST_PG_URL)")
    if url.startswith("postgresql://"):
        url = "postgresql+asyncpg://" + url[len("postgresql://") :]
    parts = urlparse(url)
    if (
        parts.hostname not in _ALLOWED_HOSTS
        or (parts.port or 5432) != _ALLOWED_PORT
        or (parts.path or "").lstrip("/") not in _ALLOWED_DBS
    ):
        pytest.fail(
            "refusing live run: target is not the staging dev stack "
            f"(host={parts.hostname}, port={parts.port}, db={(parts.path or '').lstrip('/')}); "
            "point IAP_PROOF_TEST_PG_URL at localhost:5452/iap"
        )
    return url
```

```python
# Strict route builder: identical shape to test_certificate_checker._definition_setup
# (all seven RangeReadFact artifact fields; envelope-only premise; digest via the
# kernel's own _artifact_hash, never a hand-rolled hash).
def _strict_definition_route(symbol: str = "OrderService") -> RouteInput:
    from investigation_agent_platform.domain.proof.certificate import _artifact_hash
    from investigation_agent_platform.domain.proof.manifest import build_manifest

    window = "class OrderService:\n    pass\n"
    artifact = {
        "file_path": "svc.py",
        "repository": "synthetic/service",
        "revision": "rev-live",
        "line_start": 10,
        "line_end": 14,
        "target": symbol,
        "window_text": window,
    }
    digest = _artifact_hash(artifact)
    manifest = build_manifest(["row-1"], {"row-1": digest}, premise_types={"row-1": "RANGE_READ_FACT"})
    proposition = {
        "canon_version": "canon/v1",
        "completeness": "PARTIAL_OK",
        "form": "EXISTENCE",
        "modality": "STATIC",
        "predicate": f"defined({symbol})",
        "quantifier": "EXISTENTIAL",
        "relation": "CALL_EDGE",
        "scope": {"configuration": "default", "execution": "n/a", "revision": "rev-live"},
        "target": {"file_path": "svc.py", "symbol": symbol},
    }
    premise = {
        "content_hash": digest,
        "evidence_id": "row-1",
        "premise_type": "RANGE_READ_FACT",
        "scope": {"revision": "rev-live"},
    }
    rule = RULE_REGISTRY["definition/v1"]
    return RouteInput(
        proposition=proposition,
        manifest=manifest,
        premises=[premise],
        artifacts={"row-1": artifact},
        rule_bundle_version="rules/v2",
        rule_id=rule.rule_id,
        rule_version=rule.rule_version,
        implementation_digest=rule.implementation_digest,
    )
```

```python
async def _hypothesis_version_status(live: Any, hyp_id: UUID) -> tuple[int, str]:
    async with rls_session(live.factory, TENANT) as session:
        row = (await session.execute(
            select(HypothesisORM).where(
                HypothesisORM.id == hyp_id, HypothesisORM.tenant_id == TENANT)))
        row = row.scalars().first()
        assert row is not None
        return int(row.version), str(row.status)


async def _hypothesis_with_status(live: Any, inv_id: UUID, status: str) -> Any:
    async with rls_session(live.factory, TENANT) as session:
        row = HypothesisORM(
            tenant_id=TENANT, investigation_id=inv_id, title="probe",
            description="probe", statement="defined(OrderService)", status=status)
        session.add(row)
        await session.flush()
        await session.refresh(row, attribute_names=["id", "version", "status"])
        return row


@pytest.mark.asyncio
async def test_T13_concurrent_commits_with_lifecycle_one_winner(live: Any) -> None:
    inv_id = await _investigation(live)
    hyp = await _hypothesis(live, inv_id)
    await _evidence(live, inv_id, "fp-t13", "k-t13")
    exp = await _expectation(live, hyp)
    svc = ProofPublicationService(live.factory)
    barrier = asyncio.Barrier(2)

    async def _raced():
        await barrier.wait()
        return await svc.commit(
            TENANT, inv_id, exp, [_strict_definition_route()], lifecycle=_lifecycle())

    first, second = await asyncio.gather(_raced(), _raced())
    winners = [r for r in (first, second) if r.committed]
    losers = [r for r in (first, second) if not r.committed]
    assert len(winners) == 1 and len(losers) == 1, (first.reason, second.reason)
    assert winners[0].reason == "COMMITTED", winners[0].reason
    assert losers[0].reason == "STALE_VERSION", losers[0].reason
    assert await _counts(live, inv_id) == (1, 1, 1, 1)
    assert await _hypothesis_version_status(live, hyp.id) == (2, "VERIFIED")
    # Content: the projected rows carry the lifecycle values for THIS investigation.
    async with rls_session(live.factory, TENANT) as session:
        finding = (await session.execute(
            select(FindingORM).where(FindingORM.investigation_id == inv_id))).scalars().one()
        conclusion = (await session.execute(
            select(InvestigationConclusionORM).where(
                InvestigationConclusionORM.investigation_id == inv_id))).scalars().one()
    assert (finding.title, finding.statement) == ("OrderService defined", "OrderService is defined")
    assert conclusion.root_cause == "OrderService is defined"
    assert conclusion.status == "ROOT_CAUSE_ESTABLISHED"


@pytest.mark.asyncio
async def test_T14_cross_tenant_commit_denied(live: Any) -> None:
    inv_id = await _investigation(live)
    hyp = await _hypothesis(live, inv_id)
    await _evidence(live, inv_id, "fp-t14", "k-t14")
    exp = await _expectation(live, hyp)
    svc = ProofPublicationService(live.factory)
    denied = await svc.commit(
        "proof-commit-foreign", inv_id, exp, [_strict_definition_route()])
    assert denied.committed is False
    assert denied.reason == "HYPOTHESIS_NOT_FOUND", denied.reason
    assert denied.derivation_ids == ()
    assert await _counts(live, inv_id) == (0, 0, 0, 0)
    assert await _hypothesis_version_status(live, hyp.id) == (1, "SUPPORTED")


@pytest.mark.asyncio
async def test_T15_draft_status_lifecycle_denied(live: Any) -> None:
    # DESIGNED BAR (likely red — see §0/§9): lifecycle denials must write nothing.
    # Code inspection shows LIFECYCLE_EDGE_DENIED returns after persistence
    # (service lines 227-269 before 270-284); a red run is a service bug report
    # (prescribed fix: hoist new_status + _COMMITTABLE_FROM validation to
    # immediately after the row lock), not a test failure to "fix" by weakening.
    inv_id = await _investigation(live)
    hyp = await _hypothesis_with_status(live, inv_id, "DRAFT")
    await _evidence(live, inv_id, "fp-t15", "k-t15")
    exp = await _expectation(live, hyp)
    svc = ProofPublicationService(live.factory)
    denied = await svc.commit(
        TENANT, inv_id, exp, [_strict_definition_route()], lifecycle=_lifecycle())
    assert denied.committed is False
    assert denied.reason == "LIFECYCLE_EDGE_DENIED", denied.reason
    assert await _counts(live, inv_id) == (0, 0, 0, 0)
    assert await _hypothesis_version_status(live, hyp.id) == (1, "DRAFT")


@pytest.mark.asyncio
async def test_T16_unsupported_projection_status_denied(live: Any) -> None:
    # DESIGNED BAR (likely red — same post-write shape as T15).
    inv_id = await _investigation(live)
    hyp = await _hypothesis(live, inv_id)
    await _evidence(live, inv_id, "fp-t16", "k-t16")
    exp = await _expectation(live, hyp)
    svc = ProofPublicationService(live.factory)
    bad = _lifecycle()
    bad = LifecycleProjection(new_status="SUPPORTED", finding=bad.finding, conclusion=bad.conclusion)
    denied = await svc.commit(
        TENANT, inv_id, exp, [_strict_definition_route()], lifecycle=bad)
    assert denied.committed is False
    assert denied.reason == "UNSUPPORTED_PROJECTION_STATUS", denied.reason
    assert await _counts(live, inv_id) == (0, 0, 0, 0)
    assert await _hypothesis_version_status(live, hyp.id) == (1, "SUPPORTED")
```

**What the service-level pair proves and what it does not:** the barrier guarantees both attempts are in flight at service entry; the row lock (proven by P1a) forces the loser through the version check after the winner's lifecycle advance — so T13 proves serialized outcome under forced contention, not lock internals. The lock internals are P1a's claim. Neither leg alone proves both; together they do.

**Alternative considered:** A third parallel module (v1.0). Withdrawn — it duplicated eight passing tests while regressing isolation, cleanup, and DSN handling.

**Rationale:** Four tests, each bound to one uncovered reason code; the tables (via existing `_counts` + version/status + content reads) are the oracle, never the service's word.

### D4: The standard assertion bundle (scope: new tests + operator legs)

The T13–T16 tests and the P1b, P2, and P3 operator legs assert this bundle over ALL affected tables — P2 included: D2 already requires the all-zero bundle plus unchanged hypothesis version/status for the pre-commit evidence-change denial, and this scope paragraph now says so explicitly (v1.2's omission was the defect). The base module's 12 tests keep their recorded count assertions and do NOT assert hypothesis version/status (e.g. `test_evidence_append_denied` checks `(0,0,0,0)` but not the hypothesis row) — stated here as an accepted limitation of the base, not re-litigated: the bundle below is the bar for the new atomicity claim, and the base tests are regression cover, not bundle proof.

| Check | Happy path / winner | Every denial |
|---|---|---|
| `_counts(live, inv_id)` → (commits, nodes, findings, conclusions) | `(1, 1, 1, 1)` | `(0, 0, 0, 0)` |
| hypothesis (version, status) | `(N+1, "VERIFIED")` | `(N, <pre-status>)` unchanged |
| finding row content | title/statement == projection | no row |
| conclusion row content | root_cause/status == projection, investigation-bound | no row (upsert target untouched) |

For T13 the loser-specific claim is proven globally: exactly one COMMITTED plus `(1, 1, 1, 1)` and version `N+1` (not `N+2`) entails the loser wrote nothing — any loser write would add rows or a second version bump. The no-lifecycle case is excluded from T13 by construction (without lifecycle the second attempt deterministically returns `ALREADY_COMMITTED`, `committed=True` — the D2 replay table, not a loser).

**Alternative considered:** Commit/derivation counts only (v1.0). Rejected — lifecycle rows and hypothesis state are the observable side effects; a torn commit (commit row without projections) passes count-only checks.

### D5: Exit criteria and gate mapping

G5 atomicity is closed when ALL of the following are recorded: D1 recon clean with deviations owner-ruled (including the base-module 12-green baseline and any route-shape pre-work); D2 P1a block observed + P1b R=10 tally 10 COMMITTED + 10 STALE_VERSION + P2/P3 PASS bars met; D3 extended module green — existing 12 + T13–T16 = **16 passed** on staging with `IAP_LIVE_TESTS=1` (T15/T16 either green, or red-with-service-bug-report per §0 and the fix landed + rerun green); D4 bundle green on every test; D5 RLS posture re-verified with the exact queries below (outputs attached, host/port/db only):

```sql
-- acceptance: RLS enabled AND forced on both tables (both flags true)
SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class
  WHERE relname IN ('proof_derivations','proof_publication_commits');
-- acceptance: deployed revision is the proof-tables head (not just source present)
SELECT version_num FROM alembic_version;
-- expect: 034_proof_tables_grants
-- acceptance: every row false/false; current_user is the least-privilege role
SELECT rolname, rolbypassrls, rolsuper FROM pg_roles
  WHERE rolname IN ('iap_app','iap_worker','iap_test1');
SELECT current_user, current_setting('app.tenant_id', true);
-- acceptance: predicate text keys on app.tenant_id on both tables
SELECT tablename, policyname, qual, with_check FROM pg_policies
  WHERE tablename IN ('proof_derivations','proof_publication_commits');
```

The gate owner then declares G5 atomicity PASS or HOLD with named gaps. Writer routing, multi-route calls, authority routing, production wiring, and rollout are explicitly NOT closed by this exit.

## 5. Denial-coverage map (scoped claim)

Selected atomicity cases only — not "every denial path":

| Reason | Covered by | Notes |
|---|---|---|
| `STALE_VERSION` (sequential) | existing interleaved test | version bump before commit |
| `STALE_VERSION` (concurrent) | T13 | barrier-forced pair + lifecycle |
| `STALE_CONTEXT` | existing evidence-append test + P2 | pre-commit change; race window open (§9) |
| `CONFLICT_UNRESOLVED` | existing conflict test | + adjudicated-commit path |
| `POLICY_DENIED` (single) | existing forged/policy test | zero-write asserted; policy inputs are call-scoped (`policy_eligible`/`policy_reason` shared by all routes in one call), so the first fresh route is denied before any store — no later-route-only denial exists in the current API, no residual |
| `EVALUATION_FAILED` | existing forged test | certify-side rejection |
| `ALREADY_COMMITTED` | existing idempotent test + D2 replay table | committed=True, no new rows |
| `HYPOTHESIS_NOT_FOUND` | T14 | cross-tenant; new |
| `LIFECYCLE_EDGE_DENIED` | T15 | designed bar; likely red → bug report |
| `UNSUPPORTED_PROJECTION_STATUS` | T16 | designed bar; likely red → bug report |
| `COMMIT_RACE_LOST` | inspected, not forced | handler present (rollback + re-check); unreachable under the row lock by construction; opportunistic coverage only |

## 6. Data Storage

No new tables, no new migration. Verified present per 033/034 (see §2). If D1 finds drift from 033/034, the proposal stops — schema changes ride a new migration decision, never this document.

## 7. Data Structures

Existing dataclasses, restated as used (frozen): `CommitExpectation(hypothesis_id, hypothesis_version, evidence_digest)`; `RouteInput` with `rule_bundle_version`/`rule_version`/`implementation_digest` pinned from `RULE_REGISTRY["definition/v1"]` at build time (never the `"rules/v1"`/`"v1"` dataclass defaults); `LifecycleProjection(new_status="VERIFIED", finding, conclusion)`; `CommitResult(committed, reason, commit_id, derivation_ids)`.

## 8. Interfaces

### Operator protocol (staging)

| Step | Action | Pass signal |
|---|---|---|
| Preconditions | G5 protocol §1 + 033/034 at head + D1 item-6 role checks | All true, outputs recorded |
| P1a held lock | Raw-SQL `FOR UPDATE` hold + contending lock | Observed block until commit |
| P1b barrier pairs | R=10 fresh-fixture concurrent pairs with lifecycle | 10 COMMITTED + 10 STALE_VERSION |
| P2 pre-commit change | Insert evidence, then commit | STALE_CONTEXT, bundle all-zero |
| P3 cross-tenant | Commit under `OTHER` tenant | HYPOTHESIS_NOT_FOUND, T-side untouched |

### Live-test gate

| Command | Gate | Notes |
|---|---|---|
| `IAP_LIVE_TESTS=1 uv run python -m pytest -q tests/integration/test_proof_publication_live.py` | 16 passed on staging | 12 existing + T13–T16; scratch tenant only |

## 9. Risks / Trade-offs (updated)

### Staging is not production

All of D2/D3 runs on staging; production interleaving stays unproven. Named explicitly: the exit closes staging atomicity only.

### Post-write lifecycle denials (NEW — likely red)

T15/T16 encode the designed bar against code that validates lifecycle after persisting. A red run prescribes exactly one fix shape: hoist `new_status` + `_COMMITTABLE_FROM` validation to immediately after the row lock (before `find_commit`/store), then rerun. No test weakening, no expectation editing.

### Multi-route calls stay out of the gate (scope note, not a residual)

`mode="all"`/`"any"` over live routes is not exercised by this proposal: policy inputs are call-scoped, composition runs before any write, and no per-route policy model exists in the current API. The gate stays `mode="single"`; multi-route atomicity needs its own decision with its own API — v1.1's "partial denial" residual is withdrawn as unsupported by the code.

### Barrier timing

The barrier forces joint entry, not lock overlap; lock overlap is P1a's claim. Assertions are order-free (exactly one winner). A slow runner cannot flip the outcome — only which side wins.

### Service staleness (`rules/v1` default)

`RouteInput` still defaults to `"rules/v1"` while the kernel is `rules/v2`. D1 item 5 + the registry-pinned builder contain it; changing the default is a service-code decision for the implementation GO, proposed but not taken here.

### Loop-evidence race stays open

P2 tests the digest check, not the window. Carried openly: the check-then-commit narrows the window to one transaction but production write paths remain untouched by design.

## 10. Testing Philosophy

### Recon before code

v1.0 proved why: it asserted a missing suite that exists. Every API the tests touch is re-pinned by D1 before D3 exists. A red recon item is a stop, not a fix-it-live.

### Denials carry row-count proofs

Each denial test asserts the reason code AND the §4 bundle. A denial that writes is worse than a denial that lies — the tables are the oracle.

### State the tally before the run

R=10, per-round bars, and the replay table are fixed before execution. A run that "mostly passed" without a pre-stated tally proves nothing.

### Likely-red is a vector, not a failure

T15/T16 are pre-registered as probably red against current code, with the fix shape prescribed. If they go green, the code inspection was wrong — record that too.

## 11. Documentation Plan

### This file (`docs/IAP-proof-kernel-g5-atomicity-proposal-v1.3.md` — the single final record; superseded v1.0–v1.2 removed on owner direction 2026-10-26, history retained in §0 above)

**Audience:** Owner + implementer

v1.3 is the execution candidate. Promoted to design record on owner decision; superseded explicitly, never silently edited into a different scope.

### Traceability (on exit, not now)

**Audience:** Owner

On G5 atomicity PASS/HOLD, append the exit record (recon outputs, staging tallies, live-test output, RLS re-verification) to `docs/IAP-proof-kernel-traceability-v1.0.md` and the ledger. No traceability edit in this proposal — only the decision creates records.

## 12. Accessibility

### Operator and test interfaces

- **Parseable output:** PASS/FAIL tallies are plain `X passed` / reason-code strings; no color-only signals. Reports transcribe counts as text.
- **Help text:** Live tests skip with an explicit reason when the gate is unset instead of failing obscurely.
- **Error messages:** Every denial path returns a stable reason code; operator steps match on codes, never on prose.
