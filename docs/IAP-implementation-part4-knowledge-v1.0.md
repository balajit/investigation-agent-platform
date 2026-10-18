# IAP Implementation Part 4 v1.0: API, Persistence, Eventing, Security, Deployment

Supersedes `docs/archive/IAP-implementation-part4-py-v1.md` (archived for stale §2 phases, §4.1 matrix, §5 tables; D4 verdicts folded in below as fact). Verified against code 2026-09-27. Living issues: `docs/IAP-issues-0927-wip.md`.

## Facts (current platform)

FastAPI gateway (`api/app.py`, `/api/v1`): investigations create/get/list-open/start/cancel/retry (FAILED-only)/conclusion/pause/resume/approve-action (canonical, in `events.py`), timeline, evidence list/get, hypotheses list, profiles list, intake `POST /v1/intake/errors`, knowledge list/search, `/health/live` + `/health/ready`. Auth: tenant + principal deps (JWT in production, header consistency-check in dev); rate limiting; idempotency keys; outbox events. Lifecycle enum is `InvestigationStatus` (`-ING` inflections + `PAUSED` holding state + `COMPLETED/FAILED → INVESTIGATING` reopen edges).

Persistence: SQLAlchemy 2.0 async repositories (investigation, evidence, hypothesis, timeline, profile, transition, checkpoint, action-execution, idempotency, outbox, knowledge, session) with RLS and OCC; Alembic chain; `TimelineEvent` rows in `timeline_events`; single `application_profiles` table. Eventing: outbox + FastStream/Kafka publisher, Temporal signals for control plane. Security: gateway sanitization, SecretStr credentials, fail-hard production boot (missing deps abort, never degrade). Deployment: `docker-compose.yml` (postgres+pgvector, elasticsearch:8.12.0, temporal:1.23, kafka:3.7, neo4j:5.26-community) + `scripts/start-platform.sh` (infra → wait → migrate → API + worker). No frontend, no K8s manifests, no SSE stream (struck: needs streaming infra; trigger UI demand), no applications CRUD (profiles are operator-managed).

## Decisions kept

All side effects inside idempotent Temporal activities; failed starts never reported as success (F-008); unwired Temporal fails loud (503), never silent; production never degrades to in-memory.

## Future extensions

- **Manageability**: `GET /investigations` full query/filter (needs repository listing support); evidence-source fetch (needs payload-store handle on `AppContext`); per-hypothesis verification trigger; K8s manifests from the compose topology.
- **Scalability**: API horizontal replicas are stateless-safe (state in Postgres/Temporal); add DB read-replica routing for list endpoints under load.
- **Performance**: paginate conclusion-adjacent heavy reads; SSE or websocket stream for live investigation updates when UI demand arrives.
