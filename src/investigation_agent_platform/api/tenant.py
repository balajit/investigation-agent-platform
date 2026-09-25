# src/investigation_agent_platform/api/tenant.py
"""Tenant and principal authentication.

Identity is derived exclusively from a cryptographically verified JWT
(signature, issuer, audience, expiry, algorithm allow-list all checked).
``X-Tenant-ID`` is accepted only as an optional consistency check against the
verified tenant claim — it is never treated as authority. In non-production
environments where no JWKS/issuer is configured, ``X-Tenant-ID`` may be used
alone as a development convenience, but this path is refused entirely when
``IAP_ENVIRONMENT=production`` (see F-003 in the adversarial review).
"""

from __future__ import annotations

import functools
import logging
import os
from dataclasses import dataclass
from typing import Any

import jwt
from fastapi import Depends, Header, HTTPException, Request, status
from jwt import PyJWKClient

logger = logging.getLogger(__name__)


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
    def is_production(self) -> bool:
        return self.environment.lower() == "production"

    @property
    def jwt_verification_enabled(self) -> bool:
        return bool(self.jwks_url and self.issuer)


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


async def get_verified_identity(
    request: Request,
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-ID"),
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> VerifiedIdentity:
    identity = await _resolve_verified_identity(x_tenant_id, authorization)
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

    if settings.is_production:
        # Production must never authenticate off an unverified header alone.
        if not settings.jwt_verification_enabled:
            raise AuthConfigurationError(
                "Production requires IAP_AUTH_JWKS_URL and IAP_AUTH_ISSUER to be configured"
            )
        if not bearer_token:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing or invalid Authorization bearer token",
            )
        try:
            claims = _verify_jwt(bearer_token, settings)
        except AuthConfigurationError:
            raise
        except jwt.PyJWTError as exc:
            logger.warning("JWT verification failed", extra={"reason": type(exc).__name__})
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid authentication token"
            ) from exc

        tenant = _tenant_claim(claims)
        if tenant is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token does not contain a tenant claim",
            )
        principal = claims.get("sub")
        if not isinstance(principal, str) or not principal.strip():
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Token does not contain a subject"
            )
        if header_tenant is not None and header_tenant != tenant:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Tenant mismatch between X-Tenant-ID and verified token",
            )
        return VerifiedIdentity(tenant_id=tenant, principal_id=principal.strip())

    # Non-production: prefer verified JWT when JWKS is configured; otherwise
    # fall back to X-Tenant-ID as a development convenience only.
    if bearer_token and settings.jwt_verification_enabled:
        try:
            claims = _verify_jwt(bearer_token, settings)
        except jwt.PyJWTError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid authentication token"
            ) from exc
        tenant = _tenant_claim(claims) or header_tenant
        principal = claims.get("sub")
        if tenant is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing tenant")
        if header_tenant is not None and header_tenant != tenant:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Tenant mismatch between X-Tenant-ID and verified token",
            )
        return VerifiedIdentity(
            tenant_id=tenant,
            principal_id=str(principal).strip() if principal else "dev-principal",
        )

    if header_tenant is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing X-Tenant-ID")
    return VerifiedIdentity(tenant_id=header_tenant, principal_id="dev-principal")


async def require_tenant(identity: VerifiedIdentity = Depends(get_verified_identity)) -> str:  # noqa: B008 - FastAPI Depends idiom
    """FastAPI dependency returning the verified tenant ID for the request."""
    return identity.tenant_id


async def require_principal(identity: VerifiedIdentity = Depends(get_verified_identity)) -> str:  # noqa: B008 - FastAPI Depends idiom
    """FastAPI dependency returning the verified principal ID for the request."""
    return identity.principal_id
