# src/investigation_agent_platform/infrastructure/evidence/runtime/elastic.py
import asyncio
import base64
import hashlib
import hmac
import json
import logging
import re
import uuid
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from elasticsearch import AsyncElasticsearch
from opentelemetry import trace

from investigation_agent_platform.domain.common.exceptions import (
    ExecutionError,
    SecurityPolicyViolationException,
)
from investigation_agent_platform.domain.evidence.models import (
    ClassificationLevel,
    Evidence,
    EvidenceType,
)
from investigation_agent_platform.domain.evidence.requests import RuntimeEvidenceRequest
from investigation_agent_platform.domain.profile.models import ObservabilityProfile
from investigation_agent_platform.domain.provenance.models import (
    EvidenceFreshness,
    EvidenceProvenance,
    QueryFingerprint,
    SourceLocation,
)
from investigation_agent_platform.ports.evidence.gateway import EvidenceQueryResult

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)

ALLOWED_LABEL_KEYS: set[str] = {
    "trace_id",
    "span_id",
    "service_name",
    "user_id",
    "session_id",
    "container_id",
}
CURSOR_HMAC_SECRET = b"iap-elastic-cursor-binding-key"

# F-043: provider-side bounds enforced regardless of what an authenticated
# caller requests — the platform must defend against expensive LLM-generated
# queries, not rely on prompt-level instructions.
ELASTIC_MAX_HITS = 200
ELASTIC_MAX_TIME_WINDOW_SECONDS = 7 * 24 * 3600
ELASTIC_MAX_KEYWORD_TERMS = 20
ELASTIC_MAX_SERVICES = 10


