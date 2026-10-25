# src/investigation_agent_platform/ports/secrets/store.py
"""Secret store port: backend error contract and store interface."""

from __future__ import annotations

from typing import Protocol


class SecretStoreError(Exception):
    """Fail-closed secret store failure (misconfiguration, denial, transport)."""


class SecretStore(Protocol):
    """Minimal backend contract consumed by the secret-store resolver."""

    @property
    def name(self) -> str: ...  # pragma: no cover - interface only

    async def get(self, path: str) -> str | None: ...  # pragma: no cover
