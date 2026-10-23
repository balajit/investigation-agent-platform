# src/investigation_agent_platform/application/onboarding/catalog.py
"""Versioned capability catalog for tenant onboarding (Phase 3, design D2/D2.1).

The catalog is the common contract for intake validation, activation, status,
operations, and deletion: every supported onboarding capability is declared
with its owner, authorization, lifecycle states, readiness probe, retry
behavior, and deletion action. An adapter not present in the catalog cannot be
accepted from intake, and unknown policy data fails closed before any network
or filesystem side effect.

Disposition vocabulary is platform-owned (REQUIRED, OPTIONAL,
NOT_APPLICABLE, UNSUPPORTED); this module consumes it and defines nothing
parallel to the agent contracts.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

CATALOG_VERSION = "v0.1"
"""Current catalog version pinned by intake validation and readiness reports."""

V01_SERVICE_TIERS = ("standard",)
"""Service tiers configured for the v0.1 catalog. Single tier by design."""


class PolicyDisposition(StrEnum):
    """Closed policy vocabulary. Free-form status strings are invalid."""

    REQUIRED = "REQUIRED"
    OPTIONAL = "OPTIONAL"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    UNSUPPORTED = "UNSUPPORTED"


class CapabilityCatalogEntry(BaseModel):
    """One supported onboarding capability and its lifecycle contract."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    capability_key: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=32)
    source_class: str = Field(min_length=1, max_length=64)
    owner: str = Field(min_length=1, max_length=128)
    scope_level: str = Field(min_length=1, max_length=32)
    required_capability: str = Field(min_length=1, max_length=128)
    policy_by_tier: dict[str, PolicyDisposition] = Field(max_length=32)
    readiness_probe: str = Field(min_length=1, max_length=128)
    freshness_seconds: int = Field(ge=0)
    retry_policy_version: str = Field(min_length=1, max_length=32)
    deletion_action: str = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def _tiers_must_be_declared(self) -> CapabilityCatalogEntry:
        unknown = set(self.policy_by_tier) - set(V01_SERVICE_TIERS)
        if unknown:
            raise ValueError(f"policy references unconfigured tiers: {sorted(unknown)}")
        return self


