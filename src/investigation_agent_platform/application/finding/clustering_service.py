# src/investigation_agent_platform/application/finding/clustering_service.py
"""Incremental cross-investigation finding clustering (Part 11.7).

Generalizes batch LLM assignment over a semantic candidate set: unassigned
findings are batched (default 25), matched against lexical (+ collaborative)
candidates from platform-owned tables, and assigned via strict-schema LLM
output. Every input finding gets exactly one output — malformed or
unsupported results land in the explicit UNASSIGNED bucket with provenance,
never silently dropped. Runs are bounded, quota-guarded, and resumable by
re-invocation (assigned findings are skipped).
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from investigation_agent_platform.domain.common.provenance import ProvenanceRecord
from investigation_agent_platform.domain.finding.clustering import (
    DEFAULT_CLUSTER_BATCH_SIZE,
    MAX_TAXONOMY_CLUSTERS,
    UNASSIGNED_CLUSTER_ID,
    ClusterAssignmentResult,
    FindingCluster,
    FindingClusterAssignment,
    FindingEmbedding,
)
from investigation_agent_platform.domain.finding.models import Finding

logger = logging.getLogger(__name__)

CLUSTERING_PROMPT_VERSION = "1.0"
CLUSTERING_MODEL_FAMILY = "clustering-llm"
UNASSIGNED_REASON_CAP = "taxonomy at capacity; merged into UNASSIGNED"

_ASSIGN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "assignments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "finding_id": {"type": "string"},
                    "cluster_key": {"type": "string"},
                    "new_cluster": {
                        "type": ["object", "null"],
                        "properties": {
                            "key": {"type": "string"},
                            "label": {"type": "string"},
                            "description": {"type": "string"},
                        },
                    },
                    "confidence": {"type": "number"},
                },
                "required": ["finding_id", "cluster_key"],
            },
        }
    },
    "required": ["assignments"],
}

_SYSTEM_PROMPT = (
    "You cluster software-investigation findings into root-cause patterns. "
    "Assign every finding to exactly one cluster: prefer an existing cluster, "
    "propose a new one only when nothing fits. "
    f'Use cluster key "{UNASSIGNED_CLUSTER_ID}" only when a finding carries '
    "no actionable signal at all. Return ONLY the JSON object matching the schema."
)


class ClusteringRunResult(BaseModel):
    """Summary of one incremental clustering run (Part 11.7)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: str
    taxonomy_revision: int
    examined: int
    assigned: int
    unassigned: int
    new_clusters: int
    prompt_version: str = CLUSTERING_PROMPT_VERSION
    generated_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())


def _finding_text(finding: Finding) -> str:
    return f"{finding.title} {finding.statement}".strip()


def _next_cluster_key(existing_keys: list[str]) -> str:
    taken = set(existing_keys)
    candidate = 1
    while True:
        key = f"C{candidate:03d}"
        if key not in taken:
            return key
        candidate += 1


