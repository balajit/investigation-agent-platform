# src/investigation_agent_platform/api/dev_seed.py
"""Opt-in development seed for UI-backed tenants (e.g. ``n-a``).

Enabled only when ``IAP_DEV_SEED_TENANTS`` is set to a comma-separated
tenant list. Seeds a realistic, deterministic dataset into the process-local
(AppContext) stores so every UI screen renders rows instead of empty states:

- one ``example-app`` profile per tenant (cloned from ``_DEFAULT_PROFILE``)
- six investigations across statuses (INVESTIGATING showcase with
  evidence/timeline/hypotheses/findings/knowledge, AWAITING_INPUT with a
  pending requirement, COMPLETED with a conclusion, PAUSED, FAILED, CREATED)
- three background jobs (RUNNING / DONE / FAILED)
- two finding clusters with assignments into the showcase findings

Idempotent within a process (skips tenants that already have the profile).
Seed data is intentionally ephemeral: the dev AppContext is in-memory, so a
restart reseeds from scratch. Never runs in production — callers must gate on
``IAP_ENVIRONMENT != "production"`` (enforced here as well).
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import UTC, datetime, timedelta
from uuid import UUID

logger = logging.getLogger(__name__)

SEED_TENANTS_ENV = "IAP_DEV_SEED_TENANTS"
SEED_APP_ID = "example-app"


async def _seed_write(write_coro, counts: dict[str, int], what: str) -> bool:
    """Run one seed write, tolerating already-exists conflicts.

    Existence checks race across restarts/processes (check-then-act against
    a store another boot is also seeding): a duplicate-key conflict means
    the row is already there, so count it as resumed instead of crashing
    the seed — or worse, journaling a doomed WAL entry that ends HELD.
    Any other error still propagates (genuine failures must stay loud).
    """
    from sqlalchemy.exc import IntegrityError

    from investigation_agent_platform.domain.common.exceptions import ConcurrencyError

    try:
        await write_coro
    except (ConcurrencyError, IntegrityError):
        counts["resumed"] += 1
        logger.info("Seed write already present, counted as resumed", extra={"what": what})
        return False
    return True


def seed_tenants_from_env() -> list[str]:
    return [
        t.strip()
        for t in os.environ.get(SEED_TENANTS_ENV, "").split(",")
        if t.strip()
    ]


async def seed_dev_tenant(ctx: object, tenant_id: str) -> dict[str, int]:
    """Seed one tenant; returns created/resumed counts.

    Every step is guarded (existence check before write) so partial seeds
    resume cleanly across restarts — including the Postgres-backed path,
    where an early failure must not wedge later steps behind a stale gate.
    """
    from investigation_agent_platform.api.dependencies import _DEFAULT_PROFILE

    now = datetime.now(UTC)
    counts = {"investigations": 0, "jobs": 0, "resumed": 0}

    # -- profile ---------------------------------------------------------
    existing_profile = await ctx.profile_repo.get_by_application_id(tenant_id, SEED_APP_ID)  # type: ignore[attr-defined]
    if existing_profile is None:
        profile = _DEFAULT_PROFILE.model_copy(update={"tenant_id": tenant_id})
        await _seed_write(
            ctx.profile_repo.save(tenant_id, profile),  # type: ignore[attr-defined]
            counts,
            "profile/example-app",
        )
    else:
        counts["resumed"] += 1

    # -- investigations ---------------------------------------------------
    from investigation_agent_platform.domain.investigation.models import (
        Investigation,
        InvestigationContext,
        InvestigationRequest,
        InvestigationStatus,
    )

    def _make_inv(
        slug: str,
        problem: str,
        status: InvestigationStatus,
        age_hours: int,
    ) -> Investigation:
        start = now - timedelta(hours=age_hours)
        req = InvestigationRequest(
            application_id=SEED_APP_ID,
            problem_description=problem,
            session_id=f"dev-{slug}",
            requested_by="dev-seed",
        )
        inv_ctx = InvestigationContext(
            environment="production",
            time_window=(start - timedelta(hours=1), start),
        )
        return Investigation(
            # Deterministic per (tenant, slug): stable across restarts so
            # UI deep-links and verification scripts keep working.
            id=uuid.uuid5(uuid.NAMESPACE_URL, f"iap-dev-seed:{tenant_id}:{slug}"),
            session_id=f"dev-{slug}",
            application_id=SEED_APP_ID,
            tenant_id=tenant_id,
            request=req,
            context=inv_ctx,
            status=status,
            created_at=start,
            updated_at=now,
            version=1,
        )

    showcase = _make_inv(
        "checkout-error-rate",
        "Elevated 5xx rate on checkout service during synthetic canary",
        InvestigationStatus.INVESTIGATING,
        5,
    )
    awaiting = _make_inv(
        "payment-timeouts",
        "Payment authorization timeouts spiking for EU region",
        InvestigationStatus.AWAITING_INPUT,
        3,
    )
    completed = _make_inv(
        "ledger-drift",
        "Ledger balance drift detected in nightly reconciliation",
        InvestigationStatus.COMPLETED,
        30,
    )
    paused = _make_inv(
        "search-latency",
        "p99 search latency regression after index rollover",
        InvestigationStatus.PAUSED,
        8,
    )
    failed = _make_inv(
        "webhook-retries",
        "Webhook delivery retry storm saturating egress queue",
        InvestigationStatus.FAILED,
        12,
    )
    created = _make_inv(
        "inventory-sync",
        "Inventory sync lag behind warehouse feed",
        InvestigationStatus.CREATED,
        1,
    )
    for inv in (showcase, awaiting, completed, paused, failed, created):
        # Deterministic IDs: create only when missing so partial seeds
        # resume cleanly across restarts (SQLAlchemy create would clash).
        if await ctx.investigation_repo.get_by_id(tenant_id, inv.id) is None:  # type: ignore[attr-defined]
            if await _seed_write(
                ctx.investigation_repo.create(tenant_id, inv),  # type: ignore[attr-defined]
                counts,
                f"investigation/{inv.id}",
            ):
                counts["investigations"] += 1
        else:
            counts["resumed"] += 1

    # -- showcase sub-resources -------------------------------------------
    from investigation_agent_platform.domain.evidence.models import (
        Evidence,
        EvidenceType,
    )
    from investigation_agent_platform.domain.hypothesis.models import Hypothesis
    from investigation_agent_platform.domain.provenance.models import (
        EvidenceFreshness,
        EvidenceProvenance,
        QueryFingerprint,
        SourceLocation,
    )
    from investigation_agent_platform.domain.timeline.models import (
        TimelineEvent,
        TimelineEventType,
    )

    def _provenance() -> EvidenceProvenance:
        return EvidenceProvenance(
            tenant_id=tenant_id,
            investigation_id=showcase.id,
            provider_type="dev-seed",
            requested_provider_id="dev-seed",
            actual_provider_id="dev-seed",
            source_system="dev-seed",
            retrieval_timestamp=now,
            query_fingerprint=QueryFingerprint(
                provider_type="dev-seed",
                operation="seed",
                normalized_query_hash="dev",
            ),
            source_location=SourceLocation(system="dev-seed", identifier="seed"),
        )

    evidence_ids: list[UUID] = []
    _existing_evidence = await ctx.evidence_repo.find_by_investigation_id(tenant_id, showcase.id)  # type: ignore[attr-defined]
    if _existing_evidence:
        evidence_ids = [e.evidence_id for e in _existing_evidence]
        counts["resumed"] += 1
    else:
        for i, (title, summary) in enumerate(
            [
                ("Checkout 5xx spike in gateway logs", "5xx rate 0.2% -> 4.1% at canary start"),
                ("Trace waterfall shows payment auth stall", "auth span p99 220ms -> 3.8s"),
                ("Deploy marker correlates with onset", "payments-service v2.14.3 rolled at T-5h"),
            ]
        ):
            ev = Evidence(
                tenant_id=tenant_id,
                investigation_id=showcase.id,
                evidence_type=EvidenceType.LOG,
                provider="dev-seed",
                source=f"urn:dev-seed:{i}",
                title=title,
                summary=summary,
                fingerprint=f"dev-seed-fp-{i}",
                observed_at=now - timedelta(hours=4 - i),
                retrieved_at=now,
                provenance=_provenance(),
                freshness=EvidenceFreshness(
                    observed_at=now - timedelta(hours=4 - i), retrieved_at=now
                ),
            )
            await _seed_write(
                ctx.evidence_repo.save(tenant_id, ev, showcase.id),  # type: ignore[attr-defined]
                counts,
                f"evidence/{ev.evidence_id}",
            )
            evidence_ids.append(ev.evidence_id)

    _existing_timeline = await ctx.timeline_repo.find_by_investigation_id(tenant_id, showcase.id)  # type: ignore[attr-defined]
    if _existing_timeline:
        counts["resumed"] += 1
    else:
        for i, (etype, desc) in enumerate(
            [
                (TimelineEventType.REQUEST, "Investigation opened from canary alert"),
                (TimelineEventType.DEPLOYMENT, "payments-service v2.14.3 deployed"),
                (TimelineEventType.LOG_EVENT, "5xx rate crossed alert threshold"),
                (TimelineEventType.STATE_CHANGE, "Status moved to INVESTIGATING"),
            ]
        ):
            await _seed_write(
                ctx.timeline_repo.append(  # type: ignore[attr-defined]
                tenant_id,
                TimelineEvent(
                    tenant_id=tenant_id,
                    investigation_id=showcase.id,
                    sequence_number=i,
                    timestamp=now - timedelta(hours=5 - i),
                    event_type=etype,
                    description=desc,
                ),
                showcase.id,
                ),
                counts,
                f"timeline/{showcase.id}/{i}",
            )

    _existing_hypotheses = await ctx.hypothesis_repo.find_by_investigation_id(tenant_id, showcase.id)  # type: ignore[attr-defined]
    if _existing_hypotheses:
        counts["resumed"] += 1
    else:
        for title, statement in [
            ("Retry storm on payment auth", "Client retries amplify a slow auth dependency"),
            ("Connection pool exhaustion", "Pool saturation under canary traffic causes 5xx"),
        ]:
            await _seed_write(
                ctx.hypothesis_repo.save(  # type: ignore[attr-defined]
                tenant_id,
                Hypothesis(
                    tenant_id=tenant_id,
                    investigation_id=showcase.id,
                    title=title,
                    description=title,
                    statement=statement,
                    supporting_evidence_ids=[evidence_ids[0]],
                ),
                showcase.id,
                ),
                counts,
                f"hypothesis/{title}",
            )

    from investigation_agent_platform.domain.finding.models import (
        ConclusionStatus,
        Finding,
        FindingType,
        InvestigationConclusion,
    )

    showcase_findings: list[Finding] = []
    _existing_findings, _ = await ctx.finding_repo.list_findings(tenant_id, showcase.id)  # type: ignore[attr-defined]
    if _existing_findings:
        showcase_findings = list(_existing_findings)
        counts["resumed"] += 1
    else:
        for title, statement in [
            ("Auth dependency is the bottleneck", "Payment auth p99 rose 17x at canary start"),
            ("Deploy v2.14.3 is the trigger", "Onset aligns with deploy marker +/- 2 min"),
        ]:
            finding = Finding(
                tenant_id=tenant_id,
                investigation_id=showcase.id,
                finding_type=FindingType.OBSERVATION,
                title=title,
                statement=statement,
                evidence_ids=[str(e) for e in evidence_ids[:2]],
            )
            await _seed_write(
                ctx.finding_repo.save_finding_record(tenant_id, finding),  # type: ignore[attr-defined]
                counts,
                f"finding/{finding.id}",
            )
            showcase_findings.append(finding)

    _existing_completed, _ = await ctx.finding_repo.list_findings(tenant_id, completed.id)  # type: ignore[attr-defined]
    if _existing_completed:
        counts["resumed"] += 1
    else:
        completed_finding = Finding(
            tenant_id=tenant_id,
            investigation_id=completed.id,
            finding_type=FindingType.OBSERVATION,
            title="Double-applied refund batch",
            statement="Refund job ran twice after a lock timeout",
            evidence_ids=[],
        )
        await _seed_write(
            ctx.finding_repo.save_finding_record(tenant_id, completed_finding),  # type: ignore[attr-defined]
            counts,
            f"finding/{completed_finding.id}",
        )
    await _seed_write(
        ctx.finding_repo.save_conclusion(  # type: ignore[attr-defined]
        tenant_id,
        InvestigationConclusion(
            tenant_id=tenant_id,
            investigation_id=completed.id,
            status=ConclusionStatus.ROOT_CAUSE_ESTABLISHED,
            root_cause="Refund batch re-ran after advisory-lock timeout",
            supporting_evidence_ids=[uuid.uuid4()],
            supporting_hypothesis_ids=[],
            confidence=0.9,
            recommended_next_steps=["Make refund batch idempotent"],
        ),
        ),
        counts,
        f"conclusion/{completed.id}",
    )

    # -- pending input requirement (AWAITING_INPUT) -------------------------
    from investigation_agent_platform.domain.investigation.input_requirements import (
        InputRequirement,
    )

    _existing_pending = await ctx.input_repo.get_pending(tenant_id, awaiting.id)  # type: ignore[attr-defined]
    if _existing_pending:
        counts["resumed"] += 1
    else:
        await _seed_write(
            ctx.input_repo.create_requirement(  # type: ignore[attr-defined]
                tenant_id,
                InputRequirement(
                    tenant_id=tenant_id,
                    investigation_id=awaiting.id,
                    reason="Need a failing transaction id to pull traces",
                    json_schema={
                        "type": "object",
                        "properties": {"txn_id": {"type": "string"}},
                        "required": ["txn_id"],
                    },
                ),
            ),
            counts,
            f"input-requirement/{awaiting.id}",
        )

    # -- knowledge artifact + session (showcase) ------------------------------
    from investigation_agent_platform.domain.knowledge.models import (
        InvestigationSession,
        KnowledgeArtifact,
        RefreshPolicy,
    )

    _existing_artifacts = await ctx.artifact_repo.list_for_investigation(tenant_id, showcase.id)  # type: ignore[attr-defined]
    if _existing_artifacts:
        counts["resumed"] += 1
    else:
        await _seed_write(
            ctx.artifact_repo.save(  # type: ignore[attr-defined]
            tenant_id,
            KnowledgeArtifact(
                tenant_id=tenant_id,
                application_id=SEED_APP_ID,
                investigation_id=showcase.id,
                kind="runbook",
                statement="Payment auth slowdowns present as checkout 5xx; check auth p99 first.",
                confidence=0.8,
                refresh_policy=RefreshPolicy.IMMUTABLE,
                valid_from=now - timedelta(days=1),
                source_evidence_ids=[evidence_ids[0]],
            ),
            ),
            counts,
            f"artifact/{SEED_APP_ID}",
        )
    if await ctx.session_repo.count_own_sessions(tenant_id, showcase.id):  # type: ignore[attr-defined]
        counts["resumed"] += 1
    else:
        await _seed_write(
            ctx.session_repo.append(  # type: ignore[attr-defined]
                tenant_id,
                InvestigationSession(
                    investigation_id=showcase.id,
                    session_number=1,
                    tenant_id=tenant_id,
                    occurred_at=now - timedelta(hours=4),
                    log_refs=["logs-app"],
                    trace_refs=["trace-abc"],
                    status="OPEN",
                ),
            ),
            counts,
            f"session/{showcase.id}",
        )

    # -- background jobs ------------------------------------------------------
    from investigation_agent_platform.domain.common.background_job import (
        BackgroundJob,
        BackgroundJobStatus,
    )

    running_job = BackgroundJob(
        tenant_id=tenant_id,
        application_id=SEED_APP_ID,
        kind="finding-clustering",
        status=BackgroundJobStatus.RUNNING,
        progress=3,
        total=8,
        created_by="dev-seed",
        started_at=now - timedelta(minutes=20),
        workflow_id=f"dev-seed-{uuid.uuid4().hex[:8]}",
    )
    done_job = BackgroundJob(
        tenant_id=tenant_id,
        application_id=SEED_APP_ID,
        kind="error-batch-intake",
        status=BackgroundJobStatus.DONE,
        progress=8,
        total=8,
        created_by="dev-seed",
        started_at=now - timedelta(hours=2),
        completed_at=now - timedelta(hours=2) + timedelta(minutes=5),
    )
    failed_job = BackgroundJob(
        tenant_id=tenant_id,
        application_id=SEED_APP_ID,
        kind="investigation-run",
        status=BackgroundJobStatus.FAILED,
        progress=2,
        total=6,
        created_by="dev-seed",
        started_at=now - timedelta(hours=6),
        completed_at=now - timedelta(hours=6) + timedelta(minutes=2),
        error="LLM gateway unreachable in dev (placeholder key)",
    )
    _existing_jobs, _ = await ctx.background_job_repo.list_jobs(tenant_id, limit=200)  # type: ignore[attr-defined]
    if any(j.created_by == "dev-seed" for j in _existing_jobs):
        counts["resumed"] += 1
    else:
        for job in (running_job, done_job, failed_job):
            if await _seed_write(
                ctx.background_job_repo.create(job),  # type: ignore[attr-defined]
                counts,
                f"job/{job.kind}",
            ):
                counts["jobs"] += 1

    # -- clusters ---------------------------------------------------------------
    from investigation_agent_platform.domain.finding.clustering import (
        FindingCluster,
        FindingClusterAssignment,
    )

    cluster_a = FindingCluster(
        tenant_id=tenant_id,
        cluster_key="auth",
        label="Auth dependency failures",
        description="Payment auth slowdowns and retry storms",
        taxonomy_revision=1,
    )
    cluster_b = FindingCluster(
        tenant_id=tenant_id,
        cluster_key="deploy",
        label="Deploy-correlated regressions",
        description="Regressions aligned with deploy markers",
        taxonomy_revision=1,
    )
    _existing_taxonomy = await ctx.finding_cluster_repo.load_taxonomy(tenant_id)  # type: ignore[attr-defined]
    if _existing_taxonomy:
        counts["resumed"] += 1
    else:
        for cluster in (cluster_a, cluster_b):
            await _seed_write(
                ctx.finding_cluster_repo.create_cluster(tenant_id, cluster),  # type: ignore[attr-defined]
                counts,
                f"cluster/{cluster.cluster_key}",
            )
        for finding, cluster in zip(showcase_findings, (cluster_a, cluster_b)):
            await _seed_write(
                ctx.finding_cluster_repo.assign_finding(  # type: ignore[attr-defined]
                tenant_id,
                FindingClusterAssignment(
                    tenant_id=tenant_id,
                    finding_id=finding.id,
                    cluster_id=cluster.id,
                    method="dev-seed",
                    confidence=0.9,
                    taxonomy_revision=1,
                ),
                ),
                counts,
                f"assign/{finding.id}/{cluster.id}",
            )

    logger.info(
        "Dev seed complete",
        extra={"tenant_id": tenant_id, **counts},
    )
    return counts


async def seed_dev_tenants(ctx: object) -> dict[str, dict[str, int]]:
    """Seed every tenant in ``IAP_DEV_SEED_TENANTS``. Refuses production.

    MUST run after lifespan wiring (buffered Postgres proxies), never in
    ``AppContext.__init__``: rows must land in Postgres (write-through) so
    API reads (primary-first) and the Temporal worker (separate process)
    both see them. In-memory-only seeds are shadowed and worker-invisible.
    """
    from investigation_agent_platform.infrastructure.configuration.layers import (
        effective_environment,
    )

    if effective_environment() == "production":
        raise RuntimeError("Dev seed is refused in production")
    result: dict[str, dict[str, int]] = {}
    for tenant in seed_tenants_from_env():
        result[tenant] = await seed_dev_tenant(ctx, tenant)
    return result
