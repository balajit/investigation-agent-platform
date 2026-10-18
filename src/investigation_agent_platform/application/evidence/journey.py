"""Journey spine construction (G-1).

Pure helpers: group flat log/trace row dicts into an ordered session →
tracking-ID spine. Mapping-agnostic: callers extract (tracking_id,
service, timestamp, status) per row using the profile's declared field
names; this module only orders and structures. Never raises.
"""

from __future__ import annotations

from typing import Any


def _text(value: Any) -> str:
    try:
        if value is None:
            return ""
        return str(value)[:256]
    except Exception:
        return ""


def build_journey_spine(
    rows: list[dict[str, Any]],
    *,
    session_id: str = "",
    max_steps: int = 20,
    max_services_per_step: int = 10,
) -> dict[str, Any]:
    """Order rows into tracking-ID steps (pure, deterministic).

    Input rows carry tracking_id/service/timestamp/status keys (already
    extracted by the caller). Steps sort by earliest timestamp; services
    within a step sort by name; everything capped. Rows without a
    tracking ID collect under "" (unattributed) and sort last.
    """
    try:
        steps: dict[str, dict[str, Any]] = {}
        order: list[str] = []
        for row in rows or []:
            try:
                if not isinstance(row, dict):
                    continue
                tracking = _text(row.get("tracking_id", ""))
                service = _text(row.get("service", "")) or "unknown"
                timestamp = _text(row.get("timestamp", ""))
                status = _text(row.get("status", ""))
                if tracking not in steps:
                    if len(order) >= max_steps and tracking != "":
                        continue
                    steps[tracking] = {
                        "tracking_id": tracking,
                        "services": [],
                        "service_set": set(),
                        "first_seen": timestamp,
                        "last_seen": timestamp,
                        "row_count": 0,
                        "error_count": 0,
                    }
                    order.append(tracking)
                step = steps[tracking]
                step["row_count"] += 1
                if timestamp:
                    if not step["first_seen"] or timestamp < step["first_seen"]:
                        step["first_seen"] = timestamp
                    if not step["last_seen"] or timestamp > step["last_seen"]:
                        step["last_seen"] = timestamp
                if status and "error" in status.lower():
                    step["error_count"] += 1
                if service not in step["service_set"]:
                    if len(step["services"]) < max_services_per_step:
                        step["services"].append(service)
                    step["service_set"].add(service)
            except Exception:
                continue
        result = []
        for tracking in order:
            step = steps[tracking]
            result.append(
                {
                    "tracking_id": step["tracking_id"],
                    "services": sorted(step["services"]),
                    "first_seen": step["first_seen"],
                    "last_seen": step["last_seen"],
                    "row_count": step["row_count"],
                    "error_count": step["error_count"],
                }
            )
        result.sort(
            key=lambda s: (s["tracking_id"] == "", s["first_seen"] or "~~", s["tracking_id"])
        )
        return {"session_id": _text(session_id), "steps": result[:max_steps]}
    except Exception:
        return {"session_id": _text(session_id), "steps": []}
