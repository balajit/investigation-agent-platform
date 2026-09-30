# src/investigation_agent_platform/domain/common/exceptions.py
"""Unified Domain exception hierarchy for Investigation Agent Platform."""

import logging
from typing import Any

logger = logging.getLogger(__name__)


class DomainException(Exception):
    """Base domain exception for Investigation Agent Platform with structured telemetry."""

    def __init__(
        self,
        message: str,
        error_code: str = "DOMAIN_ERROR",
        details: dict[str, Any] | None = None,
        retryable: bool = False,
        http_status_code: int = 400,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.error_code = error_code
        self.details = details or {}
        self.retryable = retryable
        self.http_status_code = http_status_code

    @property
    def code(self) -> str:
        return self.error_code

    def to_dict(self) -> dict[str, Any]:
        return {
            "error_code": self.error_code,
            "message": self.message,
            "details": self.details,
            "retryable": self.retryable,
            "http_status_code": self.http_status_code,
        }


class DomainValidationException(DomainException):
    """Exception raised when domain state validation constraints are breached."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(
            message=message,
            error_code="DOMAIN_VALIDATION_ERROR",
            details=details,
            retryable=False,
            http_status_code=422,
        )


class InvalidInvestigationStateException(DomainException):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(
            message=message,
            error_code="INVALID_INVESTIGATION_STATE",
            details=details,
            retryable=False,
            http_status_code=409,
        )


class InvalidLifecycleTransitionException(DomainException):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(
            message=message,
            error_code="INVALID_LIFECYCLE_TRANSITION",
            details=details,
            retryable=False,
            http_status_code=409,
        )


class InvalidHypothesisException(DomainException):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(
            message=message,
            error_code="INVALID_HYPOTHESIS",
            details=details,
            retryable=False,
            http_status_code=422,
        )


# Resource & Not Found Exceptions
class EvidenceNotFoundException(DomainException):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(
            message=message,
            error_code="EVIDENCE_NOT_FOUND",
            details=details,
            retryable=False,
            http_status_code=404,
        )


class ApplicationProfileNotFoundException(DomainException):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(
            message=message,
            error_code="APPLICATION_PROFILE_NOT_FOUND",
            details=details,
            retryable=False,
            http_status_code=404,
        )


# Guardrail & Boundary Exceptions
class InvestigationLimitExceededException(DomainException):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(
            message=message,
            error_code="INVESTIGATION_LIMIT_EXCEEDED",
            details=details,
            retryable=False,
            http_status_code=429,
        )


class GuardrailError(DomainException):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(
            message=message,
            error_code="GUARDRAIL_BREACH",
            details=details,
            retryable=False,
            http_status_code=403,
        )


class SecurityPolicyViolationException(DomainException):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(
            message=message,
            error_code="SECURITY_POLICY_VIOLATION",
            details=details,
            retryable=False,
            http_status_code=403,
        )


# Pagination Exceptions (Part 9: invalid cursors fail, never fall back to page one)
class InvalidCursorException(DomainException):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(
            message=message,
            error_code="INVALID_CURSOR",
            details=details,
            retryable=False,
            http_status_code=400,
        )


# Provider Exceptions (Part 9: typed provider failures instead of one ExecutionError)
class ProviderTimeoutException(DomainException):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(
            message=message,
            error_code="PROVIDER_TIMEOUT",
            details=details,
            retryable=True,
            http_status_code=504,
        )


class ProviderUnavailableException(DomainException):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(
            message=message,
            error_code="PROVIDER_UNAVAILABLE",
            details=details,
            retryable=True,
            http_status_code=503,
        )


# Infrastructure & System Configuration Exceptions
class PlatformConfigurationError(DomainException):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(
            message=message,
            error_code="PLATFORM_CONFIGURATION_ERROR",
            details=details,
            retryable=False,
            http_status_code=500,
        )


class CredentialProviderError(DomainException):
    """Outbound credential acquisition failure (Part 11.4).

    Never carries token values, client secrets, or full auth responses —
    details are limited to provider id, host, and sanitized failure class.
    """

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(
            message=message,
            error_code="CREDENTIAL_PROVIDER_ERROR",
            details=details,
            retryable=False,
            http_status_code=502,
        )


class ExecutionError(DomainException):
    def __init__(
        self, message: str, details: dict[str, Any] | None = None, retryable: bool = True
    ) -> None:
        super().__init__(
            message=message,
            error_code="EXECUTION_ERROR",
            details=details,
            retryable=retryable,
            http_status_code=502,
        )


class ConcurrencyError(DomainException):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(
            message=message,
            error_code="CONCURRENCY_CONFLICT",
            details=details,
            retryable=True,
            http_status_code=409,
        )


class VerificationError(DomainException):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(
            message=message,
            error_code="VERIFICATION_FAILURE",
            details=details,
            retryable=False,
            http_status_code=422,
        )


class IdempotencyConflictError(DomainException):
    """Raised when an idempotency key is reused with a different request payload (F-014)."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(
            message=message,
            error_code="IDEMPOTENCY_KEY_CONFLICT",
            details=details,
            retryable=False,
            http_status_code=409,
        )


