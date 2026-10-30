"""Expectation derivation from JSON-schema-ish dicts (D0).

Pure helpers: extract AttributeExpectations from OpenAPI-style object
schemas (properties/required/enum/minimum/maximum). Mirrors the
batch_service.validate_parameters subset — unknown keywords ignored,
never invented. Operator YAML augmentations merge over derived entries
by attribute name (operator wins).
"""

from __future__ import annotations

from typing import Any


def derive_endpoint_expectations(endpoint_id: str, schema: dict[str, Any] | None) -> dict[str, Any]:
    """Derive an EndpointExpectation-shaped dict from an object schema.

    Pure and deterministic. Returns {} when the schema carries nothing
    usable (never fabricates value sets).
    """
    try:
        if not isinstance(schema, dict):
            return {}
        properties = schema.get("properties")
        if not isinstance(properties, dict) or not properties:
            return {}
        required = schema.get("required")
        required_set = set(required) if isinstance(required, list) else set()
        attributes: list[dict[str, Any]] = []
        for name, subschema in properties.items():
            try:
                if not isinstance(name, str) or not name:
                    continue
                if not isinstance(subschema, dict):
                    continue
                entry: dict[str, Any] = {"name": name}
                if name in required_set:
                    entry["required"] = True
                enum_values = subschema.get("enum")
                if isinstance(enum_values, list):
                    allowed = [
                        str(v) for v in enum_values if isinstance(v, (str, int, float, bool))
                    ]
                    if allowed:
                        entry["allowed_values"] = allowed[:100]
                minimum = subschema.get("minimum")
                maximum = subschema.get("maximum")
                if isinstance(minimum, (int, float)) and not isinstance(minimum, bool):
                    entry["min_value"] = float(minimum)
                if isinstance(maximum, (int, float)) and not isinstance(maximum, bool):
                    entry["max_value"] = float(maximum)
                if len(entry) > 1:
                    attributes.append(entry)
            except Exception:
                continue
        if not attributes:
            return {}
        return {"endpoint_id": str(endpoint_id), "attributes": attributes}
    except Exception:
        return {}


def merge_expectations(
    derived: list[dict[str, Any]], operator: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Merge operator augmentations over derived entries (pure).

    Keyed by attribute name within each endpoint_id; operator entries win
    on conflict, derived-only entries survive. Never invents.
    """
    try:
        merged: dict[str, dict[str, dict[str, Any]]] = {}
        order: list[str] = []
        for source, wins in ((derived or [], False), (operator or [], True)):
            for endpoint in source:
                try:
                    if not isinstance(endpoint, dict):
                        continue
                    eid = str(endpoint.get("endpoint_id", "") or "")
                    if not eid:
                        continue
                    if eid not in merged:
                        merged[eid] = {}
                        order.append(eid)
                    for attr in endpoint.get("attributes", []) or []:
                        if not isinstance(attr, dict):
                            continue
                        name = str(attr.get("name", "") or "")
                        if not name:
                            continue
                        if name not in merged[eid] or wins:
                            merged[eid][name] = dict(attr)
                except Exception:
                    continue
        return [
            {"endpoint_id": eid, "attributes": [merged[eid][n] for n in merged[eid]]}
            for eid in order
        ]
    except Exception:
        return list(derived or [])
