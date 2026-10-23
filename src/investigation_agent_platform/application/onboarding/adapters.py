# src/investigation_agent_platform/application/onboarding/adapters.py
"""Request-scoped readiness adapters (Phase 3, design D5).

Each catalogued adapter is a request-scoped instance with typed outcomes.
Probes run four immutable stages (discover -> normalize -> bind -> verify) and
return probe evidence recording adapter/version, capability key, exact scope,
request revision, policy/catalog versions, timestamp, expiry, auth mode, and
a closed-vocabulary diagnostic code. Only a successful, scoped, fresh probe
can satisfy a mandatory gate; provider failure is never equivalent to empty
data and unknown capabilities raise instead of returning a soft result.

Sample adapters implement real, scoped, deterministic checks against an
explicitly configured in-memory inventory (v0.1 sample-only scope). No network
access, no secret values in evidence, no fabricated verification.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, ClassVar, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ProbeOutcome(StrEnum):
    """Typed adapter outcomes. Unknown strings are never valid."""

    VERIFIED = "VERIFIED"
    DEGRADED = "DEGRADED"
    FAILED = "FAILED"
    UNSUPPORTED = "UNSUPPORTED"
    SKIPPED = "SKIPPED"
    NOT_CONFIGURED = "NOT_CONFIGURED"


class DiagnosticCode(StrEnum):
    """Closed diagnostic vocabulary for probe evidence."""

    OK = "OK"
    NOT_CONFIGURED = "NOT_CONFIGURED"
    TIMEOUT = "TIMEOUT"
    UNAUTHORIZED = "UNAUTHORIZED"
    STALE_CREDENTIAL = "STALE_CREDENTIAL"
    PROVIDER_OUTAGE = "PROVIDER_OUTAGE"
    PARTIAL_RESULT = "PARTIAL_RESULT"
    UNSUPPORTED_KIND = "UNSUPPORTED_KIND"
    UNKNOWN_REFERENCE = "UNKNOWN_REFERENCE"
    EMPTY_VALID = "EMPTY_VALID"
    INVALID_SCOPE = "INVALID_SCOPE"


class AdapterScope(BaseModel):
    """Exact scope a probe binds its evidence to."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: str = Field(min_length=1, max_length=64)
    application_id: str = Field(min_length=1, max_length=128)
    environment: str = Field(min_length=1, max_length=64)
    request_id: UUID
    revision: int = Field(ge=1)
    policy_version: str = Field(min_length=1, max_length=32)
    catalog_version: str = Field(min_length=1, max_length=32)


class ProbeEvidence(BaseModel):
    """One immutable probe result. Evidence applies only to its exact scope."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    adapter: str = Field(min_length=1, max_length=128)
    adapter_version: str = Field(min_length=1, max_length=32)
    capability_key: str = Field(min_length=1, max_length=128)
    tenant_id: str = Field(min_length=1, max_length=64)
    application_id: str = Field(min_length=1, max_length=128)
    environment: str = Field(min_length=1, max_length=64)
    request_id: UUID
    revision: int = Field(ge=1)
    policy_version: str = Field(min_length=1, max_length=32)
    catalog_version: str = Field(min_length=1, max_length=32)
    outcome: ProbeOutcome
    diagnostic_code: DiagnosticCode
    detail: str = Field(default="", max_length=1024)
    auth_mode: str = Field(min_length=1, max_length=64)
    probed_at: datetime
    expires_at: datetime


class AdapterReference(BaseModel):
    """Structured reference an adapter probes. No URLs, paths, or secrets."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: str = Field(min_length=1, max_length=64)
    provider_key: str = Field(min_length=1, max_length=32)
    resource_id: str = Field(min_length=1, max_length=256)


