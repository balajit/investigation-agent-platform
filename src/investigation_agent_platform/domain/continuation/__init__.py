"""S7 continuation domain contracts."""

from investigation_agent_platform.domain.continuation.models import (
    S7_CONTINUATION_LEASE_SECONDS,
    S7_CONTINUATION_MAX_ATTEMPTS,
    S7_CONTINUATION_TTL_SECONDS,
    S7_CONTINUATION_VERSION_CURRENT,
    ContinuationStatus,
    S7Continuation,
)

__all__ = [
    "S7_CONTINUATION_LEASE_SECONDS",
    "S7_CONTINUATION_MAX_ATTEMPTS",
    "S7_CONTINUATION_TTL_SECONDS",
    "S7_CONTINUATION_VERSION_CURRENT",
    "ContinuationStatus",
    "S7Continuation",
]
