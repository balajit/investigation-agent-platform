# src/investigation_agent_platform/application/worker/activities/topology.py
"""Temporal activity thin adapters — each delegates to the application/domain layer via AppContext."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from temporalio import activity

try:
    from temporalio.exceptions import ApplicationFailure  # type: ignore[attr-defined]
except ImportError:
    from temporalio.exceptions import (
        ApplicationError as ApplicationFailure,  # type: ignore[assignment, no-redef]
    )

logger = logging.getLogger(__name__)

from investigation_agent_platform.infrastructure.observability.tracing import traced

from .common import (
    COMP_ACTVTY,
    OUTCOME_COMPLETED,
    OUTCOME_FAILED,
    OUTCOME_SKIPPED,
    OUTCOME_STARTED,
    _activity_extra,
    _activity_outcome,
    _application_failure_from_exc,
    _coerced_data,
    elapsed_ms,
    story_log,
    tick,
)
from .inputs import GenericActivityResult, _get_ctx


@dataclass
class CollectSnapshotsInput:
    """One repository's snapshot-retention pass (ISSUE-4)."""

    tenant_id: str
    repository_id: str


@dataclass
class CollectSnapshotsOutput:
    collected_revisions: list[str] = field(default_factory=list)
    pinned_count: int = 0
    deleted_node_count: int = 0
    deleted_edge_count: int = 0
    error: str | None = None


@activity.defn
async def collect_snapshots_activity(
    params: CollectSnapshotsInput,
) -> CollectSnapshotsOutput:
    # TO_DO-011: not idempotency-wired — collection is idempotent by design
    # (already-collected snapshots report, never duplicate; audit nodes are
    # never deleted).
    """Classify one repository's topology snapshots and collect the
    unpinned, out-of-window ones (ISSUE-4 janitor).

    Never deletes a ``TopologySnapshot`` audit node, only the AST subgraph
    beneath it, and never touches anything pinned by an open investigation's
    own recorded evidence. Collection failures are reported, not raised —
    a transient Neo4j hiccup on one repository must not fail the whole
    scheduled pass (the workflow retries this activity per repository).
    """
    t0 = tick()
    data = _coerced_data(CollectSnapshotsInput, params, activity="collect_snapshots_activity")
    tenant_id_probe = str(data.get("tenant_id", ""))
    story_log(
        logger,
        logging.INFO,
        "collect_snapshots_activity started",
        comp=COMP_ACTVTY,
        func="collect_snapshots_activity",
        stage="execute",
        step="collect",
        outcome=OUTCOME_STARTED,
        tenant_id=tenant_id_probe,
        event="collect_snapshots_activity started",
        inputs={"repository_id": str(data.get("repository_id", ""))},
        repository_id=str(data.get("repository_id", "")),
    )
    tenant_id = str(data.get("tenant_id", ""))
    repository_id = str(data.get("repository_id", ""))
    if not tenant_id or not repository_id:
        return CollectSnapshotsOutput(error="tenant_id and repository_id required")

    try:
        from datetime import UTC, datetime

        from investigation_agent_platform.application.topology.pinned_revisions import (
            EvidenceBackedPinnedRevisionsProvider,
        )
        from investigation_agent_platform.application.topology.retention import (
            classify_snapshots,
        )

        ctx = _get_ctx()
        topology_adapter: Any = getattr(ctx, "topology_adapter", None)
        if topology_adapter is None:
            return CollectSnapshotsOutput(error="topology_adapter is not configured")
        topology_config: Any = getattr(ctx, "topology_config", None)
        if topology_config is None or not getattr(topology_config, "retention_enabled", False):
            return CollectSnapshotsOutput(error="retention is not enabled")

        pinned_provider = EvidenceBackedPinnedRevisionsProvider(
            investigation_repo=ctx.investigation_repo,
            evidence_repo=ctx.evidence_repo,
        )
        pinned_revisions = await pinned_provider.list_pinned_revisions(tenant_id, repository_id)
        snapshots = await topology_adapter.list_snapshots(tenant_id, repository_id)
        classification = classify_snapshots(
            snapshots,
            pinned_revisions,
            now=datetime.now(UTC),
            retention_window_days=topology_config.retention_window_days,
            retention_max_snapshots_per_repository=(
                topology_config.retention_max_snapshots_per_repository
            ),
        )

        collected: list[str] = []
        deleted_nodes = 0
        deleted_edges = 0
        for snapshot in classification.collectible:
            result = await topology_adapter.collect_snapshot(
                tenant_id, repository_id, snapshot.revision
            )
            if not result.already_collected:
                collected.append(snapshot.revision)
                deleted_nodes += result.deleted_node_count
                deleted_edges += result.deleted_edge_count

        story_log(
            logger,
            logging.INFO,
            "collect_snapshots_activity completed",
            comp=COMP_ACTVTY,
            func="collect_snapshots_activity",
            stage="execute",
            step="collect",
            outcome=OUTCOME_COMPLETED,
            tenant_id=tenant_id,
            event="collect_snapshots_activity completed",
            outputs={"outcome": "collected", "collected_count": len(collected)},
            elapsed_ms=elapsed_ms(t0),
            repository_id=repository_id,
            collected_count=len(collected),
        )
        return CollectSnapshotsOutput(
            collected_revisions=collected,
            pinned_count=len(classification.pinned),
            deleted_node_count=deleted_nodes,
            deleted_edge_count=deleted_edges,
        )
    except ApplicationFailure:
        raise
    except Exception as exc:
        story_log(
            logger,
            logging.ERROR,
            "collect_snapshots_activity failed",
            comp=COMP_ACTVTY,
            func="collect_snapshots_activity",
            stage="execute",
            step="collect",
            outcome=OUTCOME_FAILED,
            tenant_id=tenant_id_probe,
            event="collect_snapshots_activity failed",
            error=str(exc),
            error_type=type(exc).__name__,
            exc_info=True,
            repository_id=repository_id,
        )
        return CollectSnapshotsOutput(error=str(exc))