class IdempotencyInProgressError(DomainException):
    """Raised when a concurrent request with the same idempotency key is still executing."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(
            message=message,
            error_code="IDEMPOTENCY_REQUEST_IN_PROGRESS",
            details=details,
            retryable=True,
            http_status_code=409,
        )


# ---------------------------------------------------------------------------
# Layer 3 topology error taxonomy (see prompt1_v1.md "Error Taxonomy")
# ---------------------------------------------------------------------------


class TopologyNotConfiguredError(DomainException):
    """Tenant/application has no topology provider configured. Non-retryable."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(
            message=message,
            error_code="TOPOLOGY_NOT_CONFIGURED",
            details=details,
            retryable=False,
            http_status_code=409,
        )


class TopologySnapshotNotReadyError(DomainException):
    """Revision snapshot is PENDING/INGESTING/FAILED. Retryable only while ingesting."""

    def __init__(
        self, message: str, retryable: bool = True, details: dict[str, Any] | None = None
    ) -> None:
        super().__init__(
            message=message,
            error_code="TOPOLOGY_SNAPSHOT_NOT_READY",
            details=details,
            retryable=retryable,
            http_status_code=409,
        )


class TopologyNodeNotFoundError(DomainException):
    """No AST node contains the requested source location. Non-retryable."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(
            message=message,
            error_code="TOPOLOGY_NODE_NOT_FOUND",
            details=details,
            retryable=False,
            http_status_code=404,
        )


class TopologyAmbiguousMatchError(DomainException):
    """Multiple equally specific nodes matched a source location. Non-retryable."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(
            message=message,
            error_code="TOPOLOGY_AMBIGUOUS_MATCH",
            details=details,
            retryable=False,
            http_status_code=409,
        )


class TopologyAccessDeniedError(DomainException):
    """Tenant lacks the capability/authorization for this repository/operation. Non-retryable."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(
            message=message,
            error_code="TOPOLOGY_ACCESS_DENIED",
            details=details,
            retryable=False,
            http_status_code=403,
        )


class TopologySchemaMismatchError(DomainException):
    """Ingestion payload schema/parser version is incompatible. Non-retryable."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(
            message=message,
            error_code="TOPOLOGY_SCHEMA_MISMATCH",
            details=details,
            retryable=False,
            http_status_code=422,
        )


class TopologyProviderUnavailableError(DomainException):
    """Transient Neo4j/provider failure. Retryable with bounded attempts."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(
            message=message,
            error_code="TOPOLOGY_PROVIDER_UNAVAILABLE",
            details=details,
            retryable=True,
            http_status_code=503,
        )


class AttributionInconclusiveError(DomainException):
    """Evidence cannot distinguish defect from misuse. Non-retryable investigation
    result — not an infrastructure failure. Callers should generally catch this
    and record an INCONCLUSIVE result rather than treating it as an error."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(
            message=message,
            error_code="ATTRIBUTION_INCONCLUSIVE",
            details=details,
            retryable=False,
            http_status_code=422,
        )


# Aliases for API layer compatibility
ValidationError = DomainValidationException
EntityNotFoundError = EvidenceNotFoundException
UnauthorizedError = SecurityPolicyViolationException
ConflictError = ConcurrencyError
