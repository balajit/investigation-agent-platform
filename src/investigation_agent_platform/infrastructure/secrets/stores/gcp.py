# src/investigation_agent_platform/infrastructure/secrets/stores/gcp.py
"""GCP Secret Manager adapter (extension seam; unconfigured fail-closed)."""

from __future__ import annotations

from investigation_agent_platform.infrastructure.secrets.resolver import (
    SecretResolutionError,
    validate_secret_path,
)
from investigation_agent_platform.ports.secrets.store import SecretStoreError


class GcpSecretStore:
    """Placeholder adapter: rejects every lookup until SDK wiring lands."""

    name = "gcp"

    async def get(self, path: str) -> str | None:
        try:
            validate_secret_path(path)
        except SecretResolutionError as exc:
            raise SecretStoreError(f"Invalid secret path {path!r}.") from exc
        raise SecretStoreError("GCP secret backend is not configured.")

    async def close(self) -> None:
        return None
