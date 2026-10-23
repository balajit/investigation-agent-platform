# src/investigation_agent_platform/application/onboarding/cleanup.py
"""Deletion preview, approval, purge, and status (Phase 5, D7/D8).

Preview enumerates every required store and blocks approval on any gap;
approval binds one canonical payload and is consumed atomically once;
purge executes the approved plan entries idempotently and appends
append-only outcomes; terminal states are computed, never asserted.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from investigation_agent_platform.application.onboarding.deletion import (
    DeletionStatus,
    DeploymentRegistry,
    StoreInventoryResult,
    approval_blockers,
    approval_matches,
    build_approval_payload,
    canonical_plan_hash,
)
from investigation_agent_platform.application.onboarding.service import ServiceError
from investigation_agent_platform.application.onboarding.store_adapters import (
    StoreAdapterRegistry,
)
from investigation_agent_platform.infrastructure.persistence.onboarding_deletion_repository import (
    DeletionApprovalError,
    evaluate_terminal_state,
)


class CleanupService:
    """Compose registry, adapters, and deletion persistence."""

    def __init__(self, *, deletions: Any, adapter_registry: StoreAdapterRegistry) -> None:
        self._deletions = deletions
        self._adapters = adapter_registry

    async def load_registry(
        self, deployment_id: str, registry_version: str | None = None
    ) -> DeploymentRegistry:
        """Load the operator-managed deployment registry from durable rows."""
        from investigation_agent_platform.application.onboarding.deletion import (
            StoreRegistryEntry,
        )

        rows = await self._deletions.list_registry(deployment_id, registry_version)
        if not rows:
            raise ServiceError(
                "STORE_REGISTRY_UNKNOWN", 404, "no store registry for this deployment"
            )
        versions = {row.registry_version for row in rows}
        if registry_version is None:
            registry_version = max(versions)
            rows = [row for row in rows if row.registry_version == registry_version]
        return DeploymentRegistry(
            deployment_id=deployment_id,
            registry_version=registry_version,
            entries=tuple(
                StoreRegistryEntry(
                    deployment_id=row.deployment_id,
                    store_id=row.store_id,
                    store_type=row.store_type,
                    adapter_version=row.adapter_version,
                    registry_version=row.registry_version,
                    required_for_deletion=row.required_for_deletion,
                    enabled=row.enabled,
                    capabilities_json=dict(row.capabilities_json or {}),
                )
                for row in rows
            ),
        )

    def _manifest_fields(
        self,
        *,
        manifest_id: UUID,
        tenant_id: str,
        application_id: str | None,
        environment: str,
        registry: DeploymentRegistry,
        policy_version: str,
        inventory: list[StoreInventoryResult],
        backup_id: str,
        backup_verified_at: datetime,
        requester_id: str,
        approver_ids: list[str],
        expires_at: datetime,
    ) -> dict[str, Any]:
        fields: dict[str, Any] = {
            "manifest_id": str(manifest_id),
            "tenant_id": tenant_id,
            "application_id": application_id,
            "environment": environment,
            "plan_version": 1,
            "store_registry_version": registry.registry_version,
            "policy_version": policy_version,
            "inventory": [item.model_dump(mode="json") for item in inventory],
            "backup_id": backup_id,
            "backup_verified_at": backup_verified_at.isoformat(),
            "requester_id": requester_id,
            "approver_ids": list(approver_ids),
            "expires_at": expires_at.isoformat(),
        }
        fields["canonical_plan_hash"] = canonical_plan_hash(fields)
        return fields

    async def preview(
        self,
        *,
        tenant_id: str,
        application_id: str | None,
        environment: str,
        deployment_id: str,
        registry: DeploymentRegistry,
        policy_version: str,
        requester_id: str,
        approver_ids: list[str],
        backup_id: str,
        expires_at: datetime,
        manifest_id: UUID | None = None,
    ) -> dict[str, Any]:
        """Enumerate required stores and persist the immutable manifest."""
        if registry.deployment_id != deployment_id:
            raise ServiceError("PLAN_CHANGED", 409, "registry is not for this deployment")
        inventory: list[StoreInventoryResult] = []
        for entry in registry.required_stores():
            adapter = self._adapters.adapter_for(entry.store_id)
            inventory.append(
                adapter.enumerate(
                    tenant_id=tenant_id,
                    application_id=application_id,
                    environment=environment,
                )
            )
        blockers = approval_blockers(registry, inventory)
        manifest_uuid = manifest_id or uuid4()
        fields = self._manifest_fields(
            manifest_id=manifest_uuid,
            tenant_id=tenant_id,
            application_id=application_id,
            environment=environment,
            registry=registry,
            policy_version=policy_version,
            inventory=inventory,
            backup_id=backup_id,
            backup_verified_at=datetime.now(UTC),
            requester_id=requester_id,
            approver_ids=approver_ids,
            expires_at=expires_at,
        )
        state = (
            DeletionStatus.APPROVAL_PENDING.value if not blockers else DeletionStatus.BLOCKED.value
        )
        stored = await self._deletions.save_manifest(
            tenant_id=tenant_id,
            manifest_id=manifest_uuid,
            application_id=application_id,
            environment=environment,
            plan_version=1,
            store_registry_version=registry.registry_version,
            policy_version=policy_version,
            manifest_json=fields,
            plan_hash=fields["canonical_plan_hash"],
            state=state,
            requester_id=requester_id,
            expires_at=expires_at,
        )
        return {
            "tenant_id": tenant_id,
            "manifest_id": stored.manifest_id,
            "plan_hash": stored.plan_hash,
            "state": stored.state,
            "blocking_codes": blockers,
            "resource_counts": {item.store_id: item.resource_count for item in inventory},
        }

    async def mint_approval(
        self,
        *,
        tenant_id: str,
        manifest_id: UUID,
        approver_id: str,
        requester_id: str,
        policy_version: str,
        store_registry_version: str,
        expires_at: datetime,
        nonce: str | None = None,
    ) -> dict[str, Any]:
        """Mint a one-time approval token bound to the manifest plan."""
        import secrets as _secrets

        manifest = await self._deletions.read_manifest(tenant_id, manifest_id)
        if manifest is None:
            raise ServiceError("REQUEST_NOT_FOUND", 404, "deletion manifest not found")
        if manifest.state == DeletionStatus.BLOCKED.value:
            raise ServiceError("STORE_INVENTORY_INCOMPLETE", 409, "manifest is blocked")
        payload = build_approval_payload(
            manifest_id=manifest_id,
            plan_hash=manifest.plan_hash,
            tenant_id=tenant_id,
            application_id=manifest.application_id,
            environment=manifest.environment,
            requester_id=requester_id,
            approver_id=approver_id,
            policy_version=policy_version,
            store_registry_version=store_registry_version,
            expires_at=expires_at,
            nonce=nonce or _secrets.token_urlsafe(24),
        )
        if requester_id == approver_id:
            raise ServiceError("SEPARATION_OF_DUTIES", 403, "approver must differ from requester")
        try:
            _row, token = await self._deletions.mint_approval(
                tenant_id=tenant_id, payload=payload, requester_id=requester_id
            )
        except DeletionApprovalError as exc:
            raise ServiceError("APPROVAL_INVALID", 409, str(exc)) from exc
        await self._deletions.update_manifest_state(
            tenant_id, manifest_id, DeletionStatus.APPROVAL_PENDING.value
        )
        return {
            "tenant_id": tenant_id,
            "manifest_id": manifest_id,
            "approval_token": token,
            "expires_at": payload.expires_at,
            "consumed": False,
        }

    async def consume_approval(
        self,
        *,
        tenant_id: str,
        manifest_id: UUID,
        presented_token: str,
        approver_id: str,
    ) -> dict[str, Any]:
        """Verify the full payload binding, then atomically consume once."""
        approvals = await self._deletions.list_approvals(tenant_id, manifest_id)
        candidate = next((a for a in approvals if a.consumed_at is None), None)
        if candidate is None:
            raise ServiceError("APPROVAL_INVALID", 409, "no pending approval for this manifest")
        manifest = await self._deletions.read_manifest(tenant_id, manifest_id)
        if manifest is None:
            raise ServiceError("REQUEST_NOT_FOUND", 404, "deletion manifest not found")
        payload = build_approval_payload(
            manifest_id=manifest_id,
            plan_hash=candidate.manifest_hash,
            tenant_id=tenant_id,
            application_id=manifest.application_id,
            environment=manifest.environment,
            requester_id=candidate.requester_id,
            approver_id=candidate.approver_id,
            policy_version=candidate.policy_version,
            store_registry_version=candidate.store_registry_version,
            expires_at=candidate.expires_at,
            nonce=candidate.nonce,
        )
        if not approval_matches(
            presented_token=presented_token,
            stored_token_digest=candidate.token_digest,
            payload=payload,
            stored_payload_digest=candidate.payload_digest,
            authenticated_approver_id=approver_id,
        ):
            raise ServiceError(
                "APPROVAL_INVALID", 409, "approval token, payload, or approver mismatch"
            )
        try:
            consumed = await self._deletions.consume_approval(
                tenant_id=tenant_id,
                manifest_id=manifest_id,
                presented_token=presented_token,
                approver_id=approver_id,
            )
        except DeletionApprovalError as exc:
            raise ServiceError("APPROVAL_EXPIRED", 409, str(exc)) from exc
        await self._deletions.update_manifest_state(
            tenant_id, manifest_id, DeletionStatus.QUEUED.value
        )
        return {
            "tenant_id": tenant_id,
            "manifest_id": manifest_id,
            "purge_job_id": consumed.purge_job_id,
            "consumed": True,
        }

    async def run_purge(
        self, *, tenant_id: str, manifest_id: UUID, attempt: int = 1
    ) -> dict[str, Any]:
        """Execute approved plan entries idempotently and finalize honestly."""
        manifest = await self._deletions.read_manifest(tenant_id, manifest_id)
        if manifest is None:
            raise ServiceError("REQUEST_NOT_FOUND", 404, "deletion manifest not found")
        if manifest.state not in (DeletionStatus.QUEUED.value, DeletionStatus.IN_PROGRESS.value):
            raise ServiceError(
                "INVALID_STATE", 409, f"manifest is {manifest.state}, purge requires QUEUED"
            )
        approvals = await self._deletions.list_approvals(tenant_id, manifest_id)
        if not any(a.consumed_at is not None for a in approvals):
            raise ServiceError(
                "APPROVAL_INVALID", 409, "no consumed approval; purge is not authorized"
            )
        await self._deletions.update_manifest_state(
            tenant_id, manifest_id, DeletionStatus.IN_PROGRESS.value
        )
        entries = manifest.manifest_json.get("inventory") or []
        plans: list[dict[str, Any]] = []
        for inventory in entries:
            plans.extend(inventory.get("plan_entries", []))
        from investigation_agent_platform.application.onboarding.deletion import (
            StoreResourcePlan,
        )

        states: list[str] = []
        for raw in plans:
            plan = StoreResourcePlan.model_validate(raw)
            entry_key = f"{plan.store_id}:{plan.resource_type}:{plan.resource_ref}"
            adapter = self._adapters.adapter_for(plan.store_id)
            try:
                evidence = adapter.apply(plan)
                state = "RETAINED_BY_POLICY" if plan.action.value == "RETAIN" else "DONE"
                await self._deletions.record_outcome(
                    tenant_id=tenant_id,
                    manifest_id=manifest_id,
                    entry_key=entry_key,
                    store_id=plan.store_id,
                    action=plan.action.value,
                    state=state,
                    attempt=attempt,
                    completion_evidence_ref=evidence[:512],
                )
            except ValueError as exc:
                await self._deletions.record_outcome(
                    tenant_id=tenant_id,
                    manifest_id=manifest_id,
                    entry_key=entry_key,
                    store_id=plan.store_id,
                    action=plan.action.value,
                    state="BLOCKED",
                    attempt=attempt,
                    residual_code=str(exc)[:64],
                )
                state = "BLOCKED"
            states.append(state)
        terminal = evaluate_terminal_state(states)
        recorded = await self._deletions.list_outcome_states(tenant_id, manifest_id)
        if not states or len(recorded) < len(plans):
            terminal = DeletionStatus.INCOMPLETE.value
        await self._deletions.update_manifest_state(tenant_id, manifest_id, terminal)
        return {
            "tenant_id": tenant_id,
            "manifest_id": manifest_id,
            "terminal_state": terminal,
            "entries": len(plans),
        }

    async def get_deletion(self, tenant_id: str, manifest_id: UUID) -> dict[str, Any]:
        """Show per-store status and residuals, tenant-authorized."""
        manifest = await self._deletions.read_manifest(tenant_id, manifest_id)
        if manifest is None:
            raise ServiceError("REQUEST_NOT_FOUND", 404, "deletion manifest not found")
        outcomes = await self._deletions.list_outcome_states(tenant_id, manifest_id)
        return {
            "tenant_id": tenant_id,
            "manifest_id": manifest_id,
            "state": manifest.state,
            "plan_hash": manifest.plan_hash,
            "store_registry_version": manifest.store_registry_version,
            "outcomes": [{"entry_key": key, "state": state} for key, state in outcomes],
            "blocking_codes": [],
            "expires_at": manifest.expires_at,
        }