class SecretMetadata(BaseModel):
    """What secret resolution may reveal: existence + scope, never values."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=128)
    provider: str = Field(min_length=1, max_length=32)
    scope_ok: bool


class SecretResolver(Protocol):
    """Scoped secret resolution at execution time (read-only, metadata only)."""

    def resolve(self, name: str, *, tenant_id: str, environment: str) -> SecretMetadata:
        """Return secret metadata, or raise SecretResolutionError."""
        ...


class SecretResolutionError(ValueError):
    """A secret reference cannot be resolved in this scope."""


class ProbeBackendError(Exception):
    """Provider-side failure mapped to FAILED evidence, never raised outward."""

    def __init__(self, code: DiagnosticCode, detail: str = "") -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


class BaseAdapter:
    """Four-stage probe template: discover -> normalize -> bind -> verify.

    Subclasses implement verify(); the template guarantees every returned
    evidence carries the exact scope, pins, timestamps, and a diagnostic code.
    Provider failures raised as ProbeBackendError become FAILED evidence.
    """

    adapter_name: str = "base"
    adapter_version: str = "v0.1"
    capabilities: tuple[str, ...] = ()
    freshness_seconds: int = 3600
    auth_mode: str = "scoped-secret"

    def __init__(self, secret_resolver: SecretResolver | None = None) -> None:
        self._secret_resolver = secret_resolver

    def probe(
        self,
        scope: AdapterScope,
        reference: AdapterReference,
        *,
        now: datetime | None = None,
    ) -> ProbeEvidence:
        """Run all four stages and return immutable evidence. Never returns None."""
        current = now or datetime.now(UTC)
        try:
            discovered = self.discover(reference)
            normalized = self.normalize(discovered)
            bound = self.bind(scope, normalized)
            outcome, code, detail = self.verify(scope, bound, current)
            capability_key = bound.capability_key
        except ProbeBackendError as exc:
            outcome, code, detail = ProbeOutcome.FAILED, exc.code, exc.detail
            try:
                capability_key = self.capability_for(reference.kind)
            except ProbeBackendError:
                capability_key = reference.kind
        return ProbeEvidence(
            adapter=self.adapter_name,
            adapter_version=self.adapter_version,
            capability_key=capability_key,
            tenant_id=scope.tenant_id,
            application_id=scope.application_id,
            environment=scope.environment,
            request_id=scope.request_id,
            revision=scope.revision,
            policy_version=scope.policy_version,
            catalog_version=scope.catalog_version,
            outcome=outcome,
            diagnostic_code=code,
            detail=detail,
            auth_mode=self.auth_mode,
            probed_at=current,
            expires_at=current + timedelta(seconds=self.freshness_seconds),
        )

    def discover(self, reference: AdapterReference) -> AdapterReference:
        """Stage 1: accept the reference; reject unknown provider keys."""
        if reference.provider_key not in self.provider_keys():
            raise ProbeBackendError(
                DiagnosticCode.UNSUPPORTED_KIND,
                f"provider {reference.provider_key!r} not handled by {self.adapter_name}",
            )
        return reference

    def normalize(self, reference: AdapterReference) -> AdapterReference:
        """Stage 2: return a new immutable reference; no shared mutable state."""
        return AdapterReference(
            kind=reference.kind,
            provider_key=reference.provider_key,
            resource_id=reference.resource_id,
        )

    def bind(self, scope: AdapterScope, reference: AdapterReference) -> BoundReference:
        """Stage 3: attach the capability contract; unknown kinds fail closed."""
        capability_key = self.capability_for(reference.kind)
        if capability_key not in self.capabilities:
            raise ProbeBackendError(
                DiagnosticCode.UNSUPPORTED_KIND,
                f"kind {reference.kind!r} has no catalogued capability",
            )
        return BoundReference(capability_key=capability_key, reference=reference)

    def verify(
        self,
        scope: AdapterScope,
        bound: BoundReference,
        now: datetime,
    ) -> tuple[ProbeOutcome, DiagnosticCode, str]:
        """Stage 4: perform the scoped check. Subclasses implement this."""
        raise NotImplementedError

    def provider_keys(self) -> frozenset[str]:
        """Provider keys this adapter handles."""
        return frozenset()

    def capability_for(self, kind: str) -> str:
        """Map a reference kind to its catalogued capability key."""
        return kind


class BoundReference(BaseModel):
    """A reference bound to its capability contract after stage 3."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    capability_key: str = Field(min_length=1, max_length=128)
    reference: AdapterReference


