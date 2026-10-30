# tests/unit/test_secret_stores.py
"""Wave 0 tests: SecretStore adapter strategy + layered config (TO_DO-002/004)."""

from __future__ import annotations

import pytest

from investigation_agent_platform.infrastructure.secrets.resolver import (
    SecretResolutionError,
    SecretStoreSecretResolver,
)
from investigation_agent_platform.infrastructure.secrets.stores.factory import (
    build_secret_store_from_env,
    selected_provider,
)
from investigation_agent_platform.ports.secrets.store import SecretStoreError


class TestStoreFactory:
    def test_no_provider_returns_none(self, monkeypatch) -> None:
        monkeypatch.delenv("IAP_SECRET_STORE_PROVIDER", raising=False)
        assert selected_provider() == ""
        assert build_secret_store_from_env() is None

    def test_unknown_provider_fails_closed(self, monkeypatch) -> None:
        monkeypatch.setenv("IAP_SECRET_STORE_PROVIDER", "vault-9")
        with pytest.raises(SecretStoreError):
            build_secret_store_from_env()

    def test_openbao_selected(self, monkeypatch) -> None:
        monkeypatch.setenv("IAP_SECRET_STORE_PROVIDER", "openbao")
        monkeypatch.setenv("IAP_OPENBAO_ADDR", "https://bao.local:8200")
        monkeypatch.setenv("IAP_OPENBAO_TOKEN", "test-token")
        store = build_secret_store_from_env()
        assert store is not None and store.name == "openbao"


class TestOpenBaoStore:
    def _store(self):  # type: ignore[no-untyped-def]
        from pydantic import SecretStr

        from investigation_agent_platform.infrastructure.secrets.stores.openbao import (
            OpenBaoSecretStore,
            OpenBaoSettings,
        )

        return OpenBaoSecretStore(
            settings=OpenBaoSettings(
                address="https://bao.local:8200", token=SecretStr("test-token")
            )
        )

    @pytest.mark.asyncio
    async def test_invalid_path_fails_closed(self) -> None:
        with pytest.raises(SecretStoreError):
            await self._store().get("../escape")

    @pytest.mark.asyncio
    async def test_missing_entry_returns_none(self) -> None:
        import httpx
        from pydantic import SecretStr

        from investigation_agent_platform.infrastructure.secrets.stores.openbao import (
            OpenBaoSecretStore,
            OpenBaoSettings,
        )

        def _handler(request: httpx.Request) -> httpx.Response:
            # Injected mock client carries no auth headers; auth is covered by
            # construction tests, not transport tests.
            return httpx.Response(404, json={})

        store = OpenBaoSecretStore(
            settings=OpenBaoSettings(
                address="https://bao.local:8200", token=SecretStr("test-token")
            )
        )
        store._client = httpx.AsyncClient(
            base_url="https://bao.local:8200", transport=httpx.MockTransport(_handler)
        )
        try:
            assert await store.get("db/prod") is None
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_value_entry_resolves(self) -> None:
        import httpx
        from pydantic import SecretStr

        from investigation_agent_platform.infrastructure.secrets.stores.openbao import (
            OpenBaoSecretStore,
            OpenBaoSettings,
        )

        def _handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json={"data": {"data": {"value": "postgresql+asyncpg://u:p@h/db"}}}
            )

        store = OpenBaoSecretStore(
            settings=OpenBaoSettings(
                address="https://bao.local:8200", token=SecretStr("test-token")
            )
        )
        store._client = httpx.AsyncClient(
            base_url="https://bao.local:8200", transport=httpx.MockTransport(_handler)
        )
        try:
            assert await store.get("db/prod") == "postgresql+asyncpg://u:p@h/db"
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_rejected_lookup_fails_closed(self) -> None:
        import httpx
        from pydantic import SecretStr

        from investigation_agent_platform.infrastructure.secrets.stores.openbao import (
            OpenBaoSecretStore,
            OpenBaoSettings,
        )

        def _handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, json={"errors": ["denied"]})

        store = OpenBaoSecretStore(
            settings=OpenBaoSettings(
                address="https://bao.local:8200", token=SecretStr("test-token")
            )
        )
        store._client = httpx.AsyncClient(
            base_url="https://bao.local:8200", transport=httpx.MockTransport(_handler)
        )
        try:
            with pytest.raises(SecretStoreError):
                await store.get("db/prod")
        finally:
            await store.close()


