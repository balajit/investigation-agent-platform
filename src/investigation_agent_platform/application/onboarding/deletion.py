# src/investigation_agent_platform/application/onboarding/deletion.py
"""Deletion store registry, manifest, and approval contracts (Phase 5, D7/D8).

Cleanup is a durable exact-plan workflow: each deployment has a versioned
registry of required store adapters; a preview enumerates every applicable
required store and produces complete evidence; approval binds one canonical
payload and is consumed atomically once. Terminal states distinguish
complete, retained, approved-residual, incomplete, blocked, and canceled --
no residual is ever described as erased.
"""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Mapping
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from investigation_agent_platform.application.onboarding_agent.contracts.linkage import (
    canonical_json_bytes,
)

MANIFEST_CANONICALIZATION_VERSION = "rfc8785-v1"
MANIFEST_DOMAIN = b"IAP-TENANT-ONBOARDING-MANIFEST\x00v1\x00"
APPROVAL_DOMAIN = b"IAP-TENANT-ONBOARDING-APPROVAL\x00v1\x00"


class ResourceAction(StrEnum):
    DELETE = "DELETE"
    DETACH = "DETACH"
    REVOKE = "REVOKE"
    RETAIN = "RETAIN"
    EXPIRE = "EXPIRE"


class OwnershipClass(StrEnum):
    TENANT_OWNED = "TENANT_OWNED"
    SHARED_REFERENCE = "SHARED_REFERENCE"
    DERIVED_CACHE = "DERIVED_CACHE"
    EXTERNAL_CUSTOMER = "EXTERNAL_CUSTOMER"
    PLATFORM_AUDIT = "PLATFORM_AUDIT"


class InventoryCompleteness(StrEnum):
    COMPLETE = "COMPLETE"
    INCOMPLETE = "INCOMPLETE"
    UNAVAILABLE = "UNAVAILABLE"


class InventoryMode(StrEnum):
    EXACT_ENUMERATION = "EXACT_ENUMERATION"
    SCOPED_REVOCATION = "SCOPED_REVOCATION"
    POLICY_EXPIRY = "POLICY_EXPIRY"
    UNSUPPORTED = "UNSUPPORTED"


class DeletionStatus(StrEnum):
    PREVIEW = "PREVIEW"
    APPROVAL_PENDING = "APPROVAL_PENDING"
    QUEUED = "QUEUED"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETE = "COMPLETE"
    COMPLETE_WITH_RETENTION = "COMPLETE_WITH_RETENTION"
    COMPLETE_WITH_APPROVED_RESIDUALS = "COMPLETE_WITH_APPROVED_RESIDUALS"
    INCOMPLETE = "INCOMPLETE"
    BLOCKED = "BLOCKED"
    CANCELED = "CANCELED"


class StoreResourcePlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    store_id: str = Field(min_length=1, max_length=128)
    store_type: str = Field(min_length=1, max_length=64)
    adapter_version: str = Field(min_length=1, max_length=32)
    resource_type: str = Field(min_length=1, max_length=128)
    resource_ref: str = Field(min_length=1, max_length=1024)
    tenant_id: str = Field(min_length=1, max_length=64)
    application_id: str | None = Field(default=None, max_length=128)
    environment: str = Field(min_length=1, max_length=64)
    ownership: OwnershipClass
    action: ResourceAction
    idempotency_key: str = Field(min_length=1, max_length=256)
    retention_decision: str = Field(min_length=1, max_length=64)
    legal_hold: bool
    evidence_ref: str | None = Field(default=None, max_length=512)


class StoreInventoryResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    store_id: str
    adapter_version: str
    enumerated_at: datetime
    inventory_mode: InventoryMode
    completeness: InventoryCompleteness
    resource_count: int = Field(ge=0)
    inventory_version: str
    plan_entries: tuple[StoreResourcePlan, ...]
    diagnostic_code: str | None = None


class DeletionManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    manifest_id: UUID
    schema_version: str = Field(min_length=1, max_length=32)
    canonicalization_version: str = Field(min_length=1, max_length=32)
    hash_algorithm: str = "SHA-256"
    tenant_id: str
    application_id: str | None = None
    environment: str
    plan_version: int = Field(ge=1)
    store_registry_version: str
    policy_version: str
    inventory: tuple[StoreInventoryResult, ...]
    backup_id: str
    backup_verified_at: datetime
    requester_id: str
    approver_ids: tuple[str, ...]
    expires_at: datetime
    canonical_plan_hash: str


