"""Phase 5 contract tests: manifest hashing, approval, blockers, adapters."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest

from investigation_agent_platform.application.onboarding.deletion import (
    DeletionStatus,
    DeploymentRegistry,
    InventoryCompleteness,
    InventoryMode,
    OwnershipClass,
    ResourceAction,
    StoreInventoryResult,
    StoreRegistryEntry,
    approval_blockers,
    approval_matches,
    build_approval_payload,
    canonical_approval_digest,
    canonical_plan_hash,
    mint_approval_token,
)
from investigation_agent_platform.application.onboarding.store_adapters import (
    InMemoryStoreAdapter,
    StoreAdapterRegistry,
)
from investigation_agent_platform.infrastructure.persistence.onboarding_deletion_repository import (
    evaluate_terminal_state,
)


def _manifest_fields() -> dict[str, Any]:
    return {
        "manifest_id": "11111111-1111-1111-1111-111111111111",
        "tenant_id": "tenant-a",
        "application_id": "app-1",
        "environment": "prod",
        "plan_version": 1,
        "store_registry_version": "reg-v1",
        "policy_version": "p1",
        "inventory": [
            {
                "store_id": "b-store",
                "plan_entries": [
                    {"store_id": "b-store", "resource_type": "t", "resource_ref": "r2"},
                    {"store_id": "b-store", "resource_type": "t", "resource_ref": "r1"},
                ],
            },
            {
                "store_id": "a-store",
                "plan_entries": [
                    {"store_id": "a-store", "resource_type": "t", "resource_ref": "r1"},
                ],
            },
        ],
        "backup_id": "backup-1",
        "requester_id": "req-1",
        "approver_ids": ["app-1"],
        "expires_at": "2026-01-01T00:00:00+00:00",
    }


def test_canonical_plan_hash_is_order_insensitive_and_pinned() -> None:
    left = canonical_plan_hash(_manifest_fields())
    assert len(left) == 64
    assert canonical_plan_hash(_manifest_fields()) == left
    changed = _manifest_fields()
    changed["inventory"][0]["plan_entries"].append(
        {"store_id": "b-store", "resource_type": "t", "resource_ref": "r3"}
    )
    assert canonical_plan_hash(changed) != left
    # Outcomes and volatile metadata are never plan inputs: same plan, same hash.
    assert "canonical_plan_hash" not in _manifest_fields()


def test_approval_token_binds_exact_payload() -> None:
    manifest_id = uuid4()
    base: dict[str, Any] = {
        "manifest_id": manifest_id,
        "plan_hash": "a" * 64,
        "tenant_id": "tenant-a",
        "application_id": "app-1",
        "environment": "prod",
        "requester_id": "req-1",
        "approver_id": "approver-1",
        "policy_version": "p1",
        "store_registry_version": "reg-v1",
        "expires_at": datetime.now(UTC) + timedelta(hours=1),
        "nonce": "n" * 32,
    }
    payload = build_approval_payload(**base)
    token, token_digest = mint_approval_token()
    payload_digest = canonical_approval_digest(payload)
    assert token_digest != token  # digest stored, never the raw token
    assert approval_matches(
        presented_token=token,
        stored_token_digest=token_digest,
        payload=payload,
        stored_payload_digest=payload_digest,
        authenticated_approver_id="approver-1",
    )


def test_approval_rejects_tamper_expiry_and_wrong_approver() -> None:
    manifest_id = uuid4()
    base: dict[str, Any] = {
        "manifest_id": manifest_id,
        "plan_hash": "a" * 64,
        "tenant_id": "tenant-a",
        "application_id": "app-1",
        "environment": "prod",
        "requester_id": "req-1",
        "approver_id": "approver-1",
        "policy_version": "p1",
        "store_registry_version": "reg-v1",
        "expires_at": datetime.now(UTC) + timedelta(hours=1),
        "nonce": "n" * 32,
    }
    payload = build_approval_payload(**base)
    token, token_digest = mint_approval_token()
    payload_digest = canonical_approval_digest(payload)
    assert not approval_matches(
        presented_token="wrong-token",
        stored_token_digest=token_digest,
        payload=payload,
        stored_payload_digest=payload_digest,
        authenticated_approver_id="approver-1",
    )
    assert not approval_matches(
        presented_token=token,
        stored_token_digest=token_digest,
        payload=payload,
        stored_payload_digest=payload_digest,
        authenticated_approver_id="self-approver",
    )
    expired = build_approval_payload(
        **{**base, "expires_at": datetime.now(UTC) - timedelta(seconds=1)}
    )
    assert not approval_matches(
        presented_token=token,
        stored_token_digest=token_digest,
        payload=expired,
        stored_payload_digest=canonical_approval_digest(expired),
        authenticated_approver_id="approver-1",
    )
    tampered = payload.model_copy(update={"plan_hash": "b" * 64})
    assert not approval_matches(
        presented_token=token,
        stored_token_digest=token_digest,
        payload=tampered,
        stored_payload_digest=payload_digest,
        authenticated_approver_id="approver-1",
    )


def test_approval_payload_rejects_naive_expiry_and_empty_nonce() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        build_approval_payload(
            manifest_id=uuid4(),
            plan_hash="a" * 64,
            tenant_id="t",
            application_id=None,
            environment="e",
            requester_id="r",
            approver_id="a",
            policy_version="p",
            store_registry_version="s",
            expires_at=datetime(2026, 1, 1),
            nonce="n" * 32,
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
        deployment_id="dep-1", registry_version="reg-v1", entries=(entry("s1"), entry("s2"))
    )


def _inventory_result(store_id: str, **overrides: Any) -> StoreInventoryResult:
    base: dict[str, Any] = {
        "store_id": store_id,
        "adapter_version": "v0.1",
        "enumerated_at": datetime.now(UTC),
        "inventory_mode": InventoryMode.EXACT_ENUMERATION,
        "completeness": InventoryCompleteness.COMPLETE,
        "resource_count": 0,
        "inventory_version": "inv-v0.1",
        "plan_entries": (),
    }
    base.update(overrides)
    return StoreInventoryResult(**base)


def test_approval_blockers_cover_missing_stale_and_partial() -> None:
    registry = _registry()
    assert approval_blockers(registry, [_inventory_result("s1"), _inventory_result("s2")]) == []
    missing = approval_blockers(registry, [_inventory_result("s1")])
    assert missing == ["S2_NOT_ENUMERATED"]
    stale = approval_blockers(
        registry, [_inventory_result("s1"), _inventory_result("s2", adapter_version="v9")]
    )
    assert stale == ["S2_STALE_ADAPTER"]
    partial = approval_blockers(
        registry,
        [
            _inventory_result("s1"),
            _inventory_result("s2", completeness=InventoryCompleteness.INCOMPLETE),
        ],
    )
    assert partial == ["S2_INVENTORY_INCOMPLETE"]


def test_terminal_states_are_honest() -> None:
    assert evaluate_terminal_state([]) == DeletionStatus.INCOMPLETE.value
    assert evaluate_terminal_state(["DONE", "DONE"]) == DeletionStatus.COMPLETE.value
    assert (
        evaluate_terminal_state(["DONE", "RETAINED_BY_POLICY"])
        == DeletionStatus.COMPLETE_WITH_RETENTION.value
    )
    assert (
        evaluate_terminal_state(["DONE", "APPROVED_RESIDUAL"])
        == DeletionStatus.COMPLETE_WITH_APPROVED_RESIDUALS.value
    )
    assert evaluate_terminal_state(["DONE", "BLOCKED"]) == DeletionStatus.BLOCKED.value
    assert evaluate_terminal_state(["DONE", "CANCELED"]) == DeletionStatus.CANCELED.value
    assert evaluate_terminal_state(["DONE", "FAILED"]) == DeletionStatus.INCOMPLETE.value


def test_sample_adapter_enumerate_apply_idempotent_and_safe() -> None:
    adapter = InMemoryStoreAdapter("s1", "object")
    adapter.seed("blob", "r1")
    adapter.seed("blob", "shared", OwnershipClass.SHARED_REFERENCE, refcount=1)
    adapter.seed("blob", "ext", OwnershipClass.EXTERNAL_CUSTOMER)
    inventory = adapter.enumerate(tenant_id="t", application_id=None, environment="e")
    assert inventory.completeness == InventoryCompleteness.COMPLETE
    assert inventory.resource_count == 3
    by_ref = {plan.resource_ref: plan for plan in inventory.plan_entries}
    assert by_ref["ext"].action == ResourceAction.RETAIN

    assert adapter.apply(by_ref["r1"]).startswith("delete:")
    assert adapter.apply(by_ref["r1"]).startswith("already-applied:")
    assert adapter.resource_count() == 2
    assert adapter.apply(by_ref["shared"]).startswith("detached:")
    with pytest.raises(ValueError, match="never deleted"):
        adapter.apply(by_ref["ext"].model_copy(update={"action": ResourceAction.DELETE}))
    assert adapter.apply(by_ref["ext"]).startswith("retain:")
    with pytest.raises(KeyError, match="no registered adapter"):
        StoreAdapterRegistry([adapter]).adapter_for("nope")
    with pytest.raises(ValueError, match="duplicate adapter"):
        StoreAdapterRegistry([adapter, InMemoryStoreAdapter("s1", "object")])


def test_unsupported_store_blocks_instead_of_completing() -> None:
    from investigation_agent_platform.application.onboarding.deletion import InventoryMode

    adapter = InMemoryStoreAdapter("s9", "x", inventory_mode=InventoryMode.UNSUPPORTED)
    inventory = adapter.enumerate(tenant_id="t", application_id=None, environment="e")
    assert inventory.completeness == InventoryCompleteness.UNAVAILABLE
