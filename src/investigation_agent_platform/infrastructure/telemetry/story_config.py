# src/investigation_agent_platform/infrastructure/telemetry/story_config.py
"""Story-logging configuration loader (D9).

Resolution order: ``IAP_STORY_*`` env > ``logging.yaml`` ``story:`` section >
code defaults (mirroring ``StoryLoggingConfig``). Cached process-wide, first
use wins; ``reload_story_config()`` re-reads (tests, SIGHUP-style refresh).

Never raises: unreadable YAML, unknown levels, or malformed env values fall
back to defaults so logging can never break startup — or a Temporal sandbox
that restricts file/env access (workflow code paths hit this loader too).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

_DEFAULT_TEMPLATE = "[{comp}] {func} · {stage}/{step} · {outcome}"
_DEFAULT_FIELD_ORDER = (
    "comp",
    "func",
    "stage",
    "step",
    "outcome",
    "elapsed_ms",
    "inputs",
    "outputs",
    "error",
    "error_type",
    "tenant_id",
    "investigation_id",
    "event",
)
_DEFAULT_LEVELS = {
    "API": "INFO",
    "TMP-WRKR": "INFO",
    "ACTVTY": "INFO",
    "WFLW": "INFO",
    "SVC": "INFO",
    "DOMAIN": "INFO",
    "REPO": "DEBUG",
    "PROV": "DEBUG",
    "TOOL": "INFO",
}
_DEFAULT_CEILING = {"persist": "DEBUG"}
_DEFAULT_CAPS = {"max_keys": 20, "max_str": 1000, "max_list": 20, "max_key_len": 128}

_LEVELS = ("TRACE", "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

_CACHED: dict[str, Any] | None = None


def _parse_levels(raw: str) -> dict[str, str]:
    """Parse ``COMP=LEVEL,COMP=LEVEL`` into a validated map (unknown dropped)."""
    out: dict[str, str] = {}
    for chunk in (raw or "").split(","):
        chunk = chunk.strip()
        if "=" not in chunk:
            continue
        comp, _, level = chunk.partition("=")
        comp, level = comp.strip(), level.strip().upper()
        if comp and level in _LEVELS:
            out[comp] = level
    return out


def _load_yaml_section() -> dict[str, Any]:
    try:
        import yaml  # local import: keeps this module import-safe without pyyaml
    except Exception:
        return {}
    try:
        candidate = Path(
            os.environ.get("IAP_LOGGING_YAML", "") or (Path.cwd() / "config" / "logging.yaml")
        )
        raw = yaml.safe_load(candidate.read_text(encoding="utf-8")) or {}
        story = (raw or {}).get("story", {}) or {}
        return story if isinstance(story, dict) else {}
    except Exception:
        return {}


def load_story_config(*, refresh: bool = False) -> dict[str, Any]:
    """Return the resolved story config dict (cached unless ``refresh``)."""
    global _CACHED
    if _CACHED is not None and not refresh:
        return _CACHED
    try:
        yml = _load_yaml_section()
        cfg: dict[str, Any] = {
            "message_template": str(yml.get("message_template", _DEFAULT_TEMPLATE)),
            "field_order": list(yml.get("field_order", list(_DEFAULT_FIELD_ORDER))),
            "caps": {
                **_DEFAULT_CAPS,
                **{k: int(v) for k, v in ((yml.get("caps", {}) or {}).items())},
            },
            "component_levels": {
                **_DEFAULT_LEVELS,
                **{
                    str(k): str(v).upper()
                    for k, v in ((yml.get("component_levels", {}) or {}).items())
                },
            },
            "stage_ceiling": {
                **_DEFAULT_CEILING,
                **{
                    str(k): str(v).upper()
                    for k, v in ((yml.get("stage_ceiling", {}) or {}).items())
                },
            },
            "extra_redacted_keys": [str(k) for k in (yml.get("extra_redacted_keys", []) or [])],
        }
        # Env overrides (highest precedence).
        if os.environ.get("IAP_STORY_TEMPLATE"):
            cfg["message_template"] = os.environ["IAP_STORY_TEMPLATE"]
        for key, cap in (
            ("IAP_STORY_MAX_KEYS", "max_keys"),
            ("IAP_STORY_MAX_STR", "max_str"),
            ("IAP_STORY_MAX_LIST", "max_list"),
            ("IAP_STORY_MAX_KEY_LEN", "max_key_len"),
        ):
            if os.environ.get(key):
                try:
                    cfg["caps"][cap] = int(os.environ[key])
                except ValueError:
                    pass
        cfg["component_levels"].update(_parse_levels(os.environ.get("IAP_STORY_LEVELS", "")))
        cfg["stage_ceiling"].update(_parse_levels(os.environ.get("IAP_STORY_STAGE_CEILING", "")))
        # Validate levels; unknown → INFO.
        for table in ("component_levels", "stage_ceiling"):
            for comp, level in list(cfg[table].items()):
                if level not in _LEVELS:
                    cfg[table][comp] = "INFO"
        _CACHED = cfg
    except Exception:
        _CACHED = {
            "message_template": _DEFAULT_TEMPLATE,
            "field_order": list(_DEFAULT_FIELD_ORDER),
            "caps": dict(_DEFAULT_CAPS),
            "component_levels": dict(_DEFAULT_LEVELS),
            "stage_ceiling": dict(_DEFAULT_CEILING),
            "extra_redacted_keys": [],
        }
    return _CACHED


def reload_story_config() -> dict[str, Any]:
    """Re-read configuration (tests, operator refresh)."""
    return load_story_config(refresh=True)


def reset_story_config() -> None:
    """Drop the cache (tests)."""
    global _CACHED
    _CACHED = None