class FindingClusterService:
    """Category-agnostic incremental clustering over persisted findings."""

    def __init__(
        self,
        finding_repo: Any,
        cluster_repo: Any,
        llm_gateway: Any | None = None,
        embedder: Any | None = None,
        batch_size: int = DEFAULT_CLUSTER_BATCH_SIZE,
    ) -> None:
        self._finding_repo = finding_repo
        self._cluster_repo = cluster_repo
        self._llm_gateway = llm_gateway
        self._embedder = embedder
        self._batch_size = max(1, min(batch_size, 100))

    async def run_incremental(
        self,
        tenant_id: str,
        limit: int = 500,
        quota_enforcer: Any | None = None,
        quota_policy: Any | None = None,
        scope: Any | None = None,
    ) -> ClusteringRunResult:
        """Cluster up to ``limit`` unassigned findings for one tenant."""
        if quota_enforcer is not None and quota_policy is not None and scope is not None:
            decision = await quota_enforcer.check(scope, "clustering.run", quota_policy)
            if not decision.allowed:
                from investigation_agent_platform.infrastructure.observability.job_telemetry import (
                    record_quota_decision,
                )

                record_quota_decision(None, "clustering.run", False, tenant_id)
                raise QuotaExceededError(tenant_id, decision)
            try:
                return await self._run_bounded(tenant_id, limit)
            finally:
                await quota_enforcer.release(scope, "clustering.run")
        return await self._run_bounded(tenant_id, limit)

    async def _run_bounded(self, tenant_id: str, limit: int) -> ClusteringRunResult:
        findings, _ = await self._finding_repo.list_tenant_findings(
            tenant_id, limit=max(1, min(limit, 2000))
        )
        if not findings:
            return ClusteringRunResult(
                tenant_id=tenant_id,
                taxonomy_revision=await self._cluster_repo.current_revision(tenant_id),
                examined=0,
                assigned=0,
                unassigned=0,
                new_clusters=0,
            )
        assigned_ids = await self._cluster_repo.assigned_finding_ids(
            tenant_id, [finding.id for finding in findings]
        )
        unassigned = [finding for finding in findings if finding.id not in assigned_ids]
        taxonomy = await self._cluster_repo.load_taxonomy(tenant_id)
        revision = await self._cluster_repo.current_revision(tenant_id)
        revision = max(1, revision)

        if self._embedder is not None:
            await self._embed_batch(tenant_id, unassigned)

        examined = 0
        assigned = 0
        unassigned_count = 0
        new_clusters = 0
        for batch in _chunks(unassigned, self._batch_size):
            results = await self._assign_batch(tenant_id, batch, taxonomy)
            for finding, result in zip(batch, results):
                examined += 1
                if result.cluster_key == UNASSIGNED_CLUSTER_ID:
                    unassigned_count += 1
                else:
                    assigned += 1
                if result.new_cluster is not None:
                    new_clusters += 1
                    taxonomy = await self._cluster_repo.load_taxonomy(tenant_id)
                await self._persist_assignment(tenant_id, finding, result, revision)
        return ClusteringRunResult(
            tenant_id=tenant_id,
            taxonomy_revision=revision,
            examined=examined,
            assigned=assigned,
            unassigned=unassigned_count,
            new_clusters=new_clusters,
        )

    async def _embed_batch(self, tenant_id: str, findings: list[Finding]) -> None:
        assert self._embedder is not None
        try:
            results = await self._embedder.embed([_finding_text(finding) for finding in findings])
        except Exception as exc:
            logger.warning(
                "Finding embedding failed; continuing lexical-only",
                extra={"tenant_id": tenant_id, "error": str(exc)},
            )
            return
        generation = await self._cluster_repo.active_generation(
            tenant_id, _model_of(results), _version_of(results)
        )
        generation = max(1, generation)
        for finding, result in zip(findings, results):
            if result is None:
                continue
            await self._cluster_repo.upsert_finding_embedding(
                tenant_id,
                FindingEmbedding(
                    finding_id=finding.id,
                    tenant_id=tenant_id,
                    embedding_model=result.embedding_model,
                    embedding_version=result.embedding_version,
                    generation=generation,
                    vector=list(result.vector),
                    lexical_text=_finding_text(finding)[:4000],
                    provenance=ProvenanceRecord(
                        plugin_id="finding-clusterer",
                        plugin_version="1.0",
                        model_id=result.embedding_model,
                        input_digests={"finding_id": str(finding.id)},
                    ),
                ),
            )

    async def _assign_batch(
        self,
        tenant_id: str,
        batch: list[Finding],
        taxonomy: list[FindingCluster],
    ) -> list[ClusterAssignmentResult]:
        """Assign one batch; every input yields exactly one result."""
        if self._llm_gateway is None:
            logger.warning(
                "No LLM gateway configured; findings explicitly UNASSIGNED",
                extra={"tenant_id": tenant_id, "count": len(batch)},
            )
            return [
                ClusterAssignmentResult(
                    finding_id=finding.id,
                    cluster_key=UNASSIGNED_CLUSTER_ID,
                    confidence=0.0,
                )
                for finding in batch
            ]
        candidates = await self._collect_candidates(tenant_id, batch, taxonomy)
        prompt = _build_assign_prompt(batch, candidates)
        try:
            from investigation_agent_platform.ports.reasoning.llm_gateway import (
                LLMGatewayRequest,
            )

            response = await self._llm_gateway.complete(
                tenant_id,
                LLMGatewayRequest(
                    prompt=prompt,
                    system_prompt=_SYSTEM_PROMPT,
                    response_schema=_ASSIGN_SCHEMA,
                    temperature=0.0,
                ),
            )
            payload = _extract_assignments(response)
        except Exception as exc:
            logger.warning(
                "Cluster assignment LLM call failed; batch UNASSIGNED",
                extra={"tenant_id": tenant_id, "error": str(exc)},
            )
            return [
                ClusterAssignmentResult(
                    finding_id=finding.id,
                    cluster_key=UNASSIGNED_CLUSTER_ID,
                    confidence=0.0,
                )
                for finding in batch
            ]
        return self._validate_assignments(tenant_id, batch, taxonomy, payload)

    async def _collect_candidates(
        self,
        tenant_id: str,
        batch: list[Finding],
        taxonomy: list[FindingCluster],
    ) -> list[FindingCluster]:
        """Bounded candidate set: lexical taxonomy hits + collaborative hits."""
        texts = [_finding_text(finding) for finding in batch]
        query = " ".join(texts)[:2000]
        lexical = await self._cluster_repo.find_candidate_clusters(tenant_id, query, limit=12)
        by_key = {cluster.cluster_key: cluster for cluster in taxonomy}
        similar_ids = await self._cluster_repo.find_similar_assigned_clusters(
            tenant_id, query, limit=12
        )
        by_id = {cluster.id: cluster for cluster in taxonomy}
        ordered: list[FindingCluster] = []
        for cluster in list(lexical) + [by_id[cid] for cid in similar_ids if cid in by_id]:
            if cluster.cluster_key not in by_key:
                continue
            if all(existing.id != cluster.id for existing in ordered):
                ordered.append(cluster)
            if len(ordered) >= MAX_TAXONOMY_CLUSTERS:
                break
        return ordered or list(taxonomy[:MAX_TAXONOMY_CLUSTERS])

    def _validate_assignments(
        self,
        tenant_id: str,
        batch: list[Finding],
        taxonomy: list[FindingCluster],
        payload: dict[str, Any],
    ) -> list[ClusterAssignmentResult]:
        """Enforce the exactly-one-result invariant; malformed → UNASSIGNED."""
        by_finding = {str(finding.id): finding for finding in batch}
        raw_assignments = payload.get("assignments")
        if not isinstance(raw_assignments, list):
            return self._all_unassigned(batch, "response missing assignments array")
        by_key = {cluster.cluster_key: cluster for cluster in taxonomy}
        seen: set[str] = set()
        minted_keys: set[str] = set()
        results: dict[str, ClusterAssignmentResult] = {}
        for raw in raw_assignments:
            if not isinstance(raw, dict):
                continue
            finding_id = str(raw.get("finding_id", ""))
            finding = by_finding.get(finding_id)
            if finding is None or finding_id in seen:
                continue
            seen.add(finding_id)
            cluster_key = str(raw.get("cluster_key", "") or "")
            confidence = _coerce_confidence(raw.get("confidence"))
            new_cluster = raw.get("new_cluster")
            if cluster_key == UNASSIGNED_CLUSTER_ID:
                # Explicitly unassigned: any attached new_cluster payload is
                # ignored (a key must be claimed, never smuggled).
                results[finding_id] = ClusterAssignmentResult(
                    finding_id=finding.id,
                    cluster_key=UNASSIGNED_CLUSTER_ID,
                    confidence=confidence,
                )
                continue
            if cluster_key not in by_key:
                if cluster_key in minted_keys:
                    # Joins a cluster minted earlier in this same batch: no new
                    # payload needed; persistence resolves the id by key.
                    results[finding_id] = ClusterAssignmentResult(
                        finding_id=finding.id,
                        cluster_key=cluster_key,
                        confidence=confidence,
                    )
                    continue
                if isinstance(new_cluster, dict):
                    created_key = self._propose_cluster_key(
                        tenant_id, taxonomy, new_cluster, minted_keys
                    )
                    if created_key is None:
                        results[finding_id] = ClusterAssignmentResult(
                            finding_id=finding.id,
                            cluster_key=UNASSIGNED_CLUSTER_ID,
                            confidence=confidence,
                        )
                        continue
                    minted_keys.add(created_key)
                    label = str(new_cluster.get("label", created_key))[:128]
                    description = str(new_cluster.get("description", ""))[:512]
                    results[finding_id] = ClusterAssignmentResult(
                        finding_id=finding.id,
                        cluster_key=created_key,
                        new_cluster={
                            "key": created_key,
                            "label": label,
                            "description": description,
                        },
                        confidence=confidence,
                    )
                    continue
                results[finding_id] = ClusterAssignmentResult(
                    finding_id=finding.id,
                    cluster_key=UNASSIGNED_CLUSTER_ID,
                    confidence=confidence,
                )
                continue
            results[finding_id] = ClusterAssignmentResult(
                finding_id=finding.id,
                cluster_key=cluster_key,
                confidence=confidence,
            )
        ordered: list[ClusterAssignmentResult] = []
        for finding in batch:
            result = results.get(str(finding.id))
            if result is None:
                result = ClusterAssignmentResult(
                    finding_id=finding.id,
                    cluster_key=UNASSIGNED_CLUSTER_ID,
                    confidence=0.0,
                )
            ordered.append(result)
        return ordered

    def _propose_cluster_key(
        self,
        tenant_id: str,
        taxonomy: list[FindingCluster],
        new_cluster: dict[str, Any],
        minted_keys: set[str],
    ) -> str | None:
        """Allocate a key for a proposed cluster, or None when at capacity."""
        _ = tenant_id
        proposed = str(new_cluster.get("key", "") or "")
        existing = {cluster.cluster_key for cluster in taxonomy} | minted_keys
        if proposed and proposed not in existing and len(taxonomy) < MAX_TAXONOMY_CLUSTERS:
            return proposed
        if len(taxonomy) >= MAX_TAXONOMY_CLUSTERS:
            logger.warning(
                "Taxonomy at capacity; new cluster merged into UNASSIGNED",
                extra={"tenant_id": tenant_id},
            )
            return None
        return _next_cluster_key(
            [cluster.cluster_key for cluster in taxonomy] + sorted(minted_keys)
        )

    def _all_unassigned(self, batch: list[Finding], reason: str) -> list[ClusterAssignmentResult]:
        logger.warning(
            "Cluster assignment output invalid; batch UNASSIGNED",
            extra={"reason": reason, "count": len(batch)},
        )
        return [
            ClusterAssignmentResult(
                finding_id=finding.id,
                cluster_key=UNASSIGNED_CLUSTER_ID,
                confidence=0.0,
            )
            for finding in batch
        ]

    async def _persist_assignment(
        self,
        tenant_id: str,
        finding: Finding,
        result: ClusterAssignmentResult,
        revision: int,
    ) -> None:
        """Persist cluster row (if new) before its assignment — never reversed."""
        cluster_id: UUID | None = None
        if result.new_cluster is not None:
            cluster = FindingCluster(
                id=uuid4(),
                tenant_id=tenant_id,
                cluster_key=result.new_cluster["key"],
                label=result.new_cluster.get("label", result.new_cluster["key"]),
                description=result.new_cluster.get("description", ""),
                taxonomy_revision=revision,
            )
            await self._cluster_repo.create_cluster(tenant_id, cluster)
            cluster_id = cluster.id
        elif result.cluster_key != UNASSIGNED_CLUSTER_ID:
            taxonomy = await self._cluster_repo.load_taxonomy(tenant_id)
            match = next((c for c in taxonomy if c.cluster_key == result.cluster_key), None)
            cluster_id = match.id if match is not None else None
        await self._cluster_repo.assign_finding(
            tenant_id,
            FindingClusterAssignment(
                tenant_id=tenant_id,
                finding_id=finding.id,
                cluster_id=cluster_id,
                method="llm-assign",
                confidence=result.confidence,
                taxonomy_revision=revision,
                provenance=ProvenanceRecord(
                    plugin_id="finding-clusterer",
                    plugin_version="1.0",
                    prompt_version=CLUSTERING_PROMPT_VERSION,
                    input_digests={"finding_id": str(finding.id)},
                ),
            ),
        )


