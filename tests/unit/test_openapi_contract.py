# tests/unit/test_openapi_contract.py
"""OpenAPI contract guard (IAP OpenAPI spec 1.0 §8-i).

Every 2xx response carrying content must reference a component schema, and no
2xx schema may be an open `additionalProperties` object. `UNTYPED_ALLOWLIST`
is a ratchet, not a waiver: a route may only leave it by gaining a response
model, and any route entering it fails the suite. Shrink it router by router
per docs/IAP-implementation-openapi-spec-v1.0.md §7.
"""

from __future__ import annotations

from investigation_agent_platform.api.app import create_app

# Routes still awaiting response_model declarations. Remove entries as routers
# land; never add entries (the second assertion enforces both directions).
#
# SSE_EXEMPT covers the chat streaming route: it declares text/event-stream
# content (out of scope per the spec's Non-Goals), and FastAPI additionally
# emits a default empty application/json 200 that no JSON model applies to.
SSE_EXEMPT = frozenset({"POST /api/v1/chat/sessions/{session_id}/messages -> 200"})
UNTYPED_ALLOWLIST = frozenset(
    {
    }
)

# Subset of the above whose 2xx schema is an open additionalProperties object.
# The other four allowlisted routes are untyped but structurally closed
# (e.g. {"status": "UP"}); they need response models all the same.
OPEN_OBJECT_ALLOWLIST = UNTYPED_ALLOWLIST - frozenset(
    {
    }
)


def _iter_success_schemas(spec: dict):
    for path, ops in spec["paths"].items():
        for method, op in ops.items():
            if method not in ("get", "post", "put", "delete", "patch"):
                continue
            for code, resp in (op.get("responses") or {}).items():
                if not str(code).startswith("2"):
                    continue
                content = resp.get("content") or {}
                # Non-JSON bodies (SSE streams, websockets-adjacent) are out
                # of scope: no JSON response model applies to them.
                if "application/json" not in content:
                    continue
                schema = content["application/json"].get("schema", {})
                yield f"{method.upper()} {path} -> {code}", schema


def _is_typed(schema: dict) -> bool:
    if not isinstance(schema, dict):
        return False
    if "$ref" in schema:
        return True
    if schema.get("type") == "array":
        return "$ref" in (schema.get("items") or {})
    for key in ("anyOf", "allOf", "oneOf"):
        if key in schema:
            return True
    return False


def _is_open_object(schema: dict, components: dict, depth: int = 0) -> bool:
    if not isinstance(schema, dict) or depth > 3:
        return False
    if "$ref" in schema:
        name = schema["$ref"].split("/")[-1]
        target = (components.get("schemas") or {}).get(name, {})
        return _is_open_object(target, components, depth + 1)
    if schema.get("type") == "array":
        return _is_open_object(schema.get("items") or {}, components, depth + 1)
    for key in ("anyOf", "allOf", "oneOf"):
        if key in schema:
            return any(_is_open_object(s, components, depth + 1) for s in schema[key])
    if schema.get("type") == "object" and schema.get("additionalProperties") is True:
        return "properties" not in schema
    return False


def test_every_success_response_is_typed() -> None:
    spec = create_app().openapi()
    untyped = sorted(
        label
        for label, schema in _iter_success_schemas(spec)
        if not _is_typed(schema) and label not in SSE_EXEMPT
    )
    assert set(untyped) - UNTYPED_ALLOWLIST == set(), (
        "New untyped success responses:\n" + "\n".join(sorted(set(untyped) - UNTYPED_ALLOWLIST))
    )
    assert UNTYPED_ALLOWLIST - set(untyped) == set(), (
        "Allowlisted routes are now typed; remove them from UNTYPED_ALLOWLIST:\n"
        + "\n".join(sorted(UNTYPED_ALLOWLIST - set(untyped)))
    )


def test_no_success_response_is_an_open_object() -> None:
    spec = create_app().openapi()
    components = spec.get("components", {})
    offenders = sorted(
        label
        for label, schema in _iter_success_schemas(spec)
        if _is_open_object(schema, components) and label not in SSE_EXEMPT
    )
    assert set(offenders) - OPEN_OBJECT_ALLOWLIST == set(), (
        "New open-object success responses:\n"
        + "\n".join(sorted(set(offenders) - OPEN_OBJECT_ALLOWLIST))
    )
    assert OPEN_OBJECT_ALLOWLIST - set(offenders) == set(), (
        "Allowlisted routes are no longer open objects; remove them:\n"
        + "\n".join(sorted(OPEN_OBJECT_ALLOWLIST - set(offenders)))
    )
