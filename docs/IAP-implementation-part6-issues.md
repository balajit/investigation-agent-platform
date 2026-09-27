# IAP Part 6 — Open Issues Needing a Fix

Companion to `docs/IAP-implementation-part6-knowledge-v1.md` (design). Each
issue below is a self-contained unit of work: background, exact gap, scope,
acceptance criteria, and test requirements. Issues are ordered by priority.

Related code lives under
`src/investigation_agent_platform/domain/knowledge/` (envelope model,
fingerprinting), `src/investigation_agent_platform/application/knowledge/`
(capture, retrieve, intake), `src/investigation_agent_platform/infrastructure/knowledge/`
(Mem0/Graphiti adapters), and
`src/investigation_agent_platform/ports/knowledge/` (port protocols).

Slice 0 (envelope + Postgres), Slice 1 (Mem0), and Slice 2 (Graphiti) are
implemented and covered by `tests/unit/test_knowledge_coverage.py` and
`tests/unit/test_knowledge_mem0_coverage.py`. The issues below are the
Slice 3 ("hardening") gaps the design doc's Implementation Detail section
calls out, plus one issue (ISSUE-8) discovered while reviewing Slice 0's
TTL handling.

---

## ISSUE-8 (high, open): TTL artifacts are unusable the moment they're captured

**Status:** Open — a correctness bug, not just a missing hardening pass.

