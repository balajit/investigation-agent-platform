# src/investigation_agent_platform/application/onboarding/readiness.py
"""Readiness policy evaluation for tenant onboarding (Phase 3, design D5/D10).

A readiness report decides, for one request revision and service tier, whether
every catalogued capability required by policy has fresh, scoped, VERIFIED
probe evidence pinned to the same request, policy, and catalog versions. No
mandatory gate may be satisfied without a successful scoped probe; probe
expiry or a policy/catalog change invalidates readiness. Optional connectors
may be skipped only by explicit service-tier policy.

Activation gating is advisory-only by default (agent design AG-07): reports
inform but never gate until Product + Security approve enforcement. The
activation hook exists so the transition table has a single place to consult.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from investigation_agent_platform.application.onboarding.adapters import (
    AdapterScope,
    ProbeEvidence,
    ProbeOutcome,
)
from investigation_agent_platform.application.onboarding.catalog import (
    V01_SERVICE_TIERS,
    CapabilityCatalog,
    PolicyDisposition,
)


class ReadinessDecision(StrEnum):
    """Closed readiness outcomes."""

    READY = "READY"
    BLOCKED = "BLOCKED"


class EnforcementMode(StrEnum):
    """Advisory informs; enforcing gates activation (requires AG-07 sign-off)."""

    ADVISORY = "ADVISORY"
    ENFORCING = "ENFORCING"


class CapabilityReadiness(BaseModel):
    """Per-capability gate outcome within one readiness report."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    capability_key: str = Field(min_length=1, max_length=128)
    disposition: PolicyDisposition
    outcome: ProbeOutcome
    satisfied: bool
    blocking_code: str | None = Field(default=None, max_length=64)


class ReadinessReport(BaseModel):
    """Durable readiness decision for one request revision and tier."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: str = Field(min_length=1, max_length=64)
    request_id: UUID
    revision: int = Field(ge=1)
    tier: str = Field(min_length=1, max_length=32)
    policy_version: str = Field(min_length=1, max_length=32)
    catalog_version: str = Field(min_length=1, max_length=32)
    decision: ReadinessDecision
    blocking_codes: tuple[str, ...] = Field(default_factory=tuple)
    gates: tuple[CapabilityReadiness, ...] = Field(default_factory=tuple)
    enforcement: EnforcementMode = EnforcementMode.ADVISORY
    evaluated_at: datetime
    expires_at: datetime


def _fresh(evidence: ProbeEvidence, *, now: datetime) -> bool:
    return evidence.probed_at <= now < evidence.expires_at


def _pinned(evidence: ProbeEvidence, scope: AdapterScope) -> bool:
    return (
        evidence.tenant_id == scope.tenant_id
        and evidence.application_id == scope.application_id
        and evidence.environment == scope.environment
        and evidence.request_id == scope.request_id
        and evidence.revision == scope.revision
        and evidence.policy_version == scope.policy_version
        and evidence.catalog_version == scope.catalog_version
    )


def evaluate_readiness(
    catalog: CapabilityCatalog,
    scope: AdapterScope,
    evidences: list[ProbeEvidence],
    *,
    tier: str,
    now: datetime | None = None,
) -> ReadinessReport:
    """Evaluate every catalogued capability for one tier.

    Unknown tiers and unknown evidence capabilities fail closed. Only fresh,
    exactly-pinned VERIFIED evidence satisfies a REQUIRED gate. `auto_promote`
    cannot bypass a required gate: there is no promote path in this function.
    """
    current = now or datetime.now(UTC)
    if tier not in V01_SERVICE_TIERS:
        raise ValueError(f"unknown service tier: {tier!r}")

    by_capability: dict[str, ProbeEvidence] = {}
    for item in evidences:
        try:
            catalog.entry_for(item.capability_key)
        except KeyError:
            raise ValueError(
                f"evidence for uncatalogued capability {item.capability_key!r}"
            ) from None
        by_capability[item.capability_key] = item

    gates: list[CapabilityReadiness] = []
    blocking: list[str] = []
    expiries: list[datetime] = []
    for entry in catalog.entries:
        disposition = entry.policy_by_tier.get(tier, PolicyDisposition.NOT_APPLICABLE)
        if disposition in (PolicyDisposition.NOT_APPLICABLE, PolicyDisposition.UNSUPPORTED):
            gates.append(
                CapabilityReadiness(
                    capability_key=entry.capability_key,
                    disposition=disposition,
                    outcome=ProbeOutcome.SKIPPED,
                    satisfied=True,
                )
            )
            continue
        evidence = by_capability.get(entry.capability_key)
        if evidence is None:
            no_probe = f"{entry.capability_key.upper()}_NO_PROBE"
            gates.append(
                CapabilityReadiness(
                    capability_key=entry.capability_key,
                    disposition=disposition,
                    outcome=ProbeOutcome.NOT_CONFIGURED,
                    satisfied=disposition == PolicyDisposition.OPTIONAL,
                    blocking_code=None if disposition == PolicyDisposition.OPTIONAL else no_probe,
                )
            )
            if disposition == PolicyDisposition.REQUIRED:
                blocking.append(no_probe)
            continue
        usable = _pinned(evidence, scope) and _fresh(evidence, now=current)
        expiries.append(evidence.expires_at)
        if disposition == PolicyDisposition.REQUIRED:
            satisfied = usable and evidence.outcome == ProbeOutcome.VERIFIED
            code = None if satisfied else f"{entry.capability_key.upper()}_NOT_VERIFIED"
            gates.append(
                CapabilityReadiness(
                    capability_key=entry.capability_key,
                    disposition=disposition,
                    outcome=evidence.outcome,
                    satisfied=satisfied,
                    blocking_code=code,
                )
            )
            if code is not None:
                blocking.append(code)
        else:  # OPTIONAL: never blocks; records what the probe said.
            gates.append(
                CapabilityReadiness(
                    capability_key=entry.capability_key,
                    disposition=disposition,
                    outcome=evidence.outcome,
                    satisfied=True,
                )
            )

    decision = ReadinessDecision.READY if not blocking else ReadinessDecision.BLOCKED
    return ReadinessReport(
        tenant_id=scope.tenant_id,
        request_id=scope.request_id,
        revision=scope.revision,
        tier=tier,
        policy_version=scope.policy_version,
        catalog_version=scope.catalog_version,
        decision=decision,
        blocking_codes=tuple(blocking),
        gates=tuple(gates),
        evaluated_at=current,
        expires_at=min(expiries) if expiries else current,
    )


def activation_permitted(
    report: ReadinessReport,
    *,
    enforcement: EnforcementMode = EnforcementMode.ADVISORY,
) -> bool:
    """Single activation-gating hook. Advisory mode never gates.

    Enforcement requires an explicit ENFORCING policy, which itself requires
    Product + Security sign-off (AG-07). Until then every call uses the
    default and reports stay advisory.
    """
    if enforcement == EnforcementMode.ADVISORY:
        return True
    return report.decision == ReadinessDecision.READY
