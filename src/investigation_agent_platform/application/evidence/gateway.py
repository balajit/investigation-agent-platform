# src/investigation_agent_platform/application/evidence/gateway.py
import asyncio
import logging
from typing import Any
from uuid import UUID

from opentelemetry import trace
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from investigation_agent_platform.domain.common.exceptions import (
    ExecutionError,
    SecurityPolicyViolationException,
)
from investigation_agent_platform.domain.evidence.models import Evidence
from investigation_agent_platform.domain.evidence.requests import (
    ApplicationStateRequest,
    CallGraphRequest,
    CodeHistoryRequest,
    CodeSearchRequest,
    RuntimeEvidenceRequest,
    SourceRequest,
    SymbolRequest,
)
from investigation_agent_platform.ports.correlation.engine import CorrelationExpander
from investigation_agent_platform.ports.evidence.gateway import (
    EvidenceGatewayProtocol,
    EvidenceQueryResult,
)
from investigation_agent_platform.ports.evidence.store import EvidenceStorePort
from investigation_agent_platform.ports.observability.telemetry import ObservabilityPort
from investigation_agent_platform.ports.security.redactor import (
    ActionAuthorizerPort,
    CapabilityRegistryPort,
    EvidenceSanitizerPort,
    QueryPolicyPort,
)

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)


class EvidenceDeduplicator:
    """Fingerprint-based deduplicator scoped per tenant.

    Keeps in-memory fingerprint history and can optionally consult an
    ``EvidenceRepository`` for persistence-backed checks.  Fingerprints
    are deterministic (see ``domain/evidence/models.py`` and provider
    implementations for elastic/oracle/git).
    """

    def __init__(self, evidence_repository: Any | None = None) -> None:
        self._seen: set[tuple[str, str]] = set()
        self._evidence_repository = evidence_repository

    def _is_seen(self, tenant_id: str, fingerprint: str) -> bool:
        return (tenant_id, fingerprint) in self._seen

    def _mark_seen(self, tenant_id: str, fingerprint: str) -> None:
        self._seen.add((tenant_id, fingerprint))

    async def _exists_in_store(self, tenant_id: str, fingerprint: str) -> bool:
        """Check persistence store for fingerprint collision if repository supports it."""
        repo = self._evidence_repository
        if repo is None:
            return False
        # Prefer explicit fingerprint lookup if repository exposes it
        check = getattr(repo, "exists_by_fingerprint", None)
        if callable(check):
            result = check(tenant_id, fingerprint)
            if asyncio.iscoroutine(result):
                return bool(await result)
            return bool(result)
        # Fallback: scan in-memory store (used by InMemoryEvidenceRepository)
        by_fp = getattr(repo, "_by_fingerprint", None)
        if isinstance(by_fp, dict):
            return (tenant_id, fingerprint) in by_fp
        return False

    async def deduplicate(self, tenant_id: str, items: list[Evidence]) -> list[Evidence]:
        deduped: list[Evidence] = []
        seen_batch: set[str] = set()
        for item in items:
            if item.fingerprint in seen_batch:
                continue
            if self._is_seen(tenant_id, item.fingerprint):
                continue
            if await self._exists_in_store(tenant_id, item.fingerprint):
                self._mark_seen(tenant_id, item.fingerprint)
                continue
            seen_batch.add(item.fingerprint)
            self._mark_seen(tenant_id, item.fingerprint)
            deduped.append(item)
        return deduped

    def deduplicate_sync(self, tenant_id: str, items: list[Evidence]) -> list[Evidence]:
        """Synchronous variant for contexts without repository I/O."""
        deduped: list[Evidence] = []
        seen_batch: set[str] = set()
        for item in items:
            if item.fingerprint in seen_batch:
                continue
            if self._is_seen(tenant_id, item.fingerprint):
                continue
            seen_batch.add(item.fingerprint)
            self._mark_seen(tenant_id, item.fingerprint)
            deduped.append(item)
        return deduped


