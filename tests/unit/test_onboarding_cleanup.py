"""Unit tests for the cleanup service (fakes, no database)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from investigation_agent_platform.application.onboarding.cleanup import CleanupService
from investigation_agent_platform.application.onboarding.deletion import (
    DeploymentRegistry,
    StoreRegistryEntry,
)
from investigation_agent_platform.application.onboarding.service import ServiceError
from investigation_agent_platform.application.onboarding.store_adapters import (
    InMemoryStoreAdapter,
    StoreAdapterRegistry,
)


def _registry() -> DeploymentRegistry:
    def entry(store_id: str) -> StoreRegistryEntry:
        return StoreRegistryEntry(
            deployment_id="dep-1",
            store_id=store_id,
            store_type="object",
            adapter_version="v0.1",
            registry_version="reg-v1",
        )

    return DeploymentRegistry(
        deployment_id="dep-1", registry_version="reg-v1", entries=(entry("s1"),)
    )


class _Deletions:
    def __init__(self) -> None:
        self.manifests: dict[str, SimpleNamespace] = {}
        self.approvals: dict[str, list[SimpleNamespace]] = {}
        self.outcomes: list[SimpleNamespace] = []
        self.states: dict[str, str] = {}

    async def save_manifest(self, **kwargs: Any) -> SimpleNamespace:
        row = SimpleNamespace(
            manifest_id=kwargs["manifest_id"],
            plan_hash=kwargs["plan_hash"],
            state=kwargs["state"],
            manifest_json=kwargs["manifest_json"],
        )
        self.manifests[str(kwargs["manifest_id"])] = row
        self.states[str(kwargs["manifest_id"])] = kwargs["state"]
        return row

    async def read_manifest(self, tenant_id: str, manifest_id: UUID) -> Any:
        row = self.manifests.get(str(manifest_id))
        if row is None:
            return None
        return SimpleNamespace(
            manifest_id=manifest_id,
            plan_hash=row.plan_hash,
            state=self.states[str(manifest_id)],
            application_id="app-1",
            environment="prod",
            manifest_json=row.manifest_json,
        )

    async def update_manifest_state(self, tenant_id: str, manifest_id: UUID, state: str) -> bool:
        self.states[str(manifest_id)] = state
        return True

    async def mint_approval(self, **kwargs: Any) -> tuple[Any, str]:
        from investigation_agent_platform.application.onboarding.deletion import (
            canonical_approval_digest,
            mint_approval_token,
        )

        payload = kwargs["payload"]
        _token, token_digest = mint_approval_token()
        row = SimpleNamespace(
            manifest_hash="p" * 64,
            token_digest=token_digest,
            payload_digest=canonical_approval_digest(payload),
            requester_id=payload.requester_id,
            approver_id=payload.approver_id,
            nonce=payload.nonce,
            expires_at=payload.expires_at,
            policy_version=payload.policy_version,
            store_registry_version=payload.store_registry_version,
            consumed_at=None,
        )
        self.approvals.setdefault(str(payload.manifest_id), []).append(row)
        return row, "token-plaintext"

    async def list_approvals(self, tenant_id: str, manifest_id: UUID) -> list[Any]:
        return self.approvals.get(str(manifest_id), [])

    async def consume_approval(self, **kwargs: Any) -> SimpleNamespace:
        rows = self.approvals.get(str(kwargs["manifest_id"]), [])
        if not rows:
            from investigation_agent_platform.infrastructure.persistence.onboarding_deletion_repository import (
                DeletionApprovalError,
            )

            raise DeletionApprovalError("no approval")
        rows[0].consumed_at = datetime.now(UTC)
        return SimpleNamespace(purge_job_id=uuid4())

    async def record_outcome(self, **kwargs: Any) -> SimpleNamespace:
        row = SimpleNamespace(**kwargs)
        self.outcomes.append(row)
        return row

    async def list_outcome_states(self, tenant_id: str, manifest_id: UUID) -> list[Any]:
        seen: dict[str, str] = {}
        for outcome in self.outcomes:
            if str(outcome.manifest_id) == str(manifest_id):
                seen[outcome.entry_key] = outcome.state
        return list(seen.items())


def _service(adapter: Any = None) -> CleanupService:
    adapter = adapter or InMemoryStoreAdapter("s1", "object")
    return CleanupService(deletions=_Deletions(), adapter_registry=StoreAdapterRegistry([adapter]))


async def test_preview_persists_manifest_with_plan_hash() -> None:
    service = _service()
    receipt = await service.preview(
        tenant_id="tenant-a",
        application_id="app-1",
        environment="prod",
        deployment_id="dep-1",
        registry=_registry(),
        policy_version="p1",
        requester_id="req-1",
        approver_ids=["approver-1"],
        backup_id="backup-1",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    assert len(receipt["plan_hash"]) == 64
    assert receipt["state"] == "APPROVAL_PENDING"
    assert receipt["blocking_codes"] == []


async def test_preview_blocks_on_missing_store() -> None:
    service = _service()
    registry = DeploymentRegistry(
        deployment_id="dep-1",
        registry_version="reg-v1",
        entries=(
            StoreRegistryEntry(
                deployment_id="dep-1",
                store_id="ghost",
                store_type="object",
                adapter_version="v0.1",
                registry_version="reg-v1",
            ),
        ),
    )
    with pytest.raises(KeyError, match="no registered adapter"):
        await service.preview(
            tenant_id="tenant-a",
            application_id=None,
            environment="prod",
            deployment_id="dep-1",
            registry=registry,
            policy_version="p1",
            requester_id="req-1",
            approver_ids=["a-1"],
            backup_id="b-1",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )


async def test_mint_rejects_self_approval() -> None:
    service = _service()
    preview = await service.preview(
        tenant_id="tenant-a",
        application_id=None,
        environment="prod",
        deployment_id="dep-1",
        registry=_registry(),
        policy_version="p1",
        requester_id="same-person",
        approver_ids=["same-person"],
        backup_id="b-1",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    with pytest.raises(ServiceError) as exc:
        await service.mint_approval(
            tenant_id="tenant-a",
            manifest_id=preview["manifest_id"],
            approver_id="same-person",
            requester_id="same-person",
            policy_version="p1",
            store_registry_version="reg-v1",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
    assert exc.value.code == "SEPARATION_OF_DUTIES"


async def test_run_purge_requires_consumed_approval() -> None:
    service = _service()
    with pytest.raises(ServiceError) as exc:
        await service.run_purge(tenant_id="tenant-a", manifest_id=uuid4())
    assert exc.value.code in ("REQUEST_NOT_FOUND", "APPROVAL_INVALID")


async def test_run_purge_completes_and_finalizes() -> None:
    adapter = InMemoryStoreAdapter("s1", "object")
    adapter.seed("blob", "r1")
    service = _service(adapter)
    preview = await service.preview(
        tenant_id="tenant-a",
        application_id="app-1",
        environment="prod",
        deployment_id="dep-1",
        registry=_registry(),
        policy_version="p1",
        requester_id="req-1",
        approver_ids=["approver-1"],
        backup_id="b-1",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    manifest_id = preview["manifest_id"]
    minted = await service.mint_approval(
        tenant_id="tenant-a",
        manifest_id=manifest_id,
        approver_id="approver-1",
        requester_id="req-1",
        policy_version="p1",
        store_registry_version="reg-v1",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    assert minted["consumed"] is False
    # Simulate workflow-authority consumption.
    service._deletions.approvals[str(manifest_id)][0].consumed_at = datetime.now(UTC)
    service._deletions.states[str(manifest_id)] = "QUEUED"
    result = await service.run_purge(tenant_id="tenant-a", manifest_id=manifest_id)
    assert result["terminal_state"] == "COMPLETE"
    assert result["entries"] == 1
    assert adapter.resource_count() == 0
