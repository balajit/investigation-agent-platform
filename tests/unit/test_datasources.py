# tests/unit/test_datasources.py
"""Phase 4 tests: SecretResolver, DatasourceRegistry, Postgres, selector fallback."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from investigation_agent_platform.domain.profile.models import (
    Datasource,
    DatasourceKind,
    DatasourceRole,
    QueryTemplate,
)
from investigation_agent_platform.ports.secrets.resolver import (
    SecretResolutionError,
    UnsupportedConnectionSchemeError,
)


def _ds(**overrides):
    kwargs = {
        "id": "primary",
        "name": "db (postgres)",
        "kind": DatasourceKind.POSTGRES,
        "role": DatasourceRole.STATE,
        "connection_ref": "env://IAP_DS_TENANT_A_PRIMARY",
        "allowed_tables": ["orders"],
        "query_templates": {
            "find_transaction": QueryTemplate(
                description=None,
                sql="SELECT * FROM orders WHERE transaction_id = :id",
                read_only=True,
                allowed_tables=["orders"],
            )
        },
    }
    kwargs.update(overrides)
    return Datasource(**kwargs)


def _profile(datasources):
    from investigation_agent_platform.api.dependencies import _DEFAULT_PROFILE

    return _DEFAULT_PROFILE.model_copy(update={"datasources": datasources})


class _ProfileRepo:
    def __init__(self, profile):
        self._profile = profile

    async def get_by_application_id(self, tenant_id, application_id, version=None):
        return self._profile


class TestEnvSecretResolver:
    @pytest.mark.asyncio
    async def test_resolves_env_var(self, monkeypatch) -> None:
        from investigation_agent_platform.infrastructure.secrets.resolver import (
            EnvSecretResolver,
        )

        monkeypatch.setenv("IAP_DS_X", "postgresql+asyncpg://u:p@h/db")
        assert await EnvSecretResolver().resolve("env://IAP_DS_X") == (
            "postgresql+asyncpg://u:p@h/db"
        )

    @pytest.mark.asyncio
    async def test_missing_var_fails_closed(self) -> None:
        from investigation_agent_platform.infrastructure.secrets.resolver import (
            EnvSecretResolver,
        )

        with pytest.raises(SecretResolutionError):
            await EnvSecretResolver().resolve("env://IAP_DS_DEFINITELY_MISSING")

    @pytest.mark.asyncio
    async def test_bad_var_name_rejected(self) -> None:
        from investigation_agent_platform.infrastructure.secrets.resolver import (
            EnvSecretResolver,
        )

        with pytest.raises(SecretResolutionError):
            await EnvSecretResolver().resolve("env://has-dash")

    @pytest.mark.asyncio
    async def test_wrong_scheme_unsupported(self) -> None:
        from investigation_agent_platform.infrastructure.secrets.resolver import (
            EnvSecretResolver,
        )

        with pytest.raises(UnsupportedConnectionSchemeError):
            await EnvSecretResolver().resolve("secret://x")


class TestSecretStoreResolver:
    @pytest.mark.asyncio
    async def test_store_backend_wins(self) -> None:
        from investigation_agent_platform.infrastructure.secrets.resolver import (
            SecretStoreSecretResolver,
        )

        async def _store(path):
            return "vault-value" if path == "db/main" else None

        r = SecretStoreSecretResolver(store_get=_store, environment="production")
        assert await r.resolve("secret://db/main") == "vault-value"

    @pytest.mark.asyncio
    async def test_dev_fallback_env(self, monkeypatch) -> None:
        from investigation_agent_platform.infrastructure.secrets.resolver import (
            SecretStoreSecretResolver,
        )

        monkeypatch.setenv("IAP_SECRET_DB_MAIN", "dev-value")
        r = SecretStoreSecretResolver(environment="test")
        assert await r.resolve("secret://db/main") == "dev-value"

    @pytest.mark.asyncio
    async def test_prod_without_backend_fails_closed(self) -> None:
        from investigation_agent_platform.infrastructure.secrets.resolver import (
            SecretStoreSecretResolver,
        )

        r = SecretStoreSecretResolver(environment="production")
        with pytest.raises(SecretResolutionError):
            await r.resolve("secret://db/main")


class TestResolveConnection:
    @pytest.mark.asyncio
    async def test_tbd_rejected(self) -> None:
        from investigation_agent_platform.infrastructure.secrets.resolver import (
            resolve_connection,
        )

        with pytest.raises(SecretResolutionError):
            await resolve_connection("TBD", environment="test")

    @pytest.mark.asyncio
    async def test_plain_dev_allowed_prod_rejected(self) -> None:
        from investigation_agent_platform.infrastructure.secrets.resolver import (
            resolve_connection,
        )

        assert (
            await resolve_connection("postgresql://localhost/db", environment="test")
            == "postgresql://localhost/db"
        )
        with pytest.raises(UnsupportedConnectionSchemeError):
            await resolve_connection("postgresql://localhost/db", environment="production")


class TestDatasourceRegistry:
    def _registry(self, profile, monkeypatch=None, **overrides):
        from investigation_agent_platform.infrastructure.evidence.datasources.registry import (
            DatasourceRegistry,
        )

        engines = []

        def _factory(dsn):
            engines.append(dsn)
            return SimpleNamespace(dsn=dsn)

        kwargs = {
            "profile_repo": _ProfileRepo(profile),
            "environment": "test",
            "engine_factory": _factory,
        }
        kwargs.update(overrides)
        return DatasourceRegistry(**kwargs), engines

    @pytest.mark.asyncio
    async def test_resolves_postgres_with_namespaced_templates(self, monkeypatch) -> None:
        from investigation_agent_platform.infrastructure.evidence.state.postgres import (
            AsyncPostgresStateAdapter,
        )

        monkeypatch.setenv("IAP_DS_TENANT_A_PRIMARY", "postgresql+asyncpg://u:p@h/db")
        registry, engines = self._registry(_profile([_ds()]))
        adapter = await registry.resolve("tenant-a", "example-app", "primary")
        assert isinstance(adapter, AsyncPostgresStateAdapter)
        assert engines == ["postgresql+asyncpg://u:p@h/db"]
        assert "primary.find_transaction" in adapter._templates

    @pytest.mark.asyncio
    async def test_cached_per_revision(self, monkeypatch) -> None:
        monkeypatch.setenv("IAP_DS_TENANT_A_PRIMARY", "postgresql+asyncpg://u:p@h/db")
        registry, engines = self._registry(_profile([_ds()]))
        first = await registry.resolve("tenant-a", "example-app", "primary")
        second = await registry.resolve("tenant-a", "example-app", "primary")
        assert first is second
        assert len(engines) == 1

    @pytest.mark.asyncio
    async def test_unknown_datasource_rejected(self) -> None:
        registry, _ = self._registry(_profile([_ds()]))
        from investigation_agent_platform.infrastructure.evidence.datasources.registry import (
            DatasourceResolutionError,
        )

        with pytest.raises(DatasourceResolutionError):
            await registry.resolve("tenant-a", "example-app", "nope")

    @pytest.mark.asyncio
    async def test_tbd_connection_rejected(self) -> None:
        registry, _ = self._registry(_profile([_ds(connection_ref="TBD")]))
        from investigation_agent_platform.infrastructure.evidence.datasources.registry import (
            DatasourceResolutionError,
        )

        with pytest.raises(DatasourceResolutionError):
            await registry.resolve("tenant-a", "example-app", "primary")

    @pytest.mark.asyncio
    async def test_non_readonly_refused(self) -> None:
        registry, _ = self._registry(_profile([_ds(read_only=False)]))
        from investigation_agent_platform.infrastructure.evidence.datasources.registry import (
            DatasourceResolutionError,
        )

        with pytest.raises(DatasourceResolutionError):
            await registry.resolve("tenant-a", "example-app", "primary")

    @pytest.mark.asyncio
    async def test_unsupported_kind_rejected(self) -> None:
        registry, _ = self._registry(_profile([_ds(kind=DatasourceKind.REDIS, role=DatasourceRole.CACHE)]))
        from investigation_agent_platform.infrastructure.evidence.datasources.registry import (
            UnsupportedDatasourceKindError,
        )

        with pytest.raises(UnsupportedDatasourceKindError):
            await registry.resolve("tenant-a", "example-app", "primary")

    @pytest.mark.asyncio
    async def test_resolve_default_prefers_state(self, monkeypatch) -> None:
        from investigation_agent_platform.infrastructure.evidence.state.postgres import (
            AsyncPostgresStateAdapter,
        )

        monkeypatch.setenv("IAP_DS_TENANT_A_PRIMARY", "postgresql+asyncpg://u:p@h/db")
        monkeypatch.setenv("IAP_DS_TENANT_A_REPLICA", "postgresql+asyncpg://u:p@h/db2")
        profile = _profile(
            [
                _ds(
                    id="replica",
                    connection_ref="env://IAP_DS_TENANT_A_REPLICA",
                    role=DatasourceRole.READONLY,
                ),
                _ds(),
            ]
        )
        registry, _ = self._registry(profile)
        adapter = await registry.resolve_default("tenant-a", "example-app")
        assert isinstance(adapter, AsyncPostgresStateAdapter)
        assert adapter._provider_id == "postgres-primary"

    @pytest.mark.asyncio
    async def test_mcp_declared_name_resolves_from_catalog(self) -> None:
        from investigation_agent_platform.infrastructure.evidence.datasources.registry import (
            DatasourceRegistry,
        )
        from investigation_agent_platform.infrastructure.evidence.mcp import McpEvidenceAdapter

        registry = DatasourceRegistry(
            profile_repo=_ProfileRepo(_profile([])),
            mcp_catalog={
                "oracle-main": {
                    "command": "sqlcl",
                    "args": ["-c"],
                    "connection": "MAIN-conn",
                    "allowed_tools": ["run-sql"],
                    "timeout_seconds": 30.0,
                    "max_rows": 100,
                }
            },
        )
        profile = _profile([_ds(mcp_server="oracle-main", tenants=["tenant-b"])])
        adapter = registry.resolve_mcp("tenant-a", profile, profile.datasources[0])
        assert isinstance(adapter, McpEvidenceAdapter)
        assert adapter._tenant_allowlist == {"tenant-a": {"MAIN-conn"}, "tenant-b": {"MAIN-conn"}}

    def test_mcp_unknown_name_returns_none(self) -> None:
        from investigation_agent_platform.infrastructure.evidence.datasources.registry import (
            DatasourceRegistry,
        )

        registry = DatasourceRegistry(profile_repo=_ProfileRepo(_profile([])))
        profile = _profile([_ds(mcp_server="ghost")])
        assert registry.resolve_mcp("tenant-a", profile, profile.datasources[0]) is None
        profile2 = _profile([_ds(mcp_server=None)])
        assert registry.resolve_mcp("tenant-a", profile2, profile2.datasources[0]) is None


class TestSelectorDatasourceFallback:
    @pytest.mark.asyncio
    async def test_registry_serves_unregistered_context(self) -> None:
        from unittest.mock import MagicMock

        from investigation_agent_platform.application.evidence.selector import (
            EvidenceProviderSelector,
        )

        sentinel = MagicMock()
        selector = EvidenceProviderSelector()

        class _Registry:
            async def resolve_default(self, tenant_id, application_id):
                assert (tenant_id, application_id) == ("tenant-a", "app-1")
                return sentinel

        selector.attach_datasource_registry(_Registry())
        assert await selector.get_state_provider("tenant-a", "app-1", "production") is sentinel

    @pytest.mark.asyncio
    async def test_registry_failure_fails_closed(self) -> None:
        from investigation_agent_platform.application.evidence.selector import (
            EvidenceProviderSelector,
        )
        from investigation_agent_platform.domain.common.exceptions import ExecutionError

        selector = EvidenceProviderSelector()

        class _Registry:
            async def resolve_default(self, tenant_id, application_id):
                raise RuntimeError("boom")

        selector.attach_datasource_registry(_Registry())
        with pytest.raises(ExecutionError):
            await selector.get_state_provider("tenant-a", "app-1", "production")

    @pytest.mark.asyncio
    async def test_static_registration_keeps_priority(self) -> None:
        from investigation_agent_platform.application.evidence.selector import (
            EvidenceProviderSelector,
        )
        from investigation_agent_platform.infrastructure.evidence.state.oracle import (
            SqlglotTemplateValidator,
        )
        from investigation_agent_platform.infrastructure.evidence.state.postgres import (
            AsyncPostgresStateAdapter,
        )

        static = AsyncPostgresStateAdapter(
            engine=SimpleNamespace(),
            template_repository={},
            validator=SqlglotTemplateValidator(set(), dialect="postgres"),
        )
        selector = EvidenceProviderSelector()
        selector.register_state_provider("tenant-a:app-1:production", static)

        class _Registry:
            async def resolve_default(self, tenant_id, application_id):
                raise AssertionError("must not be consulted")

        selector.attach_datasource_registry(_Registry())
        assert await selector.get_state_provider("tenant-a", "app-1", "production") is static


class TestPostgresAdapter:
    def test_validator_postgres_dialect(self) -> None:
        from investigation_agent_platform.infrastructure.evidence.state.oracle import (
            SqlglotTemplateValidator,
        )

        v = SqlglotTemplateValidator({"orders"}, dialect="postgres")
        out = v.validate_and_bound_template("SELECT * FROM orders WHERE id = :id", 10)
        assert "SELECT" in out.upper()

    def test_validator_default_stays_oracle(self) -> None:
        from investigation_agent_platform.infrastructure.evidence.state.oracle import (
            SqlglotTemplateValidator,
        )

        v = SqlglotTemplateValidator({"orders"})
        out = v.validate_and_bound_template("SELECT * FROM orders WHERE id = :id", 10)
        assert "SELECT" in out.upper()

    @pytest.mark.asyncio
    async def test_unknown_template_rejected_before_io(self) -> None:
        from investigation_agent_platform.domain.common.exceptions import (
            SecurityPolicyViolationException,
        )
        from investigation_agent_platform.domain.evidence.requests import ApplicationStateRequest
        from investigation_agent_platform.infrastructure.evidence.state.oracle import (
            SqlglotTemplateValidator,
        )
        from investigation_agent_platform.infrastructure.evidence.state.postgres import (
            AsyncPostgresStateAdapter,
        )

        adapter = AsyncPostgresStateAdapter(
            engine=SimpleNamespace(),
            template_repository={},
            validator=SqlglotTemplateValidator(set(), dialect="postgres"),
        )
        request = ApplicationStateRequest(
            environment="production", template_id="nope", parameters={}, limit=5
        )
        with pytest.raises(SecurityPolicyViolationException):
            await adapter.get_application_state(
                "tenant-a", "00000000-0000-0000-0000-000000000000", request, _profile([])
            )


class TestTemplateIdFormat:
    def _policy(self):  # type: ignore[no-untyped-def]
        from investigation_agent_platform.infrastructure.evidence.security import (
            QuerySafetyPolicy,
        )

        return QuerySafetyPolicy()

    @pytest.mark.asyncio
    async def test_namespaced_ids_accepted(self) -> None:
        policy = self._policy()
        # The registry namespaces templates "{datasource_id}.{name}" and the
        # reasoner prompt teaches exactly that form — rejecting dots broke
        # every QUERY_STATE with a documented ID (fail-closed the wrong way).
        for template_id in ("primary.evidence_recent", "default", "a_b.c_d"):
            await policy.validate_state_request("t", template_id, {})

    @pytest.mark.asyncio
    async def test_unsafe_ids_rejected(self) -> None:
        from investigation_agent_platform.domain.common.exceptions import (
            SecurityPolicyViolationException,
        )

        policy = self._policy()
        for template_id in (
            "",
            ".leading",
            "trailing.",
            "double..dot",
            "semi;colon",
            "dash-ed",
            "space here",
            "../traversal",
            "x" * 257,
        ):
            with pytest.raises(SecurityPolicyViolationException):
                await policy.validate_state_request("t", template_id, {})