class AsyncEvidenceGateway(EvidenceGatewayProtocol):
    """Asynchronous orchestrator for routing, redacting, persisting, and correlating evidence."""

    def __init__(
        self,
        provider_selector: Any,
        query_safety_policy: QueryPolicyPort,
        sanitizer: EvidenceSanitizerPort,
        evidence_store: EvidenceStorePort | None = None,
        correlation_engine: CorrelationExpander | None = None,
        telemetry: ObservabilityPort | None = None,
        timeout_seconds: float = 30.0,
        authorizer: ActionAuthorizerPort | None = None,
        capability_registry: CapabilityRegistryPort | None = None,
        structured_output_validator: Any | None = None,
        evidence_repository: Any | None = None,
        deduplicator: EvidenceDeduplicator | None = None,
    ) -> None:
        if sanitizer is None:
            raise ValueError(
                "EvidenceSanitizerPort is mandatory - sanitization at execution boundary is required"
            )
        if query_safety_policy is None:
            raise ValueError(
                "QueryPolicyPort is mandatory - validation at execution boundary is required"
            )
        self._provider_selector = provider_selector
        self._query_safety_policy = query_safety_policy
        self._sanitizer = sanitizer
        self._evidence_store = evidence_store
        self._correlation_engine = correlation_engine
        self._telemetry = telemetry
        self._timeout_seconds = timeout_seconds
        self._authorizer = authorizer
        self._capability_registry = capability_registry
        self._structured_output_validator = structured_output_validator
        self._evidence_repository = evidence_repository
        self._deduplicator = deduplicator or EvidenceDeduplicator(evidence_repository)

    async def _enforce_authorization(
        self, tenant_id: str, capability: str, resource: str = ""
    ) -> None:
        if self._authorizer is not None:
            authorized = await self._authorizer.authorize_action(
                tenant_id, capability, resource, {}
            )
            if not authorized:
                raise SecurityPolicyViolationException(
                    f"Agent unauthorized for capability '{capability}'"
                )
        if self._capability_registry is not None:
            enabled = await self._capability_registry.is_capability_enabled(tenant_id, capability)
            if not enabled:
                raise SecurityPolicyViolationException(f"Capability '{capability}' is disabled")

    async def _validate_structured_output(self, raw: Any) -> None:
        if self._structured_output_validator is not None:
            # Expect validator to raise SecurityPolicyViolationException on failure
            validate = getattr(self._structured_output_validator, "validate", None)
            if callable(validate):
                await validate(raw) if asyncio.iscoroutinefunction(validate) else validate(raw)
            else:
                # Generic callable
                result = self._structured_output_validator(raw)
                if asyncio.iscoroutine(result):
                    await result

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception(lambda e: getattr(e, "retryable", False)),
        reraise=True,
    )
    async def search_runtime_evidence(
        self,
        tenant_id: str,
        investigation_id: UUID,
        application_id: str,
        request: RuntimeEvidenceRequest,
    ) -> EvidenceQueryResult:
        with tracer.start_as_current_span("AsyncEvidenceGateway.search_runtime_evidence") as span:
            span.set_attribute("tenant_id", tenant_id)
            span.set_attribute("investigation_id", str(investigation_id))
            span.set_attribute("application_id", application_id)
            await self._enforce_authorization(tenant_id, "SEARCH_LOGS")
            await self._validate_structured_output(request)
            await self._query_safety_policy.validate_runtime_request(tenant_id, request)
            provider = await self._provider_selector.get_runtime_provider(
                tenant_id, application_id, request.environment
            )
            if not provider:
                raise ExecutionError(
                    f"No runtime provider available for environment '{request.environment}'"
                )

            result = await asyncio.wait_for(
                provider.search_runtime_evidence(
                    tenant_id, investigation_id, request, provider.profile
                ),
                timeout=self._timeout_seconds,
            )

            sanitized_items = [
                await self._sanitizer.sanitize_evidence(tenant_id, item) for item in result.items
            ]
            deduped = await self._deduplicator.deduplicate(tenant_id, sanitized_items)
            return EvidenceQueryResult(
                items=deduped,
                cursor=result.cursor,
                total_count=len(deduped),
                has_more=result.has_more,
                provider_metadata=result.provider_metadata,
                execution_metadata=result.execution_metadata,
            )

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception(lambda e: getattr(e, "retryable", False)),
        reraise=True,
    )
    async def get_application_state(
        self,
        tenant_id: str,
        investigation_id: UUID,
        application_id: str,
        request: ApplicationStateRequest,
    ) -> EvidenceQueryResult:
        with tracer.start_as_current_span("AsyncEvidenceGateway.get_application_state"):
            await self._enforce_authorization(tenant_id, "QUERY_STATE")
            await self._validate_structured_output(request)
            await self._query_safety_policy.validate_state_request(
                tenant_id, request.template_id, request.parameters
            )
            provider = await self._provider_selector.get_state_provider(
                tenant_id, application_id, request.environment
            )
            if not provider:
                raise ExecutionError(
                    f"No state provider available for environment '{request.environment}'"
                )

            result = await asyncio.wait_for(
                provider.get_application_state(
                    tenant_id, investigation_id, request, provider.profile
                ),
                timeout=self._timeout_seconds,
            )
            sanitized_items = [
                await self._sanitizer.sanitize_evidence(tenant_id, item) for item in result.items
            ]
            deduped = await self._deduplicator.deduplicate(tenant_id, sanitized_items)
            return EvidenceQueryResult(
                items=deduped,
                cursor=result.cursor,
                total_count=len(deduped),
                has_more=result.has_more,
            )

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception(lambda e: getattr(e, "retryable", False)),
        reraise=True,
    )
    async def search_code(
        self,
        tenant_id: str,
        investigation_id: UUID,
        application_id: str,
        request: CodeSearchRequest,
    ) -> EvidenceQueryResult:
        with tracer.start_as_current_span("AsyncEvidenceGateway.search_code"):
            await self._enforce_authorization(tenant_id, "SEARCH_CODE")
            await self._validate_structured_output(request)
            # Validate via policy if method exists
            validate_fn = getattr(self._query_safety_policy, "validate_code_search_request", None)
            if callable(validate_fn):
                await validate_fn(tenant_id, request)
            provider = await self._provider_selector.get_code_intelligence_provider(
                tenant_id, application_id
            )
            if not provider:
                raise ExecutionError(f"No code provider available for app '{application_id}'")
            raw_items = await asyncio.wait_for(
                provider.search_code(tenant_id, request.query, provider.profile),
                timeout=self._timeout_seconds,
            )
            sanitized_items = [
                await self._sanitizer.sanitize_evidence(tenant_id, e) for e in raw_items
            ]
            deduped = await self._deduplicator.deduplicate(tenant_id, sanitized_items)
            return EvidenceQueryResult(items=deduped, total_count=len(deduped))

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception(lambda e: getattr(e, "retryable", False)),
        reraise=True,
    )
    async def find_symbol(
        self, tenant_id: str, investigation_id: UUID, application_id: str, request: SymbolRequest
    ) -> EvidenceQueryResult:
        with tracer.start_as_current_span("AsyncEvidenceGateway.find_symbol"):
            await self._enforce_authorization(tenant_id, "GET_CODE")
            await self._validate_structured_output(request)
            validate_fn = getattr(self._query_safety_policy, "validate_symbol_request", None)
            if callable(validate_fn):
                await validate_fn(tenant_id, request)
            provider = await self._provider_selector.get_code_intelligence_provider(
                tenant_id, application_id
            )
            if not provider:
                raise ExecutionError(f"No code provider available for app '{application_id}'")
            symbols = await asyncio.wait_for(
                provider.find_symbol(tenant_id, request.symbol_name, provider.profile),
                timeout=self._timeout_seconds,
            )
            # Symbols are metadata; sanitize not applicable but ensure no raw source leak — return via sanitized envelope
            return EvidenceQueryResult(
                items=[],
                total_count=len(symbols),
                provider_metadata={"symbols": [s.model_dump() for s in symbols]},
            )

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception(lambda e: getattr(e, "retryable", False)),
        reraise=True,
    )
    async def get_source(
        self, tenant_id: str, investigation_id: UUID, application_id: str, request: SourceRequest
    ) -> Evidence:
        with tracer.start_as_current_span("AsyncEvidenceGateway.get_source"):
            await self._enforce_authorization(tenant_id, "GET_CODE", request.file_path)
            await self._validate_structured_output(request)
            validate_fn = getattr(self._query_safety_policy, "validate_source_request", None)
            if callable(validate_fn):
                await validate_fn(tenant_id, request)
            provider = await self._provider_selector.get_code_provider(tenant_id, application_id)
            if not provider:
                raise ExecutionError(f"No code provider available for app '{application_id}'")
            raw_evidence = await asyncio.wait_for(
                provider.get_source(
                    tenant_id, investigation_id, request.file_path, provider.profile
                ),
                timeout=self._timeout_seconds,
            )
            return await self._sanitizer.sanitize_evidence(tenant_id, raw_evidence)

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception(lambda e: getattr(e, "retryable", False)),
        reraise=True,
    )
    async def find_call_graph(
        self, tenant_id: str, investigation_id: UUID, application_id: str, request: CallGraphRequest
    ) -> EvidenceQueryResult:
        with tracer.start_as_current_span("AsyncEvidenceGateway.find_call_graph"):
            await self._enforce_authorization(tenant_id, "GET_CODE")
            await self._validate_structured_output(request)
            validate_fn = getattr(self._query_safety_policy, "validate_symbol_request", None)
            if callable(validate_fn):
                # Reuse symbol validation for symbol field
                await validate_fn(tenant_id, request)
            provider = await self._provider_selector.get_code_intelligence_provider(
                tenant_id, application_id
            )
            if not provider:
                raise ExecutionError(f"No code provider available for app '{application_id}'")
            callers = await asyncio.wait_for(
                provider.find_callers(tenant_id, request.symbol, provider.profile),
                timeout=self._timeout_seconds,
            )
            return EvidenceQueryResult(
                items=[],
                total_count=len(callers),
                provider_metadata={"callers": [c.model_dump() for c in callers]},
            )

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception(lambda e: getattr(e, "retryable", False)),
        reraise=True,
    )
    async def get_code_history(
        self,
        tenant_id: str,
        investigation_id: UUID,
        application_id: str,
        request: CodeHistoryRequest,
    ) -> EvidenceQueryResult:
        with tracer.start_as_current_span("AsyncEvidenceGateway.get_code_history"):
            await self._enforce_authorization(tenant_id, "GET_CODE", request.file_path)
            await self._validate_structured_output(request)
            validate_fn = getattr(self._query_safety_policy, "validate_code_history_request", None)
            if callable(validate_fn):
                await validate_fn(tenant_id, request)
            provider = await self._provider_selector.get_code_provider(tenant_id, application_id)
            if not provider:
                raise ExecutionError(f"No code provider available for app '{application_id}'")
            items = await asyncio.wait_for(
                provider.get_code_history(
                    tenant_id, investigation_id, request.file_path, provider.profile
                ),
                timeout=self._timeout_seconds,
            )
            sanitized = [await self._sanitizer.sanitize_evidence(tenant_id, item) for item in items]
            deduped = await self._deduplicator.deduplicate(tenant_id, sanitized)
            return EvidenceQueryResult(items=deduped, total_count=len(deduped))

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception(lambda e: getattr(e, "retryable", False)),
        reraise=True,
    )
    async def correlate(
        self,
        tenant_id: str,
        investigation_id: UUID,
        application_id: str,
        root_evidence_ids: list[UUID],
        max_depth: int,
    ) -> EvidenceQueryResult:
        with tracer.start_as_current_span("AsyncEvidenceGateway.correlate"):
            await self._enforce_authorization(tenant_id, "CORRELATE")
            if not self._correlation_engine:
                raise ExecutionError("Correlation engine is not configured in AsyncEvidenceGateway")
            graph = await asyncio.wait_for(
                self._correlation_engine.expand_correlation(
                    tenant_id, application_id, root_evidence_ids, max_depth
                ),
                timeout=self._timeout_seconds,
            )
            return EvidenceQueryResult(
                items=[],
                total_count=len(graph.nodes),
                provider_metadata={"graph": graph.model_dump()},
            )
