# src/investigation_agent_platform/ports/evidence/mapping.py
"""Evidence-identity mapping lookup port (Part 9).

Platform evidence IDs are minted on qualification and never equal provider
record IDs. Direct retrieval resolves the platform ID through this mapping
first; the mapping row also carries the investigation scope for the ownership
check. ``SqlAlchemyEvidenceRepository.get_by_id`` satisfies this protocol
structurally.
"""

from typing import Protocol, runtime_checkable
from uuid import UUID

from investigation_agent_platform.domain.evidence.models import Evidence


@runtime_checkable
class EvidenceMappingLookup(Protocol):
    """Tenant-scoped lookup of persisted evidence by platform evidence ID."""

    async def get_evidence(self, tenant_id: str, evidence_id: UUID) -> Evidence | None: ...
