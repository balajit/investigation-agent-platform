"""Profile assembler: fragments → ApplicationProfile dict (P4).

Pure assembly: repo parse results + merged Spring config + operator
overlay → (profile_dict, report). Derived base, operator wins on
conflict, tombstones suppress re-derivation. Refresh reuses the same
merge in reverse (new derived base + operator overlay), so operator
content is never clobbered.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any


def _slug(text: str) -> str:
    try:
        import re as _re

        slug = _re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")
        return slug[:64] or "endpoint"
    except Exception:
        return "endpoint"


def normalize_placeholders(path: str) -> tuple[str, list[str]]:
    """Normalize path placeholders across frameworks (pure).

    G4: Spring `{id}`, Flask `:id`, Django `<int:id>`/`<id>` all become
    `{id}` with the name recorded in placeholders. Query strings strip
    (params belong in allowed_query_params, populated by callers).
    """
    try:
        import re as _re

        clean = str(path or "").split("?", 1)[0]
        placeholders: list[str] = []

        def _record(match: Any) -> str:
            try:
                name = (
                    match.group("name") or match.group("bracket") or match.group("colon2") or ""
                ).strip()
                if name and name not in placeholders:
                    placeholders.append(name[:64])
                return "{" + name + "}" if name else match.group(0)
            except Exception:
                return match.group(0)

        template = _re.sub(
            r":(?P<colon2>[\w]+)|\{(?P<name>[^}/:]+)\}|<((int|str|slug|uuid|path):)?(?P<bracket>[^>/]+)>",
            _record,
            clean,
        )
        if not template.startswith("/"):
            template = "/" + template
        return template or "/", placeholders
    except Exception:
        path_text = str(path or "")
        return (path_text if path_text.startswith("/") else "/" + path_text), []


def assemble_profile(
    *,
    tenant_id: str,
    app_id: str,
    org: str,
    repo: str,
    default_branch: str = "main",
    modules: list[dict[str, Any]] | None = None,
    controllers: list[dict[str, Any]] | None = None,
    validations: list[dict[str, Any]] | None = None,
    feign_clients: list[dict[str, Any]] | None = None,
    config: dict[str, Any] | None = None,
    overridden_keys: list[str] | None = None,
    operator: dict[str, Any] | None = None,
    language: str | None = None,
    repo_type: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Assemble a DRAFT profile dict + derivation report (pure).

    Never invents: telemetry indices, connection refs, and journey keys
    stay empty with needs_review entries when unobserved. Operator
    fragment overlays last (deep merge, operator wins).
    """
    try:
        config_map = dict(config or {})
        modules = list(modules or [])
        source_roots = [str(m.get("path", "")) for m in modules if m.get("path")]
        if "." in source_roots:
            # Whole-tree root subsumes module roots (single-module repos).
            source_roots = ["."]
        build_systems = sorted(
            {str(m.get("build_system", "")) for m in modules if m.get("build_system")}
        )

        def _cfg(*names: str, default: Any = None) -> Any:
            for name in names:
                if name in config_map and config_map[name] not in (None, ""):
                    return config_map[name]
            return default

        app_name = str(_cfg("spring.application.name", default=app_id) or app_id)[:128]
        probeable = []
        for controller in controllers or []:
            try:
                if not isinstance(controller, dict):
                    continue
                for endpoint in controller.get("endpoints", []) or []:
                    method = str(endpoint.get("method", "GET") or "GET").upper()
                    if method not in ("GET", "HEAD", "POST", "PUT", "DELETE", "PATCH"):
                        continue
                    path = str(endpoint.get("path", "") or "")
                    if not path:
                        continue
                    template, placeholders = normalize_placeholders(path)
                    probeable.append(
                        {
                            "id": _slug(f"{method} {template}"),
                            "description": f"Derived {method} {template}",
                            "method": method,
                            "base_url": "",
                            "path_template": template,
                            "path_placeholders": placeholders,
                            "allowed_query_params": [],
                        }
                    )
                    if len(probeable) >= 50:
                        break
            except Exception:
                continue
        dependencies = []
        for client in feign_clients or []:
            try:
                if not isinstance(client, dict) or not client.get("name"):
                    continue
                dependencies.append({"service": str(client["name"])[:128], "critical": False})
            except Exception:
                continue
        expectation_attrs: dict[str, dict[str, dict[str, Any]]] = {}
        for validation in validations or []:
            try:
                if not isinstance(validation, dict):
                    continue
                # Group by source file: one endpoint entry per file keeps
                # attribute lists bounded (a repo-wide merge explodes past
                # the 100-attribute cap).
                group = str(validation.get("file", "") or "dto")[:256]
                bucket = expectation_attrs.setdefault(group, {})
                # Parser shape {field, constraints[]} plus legacy
                # {attributes: [...]} — both accepted.
                candidates: list[dict[str, Any]] = []
                if validation.get("field"):
                    constraints = [
                        c for c in (validation.get("constraints", []) or []) if isinstance(c, str)
                    ]
                    candidates.append(
                        {
                            "name": str(validation["field"]),
                            "required": any(
                                c in ("NotNull", "NotBlank", "NotEmpty", "Required", "Valid")
                                for c in constraints
                            ),
                        }
                    )
                for attr in validation.get("attributes", []) or []:
                    if isinstance(attr, dict) and attr.get("name"):
                        candidates.append(attr)
                for attr in candidates:
                    name = str(attr.get("name", "") or "")
                    if not name:
                        continue
                    existing = bucket.get(name, {})
                    allowed = list(attr.get("allowed_values", []) or [])
                    if allowed:
                        merged_allowed = sorted(
                            set(existing.get("allowed_values", []) or []) | set(map(str, allowed))
                        )
                        existing["allowed_values"] = merged_allowed[:20]
                    if attr.get("required", False):
                        existing["required"] = True
                    existing["name"] = name
                    bucket[name] = existing
                    if len(bucket) >= 50:
                        break
            except Exception:
                continue
        needs_review = [
            "telemetry.log_indices (never invented; declare per service)",
            "datasources[].connection_ref (TBD placeholders need secrets)",
            "services[].telemetry.session_key/tracking_key (journey identity)",
        ]
        if overridden_keys:
            needs_review.append(
                f"config-server overrides applied: {', '.join(list(overridden_keys)[:10])}"
            )
        profile: dict[str, Any] = {
            "id": app_id,
            "tenant_id": tenant_id,
            "name": app_name,
            "description": f"DRAFT generated from {org}/{repo}.",
            "environment": "development",
            "version": 1,
            "status": "DRAFT",
            "owner": "profile-generator",
            "allowed_capabilities": [
                "SEARCH_CODE",
                "FIND_SYMBOL",
                "FIND_CALL_GRAPH",
                "GET_CODE",
                "GET_CODE_HISTORY",
                "CORRELATE",
            ],
            "mapping_source_id": "generic-ecs",
            "services": [
                {
                    "name": app_name,
                    "role": "edge",
                    "telemetry": {},
                    "code": {
                        "org": org,
                        "repos": [repo],
                        "default_branch": default_branch or "main",
                        "language": language,
                        "source_roots": source_roots,
                        "build_system": build_systems[0] if build_systems else "",
                    },
                    "dependencies": dependencies[:50],
                    "probeable_endpoints": probeable[:50],
                }
            ],
            "investigation": {
                "enabledEvidenceTypes": [
                    "SOURCE_CODE",
                    "CALL_GRAPH",
                    "COMMIT_HISTORY",
                    "TRACE",
                ],
            },
            "expectations": {
                "endpoints": [
                    {"endpoint_id": group, "attributes": list(bucket.values())}
                    for group, bucket in sorted(expectation_attrs.items())
                ][:20]
            },
        }
        report = {
            "fetched_at": datetime.now(UTC).isoformat(),
            "repo_type": repo_type or "",
            "modules": len(modules),
            "controllers": len(controllers or []),
            "probeable_endpoints": len(probeable),
            "dependencies": len(dependencies),
            "expectation_attributes": sum(len(bucket) for bucket in expectation_attrs.values()),
            "needs_review": needs_review,
            "overridden_keys": list(overridden_keys or []),
        }
        merged_profile = _deep_merge(profile, operator or {})
        return merged_profile, report
    except Exception as exc:
        return (
            {
                "id": app_id,
                "tenant_id": tenant_id,
                "name": app_id,
                "status": "DRAFT",
                "services": [],
            },
            {"error": str(exc)[:300], "needs_review": ["assembly failed; manual profile required"]},
        )


