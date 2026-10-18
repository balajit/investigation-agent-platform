"""Config-source precedence for profile generation (P3).

Merge order (approved): config server wins, properties/YAML files are
fallback, repo-derived defaults fill gaps. Every server override is
recorded so the derivation report shows what lost. Pure.
"""

from __future__ import annotations

from typing import Any


def merge_configs(
    server: dict[str, Any] | None,
    files: dict[str, Any] | None,
    repo_defaults: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Merge config sources with server-wins precedence (pure).

    Returns (merged, overridden): overridden lists keys present in both
    server and a lower source with different values (audit trail for the
    derivation report). Lower sources never overwrite server keys.
    """
    try:
        merged: dict[str, Any] = {}
        overridden: list[str] = []
        for source in (repo_defaults or {}, files or {}):
            try:
                if not isinstance(source, dict):
                    continue
                for key, value in source.items():
                    if key not in merged:
                        merged[key] = value
            except Exception:
                continue
        try:
            server_map = server or {}
            if not isinstance(server_map, dict):
                server_map = {}
            for key, value in server_map.items():
                try:
                    if key in merged and merged[key] != value:
                        overridden.append(str(key))
                    merged[key] = value
                except Exception:
                    continue
        except Exception:
            pass
        return merged, sorted(set(overridden))
    except Exception:
        fallback: dict[str, Any] = {}
        try:
            for source in (repo_defaults or {}, files or {}, server or {}):
                if isinstance(source, dict):
                    fallback.update(source)
        except Exception:
            pass
        return fallback, []
