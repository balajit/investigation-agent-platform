# src/investigation_agent_platform/application/analysis/summary.py
"""Analyzer outcome summarizer (J10 D20, single home).

Reduces any number of analyzer payloads to scalar counts by status and
limitation class. Records no values, messages or identifiers, so it is
safe in Temporal history and replay-stable.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from typing import Any

from investigation_agent_platform.application.analysis.limitations import limitation_class


def summarize_analysis(payloads: Iterable[Mapping[str, Any] | None]) -> dict[str, int] | None:
    """Counts by status and limitation class over analyzer payloads; None when none ran."""
    status: Counter[str] = Counter()
    classes: Counter[str] = Counter()
    for raw in payloads:
        if not isinstance(raw, Mapping):
            continue
        state = str(raw.get("status", "") or "").upper()
        if state not in {"RESOLVED", "PARTIAL", "UNRESOLVED"}:
            continue
        status[state.lower()] += 1
        codes = raw.get("limitation_codes") or ()
        if isinstance(codes, str):
            codes = (codes,)
        try:
            items = list(codes)
        except TypeError:
            continue
        for code in items:
            classes[limitation_class(str(code)).value] += 1
    if not status:
        return None
    return {k: status.get(k, 0) for k in ("resolved", "partial", "unresolved")} | {
        k: classes.get(k, 0)
        for k in ("window", "runtime", "unsupported", "failure", "provenance")
    }
