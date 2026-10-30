# src/investigation_agent_platform/domain/analysis/hub_memory.py
"""Hub lookup + orientation + verify corroboration (Phase 2 hooks, §§4-5).

Pure helpers the reason loop and state-claim validation call. Memory
suggests, evidence decides: orientation only, above threshold; the validator
still demands live citations.
"""

from __future__ import annotations


def lookup_hubs(profile_hubs: list[dict], characteristic: str, *, limit: int = 5) -> list[dict]:
    """Characteristic→hub walk: substring match over hub names (deterministic)."""
    needle = characteristic.strip().lower()
    if not needle:
        return []
    scored = []
    for h in profile_hubs:
        name = str(h.get("name", ""))
        if needle in name.lower():
            scored.append(h)
    scored.sort(key=lambda h: (-float(h.get("score", 0.0)), str(h.get("name", ""))))
    return scored[:limit]


def hub_orientation_block(hubs: list[dict], associations: list[dict] | None = None) -> dict:
    """Reason orientation block: hub list + association notes beside refdoc orientation."""
    return {
        "type": "hub_orientation",
        "hubs": [
            {"name": h.get("name"), "score": h.get("score"), "witnesses": h.get("witnesses", [])}
            for h in hubs
        ],
        "associations": list(associations or [])[:5],
    }


def hub_membership_corroborates(hub_names: list[str], cited_tables: list[str]) -> bool:
    """Verify corroboration: cited state tables fall inside known hubs."""
    hubs = {h.strip().lower() for h in hub_names if h.strip()}
    tables = {t.strip().lower() for t in cited_tables if t.strip()}
    return bool(hubs & tables)
