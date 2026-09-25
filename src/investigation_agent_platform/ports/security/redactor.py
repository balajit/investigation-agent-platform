# src/investigation_agent_platform/ports/security/redactor.py
"""Security, policy enforcement, data sanitization, and safety port protocols."""

from typing import Any, Protocol, runtime_checkable

from investigation_agent_platform.domain.evidence.models import Evidence


@runtime_checkable
class EvidenceSanitizerPort(Protocol):
    """Port for inspecting, anonymizing, and redacting PII, credentials, and secrets."""

    async def sanitize_evidence(self, tenant_id: str, evidence: Evidence) -> Evidence:
        ...

    async def redact_text(self, tenant_id: str, text_content: str) -> str:
        ...


@runtime_checkable
class QueryPolicyPort(Protocol):
    """Port interface for validating incoming query requests against tenant access rules."""

    async def validate_runtime_request(self, tenant_id: str, request: Any) -> None:
        ...

    async def validate_state_request(
        self, tenant_id: str, template_id: str, parameters: dict[str, Any]
    ) -> None:
        ...

    async def validate_code_search_request(self, tenant_id: str, request: Any) -> None:
        ...

    async def validate_source_request(self, tenant_id: str, request: Any) -> None:
        ...

    async def validate_symbol_request(self, tenant_id: str, request: Any) -> None:
        ...

    async def validate_code_history_request(self, tenant_id: str, request: Any) -> None:
        ...


@runtime_checkable
class ActionAuthorizerPort(Protocol):
    """Port interface for validating whether an agent is authorized to execute an action."""

    async def authorize_action(
        self, tenant_id: str, action_type: str, target_resource: str, context: dict[str, Any]
    ) -> bool:
        ...


@runtime_checkable
class CapabilityRegistryPort(Protocol):
    """Port interface for managing dynamic tenant capabilities and allowed tool lists."""

    async def is_capability_enabled(self, tenant_id: str, capability_name: str) -> bool:
        ...

    async def list_allowed_capabilities(self, tenant_id: str) -> list[str]:
        ...


@runtime_checkable
class PromptSafetyPolicyPort(Protocol):
    """Port interface for evaluating prompt safety and defending against indirect prompt injection."""

    async def validate_prompt_safety(self, tenant_id: str, prompt_text: str) -> bool:
        ...