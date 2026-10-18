# src/investigation_agent_platform/infrastructure/observability/temporal_interceptor.py
"""Temporal OTel bridging (#9).

End-to-end traces across API dispatch -> workflow -> activities with no
spans in workflow code (replay determinism preserved): the contrib
``TracingInterceptor`` propagates context and spans client + activity
calls, and never creates workflow-code spans unless explicitly asked
(``always_create_workflow_spans`` stays False). All construction is
fail-open — telemetry must never break worker/API boot.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def build_temporal_tracing_interceptor() -> Any | None:
    """Return a Temporal TracingInterceptor, or None when unavailable.

    None means: temporalio contrib missing, OTel API missing, or telemetry
    disabled — callers pass the result straight into ``interceptors=``
    (empty list when None), so boot proceeds untraced rather than failing.
    """
    try:
        from temporalio.contrib.opentelemetry import TracingInterceptor
    except ImportError as exc:
        logger.info("Temporal OTel contrib unavailable; traces stay activity-local", extra={"error": str(exc)[:150]})
        return None
    try:
        return TracingInterceptor()
    except Exception as exc:
        logger.warning("Temporal TracingInterceptor build failed; continuing untraced", extra={"error": str(exc)[:200]})
        return None


def temporal_interceptors() -> list[Any]:
    """Interceptor list for Client.connect / Worker (empty when disabled)."""
    interceptor = build_temporal_tracing_interceptor()
    return [interceptor] if interceptor is not None else []