class AllowlistSecretResolver:
    """Sample secret resolver: explicit names only, metadata only."""

    def __init__(self, allowed_names: frozenset[str]) -> None:
        self._allowed = allowed_names

    def resolve(self, name: str, *, tenant_id: str, environment: str) -> SecretMetadata:
        if not name or name not in self._allowed:
            raise SecretResolutionError(f"secret {name!r} is not resolvable in scope")
        return SecretMetadata(name=name, provider="manager", scope_ok=True)


class SampleGitHostAdapter(BaseAdapter):
    """Sample git-host adapter: resolves structured repo IDs against inventory.

    VERIFIED only when the exact repository_id is present in the explicitly
    configured inventory for this instance. Empty inventory yields
    NOT_CONFIGURED; unknown IDs yield FAILED/UNKNOWN_REFERENCE. No network.
    """

    adapter_name = "sample-git-host"
    adapter_version = "v0.1"
    capabilities = ("repository", "logs", "traces", "metrics")

    _KIND_TO_CAPABILITY: ClassVar[dict[str, str]] = {
        "repository": "repository",
        "logs": "logs",
        "traces": "traces",
        "metrics": "metrics",
    }

    def __init__(
        self,
        inventory: dict[str, dict[str, Any]] | None = None,
        secret_resolver: SecretResolver | None = None,
    ) -> None:
        super().__init__(secret_resolver)
        self._inventory = dict(inventory or {})

    def provider_keys(self) -> frozenset[str]:
        return frozenset({"sample-git-host"})

    def capability_for(self, kind: str) -> str:
        try:
            return self._KIND_TO_CAPABILITY[kind]
        except KeyError:
            raise ProbeBackendError(
                DiagnosticCode.UNSUPPORTED_KIND,
                f"kind {kind!r} has no catalogued capability",
            ) from None

    def verify(
        self,
        scope: AdapterScope,
        bound: BoundReference,
        now: datetime,
    ) -> tuple[ProbeOutcome, DiagnosticCode, str]:
        if not self._inventory:
            return (
                ProbeOutcome.NOT_CONFIGURED,
                DiagnosticCode.NOT_CONFIGURED,
                "no sample inventory configured",
            )
        record = self._inventory.get(bound.reference.resource_id)
        if record is None:
            return (
                ProbeOutcome.FAILED,
                DiagnosticCode.UNKNOWN_REFERENCE,
                f"reference {bound.reference.resource_id!r} not in inventory",
            )
        if not record:
            return (
                ProbeOutcome.NOT_CONFIGURED,
                DiagnosticCode.EMPTY_VALID,
                "inventory record is empty but valid",
            )
        return (
            ProbeOutcome.VERIFIED,
            DiagnosticCode.OK,
            f"resolved {bound.reference.resource_id!r} at inventory pin",
        )