class AsyncElasticAdapter:
    """Asynchronous Elasticsearch adapter for searching and mapping runtime logs and traces."""

    def __init__(
        self,
        client: AsyncElasticsearch,
        provider_id: str = "elastic-primary",
        request_timeout: int = 30,
    ) -> None:
        self._client = client
        self._provider_id = provider_id
        self._request_timeout = request_timeout

    @staticmethod
    def _encode_signed_cursor(sort_values: list[Any], tenant_id: str, app_id: str) -> str:
        payload = json.dumps({"sort": sort_values, "tenant_id": tenant_id, "app_id": app_id})
        sig = hmac.new(CURSOR_HMAC_SECRET, payload.encode("utf-8"), hashlib.sha256).hexdigest()
        token = json.dumps({"p": payload, "s": sig})
        return base64.urlsafe_b64encode(token.encode("utf-8")).decode("ascii")

    @staticmethod
    def _decode_signed_cursor(cursor: str, tenant_id: str, app_id: str) -> list[Any] | None:
        try:
            token = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8"))
            payload_str, expected_sig = token["p"], token["s"]
            actual_sig = hmac.new(
                CURSOR_HMAC_SECRET, payload_str.encode("utf-8"), hashlib.sha256
            ).hexdigest()
            if not hmac.compare_digest(actual_sig, expected_sig):
                logger.warning(
                    "Cursor signature verification failed",
                    extra={"context": {"tenant_id": tenant_id}},
                )
                return None
            data = json.loads(payload_str)
            if data.get("tenant_id") != tenant_id or data.get("app_id") != app_id:
                logger.warning(
                    "Cursor tenant/app mismatch", extra={"context": {"tenant_id": tenant_id}}
                )
                return None
            return data.get("sort")  # type: ignore[no-any-return]
        except Exception:
            logger.warning(
                "Malformed pagination cursor ignored", extra={"context": {"cursor": cursor}}
            )
            return None

    async def search_runtime_evidence(
        self,
        tenant_id: str,
        investigation_id: UUID,
        request: RuntimeEvidenceRequest,
        profile: ObservabilityProfile,
        correlation_id: str | None = None,
    ) -> EvidenceQueryResult:
        with tracer.start_as_current_span("AsyncElasticAdapter.search_runtime_evidence") as span:
            span.set_attribute("tenant_id", tenant_id)
            span.set_attribute("investigation_id", str(investigation_id))
            if correlation_id:
                span.set_attribute("correlation_id", correlation_id)

            # Derive application context from profile provider if available; fallback to tenant-scoped pattern
            index_pattern = f"logs-{tenant_id}-*-{request.environment}-*"
            must_clauses: list[dict[str, Any]] = []

            # F-043: clamp every caller-controlled dimension at the provider boundary.
            effective_limit = min(request.limit, ELASTIC_MAX_HITS)
            keywords = list(request.keywords or [])[:ELASTIC_MAX_KEYWORD_TERMS]
            services = list(request.services or [])[:ELASTIC_MAX_SERVICES]
            severities = [
                s
                for s in (request.severities or [])
                if isinstance(s, str) and re.fullmatch(r"[A-Z_]{1,16}", s)
            ][:10]

            for key, val in request.identifiers.items():
                sanitized_key = re.sub(r"[^a-zA-Z0-9_]", "", key)
                if sanitized_key not in ALLOWED_LABEL_KEYS:
                    raise SecurityPolicyViolationException(f"Unauthorized label filter key: {key}")
                must_clauses.append({"term": {f"labels.{sanitized_key}.keyword": val}})

            if severities:
                must_clauses.append({"terms": {"log.level": severities}})
            if services:
                must_clauses.append({"terms": {"service.name": services}})
            if keywords:
                must_clauses.append(
                    {
                        "multi_match": {
                            "query": " ".join(keywords),
                            "fields": ["message", "error.message"],
                        }
                    }
                )

            filter_clauses: list[dict[str, Any]] = []
            if request.time_range:
                window_seconds = (
                    request.time_range.end_time - request.time_range.start_time
                ).total_seconds()
                if window_seconds > ELASTIC_MAX_TIME_WINDOW_SECONDS:
                    raise SecurityPolicyViolationException(
                        f"Requested time window ({window_seconds:.0f}s) exceeds provider ceiling "
                        f"({ELASTIC_MAX_TIME_WINDOW_SECONDS}s)"
                    )
                if window_seconds <= 0:
                    raise SecurityPolicyViolationException(
                        "Invalid time range: end must be after start"
                    )
                filter_clauses.append(
                    {
                        "range": {
                            "@timestamp": {
                                "gte": request.time_range.start_time.isoformat(),
                                "lte": request.time_range.end_time.isoformat(),
                            }
                        }
                    }
                )

            query_body: dict[str, Any] = {
                "query": {"bool": {"must": must_clauses, "filter": filter_clauses}},
                "size": effective_limit,
                "sort": [
                    {
                        "@timestamp": {
                            "order": "desc",
                            "format": "epoch_millis",
                            "unmapped_type": "long",
                        }
                    },
                    {"_id": "asc"},
                ],
                # Defense against unbounded aggregations/deep pagination via crafted bodies.
                "track_total_hits": effective_limit,
                "timeout": "25s",
            }

            if request.cursor:
                sort_values = self._decode_signed_cursor(
                    request.cursor, tenant_id, str(investigation_id)
                )
                if sort_values is not None:
                    query_body["search_after"] = sort_values

            try:
                response: Any = await asyncio.wait_for(
                    self._client.search(index=index_pattern, body=query_body),
                    timeout=float(self._request_timeout),
                )
            except Exception as exc:
                logger.error(
                    "Elasticsearch query failure",
                    exc_info=exc,
                    extra={"context": {"tenant_id": tenant_id, "correlation_id": correlation_id}},
                )
                raise ExecutionError(f"Elasticsearch query failed: {exc}") from exc

            hits = response.get("hits", {}).get("hits", [])
            items = [self._map_hit_to_evidence(hit, tenant_id, investigation_id) for hit in hits]

            has_more = len(hits) == effective_limit
            next_cursor: str | None = None
            if has_more:
                last_hit = hits[-1]
                sort_values = last_hit.get("sort")
                if isinstance(sort_values, list) and len(sort_values) >= 2:
                    next_cursor = self._encode_signed_cursor(
                        sort_values[:2], tenant_id, str(investigation_id)
                    )

            total = response.get("hits", {}).get("total")
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
            )

    def _map_hit_to_evidence(
        self, hit: dict[str, Any], tenant_id: str, investigation_id: UUID
    ) -> Evidence:
        src = hit.get("_source", {})
        ts_str = src.get("@timestamp")
        try:
            obs_time = (
                datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                if ts_str
                else datetime.now(UTC)
            )
        except ValueError:
            obs_time = datetime.now(UTC)
        now = datetime.now(UTC)

        prov = EvidenceProvenance(
            tenant_id=tenant_id,
            investigation_id=investigation_id,
            provider_type="ELASTIC",
            requested_provider_id=self._provider_id,
            actual_provider_id=self._provider_id,
            source_system="Elasticsearch",
            retrieval_timestamp=now,
            query_fingerprint=QueryFingerprint(
                provider_type="ELASTIC", operation="SEARCH", normalized_query_hash=hit["_id"]
            ),
            source_location=SourceLocation(system="Elasticsearch", identifier=hit["_id"]),
        )
        fresh = EvidenceFreshness(observed_at=obs_time, retrieved_at=now)

        return Evidence(
            tenant_id=tenant_id,
            investigation_id=prov.investigation_id,
            evidence_id=uuid.uuid5(uuid.NAMESPACE_DNS, f"elastic:{hit['_index']}:{hit['_id']}"),
            evidence_type=EvidenceType.RUNTIME_LOG,
            provider="ELASTIC",
            source=f"elasticsearch://{hit['_index']}/{hit['_id']}",
            title=f"Log Record [{hit['_id'][:8]}]",
            summary=str(src.get("message", "Runtime log event"))[:200],
            content_snippet=json.dumps(src, default=str)[:4000],
            content_uri=f"elastic://{hit['_index']}/{hit['_id']}",
            fingerprint=hit["_id"],
            classification=ClassificationLevel.INTERNAL,
            observed_at=obs_time,
            retrieved_at=now,
            attributes={**src, "tenant_id": tenant_id},
            provenance=prov,
            freshness=fresh,
        )
