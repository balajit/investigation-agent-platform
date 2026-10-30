# src/investigation_agent_platform/infrastructure/observability/tracing.py
"""Shared OTel decorator for the investigation path.

``@traced`` is the single compliant way to emit spans. The span IS the
telemetry record: name ``{layer}.{component}.{operation}``, required attrs
``iap.tenant_id`` / ``iap.investigation_id`` / ``iap.component`` /
``iap.outcome``, optional families per kind, hard bans on payload content
(see ``docs/IAP-OTEL-telemetry-format.md`` §1–§4 — the normative format spec).

Fail-safety: OTel's global tracer is a no-op without an SDK/exporter, so
decorated code runs unchanged with zero spans. Decorator bugs must never
break the wrapped function — extraction/attribution errors degrade to a
warning + a less-attributed span, never to a raised error.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import logging
from collections.abc import Callable
from typing import Any, Self

logger = logging.getLogger(__name__)

# Hard cap for any string attribute value (defense in depth; callers MUST
# also avoid passing payload content — see _suspicious()).
MAX_ATTRIBUTE_CHARS = 256

# Attribute names that MUST look like IDs/counts, never free text. Values for
# these keys are passed through; everything else stringified + truncated.
_PASSTHROUGH_KEYS = frozenset(
    {
        "iap.tenant_id",
        "iap.investigation_id",
        "iap.component",
        "iap.outcome",
        "iap.action_type",
        "iap.provider",
        "iap.tool",
        "iap.transport",
        "iap.model",
        "iap.verdict",
        "iap.cache",
        "iap.cache_backend",
        "iap.error_type",
    }
)


def _truncate(value: Any) -> Any:
    if isinstance(value, str) and len(value) > MAX_ATTRIBUTE_CHARS:
        return value[:MAX_ATTRIBUTE_CHARS]
    return value


def _module_component(func: Callable[..., Any]) -> str:
    """Component default: owning module short name (e.g. `activities`)."""
    module = (func.__module__ or "").split(".")[-1] or "unknown"
    return module


def extract_scope(
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    tenant_from: Callable[[tuple[Any, ...], dict[str, Any]], Any | None] | None = None,
    inv_from: Callable[[tuple[Any, ...], dict[str, Any]], Any | None] | None = None,
) -> tuple[str | None, str | None]:
    """Best-effort (tenant_id, investigation_id) extraction. Never raises."""
    try:
        params = kwargs.get("params")
        if tenant_from is not None:
            tenant = tenant_from(args, kwargs)
        else:
            tenant = kwargs.get("tenant_id")
            if tenant is None and params is not None:
                # Temporal activities take a single dataclass `params` input
                # carrying tenant_id/investigation_id as fields.
                tenant = (
                    params.get("tenant_id")
                    if isinstance(params, dict)
                    else getattr(params, "tenant_id", None)
                )
            if tenant is None:
                for alias in ("context", "scope"):
                    holder = kwargs.get(alias)
                    if holder is not None:
                        tenant = getattr(holder, "tenant_id", None)
                        if tenant is not None:
                            break
        if inv_from is not None:
            inv = inv_from(args, kwargs)
        else:
            inv = kwargs.get("investigation_id") or kwargs.get("investigation_uuid")
            if inv is None and params is not None:
                if isinstance(params, dict):
                    inv = params.get("investigation_id")
                else:
                    inv = getattr(params, "investigation_id", None)
            if inv is None:
                for alias in ("context", "scope"):
                    holder = kwargs.get(alias)
                    if holder is not None:
                        inv = getattr(holder, "investigation_id", None)
                        if inv is not None:
                            break
        if tenant is None or inv is None:
            # Last resort: scan positional args for holders (e.g. an
            # unbound dataclass). First non-None attribute wins.
            for candidate in args:
                if isinstance(candidate, (str, bytes, int, float, bool)) or candidate is None:
                    continue
                if tenant is None:
                    tenant = getattr(candidate, "tenant_id", None)
                if inv is None:
                    inv = getattr(candidate, "investigation_id", None)
                if tenant is not None and inv is not None:
                    break
        tenant_str = str(tenant) if tenant is not None else None
        inv_str = str(inv) if inv is not None else None
        return tenant_str, inv_str
    except Exception as exc:
        logger.warning("Trace scope extraction failed open: %s", exc)
        return None, None


def _set_span_attributes(span: Any, attributes: dict[str, Any]) -> None:
    try:
        for key, value in attributes.items():
            if value is None:
                continue
            if key in _PASSTHROUGH_KEYS or isinstance(value, (bool, int, float)):
                span.set_attribute(key, value)
            elif isinstance(value, (list, tuple)) and len(value) <= 10:
                span.set_attribute(key, [str(v)[:64] for v in value])
            else:
                span.set_attribute(key, _truncate(str(value)))
    except Exception as exc:
        logger.warning("Trace attribution failed open: %s", exc)


def _finalize(span: Any, outcome: str, extra: dict[str, Any] | None = None) -> None:
    try:
        from opentelemetry.trace import StatusCode

        _set_span_attributes(span, {"iap.outcome": outcome, **(extra or {})})
        if outcome == "error":
            span.set_status(StatusCode.ERROR)
        else:
            span.set_status(StatusCode.OK)
    except Exception as exc:
        logger.warning("Trace finalization failed open: %s", exc)


def _fail(span: Any, exc: BaseException) -> None:
    try:
        from opentelemetry.trace import StatusCode

        _set_span_attributes(
            span,
            {
                "iap.outcome": "error",
                "iap.error_type": type(exc).__name__,
                "iap.error_message": str(exc),
            },
        )
        span.set_status(StatusCode.ERROR)
    except Exception as log_exc:
        logger.warning("Trace error path failed open: %s", log_exc)


def traced(
    layer: str,
    *,
    component: str | None = None,
    operation: str | None = None,
    attributes: dict[str, Any] | None = None,
    tenant_from: Callable[[tuple[Any, ...], dict[str, Any]], Any | None] | None = None,
    inv_from: Callable[[tuple[Any, ...], dict[str, Any]], Any | None] | None = None,
    outcome_from: Callable[[Any], str] | None = None,
    extra_from: Callable[[Any], dict[str, Any]] | None = None,
    # require_scope=False for pure helpers (compilers, rankers): the span
    # joins the parent trace but no missing-tenant warning fires — scope
    # genuinely does not exist at that level, and the parent carries it.
    require_scope: bool = True,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Emit one OTel span per call: ``{layer}.{component}.{operation}``.

    - ``layer``: worker | reason | gateway | provider | knowledge | topology | mcp.
    - ``component`` defaults to the owning module short name.
    - ``operation`` defaults to the function name.
    - ``attributes``: static extra span attrs (bounded, non-payload).
    - ``tenant_from`` / ``inv_from``: custom scope extractors; default covers
      kwargs, ``context``/``scope`` holders, and dataclass ``params``.
    - ``outcome_from``: maps the return value to ok/degraded/skipped/blocked
      (default ok). Errors always map to error via the exception path.
    - ``extra_from``: maps the return value to extra span attrs (counts only).
    """

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        comp = component or _module_component(func)
        op = operation or func.__name__
        span_name = f"{layer}.{comp}.{op}"
        try:
            from opentelemetry import trace as _trace

            tracer = _trace.get_tracer(f"iap.{comp}")
        except Exception:
            tracer = None  # type: ignore[assignment]
        try:
            signature = inspect.signature(func)
        except (TypeError, ValueError):
            signature = None

        def _open_span(f_args: tuple[Any, ...], f_kwargs: dict[str, Any]) -> tuple[Any, Any]:
            # Bind positional args to parameter names so scope extraction
            # works for positional calls (activities take `params` first).
            bound_kwargs = dict(f_kwargs)
            if signature is not None:
                try:
                    bound = signature.bind_partial(*f_args, **f_kwargs)
                    for name, value in bound.arguments.items():
                        bound_kwargs.setdefault(name, value)
                except Exception as bind_exc:
                    logger.warning("Trace arg binding failed open: %s", bind_exc)
            tenant_id, investigation_id = extract_scope(f_args, bound_kwargs, tenant_from, inv_from)
            if tenant_id is None and require_scope:
                logger.warning("Span %s missing iap.tenant_id (non-compliant)", span_name)
            cm = tracer.start_as_current_span(span_name) if tracer is not None else _null_span()
            span = cm.__enter__()
            _set_span_attributes(
                span,
                {
                    "iap.tenant_id": tenant_id,
                    "iap.investigation_id": investigation_id,
                    "iap.component": comp,
                    **(attributes or {}),
                },
            )
            return cm, span

        def _close_span(cm: Any, span: Any, result: Any, exc: BaseException | None) -> None:
            try:
                if exc is not None:
                    _fail(span, exc)
                else:
                    outcome = "ok"
                    extra: dict[str, Any] = {}
                    if outcome_from is not None:
                        try:
                            outcome = outcome_from(result)
                        except Exception as map_exc:
                            logger.warning("Outcome mapping failed open: %s", map_exc)
                    if extra_from is not None:
                        try:
                            extra = dict(extra_from(result) or {})
                        except Exception as map_exc:
                            logger.warning("Extra mapping failed open: %s", map_exc)
                    _finalize(span, outcome, extra)
            finally:
                try:
                    cm.__exit__(None, None, None)
                except Exception as exit_exc:
                    logger.warning("Span close failed open: %s", exit_exc)

        if asyncio.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*f_args: Any, **f_kwargs: Any) -> Any:
                cm, span = _open_span(f_args, f_kwargs)
                try:
                    result = await func(*f_args, **f_kwargs)
                except BaseException as exc:
                    _close_span(cm, span, None, exc)
                    raise
                _close_span(cm, span, result, None)
                return result

            return async_wrapper

        @functools.wraps(func)
        def sync_wrapper(*f_args: Any, **f_kwargs: Any) -> Any:
            cm, span = _open_span(f_args, f_kwargs)
            try:
                result = func(*f_args, **f_kwargs)
            except BaseException as exc:
                _close_span(cm, span, None, exc)
                raise
            _close_span(cm, span, result, None)
            return result

        return sync_wrapper

    return decorator


