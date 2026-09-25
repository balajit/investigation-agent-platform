# src/investigation_agent_platform/ports/evidence/store.py
"""Tier-2 evidence payload store port protocol."""

from typing import Any, Protocol, runtime_checkable
from uuid import UUID


@runtime_checkable
class EvidenceStorePort(Protocol):
    """Port for offloading heavy sanitized evidence payloads to Tier-2 object storage."""

    async def store_sanitized_payload(
        self,
        tenant_id: str,
        investigation_id: UUID,
        evidence_id: UUID,
        sanitized_payload: bytes,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        ...

    async def fetch_payload(self, tenant_id: str, content_uri: str) -> bytes:
        ...