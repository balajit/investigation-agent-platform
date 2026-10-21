# src/investigation_agent_platform/application/worker/activities.py
"""Temporal activity thin adapters — each delegates to the application/domain layer via AppContext."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from temporalio import activity

try:
    from temporalio.exceptions import ApplicationFailure  # type: ignore[attr-defined]
except ImportError:
    from temporalio.exceptions import (
        ApplicationError as ApplicationFailure,  # type: ignore[assignment, no-redef]
    )

logger = logging.getLogger(__name__)

from investigation_agent_platform.domain.investigation.models import ActionType
from investigation_agent_platform.infrastructure.observability.tracing import traced

# Log evidence types bypass the persistence room (uncapped raw banking for
# ALL log providers; budgets still bound calls/iterations/tokens/prompts).
_LOG_BYPASS_TYPES = frozenset({"RUNTIME_LOG", "RUNTIME_TRACE", "LOG", "TRACE"})


def _evidence_type_name(item: Any) -> str:
    etype = getattr(item, "evidence_type", "")
    return str(getattr(etype, "value", etype) or "").upper()


def _split_log_bypass(items: list) -> tuple[list, list]:
    """Partition items into (uncapped log rows, budget-bound rows). Pure."""
    logs = [item for item in items if _evidence_type_name(item) in _LOG_BYPASS_TYPES]
    rest = [item for item in items if _evidence_type_name(item) not in _LOG_BYPASS_TYPES]
    return logs, rest


async def _persist_log_exemplars(
    ctx: Any, tenant_id: str, investigation_id: Any, banked: list
) -> int:
    """Group banked log rows and persist one exemplar row per group.

    Member IDs are the raw rows' evidence IDs verbatim (human matchable).
    Fail-open by contract — callers swallow all errors.
    """
    from uuid import UUID as _UUID

    from investigation_agent_platform.application.compression.logparse import (
        mask_volatiles,
    )
    from investigation_agent_platform.domain.evidence.models import LogExemplar

    repo = getattr(ctx, "log_exemplar_repo", None)
    if repo is None or not banked:
        return 0
    groups: dict[str, list] = {}
    for item in banked:
        etype = getattr(item, "evidence_type", "")
        if str(getattr(etype, "value", etype) or "").upper() not in _LOG_BYPASS_TYPES:
            continue
        key = (
            str(getattr(item, "provider", "")),
            str(getattr(item, "source", "")),
            mask_volatiles(
                str(getattr(item, "summary", "") or "")
                + "\n"
                + str(getattr(item, "content_snippet", "") or "")
            ),
        )
        groups.setdefault("\x1f".join(key), []).append(item)
    exemplars: list = []
    for signature_text, members in groups.items():
        first = members[0]
        member_ids = []
        for member in members:
            eid = getattr(member, "evidence_id", None)
            if isinstance(eid, _UUID):
                member_ids.append(eid)
            elif eid:
                try:
                    member_ids.append(_UUID(str(eid)))
                except (ValueError, TypeError):
                    continue
        if not member_ids:
            continue
        exemplars.append(
            LogExemplar(
                tenant_id=tenant_id,
                investigation_id=investigation_id
                if isinstance(investigation_id, _UUID)
                else _UUID(str(investigation_id)),
                signature=signature_text[:4096],
                member_evidence_ids=member_ids[:500],
                count=len(member_ids),
                representative_snippet=str(
                    getattr(first, "content_snippet", "") or getattr(first, "summary", "") or ""
                )[:4096],
            )
        )
    if not exemplars:
        return 0
    try:
        inserted = await repo.save_batch(tenant_id, exemplars)
    except Exception as exc:
        logger.warning(
            "Log exemplar save failed open; raw banking stands",
            extra=_log_extra(
                tenant_id=tenant_id,
                investigation_id=str(investigation_id),
                event="log exemplar save failed",
                error=str(exc),
            ),
        )
        return 0
    logger.info(
        "Log exemplars persisted",
        extra=_log_extra(
            tenant_id=tenant_id,
            investigation_id=str(investigation_id),
            event="log exemplars persisted",
            groups=len(exemplars),
            rows=len(banked),
        ),
    )
    return int(inserted or 0)


# Action types that must reach the evidence gateway to execute; used to
# fail closed (EVIDENCE_PROVIDER_UNAVAILABLE) rather than silently no-op when
# no gateway is wired (F-021/F-023).
_ACTION_REQUIRED_EVIDENCE_TYPE_LOCAL = {
    ActionType.QUERY_STATE,
    ActionType.SEARCH_LOGS,
    ActionType.GET_CODE,
    ActionType.SEARCH_CODE,
    ActionType.FIND_SYMBOL,
    ActionType.FIND_CALL_GRAPH,
    ActionType.GET_CODE_HISTORY,
    ActionType.PROBE_API,
    ActionType.CORRELATE,
}


_EFFECTIVE_LIMIT_FIELDS = frozenset(
    {
        "max_iterations",
        "max_tool_calls",
        "max_evidence_items",
        "max_hypotheses",
        "max_llm_tokens",
    }
)


def apply_effective_limits(limits: Any, investigation: Any) -> Any:
    """Prefer a reprocess-stored effective budget over profile-overlaid limits.

    Reads `investigation.metadata.effective_limits` (written by reprocess
    with a validated multiplier); known positive-int fields only, anything
    else ignored fail-closed to the passed limits. Never raises.
    """
    try:
        metadata = getattr(investigation, "metadata", None) or {}
        effective = metadata.get("effective_limits") if isinstance(metadata, dict) else None
        if not isinstance(effective, dict) or not effective:
            return limits
        updates = {
            field: effective[field]
            for field in _EFFECTIVE_LIMIT_FIELDS
            if isinstance(effective.get(field), int)
            and not isinstance(effective.get(field), bool)
            and effective[field] >= 1
        }
        if updates and hasattr(limits, "model_copy"):
            return limits.model_copy(update=updates)
        return limits
    except Exception:
        return limits


def _coerce_root_ids(parameters: Any) -> tuple[list, int]:
    """Coerce CORRELATE root IDs, dropping unparseable entries (G7).

    LLM-supplied IDs are untrusted exploration input: returns (valid UUIDs,
    dropped count). Pure — unit-tested directly.
    """
    from uuid import UUID as _UUID

    valid: list = []
    dropped = 0
    try:
        raw_ids = parameters.get("root_evidence_ids", []) if isinstance(parameters, dict) else []
    except Exception:
        return [], 0
    for raw in raw_ids or []:
        try:
            valid.append(_UUID(str(raw)))
        except (ValueError, TypeError, AttributeError):
            dropped += 1
    return valid, dropped


def _activity_outcome(result: Any) -> str:
    """Map GenericActivityResult → span outcome (OTEL_PLAN §1.2).

    success=False (returned, not raised) is an activity-level error;
    BLOCKED/degraded/budget signals are first-class non-error outcomes.
    """
    try:
        if not getattr(result, "success", False):
            return "error"
        data = getattr(result, "data", None) or {}
        if not isinstance(data, dict):
            return "ok"
        status = str(data.get("status", "")).upper()
        if status in ("FAILED", "WORKFLOW_ERROR"):
            return "error"
        if status == "BLOCKED":
            return "blocked"
        if status in ("DEGRADED", "EMPTY") or data.get("budget_exceeded"):
            return "degraded"
        return "ok"
    except Exception:
        return "ok"


def _activity_extra(result: Any) -> dict[str, Any]:
    """Counts only (never IDs/payloads) from activity result data."""
    try:
        data = getattr(result, "data", None) or {}
        if not isinstance(data, dict):
            return {}
        extra: dict[str, Any] = {}
        for key in (
            "items_count",
            "evidence_added",
            "artifacts_stored",
            "hypotheses_evaluated",
        ):
            value = data.get(key)
            if isinstance(value, (int, float)):
                extra[f"iap.{key}"] = value
        usage = data.get("llm_usage") or {}
        if isinstance(usage, dict):
            for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                if isinstance(usage.get(key), (int, float)):
                    extra[f"iap.tokens.{key}"] = usage[key]
            if isinstance(usage.get("cost_usd"), (int, float)):
                extra["iap.cost_usd"] = usage["cost_usd"]
        readiness = data.get("conclusion_readiness")
        if isinstance(readiness, (int, float)):
            extra["iap.readiness"] = readiness
        return extra
    except Exception:
        return {}


def _application_failure_from_exc(exc: BaseException) -> ApplicationFailure:
    """Map exceptions to Temporal ApplicationFailure using an explicit retry taxonomy (F-056).

    - ``DomainException`` carries its own explicit ``retryable`` flag — honored as-is.
    - Known transient infrastructure errors (timeouts, connection errors) retry.
    - Known non-transient programming/validation errors (bad input, bugs) never retry.
    - Any other unclassified exception defaults to NON-retryable: an unknown
      failure mode must not be assumed safe to blindly retry and burn budget.
    """
    import asyncio as _asyncio

    from investigation_agent_platform.domain.common.exceptions import DomainException

    if isinstance(exc, DomainException):
        return ApplicationFailure(
            str(exc),
            type=exc.__class__.__name__,
            non_retryable=not exc.retryable,
        )

    _TRANSIENT_INFRA_TYPES = (
        ConnectionError,
        TimeoutError,
        _asyncio.TimeoutError,
        OSError,
    )
    _NON_RETRYABLE_TYPES = (
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        LookupError,
        AssertionError,
    )

    if isinstance(exc, _TRANSIENT_INFRA_TYPES):
        return ApplicationFailure(str(exc), type=exc.__class__.__name__, non_retryable=False)
    if isinstance(exc, _NON_RETRYABLE_TYPES):
        return ApplicationFailure(str(exc), type=exc.__class__.__name__, non_retryable=True)
    # Unclassified exception: fail closed rather than retry indefinitely.
    return ApplicationFailure(str(exc), type=exc.__class__.__name__, non_retryable=True)


def classify_provider_failure(exc: BaseException, absent_label: str) -> tuple[str, str, bool]:
    """(miss_reason, outcome_status, blocking) for a provider failure (Phase 3.1).

    Precedence: explicit exception type, then auth-signal message patterns,
    then the legacy absent label. A timeout is transient until gateway
    retries exhaust — the caller records TIMEOUT ("retry then record"),
    never absence. Only first-party CredentialProviderError blocks the
    investigation (expired credentials cannot be worked around); sniffed
    auth signals from third-party messages degrade as unavailable.
    Function-level imports per C13 (see _sanitize).
    """
    # Function-level: keeps the worker import graph light (C13).
    from investigation_agent_platform.domain.common.exceptions import (
        CredentialProviderError,
        ExecutionError,
        ProviderNotConfiguredError,
        ProviderTimeoutException,
        ProviderUnavailableException,
        RateLimitException,
        UnsupportedCapabilityError,
    )

    if isinstance(exc, UnsupportedCapabilityError):
        return ("unsupported_capability", "DEGRADED", False)
    if isinstance(exc, ProviderTimeoutException):
        return ("timeout", "DEGRADED", False)
    try:
        import asyncio as _asyncio

        if isinstance(exc, (_asyncio.TimeoutError, TimeoutError)):
            return ("timeout", "DEGRADED", False)
    except Exception:
        pass
    if isinstance(exc, CredentialProviderError):
        return ("auth_failed", "BLOCKED", True)
    if isinstance(exc, ProviderNotConfiguredError):
        return (absent_label, "DEGRADED", False)
    if isinstance(exc, ProviderUnavailableException):
        return ("provider_unavailable", "DEGRADED", False)
    if isinstance(exc, ConnectionError):
        return ("provider_unavailable", "DEGRADED", False)
    if isinstance(exc, RateLimitException):
        # Throttling is transient by definition: degrade, never block.
        # retry_after rides the outcome/logging extras for visibility —
        # never executed as a sleep (workflow backoff is a separate
        # replay-safety design; see ledger).
        return ("rate_limited", "DEGRADED", False)
    try:
        text = str(exc).lower()
    except Exception:
        text = ""
    if any(signal in text for signal in ("timed out", "timeout", "deadline exceeded", "timedout")):
        return ("timeout", "DEGRADED", False)
    if any(
        signal in text
        for signal in (
            "429",
            "too many requests",
            "rate limit exceeded",
            "rate limited",
            "rate-limit",
            "ratelimit",
        )
    ):
        # Untyped third-party throttle text degrades as rate_limited —
        # never blocking (only first-party RateLimitException carries a
        # trusted retry_after, and even that never blocks).
        return ("rate_limited", "DEGRADED", False)
    if any(
        signal in text
        for signal in (
            "unauthorized",
            "unauthenticated",
            "authentication failed",
            "invalid credentials",
            "expired token",
            "access denied",
            " 401",
            " 403",
            "(401)",
            "(403)",
        )
    ):
        # Sniffed auth signals degrade as unavailable: only first-party
        # CredentialProviderError blocks the run (above). A third-party
        # message mentioning auth must never halt an investigation by itself.
        return ("provider_unavailable", "DEGRADED", False)
    if isinstance(exc, ExecutionError):
        # Genuinely unclassified execution failure. Provider/profile
        # absence raises ProviderNotConfiguredError at the gateway, so a
        # bare ExecutionError here is an internal provider fault — never
        # mislabeled as "not configured".
        return ("internal_error", "DEGRADED", False)
    raise exc


_KNOWN_LIMITATION_CODES = frozenset(
    {
        "RIVAL_UNTESTED",
        "SINGLE_PROVIDER",
        "CONCLUSION_UNMAPPED",
        "RELATION_UNDEMONSTRATED",
    }
)


def _emit_telemetry(
    ctx: Any, tenant_id: Any, investigation_id: Any, method: str, *args: Any
) -> None:
    """Fire-and-forget proof-chain counter (Phase 4.2). Never raises.

    Label vocabularies are closed by the port contract — high-cardinality
    values (paths, ranges, SHAs) never reach labels. A missing or broken
    observability port degrades to nothing: metrics must never fail an
    investigation.
    """
    try:
        obs = getattr(ctx, "observability", None)
        if obs is None:
            return
        from investigation_agent_platform.ports.observability.telemetry import (
            SpanContext,
        )

        sctx = SpanContext(
            tenant_id=str(tenant_id or ""), investigation_id=UUID(str(investigation_id))
        )
        handler = getattr(obs, method, None)
        if not callable(handler):
            return
        handler(sctx, *args)
    except Exception:
        pass


def _limitation_kind(text: Any) -> str:
    """Closed-vocabulary kind for a "CODE: detail" limitation string."""
    try:
        code = str(text or "").split(":", 1)[0].strip().upper()
        return code if code in _KNOWN_LIMITATION_CODES else "other"
    except Exception:
        return "other"


def _emit_verify_telemetry(
    ctx: Any,
    tenant_id: Any,
    investigation_id: Any,
    candidate: Any,
    result: Any,
    siblings: list[Any] | None,
) -> None:
    """Verification decision + location-exemption counters (Phase 4.2)."""
    try:
        from investigation_agent_platform.application.investigation.verification import (
            VerificationEngine,
        )

        kind = VerificationEngine.effective_claim_kind(candidate)
        kind_label = str(getattr(kind, "value", kind) or "") if kind is not None else "unspecified"
        status = getattr(getattr(result, "status", ""), "value", getattr(result, "status", ""))
        limitations = list(getattr(result, "limitations", None) or [])
        first = "none"
        if limitations:
            code = getattr(limitations[0], "code", "")
            first = str(getattr(code, "value", code) or "") or "none"
        _emit_telemetry(
            ctx,
            tenant_id,
            investigation_id,
            "record_verification",
            kind_label or "unspecified",
            str(status or "unknown"),
            first,
        )
        if bool(getattr(result, "location_validated", False)):
            untested = VerificationEngine.untested_rival_statements(candidate, siblings)
            _emit_telemetry(
                ctx, tenant_id, investigation_id, "record_location_answer", bool(untested)
            )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Wave 2 shared helpers (TO_DO-007/011/012/013/016): telemetry extras,
# strict input coercion, idempotency wiring, actor threading, domain graph.
# ---------------------------------------------------------------------------


def _sanitize(extra: dict[str, Any]) -> dict[str, Any]:
    """Apply the api/errors.py redaction policy to a log ``extra`` dict (TO_DO-009).

    Function-level import with cycle justification (C13): importing
    ``investigation_agent_platform.api.errors`` at module top executes the
    ``api`` package ``__init__`` (FastAPI app wiring); this module already
    keeps ``api`` imports lazy (see ``_get_ctx``) to avoid a worker->api
    import cycle.     ``sys.modules`` caching makes repeat calls cheap.
    """
    # Function-level: top-level import would execute api/__init__ (FastAPI app) -> cycle (C13).
    from investigation_agent_platform.api.errors import sanitize_extra

    return sanitize_extra(extra)


def _log_extra(
    *,
    tenant_id: str = "",
    investigation_id: Any = "",
    event: str = "",
    error: str | None = None,
    error_type: str | None = None,
    **data: Any,
) -> dict[str, Any]:
    """Build a C3 telemetry ``extra`` dict: IDs plus stable ``event`` (TO_DO-007).

    ``investigation_id`` accepts ``UUID``/``None`` and is stringified. Every
    value passes through ``sanitize_extra`` so secret-bearing keys can never
    leak outside the API layer (TO_DO-009).
    """
    extra: dict[str, Any] = {
        "tenant_id": tenant_id or "",
        "investigation_id": "" if investigation_id is None else str(investigation_id),
        "event": event,
    }
    if error is not None:
        extra["error"] = error
    if error_type is not None:
        extra["error_type"] = error_type
    extra.update(data)
    return _sanitize(extra)


def _coerced_data(cls: type, params: object, *, activity: str) -> dict[str, Any]:
    """Strict activity-input coercion replacing ``X | dict[str, Any]`` unions (TO_DO-016).

    Temporal may deliver inputs as plain dicts; both typed dataclass
    instances and dicts are accepted, but dicts are validated field-by-field:
    every required dataclass field must be present as a key. Violations raise
    non-retryable ``ApplicationFailure`` instead of surfacing later as
    ``KeyError``/``ValueError`` deep in workflow logic. Present-but-empty
    values still flow to the existing per-activity guards, which return the
    ``success=False`` envelope (backward compatible with validation-style
    tests). (Inputs stay stdlib dataclasses — not Pydantic ``BaseModel`` —
    because the Temporal payload converter in this repo is dataclass-based;
    this helper is the functional equivalent of ``Model.model_validate`` at
    the boundary.)
    """
    import dataclasses as _dataclasses

    if isinstance(params, cls):
        return {f.name: getattr(params, f.name) for f in _dataclasses.fields(cls)}
    if isinstance(params, dict):
        fields = _dataclasses.fields(cls)
        known = {f.name for f in fields}
        problems = sorted(
            f.name
            for f in fields
            if f.default is _dataclasses.MISSING
            and f.default_factory is _dataclasses.MISSING  # type: ignore[comparison-overlap]
            and f.name not in params
        )
        if problems:
            logger.warning(
                "%s rejected invalid activity input: %s",
                activity,
                ", ".join(problems),
                extra=_log_extra(event=f"{activity} input rejected"),
            )
            raise ApplicationFailure(
                f"{activity} received invalid input: missing {', '.join(problems)}",
                type="ActivityInputValidationError",
                non_retryable=True,
            )
        try:
            instance = cls(**{k: v for k, v in params.items() if k in known})
        except TypeError as exc:
            logger.exception(
                "%s rejected malformed activity input",
                activity,
                extra=_log_extra(event=f"{activity} input rejected", error=str(exc)),
            )
            raise ApplicationFailure(
                f"{activity} received invalid input: {exc}",
                type="ActivityInputValidationError",
                non_retryable=True,
            ) from exc
        return {f.name: getattr(instance, f.name) for f in fields}
    logger.warning(
        "%s rejected unsupported input type %s",
        activity,
        type(params).__name__,
        extra=_log_extra(event=f"{activity} input rejected"),
    )
    raise ApplicationFailure(
        f"{activity} received unsupported input of type {type(params).__name__}; "
        f"expected {cls.__name__} or dict",
        type="ActivityInputValidationError",
        non_retryable=True,
    )


def _latest_fuzzy_orientation(recent_actions: Any) -> dict[str, Any] | None:
    """Newest history-carried fuzzy orientation, if any (D4, best-effort).

    The gateway banks code-recall orientation in result provider_metadata
    (never as evidence); the workflow merges it into history entries, so the
    latest hint is picked up here and threaded into reason() as non-citable
    steering. Malformed entries degrade to None, never raise.
    """
    try:
        for entry in reversed(recent_actions or []):
            if isinstance(entry, dict) and isinstance(entry.get("fuzzy_orientation"), dict):
                return entry["fuzzy_orientation"]
    except Exception:
        pass
    return None


async def _hub_orientation_for(
    ctx: Any, tenant_id: str, investigation: Any
) -> dict[str, Any] | None:
    """Build hub orientation from the investigation's profile (best-effort, never raises).

    Looks up domain_hubs by problem-description characteristic and returns a
    hub_orientation_block, or None when the profile has no hubs. Activities
    pass it into coordinator.reason(); the prompt treats it as direction only.
    """
    try:
        from investigation_agent_platform.domain.analysis.hub_memory import (
            hub_orientation_block,
            lookup_hubs,
        )

        profile_repo: Any = getattr(ctx, "profile_repo", None)
        if profile_repo is None or investigation is None:
            return None
        app_id = str(getattr(investigation, "application_id", "") or "")
        if not app_id:
            return None
        try:
            profile = await profile_repo.get_by_application_id(tenant_id, app_id)
        except Exception:
            return None
        if profile is None:
            return None
        hubs = getattr(profile, "domain_hubs", None) or []
        hub_dicts = []
        for h in hubs:
            try:
                hub_dicts.append(h.model_dump() if hasattr(h, "model_dump") else dict(h))
            except Exception:
                continue
        if not hub_dicts:
            return None
        problem = str(
            getattr(getattr(investigation, "request", None), "problem_description", "")
            or getattr(investigation, "problem_description", "")
            or ""
        )
        matched: list[dict[str, Any]] = []
        for token in [
            t for t in problem.replace("/", " ").replace("_", " ").split() if len(t) >= 4
        ][:8]:
            matched.extend(lookup_hubs(hub_dicts, token, limit=5))
        seen: dict[str, dict[str, Any]] = {}
        for h in matched:
            seen.setdefault(str(h.get("name", "")), h)
        top = sorted(
            seen.values(),
            key=lambda h: (-float(h.get("score", 0.0) or 0.0), str(h.get("name", ""))),
        )[:5]
        if not top:
            top = sorted(
                hub_dicts,
                key=lambda h: (-float(h.get("score", 0.0) or 0.0), str(h.get("name", ""))),
            )[:3]
        return hub_orientation_block(top)
    except Exception:
        return None


def _idempotency_store_for(ctx: Any) -> Any | None:
    """Return the ctx idempotency store when it speaks the durable protocol.

    ``Any`` is justified: ``AppContext`` is assembled in bootstrap/api layers
    and intentionally untyped at the worker boundary; the structural check
    keeps activities backward compatible with minimal/test contexts that
    carry no store (idempotency then degrades to downstream dedup, logged).
    """
    store = getattr(ctx, "idempotency_store", None)
    if store is None:
        return None
    if not (hasattr(store, "reserve_or_get") and hasattr(store, "complete")):
        return None
    return store


def _request_hash(payload: dict[str, Any]) -> str:
    """Deterministic F-014 request hash for an idempotency reservation."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def _reserve_or_replay(
    *,
    ctx: Any,
    tenant_id: str,
    key: str,
    request_payload: dict[str, Any],
    operation: str,
    activity: str,
    investigation_id: Any = "",
) -> tuple[dict[str, Any] | None, str | None]:
    """Reserve an idempotency key or return the stored replay response (TO_DO-011).

    Returns ``(replayed_response, active_key)``: when ``replayed_response`` is
    not ``None`` the caller must return it without re-executing. When the
    store is unwired, returns ``(None, None)`` and the caller proceeds —
    downstream dedup (deterministic ``action_id`` F-057, evidence fingerprint,
    outbox key) remains the safety net, and the skip is logged loudly.
    """
    store = _idempotency_store_for(ctx)
    if store is None:
        logger.warning(
            "%s proceeding without idempotency reservation; store unwired",
            activity,
            extra=_log_extra(
                tenant_id=tenant_id,
                investigation_id=investigation_id,
                event=f"{activity} idempotency skipped",
                idempotency_key=key,
            ),
        )
        return None, None
    # Function-level: keeps the worker import graph light (C13).
    from investigation_agent_platform.domain.common.exceptions import (
        IdempotencyConflictError,
        IdempotencyInProgressError,
    )

    # P0-1: a colliding owner is usually milliseconds ahead (duplicate
    # dispatch / Temporal redelivery), so wait for it and replay its result
    # instead of failing the run. Bounded: 5 polls x 2s fits well inside the
    # 60s activity timeout; on exhaustion raise as before (Temporal retries
    # later, by which time row expiry may have cleared a dead owner).
    _WAIT_POLLS = 5
    _WAIT_SECONDS = 2.0
    cached: Any = None
    reserved = False
    for _wait_attempt in range(_WAIT_POLLS + 1):
        try:
            cached, reserved = await store.reserve_or_get(
                tenant_id, key, _request_hash(request_payload), operation=operation
            )
            break
        except IdempotencyConflictError as exc:
            logger.warning(
                "%s idempotency conflict; refusing duplicate",
                activity,
                extra=_log_extra(
                    tenant_id=tenant_id,
                    investigation_id=investigation_id,
                    event=f"{activity} idempotency conflict",
                    error=str(exc),
                    error_type=type(exc).__name__,
                ),
            )
            raise ApplicationFailure(
                str(exc), type="IdempotencyKeyConflict", non_retryable=True
            ) from exc
        except IdempotencyInProgressError as exc:
            if _wait_attempt >= _WAIT_POLLS:
                logger.warning(
                    "%s idempotency owner still in progress after wait; yielding to retry",
                    activity,
                    extra=_log_extra(
                        tenant_id=tenant_id,
                        investigation_id=investigation_id,
                        event=f"{activity} idempotency wait exhausted",
                        error=str(exc),
                        error_type=type(exc).__name__,
                    ),
                )
                raise ApplicationFailure(
                    str(exc), type="IdempotencyRequestInProgress", non_retryable=False
                ) from exc
            logger.info(
                "%s idempotency in progress; waiting for owner (attempt %d/%d)",
                activity,
                _wait_attempt + 1,
                _WAIT_POLLS,
                extra=_log_extra(
                    tenant_id=tenant_id,
                    investigation_id=investigation_id,
                    event=f"{activity} idempotency wait",
                    idempotency_key=key,
                ),
            )
            await asyncio.sleep(_WAIT_SECONDS)
    if not reserved:
        logger.info(
            "%s idempotency replay; skipping re-execution",
            activity,
            extra=_log_extra(
                tenant_id=tenant_id,
                investigation_id=investigation_id,
                event=f"{activity} idempotency replay",
                idempotency_key=key,
            ),
        )
        return (cached or {}), None
    return None, key


async def _complete_reservation(
    *,
    ctx: Any,
    tenant_id: str,
    key: str,
    operation: str,
    response: dict[str, Any],
    activity: str,
    investigation_id: Any = "",
) -> None:
    """Store an idempotency response; failures are loud but never fail the activity.

    The mutation already succeeded at this point: a pending reservation only
    means a later retry re-executes (downstream dedup still applies), so a
    ``complete`` failure must not convert success into failure.
    """
    store = _idempotency_store_for(ctx)
    if store is None:
        return
    try:
        await store.complete(tenant_id, key, response, operation=operation)
    except Exception as exc:
        logger.exception(
            "%s idempotency complete failed; reservation left pending",
            activity,
            extra=_log_extra(
                tenant_id=tenant_id,
                investigation_id=investigation_id,
                event=f"{activity} idempotency complete failed",
                error=str(exc),
                error_type=type(exc).__name__,
                idempotency_key=key,
            ),
        )


async def _abandon_reservation(
    *,
    ctx: Any,
    tenant_id: str,
    key: str | None,
    operation: str,
    activity: str,
    investigation_id: Any = "",
) -> None:
    """Release a pending reservation after the reserved body raised.

    Without this, a failed attempt leaves an in-progress lease that turns
    every Temporal retry into IdempotencyRequestInProgress until TTL expiry.
    Safe to call when no lease is active (None/empty key or operation) and
    when the store is unwired or lacks ``abandon``; failures are loud but
    never mask the original error.
    """
    if not key or not operation:
        return
    store = _idempotency_store_for(ctx)
    if store is None:
        return
    abandon = getattr(store, "abandon", None)
    if not callable(abandon):
        return
    try:
        await abandon(tenant_id, key, operation)
    except Exception as exc:
        logger.exception(
            "%s idempotency abandon failed; retry may see a stale lease",
            activity,
            extra=_log_extra(
                tenant_id=tenant_id,
                investigation_id=investigation_id,
                event=f"{activity} idempotency abandon failed",
                error=str(exc),
                error_type=type(exc).__name__,
                idempotency_key=key,
            ),
        )


_DOMAIN_TRANSITION_GRAPH: dict[Any, set[Any]] | None = None
# Any-keyed: statuses are domain InvestigationStatus enums resolved lazily to
# honor this module's no-top-level-domain-import convention beyond ActionType.


def _domain_transition_graph() -> dict[Any, set[Any]]:
    """Derive lifecycle adjacency from the domain single source (TO_DO-013).

    Probes ``Investigation.transition_to`` on scratch instances instead of
    duplicating the edge table: any future domain edge edit is picked up
    without a worker change. Computed once per process.
    """
    global _DOMAIN_TRANSITION_GRAPH
    if _DOMAIN_TRANSITION_GRAPH is not None:
        return _DOMAIN_TRANSITION_GRAPH
    # Function-level imports: keeps the worker import graph light (C13).
    from investigation_agent_platform.domain.common.exceptions import (
        InvalidLifecycleTransitionException,
    )
    from investigation_agent_platform.domain.investigation.models import (
        ActorType,
        Investigation,
        InvestigationStatus,
    )

    graph: dict[Any, set[Any]] = {}
    for source in InvestigationStatus:
        probe = Investigation.model_construct(status=source, version=1, started_at=None)
        reachable: set[Any] = set()
        for target in InvestigationStatus:
            try:
                probe.transition_to(target, ActorType.SYSTEM, "graph probe")
            except InvalidLifecycleTransitionException:
                continue
            reachable.add(target)
        graph[source] = reachable
    _DOMAIN_TRANSITION_GRAPH = graph
    return graph


def _normalize_actor(value: Any) -> Any:
    """Coerce an actor value to domain ActorType, defaulting to SYSTEM (TO_DO-012)."""
    # Function-level: keeps the worker import graph light (C13).
    from investigation_agent_platform.domain.investigation.models import (
        ActorType,
    )

    if isinstance(value, ActorType):
        return value
    try:
        return ActorType(str(value or "SYSTEM"))
    except ValueError:
        return ActorType.SYSTEM


@dataclass
class CreateInvestigationInput:
    application_id: str
    session_id: str | None = None
    tenant_id: str = ""
    description: str | None = None
    # Existing investigation ID (F-IDENTITY): when set, the activity MUST load
    # this investigation rather than creating a new aggregate. This preserves
    # one investigation_id end-to-end across API, workflow, checkpoints,
    # evidence, and topology attribution.
    investigation_id: str | None = None


@dataclass
class CreateInvestigationOutput:
    investigation_id: str
    status: str


@dataclass
class GenericActivityResult:
    success: bool
    data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


# Typed activity inputs — tenant_id + investigation_id consistently required.


@dataclass
class ReasonInput:
    tenant_id: str
    investigation_id: str
    iteration: int = 0
    # Prior iterations' reason decisions (plain JSON scalars, most recent
    # last), accumulated by the workflow. Lets the model see repetition
    # (e.g. SEARCH_LOGS ×N with empty evidence) and escalate capability
    # instead of looping the same empty action to the iteration cap.
    recent_actions: list = field(default_factory=list)
    # B-6: orientation notes (refdoc hits, knowledge facts) as plain JSON
    # scalars. Orientation only — never evidence; the validator drops
    # citations that do not resolve to banked evidence rows.
    knowledge_orientation: list = field(default_factory=list)


@dataclass
class ExecuteActionInput:
    tenant_id: str
    investigation_id: str
    action: str = "QUERY_STATE"
    parameters: dict[str, Any] = field(default_factory=dict)
    # Reason-proposed hypotheses (decision.hypotheses_evaluated) carried by
    # the workflow so FORMULATE_HYPOTHESIS can persist them. Empty for all
    # other actions.
    hypotheses: list[str] = field(default_factory=list)
    # Phase 2.1: typed claims (decision.claims) with per-claim evidence
    # binding. Empty for legacy callers; FORMULATE falls back to the
    # batch-wide supporting_evidence_ids parameter for statements without one.
    claims: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class RetrieveEvidenceInput:
    tenant_id: str
    investigation_id: str
    application_id: str | None = None


@dataclass
class VerifyRootCauseInput:
    tenant_id: str
    investigation_id: str
    # D5: triage route selects the runtime verification preset for
    # failure-shaped runs; empty (legacy callers) keeps the default.
    triage_route: str = ""


@dataclass
class CheckpointInput:
    tenant_id: str
    investigation_id: str
    step: int = 0
    state_snapshot: dict[str, Any] | None = None
    # Cumulative LLM spend resolved by the workflow (same from-start basis
    # as the checkpoint counters, but fresher). Overrides carried-forward
    # values when present; never sums with them (that would double-count).
    usage_totals: dict[str, Any] | None = None


@dataclass
class ConcludeInvestigationInput:
    tenant_id: str
    investigation_id: str
    status: str
    # Workflow terminal reason (last_error: budget_exceeded, input_expired,
    # verification_failed_or_limit_reached, ...). Recorded as a stable
    # failure code on FAILED aggregates so the API can report it.
    reason: str = ""
    # C-2: workflow loop counters (plain JSON scalars). Stamped onto
    # metadata["loop_telemetry"] with derived repo counts so budgets and
    # hypothesis lifecycle are API-visible without worker-log access.
    # None (legacy callers) means telemetry unstamped.
    loop_telemetry: dict[str, Any] | None = None
    # D5: triage route selects the runtime verification preset for
    # failure-shaped runs; empty (legacy callers) keeps the default.
    triage_route: str = ""


def _stamp_loop_telemetry(inv: Any, telemetry: dict[str, Any] | None) -> Any:
    """Attach loop telemetry to investigation metadata (no I/O).

    Mirrors _stamp_failure: stamped BEFORE the status transition so it
    rides the normal save path. No-op when telemetry is None (legacy).
    """
    if not telemetry:
        return inv
    stamped_metadata = dict(getattr(inv, "metadata", None) or {})
    stamped = {str(key): telemetry[key] for key in sorted(telemetry) if isinstance(key, str)}
    # Variance triage (#29/#50): pin the effective sampler config alongside
    # the loop counters so future rerun comparisons can rule out config
    # drift. Activity context (not workflow code), so env reads are safe.
    try:
        import os as _os

        _provider = str(_os.environ.get("IAP_LLM_PROVIDER", "") or "").strip()
        _model = ""
        if _provider:
            _model = str(_os.environ.get(f"IAP_{_provider.upper()}_MODEL", "") or "").strip()
        stamped.setdefault("llm_provider", _provider)
        stamped.setdefault("llm_model", _model)
        stamped.setdefault(
            "llm_temperature", str(_os.environ.get("IAP_LLM_TEMPERATURE", "0.0") or "0.0")
        )
    except Exception:
        pass
    stamped_metadata["loop_telemetry"] = stamped
    return inv.model_copy(update={"metadata": stamped_metadata})


def _failure_code_for(reason: str, concluded_from_completed_request: bool) -> str:
    """Normalize a workflow conclude reason to a stable terminal failure code.

    Called only when the resolved target status is FAILED. A COMPLETED
    request that lands FAILED was denied by the conclusion gate
    (ConclusionGate blockers), which takes precedence as the proximate cause.
    """
    if concluded_from_completed_request:
        return "CONCLUSION_DENIED"
    normalized = (reason or "").strip().lower()
    if normalized == "budget_exceeded":
        return "BUDGET_EXCEEDED"
    if normalized == "input_expired":
        return "INPUT_EXPIRED"
    if normalized == "verification_failed_or_limit_reached":
        return "UNVERIFIED"
    return "UNKNOWN"


def _stamp_failure(inv: Any, code: str, reason: str) -> Any:
    """Attach a terminal failure record to investigation metadata (no I/O).

    The stamp must happen BEFORE the status transition so it rides the
    normal save path (model_copy preserves metadata; SQLAlchemy persists it
    via metadata_json). Shared by the conclude path and the workflow-error
    reconcile path so every FAILED aggregate reports its cause via the API.
    """
    stamped_metadata = dict(getattr(inv, "metadata", None) or {})
    stamped_metadata["failure"] = {
        "code": code,
        "reason": (reason or "")[:500],
        "failed_at": datetime.now(UTC).isoformat(),
    }
    return inv.model_copy(update={"metadata": stamped_metadata})


def _select_verification_policy(triage_route: Any) -> Any:
    """Runtime preset for failure-shaped runs, default otherwise (D5).

    Any non-UNKNOWN route means the run triaged a failure (transient,
    config, data, bug) rather than a plain question — select the runtime
    preset so band tuning has one home. Pure.
    """
    try:
        from investigation_agent_platform.application.investigation.verification import (
            RootCauseVerificationPolicy,
        )

        route = str(triage_route or "").upper()
        if route and route != "UNKNOWN":
            return RootCauseVerificationPolicy.runtime()
        return RootCauseVerificationPolicy()
    except Exception:
        from investigation_agent_platform.application.investigation.verification import (
            RootCauseVerificationPolicy,
        )

        return RootCauseVerificationPolicy()


def _derive_loop_telemetry(
    passed: Any,
    hypothesis_counts: dict[str, int],
    evidence_banked: int,
) -> dict[str, Any] | None:
    """Merge workflow counters with derived repo counts (pure).

    C-2: passed holds llm_tokens_total/llm_cost_total/formulated and forced
    counters from the workflow; hypothesis_counts carries formulated/bound/
    verified from the repo; evidence_banked is the repo row count. None in
    (legacy caller) means None out — telemetry unstamped.
    """
    try:
        if not isinstance(passed, dict) or not passed:
            return None
        telemetry: dict[str, Any] = {}
        for key in (
            "llm_tokens_total",
            "llm_cost_total",
            "formulated_count",
            "evidence_banked_total",
            "forced_formulate_count",
            "forced_reread_count",
            "forced_log_search_count",
            "forced_walk_stack_count",
            "forced_replan_count",
            "forced_synthesis_count",
            "overconfident_turns",
            "iterations",
        ):
            try:
                value = passed.get(key)
            except Exception:
                continue
            if isinstance(value, bool) or value is None:
                continue
            if isinstance(value, (int, float)):
                telemetry[key] = value
        try:
            route = passed.get("triage_route", None)
            if isinstance(route, str) and route:
                telemetry["triage_route"] = route[:32]
        except Exception:
            pass
        if isinstance(hypothesis_counts, dict):
            for key in ("hypotheses_formulated", "hypotheses_bound", "hypotheses_verified"):
                try:
                    value = int(hypothesis_counts.get(key, 0) or 0)
                except (TypeError, ValueError):
                    continue
                telemetry[key] = max(0, value)
        try:
            telemetry["evidence_banked_repo"] = max(0, int(evidence_banked or 0))
        except (TypeError, ValueError):
            pass
        return telemetry or None
    except Exception:
        return None


async def _count_hypothesis_lifecycle(
    ctx: Any, tenant_id: str, investigation_id: Any
) -> dict[str, int]:
    """Formulated/bound/verified hypothesis counts (fail-open to zeros)."""
    counts = {"hypotheses_formulated": 0, "hypotheses_bound": 0, "hypotheses_verified": 0}
    try:
        repo: Any = getattr(ctx, "hypothesis_repo", None)
        if repo is None:
            return counts
        hyps = await repo.find_by_investigation_id(tenant_id, investigation_id)
        counts["hypotheses_formulated"] = len(hyps or [])
        for hyp in hyps or []:
            try:
                supporting = getattr(hyp, "supporting_evidence_ids", None) or []
                refuting = getattr(hyp, "refuting_evidence_ids", None) or []
                if len(list(supporting)) + len(list(refuting)) > 0:
                    counts["hypotheses_bound"] += 1
                status = str(
                    getattr(getattr(hyp, "status", ""), "value", getattr(hyp, "status", ""))
                ).upper()
                if status == "VERIFIED":
                    counts["hypotheses_verified"] += 1
            except Exception:
                continue
    except Exception as exc:
        logger.warning(
            "hypothesis lifecycle count failed; telemetry partial",
            extra=_log_extra(
                tenant_id=str(tenant_id),
                event="hypothesis lifecycle count failed",
                error=str(exc),
            ),
        )
    return counts


async def _count_evidence_banked(ctx: Any, tenant_id: str, investigation_id: Any) -> int:
    """Banked evidence row count (fail-open to 0)."""
    try:
        repo: Any = getattr(ctx, "evidence_repo", None)
        if repo is None:
            return 0
        rows = await repo.find_by_investigation_id(tenant_id, investigation_id)
        return len(rows or [])
    except Exception as exc:
        logger.warning(
            "evidence count failed; telemetry partial",
            extra=_log_extra(
                tenant_id=str(tenant_id),
                event="evidence count failed",
                error=str(exc),
            ),
        )
        return 0


@dataclass
class TransitionInvestigationInput:
    tenant_id: str
    investigation_id: str
    target_status: str
    reason: str = ""
    # Domain actor for the audit trail (TO_DO-012): threaded through
    # _transition_via_path into transition_to/record_transition. Defaults to
    # SYSTEM for workflow-driven edges; callers acting for a user pass USER.
    actor: str = "SYSTEM"
    # Terminal failure record for workflow-error reconciliation: when the
    # target is FAILED and a code is present, it is stamped onto metadata
    # (see _stamp_failure) so the API reports the cause. Empty otherwise.
    failure_code: str = ""
    failure_reason: str = ""


@dataclass
class PublishEventInput:
    tenant_id: str
    investigation_id: str
    event: str


@dataclass
class RetrieveKnowledgeInput:
    tenant_id: str
    investigation_id: str
    kinds: list[str] | None = None
    # B-6: complaint text for reference-docs recall. None skips refdoc
    # recall (artifact legs still run); keeps legacy callers working.
    query: str | None = None


@dataclass
class CaptureKnowledgeInput:
    tenant_id: str
    investigation_id: str


def _get_ctx() -> Any:
    # Any justified: AppContext is assembled in bootstrap/api layers and
    # intentionally untyped at the worker boundary (see _idempotency_store_for).
    from investigation_agent_platform.api.dependencies import get_app_context

    return get_app_context()


@activity.defn
@traced("worker", outcome_from=_activity_outcome, extra_from=_activity_extra)
async def create_investigation_activity(
    params: CreateInvestigationInput,
) -> CreateInvestigationOutput:
    # TO_DO-011: not idempotency-wired — creation mints a new identity per
    # call so no deterministic key exists; the F-IDENTITY load path
    # (investigation_id set) is idempotent by lookup instead.
    try:
        data = _coerced_data(
            CreateInvestigationInput, params, activity="create_investigation_activity"
        )
        params = CreateInvestigationInput(**data)
        logger.info(
            "create_investigation_activity started",
            extra=_log_extra(
                tenant_id=params.tenant_id,
                investigation_id=params.investigation_id or "",
                event="create_investigation_activity started",
                application_id=params.application_id,
            ),
        )
        ctx = _get_ctx()

        # F-IDENTITY: if the caller (API) already created this investigation,
        # load it instead of creating a second aggregate with a new ID. This
        # is the single most important identity invariant in the workflow:
        # the API's investigation_id, the Temporal workflow ID
        # (`wf-investigation-{investigation_id}`), and every downstream
        # activity/checkpoint/evidence/attribution record must refer to the
        # same investigation.
        if params.investigation_id:
            inv_repo: Any = getattr(ctx, "investigation_repo", None)
            if inv_repo is None:
                raise ApplicationFailure(
                    "Investigation repository is not configured; cannot load existing investigation",
                    type="InvestigationRepositoryUnavailable",
                    non_retryable=True,
                )
            existing_inv_id = UUID(params.investigation_id)
            existing = await inv_repo.get_by_id(params.tenant_id, existing_inv_id)
            if existing is None:
                raise ApplicationFailure(
                    f"Investigation {existing_inv_id} not found for tenant; "
                    "workflow cannot proceed under a different identity",
                    type="InvestigationNotFoundError",
                    non_retryable=True,
                )
            logger.info(
                "create_investigation_activity completed",
                extra=_log_extra(
                    tenant_id=params.tenant_id,
                    investigation_id=str(existing.id),
                    event="create_investigation_activity completed",
                    outcome="loaded_existing",
                ),
            )
            return CreateInvestigationOutput(
                investigation_id=str(existing.id), status=existing.status.value
            )

        from investigation_agent_platform.domain.investigation.models import InvestigationRequest

        svc = ctx.create_investigation_service()
        req = InvestigationRequest(
            problem_description=params.description or "",
            application_id=params.application_id,
            session_id=params.session_id or "",
            requested_by=params.tenant_id,
        )
        inv = await svc.execute(req, tenant_id=params.tenant_id)
        logger.info(
            "create_investigation_activity completed",
            extra=_log_extra(
                tenant_id=params.tenant_id,
                investigation_id=str(inv.id),
                event="create_investigation_activity completed",
                outcome="created",
            ),
        )
        return CreateInvestigationOutput(investigation_id=str(inv.id), status=inv.status.value)
    except ApplicationFailure:
        raise
    except Exception as exc:
        logger.exception(
            "create_investigation_activity failed",
            extra=_log_extra(
                tenant_id=getattr(params, "tenant_id", "")
                if isinstance(params, CreateInvestigationInput)
                else "",
                investigation_id=getattr(params, "investigation_id", "")
                if isinstance(params, CreateInvestigationInput)
                else "",
                event="create_investigation_activity failed",
                error=str(exc),
                error_type=type(exc).__name__,
            ),
        )
        raise _application_failure_from_exc(exc) from exc


@activity.defn
@traced("worker", outcome_from=_activity_outcome, extra_from=_activity_extra)
async def retrieve_evidence_activity(
    params: RetrieveEvidenceInput,
) -> GenericActivityResult:
    # TO_DO-011: not idempotency-wired — this is a read path whose
    # side-effect writes dedupe on evidence fingerprint, and the workflow
    # loop intentionally re-queries every iteration expecting fresh results;
    # an activity-level replay key would freeze a point-in-time count.
    data = _coerced_data(RetrieveEvidenceInput, params, activity="retrieve_evidence_activity")
    tenant_id_probe = str(data.get("tenant_id", ""))
    inv_id_probe = str(data.get("investigation_id", ""))
    logger.info(
        "retrieve_evidence_activity started",
        extra=_log_extra(
            tenant_id=tenant_id_probe,
            investigation_id=inv_id_probe,
            event="retrieve_evidence_activity started",
        ),
    )
    try:
        tenant_id: str = str(data.get("tenant_id", ""))
        inv_id_str: str = str(data.get("investigation_id", ""))
        if not tenant_id or not inv_id_str:
            return GenericActivityResult(
                success=False, error="tenant_id and investigation_id required"
            )
        investigation_id = UUID(inv_id_str)
        ctx = _get_ctx()
        gateway: Any = getattr(ctx, "evidence_gateway", None)
        if gateway is None:
            # F-021: a missing evidence gateway is a configuration error, not a
            # zero-result success. The workflow must not proceed as though
            # retrieval happened.
            raise ApplicationFailure(
                "Evidence gateway is not configured; evidence retrieval cannot proceed",
                type="EVIDENCE_PROVIDER_UNAVAILABLE",
                non_retryable=True,
            )

        # F-022: build a bounded query scoped to the investigation's profile
        # (environment, time window, result cap) instead of a wildcard
        # full-environment scan.
        from datetime import UTC, datetime, timedelta

        from investigation_agent_platform.domain.evidence.requests import (
            RuntimeEvidenceRequest,
            TimeRange,
        )

        inv_repo: Any = getattr(ctx, "investigation_repo", None)
        profile_repo: Any = getattr(ctx, "profile_repo", None)
        investigation_obj: Any = None
        if inv_repo is not None:
            investigation_obj = await inv_repo.get_by_id(tenant_id, investigation_id)

        app_id: str = str(
            data.get("application_id")
            or (getattr(investigation_obj, "application_id", None) if investigation_obj else None)
            or "example-app"
        )
        environment = "production"
        window_seconds = 3600
        max_results = 100
        if profile_repo is not None:
            profile = await profile_repo.get_by_application_id(tenant_id, app_id)
            if profile is not None:
                environment = profile.environment
                window_seconds = profile.investigation.default_time_window
                # Part 9: RuntimeEvidenceRequest caps limit at the provider
                # ceiling (200); clamp here so a generous profile knob cannot
                # fail request validation (provider clamped identically before).
                max_results = min(profile.investigation.max_evidence_per_query, 200)

        now = datetime.now(UTC)
        req = RuntimeEvidenceRequest(
            environment=environment,
            time_range=TimeRange(start_time=now - timedelta(seconds=window_seconds), end_time=now),
            limit=max_results,
        )
        try:
            result = await gateway.search_runtime_evidence(tenant_id, investigation_id, app_id, req)
        except Exception as exc:
            from investigation_agent_platform.domain.common.exceptions import (
                SecurityPolicyViolationException,
            )

            if isinstance(exc, SecurityPolicyViolationException):
                # Tranche B doctrine: a code-only profile denies SEARCH_LOGS
                # by design — denial degrades to empty, never fails the run.
                # Provider/config errors below stay loud (F-021).
                logger.warning(
                    "retrieve_evidence_activity denied by authorization; degrading to empty",
                    extra=_log_extra(
                        tenant_id=tenant_id,
                        investigation_id=inv_id_str,
                        event="retrieve_evidence_activity auth degraded",
                        error=str(exc),
                        error_type=type(exc).__name__,
                    ),
                )
                return GenericActivityResult(
                    success=True,
                    data={"items_count": 0, "total_count": 0, "degraded": "unauthorized"},
                )
            logger.exception(
                "retrieve_evidence_activity gateway call failed",
                extra=_log_extra(
                    tenant_id=tenant_id,
                    investigation_id=inv_id_str,
                    event="retrieve_evidence_activity gateway call failed",
                    error=str(exc),
                    error_type=type(exc).__name__,
                ),
            )
            raise _application_failure_from_exc(exc) from exc
        logger.info(
            "retrieve_evidence_activity completed",
            extra=_log_extra(
                tenant_id=tenant_id,
                investigation_id=inv_id_str,
                event="retrieve_evidence_activity completed",
                outcome="retrieved",
                items_count=len(result.items),
                total_count=result.total_count,
            ),
        )
        return GenericActivityResult(
            success=True,
            data={"items_count": len(result.items), "total_count": result.total_count},
        )
    except ApplicationFailure:
        raise
    except Exception as exc:
        logger.exception(
            "retrieve_evidence_activity failed",
            extra=_log_extra(
                tenant_id=tenant_id_probe,
                investigation_id=inv_id_probe,
                event="retrieve_evidence_activity failed",
                error=str(exc),
                error_type=type(exc).__name__,
            ),
        )
        raise _application_failure_from_exc(exc) from exc


def select_reasoning_evidence(items: Any, hypotheses: Any, limit: int = 200) -> list[Any]:
    """Phase 1.3: deterministic, claim-prioritized evidence hydration.

    The repository returns rows unordered, and ``InvestigationState.evidence``
    holds at most 200. Taking the first N therefore dropped cited or code rows
    arbitrarily. Priority: rows cited by a hypothesis, then code rows, then
    the newest of the rest; the final order is a stable
    (retrieved_at, observed_at, evidence_id) sort so retries see identical input.
    """

    def _key(e: Any) -> tuple[str, str, str]:
        return (
            str(getattr(e, "retrieved_at", "") or ""),
            str(getattr(e, "observed_at", "") or ""),
            str(getattr(e, "evidence_id", "") or ""),
        )

    cited: set[str] = set()
    for h in hypotheses or []:
        for attr in ("supporting_evidence_ids", "refuting_evidence_ids"):
            cited.update(str(i) for i in (getattr(h, attr, None) or []) if i)

    def _is_code(e: Any) -> bool:
        etype = str(getattr(getattr(e, "evidence_type", ""), "value", "") or "").upper()
        return "CODE" in etype or "EXCEPTION" in etype

    ordered = sorted(items or [], key=_key)
    chosen: list[Any] = []
    seen: set[str] = set()

    def _take(rows: list[Any]) -> None:
        for row in reversed(rows):  # newest first within each tier
            rid = str(getattr(row, "evidence_id", "") or "")
            if rid in seen or len(chosen) >= limit:
                continue
            seen.add(rid)
            chosen.append(row)

    _take([e for e in ordered if str(getattr(e, "evidence_id", "")) in cited])
    _take([e for e in ordered if _is_code(e)])
    _take(ordered)
    return sorted(chosen, key=_key)


@activity.defn
@traced("worker", outcome_from=_activity_outcome, extra_from=_activity_extra)
async def reason_activity(params: ReasonInput) -> GenericActivityResult:
    # TO_DO-011: not idempotency-wired — pure compute over persisted state,
    # no durable writes of its own; re-execution is safe by construction.
    data = _coerced_data(ReasonInput, params, activity="reason_activity")
    tenant_id_probe = str(data.get("tenant_id", ""))
    inv_id_probe = str(data.get("investigation_id", ""))
    logger.info(
        "reason_activity started",
        extra=_log_extra(
            tenant_id=tenant_id_probe,
            investigation_id=inv_id_probe,
            event="reason_activity started",
            iteration=int(data.get("iteration", 0)),
        ),
    )
    try:
        tenant_id = str(data.get("tenant_id", ""))
        inv_id_str = str(data.get("investigation_id", ""))
        iteration = int(data.get("iteration", 0))
        recent_actions = [dict(a) for a in (data.get("recent_actions", []) or [])]
        knowledge_orientation = [
            dict(n) for n in (data.get("knowledge_orientation", []) or []) if isinstance(n, dict)
        ]
        if not tenant_id or not inv_id_str:
            return GenericActivityResult(
                success=False, error="tenant_id and investigation_id required"
            )
        investigation_id = UUID(inv_id_str)
        ctx = _get_ctx()
        # Resolve ReasoningCoordinator: prefer ctx.reasoning_coordinator, else build one from
        # production configuration. Construction failure is a security/configuration error and
        # must never fall back to a permissive/allow-all safety policy (F-005).
        coordinator: Any = getattr(ctx, "reasoning_coordinator", None)
        if coordinator is None:
            from investigation_agent_platform.bootstrap import build_reasoning_coordinator
            from investigation_agent_platform.infrastructure.configuration.config import (
                load_application_config_from_env,
            )

            try:
                cfg = load_application_config_from_env()
                coordinator = build_reasoning_coordinator(cfg)
            except Exception as exc:
                logger.exception(
                    "reason_activity: failed to construct reasoning coordinator; "
                    "refusing to process prompts with a permissive fallback",
                    extra=_log_extra(
                        tenant_id=tenant_id,
                        investigation_id=inv_id_str,
                        event="reason_activity coordinator init failed",
                        error=str(exc),
                        error_type=type(exc).__name__,
                    ),
                )
                raise ApplicationFailure(
                    "Reasoning/safety policy could not be initialized; refusing to process",
                    type="SecurityPolicyInitializationError",
                    non_retryable=True,
                ) from exc

        # Build minimal InvestigationState for reasoning.
        attach = getattr(coordinator, "attach_profile_repo", None)
        if callable(attach):
            try:
                attach(getattr(ctx, "profile_repo", None))
            except Exception as exc:
                logger.warning(
                    "reason profile attach failed",
                    extra=_log_extra(
                        tenant_id=tenant_id,
                        investigation_id=inv_id_str,
                        event="reason profile attach failed",
                        error=str(exc),
                        error_type=type(exc).__name__,
                    ),
                )
        try:
            inv_repo: Any = getattr(ctx, "investigation_repo", None)
            investigation: Any = None
            if inv_repo is not None:
                investigation = await inv_repo.get_by_id(tenant_id, investigation_id)
            if investigation is None:
                # No persisted investigation — this is a genuine failure, not a
                # basis for fabricating a readiness score (F-006).
                raise ApplicationFailure(
                    f"Investigation {investigation_id} not found for tenant",
                    type="InvestigationNotFoundError",
                    non_retryable=True,
                )

            from investigation_agent_platform.domain.investigation.models import InvestigationState

            # Try to hydrate from checkpoint if available.
            checkpoint_repo: Any = getattr(ctx, "checkpoint_repo", None)
            state: InvestigationState | None = None
            if checkpoint_repo is not None:
                try:
                    snap = await checkpoint_repo.get_latest_checkpoint(tenant_id, investigation_id)
                    if snap is not None:
                        state = InvestigationState.model_validate(snap)
                except Exception as exc:
                    logger.warning(
                        "reason_activity checkpoint hydration failed",
                        extra=_log_extra(
                            tenant_id=tenant_id,
                            investigation_id=inv_id_str,
                            event="reason_activity checkpoint hydration failed",
                            error=str(exc),
                            error_type=type(exc).__name__,
                        ),
                    )
            if state is None:
                state = InvestigationState(investigation=investigation)

            # Enrich reasoning state from the repositories. Checkpoints (and
            # fresh states) carry the aggregate only — evidence, hypotheses,
            # findings, and timeline live in their own repos. Without this
            # the model reasons over an eternally empty evidence_records list:
            # it keeps gathering, never formulates, readiness stays 0, and
            # every run ends FAILED at the iteration cap.
            try:
                hyps: list[Any] = []
                hypothesis_repo: Any = getattr(ctx, "hypothesis_repo", None)
                if hypothesis_repo is not None:
                    hyps = list(
                        await hypothesis_repo.find_by_investigation_id(tenant_id, investigation_id)
                    )[:50]
                    state = state.model_copy(update={"active_hypotheses": hyps})
                evidence_repo: Any = getattr(ctx, "evidence_repo", None)
                if evidence_repo is not None:
                    items = await evidence_repo.find_by_investigation_id(
                        tenant_id, investigation_id
                    )
                    state = state.model_copy(
                        update={"evidence": select_reasoning_evidence(items, hyps)}
                    )
                finding_repo: Any = getattr(ctx, "finding_repo", None)
                if finding_repo is not None:
                    found, _ = await finding_repo.list_findings(
                        tenant_id, investigation_id, limit=50
                    )
                    state = state.model_copy(update={"findings": list(found)[:50]})
                timeline_repo: Any = getattr(ctx, "timeline_repo", None)
                if timeline_repo is not None:
                    events = await timeline_repo.find_by_investigation_id(
                        tenant_id, investigation_id
                    )
                    state = state.model_copy(update={"timeline": list(events)[-100:]})
            except Exception as exc:
                logger.warning(
                    "reason_activity state enrichment failed; reasoning unassisted",
                    extra=_log_extra(
                        tenant_id=tenant_id,
                        investigation_id=inv_id_str,
                        event="reason_activity state enrichment failed",
                        error=str(exc),
                        error_type=type(exc).__name__,
                    ),
                )

            # D4 fuzzy orientation: history-carried, newest wins (see
            # _latest_fuzzy_orientation). Threaded into reason(), whose
            # renderer already gates it as non-citable steering.
            _fuzzy_orientation = _latest_fuzzy_orientation(recent_actions)
            decision = await coordinator.reason(
                tenant_id,
                state,
                recent_actions=recent_actions,
                orientation_notes=knowledge_orientation,
                hub_orientation=await _hub_orientation_for(ctx, tenant_id, investigation),
                fuzzy_orientation=_fuzzy_orientation,
            )
            # Phase 3.4: an LLM outage is a platform failure, not a default
            # action. A REASONING_UNAVAILABLE decision previously flowed
            # through with action=None and the workflow dispatched QUERY_STATE
            # — a contentless loop that could end as causeless abstention.
            # Surface it explicitly; the workflow breaks with the cause.
            _decision_meta = getattr(decision, "metadata", None) or {}
            _unavailable_reason = str(_decision_meta.get("reason", "") or "").strip()
            if (
                str(_decision_meta.get("status", "") or "") == "REASONING_UNAVAILABLE"
                or str(getattr(decision, "reasoning_chain", "") or "") == "REASONING_UNAVAILABLE"
            ):
                logger.warning(
                    "reason_activity reasoning unavailable; propagating typed failure",
                    extra=_log_extra(
                        tenant_id=tenant_id,
                        investigation_id=inv_id_str,
                        event="reason_activity reasoning unavailable",
                        error=_unavailable_reason[:500],
                    ),
                )
                return GenericActivityResult(
                    success=True,
                    data={
                        "reasoning_unavailable": True,
                        "reason": _unavailable_reason[:1000],
                        "conclusion_readiness": 0.0,
                        "observations": [],
                        "iteration": iteration,
                    },
                )
            # Choreography: the workflow dispatches on next_action /
            # action_parameters — they must be propagated here, not dropped.
            # (Previously only readiness/observations were returned, so the
            # workflow always fell back to QUERY_STATE and reasoning was
            # effectively severed from execution.)
            action = getattr(decision, "action", None)
            action_type_value = getattr(getattr(action, "action_type", None), "value", None)
            hypotheses_out = list(getattr(decision, "hypotheses_evaluated", None) or [])
            # P0-3: persist model-reported contradictions instead of dropping
            # them — verification and the conclusion gate read them back from
            # the repo. Best-effort: persistence trouble must never fail
            # reasoning itself. Statements dedupe against existing rows.
            try:
                contradiction_repo: Any = getattr(ctx, "contradiction_repo", None)
                reported = [
                    str(s or "").strip()[:2048]
                    for s in (getattr(decision, "contradictions_found", None) or [])
                ]
                reported = [s for s in reported if s]
                if contradiction_repo is not None and reported:
                    from investigation_agent_platform.application.investigation.verification import (
                        Contradiction,
                        ContradictionSeverity,
                    )

                    existing = await contradiction_repo.find_by_investigation_id(
                        tenant_id, investigation_id
                    )
                    known_statements = {
                        str(getattr(c, "statement", "")).strip() for c in (existing or [])
                    }
                    for statement in reported:
                        if statement in known_statements:
                            continue
                        known_statements.add(statement)
                        await contradiction_repo.save(
                            tenant_id,
                            Contradiction(
                                statement=statement,
                                severity=ContradictionSeverity.MEDIUM,
                            ),
                            investigation_id,
                        )
            except Exception as exc:
                logger.warning(
                    "contradiction persistence failed; continuing unassisted",
                    extra=_log_extra(
                        tenant_id=tenant_id,
                        investigation_id=inv_id_str,
                        event="contradiction persistence failed",
                        error=str(exc),
                        error_type=type(exc).__name__,
                    ),
                )
            # Operational telemetry: type names and counts only, never prompt
            # or payload values (prompt content stays out of logs). Values ride
            # as extra keys (TO_DO-007/019); the message keeps lazy %-style.
            logger.warning(
                "reason_activity decision action=%s readiness=%s hypotheses=%s observations=%s",
                action_type_value or "QUERY_STATE",
                decision.conclusion_readiness,
                len(hypotheses_out),
                len(decision.observations or []),
                extra=_log_extra(
                    tenant_id=tenant_id,
                    investigation_id=inv_id_str,
                    event="reason_activity decision",
                    action=action_type_value or "QUERY_STATE",
                    readiness=decision.conclusion_readiness,
                    hypotheses_count=len(hypotheses_out),
                    observations_count=len(decision.observations or []),
                ),
            )
            logger.info(
                "reason_activity completed",
                extra=_log_extra(
                    tenant_id=tenant_id,
                    investigation_id=inv_id_str,
                    event="reason_activity completed",
                    outcome="reasoned",
                    iteration=iteration,
                ),
            )
            # Token budget ledger: accumulate this call into the hydrated
            # counters and route to conclude when the investigation exceeds
            # its LLM token budget — same routing philosophy as the tool and
            # evidence budgets (overshoot is bounded by a single call).
            # Usage also rides the result so the workflow can persist
            # cumulative totals into checkpoints.
            from investigation_agent_platform.domain.investigation.models import (
                InvestigationLimits,
            )

            usage = _decision_llm_usage(decision)
            call_tokens = int(usage["prompt_tokens"]) + int(usage["completion_tokens"])
            call_cost = float(usage["cost_usd"])
            cumulative_tokens = int(getattr(state, "llm_tokens_used", 0) or 0) + call_tokens
            limits = InvestigationLimits()
            try:
                profile_repo = getattr(ctx, "profile_repo", None)
                if profile_repo is not None:
                    _budget_profile = await profile_repo.get_by_application_id(
                        tenant_id, state.investigation.application_id
                    )
                    if _budget_profile is not None:
                        limits = overlay_evidence_budget(limits, _budget_profile)
            except Exception:
                pass
            limits = apply_effective_limits(limits, state.investigation)
            if cumulative_tokens >= limits.max_llm_tokens:
                logger.warning(
                    "reason_activity token budget exhausted; routing to conclude",
                    extra=_log_extra(
                        tenant_id=tenant_id,
                        investigation_id=inv_id_str,
                        event="reason_activity token budget exhausted",
                        cumulative_tokens=cumulative_tokens,
                        limit=limits.max_llm_tokens,
                    ),
                )
                return GenericActivityResult(
                    success=True,
                    data={
                        "budget_exceeded": True,
                        "llm_tokens": call_tokens,
                        "llm_cost": call_cost,
                        "limit": limits.max_llm_tokens,
                    },
                )
            return GenericActivityResult(
                success=True,
                data={
                    "conclusion_readiness": decision.conclusion_readiness,
                    "observations": list(decision.observations or []),
                    "iteration": iteration,
                    "next_action": action_type_value or "QUERY_STATE",
                    "action_parameters": dict(getattr(action, "parameters", None) or {}),
                    "hypotheses_evaluated": hypotheses_out,
                    # Phase 2.1: typed claims with per-claim evidence binding.
                    "claims": [
                        c.model_dump(mode="json")
                        for c in (getattr(decision, "claims", None) or [])
                        if hasattr(c, "model_dump")
                    ],
                    # P0-2: propagate the full decision surface — previously
                    # contradictions/gaps/rationale were dropped here, so
                    # verification always received empty lists downstream.
                    "contradictions_found": list(
                        getattr(decision, "contradictions_found", None) or []
                    ),
                    "information_gaps": list(getattr(decision, "information_gaps", None) or []),
                    "reasoning_chain": str(getattr(decision, "reasoning_chain", "") or "")[:2000],
                    "llm_usage": usage,
                    "llm_tokens": call_tokens,
                    "llm_cost": call_cost,
                },
            )
        except Exception as exc:
            logger.exception(
                "reason_activity reasoning call failed",
                extra=_log_extra(
                    tenant_id=tenant_id,
                    investigation_id=inv_id_str,
                    event="reason_activity reasoning call failed",
                    error=str(exc),
                    error_type=type(exc).__name__,
                ),
            )
            raise _application_failure_from_exc(exc) from exc
    except ApplicationFailure:
        raise
    except Exception as exc:
        logger.exception(
            "reason_activity failed",
            extra=_log_extra(
                tenant_id=tenant_id_probe,
                investigation_id=inv_id_probe,
                event="reason_activity failed",
                error=str(exc),
                error_type=type(exc).__name__,
            ),
        )
        raise _application_failure_from_exc(exc) from exc


_BUDGET_FIELDS = (
    "max_tool_calls",
    "max_evidence_items",
    "max_hypotheses",
    "max_iterations",
    "max_llm_tokens",
    "max_correlation_depth",
    "max_source_files",
    "max_database_rows",
    "max_log_query_window_minutes",
    "max_duration_seconds",
)


def _decision_llm_usage(decision: Any) -> dict[str, Any]:
    """LLM usage attached by the coordinator (zeros when unavailable)."""
    usage = (getattr(decision, "metadata", None) or {}).get("llm_usage", {}) or {}

    def _num(key: str) -> float:
        try:
            return float(usage.get(key, 0) or 0)
        except (TypeError, ValueError):
            return 0.0

    return {
        "prompt_tokens": int(_num("prompt_tokens")),
        "completion_tokens": int(_num("completion_tokens")),
        "cost_usd": _num("cost_usd"),
    }


def overlay_evidence_budget(limits: Any, profile: Any) -> Any:
    """Overlay a profile's evidence_budget onto base limits (§7.6).

    Only known fields with positive-int values apply; anything else is
    ignored (fail-closed to the global default). The profile's canonical
    maximumHypotheses also applies when the evidence_budget dict does not
    set max_hypotheses (previously it never reached the ledger at all).
    Never raises.
    """
    try:
        investigation = getattr(profile, "investigation", None)
        budget = {}
        if investigation is not None:
            budget = getattr(investigation, "evidence_budget", None) or {}
        updates = {
            field: budget[field]
            for field in _BUDGET_FIELDS
            if isinstance(budget.get(field), int) and budget[field] >= 1
        }
        if "max_hypotheses" not in updates and investigation is not None:
            max_h = getattr(investigation, "max_hypotheses", None)
            if max_h is None:
                max_h = getattr(investigation, "maximumHypotheses", None)
            if isinstance(max_h, int) and max_h >= 1:
                updates["max_hypotheses"] = max_h
        if updates:
            return limits.model_copy(update=updates)
    except Exception as exc:
        logger.exception(
            "evidence_budget overlay failed; using global limits",
            extra=_log_extra(
                event="evidence_budget overlay failed",
                error=str(exc),
                error_type=type(exc).__name__,
            ),
        )
    return limits


def _symbol_evidence_packets(
    *,
    tenant_id: Any,
    investigation_id: Any,
    symbols: list[Any],
    symbol_name: str,
    now: Any | None = None,
) -> list[Any]:
    """Convert gateway symbol metadata into bankable discovery evidence.

    Phase 0.2: the gateway deliberately returns symbol results in
    ``provider_metadata`` with ``items=[]``. Callers that only read
    ``items`` therefore bank nothing (FIND_SYMBOL/TRACE_LOGIC were silent
    no-ops). Each symbol becomes a SOURCE_CODE row pinned to the location
    the provider actually observed, flagged ``discovery_only`` because the
    local provider cannot pin to a caller-supplied revision.
    """
    import hashlib as _hashlib

    from investigation_agent_platform.domain.evidence.models import (
        Evidence,
        EvidenceType,
    )
    from investigation_agent_platform.domain.provenance.models import (
        EvidenceFreshness,
        EvidenceProvenance,
        QueryFingerprint,
        SourceLocation,
    )

    observed_at = now or datetime.now(UTC)
    packets: list[Any] = []
    for raw in (symbols or [])[:50]:
        if not isinstance(raw, dict):
            continue
        location = raw.get("location") or {}
        if not isinstance(location, dict):
            continue
        file_path = str(location.get("file_path", "") or "").strip()
        try:
            line_number = int(location.get("line_number") or 0)
        except (TypeError, ValueError):
            line_number = 0
        name = str(raw.get("name", "") or symbol_name or "?")[:256]
        kind = str(raw.get("kind", "") or "symbol")[:64]
        snippet = str(location.get("snippet") or raw.get("signature") or "")[:4000]
        revision = str(location.get("revision", "") or "") or None
        revision_supported = bool(location.get("revision_supported", False))
        fingerprint = _hashlib.sha256(
            f"symbol:{tenant_id}:{investigation_id}:{name}:{file_path}:{line_number}".encode()
        ).hexdigest()[:64]
        packets.append(
            Evidence(
                tenant_id=tenant_id,
                investigation_id=investigation_id,
                evidence_type=EvidenceType.SOURCE_CODE,
                provider="code-intelligence",
                source=f"code://{file_path or '?'}#L{line_number or 0}",
                title=f"Symbol: {name} ({kind})",
                summary=(
                    f"Definition candidate '{name}' ({kind}) at "
                    f"{file_path or 'unknown file'}"
                    f"{':' + str(line_number) if line_number else ''}."
                ),
                content_snippet=snippet,
                attributes={
                    "symbol_name": name,
                    "symbol_kind": kind,
                    "discovery_only": True,
                    "revision_supported": revision_supported,
                },
                observed_at=observed_at,
                retrieved_at=observed_at,
                provenance=EvidenceProvenance(
                    tenant_id=tenant_id,
                    investigation_id=investigation_id,
                    provider_type="code-intelligence",
                    requested_provider_id="treesitter-local",
                    actual_provider_id="treesitter-local",
                    source_system="symbol-index",
                    retrieval_timestamp=observed_at,
                    query_fingerprint=QueryFingerprint(
                        provider_type="code-intelligence",
                        operation="find_symbol",
                        normalized_query_hash=_hashlib.sha256(
                            str(symbol_name).encode()
                        ).hexdigest(),
                    ),
                    source_location=SourceLocation(
                        system="Code",
                        identifier=file_path or name,
                        file_path=file_path or None,
                        line_start=line_number or None,
                        line_end=line_number or None,
                        revision=revision,
                    ),
                ),
                freshness=EvidenceFreshness(observed_at=observed_at, retrieved_at=observed_at),
                fingerprint=fingerprint,
            )
        )
    return packets


def _absence_proof_row(
    *,
    tenant_id: Any,
    investigation_id: Any,
    evidence_type: Any,
    provider: str,
    operation: str,
    scope: str,
    now: Any | None = None,
) -> Any:
    """Bankable scoped empty-search proof (Phase 2 item 3).

    An empty result alone never proves absence — but an empty result over
    a stated, complete scope does, when paired with the code path that
    would have produced the fact. This row carries the scope and the zero
    count the verifier's absence gate requires. Fingerprint is
    deterministic per investigation+scope, so repeated identical searches
    re-save idempotently (the repo dedupes on tenant+fingerprint) instead
    of flooding evidence. discovery_only: a no-match is direction for
    further reads, never a definition.
    """
    import hashlib as _hashlib

    from investigation_agent_platform.domain.evidence.models import (
        Evidence,
    )
    from investigation_agent_platform.domain.provenance.models import (
        EvidenceFreshness,
        EvidenceProvenance,
        QueryFingerprint,
        SourceLocation,
    )

    observed_at = now or datetime.now(UTC)
    fingerprint = _hashlib.sha256(
        f"absence:{tenant_id}:{investigation_id}:{scope}".encode()
    ).hexdigest()[:64]
    return Evidence(
        tenant_id=tenant_id,
        investigation_id=investigation_id,
        evidence_type=evidence_type,
        provider=provider,
        source=f"absence://{scope[:256]}",
        title=f"No matches in scope: {scope[:160]}",
        summary=f"Scoped search over {scope} returned zero matches.",
        content_snippet=f"No matches in scope: {scope}",
        attributes={
            "search_scope": scope,
            "match_count": 0,
            "discovery_only": True,
        },
        observed_at=observed_at,
        retrieved_at=observed_at,
        provenance=EvidenceProvenance(
            tenant_id=tenant_id,
            investigation_id=investigation_id,
            provider_type="search",
            requested_provider_id=provider,
            actual_provider_id=provider,
            source_system="absence-proof",
            retrieval_timestamp=observed_at,
            query_fingerprint=QueryFingerprint(
                provider_type="search",
                operation=operation,
                normalized_query_hash=_hashlib.sha256(scope.encode()).hexdigest(),
            ),
            source_location=SourceLocation(
                system="search",
                identifier=f"absence:{scope[:256]}",
            ),
        ),
        freshness=EvidenceFreshness(observed_at=observed_at, retrieved_at=observed_at),
        fingerprint=fingerprint,
    )


def _code_absence_scope(action_label: str, req: Any) -> str:
    """Human-readable search scope for a code-intelligence request (pure).

    Empty when the request carries no searchable subject — an absence
    proof over an empty query would be meaningless.
    """
    try:
        repo = str(getattr(req, "repository", "") or "").strip()
        rev = str(getattr(req, "revision", "") or "").strip() or "HEAD"
        if action_label == "SEARCH_CODE":
            query = str(getattr(req, "query", "") or "").strip()
            prefix = str(getattr(req, "path_prefix", "") or "").strip() or "/"
            if not query:
                return ""
            return f"{repo}@{rev}:{prefix}:query={query}"[:512]
        if action_label == "FIND_SYMBOL":
            symbol = str(getattr(req, "symbol_name", "") or "").strip()
            return f"{repo}@{rev}:symbol={symbol}"[:512] if symbol else ""
        if action_label == "FIND_CALL_GRAPH":
            symbol = str(getattr(req, "symbol", "") or "").strip()
            direction = str(getattr(req, "direction", "") or "BOTH")
            return f"{repo}@{rev}:callgraph={symbol}:{direction}"[:512] if symbol else ""
        if action_label == "GET_CODE_HISTORY":
            path = str(getattr(req, "file_path", "") or "").strip()
            return f"{repo}:history={path}"[:512] if path else ""
    except Exception:
        pass
    return ""


def _frame_file_matches(frame_file: Any, hit_file: Any) -> bool:
    """True when a search hit plausibly belongs to a stack frame's file.

    Phase 0.3: previously any hit with a file+revision was accepted, so a
    frame could be written onto a different file. A frame with no file hint
    accepts any hit; a frame with a path requires a suffix match, a frame
    with only a basename requires an equal basename.
    """
    frame = str(frame_file or "").replace("\\", "/").strip().strip("/")
    hit = str(hit_file or "").replace("\\", "/").strip().strip("/")
    if not frame:
        return bool(hit)
    if not hit:
        return False
    if frame == hit:
        return True
    if "/" in frame:
        return hit.endswith("/" + frame) or hit == frame
    return hit.rsplit("/", 1)[-1] == frame


def _stack_fingerprint(
    tenant_id: Any, investigation_id: Any, index: int, file_path: Any, line_no: int
) -> str:
    """Bounded fingerprint for stack-walk rows (pure).

    D2: Evidence.fingerprint caps at 128 chars; hash the identity
    instead of embedding long repo-relative paths.
    """
    try:
        import hashlib as _hashlib

        raw = f"stack:{tenant_id}:{investigation_id}:{index}:{file_path}:{line_no}"
        return _hashlib.sha256(raw.encode()).hexdigest()[:64]
    except Exception:
        return f"stack-{index}"


async def _fuse_journey_findings(
    *,
    tenant_id: str,
    investigation_id: Any,
    finding_repo: Any,
    evidence_repo: Any,
) -> None:
    """Merge step OBSERVATION findings into one incident finding (G-4).

    Fires only when JOURNEY spine evidence exists and two or more
    OBSERVATION findings were persisted: unions evidence IDs, concatenates
    statements (capped), and persists a single incident-level OBSERVATION.
    No-op otherwise. Fail-open by contract (caller catches).
    """
    from investigation_agent_platform.domain.evidence.models import EvidenceType
    from investigation_agent_platform.domain.finding.models import Finding, FindingType

    observations: list[Any] = []
    try:
        found, _ = await finding_repo.list_findings(tenant_id, investigation_id, limit=50)
        observations = [
            f
            for f in (found or [])
            if str(getattr(getattr(f, "finding_type", ""), "value", getattr(f, "finding_type", "")))
            == "OBSERVATION"
        ]
    except Exception:
        return
    if len(observations) < 2:
        return
    try:
        has_journey = False
        if evidence_repo is not None:
            rows = await evidence_repo.find_by_investigation_id(tenant_id, investigation_id)
            for row in rows or []:
                try:
                    etype = str(
                        getattr(
                            getattr(row, "evidence_type", ""),
                            "value",
                            getattr(row, "evidence_type", ""),
                        )
                    )
                except Exception:
                    continue
                if etype == str(EvidenceType.JOURNEY.value):
                    has_journey = True
                    break
        if not has_journey:
            return
    except Exception:
        return
    try:
        evidence_ids: list[Any] = []
        seen: set[str] = set()
        statements: list[str] = []
        related: list[Any] = []
        for finding in observations:
            for eid in list(getattr(finding, "evidence_ids", []) or [])[:100]:
                key = str(eid)
                if key not in seen:
                    seen.add(key)
                    evidence_ids.append(eid)
            statement = str(getattr(finding, "statement", "") or "").strip()
            if statement:
                statements.append(statement[:400])
            for hid in list(getattr(finding, "related_hypothesis_ids", []) or [])[:50]:
                if hid not in related:
                    related.append(hid)
        if not statements:
            return
        fused = Finding(
            tenant_id=tenant_id,
            investigation_id=investigation_id,
            finding_type=FindingType.OBSERVATION,
            title=f"Incident fusion across {len(observations)} journey observations",
            statement=" | ".join(statements)[:2000],
            evidence_ids=evidence_ids[:100],
            related_hypothesis_ids=related[:50],
            confidence=min(
                0.95,
                max([float(getattr(f, "confidence", 0.0) or 0.0) for f in observations] or [0.0]),
            ),
        )
        await finding_repo.save_finding_record(tenant_id, fused)
    except Exception:
        return


def _shape_search_keywords(raw: Any) -> list[str]:
    """Split sentence-like keyword blobs into distinctive tokens (pure).

    IAP audit: compound complaints arrive as one long keyword string and
    match nothing. Elements longer than ~60 chars split on non-word runs;
    tokens len>=4 kept (stopwords dropped), capped at 20, order-stable,
    deduped. Short keyword lists pass through untouched.
    """
    try:
        import re as _re

        stopwords = frozenset(
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
                "implementation",
                "slices",
                "lack",
            }
        )
        flat: list[str] = []
        items = raw if isinstance(raw, list) else [raw]
        for element in items:
            try:
                text = str(element or "").strip()
            except Exception:
                continue
            if not text:
                continue
            if len(text) <= 60:
                if text not in flat:
                    flat.append(text)
                continue
            for token in _re.split(r"[^a-z0-9]+", text.lower()):
                if len(token) >= 4 and token not in stopwords and token not in flat:
                    flat.append(token)
                if len(flat) >= 20:
                    break
            if len(flat) >= 20:
                break
        return flat[:20]
    except Exception:
        try:
            return [str(v) for v in (raw or []) if str(v or "").strip()][:20]
        except Exception:
            return []


def _caller_chain_targets(edges: list[dict], limit: int = 5) -> list[dict]:
    """Caller-role graph edges worth an automatic call-site read.

    Callers first, one target per distinct file (a second edge in the
    same file shares the ±40-line window), bounded so a hub symbol
    cannot flood the turn. Module-level for unit testability. L2
    Helper-drop fix: the chaining step previously read only the first
    edge, so the second caller never got a citable range row.
    """
    targets: list[dict] = []
    seen_files: set[str] = set()
    ordered = sorted(
        (e for e in edges if isinstance(e, dict)),
        key=lambda e: 0 if e.get("role") == "caller" else 1,
    )
    for edge in ordered:
        if edge.get("role") != "caller":
            continue
        file_path = str(edge.get("file") or "").strip()
        line = edge.get("line")
        if not file_path or not isinstance(line, int) or file_path in seen_files:
            continue
        seen_files.add(file_path)
        targets.append(edge)
        if len(targets) >= max(1, limit):
            break
    return targets


async def fallback_call_graph_to_search(
    *,
    gateway: Any,
    tenant_id: str,
    investigation_id: Any,
    application_id: Any,
    parameters: dict,
    evidence_repo: Any,
    bank: Any,
    record_outcome: Any,
    outcome_detail: dict,
    inv_id_str: Any = "",
    union: bool = False,
) -> int:
    """B-5: empty graph (no call_graph.json / stale checkout) degrades
    to a SEARCH_CODE keyword query for the symbol instead of EMPTY.

    The Step-1 L2 run ANSWERED via caller-keyword rows while the
    Step-2 rerun took the graph path to 0 edges and FAILED — same
    question, different retrieval luck. This makes the keyword
    fallback deterministic: graph miss always searches. Banked rows
    carry miss_reason so the reasoner knows they are keyword rows,
    not true graph edges. Module-level for unit testability.

    Union mode (union=True): the graph banked a row but one side is
    empty (0 callers or 0 callees) — graph incompleteness, not proof
    of absence. Keyword rows supplement (union) instead of replacing,
    marked call_graph_partial_search_union.
    """
    from investigation_agent_platform.domain.evidence.requests import (
        CodeSearchRequest,
    )

    symbol = str((parameters or {}).get("symbol", "") or "").strip()
    if not symbol:
        return 0
    marker = "call_graph_partial_search_union" if union else "call_graph_empty_search_fallback"
    try:
        qres = await gateway.search_code(
            tenant_id,
            investigation_id,
            application_id or "example-app",
            CodeSearchRequest(
                repository=application_id or "example-app",
                revision=str((parameters or {}).get("revision", "HEAD")),
                query=symbol,
                language=(parameters or {}).get("language"),
                limit=20,
            ),
        )
    except Exception as exc:
        logger.warning(
            "FIND_CALL_GRAPH keyword fallback failed; keeping empty",
            extra=_log_extra(
                tenant_id=tenant_id,
                investigation_id=inv_id_str,
                event="call graph fallback failed",
                error=str(exc),
            ),
        )
        return 0
    banked = bank(qres.items)
    if evidence_repo is not None and banked:
        await evidence_repo.save_batch(tenant_id, banked, investigation_id)
    if banked:
        outcome_detail["miss_reason"] = marker
    return record_outcome(banked)


@activity.defn
@traced("worker", outcome_from=_activity_outcome, extra_from=_activity_extra)
async def execute_action_activity(
    params: ExecuteActionInput,
) -> GenericActivityResult:
    data = _coerced_data(ExecuteActionInput, params, activity="execute_action_activity")
    tenant_id_probe = str(data.get("tenant_id", ""))
    inv_id_probe = str(data.get("investigation_id", ""))
    logger.info(
        "execute_action_activity started",
        extra=_log_extra(
            tenant_id=tenant_id_probe,
            investigation_id=inv_id_probe,
            event="execute_action_activity started",
            action=str(data.get("action", "QUERY_STATE")),
        ),
    )
    try:
        tenant_id = str(data.get("tenant_id", ""))
        inv_id_str = str(data.get("investigation_id", ""))
        action_type = str(data.get("action", "QUERY_STATE"))
        parameters: dict[str, Any] = dict(data.get("parameters", {}))
        # Reason-proposed hypothesis strings for FORMULATE_HYPOTHESIS.
        proposed_hypotheses: list[str] = [
            str(h) for h in (data.get("hypotheses", []) or []) if str(h).strip()
        ]
        # Phase 2.1: typed claims keyed by statement, kind, and target identity.
        # A claim whose statement is absent from hypotheses_evaluated is
        # still persisted. Same statement with different kinds stays
        # Caller edges and history commits for one statement also remain
        # distinct so each keeps its own citation binding.
        proposed_claims: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
        for raw_claim in (data.get("claims", []) or [])[:20]:
            if not isinstance(raw_claim, dict):
                continue
            claim_text = str(raw_claim.get("statement", "") or "").strip()[:2048]
            if not claim_text:
                continue
            claim_kind_key = str(raw_claim.get("claim_kind") or "CAUSAL").strip().upper()
            claim_target = raw_claim.get("target")
            claim_target = claim_target if isinstance(claim_target, dict) else {}
            proposed_claims[
                (
                    claim_text,
                    claim_kind_key,
                    str(claim_target.get("counterpart") or "").strip().casefold(),
                    str(claim_target.get("commit_id") or "").strip().casefold(),
                    str(claim_target.get("file_path") or "").replace("\\", "/").strip().casefold(),
                )
            ] = raw_claim
            if claim_text not in proposed_hypotheses:
                proposed_hypotheses.append(claim_text)
        if not tenant_id or not inv_id_str:
            return GenericActivityResult(
                success=False, error="tenant_id and investigation_id required"
            )
        investigation_id = UUID(inv_id_str)
        ctx = _get_ctx()

        # Mandatory authorization gate (F-004): no action reaches the evidence
        # gateway or execution surface without an explicit allow decision.
        from investigation_agent_platform.domain.investigation.models import (
            ActionType,
            InvestigationAction,
        )

        try:
            resolved_action_type = ActionType(action_type)
        except ValueError as exc:
            raise ApplicationFailure(
                f"Unknown action type: {action_type}",
                type="InvalidActionTypeError",
                non_retryable=True,
            ) from exc

        inv_repo: Any = getattr(ctx, "investigation_repo", None)
        investigation_obj: Any = None
        if inv_repo is not None:
            investigation_obj = await inv_repo.get_by_id(tenant_id, investigation_id)
        application_id = str(
            data.get("application_id")
            or (getattr(investigation_obj, "application_id", None) if investigation_obj else None)
            or ""
        )

        inv_action = InvestigationAction.create(
            tenant_id=tenant_id,
            investigation_id=investigation_id,
            action_type=resolved_action_type,
            parameters=parameters,
        )

        # TO_DO-011: idempotency-wired — action_id is deterministic (UUIDv5
        # over the execution hash, F-057), so a Temporal retry of the same
        # logical action reserves the same key instead of double-recording.
        _idem_active_key: str | None = None
        _idem_operation = "activity:execute_action"
        _idem_key = f"execute:{inv_action.action_id}"
        _idem_request = {
            "action_id": str(inv_action.action_id),
            "tenant_id": tenant_id,
            "investigation_id": inv_id_str,
            "action_type": resolved_action_type.value,
            "parameters": parameters,
        }
        _replayed, _idem_active_key = await _reserve_or_replay(
            ctx=ctx,
            tenant_id=tenant_id,
            key=_idem_key,
            request_payload=_idem_request,
            operation=_idem_operation,
            activity="execute_action_activity",
            investigation_id=inv_id_str,
        )
        if _replayed is not None:
            _replayed_data = dict(_replayed.get("data") or {})
            # Phase 3.2: execute rows stored before action_outcome are
            # legacy, not absent — tag explicitly instead of inventing an
            # outcome. The workflow records an empty outcome (identical to
            # live pre-fix behavior) while operators can distinguish replay
            # age. Budget rows are current-shape (keyed on budget_exceeded).
            if _replayed_data.get("action_outcome") is None and not _replayed_data.get(
                "budget_exceeded"
            ):
                _replayed_data["idempotency_replay_legacy"] = True
                # Phase 4.2: a legacy row's outcome shape is unknown, so a
                # match cannot be proven — count it as a mismatch.
                _emit_telemetry(ctx, tenant_id, investigation_id, "record_replay_mismatch")
            return GenericActivityResult(
                success=bool(_replayed.get("success", True)),
                data=_replayed_data,
                error=_replayed.get("error"),
            )

        action_service = ctx.execute_action_service()
        principal_id = str(data.get("principal_id", "worker"))

        # F-054: durable budget ledger — usage is derived from durable tables
        # (action_executions count + evidence count), reserved/checked before
        # execution rather than advisory config. Deny before authorize so a
        # budget-exhausted investigation cannot consume further provider calls.
        from investigation_agent_platform.domain.common.exceptions import (
            InvestigationLimitExceededException,
        )
        from investigation_agent_platform.domain.investigation.models import InvestigationLimits

        limits = InvestigationLimits()
        # Per-app evidence budget (§7.6): profile evidence_budget overlays the
        # process-global defaults; unknown/invalid keys are ignored, never
        # trusted. Also resolves the profile once for the correlate clamp.
        _budget_profile: Any = None
        profile_repo_for_limits: Any = getattr(ctx, "profile_repo", None)
        if profile_repo_for_limits is not None and application_id:
            try:
                _budget_profile = await profile_repo_for_limits.get_by_application_id(
                    tenant_id, application_id
                )
            except Exception as exc:
                logger.exception(
                    "profile budget lookup failed; using global limits",
                    extra=_log_extra(
                        tenant_id=tenant_id,
                        investigation_id=inv_id_str,
                        event="profile budget lookup failed",
                        error=str(exc),
                        error_type=type(exc).__name__,
                    ),
                )
                _budget_profile = None
        if _budget_profile is not None:
            limits = overlay_evidence_budget(limits, _budget_profile)
        limits = apply_effective_limits(limits, investigation_obj)
        app_correlation_depth = (
            _budget_profile.investigation.correlation_depth
            if _budget_profile is not None
            else limits.max_correlation_depth
        )
        try:
            action_repo_for_budget: Any = getattr(ctx, "action_repo", None)
            evidence_repo_for_budget: Any = getattr(ctx, "evidence_repo", None)
            tool_calls = 0
            evidence_count = 0
            if action_repo_for_budget is not None and hasattr(
                action_repo_for_budget, "count_by_investigation"
            ):
                tool_calls = await action_repo_for_budget.count_by_investigation(
                    tenant_id, investigation_id
                )
            if evidence_repo_for_budget is not None:
                items = await evidence_repo_for_budget.find_by_investigation_id(
                    tenant_id, investigation_id
                )
                evidence_count = len(items)
            if tool_calls >= limits.max_tool_calls:
                raise InvestigationLimitExceededException(
                    f"Maximum tool call limit reached: {limits.max_tool_calls}"
                )
            if evidence_count >= limits.max_evidence_items:
                raise InvestigationLimitExceededException(
                    f"Maximum evidence limit reached: {limits.max_evidence_items}"
                )
        except InvestigationLimitExceededException as exc:
            # Budget exhaustion is a ROUTING signal, not an execution failure:
            # the workflow breaks to CONCLUDING with the gathered corpus
            # instead of dying mid-flight (previously this raised out of the
            # activity, failed the whole execution, and stranded the aggregate
            # in a non-terminal state with no audit record).
            logger.warning(
                "execute_action_activity budget exhausted; routing to conclude",
                extra=_log_extra(
                    tenant_id=tenant_id,
                    investigation_id=inv_id_str,
                    event="execute_action_activity budget exhausted",
                    error=str(exc),
                    error_type=type(exc).__name__,
                ),
            )
            if _idem_active_key is not None:
                await _complete_reservation(
                    ctx=ctx,
                    tenant_id=tenant_id,
                    key=_idem_active_key,
                    operation=_idem_operation,
                    response={
                        "success": True,
                        "data": {
                            "status": "BUDGET_EXCEEDED",
                            "budget_exceeded": True,
                            "items_count": 0,
                        },
                    },
                    activity="execute_action_activity",
                    investigation_id=inv_id_str,
                )
            return GenericActivityResult(
                success=True,
                data={
                    "status": "BUDGET_EXCEEDED",
                    "budget_exceeded": True,
                    "items_count": 0,
                },
            )
        except Exception as exc:
            logger.exception(
                "budget ledger check failed; failing closed",
                extra=_log_extra(
                    tenant_id=tenant_id,
                    investigation_id=inv_id_str,
                    event="budget ledger check failed",
                    error=str(exc),
                    error_type=type(exc).__name__,
                ),
            )
            raise _application_failure_from_exc(exc) from exc

        # Evidence room: persist at most the remaining budget per action.
        # Provider pages rank best-first, so truncation keeps the most
        # relevant hits while leaving room for later iterations — without
        # this, one broad query (limit 100) banks 40+ rows against a
        # 25-item budget and the very next iteration trips BUDGET_EXCEEDED.
        # Log rows bypass the room: persistence is uncapped for ALL log
        # types/providers (raw + exemplar twin); budgets still bound
        # tool calls, iterations, LLM tokens, and prompt projection.
        evidence_room = max(0, limits.max_evidence_items - evidence_count)

        def _evidence_type_name(item: Any) -> str:
            etype = getattr(item, "evidence_type", "")
            return str(getattr(etype, "value", etype) or "").upper()

        def _bank(items: list) -> list:
            logs, rest = _split_log_bypass(items)
            kept_rest = rest[:evidence_room]
            kept = logs + kept_rest
            # Preserve provider best-first order across the partition.
            order = {id(item): index for index, item in enumerate(items)}
            kept.sort(key=lambda item: order.get(id(item), len(items)))
            if len(kept) < len(items):
                logger.info(
                    "Evidence persist truncated to remaining budget",
                    extra=_log_extra(
                        tenant_id=tenant_id,
                        investigation_id=inv_id_str,
                        event="evidence persist truncated",
                        kept=len(kept),
                        dropped=len(items) - len(kept),
                    ),
                )
            return kept

        try:
            await action_service.authorize(
                tenant_id,
                investigation_id,
                inv_action,
                principal_id,
                application_id=application_id or None,
            )
        except Exception as exc:
            # P0-2 BLOCKED: the action stays denied (fail-closed), but the
            # denial degrades to an EMPTY/BLOCKED outcome instead of killing
            # the investigation — the miss_reason steers the reasoner to
            # other capabilities rather than ending the run.
            from investigation_agent_platform.domain.investigation.models import (
                ActionOutcome as _BlockedOutcome,
            )
            from investigation_agent_platform.domain.investigation.models import (
                ActionOutcomeStatus as _BlockedStatus,
            )

            logger.warning(
                "execute_action_activity denied by authorization gate; degrading",
                extra=_log_extra(
                    tenant_id=tenant_id,
                    investigation_id=inv_id_str,
                    event="execute_action_activity denied by authorization gate",
                    error=str(exc),
                    error_type=type(exc).__name__,
                    action_type=action_type,
                ),
            )
            try:
                resolved = ActionType(action_type)
            except ValueError:
                resolved = ActionType.QUERY_STATE
            blocked = _BlockedOutcome(
                action_id=inv_action.action_id,
                action_type=resolved,
                status=_BlockedStatus.BLOCKED,
                miss_reason="authorization_denied",
                error_code=type(exc).__name__,
            )
            return GenericActivityResult(
                success=True,
                data={
                    "status": "BLOCKED",
                    "items_count": 0,
                    "action_outcome": blocked.model_dump(mode="json"),
                },
            )

        # Dispatch via EvidenceGateway using an exhaustive enum-to-handler
        # registry (F-023) — unknown/unsupported actions are a hard failure,
        # never a silent no-op "EXECUTED".
        gateway: Any = getattr(ctx, "evidence_gateway", None)
        evidence_repo: Any = getattr(ctx, "evidence_repo", None)
        profile_repo: Any = getattr(ctx, "profile_repo", None)
        environment = "production"
        if profile_repo is not None and application_id:
            profile = await profile_repo.get_by_application_id(tenant_id, application_id)
            if profile is not None:
                environment = profile.environment

        result_status = "EXECUTED"
        items_count = 0
        # P0-2: typed outcome side-channel. Handlers keep their int return
        # (items banked); this dict carries status/miss reason/evidence ids
        # into the ActionOutcome built at dispatch below.
        outcome_detail: dict[str, Any] = {
            "status": "SUCCEEDED",
            "miss_reason": None,
            "evidence_ids": [],
        }

        def _record_outcome(banked: list[Any]) -> int:
            outcome_detail["evidence_ids"] = [
                str(getattr(e, "evidence_id", "")) for e in banked[:25]
            ]
            if not banked and outcome_detail["status"] == "SUCCEEDED":
                outcome_detail["status"] = "EMPTY"
            return len(banked)

        async def _handle_query_state() -> int:
            from investigation_agent_platform.domain.common.exceptions import (
                CredentialProviderError,
                ExecutionError,
                ProviderNotConfiguredError,
                ProviderTimeoutException,
                ProviderUnavailableException,
                RateLimitException,
            )
            from investigation_agent_platform.domain.evidence.requests import (
                ApplicationStateRequest,
            )

            # Default page 10 (not the request-model default 50): same
            # reasoning as SEARCH_LOGS — one action must not bank the whole
            # 25-item budget. Callers may request up to 200; persistence
            # truncates to the remaining budget regardless.
            req = ApplicationStateRequest(
                environment=environment,
                template_id=str(parameters.get("template_id") or "default"),
                parameters=parameters,
                limit=min(int(parameters.get("limit", 10)), 200),
            )
            try:
                qres = await gateway.get_application_state(
                    tenant_id, investigation_id, application_id or "example-app", req
                )
            except (
                ProviderTimeoutException,
                TimeoutError,
                CredentialProviderError,
                ProviderUnavailableException,
                RateLimitException,
                ConnectionError,
                ProviderNotConfiguredError,
                ExecutionError,
            ) as exc:
                # Phase 3.1: typed failure outcomes (same contract as code
                # navigation) — timeouts, auth blocks, and outages surface
                # distinctly instead of all reading "provider absent".
                miss_reason, outcome_status, _blocking = classify_provider_failure(
                    exc, "state_provider_not_configured"
                )
                outcome_detail["status"] = outcome_status
                outcome_detail["miss_reason"] = miss_reason
                if isinstance(exc, RateLimitException):
                    outcome_detail["error_code"] = "RATE_LIMITED"
                logger.warning(
                    "QUERY_STATE provider failure; degrading",
                    extra=_log_extra(
                        tenant_id=tenant_id,
                        investigation_id=inv_id_str,
                        event="query_state provider failure",
                        error=str(exc),
                        miss_reason=miss_reason,
                        retry_after=getattr(exc, "retry_after", None),
                    ),
                )
                return 0
            # P3-2: per-source fan-out breakdown rides into the outcome.
            outcome_detail["sources"] = list(
                (getattr(qres, "provider_metadata", None) or {}).get("per_source", []) or []
            )
            banked = _bank(qres.items)
            if evidence_repo is not None and banked:
                await evidence_repo.save_batch(tenant_id, banked, investigation_id)
            if not banked and evidence_repo is not None:
                # Phase 2 item 3: an empty state query banks a scoped
                # no-match proof (same contract as code navigation above).
                try:
                    from investigation_agent_platform.domain.evidence.models import (
                        EvidenceType as _EvidenceType,
                    )

                    _params = getattr(req, "parameters", None) or {}
                    _scope_items = ",".join(
                        f"{k}={v}"
                        for k, v in sorted((str(k), str(v)) for k, v in dict(_params).items())
                    )
                    _state_scope = (
                        f"{getattr(req, 'environment', '')}:"
                        f"{getattr(req, 'template_id', '')}:{_scope_items}"
                    )[:512]
                    await evidence_repo.save(
                        tenant_id,
                        _absence_proof_row(
                            tenant_id=tenant_id,
                            investigation_id=investigation_id,
                            evidence_type=_EvidenceType.DATABASE_STATE,
                            provider="state-provider",
                            operation="query_state",
                            scope=_state_scope,
                        ),
                        investigation_id,
                    )
                except Exception:
                    pass
            return _record_outcome(banked)

        async def _handle_search_logs() -> int:
            from investigation_agent_platform.domain.evidence.requests import (
                RuntimeEvidenceRequest,
            )

            # Part 9: clamp caller-controlled dimensions to the domain bounds
            # (mirrors the provider-side clamp that applied before: min()
            # semantics preserved; per-item lengths still reject as invalid).
            # Default page 10 (not 100): the investigation budget is 25 items
            # over ~10 iterations, so one broad action must not bank the whole
            # corpus — best-first ranking makes the top-10 the most relevant.
            # Callers may still request up to 200; persistence truncates to
            # the remaining budget regardless (see _bank above).
            req = RuntimeEvidenceRequest(
                environment=environment,
                keywords=_shape_search_keywords(parameters.get("keywords", [])) or [],
                services=list(parameters.get("services", []))[:10] or [],
                limit=min(int(parameters.get("limit", 10)), 200),
            )
            qres = None
            try:
                qres = await gateway.search_runtime_evidence(
                    tenant_id, investigation_id, application_id or "example-app", req
                )
            except Exception as exc:
                from investigation_agent_platform.domain.common.exceptions import (
                    ExecutionError as _ExecError,
                )
                from investigation_agent_platform.domain.common.exceptions import (
                    ProviderNotConfiguredError as _ProviderMissing,
                )

                if not isinstance(exc, (_ProviderMissing, _ExecError)):
                    raise
                logger.warning(
                    "SEARCH_LOGS provider absent; degrades to empty",
                    extra=_log_extra(
                        tenant_id=tenant_id,
                        investigation_id=inv_id_str,
                        event="search_logs provider absent",
                        error=str(exc),
                    ),
                )
                outcome_detail["status"] = "DEGRADED"
                outcome_detail["miss_reason"] = "runtime_provider_not_configured"
                return 0
            banked = _bank(qres.items)
            if evidence_repo is not None and banked:
                await evidence_repo.save_batch(tenant_id, banked, investigation_id)
            # Log-group persistence: group banked log rows and persist one
            # exemplar row per group (same member IDs verbatim, separate
            # table). Fail-open: exemplar failures never break raw banking.
            try:
                await _persist_log_exemplars(ctx, tenant_id, investigation_id, banked)
            except Exception as exc:
                logger.warning(
                    "Log exemplar persist skipped; raw banking stands",
                    extra=_log_extra(
                        tenant_id=tenant_id,
                        investigation_id=inv_id_str,
                        event="log exemplars skipped",
                        error=str(exc),
                    ),
                )
            return _record_outcome(banked)

        async def _handle_get_code() -> int:
            from investigation_agent_platform.domain.common.exceptions import (
                CodePathNotFoundError,
                CredentialProviderError,
                ExecutionError,
                ProviderNotConfiguredError,
                ProviderTimeoutException,
                ProviderUnavailableException,
                RateLimitException,
            )
            from investigation_agent_platform.domain.evidence.requests import SourceRequest

            def _opt_line(value: Any) -> int | None:
                try:
                    number = int(str(value))
                except (TypeError, ValueError):
                    return None
                return number if number >= 1 else None

            req = SourceRequest(
                repository=application_id or "example-app",
                file_path=str(parameters.get("file_path", "")),
                # P1-2: revision/range are honored end to end ("HEAD" =
                # adapter resolves the profile default branch, previous
                # behavior — never silently substitutes another ref).
                revision=str(parameters.get("revision") or "").strip() or "HEAD",
                start_line=_opt_line(parameters.get("start_line")),
                end_line=_opt_line(parameters.get("end_line")),
            )
            if not req.file_path.strip():
                # Missing file path: agent omission, not a provider outage.
                # Degrade to empty (trace shows the attempt) instead of
                # failing the investigation — the escalation history steers
                # the reasoner away from repeating it. Unsafe paths still
                # fail closed in the validator/adapter below.
                logger.warning(
                    "GET_CODE without file_path degrades to empty",
                    extra=_log_extra(
                        tenant_id=tenant_id,
                        investigation_id=inv_id_str,
                        event="get_code missing file_path",
                    ),
                )
                outcome_detail["status"] = "DEGRADED"
                outcome_detail["miss_reason"] = "missing_file_path"
                return 0
            try:
                evidence = await gateway.get_source(
                    tenant_id, investigation_id, application_id or "example-app", req
                )
            except (
                ProviderTimeoutException,
                TimeoutError,
                CredentialProviderError,
                ProviderUnavailableException,
                RateLimitException,
                ConnectionError,
                ProviderNotConfiguredError,
                ExecutionError,
            ) as exc:
                # Phase 3.1: typed failure outcomes — a timed-out or
                # unauthorized git read is infrastructure, not absence.
                # CodePathNotFoundError below still owns genuine path misses.
                miss_reason, outcome_status, _blocking = classify_provider_failure(
                    exc, "code_provider_not_configured"
                )
                outcome_detail["status"] = outcome_status
                outcome_detail["miss_reason"] = miss_reason
                if isinstance(exc, RateLimitException):
                    outcome_detail["error_code"] = "RATE_LIMITED"
                logger.warning(
                    "GET_CODE provider failure; degrading",
                    extra=_log_extra(
                        tenant_id=tenant_id,
                        investigation_id=inv_id_str,
                        event="get_code provider failure",
                        error=str(exc),
                        miss_reason=miss_reason,
                        retry_after=getattr(exc, "retry_after", None),
                    ),
                )
                return 0
            except CodePathNotFoundError as exc:
                # Path miss (agent-guessed path not in the tree): exploration
                # miss, not infrastructure failure. Degrade the same way;
                # genuine repo outages still fail loud below.
                logger.warning(
                    "Code path not found; GET_CODE degrades to empty",
                    extra=_log_extra(
                        tenant_id=tenant_id,
                        investigation_id=inv_id_str,
                        event="get_code path miss",
                        error=str(exc),
                    ),
                )
                outcome_detail["status"] = "DEGRADED"
                outcome_detail["miss_reason"] = "code_path_not_found"
                return 0
            # Phase 0.4: label the read mode. A path-only read returns the
            # bounded file head, which is discovery — it cannot support a
            # definition claim ("X is implemented in Y"). Only an explicit
            # line range is a decisive-window read.
            read_mode = (
                "range" if (req.start_line is not None or req.end_line is not None) else "path_only"
            )
            merged_attributes = dict(getattr(evidence, "attributes", None) or {})
            merged_attributes["read_mode"] = read_mode
            if read_mode == "path_only":
                merged_attributes["discovery_only"] = True
            fact_field = str(parameters.get("field_name", "") or "").strip()
            fact_method = str(parameters.get("method_name", "") or "").strip()
            _evidence_path = str(
                getattr(
                    getattr(getattr(evidence, "provenance", None), "source_location", None),
                    "file_path",
                    "",
                )
                or ""
            )
            _is_python = _evidence_path.lower().endswith(".py")

            _version_repo = application_id or "example-app"

            async def _read_version_file(_name: str) -> str | None:
                # Trusted repo-declared version: read version files at the
                # analyzed SHA via the gateway (same authorization, never
                # banked). The dispatch module skips unpinned revisions.
                try:
                    _pinned = str(
                        getattr(
                            getattr(getattr(evidence, "provenance", None), "source_location", None),
                            "revision",
                            "",
                        )
                        or ""
                    )
                    _vreq = SourceRequest(
                        repository=_version_repo,
                        file_path=_name,
                        revision=_pinned,
                    )
                    _vev = await gateway.get_source(
                        tenant_id, investigation_id, _version_repo, _vreq
                    )
                except Exception:
                    return None
                _vattrs = getattr(_vev, "attributes", None) or {}
                if _vattrs.get("truncated"):
                    return None
                return str(getattr(_vev, "content_snippet", "") or "") or None

            if fact_field and _is_python:
                # Python value resolution (scope/class/framework): V2 only,
                # never the Java field-shaped legacy key.
                try:
                    from investigation_agent_platform.application.analysis.python.dispatch import (
                        resolve_python_field_fact as _dispatch_python_field,
                    )

                    _py_resolution, _config_source = await _dispatch_python_field(
                        evidence,
                        field_name=fact_field,
                        declaring_type=(
                            str(parameters.get("declaring_type") or "").strip() or None
                        ),
                        class_scope=(
                            str(parameters.get("class_scope") or "").strip() or None
                        ),
                        function_scope=(
                            str(parameters.get("function_scope") or "").strip() or None
                        ),
                        read_version_file=_read_version_file,
                    )
                    if _py_resolution is not None:
                        merged_attributes["code_fact_resolution_v2"] = (
                            _py_resolution.model_dump(mode="json")
                        )
                        if _config_source is not None:
                            # Names the configuration source for the
                            # verifier's CONFIGURATION_SOURCE family
                            # (declaration file, never a value).
                            merged_attributes["configuration_source"] = _config_source
                        outcome_detail["code_fact_resolution_v2"] = (
                            _py_resolution.model_dump(mode="json")
                        )
                except Exception:
                    pass
            elif fact_field:
                from investigation_agent_platform.application.analysis.java_field_facts import (
                    resolve_java_field_fact,
                )

                resolution = resolve_java_field_fact(
                    evidence,
                    fact_field,
                    declaring_type=(str(parameters.get("declaring_type") or "").strip() or None),
                )
                merged_attributes["code_fact_resolution"] = resolution.model_dump(mode="json")
                outcome_detail["code_fact_resolution"] = resolution.model_dump(mode="json")
                try:
                    from investigation_agent_platform.domain.code.models import (
                        emit_dual_resolution_attributes,
                    )

                    dual = emit_dual_resolution_attributes(resolution)
                    merged_attributes["code_fact_resolution_v2"] = dual[
                        "code_fact_resolution_v2"
                    ]
                    outcome_detail["code_fact_resolution_v2"] = dual[
                        "code_fact_resolution_v2"
                    ]
                except Exception:
                    pass
            if fact_method and _is_python:
                # Python method questions: with a class scope this answers
                # "which implementation runs" via the static MRO; without a
                # scope it inventories the function's possible paths.
                try:
                    from investigation_agent_platform.application.analysis.python.dispatch import (
                        resolve_python_method_fact as _dispatch_python_method,
                    )

                    _method_resolution = await _dispatch_python_method(
                        evidence,
                        method_name=fact_method,
                        declaring_type=(
                            str(parameters.get("declaring_type") or "").strip() or None
                        ),
                        class_scope=(
                            str(parameters.get("class_scope") or "").strip() or None
                        ),
                        guard_condition=(
                            str(parameters.get("guard_condition") or "").strip() or None
                        ),
                        read_version_file=_read_version_file,
                    )
                    merged_attributes["code_fact_resolution_v2"] = (
                        _method_resolution.model_dump(mode="json")
                    )
                    outcome_detail["code_fact_resolution_v2"] = (
                        _method_resolution.model_dump(mode="json")
                    )
                except Exception:
                    pass
            elif fact_method:
                # Method-return-on-guard resolution (L5 class): the subject
                # is a method's guarded `return` literal, which the field
                # path can never resolve. Emits V2 SOURCE_VALUE_FACT only
                # (legacy CodeFactResolution is field-shaped by construction).
                try:
                    from investigation_agent_platform.application.analysis.java.method_return_facts import (
                        resolve_java_method_return_fact,
                    )

                    _method_resolution = resolve_java_method_return_fact(
                        evidence,
                        fact_method,
                        guard_condition=(
                            str(parameters.get("guard_condition") or "").strip() or None
                        ),
                        declaring_type=(
                            str(parameters.get("declaring_type") or "").strip() or None
                        ),
                    )
                    merged_attributes["code_fact_resolution_v2"] = (
                        _method_resolution.model_dump(mode="json")
                    )
                    outcome_detail["code_fact_resolution_v2"] = (
                        _method_resolution.model_dump(mode="json")
                    )
                except Exception:
                    pass
            evidence = evidence.model_copy(update={"attributes": merged_attributes})
            if evidence_repo is not None:
                await evidence_repo.save(tenant_id, evidence, investigation_id)
            outcome_detail["evidence_ids"] = [str(evidence.evidence_id)]
            # Phase 4.2: revision pin check. An unpinned read (HEAD/empty)
            # asserts nothing, so it vacuously matches; a pinned read must
            # carry the requested SHA in provenance.
            try:
                _requested = str(getattr(req, "revision", "") or "")
                _actual_location = getattr(
                    getattr(evidence, "provenance", None), "source_location", None
                )
                _actual = str(getattr(_actual_location, "revision", "") or "")
                _pinned = _requested not in ("", "HEAD", "UNPINNED")
                _emit_telemetry(
                    ctx,
                    tenant_id,
                    investigation_id,
                    "record_revision_check",
                    "GET_CODE",
                    (not _pinned) or (_actual == _requested),
                )
            except Exception:
                pass
            return 1

        async def _handle_walk_session_graph() -> int:
            """G-3: session-anchored entity-graph walk.

            Takes {session_id, depth}: anchor template first, then one hop
            template per relationship edge, binding parent-key values from
            parsed row payloads (best-effort JSON; unparseable rows stop
            expansion but stay banked). Depth ≤3, values ≤20 per step,
            budget-capped via _bank. All templates are profile-declared
            (operator or auto-derived) — no dynamic SQL.
            """
            import json as _json

            from investigation_agent_platform.domain.evidence.requests import (
                ApplicationStateRequest,
            )

            session_id = str(parameters.get("session_id", "") or "").strip()[:256]
            try:
                depth = max(1, min(int(parameters.get("depth", 2)), 3))
            except (TypeError, ValueError):
                depth = 2
            if not session_id:
                logger.warning(
                    "WALK_SESSION_GRAPH without session_id degrades to empty",
                    extra=_log_extra(
                        tenant_id=tenant_id,
                        investigation_id=inv_id_str,
                        event="walk session graph missing session",
                    ),
                )
                outcome_detail["status"] = "DEGRADED"
                outcome_detail["miss_reason"] = "missing_session_id"
                return 0

            def _parse_rows(item: Any) -> list[dict[str, Any]]:
                try:
                    attributes = getattr(item, "attributes", None) or {}
                    raw = attributes.get("raw_payload", None)
                    if raw is None:
                        raw = getattr(item, "content_snippet", "") or ""
                    if isinstance(raw, dict):
                        return [raw]
                    if isinstance(raw, str):
                        parsed = _json.loads(raw)
                        if isinstance(parsed, dict):
                            return [parsed]
                        if isinstance(parsed, list):
                            return [r for r in parsed if isinstance(r, dict)]
                    return []
                except Exception:
                    return []

            async def _run_template(template_id: str, binds: dict[str, Any]) -> list[Any]:
                try:
                    qres = await gateway.get_application_state(
                        tenant_id,
                        investigation_id,
                        application_id or "example-app",
                        ApplicationStateRequest(
                            environment=environment,
                            template_id=template_id,
                            parameters=dict(binds),
                            limit=50,
                        ),
                    )
                    return list(qres.items or [])
                except Exception as exc:
                    from investigation_agent_platform.domain.common.exceptions import (
                        ExecutionError as _ExecError,
                    )
                    from investigation_agent_platform.domain.common.exceptions import (
                        ProviderNotConfiguredError as _ProviderMissing,
                    )

                    if not isinstance(exc, (_ProviderMissing, _ExecError)):
                        raise
                    logger.warning(
                        "WALK_SESSION_GRAPH leg degraded",
                        extra=_log_extra(
                            tenant_id=tenant_id,
                            investigation_id=inv_id_str,
                            event="walk session graph leg degraded",
                            error=str(exc),
                        ),
                    )
                    return []

            collected: list[Any] = []
            anchor_rows = await _run_template("auto_session_anchor", {"session_id": session_id})
            if not anchor_rows:
                outcome_detail["status"] = "DEGRADED"
                outcome_detail["miss_reason"] = "session_anchor_empty"
                return _record_outcome([])
            collected.extend(anchor_rows)
            frontier: list[tuple[str, Any]] = []
            try:
                if profile_repo is not None and application_id:
                    profile = await profile_repo.get_by_application_id(tenant_id, application_id)
                    index = profile.query_template_index() if profile else {}
                else:
                    index = {}
            except Exception:
                index = {}
            hop = 0
            # Seed frontier from anchor rows: every scalar value is a
            # candidate parent key (bounded below).
            try:
                for item in anchor_rows:
                    for row in _parse_rows(item):
                        for value in row.values():
                            if isinstance(value, (str, int)) and str(value).strip():
                                frontier.append((str(value).strip()[:256], hop))
                                if len(frontier) >= 20:
                                    break
                        if len(frontier) >= 20:
                            break
            except Exception:
                pass
            template_names = []
            try:
                keys = list(index.keys() if hasattr(index, "keys") else [])
            except Exception:
                keys = []
            for key in keys:
                try:
                    short = str(key).split(".")[-1]
                    if short.startswith("auto_session_hop_") and key not in template_names:
                        template_names.append(str(key))
                except Exception:
                    continue
            while hop < depth and frontier and template_names:
                hop += 1
                next_frontier: list[tuple[str, Any]] = []
                for parent_value, _ in frontier[:20]:
                    for template_name in template_names[:5]:
                        rows = await _run_template(str(template_name), {"parent_key": parent_value})
                        collected.extend(rows)
                        try:
                            for item in rows:
                                for row in _parse_rows(item):
                                    for value in row.values():
                                        if isinstance(value, (str, int)) and str(value).strip():
                                            next_frontier.append((str(value).strip()[:256], hop))
                                            if len(next_frontier) >= 20:
                                                break
                                    if len(next_frontier) >= 20:
                                        break
                        except Exception:
                            continue
                        if len(next_frontier) >= 20:
                            break
                    if len(next_frontier) >= 20:
                        break
                frontier = next_frontier
            banked = _bank(collected)
            if evidence_repo is not None and banked:
                await evidence_repo.save_batch(tenant_id, banked, investigation_id)
            return _record_outcome(banked)

        async def _handle_search_ref_docs() -> int:
            """T1-1: reference-docs search banking DOCUMENTATION rows.
            Takes {query}: refdoc service top hits become citable evidence
            (document_path + source_revision provenance) so doc answers
            verify through the doc-validated path instead of capping at
            PARTIAL. Fail-open throughout.
            """
            from investigation_agent_platform.domain.common.exceptions import (
                ExecutionError,
            )
            from investigation_agent_platform.domain.evidence.models import (
                Evidence,
                EvidenceType,
            )
            from investigation_agent_platform.domain.provenance.models import (
                EvidenceFreshness,
                EvidenceProvenance,
                QueryFingerprint,
                SourceLocation,
            )

            query = str(parameters.get("query", "") or "").strip()[:1000]
            if not query:
                logger.warning(
                    "SEARCH_REF_DOCS without query degrades to empty",
                    extra=_log_extra(
                        tenant_id=tenant_id,
                        investigation_id=inv_id_str,
                        event="search ref docs missing query",
                    ),
                )
                outcome_detail["status"] = "DEGRADED"
                outcome_detail["miss_reason"] = "missing_query"
                return 0
            try:
                from investigation_agent_platform.application.reference.reference_service import (
                    ReferenceDocumentService,
                )

                chunk_repo = getattr(ctx, "reference_repo", None)
                if chunk_repo is None:
                    raise ExecutionError("reference repository unavailable")
                service = ReferenceDocumentService(
                    chunk_repo=chunk_repo,
                    profile_repo=getattr(ctx, "profile_repo", None),
                    embedder=None,
                )
                hits, _cursor = await service.search(
                    tenant_id,
                    query,
                    application_id=str(application_id or "") or None,
                    limit=10,
                )
            except Exception as exc:
                from investigation_agent_platform.domain.common.exceptions import (
                    ExecutionError as _ExecError,
                )
                from investigation_agent_platform.domain.common.exceptions import (
                    ProviderNotConfiguredError as _ProviderMissing,
                )

                if isinstance(exc, (_ProviderMissing, _ExecError)):
                    # Provider-absent (including no reference repository):
                    # degrade to empty, never fail the run.
                    logger.warning(
                        "SEARCH_REF_DOCS provider absent; degrades to empty",
                        extra=_log_extra(
                            tenant_id=tenant_id,
                            investigation_id=inv_id_str,
                            event="search ref docs provider absent",
                            error=str(exc),
                        ),
                    )
                    outcome_detail["status"] = "DEGRADED"
                    outcome_detail["miss_reason"] = "refdoc_provider_not_configured"
                    return 0
                raise
            now = datetime.now(UTC)
            banked_rows: list[Any] = []
            for hit in hits or []:
                try:
                    document_path = str(getattr(hit, "document_path", "") or "")
                    revision = str(getattr(hit, "source_revision", "") or "")
                    content = str(getattr(hit, "content", "") or "")
                    if not document_path:
                        continue
                    banked_rows.append(
                        Evidence(
                            tenant_id=tenant_id,
                            investigation_id=investigation_id,
                            evidence_type=EvidenceType.DOCUMENTATION,
                            provider="reference-docs",
                            source=f"refdoc://{document_path}",
                            title=f"Reference: {document_path}",
                            summary=(
                                f"Reference document {document_path} "
                                f"(revision {revision or 'unknown'}): "
                                f"{content[:800]}"
                            ),
                            content_snippet=content[:4000],
                            observed_at=now,
                            retrieved_at=now,
                            provenance=EvidenceProvenance(
                                tenant_id=tenant_id,
                                investigation_id=investigation_id,
                                provider_type="reference-docs",
                                requested_provider_id="reference-docs",
                                actual_provider_id="reference-docs",
                                source_system="reference-docs",
                                retrieval_timestamp=now,
                                query_fingerprint=QueryFingerprint(
                                    provider_type="reference-docs",
                                    operation="search_reference_docs",
                                    normalized_query_hash=query[:64],
                                ),
                                source_location=SourceLocation(
                                    system="reference-docs",
                                    identifier=document_path[:256],
                                    file_path=document_path,
                                    revision=revision or "unknown",
                                ),
                            ),
                            freshness=EvidenceFreshness(observed_at=now, retrieved_at=now),
                            fingerprint=f"refdoc-{tenant_id}-{investigation_id}-{document_path}"[
                                :128
                            ],
                        )
                    )
                except Exception:
                    continue
            banked = _bank(banked_rows)
            if evidence_repo is not None and banked:
                await evidence_repo.save_batch(tenant_id, banked, investigation_id)
            if not banked:
                outcome_detail["status"] = "DEGRADED"
                outcome_detail["miss_reason"] = "refdoc_no_hits"
            return _record_outcome(banked)

        async def _handle_trace_journey() -> int:
            """G-1: session → tracking-ID spine from log rows.

            Takes {session_id} or {tracking_id}: searches logs for the
            identifier across services, extracts per-row tracking/service/
            timestamp via the profile's declared identity keys, and banks
            one JOURNEY spine row (ordered steps). Fail-open throughout.
            """
            from investigation_agent_platform.application.evidence.journey import (
                build_journey_spine,
            )
            from investigation_agent_platform.domain.evidence.models import (
                Evidence,
                EvidenceType,
            )
            from investigation_agent_platform.domain.evidence.requests import (
                RuntimeEvidenceRequest,
            )
            from investigation_agent_platform.domain.provenance.models import (
                EvidenceFreshness,
                EvidenceProvenance,
                QueryFingerprint,
                SourceLocation,
            )

            session_id = str(parameters.get("session_id", "") or "").strip()[:256]
            tracking_id = str(parameters.get("tracking_id", "") or "").strip()[:256]
            journey_id = session_id or tracking_id
            if not journey_id:
                logger.warning(
                    "TRACE_JOURNEY without session_id/tracking_id degrades to empty",
                    extra=_log_extra(
                        tenant_id=tenant_id,
                        investigation_id=inv_id_str,
                        event="trace_journey missing identifier",
                    ),
                )
                outcome_detail["status"] = "DEGRADED"
                outcome_detail["miss_reason"] = "missing_journey_id"
                return 0

            def _nested_get(row: Any, dotted: str) -> Any:
                try:
                    current: Any = row
                    for part in str(dotted or "").split("."):
                        if isinstance(current, dict):
                            current = current.get(part, None)
                        else:
                            return None
                        if current is None:
                            return None
                    return current
                except Exception:
                    return None

            service_keys: list[tuple[str, str, str]] = []
            try:
                if profile_repo is not None and application_id:
                    profile = await profile_repo.get_by_application_id(tenant_id, application_id)
                    for svc in getattr(profile, "services", []) or []:
                        telemetry = getattr(svc, "telemetry", None)
                        service_keys.append(
                            (
                                str(getattr(svc, "name", "") or ""),
                                str(getattr(telemetry, "session_key", "") or ""),
                                str(getattr(telemetry, "tracking_key", "") or ""),
                            )
                        )
            except Exception:
                service_keys = []
            try:
                qres = await gateway.search_runtime_evidence(
                    tenant_id,
                    investigation_id,
                    application_id or "example-app",
                    RuntimeEvidenceRequest(
                        environment=environment,
                        keywords=[journey_id],
                        services=[],
                        limit=100,
                    ),
                )
            except Exception as exc:
                from investigation_agent_platform.domain.common.exceptions import (
                    ExecutionError as _ExecError,
                )
                from investigation_agent_platform.domain.common.exceptions import (
                    ProviderNotConfiguredError as _ProviderMissing,
                )

                if not isinstance(exc, (_ProviderMissing, _ExecError)):
                    raise
                logger.warning(
                    "TRACE_JOURNEY provider absent; degrades to empty",
                    extra=_log_extra(
                        tenant_id=tenant_id,
                        investigation_id=inv_id_str,
                        event="trace_journey provider absent",
                        error=str(exc),
                    ),
                )
                outcome_detail["status"] = "DEGRADED"
                outcome_detail["miss_reason"] = "runtime_provider_not_configured"
                return 0
            flat_rows: list[dict[str, Any]] = []
            for item in qres.items or []:
                try:
                    attributes = getattr(item, "attributes", None) or {}
                    raw = attributes.get("raw_payload", None)
                    if isinstance(raw, dict):
                        record = raw
                    else:
                        import json as _json

                        try:
                            record = _json.loads(
                                str(
                                    attributes.get("raw_payload", None)
                                    or getattr(item, "content_snippet", "")
                                    or ""
                                )
                            )
                            if not isinstance(record, dict):
                                record = {}
                        except Exception:
                            record = {}
                    tracking_value = ""
                    service_value = ""
                    for svc_name, session_key, tracking_key in service_keys:
                        for candidate in (tracking_key, session_key):
                            if candidate:
                                found = _nested_get(record, candidate)
                                if found:
                                    if candidate == tracking_key and not tracking_value:
                                        tracking_value = str(found)
                    if not service_value:
                        for service_candidate in (
                            "service.name",
                            "service_name",
                            "service",
                            "labels.service",
                            "serviceName",
                        ):
                            found = _nested_get(record, service_candidate)
                            if found:
                                service_value = str(found)
                                break
                    if not tracking_value:
                        for fallback in (
                            "trackingId",
                            "tracking_id",
                            "transactionId",
                            "transaction_id",
                            "trace.id",
                            "traceId",
                        ):
                            found = _nested_get(record, fallback)
                            if found:
                                tracking_value = str(found)
                                break
                    timestamp_value = str(
                        _nested_get(record, "@timestamp")
                        or _nested_get(record, "timestamp")
                        or getattr(item, "observed_at", "")
                        or ""
                    )
                    level_value = str(
                        _nested_get(record, "log.level")
                        or _nested_get(record, "level")
                        or _nested_get(record, "status")
                        or ""
                    )
                    flat_rows.append(
                        {
                            "tracking_id": tracking_value,
                            "service": service_value,
                            "timestamp": timestamp_value,
                            "status": level_value,
                        }
                    )
                except Exception:
                    continue
            spine = build_journey_spine(flat_rows, session_id=session_id or tracking_id)
            steps = spine.get("steps", []) or []
            if not steps:
                outcome_detail["status"] = "DEGRADED"
                outcome_detail["miss_reason"] = "journey_no_steps"
                return _record_outcome([])
            now = datetime.now(UTC)
            lines = [
                f"step {s.get('tracking_id') or '(unattributed)'}: "
                f"{', '.join(s.get('services', [])[:5])} "
                f"[{s.get('row_count', 0)} rows"
                f"{', ' + str(s.get('error_count', 0)) + ' errors' if s.get('error_count') else ''}]"
                for s in steps[:20]
            ]
            spine_row = Evidence(
                tenant_id=tenant_id,
                investigation_id=investigation_id,
                evidence_type=EvidenceType.JOURNEY,
                provider="journey-engine",
                source=f"journey://{journey_id}",
                title=f"Journey spine for {journey_id} ({len(steps)} steps)",
                summary=(
                    f"Session journey {journey_id}: {len(steps)} tracking step(s). "
                    + "; ".join(lines)[:1500]
                ),
                content_snippet="; ".join(lines)[:4000],
                observed_at=now,
                retrieved_at=now,
                provenance=EvidenceProvenance(
                    tenant_id=tenant_id,
                    investigation_id=investigation_id,
                    provider_type="journey-engine",
                    requested_provider_id="journey-engine",
                    actual_provider_id="journey-engine",
                    source_system="log-journey",
                    retrieval_timestamp=now,
                    query_fingerprint=QueryFingerprint(
                        provider_type="journey-engine",
                        operation="trace_journey",
                        normalized_query_hash=journey_id[:64],
                    ),
                    source_location=SourceLocation(
                        system="journey-engine",
                        identifier=journey_id[:256],
                    ),
                ),
                freshness=EvidenceFreshness(observed_at=now, retrieved_at=now),
                fingerprint=f"journey-{tenant_id}-{investigation_id}-{journey_id}"[:128],
            )
            banked = _bank([spine_row])
            if evidence_repo is not None and banked:
                await evidence_repo.save_batch(tenant_id, banked, investigation_id)
            return _record_outcome(banked)

        async def _handle_correlate() -> int:
            # G7: LLM-supplied IDs are untrusted exploration input, not a
            # contract — unparseable entries degrade (EMPTY + miss_reason),
            # never WORKFLOW_ERROR. A malformed parameter must steer the
            # reasoner, not kill the run.
            root_ids, dropped = _coerce_root_ids(parameters)
            try:
                max_depth = min(int(parameters.get("max_depth", 2)), app_correlation_depth)
            except (TypeError, ValueError):
                max_depth = min(2, app_correlation_depth)
            if not root_ids:
                logger.warning(
                    "CORRELATE with no parseable root IDs degrades to empty",
                    extra=_log_extra(
                        tenant_id=tenant_id,
                        investigation_id=inv_id_str,
                        event="correlate roots unparseable",
                        dropped=dropped,
                    ),
                )
                outcome_detail["status"] = "DEGRADED"
                outcome_detail["miss_reason"] = "unparseable_root_ids"
                return 0
            qres = await gateway.correlate(
                tenant_id, investigation_id, application_id or "example-app", root_ids, max_depth
            )
            banked = _bank(qres.items)
            # P1-2: the gateway returns the graph in provider_metadata with
            # items=[] — bank one CALL_GRAPH summary so the traversal result
            # reaches reasoning instead of vanishing (items_count=0 before).
            graph = (qres.provider_metadata or {}).get("graph") or {}
            nodes = graph.get("nodes", []) or []
            edges = graph.get("edges", []) or []
            if nodes or edges:
                from investigation_agent_platform.domain.evidence.models import (
                    Evidence,
                    EvidenceType,
                )

                edge_lines = [
                    f"{e.get('source_id')} -[{e.get('relationship_type')}]-> {e.get('target_id')}"
                    for e in edges[:40]
                    if isinstance(e, dict)
                ]
                import hashlib as _hashlib

                from investigation_agent_platform.domain.provenance.models import (
                    EvidenceFreshness,
                    EvidenceProvenance,
                    QueryFingerprint,
                    SourceLocation,
                )

                now = datetime.now(UTC)
                fp = _hashlib.sha256(
                    f"correlate:{tenant_id}:{sorted(str(r) for r in root_ids)}:{max_depth}".encode()
                ).hexdigest()
                banked = list(banked) + [
                    Evidence(
                        tenant_id=tenant_id,
                        investigation_id=investigation_id,
                        evidence_type=EvidenceType.CALL_GRAPH,
                        provider="correlation-engine",
                        source=f"correlation://{investigation_id}",
                        title=f"Correlation: {len(nodes)} nodes, {len(edges)} edges",
                        summary=(
                            f"Correlation from {len(root_ids)} root(s) at depth "
                            f"{max_depth}: {len(nodes)} nodes, {len(edges)} edges."
                        ),
                        content_snippet="\n".join(edge_lines)[:4000],
                        observed_at=now,
                        retrieved_at=now,
                        provenance=EvidenceProvenance(
                            tenant_id=tenant_id,
                            investigation_id=investigation_id,
                            provider_type="correlation",
                            requested_provider_id="correlation-engine",
                            actual_provider_id="correlation-engine",
                            source_system="evidence-relationships",
                            retrieval_timestamp=now,
                            query_fingerprint=QueryFingerprint(
                                provider_type="correlation",
                                operation="correlate",
                                normalized_query_hash=fp[:32],
                            ),
                            source_location=SourceLocation(
                                system="evidence-relationships",
                                identifier=str(investigation_id),
                            ),
                        ),
                        freshness=EvidenceFreshness(observed_at=now, retrieved_at=now),
                        fingerprint=f"correlate-{fp[:32]}",
                    )
                ]
            if evidence_repo is not None and banked:
                await evidence_repo.save_batch(tenant_id, banked, investigation_id)
            return _record_outcome(banked)

        async def _handle_code_navigation(
            action_label: str,
            miss_label: str,
            build_request: Any,
            gateway_method: str,
            summarize: Any = None,
        ) -> int:
            """Shared dispatch for SEARCH_CODE / FIND_SYMBOL /
            FIND_CALL_GRAPH / GET_CODE_HISTORY (P1-1).

            All four resolve through existing gateway methods with full
            sanitization/dedup; provider-absent degrades to EMPTY with a
            miss reason instead of failing the investigation. `summarize`
            optionally converts provider_metadata into bankable evidence
            (used where the gateway returns metadata-only results).
            """
            from investigation_agent_platform.domain.common.exceptions import (
                CredentialProviderError,
                ExecutionError,
                ProviderNotConfiguredError,
                ProviderTimeoutException,
                ProviderUnavailableException,
                RateLimitException,
                UnsupportedCapabilityError,
            )

            try:
                req = build_request()
            except Exception as exc:
                logger.warning(
                    "%s invalid parameters degrades to empty",
                    action_label,
                    extra=_log_extra(
                        tenant_id=tenant_id,
                        investigation_id=inv_id_str,
                        event=f"{action_label} invalid parameters",
                        error=str(exc),
                    ),
                )
                outcome_detail["status"] = "DEGRADED"
                outcome_detail["miss_reason"] = "invalid_parameters"
                return 0
            try:
                method = getattr(gateway, gateway_method)
                qres = await method(
                    tenant_id, investigation_id, application_id or "example-app", req
                )
            except UnsupportedCapabilityError as exc:
                # The provider cannot honor a required revision. This is
                # unproven, not absent — surface it distinctly so the loop
                # never reads "capability gap" as "no such symbol".
                logger.warning(
                    "%s capability unsupported; degrading",
                    action_label,
                    extra=_log_extra(
                        tenant_id=tenant_id,
                        investigation_id=inv_id_str,
                        event=f"{action_label} unsupported capability",
                        error=str(exc),
                    ),
                )
                outcome_detail["status"] = "DEGRADED"
                outcome_detail["miss_reason"] = "unsupported_capability"
                outcome_detail["capability"] = getattr(exc, "capability", None)
                return 0
            except (
                ProviderTimeoutException,
                TimeoutError,
                CredentialProviderError,
                ProviderUnavailableException,
                RateLimitException,
                ConnectionError,
                ProviderNotConfiguredError,
                ExecutionError,
            ) as exc:
                # Phase 3.1: typed failure outcomes. Timeouts (after gateway
                # retries exhaust), auth blocks, and unavailability surface
                # distinctly — a timeout is transient, an expired credential
                # blocks the run, and none of them is "provider absent".
                miss_reason, outcome_status, _blocking = classify_provider_failure(exc, miss_label)
                outcome_detail["status"] = outcome_status
                outcome_detail["miss_reason"] = miss_reason
                if isinstance(exc, RateLimitException):
                    outcome_detail["error_code"] = "RATE_LIMITED"
                logger.warning(
                    "%s degrades to empty",
                    action_label,
                    extra=_log_extra(
                        tenant_id=tenant_id,
                        investigation_id=inv_id_str,
                        event=f"{action_label} provider failure",
                        error=str(exc),
                        miss_reason=miss_reason,
                        retry_after=getattr(exc, "retry_after", None),
                    ),
                )
                return 0
            banked = _bank(qres.items)
            # D4 fuzzy orientation is otherwise dropped at banking (the
            # fallback returns metadata-only results): copy the latest hint
            # into the outcome side-channel so the next reasoning turn can
            # use it as non-citable steering.
            try:
                _meta = getattr(qres, "provider_metadata", None) or {}
                _fuzzy = _meta.get("fuzzy_orientation") if isinstance(_meta, dict) else None
                if isinstance(_fuzzy, dict) and _fuzzy:
                    outcome_detail["fuzzy_orientation"] = _fuzzy
            except Exception:
                pass
            if summarize is not None:
                try:
                    banked = list(banked) + list(summarize(qres) or [])
                except Exception as exc:
                    logger.warning(
                        "%s summary failed; keeping items",
                        action_label,
                        extra=_log_extra(
                            tenant_id=tenant_id,
                            investigation_id=inv_id_str,
                            event=f"{action_label} summarize failed",
                            error=str(exc),
                        ),
                    )
            if evidence_repo is not None and banked:
                await evidence_repo.save_batch(tenant_id, banked, investigation_id)
            if not banked and evidence_repo is not None:
                # Phase 2 item 3: a well-formed query with zero matches banks
                # a scoped no-match proof for future absence claims. Saved
                # directly (never counted in the outcome), so replan-on-
                # empties still fires and the row stays citable via repo
                # scan. Fingerprint dedupes repeats idempotently.
                try:
                    from investigation_agent_platform.domain.evidence.models import (
                        EvidenceType as _EvidenceType,
                    )

                    _absence_scope = _code_absence_scope(action_label, req)
                    if _absence_scope:
                        await evidence_repo.save(
                            tenant_id,
                            _absence_proof_row(
                                tenant_id=tenant_id,
                                investigation_id=investigation_id,
                                evidence_type=(
                                    _EvidenceType.COMMIT_HISTORY
                                    if gateway_method == "get_code_history"
                                    else _EvidenceType.SOURCE_CODE
                                ),
                                provider="code-intelligence",
                                operation=action_label.lower(),
                                scope=_absence_scope,
                            ),
                            investigation_id,
                        )
                except Exception:
                    pass
            return _record_outcome(banked)

        async def _handle_search_code() -> int:
            from investigation_agent_platform.domain.evidence.requests import (
                CodeSearchRequest,
            )

            def _build() -> CodeSearchRequest:
                return CodeSearchRequest(
                    repository=application_id or "example-app",
                    revision=str(parameters.get("revision", "HEAD")),
                    query=str(parameters.get("query", "")),
                    language=parameters.get("language"),
                    path_prefix=parameters.get("path_prefix"),
                    limit=min(int(parameters.get("limit", 20)), 100),
                    # Fail-closed opt-in: when the caller requires the
                    # provider to honor a pinned revision, an incapable
                    # provider raises UNSUPPORTED_CAPABILITY instead of
                    # silently returning HEAD rows. Default stays discovery.
                    require_revision=bool(parameters.get("require_revision", False)),
                )

            return await _handle_code_navigation(
                "SEARCH_CODE", "code_provider_not_configured", _build, "search_code"
            )

        async def _handle_find_symbol() -> int:
            from investigation_agent_platform.domain.evidence.requests import (
                SymbolRequest,
            )

            def _build() -> SymbolRequest:
                return SymbolRequest(
                    repository=application_id or "example-app",
                    revision=str(parameters.get("revision", "HEAD")),
                    symbol_name=str(parameters.get("symbol_name", "")),
                    require_revision=bool(parameters.get("require_revision", False)),
                )

            def _summarize(qres: Any) -> list[Any]:
                # Phase 0.2: gateway returns symbols in metadata with
                # items=[]; bank typed discovery packets so the symbol
                # reaches reasoning instead of vanishing.
                meta = getattr(qres, "provider_metadata", None) or {}
                symbols = meta.get("symbols", []) or []
                outcome_detail["_symbol_count"] = len(symbols)
                if not symbols:
                    _emit_telemetry(
                        ctx, tenant_id, investigation_id, "record_symbol_empty", "FIND_SYMBOL"
                    )
                return _symbol_evidence_packets(
                    tenant_id=tenant_id,
                    investigation_id=investigation_id,
                    symbols=symbols,
                    symbol_name=str(parameters.get("symbol_name", "")),
                )

            banked_count = await _handle_code_navigation(
                "FIND_SYMBOL",
                "code_provider_not_configured",
                _build,
                "find_symbol",
                summarize=_summarize,
            )
            if banked_count == 0:
                outcome_detail.pop("_symbol_count", None)
            return banked_count

        async def _handle_walk_stack() -> int:
            """D2: resolve stack frames to ordered EXCEPTION_PATH evidence.

            For each frame (max 10): search the function name, take the
            first hit carrying file+revision provenance, and bank one
            frame-ordered row (frame file/line preferred, hit fallback).
            Frames resolving nowhere are banked as explicit unresolved
            rows (no file/line/revision, never a synthetic location) and
            counted in the miss detail, not failed. Zero resolutions
            degrades with miss_reason.
            """
            from investigation_agent_platform.domain.evidence.models import (
                Evidence,
                EvidenceType,
            )
            from investigation_agent_platform.domain.evidence.requests import (
                CodeSearchRequest,
            )
            from investigation_agent_platform.domain.provenance.models import (
                EvidenceFreshness,
                EvidenceProvenance,
                QueryFingerprint,
                SourceLocation,
            )

            raw_frames = parameters.get("frames", [])
            frames = [f for f in (raw_frames or []) if isinstance(f, dict)][:10]
            error_class = str(parameters.get("error_class", "") or "")[:256]
            if not frames:
                logger.warning(
                    "WALK_STACK without frames degrades to empty",
                    extra=_log_extra(
                        tenant_id=tenant_id,
                        investigation_id=inv_id_str,
                        event="walk_stack missing frames",
                    ),
                )
                outcome_detail["status"] = "DEGRADED"
                outcome_detail["miss_reason"] = "missing_frames"
                return 0
            now = datetime.now(UTC)
            resolved = 0
            unresolved = 0
            banked_rows: list[Any] = []

            def _unresolved_frame_row(
                frame_index: int,
                func_name: str,
                file_hint_value: str,
                reason: str,
            ) -> Any:
                """Explicit unresolved frame — never a synthetic location.

                The row carries no file_path/line/revision provenance, so
                `_cited_location_evidence` excludes it by construction. It
                exists so the reasoner sees *which* frame failed and why,
                instead of a silent count in miss_reason.
                """
                return Evidence(
                    tenant_id=tenant_id,
                    investigation_id=investigation_id,
                    evidence_type=EvidenceType.EXCEPTION_PATH,
                    provider="code-intelligence",
                    source=f"stack://frame-{frame_index}/unresolved",
                    title=f"Frame {frame_index}: unresolved ({reason})",
                    summary=(
                        f"Stack frame {frame_index} "
                        f"({func_name or file_hint_value or 'unknown'}) "
                        f"could not be resolved: {reason}."
                    ),
                    content_snippet="",
                    attributes={
                        "unresolved": True,
                        "reason": reason,
                        "frame_index": frame_index,
                        "frame_file": file_hint_value,
                        "frame_function": func_name,
                    },
                    observed_at=now,
                    retrieved_at=now,
                    provenance=EvidenceProvenance(
                        tenant_id=tenant_id,
                        investigation_id=investigation_id,
                        provider_type="code-intelligence",
                        requested_provider_id="treesitter-local",
                        actual_provider_id="treesitter-local",
                        source_system="stack-walk",
                        retrieval_timestamp=now,
                        query_fingerprint=QueryFingerprint(
                            provider_type="code-intelligence",
                            operation="walk_stack",
                            normalized_query_hash="unresolved",
                        ),
                        source_location=SourceLocation(
                            system="git",
                            identifier=f"frame-{frame_index}-unresolved",
                            file_path=None,
                            line_start=None,
                            line_end=None,
                            revision=None,
                        ),
                    ),
                    freshness=EvidenceFreshness(observed_at=now, retrieved_at=now),
                    fingerprint=_stack_fingerprint(
                        tenant_id,
                        investigation_id,
                        frame_index,
                        f"unresolved:{reason}:{file_hint_value}",
                        0,
                    ),
                )

            for index, frame in enumerate(frames):
                try:
                    func = str(frame.get("function", "") or "").strip()
                    file_hint = str(frame.get("file", "") or "").strip()
                    try:
                        frame_line = int(frame.get("line") or 0) or None
                    except (TypeError, ValueError):
                        frame_line = None
                    query = func or file_hint
                    if not query:
                        banked_rows.append(
                            _unresolved_frame_row(index, func, file_hint, "missing_query")
                        )
                        unresolved += 1
                        continue
                    qres = await gateway.search_code(
                        tenant_id,
                        investigation_id,
                        application_id or "example-app",
                        CodeSearchRequest(
                            repository=application_id or "example-app",
                            revision=str(parameters.get("revision", "HEAD")),
                            query=query,
                            limit=3,
                            # Deliberately discovery: this search only finds
                            # candidate files for the frame hint. The pin
                            # comes from the range-read confirm below, so a
                            # strict require_revision here would only turn
                            # resolvable frames into unresolved rows.
                        ),
                    )
                except Exception as exc:
                    logger.warning(
                        "WALK_STACK frame search failed; continuing",
                        extra=_log_extra(
                            tenant_id=tenant_id,
                            investigation_id=inv_id_str,
                            event="walk_stack frame failed",
                            error=str(exc),
                        ),
                    )
                    banked_rows.append(
                        _unresolved_frame_row(
                            index,
                            str(frame.get("function", "") or "").strip()
                            if isinstance(frame, dict)
                            else "",
                            str(frame.get("file", "") or "").strip()
                            if isinstance(frame, dict)
                            else "",
                            "frame_search_failed",
                        )
                    )
                    unresolved += 1
                    continue
                # Phase 0.3: a frame may only be banked when its file
                # actually matches the frame's file hint AND a range read
                # at the frame line succeeds (proving the line exists and
                # capturing the real revision). Never synthesize a location
                # from a mismatched hit.
                hit = None
                for candidate in qres.items or []:
                    try:
                        location = getattr(
                            getattr(candidate, "provenance", None),
                            "source_location",
                            None,
                        )
                        hit_file = str(getattr(location, "file_path", "") or "")
                        if hit_file and _frame_file_matches(file_hint, hit_file):
                            hit = candidate
                            break
                    except Exception:
                        continue
                if hit is None:
                    banked_rows.append(
                        _unresolved_frame_row(index, func, file_hint, "no_file_match")
                    )
                    unresolved += 1
                    continue
                try:
                    from investigation_agent_platform.domain.evidence.requests import (
                        SourceRequest,
                    )

                    hit_location = hit.provenance.source_location
                    file_path = str(getattr(hit_location, "file_path", "") or "")
                    if not file_path:
                        banked_rows.append(
                            _unresolved_frame_row(index, func, file_hint, "missing_file")
                        )
                        unresolved += 1
                        continue
                    try:
                        anchor_line = int(frame_line or getattr(hit_location, "line_start", 0) or 0)
                    except (TypeError, ValueError):
                        anchor_line = 0
                    if anchor_line < 1:
                        banked_rows.append(
                            _unresolved_frame_row(index, func, file_hint, "missing_line")
                        )
                        unresolved += 1
                        continue
                    frame_read = await gateway.get_source(
                        tenant_id,
                        investigation_id,
                        application_id or "example-app",
                        SourceRequest(
                            repository=application_id or "example-app",
                            file_path=file_path,
                            revision=str(parameters.get("revision", "HEAD")),
                            start_line=anchor_line,
                            end_line=anchor_line,
                        ),
                    )
                    read_location = getattr(
                        getattr(frame_read, "provenance", None),
                        "source_location",
                        None,
                    )
                    confirmed_line = getattr(read_location, "line_start", None)
                    if confirmed_line != anchor_line:
                        # The requested line was clamped (past EOF), so the
                        # frame does not resolve to that line. Bank it as
                        # unresolved instead of fabricating a location.
                        banked_rows.append(
                            _unresolved_frame_row(index, func, file_hint, "line_not_confirmed")
                        )
                        unresolved += 1
                        continue
                    banked_rows.append(frame_read)
                    banked_rows.append(
                        Evidence(
                            tenant_id=tenant_id,
                            investigation_id=investigation_id,
                            evidence_type=EvidenceType.EXCEPTION_PATH,
                            provider="code-intelligence",
                            source=f"stack://frame-{index}",
                            title=(
                                f"Frame {index}: {func or file_hint} ({file_path}:{anchor_line})"
                            ),
                            summary=(
                                f"Stack frame {index} ({error_class or 'error'}) "
                                f"resolves to {file_path}:{anchor_line} "
                                f"revision {getattr(read_location, 'revision', None)}."
                            ),
                            content_snippet=str(getattr(frame_read, "content_snippet", "") or "")[
                                :2000
                            ],
                            observed_at=now,
                            retrieved_at=now,
                            provenance=EvidenceProvenance(
                                tenant_id=tenant_id,
                                investigation_id=investigation_id,
                                provider_type="code-intelligence",
                                requested_provider_id="treesitter-local",
                                actual_provider_id="treesitter-local",
                                source_system="stack-walk",
                                retrieval_timestamp=now,
                                query_fingerprint=QueryFingerprint(
                                    provider_type="code-intelligence",
                                    operation="walk_stack",
                                    normalized_query_hash=str(
                                        getattr(
                                            getattr(hit, "provenance", None),
                                            "query_fingerprint",
                                            None,
                                        )
                                        and getattr(
                                            hit.provenance.query_fingerprint,
                                            "normalized_query_hash",
                                            "",
                                        )
                                        or ""
                                    ),
                                ),
                                source_location=SourceLocation(
                                    system="git",
                                    identifier=f"frame-{index}",
                                    file_path=file_path,
                                    line_start=anchor_line,
                                    line_end=anchor_line,
                                    revision=getattr(read_location, "revision", None),
                                ),
                            ),
                            freshness=EvidenceFreshness(observed_at=now, retrieved_at=now),
                            fingerprint=_stack_fingerprint(
                                tenant_id,
                                investigation_id,
                                index,
                                file_path,
                                anchor_line,
                            ),
                        )
                    )
                    resolved += 1
                except Exception as exc:
                    logger.warning(
                        "WALK_STACK row build failed; continuing",
                        extra=_log_extra(
                            tenant_id=tenant_id,
                            investigation_id=inv_id_str,
                            event="walk_stack row failed",
                            error=str(exc),
                        ),
                    )
                    banked_rows.append(
                        _unresolved_frame_row(index, func, file_hint, "row_build_failed")
                    )
                    unresolved += 1
                    continue
            banked = _bank(banked_rows)
            if evidence_repo is not None and banked:
                await evidence_repo.save_batch(tenant_id, banked, investigation_id)
            if resolved == 0:
                outcome_detail["status"] = "DEGRADED"
                outcome_detail["miss_reason"] = "walk_stack_no_resolution"
            else:
                outcome_detail["miss_reason"] = (
                    f"walk_stack_unresolved_{unresolved}" if unresolved else None
                )
            return _record_outcome(banked)

        async def _handle_trace_logic() -> int:
            """D2: resolve a consuming symbol and extract decision predicates.

            Takes {symbol, attributes[]}: FIND_SYMBOL locates the definition,
            GET_CODE reads it, and branch lines (if/switch/case/ternary)
            mentioning any attribute name are extracted as the decision
            path. Banks the range-read rows (SOURCE_CODE, citable) with the
            predicate lines recorded in miss/detail context. Generic across
            Java/Python (text-level predicates, never language AST).
            """
            import re as _re

            from investigation_agent_platform.domain.evidence.requests import (
                SymbolRequest,
            )

            symbol = str(parameters.get("symbol", "") or "").strip()
            raw_attributes = parameters.get("attributes", []) or []
            attributes = [
                str(a).strip()
                for a in (raw_attributes if isinstance(raw_attributes, list) else [])
                if str(a or "").strip()
            ][:20]
            if not symbol:
                logger.warning(
                    "TRACE_LOGIC without symbol degrades to empty",
                    extra=_log_extra(
                        tenant_id=tenant_id,
                        investigation_id=inv_id_str,
                        event="trace_logic missing symbol",
                    ),
                )
                outcome_detail["status"] = "DEGRADED"
                outcome_detail["miss_reason"] = "missing_symbol"
                return 0
            try:
                symbol_rows = await gateway.find_symbol(
                    tenant_id,
                    investigation_id,
                    application_id or "example-app",
                    SymbolRequest(
                        repository=application_id or "example-app",
                        revision=str(parameters.get("revision", "HEAD")),
                        symbol_name=symbol,
                        require_revision=bool(parameters.get("require_revision", False)),
                    ),
                )
            except Exception as exc:
                logger.warning(
                    "TRACE_LOGIC symbol lookup failed; degrading",
                    extra=_log_extra(
                        tenant_id=tenant_id,
                        investigation_id=inv_id_str,
                        event="trace_logic symbol failed",
                        error=str(exc),
                    ),
                )
                try:
                    miss_reason, outcome_status, _blocking = classify_provider_failure(
                        exc, "trace_logic_symbol_miss"
                    )
                except Exception:
                    miss_reason, outcome_status = "trace_logic_symbol_miss", "DEGRADED"
                outcome_detail["status"] = outcome_status
                outcome_detail["miss_reason"] = miss_reason
                if miss_reason == "unsupported_capability":
                    outcome_detail["capability"] = getattr(exc, "capability", None)
                return 0
            # Phase 0.2/0.4: symbols arrive in provider_metadata (not
            # items); convert to discovery packets, then read a window
            # around each definition so predicate lines are anchored to
            # absolute line numbers and the read is a real range read.
            symbol_meta = getattr(symbol_rows, "provider_metadata", None) or {}
            definitions = _symbol_evidence_packets(
                tenant_id=tenant_id,
                investigation_id=investigation_id,
                symbols=symbol_meta.get("symbols", []) or [],
                symbol_name=symbol,
            )
            outcome_detail["_symbol_count"] = len(definitions)
            if not definitions:
                _emit_telemetry(
                    ctx, tenant_id, investigation_id, "record_symbol_empty", "TRACE_LOGIC"
                )
            banked_rows: list[Any] = list(definitions)
            predicate_lines: list[str] = []
            for packet in definitions[:3]:
                try:
                    from investigation_agent_platform.domain.evidence.requests import (
                        SourceRequest,
                    )

                    location = getattr(packet.provenance, "source_location", None)
                    file_path = str(getattr(location, "file_path", "") or "")
                    if not file_path:
                        continue
                    try:
                        anchor_line = int(getattr(location, "line_start", 0) or 0)
                    except (TypeError, ValueError):
                        anchor_line = 0
                    window_start = max(1, anchor_line - 120) if anchor_line else None
                    window_end = anchor_line + 120 if anchor_line else None
                    code = await gateway.get_source(
                        tenant_id,
                        investigation_id,
                        application_id or "example-app",
                        SourceRequest(
                            repository=application_id or "example-app",
                            file_path=file_path,
                            revision=str(parameters.get("revision", "HEAD")),
                            start_line=window_start,
                            end_line=window_end,
                        ),
                    )
                    banked_rows.append(code)
                    if attributes:
                        lowered_attrs = [a.lower() for a in attributes]
                        base_line = window_start or 1
                        for offset, line in enumerate(
                            str(getattr(code, "content_snippet", "") or "").splitlines()
                        ):
                            lowered = line.lower()
                            if _re.search(
                                r"\b(if|else|switch|case|when|elif|match)\b", lowered
                            ) and any(attr in lowered for attr in lowered_attrs):
                                predicate_lines.append(
                                    f"{file_path}:{base_line + offset}: {line.strip()[:160]}"
                                )
                                if len(predicate_lines) >= 20:
                                    break
                except Exception:
                    continue
            banked = _bank(banked_rows)
            if evidence_repo is not None and banked:
                await evidence_repo.save_batch(tenant_id, banked, investigation_id)
            if not banked:
                outcome_detail["status"] = "DEGRADED"
                outcome_detail["miss_reason"] = "trace_logic_no_resolution"
            else:
                outcome_detail["miss_reason"] = None
                outcome_detail["predicate_lines"] = predicate_lines[:20]
            return _record_outcome(banked)

        async def _handle_find_call_graph() -> int:
            from investigation_agent_platform.domain.evidence.requests import (
                CallGraphRequest,
            )

            def _build() -> CallGraphRequest:
                return CallGraphRequest(
                    repository=application_id or "example-app",
                    revision=str(parameters.get("revision", "HEAD")),
                    symbol=str(parameters.get("symbol", "")),
                    direction=str(parameters.get("direction", "BOTH")),
                    depth=min(int(parameters.get("depth", 2)), 5),
                    require_revision=bool(parameters.get("require_revision", False)),
                )

            def _summarize(qres: Any) -> list[Any]:
                # P1-2: gateway returns call nodes in metadata with items=[]
                # — bank one CALL_GRAPH summary so the result reaches
                # reasoning instead of vanishing like CORRELATE used to.
                meta = getattr(qres, "provider_metadata", None) or {}
                callers = meta.get("callers", []) or []
                callees = meta.get("callees", []) or []
                outcome_detail["_graph_callers"] = len(callers)
                outcome_detail["_graph_callees"] = len(callees)
                graph_edges: list[dict] = []
                for role, node in [("caller", c) for c in callers] + [
                    ("callee", c) for c in callees
                ]:
                    try:
                        if not isinstance(node, dict):
                            continue
                        loc = node.get("location") or {}
                        if not isinstance(loc, dict):
                            continue
                        try:
                            edge_line = int(loc.get("line_number") or 0) or None
                        except (TypeError, ValueError):
                            edge_line = None
                        graph_edges.append(
                            {
                                "role": role,
                                "caller": str(node.get("caller_symbol") or "?")[:256],
                                "callee": str(node.get("callee_symbol") or "?")[:256],
                                "file": str(loc.get("file_path") or "").strip(),
                                "line": edge_line,
                            }
                        )
                    except Exception:
                        continue
                outcome_detail["_graph_edges"] = graph_edges[:10]
                if not callers and not callees:
                    return []
                import hashlib as _hashlib

                from investigation_agent_platform.domain.evidence.models import (
                    Evidence,
                    EvidenceType,
                )
                from investigation_agent_platform.domain.provenance.models import (
                    EvidenceFreshness,
                    EvidenceProvenance,
                    QueryFingerprint,
                    SourceLocation,
                )

                now = datetime.now(UTC)
                symbol = str(parameters.get("symbol", ""))
                lines = [
                    f"{(c.get('caller_symbol') or '?')} -> {(c.get('callee_symbol') or '?')} "
                    f"({(c.get('location') or {}).get('file_path', '?')})"
                    for c in (callers + callees)[:40]
                    if isinstance(c, dict)
                ]
                fp = _hashlib.sha256(
                    f"callgraph:{tenant_id}:{symbol}:{len(callers)}:{len(callees)}".encode()
                ).hexdigest()
                summary_row = Evidence(
                    tenant_id=tenant_id,
                    investigation_id=investigation_id,
                    evidence_type=EvidenceType.CALL_GRAPH,
                    provider="code-intelligence",
                    source=f"callgraph://{symbol}",
                    title=f"Call graph: {symbol} ({len(callers)} callers, {len(callees)} callees)",
                    summary=(
                        f"Call graph for '{symbol}': {len(callers)} caller(s), "
                        f"{len(callees)} callee(s)."
                    ),
                    content_snippet="\n".join(lines)[:4000],
                    attributes={
                        "discovery_only": True,
                        "revision_supported": False,
                    },
                    observed_at=now,
                    retrieved_at=now,
                    provenance=EvidenceProvenance(
                        tenant_id=tenant_id,
                        investigation_id=investigation_id,
                        provider_type="code-intelligence",
                        requested_provider_id="treesitter-local",
                        actual_provider_id="treesitter-local",
                        source_system="call-graph",
                        retrieval_timestamp=now,
                        query_fingerprint=QueryFingerprint(
                            provider_type="code-intelligence",
                            operation="find_call_graph",
                            normalized_query_hash=fp[:32],
                        ),
                        source_location=SourceLocation(
                            system="call-graph",
                            identifier=symbol,
                        ),
                    ),
                    freshness=EvidenceFreshness(observed_at=now, retrieved_at=now),
                    fingerprint=f"callgraph-{fp[:32]}",
                )
                # Per-edge packets: the nodes already carry file/line
                # provenance — bank each edge with it instead of leaving
                # only the location-less summary. Edges stay discovery_only
                # (a graph edge is not a shown call site) but now give the
                # model and the call-site forcing rule concrete files.
                edge_rows: list[Any] = []
                for edge in (callers + callees)[:10]:
                    try:
                        if not isinstance(edge, dict):
                            continue
                        loc = edge.get("location") or {}
                        if not isinstance(loc, dict):
                            continue
                        file_path = str(loc.get("file_path", "") or "").strip()
                        if not file_path:
                            continue
                        try:
                            edge_line = int(loc.get("line_number") or 0) or None
                        except (TypeError, ValueError):
                            edge_line = None
                        caller = str(edge.get("caller_symbol", "") or "?")[:256]
                        callee = str(edge.get("callee_symbol", "") or "?")[:256]
                        edge_fp = _hashlib.sha256(
                            f"calledge:{tenant_id}:{investigation_id}:{caller}:"
                            f"{callee}:{file_path}:{edge_line or 0}".encode()
                        ).hexdigest()[:64]
                        edge_rows.append(
                            Evidence(
                                tenant_id=tenant_id,
                                investigation_id=investigation_id,
                                evidence_type=EvidenceType.CALL_GRAPH,
                                provider="code-intelligence",
                                source=f"callgraph://{caller}->{callee}",
                                title=f"Call edge: {caller} -> {callee}",
                                summary=(
                                    f"'{caller}' calls '{callee}' "
                                    f"({file_path}"
                                    f"{':' + str(edge_line) if edge_line else ''})."
                                ),
                                content_snippet=(
                                    f"{caller} -> {callee} "
                                    f"({file_path}"
                                    f"{':' + str(edge_line) if edge_line else ''})"
                                )[:2000],
                                attributes={
                                    "discovery_only": True,
                                    "revision_supported": bool(
                                        loc.get("revision_supported", False)
                                    ),
                                    "caller_symbol": caller,
                                    "callee_symbol": callee,
                                },
                                observed_at=now,
                                retrieved_at=now,
                                provenance=EvidenceProvenance(
                                    tenant_id=tenant_id,
                                    investigation_id=investigation_id,
                                    provider_type="code-intelligence",
                                    requested_provider_id="treesitter-local",
                                    actual_provider_id="treesitter-local",
                                    source_system="call-graph",
                                    retrieval_timestamp=now,
                                    query_fingerprint=QueryFingerprint(
                                        provider_type="code-intelligence",
                                        operation="find_call_graph",
                                        normalized_query_hash=edge_fp[:32],
                                    ),
                                    source_location=SourceLocation(
                                        system="call-graph",
                                        identifier=f"{caller}->{callee}",
                                        file_path=file_path,
                                        line_start=edge_line,
                                        line_end=edge_line,
                                        revision=str(loc.get("revision", "") or "") or None,
                                    ),
                                ),
                                freshness=EvidenceFreshness(observed_at=now, retrieved_at=now),
                                fingerprint=edge_fp,
                            )
                        )
                    except Exception:
                        continue
                return [summary_row] + edge_rows

            banked_count = await _handle_code_navigation(
                "FIND_CALL_GRAPH",
                "code_provider_not_configured",
                _build,
                "find_call_graph",
                summarize=_summarize,
            )
            if banked_count == 0:
                outcome_detail.pop("_graph_callers", None)
                outcome_detail.pop("_graph_callees", None)
                return await fallback_call_graph_to_search(
                    gateway=gateway,
                    tenant_id=tenant_id,
                    investigation_id=investigation_id,
                    application_id=application_id,
                    parameters=parameters,
                    evidence_repo=evidence_repo,
                    bank=_bank,
                    record_outcome=_record_outcome,
                    outcome_detail=outcome_detail,
                    inv_id_str=inv_id_str,
                )
            _callers_n = outcome_detail.pop("_graph_callers", 1)
            _callees_n = outcome_detail.pop("_graph_callees", 1)
            if _callers_n == 0 or _callees_n == 0:
                extra = await fallback_call_graph_to_search(
                    gateway=gateway,
                    tenant_id=tenant_id,
                    investigation_id=investigation_id,
                    application_id=application_id,
                    parameters=parameters,
                    evidence_repo=evidence_repo,
                    bank=_bank,
                    record_outcome=_record_outcome,
                    outcome_detail=outcome_detail,
                    inv_id_str=inv_id_str,
                    union=True,
                )
                return banked_count + extra
            # Call-site chaining (L2 twice): a graph edge names the caller
            # file but no row shows the call expression the verifier
            # demands. Read a window around each caller edge (one per
            # file, bounded) so every visible caller gets a real
            # call-site row (TRACE_LOGIC pattern). Previously only the
            # first edge was read and the second caller never became
            # citable. Failures keep the graph results regardless.
            try:
                _edges = [
                    e for e in (outcome_detail.pop("_graph_edges", []) or []) if isinstance(e, dict)
                ]
                _targets = _caller_chain_targets(_edges)
                _banked_here: list[str] = []
                if evidence_repo is not None:
                    from investigation_agent_platform.domain.evidence.models import (
                        EvidenceType as _EvidenceType,
                    )
                    from investigation_agent_platform.domain.evidence.requests import (
                        SourceRequest as _SourceRequest,
                    )

                    for _target in _targets:
                        _anchor = int(_target["line"])
                        _read = await gateway.get_source(
                            tenant_id,
                            investigation_id,
                            application_id or "example-app",
                            _SourceRequest(
                                repository=application_id or "example-app",
                                file_path=str(_target["file"]),
                                revision=str(parameters.get("revision", "HEAD")),
                                start_line=max(1, _anchor - 40),
                                end_line=_anchor + 40,
                            ),
                        )
                        _read = _read.model_copy(
                            update={
                                "evidence_type": _EvidenceType.SOURCE_CODE,
                                "attributes": {
                                    **(getattr(_read, "attributes", None) or {}),
                                    "read_mode": "range",
                                    "call_graph_role": "caller",
                                },
                            }
                        )
                        await evidence_repo.save(tenant_id, _read, investigation_id)
                        _banked_here.append(str(_read.evidence_id))
                if _banked_here:
                    try:
                        outcome_detail["evidence_ids"] = list(
                            outcome_detail.get("evidence_ids") or []
                        ) + _banked_here
                    except Exception:
                        pass
                    return banked_count + len(_banked_here)
            except Exception:
                pass
            return banked_count

        async def _handle_get_code_history() -> int:
            from investigation_agent_platform.domain.evidence.requests import (
                CodeHistoryRequest,
            )

            def _build(path: str | None = None) -> CodeHistoryRequest:
                return CodeHistoryRequest(
                    repository=application_id or "example-app",
                    file_path=str(path if path is not None else parameters.get("file_path", "")),
                    symbol=parameters.get("symbol"),
                    limit=min(int(parameters.get("limit", 10)), 50),
                )

            banked_count = await _handle_code_navigation(
                "GET_CODE_HISTORY",
                "code_provider_not_configured",
                _build,
                "get_code_history",
            )
            if banked_count > 0:
                return banked_count
            # Tranche B: bare filenames (SampleServiceImpl.java) resolve
            # nowhere — locate the file via SEARCH_CODE, then take history
            # per hit (max 3). Deterministic fallback, miss-marked.
            from investigation_agent_platform.domain.evidence.requests import (
                CodeSearchRequest,
            )

            requested = str(parameters.get("file_path", "") or "").strip()
            if not requested or "/" in requested.strip("/"):
                return banked_count
            try:
                qres = await gateway.search_code(
                    tenant_id,
                    investigation_id,
                    application_id or "example-app",
                    CodeSearchRequest(
                        repository=application_id or "example-app",
                        revision="HEAD",
                        query=requested,
                        limit=10,
                    ),
                )
            except Exception as exc:
                logger.warning(
                    "GET_CODE_HISTORY filename locate failed; keeping empty",
                    extra=_log_extra(
                        tenant_id=tenant_id,
                        investigation_id=inv_id_str,
                        event="history locate failed",
                        error=str(exc),
                    ),
                )
                return banked_count
            seen: set[str] = set()
            extra_banked = 0
            for hit in qres.items or []:
                try:
                    provenance = getattr(hit, "provenance", None)
                    location = getattr(provenance, "source_location", None)
                    found = str(getattr(location, "file_path", "") or "")
                except Exception:
                    continue
                if not found or found in seen:
                    continue
                seen.add(found)
                if len(seen) > 3:
                    break
                try:
                    hres = await gateway.get_code_history(
                        tenant_id,
                        investigation_id,
                        application_id or "example-app",
                        _build(found),
                    )
                except Exception:
                    continue
                banked = _bank(hres.items)
                if evidence_repo is not None and banked:
                    await evidence_repo.save_batch(tenant_id, banked, investigation_id)
                extra_banked += _record_outcome(banked)
            if extra_banked:
                outcome_detail["miss_reason"] = "history_filename_fallback"
                outcome_detail["status"] = "SUCCEEDED"
            return banked_count + extra_banked

        async def _handle_probe_api() -> int:
            from investigation_agent_platform.domain.common.exceptions import (
                ExecutionError,
                ProbeEndpointNotFoundError,
            )
            from investigation_agent_platform.ports.evidence.api_probe import (
                ApiProbeRequest,
            )

            try:
                req = ApiProbeRequest(
                    endpoint_id=str(parameters.get("endpoint_id", "")),
                    path_params={
                        str(k): str(v) for k, v in (parameters.get("path_params", {}) or {}).items()
                    },
                    query_params={
                        str(k): str(v)
                        for k, v in (parameters.get("query_params", {}) or {}).items()
                    },
                )
            except Exception as exc:
                logger.warning(
                    "PROBE_API invalid parameters degrades to empty",
                    extra=_log_extra(
                        tenant_id=tenant_id,
                        investigation_id=inv_id_str,
                        event="PROBE_API invalid parameters",
                        error=str(exc),
                    ),
                )
                outcome_detail["status"] = "DEGRADED"
                outcome_detail["miss_reason"] = "invalid_parameters"
                return 0
            if not req.endpoint_id.strip():
                outcome_detail["status"] = "DEGRADED"
                outcome_detail["miss_reason"] = "missing_endpoint_id"
                return 0
            try:
                qres = await gateway.probe_api(
                    tenant_id, investigation_id, application_id or "example-app", req
                )
            except (ProbeEndpointNotFoundError, ExecutionError) as exc:
                logger.warning(
                    "PROBE_API degrades to empty",
                    extra=_log_extra(
                        tenant_id=tenant_id,
                        investigation_id=inv_id_str,
                        event="PROBE_API endpoint miss",
                        error=str(exc),
                    ),
                )
                outcome_detail["status"] = "DEGRADED"
                outcome_detail["miss_reason"] = "probe_endpoint_not_found"
                return 0
            banked = _bank(qres.items)
            # D4: expected-vs-actual comparison. parameters.expected holds
            # the golden payload; divergences bank as DIVERGENCE rows
            # (absence is evidence). Best-effort JSON parse, never loud.
            try:
                expected = parameters.get("expected", None)
                if isinstance(expected, dict) and expected:
                    from investigation_agent_platform.application.evidence.divergence import (
                        diff_expected_actual,
                        shape_divergence_evidence,
                    )

                    for item in qres.items or []:
                        try:
                            import json as _json

                            body_text = str(getattr(item, "content_snippet", "") or "")
                            actual = _json.loads(body_text)
                        except Exception:
                            continue
                        for div in diff_expected_actual(expected, actual)[:10]:
                            banked.append(
                                shape_divergence_evidence(
                                    tenant_id=tenant_id,
                                    investigation_id=investigation_id,
                                    source_ref=str(getattr(item, "evidence_id", "") or ""),
                                    endpoint=str(parameters.get("endpoint_id", "") or ""),
                                    divergence=div,
                                )
                            )
                        break
            except Exception:
                pass
            if evidence_repo is not None and banked:
                await evidence_repo.save_batch(tenant_id, banked, investigation_id)
            return _record_outcome(banked)

        async def _handle_internal_reasoning_action() -> int:
            # VERIFY_HYPOTHESIS / CONCLUDE never touch an external evidence
            # provider — they operate purely on already persisted domain
            # state, so there is nothing to dispatch here.
            #
            # Hypothesis persistence happens for EVERY action type, not just
            # FORMULATE_HYPOTHESIS: the model attaches hypotheses_evaluated
            # to whatever action it proposes (often VERIFY_HYPOTHESIS), so
            # gating persistence on FORMULATE leaves the repo empty forever —
            # verification can never verify, and the ConclusionGate can never
            # approve COMPLETED. Statements dedupe against existing rows.
            if not proposed_hypotheses:
                return 0
            hypothesis_repo: Any = getattr(ctx, "hypothesis_repo", None)
            if hypothesis_repo is None:
                return 0
            from investigation_agent_platform.domain.hypothesis.models import (
                AssessmentStrength,
                AssessmentType,
                ClaimKind,
                ClaimTarget,
                Hypothesis,
                HypothesisEvidenceAssessment,
            )

            try:
                existing = await hypothesis_repo.find_by_investigation_id(
                    tenant_id, investigation_id
                )
            except Exception as exc:
                logger.exception(
                    "hypothesis dedupe read failed",
                    extra=_log_extra(
                        tenant_id=tenant_id,
                        investigation_id=inv_id_str,
                        event="hypothesis dedupe read failed",
                        error=str(exc),
                        error_type=type(exc).__name__,
                    ),
                )
                existing = []
            known = {str(getattr(h, "statement", "")).strip() for h in (existing or [])}
            saved = 0
            # Hypothesis budget: the profile's maximumHypotheses (via limits
            # overlay) caps the TOTAL, not just this batch — previously the
            # validator's cap was a literal `pass` and nothing enforced it.
            allowed = max(0, limits.max_hypotheses - len(existing or []))
            # P0-3: model-cited supporting evidence. FORMULATE_HYPOTHESIS may
            # carry supporting_evidence_ids — validated here against this
            # investigation's evidence rows (unknown IDs are dropped, never
            # trusted) and persisted on the hypothesis so verification can
            # bind claims to evidence instead of the whole corpus.
            cited: list[UUID] = []
            try:
                raw_ids = parameters.get("supporting_evidence_ids", []) or []
                if isinstance(raw_ids, list) and evidence_repo is not None:
                    rows = await evidence_repo.find_by_investigation_id(tenant_id, investigation_id)
                    known_ids = {getattr(e, "evidence_id", None) for e in (rows or [])}
                    for raw in raw_ids[:20]:
                        try:
                            cid = UUID(str(raw))
                        except (ValueError, TypeError, AttributeError):
                            continue
                        if cid in known_ids:
                            cited.append(cid)
            except Exception:
                cited = []
            known_evidence_rows: dict[UUID, Any] = {}
            try:
                if evidence_repo is not None:
                    _known_rows = await evidence_repo.find_by_investigation_id(
                        tenant_id, investigation_id
                    )
                    known_evidence_rows = {
                        getattr(row, "evidence_id", None): row
                        for row in (_known_rows or [])
                        if getattr(row, "evidence_id", None) is not None
                    }
            except Exception:
                known_evidence_rows = {}
            known_evidence_ids: set[Any] = set()
            if proposed_claims and evidence_repo is not None:
                known_evidence_ids = set(known_evidence_rows)
            for text in proposed_hypotheses[:20]:
                if saved >= allowed:
                    break
                statement = str(text or "").strip()[:2048]
                if not statement or statement in known:
                    continue
                known.add(statement)
                # Phase 2.1: typed claims carry their OWN evidence binding;
                # only statements without one inherit the batch-wide list.
                # Same statement with several kinds persists each kind's
                # binding as its own assessment.
                hypothesis_id = uuid4()
                claim_cited = list(cited)
                assessments: list[Any] = []
                typed_list = [
                    (claim_key, raw)
                    for claim_key, raw in proposed_claims.items()
                    if claim_key[0] == statement
                ]
                if typed_list:
                    # A typed claim carries ONLY its own binding — the
                    # batch-wide list is dropped, never merged.
                    claim_cited = []
                claim_binding_ids: dict[tuple[str, str, str, str, str], list[UUID]] = {}
                for claim_key, typed in typed_list:
                    typed_cited: list[Any] = []
                    for raw in (typed.get("evidence_ids", []) or [])[:20]:
                        try:
                            cid = UUID(str(raw))
                        except (ValueError, TypeError, AttributeError):
                            continue
                        if cid in known_evidence_ids:
                            if cid not in claim_cited:
                                claim_cited.append(cid)
                            if cid not in typed_cited:
                                typed_cited.append(cid)
                    claim_binding_ids[claim_key] = typed_cited
                    try:
                        kind = ClaimKind(str(typed.get("claim_kind") or "CAUSAL"))
                        raw_target = typed.get("target")
                        target = (
                            ClaimTarget.model_validate(raw_target)
                            if isinstance(raw_target, dict)
                            else None
                        )
                        if kind == ClaimKind.DEFINITION:
                            raw_resolution = next(
                                (
                                    (
                                        getattr(known_evidence_rows.get(cid), "attributes", {})
                                        or {}
                                    ).get("code_fact_resolution")
                                    for cid in typed_cited
                                    if isinstance(
                                        (
                                            getattr(known_evidence_rows.get(cid), "attributes", {})
                                            or {}
                                        ),
                                        dict,
                                    )
                                    and (
                                        getattr(known_evidence_rows.get(cid), "attributes", {})
                                        or {}
                                    ).get("code_fact_resolution")
                                    is not None
                                ),
                                None,
                            )
                            if raw_resolution is not None:
                                try:
                                    from investigation_agent_platform.domain.code.models import (
                                        CodeFactResolution,
                                        CodeFactStatus,
                                    )

                                    resolution = CodeFactResolution.model_validate(raw_resolution)
                                    if resolution.status == CodeFactStatus.RESOLVED:
                                        typed_value = (
                                            typed.get("claimed_value")
                                            or (raw_target or {}).get("claimed_value")
                                            if isinstance(raw_target, dict)
                                            else typed.get("claimed_value")
                                        )
                                        typed_semantics = (
                                            typed.get("value_semantics")
                                            or (raw_target or {}).get("value_semantics")
                                            if isinstance(raw_target, dict)
                                            else typed.get("value_semantics")
                                        )
                                        if (
                                            typed_value is not None
                                            and str(typed_value) == str(resolution.resolved_value)
                                            and str(typed_semantics or "").upper()
                                            == resolution.value_semantics.value
                                        ):
                                            claim_cited.extend(
                                                cid for cid in typed_cited if cid not in claim_cited
                                            )
                                except Exception:
                                    pass
                    except Exception:
                        # Malformed typing never blocks persistence; the
                        # claim stays untyped and verifies by inference.
                        continue
                try:
                    for claim_key, typed in typed_list:
                        target_data = typed.get("target")
                        target_data = target_data if isinstance(target_data, dict) else {}
                        edge_target = (
                            ClaimTarget.model_validate(target_data) if target_data else None
                        )
                        kind = ClaimKind(str(typed.get("claim_kind") or "CAUSAL"))
                        fact_category = typed.get("fact_category") or (
                            edge_target.fact_category.value
                            if edge_target is not None and edge_target.fact_category is not None
                            else None
                        )
                        for evidence_id in claim_binding_ids.get(claim_key, []):
                            assessment = HypothesisEvidenceAssessment(
                                hypothesis_id=hypothesis_id,
                                evidence_id=evidence_id,
                                assessment=AssessmentType.SUPPORTS,
                                strength=AssessmentStrength.MODERATE,
                                reason=f"cited by {kind.value} claim",
                                claim_kind=kind,
                                claim_target=edge_target,
                                commit_id=claim_key[3] or None,
                                fact_category=fact_category,
                                value_semantics=(
                                    str(
                                        typed.get("value_semantics")
                                        or target_data.get("value_semantics", "")
                                    )[:64]
                                    or None
                                ),
                                claimed_value=(
                                    str(
                                        typed.get("claimed_value")
                                        or target_data.get("claimed_value", "")
                                    )[:512]
                                    or None
                                ),
                            )
                            assessments.append(assessment)
                    await hypothesis_repo.save(
                        tenant_id,
                        Hypothesis(
                            id=hypothesis_id,
                            tenant_id=tenant_id,
                            investigation_id=investigation_id,
                            title=statement[:256],
                            description=statement[:4096],
                            statement=statement,
                            supporting_evidence_ids=claim_cited,
                            assessments=assessments,
                        ),
                        investigation_id,
                    )
                    saved += 1
                except Exception as exc:
                    logger.exception(
                        "hypothesis persistence failed",
                        extra=_log_extra(
                            tenant_id=tenant_id,
                            investigation_id=inv_id_str,
                            event="hypothesis persistence failed",
                            error=str(exc),
                            error_type=type(exc).__name__,
                        ),
                    )
            # P0-3: VERIFY_HYPOTHESIS may carry validated causal steps for one
            # hypothesis. Steps whose evidence does not resolve to this
            # investigation are dropped; the rest attach to the hypothesis so
            # verification can require a real causal chain.
            if resolved_action_type == ActionType.VERIFY_HYPOTHESIS and hypothesis_repo is not None:
                try:
                    from investigation_agent_platform.domain.hypothesis.models import (
                        HypothesisCausalStep,
                        HypothesisStatus,
                    )

                    target_raw = parameters.get("hypothesis_id")
                    raw_steps = parameters.get("causal_steps", []) or []
                    if target_raw and isinstance(raw_steps, list) and raw_steps:
                        target_id = UUID(str(target_raw))
                        rows = (
                            await evidence_repo.find_by_investigation_id(
                                tenant_id, investigation_id
                            )
                            if evidence_repo is not None
                            else []
                        )
                        known_ids = {getattr(e, "evidence_id", None) for e in (rows or [])}
                        steps: list[HypothesisCausalStep] = []
                        for raw in raw_steps[:10]:
                            if not isinstance(raw, dict):
                                continue
                            eids: list[UUID] = []
                            for r in (raw.get("evidence_ids", []) or [])[:20]:
                                try:
                                    cid = UUID(str(r))
                                except (ValueError, TypeError, AttributeError):
                                    continue
                                if cid in known_ids:
                                    eids.append(cid)
                            if not eids:
                                continue
                            steps.append(
                                HypothesisCausalStep(
                                    cause=str(raw.get("cause", ""))[:1024],
                                    mechanism=str(raw.get("mechanism", ""))[:2048],
                                    effect=str(raw.get("effect", ""))[:1024],
                                    evidence_ids=eids,
                                )
                            )
                        if steps:
                            target = await hypothesis_repo.get_by_id(tenant_id, target_id)
                            if target is not None and str(
                                getattr(target, "investigation_id", "")
                            ) == str(investigation_id):
                                updated = target.model_copy(update={"causal_steps": steps})
                                if updated.status == HypothesisStatus.PROPOSED:
                                    updated = updated.transition_status(
                                        HypothesisStatus.UNDER_INVESTIGATION
                                    )
                                await hypothesis_repo.save(tenant_id, updated, investigation_id)
                except Exception as exc:
                    logger.warning(
                        "causal step attachment failed; continuing",
                        extra=_log_extra(
                            tenant_id=tenant_id,
                            investigation_id=inv_id_str,
                            event="causal step attachment failed",
                            error=str(exc),
                            error_type=type(exc).__name__,
                        ),
                    )
            return saved

        _HANDLERS: dict[Any, Any] = {
            ActionType.QUERY_STATE: _handle_query_state,
            ActionType.SEARCH_LOGS: _handle_search_logs,
            ActionType.GET_CODE: _handle_get_code,
            ActionType.SEARCH_CODE: _handle_search_code,
            ActionType.FIND_SYMBOL: _handle_find_symbol,
            ActionType.WALK_STACK: _handle_walk_stack,
            ActionType.TRACE_LOGIC: _handle_trace_logic,
            ActionType.FIND_CALL_GRAPH: _handle_find_call_graph,
            ActionType.GET_CODE_HISTORY: _handle_get_code_history,
            ActionType.PROBE_API: _handle_probe_api,
            ActionType.CORRELATE: _handle_correlate,
            ActionType.TRACE_JOURNEY: _handle_trace_journey,
            ActionType.WALK_SESSION_GRAPH: _handle_walk_session_graph,
            ActionType.SEARCH_REF_DOCS: _handle_search_ref_docs,
            ActionType.FORMULATE_HYPOTHESIS: _handle_internal_reasoning_action,
            ActionType.VERIFY_HYPOTHESIS: _handle_internal_reasoning_action,
            ActionType.CONCLUDE: _handle_internal_reasoning_action,
        }

        handler = _HANDLERS.get(resolved_action_type)
        if handler is None:
            # Exhaustive registry: this should be unreachable given the
            # ActionType(action_type) resolution above, but fail closed if a
            # new enum member is ever added without a handler.
            raise ApplicationFailure(
                f"No execution handler registered for action type: {resolved_action_type.value}",
                type="UnhandledActionTypeError",
                non_retryable=True,
            )

        if resolved_action_type in _ACTION_REQUIRED_EVIDENCE_TYPE_LOCAL and gateway is None:
            raise ApplicationFailure(
                "Evidence gateway is not configured; cannot execute evidence-producing action",
                type="EVIDENCE_PROVIDER_UNAVAILABLE",
                non_retryable=True,
            )

        try:
            import time as _time

            from investigation_agent_platform.domain.investigation.models import (
                ActionOutcome,
                ActionOutcomeStatus,
            )

            _dispatch_start = _time.monotonic()
            items_count = await handler()
            _dispatch_ms = int((_time.monotonic() - _dispatch_start) * 1000)
            # P0-2: typed outcome from the handler side-channel. DEGRADED
            # survives even with zero items; otherwise zero items => EMPTY.
            _outcome_status = outcome_detail.get("status") or (
                "SUCCEEDED" if items_count > 0 else "EMPTY"
            )
            action_outcome = ActionOutcome(
                action_id=inv_action.action_id,
                action_type=resolved_action_type,
                status=ActionOutcomeStatus(_outcome_status),
                evidence_ids=list(outcome_detail.get("evidence_ids") or [])[:25],
                item_count=items_count,
                error_code=outcome_detail.get("error_code"),
                miss_reason=outcome_detail.get("miss_reason"),
                duration_ms=_dispatch_ms,
                fuzzy_orientation=(
                    outcome_detail.get("fuzzy_orientation")
                    if isinstance(outcome_detail.get("fuzzy_orientation"), dict)
                    else None
                ),
                sources=[
                    {k: str(v) for k, v in s.items()}
                    for s in (outcome_detail.get("sources") or [])
                    if isinstance(s, dict)
                ][:10],
            )
            # Phase 4.2: proof-chain counter (additive, fail-open). The
            # miss_reason breakdown is what lets dashboards separate
            # provider failure from no-match.
            _emit_telemetry(
                ctx,
                tenant_id,
                investigation_id,
                "record_code_action",
                str(getattr(resolved_action_type, "value", resolved_action_type)),
                _outcome_status,
                str(outcome_detail.get("miss_reason") or "none"),
            )
            # Operational telemetry: action type + result counts only (values
            # ride as extra keys per TO_DO-007/019; lazy %-style message).
            logger.warning(
                "execute_action_activity dispatched action=%s items=%s hypotheses=%s",
                resolved_action_type.value,
                items_count,
                len(proposed_hypotheses),
                extra=_log_extra(
                    tenant_id=tenant_id,
                    investigation_id=inv_id_str,
                    event="execute_action_activity dispatched",
                    action=resolved_action_type.value,
                    items_count=items_count,
                    hypotheses_count=len(proposed_hypotheses),
                ),
            )
        except ApplicationFailure:
            raise
        except Exception as exc:
            logger.exception(
                "execute_action_activity gateway dispatch failed",
                extra=_log_extra(
                    tenant_id=tenant_id,
                    investigation_id=inv_id_str,
                    event="execute_action_activity gateway dispatch failed",
                    error=str(exc),
                    error_type=type(exc).__name__,
                    action_type=action_type,
                ),
            )
            raise _application_failure_from_exc(exc) from exc

        # Audit action execution (always — the authorized action must always
        # be recorded, whether or not a gateway is wired).
        try:
            await action_service.execute(
                tenant_id, investigation_id, inv_action, principal_id=principal_id
            )
        except Exception as exc:
            logger.exception(
                "execute_action_activity record_action failed",
                extra=_log_extra(
                    tenant_id=tenant_id,
                    investigation_id=inv_id_str,
                    event="execute_action_activity record_action failed",
                    error=str(exc),
                    error_type=type(exc).__name__,
                ),
            )
            raise _application_failure_from_exc(exc) from exc

        if _idem_active_key is not None:
            await _complete_reservation(
                ctx=ctx,
                tenant_id=tenant_id,
                key=_idem_active_key,
                operation=_idem_operation,
                response={
                    "success": True,
                    "data": {
                        "status": result_status,
                        "items_count": items_count,
                        "code_fact_resolution": outcome_detail.get("code_fact_resolution"),
                        # Phase 3.2: the stored response must equal the live
                        # return below — a replayed action without the outcome
                        # would silently change the next-turn history.
                        "action_outcome": action_outcome.model_dump(mode="json"),
                    },
                },
                activity="execute_action_activity",
                investigation_id=inv_id_str,
            )
        logger.info(
            "execute_action_activity completed",
            extra=_log_extra(
                tenant_id=tenant_id,
                investigation_id=inv_id_str,
                event="execute_action_activity completed",
                outcome="executed",
                action=resolved_action_type.value,
                items_count=items_count,
            ),
        )
        return GenericActivityResult(
            success=True,
            data={
                "status": result_status,
                "items_count": items_count,
                "code_fact_resolution": outcome_detail.get("code_fact_resolution"),
                "action_outcome": action_outcome.model_dump(mode="json"),
            },
        )
    except ApplicationFailure:
        await _abandon_reservation(
            ctx=locals().get("ctx"),
            tenant_id=str(locals().get("tenant_id", "") or ""),
            key=locals().get("_idem_active_key"),
            operation=str(locals().get("_idem_operation", "") or ""),
            activity="execute_action_activity",
            investigation_id=str(locals().get("inv_id_str", "") or ""),
        )
        raise
    except Exception as exc:
        await _abandon_reservation(
            ctx=locals().get("ctx"),
            tenant_id=str(locals().get("tenant_id", "") or ""),
            key=locals().get("_idem_active_key"),
            operation=str(locals().get("_idem_operation", "") or ""),
            activity="execute_action_activity",
            investigation_id=str(locals().get("inv_id_str", "") or ""),
        )
        logger.exception(
            "execute_action_activity failed",
            extra=_log_extra(
                tenant_id=tenant_id_probe,
                investigation_id=inv_id_probe,
                event="execute_action_activity failed",
                error=str(exc),
                error_type=type(exc).__name__,
            ),
        )
        raise _application_failure_from_exc(exc) from exc


@activity.defn
@traced("worker", outcome_from=_activity_outcome, extra_from=_activity_extra)
async def verify_root_cause_activity(
    params: VerifyRootCauseInput,
) -> GenericActivityResult:
    # TO_DO-011: not idempotency-wired — pure compute over persisted state,
    # no durable writes of its own; re-execution is safe by construction.
    data = _coerced_data(VerifyRootCauseInput, params, activity="verify_root_cause_activity")
    tenant_id_probe = str(data.get("tenant_id", ""))
    inv_id_probe = str(data.get("investigation_id", ""))
    logger.info(
        "verify_root_cause_activity started",
        extra=_log_extra(
            tenant_id=tenant_id_probe,
            investigation_id=inv_id_probe,
            event="verify_root_cause_activity started",
        ),
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
        evidence_repo: Any = getattr(ctx, "evidence_repo", None)
        hypothesis_repo: Any = getattr(ctx, "hypothesis_repo", None)

        evidence_items: list[Any] = []
        hypotheses: list[Any] = []
        if evidence_repo is not None:
            try:
                evidence_items = await evidence_repo.find_by_investigation_id(
                    tenant_id, investigation_id
                )
            except Exception as exc:
                logger.exception(
                    "verify_root_cause_activity evidence fetch failed",
                    extra=_log_extra(
                        tenant_id=tenant_id,
                        investigation_id=inv_id_str,
                        event="verify_root_cause_activity evidence fetch failed",
                        error=str(exc),
                        error_type=type(exc).__name__,
                    ),
                )
                raise _application_failure_from_exc(exc) from exc
        if hypothesis_repo is not None:
            try:
                hypotheses = await hypothesis_repo.find_by_investigation_id(
                    tenant_id, investigation_id
                )
            except Exception as exc:
                logger.exception(
                    "verify_root_cause_activity hypothesis fetch failed",
                    extra=_log_extra(
                        tenant_id=tenant_id,
                        investigation_id=inv_id_str,
                        event="verify_root_cause_activity hypothesis fetch failed",
                        error=str(exc),
                        error_type=type(exc).__name__,
                    ),
                )
                raise _application_failure_from_exc(exc) from exc

        if not hypotheses:
            return GenericActivityResult(
                success=True, data={"verified": False, "reason": "no_hypotheses"}
            )

        if not hypotheses:
            return GenericActivityResult(
                success=True, data={"verified": False, "reason": "no_hypotheses"}
            )

        import os as _os

        from investigation_agent_platform.application.investigation.verification import (
            CausalRelationship,
            VerificationEngine,
            rank_hypotheses,
            select_verifiable_candidate,
        )

        verifier_mode = str(_os.environ.get("IAP_VERIFIER_V2", "new")).lower()
        contradiction_repo: Any = getattr(ctx, "contradiction_repo", None)
        contradictions: list[Any] = []
        if contradiction_repo is not None:
            try:
                contradictions = await contradiction_repo.find_by_investigation_id(
                    tenant_id, investigation_id
                )
            except Exception:
                contradictions = []
        evidence_by_id = {getattr(e, "evidence_id", None): e for e in evidence_items}

        # P0-3: ranked candidates, bound evidence, real contradictions and
        # causal chain. Legacy mode preserves the old first-hypothesis /
        # whole-corpus / empty-lists behavior for rollback.
        ranked = rank_hypotheses(list(hypotheses))
        # G9: prefer the candidate whose citations resolve — a reprocess-
        # carried copy with purged supporting rows must not shadow a live
        # twin. Falls back to head-eligible; abstention path unchanged.
        candidate = select_verifiable_candidate(
            ranked, evidence_by_id, ("REJECTED", "CONTRADICTED", "VERIFIED")
        )
        if candidate is None:
            return GenericActivityResult(
                success=True,
                data={"verified": False, "reason": "no_eligible_hypothesis"},
            )

        def _resolve(ids: list[Any]) -> list[Any]:
            out = []
            for raw in ids or []:
                try:
                    key = raw if not isinstance(raw, str) else UUID(raw)
                except (ValueError, TypeError, AttributeError):
                    continue
                item = evidence_by_id.get(key)
                if item is not None:
                    out.append(item)
            return out

        engine = VerificationEngine()
        policy = _select_verification_policy(data.get("triage_route", ""))
        if verifier_mode == "legacy":
            hypothesis = hypotheses[0]
            result = engine.evaluate_hypothesis(
                hypothesis,
                evidence_items,
                [],
                [],
                policy,
                sibling_hypotheses=list(hypotheses[1:]),
            )
            verified = result.status.value == "VERIFIED"
            # Phase 4.2: legacy-mode decisions are observed too.
            _emit_verify_telemetry(
                ctx,
                tenant_id,
                investigation_id,
                hypothesis,
                result,
                list(hypotheses[1:]),
            )
        else:
            bound = _resolve(getattr(candidate, "supporting_evidence_ids", []) or [])
            refuting = _resolve(getattr(candidate, "refuting_evidence_ids", []) or [])
            # Tranche B: range-read rows for files the claim names join the
            # bound set even when the model cited only the search-match
            # sibling (statement-grounded inclusion — same file, proven
            # provenance; concession/contradiction gates still apply).
            try:
                _bound_ids = {getattr(e, "evidence_id", None) for e in bound}
                for _extra in VerificationEngine.statement_range_reads(
                    str(getattr(candidate, "statement", "") or ""),
                    list(evidence_by_id.values()),
                ):
                    if getattr(_extra, "evidence_id", None) not in _bound_ids:
                        _bound_ids.add(getattr(_extra, "evidence_id", None))
                        bound.append(_extra)
            except Exception:
                pass
            code_value_result = engine.validate_code_value_claim(candidate, bound, contradictions)
            code_value_claim = VerificationEngine.claims_field_value(
                str(getattr(candidate, "statement", "") or "")
            ) or str(VerificationEngine._fact_category_of(candidate) or "") == "SOURCE_VALUE_FACT"
            code_value_answered = code_value_result is not None and not refuting
            history_result = engine.validate_history_claim(candidate, bound, contradictions)
            history_answered = history_result is not None and not refuting
            # Init up front: every fast path below assigns only on its taken
            # branch, and the general-build guard is skipped when
            # code_value_claim is true — without this, a field-value claim
            # with no deterministic resolution leaves causal_chain unbound
            # (UnboundLocalError at the filter below; live L5 WORKFLOW_ERROR).
            causal_chain: list = []
            if code_value_answered:
                result = code_value_result
                causal_chain = []
            elif history_answered:
                result = history_result
                causal_chain = []
            # G1: location-answer fast path — validated code citations answer
            # location questions without causal machinery. Skipped under
            # legacy verifier mode (pre-P0-3 semantics preserved).
            location_result = engine.validate_location_claim(candidate, bound, contradictions)
            location_answered = (
                location_result is not None
                and not refuting
                and not code_value_claim
                and not history_answered
            )
            if location_answered:
                result = location_result
                causal_chain = []
            # T1-1: doc-answer fast path (mirrors location). Skipped under
            # legacy verifier mode like the location path.
            doc_answered = False
            doc_result = None
            if (
                not location_answered
                and not code_value_answered
                and not history_answered
                and not code_value_claim
            ):
                try:
                    doc_result = engine.validate_doc_claim(candidate, bound, contradictions)
                    doc_answered = doc_result is not None and not refuting
                    if doc_answered:
                        result = doc_result
                        causal_chain = []
                    elif doc_result is not None and refuting:
                        logger.info(
                            "doc answer withheld: refuting evidence bound",
                            extra=_log_extra(
                                tenant_id=tenant_id,
                                investigation_id=inv_id_str,
                                event="doc answer withheld",
                                hypothesis_id=str(candidate.id),
                            ),
                        )
                except Exception:
                    pass
            else:
                if location_result is not None and refuting:
                    logger.info(
                        "location answer withheld: refuting evidence bound",
                        extra=_log_extra(
                            tenant_id=tenant_id,
                            investigation_id=inv_id_str,
                            event="location answer withheld",
                            hypothesis_id=str(candidate.id),
                        ),
                    )
            # #65: state-answer fast path (mirrors doc). Banked DATABASE_STATE
            # rows answer value questions without causal machinery.
            state_answered = False
            state_result = None
            if (
                not location_answered
                and not code_value_answered
                and not history_answered
                and not doc_answered
                and not code_value_claim
            ):
                try:
                    state_result = engine.validate_state_claim(candidate, bound, contradictions)
                    state_answered = state_result is not None and not refuting
                    logger.info(
                        "state answer decision",
                        extra=_log_extra(
                            tenant_id=tenant_id,
                            investigation_id=inv_id_str,
                            event="state answer decision",
                            hypothesis_id=str(candidate.id),
                            bound_count=len(bound),
                            bound_types=sorted(
                                {
                                    str(
                                        getattr(
                                            getattr(e, "evidence_type", ""),
                                            "value",
                                            getattr(e, "evidence_type", ""),
                                        )
                                    )
                                    for e in bound
                                }
                            ),
                            validated=state_result is not None,
                        ),
                    )
                    if state_answered:
                        result = state_result
                        causal_chain = []
                    elif state_result is not None and refuting:
                        logger.info(
                            "state answer withheld: refuting evidence bound",
                            extra=_log_extra(
                                tenant_id=tenant_id,
                                investigation_id=inv_id_str,
                                event="state answer withheld",
                                hypothesis_id=str(candidate.id),
                            ),
                        )
                except Exception:
                    pass
            if (
                not location_answered
                and not code_value_answered
                and not history_answered
                and not doc_answered
                and not state_answered
                and not code_value_claim
            ):
                causal_chain = [
                    CausalRelationship(
                        cause=s.cause,
                        effect=s.effect,
                        mechanism=s.mechanism,
                        confidence=0.7,
                        evidence_ids=[e for e in (s.evidence_ids or []) if e in evidence_by_id],
                    )
                    for s in (getattr(candidate, "causal_steps", []) or [])
                ]
            causal_chain = [c for c in causal_chain if c.evidence_ids]
            blockers: list[str] = []
            if not bound:
                blockers.append("no bound supporting evidence")
            if refuting:
                blockers.append(f"{len(refuting)} refuting evidence item(s) bound")
            if code_value_claim and not code_value_answered:
                blockers.append("code value claim lacks a matching deterministic resolution")
            if code_value_answered or history_answered:
                blockers.clear()
            if (
                not causal_chain
                and not location_answered
                and not code_value_answered
                and not history_answered
                and not doc_answered
                and not state_answered
                and not code_value_claim
            ):
                blockers.append("no validated causal chain")
            if blockers:
                from investigation_agent_platform.application.investigation.verification import (
                    InvestigationConfidence,
                    VerificationResult,
                    VerificationStatus,
                )

                result = VerificationResult(
                    hypothesis_id=candidate.id,
                    status=VerificationStatus.INCONCLUSIVE,
                    corroborating_evidence_ids=[e.evidence_id for e in bound],
                    explanation="; ".join(blockers),
                    confidence=InvestigationConfidence(
                        coverage_score=0.0,
                        reliability_score=0.0,
                        causal_score=0.0,
                        contradiction_penalty=0.0,
                    ),
                )
            elif code_value_answered:
                result = code_value_result
                causal_chain = []
            elif history_answered:
                result = history_result
                causal_chain = []
            elif (
                not code_value_claim
                and not location_answered
                and not history_answered
                and not doc_answered
                and not state_answered
            ):
                _candidate_id = getattr(candidate, "id", None)
                result = engine.evaluate_hypothesis(
                    candidate,
                    bound,
                    contradictions,
                    causal_chain,
                    policy,
                    sibling_hypotheses=[
                        h for h in (hypotheses or []) if getattr(h, "id", None) != _candidate_id
                    ],
                )
            # Phase 4.2: verification decision + location-exemption counters.
            _emit_verify_telemetry(
                ctx,
                tenant_id,
                investigation_id,
                candidate,
                result,
                [
                    h
                    for h in (hypotheses or [])
                    if getattr(h, "id", None) != getattr(candidate, "id", None)
                ],
            )
            # Shadow telemetry: always compute the legacy answer alongside
            # and log divergence while the new path is authoritative.
            try:
                legacy = engine.evaluate_hypothesis(
                    hypotheses[0],
                    evidence_items,
                    [],
                    [],
                    policy,
                    sibling_hypotheses=list(hypotheses[1:]),
                )
                if legacy.status.value != result.status.value:
                    logger.warning(
                        "verifier divergence legacy=%s new=%s",
                        legacy.status.value,
                        result.status.value,
                        extra=_log_extra(
                            tenant_id=tenant_id,
                            investigation_id=inv_id_str,
                            event="verifier divergence",
                            hypothesis_id=str(candidate.id),
                        ),
                    )
            except Exception:
                pass
            verified = result.status.value == "VERIFIED"
            # Persist the verdict back onto the candidate (scores, bindings,
            # lifecycle) so the next ranking sorts on maintained fields.
            try:
                from investigation_agent_platform.application.investigation.verification import (
                    apply_verification_outcome,
                )

                updated = apply_verification_outcome(candidate, result)
                if updated != candidate and hypothesis_repo is not None:
                    await hypothesis_repo.save(tenant_id, updated, investigation_id)
            except Exception:
                pass
        logger.info(
            "verify_root_cause_activity completed",
            extra=_log_extra(
                tenant_id=tenant_id,
                investigation_id=inv_id_str,
                event="verify_root_cause_activity completed",
                outcome="verified" if verified else "unverified",
                verification_status=result.status.value,
            ),
        )
        return GenericActivityResult(
            success=True,
            data={
                "verified": verified,
                "status": result.status.value,
                "confidence": result.confidence.overall_confidence,
                "hypothesis_id": str(result.hypothesis_id),
                "answer_mode": bool(getattr(result, "location_validated", False)),
                "code_value_validated": bool(getattr(result, "code_value_validated", False)),
            },
        )
    except ApplicationFailure:
        raise
    except Exception as exc:
        logger.exception(
            "verify_root_cause_activity failed",
            extra=_log_extra(
                tenant_id=tenant_id_probe,
                investigation_id=inv_id_probe,
                event="verify_root_cause_activity failed",
                error=str(exc),
                error_type=type(exc).__name__,
            ),
        )
        raise _application_failure_from_exc(exc) from exc


@activity.defn
@traced("worker", outcome_from=_activity_outcome, extra_from=_activity_extra)
async def checkpoint_activity(params: CheckpointInput) -> GenericActivityResult:
    data = _coerced_data(CheckpointInput, params, activity="checkpoint_activity")
    tenant_id_probe = str(data.get("tenant_id", ""))
    inv_id_probe = str(data.get("investigation_id", ""))
    logger.info(
        "checkpoint_activity started",
        extra=_log_extra(
            tenant_id=tenant_id_probe,
            investigation_id=inv_id_probe,
            event="checkpoint_activity started",
            step=data.get("step", 0),
        ),
    )
    try:
        tenant_id = str(data.get("tenant_id", ""))
        inv_id_str = str(data.get("investigation_id", ""))
        step = int(data.get("step", 0))
        state_snapshot: dict[str, Any] | None = data.get("state_snapshot")
        usage_totals = data.get("usage_totals") or {}
        if not tenant_id or not inv_id_str:
            return GenericActivityResult(
                success=False, error="tenant_id and investigation_id required"
            )
        investigation_id = UUID(inv_id_str)
        ctx = _get_ctx()

        # TO_DO-011: idempotency-wired — (investigation_id, step) is the
        # deterministic key, so a retried checkpoint step cannot duplicate
        # the snapshot row.
        _idem_active_key: str | None = None
        _idem_operation = "activity:checkpoint"
        _idem_key = f"checkpoint:{inv_id_str}:{step}"
        _replayed, _idem_active_key = await _reserve_or_replay(
            ctx=ctx,
            tenant_id=tenant_id,
            key=_idem_key,
            request_payload={
                "tenant_id": tenant_id,
                "investigation_id": inv_id_str,
                "step": step,
                "snapshot_present": state_snapshot is not None,
            },
            operation=_idem_operation,
            activity="checkpoint_activity",
            investigation_id=inv_id_str,
        )
        if _replayed is not None:
            return GenericActivityResult(
                success=bool(_replayed.get("success", True)),
                data=dict(_replayed.get("data") or {}),
                error=_replayed.get("error"),
            )

        checkpoint_repo: Any = getattr(ctx, "checkpoint_repo", None)
        # Also try _InMemoryCheckpointRepository attached via resume service pattern.
        if checkpoint_repo is None:
            # No repo wired — treat as no-op but succeed so workflow progresses.
            logger.error(
                "checkpoint_activity repo unwired; checkpoint not persisted",
                extra=_log_extra(
                    tenant_id=tenant_id,
                    investigation_id=inv_id_str,
                    event="checkpoint_activity repo unwired",
                    step=step,
                ),
            )
            if _idem_active_key is not None:
                await _complete_reservation(
                    ctx=ctx,
                    tenant_id=tenant_id,
                    key=_idem_active_key,
                    operation=_idem_operation,
                    response={"success": True, "data": {"step": step, "noop": True}},
                    activity="checkpoint_activity",
                    investigation_id=inv_id_str,
                )
            return GenericActivityResult(success=True, data={"step": step})

        # Build snapshot from investigation if not supplied.
        if state_snapshot is None:
            inv_repo: Any = getattr(ctx, "investigation_repo", None)
            if inv_repo is not None:
                try:
                    inv = await inv_repo.get_by_id(tenant_id, investigation_id)
                    if inv is not None:
                        from investigation_agent_platform.domain.investigation.models import (
                            InvestigationState,
                        )

                        st = InvestigationState(investigation=inv)
                        # Carry token counters forward: fresh states reset
                        # them, which would unbound cumulative LLM spend.
                        # Best-effort — a missing/stale checkpoint degrades to
                        # zero, never breaks checkpointing itself.
                        carried_tokens = 0
                        carried_cost = 0.0
                        try:
                            prior = await checkpoint_repo.get_latest_checkpoint(
                                tenant_id, investigation_id
                            )
                            if isinstance(prior, dict):
                                carried_tokens = max(0, int(prior.get("llm_tokens_used", 0) or 0))
                                carried_cost = max(
                                    0.0,
                                    float(prior.get("llm_cost_usd", 0.0) or 0.0),
                                )
                        except Exception:
                            pass
                        if isinstance(usage_totals, dict) and usage_totals:
                            try:
                                carried_tokens = max(0, int(usage_totals.get("llm_tokens", 0) or 0))
                                carried_cost = max(
                                    0.0, float(usage_totals.get("llm_cost", 0.0) or 0.0)
                                )
                            except (TypeError, ValueError):
                                pass
                        st = st.model_copy(
                            update={
                                "llm_tokens_used": carried_tokens,
                                "llm_cost_usd": carried_cost,
                            }
                        )
                        state_snapshot = st.model_dump(mode="json")
                    else:
                        state_snapshot = {"step": step}
                except Exception as exc:
                    logger.exception(
                        "checkpoint_activity snapshot build failed",
                        extra=_log_extra(
                            tenant_id=tenant_id,
                            investigation_id=inv_id_str,
                            event="checkpoint_activity snapshot build failed",
                            error=str(exc),
                            error_type=type(exc).__name__,
                            step=step,
                        ),
                    )
                    state_snapshot = {"step": step}
            else:
                state_snapshot = {"step": step}

        await checkpoint_repo.save_checkpoint(
            tenant_id, investigation_id, step, state_snapshot or {"step": step}
        )
        if _idem_active_key is not None:
            await _complete_reservation(
                ctx=ctx,
                tenant_id=tenant_id,
                key=_idem_active_key,
                operation=_idem_operation,
                response={"success": True, "data": {"step": step}},
                activity="checkpoint_activity",
                investigation_id=inv_id_str,
            )
        logger.info(
            "checkpoint_activity completed",
            extra=_log_extra(
                tenant_id=tenant_id,
                investigation_id=inv_id_str,
                event="checkpoint_activity completed",
                outcome="checkpointed",
                step=step,
            ),
        )
        return GenericActivityResult(success=True, data={"step": step})
    except ApplicationFailure:
        await _abandon_reservation(
            ctx=locals().get("ctx"),
            tenant_id=str(locals().get("tenant_id", "") or ""),
            key=locals().get("_idem_active_key"),
            operation=str(locals().get("_idem_operation", "") or ""),
            activity="checkpoint_activity",
            investigation_id=str(locals().get("inv_id_str", "") or ""),
        )
        raise
    except Exception as exc:
        await _abandon_reservation(
            ctx=locals().get("ctx"),
            tenant_id=str(locals().get("tenant_id", "") or ""),
            key=locals().get("_idem_active_key"),
            operation=str(locals().get("_idem_operation", "") or ""),
            activity="checkpoint_activity",
            investigation_id=str(locals().get("inv_id_str", "") or ""),
        )
        logger.exception(
            "checkpoint_activity failed",
            extra=_log_extra(
                tenant_id=tenant_id_probe,
                investigation_id=inv_id_probe,
                event="checkpoint_activity failed",
                error=str(exc),
                error_type=type(exc).__name__,
            ),
        )
        raise _application_failure_from_exc(exc) from exc


def _find_transition_path(
    start: Any, target: Any, transitions: dict[Any, set[Any]]
) -> list[Any] | None:
    """BFS shortest path for graceful shutdown; returns list of statuses including start->target.

    ``Any`` is justified: nodes are domain InvestigationStatus enums (or their
    string values) resolved lazily; the helper stays decoupled from the domain
    import graph.
    """
    queue: deque[list[Any]] = deque([[start]])
    visited: set[Any] = {start}
    while queue:
        path = queue.popleft()
        cur = path[-1]
        if cur == target:
            return path
        for nxt in transitions.get(cur, set()):
            if nxt not in visited:
                visited.add(nxt)
                queue.append(path + [nxt])
    return None


async def _transition_via_path(
    tenant_id: str,
    inv: Any,
    target: Any,
    reason: str,
    inv_repo: Any,
    transition_repo: Any | None = None,
    timeline_repo: Any | None = None,
    *,
    actor: Any = None,
) -> tuple[Any, bool]:
    """Walk the domain graph step-by-step, persisting each transition.

    Every edge is recorded to transition_repo (when available) in addition
    to saving the aggregate — previously the audit trail only captured
    API-driven transitions (e.g. cancel) while the entire workflow-driven
    lifecycle was invisible in investigation_transitions.

    Every edge also appends a STATE_CHANGE timeline event (when available):
    previously nothing on the live path ever wrote timeline_events, so the
    timeline tab could only ever render seeded rows.

    Returns ``(updated_investigation, audit_degraded)``: ``audit_degraded``
    is True when any audit write was skipped or failed. Skips and failures
    are logged loudly at error level (TO_DO-012 — no silent skip) but never
    break the lifecycle itself.

    The path-finding adjacency is derived from the domain single source via
    ``_domain_transition_graph`` (TO_DO-013) — there is no duplicated edge
    table here to drift. The domain ``actor`` is threaded into both
    ``transition_to`` and ``record_transition`` (TO_DO-012).
    """
    graph = _domain_transition_graph()
    resolved_actor = _normalize_actor(actor)
    actor_value = getattr(resolved_actor, "value", str(resolved_actor))
    audit_degraded = False
    inv_id_str = str(getattr(inv, "id", ""))

    async def _record_edge(from_status: Any, to_status: Any) -> None:
        nonlocal audit_degraded
        if transition_repo is None:
            audit_degraded = True
            logger.error(
                "transition audit repo unwired; edge %s -> %s not recorded",
                getattr(from_status, "value", str(from_status)),
                getattr(to_status, "value", str(to_status)),
                extra=_log_extra(
                    tenant_id=tenant_id,
                    investigation_id=inv_id_str,
                    event="transition audit repo unwired",
                    from_status=getattr(from_status, "value", str(from_status)),
                    to_status=getattr(to_status, "value", str(to_status)),
                ),
            )
            return
        try:
            try:
                await transition_repo.record_transition(
                    tenant_id=tenant_id,
                    investigation_id=inv.id,
                    from_state=getattr(from_status, "value", str(from_status)),
                    to_state=getattr(to_status, "value", str(to_status)),
                    reason=reason,
                    actor=actor_value,
                )
            except TypeError:
                # Wave 3 owns the repository signature: the SQL implementation
                # still hardcodes actor="SYSTEM" while the port carries the new
                # actor kwarg — fall back loudly rather than losing the row.
                await transition_repo.record_transition(
                    tenant_id=tenant_id,
                    investigation_id=inv.id,
                    from_state=getattr(from_status, "value", str(from_status)),
                    to_state=getattr(to_status, "value", str(to_status)),
                    reason=reason,
                )
                audit_degraded = True
                logger.error(
                    "transition audit actor downgraded; repo signature predates actor",
                    extra=_log_extra(
                        tenant_id=tenant_id,
                        investigation_id=inv_id_str,
                        event="transition audit actor downgraded",
                        actor=actor_value,
                    ),
                )
        except Exception as exc:
            # Audit recording must never break the lifecycle itself.
            audit_degraded = True
            logger.exception(
                "transition audit record failed",
                extra=_log_extra(
                    tenant_id=tenant_id,
                    investigation_id=inv_id_str,
                    event="transition audit record failed",
                    error=str(exc),
                    error_type=type(exc).__name__,
                ),
            )

    async def _append_timeline(from_status: Any, to_status: Any) -> None:
        nonlocal audit_degraded
        if timeline_repo is None:
            audit_degraded = True
            logger.error(
                "timeline repo unwired; STATE_CHANGE event not appended",
                extra=_log_extra(
                    tenant_id=tenant_id,
                    investigation_id=inv_id_str,
                    event="timeline repo unwired",
                    from_status=getattr(from_status, "value", str(from_status)),
                    to_status=getattr(to_status, "value", str(to_status)),
                ),
            )
            return
        try:
            from investigation_agent_platform.domain.timeline.models import (
                TimelineEvent,
                TimelineEventType,
            )

            existing = await timeline_repo.find_by_investigation_id(tenant_id, inv.id)
            seq = len(existing or [])
            from_value = getattr(from_status, "value", str(from_status))
            to_value = getattr(to_status, "value", str(to_status))
            await timeline_repo.append(
                tenant_id,
                TimelineEvent(
                    tenant_id=tenant_id,
                    investigation_id=inv.id,
                    sequence_number=seq,
                    timestamp=datetime.now(UTC),
                    event_type=TimelineEventType.STATE_CHANGE,
                    description=f"Status {from_value} -> {to_value}: {reason}",
                ),
                inv.id,
            )
        except Exception as exc:
            # Timeline is observability: never break the lifecycle.
            audit_degraded = True
            logger.exception(
                "timeline append failed",
                extra=_log_extra(
                    tenant_id=tenant_id,
                    investigation_id=inv_id_str,
                    event="timeline append failed",
                    error=str(exc),
                    error_type=type(exc).__name__,
                ),
            )

    path = _find_transition_path(inv.status, target, graph)

    if path is None:
        # No path - try direct transition to surface proper exception
        updated, _ = inv.transition_to(target, resolved_actor, reason)
        await inv_repo.save(tenant_id, updated, expected_version=inv.version)
        await _record_edge(inv.status, target)
        await _append_timeline(inv.status, target)
        return updated, audit_degraded
    current = inv
    # Walk edges excluding start node
    for nxt in path[1:]:
        updated, _ = current.transition_to(nxt, resolved_actor, reason)
        await inv_repo.save(tenant_id, updated, expected_version=current.version)
        await _record_edge(current.status, nxt)
        await _append_timeline(current.status, nxt)
        current = updated
    return current, audit_degraded


@activity.defn
@traced("worker", outcome_from=_activity_outcome, extra_from=_activity_extra)
async def transition_investigation_activity(
    params: TransitionInvestigationInput,
) -> GenericActivityResult:
    data = _coerced_data(
        TransitionInvestigationInput, params, activity="transition_investigation_activity"
    )
    tenant_id_probe = str(data.get("tenant_id", ""))
    inv_id_probe = str(data.get("investigation_id", ""))
    logger.info(
        "transition_investigation_activity started",
        extra=_log_extra(
            tenant_id=tenant_id_probe,
            investigation_id=inv_id_probe,
            event="transition_investigation_activity started",
            target_status=str(data.get("target_status", "")),
        ),
    )
    try:
        tenant_id = str(data.get("tenant_id", ""))
        inv_id_str = str(data.get("investigation_id", ""))
        target_status = str(data.get("target_status", ""))
        reason = str(data.get("reason", "")) or f"Transition to {target_status}"
        actor = str(data.get("actor", "SYSTEM")) or "SYSTEM"
        failure_code = str(data.get("failure_code", "") or "")
        failure_reason = str(data.get("failure_reason", "") or "")
        if not tenant_id or not inv_id_str or not target_status:
            return GenericActivityResult(
                success=False, error="tenant_id, investigation_id and target_status required"
            )
        investigation_id = UUID(inv_id_str)
        ctx = _get_ctx()
        inv_repo: Any = getattr(ctx, "investigation_repo", None)
        if inv_repo is None:
            logger.error(
                "transition_investigation_activity repo unwired; transition not applied",
                extra=_log_extra(
                    tenant_id=tenant_id,
                    investigation_id=inv_id_str,
                    event="transition_investigation_activity repo unwired",
                    target_status=target_status,
                ),
            )
            return GenericActivityResult(success=True, data={"status": target_status, "noop": True})
        inv = await inv_repo.get_by_id(tenant_id, investigation_id)
        if inv is None:
            return GenericActivityResult(success=False, error="investigation not found")
        from investigation_agent_platform.domain.investigation.models import InvestigationStatus

        try:
            target = InvestigationStatus(target_status)
        except ValueError:
            return GenericActivityResult(success=False, error=f"unknown status {target_status}")
        if inv.status == target:
            return GenericActivityResult(
                success=True, data={"status": target.value, "already": True}
            )

        # TO_DO-011: idempotency-wired — (investigation_id, target, version)
        # is deterministic: a retry after a lost response either replays the
        # stored outcome or no-ops on "already" when the version advanced.
        _idem_active_key: str | None = None
        _idem_operation = "activity:transition"
        _idem_key = f"transition:{inv_id_str}:{target.value}:{inv.version}"
        _replayed, _idem_active_key = await _reserve_or_replay(
            ctx=ctx,
            tenant_id=tenant_id,
            key=_idem_key,
            request_payload={
                "tenant_id": tenant_id,
                "investigation_id": inv_id_str,
                "target": target.value,
                "version": inv.version,
                "reason": reason,
                "actor": actor,
            },
            operation=_idem_operation,
            activity="transition_investigation_activity",
            investigation_id=inv_id_str,
        )
        if _replayed is not None:
            return GenericActivityResult(
                success=bool(_replayed.get("success", True)),
                data=dict(_replayed.get("data") or {"status": target.value}),
                error=_replayed.get("error"),
            )

        transition_inv = inv
        transition_reason = reason
        if target == InvestigationStatus.FAILED and failure_code:
            transition_inv = _stamp_failure(inv, failure_code, failure_reason or reason)
            transition_reason = f"{reason} ({failure_code})"
        _, audit_degraded = await _transition_via_path(
            tenant_id,
            transition_inv,
            target,
            transition_reason,
            inv_repo,
            getattr(ctx, "transition_repo", None),
            getattr(ctx, "timeline_repo", None),
            actor=actor,
        )
        if _idem_active_key is not None:
            await _complete_reservation(
                ctx=ctx,
                tenant_id=tenant_id,
                key=_idem_active_key,
                operation=_idem_operation,
                response={
                    "success": True,
                    "data": {"status": target.value, "audit_degraded": audit_degraded},
                },
                activity="transition_investigation_activity",
                investigation_id=inv_id_str,
            )
        logger.info(
            "transition_investigation_activity completed",
            extra=_log_extra(
                tenant_id=tenant_id,
                investigation_id=inv_id_str,
                event="transition_investigation_activity completed",
                outcome="transitioned",
                target_status=target.value,
                actor=actor,
                audit_degraded=audit_degraded,
            ),
        )
        return GenericActivityResult(
            success=True, data={"status": target.value, "audit_degraded": audit_degraded}
        )
    except ApplicationFailure:
        await _abandon_reservation(
            ctx=locals().get("ctx"),
            tenant_id=str(locals().get("tenant_id", "") or ""),
            key=locals().get("_idem_active_key"),
            operation=str(locals().get("_idem_operation", "") or ""),
            activity="transition_investigation_activity",
            investigation_id=str(locals().get("inv_id_str", "") or ""),
        )
        raise
    except Exception as exc:
        await _abandon_reservation(
            ctx=locals().get("ctx"),
            tenant_id=str(locals().get("tenant_id", "") or ""),
            key=locals().get("_idem_active_key"),
            operation=str(locals().get("_idem_operation", "") or ""),
            activity="transition_investigation_activity",
            investigation_id=str(locals().get("inv_id_str", "") or ""),
        )
        logger.exception(
            "transition_investigation_activity failed",
            extra=_log_extra(
                tenant_id=tenant_id_probe,
                investigation_id=inv_id_probe,
                event="transition_investigation_activity failed",
                error=str(exc),
                error_type=type(exc).__name__,
            ),
        )
        raise _application_failure_from_exc(exc) from exc


async def _gate_conclusion_or_fail(tenant_id: str, investigation_id: UUID, ctx: Any) -> Any:
    """Evaluate ConclusionGate for a requested COMPLETED conclusion (F-051).

    Returns ``InvestigationStatus.COMPLETED`` only when the gate approves;
    otherwise returns ``InvestigationStatus.FAILED`` so the denial and its
    blockers are persisted in the lifecycle rather than swallowed. On
    approval, also persists the Finding + InvestigationConclusion that
    justify COMPLETED (Part 11.2) — the conclusion read path and clustering
    (Part 11.7+) depend on this corpus actually existing.
    """
    from investigation_agent_platform.application.investigation.verification import (
        ConclusionGate,
        RootCauseVerificationPolicy,
        VerificationEngine,
        VerificationStatus,
    )
    from investigation_agent_platform.domain.finding.models import (
        ConclusionStatus,
        Finding,
        FindingType,
        InvestigationConclusion,
    )
    from investigation_agent_platform.domain.investigation.models import InvestigationStatus

    evidence_repo: Any = getattr(ctx, "evidence_repo", None)
    hypothesis_repo: Any = getattr(ctx, "hypothesis_repo", None)
    finding_repo: Any = getattr(ctx, "finding_repo", None)
    evidence_items: list[Any] = []
    hypotheses: list[Any] = []
    if evidence_repo is not None:
        evidence_items = await evidence_repo.find_by_investigation_id(tenant_id, investigation_id)
    if hypothesis_repo is not None:
        hypotheses = await hypothesis_repo.find_by_investigation_id(tenant_id, investigation_id)
    if not hypotheses:
        logger.warning(
            "ConclusionGate: no hypotheses; denying COMPLETED",
            extra=_log_extra(
                tenant_id=tenant_id,
                investigation_id=investigation_id,
                event="ConclusionGate denied COMPLETED",
                reason="no_hypotheses",
            ),
        )
        return InvestigationStatus.FAILED

    from investigation_agent_platform.application.investigation.verification import (
        CausalRelationship,
        rank_hypotheses,
        select_verifiable_candidate,
    )

    contradiction_repo: Any = getattr(ctx, "contradiction_repo", None)
    contradictions: list[Any] = []
    if contradiction_repo is not None:
        try:
            contradictions = await contradiction_repo.find_by_investigation_id(
                tenant_id, investigation_id
            )
        except Exception:
            contradictions = []
    evidence_by_id = {getattr(e, "evidence_id", None): e for e in evidence_items}

    def _resolve(ids: list[Any]) -> list[Any]:
        out = []
        for raw in ids or []:
            try:
                key = raw if not isinstance(raw, str) else UUID(raw)
            except (ValueError, TypeError, AttributeError):
                continue
            item = evidence_by_id.get(key)
            if item is not None:
                out.append(item)
        return out

    async def _persist_abstention(
        kind: str, limitations: list[str], hypothesis_id: Any = None
    ) -> None:
        """Honest abstention is a successful terminal outcome (P0-3): persist
        an INSUFFICIENT_EVIDENCE / NO_ROOT_CAUSE_FOUND conclusion with its
        limitations instead of a generic FAILED."""
        if finding_repo is None:
            return
        status = (
            ConclusionStatus.NO_ROOT_CAUSE_FOUND
            if kind == "refuted"
            else ConclusionStatus.INSUFFICIENT_EVIDENCE
        )
        await finding_repo.save_conclusion(
            tenant_id,
            InvestigationConclusion(
                tenant_id=tenant_id,
                investigation_id=investigation_id,
                status=status,
                root_cause=None,
                supporting_evidence_ids=[],
                supporting_hypothesis_ids=([hypothesis_id] if hypothesis_id is not None else []),
                contradicting_evidence_ids=[],
                confidence=0.0,
                limitations=[str(l)[:500] for l in limitations][:20],
                # F: abstention still publishes ranked alternatives so
                # partial answers survive instead of evaporating.
                ranked_alternatives=_ranked_alternative_statements(),
                next_check=VerificationEngine.next_check_for_abstention(
                    kind, list(limitations or [])
                ),
            ),
        )
        # Phase 4.2: abstention volume with limitations (closed kind).
        try:
            _emit_telemetry(
                ctx, tenant_id, investigation_id, "record_conclusion_limitation", "abstention"
            )
        except Exception:
            pass

    def _ranked_alternative_statements() -> list[str]:
        try:
            ranked_hyps = rank_hypotheses(list(hypotheses or []))
            out: list[str] = []
            for _hyp in ranked_hyps:
                try:
                    _status = str(
                        getattr(getattr(_hyp, "status", ""), "value", getattr(_hyp, "status", ""))
                    )
                    if _status in ("REJECTED", "CONTRADICTED"):
                        continue
                    _statement = str(getattr(_hyp, "statement", "") or "").strip()
                    if not _statement:
                        continue
                    out.append(f"{_status}: {_statement}"[:500])
                    if len(out) >= 3:
                        break
                except Exception:
                    continue
            return out
        except Exception:
            return []

    engine = VerificationEngine()
    policy = RootCauseVerificationPolicy()
    import os as _os

    if str(_os.environ.get("IAP_VERIFIER_V2", "new")).lower() == "legacy":
        # P0-4 rollback: pre-P0-3 semantics (first hypothesis, whole corpus,
        # no causal requirement, best-effort persistence).
        verification = engine.evaluate_hypothesis(
            hypotheses[0],
            evidence_items,
            [],
            [],
            policy,
            sibling_hypotheses=list(hypotheses[1:]),
        )
        gate = ConclusionGate()
        gate_policy = policy.model_copy(update={"require_causal_relationship": False})
        decision = await gate.evaluate(
            tenant_id,
            investigation_id,
            verification,
            [],
            [],
            evidence_items,
            gate_policy,
        )
        if not decision.approved:
            return InvestigationStatus.FAILED
        if finding_repo is not None:
            try:
                _legacy_ids = [e.evidence_id for e in evidence_items][:100]
                _legacy_hyp = hypotheses[0]
                await finding_repo.save_finding_record(
                    tenant_id,
                    Finding(
                        tenant_id=tenant_id,
                        investigation_id=investigation_id,
                        finding_type=FindingType.ROOT_CAUSE,
                        title=_legacy_hyp.title,
                        statement=_legacy_hyp.statement,
                        evidence_ids=_legacy_ids,
                        related_hypothesis_ids=[_legacy_hyp.id],
                        causal_chain=_legacy_ids[:1],
                        confidence=verification.confidence.overall_confidence,
                    ),
                )
                await finding_repo.save_conclusion(
                    tenant_id,
                    InvestigationConclusion(
                        tenant_id=tenant_id,
                        investigation_id=investigation_id,
                        status=ConclusionStatus.ROOT_CAUSE_ESTABLISHED
                        if verification.status == VerificationStatus.VERIFIED
                        else ConclusionStatus.ROOT_CAUSE_LIKELY,
                        root_cause=_legacy_hyp.statement,
                        supporting_evidence_ids=_legacy_ids,
                        supporting_hypothesis_ids=[_legacy_hyp.id],
                        confidence=verification.confidence.overall_confidence,
                    ),
                )
            except Exception as exc:
                logger.exception(
                    "Legacy finding/conclusion persist failed",
                    extra=_log_extra(
                        tenant_id=tenant_id,
                        investigation_id=investigation_id,
                        event="legacy finding persist failed",
                        error=str(exc),
                        error_type=type(exc).__name__,
                    ),
                )
        return InvestigationStatus.COMPLETED
    ranked = rank_hypotheses(list(hypotheses))
    # G9: same resolvable-citation preference as the verify activity.
    candidate = select_verifiable_candidate(ranked, evidence_by_id)
    if candidate is None:
        await _persist_abstention("refuted", ["all hypotheses rejected or contradicted"])
        return InvestigationStatus.COMPLETED
    bound = _resolve(getattr(candidate, "supporting_evidence_ids", []) or [])
    refuting = _resolve(getattr(candidate, "refuting_evidence_ids", []) or [])
    # Tranche B: same statement-matched range-read inclusion as verify.
    try:
        _bound_ids = {getattr(e, "evidence_id", None) for e in bound}
        for _extra in VerificationEngine.statement_range_reads(
            str(getattr(candidate, "statement", "") or ""),
            list(evidence_by_id.values()),
        ):
            if getattr(_extra, "evidence_id", None) not in _bound_ids:
                _bound_ids.add(getattr(_extra, "evidence_id", None))
                bound.append(_extra)
    except Exception:
        pass
    causal_chain = [
        CausalRelationship(
            cause=s.cause,
            effect=s.effect,
            mechanism=s.mechanism,
            confidence=0.7,
            evidence_ids=[e for e in (s.evidence_ids or []) if e in evidence_by_id],
        )
        for s in (getattr(candidate, "causal_steps", []) or [])
    ]
    causal_chain = [c for c in causal_chain if c.evidence_ids]
    # G1: location-answer fast path (mirrors verify_root_cause_activity).
    _verify_engine = VerificationEngine()
    code_value_verification = _verify_engine.validate_code_value_claim(
        candidate, bound, contradictions
    )
    code_value_claim = VerificationEngine.claims_field_value(
        str(getattr(candidate, "statement", "") or "")
    ) or str(VerificationEngine._fact_category_of(candidate) or "") == "SOURCE_VALUE_FACT"
    code_value_answered = code_value_verification is not None and not refuting
    history_verification = _verify_engine.validate_history_claim(candidate, bound, contradictions)
    history_answered = history_verification is not None and not refuting
    if code_value_claim and not code_value_answered:
        await _persist_abstention(
            "refuted" if refuting else "insufficient",
            ["code value claim lacks a matching deterministic resolution"],
            candidate.id,
        )
        return InvestigationStatus.COMPLETED
    location_verification = _verify_engine.validate_location_claim(
        candidate, bound, contradictions
    )
    location_answered = (
        location_verification is not None
        and not refuting
        and not code_value_claim
        and not history_answered
    )
    # T1-1: doc validation up front so the abstention check below sees it.
    doc_answered = False
    doc_verification = None
    try:
        doc_verification = VerificationEngine().validate_doc_claim(candidate, bound, contradictions)
        doc_answered = doc_verification is not None and not refuting
    except Exception:
        pass
    # #65: state validation up front, same abstention visibility.
    state_answered = False
    state_verification = None
    if (
        not location_answered
        and not code_value_answered
        and not history_answered
        and not doc_answered
        and not code_value_claim
    ):
        try:
            state_verification = VerificationEngine().validate_state_claim(
                candidate, bound, contradictions
            )
            state_answered = state_verification is not None and not refuting
        except Exception:
            pass
    if (
        not bound
        or refuting
        or (
            not causal_chain
            and not location_answered
            and not code_value_answered
            and not history_answered
            and not doc_answered
            and not state_answered
        )
    ):
        reasons = []
        if not bound:
            reasons.append("no bound supporting evidence")
        if refuting:
            reasons.append(f"{len(refuting)} refuting evidence item(s) bound")
        if (
            not causal_chain
            and not location_answered
            and not code_value_answered
            and not history_answered
            and not doc_answered
            and not state_answered
        ):
            reasons.append("no validated causal chain")
        if code_value_claim and not code_value_answered:
            reasons.append("code value claim lacks a matching deterministic resolution")
        await _persist_abstention("refuted" if refuting else "insufficient", reasons, candidate.id)
        return InvestigationStatus.COMPLETED
    if location_answered:
        verification = location_verification
    elif code_value_answered:
        verification = code_value_verification
    elif history_answered:
        verification = history_verification
    elif doc_answered and doc_verification is not None:
        verification = doc_verification
    elif state_answered and state_verification is not None:
        verification = state_verification
    if (
        not location_answered
        and not code_value_answered
        and not history_answered
        and not doc_answered
        and not state_answered
        and not code_value_claim
    ):
        _gate_candidate_id = getattr(candidate, "id", None)
        verification = engine.evaluate_hypothesis(
            candidate,
            bound,
            contradictions,
            causal_chain,
            policy,
            sibling_hypotheses=[
                h for h in (hypotheses or []) if getattr(h, "id", None) != _gate_candidate_id
            ],
        )
    gate = ConclusionGate()
    decision = await gate.evaluate(
        tenant_id,
        investigation_id,
        verification,
        contradictions,
        causal_chain,
        bound,
        policy,
        location_validated=location_answered,
        code_value_validated=code_value_answered,
        history_validated=history_answered,
        doc_validated=doc_answered,
        state_validated=state_answered,
    )
    if not decision.approved:
        logger.warning(
            "ConclusionGate denied COMPLETED: %s",
            "; ".join(decision.blockers),
            extra=_log_extra(
                tenant_id=tenant_id,
                investigation_id=investigation_id,
                event="ConclusionGate denied COMPLETED",
                blockers=list(decision.blockers),
            ),
        )
        # Phase 2b: lead with the verification explanation so the persisted
        # next_check names the failed proof (range-read rebind, runtime
        # observation, source-value resolution) instead of the generic
        # chain message. Fail-open: explanation add-on never blocks.
        _denial_reasons: list[str] = []
        try:
            _expl = str(getattr(verification, "explanation", "") or "").strip()
            if _expl:
                _denial_reasons.append(_expl[:500])
        except Exception:
            pass
        _denial_reasons.extend(list(decision.blockers))
        await _persist_abstention("insufficient", _denial_reasons, candidate.id)
        return InvestigationStatus.COMPLETED

    # P0-3: persist the Finding + InvestigationConclusion that back this
    # approval. Atomic correctness boundary with COMPLETED: a persistence
    # failure raises (the investigation retries/fails loudly) instead of
    # publishing COMPLETED with conclusion=null. Citations are scoped to
    # the verified hypothesis's bound evidence — never the whole corpus —
    # and the causal chain is the validated relationship evidence, never
    # an arbitrary first ID.
    if finding_repo is None:
        raise ApplicationFailure(
            "Finding repository is not configured; cannot publish conclusion",
            type="CONCLUSION_STORE_UNAVAILABLE",
            non_retryable=True,
        )
    from investigation_agent_platform.domain.hypothesis.models import (
        HypothesisStatus as _HStatus,
    )

    hyp = candidate
    supporting_ids = [e.evidence_id for e in bound]
    causal_ids = [eid for c in causal_chain for eid in c.evidence_ids]
    if hyp.status == _HStatus.SUPPORTED and hypothesis_repo is not None:
        hyp = hyp.transition_status(_HStatus.VERIFIED)
        await hypothesis_repo.save(tenant_id, hyp, investigation_id)
    finding = Finding(
        tenant_id=tenant_id,
        investigation_id=investigation_id,
        finding_type=FindingType.OBSERVATION
        if (
            location_answered
            or code_value_answered
            or history_answered
            or doc_answered
            or state_answered
        )
        else FindingType.ROOT_CAUSE,
        title=hyp.title,
        statement=hyp.statement,
        evidence_ids=supporting_ids,
        related_hypothesis_ids=[hyp.id],
        causal_chain=causal_ids,
        confidence=verification.confidence.overall_confidence,
    )
    await finding_repo.save_finding_record(tenant_id, finding)
    # G-4: incident fusion. When a journey spine exists and two or more
    # OBSERVATION findings were persisted across steps, merge them into one
    # incident-level finding (union evidence, concatenated statements).
    # Fail-open: fusion never blocks the conclusion.
    try:
        await _fuse_journey_findings(
            tenant_id=tenant_id,
            investigation_id=investigation_id,
            finding_repo=finding_repo,
            evidence_repo=getattr(ctx, "evidence_repo", None),
        )
    except Exception as exc:
        logger.warning(
            "journey fusion failed; single findings stand",
            extra=_log_extra(
                tenant_id=tenant_id,
                investigation_id=investigation_id,
                event="journey fusion failed",
                error=str(exc),
            ),
        )
    # F: ranked alternatives (runner-ups, capped) computed before the
    # conclusion so partial answers survive abstention.
    _alternatives: list[str] = []
    try:
        for _other in ranked or []:
            try:
                if _other is candidate:
                    continue
                _statement = str(getattr(_other, "statement", "") or "").strip()
                if not _statement:
                    continue
                _status = str(
                    getattr(getattr(_other, "status", ""), "value", getattr(_other, "status", ""))
                )
                if _status in ("REJECTED", "CONTRADICTED"):
                    continue
                _alternatives.append(f"{_status}: {_statement}"[:500])
                if len(_alternatives) >= 3:
                    break
            except Exception:
                continue
    except Exception:
        _alternatives = []
    # Phase 2.4: the published conclusion must map to its citations.
    # Material sentences that map to no cited claim or evidence row demote
    # the conclusion to likely (never silently established), and the typed
    # limitations plus the single next check persist on the conclusion.
    from investigation_agent_platform.application.investigation.verification import (
        VerificationEngine as _VerificationEngine,
    )

    _unmapped: list[str] = []
    try:
        _causal_texts = [
            f"{getattr(c, 'cause', '')} {getattr(c, 'mechanism', '')} {getattr(c, 'effect', '')}"
            for c in (causal_chain or [])
        ]
        _unmapped = _VerificationEngine.map_conclusion_sentences(
            str(getattr(hyp, "statement", "") or ""), list(bound or []), _causal_texts
        )
        verification = _VerificationEngine.apply_conclusion_mapping(verification, _unmapped)
    except Exception:
        _unmapped = []
    _untested_rivals: list[str] = []
    try:
        _untested_rivals = _VerificationEngine.untested_rival_statements(
            candidate, list(hypotheses or [])
        )
    except Exception:
        _untested_rivals = []
    _verification_limitations: list[str] = []
    try:
        _verification_limitations = [
            f"{getattr(getattr(lim, 'code', ''), 'value', getattr(lim, 'code', ''))}: {getattr(lim, 'detail', '')}"[
                :500
            ]
            for lim in (getattr(verification, "limitations", None) or [])
        ][:20]
    except Exception:
        _verification_limitations = []
    _next_check: str | None = None
    try:
        _next_check = _VerificationEngine.next_check_for(
            getattr(verification, "limitations", None) or [],
            _untested_rivals,
            _unmapped,
        )
    except Exception:
        _next_check = None
    # Phase 4.2: conclusion limitation kinds + location-exemption state.
    # Additive and fail-open; publication never waits on metrics.
    try:
        for _lim_text in _verification_limitations:
            _emit_telemetry(
                ctx,
                tenant_id,
                investigation_id,
                "record_conclusion_limitation",
                _limitation_kind(_lim_text),
            )
        if location_answered:
            _emit_telemetry(
                ctx,
                tenant_id,
                investigation_id,
                "record_location_answer",
                bool(_untested_rivals),
            )
    except Exception:
        pass
    conclusion = InvestigationConclusion(
        tenant_id=tenant_id,
        investigation_id=investigation_id,
        limitations=_verification_limitations,
        next_check=_next_check,
        status=ConclusionStatus.ANSWERED
        if (location_answered or code_value_answered or doc_answered)
        else (
            ConclusionStatus.ROOT_CAUSE_LIKELY
            if getattr(verification, "tier", "established") == "likely"
            else (
                ConclusionStatus.ROOT_CAUSE_ESTABLISHED
                if verification.status == VerificationStatus.VERIFIED
                else ConclusionStatus.ROOT_CAUSE_LIKELY
            )
        ),
        root_cause=hyp.statement,
        supporting_evidence_ids=supporting_ids,
        supporting_hypothesis_ids=[hyp.id],
        contradicting_evidence_ids=[e.evidence_id for e in refuting],
        confidence=verification.confidence.overall_confidence,
        # D5: in-band conclusions publish best-reason/against-evidence
        # with the provisional marker; established stays clean.
        best_reason=str(getattr(verification, "best_reason", "") or "")[:2000],
        against_evidence=[
            str(item)[:300]
            for item in list(getattr(verification, "against_evidence", []) or [])[:20]
        ],
        provisional=getattr(verification, "tier", "established") == "likely",
        ranked_alternatives=_alternatives,
    )
    await finding_repo.save_conclusion(tenant_id, conclusion)
    evidence_ids = supporting_ids
    # First producer for the evidence_relationships table: verified
    # hypothesis -> supporting evidence CORROBORATES edges, so future
    # CORRELATE actions traverse real rows instead of an empty graph.
    # Best-effort like the rows above — never reverts the approval.
    try:
        _inv_repo = getattr(ctx, "investigation_repo", None)
        _engine = getattr(ctx, "engine", None)
        if _inv_repo is not None and _engine is not None:
            _inv = await _inv_repo.get_by_id(tenant_id, investigation_id)
            if _inv is not None:
                from sqlalchemy.ext.asyncio import async_sessionmaker

                from investigation_agent_platform.infrastructure.correlation.repository import (
                    PostgresRelationshipRepository,
                )
                from investigation_agent_platform.ports.persistence.repositories import (
                    EvidenceRelationshipRecord,
                )

                _rel_repo = PostgresRelationshipRepository(
                    async_sessionmaker(_engine, expire_on_commit=False)
                )
                _rel_edges = [
                    EvidenceRelationshipRecord(
                        tenant_id=tenant_id,
                        application_id=_inv.application_id,
                        source_id=str(hyp.id),
                        target_id=str(eid),
                        type="CORROBORATES",
                        confidence=verification.confidence.overall_confidence,
                        metadata={"reason": hyp.statement[:500]},
                    )
                    for eid in evidence_ids
                ]
                _recorded = await _rel_repo.record_edges(
                    tenant_id, investigation_id, _inv.application_id, _rel_edges
                )
                logger.info(
                    "Correlation edges recorded for concluded investigation",
                    extra=_log_extra(
                        tenant_id=tenant_id,
                        investigation_id=investigation_id,
                        event="correlation edges recorded",
                        edges=_recorded,
                    ),
                )
    except Exception as exc:
        logger.warning(
            "Correlation edge persist skipped; conclusion stands",
            extra=_log_extra(
                tenant_id=tenant_id,
                investigation_id=investigation_id,
                event="correlation edges skipped",
                error=str(exc),
            ),
        )
    return InvestigationStatus.COMPLETED


@activity.defn
@traced("worker", outcome_from=_activity_outcome, extra_from=_activity_extra)
async def conclude_investigation_activity(
    params: ConcludeInvestigationInput,
) -> GenericActivityResult:
    data = _coerced_data(
        ConcludeInvestigationInput, params, activity="conclude_investigation_activity"
    )
    tenant_id_probe = str(data.get("tenant_id", ""))
    inv_id_probe = str(data.get("investigation_id", ""))
    logger.info(
        "conclude_investigation_activity started",
        extra=_log_extra(
            tenant_id=tenant_id_probe,
            investigation_id=inv_id_probe,
            event="conclude_investigation_activity started",
            status=str(data.get("status", "COMPLETED")),
        ),
    )
    try:
        tenant_id = str(data.get("tenant_id", ""))
        inv_id_str = str(data.get("investigation_id", ""))
        status = str(data.get("status", "COMPLETED"))
        reason = str(data.get("reason", "") or "")
        if not tenant_id or not inv_id_str:
            return GenericActivityResult(
                success=False, error="tenant_id and investigation_id required"
            )
        investigation_id = UUID(inv_id_str)
        ctx = _get_ctx()
        inv_repo: Any = getattr(ctx, "investigation_repo", None)
        if inv_repo is not None:
            try:
                inv = await inv_repo.get_by_id(tenant_id, investigation_id)
                if inv is not None:
                    from investigation_agent_platform.domain.investigation.models import (
                        InvestigationStatus,
                    )

                    try:
                        target = InvestigationStatus(status)
                    except ValueError:
                        target = InvestigationStatus.COMPLETED
                    if target == InvestigationStatus.COMPLETED:
                        # F-051: a COMPLETED conclusion requires ConclusionGate
                        # approval — the workflow's requested status alone is not
                        # authority. Denied conclusions become FAILED with the
                        # blockers recorded, never silent COMPLETED.
                        target = await _gate_conclusion_or_fail(tenant_id, investigation_id, ctx)
                    failure_code: str | None = None
                    conclude_reason = f"Workflow concluded with {target.value}"
                    if target == InvestigationStatus.FAILED:
                        # P0-3 honest abstention: epistemic endings (unverified
                        # within budget, budget exhausted) persist an
                        # INSUFFICIENT_EVIDENCE conclusion and complete —
                        # FAILED is reserved for platform/workflow errors.
                        epistemic = (reason or "").strip().lower() in (
                            "verification_failed_or_limit_reached",
                            "budget_exceeded",
                        )
                        if epistemic:
                            from investigation_agent_platform.domain.finding.models import (
                                ConclusionStatus,
                                InvestigationConclusion,
                            )

                            finding_repo: Any = getattr(ctx, "finding_repo", None)
                            if finding_repo is not None:
                                try:
                                    _epistemic_reason = str(reason or "").strip().lower()
                                    _epistemic_check = (
                                        "Raise the iteration or evidence budget and "
                                        "resume, then re-verify."
                                        if _epistemic_reason == "budget_exceeded"
                                        else "Bind supporting evidence and a cited causal "
                                        "chain to a candidate hypothesis, then re-verify."
                                    )
                                    await finding_repo.save_conclusion(
                                        tenant_id,
                                        InvestigationConclusion(
                                            tenant_id=tenant_id,
                                            investigation_id=investigation_id,
                                            status=ConclusionStatus.INSUFFICIENT_EVIDENCE,
                                            root_cause=None,
                                            supporting_evidence_ids=[],
                                            supporting_hypothesis_ids=[],
                                            contradicting_evidence_ids=[],
                                            confidence=0.0,
                                            limitations=[
                                                (
                                                    f"Investigation ended: {reason}. "
                                                    "No hypothesis met the evidence-bound "
                                                    "verification bar within budget."
                                                )
                                            ],
                                            next_check=_epistemic_check,
                                        ),
                                    )
                                    # Phase 4.2: abstention volume counter.
                                    try:
                                        _emit_telemetry(
                                            ctx,
                                            tenant_id,
                                            investigation_id,
                                            "record_conclusion_limitation",
                                            "abstention",
                                        )
                                    except Exception:
                                        pass
                                    target = InvestigationStatus.COMPLETED
                                    conclude_reason = (
                                        "Workflow concluded with COMPLETED (INSUFFICIENT_EVIDENCE)"
                                    )
                                except Exception as exc:
                                    logger.warning(
                                        "Abstention conclusion persist failed; staying FAILED",
                                        extra=_log_extra(
                                            tenant_id=tenant_id,
                                            investigation_id=inv_id_str,
                                            event="abstention persist failed",
                                            error=str(exc),
                                        ),
                                    )
                    if target == InvestigationStatus.FAILED:
                        # Terminal failure record: stamped onto metadata BEFORE
                        # the transition (see _stamp_failure) so it surfaces
                        # via GET detail and GET conclusion — no worker-log
                        # access needed.
                        requested_completed = status.upper() == "COMPLETED"
                        failure_code = _failure_code_for(reason, requested_completed)
                        conclude_reason = f"{conclude_reason} ({failure_code})"
                        inv = _stamp_failure(inv, failure_code, reason)
                    # C-2: loop telemetry (workflow counters + derived repo
                    # counts) stamped for every terminal status, same ride.
                    inv = _stamp_loop_telemetry(
                        inv,
                        _derive_loop_telemetry(
                            data.get("loop_telemetry"),
                            await _count_hypothesis_lifecycle(ctx, tenant_id, investigation_id),
                            await _count_evidence_banked(ctx, tenant_id, investigation_id),
                        ),
                    )
                    if inv.status != target:
                        # TO_DO-011: idempotency-wired — (investigation_id,
                        # target, version) is deterministic; see transition
                        # activity for the replay contract.
                        _idem_active_key: str | None = None
                        _idem_operation = "activity:conclude"
                        _idem_key = f"conclude:{inv_id_str}:{target.value}:{inv.version}"
                        _replayed, _idem_active_key = await _reserve_or_replay(
                            ctx=ctx,
                            tenant_id=tenant_id,
                            key=_idem_key,
                            request_payload={
                                "tenant_id": tenant_id,
                                "investigation_id": inv_id_str,
                                "target": target.value,
                                "version": inv.version,
                            },
                            operation=_idem_operation,
                            activity="conclude_investigation_activity",
                            investigation_id=inv_id_str,
                        )
                        if _replayed is not None:
                            return GenericActivityResult(
                                success=bool(_replayed.get("success", True)),
                                data=dict(_replayed.get("data") or {"status": target.value}),
                                error=_replayed.get("error"),
                            )
                        _, audit_degraded = await _transition_via_path(
                            tenant_id,
                            inv,
                            target,
                            conclude_reason,
                            inv_repo,
                            getattr(ctx, "transition_repo", None),
                            getattr(ctx, "timeline_repo", None),
                        )
                        if _idem_active_key is not None:
                            await _complete_reservation(
                                ctx=ctx,
                                tenant_id=tenant_id,
                                key=_idem_active_key,
                                operation=_idem_operation,
                                response={
                                    "success": True,
                                    "data": {
                                        "status": target.value,
                                        "audit_degraded": audit_degraded,
                                    },
                                },
                                activity="conclude_investigation_activity",
                                investigation_id=inv_id_str,
                            )
                        logger.info(
                            "conclude_investigation_activity completed",
                            extra=_log_extra(
                                tenant_id=tenant_id,
                                investigation_id=inv_id_str,
                                event="conclude_investigation_activity completed",
                                outcome="concluded",
                                status=target.value,
                                audit_degraded=audit_degraded,
                            ),
                        )
                    return GenericActivityResult(success=True, data={"status": target.value})
            except ApplicationFailure:
                raise
            except Exception as exc:
                logger.exception(
                    "conclude_investigation_activity transition failed",
                    extra=_log_extra(
                        tenant_id=tenant_id,
                        investigation_id=inv_id_str,
                        event="conclude_investigation_activity transition failed",
                        error=str(exc),
                        error_type=type(exc).__name__,
                    ),
                )
                raise _application_failure_from_exc(exc) from exc
        return GenericActivityResult(success=True, data={"status": status})
    except ApplicationFailure:
        await _abandon_reservation(
            ctx=locals().get("ctx"),
            tenant_id=str(locals().get("tenant_id", "") or ""),
            key=locals().get("_idem_active_key"),
            operation=str(locals().get("_idem_operation", "") or ""),
            activity="conclude_investigation_activity",
            investigation_id=str(locals().get("inv_id_str", "") or ""),
        )
        raise
    except Exception as exc:
        await _abandon_reservation(
            ctx=locals().get("ctx"),
            tenant_id=str(locals().get("tenant_id", "") or ""),
            key=locals().get("_idem_active_key"),
            operation=str(locals().get("_idem_operation", "") or ""),
            activity="conclude_investigation_activity",
            investigation_id=str(locals().get("inv_id_str", "") or ""),
        )
        logger.exception(
            "conclude_investigation_activity failed",
            extra=_log_extra(
                tenant_id=tenant_id_probe,
                investigation_id=inv_id_probe,
                event="conclude_investigation_activity failed",
                error=str(exc),
                error_type=type(exc).__name__,
            ),
        )
        raise _application_failure_from_exc(exc) from exc


@activity.defn
@traced("worker", outcome_from=_activity_outcome, extra_from=_activity_extra)
async def publish_event_activity(
    params: PublishEventInput,
) -> GenericActivityResult:
    # TO_DO-011: not additionally idempotency-wired — the outbox row itself
    # IS the idempotency record (F-058: durable, idempotent on
    # (tenant_id, idempotency_key) with investigation_id:event_type key);
    # a second reserve/complete layer would be redundant.
    data = _coerced_data(PublishEventInput, params, activity="publish_event_activity")
    tenant_id_probe = str(data.get("tenant_id", ""))
    inv_id_probe = str(data.get("investigation_id", ""))
    logger.info(
        "publish_event_activity started",
        extra=_log_extra(
            tenant_id=tenant_id_probe,
            investigation_id=inv_id_probe,
            event="publish_event_activity started",
            event_type=str(data.get("event", "INVESTIGATION_FINISHED")),
        ),
    )
    try:
        tenant_id = str(data.get("tenant_id", ""))
        inv_id_str = str(data.get("investigation_id", ""))
        event_type = str(data.get("event", "INVESTIGATION_FINISHED"))
        if not tenant_id or not inv_id_str:
            return GenericActivityResult(
                success=False, error="tenant_id and investigation_id required"
            )
        investigation_id = UUID(inv_id_str)
        ctx = _get_ctx()
        publisher: Any = getattr(ctx, "event_publisher", getattr(ctx, "publisher", None))

        inv_repo: Any = getattr(ctx, "investigation_repo", None)
        app_id = "unknown"
        if inv_repo is not None:
            try:
                inv_obj: Any = await inv_repo.get_by_id(tenant_id, investigation_id)
                if inv_obj is not None:
                    app_id = str(inv_obj.application_id)
            except Exception as exc:
                logger.exception(
                    "publish_event_activity investigation fetch failed",
                    extra=_log_extra(
                        tenant_id=tenant_id,
                        investigation_id=inv_id_str,
                        event="publish_event_activity investigation fetch failed",
                        error=str(exc),
                        error_type=type(exc).__name__,
                    ),
                )

        # F-058: transactional outbox — persist the event first (durable,
        # idempotent on (tenant_id, idempotency_key)), then attempt delivery.
        # The row survives publisher outages; dispatch_pending retries later.
        idempotency_key = f"{investigation_id}:{event_type}"
        payload = {
            "investigation_id": str(investigation_id),
            "application_id": app_id,
            "event_type": event_type,
        }
        outbox: Any = getattr(ctx, "outbox_repo", None)
        if outbox is not None:
            try:
                await outbox.enqueue(
                    tenant_id, investigation_id, event_type, idempotency_key, payload
                )
            except Exception as exc:
                logger.exception(
                    "publish_event_activity outbox enqueue failed",
                    extra=_log_extra(
                        tenant_id=tenant_id,
                        investigation_id=inv_id_str,
                        event="publish_event_activity outbox enqueue failed",
                        error=str(exc),
                        error_type=type(exc).__name__,
                        idempotency_key=idempotency_key,
                    ),
                )
                raise _application_failure_from_exc(exc) from exc
            if publisher is not None:
                try:
                    await outbox.dispatch_pending(tenant_id, publisher, batch_size=25)
                except Exception as exc:
                    logger.warning(
                        "publish_event_activity dispatch deferred; event stays queued",
                        extra=_log_extra(
                            tenant_id=tenant_id,
                            investigation_id=inv_id_str,
                            event="publish_event_activity dispatch deferred",
                            error=str(exc),
                            error_type=type(exc).__name__,
                        ),
                    )
            logger.info(
                "publish_event_activity completed",
                extra=_log_extra(
                    tenant_id=tenant_id,
                    investigation_id=inv_id_str,
                    event="publish_event_activity completed",
                    outcome="queued",
                    event_type=event_type,
                ),
            )
            return GenericActivityResult(success=True, data={"event": event_type, "queued": True})

        if publisher is None:
            # No outbox and no publisher wired — explicit failure, not a silent no-op.
            raise ApplicationFailure(
                "No event publisher or outbox configured; cannot publish event",
                type="EventPublisherUnavailable",
                non_retryable=True,
            )

        # Legacy direct-publish fallback (only when no outbox repo is wired,
        # e.g. minimal dev contexts).
        try:
            from investigation_agent_platform.domain.events.base import InvestigationConcluded

            event = InvestigationConcluded(
                tenant_id=tenant_id,
                investigation_id=investigation_id,
                application_id=app_id,
                correlation_id=investigation_id,
                final_status=event_type,
                overall_confidence=1.0,
            )
            await publisher.publish_domain_event(tenant_id, event)
        except Exception:
            # Fallback to generic envelope publish.
            try:
                from investigation_agent_platform.ports.messaging.publisher import EventEnvelope

                envelope = EventEnvelope(
                    event_type=event_type,
                    tenant_id=tenant_id,
                    investigation_id=investigation_id,
                    idempotency_key=idempotency_key,
                    payload={"investigation_id": str(investigation_id)},
                )
                await publisher.publish(envelope)
            except Exception as exc:
                logger.exception(
                    "publish_event_activity fallback failed",
                    extra=_log_extra(
                        tenant_id=tenant_id,
                        investigation_id=inv_id_str,
                        event="publish_event_activity fallback failed",
                        error=str(exc),
                        error_type=type(exc).__name__,
                    ),
                )
                raise _application_failure_from_exc(exc) from exc

        logger.info(
            "publish_event_activity completed",
            extra=_log_extra(
                tenant_id=tenant_id,
                investigation_id=inv_id_str,
                event="publish_event_activity completed",
                outcome="published",
                event_type=event_type,
            ),
        )
        return GenericActivityResult(success=True, data={"event": event_type})
    except ApplicationFailure:
        raise
    except Exception as exc:
        logger.exception(
            "publish_event_activity failed",
            extra=_log_extra(
                tenant_id=tenant_id_probe,
                investigation_id=inv_id_probe,
                event="publish_event_activity failed",
                error=str(exc),
                error_type=type(exc).__name__,
            ),
        )
        raise _application_failure_from_exc(exc) from exc


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
    try:
        query_text = str(query or "").strip()
        logger.info(
            "reference-docs recall entry",
            extra=_log_extra(
                tenant_id=str(tenant_id),
                event="reference recall entry",
                has_query=bool(query_text),
                query_head=query_text[:80],
                has_repo=getattr(ctx, "reference_repo", None) is not None,
            ),
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
        logger.info(
            "reference-docs recall completed",
            extra=_log_extra(
                tenant_id=str(tenant_id),
                event="reference recall completed",
                query=str(query_text)[:120],
                shaped=_shape_refdoc_query(query_text),
                hits=len(hits or []),
            ),
        )
        return _shape_reference_hits(hits)
    except Exception as exc:
        logger.warning(
            "reference-docs recall failed; continuing without orientation",
            extra=_log_extra(
                tenant_id=str(tenant_id),
                event="reference recall failed",
                error=str(exc),
            ),
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
    data = _coerced_data(RetrieveKnowledgeInput, params, activity="retrieve_knowledge_activity")
    tenant_id_probe = str(data.get("tenant_id", ""))
    inv_id_probe = str(data.get("investigation_id", ""))
    logger.info(
        "retrieve_knowledge_activity started",
        extra=_log_extra(
            tenant_id=tenant_id_probe,
            investigation_id=inv_id_probe,
            event="retrieve_knowledge_activity started",
        ),
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
            logger.info(
                "retrieve_knowledge_activity completed",
                extra=_log_extra(
                    tenant_id=tenant_id,
                    investigation_id=inv_id_str,
                    event="retrieve_knowledge_activity completed",
                    verified_facts=len(context.verified_facts),
                    preferences=len(context.preferences),
                    temporal_summary=len(context.temporal_summary),
                    excluded_stale_count=context.excluded_stale_count,
                    reference_docs=len(reference_docs),
                    has_intake_seed=bool(intake_seed),
                ),
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
            logger.exception(
                "retrieve_knowledge_activity degraded to empty context",
                extra=_log_extra(
                    tenant_id=tenant_id,
                    investigation_id=inv_id_str,
                    event="retrieve_knowledge_activity degraded",
                    error=str(exc),
                    error_type=type(exc).__name__,
                ),
            )
            return GenericActivityResult(
                success=True, data={"verified_facts": [], "excluded_stale_count": -1}
            )
    except ApplicationFailure:
        raise
    except Exception as exc:
        logger.exception(
            "retrieve_knowledge_activity failed",
            extra=_log_extra(
                tenant_id=tenant_id_probe,
                investigation_id=inv_id_probe,
                event="retrieve_knowledge_activity failed",
                error=str(exc),
                error_type=type(exc).__name__,
            ),
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
    data = _coerced_data(CaptureKnowledgeInput, params, activity="capture_knowledge_activity")
    tenant_id_probe = str(data.get("tenant_id", ""))
    inv_id_probe = str(data.get("investigation_id", ""))
    logger.info(
        "capture_knowledge_activity started",
        extra=_log_extra(
            tenant_id=tenant_id_probe,
            investigation_id=inv_id_probe,
            event="capture_knowledge_activity started",
        ),
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
                logger.warning(
                    "Context cache invalidation failed open",
                    extra=_log_extra(
                        tenant_id=tenant_id,
                        investigation_id=inv_id_str,
                        event="capture cache invalidate skipped",
                        error=str(exc),
                    ),
                )
            logger.info(
                "capture_knowledge_activity completed",
                extra=_log_extra(
                    tenant_id=tenant_id,
                    investigation_id=inv_id_str,
                    event="capture_knowledge_activity completed",
                    outcome="captured",
                    artifacts_stored=len(stored),
                ),
            )
            return GenericActivityResult(success=True, data={"artifacts_stored": len(stored)})
        except Exception as exc:
            logger.exception(
                "capture_knowledge_activity failed; investigation unaffected",
                extra=_log_extra(
                    tenant_id=tenant_id,
                    investigation_id=inv_id_str,
                    event="capture_knowledge_activity degraded",
                    error=str(exc),
                    error_type=type(exc).__name__,
                ),
            )
            return GenericActivityResult(success=True, data={"artifacts_stored": 0})
    except ApplicationFailure:
        raise
    except Exception as exc:
        logger.exception(
            "capture_knowledge_activity failed",
            extra=_log_extra(
                tenant_id=tenant_id_probe,
                investigation_id=inv_id_probe,
                event="capture_knowledge_activity failed",
                error=str(exc),
                error_type=type(exc).__name__,
            ),
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
    data = _coerced_data(SweepKnowledgeInput, params, activity="sweep_knowledge_activity")
    logger.info(
        "sweep_knowledge_activity started",
        extra=_log_extra(
            event="sweep_knowledge_activity started",
            tenants=list(data.get("tenants", []) or []),
        ),
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
                    logger.warning(
                        "Context cache invalidation failed open",
                        extra=_log_extra(
                            tenant_id=tenant_id,
                            event="sweep cache invalidate skipped",
                            error=str(exc),
                        ),
                    )
            except Exception as exc:
                logger.exception(
                    "Knowledge sweep failed for tenant; continuing",
                    extra=_log_extra(
                        tenant_id=tenant_id,
                        event="sweep_knowledge_activity tenant failed",
                        error=str(exc),
                        error_type=type(exc).__name__,
                    ),
                )
                per_tenant[tenant_id] = -1
        logger.info(
            "sweep_knowledge_activity completed",
            extra=_log_extra(
                event="sweep_knowledge_activity completed",
                outcome="swept",
                expired_total=total_expired,
            ),
        )
        return GenericActivityResult(
            success=True,
            data={"expired_total": total_expired, "per_tenant": per_tenant},
        )
    except ApplicationFailure:
        raise
    except Exception as exc:
        logger.exception(
            "sweep_knowledge_activity failed",
            extra=_log_extra(
                event="sweep_knowledge_activity failed",
                error=str(exc),
                error_type=type(exc).__name__,
            ),
        )
        raise _application_failure_from_exc(exc) from exc


@dataclass
class CollectSnapshotsInput:
    """One repository's snapshot-retention pass (ISSUE-4)."""

    tenant_id: str
    repository_id: str


@dataclass
class CollectSnapshotsOutput:
    collected_revisions: list[str] = field(default_factory=list)
    pinned_count: int = 0
    deleted_node_count: int = 0
    deleted_edge_count: int = 0
    error: str | None = None


@activity.defn
async def collect_snapshots_activity(
    params: CollectSnapshotsInput,
) -> CollectSnapshotsOutput:
    # TO_DO-011: not idempotency-wired — collection is idempotent by design
    # (already-collected snapshots report, never duplicate; audit nodes are
    # never deleted).
    """Classify one repository's topology snapshots and collect the
    unpinned, out-of-window ones (ISSUE-4 janitor).

    Never deletes a ``TopologySnapshot`` audit node, only the AST subgraph
    beneath it, and never touches anything pinned by an open investigation's
    own recorded evidence. Collection failures are reported, not raised —
    a transient Neo4j hiccup on one repository must not fail the whole
    scheduled pass (the workflow retries this activity per repository).
    """
    data = _coerced_data(CollectSnapshotsInput, params, activity="collect_snapshots_activity")
    tenant_id_probe = str(data.get("tenant_id", ""))
    logger.info(
        "collect_snapshots_activity started",
        extra=_log_extra(
            tenant_id=tenant_id_probe,
            event="collect_snapshots_activity started",
            repository_id=str(data.get("repository_id", "")),
        ),
    )
    tenant_id = str(data.get("tenant_id", ""))
    repository_id = str(data.get("repository_id", ""))
    if not tenant_id or not repository_id:
        return CollectSnapshotsOutput(error="tenant_id and repository_id required")

    try:
        from datetime import UTC, datetime

        from investigation_agent_platform.application.topology.pinned_revisions import (
            EvidenceBackedPinnedRevisionsProvider,
        )
        from investigation_agent_platform.application.topology.retention import (
            classify_snapshots,
        )

        ctx = _get_ctx()
        topology_adapter: Any = getattr(ctx, "topology_adapter", None)
        if topology_adapter is None:
            return CollectSnapshotsOutput(error="topology_adapter is not configured")
        topology_config: Any = getattr(ctx, "topology_config", None)
        if topology_config is None or not getattr(topology_config, "retention_enabled", False):
            return CollectSnapshotsOutput(error="retention is not enabled")

        pinned_provider = EvidenceBackedPinnedRevisionsProvider(
            investigation_repo=ctx.investigation_repo,
            evidence_repo=ctx.evidence_repo,
        )
        pinned_revisions = await pinned_provider.list_pinned_revisions(tenant_id, repository_id)
        snapshots = await topology_adapter.list_snapshots(tenant_id, repository_id)
        classification = classify_snapshots(
            snapshots,
            pinned_revisions,
            now=datetime.now(UTC),
            retention_window_days=topology_config.retention_window_days,
            retention_max_snapshots_per_repository=(
                topology_config.retention_max_snapshots_per_repository
            ),
        )

        collected: list[str] = []
        deleted_nodes = 0
        deleted_edges = 0
        for snapshot in classification.collectible:
            result = await topology_adapter.collect_snapshot(
                tenant_id, repository_id, snapshot.revision
            )
            if not result.already_collected:
                collected.append(snapshot.revision)
                deleted_nodes += result.deleted_node_count
                deleted_edges += result.deleted_edge_count

        logger.info(
            "collect_snapshots_activity completed",
            extra=_log_extra(
                tenant_id=tenant_id,
                event="collect_snapshots_activity completed",
                outcome="collected",
                repository_id=repository_id,
                collected_count=len(collected),
            ),
        )
        return CollectSnapshotsOutput(
            collected_revisions=collected,
            pinned_count=len(classification.pinned),
            deleted_node_count=deleted_nodes,
            deleted_edge_count=deleted_edges,
        )
    except ApplicationFailure:
        raise
    except Exception as exc:
        logger.exception(
            "collect_snapshots_activity failed",
            extra=_log_extra(
                tenant_id=tenant_id_probe,
                event="collect_snapshots_activity failed",
                error=str(exc),
                error_type=type(exc).__name__,
                repository_id=repository_id,
            ),
        )
        return CollectSnapshotsOutput(error=str(exc))


# ---------------------------------------------------------------------------
# Part 11.5: structured input requirements
# ---------------------------------------------------------------------------


@dataclass
class RecordInputRequirementInput:
    tenant_id: str
    investigation_id: str
    reason: str = ""
    json_schema: dict[str, Any] = field(default_factory=dict)
    classification: str = "INTERNAL"
    resume_status: str = "INVESTIGATING"
    promote_to_evidence: bool = False
    expires_in_seconds: int | None = None


@dataclass
class SetInputRequirementStateInput:
    tenant_id: str
    requirement_id: str
    expected_version: int
    state: str = "FULFILLED"


@dataclass
class PromoteFulfillmentEvidenceInput:
    tenant_id: str
    investigation_id: str
    requirement_id: str
    data: dict[str, Any] = field(default_factory=dict)
    classification: str = "INTERNAL"


@activity.defn
@traced("worker", outcome_from=_activity_outcome, extra_from=_activity_extra)
async def record_input_requirement_activity(
    params: RecordInputRequirementInput,
) -> GenericActivityResult:
    # TO_DO-011: not idempotency-wired — each call mints a fresh requirement
    # row (new UUID), so no deterministic key is derivable from the inputs; a
    # caller-supplied idempotency key would need repository-level dedup owned
    # by Wave 3. The workflow emits one record call per input marker.
    """Persist one InputRequirement row and return its identity + wait budget."""
    from datetime import timedelta

    from investigation_agent_platform.domain.investigation.input_requirements import (
        AWAITING_INPUT_SOURCES,
        InputRequirement,
    )
    from investigation_agent_platform.domain.investigation.models import InvestigationStatus

    data = _coerced_data(
        RecordInputRequirementInput, params, activity="record_input_requirement_activity"
    )
    tenant_id_probe = str(data.get("tenant_id", ""))
    inv_id_probe = str(data.get("investigation_id", ""))
    logger.info(
        "record_input_requirement_activity started",
        extra=_log_extra(
            tenant_id=tenant_id_probe,
            investigation_id=inv_id_probe,
            event="record_input_requirement_activity started",
        ),
    )
    try:
        tenant_id = str(data.get("tenant_id", ""))
        inv_id_str = str(data.get("investigation_id", ""))
        reason = str(data.get("reason", "") or "Additional input required")
        if not tenant_id or not inv_id_str:
            return GenericActivityResult(
                success=False, error="tenant_id and investigation_id required"
            )
        investigation_id = UUID(inv_id_str)
        try:
            resume_status = InvestigationStatus(str(data.get("resume_status", "INVESTIGATING")))
        except ValueError:
            resume_status = InvestigationStatus.INVESTIGATING
        if resume_status not in AWAITING_INPUT_SOURCES:
            return GenericActivityResult(
                success=False,
                error=f"resume_status {resume_status.value} cannot suspend for input",
            )
        expires_in = data.get("expires_in_seconds")
        expires_at = None
        wait_timeout_seconds: float | None = None
        if isinstance(expires_in, (int, float)) and expires_in > 0:
            from datetime import UTC, datetime

            expires_at = datetime.now(UTC) + timedelta(seconds=float(expires_in))
            wait_timeout_seconds = float(expires_in)
        requirement = InputRequirement(
            tenant_id=tenant_id,
            investigation_id=investigation_id,
            reason=reason[:512],
            json_schema=dict(data.get("json_schema", {}) or {}),
            classification=str(data.get("classification", "INTERNAL")),
            resume_status=resume_status,
            promote_to_evidence=bool(data.get("promote_to_evidence", False)),
            expires_at=expires_at,
        )
        ctx = _get_ctx()
        await ctx.input_repo.create_requirement(tenant_id, requirement)
        result_data: dict[str, Any] = {
            "requirement_id": str(requirement.requirement_id),
            "requirement_version": requirement.requirement_version,
            "resume_status": resume_status.value,
            "promote_to_evidence": requirement.promote_to_evidence,
        }
        if wait_timeout_seconds is not None:
            result_data["wait_timeout_seconds"] = wait_timeout_seconds
        logger.info(
            "record_input_requirement_activity completed",
            extra=_log_extra(
                tenant_id=tenant_id,
                investigation_id=inv_id_str,
                event="record_input_requirement_activity completed",
                outcome="recorded",
                requirement_id=str(requirement.requirement_id),
            ),
        )
        return GenericActivityResult(success=True, data=result_data)
    except ApplicationFailure:
        raise
    except Exception as exc:
        logger.exception(
            "record_input_requirement_activity failed",
            extra=_log_extra(
                tenant_id=tenant_id_probe,
                investigation_id=inv_id_probe,
                event="record_input_requirement_activity failed",
                error=str(exc),
                error_type=type(exc).__name__,
            ),
        )
        raise _application_failure_from_exc(exc) from exc


@activity.defn
@traced("worker", outcome_from=_activity_outcome, extra_from=_activity_extra)
async def set_input_requirement_state_activity(
    params: SetInputRequirementStateInput,
) -> GenericActivityResult:
    # TO_DO-011: not idempotency-wired — the compare-and-set on
    # (requirement_id, expected_version) IS the idempotency mechanism: a
    # retry with the same version conflicts loudly instead of duplicating.
    """Compare-and-set a requirement's state (FULFILLED/EXPIRED/CANCELLED)."""
    from investigation_agent_platform.domain.investigation.input_requirements import (
        RequirementState,
    )

    data = _coerced_data(
        SetInputRequirementStateInput, params, activity="set_input_requirement_state_activity"
    )
    tenant_id_probe = str(data.get("tenant_id", ""))
    logger.info(
        "set_input_requirement_state_activity started",
        extra=_log_extra(
            tenant_id=tenant_id_probe,
            event="set_input_requirement_state_activity started",
            requirement_id=str(data.get("requirement_id", "")),
        ),
    )
    try:
        tenant_id = str(data.get("tenant_id", ""))
        req_id_str = str(data.get("requirement_id", ""))
        if not tenant_id or not req_id_str:
            return GenericActivityResult(
                success=False, error="tenant_id and requirement_id required"
            )
        try:
            state = RequirementState(str(data.get("state", "FULFILLED")))
        except ValueError:
            return GenericActivityResult(
                success=False, error=f"unknown requirement state {data.get('state')!r}"
            )
        try:
            expected_version = int(data.get("expected_version", 1))
        except (TypeError, ValueError):
            return GenericActivityResult(success=False, error="expected_version invalid")
        ctx = _get_ctx()
        await ctx.input_repo.set_state(tenant_id, UUID(req_id_str), expected_version, state)
        logger.info(
            "set_input_requirement_state_activity completed",
            extra=_log_extra(
                tenant_id=tenant_id,
                event="set_input_requirement_state_activity completed",
                outcome="state_set",
                requirement_id=req_id_str,
                state=state.value,
            ),
        )
        return GenericActivityResult(success=True, data={"state": state.value})
    except ApplicationFailure:
        raise
    except Exception as exc:
        logger.exception(
            "set_input_requirement_state_activity failed",
            extra=_log_extra(
                tenant_id=tenant_id_probe,
                event="set_input_requirement_state_activity failed",
                error=str(exc),
                error_type=type(exc).__name__,
                requirement_id=str(data.get("requirement_id", "")),
            ),
        )
        raise _application_failure_from_exc(exc) from exc


@activity.defn
@traced("worker", outcome_from=_activity_outcome, extra_from=_activity_extra)
async def promote_fulfillment_evidence_activity(
    params: PromoteFulfillmentEvidenceInput,
) -> GenericActivityResult:
    """Persist caller-supplied fulfillment data as DOCUMENTATION evidence.

    Only runs when the requirement opted in (``promote_to_evidence``); the
    classification travels from the requirement, and provenance records the
    requirement id so the evidence is traceable back to its source.
    """
    import hashlib
    import json
    from datetime import UTC, datetime

    from investigation_agent_platform.domain.evidence.models import (
        ClassificationLevel,
        Evidence,
        EvidenceType,
    )
    from investigation_agent_platform.domain.provenance.models import (
        EvidenceFreshness,
        EvidenceProvenance,
        QueryFingerprint,
        SourceLocation,
    )

    data = _coerced_data(
        PromoteFulfillmentEvidenceInput,
        params,
        activity="promote_fulfillment_evidence_activity",
    )
    tenant_id_probe = str(data.get("tenant_id", ""))
    inv_id_probe = str(data.get("investigation_id", ""))
    logger.info(
        "promote_fulfillment_evidence_activity started",
        extra=_log_extra(
            tenant_id=tenant_id_probe,
            investigation_id=inv_id_probe,
            event="promote_fulfillment_evidence_activity started",
            requirement_id=str(data.get("requirement_id", "")),
        ),
    )
    try:
        tenant_id = str(data.get("tenant_id", ""))
        inv_id_str = str(data.get("investigation_id", ""))
        req_id_str = str(data.get("requirement_id", ""))
        payload = data.get("data", {}) or {}
        if not tenant_id or not inv_id_str or not req_id_str:
            return GenericActivityResult(
                success=False,
                error="tenant_id, investigation_id and requirement_id required",
            )
        if not isinstance(payload, dict):
            return GenericActivityResult(success=False, error="fulfillment data invalid")
        try:
            classification = ClassificationLevel(str(data.get("classification", "INTERNAL")))
        except ValueError:
            classification = ClassificationLevel.INTERNAL
        investigation_id = UUID(inv_id_str)
        now = datetime.now(UTC)
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        snippet = canonical[:4000]
        evidence = Evidence(
            tenant_id=tenant_id,
            investigation_id=investigation_id,
            evidence_type=EvidenceType.DOCUMENTATION,
            provider="input-fulfillment",
            source=f"input-requirement:{req_id_str}",
            title="Caller-supplied investigation input",
            summary=f"Fulfillment for input requirement {req_id_str}",
            content_snippet=snippet,
            attributes={"requirement_id": req_id_str},
            observed_at=None,
            retrieved_at=now,
            provenance=EvidenceProvenance(
                tenant_id=tenant_id,
                investigation_id=investigation_id,
                provider_type="input-fulfillment",
                requested_provider_id="input-fulfillment",
                actual_provider_id="input-fulfillment",
                source_system="input-requirement",
                retrieval_timestamp=now,
                query_fingerprint=QueryFingerprint(
                    provider_type="input-fulfillment",
                    operation="fulfill",
                    normalized_query_hash=hashlib.sha256(req_id_str.encode()).hexdigest(),
                ),
                source_location=SourceLocation(system="input-requirement", identifier=req_id_str),
            ),
            freshness=EvidenceFreshness(observed_at=None, retrieved_at=now),
            classification=classification,
            fingerprint=hashlib.sha256(
                f"input-fulfillment:{req_id_str}:{canonical}".encode()
            ).hexdigest(),
        )
        ctx = _get_ctx()
        # TO_DO-011: idempotency-wired — the evidence fingerprint is
        # deterministic over (requirement_id, canonical payload), so the
        # requirement id is the deterministic reservation key.
        _idem_active_key: str | None = None
        _idem_operation = "activity:promote_fulfillment"
        _idem_key = f"promote_fulfillment:{req_id_str}"
        _replayed, _idem_active_key = await _reserve_or_replay(
            ctx=ctx,
            tenant_id=tenant_id,
            key=_idem_key,
            request_payload={
                "tenant_id": tenant_id,
                "investigation_id": inv_id_str,
                "requirement_id": req_id_str,
            },
            operation=_idem_operation,
            activity="promote_fulfillment_evidence_activity",
            investigation_id=inv_id_str,
        )
        if _replayed is not None:
            return GenericActivityResult(
                success=bool(_replayed.get("success", True)),
                data=dict(_replayed.get("data") or {}),
                error=_replayed.get("error"),
            )
        await ctx.evidence_repo.save(tenant_id, evidence, investigation_id)
        if _idem_active_key is not None:
            await _complete_reservation(
                ctx=ctx,
                tenant_id=tenant_id,
                key=_idem_active_key,
                operation=_idem_operation,
                response={
                    "success": True,
                    "data": {"evidence_id": str(evidence.evidence_id)},
                },
                activity="promote_fulfillment_evidence_activity",
                investigation_id=inv_id_str,
            )
        logger.info(
            "promote_fulfillment_evidence_activity completed",
            extra=_log_extra(
                tenant_id=tenant_id,
                investigation_id=inv_id_str,
                event="promote_fulfillment_evidence_activity completed",
                outcome="promoted",
                requirement_id=req_id_str,
                evidence_id=str(evidence.evidence_id),
            ),
        )
        return GenericActivityResult(success=True, data={"evidence_id": str(evidence.evidence_id)})
    except ApplicationFailure:
        await _abandon_reservation(
            ctx=locals().get("ctx"),
            tenant_id=str(locals().get("tenant_id", "") or ""),
            key=locals().get("_idem_active_key"),
            operation=str(locals().get("_idem_operation", "") or ""),
            activity="promote_fulfillment_evidence_activity",
            investigation_id=str(locals().get("inv_id_str", "") or ""),
        )
        raise
    except Exception as exc:
        await _abandon_reservation(
            ctx=locals().get("ctx"),
            tenant_id=str(locals().get("tenant_id", "") or ""),
            key=locals().get("_idem_active_key"),
            operation=str(locals().get("_idem_operation", "") or ""),
            activity="promote_fulfillment_evidence_activity",
            investigation_id=str(locals().get("inv_id_str", "") or ""),
        )
        logger.exception(
            "promote_fulfillment_evidence_activity failed",
            extra=_log_extra(
                tenant_id=tenant_id_probe,
                investigation_id=inv_id_probe,
                event="promote_fulfillment_evidence_activity failed",
                error=str(exc),
                error_type=type(exc).__name__,
            ),
        )
        raise _application_failure_from_exc(exc) from exc


# ---------------------------------------------------------------------------
# Part 11.7: finding clustering
# ---------------------------------------------------------------------------


@dataclass
class RunClusteringInput:
    tenant_id: str
    job_id: str = ""
    limit: int = 500
    batch_size: int = 25


@dataclass
class UpdateClusteringJobInput:
    tenant_id: str
    job_id: str
    status: str = ""
    stage: dict[str, Any] | None = None
    error: str | None = None
    progress: int | None = None
    total: int | None = None


@activity.defn
async def run_clustering_activity(
    params: RunClusteringInput,
) -> GenericActivityResult:
    # TO_DO-011: not idempotency-wired — a bounded incremental pass that is
    # re-runnable by design (re-running assigns nothing new); quota-exhaustion
    # returns a failure result rather than raising.
    """Run one bounded incremental clustering pass (Part 11.7)."""
    from investigation_agent_platform.application.finding.clustering_service import (
        FindingClusterService,
        QuotaExceededError,
    )
    from investigation_agent_platform.domain.common.extension import CapabilityScope
    from investigation_agent_platform.domain.common.quotas import QuotaPolicy

    data = _coerced_data(RunClusteringInput, params, activity="run_clustering_activity")
    tenant_id_probe = str(data.get("tenant_id", ""))
    logger.info(
        "run_clustering_activity started",
        extra=_log_extra(
            tenant_id=tenant_id_probe,
            event="run_clustering_activity started",
            job_id=str(data.get("job_id", "")),
        ),
    )
    try:
        tenant_id = str(data.get("tenant_id", ""))
        if not tenant_id:
            return GenericActivityResult(success=False, error="tenant_id required")
        try:
            limit = max(1, min(int(data.get("limit", 500)), 2000))
            batch_size = max(1, min(int(data.get("batch_size", 25)), 100))
        except (TypeError, ValueError):
            return GenericActivityResult(success=False, error="limit/batch_size invalid")
        ctx = _get_ctx()

        gateway: Any = None
        try:
            from investigation_agent_platform.infrastructure.configuration.config import (
                load_application_config_from_env,
            )
            from investigation_agent_platform.infrastructure.reasoning.factory import (
                create_llm_gateway,
            )

            gateway = create_llm_gateway(load_application_config_from_env().llm)
        except Exception as exc:
            # Lexical-only degraded mode: taxonomy preserved, new findings
            # explicitly UNASSIGNED with provenance. Loud, never silent.
            logger.exception(
                "Clustering without LLM gateway; lexical-only mode",
                extra=_log_extra(
                    tenant_id=tenant_id,
                    event="run_clustering_activity lexical-only",
                    error=str(exc),
                    error_type=type(exc).__name__,
                ),
            )

        quota_policy: QuotaPolicy | None = None
        quota_guarded = False
        try:
            from investigation_agent_platform.infrastructure.configuration.config import (
                load_application_config_from_env as _load_cfg,
            )

            quota_policy = QuotaPolicy.model_validate(_load_cfg().quotas.model_dump(mode="json"))
            quota_guarded = True
        except Exception as exc:
            logger.exception(
                "Clustering without quota guard; config unavailable",
                extra=_log_extra(
                    tenant_id=tenant_id,
                    event="run_clustering_activity quota unguarded",
                    error=str(exc),
                    error_type=type(exc).__name__,
                ),
            )

        service = FindingClusterService(
            finding_repo=getattr(ctx, "finding_repo", None),
            cluster_repo=getattr(ctx, "finding_cluster_repo", None),
            llm_gateway=gateway,
            embedder=None,
            batch_size=batch_size,
        )
        if service._finding_repo is None or service._cluster_repo is None:
            return GenericActivityResult(
                success=False, error="finding or cluster repository unavailable"
            )
        scope = CapabilityScope(tenant_id=tenant_id)
        try:
            result = await service.run_incremental(
                tenant_id,
                limit=limit,
                quota_enforcer=getattr(ctx, "quota_enforcer", None) if quota_guarded else None,
                quota_policy=quota_policy,
                scope=scope if quota_guarded else None,
            )
        except QuotaExceededError as exc:
            return GenericActivityResult(success=False, error=str(exc))
        result_data = result.model_dump(mode="json")
        result_data["quota_guarded"] = quota_guarded
        result_data["lexical_only"] = gateway is None
        logger.info(
            "run_clustering_activity completed",
            extra=_log_extra(
                tenant_id=tenant_id,
                event="run_clustering_activity completed",
                outcome="clustered",
                quota_guarded=quota_guarded,
            ),
        )
        return GenericActivityResult(success=True, data=result_data)
    except ApplicationFailure:
        raise
    except Exception as exc:
        logger.exception(
            "run_clustering_activity failed",
            extra=_log_extra(
                tenant_id=tenant_id_probe,
                event="run_clustering_activity failed",
                error=str(exc),
                error_type=type(exc).__name__,
            ),
        )
        raise _application_failure_from_exc(exc) from exc


@activity.defn
async def update_clustering_job_activity(
    params: UpdateClusteringJobInput,
) -> GenericActivityResult:
    # TO_DO-011: not idempotency-wired — optimistic-concurrency versioning
    # (expected_version) IS the mechanism: a retried stale update conflicts
    # loudly instead of duplicating.
    """Apply a status/stage/error update to the linked BackgroundJob row."""
    from uuid import UUID as _UUID

    from investigation_agent_platform.domain.common.background_job import (
        BackgroundJobStage,
        BackgroundJobStatus,
    )

    data = _coerced_data(
        UpdateClusteringJobInput, params, activity="update_clustering_job_activity"
    )
    tenant_id_probe = str(data.get("tenant_id", ""))
    logger.info(
        "update_clustering_job_activity started",
        extra=_log_extra(
            tenant_id=tenant_id_probe,
            event="update_clustering_job_activity started",
            job_id=str(data.get("job_id", "")),
        ),
    )
    try:
        tenant_id = str(data.get("tenant_id", ""))
        job_id_str = str(data.get("job_id", ""))
        if not tenant_id or not job_id_str:
            return GenericActivityResult(success=False, error="tenant_id and job_id required")
        try:
            job_id = _UUID(job_id_str)
        except ValueError:
            return GenericActivityResult(success=False, error="job_id invalid")
        ctx = _get_ctx()
        repo = getattr(ctx, "background_job_repo", None)
        if repo is None:
            return GenericActivityResult(success=False, error="background job repo unavailable")
        job = await repo.get_by_id(tenant_id, job_id)
        if job is None:
            return GenericActivityResult(success=False, error="background job not found")
        updates: dict[str, Any] = {"version": job.version + 1}
        status_raw = str(data.get("status", "") or "")
        if status_raw:
            try:
                updates["status"] = BackgroundJobStatus(status_raw)
            except ValueError:
                return GenericActivityResult(
                    success=False, error=f"unknown job status {status_raw!r}"
                )
        stage_raw = data.get("stage")
        if isinstance(stage_raw, dict) and stage_raw.get("name"):
            stages = list(job.stages)
            stages.append(
                BackgroundJobStage(
                    seq=len(stages),
                    name=str(stage_raw["name"])[:128],
                    message=str(stage_raw.get("message", ""))[:1024],
                )
            )
            updates["stages"] = stages[-50:]
        if data.get("error") is not None:
            updates["error"] = str(data["error"])[:2048]
        if isinstance(data.get("progress"), int):
            updates["progress"] = max(0, data["progress"])
        if isinstance(data.get("total"), int):
            updates["total"] = max(0, data["total"])
        updated = job.model_copy(update=updates)
        await repo.save(tenant_id, updated, expected_version=job.version)
        from investigation_agent_platform.infrastructure.messaging.job_fanout import (
            maybe_publish_job_progress,
        )

        await maybe_publish_job_progress(ctx, tenant_id, updated)
        logger.info(
            "update_clustering_job_activity completed",
            extra=_log_extra(
                tenant_id=tenant_id,
                event="update_clustering_job_activity completed",
                outcome="job_updated",
                job_id=job_id_str,
                status=updated.status.value,
            ),
        )
        return GenericActivityResult(success=True, data={"status": updated.status.value})
    except ApplicationFailure:
        raise
    except Exception as exc:
        logger.exception(
            "update_clustering_job_activity failed",
            extra=_log_extra(
                tenant_id=tenant_id_probe,
                event="update_clustering_job_activity failed",
                error=str(exc),
                error_type=type(exc).__name__,
                job_id=str(data.get("job_id", "")),
            ),
        )
        raise _application_failure_from_exc(exc) from exc


@dataclass
class RunReportInput:
    tenant_id: str
    job_id: str = ""
    legal_hold: bool = False


@dataclass
class UpdateReportJobInput:
    tenant_id: str
    job_id: str
    status: str = ""
    stage: dict[str, Any] | None = None
    error: str | None = None
    progress: int | None = None
    total: int | None = None


@activity.defn
async def run_report_activity(
    params: RunReportInput,
) -> GenericActivityResult:
    """Build, render, and store one aggregate report (Part 11.8)."""
    from uuid import UUID as _UUID

    from investigation_agent_platform.application.reporting.aggregate_service import (
        InvestigationAggregateReportService,
    )
    from investigation_agent_platform.application.reporting.artifacts import (
        store_report_run,
    )
    from investigation_agent_platform.infrastructure.reporting.html_renderer import (
        HtmlAggregateReportRenderer,
    )

    data = _coerced_data(RunReportInput, params, activity="run_report_activity")
    tenant_id_probe = str(data.get("tenant_id", ""))
    logger.info(
        "run_report_activity started",
        extra=_log_extra(
            tenant_id=tenant_id_probe,
            event="run_report_activity started",
            job_id=str(data.get("job_id", "")),
        ),
    )
    try:
        tenant_id = str(data.get("tenant_id", ""))
        job_id_str = str(data.get("job_id", ""))
        if not tenant_id:
            return GenericActivityResult(success=False, error="tenant_id required")
        try:
            job_id = _UUID(job_id_str) if job_id_str else None
        except ValueError:
            return GenericActivityResult(success=False, error="job_id invalid")
        legal_hold = bool(data.get("legal_hold", False))
        ctx = _get_ctx()
        cluster_repo = getattr(ctx, "finding_cluster_repo", None)
        store = getattr(ctx, "artifact_store", None)
        if cluster_repo is None or store is None:
            return GenericActivityResult(
                success=False, error="cluster repository or artifact store unavailable"
            )
        service = InvestigationAggregateReportService(
            cluster_repo=cluster_repo,
            finding_repo=getattr(ctx, "finding_repo", None),
        )
        report = await service.build(tenant_id, job_id=job_id)
        html = HtmlAggregateReportRenderer().render(report)
        manifest_json = report.model_dump_json(indent=2).encode("utf-8")
        if job_id is None:
            return GenericActivityResult(
                success=False, error="job_id required for artifact history"
            )
        # TO_DO-011: idempotency-wired — (job_id) is deterministic per report
        # run, so a retried run replays stored artifact keys instead of
        # writing duplicate history artifacts.
        _idem_active_key: str | None = None
        _idem_operation = "activity:run_report"
        _idem_key = f"run_report:{job_id}"
        _replayed, _idem_active_key = await _reserve_or_replay(
            ctx=ctx,
            tenant_id=tenant_id,
            key=_idem_key,
            request_payload={
                "tenant_id": tenant_id,
                "job_id": job_id_str,
                "legal_hold": legal_hold,
            },
            operation=_idem_operation,
            activity="run_report_activity",
        )
        if _replayed is not None:
            return GenericActivityResult(
                success=bool(_replayed.get("success", True)),
                data=dict(_replayed.get("data") or {}),
                error=_replayed.get("error"),
            )
        refs = await store_report_run(
            store, tenant_id, job_id, html, manifest_json, legal_hold=legal_hold
        )
        await _link_report_result(ctx, tenant_id, job_id, refs["history_html"].key)
        if _idem_active_key is not None:
            await _complete_reservation(
                ctx=ctx,
                tenant_id=tenant_id,
                key=_idem_active_key,
                operation=_idem_operation,
                response={
                    "success": True,
                    "data": {
                        "taxonomy_revision": report.taxonomy_revision,
                        "assignment_count": report.assignment_count,
                        "artifact_keys": [refs["history_html"].key, refs["history_json"].key],
                    },
                },
                activity="run_report_activity",
            )
        logger.info(
            "run_report_activity completed",
            extra=_log_extra(
                tenant_id=tenant_id,
                event="run_report_activity completed",
                outcome="reported",
                job_id=job_id_str,
                assignment_count=report.assignment_count,
            ),
        )
        return GenericActivityResult(
            success=True,
            data={
                "taxonomy_revision": report.taxonomy_revision,
                "assignment_count": report.assignment_count,
                "artifact_keys": [refs["history_html"].key, refs["history_json"].key],
            },
        )
    except ApplicationFailure:
        await _abandon_reservation(
            ctx=locals().get("ctx"),
            tenant_id=str(locals().get("tenant_id", "") or ""),
            key=locals().get("_idem_active_key"),
            operation=str(locals().get("_idem_operation", "") or ""),
            activity="run_report_activity",
        )
        raise
    except Exception as exc:
        await _abandon_reservation(
            ctx=locals().get("ctx"),
            tenant_id=str(locals().get("tenant_id", "") or ""),
            key=locals().get("_idem_active_key"),
            operation=str(locals().get("_idem_operation", "") or ""),
            activity="run_report_activity",
        )
        logger.exception(
            "run_report_activity failed",
            extra=_log_extra(
                tenant_id=tenant_id_probe,
                event="run_report_activity failed",
                error=str(exc),
                error_type=type(exc).__name__,
                job_id=str(data.get("job_id", "")),
            ),
        )
        raise _application_failure_from_exc(exc) from exc


async def _link_report_result(ctx: Any, tenant_id: str, job_id: Any, result_ref: str) -> None:
    """Best-effort result_ref linkage; OCC races must not fail the run."""
    try:
        repo = getattr(ctx, "background_job_repo", None)
        if repo is None:
            return
        job = await repo.get_by_id(tenant_id, job_id)
        if job is None:
            return
        updated = job.model_copy(update={"result_ref": result_ref, "version": job.version + 1})
        await repo.save(tenant_id, updated, expected_version=job.version)
    except Exception as exc:
        logger.exception(
            "Report result_ref linkage skipped",
            extra=_log_extra(
                tenant_id=tenant_id,
                event="report result_ref linkage skipped",
                error=str(exc),
                error_type=type(exc).__name__,
            ),
        )


@activity.defn
async def update_report_job_activity(
    params: UpdateReportJobInput,
) -> GenericActivityResult:
    # TO_DO-011: not idempotency-wired — optimistic-concurrency versioning
    # (expected_version) IS the mechanism: a retried stale update conflicts
    # loudly instead of duplicating.
    """Apply a status/stage/error update to the linked aggregate-report job."""
    from uuid import UUID as _UUID

    from investigation_agent_platform.domain.common.background_job import (
        BackgroundJobStage,
        BackgroundJobStatus,
    )

    data = _coerced_data(UpdateReportJobInput, params, activity="update_report_job_activity")
    tenant_id_probe = str(data.get("tenant_id", ""))
    logger.info(
        "update_report_job_activity started",
        extra=_log_extra(
            tenant_id=tenant_id_probe,
            event="update_report_job_activity started",
            job_id=str(data.get("job_id", "")),
        ),
    )
    try:
        tenant_id = str(data.get("tenant_id", ""))
        job_id_str = str(data.get("job_id", ""))
        if not tenant_id or not job_id_str:
            return GenericActivityResult(success=False, error="tenant_id and job_id required")
        try:
            job_id = _UUID(job_id_str)
        except ValueError:
            return GenericActivityResult(success=False, error="job_id invalid")
        ctx = _get_ctx()
        repo = getattr(ctx, "background_job_repo", None)
        if repo is None:
            return GenericActivityResult(success=False, error="background job repo unavailable")
        job = await repo.get_by_id(tenant_id, job_id)
        if job is None:
            return GenericActivityResult(success=False, error="background job not found")
        updates: dict[str, Any] = {"version": job.version + 1}
        status_raw = str(data.get("status", "") or "")
        if status_raw:
            try:
                updates["status"] = BackgroundJobStatus(status_raw)
            except ValueError:
                return GenericActivityResult(
                    success=False, error=f"unknown job status {status_raw!r}"
                )
        stage_raw = data.get("stage")
        if isinstance(stage_raw, dict) and stage_raw.get("name"):
            stages = list(job.stages)
            stages.append(
                BackgroundJobStage(
                    seq=len(stages),
                    name=str(stage_raw["name"])[:128],
                    message=str(stage_raw.get("message", ""))[:1024],
                )
            )
            updates["stages"] = stages[-50:]
        if data.get("error") is not None:
            updates["error"] = str(data["error"])[:2048]
        if isinstance(data.get("progress"), int):
            updates["progress"] = max(0, data["progress"])
        if isinstance(data.get("total"), int):
            updates["total"] = max(0, data["total"])
        updated = job.model_copy(update=updates)
        await repo.save(tenant_id, updated, expected_version=job.version)
        from investigation_agent_platform.infrastructure.messaging.job_fanout import (
            maybe_publish_job_progress,
        )

        await maybe_publish_job_progress(ctx, tenant_id, updated)
        logger.info(
            "update_report_job_activity completed",
            extra=_log_extra(
                tenant_id=tenant_id,
                event="update_report_job_activity completed",
                outcome="job_updated",
                job_id=job_id_str,
                status=updated.status.value,
            ),
        )
        return GenericActivityResult(success=True, data={"status": updated.status.value})
    except ApplicationFailure:
        raise
    except Exception as exc:
        logger.exception(
            "update_report_job_activity failed",
            extra=_log_extra(
                tenant_id=tenant_id_probe,
                event="update_report_job_activity failed",
                error=str(exc),
                error_type=type(exc).__name__,
                job_id=str(data.get("job_id", "")),
            ),
        )
        raise _application_failure_from_exc(exc) from exc


@dataclass
class RunReindexInput:
    tenant_id: str
    job_id: str = ""
    source_id: str = ""
    application_id: str = ""


@dataclass
class UpdateReindexJobInput:
    tenant_id: str
    job_id: str
    status: str = ""
    stage: dict[str, Any] | None = None
    error: str | None = None
    progress: int | None = None
    total: int | None = None


@activity.defn
async def run_reindex_activity(
    params: RunReindexInput,
) -> GenericActivityResult:
    # TO_DO-011: not idempotency-wired — index_source writes a new generation
    # per call and is re-runnable (a retry supersedes rather than duplicates);
    # generation-scoped dedup lives in the reference service.
    """Index one reference source into a new generation (Part 11.6)."""
    from investigation_agent_platform.application.reference.reference_service import (
        ReferenceDocumentService,
    )

    data = _coerced_data(RunReindexInput, params, activity="run_reindex_activity")
    tenant_id_probe = str(data.get("tenant_id", ""))
    logger.info(
        "run_reindex_activity started",
        extra=_log_extra(
            tenant_id=tenant_id_probe,
            event="run_reindex_activity started",
            source_id=str(data.get("source_id", "")),
        ),
    )
    try:
        tenant_id = str(data.get("tenant_id", ""))
        source_id = str(data.get("source_id", ""))
        if not tenant_id or not source_id:
            return GenericActivityResult(success=False, error="tenant_id and source_id required")
        ctx = _get_ctx()
        chunk_repo = getattr(ctx, "reference_repo", None)
        if chunk_repo is None:
            return GenericActivityResult(success=False, error="reference repository unavailable")
        service = ReferenceDocumentService(
            chunk_repo=chunk_repo,
            profile_repo=getattr(ctx, "profile_repo", None),
            embedder=None,
        )
        try:
            summary = await service.index_source(
                tenant_id, source_id, data.get("application_id") or None
            )
        except (RuntimeError, ValueError) as exc:
            return GenericActivityResult(success=False, error=str(exc))
        logger.info(
            "run_reindex_activity completed",
            extra=_log_extra(
                tenant_id=tenant_id,
                event="run_reindex_activity completed",
                outcome="indexed",
                source_id=source_id,
            ),
        )
        return GenericActivityResult(success=True, data=summary.model_dump(mode="json"))
    except ApplicationFailure:
        raise
    except Exception as exc:
        logger.exception(
            "run_reindex_activity failed",
            extra=_log_extra(
                tenant_id=tenant_id_probe,
                event="run_reindex_activity failed",
                error=str(exc),
                error_type=type(exc).__name__,
                source_id=str(data.get("source_id", "")),
            ),
        )
        raise _application_failure_from_exc(exc) from exc


@activity.defn
async def update_reindex_job_activity(
    params: UpdateReindexJobInput,
) -> GenericActivityResult:
    # TO_DO-011: not idempotency-wired — optimistic-concurrency versioning
    # (expected_version) IS the mechanism: a retried stale update conflicts
    # loudly instead of duplicating.
    """Apply a status/stage/error update to the linked reindex job."""
    from uuid import UUID as _UUID

    from investigation_agent_platform.domain.common.background_job import (
        BackgroundJobStage,
        BackgroundJobStatus,
    )

    data = _coerced_data(UpdateReindexJobInput, params, activity="update_reindex_job_activity")
    tenant_id_probe = str(data.get("tenant_id", ""))
    logger.info(
        "update_reindex_job_activity started",
        extra=_log_extra(
            tenant_id=tenant_id_probe,
            event="update_reindex_job_activity started",
            job_id=str(data.get("job_id", "")),
        ),
    )
    try:
        tenant_id = str(data.get("tenant_id", ""))
        job_id_str = str(data.get("job_id", ""))
        if not tenant_id or not job_id_str:
            return GenericActivityResult(success=False, error="tenant_id and job_id required")
        try:
            job_id = _UUID(job_id_str)
        except ValueError:
            return GenericActivityResult(success=False, error="job_id invalid")
        ctx = _get_ctx()
        repo = getattr(ctx, "background_job_repo", None)
        if repo is None:
            return GenericActivityResult(success=False, error="background job repo unavailable")
        job = await repo.get_by_id(tenant_id, job_id)
        if job is None:
            return GenericActivityResult(success=False, error="background job not found")
        updates: dict[str, Any] = {"version": job.version + 1}
        status_raw = str(data.get("status", "") or "")
        if status_raw:
            try:
                updates["status"] = BackgroundJobStatus(status_raw)
            except ValueError:
                return GenericActivityResult(
                    success=False, error=f"unknown job status {status_raw!r}"
                )
        stage_raw = data.get("stage")
        if isinstance(stage_raw, dict) and stage_raw.get("name"):
            stages = list(job.stages)
            stages.append(
                BackgroundJobStage(
                    seq=len(stages),
                    name=str(stage_raw["name"])[:128],
                    message=str(stage_raw.get("message", ""))[:1024],
                )
            )
            updates["stages"] = stages[-50:]
        if data.get("error") is not None:
            updates["error"] = str(data["error"])[:2048]
        if isinstance(data.get("progress"), int):
            updates["progress"] = max(0, data["progress"])
        if isinstance(data.get("total"), int):
            updates["total"] = max(0, data["total"])
        updated = job.model_copy(update=updates)
        await repo.save(tenant_id, updated, expected_version=job.version)
        from investigation_agent_platform.infrastructure.messaging.job_fanout import (
            maybe_publish_job_progress,
        )

        await maybe_publish_job_progress(ctx, tenant_id, updated)
        logger.info(
            "update_reindex_job_activity completed",
            extra=_log_extra(
                tenant_id=tenant_id,
                event="update_reindex_job_activity completed",
                outcome="job_updated",
                job_id=job_id_str,
                status=updated.status.value,
            ),
        )
        return GenericActivityResult(success=True, data={"status": updated.status.value})
    except ApplicationFailure:
        raise
    except Exception as exc:
        logger.exception(
            "update_reindex_job_activity failed",
            extra=_log_extra(
                tenant_id=tenant_id_probe,
                event="update_reindex_job_activity failed",
                error=str(exc),
                error_type=type(exc).__name__,
                job_id=str(data.get("job_id", "")),
            ),
        )
        raise _application_failure_from_exc(exc) from exc


@dataclass
class LoadBatchRecordsInput:
    tenant_id: str
    job_id: str = ""
    start_index: int = 0


@dataclass
class MarkBatchRecordsInput:
    tenant_id: str
    job_id: str = ""
    record_indices: list[int] = None  # type: ignore[assignment]
    status: str = ""
    child_workflow_ids: dict[str, str] | None = None


@dataclass
class MarkBatchRecordResultInput:
    tenant_id: str
    job_id: str = ""
    record_index: int = 0
    status: str = ""
    error: str | None = None


@dataclass
class ResolveBatchStragglersInput:
    tenant_id: str
    job_id: str = ""
    record_indices: list[int] = None  # type: ignore[assignment]


@dataclass
class CancelBatchChildrenInput:
    tenant_id: str
    job_id: str = ""


@dataclass
class UpdateBatchJobInput:
    tenant_id: str
    job_id: str
    status: str = ""
    stage: dict[str, Any] | None = None
    error: str | None = None
    progress: int | None = None
    total: int | None = None


def _batch_uuid(value: str) -> Any:
    from uuid import UUID as _UUID

    try:
        return _UUID(value)
    except ValueError:
        return None


@activity.defn
async def load_batch_records_activity(
    params: LoadBatchRecordsInput,
) -> GenericActivityResult:
    # TO_DO-011: not idempotency-wired — read-only cursor load; no writes.
    """Load PENDING batch records from a dispatch cursor (Part 11.9)."""
    data = _coerced_data(LoadBatchRecordsInput, params, activity="load_batch_records_activity")
    tenant_id_probe = str(data.get("tenant_id", ""))
    logger.info(
        "load_batch_records_activity started",
        extra=_log_extra(
            tenant_id=tenant_id_probe,
            event="load_batch_records_activity started",
            job_id=str(data.get("job_id", "")),
        ),
    )
    try:
        tenant_id = str(data.get("tenant_id", ""))
        job_id = _batch_uuid(str(data.get("job_id", "")))
        if not tenant_id or job_id is None:
            return GenericActivityResult(success=False, error="tenant_id and job_id required")
        try:
            start_index = max(0, int(data.get("start_index", 0)))
        except (TypeError, ValueError):
            return GenericActivityResult(success=False, error="start_index invalid")
        ctx = _get_ctx()
        repo = getattr(ctx, "batch_repo", None)
        if repo is None:
            return GenericActivityResult(success=False, error="batch repository unavailable")
        records, total = await repo.list_records(tenant_id, job_id, limit=500, offset=0)
        pending = [
            {
                "record_index": record.record_index,
                "investigation_id": str(record.investigation_id)
                if record.investigation_id
                else None,
                "application_id": record.application_id,
                "external_key": record.external_key,
                "attempt": record.attempt,
            }
            for record in records
            if record.record_index >= start_index and record.status.value == "PENDING"
        ]
        logger.info(
            "load_batch_records_activity completed",
            extra=_log_extra(
                tenant_id=tenant_id,
                event="load_batch_records_activity completed",
                outcome="loaded",
                pending_count=len(pending),
                total=total,
            ),
        )
        return GenericActivityResult(success=True, data={"records": pending, "total": total})
    except ApplicationFailure:
        raise
    except Exception as exc:
        logger.exception(
            "load_batch_records_activity failed",
            extra=_log_extra(
                tenant_id=tenant_id_probe,
                event="load_batch_records_activity failed",
                error=str(exc),
                error_type=type(exc).__name__,
                job_id=str(data.get("job_id", "")),
            ),
        )
        raise _application_failure_from_exc(exc) from exc


@activity.defn
async def mark_batch_records_activity(
    params: MarkBatchRecordsInput,
) -> GenericActivityResult:
    # TO_DO-011: not idempotency-wired — convergent status sets: re-marking
    # the same records with the same status/child ids is semantically a
    # no-op, so retries converge instead of duplicating.
    """Bulk-mark records DISPATCHED/RUNNING with child workflow ids."""
    from investigation_agent_platform.domain.intake.batch import BatchRecordStatus

    data = _coerced_data(MarkBatchRecordsInput, params, activity="mark_batch_records_activity")
    tenant_id_probe = str(data.get("tenant_id", ""))
    logger.info(
        "mark_batch_records_activity started",
        extra=_log_extra(
            tenant_id=tenant_id_probe,
            event="mark_batch_records_activity started",
            job_id=str(data.get("job_id", "")),
        ),
    )
    try:
        tenant_id = str(data.get("tenant_id", ""))
        job_id = _batch_uuid(str(data.get("job_id", "")))
        if not tenant_id or job_id is None:
            return GenericActivityResult(success=False, error="tenant_id and job_id required")
        try:
            status = BatchRecordStatus(str(data.get("status", "") or ""))
        except ValueError:
            return GenericActivityResult(success=False, error="unknown record status")
        indices = data.get("record_indices") or []
        child_ids = data.get("child_workflow_ids") or {}
        ctx = _get_ctx()
        repo = getattr(ctx, "batch_repo", None)
        if repo is None:
            return GenericActivityResult(success=False, error="batch repository unavailable")
        marked = 0
        for raw_index in indices:
            record = await repo.get_record(tenant_id, job_id, int(raw_index))
            if record is None:
                continue
            child_id = child_ids.get(str(raw_index))
            await repo.save_record(
                tenant_id,
                job_id,
                record.model_copy(update={"status": status, "child_workflow_id": child_id}),
            )
            marked += 1
        logger.info(
            "mark_batch_records_activity completed",
            extra=_log_extra(
                tenant_id=tenant_id,
                event="mark_batch_records_activity completed",
                outcome="marked",
                marked=marked,
            ),
        )
        return GenericActivityResult(success=True, data={"marked": marked})
    except ApplicationFailure:
        raise
    except Exception as exc:
        logger.exception(
            "mark_batch_records_activity failed",
            extra=_log_extra(
                tenant_id=tenant_id_probe,
                event="mark_batch_records_activity failed",
                error=str(exc),
                error_type=type(exc).__name__,
                job_id=str(data.get("job_id", "")),
            ),
        )
        raise _application_failure_from_exc(exc) from exc


@activity.defn
async def mark_batch_record_result_activity(
    params: MarkBatchRecordResultInput,
) -> GenericActivityResult:
    # TO_DO-011: not idempotency-wired — terminal marking is convergent
    # (same status/error rewrite is a no-op); guarded to terminal states.
    """Mark one record terminal (DONE/FAILED/CANCELED) with an error."""
    from investigation_agent_platform.domain.intake.batch import BatchRecordStatus

    data = _coerced_data(
        MarkBatchRecordResultInput, params, activity="mark_batch_record_result_activity"
    )
    tenant_id_probe = str(data.get("tenant_id", ""))
    logger.info(
        "mark_batch_record_result_activity started",
        extra=_log_extra(
            tenant_id=tenant_id_probe,
            event="mark_batch_record_result_activity started",
            job_id=str(data.get("job_id", "")),
        ),
    )
    try:
        tenant_id = str(data.get("tenant_id", ""))
        job_id = _batch_uuid(str(data.get("job_id", "")))
        if not tenant_id or job_id is None:
            return GenericActivityResult(success=False, error="tenant_id and job_id required")
        try:
            index = int(data.get("record_index", -1))
            status = BatchRecordStatus(str(data.get("status", "") or ""))
        except (TypeError, ValueError):
            return GenericActivityResult(success=False, error="record result invalid")
        if status not in (
            BatchRecordStatus.DONE,
            BatchRecordStatus.FAILED,
            BatchRecordStatus.CANCELED,
        ):
            return GenericActivityResult(success=False, error="status must be terminal")
        ctx = _get_ctx()
        repo = getattr(ctx, "batch_repo", None)
        if repo is None:
            return GenericActivityResult(success=False, error="batch repository unavailable")
        record = await repo.get_record(tenant_id, job_id, index)
        if record is None:
            return GenericActivityResult(success=False, error="batch record not found")
        error = data.get("error")
        await repo.save_record(
            tenant_id,
            job_id,
            record.model_copy(
                update={"status": status, "error": str(error)[:2048] if error else None}
            ),
        )
        logger.info(
            "mark_batch_record_result_activity completed",
            extra=_log_extra(
                tenant_id=tenant_id,
                event="mark_batch_record_result_activity completed",
                outcome="marked",
                status=status.value,
            ),
        )
        return GenericActivityResult(success=True, data={"status": status.value})
    except ApplicationFailure:
        raise
    except Exception as exc:
        logger.exception(
            "mark_batch_record_result_activity failed",
            extra=_log_extra(
                tenant_id=tenant_id_probe,
                event="mark_batch_record_result_activity failed",
                error=str(exc),
                error_type=type(exc).__name__,
                job_id=str(data.get("job_id", "")),
            ),
        )
        raise _application_failure_from_exc(exc) from exc


@activity.defn
async def resolve_batch_stragglers_activity(
    params: ResolveBatchStragglersInput,
) -> GenericActivityResult:
    # TO_DO-011: not idempotency-wired — classification from persisted
    # investigation status is convergent (re-running re-derives the same
    # AWAITING_INPUT/RUNNING labels); never invents terminal states.
    """Classify still-running records via the persisted investigation status.

    AWAITING_INPUT investigations keep running (parent completes partial);
    anything else still active stays RUNNING (also left running). Never
    invents terminal states for live executions.
    """
    from investigation_agent_platform.domain.intake.batch import BatchRecordStatus

    data = _coerced_data(
        ResolveBatchStragglersInput, params, activity="resolve_batch_stragglers_activity"
    )
    tenant_id_probe = str(data.get("tenant_id", ""))
    logger.info(
        "resolve_batch_stragglers_activity started",
        extra=_log_extra(
            tenant_id=tenant_id_probe,
            event="resolve_batch_stragglers_activity started",
            job_id=str(data.get("job_id", "")),
        ),
    )
    try:
        tenant_id = str(data.get("tenant_id", ""))
        job_id = _batch_uuid(str(data.get("job_id", "")))
        if not tenant_id or job_id is None:
            return GenericActivityResult(success=False, error="tenant_id and job_id required")
        indices = [int(index) for index in (data.get("record_indices") or [])]
        ctx = _get_ctx()
        repo = getattr(ctx, "batch_repo", None)
        investigation_repo = getattr(ctx, "investigation_repo", None)
        if repo is None or investigation_repo is None:
            return GenericActivityResult(success=False, error="repositories unavailable")
        awaiting = 0
        running = 0
        for index in indices:
            record = await repo.get_record(tenant_id, job_id, index)
            if record is None or record.investigation_id is None:
                continue
            investigation = await investigation_repo.get_by_id(tenant_id, record.investigation_id)
            status_value = str(getattr(getattr(investigation, "status", None), "value", ""))
            if status_value == "AWAITING_INPUT":
                await repo.save_record(
                    tenant_id,
                    job_id,
                    record.model_copy(update={"status": BatchRecordStatus.AWAITING_INPUT}),
                )
                awaiting += 1
            else:
                await repo.save_record(
                    tenant_id,
                    job_id,
                    record.model_copy(update={"status": BatchRecordStatus.RUNNING}),
                )
                running += 1
        logger.info(
            "resolve_batch_stragglers_activity completed",
            extra=_log_extra(
                tenant_id=tenant_id,
                event="resolve_batch_stragglers_activity completed",
                outcome="resolved",
                awaiting_input=awaiting,
                running=running,
            ),
        )
        return GenericActivityResult(
            success=True, data={"awaiting_input": awaiting, "running": running}
        )
    except ApplicationFailure:
        raise
    except Exception as exc:
        logger.exception(
            "resolve_batch_stragglers_activity failed",
            extra=_log_extra(
                tenant_id=tenant_id_probe,
                event="resolve_batch_stragglers_activity failed",
                error=str(exc),
                error_type=type(exc).__name__,
                job_id=str(data.get("job_id", "")),
            ),
        )
        raise _application_failure_from_exc(exc) from exc


@activity.defn
async def cancel_batch_children_activity(
    params: CancelBatchChildrenInput,
) -> GenericActivityResult:
    # TO_DO-011: not idempotency-wired — Temporal workflow cancel is
    # idempotent per child handle and each outcome is recorded on the batch
    # row, so retries converge (already-canceled children are skipped by
    # their CANCELED status filter).
    """Cancel live child executions for RUNNING records (Part 11.9).

    Explicit propagation: the declared parent-close policy is ABANDON (so
    awaiting-input children survive parent completion), therefore parent
    cancellation fans out here — one client cancel per live child, each
    outcome recorded. Failures cancel auditably, never silently.
    """
    from investigation_agent_platform.domain.intake.batch import BatchRecordStatus

    data = _coerced_data(
        CancelBatchChildrenInput, params, activity="cancel_batch_children_activity"
    )
    tenant_id_probe = str(data.get("tenant_id", ""))
    logger.info(
        "cancel_batch_children_activity started",
        extra=_log_extra(
            tenant_id=tenant_id_probe,
            event="cancel_batch_children_activity started",
            job_id=str(data.get("job_id", "")),
        ),
    )
    try:
        tenant_id = str(data.get("tenant_id", ""))
        job_id = _batch_uuid(str(data.get("job_id", "")))
        if not tenant_id or job_id is None:
            return GenericActivityResult(success=False, error="tenant_id and job_id required")
        ctx = _get_ctx()
        repo = getattr(ctx, "batch_repo", None)
        client = getattr(ctx, "temporal_client", None)
        if repo is None:
            return GenericActivityResult(success=False, error="batch repository unavailable")
        if client is None:
            return GenericActivityResult(success=False, error="temporal client unavailable")
        records, _ = await repo.list_records(tenant_id, job_id, limit=500, offset=0)
        canceled = 0
        failed: list[str] = []
        for record in records:
            if record.status not in (BatchRecordStatus.RUNNING, BatchRecordStatus.DISPATCHED):
                continue
            if not record.child_workflow_id:
                continue
            try:
                handle = client.get_workflow_handle(record.child_workflow_id)
                await handle.cancel()
                await repo.save_record(
                    tenant_id,
                    job_id,
                    record.model_copy(update={"status": BatchRecordStatus.CANCELED}),
                )
                canceled += 1
            except Exception as exc:
                logger.exception(
                    "cancel_batch_children_activity child cancel failed",
                    extra=_log_extra(
                        tenant_id=tenant_id,
                        event="cancel_batch_children_activity child cancel failed",
                        error=str(exc),
                        error_type=type(exc).__name__,
                        record_index=record.record_index,
                    ),
                )
                failed.append(f"{record.record_index}:{exc}")
        logger.info(
            "cancel_batch_children_activity completed",
            extra=_log_extra(
                tenant_id=tenant_id,
                event="cancel_batch_children_activity completed",
                outcome="canceled",
                canceled=canceled,
                failed_count=len(failed),
            ),
        )
        return GenericActivityResult(success=True, data={"canceled": canceled, "failed": failed})
    except ApplicationFailure:
        raise
    except Exception as exc:
        logger.exception(
            "cancel_batch_children_activity failed",
            extra=_log_extra(
                tenant_id=tenant_id_probe,
                event="cancel_batch_children_activity failed",
                error=str(exc),
                error_type=type(exc).__name__,
                job_id=str(data.get("job_id", "")),
            ),
        )
        raise _application_failure_from_exc(exc) from exc


@activity.defn
async def update_batch_job_activity(
    params: UpdateBatchJobInput,
) -> GenericActivityResult:
    # TO_DO-011: not idempotency-wired — optimistic-concurrency versioning
    # (expected_version) IS the mechanism: a retried stale update conflicts
    # loudly instead of duplicating.
    """Apply a status/stage/error update to the linked batch job."""
    from uuid import UUID as _UUID

    from investigation_agent_platform.domain.common.background_job import (
        BackgroundJobStage,
        BackgroundJobStatus,
    )

    data = _coerced_data(UpdateBatchJobInput, params, activity="update_batch_job_activity")
    tenant_id_probe = str(data.get("tenant_id", ""))
    logger.info(
        "update_batch_job_activity started",
        extra=_log_extra(
            tenant_id=tenant_id_probe,
            event="update_batch_job_activity started",
            job_id=str(data.get("job_id", "")),
        ),
    )
    try:
        tenant_id = str(data.get("tenant_id", ""))
        job_id_str = str(data.get("job_id", ""))
        if not tenant_id or not job_id_str:
            return GenericActivityResult(success=False, error="tenant_id and job_id required")
        try:
            job_id = _UUID(job_id_str)
        except ValueError:
            return GenericActivityResult(success=False, error="job_id invalid")
        ctx = _get_ctx()
        repo = getattr(ctx, "background_job_repo", None)
        if repo is None:
            return GenericActivityResult(success=False, error="background job repo unavailable")
        job = await repo.get_by_id(tenant_id, job_id)
        if job is None:
            return GenericActivityResult(success=False, error="background job not found")
        updates: dict[str, Any] = {"version": job.version + 1}
        status_raw = str(data.get("status", "") or "")
        if status_raw:
            try:
                updates["status"] = BackgroundJobStatus(status_raw)
            except ValueError:
                return GenericActivityResult(
                    success=False, error=f"unknown job status {status_raw!r}"
                )
        stage_raw = data.get("stage")
        if isinstance(stage_raw, dict) and stage_raw.get("name"):
            stages = list(job.stages)
            stages.append(
                BackgroundJobStage(
                    seq=len(stages),
                    name=str(stage_raw["name"])[:128],
                    message=str(stage_raw.get("message", ""))[:1024],
                )
            )
            updates["stages"] = stages[-50:]
        if data.get("error") is not None:
            updates["error"] = str(data["error"])[:2048]
        if isinstance(data.get("progress"), int):
            updates["progress"] = max(0, data["progress"])
        if isinstance(data.get("total"), int):
            updates["total"] = max(0, data["total"])
        updated = job.model_copy(update=updates)
        await repo.save(tenant_id, updated, expected_version=job.version)
        from investigation_agent_platform.infrastructure.messaging.job_fanout import (
            maybe_publish_job_progress,
        )

        await maybe_publish_job_progress(ctx, tenant_id, updated)
        logger.info(
            "update_batch_job_activity completed",
            extra=_log_extra(
                tenant_id=tenant_id,
                event="update_batch_job_activity completed",
                outcome="job_updated",
                job_id=job_id_str,
                status=updated.status.value,
            ),
        )
        return GenericActivityResult(success=True, data={"status": updated.status.value})
    except ApplicationFailure:
        raise
    except Exception as exc:
        logger.exception(
            "update_batch_job_activity failed",
            extra=_log_extra(
                tenant_id=tenant_id_probe,
                event="update_batch_job_activity failed",
                error=str(exc),
                error_type=type(exc).__name__,
                job_id=str(data.get("job_id", "")),
            ),
        )
        raise _application_failure_from_exc(exc) from exc


@dataclass
class SummarizeBatchInput:
    tenant_id: str
    job_id: str = ""


@activity.defn
async def summarize_batch_activity(
    params: SummarizeBatchInput,
) -> GenericActivityResult:
    # TO_DO-011: not idempotency-wired — pure aggregation over record rows;
    # no writes.
    """Aggregate batch counts over record rows (Part 11.9)."""
    data = _coerced_data(SummarizeBatchInput, params, activity="summarize_batch_activity")
    tenant_id_probe = str(data.get("tenant_id", ""))
    logger.info(
        "summarize_batch_activity started",
        extra=_log_extra(
            tenant_id=tenant_id_probe,
            event="summarize_batch_activity started",
            job_id=str(data.get("job_id", "")),
        ),
    )
    try:
        tenant_id = str(data.get("tenant_id", ""))
        job_id = _batch_uuid(str(data.get("job_id", "")))
        if not tenant_id or job_id is None:
            return GenericActivityResult(success=False, error="tenant_id and job_id required")
        ctx = _get_ctx()
        repo = getattr(ctx, "batch_repo", None)
        if repo is None:
            return GenericActivityResult(success=False, error="batch repository unavailable")
        records, _ = await repo.list_records(tenant_id, job_id, limit=500, offset=0)
        counts = {
            "total": len(records),
            "succeeded": 0,
            "failed": 0,
            "awaiting_input": 0,
            "canceled": 0,
        }
        for record in records:
            status = record.status.value
            if status == "DONE":
                counts["succeeded"] += 1
            elif status == "FAILED":
                counts["failed"] += 1
            elif status == "AWAITING_INPUT":
                counts["awaiting_input"] += 1
            elif status == "CANCELED":
                counts["canceled"] += 1
        logger.info(
            "summarize_batch_activity completed",
            extra=_log_extra(
                tenant_id=tenant_id,
                event="summarize_batch_activity completed",
                outcome="summarized",
                total=counts["total"],
            ),
        )
        return GenericActivityResult(success=True, data=counts)
    except ApplicationFailure:
        raise
    except Exception as exc:
        logger.exception(
            "summarize_batch_activity failed",
            extra=_log_extra(
                tenant_id=tenant_id_probe,
                event="summarize_batch_activity failed",
                error=str(exc),
                error_type=type(exc).__name__,
                job_id=str(data.get("job_id", "")),
            ),
        )
        raise _application_failure_from_exc(exc) from exc


@dataclass
class ProjectTopologyInput:
    """One topology projection pass over a local checkout (cognee pilot).

    `checkout` is resolved under `IAP_CODE_REPO_BASE` with containment
    enforcement; absolute/traversal paths are rejected. `revision` defaults
    to the checkout's HEAD when empty. `changed_files` (from webhook pushes)
    is recorded for future incremental work; the payload rebuild stays full
    (edge resolution spans files — partial projection would corrupt CALLS).
    """

    tenant_id: str = ""
    repository_id: str = ""
    revision: str = ""
    checkout: str = ""
    application_id: str = ""
    changed_files: list[str] = field(default_factory=list)


@activity.defn
@traced("worker", outcome_from=_activity_outcome, extra_from=_activity_extra)
async def project_topology_activity(
    params: ProjectTopologyInput,
) -> GenericActivityResult:
    """Project one repo revision into the topology store (scheduled janitor shape).

    Skips (success, status=skipped) when the pilot is disabled, the checkout
    is absent/unresolvable, or config is missing — a janitor never fails a
    schedule on a missing repo. Genuine projection failures raise for
    Temporal retry.
    """
    import os

    data = _coerced_data(ProjectTopologyInput, params, activity="project_topology_activity")
    tenant_id = str(data.get("tenant_id", ""))
    repository_id = str(data.get("repository_id", ""))
    if not tenant_id or not repository_id:
        return GenericActivityResult(success=False, error="tenant_id and repository_id required")

    def _skip(reason: str) -> GenericActivityResult:
        logger.info(
            "project_topology_activity skipped",
            extra=_log_extra(
                tenant_id=tenant_id,
                event="project_topology skipped",
                reason=reason,
                repository_id=repository_id,
            ),
        )
        return GenericActivityResult(success=True, data={"status": "skipped", "reason": reason})

    if os.environ.get("IAP_COGNEE_ENABLED", "false").lower() != "true":
        return _skip("cognee pilot disabled")
    salt = os.environ.get("IAP_COGNEE_DATASET_SALT", "")
    if not salt:
        return _skip("dataset salt unconfigured")

    from pathlib import Path

    base = Path(os.environ.get("IAP_CODE_REPO_BASE", "") or "data/code-repos").resolve()
    requested = str(data.get("checkout", "") or "")
    candidates: list[Path] = []
    if requested:
        candidates.append((base / requested).resolve())
    else:
        # Layout candidates: flat slug first, then nested org/repo
        # (self-triage worktrees live nested).
        candidates.append((base / repository_id).resolve())
        if "--" in repository_id:
            org, _, name = repository_id.partition("--")
            if org and name:
                candidates.append((base / org / name).resolve())
    candidate: Path | None = None
    for path in candidates:
        try:
            inside = str(path).startswith(str(base) + os.sep) or path == base
        except Exception:
            continue
        if not inside:
            continue
        if path.is_dir():
            candidate = path
            break
    if candidate is None:
        # Distinguish escape (reject loudly in logs) from absence (skip).
        for path in candidates:
            try:
                if not (str(path).startswith(str(base) + os.sep) or path == base):
                    return _skip("checkout escapes repo base")
            except Exception:
                continue
        return _skip("checkout absent")

    revision = str(data.get("revision", "") or "")
    if not revision:
        try:
            import pygit2  # type: ignore[import-not-found]

            revision = str(pygit2.Repository(str(candidate)).head.target)
        except Exception:
            revision = ""
    if not revision:
        return _skip("revision unresolvable")

    try:
        from investigation_agent_platform.infrastructure.topology.cognee_adapter import (
            CogneeTopologyAdapter,
            SharedNeo4jDriver,
        )
        from investigation_agent_platform.infrastructure.topology.ownership import (
            derive_repository_domain,
        )
        from investigation_agent_platform.infrastructure.topology.payload import (
            build_payload_for_repo,
        )

        application_id = str(data.get("application_id", "") or f"{tenant_id}-{repository_id}")
        payload, truncated = await build_payload_for_repo(
            candidate, tenant_id, application_id, repository_id, revision
        )
        if truncated:
            return GenericActivityResult(
                success=False, error="payload truncated; refusing partial projection"
            )
        ownership_path = []
        try:
            domain = await asyncio.to_thread(derive_repository_domain, candidate)
            if domain:
                ownership_path.append(domain)
        except Exception:
            pass
        holder = await SharedNeo4jDriver.from_env().connect()
        try:
            adapter = CogneeTopologyAdapter(enabled=True, dataset_salt=salt, neo4j_driver=holder)
            result = await adapter.project_snapshot(payload, ownership_path=ownership_path)
        finally:
            await holder.close()
        changed = list(data.get("changed_files", []) or [])
        logger.info(
            "project_topology_activity completed",
            extra=_log_extra(
                tenant_id=tenant_id,
                event="project_topology completed",
                repository_id=repository_id,
                revision=revision[:12],
                dataset=result.dataset,
                points=result.point_count,
                edges=result.edge_count,
                changed_files=len(changed),
            ),
        )
        return GenericActivityResult(
            success=True,
            data={
                "status": "projected",
                "dataset": result.dataset,
                "points": result.point_count,
                "edges": result.edge_count,
                "revision": revision,
                "changed_files": changed,
            },
        )
    except ApplicationFailure:
        raise
    except Exception as exc:
        logger.exception(
            "project_topology_activity failed",
            extra=_log_extra(
                tenant_id=tenant_id,
                event="project_topology failed",
                error=str(exc),
                error_type=type(exc).__name__,
                repository_id=repository_id,
            ),
        )
        raise _application_failure_from_exc(exc) from exc
