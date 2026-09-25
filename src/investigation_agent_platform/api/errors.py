# src/investigation_agent_platform/api/errors.py
"""Public error mapping and logging redaction policy (F-062/F-063).

F-062: internal exception messages (which may contain SQL fragments, provider
responses, or implementation details) must never reach API callers. Every
domain exception maps to a stable public ``(code, message, http_status)``
triple; detailed diagnostics stay in secure server-side logs/traces,
correlated by correlation ID.

F-063: structured log fields are classified — secrets, tokens, prompts,
evidence content, SQL parameters, and raw payloads are redacted; long values
are truncated. Use ``sanitize_extra()`` on every ``extra={...}`` dict and
``redact_value()`` for free-form strings.
"""

from __future__ import annotations

import re
from typing import Any

# Case-insensitive keys/patterns that must never appear in logs or responses.
REDACTED_KEYS = frozenset(
    {
        "authorization",
        "api_key",
        "apikey",
        "secret",
        "password",
        "token",
        "bearer",
        "prompt",
        "system_prompt",
        "evidence",
        "payload",
        "sql",
        "query_string",
        "raw_sql",
        "connection_uri",
        "dsn",
    }
)
_SECRET_PATTERN = re.compile(r"(?i)(sk-[a-z0-9_-]{4,}|Bearer\s+[A-Za-z0-9._~+/-]+=*)")
_MAX_FIELD_CHARS = 500

# Public, stable messages per error code. The internal message is NEVER sent.
_PUBLIC_MESSAGES: dict[str, str] = {
    "DOMAIN_VALIDATION_ERROR": "Invalid request parameters or payload.",
    "VALIDATION_ERROR": "Invalid request parameters or payload.",
    "SECURITY_POLICY_VIOLATION": "Not authorized to perform this action.",
    "UNAUTHORIZED": "Not authorized to perform this action.",
    "EVIDENCE_NOT_FOUND": "Requested resource was not found.",
    "NOT_FOUND": "Requested resource was not found.",
    "CONCURRENCY_CONFLICT": "Resource conflict; fetch the latest state and retry.",
    "IDEMPOTENCY_KEY_CONFLICT": "Idempotency key was already used with a different request.",
    "IDEMPOTENCY_REQUEST_IN_PROGRESS": "A request with this idempotency key is already in progress.",
    "VERIFICATION_FAILURE": "Verification could not be completed.",
    "INVESTIGATION_LIMIT_EXCEEDED": "Investigation budget or limit exceeded.",
    "EXECUTION_ERROR": "Upstream execution failed; retry later.",
    "PLATFORM_CONFIGURATION_ERROR": "Service is not configured correctly.",
    "DOMAIN_ERROR": "An internal error occurred.",
}


def public_error(code: str, http_status: int) -> dict[str, Any]:
    """Return the stable public error body for a code/status pair."""
    return {
        "code": code,
        "message": _PUBLIC_MESSAGES.get(code, "An internal error occurred."),
    }


def redact_value(value: Any) -> Any:
    """Redact a single log/response value."""
    if isinstance(value, str):
        redacted = _SECRET_PATTERN.sub("[REDACTED]", value)
        if len(redacted) > _MAX_FIELD_CHARS:
            return redacted[:_MAX_FIELD_CHARS] + "…[truncated]"
        return redacted
    if isinstance(value, dict):
        return {
            ("[REDACTED_KEY]" if str(k).lower() in REDACTED_KEYS else k): (
                "[REDACTED]" if str(k).lower() in REDACTED_KEYS else redact_value(v)
            )
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_value(v) for v in value][:50]
    return value


def sanitize_extra(extra: dict[str, Any]) -> dict[str, Any]:
    """Apply the logging redaction policy to a structlog ``extra`` dict."""
    return {
        k: ("[REDACTED]" if str(k).lower() in REDACTED_KEYS else redact_value(v))
        for k, v in extra.items()
    }
