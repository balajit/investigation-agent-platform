# src/investigation_agent_platform/api/tenant.py
"""Tenant authorization — hard E2E enforcement (Phase 2).

NOTE: X-Tenant-ID header-only authentication is for local/dev only.
In production this must be replaced by JWT/signature verification where
the tenant claim is extracted from a verified token. An optional
Authorization: Bearer <JWT> check is performed below when the header
is present — if the JWT tenant claim mismatches X-Tenant-ID the request
is rejected. X-Tenant-ID remains primary until JWT issuance is wired.
"""

from __future__ import annotations

import base64
import json

from fastapi import Header, HTTPException, status


def _extract_tenant_from_jwt(token: str) -> str | None:
    """Best-effort decode of JWT payload tenant claim without signature verify.

    Returns None if token is not a well-formed JWT or has no tenant claim.
    Signature verification is intentionally omitted for dev; production
    must verify via JWKS/issuer.
    """
    parts = token.split(".")
    if len(parts) != 3:
        return None
    payload_b64 = parts[1]
    # Pad base64url
    padding = "=" * (-len(payload_b64) % 4)
    try:
        decoded = base64.urlsafe_b64decode(payload_b64 + padding)
        payload = json.loads(decoded)
    except (ValueError, json.JSONDecodeError, Exception):  # noqa: BLE001 - best-effort dev JWT decode
        return None
    if not isinstance(payload, dict):
        return None
    tenant = payload.get("tenant_id") or payload.get("tenant") or payload.get("tid")
    if isinstance(tenant, str) and tenant:
        return tenant
    return None


async def require_tenant(
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> str:
    if not x_tenant_id or x_tenant_id.strip() == "" or x_tenant_id == "anonymous":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing X-Tenant-ID")
    # Normalize
    tenant = x_tenant_id.strip()
    # Optional JWT validation: if Authorization Bearer token present, ensure tenant claim matches header.
    if authorization is not None and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
        if token:
            jwt_tenant = _extract_tenant_from_jwt(token)
            # If JWT contains a tenant claim, enforce match (prevents header spoofing when JWT is issued)
            if jwt_tenant is not None and jwt_tenant != tenant:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Tenant mismatch between X-Tenant-ID and Authorization token",
                )
    return tenant