class CapabilityCatalog(BaseModel):
    """Versioned set of catalog entries with cross-field validation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    catalog_version: str = Field(min_length=1, max_length=32)
    entries: tuple[CapabilityCatalogEntry, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_tier_coverage(self) -> CapabilityCatalog:
        seen: dict[str, int] = {}
        for entry in self.entries:
            seen[entry.capability_key] = seen.get(entry.capability_key, 0) + 1
            missing = [tier for tier in V01_SERVICE_TIERS if tier not in entry.policy_by_tier]
            if missing:
                raise ValueError(
                    f"capability {entry.capability_key!r} missing disposition for tiers: {missing}"
                )
        duplicates = sorted(key for key, count in seen.items() if count > 1)
        if duplicates:
            raise ValueError(f"duplicate capability keys: {duplicates}")
        return self

    def entry_for(self, capability_key: str) -> CapabilityCatalogEntry:
        """Return the entry for a capability, or raise a closed error."""
        for entry in self.entries:
            if entry.capability_key == capability_key:
                return entry
        raise KeyError(f"capability {capability_key!r} is not in catalog {self.catalog_version}")

    def disposition_for(self, capability_key: str, tier: str) -> PolicyDisposition:
        """Disposition for one capability/tier pair; unknown fails closed."""
        if tier not in V01_SERVICE_TIERS:
            raise ValueError(f"unknown service tier: {tier!r}")
        return self.entry_for(capability_key).policy_by_tier[tier]


def _lifecycle(
    capability_key: str,
    owner: str,
    required_capability: str,
    *,
    # Lifecycle operations are transition-guarded, not probe-gated: readiness
    # evaluates adapter evidence only. Activation itself remains a REQUIRED
    # gate owned by the tenant-registry transition.
    disposition: PolicyDisposition = PolicyDisposition.NOT_APPLICABLE,
) -> CapabilityCatalogEntry:
    return CapabilityCatalogEntry(
        capability_key=capability_key,
        version="v0.1",
        source_class="lifecycle-operation",
        owner=owner,
        scope_level="tenant",
        required_capability=required_capability,
        policy_by_tier={"standard": disposition},
        readiness_probe="lifecycle-guard",
        freshness_seconds=300,
        retry_policy_version="v0.1",
        deletion_action="RETAIN",
    )


V01_CATALOG = CapabilityCatalog(
    catalog_version=CATALOG_VERSION,
    entries=(
        CapabilityCatalogEntry(
            capability_key="repository",
            version="v0.1",
            source_class="repository",
            owner="profile-service",
            scope_level="application",
            required_capability="tenant:onboard",
            policy_by_tier={"standard": PolicyDisposition.REQUIRED},
            readiness_probe="sample-git-host:resolve",
            freshness_seconds=3600,
            retry_policy_version="v0.1",
            deletion_action="DETACH",
        ),
        CapabilityCatalogEntry(
            capability_key="logs",
            version="v0.1",
            source_class="telemetry",
            owner="telemetry-provider",
            scope_level="application",
            required_capability="tenant:configure",
            policy_by_tier={"standard": PolicyDisposition.OPTIONAL},
            readiness_probe="sample-git-host:resolve",
            freshness_seconds=3600,
            retry_policy_version="v0.1",
            deletion_action="REVOKE",
        ),
        CapabilityCatalogEntry(
            capability_key="traces",
            version="v0.1",
            source_class="telemetry",
            owner="telemetry-provider",
            scope_level="application",
            required_capability="tenant:configure",
            policy_by_tier={"standard": PolicyDisposition.OPTIONAL},
            readiness_probe="sample-git-host:resolve",
            freshness_seconds=3600,
            retry_policy_version="v0.1",
            deletion_action="REVOKE",
        ),
        CapabilityCatalogEntry(
            capability_key="metrics",
            version="v0.1",
            source_class="telemetry",
            owner="telemetry-provider",
            scope_level="application",
            required_capability="tenant:configure",
            policy_by_tier={"standard": PolicyDisposition.OPTIONAL},
            readiness_probe="sample-git-host:resolve",
            freshness_seconds=3600,
            retry_policy_version="v0.1",
            deletion_action="REVOKE",
        ),
        CapabilityCatalogEntry(
            capability_key="datasource",
            version="v0.1",
            source_class="datasource",
            owner="datasource-provider",
            scope_level="application",
            required_capability="tenant:configure",
            policy_by_tier={"standard": PolicyDisposition.REQUIRED},
            readiness_probe="sample-database-provider:connect",
            freshness_seconds=3600,
            retry_policy_version="v0.1",
            deletion_action="REVOKE",
        ),
        CapabilityCatalogEntry(
            capability_key="config_server",
            version="v0.1",
            source_class="config",
            owner="config-provider",
            scope_level="application",
            required_capability="tenant:configure",
            policy_by_tier={"standard": PolicyDisposition.NOT_APPLICABLE},
            readiness_probe="sample-database-provider:connect",
            freshness_seconds=3600,
            retry_policy_version="v0.1",
            deletion_action="REVOKE",
        ),
        CapabilityCatalogEntry(
            capability_key="knowledge_corpus",
            version="v0.1",
            source_class="knowledge",
            owner="knowledge-provider",
            scope_level="application",
            required_capability="tenant:configure",
            policy_by_tier={"standard": PolicyDisposition.OPTIONAL},
            readiness_probe="sample-database-provider:connect",
            freshness_seconds=3600,
            retry_policy_version="v0.1",
            deletion_action="DELETE",
        ),
        _lifecycle("intake.submit", "onboarding-control-store", "tenant:configure"),
        _lifecycle("apply", "profile-service", "tenant:onboard"),
        _lifecycle("attach", "profile-service", "tenant:configure"),
        _lifecycle("activation", "tenant-registry", "tenant:approve"),
        _lifecycle("suspend", "tenant-registry", "tenant:suspend"),
        _lifecycle("resume", "tenant-registry", "tenant:resume"),
    ),
)
"""Built-in v0.1 catalog: sample-only providers, single standard tier."""
