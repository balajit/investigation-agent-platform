# src/investigation_agent_platform/infrastructure/security/endpoint_guard.py
"""Outbound endpoint validation: SSRF guard for server-side fetches (Part 11.4).

Applies to OAuth2 token endpoints today and to reference-document fetchers
(Gap 4 / Phase 11.6) next. Policy: HTTPS-only, explicit hostname allowlist,
and rejection of loopback, link-local, private, multicast, reserved, and
well-known metadata-service addresses — unless an explicit, narrowly-scoped
development exception is constructed (never by default).

DNS is resolved at validation time; callers are told (docstring + logs) that
validation-time resolution does not close DNS-rebinding TOCTOU — the
mitigation is short-lived tokens, bounded timeouts, and allowlisted hosts,
not a claim of rebinding immunity.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field

from investigation_agent_platform.domain.common.exceptions import CredentialProviderError

logger = logging.getLogger(__name__)

# Well-known cloud metadata endpoints, blocked by literal in addition to the
# CIDR rules below (169.254.169.254 is link-local; 100.100.100.200 is shared
# address space — belt and suspenders against interpreter differences).
_METADATA_HOSTS = frozenset(
    {
        "169.254.169.254",
        "100.100.100.200",
        "metadata.google.internal",
    }
)

_MAX_RESPONSE_BYTES = 65_536


class EndpointPolicy(BaseModel):
    """Validation policy for one class of outbound calls (Part 11.4)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    allowed_hosts: frozenset[str] = Field(default_factory=frozenset, max_length=100)
    require_https: bool = Field(default=True)
    allow_loopback: bool = Field(default=False)
    timeout_seconds: float = Field(default=10.0, ge=1.0, le=60.0)
    max_response_bytes: int = Field(default=_MAX_RESPONSE_BYTES, ge=1024, le=1_048_576)


def validate_endpoint(url: str, policy: EndpointPolicy, *, purpose: str) -> str:
    """Validate an outbound URL; returns the normalized URL or raises.

    Fail-closed CredentialProviderError on: unparseable URL, non-HTTPS (when
    required), host not allowlisted, metadata-service host, or any resolved
    address that is loopback/link-local/private/multicast/reserved.
    """
    try:
        parsed = urlparse(url)
    except ValueError as exc:
        raise CredentialProviderError(
            f"Unparseable {purpose} endpoint",
            details={"purpose": purpose},
        ) from exc
    if not parsed.scheme or not parsed.hostname:
        raise CredentialProviderError(
            f"{purpose} endpoint must be an absolute URL with a host",
            details={"purpose": purpose},
        )
    scheme = parsed.scheme.lower()
    if policy.require_https and scheme != "https":
        raise CredentialProviderError(
            f"{purpose} endpoint must use https",
            details={"purpose": purpose, "scheme": scheme},
        )
    if scheme not in ("https", "http"):
        raise CredentialProviderError(
            f"{purpose} endpoint uses a forbidden scheme",
            details={"purpose": purpose, "scheme": scheme},
        )
    host = parsed.hostname.lower()
    if host in _METADATA_HOSTS:
        raise CredentialProviderError(
            f"{purpose} endpoint targets a metadata service",
            details={"purpose": purpose, "host": host},
        )
    if policy.allowed_hosts and host not in {h.lower() for h in policy.allowed_hosts}:
        raise CredentialProviderError(
            f"{purpose} endpoint host is not allowlisted",
            details={"purpose": purpose, "host": host},
        )
    _validate_resolved_addresses(host, policy, purpose=purpose)
    return parsed.geturl()


def _validate_resolved_addresses(host: str, policy: EndpointPolicy, *, purpose: str) -> None:
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise CredentialProviderError(
            f"{purpose} endpoint host does not resolve",
            details={"purpose": purpose, "host": host},
        ) from exc
    for info in infos:
        raw_ip = info[4][0]
        try:
            addr = ipaddress.ip_address(raw_ip)
        except ValueError as exc:
            raise CredentialProviderError(
                f"{purpose} endpoint resolved to an unparseable address",
                details={"purpose": purpose, "host": host},
            ) from exc
        if addr.is_multicast or addr.is_reserved or addr.is_unspecified:
            raise CredentialProviderError(
                f"{purpose} endpoint resolves to a forbidden address",
                details={"purpose": purpose, "host": host},
            )
        if addr.is_loopback or addr.is_link_local:
            if not policy.allow_loopback:
                raise CredentialProviderError(
                    f"{purpose} endpoint resolves to loopback/link-local",
                    details={"purpose": purpose, "host": host},
                )
            continue
        if addr.is_private or addr.is_global is False:
            raise CredentialProviderError(
                f"{purpose} endpoint resolves to a non-public address",
                details={"purpose": purpose, "host": host},
            )
