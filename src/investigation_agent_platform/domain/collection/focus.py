# src/investigation_agent_platform/domain/collection/focus.py
"""Point-of-focus extraction and pivot detection (P6 2b-i, pure).

A FocusPoint is the reasoner's actionable surface for the next fetch:
a file (with optional line window) or a bare symbol. Between turns,
detect_shift names the movement (PIVOTED/REFINED/NONE) and
pivot_can_pay_off gates the recollect on liveness only — the loop is
budget-blind by design (agentic layers own budget governance); the
only question is whether turns remain for a recollect to pay off.

Pure: no I/O, no clock, never raises.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class FocusShift(StrEnum):
    """Movement between two points of focus."""

    PIVOTED = "PIVOTED"
    REFINED = "REFINED"
    NONE = "NONE"


#: Actions whose parameters carry a pivotable file surface. Disjoint by
#: construction from the committal actions (FORMULATE/VERIFY/CONCLUDE)
#: that the floor and re-read rules trigger on — a pivot can never
#: compete with those two rules, so they carry no pivot guard.
PIVOT_FILE_ACTIONS = frozenset({"GET_CODE", "GET_SOURCE", "SEARCH_CODE", "WALK_STACK"})


class FocusPoint(BaseModel):
    """One actionable collection surface (JSON-serializable, replay-safe)."""

    model_config = {"extra": "ignore"}

    surface: str = Field(min_length=1)
    surface_kind: str = "file"
    line_start: int | None = None
    line_end: int | None = None


def _clean_str(value: Any) -> str:
    try:
        return str(value or "").strip()
    except Exception:
        return ""


def _clean_line(value: Any) -> int | None:
    try:
        if isinstance(value, bool):
            return None
        line = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return line if line >= 1 else None


def focus_from_reason_data(reason_data: dict[str, Any] | None) -> FocusPoint | None:
    """Extract the reasoner's point of focus from its decided next fetch.

    GET_CODE (or any action carrying ``file_path``) yields a file focus
    with the requested window; FIND_SYMBOL (``symbol_name``) yields a
    symbol focus. Anything else yields None — no focus, no pivot.
    Never raises.
    """
    try:
        data = reason_data if isinstance(reason_data, dict) else {}
        action = _clean_str(data.get("next_action")).upper()
        params = data.get("action_parameters")
        if not isinstance(params, dict):
            params = {}
        file_path = _clean_str(params.get("file_path"))
        symbol = _clean_str(params.get("symbol_name"))
        if action == "FIND_SYMBOL" and symbol:
            return FocusPoint(surface=symbol, surface_kind="symbol")
        if file_path and action in PIVOT_FILE_ACTIONS:
            return FocusPoint(
                surface=file_path,
                surface_kind="file",
                line_start=_clean_line(params.get("start_line")),
                line_end=_clean_line(params.get("end_line")),
            )
        return None
    except Exception:
        return None


def detect_shift(
    prev: FocusPoint | None, curr: FocusPoint | None
) -> FocusShift:
    """Name the movement from prev focus to curr focus.

    No previous focus means nothing to pivot from (NONE, not PIVOTED —
    the first focus must not hijack the model's opening action). Identity
    is (kind, surface): a file focus and a symbol focus on the same token
    need different fetches (GET_CODE vs FIND_SYMBOL), so a kind change is
    a pivot, never a refinement. A changed surface is a pivot; the same
    surface with a changed window is a refinement; identical surfaces are
    no shift. Never raises.
    """
    try:
        if curr is None or prev is None:
            return FocusShift.NONE
        if (curr.surface_kind, curr.surface) != (prev.surface_kind, prev.surface):
            return FocusShift.PIVOTED
        if (curr.line_start, curr.line_end) != (prev.line_start, prev.line_end):
            return FocusShift.REFINED
        return FocusShift.NONE
    except Exception:
        return FocusShift.NONE


def pivot_can_pay_off(turns_remaining: Any) -> bool:
    """Liveness gate: a recollect costs one collection turn plus at least
    one turn to use what it banks. Non-integer or < 2 never pays off.
    Never raises."""
    try:
        if isinstance(turns_remaining, bool):
            return False
        return int(turns_remaining) >= 2  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
