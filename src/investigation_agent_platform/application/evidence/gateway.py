# src/investigation_agent_platform/application/evidence/gateway.py
import asyncio
import logging
import os
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from opentelemetry import trace
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from investigation_agent_platform.domain.common.exceptions import (
    ExecutionError,
    SecurityPolicyViolationException,
    UnsupportedCapabilityError,
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
from investigation_agent_platform.infrastructure.observability.tracing import traced
from investigation_agent_platform.ports.correlation.engine import CorrelationExpander
from investigation_agent_platform.ports.evidence.api_probe import (
    ApiProbeRequest,
    ApiProbeResult,
)
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

# P0-3: platform log rows embed investigation/workflow IDs
# (wf-investigation-<uuid>). A row naming a DIFFERENT investigation than the
# one being run is cross-run leakage, never evidence — drop it. Rows naming
# the current investigation (own retry lines) or no UUID at all pass.
_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


def references_foreign_investigation(item: Any, investigation_id: Any) -> bool:
    """Whether a log evidence row names another investigation (pure).

    Scans title/summary/content_snippet/source for UUIDs; returns True when
    any found UUID differs from the current investigation ID. Pure and
    deterministic — unit-testable without providers.
    """
    try:
        current = str(investigation_id or "").lower()
        texts = []
        for field in ("title", "summary", "content_snippet", "source"):
            try:
                value = getattr(item, field, "")
            except Exception:
                continue
            if value:
                texts.append(str(value))
        if not texts:
            return False
        for match in _UUID_RE.finditer(" ".join(texts)):
            if match.group(0).lower() != current:
                return True
        return False
    except Exception:
        return False

if TYPE_CHECKING:
    from investigation_agent_platform.domain.topology.models import (
        DomainAttributionResult,
        StackFrameLocation,
        StaticOwnershipResult,
    )


class ContextCompressor:
    """Compresses/truncates telemetry and log items to fit within token budgets."""

    def __init__(self, max_token_budget: int = 4000) -> None:
        self.max_token_budget = max_token_budget

    def truncate_log_stream(self, raw_logs: list[str], max_tokens: int) -> str:
        if not raw_logs:
            return ""
        formatted = "\n".join(raw_logs)
        # Approximate 4 characters per token heuristic
        if len(formatted) / 4 <= max_tokens:
            return formatted

        head_count = max(1, int(len(raw_logs) * 0.2))
        tail_count = max(1, int(len(raw_logs) * 0.6))

        head = raw_logs[:head_count]
        tail = raw_logs[-tail_count:]

        truncated_msg = (
            f"\n... [TRUNCATED {len(raw_logs) - (head_count + tail_count)} LOG LINES] ...\n"
        )
        return "\n".join(head) + truncated_msg + "\n".join(tail)




def _dedupe_extra(items: Any) -> dict[str, Any]:
    """Survivor count only."""
    try:
        if isinstance(items, list):
            return {"iap.items_count": len(items)}
        return {}
    except Exception:
        return {}


def _query_dialect(profile: Any, template_id: str) -> str | None:
    """SQL standard of the template's datasource (oracle, postgres, ...).

    #65: namespaces are ``<datasource_id>.<name>`` — the datasource kind is
    the query dialect. Bare/unknown names and ad-hoc SQL (no template) yield
    None, preserving today's fan-out-everywhere behavior for those shapes.
    """
    try:
        template_id = str(template_id or "")
        datasources = getattr(profile, "datasources", []) or []
        if "." in template_id:
            ds_id, _ = template_id.split(".", 1)
            for ds in datasources:
                if getattr(ds, "id", "") == ds_id:
                    kind = getattr(ds, "kind", None)
                    return str(getattr(kind, "value", kind) or "").lower() or None
            return None
        for ds in datasources:
            if template_id in (getattr(ds, "query_templates", {}) or {}):
                kind = getattr(ds, "kind", None)
                return str(getattr(kind, "value", kind) or "").lower() or None
    except Exception:
        pass
    return None


def _gateway_extra(result: Any) -> dict[str, Any]:
    """Result counts + per-source outcome tallies (never item contents)."""
    try:
        extra: dict[str, Any] = {}
        total = getattr(result, "total_count", None)
        if isinstance(total, int):
            extra["iap.items_count"] = total
        else:
            items = getattr(result, "items", None)
            if isinstance(items, list):
                extra["iap.items_count"] = len(items)
        metadata = getattr(result, "provider_metadata", None) or {}
        per_source = metadata.get("per_source") if isinstance(metadata, dict) else None
        if isinstance(per_source, list):
            tallies: dict[str, int] = {}
            for entry in per_source:
                if isinstance(entry, dict):
                    status = str(entry.get("status", "UNKNOWN"))
                    tallies[status] = tallies.get(status, 0) + 1
            for status, count in tallies.items():
                extra[f"iap.sources_{status.lower()}"] = count
        return extra
    except Exception:
        return {}


def _resolve_fanout_concurrency(explicit: int | None) -> int | None:
    """R2: effective MCP fan-out parallelism.

    Explicit ctor value wins; otherwise `IAP_MCP_FANOUT_CONCURRENCY` when it
    parses as an int. None (or <= 0) = unbounded — one task per candidate.
    1 restores the legacy serial loop (ops kill-switch).
    """
    if explicit is not None:
        return explicit if explicit > 0 else None
    raw = (os.environ.get("IAP_MCP_FANOUT_CONCURRENCY", "") or "").strip()
    if not raw:
        return None
    try:
        parsed = int(raw)
    except ValueError:
        return None
    return parsed if parsed > 0 else None


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

    @traced("gateway", component="deduplicator", extra_from=_dedupe_extra)
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
        compressor: ContextCompressor | None = None,
        max_tokens_per_stream: int = 3000,
        repository_registry: Any | None = None,
        failure_attribution_service: Any | None = None,
        profile_repo: Any | None = None,
        # R2 concurrent fan-out: max parallel MCP fan-out queries. None (or
        # <= 0) = unbounded (one task per candidate); 1 restores the legacy
        # serial loop. Env override `IAP_MCP_FANOUT_CONCURRENCY` wins when set.
        fanout_concurrency: int | None = None,
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
        self._compressor = compressor or ContextCompressor()
        self._max_tokens_per_stream = max_tokens_per_stream
        # Layer 3 topology (prompt1_v1.md): both optional so the gateway
        # remains fully functional without a topology deployment. Missing
        # wiring surfaces as an explicit TOPOLOGY_NOT_CONFIGURED error rather
        # than a silent no-op.
        self._repository_registry = repository_registry
        self._failure_attribution_service = failure_attribution_service
        self._profile_repo = profile_repo
        self._fanout_concurrency = _resolve_fanout_concurrency(fanout_concurrency)

    async def _effective_profile(
        self, provider: Any, tenant_id: str, application_id: str
    ) -> Any | None:
        """Profile for provider calls: attached provider profile first.

        Static providers (Elastic) carry their construction-time profile;
        per-app providers (file-log, registry-built state) resolve sources
        from the live revision so declarations stay single-sourced.
        Fail-open (None) — callers treat absence as "no declared sources".
        """
        profile = getattr(provider, "profile", None)
        if profile is not None:
            return profile
        repo = self._profile_repo
        if repo is None:
            return None
        try:
            return await repo.get_by_application_id(tenant_id, application_id)
        except Exception:
            return None

    @staticmethod
    def _require_revision_capability(
        provider: Any, revision: str | None, require_revision: bool
    ) -> None:
        """Fail closed when a pinned revision is required but unsupported.

        A provider that scans a live checkout cannot describe another
        revision. Returning its rows would silently attribute HEAD to the
        requested revision, so a load-bearing revision is a typed
        capability error, not an empty result.
        """
        requested = str(revision or "").strip()
        if not require_revision or requested in ("", "HEAD"):
            return
        if not bool(getattr(provider, "revision_supported", False)):
            provider_id = getattr(provider, "provider_id", type(provider).__name__)
            raise UnsupportedCapabilityError(
                f"Provider '{provider_id}' cannot honor revision '{requested}'",
                capability="revision",
                details={
                    "provider_id": str(provider_id),
                    "requested_revision": requested,
                },
            )

    async def _enforce_authorization(
        self,
        tenant_id: str,
        capability: str,
        resource: str = "",
        application_id: str | None = None,
    ) -> None:
        # The profile authorizer scopes by application: without the
        # application_id it fail-closes (no profile → deny). Every public
        # gateway method holds application_id and must thread it through —
        # omitting it denies all provider calls unconditionally.
        context = {"application_id": application_id} if application_id else {}
        if self._authorizer is not None:
            authorized = await self._authorizer.authorize_action(
                tenant_id, capability, resource, context
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
    @traced("gateway", extra_from=_gateway_extra)
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
            await self._enforce_authorization(tenant_id, "SEARCH_LOGS", application_id=application_id)
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
                    tenant_id,
                    investigation_id,
                    request,
                    await self._effective_profile(provider, tenant_id, application_id),
                ),
                timeout=self._timeout_seconds,
            )

            sanitized_items = [
                await self._sanitizer.sanitize_evidence(tenant_id, item) for item in result.items
            ]
            deduped = await self._deduplicator.deduplicate(tenant_id, sanitized_items)

            if not deduped:
                # Generic file-log fallback: when the default deployment has
                # no rows for this context (e.g. no Elastic integration),
                # consult file_log datasources before reporting empty. The
                # adapter resolves sources from the live profile revision.
                find_fallback = getattr(
                    self._provider_selector, "find_runtime_provider", None
                )
                fallback = find_fallback("file-log") if callable(find_fallback) else None
                if fallback is not None and fallback is not provider:
                    try:
                        fallback_result = await asyncio.wait_for(
                            fallback.search_runtime_evidence(
                                tenant_id,
                                investigation_id,
                                request,
                                await self._effective_profile(
                                    fallback, tenant_id, application_id
                                ),
                            ),
                            timeout=self._timeout_seconds,
                        )
                        fallback_items = [
                            await self._sanitizer.sanitize_evidence(tenant_id, item)
                            for item in fallback_result.items
                        ]
                        deduped = await self._deduplicator.deduplicate(
                            tenant_id, fallback_items
                        )
                        if deduped:
                            result = fallback_result
                    except Exception as exc:
                        logger.warning(
                            "File-log fallback failed; keeping primary result",
                            extra={"error": str(exc)},
                        )

            # P0-3: drop cross-run leakage — log rows naming a different
            # investigation than the one being run (own rows pass through).
            kept = [
                item
                for item in deduped
                if not references_foreign_investigation(item, investigation_id)
            ]
            dropped = len(deduped) - len(kept)
            if dropped:
                logger.info(
                    "Dropped %d log row(s) referencing foreign investigations",
                    dropped,
                    extra={
                        "tenant_id": tenant_id,
                        "investigation_id": str(investigation_id),
                        "event": "foreign log rows dropped",
                        "dropped": dropped,
                    },
                )
            deduped = kept

            # Apply token budget compression to log streams in evidence payload
            for item in deduped:
                if hasattr(item, "payload") and isinstance(item.payload, dict):
                    logs = item.payload.get("logs")
                    if isinstance(logs, list):
                        item.payload["logs"] = self._compressor.truncate_log_stream(
                            logs, self._max_tokens_per_stream
                        )
                        item.payload["is_compressed"] = True

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
    @traced("gateway", extra_from=_gateway_extra)
    async def get_application_state(
        self,
        tenant_id: str,
        investigation_id: UUID,
        application_id: str,
        request: ApplicationStateRequest,
    ) -> EvidenceQueryResult:
        with tracer.start_as_current_span("AsyncEvidenceGateway.get_application_state"):
            await self._enforce_authorization(tenant_id, "QUERY_STATE", application_id=application_id)
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
            profile = await self._effective_profile(provider, tenant_id, application_id)
            if profile is None:
                raise ExecutionError(
                    f"No application profile for '{application_id}'; cannot scope state access"
                )

            result = await asyncio.wait_for(
                provider.get_application_state(
                    tenant_id, investigation_id, request, profile
                ),
                timeout=self._timeout_seconds,
            )
            sanitized_items = [
                await self._sanitizer.sanitize_evidence(tenant_id, item) for item in result.items
            ]
            deduped = await self._deduplicator.deduplicate(tenant_id, sanitized_items)
            # R2: fan out across every other tenant-authorized MCP state
            # provider concurrently (an app with Oracle + Postgres gets both
            # interrogated, not just the resolved primary). Per-source
            # degrade: one source failing never fails the action; dialect
            # mismatches land here. `asyncio.gather` preserves candidate
            # order, so `per_source` stays deterministic.
            per_source: list[dict[str, Any]] = [
                {"source": "primary", "status": "SUCCEEDED", "items": len(deduped)}
            ]
            extra_items: list[Any] = []
            try:
                candidates = self._provider_selector.list_state_providers()
            except Exception:
                candidates = []

            async def _fanout_one(
                key: str, other: Any, semaphore: asyncio.Semaphore | None
            ) -> tuple[str, str, list[Any]]:
                # Manual span: decorator cannot name dynamic per-source spans.
                # Name carries the source key (low-cardinality config value).
                # Degrade contract preserved: exceptions → DEGRADED, never raise.
                from opentelemetry.trace import StatusCode

                from investigation_agent_platform.infrastructure.observability.tracing import (
                    span_attributes,
                )

                with tracer.start_as_current_span(f"provider.fanout.{key}") as leg:
                    try:
                        for attr_key, attr_value in span_attributes(
                            tenant_id=tenant_id,
                            investigation_id=str(investigation_id),
                            component="fanout",
                            **{"iap.source": key},
                        ).items():
                            if attr_value is not None:
                                leg.set_attribute(attr_key, attr_value)
                        if semaphore is not None:
                            async with semaphore:
                                outcome = await _query_other(key, other)
                        else:
                            outcome = await _query_other(key, other)
                        leg.set_attribute("iap.outcome", "ok")
                        leg.set_attribute("iap.items_count", len(outcome[2]))
                        leg.set_status(StatusCode.OK)
                        return outcome
                    except Exception as exc:
                        logger.warning(
                            "Fan-out state query degraded for source",
                            extra={"source": str(key), "error": str(exc)},
                        )
                        leg.set_attribute("iap.outcome", "degraded")
                        leg.set_attribute("iap.error_type", type(exc).__name__)
                        leg.set_status(StatusCode.OK)
                        return str(key), "DEGRADED", []

            async def _query_other(key: str, other: Any) -> tuple[str, str, list[Any]]:
                other_result = await asyncio.wait_for(
                    other.get_application_state(
                        tenant_id, investigation_id, request, profile
                    ),
                    timeout=self._timeout_seconds,
                )
                return str(key), "SUCCEEDED", list(other_result.items or [])

            eligible: list[tuple[str, Any]] = []
            # #65: each store speaks its own SQL standard — fan out only to
            # dialect-matching servers. Query dialect comes from the template
            # namespace's datasource kind; unknown (ad-hoc SQL) fans out as
            # before; dialect-less servers match everything (backward compat).
            query_dialect = _query_dialect(profile, request.template_id)
            for key, other in candidates:
                if "-mcp-" not in str(key):
                    continue
                if other is provider:
                    continue
                serves = getattr(other, "serves_tenant", None)
                try:
                    if callable(serves) and not serves(tenant_id):
                        per_source.append(
                            {"source": str(key), "status": "SKIPPED", "items": 0}
                        )
                        continue
                except Exception as exc:
                    logger.warning(
                        "Fan-out state query degraded for source",
                        extra={"source": str(key), "error": str(exc)},
                    )
                    per_source.append(
                        {"source": str(key), "status": "DEGRADED", "items": 0}
                    )
                    continue
                server_dialect = getattr(other, "dialect", None)
                if query_dialect and server_dialect and server_dialect != query_dialect:
                    per_source.append(
                        {
                            "source": str(key),
                            "status": "SKIPPED",
                            "items": 0,
                            "reason": f"dialect {server_dialect} != query {query_dialect}",
                        }
                    )
                    continue
                eligible.append((str(key), other))
            if eligible:
                semaphore = (
                    asyncio.Semaphore(self._fanout_concurrency)
                    if self._fanout_concurrency is not None
                    else None
                )
                # gather (not wait_for): each leg already carries its own
                # per-provider timeout; a global deadline would change the
                # documented worst case (deferred follow-up, see R2 plan).
                outcomes = await asyncio.gather(
                    *(_fanout_one(key, other, semaphore) for key, other in eligible)
                )
                for source_key, status, items in outcomes:
                    extra_items.extend(items)
                    per_source.append(
                        {"source": source_key, "status": status, "items": len(items)}
                    )
            if not per_source:
                per_source.append(
                    {
                        "source": "state-mcp-fanout",
                        "status": "DEGRADED",
                        "items": 0,
                        "reason": "no-state-mcp-servers-registered",
                    }
                )
            if extra_items:
                sanitized_extra = [
                    await self._sanitizer.sanitize_evidence(tenant_id, item)
                    for item in extra_items
                ]
                # P3-2: dedup ONLY the fan-out extras — re-running dedup over
                # the already-marked primary items would drop everything.
                deduped_extra = await self._deduplicator.deduplicate(
                    tenant_id, sanitized_extra
                )
                deduped = list(deduped) + list(deduped_extra)
            # D4: template assertions evaluated in code over parsed row
            # payloads. FAILs bank as DIVERGENCE rows (absence is evidence);
            # unparseable rows skip. Fail-open: never breaks the query.
            try:
                assertion_rows = await self._evaluate_template_assertions(
                    profile,
                    request.template_id,
                    tenant_id,
                    investigation_id,
                    deduped,
                )
                deduped = list(deduped) + list(assertion_rows)
            except Exception as exc:
                logger.warning(
                    "Template assertion evaluation failed; keeping rows",
                    extra={"error": str(exc)},
                )
            return EvidenceQueryResult(
                items=deduped,
                cursor=result.cursor,
                total_count=len(deduped),
                has_more=result.has_more,
                provider_metadata={"per_source": per_source},
            )

    async def _evaluate_template_assertions(
        self,
        profile: Any,
        template_id: str,
        tenant_id: str,
        investigation_id: Any,
        items: list[Any],
    ) -> list[Any]:
        """Evaluate QueryTemplate.assertions over banked rows (fail-open).

        D4: parses each row's raw payload as JSON (best-effort); FAILs
        shape into sanitized DIVERGENCE rows citable by silent-failure
        claims. No assertions declared → []. Never raises.
        """
        try:
            from investigation_agent_platform.application.evidence.divergence import (
                evaluate_assertions,
                shape_divergence_evidence,
            )

            index = profile.query_template_index() if profile is not None else {}
            template = index.get(str(template_id or ""), None)
            assertions = list(getattr(template, "assertions", None) or [])
            if not assertions or not items:
                return []
            import json as _json

            parsed_rows: list[dict[str, Any]] = []
            for item in items:
                try:
                    attributes = getattr(item, "attributes", None) or {}
                    raw = attributes.get("raw_payload", None)
                    if raw is None:
                        raw = getattr(item, "content_snippet", "") or ""
                    if isinstance(raw, dict):
                        parsed_rows.append(raw)
                    elif isinstance(raw, str):
                        parsed = _json.loads(raw)
                        parsed_rows.append(parsed if isinstance(parsed, dict) else {})
                    else:
                        parsed_rows.append({})
                except Exception:
                    parsed_rows.append({})
            fails = evaluate_assertions(parsed_rows, assertions)
            if not fails:
                return []
            rows = []
            for fail in fails[:20]:
                try:
                    rows.append(
                        shape_divergence_evidence(
                            tenant_id=tenant_id,
                            investigation_id=investigation_id,
                            source_ref=f"template:{template_id}",
                            endpoint=str(template_id or ""),
                            divergence={
                                "path": str(fail.get("field", "")),
                                "kind": f"assertion_{fail.get('op', '')}",
                                "expected": fail.get("expected", None),
                                "actual": fail.get("observed", None),
                            },
                        )
                    )
                except Exception:
                    continue
            sanitized = []
            for row in rows:
                try:
                    sanitized.append(await self._sanitizer.sanitize_evidence(tenant_id, row))
                except Exception:
                    continue
            return sanitized
        except Exception:
            return []

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception(lambda e: getattr(e, "retryable", False)),
        reraise=True,
    )
    @traced("gateway", extra_from=_gateway_extra)
    async def search_code(
        self,
        tenant_id: str,
        investigation_id: UUID,
        application_id: str,
        request: CodeSearchRequest,
    ) -> EvidenceQueryResult:
        with tracer.start_as_current_span("AsyncEvidenceGateway.search_code"):
            await self._enforce_authorization(tenant_id, "SEARCH_CODE", application_id=application_id)
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
            profile = await self._effective_profile(provider, tenant_id, application_id)
            if profile is None:
                raise ExecutionError(
                    f"No application profile for '{application_id}'; cannot scope code access"
                )
            self._require_revision_capability(provider, request.revision, request.require_revision)
            raw_items = await asyncio.wait_for(
                provider.search_code(
                    tenant_id,
                    request.query,
                    profile,
                    investigation_id=investigation_id,
                    language=request.language,
                    path_prefix=request.path_prefix,
                    limit=request.limit,
                    revision=request.revision,
                ),
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
    @traced("gateway", extra_from=_gateway_extra)
    async def find_symbol(
        self, tenant_id: str, investigation_id: UUID, application_id: str, request: SymbolRequest
    ) -> EvidenceQueryResult:
        with tracer.start_as_current_span("AsyncEvidenceGateway.find_symbol"):
            await self._enforce_authorization(tenant_id, "FIND_SYMBOL", application_id=application_id)
            await self._validate_structured_output(request)
            validate_fn = getattr(self._query_safety_policy, "validate_symbol_request", None)
            if callable(validate_fn):
                await validate_fn(tenant_id, request)
            provider = await self._provider_selector.get_code_intelligence_provider(
                tenant_id, application_id
            )
            if not provider:
                raise ExecutionError(f"No code provider available for app '{application_id}'")
            profile = await self._effective_profile(provider, tenant_id, application_id)
            if profile is None:
                raise ExecutionError(
                    f"No application profile for '{application_id}'; cannot scope code access"
                )
            self._require_revision_capability(provider, request.revision, request.require_revision)
            symbols = await asyncio.wait_for(
                provider.find_symbol(
                    tenant_id,
                    request.symbol_name,
                    profile,
                    investigation_id=investigation_id,
                    revision=request.revision,
                ),
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
    @traced("gateway", extra_from=_gateway_extra)
    async def get_source(
        self, tenant_id: str, investigation_id: UUID, application_id: str, request: SourceRequest
    ) -> Evidence:
        with tracer.start_as_current_span("AsyncEvidenceGateway.get_source"):
            await self._enforce_authorization(tenant_id, "GET_CODE", request.file_path, application_id=application_id)
            await self._validate_structured_output(request)
            validate_fn = getattr(self._query_safety_policy, "validate_source_request", None)
            if callable(validate_fn):
                await validate_fn(tenant_id, request)
            provider = await self._provider_selector.get_code_provider(tenant_id, application_id)
            if not provider:
                raise ExecutionError(f"No code provider available for app '{application_id}'")
            profile = await self._effective_profile(provider, tenant_id, application_id)
            if profile is None:
                raise ExecutionError(
                    f"No application profile for '{application_id}'; cannot scope code access"
                )
            raw_evidence = await asyncio.wait_for(
                provider.get_source(
                    tenant_id,
                    investigation_id,
                    request.file_path,
                    profile,
                    revision=request.revision,
                    start_line=request.start_line,
                    end_line=request.end_line,
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
    @traced("gateway", extra_from=_gateway_extra)
    async def find_call_graph(
        self, tenant_id: str, investigation_id: UUID, application_id: str, request: CallGraphRequest
    ) -> EvidenceQueryResult:
        with tracer.start_as_current_span("AsyncEvidenceGateway.find_call_graph"):
            await self._enforce_authorization(tenant_id, "FIND_CALL_GRAPH", application_id=application_id)
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
            profile = await self._effective_profile(provider, tenant_id, application_id)
            if profile is None:
                raise ExecutionError(
                    f"No application profile for '{application_id}'; cannot scope code access"
                )
            self._require_revision_capability(provider, request.revision, request.require_revision)
            # P1-2: honor direction/depth — INBOUND walks callers,
            # OUTBOUND walks callees, BOTH walks each side, up to depth hops.
            direction = str(request.direction or "BOTH").upper()
            callers: list[Any] = []
            callees: list[Any] = []
            if direction in ("INBOUND", "BOTH"):
                callers = await asyncio.wait_for(
                    provider.find_callers(
                        tenant_id,
                        request.symbol,
                        profile,
                        investigation_id=investigation_id,
                        revision=request.revision,
                    ),
                    timeout=self._timeout_seconds,
                )
            if direction in ("OUTBOUND", "BOTH"):
                callees = await asyncio.wait_for(
                    provider.find_callees(
                        tenant_id,
                        request.symbol,
                        profile,
                        investigation_id=investigation_id,
                        revision=request.revision,
                    ),
                    timeout=self._timeout_seconds,
                )
            hops: list[dict[str, Any]] = []
            for hop in range(1, request.depth):
                # Depth > 1: expand one more hop from the previous frontier
                # on each requested side (bounded: first 10 nodes per side).
                frontier = [
                    (n.caller_symbol if direction == "INBOUND" else n.callee_symbol)
                    for n in (callers if direction == "INBOUND" else callees)[:10]
                ]
                grown: list[Any] = []
                for symbol in frontier:
                    fn = (
                        provider.find_callers
                        if direction == "INBOUND"
                        else provider.find_callees
                    )
                    try:
                        grown.extend(
                            await asyncio.wait_for(
                                fn(
                                    tenant_id,
                                    symbol,
                                    profile,
                                    investigation_id=investigation_id,
                                    revision=request.revision,
                                ),
                                timeout=self._timeout_seconds,
                            )
                        )
                    except Exception:
                        continue
                if not grown:
                    break
                hops.extend([g.model_dump() for g in grown])
                if direction == "INBOUND":
                    callers = list(callers) + grown
                else:
                    callees = list(callees) + grown
            return EvidenceQueryResult(
                items=[],
                total_count=len(callers) + len(callees),
                provider_metadata={
                    "direction": direction,
                    "depth": request.depth,
                    "callers": [c.model_dump() for c in callers],
                    "callees": [c.model_dump() for c in callees],
                    "extended_hops": hops,
                },
            )

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception(lambda e: getattr(e, "retryable", False)),
        reraise=True,
    )
    @traced("gateway", extra_from=_gateway_extra)
    async def get_code_history(
        self,
        tenant_id: str,
        investigation_id: UUID,
        application_id: str,
        request: CodeHistoryRequest,
    ) -> EvidenceQueryResult:
        with tracer.start_as_current_span("AsyncEvidenceGateway.get_code_history"):
            await self._enforce_authorization(tenant_id, "GET_CODE_HISTORY", request.file_path, application_id=application_id)
            await self._validate_structured_output(request)
            validate_fn = getattr(self._query_safety_policy, "validate_code_history_request", None)
            if callable(validate_fn):
                await validate_fn(tenant_id, request)
            provider = await self._provider_selector.get_code_provider(tenant_id, application_id)
            if not provider:
                raise ExecutionError(f"No code provider available for app '{application_id}'")
            profile = await self._effective_profile(provider, tenant_id, application_id)
            if profile is None:
                raise ExecutionError(
                    f"No application profile for '{application_id}'; cannot scope code access"
                )
            items = await asyncio.wait_for(
                provider.get_code_history(
                    tenant_id, investigation_id, request.file_path, profile
                ),
                timeout=self._timeout_seconds,
            )
            sanitized = [await self._sanitizer.sanitize_evidence(tenant_id, item) for item in items]
            deduped = await self._deduplicator.deduplicate(tenant_id, sanitized)
            return EvidenceQueryResult(items=deduped, total_count=len(deduped))

    @traced("gateway", extra_from=_gateway_extra)
    async def probe_api(
        self,
        tenant_id: str,
        investigation_id: UUID,
        application_id: str,
        request: ApiProbeRequest,
    ) -> EvidenceQueryResult:
        """Sandboxed live probe (P2-2): endpoint allowlist enforced in the
        adapter; gateway adds authorization + sanitization + dedup like every
        other evidence path."""
        from investigation_agent_platform.infrastructure.evidence.api.probe_adapter import (
            HttpxApiProbeAdapter,
        )

        with tracer.start_as_current_span("AsyncEvidenceGateway.probe_api"):
            await self._enforce_authorization(
                tenant_id, "PROBE_API", application_id=application_id
            )
            await self._validate_structured_output(request)
            if self._profile_repo is None:
                raise ExecutionError(
                    f"No application profile store; cannot scope probe for '{application_id}'"
                )
            adapter = HttpxApiProbeAdapter(profile_repo=self._profile_repo)
            result = await asyncio.wait_for(
                adapter.probe(tenant_id, application_id, request),
                timeout=self._timeout_seconds,
            )
            item = self._probe_result_to_evidence(
                tenant_id, investigation_id, application_id, result
            )
            sanitized = await self._sanitizer.sanitize_evidence(tenant_id, item)
            deduped = await self._deduplicator.deduplicate(tenant_id, [sanitized])
            return EvidenceQueryResult(items=deduped, total_count=len(deduped))

    @staticmethod
    def _probe_result_to_evidence(
        tenant_id: str,
        investigation_id: UUID,
        application_id: str,
        result: ApiProbeResult,
    ) -> Evidence:
        import hashlib as _hashlib

        from investigation_agent_platform.domain.evidence.models import (
            Evidence,
            EvidenceType,
        )
        from investigation_agent_platform.domain.provenance.models import (
            EvidenceFreshness,
            EvidenceProvenance,
            QueryFingerprint,
            SourceLocation,
        )

        now = datetime.now(UTC)
        assert isinstance(result, ApiProbeResult)
        fp = _hashlib.sha256(
            f"probe:{tenant_id}:{application_id}:{result.endpoint_id}:{result.resolved_url}".encode()
        ).hexdigest()
        return Evidence(
            tenant_id=tenant_id,
            investigation_id=investigation_id,
            evidence_type=EvidenceType.API_PROBE,
            provider="api-probe",
            source=f"apiprobe://{application_id}/{result.endpoint_id}",
            title=f"API probe {result.method} -> {result.status_code} ({result.endpoint_id})",
            summary=(
                f"Live {result.method} {result.resolved_url} returned "
                f"{result.status_code} in {result.latency_ms}ms"
                + (" (body truncated)" if result.response_body_truncated else "")
                + f". Body: {result.response_body_excerpt[:1500]}"
            ),
            content_snippet=result.response_body_excerpt,
            observed_at=now,
            retrieved_at=now,
            provenance=EvidenceProvenance(
                tenant_id=tenant_id,
                investigation_id=investigation_id,
                provider_type="api-probe",
                requested_provider_id="api-probe",
                actual_provider_id="api-probe",
                source_system="http",
                retrieval_timestamp=now,
                query_fingerprint=QueryFingerprint(
                    provider_type="api-probe",
                    operation="probe_api",
                    normalized_query_hash=fp[:32],
                ),
                source_location=SourceLocation(
                    system="http",
                    identifier=result.resolved_url,
                ),
            ),
            freshness=EvidenceFreshness(observed_at=now, retrieved_at=now),
            fingerprint=f"apiprobe-{fp[:32]}",
        )

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception(lambda e: getattr(e, "retryable", False)),
        reraise=True,
    )
    @traced("gateway", extra_from=_gateway_extra)
    async def correlate(
        self,
        tenant_id: str,
        investigation_id: UUID,
        application_id: str,
        root_evidence_ids: list[UUID],
        max_depth: int,
    ) -> EvidenceQueryResult:
        with tracer.start_as_current_span("AsyncEvidenceGateway.correlate"):
            await self._enforce_authorization(tenant_id, "CORRELATE", application_id=application_id)
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

    # -- Layer 3 topology integration (prompt1_v1.md D7) -----------------------

    @traced("gateway", extra_from=_gateway_extra)
    async def resolve_code_ownership(
        self,
        tenant_id: str,
        investigation_id: UUID,
        application_id: str,
        file_path: str,
        line_number: int,
        revision: str | None = None,
    ) -> "StaticOwnershipResult":
        """Resolve one source location to its static ownership path.

        Authorizes ``QUERY_CODE_TOPOLOGY`` before delegating to the topology
        port. Raises ``TopologyNotConfiguredError`` if no registry/port is
        wired for this tenant/application — never silently returns an empty
        or fabricated result.
        """
        from investigation_agent_platform.domain.common.exceptions import (
            TopologyNotConfiguredError,
        )

        with tracer.start_as_current_span("AsyncEvidenceGateway.resolve_code_ownership"):
            await self._enforce_authorization(tenant_id, "QUERY_CODE_TOPOLOGY", file_path, application_id=application_id)
            if self._repository_registry is None or self._failure_attribution_service is None:
                raise TopologyNotConfiguredError(
                    f"No topology provider configured for tenant={tenant_id}",
                    details={"tenant_id": tenant_id, "application_id": application_id},
                )
            repo = await self._repository_registry.resolve_for_application(
                tenant_id, application_id
            )
            if repo is None:
                raise TopologyNotConfiguredError(
                    f"No repository registered for application={application_id}",
                    details={"tenant_id": tenant_id, "application_id": application_id},
                )
            attribution_port = self._failure_attribution_service._attribution_port
            return await asyncio.wait_for(
                attribution_port.resolve_source_location(
                    tenant_id=tenant_id,
                    application_id=application_id,
                    investigation_id=investigation_id,
                    repository_id=repo.repository_id,
                    revision=revision or repo.default_branch,
                    file_path=file_path,
                    line_number=line_number,
                ),
                timeout=self._timeout_seconds,
            )

    @traced("gateway", extra_from=_gateway_extra)
    async def attribute_failure_frames(
        self,
        tenant_id: str,
        investigation_id: UUID,
        application_id: str,
        failure_frame: "StackFrameLocation",
        caller_frame: "StackFrameLocation | None" = None,
        supporting_evidence_ids: list[UUID] | None = None,
        caller_input_violates_contract: bool | None = None,
    ) -> "DomainAttributionResult":
        """Resolve dual-frame ownership and persist it as provenanced evidence.

        Authorizes ``RESOLVE_DOMAIN_ATTRIBUTION`` before delegating to
        ``FailureAttributionService``. This is the only sanctioned path from
        a Temporal activity to Layer 3 topology — direct Neo4j/driver access
        from activities is not permitted (D7).
        """
        from investigation_agent_platform.domain.common.exceptions import (
            TopologyNotConfiguredError,
        )

        with tracer.start_as_current_span("AsyncEvidenceGateway.attribute_failure_frames"):
            await self._enforce_authorization(
                tenant_id,
                "RESOLVE_DOMAIN_ATTRIBUTION",
                failure_frame.repository_id,
                application_id=application_id,
            )
            if self._failure_attribution_service is None:
                raise TopologyNotConfiguredError(
                    f"No failure attribution service configured for tenant={tenant_id}",
                    details={"tenant_id": tenant_id},
                )
            return await asyncio.wait_for(
                self._failure_attribution_service.attribute_failure(
                    tenant_id=tenant_id,
                    application_id=application_id,
                    investigation_id=investigation_id,
                    failure_frame=failure_frame,
                    caller_frame=caller_frame,
                    supporting_evidence_ids=supporting_evidence_ids,
                    caller_input_violates_contract=caller_input_violates_contract,
                ),
                timeout=self._timeout_seconds,
            )