class ApprovalPayload(BaseModel):
    """The only payload mint and validate both accept."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    canonicalization_version: str = Field(min_length=1, max_length=32)
    manifest_id: UUID
    plan_hash: str = Field(min_length=64, max_length=64)
    tenant_id: str = Field(min_length=1, max_length=64)
    application_id: str | None = Field(default=None, max_length=128)
    environment: str = Field(min_length=1, max_length=64)
    requester_id: str = Field(min_length=1, max_length=256)
    approver_id: str = Field(min_length=1, max_length=256)
    policy_version: str = Field(min_length=1, max_length=32)
    store_registry_version: str = Field(min_length=1, max_length=32)
    expires_at: datetime
    nonce: str = Field(min_length=32, max_length=128)


def canonical_plan_hash(manifest_fields: Mapping[str, Any]) -> str:
    """Hash the canonical manifest plan with domain separation.

    Unordered resource collections are sorted by stable keys first;
    meaningful order is preserved by the caller. Outcomes and volatile
    metadata are never plan inputs -- pass only the approved plan fields.
    """
    normalized = _sort_plan_collections(dict(manifest_fields))
    normalized["canonicalization_version"] = MANIFEST_CANONICALIZATION_VERSION
    return hashlib.sha256(MANIFEST_DOMAIN + canonical_json_bytes(normalized)).hexdigest()


def _sort_plan_collections(fields: dict[str, Any]) -> dict[str, Any]:
    inventory = fields.get("inventory")
    if isinstance(inventory, list):
        fields["inventory"] = sorted(
            (
                {
                    **entry,
                    "plan_entries": sorted(
                        entry.get("plan_entries", []),
                        key=lambda e: (
                            e.get("store_id", ""),
                            e.get("resource_type", ""),
                            e.get("resource_ref", ""),
                        ),
                    ),
                }
                if isinstance(entry, dict)
                else entry
                for entry in inventory
            ),
            key=lambda e: e.get("store_id", "") if isinstance(e, dict) else "",
        )
    return fields


def build_approval_payload(
    *,
    manifest_id: UUID,
    plan_hash: str,
    tenant_id: str,
    application_id: str | None,
    environment: str,
    requester_id: str,
    approver_id: str,
    policy_version: str,
    store_registry_version: str,
    expires_at: datetime,
    nonce: str,
) -> ApprovalPayload:
    """Build the only payload accepted by mint and validate operations."""
    if expires_at.tzinfo is None:
        raise ValueError("expires_at must be timezone-aware")
    if not nonce:
        raise ValueError("nonce must not be empty")
    return ApprovalPayload(
        canonicalization_version=MANIFEST_CANONICALIZATION_VERSION,
        manifest_id=manifest_id,
        plan_hash=plan_hash,
        tenant_id=tenant_id,
        application_id=application_id,
        environment=environment,
        requester_id=requester_id,
        approver_id=approver_id,
        policy_version=policy_version,
        store_registry_version=store_registry_version,
        expires_at=expires_at,
        nonce=nonce,
    )


def canonical_approval_digest(payload: ApprovalPayload) -> str:
    """Digest over the canonical approval payload with domain separation."""
    return hashlib.sha256(
        APPROVAL_DOMAIN + canonical_json_bytes(payload.model_dump(mode="json"))
    ).hexdigest()


def mint_approval_token() -> tuple[str, str]:
    """Return one-time token and stored digest. Never persist the raw token."""
    token = secrets.token_urlsafe(32)
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return token, digest


def approval_matches(
    *,
    presented_token: str,
    stored_token_digest: str,
    payload: ApprovalPayload,
    stored_payload_digest: str,
    authenticated_approver_id: str,
    now: datetime | None = None,
) -> bool:
    """Validate exact payload, actual approver identity, token, and expiry."""
    if payload.approver_id != authenticated_approver_id:
        return False
    current = now or datetime.now(UTC)
    if current >= payload.expires_at.astimezone(UTC):
        return False
    token_digest = hashlib.sha256(presented_token.encode("utf-8")).hexdigest()
    payload_digest = canonical_approval_digest(payload)
    return secrets.compare_digest(token_digest, stored_token_digest) and secrets.compare_digest(
        payload_digest, stored_payload_digest
    )


class StoreRegistryEntry(BaseModel):
    """One required store adapter in a deployment registry version."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    deployment_id: str = Field(min_length=1, max_length=128)
    store_id: str = Field(min_length=1, max_length=128)
    store_type: str = Field(min_length=1, max_length=64)
    adapter_version: str = Field(min_length=1, max_length=32)
    registry_version: str = Field(min_length=1, max_length=32)
    required_for_deletion: bool = True
    enabled: bool = True
    capabilities_json: dict[str, Any] = Field(default_factory=dict)


class DeploymentRegistry(BaseModel):
    """Versioned deployment inventory declaring required cleanup adapters."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    deployment_id: str = Field(min_length=1, max_length=128)
    registry_version: str = Field(min_length=1, max_length=32)
    entries: tuple[StoreRegistryEntry, ...] = Field(min_length=1)

    def required_stores(self) -> list[StoreRegistryEntry]:
        """Enabled stores required for deletion approval."""
        return [e for e in self.entries if e.required_for_deletion and e.enabled]


def approval_blockers(
    registry: DeploymentRegistry,
    inventory: list[StoreInventoryResult],
) -> list[str]:
    """Block approval unless every required store enumerated completely.

    Missing adapters, stale registry versions, partial enumeration, provider
    timeouts, and unknown ownership all block -- never silently succeed.
    """
    blockers: list[str] = []
    by_store = {result.store_id: result for result in inventory}
    for entry in registry.required_stores():
        code_prefix = entry.store_id.upper().replace("-", "_")
        result = by_store.get(entry.store_id)
        if result is None:
            blockers.append(f"{code_prefix}_NOT_ENUMERATED")
            continue
        if result.adapter_version != entry.adapter_version:
            blockers.append(f"{code_prefix}_STALE_ADAPTER")
        if result.completeness != InventoryCompleteness.COMPLETE:
            blockers.append(f"{code_prefix}_INVENTORY_{result.completeness.value}")
        for plan in result.plan_entries:
            if plan.legal_hold and plan.action == ResourceAction.DELETE:
                blockers.append(f"{code_prefix}_LEGAL_HOLD_BLOCKS")
                break
    return blockers
