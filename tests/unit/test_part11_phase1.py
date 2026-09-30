# tests/unit/test_part11_phase1.py
"""Part 11 Phase 11.1 tests: extension contracts, scope, and compatibility.

Covers: registry rejection paths, contract-version compatibility, plugin
configuration policy (schema + secret handling), scope/cache-key isolation,
schema upcasting, capability-discovery secrecy, and old-fixture parsing.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from investigation_agent_platform.api.app import create_app
from investigation_agent_platform.api.dependencies import AppContext, set_app_context
from investigation_agent_platform.application.extensions.capabilities import list_capabilities
from investigation_agent_platform.application.extensions.registries import (
    CredentialProviderRegistry,
    PluginConfigError,
    PluginRegistrationError,
    ReferenceFetcherRegistry,
    adapt_llm_factory_manifests,
    build_default_registries,
    redact_config,
    resolve_secret,
    scope_cache_key,
    validate_plugin_config,
)
from investigation_agent_platform.domain.common.extension import (
    CapabilityScope,
    CompatibilityRange,
    PluginConfig,
    PluginKind,
    PluginManifest,
    SecretReference,
)
from investigation_agent_platform.ports.extension.versioning import (
    UnknownSchemaVersionError,
    upcast_to,
)


@pytest.fixture
def _restore_context():
    import investigation_agent_platform.api.dependencies as deps

    prev = deps._context
    yield
    deps._context = prev


def _manifest(**overrides) -> PluginManifest:
    base: dict = {
        "plugin_id": "test-fetcher",
        "plugin_kind": PluginKind.REFERENCE_FETCHER,
        "contract_version": "1.0",
        "implementation_version": "1.0",
        "config_schema_version": "1.0",
        "capabilities": frozenset({"fetch"}),
        "config_schema": {"type": "object"},
    }
    base.update(overrides)
    return PluginManifest(**base)


# ===========================================================================
# Registry rejection paths
# ===========================================================================


class TestRegistry:
    def test_register_and_resolve(self) -> None:
        registry = ReferenceFetcherRegistry()
        registry.register(_manifest(), implementation=object())
        assert len(registry) == 1
        assert registry.get("test-fetcher") is not None
        assert registry.manifests()[0].plugin_id == "test-fetcher"

    def test_duplicate_plugin_id_rejected(self) -> None:
        registry = ReferenceFetcherRegistry()
        registry.register(_manifest(), implementation=object())
        with pytest.raises(PluginRegistrationError):
            registry.register(_manifest(), implementation=object())

    def test_wrong_kind_rejected(self) -> None:
        registry = CredentialProviderRegistry()
        with pytest.raises(PluginRegistrationError):
            registry.register(_manifest(), implementation=object())

    def test_incompatible_contract_rejected(self) -> None:
        registry = ReferenceFetcherRegistry()
        with pytest.raises(PluginRegistrationError):
            registry.register(_manifest(contract_version="2.0"), implementation=object())
        with pytest.raises(PluginRegistrationError):
            registry.register(_manifest(contract_version="0.9"), implementation=object())

    def test_unknown_plugin_id_fails_closed(self) -> None:
        registry = ReferenceFetcherRegistry()
        with pytest.raises(PluginRegistrationError):
            registry.get("no-such-plugin")

    def test_registries_are_independent(self) -> None:
        fetchers = ReferenceFetcherRegistry()
        creds = CredentialProviderRegistry()
        fetchers.register(_manifest(), implementation=object())
        assert len(creds) == 0
        with pytest.raises(PluginRegistrationError):
            creds.get("test-fetcher")


# ===========================================================================
# Compatibility ranges
# ===========================================================================


class TestCompatibility:
    def test_same_major_in_range(self) -> None:
        assert CompatibilityRange().is_compatible("1.0")
        assert CompatibilityRange().is_compatible("1.7.2")
        assert CompatibilityRange().is_compatible("v1.2")

    def test_major_bump_rejected(self) -> None:
        assert not CompatibilityRange().is_compatible("2.0")

    def test_below_min_rejected(self) -> None:
        assert not CompatibilityRange(min_version="1.2").is_compatible("1.1.9")

    def test_custom_range(self) -> None:
        r = CompatibilityRange(min_version="1.2", max_version="1.5")
        assert r.is_compatible("1.3")
        assert not r.is_compatible("1.6")


# ===========================================================================
# Plugin configuration policy
# ===========================================================================


class TestPluginConfig:
    def _config(self, **overrides) -> PluginConfig:
        base: dict = {
            "plugin_id": "test-fetcher",
            "config_schema_version": "1.0",
            "config": {},
        }
        base.update(overrides)
        return PluginConfig(**base)

    def test_valid_config_passes(self) -> None:
        manifest = _manifest(config_schema={"type": "object"})
        validate_plugin_config(manifest, self._config())

    def test_id_mismatch_rejected(self) -> None:
        with pytest.raises(PluginConfigError):
            validate_plugin_config(_manifest(), self._config(plugin_id="other"))

    def test_schema_version_skew_rejected(self) -> None:
        with pytest.raises(PluginConfigError):
            validate_plugin_config(_manifest(), self._config(config_schema_version="2.0"))

    def test_json_schema_violation_rejected(self) -> None:
        manifest = _manifest(
            config_schema={
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            }
        )
        with pytest.raises(PluginConfigError):
            validate_plugin_config(manifest, self._config())

    def test_inline_secret_rejected(self) -> None:
        manifest = _manifest(config_schema={"type": "object"})
        with pytest.raises(PluginConfigError):
            validate_plugin_config(manifest, self._config(config={"token": "inline-secret"}))

    def test_secret_reference_shape_passes_schema(self) -> None:
        manifest = _manifest(config_schema={"type": "object"})
        validate_plugin_config(manifest, self._config(config={"token": {"$secret": "OAUTH_TOKEN"}}))

    def test_secret_reference_model_policy(self) -> None:
        ref = SecretReference(provider="env", name="OAUTH_TOKEN")
        assert ref.name == "OAUTH_TOKEN"
        with pytest.raises(ValidationError):
            SecretReference(provider="env", name="lowercase-not-allowed")
        with pytest.raises(ValidationError):
            SecretReference(provider="", name="X")

    def test_resolve_secret_allowlist(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("IAP_TEST_SECRET", "s3cr3t")
        allowed = frozenset({"IAP_TEST_SECRET"})
        assert (
            resolve_secret(
                SecretReference(provider="env", name="IAP_TEST_SECRET"), allowed_env_names=allowed
            )
            == "s3cr3t"
        )
        with pytest.raises(PluginConfigError):
            resolve_secret(
                SecretReference(provider="env", name="NOT_ALLOWLISTED"),
                allowed_env_names=allowed,
            )
        with pytest.raises(PluginConfigError):
            resolve_secret(
                SecretReference(provider="vault", name="IAP_TEST_SECRET"),
                allowed_env_names=allowed,
            )

    def test_resolve_secret_unset_fails_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("IAP_TEST_SECRET_UNSET", raising=False)
        with pytest.raises(PluginConfigError):
            resolve_secret(
                SecretReference(provider="env", name="IAP_TEST_SECRET_UNSET"),
                allowed_env_names=frozenset({"IAP_TEST_SECRET_UNSET"}),
            )

    def test_redact_config(self) -> None:
        redacted = redact_config(
            {
                "url": "https://x",
                "token": "abc",
                "nested": {"password": "p", "ok": 1},
                "items": [{"api_key": "k"}, {"plain": "v"}],
            }
        )
        assert redacted["url"] == "https://x"
        assert redacted["token"] == "***"
        assert redacted["nested"] == {"password": "***", "ok": 1}
        assert redacted["items"] == [{"api_key": "***"}, {"plain": "v"}]


# ===========================================================================
# Scope and cache keys
# ===========================================================================


class TestScope:
    def test_cache_key_is_scoped(self) -> None:
        a = CapabilityScope(tenant_id="t-a", application_id="app")
        b = CapabilityScope(tenant_id="t-b", application_id="app")
        assert scope_cache_key(a, "tok") != scope_cache_key(b, "tok")
        assert scope_cache_key(a, "tok") == "t-a:app:tok"

    def test_cache_key_without_application(self) -> None:
        scope = CapabilityScope(tenant_id="t-a")
        assert scope_cache_key(scope, "tok") == "t-a:-:tok"

    def test_scope_rejects_empty_tenant(self) -> None:
        with pytest.raises(ValidationError):
            CapabilityScope(tenant_id="")


# ===========================================================================
# Schema upcasting
# ===========================================================================


class _BumpMinor:
    from_version = "1.0"
    to_version = "1.1"

    def upcast(self, payload: dict) -> dict:
        out = dict(payload)
        out["added_in_11"] = True
        return out


class TestUpcasting:
    def test_already_current_returns_as_is(self) -> None:
        payload = {"schema_version": "1.1", "x": 1}
        assert upcast_to(payload, "1.1", [_BumpMinor()]) == payload

    def test_chain_applies_steps(self) -> None:
        out = upcast_to({"schema_version": "1.0"}, "1.1", [_BumpMinor()])
        assert out["schema_version"] == "1.1"
        assert out["added_in_11"] is True

    def test_missing_step_fails_closed(self) -> None:
        with pytest.raises(UnknownSchemaVersionError):
            upcast_to({"schema_version": "9.9"}, "1.1", [_BumpMinor()])

    def test_exhausted_chain_fails_closed(self) -> None:
        class _Loop:
            from_version = "1.0"
            to_version = "1.0"

            def upcast(self, payload: dict) -> dict:
                return dict(payload)

        with pytest.raises(UnknownSchemaVersionError):
            upcast_to({"schema_version": "1.0"}, "2.0", [_Loop()], max_steps=2)


# ===========================================================================
# Capability discovery endpoint
# ===========================================================================


class TestCapabilitiesEndpoint:
    def test_requires_authentication(self, _restore_context: None) -> None:
        set_app_context(AppContext())
        client = TestClient(create_app())
        resp = client.get("/api/v1/capabilities")
        assert resp.status_code == 401

    def test_lists_safe_fields_only(self, _restore_context: None) -> None:
        set_app_context(AppContext())
        client = TestClient(create_app())
        resp = client.get("/api/v1/capabilities", headers={"X-Tenant-ID": "tenant-a"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == len(body["items"]) > 0
        for item in body["items"]:
            assert set(item.keys()) == {
                "capability_id",
                "contract_version",
                "status",
                "modes",
                "limits",
            }
        ids = {item["capability_id"] for item in body["items"]}
        assert any(i.startswith("LLM_COMPLETION:llm:") for i in ids)
        assert any(i.startswith("mcp_tool:") for i in ids)

    def test_no_secrets_endpoints_or_config_in_payload(self, _restore_context: None) -> None:
        set_app_context(AppContext())
        client = TestClient(create_app())
        resp = client.get("/api/v1/capabilities", headers={"X-Tenant-ID": "tenant-a"})
        blob = resp.text.lower()
        for forbidden in ("localhost", "5432", "sk-", "bearer", "postgresql://", "secret"):
            assert forbidden not in blob


# ===========================================================================
# Factory adaptation + old-fixture parsing
# ===========================================================================


class TestFactoryAdaptation:
    def test_llm_manifests_published(self) -> None:
        manifests = adapt_llm_factory_manifests()
        assert len(manifests) > 0
        assert all(m.plugin_kind == PluginKind.LLM_COMPLETION for m in manifests)
        assert all(m.contract_version == "1.0" for m in manifests)
        registries = build_default_registries()
        assert len(registries.completion_gateways) == len(manifests)
        assert len(list_capabilities(registries)) >= len(manifests)

    def test_background_job_registry_kind(self) -> None:
        from investigation_agent_platform.application.extensions.registries import (
            BackgroundJobKindRegistry,
        )

        registry = BackgroundJobKindRegistry()
        manifest = _manifest(
            plugin_id="kind-a",
            plugin_kind=PluginKind.BACKGROUND_JOB_KIND,
        )
        registry.register(manifest, implementation=object())
        assert registry.get("kind-a") is not None


class TestOldFixtures:
    def test_minimal_v1_fixture_still_parses(self) -> None:
        """A fixture written against the original contract (no optional
        fields) must still validate — additive compatibility."""
        fixture = {
            "plugin_id": "old-fetcher",
            "plugin_kind": "REFERENCE_FETCHER",
            "contract_version": "1.0",
            "implementation_version": "1.0",
            "config_schema_version": "1.0",
        }
        manifest = PluginManifest(**fixture)
        assert manifest.capabilities == frozenset()
        assert manifest.config_schema == {}

    def test_scope_fixture_parses(self) -> None:
        assert (
            CapabilityScope.model_validate({"tenant_id": "t", "application_id": "a"}).application_id
            == "a"
        )
        assert CapabilityScope.model_validate({"tenant_id": "t"}).application_id is None
