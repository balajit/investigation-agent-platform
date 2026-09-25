# src/investigation_agent_platform/domain/common/utils.py
"""Domain utilities, clock abstractions, and identifier generators."""

import hashlib
import json
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID, uuid4


class Clock(Protocol):
    """Abstract clock protocol for deterministic time retrieval."""

    def utcnow(self) -> datetime:
        ...


class SystemClock:
    """System implementation returning current UTC time."""

    def utcnow(self) -> datetime:
        return datetime.now(UTC)


def generate_uuid() -> UUID:
    """Generate a cryptographically secure random UUIDv4."""
    return uuid4()


class CanonicalJSONEncoder(json.JSONEncoder):
    """Deterministic JSON encoder handling UUIDs, datetimes, and custom primitives."""

    def default(self, o: Any) -> Any:
        if isinstance(o, UUID):
            return str(o)
        if isinstance(o, datetime):
            if o.tzinfo is None:
                o = o.replace(tzinfo=UTC)
            return o.isoformat()
        if hasattr(o, "to_dict"):
            return o.to_dict()
        return super().default(o)


def generate_execution_hash(
    tenant_id: str,
    investigation_id: str | UUID,
    action_type: str,
    parameters: dict[str, Any],
    capability: str = "default",
    provider: str = "default",
    revision: str = "v1",
) -> str:
    """Generate deterministic cryptographic fingerprint including full scoping bounds."""
    canonical_payload = {
        "tenant_id": tenant_id,
        "investigation_id": str(investigation_id),
        "action_type": action_type,
        "capability": capability,
        "provider": provider,
        "parameters": parameters,
        "revision": revision,
    }
    canonical_json = json.dumps(
        canonical_payload,
        cls=CanonicalJSONEncoder,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()