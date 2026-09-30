# src/investigation_agent_platform/application/extensions/registries.py
"""Typed plugin registries and plugin-configuration policy (Part 11.1).

Registries hold platform-installed implementations only: ``register()``
accepts a manifest plus an already-constructed implementation object, so
tenant/application input can select plugins by id but can never introduce new
code. Each registry enforces its ``PluginKind`` and a compatible contract
range at registration time (fail-closed), not at first use.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from investigation_agent_platform.domain.common.exceptions import PlatformConfigurationError
from investigation_agent_platform.domain.common.extension import (
    CapabilityScope,
    CompatibilityRange,
    PluginConfig,
    PluginKind,
    PluginManifest,
    SecretReference,
)

logger = logging.getLogger(__name__)


class PluginRegistrationError(PlatformConfigurationError):
    """Raised when a plugin manifest is rejected at registration."""


class PluginConfigError(PlatformConfigurationError):
    """Raised when a profile's plugin configuration is invalid or unsafe."""


@dataclass
class RegisteredPlugin[T]:
    manifest: PluginManifest
    implementation: T


class PluginRegistry[T]:
    """Kind-checked registry of trusted plugin implementations."""

    KIND: PluginKind | None = None

    def __init__(self, supported: CompatibilityRange | None = None) -> None:
        self._supported = supported or CompatibilityRange()
        self._entries: dict[str, RegisteredPlugin[T]] = {}

    def register(self, manifest: PluginManifest, implementation: T) -> None:
        """Register one implementation; rejects kind/contract/id problems."""
        if self.KIND is not None and manifest.plugin_kind != self.KIND:
            raise PluginRegistrationError(
                f"Registry for {self.KIND.value} cannot accept "
                f"{manifest.plugin_kind.value} plugin {manifest.plugin_id!r}"
            )
        if not self._supported.is_compatible(manifest.contract_version):
            raise PluginRegistrationError(
                f"Plugin {manifest.plugin_id!r} contract "
                f"{manifest.contract_version!r} outside supported range "
                f"[{self._supported.min_version}, {self._supported.max_version}]"
            )
        if manifest.plugin_id in self._entries:
            raise PluginRegistrationError(
                f"Duplicate plugin_id {manifest.plugin_id!r} in {type(self).__name__}"
            )
        self._entries[manifest.plugin_id] = RegisteredPlugin(manifest, implementation)
        logger.info(
            "Registered plugin",
            extra={"plugin_id": manifest.plugin_id, "kind": manifest.plugin_kind.value},
        )

    def get(self, plugin_id: str) -> T:
        """Resolve an implementation by id; unknown ids fail closed."""
        try:
            return self._entries[plugin_id].implementation
        except KeyError:
            raise PluginRegistrationError(
                f"Unknown plugin_id {plugin_id!r} in {type(self).__name__}"
            ) from None

    def manifests(self) -> list[PluginManifest]:
        """Manifests in registration order (safe to expose; no config values)."""
        return [entry.manifest for entry in self._entries.values()]

    def __len__(self) -> int:
        return len(self._entries)


class ReferenceFetcherRegistry(PluginRegistry[Any]):
    KIND = PluginKind.REFERENCE_FETCHER


class CredentialProviderRegistry(PluginRegistry[Any]):
    KIND = PluginKind.CREDENTIAL_PROVIDER


class ReportRendererRegistry(PluginRegistry[Any]):
    KIND = PluginKind.REPORT_RENDERER


class CompletionLLMGatewayRegistry(PluginRegistry[Any]):
    KIND = PluginKind.LLM_COMPLETION


class StreamingLLMGatewayRegistry(PluginRegistry[Any]):
    KIND = PluginKind.LLM_STREAMING


class BackgroundJobKindRegistry(PluginRegistry[Any]):
    KIND = PluginKind.BACKGROUND_JOB_KIND


@dataclass
class ExtensionRegistries:
    """The six typed registries composing the Part 11 extension surface."""

    reference_fetchers: ReferenceFetcherRegistry = field(default_factory=ReferenceFetcherRegistry)
    credential_providers: CredentialProviderRegistry = field(
        default_factory=CredentialProviderRegistry
    )
    report_renderers: ReportRendererRegistry = field(default_factory=ReportRendererRegistry)
    completion_gateways: CompletionLLMGatewayRegistry = field(
        default_factory=CompletionLLMGatewayRegistry
    )
    streaming_gateways: StreamingLLMGatewayRegistry = field(
        default_factory=StreamingLLMGatewayRegistry
    )
    background_job_kinds: BackgroundJobKindRegistry = field(
        default_factory=BackgroundJobKindRegistry
    )

    def all_manifests(self) -> list[PluginManifest]:
        manifests: list[PluginManifest] = []
        for registry in (
            self.reference_fetchers,
            self.credential_providers,
            self.report_renderers,
            self.completion_gateways,
            self.streaming_gateways,
            self.background_job_kinds,
        ):
            manifests.extend(registry.manifests())
        return manifests


