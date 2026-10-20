# src/investigation_agent_platform/application/evidence/code_recall.py
"""Structural-first code recall with a typed fuzzy fallback (D1-D5).

Design: ``docs/IAP-Cognee-integration-design.md`` decisions D1 (miss taxonomy),
D2 (gateway-internal retrieval-only fallback), D3 (authority re-verification),
D4 (orientation-only steering channel), D5 (telemetry and budget discipline).

Pure logic only: this module never imports the ``cognee`` package. The
retrieval-only guard is reused from the infrastructure adapter via a
function-local import so module import stays dependency-free. Gateway wiring
(step 4) lives in ``application/evidence/gateway.py`` and consumes the helpers
here; the orientation block enters reason context in
``application/investigation/reasoning.py`` with validator enforcement in
``application/investigation/verification.py``.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)


class CodeRecallMiss(StrEnum):
    """Closed miss taxonomy: only non-NO_MISS classes may escalate to fuzzy fallback."""

    NO_MISS = "NO_MISS"
    ZERO_EXACT_HITS = "ZERO_EXACT_HITS"
    AMBIGUOUS_SYMBOL = "AMBIGUOUS_SYMBOL"
    AMBIGUOUS_OVERLOAD = "AMBIGUOUS_OVERLOAD"
    CONCEPT_LOCATION_NO_ANCHOR = "CONCEPT_LOCATION_NO_ANCHOR"


CONCEPT_LOCATION_MARKERS: tuple[str, ...] = (
    "where is",
    "where are",
    "which service handles",
    "who owns",
    "handles rate limiting",
    "handles auth",
)


def classify_code_recall_miss(
    question_shape: str,
    exact_hits: int,
    candidate_count: int,
    problem: str,
) -> CodeRecallMiss:
    """Closed miss taxonomy: only these classes may escalate to fuzzy fallback."""
    lowered = f" {(problem or '').lower()} "
    if exact_hits > 0:
        return CodeRecallMiss.NO_MISS
    if candidate_count > 1:
        return CodeRecallMiss.AMBIGUOUS_SYMBOL
    if question_shape == "code" and any(m in lowered for m in CONCEPT_LOCATION_MARKERS):
        return CodeRecallMiss.CONCEPT_LOCATION_NO_ANCHOR
    if exact_hits == 0:
        return CodeRecallMiss.ZERO_EXACT_HITS
    return CodeRecallMiss.NO_MISS


# CYPHER is deliberately absent: it interprets NL with an LLM, so adding it
# later requires its own design note plus an explicit per-call reason.
FALLBACK_SEARCH_ORDER: tuple[str, ...] = ("CODE", "CHUNKS_LEXICAL", "CHUNKS", "SUMMARIES")
# Code constants, not env knobs (D5): budget escapes must require review.
FALLBACK_CANDIDATE_CAP = 10
FALLBACK_RUN_CAP = 3


@dataclass
class FuzzyFallbackOutcome:
    """In-memory fallback result. Carries candidate ID strings only, never payloads."""

    miss: CodeRecallMiss
    candidates_seen: int = 0
    candidate_ids: list[str] = field(default_factory=list)
    verified_node_ids: list[UUID] = field(default_factory=list)
    discarded_count: int = 0
    search_types_used: list[str] = field(default_factory=list)


async def fuzzy_fallback_candidates(
    query_text: str,
    dataset: str,
    run_used: int,
    search_fn: Callable[..., Awaitable[Sequence[Mapping[str, object]] | None]],
) -> FuzzyFallbackOutcome:
    """Gateway-internal fuzzy lookup. Returns candidate IDs only downstream.

    ``search_fn`` is an injected retrieval-only search callable so this logic is
    unit-testable without Cognee. Callers re-verify returned IDs against the
    topology authority; this function never returns symbols, only IDs.
    """
    from investigation_agent_platform.infrastructure.topology.cognee_adapter import (
        assert_retrieval_only,
    )

    outcome = FuzzyFallbackOutcome(miss=CodeRecallMiss.ZERO_EXACT_HITS)
    if run_used >= FALLBACK_RUN_CAP:
        return outcome
    seen: list[str] = []
    for search_type in FALLBACK_SEARCH_ORDER:
        assert_retrieval_only(search_type)
        results = await search_fn(query_text=query_text, search_type=search_type, dataset=dataset)
        outcome.search_types_used.append(search_type)
        for row in results or []:
            node_id = (row or {}).get("node_id")
            if node_id and str(node_id) not in seen:
                seen.append(str(node_id))
            if len(seen) >= FALLBACK_CANDIDATE_CAP:
                break
        if seen:
            break
    outcome.candidates_seen = len(seen)
    outcome.candidate_ids = seen
    return outcome


def verify_fuzzy_candidates(
    candidate_ids: list[str],
    authority_index: dict[str, object],
    tenant_id: str,
    repository_id: str,
    revision: str,
) -> tuple[list[UUID], int]:
    """Keep only IDs present in the revision-pinned authority. Returns (kept, discarded)."""
    kept: list[UUID] = []
    discarded = 0
    for raw in candidate_ids:
        node = authority_index.get(str(raw))
        if node is None:
            discarded += 1
            continue
        if (
            getattr(node, "tenant_id", None) != tenant_id
            or getattr(node, "repository_id", None) != repository_id
            or getattr(node, "revision", None) != revision
        ):
            discarded += 1
            continue
        if getattr(node, "granularity", "macro") != "macro":
            discarded += 1
            continue
        try:
            kept.append(UUID(str(raw)))
        except ValueError:
            discarded += 1
    return kept, discarded


class FuzzyOrientation(BaseModel):
    """Non-citable steering hint. Never evidence, never cited, never concluded from."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    unverified: bool = True
    miss: str = Field(..., max_length=64)
    suggested_symbols: list[str] = Field(default_factory=list, max_length=10)
    steering_note: str = Field(default="", max_length=512)


