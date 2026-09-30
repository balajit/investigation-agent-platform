# src/investigation_agent_platform/ports/security/quotas.py
"""Quota enforcement port (Part 11.3D)."""

from typing import Protocol, runtime_checkable

from investigation_agent_platform.domain.common.extension import CapabilityScope
from investigation_agent_platform.domain.common.quotas import QuotaCheckResult, QuotaPolicy


@runtime_checkable
class QuotaEnforcerPort(Protocol):
    """Atomic quota checks before workflow dispatch (Part 11.3D)."""

    async def check(
        self, scope: CapabilityScope, operation: str, policy: QuotaPolicy
    ) -> QuotaCheckResult:
        """Atomically check-and-reserve quota for one operation dispatch."""
        ...

    async def release(self, scope: CapabilityScope, operation: str) -> None:
        """Release a previously reserved quota unit (idempotent)."""
        ...
