# src/investigation_agent_platform/application/evidence/selector.py
import logging
from typing import TypeVar

from opentelemetry import trace

from investigation_agent_platform.domain.common.exceptions import ExecutionError
from investigation_agent_platform.ports.evidence.code import (
    CodeEvidenceProviderProtocol,
    CodeIntelligenceProviderProtocol,
)
from investigation_agent_platform.ports.evidence.runtime import RuntimeEvidenceProviderProtocol
from investigation_agent_platform.ports.evidence.state import StateEvidenceProviderProtocol

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)

T = TypeVar("T")


class EvidenceProviderSelector:
    """Dynamically routes evidence requests with strict tenant isolation and contract enforcement."""

    def __init__(self) -> None:
        self._runtime_providers: dict[str, RuntimeEvidenceProviderProtocol] = {}
        self._state_providers: dict[str, StateEvidenceProviderProtocol] = {}
        self._code_providers: dict[str, CodeEvidenceProviderProtocol] = {}
        self._code_intelligence_providers: dict[str, CodeIntelligenceProviderProtocol] = {}
        self._default_runtime_key: str | None = None
        self._is_frozen = False

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
                raise ExecutionError(f"No runtime provider configured for tenant context '{key}'")
            return provider

    async def get_state_provider(
        self, tenant_id: str, application_id: str, environment: str
    ) -> StateEvidenceProviderProtocol:
        with tracer.start_as_current_span("EvidenceProviderSelector.get_state_provider"):
            key = f"{tenant_id}:{application_id}:{environment}"
            provider = self._state_providers.get(key)
            if not provider:
                raise ExecutionError(f"No state provider configured for tenant context '{key}'")
            return provider

    async def get_code_provider(
        self, tenant_id: str, application_id: str
    ) -> CodeEvidenceProviderProtocol:
        with tracer.start_as_current_span("EvidenceProviderSelector.get_code_provider"):
            key = f"{tenant_id}:{application_id}"
            provider = self._code_providers.get(key)
            if not provider:
                raise ExecutionError(f"No code provider configured for tenant context '{key}'")
            return provider

    async def get_code_intelligence_provider(
        self, tenant_id: str, application_id: str
    ) -> CodeIntelligenceProviderProtocol:
        with tracer.start_as_current_span(
            "EvidenceProviderSelector.get_code_intelligence_provider"
        ):
            key = f"{tenant_id}:{application_id}"
            provider = self._code_intelligence_providers.get(key)
            if not provider:
                raise ExecutionError(
                    f"No code intelligence provider configured for tenant context '{key}'"
                )
            return provider
