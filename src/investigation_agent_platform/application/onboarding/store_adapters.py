# src/investigation_agent_platform/application/onboarding/store_adapters.py
"""Store adapters for deletion enumeration and execution (Phase 5, D7).

Adapters implement enumerate/plan/apply/verify with versioned schemas.
Sample adapters are honest in-memory implementations with idempotent apply:
repeating an applied idempotency key succeeds without duplicating effects.
Unsupported required classes report UNSUPPORTED inventory mode, which blocks
approval instead of completing silently.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Protocol

from investigation_agent_platform.application.onboarding.deletion import (
    InventoryCompleteness,
    InventoryMode,
    OwnershipClass,
    ResourceAction,
    StoreInventoryResult,
    StoreResourcePlan,
)


class StoreAdapter(Protocol):
    """Per-store enumerate/plan/apply contract."""

    @property
    def store_id(self) -> str: ...

    @property
    def store_type(self) -> str: ...

    @property
    def adapter_version(self) -> str: ...

    def enumerate(
        self, *, tenant_id: str, application_id: str | None, environment: str
    ) -> StoreInventoryResult: ...

    def apply(self, plan: StoreResourcePlan) -> str:
        """Execute one plan entry idempotently; return completion evidence ref."""
        ...


class InMemoryStoreAdapter:
    """Sample store: tenant-scoped resources with exact enumeration.

    Resources are ``(resource_type, resource_ref, ownership)`` tuples.
    ``apply`` honors ownership and action semantics: EXTERNAL_CUSTOMER rows
    are never deleted (RETAIN/DETACH only); SHARED_REFERENCE rows detach
    while referenced and delete once the reference count reaches zero.
    """

    def __init__(
        self,
        store_id: str,
        store_type: str,
        *,
        adapter_version: str = "v0.1",
        inventory_mode: InventoryMode = InventoryMode.EXACT_ENUMERATION,
    ) -> None:
        self._store_id = store_id
        self._store_type = store_type
        self._adapter_version = adapter_version
        self._inventory_mode = inventory_mode
        self._resources: dict[str, dict[str, Any]] = {}
        self._applied_keys: set[str] = set()
        self._refcounts: dict[str, int] = {}

    @property
    def store_id(self) -> str:
        return self._store_id

    @property
    def store_type(self) -> str:
        return self._store_type

    @property
    def adapter_version(self) -> str:
        return self._adapter_version

    def seed(
        self,
        resource_type: str,
        resource_ref: str,
        ownership: OwnershipClass = OwnershipClass.TENANT_OWNED,
        refcount: int = 0,
    ) -> None:
        """Test/operator seeding only; production adapters enumerate live stores."""
        self._resources[resource_ref] = {
            "resource_type": resource_type,
            "ownership": ownership,
            "refcount": refcount,
        }

    def enumerate(
        self, *, tenant_id: str, application_id: str | None, environment: str
    ) -> StoreInventoryResult:
        if self._inventory_mode == InventoryMode.UNSUPPORTED:
            return StoreInventoryResult(
                store_id=self._store_id,
                adapter_version=self._adapter_version,
                enumerated_at=datetime.now(UTC),
                inventory_mode=self._inventory_mode,
                completeness=InventoryCompleteness.UNAVAILABLE,
                resource_count=0,
                inventory_version="inv-v0.1",
                plan_entries=(),
                diagnostic_code="UNSUPPORTED_STORE",
            )
        entries: list[StoreResourcePlan] = []
        for ref, record in sorted(self._resources.items()):
            ownership = record["ownership"]
            action = (
                ResourceAction.RETAIN
                if ownership == OwnershipClass.EXTERNAL_CUSTOMER
                else ResourceAction.DELETE
            )
            entries.append(
                StoreResourcePlan(
                    store_id=self._store_id,
                    store_type=self._store_type,
                    adapter_version=self._adapter_version,
                    resource_type=record["resource_type"],
                    resource_ref=ref,
                    tenant_id=tenant_id,
                    application_id=application_id,
                    environment=environment,
                    ownership=ownership,
                    action=action,
                    idempotency_key=f"{self._store_id}:{ref}",
                    retention_decision="purge-eligible"
                    if action == ResourceAction.DELETE
                    else "retain-per-policy",
                    legal_hold=False,
                    evidence_ref=None,
                )
            )
        return StoreInventoryResult(
            store_id=self._store_id,
            adapter_version=self._adapter_version,
            enumerated_at=datetime.now(UTC),
            inventory_mode=self._inventory_mode,
            completeness=InventoryCompleteness.COMPLETE,
            resource_count=len(entries),
            inventory_version="inv-v0.1",
            plan_entries=tuple(entries),
        )

    def apply(self, plan: StoreResourcePlan) -> str:
        """Idempotent apply; returns a completion evidence reference."""
        if plan.idempotency_key in self._applied_keys:
            return f"already-applied:{plan.idempotency_key}"
        record = self._resources.get(plan.resource_ref)
        if record is None:
            self._applied_keys.add(plan.idempotency_key)
            return f"already-absent:{plan.resource_ref}"
        if plan.action == ResourceAction.DELETE:
            if record["ownership"] == OwnershipClass.EXTERNAL_CUSTOMER:
                raise ValueError("external customer data is never deleted by IAP")
            if (
                record["ownership"] == OwnershipClass.SHARED_REFERENCE
                and record.get("refcount", 0) > 0
            ):
                record["refcount"] -= 1
                self._applied_keys.add(plan.idempotency_key)
                return f"detached:{plan.resource_ref}:remaining={record['refcount']}"
            del self._resources[plan.resource_ref]
        self._applied_keys.add(plan.idempotency_key)
        return f"{plan.action.value.lower()}:{plan.resource_ref}"

    def resource_count(self) -> int:
        """Remaining live resources (test/operator visibility only)."""
        return len(self._resources)


class StoreAdapterRegistry:
    """Maps store ids to adapters. Unknown stores raise."""

    def __init__(self, adapters: list[StoreAdapter] | None = None) -> None:
        self._adapters: dict[str, StoreAdapter] = {}
        for adapter in adapters or []:
            if adapter.store_id in self._adapters:
                raise ValueError(f"duplicate adapter for store {adapter.store_id!r}")
            self._adapters[adapter.store_id] = adapter

    def adapter_for(self, store_id: str) -> StoreAdapter:
        """Return the adapter for a store, or raise a closed error."""
        try:
            return self._adapters[store_id]
        except KeyError:
            raise KeyError(f"store {store_id!r} has no registered adapter") from None
