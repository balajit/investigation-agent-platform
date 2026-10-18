# src/investigation_agent_platform/application/investigation/context.py
"""Trusted investigation context for application services (Part 9).

Tenant and investigation identity always originate from authenticated
infrastructure (API JWT verification, MCP session binding) — never from
agent-controlled tool arguments. Services accept this context object, not raw
IDs, so the trust boundary is visible in every signature. Existence and
tenant-ownership of the investigation are verified per call via
``require_investigation`` (fail closed, no disclosure).
"""

import re
import uuid
from dataclasses import dataclass, field
from uuid import UUID

from investigation_agent_platform.domain.common.exceptions import SecurityPolicyViolationException
from investigation_agent_platform.domain.investigation.models import Investigation
from investigation_agent_platform.ports.persistence.repositories import InvestigationRepository

# Trusted-context identifier shape (mirrors the provider index-scope rule).
# Kept local: the application layer must not import infrastructure modules.
_TENANT_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]*$"
MAX_TENANT_ID_LENGTH = 128


@dataclass(frozen=True)
class InvestigationScope:
    """Authenticated scope for one investigation operation.

    Named to avoid the established domain ``InvestigationContext``
    (operational parameters: environment, time window, ...) — this is the
    *authorization* scope: who may see which investigation's evidence.
    """

    tenant_id: str
    investigation_id: UUID
    correlation_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    actor_id: str | None = None

    @classmethod
    def create(
        cls,
        *,
        tenant_id: str,
        investigation_id: UUID,
        correlation_id: str | None = None,
        actor_id: str | None = None,
    ) -> "InvestigationScope":
        """Build a context from verified identity fields (never agent input)."""
        if (
            not isinstance(tenant_id, str)
            or not 1 <= len(tenant_id) <= MAX_TENANT_ID_LENGTH
            or not re.fullmatch(_TENANT_ID_PATTERN, tenant_id)
        ):
            raise SecurityPolicyViolationException(
                "Invalid tenant scope for investigation context."
            )
        if not isinstance(investigation_id, UUID):
            raise SecurityPolicyViolationException("Invalid investigation scope for context.")
        if correlation_id is not None and (
            not isinstance(correlation_id, str) or not 1 <= len(correlation_id) <= 256
        ):
            raise SecurityPolicyViolationException("Invalid correlation ID for context.")
        if actor_id is not None and (not isinstance(actor_id, str) or len(actor_id) > 256):
            raise SecurityPolicyViolationException("Invalid actor ID for context.")
        return cls(
            tenant_id=tenant_id,
            investigation_id=investigation_id,
            correlation_id=correlation_id or uuid.uuid4().hex,
            actor_id=actor_id,
        )


async def require_investigation(
    investigation_repo: InvestigationRepository,
    tenant_id: str,
    investigation_id: UUID,
) -> Investigation:
    """Load the investigation or fail closed without disclosing scope state.

    Unknown IDs, cross-tenant IDs, and tenant-mismatched rows all surface as
    ``SecurityPolicyViolationException``: record-level misses are
    ``EvidenceNotFoundException`` territory, but a broken *context* is an
    authorization failure, never a lookup miss.
    """
    investigation = await investigation_repo.get_by_id(tenant_id, investigation_id)
    if investigation is None or investigation.tenant_id != tenant_id:
        raise SecurityPolicyViolationException(
            "Investigation is not available in the current tenant scope."
        )
    return investigation