# ---------------------------------------------------------------------------
# Part 11.5: structured input requirements
# ---------------------------------------------------------------------------


@dataclass
class ProjectTopologyInput:
    """One topology projection pass over a local checkout (cognee pilot).

    `checkout` is resolved under `IAP_CODE_REPO_BASE` with containment
    enforcement; absolute/traversal paths are rejected. `revision` defaults
    to the checkout's HEAD when empty. `changed_files` (from webhook pushes)
    is recorded for future incremental work; the payload rebuild stays full
    (edge resolution spans files — partial projection would corrupt CALLS).
    """

    tenant_id: str = ""
    repository_id: str = ""
    revision: str = ""
    checkout: str = ""
    application_id: str = ""
    changed_files: list[str] = field(default_factory=list)


@activity.defn
@traced("worker", outcome_from=_activity_outcome, extra_from=_activity_extra)
async def project_topology_activity(
    params: ProjectTopologyInput,
) -> GenericActivityResult:
    """Project one repo revision into the topology store (scheduled janitor shape).

    Skips (success, status=skipped) when the pilot is disabled, the checkout
    is absent/unresolvable, or config is missing — a janitor never fails a
    schedule on a missing repo. Genuine projection failures raise for
    Temporal retry.
    """
    import os

    t0 = tick()
    data = _coerced_data(ProjectTopologyInput, params, activity="project_topology_activity")
    tenant_id = str(data.get("tenant_id", ""))
    repository_id = str(data.get("repository_id", ""))
    if not tenant_id or not repository_id:
        return GenericActivityResult(success=False, error="tenant_id and repository_id required")

    def _skip(reason: str) -> GenericActivityResult:
        story_log(
            logger,
            logging.INFO,
            "project_topology_activity skipped",
            comp=COMP_ACTVTY,
            func="project_topology_activity",
            stage="execute",
            step="project",
            outcome=OUTCOME_SKIPPED,
            tenant_id=tenant_id,
            event="project_topology skipped",
            outputs={"status": "skipped", "reason": reason},
            elapsed_ms=elapsed_ms(t0),
            reason=reason,
            repository_id=repository_id,
        )
        return GenericActivityResult(success=True, data={"status": "skipped", "reason": reason})

    if os.environ.get("IAP_COGNEE_ENABLED", "false").lower() != "true":
        return _skip("cognee pilot disabled")
    salt = os.environ.get("IAP_COGNEE_DATASET_SALT", "")
    if not salt:
        return _skip("dataset salt unconfigured")

    from pathlib import Path

    base = Path(os.environ.get("IAP_CODE_REPO_BASE", "") or "data/code-repos").resolve()
    requested = str(data.get("checkout", "") or "")
    candidates: list[Path] = []
    if requested:
        candidates.append((base / requested).resolve())
    else:
        # Layout candidates: flat slug first, then nested org/repo
        # (self-triage worktrees live nested).
        candidates.append((base / repository_id).resolve())
        if "--" in repository_id:
            org, _, name = repository_id.partition("--")
            if org and name:
                candidates.append((base / org / name).resolve())
    candidate: Path | None = None
    for path in candidates:
        try:
            inside = str(path).startswith(str(base) + os.sep) or path == base
        except Exception:
            continue
        if not inside:
            continue
        if path.is_dir():
            candidate = path
            break
    if candidate is None:
        # Distinguish escape (reject loudly in logs) from absence (skip).
        for path in candidates:
            try:
                if not (str(path).startswith(str(base) + os.sep) or path == base):
                    return _skip("checkout escapes repo base")
            except Exception:
                continue
        return _skip("checkout absent")

    revision = str(data.get("revision", "") or "")
    if not revision:
        try:
            import pygit2  # type: ignore[import-not-found]

            revision = str(pygit2.Repository(str(candidate)).head.target)
        except Exception:
            revision = ""
    if not revision:
        return _skip("revision unresolvable")

    try:
        from investigation_agent_platform.infrastructure.topology.cognee_adapter import (
            CogneeTopologyAdapter,
            SharedNeo4jDriver,
        )
        from investigation_agent_platform.infrastructure.topology.ownership import (
            derive_repository_domain,
        )
        from investigation_agent_platform.infrastructure.topology.payload import (
            build_payload_for_repo,
        )

        application_id = str(data.get("application_id", "") or f"{tenant_id}-{repository_id}")
        payload, truncated = await build_payload_for_repo(
            candidate, tenant_id, application_id, repository_id, revision
        )
        if truncated:
            return GenericActivityResult(
                success=False, error="payload truncated; refusing partial projection"
            )
        ownership_path = []
        try:
            domain = await asyncio.to_thread(derive_repository_domain, candidate)
            if domain:
                ownership_path.append(domain)
        except Exception:
            pass
        holder = await SharedNeo4jDriver.from_env().connect()
        try:
            adapter = CogneeTopologyAdapter(enabled=True, dataset_salt=salt, neo4j_driver=holder)
            result = await adapter.project_snapshot(payload, ownership_path=ownership_path)
        finally:
            await holder.close()
        changed = list(data.get("changed_files", []) or [])
        story_log(
            logger,
            logging.INFO,
            "project_topology_activity completed",
            comp=COMP_ACTVTY,
            func="project_topology_activity",
            stage="execute",
            step="project",
            outcome=OUTCOME_COMPLETED,
            tenant_id=tenant_id,
            event="project_topology completed",
            outputs={
                "dataset": result.dataset,
                "points": result.point_count,
                "edges": result.edge_count,
                "revision": revision[:12],
            },
            elapsed_ms=elapsed_ms(t0),
            repository_id=repository_id,
            revision=revision[:12],
            dataset=result.dataset,
            points=result.point_count,
            edges=result.edge_count,
            changed_files=len(changed),
        )
        return GenericActivityResult(
            success=True,
            data={
                "status": "projected",
                "dataset": result.dataset,
                "points": result.point_count,
                "edges": result.edge_count,
                "revision": revision,
                "changed_files": changed,
            },
        )
    except ApplicationFailure:
        raise
    except Exception as exc:
        story_log(
            logger,
            logging.ERROR,
            "project_topology_activity failed",
            comp=COMP_ACTVTY,
            func="project_topology_activity",
            stage="execute",
            step="project",
            outcome=OUTCOME_FAILED,
            tenant_id=tenant_id,
            event="project_topology failed",
            error=str(exc),
            error_type=type(exc).__name__,
            exc_info=True,
            repository_id=repository_id,
        )
        raise _application_failure_from_exc(exc) from exc
