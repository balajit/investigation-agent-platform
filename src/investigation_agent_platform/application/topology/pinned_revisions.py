# src/investigation_agent_platform/application/topology/pinned_revisions.py
"""``PinnedRevisionsPort`` backed by each open investigation's own evidence.

Never guesses which revisions are still needed: an investigation pins a
revision only because it already retrieved and stored a
``TOPOLOGY_ATTRIBUTION`` ``Evidence`` record referencing it
(``application/topology/attribution.py:_to_evidence``). A missing evidence
record simply does not pin — there is no separate "maybe still needed" state
to get wrong.
"""

from __future__ import annotations

import logging

from investigation_agent_platform.ports.persistence.repositories import (
    EvidenceRepository,
    InvestigationRepository,
)

logger = logging.getLogger(__name__)

#: Must match `application/topology/attribution.py:_to_evidence`'s
#: `provider="TOPOLOGY_ATTRIBUTION"` and `source=f"topology://{repo}/{rev}"`.
_TOPOLOGY_ATTRIBUTION_PROVIDER = "TOPOLOGY_ATTRIBUTION"
_TOPOLOGY_SOURCE_PREFIX = "topology://"


class EvidenceBackedPinnedRevisionsProvider:
    """``PinnedRevisionsPort`` implementation: scans open investigations'
    topology-attribution evidence for the ``(repository_id, revision)``
    pairs they reference."""

    def __init__(
        self,
        investigation_repo: InvestigationRepository,
        evidence_repo: EvidenceRepository,
    ) -> None:
        self._investigation_repo = investigation_repo
        self._evidence_repo = evidence_repo

    async def list_pinned_revisions(self, tenant_id: str, repository_id: str) -> set[str]:
        pinned: set[str] = set()
        open_ids = await self._investigation_repo.list_open_ids(tenant_id)
        for investigation_id in open_ids:
            evidence_items = await self._evidence_repo.find_by_investigation_id(
                tenant_id, investigation_id
            )
            for evidence in evidence_items:
                if evidence.provider != _TOPOLOGY_ATTRIBUTION_PROVIDER:
                    continue
                if not evidence.source.startswith(_TOPOLOGY_SOURCE_PREFIX):
                    continue
                locator = evidence.source[len(_TOPOLOGY_SOURCE_PREFIX) :]
                if "/" not in locator:
                    continue
                evidence_repository_id, _, revision = locator.rpartition("/")
                if evidence_repository_id != repository_id or not revision:
                    continue
                pinned.add(revision)
        return pinned
