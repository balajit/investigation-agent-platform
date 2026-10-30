# src/investigation_agent_platform/infrastructure/evidence/runtime/elastic.py
"""Thin Elasticsearch runtime adapter: orchestration over focused modules (Part 9).

Responsibility split (target architecture):

- ``cursor.py`` — signed, query-bound, expiring pagination codec;
- ``query.py`` — normalized query, fingerprint, safe body construction;
- ``validation.py`` — provider-boundary input and index-scope validation;
- ``projection.py`` — provider hit to domain evidence mapping;
- this module — client lifecycle, provider I/O, typed error mapping,
  per-record resilience, and the public adapter API.

Re-exports below preserve the pre-split import surface.
"""

import asyncio
import fnmatch
import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from elasticsearch import AsyncElasticsearch
from elasticsearch import exceptions as elastic_exceptions
from opentelemetry import trace

from investigation_agent_platform.domain.common.exceptions import (
    DomainException,
    EvidenceNotFoundException,
    ExecutionError,
    PlatformConfigurationError,
    ProviderNotConfiguredError,
    ProviderTimeoutException,
    ProviderUnavailableException,
)
from investigation_agent_platform.domain.evidence.models import Evidence
from investigation_agent_platform.domain.evidence.requests import (
    RuntimeEvidenceRequest,
)
from investigation_agent_platform.domain.observability.mapping import (
    GENERIC_ECS_SOURCE_ID,
    ObservabilitySourceMapping,
    generic_ecs_mapping,
)
from investigation_agent_platform.domain.profile.models import ApplicationProfile
from investigation_agent_platform.infrastructure.configuration.mapping_registry import (
    MappingProfileRegistry,
)
from investigation_agent_platform.infrastructure.evidence.runtime.cursor import (
    CURSOR_VERSION,
    ElasticCursorCodec,
)
from investigation_agent_platform.infrastructure.evidence.runtime.tenant_telemetry import (
    TenantTelemetryRouter,
    endpoint_index_allowed,
)
from investigation_agent_platform.infrastructure.evidence.runtime.projection import (
    ElasticEvidenceProjector,
    MalformedProviderRecord,
)
from investigation_agent_platform.infrastructure.evidence.runtime.query import (
    SORT_VERSION,
    NormalizedRuntimeQuery,
    build_query_body,
    normalize_request,
)
from investigation_agent_platform.infrastructure.evidence.runtime.validation import (
    ProviderCeilings,
    build_index_pattern,
    resolve_index_pattern,
)
from investigation_agent_platform.ports.evidence.gateway import EvidenceQueryResult
from investigation_agent_platform.ports.evidence.mapping import EvidenceMappingLookup

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)

__all__ = [
    "CURSOR_VERSION",
    "SORT_VERSION",
    "AsyncElasticAdapter",
    "ElasticAdapterSettings",
    "NormalizedRuntimeQuery",
    "build_index_pattern",
    "resolve_index_pattern",
]


@dataclass(frozen=True)
class ElasticAdapterSettings:
    """Provider-side policy for the Elastic runtime adapter (Part 9).

    The cursor signing key is required: the adapter refuses to construct
    without one, so pagination integrity can never rest on a hard-coded
    constant. Ceilings mirror ``EvidenceConfig``; operators may lower them,
    never raise them above the security maxima enforced at config load.

    Timeout trio (server < client < application cancellation):

    - ``query_timeout_seconds`` — Elasticsearch server-side ``timeout``;
    - ``request_timeout`` (constructor) — HTTP client timeout and the
      ``asyncio`` cancellation boundary wrapping each provider call.
      Construction fails closed unless strictly greater than the query
      timeout; otherwise the outer boundary would fire first and the query
      timeout could never take effect.
    """

    cursor_signing_key: bytes
    cursor_ttl_seconds: int = 3600
    max_hits: int = 200
    max_time_window_seconds: int = 7 * 24 * 3600
    max_keyword_terms: int = 20
    max_services: int = 10
    query_timeout_seconds: float = 25.0

    @classmethod
    def from_evidence_config(cls, config: Any) -> "ElasticAdapterSettings":
        key = config.cursor_signing_key.get_secret_value().encode("utf-8")
        if not key:
            raise PlatformConfigurationError(
                "Elastic adapter requires a cursor signing key "
                "(IAP_ELASTIC_CURSOR_SIGNING_KEY); refusing to construct."
            )
        return cls(
            cursor_signing_key=key,
            cursor_ttl_seconds=config.cursor_ttl_seconds,
            max_hits=config.elastic_max_hits,
            max_time_window_seconds=config.elastic_max_time_window_seconds,
            max_keyword_terms=config.elastic_max_keyword_terms,
            max_services=config.elastic_max_services,
            query_timeout_seconds=config.elastic_query_timeout_seconds,
        )


