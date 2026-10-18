# src/investigation_agent_platform/ports/extension/lifecycle.py
"""Plugin lifecycle port (Part 11.1)."""

from typing import Protocol, runtime_checkable

from investigation_agent_platform.domain.common.extension import PluginHealth


@runtime_checkable
class PluginLifecycle(Protocol):
    """Initialize/health/close contract for every stateful plugin instance."""

    async def initialize(self) -> None: ...

    async def health(self) -> PluginHealth: ...

    async def close(self) -> None: ...
