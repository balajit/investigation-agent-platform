# src/investigation_agent_platform/application/onboarding_agent/contracts/registry.py
"""Support-registry contract shapes (agent design §4.7, AG-05a).

The registry itself is owned by the platform (DEP-02b tables); this module
defines only the shapes the agent reads. Strategy vocabulary is closed here;
disposition vocabulary is platform-owned (D2.1/F-11) and referenced, not
redefined.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class SupportStrategy(StrEnum):
    """How the platform handles an estate capability. Closed vocabulary."""

    NATIVE = "NATIVE"
    GENERIC = "GENERIC"
    BRIDGE = "BRIDGE"
    UNSUPPORTED = "UNSUPPORTED"


class TrustState(StrEnum):
    """Verification state of a bridge or registry entry."""

    UNVERIFIED = "UNVERIFIED"
    PROVISIONAL = "PROVISIONAL"
    TRUSTED = "TRUSTED"


class RegistryEntry(BaseModel):
    """One row of the support registry as seen by the agent (read side)."""

    model_config = ConfigDict(extra="forbid")

    capability_key: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=32)
    strategy: SupportStrategy
    limits: str | None = Field(default=None, max_length=512)
    trust_state: TrustState = TrustState.UNVERIFIED
    evidence_requirements: str | None = Field(default=None, max_length=1024)
    owner: str = Field(min_length=1, max_length=256)
