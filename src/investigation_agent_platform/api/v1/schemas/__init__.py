# src/investigation_agent_platform/api/v1/schemas/__init__.py
"""Typed response contracts for the v1 API (IAP OpenAPI spec 1.0).

One module per router module; schemas import from domain/ports models only,
never from routers. See docs/IAP-implementation-openapi-spec-v1.0.md.
"""

from investigation_agent_platform.api.v1.schemas.envelopes import (
    CursorEnvelope,
    ItemsEnvelope,
    ListEnvelope,
)
from investigation_agent_platform.api.v1.schemas.errors import (
    COMMON_ERROR_RESPONSES,
    ErrorDetail,
    ErrorResponse,
)

__all__ = [
    "COMMON_ERROR_RESPONSES",
    "CursorEnvelope",
    "ErrorDetail",
    "ErrorResponse",
    "ItemsEnvelope",
    "ListEnvelope",
]
