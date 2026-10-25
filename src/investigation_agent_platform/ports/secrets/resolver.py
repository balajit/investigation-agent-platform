# src/investigation_agent_platform/ports/secrets/resolver.py
"""Secrets resolver port: error contracts and resolver interface."""

from __future__ import annotations

from typing import Protocol


class SecretResolutionError(Exception):
    """A secret reference could not be resolved (fail closed, no value logged)."""


class UnsupportedConnectionSchemeError(SecretResolutionError):
    """A connection reference uses a scheme the resolver does not serve."""


class SecretResolver(Protocol):
    """Resolves one secret reference to its plaintext value (never logged)."""

    async def resolve(self, ref: str) -> str: ...