def _map_provider_exception(exc: Exception) -> DomainException:
    """Map provider failures to typed domain errors without leaking internals.

    Cancellation is never mapped (it propagates via ``except`` ordering at
    the call sites). Messages carry the failure category only — connection
    details, credentials, and query internals stay in server logs.
    """
    if isinstance(
        exc,
        (
            elastic_exceptions.AuthenticationException,
            elastic_exceptions.AuthorizationException,
        ),
    ):
        return ExecutionError(
            "Elasticsearch provider authentication failed; check provider configuration.",
            retryable=False,
        )
    if isinstance(exc, elastic_exceptions.ConnectionTimeout):
        return ProviderTimeoutException("Elasticsearch provider query timed out.")
    if isinstance(exc, (elastic_exceptions.ConnectionError, elastic_exceptions.SSLError)):
        return ProviderUnavailableException("Elasticsearch provider is unreachable.")
    if isinstance(exc, elastic_exceptions.NotFoundError):
        return ExecutionError("Elasticsearch provider reported a missing index.", retryable=False)
    if isinstance(exc, elastic_exceptions.ApiError):
        if exc.status_code in (429, 502, 503, 504):
            return ProviderUnavailableException(
                f"Elasticsearch provider returned status {exc.status_code}."
            )
        return ExecutionError(
            f"Elasticsearch provider rejected the request (status {exc.status_code}).",
            retryable=False,
        )
    return ExecutionError(f"Elasticsearch query failed: {type(exc).__name__}.")


