# src/investigation_agent_platform/application/topology/retention.py
"""Snapshot retention policy (ISSUE-4).

Design D9 makes topology snapshots immutable with a ``SUPERSEDED`` state,
but nothing previously deleted anything: per-commit
``(tenant_id, repository_id, revision)`` snapshots accumulate forever and
Neo4j eventually runs out of disk (``docs/ISSUES_0926.md`` risk 2).

Policy (exact pinning rules and window sizes):

1. **Pinned — never collectible, regardless of age:**
   - Any snapshot whose revision is referenced by an *open* investigation
     (any status other than ``COMPLETED``/``FAILED``/``CANCELLED``) —
     resolved by ``PinnedRevisionsPort`` from each investigation's own
     topology-attribution evidence, never guessed.
   - Any snapshot not in ``READY`` or ``SUPERSEDED`` status (``PENDING``/
     ``INGESTING``/``FAILED`` snapshots are mid-flight or broken, not
     eligible for collection either way).
   - Any snapshot already ``COLLECTED`` (nothing left to collect).

2. **Kept by policy (LRU bound):** among the remaining READY/SUPERSEDED,
   unpinned snapshots per repository, the ``retention_max_snapshots_per_repository``
   most-recently-ingested are kept even if older than the window — this is
   the "keep HEAD snapshots" rule from the design doc, approximated by
   recency since this platform does not track which revision is HEAD on
   which branch.

3. **Kept by policy (time window):** any snapshot ingested within
   ``retention_window_days`` is kept regardless of the LRU bound.

4. **Collectible:** everything else — unpinned, READY or SUPERSEDED,
   older than the window, and beyond the per-repository LRU bound.

The janitor (Temporal workflow) calls ``classify_snapshots`` with data from
``SnapshotRetentionPort.list_snapshots`` and a caller-supplied pinned-revision
set, then calls ``SnapshotRetentionPort.collect_snapshot`` only on the
collectible list — this module never touches the store directly, so it is
fully unit-testable without Neo4j.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from investigation_agent_platform.domain.topology.models import (
    SnapshotDescriptor,
    TopologySnapshotStatus,
)

#: Statuses a snapshot must be in to even be considered for collection.
#: PENDING/INGESTING are mid-flight (never touch); FAILED has no complete
#: subgraph to reclaim meaningfully and is left for operator triage;
#: COLLECTED is already done (idempotent no-op, not re-classified).
_COLLECTIBLE_STATUSES = frozenset({TopologySnapshotStatus.READY, TopologySnapshotStatus.SUPERSEDED})


@dataclass(frozen=True)
class RetentionClassification:
    """Result of classifying one repository's snapshots."""

    pinned: list[SnapshotDescriptor]
    collectible: list[SnapshotDescriptor]


def classify_snapshots(
    snapshots: list[SnapshotDescriptor],
    pinned_revisions: set[str],
    *,
    now: datetime,
    retention_window_days: int,
    retention_max_snapshots_per_repository: int,
) -> RetentionClassification:
    """Classify snapshots (already scoped to one tenant/repository) into
    pinned vs. collectible per the policy documented on this module.

    Never mutates or deletes anything — pure decision logic only.
    """
    pinned: list[SnapshotDescriptor] = []
    candidates: list[SnapshotDescriptor] = []

    for snapshot in snapshots:
        if snapshot.status not in _COLLECTIBLE_STATUSES:
            pinned.append(snapshot)
            continue
        if snapshot.revision in pinned_revisions:
            pinned.append(snapshot)
            continue
        candidates.append(snapshot)

    # Most-recently-ingested first, so the LRU bound keeps the newest N.
    candidates.sort(key=lambda s: s.ingested_at, reverse=True)

    window_cutoff = now - timedelta(days=retention_window_days)
    collectible: list[SnapshotDescriptor] = []
    for index, snapshot in enumerate(candidates):
        within_lru_bound = index < retention_max_snapshots_per_repository
        within_window = snapshot.ingested_at >= window_cutoff
        if within_lru_bound or within_window:
            pinned.append(snapshot)
        else:
            collectible.append(snapshot)

    return RetentionClassification(pinned=pinned, collectible=collectible)
