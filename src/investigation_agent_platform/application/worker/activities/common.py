# src/investigation_agent_platform/application/worker/activities/common.py
"""Temporal activity thin adapters — each delegates to the application/domain layer via AppContext."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from typing import Any
from uuid import UUID

try:
    from temporalio.exceptions import ApplicationFailure  # type: ignore[attr-defined]
except ImportError:
    from temporalio.exceptions import (
        ApplicationError as ApplicationFailure,  # type: ignore[assignment, no-redef]
    )

logger = logging.getLogger(__name__)

from investigation_agent_platform.domain.investigation.models import ActionType
from investigation_agent_platform.infrastructure.telemetry.story import (
    COMP_ACTVTY,
    COMP_WFLW,
    OUTCOME_COMPLETED,
    OUTCOME_FAILED,
    OUTCOME_SKIPPED,
    OUTCOME_STARTED,
    elapsed_ms,
    story_event,
    story_level,
    story_message,
    tick,
)

__all__ = [
    "COMP_ACTVTY",
    "COMP_WFLW",
    "OUTCOME_COMPLETED",
    "OUTCOME_FAILED",
    "OUTCOME_SKIPPED",
    "OUTCOME_STARTED",
    "elapsed_ms",
    "story_event",
    "story_level",
    "story_log",
    "story_message",
    "tick",
]

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
        story_log(
            logger,
            logging.WARNING,
            "Log exemplar save failed open; raw banking stands",
            comp=COMP_ACTVTY,
            func="_persist_log_exemplars",
            stage="persist",
            step="save_exemplars",
            outcome=OUTCOME_SKIPPED,
            tenant_id=tenant_id,
            investigation_id=str(investigation_id),
            event="log exemplar save failed",
            error=str(exc),
        )
        return 0
    story_log(
        logger,
        logging.INFO,
        "Log exemplars persisted",
        comp=COMP_ACTVTY,
        func="_persist_log_exemplars",
        stage="persist",
        step="save_exemplars",
        outcome=OUTCOME_COMPLETED,
        tenant_id=tenant_id,
        investigation_id=str(investigation_id),
        event="log exemplars persisted",
        outputs={"groups": len(exemplars), "rows": len(banked)},
        groups=len(exemplars),
        rows=len(banked),
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
        # Phase 1 (claim binding) additions; unknown codes still coerce
        # to "other" with the signal counted, never dropped silently.
        "NO_RUNTIME_OBSERVATION",
        "RIVAL_SHARED_EVIDENCE",
        "UNMAPPED_CITATION",
        "EVAPORATED_CITATION",
        # Phase 3 (claim binding): repaired-verdict marker.
        "REPAIRED_CITATION",
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


_BINDING_KIND_LABELS = frozenset(
    {
        "DEFINITION",
        "CALL_EDGE",
        "BEHAVIOR",
        "ABSENCE",
        "HISTORY",
        "CAUSAL",
        "UNTYPED",
        "unspecified",
    }
)


def _emit_verify_telemetry(
    ctx: Any,
    tenant_id: Any,
    investigation_id: Any,
    candidate: Any,
    result: Any,
    siblings: list[Any] | None,
    binding_version: str = "cbv1",
    binding_source: str = "unmarked",
    recorded: list[Any] | None = None,
    evidence: list[Any] | None = None,
) -> None:
    """Verification decision + location-exemption counters (Phase 4.2).

    Phase 1 (claim binding) additions: per-claim binding dispositions,
    one binding-version stamp per turn, and one HISTORY subject outcome
    per turn — all closed-vocabulary, all fire-and-forget. ``recorded``
    is the timeline-backed attempt list (``None`` degrades to unmapped);
    ``evidence`` is the investigation bank for subject recomputation.
    """
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
    # Phase 1 (claim binding) counters: never raise, never block.
    try:
        _emit_telemetry(
            ctx,
            tenant_id,
            investigation_id,
            "record_binding_version",
            str(binding_version or "cbv1"),
            str(binding_source or "unmarked"),
        )
    except Exception:
        pass
    try:
        from investigation_agent_platform.application.investigation.verification import (
            collect_verification_diagnostics as _collect_diag,
        )

        _diag = None
        try:
            _diag = _collect_diag(candidate, list(evidence or []), result, recorded)
        except Exception:
            _diag = getattr(result, "diagnostics", None)
        if _diag is None:
            _diag = getattr(result, "diagnostics", None)
        for _binding in list(getattr(_diag, "claim_bindings", None) or []):
            try:
                _raw_key = str(getattr(_binding, "claim_key", "") or "")
                _k = _raw_key.rsplit("|", 1)[-1].strip() if "|" in _raw_key else "UNTYPED"
                _k = _k if _k in _BINDING_KIND_LABELS else "unspecified"
                if list(getattr(_binding, "claim_local", None) or []):
                    _disp = "claim_local"
                elif list(getattr(_binding, "bank_fallback", None) or []):
                    _disp = "bank_fallback"
                elif list(getattr(_binding, "unknown", None) or []):
                    _disp = "evaporated"
                elif bool(getattr(_binding, "unmapped", False)):
                    _disp = "unmapped"
                else:
                    _disp = "no_citations"
                _emit_telemetry(
                    ctx,
                    tenant_id,
                    investigation_id,
                    "record_claim_binding",
                    _k,
                    _disp,
                    str(binding_version or "cbv1"),
                )
            except Exception:
                continue
    except Exception:
        pass
    try:
        from investigation_agent_platform.application.investigation.verification import (
            VerificationEngine as _VE,
        )

        _hkind = _VE.effective_claim_kind(candidate)
        _hkind_value = str(getattr(_hkind, "value", _hkind) or "") if _hkind is not None else ""
        if _hkind_value == "HISTORY":
            _outcome = "mismatched_subject"
            _newest = "not_applicable"
            try:
                _hstatus = str(
                    getattr(getattr(result, "status", ""), "value", getattr(result, "status", ""))
                    or ""
                )
                _target = getattr(candidate, "claim_target", None)
                _claimed = (
                    str(getattr(_target, "commit_subject", "") or "") if _target is not None else ""
                )
                _best = "mismatched_subject"
                _claimed_empty = not _claimed
                _cited_rows = list(getattr(result, "corroborating_evidence_ids", None) or [])
                if _claimed_empty:
                    _outcome = "missing_subject"
                elif not _cited_rows:
                    _outcome = "missing_row"
                elif _hstatus == "VERIFIED":
                    _by_id: dict[str, Any] = {}
                    for _row in evidence or []:
                        try:
                            _by_id[str(getattr(_row, "evidence_id", "") or "")] = _row
                        except Exception:
                            continue
                    _seen_row = False
                    for _raw_corro in _cited_rows:
                        try:
                            _row = _by_id.get(_raw_corro)
                            if _row is None:
                                try:
                                    _row = _by_id.get(str(_raw_corro))
                                except Exception:
                                    continue
                            if _row is None:
                                continue
                            _seen_row = True
                            _summary = str(getattr(_row, "summary", "") or "")
                        except Exception:
                            continue
                        _o = _VE.subject_match_outcome(_claimed, _summary)
                        if _o == "matched_exact":
                            _best = _o
                            break
                        if _o == "matched_token_set" and _best != "matched_exact":
                            _best = _o
                    if not _seen_row:
                        _best = "missing_row"
                    _outcome = _best
                    _newest = "satisfied"
                else:
                    _outcome = "mismatched_subject"
                    _newest = "not_applicable"
            except Exception:
                _outcome = "mismatched_subject"
                _newest = "not_applicable"
            _emit_telemetry(
                ctx,
                tenant_id,
                investigation_id,
                "record_history_subject",
                _outcome,
                _newest,
            )
    except Exception:
        pass
    # Phase 3 (claim binding): per-record repair counters. Closed
    # vocabularies only — kind parsed from the record's claim_key tail
    # (same rule as bindings above), basis/outcome/version coerced by
    # the adapter. Snippets, paths, SHAs, and statements never reach
    # labels. Fire-and-forget: never raises, never blocks.
    try:
        _rdiag = getattr(result, "diagnostics", None)
        for _rep in list(getattr(_rdiag, "repaired", None) or []):
            try:
                _rkey = str(getattr(_rep, "claim_key", "") or "")
                _rk = _rkey.rsplit("|", 1)[-1].strip() if "|" in _rkey else "UNTYPED"
                _rk = _rk if _rk in _BINDING_KIND_LABELS else "unspecified"
                _emit_telemetry(
                    ctx,
                    tenant_id,
                    investigation_id,
                    "record_claim_repair",
                    _rk,
                    str(getattr(_rep, "basis", "") or ""),
                    str(getattr(_rep, "outcome", "") or ""),
                    str(getattr(_rep, "binding_version", "") or ""),
                )
            except Exception:
                continue
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


def _story_truncate(value: Any, *, max_str: int = 1000, max_list: int = 20) -> Any:
    """Bound a debug-story payload: strings capped, lists capped, dicts walked.

    Secrets are NOT handled here — callers must route the result through
    ``_log_extra`` (which sanitizes) before handing it to the logger.
    """
    if isinstance(value, str):
        return value[:max_str]
    if isinstance(value, (list, tuple)):
        return [
            _story_truncate(v, max_str=max_str, max_list=max_list) for v in list(value)[:max_list]
        ]
    if isinstance(value, dict):
        return {
            str(k)[:128]: _story_truncate(v, max_str=max_str, max_list=max_list)
            for k, v in list(value.items())[:max_list]
        }
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:max_str]


def _story_debug(
    *, tenant_id: str = "", investigation_id: Any = "", event: str, **data: Any
) -> None:
    """Emit one DEBUG story event with the full (truncated, sanitized) payload.

    No-op cost at INFO: ``logger.debug`` arguments are built only when the
    effective level allows it. Every value passes through ``_log_extra`` so
    the secret-redaction policy applies to story payloads too.
    """
    if not logger.isEnabledFor(logging.DEBUG):
        return
    logger.debug(
        event,
        extra=_log_extra(
            tenant_id=tenant_id,
            investigation_id=investigation_id,
            event=event,
            **{k: _story_truncate(v) for k, v in data.items()},
        ),
    )


def story_log(
    log: logging.Logger,
    level: int,
    msg: str,
    *args: Any,
    comp: str = COMP_ACTVTY,
    func: str = "",
    stage: str = "execute",
    step: str = "",
    outcome: str = "",
    tenant_id: str = "",
    investigation_id: Any = "",
    event: str = "",
    inputs: dict[str, Any] | None = None,
    outputs: dict[str, Any] | None = None,
    elapsed_ms: int | None = None,
    error: str | None = None,
    error_type: str | None = None,
    exc_info: Any = None,
    **data: Any,
) -> None:
    """Story-envelope log record (D1/D2/D8/D9): message text and data keys unchanged.

    The human message (matched verbatim by ``scripts/story.py`` milestones
    and several unit tests) and every ``extra`` data key are preserved; the
    story envelope (``comp/func/stage/step/outcome/inputs/outputs/elapsed_ms``)
    is merged in via ``story_event`` so rendering, caps, and redaction stay
    config-driven. Levels below WARNING route through ``story_level``
    (operators can quiet/reroute per component or stage with zero call-site
    edits); WARNING and above always stay at level.
    """
    routed = story_level(comp, stage, level) if level < logging.WARNING else level
    extra = _log_extra(
        tenant_id=tenant_id,
        investigation_id=investigation_id,
        event=event or msg,
        error=error,
        error_type=error_type,
        **data,
    )
    extra.update(
        story_event(
            comp=comp,
            func=func,
            stage=stage,
            step=step,
            outcome=outcome,
            tenant_id=tenant_id,
            investigation_id=investigation_id,
            inputs=inputs,
            outputs=outputs,
            elapsed_ms=elapsed_ms,
            error=error,
            error_type=error_type,
        )
    )
    if exc_info is not None:
        log.log(routed, msg, *args, extra=extra, exc_info=exc_info)
    else:
        log.log(routed, msg, *args, extra=extra)


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
            story_log(
                logger,
                logging.WARNING,
                "%s rejected invalid activity input: %s",
                activity,
                ", ".join(problems),
                comp=COMP_ACTVTY,
                func="_coerced_data",
                stage="execute",
                step="coerce",
                outcome=OUTCOME_SKIPPED,
                event=f"{activity} input rejected",
                activity_name=activity,
            )
            raise ApplicationFailure(
                f"{activity} received invalid input: missing {', '.join(problems)}",
                type="ActivityInputValidationError",
                non_retryable=True,
            )
        try:
            instance = cls(**{k: v for k, v in params.items() if k in known})
        except TypeError as exc:
            story_log(
                logger,
                logging.ERROR,
                "%s rejected malformed activity input",
                activity,
                comp=COMP_ACTVTY,
                func="_coerced_data",
                stage="execute",
                step="coerce",
                outcome=OUTCOME_FAILED,
                event=f"{activity} input rejected",
                error=str(exc),
                exc_info=True,
                activity_name=activity,
            )
            raise ApplicationFailure(
                f"{activity} received invalid input: {exc}",
                type="ActivityInputValidationError",
                non_retryable=True,
            ) from exc
        return {f.name: getattr(instance, f.name) for f in fields}
    story_log(
        logger,
        logging.WARNING,
        "%s rejected unsupported input type %s",
        activity,
        type(params).__name__,
        comp=COMP_ACTVTY,
        func="_coerced_data",
        stage="execute",
        step="coerce",
        outcome=OUTCOME_SKIPPED,
        event=f"{activity} input rejected",
        activity_name=activity,
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
        story_log(
            logger,
            logging.WARNING,
            "%s proceeding without idempotency reservation; store unwired",
            activity,
            comp=COMP_ACTVTY,
            func="_reserve_or_replay",
            stage="execute",
            step="reserve",
            outcome=OUTCOME_SKIPPED,
            tenant_id=tenant_id,
            investigation_id=investigation_id,
            event=f"{activity} idempotency skipped",
            activity_name=activity,
            idempotency_key=key,
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
            story_log(
                logger,
                logging.WARNING,
                "%s idempotency conflict; refusing duplicate",
                activity,
                comp=COMP_ACTVTY,
                func="_reserve_or_replay",
                stage="execute",
                step="reserve",
                outcome=OUTCOME_FAILED,
                tenant_id=tenant_id,
                investigation_id=investigation_id,
                event=f"{activity} idempotency conflict",
                error=str(exc),
                error_type=type(exc).__name__,
                activity_name=activity,
            )
            raise ApplicationFailure(
                str(exc), type="IdempotencyKeyConflict", non_retryable=True
            ) from exc
        except IdempotencyInProgressError as exc:
            if _wait_attempt >= _WAIT_POLLS:
                story_log(
                    logger,
                    logging.WARNING,
                    "%s idempotency owner still in progress after wait; yielding to retry",
                    activity,
                    comp=COMP_ACTVTY,
                    func="_reserve_or_replay",
                    stage="execute",
                    step="reserve",
                    outcome=OUTCOME_FAILED,
                    tenant_id=tenant_id,
                    investigation_id=investigation_id,
                    event=f"{activity} idempotency wait exhausted",
                    error=str(exc),
                    error_type=type(exc).__name__,
                    activity_name=activity,
                )
                raise ApplicationFailure(
                    str(exc), type="IdempotencyRequestInProgress", non_retryable=False
                ) from exc
            story_log(
                logger,
                logging.INFO,
                "%s idempotency in progress; waiting for owner (attempt %d/%d)",
                activity,
                _wait_attempt + 1,
                _WAIT_POLLS,
                comp=COMP_ACTVTY,
                func="_reserve_or_replay",
                stage="execute",
                step="reserve",
                outcome=OUTCOME_STARTED,
                tenant_id=tenant_id,
                investigation_id=investigation_id,
                event=f"{activity} idempotency wait",
                activity_name=activity,
                idempotency_key=key,
            )
            await asyncio.sleep(_WAIT_SECONDS)
    if not reserved:
        story_log(
            logger,
            logging.INFO,
            "%s idempotency replay; skipping re-execution",
            activity,
            comp=COMP_ACTVTY,
            func="_reserve_or_replay",
            stage="execute",
            step="reserve",
            outcome=OUTCOME_SKIPPED,
            tenant_id=tenant_id,
            investigation_id=investigation_id,
            event=f"{activity} idempotency replay",
            activity_name=activity,
            idempotency_key=key,
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
        story_log(
            logger,
            logging.ERROR,
            "%s idempotency complete failed; reservation left pending",
            activity,
            comp=COMP_ACTVTY,
            func="_complete_reservation",
            stage="execute",
            step="complete",
            outcome=OUTCOME_FAILED,
            tenant_id=tenant_id,
            investigation_id=investigation_id,
            event=f"{activity} idempotency complete failed",
            error=str(exc),
            error_type=type(exc).__name__,
            exc_info=True,
            activity_name=activity,
            idempotency_key=key,
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
        story_log(
            logger,
            logging.ERROR,
            "%s idempotency abandon failed; retry may see a stale lease",
            activity,
            comp=COMP_ACTVTY,
            func="_abandon_reservation",
            stage="execute",
            step="abandon",
            outcome=OUTCOME_FAILED,
            tenant_id=tenant_id,
            investigation_id=investigation_id,
            event=f"{activity} idempotency abandon failed",
            error=str(exc),
            error_type=type(exc).__name__,
            exc_info=True,
            activity_name=activity,
            idempotency_key=key,
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
        story_log(
            logger,
            logging.ERROR,
            "evidence_budget overlay failed; using global limits",
            comp=COMP_ACTVTY,
            func="overlay_evidence_budget",
            stage="execute",
            step="overlay",
            outcome=OUTCOME_FAILED,
            event="evidence_budget overlay failed",
            error=str(exc),
            error_type=type(exc).__name__,
            exc_info=True,
        )
    return limits
