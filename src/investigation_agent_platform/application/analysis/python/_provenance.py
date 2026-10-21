# src/investigation_agent_platform/application/analysis/python/_provenance.py
"""Revision-pinned source acquisition for deterministic Python analysis.

Mirrors ``application/analysis/java/_provenance.py``: only range-pinned
source reads at an immutable revision, with contiguous, ordered numbered
lines. Gaps, duplicates, or out-of-order lines fail closed — offsets must
never be computed across missing lines.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from itertools import pairwise
from typing import Any

from investigation_agent_platform.domain.evidence.models import EvidenceType

_RANGE_OPERATIONS = frozenset({"GET_CODE", "GET_SOURCE", "READ_FILE"})
_UNPINNED_REVISIONS = frozenset({"HEAD", "UNPINNED"})
_LINE_RE = re.compile(r"^\s*(\d+)\| ?(.*)$")


@dataclass(frozen=True)
class ParsedLine:
    number: int
    text: str


def python_source_lines(evidence: Any) -> tuple[list[ParsedLine], str | None]:
    """Return verified numbered source lines or a fail-closed reason."""
    provenance = getattr(evidence, "provenance", None)
    fingerprint = getattr(provenance, "query_fingerprint", None)
    location = getattr(provenance, "source_location", None)
    attrs = getattr(evidence, "attributes", None) or {}
    operation = str(getattr(fingerprint, "operation", "") or "").upper()
    revision = str(getattr(location, "revision", "") or "")
    start = getattr(location, "line_start", None)
    end = getattr(location, "line_end", None)
    if (
        str(getattr(getattr(evidence, "evidence_type", None), "value", "")).upper()
        != EvidenceType.SOURCE_CODE.value
    ):
        return [], "not_source_code_evidence"
    if operation not in _RANGE_OPERATIONS:
        return [], "not_a_source_read"
    if not isinstance(attrs, dict) or attrs.get("read_mode") not in (None, "range"):
        return [], "source_read_not_range_pinned"
    if attrs.get("discovery_only"):
        return [], "source_read_is_discovery_only"
    if not revision or revision in _UNPINNED_REVISIONS or not getattr(location, "file_path", None):
        return [], "missing_immutable_source_provenance"
    if not isinstance(start, int) or not isinstance(end, int) or start < 1 or end < start:
        return [], "missing_source_line_window"
    if attrs.get("truncated"):
        return [], "source_range_truncated"

    parsed: list[ParsedLine] = []
    for raw in str(getattr(evidence, "content_snippet", "") or "").splitlines():
        match = _LINE_RE.match(raw)
        if not match:
            return [], "source_range_missing_numbered_lines"
        parsed.append(ParsedLine(number=int(match.group(1)), text=match.group(2)))
    if not parsed or parsed[0].number < start or parsed[-1].number > end:
        return [], "source_lines_outside_provenance_window"
    numbers = [line.number for line in parsed]
    if any(b - a != 1 for a, b in pairwise(numbers)):
        return [], "source_lines_noncontiguous"
    return parsed, None


def repository_of(identifier: str, source_path: str) -> str:
    """Derive the repository identity from a location identifier and file path."""
    if source_path and identifier.endswith("/" + source_path):
        return identifier[: -(len(source_path) + 1)]
    if identifier == source_path:
        return ""
    return identifier
