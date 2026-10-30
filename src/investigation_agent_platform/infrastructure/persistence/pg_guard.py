# src/investigation_agent_platform/infrastructure/persistence/pg_guard.py
"""Disposable-PostgreSQL guard: refuse migration/test traffic to shared databases.

Alembic resolves its target from an inherited URL and the opt-in role tests do
the same, so a single wrong environment variable can point destructive or
version-mutating operations at a persistent database (see the 026 stamp
incident). This module is the single decision point: pure verdict function
(no I/O, no connections) plus a thin enforcement wrapper. Callers:
``migrations/env.py`` (write commands only) and the onboarding opt-in test
fixtures.
"""

from __future__ import annotations

import ipaddress
import os
import sys
from dataclasses import dataclass
from enum import StrEnum
from typing import Final
from urllib.parse import urlparse

#: Loopback hosts considered local. Anything else is a remote database.
_LOOPBACK_HOSTS: Final = frozenset({"localhost", "127.0.0.1", "::1"})

#: Loopback ports that are never disposable endpoints. Default PostgreSQL
#: plus the project's dev database. Amend with a review + test update.
PROTECTED_LOOPBACK_PORTS: Final = frozenset({5432, 5452})

_POSTGRES_SCHEMES: Final = frozenset(
    {"postgresql", "postgresql+psycopg", "postgresql+asyncpg", "postgres"}
)

OVERRIDE_ENV_VAR: Final = "IAP_PG_GUARD_OVERRIDE"


class Verdict(StrEnum):
    """Guard outcome. Anything but ALLOW blocks the operation."""

    ALLOW = "allow"
    DENY_REMOTE = "deny_remote"
    DENY_PROTECTED = "deny_protected"
    DENY_UNPARSABLE = "deny_unparsable"


@dataclass(frozen=True)
class GuardDecision:
    """Verdict plus a human-readable reason. Never carries credentials."""

    verdict: Verdict
    reason: str


def _is_loopback(host: str) -> bool:
    normalized = host.strip().lower().rstrip(".")
    if normalized in _LOOPBACK_HOSTS:
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _host_port(url: str) -> str:
    try:
        parsed = urlparse(url)
    except ValueError:
        return "<unparsable-url>"
    host = (parsed.hostname or "<missing-host>").lower()
    port = parsed.port or 5432
    return f"{host}:{port}"


def verdict_for_url(url: str) -> GuardDecision:
    """Pure verdict for one database URL. Makes no connections."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return GuardDecision(Verdict.DENY_UNPARSABLE, "database URL is not parseable")
    if not parsed.scheme or parsed.scheme.lower() not in _POSTGRES_SCHEMES:
        return GuardDecision(
            Verdict.DENY_UNPARSABLE,
            f"unsupported database scheme {parsed.scheme!r}; expected postgresql",
        )
    host = (parsed.hostname or "").lower()
    if not host:
        return GuardDecision(Verdict.DENY_UNPARSABLE, "database URL has no host")
    if not _is_loopback(host):
        return GuardDecision(
            Verdict.DENY_REMOTE,
            f"refusing remote database host {host!r}; disposable verification "
            "databases must be loopback",
        )
    port = parsed.port or 5432
    if port in PROTECTED_LOOPBACK_PORTS:
        return GuardDecision(
            Verdict.DENY_PROTECTED,
            f"refusing loopback port {port} (protected: persistent dev/default "
            "database); run a disposable container on another port",
        )
    return GuardDecision(Verdict.ALLOW, "loopback non-protected port")


def _override_reason() -> str | None:
    value = os.environ.get(OVERRIDE_ENV_VAR, "").strip()
    return value or None


def require_disposable(url: str, *, purpose: str) -> None:
    """Allow disposable loopback targets; fail closed on anything else.

    Raises RuntimeError with an actionable message (endpoint without
    credentials, why it was refused, the disposable flow, the override
    form). An explicit non-empty ``IAP_PG_GUARD_OVERRIDE`` reason bypasses
    the host/port rules for sanctioned use; garbage URLs are never bypassed.
    """
    override = _override_reason()
    if override is not None:
        try:
            parsed = urlparse(url)
            scheme_ok = parsed.scheme.lower() in _POSTGRES_SCHEMES and bool(parsed.hostname)
        except ValueError:
            scheme_ok = False
        if not scheme_ok:
            raise RuntimeError(
                f"refusing {purpose}: database URL is not a usable postgresql "
                f"URL (override {OVERRIDE_ENV_VAR} does not bypass parsing)"
            )
        print(
            f"pg_guard override accepted for {purpose}: {override}",
            file=sys.stderr,
        )
        return
    decision = verdict_for_url(url)
    if decision.verdict != Verdict.ALLOW:
        endpoint = _host_port(url)
        raise RuntimeError(
            f"refusing {purpose} against {endpoint}: {decision.reason}. "
            "Run a disposable container (e.g. docker run --rm -p 5544:5432 "
            "postgres:16) and point IAP_TEST_PG_URL / -x url at it, or set "
            f"{OVERRIDE_ENV_VAR}='<reason>' for sanctioned use (never export it)."
        )


def command_is_write(command_name: str | None) -> bool:
    """Whether an alembic command may mutate database state.

    Unknown commands default to gated (fail closed). Read-only commands
    (current, heads, history, show, check, list_templates, init) are exempt.
    """
    if command_name is None:
        return True
    return command_name in {"upgrade", "downgrade", "stamp"}