def adapt_llm_factory_manifests() -> list[PluginManifest]:
    """Expose the existing LLM factory MODEL_REGISTRY as plugin manifests.

    Read-only adaptation: the factory keeps full authority over model policy;
    this only publishes contract metadata so capability discovery and future
    completion-gateway registration share one source of truth.
    """
    from investigation_agent_platform.infrastructure.reasoning.factory import MODEL_REGISTRY

    return [
        PluginManifest(
            plugin_id=f"llm:{name}",
            plugin_kind=PluginKind.LLM_COMPLETION,
            contract_version="1.0",
            implementation_version="1.0",
            config_schema_version="1.0",
            capabilities=frozenset({"complete"}),
            config_schema={"type": "object"},
        )
        for name in sorted(MODEL_REGISTRY)
    ]


def build_default_registries() -> ExtensionRegistries:
    """Fresh registries with the LLM completion manifests pre-published.

    Feature phases (11.4+) register their adapters into an instance of this
    structure; nothing here performs I/O or reads tenant input.
    """
    registries = ExtensionRegistries()
    for manifest in adapt_llm_factory_manifests():
        registries.completion_gateways.register(manifest, implementation=None)
    return registries


# Keys that must only ever hold SecretReference values, never inline strings.
_SECRET_KEY_NAMES = frozenset({"secret", "token", "password", "api_key", "client_secret"})


def validate_plugin_config(manifest: PluginManifest, config: PluginConfig) -> None:
    """Validate a profile's plugin selection (fail-closed).

    Checks: id match, schema-version match, JSON Schema conformance, and that
    secret-typed fields use ``SecretReference`` (provider + allowlisted name)
    instead of inline credential strings.
    """
    if config.plugin_id != manifest.plugin_id:
        raise PluginConfigError(
            f"PluginConfig targets {config.plugin_id!r}, not {manifest.plugin_id!r}"
        )
    if config.config_schema_version != manifest.config_schema_version:
        raise PluginConfigError(
            f"Plugin {manifest.plugin_id!r} config schema "
            f"{config.config_schema_version!r} != manifest "
            f"{manifest.config_schema_version!r}; no silent schema skew"
        )
    if manifest.config_schema:
        import jsonschema  # type: ignore[import-untyped]

        try:
            jsonschema.validate(config.config, manifest.config_schema)
        except jsonschema.ValidationError as exc:
            raise PluginConfigError(
                f"Plugin {manifest.plugin_id!r} config failed schema validation: {exc.message}"
            ) from exc
    _reject_inline_secrets(manifest.plugin_id, config.config)


def _reject_inline_secrets(plugin_id: str, node: Any) -> None:
    """Walk config; secret-named fields must be SecretReference dicts."""
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(key, str) and key.lower() in _SECRET_KEY_NAMES and isinstance(value, str):
                raise PluginConfigError(
                    f"Plugin {plugin_id!r} field {key!r} must use a SecretReference "
                    "(provider + name), never an inline credential string"
                )
            _reject_inline_secrets(plugin_id, value)
    elif isinstance(node, list):
        for item in node:
            _reject_inline_secrets(plugin_id, item)


def redact_config(config: dict[str, Any]) -> dict[str, Any]:
    """Return a copy with secret-named values replaced (safe for logging)."""
    redacted: dict[str, Any] = {}
    for key, value in config.items():
        if isinstance(key, str) and key.lower() in _SECRET_KEY_NAMES:
            redacted[key] = "***"
        elif isinstance(value, dict):
            redacted[key] = redact_config(value)
        elif isinstance(value, list):
            redacted[key] = [
                redact_config(item) if isinstance(item, dict) else item for item in value
            ]
        else:
            redacted[key] = value
    return redacted


def resolve_secret(reference: SecretReference, *, allowed_env_names: frozenset[str]) -> str:
    """Resolve a SecretReference without ever logging the value (Part 11.1).

    ``env`` reads an allowlisted environment variable; anything else raises.
    ``manager`` is reserved for a future secret-manager adapter (Phase 11.4+).
    """
    import os

    if reference.provider == "env":
        if reference.name not in allowed_env_names:
            raise PluginConfigError(
                f"Secret {reference.name!r} is not in the allowlisted secret names"
            )
        value = os.environ.get(reference.name, "")
        if not value:
            raise PluginConfigError(f"Secret {reference.name!r} is not set")
        return value
    raise PluginConfigError(f"Unsupported secret provider {reference.provider!r}; expected 'env'")


def scope_cache_key(scope: CapabilityScope, *parts: str) -> str:
    """Build a tenant/application-scoped cache key (never provider-id alone)."""
    base = [scope.tenant_id, scope.application_id or "-", *parts]
    return ":".join(base)
