# src/investigation_agent_platform/application/reporting/artifacts.py
"""Aggregate-report artifact layout (Part 11.8).

Layout (logical keys; the adapter confines tenant/application prefixes)::

    aggregate-reports/latest/{scope}/aggregate-report.html (+ .json sidecar)
    aggregate-reports/history/{scope}/{job_id}/aggregate-report.html (+ .json)

``scope`` is an opaque digest of the tenant — never the raw tenant id —
so keys carry no attributable identifiers. Clients receive bytes through
the authorized API or short-lived signed URLs, never raw storage keys.
Retention is enforced by job creation time (refs carry no timestamps):
history runs whose job is older than the retention window are purged;
unattributable keys are never deleted.
"""

from __future__ import annotations

import hashlib
from typing import Any
from uuid import UUID, uuid4

from investigation_agent_platform.domain.common.extension import CapabilityScope
from investigation_agent_platform.ports.artifacts.store import (
    ArtifactObject,
    ArtifactPage,
    ArtifactRef,
)

_HTML_SUFFIX = "aggregate-report.html"
_JSON_SUFFIX = "aggregate-report.json"


def scope_id_for_tenant(tenant_id: str) -> str:
    """Opaque internal scope id; stable per tenant, reveals nothing."""
    return hashlib.sha256(f"aggregate-report:{tenant_id}".encode()).hexdigest()[:32]


def report_scope(tenant_id: str) -> CapabilityScope:
    return CapabilityScope(tenant_id=tenant_id)


def latest_keys(scope_id: str) -> tuple[str, str]:
    base = f"aggregate-reports/latest/{scope_id}"
    return f"{base}/{_HTML_SUFFIX}", f"{base}/{_JSON_SUFFIX}"


def history_keys(scope_id: str, job_id: UUID) -> tuple[str, str]:
    base = f"aggregate-reports/history/{scope_id}/{job_id}"
    return f"{base}/{_HTML_SUFFIX}", f"{base}/{_JSON_SUFFIX}"


def history_prefix(scope_id: str) -> str:
    return f"aggregate-reports/history/{scope_id}/"


def ref_for_key(key: str, content_type: str, size: int) -> ArtifactRef:
    return ArtifactRef(
        artifact_id=uuid4(),
        key=key,
        content_digest="",
        content_type=content_type,
        size_bytes=size,
        retention_class="reports",
    )


async def store_report_run(
    store: Any,
    tenant_id: str,
    job_id: UUID,
    html: bytes,
    manifest_json: bytes,
    legal_hold: bool = False,
) -> dict[str, ArtifactRef]:
    """Write history + latest artifacts for one report run."""
    scope = report_scope(tenant_id)
    scope_id = scope_id_for_tenant(tenant_id)
    metadata = {"legal_hold": "true" if legal_hold else "false", "job_id": str(job_id)}
    html_key, json_key = history_keys(scope_id, job_id)
    refs: dict[str, ArtifactRef] = {
        "history_html": await store.put(
            scope, html_key, html, "text/html; charset=utf-8", metadata
        ),
        "history_json": await store.put(
            scope, json_key, manifest_json, "application/json", metadata
        ),
    }
    latest_html_key, latest_json_key = latest_keys(scope_id)
    refs["latest_html"] = await store.put(
        scope, latest_html_key, html, "text/html; charset=utf-8", metadata
    )
    refs["latest_json"] = await store.put(
        scope, latest_json_key, manifest_json, "application/json", metadata
    )
    return refs


async def read_latest(store: Any, tenant_id: str) -> ArtifactObject | None:
    """Fetch the latest HTML artifact, or None when no report exists yet."""
    scope = report_scope(tenant_id)
    key, _ = latest_keys(scope_id_for_tenant(tenant_id))
    try:
        result: ArtifactObject = await store.get(
            scope, ref_for_key(key, "text/html; charset=utf-8", 0)
        )
        return result
    except Exception:
        return None


async def list_history(store: Any, tenant_id: str, cursor: str | None, limit: int) -> ArtifactPage:
    scope = report_scope(tenant_id)
    page: ArtifactPage = await store.list(
        scope, history_prefix(scope_id_for_tenant(tenant_id)), cursor, limit
    )
    return page


def job_id_from_history_key(key: str) -> UUID | None:
    """Extract the job id from a history key, or None when unattributable."""
    parts = key.strip("/").split("/")
    try:
        return UUID(parts[3])
    except (IndexError, ValueError):
        return None
