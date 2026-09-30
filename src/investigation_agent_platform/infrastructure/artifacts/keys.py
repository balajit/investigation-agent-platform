# src/investigation_agent_platform/infrastructure/artifacts/keys.py
"""Artifact key construction and validation (Part 11.3A shared).

Adapters build ``{tenant_id}/{application_id}/{key}`` internally; callers
supply only logical keys. Absolute paths, ``..`` segments, backslashes, and
empty segments are rejected by every implementation.
"""

from __future__ import annotations


def build_object_key(tenant_id: str, application_id: str | None, key: str) -> str:
    """Build the storage key; raises ValueError on traversal/absolute keys."""
    _validate_segment("tenant_id", tenant_id)
    app = application_id or "-"
    _validate_segment("application_id", app)
    _validate_key(key)
    return f"{tenant_id}/{app}/{key}"


def _validate_segment(name: str, value: str) -> None:
    if not value or value in (".", ".."):
        raise ValueError(f"invalid {name}: {value!r}")
    if "/" in value or "\\" in value or "\x00" in value:
        raise ValueError(f"invalid {name}: {value!r}")


def _validate_key(key: str) -> None:
    if not key or key.startswith("/") or "\\" in key or "\x00" in key:
        raise ValueError(f"invalid artifact key: {key!r}")
    for segment in key.split("/"):
        if segment in ("", ".", ".."):
            raise ValueError(f"invalid artifact key: {key!r}")


MAX_ARTIFACT_BYTES = 500_000_000
MAX_METADATA_ENTRIES = 20
