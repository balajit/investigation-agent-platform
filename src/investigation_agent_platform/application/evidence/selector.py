# src/investigation_agent_platform/application/evidence/selector.py
import logging

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


class EvidenceProviderSelector:
    """Dynamically routes evidence requests with strict tenant isolation and contract enforcement."""

    def __init__(self) -> None:
        self._runtime_providers: dict[str, RuntimeEvidenceProviderProtocol] = {}
        self._state_providers: dict[str, StateEvidenceProviderProtocol] = {}
        self._code_providers: dict[str, CodeEvidenceProviderProtocol] = {}
        self._code_intelligence_providers: dict[str, CodeIntelligenceProviderProtocol] = {}
        self._is_frozen = False

    def freeze(self) -> None:
        """Freezes provider registrations to guarantee startup immutability."""
        self._is_frozen = True

    def register_runtime_provider(
        self, key: str, provider: RuntimeEvidenceProviderProtocol
    ) -> None:
        if self._is_frozen:
            raise RuntimeError("Provider selector is frozen and cannot accept registrations.")
        if not isinstance(provider, RuntimeEvidenceProviderProtocol):
            raise TypeError(
                f"Provider does not implement RuntimeEvidenceProviderProtocol: {provider}"
            )
        self._runtime_providers[key] = provider
        logger.info("Registered runtime provider", extra={"provider_key": key})

    def register_state_provider(self, key: str, provider: StateEvidenceProviderProtocol) -> None:
        if self._is_frozen:
            raise RuntimeError("Provider selector is frozen.")
        if not isinstance(provider, StateEvidenceProviderProtocol):
            raise TypeError(
                f"Provider does not implement StateEvidenceProviderProtocol: {provider}"
            )
        self._state_providers[key] = provider
        logger.info("Registered state provider", extra={"provider_key": key})

    def register_code_provider(self, key: str, provider: CodeEvidenceProviderProtocol) -> None:
        if self._is_frozen:
            raise RuntimeError("Provider selector is frozen.")
        if not isinstance(provider, CodeEvidenceProviderProtocol):
            raise TypeError(f"Provider does not implement CodeEvidenceProviderProtocol: {provider}")
        self._code_providers[key] = provider
        logger.info("Registered code provider", extra={"provider_key": key})

    def register_code_intelligence_provider(
        self, key: str, provider: CodeIntelligenceProviderProtocol
    ) -> None:
        if self._is_frozen:
            raise RuntimeError("Provider selector is frozen.")
        if not isinstance(provider, CodeIntelligenceProviderProtocol):
            raise TypeError(
                f"Provider does not implement CodeIntelligenceProviderProtocol: {provider}"
            )
        self._code_intelligence_providers[key] = provider
        logger.info("Registered code intelligence provider", extra={"provider_key": key})

    async def get_runtime_provider(
        self, tenant_id: str, application_id: str, environment: str
    ) -> RuntimeEvidenceProviderProtocol | None:
        with tracer.start_as_current_span("EvidenceProviderSelector.get_runtime_provider"):
            key = f"{tenant_id}:{application_id}:{environment}"
            provider = self._runtime_providers.get(key)
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
    ) -> StateEvidenceProviderProtocol | None:
        with tracer.start_as_current_span("EvidenceProviderSelector.get_state_provider"):
            key = f"{tenant_id}:{application_id}:{environment}"
            provider = self._state_providers.get(key)
            if not provider:
                raise ExecutionError(f"No state provider configured for tenant context '{key}'")
            return provider

    async def get_code_provider(
        self, tenant_id: str, application_id: str
    ) -> CodeEvidenceProviderProtocol | None:
        with tracer.start_as_current_span("EvidenceProviderSelector.get_code_provider"):
            key = f"{tenant_id}:{application_id}"
            provider = self._code_providers.get(key)
            if not provider:
                raise ExecutionError(f"No code provider configured for tenant context '{key}'")
            return provider

    async def get_code_intelligence_provider(
        self, tenant_id: str, application_id: str
    ) -> CodeIntelligenceProviderProtocol | None:
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
