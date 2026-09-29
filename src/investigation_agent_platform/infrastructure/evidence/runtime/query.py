# src/investigation_agent_platform/infrastructure/evidence/runtime/query.py
"""Normalized query construction for the Elastic runtime adapter (Parts 9-10).

``NormalizedRuntimeQuery`` is the canonical internal representation of a
search: only supported semantics, order-normalized so the same logical query
always yields the same fingerprint. The fingerprint binds pagination cursors
to the exact query and populates provenance.

Part 10: every physical field name resolves through the request's
``ObservabilitySourceMapping`` — no Python literal below names a log field.
The body builder generates only known-safe clauses (``term``/``terms``/
``multi_match``/``range``/``bool`` plus the mapping sort) — raw DSL, scripts,
aggregations, and caller-controlled sort are structurally impossible.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from investigation_agent_platform.domain.common.exceptions import (
    DomainValidationException,
    SecurityPolicyViolationException,
)
from investigation_agent_platform.domain.evidence.requests import RuntimeEvidenceRequest
from investigation_agent_platform.domain.observability.mapping import (
    FieldValueType,
    ObservabilitySourceMapping,
    SeverityStrategy,
    TenantScopeStrategy,
)
from investigation_agent_platform.infrastructure.evidence.runtime.field_resolver import (
    resolve_identifier_field,
)
from investigation_agent_platform.infrastructure.evidence.runtime.validation import (
    ProviderCeilings,
    validate_identifier_items,
    validate_keyword_terms,
    validate_service_terms,
    validate_time_window_seconds,
)

# Deterministic sort contract for all runtime searches. Part of the normalized
# query: cursors bind to this version and reject on mismatch after rotation.
# (Per-mapping sort field/order is bound separately; see fingerprint.)
SORT_VERSION = "ts-desc-id-asc-v1"


@dataclass(frozen=True)
class NormalizedRuntimeQuery:
    """Canonical internal representation of a runtime search."""

    tenant_id: str
    investigation_id: UUID
    environment: str
    identifiers: tuple[tuple[str, str], ...]
    keywords: tuple[str, ...]
    services: tuple[str, ...]
    severities: tuple[str, ...]
    start_time: datetime | None
    end_time: datetime | None
    effective_limit: int
    sort_version: str = SORT_VERSION
    # Part 10: bound so pagination state cannot cross mappings. Normalized so
    # None (implicit generic-ECS) and "generic-ecs" fingerprint identically.
    mapping_source_id: str = "generic-ecs"
    sort_field: str = "@timestamp"
    sort_order: str = "desc"

    def fingerprint(self) -> str:
        canonical = {
            "tenant_id": self.tenant_id,
            "investigation_id": str(self.investigation_id),
            "environment": self.environment,
            "identifiers": [list(pair) for pair in self.identifiers],
            "keywords": list(self.keywords),
            "services": list(self.services),
            "severities": list(self.severities),
            "start_time": self.start_time.isoformat() if self.start_time else None,
            "end_time": self.end_time.isoformat() if self.end_time else None,
            "effective_limit": self.effective_limit,
            "sort_version": self.sort_version,
            "mapping_source_id": self.mapping_source_id,
            "sort_field": self.sort_field,
            "sort_order": self.sort_order,
        }
        encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


def _effective_mapping_id(request: RuntimeEvidenceRequest) -> str:
    """Normalize None to the built-in generic-ECS id for stable fingerprints."""
    return request.mapping_source_id or "generic-ecs"


def normalize_request(
    tenant_id: str,
    investigation_id: UUID,
    request: RuntimeEvidenceRequest,
    ceilings: ProviderCeilings,
    mapping: ObservabilitySourceMapping,
) -> NormalizedRuntimeQuery:
    """Canonicalize a search and enforce provider ceilings independently.

    Oversized restrictive filters reject (never silently truncate, which would
    alter investigation semantics); only the result size is normalized down
    via ``min()``, visibly through ``has_more``.

    Part 10: identifier keys are validated against the resolved mapping
    (raising ``DomainValidationException`` so the MCP tier keeps rendering
    ``INVALID_REQUEST``); filters over concepts the mapping does not declare
    (keywords without a message field, services without a service field,
    severities against an UNSUPPORTED source) reject the same way — a filter
    that cannot match anything must fail loudly, never narrow to zero hits.
    """
    keywords = validate_keyword_terms(list(request.keywords or []), ceilings)
    services = validate_service_terms(list(request.services or []), ceilings)
    identifiers = tuple(validate_identifier_items(dict(request.identifiers or {}), mapping))

    if keywords and mapping.message is None:
        raise DomainValidationException(
            "Keyword search is not supported by this observability source."
        )
    if services and mapping.service is None:
        raise DomainValidationException(
            "Service filtering is not supported by this observability source."
        )
    if (request.severities or []) and (mapping.severity.strategy == SeverityStrategy.UNSUPPORTED):
        raise DomainValidationException(
            "Severity filtering is not supported by this observability source."
        )

    start_time = request.time_range.start_time if request.time_range else None
    end_time = request.time_range.end_time if request.time_range else None
    if request.time_range:
        window_seconds = (
            request.time_range.end_time - request.time_range.start_time
        ).total_seconds()
        validate_time_window_seconds(window_seconds, ceilings)

    effective_limit = min(request.limit, ceilings.max_hits)
    if effective_limit < 1:
        raise SecurityPolicyViolationException("Invalid result limit at provider boundary.")

    return NormalizedRuntimeQuery(
        tenant_id=tenant_id,
        investigation_id=investigation_id,
        environment=request.environment,
        identifiers=identifiers,
        keywords=keywords,
        services=services,
        # Domain-validated enum values pass through as their string forms; no
        # provider-side filtering that could silently broaden the query.
        severities=tuple(sorted(str(severity) for severity in (request.severities or []))),
        start_time=start_time,
        end_time=end_time,
        effective_limit=effective_limit,
        mapping_source_id=_effective_mapping_id(request),
        sort_field=mapping.sort.field,
        sort_order=mapping.sort.order.value,
    )


def _severity_clauses(
    severities: tuple[str, ...], mapping: ObservabilitySourceMapping
) -> list[dict[str, Any]]:
    """Translate requested severities per the mapping's derivation strategy.

    ERROR_FLAG projects the boolean signal onto the configured pair: each
    requested severity votes for its side, and sides with no votes emit no
    clause. No votes at all means nothing in this source can carry a
    requested severity — an explicit match-none, never a dropped filter.
    """
    derivation = mapping.severity
    if derivation.strategy == SeverityStrategy.UNSUPPORTED:
        # Rejected upstream in normalize_request; unreachable here.
        raise DomainValidationException(
            "Severity filtering is not supported by this observability source."
        )
    if derivation.strategy == SeverityStrategy.FIELD:
        if derivation.field is None:  # defensive; model should guarantee
            raise DomainValidationException("Severity field mapping is missing for this source.")
        field = resolve_identifier_field("severity", derivation.field)
        return [{"terms": {field: list(severities)}}]
    wanted = set(severities)
    clauses: list[dict[str, Any]] = []
    if derivation.true_severity.value in wanted and derivation.error_field:
        clauses.append({"term": {derivation.error_field: True}})
    if derivation.false_severity.value in wanted and derivation.error_field:
        clauses.append({"term": {derivation.error_field: False}})
    if not clauses:
        return [{"bool": {"must_not": {"match_all": {}}}}]
    if len(clauses) == 1:
        return clauses
    return [{"bool": {"should": clauses, "minimum_should_match": 1}}]


def build_query_body(
    normalized: NormalizedRuntimeQuery,
    mapping: ObservabilitySourceMapping,
    search_after: list[Any] | None,
    query_timeout_seconds: float,
) -> dict[str, Any]:
    """Build the bounded request body from normalized state (data only)."""
    must_clauses: list[dict[str, Any]] = []
    # Tenant isolation clause first: not agent-visible, not agent-overridable.
    # Only FIELD-strategy sources emit one; the strategy itself is operator
    # configuration, and NONE_REQUIRED sources log distinctly at startup.
    if mapping.tenant_scope.strategy == TenantScopeStrategy.FIELD:
        if not mapping.tenant_scope.field:
            raise DomainValidationException("Tenant field mapping is missing for this source.")
        must_clauses.append({"term": {mapping.tenant_scope.field: normalized.tenant_id}})
    for key, value in normalized.identifiers:
        field_mapping = mapping.identifiers.get(key)
        if field_mapping is None:  # defensive; normalize_request validates keys
            raise DomainValidationException(f"Unauthorized identifier filter key: {key}")
        field = resolve_identifier_field(key, field_mapping)
        must_clauses.append({"term": {field: value}})
    if normalized.severities:
        must_clauses.extend(_severity_clauses(normalized.severities, mapping))
    if normalized.services:
        if mapping.service is None:  # defensive; normalize_request rejects
            raise DomainValidationException(
                "Service filtering is not supported by this observability source."
            )
        must_clauses.append(
            {
                "terms": {
                    resolve_identifier_field("service", mapping.service): list(normalized.services)
                }
            }
        )
    if normalized.keywords:
        if mapping.message is None:  # defensive; normalize_request rejects
            raise DomainValidationException(
                "Keyword search is not supported by this observability source."
            )
        # Recorded contract: keywords are OR-matched across the message
        # fields (explicit operator; previously the Elastic default).
        # Changing this alters investigation semantics — do so deliberately.
        must_clauses.append(
            {
                "multi_match": {
                    "query": " ".join(normalized.keywords),
                    "fields": list(mapping.message.candidates),
                    "operator": "OR",
                }
            }
        )

    filter_clauses: list[dict[str, Any]] = []
    if normalized.start_time is not None and normalized.end_time is not None:
        if mapping.timestamp.value_type == FieldValueType.DATE_ISO:
            gte: Any = normalized.start_time.isoformat()
            lte: Any = normalized.end_time.isoformat()
        elif mapping.timestamp.value_type == FieldValueType.DATE_EPOCH_MILLIS:
            gte = int(normalized.start_time.timestamp() * 1000)
            lte = int(normalized.end_time.timestamp() * 1000)
        elif mapping.timestamp.value_type == FieldValueType.DATE_EPOCH_SECONDS:
            gte = int(normalized.start_time.timestamp())
            lte = int(normalized.end_time.timestamp())
        else:
            raise DomainValidationException(
                "Time-range filtering is not supported by this observability source."
            )
        filter_clauses.append(
            {
                "range": {
                    mapping.timestamp.candidates[0]: {
                        "gte": gte,
                        "lte": lte,
                    }
                }
            }
        )

    sort_spec: dict[str, Any] = {"order": normalized.sort_order}
    if mapping.sort.format is not None:
        sort_spec["format"] = mapping.sort.format
    if mapping.sort.unmapped_type is not None:
        sort_spec["unmapped_type"] = mapping.sort.unmapped_type
    query_body: dict[str, Any] = {
        "query": {"bool": {"must": must_clauses, "filter": filter_clauses}},
        "size": normalized.effective_limit,
        "sort": [
            {mapping.sort.field: sort_spec},
            {"_id": "asc"},
        ],
        # Defense against unbounded aggregations/deep pagination via crafted bodies.
        "track_total_hits": normalized.effective_limit,
        "timeout": f"{query_timeout_seconds:g}s",
        "_source": {"includes": sorted(mapping.top_level_allowlist)},
    }
    if search_after is not None:
        query_body["search_after"] = search_after
    return query_body
