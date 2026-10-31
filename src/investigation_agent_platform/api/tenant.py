# src/investigation_agent_platform/api/tenant.py
"""Tenant and principal authentication.

Identity is derived exclusively from a cryptographically verified JWT
(signature, issuer, audience, expiry, algorithm allow-list all checked).
``X-Tenant-ID`` is accepted only as an optional consistency check against the
verified tenant claim — it is never treated as authority. ``X-Tenant-ID`` may
be used alone only in an explicitly named development environment
(``DEV_ENVIRONMENTS``) AND only while no JWKS/issuer is configured. Every other
environment name (``production``, ``prod``, ``staging``, ``live``, a typo) takes
the strict verified-token path, and configuring JWT verification turns the
header-only path off in every environment (F-003; review R3).
"""

from __future__ import annotations

import functools
import logging
import os
from dataclasses import dataclass
from typing import Any

import jwt
from fastapi import Depends, Header, Request, status
from jwt import PyJWKClient

from investigation_agent_platform.api.errors import ApiError
from investigation_agent_platform.api.rate_limit import check_rate_limit as _check_rate_limit
from investigation_agent_platform.api.rate_limit import rate_limit_enabled as _rate_limit_enabled

logger = logging.getLogger(__name__)


# Environments where a bare X-Tenant-ID header may identify the caller. An
# allowlist, not a "production" denylist: any other value - including
# spellings like "prod" or "live" - is treated as production-grade.
DEV_ENVIRONMENTS = frozenset({"development", "dev", "local", "test", "testing"})


class AuthConfigurationError(RuntimeError):
    """Raised when production authentication cannot be safely configured."""


@dataclass(frozen=True)
class AuthSettings:
    environment: str
    jwks_url: str | None
    issuer: str | None
    audience: str | None
    algorithms: tuple[str, ...]

    @property
    def jwt_verification_enabled(self) -> bool:
        return bool(self.jwks_url and self.issuer)

    @property
    def header_auth_allowed(self) -> bool:
        """True only for a named dev environment with JWT verification off."""
        return (
            self.environment.strip().lower() in DEV_ENVIRONMENTS
            and not self.jwt_verification_enabled
        )


def _load_auth_settings() -> AuthSettings:
    algorithms_raw = os.environ.get("IAP_AUTH_ALGORITHMS", "RS256")
    return AuthSettings(
        environment=os.environ.get("IAP_ENVIRONMENT", "development"),
        jwks_url=os.environ.get("IAP_AUTH_JWKS_URL") or None,
        issuer=os.environ.get("IAP_AUTH_ISSUER") or None,
        audience=os.environ.get("IAP_AUTH_AUDIENCE") or None,
        algorithms=tuple(a.strip() for a in algorithms_raw.split(",") if a.strip()),
    )


@functools.lru_cache(maxsize=1)
def _get_jwk_client(jwks_url: str) -> PyJWKClient:
    return PyJWKClient(jwks_url)


@dataclass(frozen=True)
class VerifiedIdentity:
    tenant_id: str
    principal_id: str
    #: Role claims from the verified token (``roles``/``groups`` list or a
    #: single ``role`` string; empty for header-only dev identities and for
    #: tokens that carry none). Never invented — only what the token states.
    roles: tuple[str, ...] = ()


@dataclass(frozen=True)
class VerifiedPrincipal:
    """Verified subject identity; tenant claim is context, never entitlement."""

    principal_id: str


def _verify_jwt(token: str, settings: AuthSettings) -> dict[str, Any]:
    """Verify signature, issuer, audience, expiry, and algorithm allow-list.

    Raises ``jwt.PyJWTError`` subclasses on any verification failure. Callers
    must not swallow these into a permissive fallback.
    """
    if not settings.jwks_url:
        raise AuthConfigurationError("IAP_AUTH_JWKS_URL is not configured")
    if not settings.issuer:
        raise AuthConfigurationError("IAP_AUTH_ISSUER is not configured")
    if not settings.algorithms:
        raise AuthConfigurationError("IAP_AUTH_ALGORITHMS must not be empty")
    if "none" in {a.lower() for a in settings.algorithms}:
        raise AuthConfigurationError("alg=none is never permitted")

    signing_key = _get_jwk_client(settings.jwks_url).get_signing_key_from_jwt(token)
    return jwt.decode(
        token,
        signing_key.key,
        algorithms=list(settings.algorithms),
        issuer=settings.issuer,
        audience=settings.audience,
        options={
            "require": ["exp", "iat", "sub"],
            "verify_signature": True,
            "verify_exp": True,
            "verify_iat": True,
            "verify_aud": settings.audience is not None,
            "verify_iss": True,
        },
    )


