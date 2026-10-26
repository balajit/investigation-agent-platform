# src/investigation_agent_platform/application/worker/activities/knowledge.py
"""Temporal activity thin adapters — each delegates to the application/domain layer via AppContext."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from temporalio import activity

try:
    from temporalio.exceptions import ApplicationFailure  # type: ignore[attr-defined]
except ImportError:
    from temporalio.exceptions import (
        ApplicationError as ApplicationFailure,  # type: ignore[assignment, no-redef]
    )

logger = logging.getLogger(__name__)

from investigation_agent_platform.infrastructure.observability.tracing import traced

from .common import (
    COMP_ACTVTY,
    OUTCOME_COMPLETED,
    OUTCOME_FAILED,
    OUTCOME_SKIPPED,
    OUTCOME_STARTED,
    _activity_extra,
    _activity_outcome,
    _application_failure_from_exc,
    _coerced_data,
    elapsed_ms,
    story_log,
    tick,
)
from .inputs import CaptureKnowledgeInput, GenericActivityResult, RetrieveKnowledgeInput, _get_ctx


def _shape_reference_hits(hits: Any) -> list[dict[str, Any]]:
    """Trim refdoc hits to orientation notes (pure, deterministic).

    B-6: plain JSON scalars only (Temporal-safe); content capped so five
    hits cannot blow the prompt budget. Orientation — never evidence.
    """
    try:
        notes = []
        for hit in list(hits or [])[:5]:
            try:
                content = str(getattr(hit, "content", "") or "")
            except Exception:
                content = ""
            notes.append(
                {
                    "document_path": str(getattr(hit, "document_path", "") or ""),
                    "score": float(getattr(hit, "score", 0.0) or 0.0),
                    "source_revision": str(getattr(hit, "source_revision", "") or ""),
                    "content": content[:2000],
                }
            )
        return notes
    except Exception:
        return []


_REFDOC_QUERY_STOPWORDS = frozenset(
    {
        "what",
        "where",
        "when",
        "which",
        "does",
        "doing",
        "have",
        "has",
        "had",
        "with",
        "from",
        "that",
        "this",
        "these",
        "those",
        "there",
        "their",
        "about",
        "show",
        "recent",
        "return",
        "status",
        "record",
        "path",
        "open",
        # Platform-domain terms: present in nearly every doc chunk, so
        # AND-semantics (plainto_tsquery) would keep only chunks naming
        # the platform itself. Hyphenated compounds containing them
        # (investigation-agent-platform) are excluded as a whole.
        "platform",
        "agent",
        "agents",
        "investigation",
        "investigations",
        "service",
        "services",
        "application",
        "repository",
        "project",
    }
)


def _shape_refdoc_query(text: Any) -> str:
    """Distinctive tokens for lexical recall (pure, deterministic).

    B-6: plainto_tsquery ANDs every lexeme, so a raw complaint sentence
    ("Is there an open design decision about Cognee ...?") matches nothing
    while "Cognee decision" matches. Keep distinctive tokens: len>=5
    non-stopwords, hyphenated compounds, and short ALL-CAPS acronyms.
    """
    import re as _re

    try:
        tokens: list[str] = []
        for raw in _re.split(r"[^A-Za-z0-9\-]+", str(text or "")):
            token = raw.strip("-")
            if not token or token in tokens:
                continue
            lowered = token.lower()
            parts = [p for p in lowered.split("-") if p]
            if any(p in _REFDOC_QUERY_STOPWORDS for p in parts):
                continue
            if (
                "-" in token
                and len(token) >= 4
                or token.isupper()
                and len(token) >= 2
                or len(token) >= 5
                and lowered not in _REFDOC_QUERY_STOPWORDS
            ):
                tokens.append(token)
            if len(tokens) >= 8:
                break
        return " ".join(tokens)
    except Exception:
        return ""


async def _recall_reference_docs(
    ctx: Any,
    tenant_id: str,
    application_id: Any,
    query: Any,
) -> list[dict[str, Any]]:
    """Reference-docs recall for reason orientation. Fail-open to []."""
    t0 = tick()
    try:
        query_text = str(query or "").strip()
        story_log(
            logger,
            logging.INFO,
            "reference-docs recall entry",
            comp=COMP_ACTVTY,
            func="_recall_reference_docs",
            stage="reason",
            step="recall",
            outcome=OUTCOME_STARTED,
            tenant_id=str(tenant_id),
            event="reference recall entry",
            inputs={"has_query": bool(query_text)},
            has_query=bool(query_text),
            query_head=query_text[:80],
            has_repo=getattr(ctx, "reference_repo", None) is not None,
        )
        if not query_text:
            return []
        chunk_repo = getattr(ctx, "reference_repo", None)
        if chunk_repo is None:
            return []
        from investigation_agent_platform.application.reference.reference_service import (
            ReferenceDocumentService,
        )

        service = ReferenceDocumentService(
            chunk_repo=chunk_repo,
            profile_repo=getattr(ctx, "profile_repo", None),
            embedder=None,
        )
        hits, _cursor = await service.search(
            tenant_id,
            _shape_refdoc_query(query_text) or query_text,
            application_id=str(application_id or "") or None,
            limit=5,
        )
        story_log(
            logger,
            logging.INFO,
            "reference-docs recall completed",
            comp=COMP_ACTVTY,
            func="_recall_reference_docs",
            stage="reason",
            step="recall",
            outcome=OUTCOME_COMPLETED,
            tenant_id=str(tenant_id),
            event="reference recall completed",
            outputs={"hits": len(hits or [])},
            elapsed_ms=elapsed_ms(t0),
            query=str(query_text)[:120],
            shaped=_shape_refdoc_query(query_text),
            hits=len(hits or []),
        )
        return _shape_reference_hits(hits)
    except Exception as exc:
        story_log(
            logger,
            logging.WARNING,
            "reference-docs recall failed; continuing without orientation",
            comp=COMP_ACTVTY,
            func="_recall_reference_docs",
            stage="reason",
            step="recall",
            outcome=OUTCOME_SKIPPED,
            tenant_id=str(tenant_id),
            event="reference recall failed",
            error=str(exc),
        )
        return []


@activity.defn
@traced("worker", outcome_from=_activity_outcome, extra_from=_activity_extra)
async def retrieve_knowledge_activity(
    params: RetrieveKnowledgeInput,
) -> GenericActivityResult:
    # TO_DO-011: not idempotency-wired — read-only retrieval; an empty
    # context degrades to unassisted reasoning rather than failing.
    """Validity-gated knowledge retrieval before reasoning (Part 6 D4, Slice 0).

    Returns verified facts + excluded_stale_count. Never raises on store
    trouble: an empty context degrades the investigation to unassisted
    reasoning rather than failing it.
    """
    t0 = tick()
    data = _coerced_data(RetrieveKnowledgeInput, params, activity="retrieve_knowledge_activity")
    tenant_id_probe = str(data.get("tenant_id", ""))
    inv_id_probe = str(data.get("investigation_id", ""))
    story_log(
        logger,
        logging.INFO,
        "retrieve_knowledge_activity started",
        comp=COMP_ACTVTY,
        func="retrieve_knowledge_activity",
        stage="reason",
        step="recall",
        outcome=OUTCOME_STARTED,
        tenant_id=tenant_id_probe,
        investigation_id=inv_id_probe,
        event="retrieve_knowledge_activity started",
    )
    try:
        tenant_id = str(data.get("tenant_id", ""))
        inv_id_str = str(data.get("investigation_id", ""))
        kinds = data.get("kinds")
        if not tenant_id or not inv_id_str:
            return GenericActivityResult(
                success=False, error="tenant_id and investigation_id required"
            )
        investigation_id = UUID(inv_id_str)
        ctx = _get_ctx()
        try:
            service = ctx.knowledge_retrieval_service()
            fingerprints = await _caller_fingerprints_for(ctx, tenant_id, investigation_id)
            context = await service.retrieve_for_reasoning(
                tenant_id,
                await _application_id_for(ctx, tenant_id, investigation_id),
                investigation_id,
                kinds=list(kinds) if kinds else None,
                code_issue_fingerprints=fingerprints,
            )
            # B-6: reference-docs recall as reason orientation (not evidence).
            # Fail-open: empty list degrades to artifact-only orientation.
            reference_docs = await _recall_reference_docs(
                ctx,
                tenant_id,
                await _application_id_for(ctx, tenant_id, investigation_id),
                data.get("query"),
            )
            # D2: intake seed (frames/refs/severity/golden) rides the same
            # data envelope into the workflow for first-turn forcing.
            intake_seed = await _intake_seed_for(ctx, tenant_id, investigation_id)
            story_log(
                logger,
                logging.INFO,
                "retrieve_knowledge_activity completed",
                comp=COMP_ACTVTY,
                func="retrieve_knowledge_activity",
                stage="reason",
                step="recall",
                outcome=OUTCOME_COMPLETED,
                tenant_id=tenant_id,
                investigation_id=inv_id_str,
                event="retrieve_knowledge_activity completed",
                outputs={
                    "verified_facts": len(context.verified_facts),
                    "excluded_stale_count": context.excluded_stale_count,
                    "reference_docs": len(reference_docs),
                },
                elapsed_ms=elapsed_ms(t0),
                verified_facts=len(context.verified_facts),
                preferences=len(context.preferences),
                temporal_summary=len(context.temporal_summary),
                excluded_stale_count=context.excluded_stale_count,
                reference_docs=len(reference_docs),
                has_intake_seed=bool(intake_seed),
            )
            return GenericActivityResult(
                success=True,
                data={
                    "verified_facts": [v.model_dump(mode="json") for v in context.verified_facts],
                    "preferences": [v.model_dump(mode="json") for v in context.preferences],
                    "temporal_summary": [
                        v.model_dump(mode="json") for v in context.temporal_summary
                    ],
                    "excluded_stale_count": context.excluded_stale_count,
                    "reference_docs": reference_docs,
                    "intake_seed": intake_seed,
                },
            )
        except Exception as exc:
            story_log(
                logger,
                logging.ERROR,
                "retrieve_knowledge_activity degraded to empty context",
                comp=COMP_ACTVTY,
                func="retrieve_knowledge_activity",
                stage="reason",
                step="recall",
                outcome=OUTCOME_FAILED,
                tenant_id=tenant_id,
                investigation_id=inv_id_str,
                event="retrieve_knowledge_activity degraded",
                outputs={"verified_facts": [], "excluded_stale_count": -1},
                elapsed_ms=elapsed_ms(t0),
                error=str(exc),
                error_type=type(exc).__name__,
                exc_info=True,
            )
            return GenericActivityResult(
                success=True, data={"verified_facts": [], "excluded_stale_count": -1}
            )
    except ApplicationFailure:
        raise
    except Exception as exc:
        story_log(
            logger,
            logging.ERROR,
            "retrieve_knowledge_activity failed",
            comp=COMP_ACTVTY,
            func="retrieve_knowledge_activity",
            stage="reason",
            step="recall",
            outcome=OUTCOME_FAILED,
            tenant_id=tenant_id_probe,
            investigation_id=inv_id_probe,
            event="retrieve_knowledge_activity failed",
            error=str(exc),
            error_type=type(exc).__name__,
            exc_info=True,
        )
        raise _application_failure_from_exc(exc) from exc


async def _caller_fingerprints_for(ctx: Any, tenant_id: str, investigation_id: UUID) -> list[str]:
    """Membership proof for cross-tenant SHARED reads: fingerprints of code
    issues this tenant holds a session on, resolved from its own
    tenant-scoped rows only."""
    fingerprints: list[str] = []
    inv_repo: Any = getattr(ctx, "investigation_repo", None)
    if inv_repo is not None:
        investigation = await inv_repo.get_by_id(tenant_id, investigation_id)
        fingerprint = getattr(investigation, "code_issue_fingerprint", None)
        if fingerprint:
            fingerprints.append(fingerprint)
    return fingerprints


async def _application_id_for(ctx: Any, tenant_id: str, investigation_id: UUID) -> str:
    inv_repo: Any = getattr(ctx, "investigation_repo", None)
    if inv_repo is not None:
        investigation = await inv_repo.get_by_id(tenant_id, investigation_id)
        application_id = getattr(investigation, "application_id", None)
        if application_id:
            return str(application_id)
    return "unknown"


async def _intake_seed_for(ctx: Any, tenant_id: str, investigation_id: UUID) -> dict:
    """Intake seed from investigation metadata (D2, fail-open to {}).

    Carries frames/log-trace refs/severity/golden pairs packed at intake
    into the reason loop without extra round trips.
    """
    try:
        inv_repo: Any = getattr(ctx, "investigation_repo", None)
        if inv_repo is None:
            return {}
        investigation = await inv_repo.get_by_id(tenant_id, investigation_id)
        if investigation is None:
            return {}
        seed = (getattr(investigation, "metadata", None) or {}).get("intake_seed", None)
        return dict(seed) if isinstance(seed, dict) else {}
    except Exception:
        return {}


@activity.defn
@traced("worker", outcome_from=_activity_outcome, extra_from=_activity_extra)
async def capture_knowledge_activity(
    params: CaptureKnowledgeInput,
) -> GenericActivityResult:
    # TO_DO-011: not idempotency-wired — best-effort post-conclusion capture
    # with a never-fail contract; a reserve/raise reservation would convert
    # degraded-0 outcomes into activity failures, violating that contract.
    """Distill concluded state into envelopes (Part 6 D4, Slice 0).

    Runs after conclusion. Capture failures never fail the investigation.
    """
    t0 = tick()
    data = _coerced_data(CaptureKnowledgeInput, params, activity="capture_knowledge_activity")
    tenant_id_probe = str(data.get("tenant_id", ""))
    inv_id_probe = str(data.get("investigation_id", ""))
    story_log(
        logger,
        logging.INFO,
        "capture_knowledge_activity started",
        comp=COMP_ACTVTY,
        func="capture_knowledge_activity",
        stage="persist",
        step="capture",
        outcome=OUTCOME_STARTED,
        tenant_id=tenant_id_probe,
        investigation_id=inv_id_probe,
        event="capture_knowledge_activity started",
    )
    try:
        tenant_id = str(data.get("tenant_id", ""))
        inv_id_str = str(data.get("investigation_id", ""))
        if not tenant_id or not inv_id_str:
            return GenericActivityResult(
                success=False, error="tenant_id and investigation_id required"
            )
        investigation_id = UUID(inv_id_str)
        ctx = _get_ctx()
        try:
            service = ctx.knowledge_capture_service()
            stored = await service.capture_for_investigation(tenant_id, investigation_id)
            # R3: capture mutates envelopes → drop this investigation's cached
            # contexts everywhere (global with Redis). Fail-open: invalidation
            # errors never break the never-fail capture contract.
            try:
                cache = ctx.knowledge_context_cache()
                if cache is not None:
                    await cache.invalidate_investigation(tenant_id, inv_id_str)
            except Exception as exc:
                story_log(
                    logger,
                    logging.WARNING,
                    "Context cache invalidation failed open",
                    comp=COMP_ACTVTY,
                    func="capture_knowledge_activity",
                    stage="persist",
                    step="invalidate_cache",
                    outcome=OUTCOME_SKIPPED,
                    tenant_id=tenant_id,
                    investigation_id=inv_id_str,
                    event="capture cache invalidate skipped",
                    error=str(exc),
                )
            story_log(
                logger,
                logging.INFO,
                "capture_knowledge_activity completed",
                comp=COMP_ACTVTY,
                func="capture_knowledge_activity",
                stage="persist",
                step="capture",
                outcome=OUTCOME_COMPLETED,
                tenant_id=tenant_id,
                investigation_id=inv_id_str,
                event="capture_knowledge_activity completed",
                outputs={"outcome": "captured", "artifacts_stored": len(stored)},
                elapsed_ms=elapsed_ms(t0),
                artifacts_stored=len(stored),
            )
            return GenericActivityResult(success=True, data={"artifacts_stored": len(stored)})
        except Exception as exc:
            story_log(
                logger,
                logging.ERROR,
                "capture_knowledge_activity failed; investigation unaffected",
                comp=COMP_ACTVTY,
                func="capture_knowledge_activity",
                stage="persist",
                step="capture",
                outcome=OUTCOME_FAILED,
                tenant_id=tenant_id,
                investigation_id=inv_id_str,
                event="capture_knowledge_activity degraded",
                outputs={"artifacts_stored": 0},
                elapsed_ms=elapsed_ms(t0),
                error=str(exc),
                error_type=type(exc).__name__,
                exc_info=True,
            )
            return GenericActivityResult(success=True, data={"artifacts_stored": 0})
    except ApplicationFailure:
        raise
    except Exception as exc:
        story_log(
            logger,
            logging.ERROR,
            "capture_knowledge_activity failed",
            comp=COMP_ACTVTY,
            func="capture_knowledge_activity",
            stage="persist",
            step="capture",
            outcome=OUTCOME_FAILED,
            tenant_id=tenant_id_probe,
            investigation_id=inv_id_probe,
            event="capture_knowledge_activity failed",
            error=str(exc),
            error_type=type(exc).__name__,
            exc_info=True,
        )
        raise _application_failure_from_exc(exc) from exc


@dataclass
class SweepKnowledgeInput:
    """One janitor sweep pass over a set of tenants (ISSUE-9)."""

    tenants: list[str] = field(default_factory=list)
    reverify_conditional: bool = False


@activity.defn
@traced("worker", outcome_from=_activity_outcome, extra_from=_activity_extra)
async def sweep_knowledge_activity(
    params: SweepKnowledgeInput,
) -> GenericActivityResult:
    # TO_DO-011: not idempotency-wired — the TTL sweep is idempotent by
    # design (re-running expires nothing new); never fails a workflow.
    """Proactive TTL-expiry sweep. Idempotent; never fails a workflow on error."""
    t0 = tick()
    data = _coerced_data(SweepKnowledgeInput, params, activity="sweep_knowledge_activity")
    story_log(
        logger,
        logging.INFO,
        "sweep_knowledge_activity started",
        comp=COMP_ACTVTY,
        func="sweep_knowledge_activity",
        stage="execute",
        step="sweep",
        outcome=OUTCOME_STARTED,
        event="sweep_knowledge_activity started",
        inputs={"tenants": list(data.get("tenants", []) or [])},
        tenants=list(data.get("tenants", []) or []),
    )
    try:
        from investigation_agent_platform.application.knowledge.janitor import (
            KnowledgeArtifactJanitor,
        )

        tenants = list(data.get("tenants", []) or [])
        reverify = bool(data.get("reverify_conditional", False))
        ctx = _get_ctx()
        total_expired = 0
        per_tenant: dict[str, int] = {}
        for tenant_id in tenants:
            try:
                retrieval = ctx.knowledge_retrieval_service()
                janitor = KnowledgeArtifactJanitor(ctx.artifact_repo, retrieval)
                result = await janitor.sweep_tenant(tenant_id, reverify_conditional=reverify)
                per_tenant[tenant_id] = result.expired_count
                total_expired += result.expired_count
                # R3: sweep transitions envelopes (EXPIRED/SUPERSEDED/QUARANTINED)
                # → drop the tenant's cached contexts. Fail-open per tenant.
                try:
                    cache = ctx.knowledge_context_cache()
                    if cache is not None:
                        await cache.invalidate_tenant(tenant_id)
                except Exception as exc:
                    story_log(
                        logger,
                        logging.WARNING,
                        "Context cache invalidation failed open",
                        comp=COMP_ACTVTY,
                        func="sweep_knowledge_activity",
                        stage="execute",
                        step="invalidate_cache",
                        outcome=OUTCOME_SKIPPED,
                        tenant_id=tenant_id,
                        event="sweep cache invalidate skipped",
                        error=str(exc),
                    )
            except Exception as exc:
                story_log(
                    logger,
                    logging.ERROR,
                    "Knowledge sweep failed for tenant; continuing",
                    comp=COMP_ACTVTY,
                    func="sweep_knowledge_activity",
                    stage="execute",
                    step="sweep",
                    outcome=OUTCOME_FAILED,
                    tenant_id=tenant_id,
                    event="sweep_knowledge_activity tenant failed",
                    error=str(exc),
                    error_type=type(exc).__name__,
                    exc_info=True,
                )
                per_tenant[tenant_id] = -1
        story_log(
            logger,
            logging.INFO,
            "sweep_knowledge_activity completed",
            comp=COMP_ACTVTY,
            func="sweep_knowledge_activity",
            stage="execute",
            step="sweep",
            outcome=OUTCOME_COMPLETED,
            event="sweep_knowledge_activity completed",
            outputs={"outcome": "swept", "expired_total": total_expired},
            elapsed_ms=elapsed_ms(t0),
            expired_total=total_expired,
        )
        return GenericActivityResult(
            success=True,
            data={"expired_total": total_expired, "per_tenant": per_tenant},
        )
    except ApplicationFailure:
        raise
    except Exception as exc:
        story_log(
            logger,
            logging.ERROR,
            "sweep_knowledge_activity failed",
            comp=COMP_ACTVTY,
            func="sweep_knowledge_activity",
            stage="execute",
            step="sweep",
            outcome=OUTCOME_FAILED,
            event="sweep_knowledge_activity failed",
            error=str(exc),
            error_type=type(exc).__name__,
            exc_info=True,
        )
        raise _application_failure_from_exc(exc) from exc
