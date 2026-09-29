# src/investigation_agent_platform/infrastructure/evidence/runtime/projection.py
"""Evidence projection from Elasticsearch hits to domain evidence (Parts 9-10).

This is the structural layer of a three-layer data-minimization chain:

1. **Projector (here, provider boundary):** decides *which* provider fields
   enter the domain object at all (explicit allowlist), redacts
   secret-bearing keys by name, and enforces size/depth bounds. Never passes
   the full ``_source`` through.
2. **Gateway sanitizer** (``SensitiveDataRedactor``): scrubs secret-shaped
   *values* (tokens, keys, PII patterns) with a redaction manifest.
3. **MCP projection** (Phase 6): selects agent-visible fields per tool.

Part 10: the allowlist, timestamp source, classification paths, and
presentation fields all resolve through the request's
``ObservabilitySourceMapping`` — no literal below names a log field (the
dotted-fold namespaces stay mechanical: they describe *shapes*, not fields).

Timestamps: a missing, unparseable, or timezone-naive observation time yields
``observed_at=None`` plus a data-quality flag — retrieval time is never
fabricated as observation time. Malformed hits raise
``MalformedProviderRecord``; the adapter skips them with an explicit
partial-result signal instead of failing the page or skipping silently.
"""

import json
import logging
import re
import unicodedata
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from investigation_agent_platform.domain.evidence.completeness import EvidenceDataQuality
from investigation_agent_platform.domain.evidence.models import (
    ClassificationLevel,
    Evidence,
    EvidenceType,
    RedactionEntry,
)
from investigation_agent_platform.domain.observability.mapping import (
    ObservabilitySourceMapping,
    SeverityStrategy,
    generic_ecs_mapping,
)
from investigation_agent_platform.domain.provenance.models import (
    EvidenceFreshness,
    EvidenceProvenance,
    QueryFingerprint,
    SourceLocation,
)
from investigation_agent_platform.infrastructure.evidence.runtime.field_resolver import (
    resolve_severity,
    resolve_timestamp,
    resolve_value,
)

logger = logging.getLogger(__name__)

REDACTED_TOKEN = "[REDACTED]"
TRUNCATED_TOKEN = "[TRUNCATED]"

# Flat dotted keys (mapping-independent shape) in these namespaces are folded
# into nested dicts so downstream readers see one canonical shape. Other
# dotted keys are dropped. Namespaces here, not field names: which top-level
# keys enter at all is the mapping's allowlist decision.
DOTTED_FOLD_NAMESPACES: frozenset[str] = frozenset(
    {
        "span",
        "service",
        "parent",
        "trace",
        "transaction",
        "labels",
        "log",
        "code",
        "db",
        "error",
        "event",
        "host",
        "container",
    }
)

# Secret-bearing key-name fragments (case-insensitive substring). Deliberately
# excludes correlation identifiers (`session`, `trace`, `span`, `user_id`)
# whose values investigations depend on.
SECRET_KEY_FRAGMENTS: tuple[str, ...] = (
    "password",
    "passwd",
    "pwd",
    "secret",
    "token",
    "auth",
    "credential",
    "private_key",
    "privatekey",
    "cookie",
    "api_key",
    "apikey",
    "access_key",
    "client_secret",
    "email",
)

MAX_STRING_LENGTH = 2000
MAX_DEPTH = 6
MAX_DICT_KEYS = 100
MAX_LIST_ITEMS = 100
MAX_ATTRIBUTES_BYTES = 16384
MAX_MANIFEST_ENTRIES = 50
SUMMARY_MAX_CHARS = 200
SNIPPET_MAX_CHARS = 2000
MESSAGE_EXCERPT_CHARS = 1000
CORE_STRING_CHARS = 200