class TestExtensionAdapters:
    @pytest.mark.asyncio
    async def test_invalid_path_rejected_before_sdk(self) -> None:
        from investigation_agent_platform.infrastructure.secrets.stores.aws import (
            AwsSecretStore,
        )
        from investigation_agent_platform.infrastructure.secrets.stores.azure import (
            AzureSecretStore,
        )
        from investigation_agent_platform.infrastructure.secrets.stores.gcp import (
            GcpSecretStore,
        )

        for store_cls in (AwsSecretStore, AzureSecretStore, GcpSecretStore):
            with pytest.raises(SecretStoreError):
                await store_cls().get("..")

    def test_adapter_names(self) -> None:
        from investigation_agent_platform.infrastructure.secrets.stores.aws import (
            AwsSecretStore,
        )
        from investigation_agent_platform.infrastructure.secrets.stores.azure import (
            AzureSecretStore,
        )
        from investigation_agent_platform.infrastructure.secrets.stores.gcp import (
            GcpSecretStore,
        )

        assert AwsSecretStore().name == "aws"
        assert AzureSecretStore().name == "azure"
        assert GcpSecretStore().name == "gcp"


class TestStoreBackedResolver:
    @pytest.mark.asyncio
    async def test_store_value_wins(self) -> None:
        async def _fake_store(path: str) -> str | None:
            return "postgresql+asyncpg://vault:u@h/db" if path == "db/prod" else None

        resolver = SecretStoreSecretResolver(store_get=_fake_store, environment="test")
        assert await resolver.resolve("secret://db/prod") == ("postgresql+asyncpg://vault:u@h/db")

    @pytest.mark.asyncio
    async def test_production_without_backend_fails_closed(self) -> None:
        resolver = SecretStoreSecretResolver(environment="production")
        with pytest.raises(SecretResolutionError):
            await resolver.resolve("secret://db/prod")


class TestLayeredConfig:
    def test_dotenv_fills_unset_without_override(self, tmp_path, monkeypatch) -> None:
        from investigation_agent_platform.infrastructure.configuration.layers import (
            apply_layered_config,
        )

        (tmp_path / ".env").write_text("IAP_LAYER_TEST_A=from-file\n", encoding="utf-8")
        monkeypatch.delenv("IAP_LAYER_TEST_A", raising=False)
        monkeypatch.setenv("IAP_LAYER_TEST_B", "runtime")
        (tmp_path / ".env.development").write_text("IAP_LAYER_TEST_B=from-file\n", encoding="utf-8")
        monkeypatch.setenv("IAP_ENVIRONMENT", "development")
        report = apply_layered_config(project_root=tmp_path)
        import os

        assert os.environ["IAP_LAYER_TEST_A"] == "from-file"
        assert os.environ["IAP_LAYER_TEST_B"] == "runtime"
        assert ".env" in report.dotenv_files

    def test_yaml_allowlists_unset_nonsecrets(self, tmp_path, monkeypatch) -> None:
        from investigation_agent_platform.infrastructure.configuration.layers import (
            apply_layered_config,
        )

        (tmp_path / "config").mkdir()
        (tmp_path / "config" / "application.yaml").write_text(
            "platform:\n  name: yaml-app\ninvestigation:\n  max_tool_calls: 42\n",
            encoding="utf-8",
        )
        monkeypatch.delenv("IAP_APP_ID", raising=False)
        monkeypatch.delenv("IAP_MAX_TOOL_CALLS", raising=False)
        report = apply_layered_config(project_root=tmp_path)
        import os

        assert os.environ["IAP_APP_ID"] == "yaml-app"
        assert os.environ["IAP_MAX_TOOL_CALLS"] == "42"
        assert "IAP_APP_ID" in report.yaml_keys_applied

    def test_yaml_never_overrides_runtime(self, tmp_path, monkeypatch) -> None:
        from investigation_agent_platform.infrastructure.configuration.layers import (
            apply_layered_config,
        )

        (tmp_path / "config").mkdir()
        (tmp_path / "config" / "application.yaml").write_text(
            "platform:\n  name: yaml-app\n", encoding="utf-8"
        )
        monkeypatch.setenv("IAP_APP_ID", "runtime-app")
        report = apply_layered_config(project_root=tmp_path)
        import os

        assert os.environ["IAP_APP_ID"] == "runtime-app"
        assert "IAP_APP_ID" not in report.yaml_keys_applied