def _chunks(items: list[Finding], size: int) -> list[list[Finding]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def _coerce_confidence(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.5


def _model_of(results: list[Any]) -> str:
    for result in results:
        try:
            model = result.embedding_model
        except AttributeError:
            continue
        if model:
            return str(model)
    return "unknown"


def _version_of(results: list[Any]) -> str:
    for result in results:
        try:
            version = result.embedding_version
        except AttributeError:
            continue
        if version:
            return str(version)
    return "1.0"


def _build_assign_prompt(batch: list[Finding], candidates: list[FindingCluster]) -> str:
    findings = "\n".join(
        f"- id={finding.id} type={finding.finding_type.value} "
        f"title={finding.title[:120]} statement={finding.statement[:300]}"
        for finding in batch
    )
    clusters = (
        "\n".join(
            f"- key={cluster.cluster_key} label={cluster.label} "
            f"description={cluster.description[:200]}"
            for cluster in candidates
        )
        or "(no existing clusters)"
    )
    return (
        f"Findings to classify:\n{findings}\n\n"
        f"Existing clusters:\n{clusters}\n\n"
        "Assign every finding by id. For a new cluster include new_cluster "
        "with key (format C followed by digits, e.g. C007), label, description."
    )


def _extract_assignments(response: Any) -> dict[str, Any]:
    parsed = getattr(response, "parsed", None)
    if isinstance(parsed, dict) and isinstance(parsed.get("assignments"), list):
        return parsed
    content = getattr(response, "content", "") or ""
    try:
        payload = json.loads(content)
    except (ValueError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


class QuotaExceededError(Exception):
    """Raised when a clustering run is quota-rejected before dispatch."""

    def __init__(self, tenant_id: str, decision: Any) -> None:
        super().__init__(f"Clustering quota exceeded for tenant {tenant_id}")
        self.tenant_id = tenant_id
        self.decision = decision


def finding_clustering_schedules(
    tenant_ids: list[str],
    interval_hours: int = 24,
    task_queue: str = "analytics-tasks",
) -> list[Any]:
    """Build schedule descriptors for periodic clustering (Part 11.7).

    Returned descriptors are reconciled by the Phase 11.3 schedule
    reconciler at startup/operator invocation — this helper only declares
    desired state (stable ids, SKIP overlap, jitter, no backfill).
    """
    from investigation_agent_platform.domain.common.schedules import (
        ScheduleDescriptor,
        ScheduleOverlapPolicy,
    )

    descriptors: list[Any] = []
    for tenant_id in tenant_ids:
        descriptors.append(
            ScheduleDescriptor(
                schedule_id=f"finding-clustering-{tenant_id}",
                workflow_type="FindingClusteringWorkflow",
                task_queue=task_queue,
                interval_seconds=max(3600, interval_hours * 3600),
                overlap_policy=ScheduleOverlapPolicy.SKIP,
                jitter_seconds=300,
                paused=False,
                catchup_backfill=False,
                tenant_id=tenant_id,
            )
        )
    return descriptors
