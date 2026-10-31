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

from investigation_agent_platform.domain.proof.canonical import normalize_path


class FocusShift(StrEnum):
    """Movement between two points of focus."""

    PIVOTED = "PIVOTED"
    REFINED = "REFINED"
    NONE = "NONE"


#: Line numbers above this are treated as absent (a model typo, not a window).
MAX_FOCUS_LINE = 10_000_000


def canonical_surface(surface: Any, kind: str = "file") -> str:
    """Comparison identity of a focus surface (pure, total, never raises).

    A file surface is slash-normalized with the repository's path strip rules (``./``,
    ``//``, trailing slash) and keeps its case and any leading slash. A symbol surface is
    only stripped. The dispatched fetch keeps the model's own spelling; this value is used
    to decide whether two proposals name the same file, never to rewrite a request.
    """
    try:
        text = str(surface or "").strip()
    except Exception:
        return ""
    if kind != "file" or not text:
        return text
    text = text.replace("\\", "/")
    try:
        body = normalize_path(text)
    except Exception:
        return text
    return ("/" + body) if text.startswith("/") and body else body


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

    @property
    def identity(self) -> tuple[str, str]:
        """(kind, canonical surface): what makes two foci the same surface."""
        return (self.surface_kind, canonical_surface(self.surface, self.surface_kind))


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
    except (TypeError, ValueError, OverflowError):
        return None
    return line if line >= 1 else None


def _sanitized_window(start: int | None, end: int | None) -> tuple[int | None, int | None]:
    start = start if start is not None and start <= MAX_FOCUS_LINE else None
    end = end if end is not None and end <= MAX_FOCUS_LINE else None
    if start is not None and end is not None and end < start:
        end = None
    return start, end


def focus_from_reason_data(
    reason_data: dict[str, Any] | None, *, hardened: bool = False
) -> FocusPoint | None:
    """Extract the reasoner's point of focus from its decided next fetch.

    GET_CODE (or any action carrying ``file_path``) yields a file focus
    with the requested window; FIND_SYMBOL (``symbol_name``) yields a
    symbol focus. Anything else yields None — no focus, no pivot.
    An infinite line number means "no line", not "no focus" (W5-D24,
    unpinned contract repair). Under ``hardened`` (LV_W) oversized line
    numbers are absent and an inverted window drops its end (W5-D25).
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
            start, end = _clean_line(params.get("start_line")), _clean_line(params.get("end_line"))
            if hardened:
                start, end = _sanitized_window(start, end)
            return FocusPoint(
                surface=file_path,
                surface_kind="file",
                line_start=start,
                line_end=end,
            )
        return None
    except Exception:
        return None


def detect_shift(
    prev: FocusPoint | None, curr: FocusPoint | None, *, hardened: bool = False
) -> FocusShift:
    """Name the movement from prev focus to curr focus.

    No previous focus means nothing to pivot from (NONE, not PIVOTED —
    the first focus must not hijack the model's opening action). Identity
    is (kind, surface): a file focus and a symbol focus on the same token
    need different fetches (GET_CODE vs FIND_SYMBOL), so a kind change is
    a pivot, never a refinement. A changed surface is a pivot; the same
    surface with a changed window is a refinement; identical surfaces are
    no shift. Under ``hardened`` (LV_W) file surfaces compare by canonical
    identity (W5-D23); case and absolute paths stay distinct. Never raises.
    """
    try:
        if curr is None or prev is None:
            return FocusShift.NONE
        same_surface = (
            (prev.identity == curr.identity)
            if hardened
            else (curr.surface_kind, curr.surface) == (prev.surface_kind, prev.surface)
        )
        if not same_surface:
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
    except (TypeError, ValueError, OverflowError):
        return False
