"""Relationship-map merge + session template materialization (G-3).

Pure helpers: merge derived FK edges with operator augmentations
(tombstones suppress, operator wins on key conflict), and materialize
auto-derived session join templates into a profile dict so the loop
uses them like any operator template (validator-safe by construction:
SELECT-only, equality binds, allowlisted tables).
"""

from __future__ import annotations

from typing import Any


def _edge_key(edge: dict[str, Any]) -> tuple[str, str, str, str]:
    try:
        return (
            str(edge.get("from_entity", "")),
            str(edge.get("from_key", "")),
            str(edge.get("to_entity", "")),
            str(edge.get("to_key", "")),
        )
    except Exception:
        return ("", "", "", "")


def merge_relationships(
    derived: list[dict[str, Any]], operator: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Merge operator augmentations over derived FK edges (pure).

    Tombstoned operator entries suppress the matching derived edge
    (re-derivation will not resurrect them). Operator edges win on key
    conflict with source marked operator. Deterministic order: derived
    first, then operator-only additions.
    """
    try:
        tombstones = set()
        for entry in operator or []:
            try:
                if isinstance(entry, dict) and entry.get("tombstoned") is True:
                    tombstones.add(_edge_key(entry))
            except Exception:
                continue
        merged: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        for entry in derived or []:
            try:
                if not isinstance(entry, dict):
                    continue
                key = _edge_key(entry)
                if key in tombstones or key == ("", "", "", ""):
                    continue
                merged.setdefault(key, {**entry, "source": "derived"})
            except Exception:
                continue
        for entry in operator or []:
            try:
                if not isinstance(entry, dict):
                    continue
                key = _edge_key(entry)
                if key == ("", "", "", ""):
                    continue
                if entry.get("tombstoned") is True:
                    merged.pop(key, None)
                    continue
                merged[key] = {**entry, "source": "operator"}
            except Exception:
                continue
        return [merged[key] for key in merged]
    except Exception:
        return list(derived or [])


def _hub_key(hub: dict[str, Any]) -> str:
    try:
        return str(hub.get("name", "")).strip().lower()
    except Exception:
        return ""


def merge_domain_hubs(
    derived: list[dict[str, Any]], operator: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Merge operator hub augmentations over derived hub rankings (pure).

    Operator hubs win on name conflict; derived order is score-descending.
    Deterministic: derived sorted by (-score, name), operator additions appended.
    """
    try:
        merged: dict[str, dict[str, Any]] = {}
        ranked = sorted(
            (h for h in (derived or []) if isinstance(h, dict) and _hub_key(h)),
            key=lambda h: (-float(h.get("score", 0.0) or 0.0), str(h.get("name", ""))),
        )
        for hub in ranked:
            merged.setdefault(_hub_key(hub), {**hub, "source": "derived"})
        for hub in operator or []:
            try:
                if not isinstance(hub, dict) or not _hub_key(hub):
                    continue
                merged[_hub_key(hub)] = {**hub, "source": "operator"}
            except Exception:
                continue
        return [merged[k] for k in merged]
    except Exception:
        return list(derived or [])


def materialize_session_templates(profile: dict[str, Any]) -> dict[str, Any]:
    """Generate session join templates into datasources (pure).

    For each datasource with session_entity + session_key + relationships:
    emits `auto_session_anchor` (anchor rows by session key) and
    `auto_session_hop_<n>` (one equality join per merged edge). Generated
    names never collide with operator templates (auto_ prefix reserved;
    operator template of the same name is left untouched). Returns a new
    dict; input unmodified.
    """
    try:
        import copy

        out = copy.deepcopy(profile)
        datasources = out.get("datasources", None)
        if not isinstance(datasources, list):
            return out
        for ds in datasources:
            try:
                if not isinstance(ds, dict):
                    continue
                session_entity = str(ds.get("session_entity", "") or "")
                session_key = str(ds.get("session_key", "") or "")
                if not session_entity or not session_key:
                    continue
                templates = ds.get("query_templates", None)
                if not isinstance(templates, dict):
                    templates = {}
                    ds["query_templates"] = templates
                anchor_name = "auto_session_anchor"
                if anchor_name not in templates:
                    templates[anchor_name] = {
                        "description": (
                            "Auto-derived (G-3): anchor rows for a session."
                        ),
                        "sql": (
                            f"SELECT * FROM {session_entity} "
                            f"WHERE {session_key} = :session_id"
                        ),
                        "read_only": True,
                    }
                edges = merge_relationships([], ds.get("relationships", []) or [])
                for index, edge in enumerate(edges):
                    name = f"auto_session_hop_{index}"
                    if name in templates:
                        continue
                    templates[name] = {
                        "description": (
                            "Auto-derived (G-3): one-hop join "
                            f"{edge.get('from_entity')}.{edge.get('from_key')} "
                            f"= {edge.get('to_entity')}.{edge.get('to_key')}."
                        ),
                        "sql": (
                            f"SELECT t.* FROM {edge.get('to_entity')} t "
                            f"JOIN {edge.get('from_entity')} f "
                            f"ON t.{edge.get('to_key')} = f.{edge.get('from_key')} "
                            f"WHERE f.{edge.get('from_key')} = :parent_key"
                        ),
                        "read_only": True,
                    }
            except Exception:
                continue
        return out
    except Exception:
        return profile
