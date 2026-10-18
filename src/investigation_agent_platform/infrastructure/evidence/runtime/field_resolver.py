# src/investigation_agent_platform/infrastructure/evidence/runtime/field_resolver.py
"""Pure field resolution against operator-authored mappings (Part 10).

Every function here takes an explicit `FieldMapping` (or derivation) and a
raw provider `_source` dict. No globals, no registry access, no I/O — so the
whole module is unit-testable against fixture documents without a cluster.
Candidate semantics everywhere: first present-and-non-empty path wins; values
are never merged across candidates. `0` and `False` are legitimate values
and are never treated as absent.
"""

from datetime import UTC, datetime
from typing import Any

from investigation_agent_platform.domain.evidence.completeness import EvidenceDataQuality
from investigation_agent_platform.domain.evidence.models import LogSeverity
from investigation_agent_platform.domain.observability.mapping import (
    FieldMapping,
    FieldValueType,
    SeverityDerivation,
    SeverityStrategy,
)


def _traverse(source: Any, path: str) -> Any:
    """Walk a dotted path; any non-dict hop or missing key fails the path."""
    node = source
    for segment in path.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(segment)
    return node


def _is_absent(value: Any) -> bool:
    """Absent means None, empty string, or empty container — never 0/False."""
    if value is None:
        return True
    if isinstance(value, str) and not value:
        return True
    return isinstance(value, (dict, list)) and not value


def resolve_value(source: dict[str, Any], mapping: FieldMapping) -> Any | None:
    """First present-and-non-empty candidate value, or None."""
    for candidate in mapping.candidates:
        value = _traverse(source, candidate)
        if not _is_absent(value):
            return value
    return None


def resolve_timestamp(
    source: dict[str, Any], mapping: FieldMapping
) -> tuple[datetime | None, list[EvidenceDataQuality]]:
    """Parse the first present timestamp candidate per its declared type.

    A present-but-unparseable value reports INVALID (never falls through to
    a later candidate — the value was there, it was just bad). Nothing
    present reports MISSING. Retrieval time is never substituted; that
    invariant lives with the caller.
    """
    for candidate in mapping.candidates:
        raw = _traverse(source, candidate)
        if raw is None:
            # Truly absent: fall through to the next candidate. Present but
            # empty or malformed values below are INVALID, never skipped —
            # a present value claims the field, even when it parses to nothing.
            continue
        value_type = mapping.value_type
        if value_type == FieldValueType.DATE_ISO:
            if not isinstance(raw, str) or not raw.strip():
                return None, [EvidenceDataQuality.TIMESTAMP_INVALID]
            try:
                parsed = datetime.fromisoformat(raw.strip())
            except ValueError:
                return None, [EvidenceDataQuality.TIMESTAMP_INVALID]
            if parsed.tzinfo is None:
                return None, [EvidenceDataQuality.TIMESTAMP_NAIVE]
            return parsed, []
        if value_type in (FieldValueType.DATE_EPOCH_MILLIS, FieldValueType.DATE_EPOCH_SECONDS):
            if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                return None, [EvidenceDataQuality.TIMESTAMP_INVALID]
            seconds = raw / 1000.0 if value_type == FieldValueType.DATE_EPOCH_MILLIS else raw
            try:
                return datetime.fromtimestamp(seconds, tz=UTC), []
            except (OverflowError, OSError, ValueError):
                return None, [EvidenceDataQuality.TIMESTAMP_INVALID]
        return None, [EvidenceDataQuality.TIMESTAMP_INVALID]
    return None, [EvidenceDataQuality.TIMESTAMP_MISSING]


def resolve_severity(source: dict[str, Any], derivation: SeverityDerivation) -> LogSeverity | None:
    """Derive the domain severity, or None when the source cannot say."""
    if derivation.strategy == SeverityStrategy.UNSUPPORTED:
        return None
    if derivation.strategy == SeverityStrategy.FIELD:
        if derivation.field is None:
            return None
        raw = resolve_value(source, derivation.field)
        if not isinstance(raw, str):
            return None
        try:
            return LogSeverity(raw.strip().upper())
        except ValueError:
            return None
    # ERROR_FLAG: project the boolean signal onto the configured pair.
    raw = _traverse(source, derivation.error_field or "")
    if isinstance(raw, bool):
        flag: bool | None = raw
    elif isinstance(raw, (int, float)):
        flag = raw != 0
    elif isinstance(raw, str):
        lowered = raw.strip().lower()
        if lowered in ("true", "1", "yes"):
            flag = True
        elif lowered in ("false", "0", "no"):
            flag = False
        else:
            return None
    else:
        return None
    return derivation.true_severity if flag else derivation.false_severity


def resolve_identifier_field(logical_key: str, mapping: FieldMapping) -> str | None:
    """Physical ES field path to filter a logical identifier key on.

    TEXT-typed candidates are keyword-subfield-qualified (exact-match terms
    against analyzed text never match); every other type is used as-is.
    Only the first candidate is used: term filters name one field, so mapping
    authors order candidates by prevalence. A document carrying the value
    solely under a later candidate will not match — restrictive by design
    (missed evidence surfaces as empty results, never as wrong results).
    Returns None only when the mapping has no candidates (defensive; the
    model requires at least one).
    """
    if not mapping.candidates:
        return None
    first = mapping.candidates[0]
    if mapping.value_type == FieldValueType.TEXT:
        return f"{first}.keyword"
    return first
