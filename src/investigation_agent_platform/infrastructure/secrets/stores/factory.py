# src/investigation_agent_platform/infrastructure/secrets/stores/factory.py
"""Secret store selection from environment (fail closed on unknown provider)."""

from __future__ import annotations

import os
from typing import Any

from pydantic import SecretStr

from investigation_agent_platform.ports.secrets.store import SecretStoreError


def selected_provider() -> str:
    """Return the configured provider id, or ``""`` when none is selected."""
    return os.environ.get("IAP_SECRET_STORE_PROVIDER", "").strip().lower()


def build_secret_store_from_env() -> Any | None:
    """Build the configured store, ``None`` when no provider is selected.

    Unknown provider ids and incomplete provider configuration raise
    ``SecretStoreError`` — never fall back to another backend silently.
    """
    provider = selected_provider()
    if not provider:
        return None
    if provider == "openbao":
        from investigation_agent_platform.infrastructure.secrets.stores.openbao import (
            OpenBaoSecretStore,
            OpenBaoSettings,
        )

        address = os.environ.get("IAP_OPENBAO_ADDR", "").strip()
        token = os.environ.get("IAP_OPENBAO_TOKEN", "")
        if not address or not token:
            raise SecretStoreError("OpenBao selected but IAP_OPENBAO_ADDR/TOKEN unset.")
        return OpenBaoSecretStore(settings=OpenBaoSettings(address=address, token=SecretStr(token)))
    if provider == "aws":
        from investigation_agent_platform.infrastructure.secrets.stores.aws import (
            AwsSecretStore,
        )

        return AwsSecretStore()
    if provider == "azure":
        from investigation_agent_platform.infrastructure.secrets.stores.azure import (
            AzureSecretStore,
        )

        return AzureSecretStore()
    if provider == "gcp":
        from investigation_agent_platform.infrastructure.secrets.stores.gcp import (
            GcpSecretStore,
        )

        return GcpSecretStore()
    raise SecretStoreError(f"Unknown secret store provider {provider!r}.")