def _deep_merge(base: Any, overlay: Any) -> Any:
    """Deep merge with operator-wins semantics (pure).

    Dicts recurse; lists of dicts with identifying keys (id/name/endpoint_id)
    merge by key; other lists replace only when the overlay is non-empty
    (empty overlay never wipes derived content); scalars overlay wins when
    present. Tombstone dicts ({"__tombstone": true, ...key}) remove matches.
    """
    try:
        if isinstance(overlay, dict) and isinstance(base, dict):
            out = dict(base)
            for key, value in overlay.items():
                try:
                    if isinstance(value, dict) and value.get("__tombstone") is True:
                        out.pop(key, None)
                        continue
                    if key in out:
                        out[key] = _deep_merge(out[key], value)
                    else:
                        out[key] = value
                except Exception:
                    continue
            return out
        if isinstance(overlay, list) and isinstance(base, list):
            if not overlay:
                return base
            if all(isinstance(e, dict) for e in base + overlay):
                # Tombstones processed FIRST as a set (never interleaved
                # with adds): matching base entries are removed.
                dead: set[str] = set()

                def _ident(entry: dict) -> str | None:
                    try:
                        for id_key in ("id", "name", "endpoint_id", "key"):
                            if entry.get(id_key) is not None:
                                return f"{id_key}={entry.get(id_key)}"
                    except Exception:
                        pass
                    return None

                try:
                    for entry in overlay:
                        try:
                            if entry.get("__tombstone") is True:
                                ident = _ident(entry)
                                if ident is not None:
                                    dead.add(ident)
                        except Exception:
                            continue
                except Exception:
                    pass
                merged: dict[str, dict] = {}
                order: list[str] = []
                for entry in list(base) + [e for e in overlay if not e.get("__tombstone") is True]:
                    try:
                        ident = _ident(entry)
                        if ident is None:
                            ident = f"pos-{len(order)}"
                        if ident in dead:
                            continue
                        if ident in merged:
                            merged[ident] = _deep_merge(merged[ident], entry)
                        else:
                            merged[ident] = dict(entry)
                            order.append(ident)
                    except Exception:
                        continue
                return [merged[ident] for ident in order]
            return list(overlay)
        return overlay
    except Exception:
        return overlay


def refresh_profile(
    current: dict[str, Any], fresh_derived: dict[str, Any], operator: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Non-destructive refresh (pure).

    Re-derivation forms the new base; the operator overlay re-applies on
    top, so operator-authored sections survive refresh byte-identical
    unless tombstoned. Version/status carried from current (caller bumps).
    """
    try:
        merged = _deep_merge(fresh_derived or {}, operator or {})
        if not isinstance(merged, dict):
            return dict(current or {})
        for key in ("id", "tenant_id", "version", "status"):
            try:
                if isinstance(current, dict) and current.get(key) is not None:
                    merged[key] = current[key]
            except Exception:
                continue
        return merged
    except Exception:
        return dict(current or {})
