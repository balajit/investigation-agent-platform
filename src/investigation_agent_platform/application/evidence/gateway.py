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

from investigation_agent_platform.application.evidence.code_recall import (
    FALLBACK_RUN_CAP,
    CodeRecallMiss,
    build_fuzzy_orientation,
    classify_code_recall_miss,
    fuzzy_fallback_candidates,
    verify_fuzzy_candidates,
)
from investigation_agent_platform.domain.common.exceptions import (
    ExecutionError,
    ProviderNotConfiguredError,
    SecurityPolicyViolationException,
    TopologyAmbiguousMatchError,
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


def _is_retryable_gateway_failure(exc: BaseException) -> bool:
    """Tenacity predicate for gateway provider calls (Phase 3.1).

    Retryable-flagged domain errors retry as before. Builtin timeouts from
    the asyncio.wait_for boundary also retry (bounded by the decorator):
    a timeout is transient until proven otherwise — the final failure is
    recorded as a typed TIMEOUT outcome by the caller ("retry then
    record"), never mistaken for absence.
    """
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return True
    return bool(getattr(exc, "retryable", False))


# Upper bound for honoring a server Retry-After hint (seconds). Gateway
# calls run inside 60s activities with a 30s call timeout: an uncapped
# hint (throttled providers may ask for minutes) would blow the activity
# budget and turn every throttle into a Temporal timeout. All waits run
# activity-side, so sleeping is replay-safe (activities re-execute;
# only workflow code faces determinism constraints).
MAX_RETRY_AFTER_WAIT = 15.0


def _wait_with_retry_after(retry_state: Any) -> float:
    """Tenacity wait honoring first-party Retry-After hints (close-out).

    Exponential base preserves existing behavior; a trusted retry_after
    on the failing exception (RateLimitException only — never sniffed
    text) extends the wait up to the cap. Unknown shapes fall back to
    the base wait; nothing here ever raises.
    """
    base = wait_exponential(multiplier=1, min=2, max=10)(retry_state)
    try:
        outcome = getattr(retry_state, "outcome", None)
        exc = outcome.exception() if outcome is not None else None
        hint = float(getattr(exc, "retry_after", None) or 0.0)
    except (TypeError, ValueError, AttributeError):
        hint = 0.0
    except Exception:
        hint = 0.0
    if hint <= 0:
        return base
    try:
        return min(max(float(base), hint), MAX_RETRY_AFTER_WAIT)
    except (TypeError, ValueError):
        return base


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


def _cognee_fallback_enabled() -> bool:
    """Step-4 gate: fuzzy fallback runs only under explicit opt-in.

    ``IAP_COGNEE_ENABLED`` defaults to off; the flag-off path through
    ``find_symbol`` is byte-identical to the pre-fallback behavior (same
    envelope, same raise contract, no new telemetry).
    """
    return (os.environ.get("IAP_COGNEE_ENABLED", "false") or "").strip().lower() == "true"


# Bound on remembered per-investigation fallback-run counters: the gateway is
# long-lived, so run-budget tracking is capped like the deduplicator's memory.
_FUZZY_RUN_TRACKING_MAX = 5_000


class EvidenceDeduplicator:
    """Fingerprint-based deduplicator scoped per investigation.

    Keys are (tenant, investigation, fingerprint): evidence rows are
    investigation-scoped (citations resolve per investigation), so a row
    banked by one investigation must never suppress the identical row in
    another. The pre-Phase-4 tenant-only key did exactly that — a repeat
    query in a later investigation returned zero rows, which the loop
    read as absence. Within one investigation, repeats still dedupe.
    History is insertion-ordered and capped; there is deliberately no
    persistence-backed check: crash-retry re-banking is owned by action
    idempotency (which replays cached responses without re-executing),
    and any cross-investigation suppression corrupts citations.
    Fingerprints are deterministic (see ``domain/evidence/models.py``
    and provider implementations for elastic/oracle/git).
    """

    # Upper bound on remembered keys: a long-lived gateway otherwise
    # grows without limit across investigations.
    MAX_REMEMBERED = 20_000

    def __init__(self, evidence_repository: Any | None = None) -> None:
        self._seen: dict[tuple[str, str, str], None] = {}
        self._evidence_repository = evidence_repository

    @staticmethod
    def _key(tenant_id: Any, investigation_id: Any, fingerprint: str) -> tuple[str, str, str]:
        return (str(tenant_id or ""), str(investigation_id or ""), str(fingerprint or ""))

    def _is_seen(self, tenant_id: Any, investigation_id: Any, fingerprint: str) -> bool:
        return self._key(tenant_id, investigation_id, fingerprint) in self._seen

    def _mark_seen(self, tenant_id: Any, investigation_id: Any, fingerprint: str) -> None:
        key = self._key(tenant_id, investigation_id, fingerprint)
        if key not in self._seen:
            self._seen[key] = None
            while len(self._seen) > self.MAX_REMEMBERED:
                self._seen.pop(next(iter(self._seen)))

    @traced("gateway", component="deduplicator", extra_from=_dedupe_extra)
    async def deduplicate(
        self,
        tenant_id: str,
        items: list[Evidence],
        investigation_id: Any | None = None,
    ) -> list[Evidence]:
        deduped: list[Evidence] = []
        seen_batch: set[str] = set()
        for item in items:
            if item.fingerprint in seen_batch:
                continue
            if self._is_seen(tenant_id, investigation_id, item.fingerprint):
                continue
            seen_batch.add(item.fingerprint)
            self._mark_seen(tenant_id, investigation_id, item.fingerprint)
            deduped.append(item)
        return deduped

    def deduplicate_sync(
        self,
        tenant_id: str,
        items: list[Evidence],
        investigation_id: Any | None = None,
    ) -> list[Evidence]:
        """Synchronous variant for contexts without repository I/O."""
        deduped: list[Evidence] = []
        seen_batch: set[str] = set()
        for item in items:
            if item.fingerprint in seen_batch:
                continue
            if self._is_seen(tenant_id, investigation_id, item.fingerprint):
                continue
            seen_batch.add(item.fingerprint)
            self._mark_seen(tenant_id, investigation_id, item.fingerprint)
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
        # Step-4 fuzzy-fallback seams (all optional; all default to today's
        # behavior). `fuzzy_search_fn` is an injected retrieval-only search
        # callable `(query_text, search_type, dataset) -> rows` so the wiring
        # is unit-testable without the `cognee` package.
        # `fuzzy_authority_fn` resolves the revision-pinned authority index
        # `(tenant_id, repository_id, revision) -> mapping`; `fuzzy_dataset_fn`
        # resolves the salted dataset `(tenant_id, repository_id, revision)`.
        # With the flag off (or any seam missing) the fallback never runs.
        fuzzy_search_fn: Any | None = None,
        fuzzy_authority_fn: Any | None = None,
        fuzzy_dataset_fn: Any | None = None,
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
        self._fuzzy_search_fn = fuzzy_search_fn
        self._fuzzy_authority_fn = fuzzy_authority_fn
        self._fuzzy_dataset_fn = fuzzy_dataset_fn
        # Per-investigation fallback-run counters for the D5 per-run budget
        # (FALLBACK_RUN_CAP): incremented each time the fallback actually
        # searches, never on the flag-off path.
        self._fuzzy_runs_used: dict[str, int] = {}

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
        wait=_wait_with_retry_after,
        retry=retry_if_exception(_is_retryable_gateway_failure),
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
            deduped = await self._deduplicator.deduplicate(
                tenant_id, sanitized_items, investigation_id
            )

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
                            tenant_id, fallback_items, investigation_id
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
        wait=_wait_with_retry_after,
        retry=retry_if_exception(_is_retryable_gateway_failure),
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
                raise ProviderNotConfiguredError(
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
            deduped = await self._deduplicator.deduplicate(
                tenant_id, sanitized_items, investigation_id
            )
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
                    tenant_id, sanitized_extra, investigation_id
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
        wait=_wait_with_retry_after,
        retry=retry_if_exception(_is_retryable_gateway_failure),
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
                raise ProviderNotConfiguredError(f"No code provider available for app '{application_id}'")
            profile = await self._effective_profile(provider, tenant_id, application_id)
            if profile is None:
                raise ProviderNotConfiguredError(
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
            deduped = await self._deduplicator.deduplicate(
                tenant_id, sanitized_items, investigation_id
            )
            return EvidenceQueryResult(items=deduped, total_count=len(deduped))

    @retry(
        stop=stop_after_attempt(3),
        wait=_wait_with_retry_after,
        retry=retry_if_exception(_is_retryable_gateway_failure),
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
                raise ProviderNotConfiguredError(f"No code provider available for app '{application_id}'")
            profile = await self._effective_profile(provider, tenant_id, application_id)
            if profile is None:
                raise ProviderNotConfiguredError(
                    f"No application profile for '{application_id}'; cannot scope code access"
                )
            self._require_revision_capability(provider, request.revision, request.require_revision)
            try:
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
            except TopologyAmbiguousMatchError as exc:
                # Resolver-layer overload abstention maps to AMBIGUOUS_OVERLOAD
                # at this call site. The classifier intentionally never emits
                # it (it only sees shape-level classes), so the mapping lives
                # here, next to the abstaining call.
                return await self._find_symbol_fuzzy_fallback(
                    tenant_id,
                    investigation_id,
                    application_id,
                    request,
                    CodeRecallMiss.AMBIGUOUS_OVERLOAD,
                    symbols=[],
                    abstention=exc,
                )
            if symbols:
                return EvidenceQueryResult(
                    items=[],
                    total_count=len(symbols),
                    provider_metadata={"symbols": [s.model_dump() for s in symbols]},
                )
            miss = classify_code_recall_miss(
                "code", 0, 0, request.symbol_name or ""
            )
            return await self._find_symbol_fuzzy_fallback(
                tenant_id,
                investigation_id,
                application_id,
                request,
                miss,
                symbols=[],
                abstention=None,
            )

    def _emit_code_recall_metric(
        self, metric_name: str, value: float, tags: dict[str, str]
    ) -> None:
        """D5 counter emission through the existing telemetry hook.

        Fire-and-forget: adapters must never raise into an investigation, and
        a missing telemetry port skips silently. Label vocabularies stay
        closed (miss classes, search-type names) — symbols, paths, and
        revisions never appear as labels.
        """
        try:
            telemetry = self._telemetry
            if telemetry is None:
                return
            record = getattr(telemetry, "record_metric", None)
            if not callable(record):
                return
            record(metric_name, float(value), dict(tags))
        except Exception:
            logger.debug(
                "code-recall metric emission failed",
                extra={"metric": metric_name},
            )

    async def _resolve_fuzzy_repository_id(
        self, tenant_id: str, application_id: str, request: SymbolRequest
    ) -> str | None:
        """Best-effort repository identity for fallback scoping (never raises)."""
        try:
            registry = self._repository_registry
            resolve = getattr(registry, "resolve_for_application", None)
            if callable(resolve):
                identity = await resolve(tenant_id, application_id)
                repository_id = getattr(identity, "repository_id", None)
                if repository_id:
                    return str(repository_id)
        except Exception:
            pass
        fallback = str(getattr(request, "repository", "") or "").strip()
        return fallback or None

    def _resolve_fuzzy_dataset(
        self, tenant_id: str, repository_id: str, revision: str
    ) -> str | None:
        """Salted dataset for fallback scoping, or None when unresolvable."""
        try:
            if self._fuzzy_dataset_fn is not None:
                dataset = self._fuzzy_dataset_fn(tenant_id, repository_id, revision)
                return str(dataset) if dataset else None
            from investigation_agent_platform.infrastructure.topology.cognee_adapter import (
                derive_dataset_id,
            )

            salt = os.environ.get("IAP_COGNEE_DATASET_SALT", "")
            return derive_dataset_id(tenant_id, repository_id, revision, salt)
        except Exception:
            return None

    async def _resolve_fuzzy_authority(
        self, tenant_id: str, repository_id: str, revision: str
    ) -> dict[str, Any]:
        """Revision-pinned authority index for re-verification (never raises)."""
        try:
            fn = self._fuzzy_authority_fn
            if fn is None:
                return {}
            result = fn(tenant_id, repository_id, revision)
            if asyncio.iscoroutine(result):
                result = await result
            return dict(result or {})
        except Exception:
            return {}

    def _track_fuzzy_run(self, run_key: str) -> int:
        """Per-investigation fallback-run usage for the D5 per-run budget.

        Returns the runs already used (before this attempt). The tracking map
        is bounded; eviction drops the oldest keys first.
        """
        used = self._fuzzy_runs_used.get(run_key, 0)
        self._fuzzy_runs_used[run_key] = used + 1
        while len(self._fuzzy_runs_used) > _FUZZY_RUN_TRACKING_MAX:
            self._fuzzy_runs_used.pop(next(iter(self._fuzzy_runs_used)))
        return used

    @staticmethod
    def _deterministic_symbol_result(
        symbols: list[Any], miss: CodeRecallMiss | None = None
    ) -> EvidenceQueryResult:
        """Today's find_symbol envelope, optionally annotated with the miss."""
        metadata: dict[str, Any] = {"symbols": [s.model_dump() for s in symbols]}
        if miss is not None:
            metadata["code_recall_miss"] = miss.value
        return EvidenceQueryResult(
            items=[],
            total_count=len(symbols),
            provider_metadata=metadata,
        )

    async def _find_symbol_fuzzy_fallback(
        self,
        tenant_id: str,
        investigation_id: UUID,
        application_id: str,
        request: SymbolRequest,
        miss: CodeRecallMiss,
        symbols: list[Any],
        abstention: TopologyAmbiguousMatchError | None,
    ) -> EvidenceQueryResult:
        """Step-4 wiring: miss → capped fallback → verification → targets.

        Strictly behind ``IAP_COGNEE_ENABLED``: the flag-off path returns the
        deterministic envelope (or re-raises the resolver abstention) with no
        new side effects. The flag-on path without search seams degrades to
        the deterministic envelope annotated with the miss class. Fuzzy output
        is never banked as Evidence rows: ``items`` stays empty and only
        verified IDs surface, as range-read targets plus a non-citable
        orientation hint in metadata.
        """
        if miss is CodeRecallMiss.NO_MISS or not _cognee_fallback_enabled():
            if abstention is not None:
                raise abstention
            return self._deterministic_symbol_result(symbols)
        miss_tag = {"miss": miss.value}
        self._emit_code_recall_metric("iap.code_recall.miss", 1.0, miss_tag)
        search_fn = self._fuzzy_search_fn
        if search_fn is None:
            if abstention is not None:
                raise abstention
            return self._deterministic_symbol_result(symbols, miss)
        repository_id = await self._resolve_fuzzy_repository_id(
            tenant_id, application_id, request
        )
        revision = str(getattr(request, "revision", "") or "") or "HEAD"
        dataset = (
            self._resolve_fuzzy_dataset(tenant_id, repository_id, revision)
            if repository_id
            else None
        )
        if not repository_id or not dataset:
            if abstention is not None:
                raise abstention
            return self._deterministic_symbol_result(symbols, miss)
        run_key = f"{tenant_id}:{investigation_id}"
        run_used = self._fuzzy_runs_used.get(run_key, 0)
        if run_used >= FALLBACK_RUN_CAP:
            # Telescoping budget: the per-run cap gates the per-call cap.
            # Exhaustion is a normal counted outcome, never an error.
            self._emit_code_recall_metric(
                "iap.code_recall.fallback.run_budget_exhausted", 1.0, miss_tag
            )
            if abstention is not None:
                raise abstention
            return self._deterministic_symbol_result(symbols, miss)
        outcome = await fuzzy_fallback_candidates(
            query_text=request.symbol_name,
            dataset=dataset,
            run_used=run_used,
            search_fn=search_fn,
        )
        self._track_fuzzy_run(run_key)
        self._emit_code_recall_metric(
            "iap.code_recall.fallback.search_types_used",
            float(len(outcome.search_types_used)),
            {**miss_tag, "search_types": ",".join(outcome.search_types_used) or "none"},
        )
        self._emit_code_recall_metric(
            "iap.code_recall.fallback.candidates_seen",
            float(outcome.candidates_seen),
            miss_tag,
        )
        authority = await self._resolve_fuzzy_authority(
            tenant_id, repository_id, revision
        )
        kept, discarded = verify_fuzzy_candidates(
            outcome.candidate_ids, authority, tenant_id, repository_id, revision
        )
        self._emit_code_recall_metric(
            "iap.code_recall.fallback.verified_kept", float(len(kept)), miss_tag
        )
        self._emit_code_recall_metric(
            "iap.code_recall.fallback.discarded", float(discarded), miss_tag
        )
        # Only IDs crossed the verification boundary: targets and orientation
        # symbols are read off the authority index, never off fuzzy payloads
        # (scores, snippets, summaries were dropped with the raw rows).
        verified_nodes: list[Any] = []
        targets: list[dict[str, Any]] = []
        for node_id in kept:
            node = authority.get(str(node_id))
            if node is None:
                continue
            verified_nodes.append(node)
            try:
                file_path = getattr(node, "file_path", None)
                if isinstance(node, dict):
                    file_path = node.get("file_path", file_path)
                target: dict[str, Any] = {"node_id": str(node_id)}
                if file_path:
                    target["file_path"] = str(file_path)
                for line_key in ("start_line", "end_line"):
                    line_value = getattr(node, line_key, None)
                    if isinstance(node, dict) and line_key in node:
                        line_value = node.get(line_key)
                    if line_value is not None:
                        target[line_key] = int(line_value)
                qualified = getattr(node, "qualified_name", None)
                if isinstance(node, dict):
                    qualified = node.get("qualified_name", qualified)
                if qualified:
                    target["qualified_name"] = str(qualified)
                targets.append(target)
            except (TypeError, ValueError):
                continue
        orientation = build_fuzzy_orientation(
            miss,
            verified_nodes,
            steering_note=(
                f"Fuzzy fallback ({miss.value}) verified {len(kept)} of "
                f"{outcome.candidates_seen} candidate(s) against the topology "
                f"authority; unverified steering only — fire deterministic "
                f"range reads for the targets and cite those reads, never "
                f"this hint."
            ),
        )
        return EvidenceQueryResult(
            items=[],
            total_count=0,
            provider_metadata={
                "symbols": [],
                "code_recall_miss": miss.value,
                "fuzzy_verified_targets": targets,
                "fuzzy_orientation": orientation.model_dump(mode="json"),
                "fuzzy_search_types_used": list(outcome.search_types_used),
                "fuzzy_candidates_seen": outcome.candidates_seen,
            },
        )

    @retry(
        stop=stop_after_attempt(3),
        wait=_wait_with_retry_after,
        retry=retry_if_exception(_is_retryable_gateway_failure),
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
                raise ProviderNotConfiguredError(f"No code provider available for app '{application_id}'")
            profile = await self._effective_profile(provider, tenant_id, application_id)
            if profile is None:
                raise ProviderNotConfiguredError(
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
        wait=_wait_with_retry_after,
        retry=retry_if_exception(_is_retryable_gateway_failure),
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
                raise ProviderNotConfiguredError(f"No code provider available for app '{application_id}'")
            profile = await self._effective_profile(provider, tenant_id, application_id)
            if profile is None:
                raise ProviderNotConfiguredError(
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
        wait=_wait_with_retry_after,
        retry=retry_if_exception(_is_retryable_gateway_failure),
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
                raise ProviderNotConfiguredError(f"No code provider available for app '{application_id}'")
            profile = await self._effective_profile(provider, tenant_id, application_id)
            if profile is None:
                raise ProviderNotConfiguredError(
                    f"No application profile for '{application_id}'; cannot scope code access"
                )
            items = await asyncio.wait_for(
                provider.get_code_history(
                    tenant_id, investigation_id, request.file_path, profile
                ),
                timeout=self._timeout_seconds,
            )
            sanitized = [await self._sanitizer.sanitize_evidence(tenant_id, item) for item in items]
            deduped = await self._deduplicator.deduplicate(tenant_id, sanitized, investigation_id)
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
                raise ProviderNotConfiguredError(
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
            deduped = await self._deduplicator.deduplicate(tenant_id, [sanitized], investigation_id)
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
        wait=_wait_with_retry_after,
        retry=retry_if_exception(_is_retryable_gateway_failure),
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
