# src/investigation_agent_platform/domain/common/idempotency.py
"""Scoped idempotency key mapping (Part 11.3D).

Generalizes durable idempotency records from ``(tenant_id, key)`` to
``(scope, operation, key)``. The default operation with no application id
maps to the bare key verbatim — existing callers keep byte-identical storage
keys, so in-flight reservations survive the upgrade.
"""

from __future__ import annotations


def scoped_idempotency_key(
    key: str,
    operation: str = "default",
    application_id: str | None = None,
) -> str:
    """Map (operation, application_id, key) to one storage key."""
    if not key:
        raise ValueError("idempotency key must not be empty")
    if operation == "default" and not application_id:
        return key
    return f"{operation}:{application_id or '-'}:{key}"