# D4 enforcement marker: evidence ``attributes`` key claiming a row's support
# traces to fuzzy orientation. Orientation itself never becomes rows, so a
# marked row is either a test seam or a future write-path bug — either way the
# validator must reject claims whose only support carries it.
FUZZY_ORIENTATION_ATTR = "fuzzy_orientation_only"


def is_orientation_traced_evidence(item: object) -> bool:
    """Whether an evidence row traces its support to fuzzy orientation (pure).

    Only the explicit marker counts: documentation and reference rows stay
    citable for doc answers, and unknown shapes fail closed (not orientation).
    """
    try:
        attrs = getattr(item, "attributes", None)
        if isinstance(attrs, Mapping) and attrs.get(FUZZY_ORIENTATION_ATTR) is True:
            return True
    except Exception:
        pass
    return False


def _authority_field(node: object, name: str) -> Any:
    """Read one field off an authority-index node (mapping or attribute)."""
    try:
        if isinstance(node, Mapping):
            return node.get(name)
        return getattr(node, name, None)
    except Exception:
        return None


def build_fuzzy_orientation(
    miss: CodeRecallMiss | str,
    verified_nodes: Sequence[object],
    steering_note: str = "",
) -> FuzzyOrientation:
    """Build the non-citable steering hint from verified authority nodes only.

    Suggested symbols come from the topology authority (qualified names of
    verified IDs), never from fuzzy payloads — only IDs crossed the
    verification boundary, so there is no fuzzy text to leak here.
    """
    names: list[str] = []
    for node in verified_nodes or []:
        name = _authority_field(node, "qualified_name")
        text = str(name or "").strip()
        if text and text not in names:
            names.append(text)
        if len(names) >= 10:
            break
    return FuzzyOrientation(
        miss=str(miss.value if isinstance(miss, CodeRecallMiss) else (miss or ""))[:64],
        suggested_symbols=names[:10],
        steering_note=str(steering_note or "")[:512],
    )
