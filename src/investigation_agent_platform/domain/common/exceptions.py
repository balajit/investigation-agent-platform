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


class ExecutionError(DomainException):
    def __init__(self, message: str, details: dict[str, Any] | None = None, retryable: bool = True) -> None:
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


# Aliases for API layer compatibility
ValidationError = DomainValidationException
EntityNotFoundError = EvidenceNotFoundException
UnauthorizedError = SecurityPolicyViolationException
ConflictError = ConcurrencyError