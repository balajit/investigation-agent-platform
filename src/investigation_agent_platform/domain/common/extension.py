# src/investigation_agent_platform/domain/common/extension.py
"""Extension contracts: scope, plugin manifests, versions, secrets (Part 11.1).

Every future adapter (reference fetchers, credential providers, report
renderers, LLM gateways, background-job kinds) is described by a
``PluginManifest`` and resolved through a typed registry. Profiles select
registered plugins by id and supply configuration validated against the
manifest's published JSON Schema — tenant input can never import arbitrary
code or name arbitrary secrets.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class CapabilityScope(BaseModel):
    """Tenant/application scope carried by every extension call and cache key."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: str = Field(..., min_length=1, max_length=128)
    application_id: str | None = Field(default=None, max_length=128)


class PluginKind(StrEnum):
    """Closed set of extension points (Part 11.1)."""

    REFERENCE_FETCHER = "REFERENCE_FETCHER"
    CREDENTIAL_PROVIDER = "CREDENTIAL_PROVIDER"
    REPORT_RENDERER = "REPORT_RENDERER"
    LLM_COMPLETION = "LLM_COMPLETION"
    LLM_STREAMING = "LLM_STREAMING"
    BACKGROUND_JOB_KIND = "BACKGROUND_JOB_KIND"


class PluginManifest(BaseModel):
    """Static descriptor for one platform-installed plugin implementation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    plugin_id: str = Field(..., min_length=1, max_length=128)
    plugin_kind: PluginKind
    contract_version: str = Field(..., min_length=1, max_length=32)
    implementation_version: str = Field(..., min_length=1, max_length=32)
    config_schema_version: str = Field(..., min_length=1, max_length=32)
    capabilities: frozenset[str] = Field(default_factory=frozenset, max_length=32)
    config_schema: dict[str, Any] = Field(default_factory=dict)


class PluginHealth(BaseModel):
    """Point-in-time health for one plugin instance (no secrets/endpoints)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    plugin_id: str = Field(..., min_length=1, max_length=128)
    status: str = Field(..., min_length=1, max_length=32)
    message: str = Field(default="", max_length=512)
    checked_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class SecretReference(BaseModel):
    """Reference to a secret by name — never carries the value.

    Tenant profiles may only name secrets through this type. ``provider``
    selects the resolution backend (``env`` reads an allowlisted environment
    variable, ``manager`` a configured secret manager); ``name`` must match
    the platform's secret-name policy so profiles cannot exfiltrate arbitrary
    environment variables.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str = Field(..., min_length=1, max_length=32)
    name: str = Field(..., min_length=1, max_length=128, pattern=r"^[A-Z][A-Z0-9_]*$")


class PluginConfig(BaseModel):
    """A profile's selection of one registered plugin plus its configuration."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    plugin_id: str = Field(..., min_length=1, max_length=128)
    config_schema_version: str = Field(..., min_length=1, max_length=32)
    config: dict[str, Any] = Field(default_factory=dict)
    required: bool = Field(default=True)


class CompatibilityRange(BaseModel):
    """Inclusive contract-version range a registry accepts."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    min_version: str = Field(default="1.0", min_length=1, max_length=32)
    max_version: str = Field(default="1.999.999", min_length=1, max_length=32)

    def is_compatible(self, version: str) -> bool:
        """Same-major, within [min, max] version check (numeric segments)."""
        return (
            _parse_version(self.min_version)
            <= _parse_version(version)
            <= _parse_version(self.max_version)
            and _parse_version(version)[0] == _parse_version(self.min_version)[0]
        )


def _parse_version(version: str) -> tuple[int, ...]:
    cleaned = version.strip().lstrip("vV")
    parts: list[int] = []
    for segment in cleaned.split("."):
        digits = "".join(ch for ch in segment if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])
