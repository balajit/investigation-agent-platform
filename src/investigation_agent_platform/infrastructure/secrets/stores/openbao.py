# src/investigation_agent_platform/infrastructure/secrets/stores/openbao.py
"""OpenBao KV secret store (fail closed; values never logged)."""

from __future__ import annotations

import httpx
from pydantic import BaseModel, SecretStr

from investigation_agent_platform.infrastructure.secrets.resolver import (
    SecretResolutionError,
    validate_secret_path,
)
from investigation_agent_platform.ports.secrets.store import SecretStoreError


class OpenBaoSettings(BaseModel):
    """Connection settings (token stays a SecretStr; never logged)."""

    address: str
    token: SecretStr


class OpenBaoSecretStore:
    """Reads KV v2 secrets from OpenBao (``<addr>/v1/secret/data/<path>``)."""

    name = "openbao"

    def __init__(self, settings: OpenBaoSettings) -> None:
        address = settings.address.strip().rstrip("/")
        if not address.startswith(("https://", "http://")):
            raise SecretStoreError("Invalid OpenBao address.")
        self._settings = settings
        self._address = address
        self._client: httpx.AsyncClient | None = None

    async def _client_or_create(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._address,
                headers={"X-Vault-Token": self._settings.token.get_secret_value()},
                timeout=10.0,
            )
        return self._client

    async def get(self, path: str) -> str | None:
        try:
            validate_secret_path(path)
        except SecretResolutionError as exc:
            raise SecretStoreError(f"Invalid secret path {path!r}.") from exc
        client = await self._client_or_create()
        try:
            response = await client.get(f"/v1/secret/data/{path}")
        except Exception as exc:
            raise SecretStoreError(f"OpenBao lookup failed for {path!r}.") from exc
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise SecretStoreError(f"OpenBao lookup failed for {path!r}.")
        try:
            value = response.json()["data"]["data"]["value"]
        except Exception as exc:
            raise SecretStoreError(f"OpenBao lookup failed for {path!r}.") from exc
        if not isinstance(value, str) or not value:
            return None
        return value

    async def close(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()
