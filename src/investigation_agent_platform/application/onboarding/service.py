# src/investigation_agent_platform/application/onboarding/service.py
"""Onboarding application service (Phase 4, design D4/D9).

One place composing intake validation, DRAFT persistence, acceptance,
lifecycle transitions, readiness, and report reads into the operations the
REST routes and CLI invoke. Every mutating operation maps repository errors
to stable service codes; 202 Accepted is returned only after the durable
accept transaction commits.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from investigation_agent_platform.application.onboarding.catalog import (
    V01_CATALOG,
)
from investigation_agent_platform.application.onboarding.intake import (
    TenantIntakeEnvelope,
)
from investigation_agent_platform.application.onboarding.readiness import (
    ReadinessDecision,
)
from investigation_agent_platform.infrastructure.persistence.onboarding_acceptance_repository import (
    AcceptanceConflictError,
    AcceptanceGenerationError,
    AcceptanceStaleError,
)
from investigation_agent_platform.infrastructure.persistence.onboarding_request_repository import (
    DraftIdempotencyConflictError,
    DraftRevisionConflictError,
)


@dataclass(frozen=True)
class ServiceError(Exception):
    """Stable service failure: code for branching, message for humans."""

    code: str
    http_status: int
    message: str


@dataclass(frozen=True)
class ApplyPayload:
    tenant_id: str
    request_id: UUID
    revision: int
    job_id: UUID
    dispatch_id: UUID
    workflow_id: str
    dispatch_state: str


@dataclass(frozen=True)
class RequestStatus:
    tenant_id: str
    request_id: UUID
    revision: int
    state: str
    state_version: int
    job_id: UUID | None
    readiness_decision: str | None
    blocking_codes: tuple[str, ...]
    registry_status: str | None


class OnboardingService:
    """Compose onboarding repositories into route/CLI operations."""

    def __init__(
        self,
        *,
        drafts: Any,
        acceptance: Any,
        dispatches: Any,
        registry: Any,
        probes: Any,
        coverage: Any,
        quota_gate: Any | None = None,
    ) -> None:
        self._drafts = drafts
        self._acceptance = acceptance
        self._dispatches = dispatches
        self._registry = registry
        self._probes = probes
        self._coverage = coverage
        self._quota_gate = quota_gate

    async def _reject_ambiguous_new_tenant_id(self, tenant_id: str) -> None:
        """Keep first-onboarding tenant ids from overlapping a sibling's index scope.

        Legacy log indexes are named ``logs-{tenant}-{service}-{env}-{n}`` and
        both tenant and service may contain ``-``, so a tenant id that is a
        dash-boundary prefix of another (``acme`` / ``acme-eu``) can read the
        other's indexes. Tenants whose registry entry is ACTIVE are untouched
        (existing tenants adding applications keep working). A tenant that is
        not ACTIVE yet is a first onboarding and must not contain ``-`` nor be
        a dash-boundary prefix of any known tenant id (registry, investigations,
        or profiles). Runs before the
        quota gate and any draft write so a rejection has no side effects.
        Enforcement at registry-row creation remains an operations duty.
        """
        entry = await self._registry.read_entry(tenant_id)
        if entry is not None and entry.status == "ACTIVE":
            return
        collides = "-" in tenant_id or any(
            other.startswith(f"{tenant_id}-")
            for other in await self._registry.list_known_tenant_ids()
        )
        if collides:
            raise ServiceError(
                "TENANT_ID_NOT_ALLOWED",
                400,
                "new tenant ids must not contain '-' or be a dash-prefix of a registered "
                "tenant id; choose a different tenant id",
            )

    async def create_request(
        self,
        *,
        tenant_id: str,
        actor_sub: str,
        idempotency_key: str,
        intake: TenantIntakeEnvelope,
        policy_version: str = "policy-v1",
        catalog_version: str = "v0.1",
    ) -> ApplyPayload:
        """Validate, persist DRAFT, and accept to QUEUED in one call.

        The DRAFT write and the accept transaction are separate durable
        steps; the 202 contract holds because accept commits request, job,
        audit linkage, and dispatch intent atomically.
        """
        await self._reject_ambiguous_new_tenant_id(tenant_id)
        if self._quota_gate is not None and not self._quota_gate(tenant_id, "onboarding.create"):
            raise ServiceError("QUOTA_EXCEEDED", 429, "tenant create quota exceeded")
        try:
            draft = await self._drafts.create_draft(
                tenant_id=tenant_id,
                actor_sub=actor_sub,
                idempotency_key=idempotency_key,
                intake=intake,
                policy_version=policy_version,
                catalog_version=catalog_version,
            )
        except DraftIdempotencyConflictError as exc:
            raise ServiceError("IDEMPOTENCY_CONFLICT", 409, str(exc)) from exc
        try:
            accepted = await self._acceptance.accept_draft(
                tenant_id=tenant_id,
                actor_sub=actor_sub,
                request_id=draft.request_id,
                expected_revision=draft.revision,
            )
        except (AcceptanceStaleError, AcceptanceGenerationError) as exc:
            raise ServiceError("STALE_REVISION", 409, str(exc)) from exc
        except AcceptanceConflictError as exc:
            raise ServiceError("DISPATCH_CONFLICT", 409, str(exc)) from exc
        return ApplyPayload(
            tenant_id=tenant_id,
            request_id=accepted.request_id,
            revision=accepted.revision,
            job_id=accepted.job_id,
            dispatch_id=accepted.id,
            workflow_id=accepted.workflow_id,
            dispatch_state=accepted.state,
        )

    async def put_intake(
        self,
        *,
        tenant_id: str,
        actor_sub: str,
        request_id: UUID,
        expected_revision: int,
        idempotency_key: str,
        intake: TenantIntakeEnvelope,
    ) -> dict[str, Any]:
        """Create the next immutable revision. Stale revisions conflict."""
        try:
            row = await self._drafts.create_draft(
                tenant_id=tenant_id,
                actor_sub=actor_sub,
                idempotency_key=idempotency_key,
                intake=intake,
                request_id=request_id,
                expected_revision=expected_revision,
            )
        except DraftIdempotencyConflictError as exc:
            raise ServiceError("IDEMPOTENCY_CONFLICT", 409, str(exc)) from exc
        except DraftRevisionConflictError as exc:
            raise ServiceError("STALE_REVISION", 409, str(exc)) from exc
        return {
            "tenant_id": tenant_id,
            "request_id": row.request_id,
            "revision": row.revision,
            "content_hash": row.content_hash,
            "accepted": False,
        }

    async def get_status(self, tenant_id: str, request_id: UUID) -> RequestStatus:
        """Tenant-authorized progress and blockers for one request."""
        row = await self._dispatches.read_latest_request_row(tenant_id, request_id)
        if row is None:
            raise ServiceError("REQUEST_NOT_FOUND", 404, "onboarding request not found")
        report = await self._probes.report_for(
            tenant_id, request_id, int(row["revision"]), "standard"
        )
        decision = str(report.decision) if report is not None else None
        blocking: tuple[str, ...] = tuple(report.blocking_codes) if report is not None else ()
        entry = await self._registry.read_entry(tenant_id)
        return RequestStatus(
            tenant_id=tenant_id,
            request_id=request_id,
            revision=int(row["revision"]),
            state=str(row["state"]),
            state_version=int(row["state_version"]),
            job_id=row["job_id"],
            readiness_decision=decision,
            blocking_codes=blocking,
            registry_status=entry.status if entry is not None else None,
        )

    async def cancel_request(
        self, tenant_id: str, request_id: UUID, revision: int
    ) -> RequestStatus:
        """Request cancellation; the workflow converges the row to CANCELED."""
        moved = await self._dispatches.request_cancellation(tenant_id, request_id, revision)
        if not moved:
            row = await self._dispatches.read_latest_request_row(tenant_id, request_id)
            if row is None:
                raise ServiceError("REQUEST_NOT_FOUND", 404, "onboarding request not found")
            raise ServiceError(
                "INVALID_STATE",
                409,
                f"request is {row['state']}, only QUEUED can be cancelled",
            )
        return await self.get_status(tenant_id, request_id)

    async def retry_dispatch(self, tenant_id: str, dispatch_id: UUID) -> dict[str, Any]:
        """Make a RETRYABLE dispatch due immediately. Terminal rows never move."""
        moved = await self._dispatches.retry_now(tenant_id, dispatch_id)
        if not moved:
            raise ServiceError("INVALID_STATE", 409, "dispatch is not RETRYABLE or does not exist")
        return {"tenant_id": tenant_id, "dispatch_id": dispatch_id, "retried": True}

    async def suspend_tenant(self, tenant_id: str) -> dict[str, Any]:
        """Suspend tenant access without deleting data."""
        from investigation_agent_platform.infrastructure.persistence.onboarding_registry_repository import (
            RegistryTransitionError,
        )

        try:
            entry = await self._registry.suspend(tenant_id)
        except RegistryTransitionError as exc:
            raise ServiceError("INVALID_STATE", 409, str(exc)) from exc
        return {"tenant_id": tenant_id, "status": entry.status, "epoch": entry.lifecycle_epoch}

    async def resume_tenant(self, tenant_id: str) -> dict[str, Any]:
        """Resume a suspended tenant after rechecking required readiness gates."""
        from investigation_agent_platform.infrastructure.persistence.onboarding_registry_repository import (
            RegistryTransitionError,
        )

        entry = await self._registry.read_entry(tenant_id)
        if entry is None:
            raise ServiceError("REQUEST_NOT_FOUND", 404, "tenant registry entry not found")
        try:
            resumed = await self._registry.resume(tenant_id)
        except RegistryTransitionError as exc:
            raise ServiceError("INVALID_STATE", 409, str(exc)) from exc
        return {"tenant_id": tenant_id, "status": resumed.status, "epoch": resumed.lifecycle_epoch}

    async def approve_and_activate(
        self,
        *,
        tenant_id: str,
        actor_sub: str,
        request_id: UUID,
        revision: int,
        idempotency_key: str,
        tier: str = "standard",
    ) -> dict[str, Any]:
        """Approve an exact request revision and activate the tenant.

        Requires a fresh READY readiness report pinned to the same
        request/policy/catalog versions, then moves the registry RESERVED to
        ACTIVE and files the ACTIVATE audit row. Advisory mode never gates:
        this path always enforces.
        """
        row = await self._dispatches.read_latest_request_row(tenant_id, request_id)
        if row is None or int(row["revision"]) != revision:
            raise ServiceError("REQUEST_NOT_FOUND", 404, "request revision not found")
        report = await self._probes.report_for(tenant_id, request_id, revision, tier)
        if report is None:
            raise ServiceError("NOT_READY", 409, "no readiness report for this revision")
        now = datetime.now(UTC)
        if report.expires_at <= now:
            raise ServiceError("NOT_READY", 409, "readiness report has expired")
        if report.policy_version != str(row["policy_version"]) or report.catalog_version != str(
            row["catalog_version"]
        ):
            raise ServiceError("PLAN_CHANGED", 409, "policy or catalog changed since evaluation")
        if report.decision != ReadinessDecision.READY.value:
            raise ServiceError(
                "NOT_READY",
                409,
                f"readiness is {report.decision}: {list(report.blocking_codes)}",
            )
        from investigation_agent_platform.infrastructure.persistence.onboarding_registry_repository import (
            RegistryTransitionError,
        )

        try:
            entry = await self._registry.activate(tenant_id)
        except RegistryTransitionError as exc:
            raise ServiceError("INVALID_STATE", 409, str(exc)) from exc
        await self._acceptance.record_activation_audit(
            tenant_id=tenant_id,
            actor_sub=actor_sub,
            request_id=request_id,
            revision=revision,
            content_hash=str(row["content_hash"]),
            idempotency_key=idempotency_key,
        )
        return {
            "tenant_id": tenant_id,
            "status": entry.status,
            "epoch": entry.lifecycle_epoch,
            "request_id": request_id,
            "revision": revision,
        }

    async def get_report(
        self, tenant_id: str, request_id: UUID, revision: int, generation: int
    ) -> dict[str, Any]:
        """Return one filed coverage report by linkage (DEP-10)."""
        row = await self._coverage.read_report(tenant_id, request_id, revision, generation)
        if row is None:
            raise ServiceError("REQUEST_NOT_FOUND", 404, "coverage report not found")
        return {
            "tenant_id": tenant_id,
            "request_id": request_id,
            "revision": revision,
            "generation": generation,
            "report_content_hash": row.report_content_hash,
            "report": row.report_json,
        }

    async def record_probe_evidence(
        self,
        *,
        tenant_id: str,
        request_id: UUID,
        revision: int,
        evidence: Any,
    ) -> dict[str, Any]:
        """File one adapter probe evidence pinned to a request revision."""
        from investigation_agent_platform.application.onboarding.adapters import (
            AdapterScope,
        )

        row = await self._dispatches.read_latest_request_row(tenant_id, request_id)
        if row is None or int(row["revision"]) != revision:
            raise ServiceError("REQUEST_NOT_FOUND", 404, "request revision not found")
        scope = AdapterScope(
            tenant_id=tenant_id,
            application_id=str(row["application_id"]),
            environment=str(row["environment"]),
            request_id=request_id,
            revision=revision,
            policy_version=str(row["policy_version"]),
            catalog_version=str(row["catalog_version"]),
        )
        stored = await self._probes.record_evidence(scope, evidence)
        return {
            "tenant_id": tenant_id,
            "request_id": request_id,
            "revision": revision,
            "capability_key": stored.capability_key,
            "outcome": stored.outcome,
        }

    async def evaluate_and_file_readiness(
        self,
        *,
        tenant_id: str,
        request_id: UUID,
        revision: int,
        tier: str = "standard",
    ) -> dict[str, Any]:
        """Evaluate stored probe evidence and file the readiness report."""
        from investigation_agent_platform.application.onboarding.adapters import (
            AdapterScope,
            ProbeEvidence,
        )
        from investigation_agent_platform.application.onboarding.catalog import (
            V01_SERVICE_TIERS,
        )
        from investigation_agent_platform.application.onboarding.readiness import (
            evaluate_readiness,
        )

        if tier not in V01_SERVICE_TIERS:
            raise ServiceError("UNKNOWN_TIER", 404, f"unknown service tier: {tier!r}")
        row = await self._dispatches.read_latest_request_row(tenant_id, request_id)
        if row is None or int(row["revision"]) != revision:
            raise ServiceError("REQUEST_NOT_FOUND", 404, "request revision not found")
        scope = AdapterScope(
            tenant_id=tenant_id,
            application_id=str(row["application_id"]),
            environment=str(row["environment"]),
            request_id=request_id,
            revision=revision,
            policy_version=str(row["policy_version"]),
            catalog_version=str(row["catalog_version"]),
        )
        stored_rows = await self._probes.list_evidence_for(scope)
        evidences = [
            ProbeEvidence(
                adapter=r.adapter,
                adapter_version=r.adapter_version,
                capability_key=r.capability_key,
                tenant_id=r.tenant_id,
                application_id=r.application_id,
                environment=r.environment,
                request_id=r.request_id,
                revision=r.revision,
                policy_version=r.policy_version,
                catalog_version=r.catalog_version,
                outcome=r.outcome,
                diagnostic_code=r.diagnostic_code,
                detail=r.detail,
                auth_mode=r.auth_mode,
                probed_at=r.probed_at,
                expires_at=r.expires_at,
            )
            for r in stored_rows
        ]
        report = evaluate_readiness(V01_CATALOG, scope, evidences, tier=tier)
        await self._probes.record_report(report)
        return {
            "tenant_id": tenant_id,
            "request_id": request_id,
            "revision": revision,
            "tier": tier,
            "decision": report.decision.value,
            "blocking_codes": list(report.blocking_codes),
        }

    def capabilities(self, tier: str = "standard") -> list[dict[str, Any]]:
        """Policy-resolved supported, required, optional, and unsupported capabilities."""
        from investigation_agent_platform.application.onboarding.catalog import (
            V01_SERVICE_TIERS,
        )

        if tier not in V01_SERVICE_TIERS:
            raise ServiceError("UNKNOWN_TIER", 404, f"unknown service tier: {tier!r}")
        return [
            {
                "capability_key": entry.capability_key,
                "version": entry.version,
                "required": entry.policy_by_tier[tier].value == "REQUIRED",
                "disposition": entry.policy_by_tier[tier].value,
                "owner": entry.owner,
                "readiness_probe": entry.readiness_probe,
            }
            for entry in V01_CATALOG.entries
        ]

    async def verify_telemetry(
        self,
        tenant_id: str,
        home_service: str,
        endpoints: list[dict[str, Any]],
        mapping_source_id: str | None = None,
    ) -> dict[str, Any]:
        """Verify inline telemetry endpoint declarations live (no filesystem).

        The online-service counterpart of the ``verify-telemetry`` CLI: the
        caller submits endpoint declarations in the request body, the same
        platform flow proves them against the declared deployments, and the
        structured results return. Declaration validation failures map to
        stable 400 codes; provider I/O failures surface per-check (the run
        reports FAIL lines, it does not raise) except router-build errors,
        which are operator configuration bugs and raise.
        """
        from investigation_agent_platform.application.onboarding.telemetry_verification import (
            DEFAULT_MAPPING_PROFILES_PATH,
            verify_tenant_telemetry,
        )
        from investigation_agent_platform.infrastructure.evidence.runtime.tenant_telemetry import (
            TelemetryEndpoint,
            TenantTelemetryConfig,
        )

        if not endpoints:
            raise ServiceError("EMPTY_TELEMETRY_DECLARATION", 400, "endpoints must not be empty")
        try:
            config = TenantTelemetryConfig(
                tenant_id=tenant_id,
                endpoints=tuple(TelemetryEndpoint(**e) for e in endpoints),
            )
        except ValueError as exc:
            raise ServiceError(
                "INVALID_TELEMETRY_DECLARATION", 400, f"invalid telemetry declaration: {exc}"
            ) from exc
        results = await verify_tenant_telemetry(
            configs={tenant_id: config},
            tenant_id=tenant_id,
            home_service=home_service,
            mapping_source_id=mapping_source_id,
            mapping_profiles_path=(DEFAULT_MAPPING_PROFILES_PATH if mapping_source_id else None),
        )
        return {
            "tenant_id": tenant_id,
            "passed": all(r.passed for r in results),
            "checks": [r.to_dict() for r in results],
        }
