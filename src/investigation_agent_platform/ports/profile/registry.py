# src/investigation_agent_platform/ports/profile/registry.py
"""Profile registry port protocol for application profiles and versioning."""

from typing import Protocol, runtime_checkable

from investigation_agent_platform.domain.profile.models import ApplicationProfile


@runtime_checkable
class ProfileRegistryPort(Protocol):
    """Port interface for central profile registration, retrieval, and revision control."""

    async def get_profile(
        self, tenant_id: str, application_id: str, version: str | None = None
    ) -> ApplicationProfile:
        ...

    async def publish_profile(self, tenant_id: str, profile: ApplicationProfile) -> str:
        ...

    async def list_revisions(self, tenant_id: str, application_id: str) -> list[str]:
        ...

    async def validate_profile(self, profile: ApplicationProfile) -> bool:
        ...