class MalformedProviderRecord(Exception):
    """A provider hit that cannot become evidence (missing identity/shape)."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"Malformed provider record: {reason}")
        self.reason = reason


@dataclass
class _ProjectionState:
    redactions: list[RedactionEntry] = field(default_factory=list)
    truncated: bool = False

    def note_redaction(self, original_length: int) -> None:
        if len(self.redactions) < MAX_MANIFEST_ENTRIES:
            self.redactions.append(
                RedactionEntry(
                    redaction_type="SECRET_KEY_NAME",
                    original_length=original_length,
                    replacement_token=REDACTED_TOKEN,
                )
            )


def _is_secret_key(key: str) -> bool:
    lowered = key.lower()
    return any(fragment in lowered for fragment in SECRET_KEY_FRAGMENTS)


def _clean_string(value: str, state: _ProjectionState) -> str:
    if len(value) > MAX_STRING_LENGTH:
        state.truncated = True
        return value[:MAX_STRING_LENGTH]
    return value


def _clean_value(value: Any, depth: int, state: _ProjectionState) -> Any:
    if isinstance(value, str):
        return _clean_string(value, state)
    if isinstance(value, dict):
        if depth > MAX_DEPTH:
            state.truncated = True
            return TRUNCATED_TOKEN
        out: dict[str, Any] = {}
        for i, (key, nested) in enumerate(value.items()):
            if not isinstance(key, str):
                state.truncated = True
                continue
            if i >= MAX_DICT_KEYS:
                state.truncated = True
                break
            if _is_secret_key(key):
                try:
                    original_length = len(json.dumps(nested, default=str).encode("utf-8"))
                except (TypeError, ValueError):
                    original_length = 0
                state.note_redaction(original_length)
                out[key] = REDACTED_TOKEN
            else:
                out[key] = _clean_value(nested, depth + 1, state)
        return out
    if isinstance(value, (list, tuple)):
        if len(value) > MAX_LIST_ITEMS:
            state.truncated = True
        return [_clean_value(item, depth + 1, state) for item in list(value)[:MAX_LIST_ITEMS]]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _clean_string(str(value), state)


def _fold_dotted_key(attrs: dict[str, Any], key: str, value: Any) -> None:
    """Fold ``namespace.rest`` into nested dicts; existing leaves win."""
    segments = key.split(".")
    if len(segments) < 2 or segments[0] not in DOTTED_FOLD_NAMESPACES:
        return
    node = attrs.setdefault(segments[0], {})
    if not isinstance(node, dict):
        return
    for segment in segments[1:-1]:
        child = node.setdefault(segment, {})
        if not isinstance(child, dict):
            return
        node = child
    node.setdefault(segments[-1], value)


def _project_attributes(
    src: dict[str, Any], mapping: ObservabilitySourceMapping
) -> tuple[dict[str, Any], list[RedactionEntry], bool]:
    """Project the mapping allowlist as a redacted, bounded attribute view."""
    state = _ProjectionState()
    allowlist = mapping.top_level_allowlist
    attrs: dict[str, Any] = {}
    for key, value in src.items():
        if not isinstance(key, str):
            state.truncated = True
            continue
        if key in allowlist:
            attrs[key] = _clean_value(value, 1, state)
    # Second pass: fold dotted variants into the canonical nested shape.
    for key, value in src.items():
        if isinstance(key, str) and key not in allowlist and "." in key:
            _fold_dotted_key(attrs, key, _clean_value(value, 1, state))
    try:
        serialized_bytes = len(json.dumps(attrs, default=str).encode("utf-8"))
    except (TypeError, ValueError):
        serialized_bytes = MAX_ATTRIBUTES_BYTES + 1
    if serialized_bytes > MAX_ATTRIBUTES_BYTES:
        # Deterministic scalar core fallback: identity-critical short fields
        # only, so the result is always bounded no matter the input size.
        state.truncated = True
        attrs = _core_attributes(attrs, mapping)
    return attrs, state.redactions, state.truncated


def _core_set(core: dict[str, Any], dotted_path: str, value: Any) -> None:
    """Set a short scalar at a dotted path, rebuilding nested shape."""
    segments = dotted_path.split(".")
    node = core
    for segment in segments[:-1]:
        child = node.setdefault(segment, {})
        if not isinstance(child, dict):
            return
        node = child
    node[segments[-1]] = value


def _core_attributes(attrs: dict[str, Any], mapping: ObservabilitySourceMapping) -> dict[str, Any]:
    """Minimal short-scalar view used when the projection exceeds the byte cap.

    Keeps timestamp, message, service, severity, and identifier leaves —
    walked as paths so nested ECS shapes (`service.name`) and flat scalar
    shapes (`appName`) both reduce to the same small core. Every kept value
    is a short string; the result is bounded by construction.
    """
    paths: list[str] = list(mapping.timestamp.candidates[:1])
    if mapping.message is not None:
        paths.extend(mapping.message.candidates[:1])
    if mapping.service is not None:
        paths.extend(mapping.service.candidates)
    derivation = mapping.severity
    if derivation.strategy == SeverityStrategy.FIELD and derivation.field is not None:
        paths.extend(derivation.field.candidates)
    elif derivation.strategy == SeverityStrategy.ERROR_FLAG and derivation.error_field:
        paths.append(derivation.error_field)
    for key in sorted(mapping.identifiers):
        paths.extend(mapping.identifiers[key].candidates[:1])
    core: dict[str, Any] = {}
    for path in paths:
        node: Any = attrs
        for segment in path.split("."):
            if not isinstance(node, dict):
                node = None
                break
            node = node.get(segment)
        if isinstance(node, str) and node:
            _core_set(core, path, node[:CORE_STRING_CHARS])
        elif isinstance(node, (bool, int, float)):
            _core_set(core, path, node)
    return core


def _derive_classification(
    src: dict[str, Any], mapping: ObservabilitySourceMapping
) -> ClassificationLevel:
    """Provider-wide default is INTERNAL (platform convention); an explicit
    recognized level in source metadata upgrades/downgrades per record.
    Unknown values never change the default."""
    if mapping.classification is not None:
        for candidate in mapping.classification.candidates:
            node: Any = src
            for segment in candidate.split("."):
                if not isinstance(node, dict):
                    node = None
                    break
                node = node.get(segment)
            if isinstance(node, str):
                try:
                    return ClassificationLevel(node.strip().upper())
                except ValueError:
                    continue
    return ClassificationLevel.INTERNAL


def _normalize_text(raw: Any, max_chars: int, fallback: str) -> str:
    text = raw if isinstance(raw, str) else str(raw) if raw is not None else ""
    collapsed = re.sub(r"\s+", " ", text).strip()
    cleaned = "".join(
        ch for ch in collapsed if ch == " " or not unicodedata.category(ch).startswith("C")
    )
    return cleaned[:max_chars] if cleaned else fallback


def _trace_identifier(attrs: dict[str, Any], mapping: ObservabilitySourceMapping) -> str | None:
    """Best-effort trace correlation value for presentation (never identity)."""
    keys = ["trace_id"] + sorted(k for k in mapping.identifiers if k != "trace_id")
    for key in keys:
        field_mapping = mapping.identifiers.get(key)
        if field_mapping is None:
            continue
        value = resolve_value(attrs, field_mapping)
        if isinstance(value, str) and value:
            return value
    return None


@dataclass(frozen=True)
class ElasticEvidenceProjector:
    """Projects validated provider hits into domain evidence."""

    provider_id: str = "elastic-primary"

    def project(
        self,
        hit: Any,
        tenant_id: str,
        investigation_id: UUID,
        operation: str,
        query_fingerprint: str,
        mapping: ObservabilitySourceMapping | None = None,
    ) -> Evidence:
        """Validate a hit and project it; raises ``MalformedProviderRecord``.

        ``mapping`` defaults to the built-in generic-ECS contract, which
        reproduces pre-Part-10 behavior exactly.
        """
        resolved = mapping if mapping is not None else generic_ecs_mapping()
        if not isinstance(hit, dict):
            raise MalformedProviderRecord("hit_not_mapping")
        document_id = hit.get("_id")
        index = hit.get("_index")
        if not isinstance(document_id, str) or not document_id:
            raise MalformedProviderRecord("missing_document_id")
        if not isinstance(index, str) or not index:
            raise MalformedProviderRecord("missing_index")
        src = hit.get("_source", {})
        if not isinstance(src, dict):
            raise MalformedProviderRecord("source_not_mapping")

        observed_at, quality_flags = resolve_timestamp(src, resolved.timestamp)
        attributes, redactions, truncated = _project_attributes(src, resolved)
        data_quality = list(quality_flags)
        if truncated:
            data_quality.append(EvidenceDataQuality.ATTRIBUTES_TRUNCATED)
        now = datetime.now(UTC)
        classification = _derive_classification(src, resolved)
        message_value = (
            resolve_value(attributes, resolved.message) if resolved.message is not None else None
        )
        message = _normalize_text(message_value, MESSAGE_EXCERPT_CHARS, "")
        summary = _normalize_text(message_value, SUMMARY_MAX_CHARS, "Runtime log event")
        service_value = (
            resolve_value(attributes, resolved.service) if resolved.service is not None else None
        )
        service = service_value if isinstance(service_value, str) else None
        derived_severity = resolve_severity(attributes, resolved.severity)
        if derived_severity is not None:
            severity: str | None = derived_severity.value
        else:
            # Unrecognized level strings still display honestly; absence stays null.
            raw_severity = (
                resolve_value(attributes, resolved.severity.field)
                if resolved.severity.strategy == SeverityStrategy.FIELD
                and resolved.severity.field is not None
                else None
            )
            severity = raw_severity if isinstance(raw_severity, str) and raw_severity else None
        snippet_core = {
            "observed_at": observed_at.isoformat() if observed_at else None,
            "service": service,
            "severity": severity,
            "trace_id": _trace_identifier(attributes, resolved),
            "message": message,
        }
        content_snippet = json.dumps(snippet_core, default=str, sort_keys=True)[:SNIPPET_MAX_CHARS]

        # Requested and actual provider are identical by construction: routing
        # selects this adapter before it runs, so no fallback can occur inside
        # it. If fallback/routing is ever added, split these fields honestly.
        provenance = EvidenceProvenance(
            tenant_id=tenant_id,
            investigation_id=investigation_id,
            provider_type="ELASTIC",
            requested_provider_id=self.provider_id,
            actual_provider_id=self.provider_id,
            source_system="Elasticsearch",
            retrieval_timestamp=now,
            query_fingerprint=QueryFingerprint(
                provider_type="ELASTIC",
                operation=operation,
                normalized_query_hash=query_fingerprint,
            ),
            # Index-qualified: a bare `_id` is ambiguous across indices.
            source_location=SourceLocation(
                system="Elasticsearch", identifier=f"{index}/{document_id}"
            ),
        )
        freshness = EvidenceFreshness(observed_at=observed_at, retrieved_at=now)

        return Evidence(
            tenant_id=tenant_id,
            investigation_id=provenance.investigation_id,
            # Platform evidence identity: stable per provider record. This is
            # document identity, not a content hash — runtime log documents
            # are treated as immutable; if that invariant ever breaks,
            # identity and content version must be separated explicitly.
            evidence_id=uuid.uuid5(uuid.NAMESPACE_DNS, f"elastic:{index}:{document_id}"),
            evidence_type=EvidenceType.RUNTIME_LOG,
            provider="ELASTIC",
            source=f"elasticsearch://{index}/{document_id}",
            title=f"Log Record [{document_id[:8]}]",
            summary=summary,
            content_snippet=content_snippet,
            content_uri=f"elastic://{index}/{document_id}",
            # Deliberately mapping-blind document identity (not query
            # binding): the same provider record is the same evidence no
            # matter which mapping projected it. Cross-mapping safety lives
            # in the query/fetch fingerprints, which bind mapping_source_id
            # and reject stale cursors — see NormalizedRuntimeQuery.
            fingerprint=document_id,
            classification=classification,
            observed_at=observed_at,
            retrieved_at=now,
            attributes=attributes,
            provenance=provenance,
            freshness=freshness,
            data_quality=data_quality,
            is_redacted=bool(redactions),
            redaction_manifest=redactions,
        )
