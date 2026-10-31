# src/investigation_agent_platform/application/analysis/java/window_result.py
"""Typed result of window enumeration (J D2)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from investigation_agent_platform.domain.code.models import CodeFactResolutionV2


class WindowStatus(StrEnum):
    OK = "ok"  # analyzed; records (possibly none) are complete for the window
    DEGRADED = "degraded"  # analyzed; records are sound but known to be incomplete
    REFUSED = "refused"  # the row is not an admissible range read (a provenance gate)
    FAILED = "failed"  # the analyzer or grammar raised; nothing can be said


@dataclass(frozen=True)
class WindowAnalysis:
    status: WindowStatus
    records: tuple[CodeFactResolutionV2, ...] = ()
    limitations: tuple[str, ...] = ()
    failure_type: str | None = None  # exception class name only, never a message
    omitted: int = 0  # records dropped by the budget

    def to_attribute(self) -> dict[str, Any]:
        """JSON form spliced onto the in-memory verify-time copy of the row."""
        return {
            "status": self.status.value,
            "limitations": list(self.limitations),
            "failure_type": self.failure_type,
            "omitted": self.omitted,
        }
