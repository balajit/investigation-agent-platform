"""Django generator stub (G3).

Registry stub: implements the interface, returns empty. Real URLconf
(path/re_path), serializer/model-field, and settings.py parsing lands
here later with zero core changes.
"""

from __future__ import annotations

from typing import Any

REPO_TYPE = "python-django"


def controllers(text: str) -> dict[str, Any]:
    """Stub: no URLconf extraction yet."""
    try:
        _ = text
        return {"base_path": "", "endpoints": []}
    except Exception:
        return {"base_path": "", "endpoints": []}


def validations(text: str) -> list[dict[str, Any]]:
    """Stub: no serializer/model-field extraction yet."""
    try:
        _ = text
        return []
    except Exception:
        return []


def clients(text: str) -> dict[str, Any]:
    """Stub: no client extraction yet."""
    try:
        _ = text
        return {"http": []}
    except Exception:
        return {"http": []}


def config_keys(text: str, filename: str = "") -> list[str]:
    """Stub: no settings extraction yet."""
    try:
        _ = text
        _ = filename
        return []
    except Exception:
        return []
