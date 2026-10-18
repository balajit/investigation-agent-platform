# src/investigation_agent_platform/application/evidence/selector.py
import logging
from typing import Any, TypeVar

from opentelemetry import trace

from investigation_agent_platform.domain.common.exceptions import (
    ProviderNotConfiguredError,
)
from investigation_agent_platform.infrastructure.observability.tracing import traced
from investigation_agent_platform.ports.evidence.code import (
    CodeEvidenceProviderProtocol,
    CodeIntelligenceProviderProtocol,
)
from investigation_agent_platform.ports.evidence.runtime import RuntimeEvidenceProviderProtocol
from investigation_agent_platform.ports.evidence.state import StateEvidenceProviderProtocol

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)


def _selected_extra(provider: Any) -> dict[str, Any]:
    """Selected provider type only (keys are config, safe; no payloads)."""
    try:
        return {"iap.provider_type": type(provider).__name__[:128]}
    except Exception:
        return {}

T = TypeVar("T")


class EvidenceProviderSelector:
    """Dynamically routes evidence requests with strict tenant isolation and contract enforcement."""

    def __init__(self) -> None:
        self._runtime_providers: dict[str, RuntimeEvidenceProviderProtocol] = {}
        self._state_providers: dict[str, StateEvidenceProviderProtocol] = {}
        self._code_providers: dict[str, CodeEvidenceProviderProtocol] = {}
        self._code_intelligence_providers: dict[str, CodeIntelligenceProviderProtocol] = {}
        self._default_runtime_key: str | None = None
        self._datasource_registry: object | None = None
        self._is_frozen = False

    def attach_datasource_registry(self, registry: object) -> None:
        """Profile-driven state fallback (appl-profile 1.0 §7.4).

        Static registrations keep priority; the registry serves contexts
        with no static provider from the profile's own datasources.
        """
        self._datasource_registry = registry

    def freeze(self) -> None:
        """Freezes provider registrations to guarantee startup immutability."""
        self._is_frozen = True
        logger.info("EvidenceProviderSelector frozen. Registrations locked.")

    def _register(
        self,
        registry: dict[str, T],
        key: str,
        provider: T,
        protocol_type: type,
        provider_type_name: str,
    ) -> None:
        """Internal generic helper to consolidate registration logic and safely check protocols."""
        if self._is_frozen:
            raise RuntimeError("Provider selector is frozen and cannot accept registrations.")

        # Safe protocol verification: runtime_checkable protocol check or fallback
        if getattr(protocol_type, "_is_runtime_protocol", False):
            if not isinstance(provider, protocol_type):
                raise TypeError(
                    f"Provider '{provider}' does not implement {protocol_type.__name__}"
                )

        registry[key] = provider
        logger.info(
            f"Registered {provider_type_name} provider",
            extra={"provider_key": key, "provider_type": provider_type_name},
        )

    # Public Registration Methods
    def register_runtime_provider(
        self, key: str, provider: RuntimeEvidenceProviderProtocol, *, default: bool = False
    ) -> None:
        self._register(
            self._runtime_providers, key, provider, RuntimeEvidenceProviderProtocol, "runtime"
        )
        if default:
            if self._is_frozen:
                raise RuntimeError("Provider selector is frozen and cannot accept registrations.")
            self._default_runtime_key = key

    def register_state_provider(self, key: str, provider: StateEvidenceProviderProtocol) -> None:
        self._register(self._state_providers, key, provider, StateEvidenceProviderProtocol, "state")

    def register_code_provider(self, key: str, provider: CodeEvidenceProviderProtocol) -> None:
        self._register(self._code_providers, key, provider, CodeEvidenceProviderProtocol, "code")

    def register_code_intelligence_provider(
        self, key: str, provider: CodeIntelligenceProviderProtocol
    ) -> None:
        self._register(
            self._code_intelligence_providers,
            key,
            provider,
            CodeIntelligenceProviderProtocol,
            "code_intelligence",
        )

    # Async Getter Methods
    @traced("gateway", component="selector", extra_from=_selected_extra)
    async def get_runtime_provider(
        self, tenant_id: str, application_id: str, environment: str
    ) -> RuntimeEvidenceProviderProtocol:
        """Resolve the runtime provider for a tenant/app/environment context.

        Routing order: exact context key first, then the registered default
        provider (Part 9: a single shared Elastic deployment serves all
        tenants — per-request tenant scoping happens in the adapter's index
        pattern, not via per-tenant provider instances). No match anywhere
        fails closed.
        """
        with tracer.start_as_current_span("EvidenceProviderSelector.get_runtime_provider"):
            key = f"{tenant_id}:{application_id}:{environment}"
            provider = self._runtime_providers.get(key)
            if provider is None and self._default_runtime_key is not None:
                provider = self._runtime_providers.get(self._default_runtime_key)
                if provider is not None:
                    logger.debug(
                        "Runtime provider resolved via default registration",
                        extra={"default_key": self._default_runtime_key},
                    )
            if not provider:
                logger.error(
                    "No runtime provider configured for tenant/app/env context",
                    extra={
                        "tenant_id": tenant_id,
                        "application_id": application_id,
                        "environment": environment,
                    },
                )
                raise ProviderNotConfiguredError(f"No runtime provider configured for tenant context '{key}'")
            return provider

    def find_runtime_provider(self, key: str) -> RuntimeEvidenceProviderProtocol | None:
        """Return the provider under a registration key, or None when absent.

        Non-raising lookup for optional fallbacks (e.g. file-log when the
        default deployment returns no rows). Never fails closed by itself —
        callers decide whether absence matters.
        """
        return self._runtime_providers.get(key)

    def list_state_providers(self) -> list[tuple[str, Any]]:
        """All registered state providers as (key, provider) pairs (P3-2).

        Used by fan-out: the gateway queries every tenant-authorized MCP
        state provider instead of resolving exactly one.
        """
        return list(self._state_providers.items())

    @traced("gateway", component="selector", extra_from=_selected_extra)
    async def get_state_provider(
        self, tenant_id: str, application_id: str, environment: str
    ) -> StateEvidenceProviderProtocol:
        with tracer.start_as_current_span("EvidenceProviderSelector.get_state_provider"):
            key = f"{tenant_id}:{application_id}:{environment}"
            provider = self._state_providers.get(key)
            if provider is not None:
                return provider
            registry = self._datasource_registry
            if registry is not None:
                resolve_default = getattr(registry, "resolve_default", None)
                if callable(resolve_default):
                    try:
                        resolved: StateEvidenceProviderProtocol = await resolve_default(
                            tenant_id, application_id
                        )
                    except Exception as exc:
                        logger.warning(
                            "Datasource registry fallback failed; failing closed",
                            extra={
                                "tenant_id": tenant_id,
                                "application_id": application_id,
                                "error": str(exc),
                            },
                        )
                    else:
                        return resolved
            raise ProviderNotConfiguredError(f"No state provider configured for tenant context '{key}'")

    @traced("gateway", component="selector", extra_from=_selected_extra)
    async def get_code_provider(
        self, tenant_id: str, application_id: str
    ) -> CodeEvidenceProviderProtocol:
        with tracer.start_as_current_span("EvidenceProviderSelector.get_code_provider"):
            key = f"{tenant_id}:{application_id}"
            provider = self._code_providers.get(key)
            if provider is not None:
                return provider
            # Bootstrap registers the local git adapter under a global key
            # ("pygit2-local"), never per-tenant — without a fallback every
            # tenant lookup misses and GET_CODE is dead. Fall back only when
            # exactly one code provider exists (single-provider deployments);
            # with several, serving another tenant's repo would breach
            # isolation, so keep failing closed. Per-request tenant scoping
            # (adapter allow-list + profile locators) still applies.
            if len(self._code_providers) == 1:
                sole = next(iter(self._code_providers.values()))
                logger.info(
                    "Code provider fallback to sole registered provider",
                    extra={"tenant_id": tenant_id, "application_id": application_id},
                )
                return sole
            raise ProviderNotConfiguredError(f"No code provider configured for tenant context '{key}'")

    @traced("gateway", component="selector", extra_from=_selected_extra)
    async def get_code_intelligence_provider(
        self, tenant_id: str, application_id: str
    ) -> CodeIntelligenceProviderProtocol:
        with tracer.start_as_current_span(
            "EvidenceProviderSelector.get_code_intelligence_provider"
        ):
            key = f"{tenant_id}:{application_id}"
            provider = self._code_intelligence_providers.get(key)
            if provider is not None:
                return provider
            # Same single-provider fallback as get_code_provider (see above
            # for the isolation rationale).
            if len(self._code_intelligence_providers) == 1:
                sole = next(iter(self._code_intelligence_providers.values()))
                logger.info(
                    "Code intelligence provider fallback to sole registered provider",
                    extra={"tenant_id": tenant_id, "application_id": application_id},
                )
                return sole
            raise ProviderNotConfiguredError(
                f"No code intelligence provider configured for tenant context '{key}'"
            )
