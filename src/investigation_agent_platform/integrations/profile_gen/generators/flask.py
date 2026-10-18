"""Flask generator stub (G3).

Registry stub: implements the interface, returns empty. Real route
(Blueprint/url_prefix), WTForms, and requests parsing lands here later
with zero core changes.
"""

from __future__ import annotations

from typing import Any

REPO_TYPE = "python-flask"


def controllers(text: str) -> dict[str, Any]:
    """Stub: no Flask route extraction yet."""
    try:
        _ = text
        return {"base_path": "", "endpoints": []}
    except Exception:
        return {"base_path": "", "endpoints": []}


def validations(text: str) -> list[dict[str, Any]]:
    """Stub: no WTForms extraction yet."""
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
    """Stub: no config extraction yet."""
    try:
        _ = text
        _ = filename
        return []
    except Exception:
        return []
