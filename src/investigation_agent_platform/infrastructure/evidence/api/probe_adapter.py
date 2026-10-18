# src/investigation_agent_platform/infrastructure/evidence/api/probe_adapter.py
"""Sandboxed HTTP probe adapter (P2-2).

Calls ONLY profile-declared endpoints (endpoint_id allowlist, no raw URLs),
declared methods only (never sends bodies), with tenant-header injection, no cross-host redirects, and
bounded/redacted responses. Every violation fails closed before any socket
opens.
"""

from __future__ import annotations

import logging
import time
from typing import Any
from urllib.parse import urlencode

import httpx

from investigation_agent_platform.domain.common.exceptions import (
    ProbeEndpointNotFoundError,
    SecurityPolicyViolationException,
)
from investigation_agent_platform.infrastructure.observability.tracing import traced
from investigation_agent_platform.ports.evidence.api_probe import (
    ApiProbePort,
    ApiProbeRequest,
    ApiProbeResult,
)

logger = logging.getLogger(__name__)

# Response headers safe to persist as evidence (never auth/cookies).
_ALLOWED_RESPONSE_HEADERS = frozenset(
    {"content-type", "content-length", "date", "x-correlation-id", "x-request-id"}
)

_MAX_RESPONSE_BYTES = 65_536
# Model cap on ApiProbeResult.response_body_excerpt — the excerpt kept as
# evidence is bounded here, not at transport size. A body longer than this
# sets response_body_truncated instead of failing model validation.
# Kept strictly below the Evidence 4096-char externalization boundary
# (content_snippet >= 4096 requires content_uri/large_payload_pointer):
# a cap of exactly 4096 produced 4096-char excerpts that failed validation
# and killed whole runs (boundary off-by-one).
_MAX_EXCERPT_CHARS = 4000


def _probe_extra(result: Any) -> dict[str, Any]:
    """Probe outcome: endpoint, status, truncation (never URL/body)."""
    try:
        extra: dict[str, Any] = {}
        endpoint = getattr(result, "endpoint_id", None)
        if endpoint:
            extra["iap.endpoint"] = str(endpoint)[:128]
        status = getattr(result, "status_code", None)
        if isinstance(status, int):
            extra["iap.status_code"] = status
        truncated = getattr(result, "response_body_truncated", None)
        if truncated is not None:
            extra["iap.truncated"] = bool(truncated)
        latency = getattr(result, "latency_ms", None)
        if isinstance(latency, (int, float)):
            extra["iap.latency_ms"] = latency
        return extra
    except Exception:
        return {}


class HttpxApiProbeAdapter(ApiProbePort):
    def __init__(
        self,
        profile_repo: Any,
        timeout_seconds: float = 5.0,
        max_response_bytes: int = _MAX_RESPONSE_BYTES,
        client_factory: Any = None,
    ) -> None:
        self._profile_repo = profile_repo
        self._timeout_seconds = timeout_seconds
        self._max_response_bytes = max_response_bytes
        self._client_factory = client_factory

    @traced("provider", component="probe_adapter", extra_from=_probe_extra)
    async def probe(
        self, tenant_id: str, application_id: str, request: ApiProbeRequest
    ) -> ApiProbeResult:
        if not tenant_id or tenant_id == "anonymous":
            raise SecurityPolicyViolationException("Probe requires an authenticated tenant")
        profile = await self._profile_repo.get_by_application_id(tenant_id, application_id)
        if profile is None:
            raise ProbeEndpointNotFoundError(
                f"No application profile for '{application_id}'; cannot scope probe"
            )
        endpoint = next(
            (
                e
                for svc in (getattr(profile, "services", []) or [])
                for e in (getattr(svc, "probeable_endpoints", []) or [])
                if getattr(e, "id", None) == request.endpoint_id
            ),
            None,
        )
        if endpoint is None:
            raise ProbeEndpointNotFoundError(
                f"Unknown probe endpoint '{request.endpoint_id}' for '{application_id}'"
            )
        method = str(getattr(endpoint, "method", "GET") or "GET").upper()
        if method not in ("GET", "HEAD", "POST", "PUT", "DELETE", "PATCH"):
            raise SecurityPolicyViolationException(
                f"Probe method {method!r} not allowed (GET/HEAD/POST/PUT/DELETE/PATCH only; no request bodies)"
            )
        placeholders = set(getattr(endpoint, "path_placeholders", []) or [])
        unknown_params = set(request.path_params or {}) - placeholders
        if unknown_params:
            raise SecurityPolicyViolationException(
                f"Path params outside declared placeholders: {sorted(unknown_params)}"
            )
        try:
            path = str(getattr(endpoint, "path_template", ""))
            for key in placeholders:
                # Empty values would silently produce double slashes or a
                # different route (and a misleading 404 in evidence) — fail
                # closed so the model learns the parameter is required.
                value = str(request.path_params.get(key, "") or "").strip()
                if not value or "/" in value:
                    raise SecurityPolicyViolationException(
                        f"Path parameter '{key}' is required and must be a single segment"
                    )
                path = path.replace("{" + key + "}", value)
        except SecurityPolicyViolationException:
            raise
        except Exception as exc:
            raise SecurityPolicyViolationException(f"Invalid path template: {exc}") from exc
        if "{" in path or "}" in path:
            raise SecurityPolicyViolationException("Unresolved path placeholders")
        allowed_qs = set(getattr(endpoint, "allowed_query_params", []) or [])
        query = {
            k: str(v) for k, v in (request.query_params or {}).items() if k in allowed_qs
        }
        base = str(getattr(endpoint, "base_url", "") or "").rstrip("/")
        if not base.startswith(("http://", "https://")):
            raise SecurityPolicyViolationException("Probe base_url must be http(s)")
        url = f"{base}{path}"
        if query:
            url = f"{url}?{urlencode(query)}"
        started = time.monotonic()
        try:
            if self._client_factory is not None:
                client_cm = self._client_factory()
            else:
                client_cm = httpx.AsyncClient(
                    timeout=self._timeout_seconds, follow_redirects=False
                )
            async with client_cm as client:
                response = await client.request(
                    method, url, headers={"X-Tenant-ID": tenant_id}
                )
        except Exception as exc:
            raise ProbeEndpointNotFoundError(f"Probe request failed: {exc}") from exc
        latency_ms = int((time.monotonic() - started) * 1000)
        body = response.content[: self._max_response_bytes + 1]
        truncated = len(body) > _MAX_EXCERPT_CHARS
        excerpt = body[:_MAX_EXCERPT_CHARS].decode("utf-8", errors="replace")
        headers = {
            k.lower(): v
            for k, v in response.headers.items()
            if k.lower() in _ALLOWED_RESPONSE_HEADERS
        }
        return ApiProbeResult(
            endpoint_id=request.endpoint_id,
            method=method,
            resolved_url=url,
            status_code=response.status_code,
            response_headers=headers,
            response_body_excerpt=excerpt,
            response_body_truncated=truncated,
            latency_ms=latency_ms,
        )

    async def probe_by_url(self, *_args: Any, **_kwargs: Any) -> ApiProbeResult:
        """Explicitly absent raw-URL mode: no calling convention exists."""
        raise SecurityPolicyViolationException("Raw-URL probing is not supported")
