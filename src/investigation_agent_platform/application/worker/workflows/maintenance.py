# src/investigation_agent_platform/application/worker/workflows/maintenance.py
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    pass

with workflow.unsafe.imports_passed_through():
    from investigation_agent_platform.application.worker.activities import (
        CollectSnapshotsInput,
        CollectSnapshotsOutput,
        ProjectTopologyInput,
        SweepKnowledgeInput,
        collect_snapshots_activity,
        project_topology_activity,
        sweep_knowledge_activity,
    )

with workflow.unsafe.imports_passed_through():
    # G6-measure shadow capture: pure compute-and-log helpers (no I/O,
    # no clock, no execution). Imported for the per-turn shadow replay;
    # the workflow never branches on its output.

    # D3 Phase 0 shadow definitions: pure receipt pairing + summary
    # (canonical digests only — deterministic, replay-safe). The
    # workflow never branches on the paired output.
    pass

with workflow.unsafe.imports_passed_through():
    # G3: ledger directive helpers — pure, deterministic, replay-safe
    # (hashlib/re only at call time; no I/O, no clock, no process-local
    # ledger reads). Imported passed-through like the shadow helpers
    # because the application layer is not import-safe in the sandbox.
    pass


@dataclass
class TopologySnapshotRetentionInput:
    """One scheduled retention pass over a set of repositories (ISSUE-4).

    Intended to be started on a Temporal ``Schedule`` (e.g. daily) — either
    one schedule per ``(tenant_id, repository_id)`` pair, or one schedule
    invoking this workflow with the full list an operator maintains. Each
    repository is collected via its own retried activity so one failing
    repository never blocks the rest of the pass.
    """

    repositories: list[tuple[str, str]]  # (tenant_id, repository_id) pairs


@dataclass
class TopologySnapshotRetentionResult:
    per_repository: dict[str, CollectSnapshotsOutput]


@dataclass
class KnowledgeArtifactJanitorInput:
    """Scheduled janitor pass over a set of tenants (ISSUE-9)."""

    tenants: list[str]
    reverify_conditional: bool = False


@dataclass
class KnowledgeArtifactJanitorResult:
    per_tenant: dict[str, int]


@workflow.defn
class KnowledgeArtifactJanitorWorkflow:
    """Scheduled sweep: TTL-elapsed -> EXPIRED via the retrieval transition
    path. Idempotent; never mutates history, only advances status."""

    @workflow.run
    async def run(
        self, input_data: KnowledgeArtifactJanitorInput
    ) -> KnowledgeArtifactJanitorResult:
        retry_policy = RetryPolicy(maximum_attempts=3)
        output = await workflow.execute_activity(
            sweep_knowledge_activity,
            SweepKnowledgeInput(
                tenants=list(input_data.tenants),
                reverify_conditional=input_data.reverify_conditional,
            ),
            start_to_close_timeout=timedelta(seconds=120),
            retry_policy=retry_policy,
        )
        data = output.data if hasattr(output, "data") else {}
        return KnowledgeArtifactJanitorResult(per_tenant=dict(data.get("per_tenant", {})))


@workflow.defn
class TopologySnapshotRetentionWorkflow:
    """Scheduled janitor: classifies and collects unpinned, out-of-window
    topology snapshots per repository (ISSUE-4). Never touches a snapshot
    referenced by an open investigation's own recorded evidence, and never
    deletes a ``TopologySnapshot`` audit node — only the AST subgraph
    beneath it.
    """

    @workflow.run
    async def run(
        self, input_data: TopologySnapshotRetentionInput
    ) -> TopologySnapshotRetentionResult:
        retry_policy = RetryPolicy(maximum_attempts=3)
        results: dict[str, CollectSnapshotsOutput] = {}
        for tenant_id, repository_id in input_data.repositories:
            output = await workflow.execute_activity(
                collect_snapshots_activity,
                CollectSnapshotsInput(tenant_id=tenant_id, repository_id=repository_id),
                start_to_close_timeout=timedelta(seconds=120),
                retry_policy=retry_policy,
            )
            results[f"{tenant_id}/{repository_id}"] = output
        return TopologySnapshotRetentionResult(per_repository=results)


@dataclass
class TopologyProjectionInput:
    """One scheduled topology projection pass (cognee pilot).

    Intended for a Temporal Schedule per (tenant, repository) or on webhook
    push. `revision` empty resolves from the checkout HEAD in the activity;
    `changed_files` is recorded for future incremental work.
    """

    tenant_id: str
    repository_id: str
    revision: str = ""
    checkout: str = ""
    application_id: str = ""
    changed_files: list[str] = field(default_factory=list)


@dataclass
class TopologyProjectionResult:
    status: str
    dataset: str = ""
    points: int = 0
    edges: int = 0
    revision: str = ""


@workflow.defn
class TopologyProjectionWorkflow:
    """Scheduled projection: local checkout → deterministic topology points.

    Idempotent per (tenant, repository, revision): re-projecting the same
    revision rewrites identical point IDs (no duplicates). Skips (never
    fails the schedule) when the pilot is disabled or the checkout is
    absent — genuine projection failures retry per repository policy.
    """

    @workflow.run
    async def run(self, input_data: TopologyProjectionInput) -> TopologyProjectionResult:
        retry_policy = RetryPolicy(maximum_attempts=3)
        output = await workflow.execute_activity(
            project_topology_activity,
            ProjectTopologyInput(
                tenant_id=input_data.tenant_id,
                repository_id=input_data.repository_id,
                revision=input_data.revision,
                checkout=input_data.checkout,
                application_id=input_data.application_id,
                changed_files=list(input_data.changed_files),
            ),
            start_to_close_timeout=timedelta(seconds=600),
            retry_policy=retry_policy,
        )
        data = output.data if hasattr(output, "data") else {}
        return TopologyProjectionResult(
            status=str(data.get("status", "unknown")),
            dataset=str(data.get("dataset", "")),
            points=int(data.get("points", 0) or 0),
            edges=int(data.get("edges", 0) or 0),
            revision=str(data.get("revision", "")),
        )
