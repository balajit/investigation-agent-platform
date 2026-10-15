# src/investigation_agent_platform/infrastructure/evidence/datasources/registry.py
"""DatasourceRegistry: profile revision → live state adapter (§7.4).

Resolves ``(tenant, app, datasource[, version])`` to a SELECT-only state
adapter built from the datasource's own declaration: kind picks the adapter
family, ``connection_ref`` resolves through the SecretResolver chain, and
query templates are namespaced ``{datasource_id}.{name}`` with the
datasource's table allowlist. Adapters are cached per profile revision and
TTL-bounded otherwise (secret rotation converges within the TTL).

Only read-only SQL datasources are served (postgres, oracle). Every other
kind raises — providers for those kinds are consumer-deferred. Datasources
with ``read_only: false`` are refused: no write path exists in 1.0.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from investigation_agent_platform.api.errors import sanitize_extra
from investigation_agent_platform.domain.common.exceptions import ExecutionError
from investigation_agent_platform.domain.profile.models import (
    ApplicationProfile,
    Datasource,
    DatasourceKind,
    DatasourceRole,
)
from investigation_agent_platform.infrastructure.secrets.resolver import resolve_connection
from investigation_agent_platform.ports.secrets.resolver import SecretResolver

logger = logging.getLogger(__name__)

_SQL_KINDS = frozenset({DatasourceKind.POSTGRES, DatasourceKind.ORACLE})
_DEFAULT_TTL_SECONDS = 300.0


class DatasourceResolutionError(ExecutionError):
    """Fail-closed datasource resolution failure."""


class UnsupportedDatasourceKindError(DatasourceResolutionError):
    """Datasource kind has no serving adapter in 1.0."""


class DatasourceRegistry:
    """Live adapter factory keyed by profile revision + TTL."""

    def __init__(
        self,
        profile_repo: Any,
        secret_resolver: SecretResolver | None = None,
        mcp_catalog: dict[str, dict[str, Any]] | None = None,
        cache_ttl_seconds: float = _DEFAULT_TTL_SECONDS,
        environment: str | None = None,
        engine_factory: Any | None = None,
    ) -> None:
        self._profile_repo = profile_repo
        self._secret_resolver = secret_resolver
        self._mcp_catalog = dict(mcp_catalog or {})
        self._ttl = max(1.0, cache_ttl_seconds)
        self._environment = environment
        self._engine_factory = engine_factory
        self._cache: dict[tuple[str, str, str, int], tuple[Any, Any, float]] = {}

    # -- resolution ------------------------------------------------------

    async def resolve(
        self,
        tenant_id: str,
        application_id: str,
        datasource_id: str,
        version: str | int | None = None,
    ) -> Any:
        """Resolve one datasource to its live adapter (cached)."""
        profile = await self._profile_repo.get_by_application_id(
            tenant_id, application_id, version
        )
        if profile is None:
            raise DatasourceResolutionError(
                f"No application profile for '{application_id}'."
            )
        datasource = next((d for d in profile.datasources if d.id == datasource_id), None)
        if datasource is None:
            raise DatasourceResolutionError(
                f"Unknown datasource {datasource_id!r} for '{application_id}'."
            )
        return await self._resolve_datasource(tenant_id, profile, datasource)

    async def resolve_default(self, tenant_id: str, application_id: str) -> Any:
        """Resolve the default state datasource (first STATE, then READONLY)."""
        profile = await self._profile_repo.get_by_application_id(tenant_id, application_id)
        if profile is None:
            raise DatasourceResolutionError(
                f"No application profile for '{application_id}'."
            )
        candidates = sorted(
            (d for d in profile.datasources if d.kind in _SQL_KINDS),
            key=lambda d: 0 if d.role == DatasourceRole.STATE else 1,
        )
        if not candidates:
            raise DatasourceResolutionError(
                f"No servable state datasource for '{application_id}'."
            )
        errors: list[Exception] = []
        for datasource in candidates:
            try:
                return await self._resolve_datasource(tenant_id, profile, datasource)
            except DatasourceResolutionError as exc:
                errors.append(exc)
        raise errors[0]

    async def _resolve_datasource(
        self, tenant_id: str, profile: ApplicationProfile, datasource: Datasource
    ) -> Any:
        if datasource.kind not in _SQL_KINDS:
            raise UnsupportedDatasourceKindError(
                f"Datasource kind {datasource.kind.value!r} has no adapter in 1.0."
            )
        if not datasource.read_only:
            raise DatasourceResolutionError(
                f"Datasource {datasource.id!r} is not read-only; refusing to serve."
            )
        key = (tenant_id, profile.id, datasource.id, profile.version)
        cached = self._cache.get(key)
        if cached is not None and cached[2] > time.monotonic():
            return cached[0]
        if cached is not None:
            self._cache.pop(key, None)
            await self._dispose_engine(cached[1])

        try:
            dsn = await resolve_connection(
                datasource.connection_ref,
                environment=self._environment,
                resolver=self._secret_resolver,
            )
        except Exception as exc:
            raise DatasourceResolutionError(
                f"Connection for datasource {datasource.id!r} is not resolvable."
            ) from exc

        templates = {f"{datasource.id}.{name}": tpl.sql for name, tpl in datasource.query_templates.items()}
        allowed_tables = set(datasource.allowed_tables)
        for tpl in datasource.query_templates.values():
            allowed_tables.update(tpl.allowed_tables)
        if templates and "default" not in templates:
            # The workflow reasoner requests template_id "default" when it
            # has no more specific choice: resolve it to this datasource's
            # first template (resolve_default already prefers the STATE
            # datasource, so "default" means the default state view). The
            # alias stays inside profile-declared, validated SQL — the
            # SecurityPolicyViolation boundary for unregistered IDs is
            # unchanged.
            first_key = next(iter(templates))
            templates = {"default": templates[first_key], **templates}

        from investigation_agent_platform.infrastructure.evidence.state.oracle import (
            SqlglotTemplateValidator,
        )

        adapter: Any
        if datasource.kind == DatasourceKind.POSTGRES:
            from investigation_agent_platform.infrastructure.evidence.state.postgres import (
                AsyncPostgresStateAdapter,
            )

            engine = self._make_engine(dsn)
            adapter = AsyncPostgresStateAdapter(
                engine=engine,
                template_repository=templates,
                validator=SqlglotTemplateValidator(allowed_tables, dialect="postgres"),
                provider_id=f"postgres-{datasource.id}",
            )
        else:
            from investigation_agent_platform.infrastructure.evidence.state.oracle import (
                AsyncOracleStateAdapter,
            )

            try:
                import importlib.util as _importlib_util

                if _importlib_util.find_spec("oracledb") is None:
                    raise DatasourceResolutionError(
                        "oracledb package absent; oracle datasource unavailable."
                    )
            except DatasourceResolutionError:
                raise
            except Exception as exc:
                raise DatasourceResolutionError("Oracle backend check failed.") from exc
            engine = self._make_engine(dsn)
            adapter = AsyncOracleStateAdapter(
                engine=engine,
                template_repository=templates,
                validator=SqlglotTemplateValidator(allowed_tables),
                provider_id=f"oracle-{datasource.id}",
            )
        # The gateway passes provider.profile into state calls (same
        # convention as the static providers); the registry owns the
        # revision the adapter was built from, so it stamps it here.
        adapter.profile = profile
        self._cache[key] = (adapter, engine, time.monotonic() + self._ttl)
        logger.info(
            "Resolved datasource",
            extra=sanitize_extra(
                {
                    "tenant_id": tenant_id,
                    "event": "datasource.resolved",
                    "application_id": profile.id,
                    "datasource_id": datasource.id,
                    "profile_version": profile.version,
                }
            ),
        )
        return adapter

    def _make_engine(self, dsn: str) -> Any:
        if self._engine_factory is not None:
            return self._engine_factory(dsn)
        from sqlalchemy.ext.asyncio import create_async_engine

        from investigation_agent_platform.bootstrap.buffered_dev import _async_db_uri

        # Resolved refs (e.g. env://IAP_DATABASE_URI) commonly carry the
        # sync scheme; the asyncio extension needs +asyncpg.
        return create_async_engine(_async_db_uri(dsn))

    @staticmethod
    async def _dispose_engine(engine: Any) -> None:
        dispose = getattr(engine, "dispose", None)
        if callable(dispose):
            try:
                result = dispose()
                if hasattr(result, "__await__"):
                    await result
            except Exception:
                pass

    # -- MCP servers declared per datasource -------------------------------

    def resolve_mcp(self, tenant_id: str, profile: ApplicationProfile, datasource: Datasource) -> Any | None:
        """Build the MCP adapter for a datasource's declared ``mcp_server``.

        The server NAME comes from the profile; the connection/command catalog
        stays operator-owned (static YAML). Unknown names return None so the
        YAML fallback owns them — never guessed.

        Fail-closed None contract (TO_DO-028): there are currently no
        in-repo callers of this method (verified by grep); any future caller
        must treat None as "unregistered" — log with the datasource id and
        either skip the MCP path or raise DatasourceResolutionError where the
        MCP backend is required. Never dereference the result unchecked.
        """
        name = datasource.mcp_server
        if not name:
            return None
        entry = self._mcp_catalog.get(name)
        if entry is None:
            return None
        from investigation_agent_platform.infrastructure.evidence.mcp import (
            McpEvidenceAdapter,
        )

        connection = str(entry.get("connection", ""))
        tenants = [tenant_id, *[t for t in datasource.tenants if t != tenant_id]]
        return McpEvidenceAdapter(
            server_params={"command": entry.get("command"), "args": entry.get("args", [])},
            allowed_tools=list(entry.get("allowed_tools", ["run-sql"])),
            tool_timeout_seconds=float(entry.get("timeout_seconds", 30.0)),
            max_items_per_call=int(entry.get("max_rows", 1000)),
            connection_name=connection,
            tenant_allowlist={t: {connection} for t in tenants},
        )