class _NullSpan:
    """No-op span used when OTel is unavailable (mirrors SDK no-op)."""

    def set_attribute(self, *args: Any, **kwargs: Any) -> None:
        return None

    def set_status(self, *args: Any, **kwargs: Any) -> None:
        return None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> bool:
        return False


class _NullSpanCM:
    def __enter__(self) -> _NullSpan:
        return _NullSpan()

    def __exit__(self, *args: object) -> bool:
        return False


def _null_span() -> _NullSpanCM:
    return _NullSpanCM()


def span_attributes(
    tenant_id: str | None = None,
    investigation_id: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Build a compliant attribute dict for manual `with` blocks (§1.2/§1.3)."""
    return {
        "iap.tenant_id": tenant_id,
        "iap.investigation_id": investigation_id,
        "iap.component": extra.pop("component", "manual"),
        **extra,
    }


_configured = False


def configure_tracing(service_name: str, otlp_endpoint: str, sample_ratio: float = 1.0) -> bool:
    """Point the GLOBAL tracer provider at the OTLP collector (idempotent).

    Called once per process at startup (see `main.py`), independent of the
    dev/production context split — the observability ADAPTER is only built on
    the production path, but spans must export in every environment where
    telemetry is enabled. Returns True when a real provider was installed.
    Never raises: tracing must never break boot.
    """
    global _configured
    if _configured:
        return True
    try:
        from opentelemetry import trace as _trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

        try:
            ratio = max(0.0, min(1.0, float(sample_ratio)))
        except (TypeError, ValueError):
            ratio = 1.0
        provider = TracerProvider(
            resource=Resource.create(attributes={"service.name": service_name}),
            sampler=ParentBased(root=TraceIdRatioBased(ratio)),
        )
        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=otlp_endpoint, insecure=True))
        )
        _trace.set_tracer_provider(provider)
        _configured = True
        logger.info(
            "OTLP tracing configured",
            extra={"endpoint": otlp_endpoint, "service": service_name},
        )
        return True
    except ImportError:
        logger.warning("OTLP exporter not installed; tracing disabled")
        return False
    except Exception as exc:
        logger.warning("OTLP tracing setup failed open: %s", exc)
        return False
