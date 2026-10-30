# src/investigation_agent_platform/api/v1/routers/integrations.py
"""Source-control webhooks: push events refresh topology projections (cognee pilot).

POST /api/v1/integrations/github/{tenant_id}/webhook accepts GitHub `push`
events, verifies the HMAC signature (`X-Hub-Signature-256` against
`IAP_GITHUB_WEBHOOK_SECRET`, fail-closed when unconfigured), extracts the
impacted file set, and dispatches `TopologyProjectionWorkflow`. Other event
types ack ignored. The projection rebuilds the full payload for the pushed
revision (edge resolution spans files — partial projection would corrupt
CALLS); the impacted set is recorded for future incremental work.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
from typing import Any

from fastapi import APIRouter, Depends, Header, Request, status
from pydantic import BaseModel, ConfigDict

from investigation_agent_platform.api.dependencies import get_app_context
from investigation_agent_platform.api.errors import ApiError, sanitize_extra
from investigation_agent_platform.api.tenant import require_tenant
from investigation_agent_platform.api.v1.schemas.errors import COMMON_ERROR_RESPONSES

logger = logging.getLogger(__name__)

router = APIRouter(tags=["integrations"])

_ZERO_SHA = "0" * 40
_MAX_CHANGED_FILES = 500
_MAX_PATH_CHARS = 1024


class GitHubPushAck(BaseModel):
    """Webhook receipt: what was accepted and what will run."""

    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    repository: str
    revision: str
    ref: str
    changed_files: int
    workflow_id: str | None
    status: str


def _webhook_secret() -> str:
    return os.environ.get("IAP_GITHUB_WEBHOOK_SECRET", "")


def _verify_signature(secret: str, body: bytes, signature: str | None) -> bool:
    """Constant-time HMAC-SHA256 check over the raw body bytes."""
    if not secret or not signature:
        return False
    prefix = "sha256="
    if not signature.startswith(prefix):
        return False
    try:
        expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature[len(prefix) :])
    except Exception:
        return False


def _repository_id_for(full_name: str) -> str:
    """Slug convention shared with scripts/ingest_github.repository_id_for."""
    slug = (full_name or "").lower().replace("/", "--")
    safe = "".join(c if (c.isalnum() or c in "-_.") else "-" for c in slug)[:120]
    return safe


def _changed_files(payload: dict[str, Any]) -> list[str]:
    """Union of added/modified/removed paths across push commits (bounded)."""
    seen: list[str] = []
    seen_set: set[str] = set()
    for commit in payload.get("commits", []) or []:
        if not isinstance(commit, dict):
            continue
        for key in ("added", "modified", "removed"):
            paths = commit.get(key) or []
            if not isinstance(paths, list):
                continue
            for path in paths:
                if not isinstance(path, str) or not path:
                    continue
                candidate = path[:_MAX_PATH_CHARS]
                if candidate not in seen_set:
                    seen_set.add(candidate)
                    seen.append(candidate)
                if len(seen) >= _MAX_CHANGED_FILES:
                    return seen
    # Head-commit fallback when the commits array is empty (e.g. tag pushes
    # GitHub still labels push): compare endpoint is out of scope here.
    return seen


@router.post(
    "/integrations/github/{tenant_id}/webhook",
    response_model=GitHubPushAck,
    response_model_by_alias=True,
    response_model_exclude_unset=True,
    responses=COMMON_ERROR_RESPONSES,
    status_code=status.HTTP_202_ACCEPTED,
)
async def github_webhook(
    tenant_id: str,
    request: Request,
    x_tenant_id: str = Depends(require_tenant),
    x_hub_signature_256: str | None = Header(default=None, alias="X-Hub-Signature-256"),
    x_github_event: str | None = Header(default=None, alias="X-GitHub-Event"),
    x_github_delivery: str | None = Header(default=None, alias="X-GitHub-Delivery"),
) -> GitHubPushAck:
    """Accept a GitHub webhook delivery for one tenant's repository."""
    if tenant_id != x_tenant_id:
        raise ApiError(
            code="TENANT_MISMATCH",
            http_status=status.HTTP_403_FORBIDDEN,
            message="Tenant authorization mismatch",
        )
    secret = _webhook_secret()
    if not secret:
        raise ApiError(
            code="WEBHOOK_NOT_CONFIGURED",
            http_status=status.HTTP_503_SERVICE_UNAVAILABLE,
            message="GitHub webhook receiver is not configured",
        )
    body = await request.body()
    if not _verify_signature(secret, body, x_hub_signature_256):
        raise ApiError(
            code="WEBHOOK_FORBIDDEN",
            http_status=status.HTTP_403_FORBIDDEN,
            message="Invalid webhook signature",
        )
    try:
        import json as _json

        payload = _json.loads(body.decode("utf-8"))
        if not isinstance(payload, dict):
            payload = {}
    except Exception as exc:
        raise ApiError(
            code="WEBHOOK_BAD_PAYLOAD",
            http_status=status.HTTP_400_BAD_REQUEST,
            message="Unparseable webhook payload",
        ) from exc

    event = (x_github_event or "").lower()
    repo = payload.get("repository") or {}
    full_name = str(repo.get("full_name", "") or "")
    if event != "push":
        logger.info(
            "GitHub event ignored (not push)",
            extra=sanitize_extra(
                {
                    "tenant_id": x_tenant_id,
                    "event": "github.webhook.ignored",
                    "github_event": event,
                    "delivery": x_github_delivery,
                }
            ),
        )
        return GitHubPushAck(
            tenant_id=x_tenant_id,
            repository=full_name,
            revision="",
            ref="",
            changed_files=0,
            workflow_id=None,
            status="ignored",
        )
    revision = str(payload.get("after", "") or "")
    ref = str(payload.get("ref", "") or "")
    if not full_name or not revision or revision == _ZERO_SHA:
        # Branch deletion (all-zero sha): nothing to project.
        return GitHubPushAck(
            tenant_id=x_tenant_id,
            repository=full_name,
            revision="",
            ref=ref,
            changed_files=0,
            workflow_id=None,
            status="skipped",
        )
    repository_id = _repository_id_for(full_name)
    if not repository_id:
        raise ApiError(
            code="WEBHOOK_BAD_PAYLOAD",
            http_status=status.HTTP_400_BAD_REQUEST,
            message="Push payload lacks repository.full_name",
        )
    changed = _changed_files(payload)

    ctx = get_app_context()
    temporal_client = getattr(ctx, "temporal_client", None)
    if temporal_client is None:
        raise ApiError(
            code="TEMPORAL_UNAVAILABLE",
            http_status=status.HTTP_503_SERVICE_UNAVAILABLE,
            message="Workflow execution engine is unavailable; push recorded but not dispatched",
        )
    from datetime import timedelta as _timedelta

    from investigation_agent_platform.application.worker.workflows import (
        TopologyProjectionInput,
    )

    workflow_id = f"wf-topology-{x_tenant_id}-{repository_id}-{revision[:12]}"
    try:
        await temporal_client.start_workflow(
            "TopologyProjectionWorkflow",
            TopologyProjectionInput(
                tenant_id=x_tenant_id,
                repository_id=repository_id,
                revision=revision,
                application_id=f"{x_tenant_id}-{repository_id}",
                changed_files=changed,
            ),
            id=workflow_id,
            task_queue=getattr(
                getattr(ctx, "temporal_config", None), "task_queue", "investigation-tasks"
            ),
            memo={"tenant_id": x_tenant_id, "trigger": "github-push"},
            execution_timeout=_timedelta(seconds=3600),
        )
    except Exception as exc:
        # Idempotent redelivery: GitHub retries pushes; an already-running
        # execution for this exact (tenant, repo, sha) IS the dispatch.
        # (temporalio raises WorkflowAlreadyStartedError from the client
        # impl module — match by name to avoid importing client internals.)
        if type(exc).__name__ == "WorkflowAlreadyStartedError":
            logger.info(
                "GitHub push already dispatched (redelivery)",
                extra=sanitize_extra(
                    {
                        "tenant_id": x_tenant_id,
                        "event": "github.webhook.redelivered",
                        "repository": full_name,
                        "workflow_id": workflow_id,
                    }
                ),
            )
        else:
            logger.exception(
                "GitHub push dispatch failed",
                extra=sanitize_extra(
                    {
                        "tenant_id": x_tenant_id,
                        "event": "github.webhook.dispatch_failed",
                        "repository": full_name,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    }
                ),
            )
            raise ApiError(
                code="WORKFLOW_START_FAILED",
                http_status=status.HTTP_502_BAD_GATEWAY,
                message="Failed to dispatch projection workflow",
            ) from exc
    logger.info(
        "GitHub push dispatched topology projection",
        extra=sanitize_extra(
            {
                "tenant_id": x_tenant_id,
                "event": "github.webhook.dispatched",
                "repository": full_name,
                "revision": revision[:12],
                "changed_files": len(changed),
                "workflow_id": workflow_id,
            }
        ),
    )
    return GitHubPushAck(
        tenant_id=x_tenant_id,
        repository=full_name,
        revision=revision,
        ref=ref,
        changed_files=len(changed),
        workflow_id=workflow_id,
        status="dispatched",
    )