class AsyncElasticAdapter:
    """Asynchronous Elasticsearch adapter for searching and mapping runtime logs and traces."""

    def __init__(
        self,
        client: AsyncElasticsearch | None,
        settings: ElasticAdapterSettings,
        provider_id: str = "elastic-primary",
        request_timeout: int = 30,
        profile: ApplicationProfile | None = None,
        mapping_registry: MappingProfileRegistry | None = None,
        tenant_router: TenantTelemetryRouter | None = None,
    ) -> None:
        if not settings.cursor_signing_key:
            raise PlatformConfigurationError(
                "Elastic adapter requires a cursor signing key; refusing to construct."
            )
        if not 0 < settings.query_timeout_seconds < request_timeout:
            raise PlatformConfigurationError(
                "Incoherent Elastic timeouts: the provider query timeout "
                f"({settings.query_timeout_seconds:g}s) must be below the request "
                f"boundary ({request_timeout}s)."
            )
        if client is None and tenant_router is None:
            raise PlatformConfigurationError(
                "Elastic adapter requires a global client or a tenant "
                "telemetry router; refusing to construct a provider that "
                "can serve nothing."
            )
        self._client = client
        self._settings = settings
        self._provider_id = provider_id
        self._request_timeout = request_timeout
        # Accepted for gateway port compatibility (`provider.profile`): the
        # profile does NOT currently scope index selection or field access —
        # scoping comes from the request plus tenant/environment validation.
        # Kept explicit (not silently dropped) as the future enforcement point.
        self._profile = profile
        self._codec = ElasticCursorCodec(
            signing_key=settings.cursor_signing_key,
            ttl_seconds=settings.cursor_ttl_seconds,
        )
        self._ceilings = ProviderCeilings(
            max_hits=settings.max_hits,
            max_time_window_seconds=settings.max_time_window_seconds,
            max_keyword_terms=settings.max_keyword_terms,
            max_services=settings.max_services,
        )
        self._projector = ElasticEvidenceProjector(provider_id=provider_id)
        self.profile = profile
        # Part 10: field-mapping resolution. None means generic-ECS-only
        # mode (pre-Part-10 behavior); named mappings require a registry.
        self._mapping_registry = mapping_registry
        # Tenant telemetry routing (DES-tenant-telemetry-config). None means
        # the pre-tenant single-deployment behavior: every request uses the
        # global client and the mapping-derived index pattern. A router
        # diverts tenants with configured endpoints to their home endpoint's
        # connection, index pattern, and timeout — selector, mappings, and
        # profile authorization are untouched.
        self._tenant_router = tenant_router

    def _identity_names(
        self, profile: ApplicationProfile | None, request: RuntimeEvidenceRequest
    ) -> list[str]:
        """Service identity for tenant-endpoint homing.

        Primary key is the investigated application (``profile.id``, which is
        the gateway's ``application_id``); the profile's declared service
        names act as aliases. Without a profile (no repo / no construction
        profile) the request's service filter is the only identity signal.
        """
        names: list[str] = []
        if profile is not None:
            app_id = getattr(profile, "id", None)
            if app_id:
                names.append(app_id)
            for service in getattr(profile, "services", None) or []:
                service_name = getattr(service, "name", None)
                if service_name:
                    names.append(service_name)
        else:
            names.extend(request.services)
        return names

    def _resolve_connection(
        self,
        tenant_id: str,
        profile: ApplicationProfile | None,
        request: RuntimeEvidenceRequest,
        mapping: ObservabilitySourceMapping,
    ) -> tuple[Any, str, float, str | None]:
        """Resolve (client, index pattern, timeout, endpoint name) for one request.

        Returns ``endpoint_name=None`` for the global path. Tenant-routed
        requests use the home endpoint's attested index pattern instead of
        the mapping-derived one (the mapping still governs the field
        contract and ceilings); the field contract for tenant-specific
        layouts arrives with mapping profiles from layout discovery.
        """
        router = self._tenant_router
        if router is None or not router.has_tenant(tenant_id):
            if self._client is None:
                logger.error(
                    "Elastic request for tenant without telemetry config "
                    "but no global deployment exists",
                    extra={"tenant_id": tenant_id},
                )
                raise ProviderNotConfiguredError(
                    f"No Elasticsearch deployment serves tenant '{tenant_id}'"
                )
            return (
                self._client,
                resolve_index_pattern(mapping, tenant_id, request.environment),
                float(self._request_timeout),
                None,
            )
        resolved = router.resolve(tenant_id, self._identity_names(profile, request))
        return (
            resolved.client,
            resolved.index_pattern,
            resolved.request_timeout,
            resolved.endpoint_name,
        )

    def _resolve_mapping(self, source_id: str | None) -> ObservabilitySourceMapping:
        """Resolve a stamped mapping id; fail closed before any provider I/O."""
        if source_id is None or source_id == GENERIC_ECS_SOURCE_ID:
            return generic_ecs_mapping()
        if self._mapping_registry is None:
            raise PlatformConfigurationError(
                "Request names an observability mapping but this adapter "
                "was built without a mapping registry."
            )
        return self._mapping_registry.resolve(source_id)

    @staticmethod
    def _extract_hits(response: Any) -> tuple[list[Any], Any]:
        """Validate the provider response envelope; malformed shapes fail the page."""
        if not isinstance(response, dict):
            raise ExecutionError(
                "Elasticsearch provider returned an unexpected response shape.",
                retryable=False,
            )
        hits_node = response.get("hits", {})
        if not isinstance(hits_node, dict):
            raise ExecutionError(
                "Elasticsearch provider returned an unexpected hits shape.", retryable=False
            )
        hits = hits_node.get("hits", [])
        if not isinstance(hits, list):
            raise ExecutionError(
                "Elasticsearch provider returned an unexpected record list.", retryable=False
            )
        return hits, hits_node.get("total")

    @staticmethod
    def _provider_ref_from_mapping(mapping: Evidence) -> tuple[str, str]:
        """Extract the (index, document ID) provider reference from a mapping row.

        The reference is the ``elasticsearch://{index}/{id}`` source URI this
        adapter wrote at search time. Anything else means this adapter cannot
        serve the record (different provider) or the row is corrupt.
        """
        source = mapping.source or ""
        if mapping.provider != "ELASTIC" or not source.startswith("elasticsearch://"):
            raise EvidenceNotFoundException(f"Evidence {mapping.evidence_id} not found")
        remainder = source[len("elasticsearch://") :]
        parts = remainder.split("/")
        if len(parts) != 2 or not parts[0] or not parts[1]:
            raise ExecutionError(
                f"Stored evidence reference for {mapping.evidence_id} is invalid.",
                retryable=False,
            )
        return parts[0], parts[1]

    async def _provider_search(
        self,
        *,
        index: str,
        body: dict[str, Any],
        tenant_id: str,
        correlation_id: str | None,
        client: Any | None = None,
        request_timeout: float | None = None,
        endpoint_name: str | None = None,
    ) -> Any:
        """Execute one bounded provider search with typed error mapping.

        Tenant-routed calls pass their home endpoint's client, timeout, and
        name; the global path uses the construction-time client/boundary.
        Required-evidence failures log at ERROR with the tenant, endpoint,
        and error code — never credentials — so a dead tenant deployment
        is unmistakable in server logs.
        """
        active_client = client if client is not None else self._client
        if active_client is None:
            raise ProviderNotConfiguredError(
                f"No Elasticsearch deployment serves tenant '{tenant_id}'"
            )
        boundary = float(request_timeout) if request_timeout is not None else float(self._request_timeout)
        try:
            response = await asyncio.wait_for(
                active_client.search(index=index, body=body),
                timeout=boundary,
            )
            # The async client returns an ObjectApiResponse wrapper, not a
            # plain dict — unwrap at the provider boundary so downstream
            # envelope validation sees the raw response shape. (Unit tests
            # inject raw dicts, which pass through untouched.)
            body_attr = getattr(response, "body", None)
            return body_attr if isinstance(body_attr, dict) else response
        except asyncio.CancelledError:
            # Cancellation is a control signal, never an execution failure.
            raise
        except TimeoutError as exc:
            raise ProviderTimeoutException(
                f"Elasticsearch query timed out after {boundary:g}s"
                + (f" on tenant endpoint '{endpoint_name}'" if endpoint_name else "")
            ) from exc
        except Exception as exc:
            mapped = _map_provider_exception(exc)
            logger.error(
                "Elasticsearch query failure",
                exc_info=exc,
                extra={
                    "context": {
                        "tenant_id": tenant_id,
                        "endpoint": endpoint_name,
                        "correlation_id": correlation_id,
                        "error_code": mapped.error_code,
                    }
                },
            )
            raise mapped from exc

    async def get_runtime_evidence(
        self,
        tenant_id: str,
        investigation_id: UUID,
        evidence_id: UUID,
        environment: str,
        mapping_lookup: EvidenceMappingLookup,
        correlation_id: str | None = None,
        mapping_source_id: str | None = None,
    ) -> Evidence:
        """Fetch one evidence record by platform evidence ID within scope.

        Resolution goes through the persisted identity mapping (platform UUID
        → provider reference); raw Elasticsearch IDs are never accepted.
        Unknown IDs, cross-investigation IDs, and non-Elastic records all
        surface as ``EvidenceNotFoundException`` with no scope disclosure.
        The returned provenance carries the caller's real investigation ID —
        never a synthetic fetch context.

        Part 10: the stored index is validated against the resolved mapping's
        index patterns (legacy mappings keep the historical tenant-prefix
        check); the fetch fingerprint binds the mapping id.
        """
        with tracer.start_as_current_span("AsyncElasticAdapter.get_runtime_evidence") as span:
            span.set_attribute("tenant_id", tenant_id)
            span.set_attribute("investigation_id", str(investigation_id))
            if correlation_id:
                span.set_attribute("correlation_id", correlation_id)
            # Validate scope inputs before any I/O.
            build_index_pattern(tenant_id, environment)
            mapping = self._resolve_mapping(mapping_source_id)

            mapping_row = await mapping_lookup.get_evidence(tenant_id, evidence_id)
            if (
                mapping_row is None
                or mapping_row.tenant_id != tenant_id
                or mapping_row.investigation_id != investigation_id
            ):
                raise EvidenceNotFoundException(f"Evidence {evidence_id} not found")
            index, document_id = self._provider_ref_from_mapping(mapping_row)
            router = self._tenant_router
            if router is not None and router.has_tenant(tenant_id):
                # Fetch path carries no service identity (no profile/request),
                # so the stored index itself selects the home endpoint: it
                # must fall under exactly one endpoint's attested pattern.
                # No match (or an ambiguous overlap) fails closed rather
                # than fetching from an unrelated deployment.
                candidates = [
                    handle
                    for handle in router.endpoints_for_tenant(tenant_id)
                    if endpoint_index_allowed(index, handle.index_pattern)
                ]
                if len(candidates) != 1:
                    logger.error(
                        "Tenant telemetry fetch resolves to no unique endpoint",
                        extra={
                            "tenant_id": tenant_id,
                            "candidates": sorted(h.endpoint_name for h in candidates),
                        },
                    )
                    raise EvidenceNotFoundException(f"Evidence {evidence_id} not found")
                handle = candidates[0]
                fetch_client: Any = handle.client
                fetch_timeout = handle.request_timeout
                fetch_endpoint: str | None = handle.endpoint_name
            else:
                if mapping.legacy_index_synthesis:
                    if not index.startswith(f"logs-{tenant_id}-"):
                        raise EvidenceNotFoundException(f"Evidence {evidence_id} not found")
                elif not any(fnmatch.fnmatchcase(index, pattern) for pattern in mapping.index_patterns):
                    raise EvidenceNotFoundException(f"Evidence {evidence_id} not found")
                fetch_client = None
                fetch_timeout = None
                fetch_endpoint = None

            response = await self._provider_search(
                index=index,
                body={"query": {"ids": {"values": [document_id]}}, "size": 1},
                tenant_id=tenant_id,
                correlation_id=correlation_id,
                client=fetch_client,
                request_timeout=fetch_timeout,
                endpoint_name=fetch_endpoint,
            )
            hits, _ = self._extract_hits(response)
            if not hits:
                raise EvidenceNotFoundException(f"Evidence {evidence_id} not found")
            fetch_descriptor = {
                "op": "GET",
                "tenant_id": tenant_id,
                "investigation_id": str(investigation_id),
                "index": index,
                "document_id": document_id,
                "mapping_source_id": mapping.source_id,
            }
            fetch_fingerprint = hashlib.sha256(
                json.dumps(fetch_descriptor, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            try:
                return self._projector.project(
                    hits[0],
                    tenant_id,
                    investigation_id,
                    operation="GET",
                    query_fingerprint=fetch_fingerprint,
                    mapping=mapping,
                )
            except MalformedProviderRecord as exc:
                logger.warning(
                    "Stored evidence record no longer maps cleanly",
                    extra={"context": {"reason": exc.reason}},
                )
                raise EvidenceNotFoundException(f"Evidence {evidence_id} not found") from exc

    async def search_runtime_evidence(
        self,
        tenant_id: str,
        investigation_id: UUID,
        request: RuntimeEvidenceRequest,
        profile: ApplicationProfile,
        correlation_id: str | None = None,
    ) -> EvidenceQueryResult:
        with tracer.start_as_current_span("AsyncElasticAdapter.search_runtime_evidence") as span:
            span.set_attribute("tenant_id", tenant_id)
            span.set_attribute("investigation_id", str(investigation_id))
            if correlation_id:
                span.set_attribute("correlation_id", correlation_id)

            # Derive application context from profile provider if available; fallback to tenant-scoped pattern
            mapping = self._resolve_mapping(request.mapping_source_id)
            client, index_pattern, request_timeout, endpoint_name = self._resolve_connection(
                tenant_id, profile, request, mapping
            )
            normalized = normalize_request(
                tenant_id, investigation_id, request, self._ceilings, mapping
            )
            query_fingerprint = normalized.fingerprint()

            search_after: list[Any] | None = None
            if request.cursor is not None:
                # Invalid cursors raise: never silently restart from page one.
                search_after = self._codec.decode(
                    cursor=request.cursor,
                    expected_query_fingerprint=query_fingerprint,
                    tenant_id=tenant_id,
                    investigation_id=investigation_id,
                )
            query_body = build_query_body(
                normalized, mapping, search_after, self._settings.query_timeout_seconds
            )

            response = await self._provider_search(
                index=index_pattern,
                body=query_body,
                tenant_id=tenant_id,
                correlation_id=correlation_id,
                client=client,
                request_timeout=request_timeout,
                endpoint_name=endpoint_name,
            )
            hits, total = self._extract_hits(response)

            # One malformed record must not fail the page — but it must not
            # disappear silently either: skipped records surface as an
            # explicit partial-result signal with per-record reasons.
            items: list[Evidence] = []
            record_errors: list[dict[str, Any]] = []
            for position, hit in enumerate(hits):
                try:
                    items.append(
                        self._projector.project(
                            hit,
                            tenant_id,
                            investigation_id,
                            operation="SEARCH",
                            query_fingerprint=query_fingerprint,
                            mapping=mapping,
                        )
                    )
                except MalformedProviderRecord as exc:
                    logger.warning(
                        "Skipping malformed provider record",
                        extra={"context": {"position": position, "reason": exc.reason}},
                    )
                    record_errors.append(
                        {
                            "code": "MALFORMED_PROVIDER_RECORD",
                            "position": position,
                            "reason": exc.reason,
                        }
                    )

            # Best-effort pagination over a changing dataset (`search_after`
            # without a snapshot): new arrivals/deletions between pages can
            # repeat or skip records. Documented contract, not a snapshot —
            # point-in-time was evaluated and deferred (operational cost).
            has_more = len(hits) == normalized.effective_limit
            next_cursor: str | None = None
            if has_more:
                last_hit = hits[-1]
                sort_values: Any = last_hit.get("sort") if isinstance(last_hit, dict) else None
                # Fail loud, never silent: a full page without issuable sort
                # state would otherwise loop page one forever (or truncate
                # silently). Corrupt provider sort data is a loud provider
                # failure, not an agent pagination error. The codec enforces
                # exact structural arity; anything else raises below.
                try:
                    next_cursor = self._codec.encode(
                        sort_values=sort_values,
                        tenant_id=tenant_id,
                        investigation_id=investigation_id,
                        query_fingerprint=query_fingerprint,
                    )
                except ValueError as exc:
                    raise ExecutionError(
                        "Elasticsearch provider returned unpaginable sort state.",
                        retryable=False,
                    ) from exc

            # `total_count` is a bounded provider total (`track_total_hits`
            # caps counting at the page size), not an exact census. Consumers
            # must treat it as "at least this many within the bound".
            total_count = (
                total.get("value")
                if isinstance(total, dict)
                else (total if isinstance(total, int) else None)
            )

            return EvidenceQueryResult(
                items=items,
                cursor=next_cursor,
                has_more=has_more,
                total_count=total_count if isinstance(total_count, int) else len(items),
                execution_metadata={
                    "execution_time_ms": 0.0,
                    "partial_result": bool(record_errors),
                    "errors": record_errors,
                    "tokens_consumed": 0,
                    # Home endpoint that served this page (None = global
                    # deployment). Lets operators attribute results — and
                    # failures — to the deployment that produced them.
                    "endpoint": endpoint_name,
                },
            )
