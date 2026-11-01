"""Opinion-only (D6) statement identity shared by the workflow and the conclude activity (T3)."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

OPINION_STATEMENT_CAP = 10
OPINION_STATEMENT_MAX_CHARS = 512


def normalize_statement(text: Any) -> str:
    """Same join key as forcing._norm_statement: strip, casefold. Pure; never raises."""
    try:
        return str(text or "").strip().casefold()
    except Exception:
        return ""


def opinion_statements(reason_data: Any) -> list[str]:
    """Normalized, bounded, de-duplicated statements the D6 turn evaluated. Pure; never raises."""
    out: list[str] = []
    try:
        raw = (reason_data or {}).get("hypotheses_evaluated") or []
        if not isinstance(raw, list):
            return []
        for item in raw[:OPINION_STATEMENT_CAP]:
            text = (item.get("statement") or item.get("text")) if isinstance(item, dict) else item
            if not isinstance(text, str):
                continue  # a statement is a string; anything else is not evidence of D6's match
            key = normalize_statement(text)[:OPINION_STATEMENT_MAX_CHARS]
            if key and key not in out:
                out.append(key)
    except Exception:
        return []
    return out


def select_opinion_candidate(
    ranked: Sequence[Any],
    statements: Sequence[str],
    is_opinion_only: Callable[[Any], bool],
) -> Any | None:
    """The best-ranked persisted hypothesis whose statement the D6 turn evaluated and whose typed
    claims are all NOT_CHECKABLE. None when no such hypothesis was persisted. Pure; never raises.
    """
    try:
        wanted = {normalize_statement(s)[:OPINION_STATEMENT_MAX_CHARS] for s in statements or []}
        wanted.discard("")
        for hypothesis in ranked or []:
            key = normalize_statement(getattr(hypothesis, "statement", ""))[:OPINION_STATEMENT_MAX_CHARS]
            if key in wanted and is_opinion_only(hypothesis):
                return hypothesis
    except Exception:
        return None
    return None
