# src/investigation_agent_platform/infrastructure/secrets/resolver.py
"""Secret resolvers: env, store-backed, and connection-ref dispatch.

Fail-closed throughout: unknown refs, missing values, and wrong schemes
raise (never return a placeholder); error messages name the reference, never
the value.
"""

from __future__ import annotations

import os
import re
from collections.abc import Awaitable, Callable

from investigation_agent_platform.ports.secrets.resolver import (
    SecretResolutionError,
    SecretResolver,
    UnsupportedConnectionSchemeError,
)

_ENV_VAR_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_PATH_SEGMENT_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.\-]*")
_MAX_PATH_LENGTH = 256

# Plain-DSN schemes servable in non-production only. Production must use a
# secret/env reference so credentials never live in profile payloads.
_DEV_PLAIN_SCHEMES = frozenset({"postgresql", "postgres", "postgresql+asyncpg"})


def _validate_env_name(name: str) -> None:
    if not name or _ENV_VAR_RE.fullmatch(name) is None:
        raise SecretResolutionError(f"Invalid env reference {name!r}.")


def validate_secret_path(path: str) -> None:
    """Reject empty, escaping, or non-canonical secret paths (fail closed)."""
    if not path or len(path) > _MAX_PATH_LENGTH:
        raise SecretResolutionError("Invalid secret path.")
    if path.startswith("/") or path.endswith("/") or "//" in path or "\\" in path:
        raise SecretResolutionError("Invalid secret path.")
    for segment in path.split("/"):
        if segment in ("", ".", ".."):
            raise SecretResolutionError("Invalid secret path.")
        if _PATH_SEGMENT_RE.fullmatch(segment) is None:
            raise SecretResolutionError("Invalid secret path.")


def secret_path_to_env_name(path: str) -> str:
    """Map a secret path to its non-production env fallback name."""
    return SecretStoreSecretResolver.fallback_env_name(path)


class EnvSecretResolver:
    """Resolves ``env://NAME`` from process environment (no other scheme)."""

    async def resolve(self, ref: str) -> str:
        if not ref.startswith("env://"):
            raise UnsupportedConnectionSchemeError(f"EnvSecretResolver does not serve {ref!r}.")
        name = ref[len("env://") :]
        _validate_env_name(name)
        try:
            return os.environ[name]
        except KeyError as exc:
            raise SecretResolutionError(f"Unset env reference {name!r}.") from exc


class SecretStoreSecretResolver:
    """Resolves ``secret://path`` via a store backend with env fallback.

    Non-production without a backend (or backend miss) falls back to the
    ``IAP_SECRET_*`` env name. Production never falls back: without a
    backend value it fails closed.
    """

    def __init__(
        self,
        store_get: Callable[[str], Awaitable[str | None]] | None = None,
        environment: str | None = None,
    ) -> None:
        self._store_get = store_get
        self._environment = (environment or "production").strip().lower()

    @staticmethod
    def fallback_env_name(path: str) -> str:
        """Map a secret path to its non-production ``IAP_SECRET_*`` fallback.

        Mirrors the ``load-oracle-env.sh`` dotted-name mapping: non-alphanumeric
        runs become single underscores, uppercased, under the ``IAP_SECRET_``
        prefix (e.g. ``oracle/sample/orders/prod/jdbc_url`` maps to
        ``IAP_SECRET_ORACLE_SAMPLE_ORDERS_PROD_JDBC_URL``).
        """
        return "IAP_SECRET_" + re.sub(r"[^A-Za-z0-9]+", "_", path).strip("_").upper()

    @property
    def _is_production(self) -> bool:
        return self._environment == "production"

    async def resolve(self, ref: str) -> str:
        if not ref.startswith("secret://"):
            raise UnsupportedConnectionSchemeError(
                "SecretStoreSecretResolver serves secret:// references only."
            )
        path = ref[len("secret://") :]
        validate_secret_path(path)
        if self._store_get is not None:
            value = await self._store_get(path)
            if value is not None:
                return value
            if self._is_production:
                raise SecretResolutionError(f"Unresolvable secret {path!r}.")
        elif self._is_production:
            raise SecretResolutionError(f"Unresolvable secret {path!r}.")
        if self._is_production:
            raise SecretResolutionError(f"Unresolvable secret {path!r}.")
        try:
            return os.environ[secret_path_to_env_name(path)]
        except KeyError as exc:
            raise SecretResolutionError(f"Unresolvable secret {path!r}.") from exc


class ChainedSecretResolver:
    """Tries resolvers in order; first success wins (fail closed on all-fail).

    With no explicit resolvers, defaults to the standard chain: environment
    first, then the secret store (with its non-production env fallback).
    """

    def __init__(
        self,
        environment: str | None = None,
        resolvers: list[SecretResolver] | None = None,
    ) -> None:
        env = (environment or "production").strip().lower()
        if resolvers is None:
            resolvers = [
                EnvSecretResolver(),
                SecretStoreSecretResolver(environment=env),
            ]
        self._environment = env
        self._resolvers = list(resolvers)

    async def resolve(self, ref: str) -> str:
        errors: list[Exception] = []
        for resolver in self._resolvers:
            try:
                return await resolver.resolve(ref)
            except UnsupportedConnectionSchemeError as exc:
                errors.append(exc)
            except SecretResolutionError as exc:
                errors.append(exc)
        raise SecretResolutionError(
            f"Unresolvable secret reference {ref!r} ({len(errors)} resolver(s) declined)."
        )


async def resolve_connection(
    ref: str | None,
    environment: str | None = None,
    resolver: SecretResolver | None = None,
) -> str:
    """Resolve a datasource ``connection_ref`` to a plaintext DSN.

    ``env://`` and ``secret://`` delegate to their resolvers (an explicitly
    passed resolver wins for ``secret://``). ``TBD``/empty refs fail closed.
    Plain DSNs are dev-only: production rejects them so credentials never
    ride in profile payloads.
    """
    env = (environment or "production").strip().lower()
    if not ref or ref.strip() in ("TBD", "TODO", "TBD-TBD"):
        raise SecretResolutionError("Connection reference is not configured.")
    ref = ref.strip()
    if ref.startswith("env://"):
        return await EnvSecretResolver().resolve(ref)
    if ref.startswith("secret://"):
        active = resolver
        if active is None:
            active = SecretStoreSecretResolver(environment=env)
        return await active.resolve(ref)
    scheme = ref.split("://", 1)[0].lower() if "://" in ref else ""
    if env == "production":
        raise UnsupportedConnectionSchemeError(
            "Plain connection strings are dev-only; use secret:// or env://."
        )
    if scheme in _DEV_PLAIN_SCHEMES:
        return ref
    raise UnsupportedConnectionSchemeError(f"Unsupported connection scheme in {ref!r}.")


__all__: list[str] = [
    "ChainedSecretResolver",
    "EnvSecretResolver",
    "SecretResolutionError",
    "SecretStoreSecretResolver",
    "resolve_connection",
    "secret_path_to_env_name",
    "validate_secret_path",
]
