"""Knowledge router (Part 6 Slice 0): human-readable artifact views.

All reads are tenant-scoped. On merged investigations, other tenants'
sessions render as redacted placeholders (session number + timestamp only);
shared static attribution renders fully. Superseded artifacts are shown
struck-through semantics via explicit status — never hidden.
"""

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status

from investigation_agent_platform.api.dependencies import get_app_context
from investigation_agent_platform.api.tenant import require_tenant

router = APIRouter(tags=["knowledge"])


async def _require_owned_investigation(ctx: Any, tenant_id: str, investigation_id: UUID) -> Any:
    investigation = await ctx.get_investigation_service().execute(tenant_id, investigation_id)
    if not investigation:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Investigation not found")
    if getattr(investigation, "tenant_id", tenant_id) != tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Tenant authorization mismatch"
        )
    return investigation


@router.get("/investigations/{investigation_id}/knowledge")
async def list_knowledge(
    investigation_id: str,
    x_tenant_id: str = Depends(require_tenant),
    include_superseded: bool = Query(default=True),
) -> dict[str, Any]:
    ctx = get_app_context()
    try:
        investigation_uuid = UUID(investigation_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid investigation id"
        ) from exc

    investigation = await _require_owned_investigation(ctx, x_tenant_id, investigation_uuid)
    artifacts = await ctx.artifact_repo.list_for_investigation(x_tenant_id, investigation_uuid)
    if not include_superseded:
        artifacts = [a for a in artifacts if a.status.value == "ACTIVE"]
    own_sessions = await ctx.session_repo.list_own_sessions(x_tenant_id, investigation_uuid)

    # Redacted placeholders for other tenants' sessions: counts and session
    # numbers stay truthful; identity and refs never leave the owning tenant.
    # Assembled server-side from counts only (see D8).
    fingerprint = getattr(investigation, "code_issue_fingerprint", None)
    other_sessions: list[dict[str, Any]] = []
    if fingerprint:
        index_rows = await ctx.code_issue_index.sessions_for_fingerprint(fingerprint)
        own_numbers = {s.session_number for s in own_sessions}
        other_sessions = [
            {"session_number": num, "occurred_at": ts.isoformat(), "tenant": "[redacted]"}
            for (num, _inv_id, ts) in index_rows
            if num not in own_numbers
        ]

    return {
        "investigation_id": investigation_id,
        "code_issue_fingerprint": fingerprint,
        "artifacts": [a.model_dump(mode="json") for a in artifacts],
        "sessions": [
            {
                "session_number": s.session_number,
                "occurred_at": s.occurred_at.isoformat(),
                "status": s.status,
                "log_refs": list(s.log_refs),
                "trace_refs": list(s.trace_refs),
            }
            for s in own_sessions
        ],
        "other_tenant_sessions": other_sessions,
    }


@router.get("/knowledge/search")
async def search_knowledge(
    x_tenant_id: str = Depends(require_tenant),
    application_id: str = Query(default="", max_length=128),
    kind: str | None = Query(default=None, max_length=64),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    """Tenant-scoped semantic-list search across own ACTIVE artifacts.

    Slice 0 ranks by confidence × recency (no vector index yet); the Mem0
    slice upgrades this to semantic recall behind the same contract.
    """
    ctx = get_app_context()
    kinds = [kind] if kind else None
    items = await ctx.artifact_repo.list_active_for_reuse(x_tenant_id, application_id, kinds)
    ranked = sorted(items, key=lambda a: (a.confidence, a.valid_from), reverse=True)[:limit]
    return {
        "items": [a.model_dump(mode="json") for a in ranked],
        "total": len(items),
    }
