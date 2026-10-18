# src/investigation_agent_platform/ports/security/outbound_credentials.py
"""Outbound credential provider port (Part 11.4).

Some evidence/tool providers need a platform-managed, auto-refreshing
outbound bearer token — distinct from the inbound JWT that authenticates the
calling agent. Implementations cache per (tenant, application, provider) and
never return expired tokens.
"""

from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from investigation_agent_platform.domain.common.extension import CapabilityScope


class AccessToken(BaseModel):
    """A valid outbound bearer token (Part 11.4)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    token: SecretStr
    token_type: str = Field(default="Bearer", min_length=1, max_length=32)
    expires_at: datetime
    provider_id: str = Field(..., min_length=1, max_length=128)

    def is_usable(self, at: datetime | None = None) -> bool:
        """True when the token has more than 60s of validity remaining."""
        now = at or datetime.now(UTC)
        return (self.expires_at - now).total_seconds() > 60


@runtime_checkable
class OutboundCredentialProvider(Protocol):
    """Scoped, auto-refreshing outbound token source (Part 11.4)."""

    async def get_token(self, scope: CapabilityScope, provider_id: str) -> AccessToken:
        """Return a valid token, refreshing if fewer than 60s remain.

        Never blocks on a full re-auth when the cached token is still
        usable. Raises CredentialProviderError on misconfiguration,
        endpoint refusal, or terminal authentication failure.
        """
        ...