**Background.** `KnowledgeRetrievalService._gate_one`
(`application/knowledge/retrieve.py:181-187`) treats a `TTL`-policy artifact
with no `valid_to` as **already expired**, by design ("TTL without explicit
valid_to is treated as already elapsed: fail closed rather than assuming
freshness"). That is the correct gate behavior. The bug is upstream:
`KnowledgeCaptureService._distill` (`application/knowledge/capture.py:133-146`)
constructs every `evidence_summary` artifact with
`refresh_policy=RefreshPolicy.TTL` and **never sets `valid_to`**. The
`KnowledgeArtifact` domain validator (`domain/knowledge/models.py:93-105`)
does not require `TTL` artifacts to carry a `valid_to` either (only
`CONDITIONAL` artifacts are validated to require a `reverify` spec).

**Consequence.** Every `evidence_summary` artifact captured today is
stored, then excluded from every future `retrieve_for_reasoning` call
immediately — `excluded_stale_count` silently absorbs 100% of this artifact
class from the moment it exists. Nothing surfaces this as an error; it
looks like normal staleness exclusion.

**Scope of the fix.**

1. `KnowledgeCaptureService._distill` must set a real `valid_to` for every
   `TTL` artifact it constructs (e.g. `now + timedelta(days=N)` — pick `N`
   deliberately, not a magic number inline; a `KnowledgeConfig` field is
   the natural home, mirroring `max_reverify_attempts`).
2. Add a domain validator to `KnowledgeArtifact`
   (`domain/knowledge/models.py:93-105`) requiring `TTL` artifacts to carry
   a `valid_to` — the same "requires X" pattern already used for
   `CONDITIONAL` + `reverify`. This makes the bug class structurally
   impossible to reintroduce, not just fixed once.
3. Audit any other caller that constructs a `TTL` artifact (currently only
   `capture.py`, but check `application/knowledge/intake.py` too) for the
   same omission.

**Acceptance criteria.**

- A newly captured `evidence_summary` artifact is retrievable (included in
  `verified_facts`, not `excluded_stale_count`) until its TTL window
  elapses, then excluded automatically.
- Constructing a `KnowledgeArtifact(refresh_policy=TTL, valid_to=None)`
  raises a validation error.

**Tests required.**

- Regression test: capture an `evidence_summary`, retrieve immediately,
  assert it appears in `verified_facts`.
- Time-travel test: same artifact, retrieve after the TTL window elapses,
  assert it's excluded and transitioned to `EXPIRED`.
- Validator test: `KnowledgeArtifact(refresh_policy=TTL, valid_to=None)`
  raises.

---

## ISSUE-9 (medium, open): No proactive supersession/expiry janitor

**Status:** Open — design gap from the Part 6 doc's Slice 3 scope
("supersession janitor (marks TTL-elapsed as EXPIRED)").

**Background.** `KnowledgeRetrievalService._gate_one`
(`application/knowledge/retrieve.py:173-188`) only marks an artifact
`EXPIRED` (TTL) or `SUPERSEDED`/`QUARANTINED` (CONDITIONAL mismatch/down)
**reactively**, at the moment something tries to retrieve it. An artifact
nobody ever retrieves again — a common case once an investigation closes
and its knowledge isn't reused — stays `ACTIVE` in Postgres forever, even
long after its `valid_to` has passed. This doesn't silently mislead the
reasoner (retrieval still gates correctly), but it does mean:

- `ArtifactRepository` accumulates stale-but-`ACTIVE` rows indefinitely,
  inflating `list_active_for_reuse`/`list_shared_for_fingerprint` scans.
- There is no proactive re-verification of `CONDITIONAL` artifacts either
  — a flag that flipped is only caught the next time some investigation
  happens to retrieve that specific artifact, not on a schedule.
- No `ArtifactRepository` method exists to list artifacts by expiry/status
  across an entire tenant for a sweep (`ports/knowledge/ports.py:52-65` has
  only `list_for_investigation`, `list_active_for_reuse` (scoped by
  `application_id`), and `list_shared_for_fingerprint` — none scoped for a
  tenant-wide or global sweep).

**Scope of the fix.**

1. Add `ArtifactRepository.list_active_before(tenant_id, cutoff: datetime,
   limit: int) -> list[KnowledgeArtifact]` (or equivalent paginated sweep
   query) to `ports/knowledge/ports.py` and the SQLAlchemy implementation
   in `infrastructure/persistence/knowledge_repository.py`.
2. Add a `KnowledgeArtifactJanitorWorkflow` Temporal scheduled workflow
   (mirror the pattern already established for
   `TopologySnapshotRetentionWorkflow` in
   `application/worker/workflows.py` — same file, same "first scheduled
   workflow" precedent, same registration point in
   `bootstrap/worker.py`) that, per tenant:
   - Sweeps `TTL` artifacts past `valid_to`, transitioning them to
     `EXPIRED` via the same `KnowledgeRetrievalService._transition` path
     (reuse it — don't duplicate the transition logic).
   - Optionally re-verifies `CONDITIONAL` artifacts on a schedule (proactive
     version of `_reverify`), so a flag flip is caught even without a
     retrieval happening to hit it.
3. Never touches `SUPERSEDED`/`QUARANTINED`/already-`EXPIRED` rows — pure
   idempotent status advancement, matching the retrieval-path semantics
   exactly (never mutate history, only transition forward).

**Acceptance criteria.**

- A `TTL` artifact past its `valid_to` that nobody retrieves is
  transitioned to `EXPIRED` by the next scheduled sweep, without requiring
  a `retrieve_for_reasoning` call.
- Sweeping is idempotent under retry (a second run over the same window
  changes nothing further).
- `CONDITIONAL` proactive re-verification (if implemented in this pass)
  reuses `KnowledgeRetrievalService._reverify`'s checker registry —
  no second reverification code path.

**Tests required.**

- Sweep test: fixture artifacts past/within/before their TTL window;
  assert only the past-window ones transition.
- Idempotency test: run the sweep twice, assert no double-transition and
  no error on already-`EXPIRED` rows.
- Reuse test: sweep-triggered reverification produces the same
  `SUPERSEDED`/`QUARANTINED` outcome the retrieval path would for the same
  fixture (same checker, same result).

---

## ISSUE-10 (medium, open): D7 budget controls are unenforced

**Status:** Open — `KnowledgeConfig.max_episodes_per_investigation`
(`infrastructure/configuration/config.py:155`) exists (default 50, matching
the design doc's D7 number exactly) but is **never read anywhere** —
confirmed zero references outside its own definition and env-loader
(`_knowledge_config_from_env`, `config.py:182`). None of D7's other
controls exist at all.

**Background.** D7 in the design doc lists five controls. Current state of
each:

1. **Per-investigation episode cap (default 50).** Config field exists,
   unenforced. `KnowledgeCaptureService._distill`
   (`application/knowledge/capture.py:84-147`) and `_project`
   (`capture.py:149-171`) never check a cap before calling
   `artifact_repo.save` or `temporal_port.project_episode` — an
   investigation with thousands of evidence items produces thousands of
   artifacts and Graphiti episodes with no ceiling.
2. **Per-episode byte cap.** Does not exist. `GraphitiTemporalKnowledge
   .project_episode` (`infrastructure/knowledge/graphiti_adapter.py:161-205`)
   serializes the full artifact body with no truncation or "pointer to
   Elastic for the remainder" fallback.
3. **`SEMAPHORE_LIMIT` + per-group serialized ingest.**
   `GraphitiTemporalKnowledge.__init__` accepts `semaphore_limit`
   (`graphiti_adapter.py:73,82`) and passes it to `Graphiti(...,
   max_coroutines=self._semaphore_limit)` (`graphiti_adapter.py:108`) —
   this bounds Graphiti's *internal* LLM-call concurrency, but there is no
   per-`group_id` lock/queue in this adapter or in
   `KnowledgeCaptureService` ensuring **this platform's own concurrent
   calls** to `project_episode` for the same `group_id` (e.g. two
   sessions of a merged investigation capturing concurrently) are
   serialized. The design doc calls this out specifically because Graphiti
   entity-resolution races under concurrent same-group writes.
4. **Token/cost telemetry on the capture activity.** Does not exist.
   `capture_knowledge_activity`
   (`application/worker/activities.py`, confirmed via the earlier ISSUE-4
   exploration pass) records no token/cost metrics; neither does
   `KnowledgeCaptureService`.
5. **Degrade-to-envelopes-only under budget pressure.** Does not exist —
   there is no budget-tracking state for `_project` to consult before
   attempting a Mem0/Graphiti projection.
6. **Mem0 `infer=False` verbatim path.** Already implemented — confirmed at
   `infrastructure/knowledge/mem0_adapter.py:34,171`
   (`VERBATIM_KINDS`/`infer=artifact.kind not in VERBATIM_KINDS`). Not part
   of this issue's remaining scope.

**Scope of the fix.**

1. Enforce `max_episodes_per_investigation` in
   `KnowledgeCaptureService.capture_for_investigation`: count episodes
   already projected for the investigation (or track a running counter
   passed in / persisted), stop projecting (not distilling — envelopes
   still get written to Postgres) once the cap is hit, and record how many
   were skipped.
2. Add a byte cap constant/config field; in
   `GraphitiTemporalKnowledge.project_episode`, truncate the JSON body at
   the cap with a pointer comment (mirroring the existing "pointer to
   Elastic for the remainder" pattern already used elsewhere in the
   evidence pipeline for log truncation) rather than raising.
3. Add per-`group_id` serialization in `KnowledgeCaptureService` (an
   `asyncio.Lock` keyed by `group_id`, or a small in-process registry) so
   concurrent `capture_for_investigation` calls for the same
   `investigation_group_id`/`baseline_group_id` never call
   `project_episode` concurrently.
4. Add token/cost telemetry: the platform already has an observability
   port pattern (see `bootstrap/__init__.py`'s wiring of
   `OpenTelemetryObservabilityAdapter`) — thread a lightweight counter
   through `capture_knowledge_activity` and `_project`, recorded via that
   same port, not a new bespoke metrics path.
5. Add a `_project` guard: when the per-investigation budget (episodes or
   estimated cost) is exceeded, skip Mem0/Graphiti projection for the
   remaining artifacts in that investigation (envelopes still saved to
   Postgres — "degrade to envelopes-only, retrievable but not indexed").

**Acceptance criteria.**

- An investigation producing more than `max_episodes_per_investigation`
  distilled artifacts stores all envelopes in Postgres but projects only
  up to the cap to Mem0/Graphiti; the excess is logged/counted, never
  silently dropped without a trace.
- A single artifact whose serialized episode body exceeds the byte cap is
  truncated with a recoverable pointer, never rejected outright.
- Two concurrent `capture_for_investigation` calls sharing a `group_id`
  never interleave `project_episode` calls to that group (verifiable via a
  lock-acquisition-order assertion in a concurrency test).
- Capture activity emits token/cost telemetry through the existing
  observability port.

**Tests required.**

- Episode-cap test: fixture investigation with more evidence than the cap;
  assert Postgres envelope count == full count, projected count == cap.
- Byte-cap test: artifact with an oversized `code_refs`/`statement`
  combination; assert the projected episode body respects the cap.
- Concurrency test: two concurrent captures for the same group_id; assert
  serialized (not interleaved) `project_episode` calls via a mock that
  records call order under a simulated delay.
- Telemetry test: capture activity records a token/cost metric via the
  observability port mock.

---

## ISSUE-11 (low, open): `code_refs` → Layer 3 cross-layer join is not implemented

**Status:** Open — design gap from the Part 6 doc's Slice 3 scope
("cross-layer join (`code_refs` → Layer 3) in the reasoner context").

**Background.** `KnowledgeArtifact.code_refs`
(`domain/knowledge/models.py:86`) and `ArtifactView.code_refs`
(`domain/knowledge/models.py:137`) both exist and are threaded through
end-to-end: `KnowledgeRetrievalService._view`
(`application/knowledge/retrieve.py:251-260`) copies
`artifact.code_refs` onto the `ArtifactView` returned to the reasoner, and
`GraphitiTemporalKnowledge.project_episode`
(`infrastructure/knowledge/graphiti_adapter.py:187`) includes them in the
projected episode body. But nothing ever **resolves** a `code_ref` string
(format `repo@rev:path#line` per the design doc) against the Layer 3
topology store — `KnowledgeRetrievalService` has no dependency on
`DomainAttributionPort`/`CodeTopologyRepository` at all (confirmed: zero
imports from `ports.topology` in `application/knowledge/`). The design
doc's framing — `"flag X was on AND code path Y reads it"` is a *join*
across layers — is not implemented; `code_refs` today are opaque strings
the reasoner receives with no attached ownership/attribution context.

**Scope of the fix.**

1. Define the `code_ref` string format precisely if not already fixed
   elsewhere (`repo@rev:path#line` per the design doc's D5 section) and add
   a small parser (reject malformed refs rather than guessing).
2. Extend `KnowledgeRetrievalService` with an optional
   `attribution_port: DomainAttributionPort | None` dependency (same
   optional-dependency pattern already used for `knowledge_store`/
   `temporal_port` — absent means the join is skipped, not an error).
3. When present, for each `ArtifactView.code_refs` entry, resolve it via
   `DomainAttributionPort.resolve_source_location` (parsing `repo@rev` into
   `repository_id`/`revision`, `path#line` into `file_path`/`line_number`)
   and attach the resulting ownership (`domain_id`, `fallback_level`) to
   the context — either as an extension field on `ArtifactView` or as a
   parallel joined structure in `KnowledgeContext`. Never fail retrieval on
   a resolution error; log and continue without the join for that ref.
4. Respect tenant scoping throughout — `resolve_source_location` already
   requires `tenant_id`, so this is enforced identically to every other
   topology read.

**Acceptance criteria.**

- A `KnowledgeContext` built with an `attribution_port` configured
  attaches resolved ownership to `code_refs` that point at a `READY`
  snapshot; refs pointing at a collected/missing snapshot degrade
  gracefully (ref still present, no ownership attached, no exception).
- Without an `attribution_port` configured, behavior is byte-for-byte
  identical to today (regression-safe default).

**Tests required.**

- Join test: artifact with a valid `code_ref` against a seeded in-memory
  topology snapshot; assert the joined ownership appears in the context.
- Degraded-snapshot test: `code_ref` pointing at a snapshot that has been
  collected (ISSUE-4's `COLLECTED` status); assert graceful omission, no
  exception.
- Malformed-ref test: garbage `code_ref` string; assert it's skipped, not
  a crash.
- Regression test: `attribution_port=None` (the current default) produces
  identical `KnowledgeContext` output to before this change.

---

## ISSUE-12 (low, open): Store-contract tests are mocked, not run against real containers

**Status:** Open — design gap from the Part 6 doc's Testing Philosophy
section ("Mem0 adapter and Graphiti adapter tested against real containers
(pgvector + Neo4j in CI)").

**Background.** `tests/unit/test_knowledge_mem0_coverage.py` and the Mem0/
Graphiti sections of `tests/unit/test_knowledge_coverage.py` exercise
`Mem0MemoryStore`/`GraphitiTemporalKnowledge` against mocked clients (the
adapters' `_client_or_raise()`-style lazy construction makes this easy to
mock, which is good for fast unit coverage, but the design doc explicitly
calls for a *separate* integration tier against real `pgvector`/Neo4j
containers to catch driver-version and query-semantics drift that mocks
cannot). `docker/docker-compose.yml` already runs a `neo4j` service
(added for Layer 3 / Part 6 Slice 2), so local infrastructure exists.

Checked what's actually in `tests/integration/` today
(`test_agent_loop_elastic_oracle.py`, `test_llm_gateway.py`,
`test_tenancy_rls.py`): despite the directory name, none of them start or
depend on a real container — they're ordinary mocked/logic tests that
happen to live in that folder. There is **no existing skip-if-unavailable
convention** to mirror, and CI (`.github/workflows/ci.yml`) never starts
`docker-compose` or Neo4j at all; `pytest -q` in the `test` job
(`ci.yml:34-44`) runs the full `tests/` tree (including `tests/integration/`)
with no service containers behind it. The one precedent for a
container-backed CI job is the separate `migrate` job
(`ci.yml:46-55`), which uses a GitHub Actions `services:` block to run
`postgres:16` for the Alembic-upgrade check — that is the pattern to
extend, not docker-compose.

**Scope of the fix.**

1. Add `tests/integration/test_knowledge_mem0_containers.py` and
   `tests/integration/test_knowledge_graphiti_containers.py` (or a combined
   file). Since no skip-if-unavailable convention exists in this repo yet,
   introduce one deliberately (e.g. a `pytest.mark.skipif` checking for a
   reachable service via a short connection attempt, or an opt-in marker
   gated behind an env var such as `IAP_RUN_CONTAINER_TESTS=1`) — document
   the chosen convention at the top of the new file so future
   container-backed tests have something to follow.
2. Cover specifically: the pgvector `get_all` + logic-wrapper trap the
   design doc calls out, and Graphiti concurrent same-group ingest
   serialization (this also gives ISSUE-10's concurrency fix a real,
   non-mocked regression test once both land).
3. Add a new CI job mirroring the `migrate` job's `services:` block
   pattern (`ci.yml:46-55`) — a `postgres:16` (or `pgvector/pgvector:pg16`)
   service for the Mem0 test and a `neo4j:5.26-community` service for the
   Graphiti test — rather than trying to invoke `docker-compose.yml`
   directly in CI.

**Acceptance criteria.**

- Both adapters have at least one integration test exercising a real
  container round-trip (write via the adapter, read back, assert
  tenant-scoped filtering holds).
- Tests skip cleanly (not fail) when Docker/containers are unavailable
  locally, matching the existing integration-test convention.

**Tests required.**

- (This issue *is* the test-infrastructure work; acceptance criteria above
  double as the requirement.)