def _tenant_claim(claims: dict[str, Any]) -> str | None:
    tenant = claims.get("tenant_id") or claims.get("tenant") or claims.get("tid")
    if isinstance(tenant, str) and tenant.strip():
        return tenant.strip()
    return None


async def resolve_identity_from_headers(
    x_tenant_id: str | None, authorization: str | None
) -> VerifiedIdentity:
    """Public header-based identity resolution for non-FastAPI transports.

    Same verification as the FastAPI dependency below (JWT allow-list,
    issuer/audience/expiry, production fail-closed); used by the MCP HTTP
    transport, which cannot use FastAPI ``Depends`` injection.
    """
    return await _resolve_verified_identity(x_tenant_id, authorization)


async def get_verified_identity(
    request: Request,
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-ID"),
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> VerifiedIdentity:
    identity = await _resolve_verified_identity(x_tenant_id, authorization)
    await _enforce_verified_rate_limit(request, identity)
    # F-017: only now — after verification has actually succeeded — is the
    # authenticated tenant recorded on request state for downstream logging.
    request.state.authenticated_tenant_id = identity.tenant_id
    return identity


async def _resolve_verified_identity(
    x_tenant_id: str | None,
    authorization: str | None,
) -> VerifiedIdentity:
    settings = _load_auth_settings()

    header_tenant = x_tenant_id.strip() if x_tenant_id else None
    if header_tenant in ("", "anonymous"):
        header_tenant = None

    bearer_token: str | None = None
    if authorization and authorization.lower().startswith("bearer "):
        bearer_token = authorization[len("bearer ") :].strip() or None

    if not settings.header_auth_allowed:
        # Anything that is not a named dev environment without JWT
        # verification must authenticate with a verified token and never
        # from an unverified header alone.
        if not settings.jwt_verification_enabled:
            raise AuthConfigurationError(
                f"Environment {settings.environment!r} requires IAP_AUTH_JWKS_URL and "
                "IAP_AUTH_ISSUER to be configured (header-only auth is dev-only)"
            )
        if not bearer_token:
            raise ApiError(
                code="AUTH_TOKEN_REQUIRED",
                http_status=status.HTTP_401_UNAUTHORIZED,
                message="Missing or invalid Authorization bearer token",
            )
        try:
            claims = _verify_jwt(bearer_token, settings)
        except AuthConfigurationError:
            raise
        except jwt.PyJWTError as exc:
            logger.warning("JWT verification failed", extra={"reason": type(exc).__name__})
            raise ApiError(
                code="AUTH_TOKEN_INVALID",
                http_status=status.HTTP_401_UNAUTHORIZED,
                message="Invalid authentication token",
            ) from exc

        tenant = _tenant_claim(claims)
        if tenant is None:
            raise ApiError(
                code="AUTH_TENANT_CLAIM_MISSING",
                http_status=status.HTTP_401_UNAUTHORIZED,
                message="Token does not contain a tenant claim",
            )
        principal = claims.get("sub")
        if not isinstance(principal, str) or not principal.strip():
            raise ApiError(
                code="AUTH_SUBJECT_MISSING",
                http_status=status.HTTP_401_UNAUTHORIZED,
                message="Token does not contain a subject",
            )
        roles: list[str] = []
        for key in ("roles", "groups"):
            claimed = claims.get(key)
            if isinstance(claimed, list):
                roles.extend(str(r) for r in claimed if str(r or "").strip())
        single = claims.get("role")
        if isinstance(single, str) and single.strip():
            roles.append(single.strip())
        if header_tenant is not None and header_tenant != tenant:
            raise ApiError(
                code="TENANT_MISMATCH",
                http_status=status.HTTP_403_FORBIDDEN,
                message="Tenant mismatch between X-Tenant-ID and verified token",
            )
        return VerifiedIdentity(tenant_id=tenant, principal_id=principal.strip(), roles=tuple(roles))

    # Named dev environment with JWT verification off: the header alone
    # identifies the caller (development convenience only).
    if header_tenant is None:
        raise ApiError(
            code="AUTH_TENANT_REQUIRED",
            http_status=status.HTTP_401_UNAUTHORIZED,
            message="Missing X-Tenant-ID",
        )
    return VerifiedIdentity(tenant_id=header_tenant, principal_id="dev-principal")


async def _enforce_verified_rate_limit(request: Request, identity: VerifiedIdentity) -> None:
    """Post-auth rate limit (AD1): per-tenant + per-principal bucket.

    Skipped when the limiter is disabled. Fails open on store errors
    (availability over strictness). Raises 429 with the verified
    identity's own bucket when exhausted — a spoofed caller can never
    spend another tenant's quota here because the key comes from the
    verified identity, not a header.
    """
    if not _rate_limit_enabled():
        return
    store = getattr(getattr(request, "app", None), "state", None)
    store = getattr(store, "rate_limit_store", None) if store is not None else None
    if store is None:
        return
    try:
        path = str(getattr(getattr(request, "url", None), "path", "") or "")
    except Exception:
        path = ""
    allowed, retry_after, klass = await _check_rate_limit(
        store, identity.tenant_id, identity.principal_id, path or "/api/"
    )
    if not allowed:
        raise ApiError(
            code="RATE_LIMITED",
            http_status=status.HTTP_429_TOO_MANY_REQUESTS,
            message=f"Rate limit exceeded for {klass}; retry later.",
            headers={"Retry-After": str(retry_after)},
        )


async def require_tenant(identity: VerifiedIdentity = Depends(get_verified_identity)) -> str:  # noqa: B008 - FastAPI Depends idiom
    """FastAPI dependency returning the verified tenant ID for the request."""
    return identity.tenant_id


async def require_principal(identity: VerifiedIdentity = Depends(get_verified_identity)) -> str:  # noqa: B008 - FastAPI Depends idiom
    """FastAPI dependency returning the verified principal ID for the request."""
    return identity.principal_id


async def require_approver_role(
    identity: VerifiedIdentity = Depends(get_verified_identity),  # noqa: B008 - FastAPI Depends idiom
) -> str:
    """FastAPI dependency gating human-in-the-loop approvals (R12D).

    In dev (header-auth allowed) any verified identity may approve, as
    today. Elsewhere the ``IAP_AUTH_APPROVER_ROLE`` role must be
    configured *and* present on the verified token; otherwise 403.
    The approver-role contract itself is an open owner decision, so an
    unset role fails closed rather than guessing.
    """
    settings = _load_auth_settings()
    if settings.header_auth_allowed:
        return "dev-approver"
    required = (os.environ.get("IAP_AUTH_APPROVER_ROLE", "") or "").strip()
    if not required:
        raise ApiError(
            code="APPROVER_ROLE_UNCONFIGURED",
            http_status=status.HTTP_403_FORBIDDEN,
            message="Approvals are disabled: no approver role is configured",
        )
    held = {str(r or "").strip() for r in (getattr(identity, "roles", None) or ())}
    if required not in held:
        raise ApiError(
            code="APPROVER_ROLE_REQUIRED",
            http_status=status.HTTP_403_FORBIDDEN,
            message="Approving this action requires the approver role",
        )
    return required


async def get_verified_principal(
    request: Request,
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-ID"),
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> VerifiedPrincipal:
    """Resolve verified JWT ``sub`` without requiring a tenant claim.

    Platform grant administration is principal-scoped and must not depend on a
    caller-controlled tenant header. This dependency still requires a verified
    JWT; non-production header-only identity is not accepted here.
    """
    settings = _load_auth_settings()
    bearer_token = (
        authorization[len("bearer ") :].strip()
        if authorization and authorization.lower().startswith("bearer ")
        else None
    )
    if not bearer_token or not settings.jwt_verification_enabled:
        raise ApiError(
            code="AUTH_TOKEN_REQUIRED",
            http_status=status.HTTP_401_UNAUTHORIZED,
            message="A verified bearer token is required",
        )
    try:
        claims = _verify_jwt(bearer_token, settings)
    except AuthConfigurationError:
        raise
    except jwt.PyJWTError as exc:
        raise ApiError(
            code="AUTH_TOKEN_INVALID",
            http_status=status.HTTP_401_UNAUTHORIZED,
            message="Invalid authentication token",
        ) from exc
    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject.strip():
        raise ApiError(
            code="AUTH_SUBJECT_MISSING",
            http_status=status.HTTP_401_UNAUTHORIZED,
            message="Token does not contain a subject",
        )
    request.state.verified_principal_id = subject.strip()
    return VerifiedPrincipal(principal_id=subject.strip())
