# src/investigation_agent_platform/api/v1/schemas/errors.py
"""The single public error envelope (F-062/F-063, OpenAPI spec 1.0 §4)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ErrorDetail(BaseModel):
    code: str = Field(description="Stable machine code, e.g. NOT_FOUND, PROFILE_VERSION_CONFLICT.")
    message: str = Field(description="Stable, non-sensitive, human-readable message.")
    correlation_id: str = Field(description="Server correlation id; also sent as X-Correlation-ID.")
    details: dict[str, Any] | None = Field(
        default=None,
        description="Optional structured detail. Only validation errors populate this.",
    )


class ErrorResponse(BaseModel):
    error: ErrorDetail


# Attach to every path operation: responses=COMMON_ERROR_RESPONSES
COMMON_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: {"model": ErrorResponse, "description": "Bad request."},
    401: {"model": ErrorResponse, "description": "Unauthorized."},
    403: {"model": ErrorResponse, "description": "Forbidden."},
    404: {"model": ErrorResponse, "description": "Resource not found."},
    409: {"model": ErrorResponse, "description": "Conflict (concurrency or idempotency)."},
    422: {"model": ErrorResponse, "description": "Validation error."},
}
