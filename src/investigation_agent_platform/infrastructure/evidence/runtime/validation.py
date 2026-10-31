# src/investigation_agent_platform/infrastructure/evidence/runtime/validation.py
"""Provider-boundary validation for the Elastic runtime adapter (Part 9).

The domain request model validates first, but this provider never trusts it:
every caller-controlled dimension with security or cost impact is re-checked
here against the configured ceilings. Tenant and environment identifiers are
validated before interpolation into any index expression — never stripped and
continued, always rejected.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass

from investigation_agent_platform.domain.common.exceptions import (
    DomainValidationException,
    SecurityPolicyViolationException,
)
from investigation_agent_platform.domain.evidence.requests import (
    ENVIRONMENT_PATTERN,
    MAX_IDENTIFIER_KEY_LENGTH,
    MAX_IDENTIFIER_VALUE_LENGTH,
    MAX_KEYWORD_LENGTH,
    MAX_SERVICE_LENGTH,
)
from investigation_agent_platform.domain.observability.mapping import (
    ObservabilitySourceMapping,
)

# Tenant identifiers are interpolated into index patterns: same conservative
# shape as the environment allowlist, with the domain tenant length bound.
TENANT_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]*$"
MAX_TENANT_ID_LENGTH = 128
MAX_ENVIRONMENT_LENGTH = 64


@dataclass(frozen=True)
class ProviderCeilings:
    """Operator-tunable provider bounds (mirrors ``EvidenceConfig`` maxima)."""

    max_hits: int = 200
    max_time_window_seconds: int = 7 * 24 * 3600
    max_keyword_terms: int = 20
    max_services: int = 10


def validate_tenant_id(tenant_id: str) -> str:
    """Reject tenant identifiers that are unsafe for index-scope construction."""
    if (
        not isinstance(tenant_id, str)
        or not 1 <= len(tenant_id) <= MAX_TENANT_ID_LENGTH
        or not re.fullmatch(TENANT_ID_PATTERN, tenant_id)
    ):
        raise SecurityPolicyViolationException(
            "Invalid tenant identifier for evidence index scope."
        )
    return tenant_id


def validate_environment(environment: str) -> str:
    """Reject environment values that are unsafe for index-scope construction."""
    if (
        not isinstance(environment, str)
        or not 1 <= len(environment) <= MAX_ENVIRONMENT_LENGTH
        or not re.fullmatch(ENVIRONMENT_PATTERN, environment)
    ):
        raise SecurityPolicyViolationException("Invalid environment for evidence index scope.")
    return environment


# A service name may be interpolated into an index expression only when it is a
# plain token: no wildcard, comma, or whitespace can ever reach the scope.
_SERVICE_INDEX_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def dash_boundary_siblings(tenant_id: str, known_tenant_ids: Iterable[str]) -> list[str]:
    """Registered tenant ids whose indexes the wildcard scope of ``tenant_id`` can overlap.

    Index names follow ``logs-{tenant}-{service}-{env}-{n}`` and both tenant and
    service may contain ``-``, so ``acme`` and ``acme-eu`` are ambiguous at the
    dash boundary in either direction. Matching is exact-case, like the
    Elasticsearch wildcard match it models.
    """
    return sorted(
        other
        for other in set(known_tenant_ids)
        if other != tenant_id
        and (other.startswith(f"{tenant_id}-") or tenant_id.startswith(f"{other}-"))
    )


def build_index_pattern(
    tenant_id: str,
    environment: str,
    *,
    services: Iterable[str] = (),
    known_tenant_ids: Iterable[str] | None = None,
) -> str:
    """Build the tenant/environment index scope after validating both inputs.

    Legacy generic-ECS path. For a dashed tenant whose request names services
    (all plain tokens), the scope narrows to one exact pattern per service,
    ``logs-{tenant}-{service}-{environment}-*``, which cannot reach a sibling
    tenant's indexes. Otherwise it synthesizes the wildcard
    ``logs-{tenant}-*-{environment}-*``; when ``known_tenant_ids`` is supplied
    the wildcard is refused (fail closed) if another registered tenant id
    overlaps it at the dash boundary. Sibling ids are never echoed in the
    error. With no sibling list the wildcard is built exactly as before.
    Mapping-driven sources use ``resolve_index_pattern`` instead.
    """
    validate_tenant_id(tenant_id)
    validate_environment(environment)
    scoped = tuple(sorted(set(services)))
    # Why narrowing is scoped to DASHED tenants only (do not "simplify" this to
    # all tenants): index naming varies by deployment, and the per-service form
    # assumes the index's service segment equals the request's service name.
    # Where that does not hold, narrowing silently turns data into zero hits.
    # Only the dash-boundary collision risk class (tenant ids that can extend
    # one another, which requires '-' in the id) justifies changing behavior;
    # a dash-free tenant keeps the historical wildcard, and the sibling guard
    # below covers it.
    if (
        "-" in tenant_id
        and scoped
        and all(
            isinstance(service, str) and _SERVICE_INDEX_TOKEN.fullmatch(service)
            for service in scoped
        )
    ):
        return ",".join(f"logs-{tenant_id}-{service}-{environment}-*" for service in scoped)
    if known_tenant_ids is not None and dash_boundary_siblings(tenant_id, known_tenant_ids):
        raise SecurityPolicyViolationException(
            "Tenant index scope is ambiguous with another registered tenant; "
            "name the services to search or contact operations."
        )
    return f"logs-{tenant_id}-*-{environment}-*"


# Charset for operator-authored index patterns: alphanumerics, separators,
# dots, and single-segment wildcards. Commas (which would split scope),
# whitespace, and control characters are never allowed.
INDEX_PATTERN_CHARS = re.compile(r"^[A-Za-z0-9._\-*]+$")


def resolve_index_pattern(
    mapping: ObservabilitySourceMapping,
    tenant_id: str,
    environment: str,
    *,
    services: Iterable[str] = (),
    known_tenant_ids: Iterable[str] | None = None,
) -> str:
    """Comma-joined searchable index scope for one resolved mapping.

    Tenant/environment shapes are always validated (charset safety, plus the
    dash-boundary sibling guard on the legacy wildcard). Legacy-synthesis mappings fall back
    to ``build_index_pattern``; every other mapping must declare explicit
    patterns. Patterns are charset-validated as defense in depth — they are
    operator configuration, never agent input, either way.
    """
    if mapping.legacy_index_synthesis:
        return build_index_pattern(
            tenant_id, environment, services=services, known_tenant_ids=known_tenant_ids
        )
    validate_tenant_id(tenant_id)
    validate_environment(environment)
    if not mapping.index_patterns:
        raise DomainValidationException(
            f"Observability mapping {mapping.source_id!r} declares no index patterns."
        )
    for pattern in mapping.index_patterns:
        if not pattern or "," in pattern or not INDEX_PATTERN_CHARS.fullmatch(pattern):
            raise DomainValidationException(
                "Observability mapping declares an unsafe index pattern."
            )
    return ",".join(mapping.index_patterns)


def validate_identifier_items(
    identifiers: dict[str, str], mapping: ObservabilitySourceMapping
) -> list[tuple[str, str]]:
    """Exact-allowlist validation of identifier filters; returns sorted pairs.

    The original key is matched directly against the resolved mapping's
    logical identifier keys — no sanitization transform that could map
    distinct inputs to one filter. Mismatches raise
    ``DomainValidationException`` (not a scope violation) so the agent-facing
    tier keeps rendering ``INVALID_REQUEST`` for bad filter keys.
    """
    allowed = set(mapping.identifiers)
    checked: list[tuple[str, str]] = []
    for key, value in identifiers.items():
        if not isinstance(key, str) or len(key) > MAX_IDENTIFIER_KEY_LENGTH:
            raise DomainValidationException("Invalid identifier key at provider boundary.")
        if key not in allowed:
            raise DomainValidationException(f"Unauthorized identifier filter key: {key}")
        if not isinstance(value, str) or not 1 <= len(value) <= MAX_IDENTIFIER_VALUE_LENGTH:
            raise DomainValidationException("Invalid identifier value at provider boundary.")
        checked.append((key, value))
    checked.sort(key=lambda pair: pair[0])
    return checked


def validate_keyword_terms(keywords: list[str], ceilings: ProviderCeilings) -> tuple[str, ...]:
    """Reject oversized keyword input; return the canonical sorted multiset."""
    if len(keywords) > ceilings.max_keyword_terms:
        raise SecurityPolicyViolationException(
            f"Keyword terms exceed provider ceiling ({ceilings.max_keyword_terms})."
        )
    for keyword in keywords:
        if not isinstance(keyword, str) or not 1 <= len(keyword) <= MAX_KEYWORD_LENGTH:
            raise SecurityPolicyViolationException("Invalid keyword term at provider boundary.")
    return tuple(sorted(keywords))


def validate_service_terms(services: list[str], ceilings: ProviderCeilings) -> tuple[str, ...]:
    """Reject oversized service input; return the canonical sorted multiset."""
    if len(services) > ceilings.max_services:
        raise SecurityPolicyViolationException(
            f"Services exceed provider ceiling ({ceilings.max_services})."
        )
    for service in services:
        if not isinstance(service, str) or not 1 <= len(service) <= MAX_SERVICE_LENGTH:
            raise SecurityPolicyViolationException("Invalid service filter at provider boundary.")
    return tuple(sorted(services))


def validate_time_window_seconds(window_seconds: float, ceilings: ProviderCeilings) -> None:
    """Reject non-positive or over-ceiling time windows."""
    if not window_seconds > 0:
        raise SecurityPolicyViolationException("Invalid time range: end must be after start")
    if window_seconds > ceilings.max_time_window_seconds:
        raise SecurityPolicyViolationException(
            f"Requested time window ({window_seconds:.0f}s) exceeds provider ceiling "
            f"({ceilings.max_time_window_seconds}s)"
        )
