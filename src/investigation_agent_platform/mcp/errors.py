# src/investigation_agent_platform/mcp/errors.py
"""Agent-facing MCP error mapping (Part 9 Phase 6).

Internal failures become a stable ``{"error": {code, message, retryable}}``
tool result. Categories distinguish caller-fixable problems (invalid request
or cursor) from retryable provider outages and from internal failures. Never
exposes Elasticsearch internals, credentials, stack traces, raw SQL with
literals, or provider request details — those stay in server logs.
"""

import logging
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field
from pydantic import ValidationError as PydanticValidationError

from investigation_agent_platform.domain.common.exceptions import (
    ApplicationProfileNotFoundException,
    DomainException,
    EvidenceNotFoundException,
    ExecutionError,
    InvalidCursorException,
    ProviderTimeoutException,
    ProviderUnavailableException,
    SecurityPolicyViolationException,
)

logger = logging.getLogger(__name__)

MAX_ERROR_MESSAGE_CHARS = 1000


class McpErrorCode(StrEnum):
    """Stable agent-facing error codes (subset used where produced)."""

    INVALID_REQUEST = "INVALID_REQUEST"
    INVALID_CURSOR = "INVALID_CURSOR"
    CURSOR_EXPIRED = "CURSOR_EXPIRED"
    FORBIDDEN = "FORBIDDEN"
    EVIDENCE_NOT_FOUND = "EVIDENCE_NOT_FOUND"
    PROVIDER_TIMEOUT = "PROVIDER_TIMEOUT"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    INCOMPLETE_EVIDENCE = "INCOMPLETE_EVIDENCE"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class McpErrorDetail(BaseModel):
    """One agent-facing error (safe to serialize to the model)."""

    model_config = ConfigDict(frozen=True)

    code: McpErrorCode
    message: str = Field(..., max_length=1000)
    retryable: bool = False


class McpErrorEnvelope(BaseModel):
    """Tool-result envelope for failures (union arm alongside success)."""

    model_config = ConfigDict(frozen=True)

    error: McpErrorDetail


def _safe_message(message: str) -> str:
    text = message if isinstance(message, str) else "Investigation operation failed."
    text = " ".join(text.split())
    return text[:MAX_ERROR_MESSAGE_CHARS] if text else "Investigation operation failed."


def map_exception_to_error(exc: BaseException) -> McpErrorDetail:
    """Map any failure to a safe, retry-hinted agent error."""
    if isinstance(exc, InvalidCursorException):
        return McpErrorDetail(
            code=McpErrorCode.INVALID_CURSOR,
            message=(
                "The pagination cursor is invalid, expired, or no longer matches "
                "this investigation query. Restart pagination without a cursor."
            ),
            retryable=False,
        )
    if isinstance(exc, EvidenceNotFoundException):
        return McpErrorDetail(
            code=McpErrorCode.EVIDENCE_NOT_FOUND,
            message="The requested evidence is not available in the current investigation.",
            retryable=False,
        )
    if isinstance(exc, SecurityPolicyViolationException):
        return McpErrorDetail(
            code=McpErrorCode.FORBIDDEN,
            message="The request violates the investigation security policy.",
            retryable=False,
        )
    if isinstance(exc, ProviderTimeoutException):
        return McpErrorDetail(
            code=McpErrorCode.PROVIDER_TIMEOUT,
            message="The evidence provider timed out. Retry with a narrower time window.",
            retryable=True,
        )
    if isinstance(exc, ProviderUnavailableException):
        return McpErrorDetail(
            code=McpErrorCode.PROVIDER_UNAVAILABLE,
            message="The evidence provider is unavailable. Retry shortly.",
            retryable=True,
        )
    if isinstance(exc, ApplicationProfileNotFoundException):
        # Configuration inconsistency; no application/investigation detail disclosed.
        return McpErrorDetail(
            code=McpErrorCode.INTERNAL_ERROR,
            message="The investigation scope is misconfigured. Contact the operator.",
            retryable=False,
        )
    if isinstance(exc, ExecutionError):
        return McpErrorDetail(
            code=McpErrorCode.INTERNAL_ERROR,
            message="The investigation operation failed.",
            retryable=bool(exc.retryable),
        )
    if isinstance(exc, DomainException):
        return McpErrorDetail(
            code=McpErrorCode.INVALID_REQUEST,
            message=_safe_message(exc.message),
            retryable=False,
        )
    if isinstance(exc, PydanticValidationError):
        return McpErrorDetail(
            code=McpErrorCode.INVALID_REQUEST,
            message="The tool arguments failed validation.",
            retryable=False,
        )
    logger.error("Unhandled MCP tool failure", exc_info=exc)
    return McpErrorDetail(
        code=McpErrorCode.INTERNAL_ERROR,
        message="An internal investigation failure occurred.",
        retryable=False,
    )


def error_envelope(exc: BaseException) -> McpErrorEnvelope:
    """Build the tool-result envelope for a failure (logs unexpected ones)."""
    return McpErrorEnvelope(error=map_exception_to_error(exc))