class SampleDatabaseProviderAdapter(BaseAdapter):
    """Sample database provider: scoped connectivity against inventory.

    VERIFIED only when the exact resource_id has a configured, credentialed
    record. Records flagged stale yield FAILED/STALE_CREDENTIAL; records
    flagged down yield FAILED/PROVIDER_OUTAGE. No network, no secret values.
    """

    adapter_name = "sample-database-provider"
    adapter_version = "v0.1"
    capabilities = ("datasource", "config_server", "knowledge_corpus")

    _KIND_TO_CAPABILITY: ClassVar[dict[str, str]] = {
        "datasource": "datasource",
        "config_server": "config_server",
        "knowledge_corpus": "knowledge_corpus",
    }

    def __init__(
        self,
        inventory: dict[str, dict[str, Any]] | None = None,
        secret_resolver: SecretResolver | None = None,
    ) -> None:
        super().__init__(secret_resolver)
        self._inventory = dict(inventory or {})

    def provider_keys(self) -> frozenset[str]:
        return frozenset({"sample-database-provider"})

    def capability_for(self, kind: str) -> str:
        try:
            return self._KIND_TO_CAPABILITY[kind]
        except KeyError:
            raise ProbeBackendError(
                DiagnosticCode.UNSUPPORTED_KIND,
                f"kind {kind!r} has no catalogued capability",
            ) from None

    def verify(
        self,
        scope: AdapterScope,
        bound: BoundReference,
        now: datetime,
    ) -> tuple[ProbeOutcome, DiagnosticCode, str]:
        if not self._inventory:
            return (
                ProbeOutcome.NOT_CONFIGURED,
                DiagnosticCode.NOT_CONFIGURED,
                "no sample inventory configured",
            )
        record = self._inventory.get(bound.reference.resource_id)
        if record is None:
            return (
                ProbeOutcome.FAILED,
                DiagnosticCode.UNKNOWN_REFERENCE,
                f"reference {bound.reference.resource_id!r} not in inventory",
            )
        if record.get("down"):
            return (
                ProbeOutcome.FAILED,
                DiagnosticCode.PROVIDER_OUTAGE,
                "provider reports outage for this record",
            )
        if record.get("credential_stale"):
            return (
                ProbeOutcome.FAILED,
                DiagnosticCode.STALE_CREDENTIAL,
                "credential for this record is stale",
            )
        secret_name = record.get("secret_name")
        if secret_name is not None:
            if self._secret_resolver is None:
                return (
                    ProbeOutcome.FAILED,
                    DiagnosticCode.UNAUTHORIZED,
                    "reference requires a secret but no resolver is configured",
                )
            try:
                self._secret_resolver.resolve(
                    str(secret_name),
                    tenant_id=scope.tenant_id,
                    environment=scope.environment,
                )
            except SecretResolutionError:
                return (
                    ProbeOutcome.FAILED,
                    DiagnosticCode.UNAUTHORIZED,
                    "secret is not resolvable in this scope",
                )
        if not record.get("connected", True):
            return (
                ProbeOutcome.FAILED,
                DiagnosticCode.PARTIAL_RESULT,
                "provider returned a partial result",
            )
        return (
            ProbeOutcome.VERIFIED,
            DiagnosticCode.OK,
            f"scoped check passed for {bound.reference.resource_id!r}",
        )


class AdapterRegistry:
    """Maps capability keys to adapters. Unknown capabilities raise."""

    def __init__(self, adapters: list[BaseAdapter] | None = None) -> None:
        self._by_capability: dict[str, BaseAdapter] = {}
        for adapter in adapters or []:
            for capability in adapter.capabilities:
                if capability in self._by_capability:
                    raise ValueError(f"duplicate adapter for capability {capability!r}")
                self._by_capability[capability] = adapter

    def adapter_for(self, capability_key: str) -> BaseAdapter:
        """Return the adapter for a capability, or raise a closed error."""
        try:
            return self._by_capability[capability_key]
        except KeyError:
            raise KeyError(f"capability {capability_key!r} has no registered adapter") from None


def v01_registry(
    *,
    git_inventory: dict[str, dict[str, Any]] | None = None,
    db_inventory: dict[str, dict[str, Any]] | None = None,
    secret_resolver: SecretResolver | None = None,
) -> AdapterRegistry:
    """Build the v0.1 sample-only adapter registry."""
    return AdapterRegistry(
        [
            SampleGitHostAdapter(git_inventory, secret_resolver),
            SampleDatabaseProviderAdapter(db_inventory, secret_resolver),
        ]
    )
