# Design: IAP Tenant Configuration Onboarding — Luna Review Revision

IAP-scoped tenant configuration onboarding with an explicit trust boundary, canonical tenant registry, operation capabilities, immutable intake revisions, policy-driven readiness, durable Temporal orchestration, transactional-outbox dispatch, and deletion manifests that prove per-store inventory and outcome coverage. This design does not provision customer identity, subscriptions, networks, DNS, certificates, or infrastructure; those remain external authorities.

## Context

IAP is a multi-tenant investigation platform operating as a shared API/worker fleet. Tenant identity is authenticated from verified JWT claims in production; tenant-bearing relational data uses PostgreSQL/RLS and tenant predicates; background jobs use Temporal execution with a PostgreSQL queryable read model; application profiles have their own revision and activation lifecycle. Operator-home scope files are organized for operator capture and environment configuration, not as shared-fleet state.

The current tenant-onboarding proposal establishes the right high-level scope and adds canonical identity, capabilities, immutable intake revisions, lifecycle states, typed secret references, constrained outbound integration, adapter probes, cleanup approval, and an implementation plan. This revision incorporates the remaining architecture review findings: capability operations must map to lifecycle gates and owners; dispatch must use a same-transaction outbox rather than leave “equivalent dispatcher” undefined; cleanup must enumerate a configured store registry and bind exact per-store resource plans; and the example approval-token algorithm must bind one canonical payload consistently.

This is a design proposal. It does not assert that onboarding code, tables, adapters, outbox dispatch, store inventory, approval logic, or tests currently exist.

### `docs/dev/architecture.md` changes

No `docs/dev/architecture.md` exists in the project. On implementation, update `docs/IAP-architecture-v1.0.md` with the module scope and authority boundaries; tenant registry and capability model; onboarding state machine and capability matrix; control-store, outbox, and workflow data flow; adapter catalog and readiness rules; deletion store registry and manifest protocol; and operations/metrics. Document rollout flags and defaults alongside operator-home behavior.

## References

- [IAP architecture](./IAP-architecture-v1.0.md) — shared-fleet deployment, Temporal, PostgreSQL/RLS, background jobs, providers, caches, artifacts, and operator-home conventions.
- [Tenant onboarding design revision 2](./IAP-TENANT-ONBOARDING-DESIGN.md) — source proposal and decisions D1–D9 incorporated and tightened here.
- [Tenant onboarding implementation plan revision 2](./IAP-TENANT-ONBOARDING-IMPL-PLAN.md) — source phase sequence and acceptance criteria, expanded here for outbox and complete deletion coverage.
- [Luna implementation plan](./IAP-TENANT-ONBOARDING-IMPL-PLAN-luna.md) — phased build sequence and gates aligned with this design.
- [Tenant onboarding architecture review](./IAP-TENANT-ONBOARDING-ARCHITECTURE-REVIEW.md) — critical review findings, including the capability matrix, dispatch choice, manifest coverage, and approval-token defect.
- [Background job contract](./IAP-implementation-part11-knowledge-v1.0.md) — Temporal workflow authority and PostgreSQL job read-model conventions.
- [Environment matrix](../config/env-matrix.md) — environment contract for any onboarding feature flags or settings introduced during implementation.

## Goals / Non-Goals

**Goals:**

- Own onboarding coordination for IAP tenant configuration only; preserve external system authority.
- Issue immutable canonical tenant identifiers and keep mutable display slugs separate.
- Enforce operation capabilities separately from tenant identity, with a safe platform-operator bootstrap flow.
- Store immutable, versioned intake, approval, policy snapshot, workflow dispatch, step execution, and deletion-plan state in shared durable storage.
- Dispatch workflows by transactional outbox in the same transaction as the accepted request, job read model, idempotency reservation, and audit linkage.
- Define a capability catalog mapping each supported connector and lifecycle operation to owner, authorization, API/CLI, state, readiness, recovery, and deletion policy.
- Run real, scoped adapter probes and gate activation on explicit mandatory/optional policy.
- Plan deletion through a versioned store registry and exact per-store/resource manifest, with hold, backup, retention, shared-reference, completion, and residual semantics.
- Prove behavior through failure-injection, cross-tenant, store-coverage, restore, and CLI/API conformance tests.

**Non-Goals:**

- Provision customer identity/SSO, subscriptions, billing, entitlements, DNS, certificates, networks, compute, storage, databases, or applications.
- Delete or mutate customer-owned data in external source systems; detach IAP access and remove IAP-owned derived copies only.
- Introduce a second workflow engine, a general-purpose provisioning platform, or a new message broker.
- Support tenant migration between hosting classes, self-service onboarding, or dedicated/hybrid infrastructure in the initial release.
- Treat database transaction boundaries as atomic with Temporal or external provider side effects.

## Decisions

### D1: Scope, identity authority, and operation capabilities

**Decision:** The module coordinates IAP tenant configuration. The tenant registry issues an immutable opaque canonical tenant ID; display slug and aliases are metadata only. A caller must have a verified identity and explicit operation capability scoped to tenant, application, environment, and action. Tenant-claim equality is necessary for tenant operations but never sufficient for privileged onboarding, approval, support, or cleanup. New tenants without a tenant claim are reserved through an operator-controlled bootstrap invitation before tenant-scoped credentials are issued. Reserved IDs cannot be self-claimed or reused after retirement.

The capability vocabulary is `tenant:read`, `tenant:onboard`, `tenant:configure`, `tenant:approve`, `tenant:cleanup`, `tenant:suspend`, `tenant:resume`, and `tenant:support`. `tenant:approve` and destructive `tenant:cleanup` require policy-defined separation of duties; the requester cannot satisfy the second-person approval requirement. Break-glass support requires a reason, short-lived authorization, restricted action set, and immutable audit entry. Authorization policy is evaluated on each API request and again before workflow side effects that cross privilege boundaries.

```python
# src/investigation_agent_platform/application/onboarding/authorization.py
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Capability(StrEnum):
    READ = "tenant:read"
    ONBOARD = "tenant:onboard"
    CONFIGURE = "tenant:configure"
    APPROVE = "tenant:approve"
    CLEANUP = "tenant:cleanup"
    SUSPEND = "tenant:suspend"
    RESUME = "tenant:resume"
    SUPPORT = "tenant:support"


@dataclass(frozen=True)
class AuthorizationScope:
    tenant_id: str
    application_id: str | None
    environment: str
    operation: str


@dataclass(frozen=True)
class AuthorizationDecision:
    allowed: bool
    principal_id: str
    capability: Capability
    scope: AuthorizationScope
    reason_code: str


def authorize(
    *,
    principal_id: str,
    granted: frozenset[Capability],
    required: Capability,
    scope: AuthorizationScope,
    platform_operator: bool,
    requester_id: str | None = None,
) -> AuthorizationDecision:
    """Authorize one operation; tenant identity alone never grants capability."""
    operator_only = {Capability.ONBOARD, Capability.CLEANUP, Capability.APPROVE}
    if required in operator_only and not platform_operator:
        return AuthorizationDecision(False, principal_id, required, scope, "OPERATOR_ROLE_REQUIRED")
    if required not in granted:
        return AuthorizationDecision(False, principal_id, required, scope, "CAPABILITY_MISSING")
    if required in {Capability.APPROVE, Capability.CLEANUP}:
        if requester_id is not None and principal_id == requester_id:
            return AuthorizationDecision(False, principal_id, required, scope, "SEPARATION_OF_DUTIES")
    return AuthorizationDecision(True, principal_id, required, scope, "ALLOWED")
```

**Alternative considered:** Allow any user whose token contains the tenant claim to mutate onboarding state. Rejected because tenant context does not prove function-level permission and fails to distinguish ordinary tenant access from platform administration. A locally hard-coded `platform_operator` boolean is only illustrative; production derives roles from the verified identity/policy service and tests its freshness and failure behavior.

**Rationale:** The registry owns identity existence; the identity provider authenticates the principal; the authorization policy decides allowed operations. No display name or unverified header participates in identity or filesystem path decisions.

**Trade-offs:** A capability policy and bootstrap process require ownership, review, and audit. Initial onboarding remains operator-assisted until a customer delegation model is designed and security-reviewed.

---

### D2: Capability catalog and lifecycle traceability

**Decision:** Every supported onboarding capability is declared in a versioned catalog. A catalog entry names capability key/version, scope, source class, owning authority, required/optional policy by service tier, required capability, API/CLI entrypoint, request/tenant lifecycle states, readiness probe/freshness contract, retry and compensation behavior, data classification, and deletion action. An adapter not present in the catalog cannot be accepted from intake. A policy must choose exactly one status for every configured item: required, optional, not applicable, or unsupported. Unknown policy data fails closed before network or filesystem side effects. The complete catalog must include lifecycle operations as well as connector types: reservation, intake edit/submit, approval, apply, attach, retry, cancel, activation, suspend/resume, cleanup-plan, cleanup-approval, and status/read operations.

The initial supported classes are repository/profile, logs, traces, metrics, datasources, config servers, and read-only knowledge corpora. Identity, subscription, billing, DNS, certificate, and infrastructure integrations remain external and are represented only by references/eligibility evidence if a later approved scope requires them.

| Capability | Owner | Authorization | Readiness contract | State/API | Deletion action |
|---|---|---|---|---|---|
| Tenant reservation | Tenant registry | `tenant:onboard`, platform operator | Canonical ID reserved, unique and not retired | Registry `RESERVED`; bootstrap operation | Retain tombstone; never reuse canonical ID |
| Intake revision | Onboarding control store | `tenant:configure` or `tenant:onboard` | Schema, policy, limits, references, expected revision valid | Request `DRAFT/VALIDATING`; submit/update API | Retain minimal audit/hash; apply retention to payload |
| Repository/profile generation | Profile service and repository adapter | `tenant:onboard/configure` | Destination allowed, immutable commit pinned, generated profile validates | Request `PROVISIONING/READINESS_CHECK`; apply/attach | Detach tenant mapping; delete IAP-owned clone/cache by policy; preserve shared references |
| Logs/traces/metrics | Registered telemetry provider adapter | `tenant:configure` | Scoped read-only probe at required freshness | Adapter result; intake/configure/status | Revoke IAP grants; purge IAP-derived cache/index copies |
| Datasource/config server | Provider adapter and secret manager | `tenant:configure` | Scoped read-only connection and policy validation | Adapter result; intake/configure/status | Revoke IAP grants; delete only IAP-owned secret material |
| Knowledge corpus | Knowledge provider adapter | `tenant:configure` | Read-only access and tenant/project scope verified | Adapter result; intake/configure/status | Delete IAP indexes/chunks; do not modify external source |
| Activation | Tenant registry plus readiness policy | `tenant:approve` or policy-authorized operator | All mandatory gates passed against same request/policy revision | Request `ACTIVE`; approve/activate | Suspend or tombstone; not a profile-only status change |
| Suspend/resume | Tenant registry/access policy | `tenant:suspend/resume` | Resume rechecks gates required by policy | Tenant lifecycle operation | Not deletion; access policy changes are recorded |
| Cleanup/deletion | Store registry and per-store adapters | `tenant:cleanup`, separated approval when policy requires | Complete, fresh plan; no legal hold; verified backup disposition | Deletion request/manifest/workflow | Per-manifest `DELETE/DETACH/REVOKE/RETAIN/EXPIRE` |

**Alternative considered:** Infer supported capability behavior from intake fields and adapter class names. Rejected because schema presence does not determine whether a source is required, who owns it, or how it is removed.

**Rationale:** The catalog is the common contract for intake validation, activation, status, operations, and deletion. It prevents capability drift between API, adapters, and cleanup.

**Trade-offs:** The catalog is versioned product policy and requires owners to update it when providers or service tiers change. The first release limits support to explicitly enumerated adapters.

---

### D3: Immutable shared intake revisions and source pinning

**Decision:** Operator YAML remains an offline capture/export format. The shared control store is authoritative. Each accepted request revision stores canonical tenant/app/environment scope, validated intake, schema version, content hash, policy version, catalog version, actor, approval references, and immutable source/provider versions. Updates use expected revision (optimistic concurrency); jobs execute the exact approved revision. Secrets are represented only by typed, tenant/source/environment-authorized references. API intake never accepts filesystem paths, arbitrary URLs, or secret values.

Repository and provider identifiers use allowlisted provider keys and structured IDs. At approval, mutable refs resolve to immutable commits. Outbound requests enforce destination allowlist, DNS/IP checks, redirect revalidation, response bounds, timeout, and no credential forwarding across hosts. Repository content is untrusted; generation never executes repository build scripts.

**Alternative considered:** Read files directly from the operator home during apply. Rejected because the shared API and worker fleet cannot guarantee replica consistency, immutable replay, or atomic updates from local files.

**Rationale:** Immutable revisions make approvals, idempotency, workflow retries, and forensic reconstruction refer to the same configuration.

**Trade-offs:** The control store and immutable artifact retention add persistence and retention ownership. Offline capture can be validated locally but cannot start work without a server-accepted revision.

---

### D4: Independent tenant, onboarding-request, profile, and job states

**Decision:** Maintain separate correlated state machines. Tenant registry owns tenant lifecycle; onboarding control store owns request lifecycle; profile service owns profile revisions and `DRAFT/ACTIVE/DEPRECATED/ARCHIVED`; Temporal owns execution history; PostgreSQL job rows are read models. Suggested request states are `DRAFT`, `VALIDATING`, `AWAITING_APPROVAL`, `QUEUED`, `PROVISIONING`, `READINESS_CHECK`, `ACTIVE`, `FAILED_RETRYABLE`, `FAILED_TERMINAL`, `CANCEL_REQUESTED`, `CANCELED`, `DEPROVISIONING`, `DEPROVISIONED`. Tenant status may additionally be `RESERVED`, `ACTIVE`, `SUSPENDED`, or `RETIRED`.

Every transition checks expected version, actor capability, request/policy revision, and transition guard. `ACTIVE` requires a durable readiness decision for every catalog-required capability. A profile becoming active does not activate a tenant. A changed intake or policy revision invalidates affected approvals and probe freshness.

**Alternative considered:** Use the background-job status as the tenant lifecycle. Rejected because execution status does not represent approval, suspension, readiness, or business activation.

**Rationale:** Separate authorities avoid divergent interpretations and permit deterministic retry, cancellation, and recovery.

**Trade-offs:** Cross-machine correlation needs invariant tests and operational dashboards. The state model is intentionally limited to business transitions that the module owns.

---

### D5: Real adapter probes and mandatory readiness policy

**Decision:** Each catalogued adapter is a request-scoped instance with typed outcomes `VERIFIED`, `DEGRADED`, `FAILED`, `UNSUPPORTED`, `SKIPPED`, or `NOT_CONFIGURED`. Probe evidence records adapter/version, capability key, tenant/app/environment scope, timestamp, freshness, auth mode, and closed-vocabulary diagnostic code. Only a successful scoped probe can satisfy a mandatory gate. Provider failure is never equivalent to empty data. Optional connectors may be skipped only by explicit service-tier policy. Unknown or omitted mandatory adapters block activation.

The four stages are `discover → normalize → bind → verify`. Each returns a new immutable result; no shared mutable adapter instance crosses requests. Connector credentials resolve at execution time through a scoped secret reference and are read-only at the external provider when supported.

**Alternative considered:** A generic `verified` boolean inferred from absence of blockers. Rejected because it can manufacture readiness without a successful scoped operation.

**Rationale:** Probe provenance gives activation a testable evidence contract and distinguishes unsupported, missing, failed, and verified sources.

**Trade-offs:** Every mandatory adapter needs provider-contract tests and freshness policy. Some providers may not support a strong tenant-scope probe; such providers remain unsupported for mandatory use until compensating controls are documented.

---

### D6: Transactional outbox is the normative workflow-start protocol

**Decision:** Create an outbox dispatch record in the same PostgreSQL transaction that commits the accepted request revision, idempotency reservation, initial job read model, audit linkage, and `QUEUED` state. A dispatcher claims records by lease and starts a deterministic Temporal workflow ID derived from canonical tenant ID, request ID, and revision. Repeated start for the same workflow ID is idempotent: an already-started execution is acknowledged and linked to its run ID. The dispatcher marks accepted only after Temporal confirms start or the same workflow already exists. A reconciler repairs expired leases, queued jobs with no workflow, dispatch rows with stale states, and workflow/job read-model disagreement.

Outbox delivery is at least once, not exactly once. The outbox closes only the database-to-workflow-start gap; adapter side effects still require per-step idempotency or discover-before-create and reconciliation. Direct start after database commit is not an alternative in this design. Any future replacement must preserve same-transaction durable intent and pass the same crash-boundary tests before adoption.

```python
# src/investigation_agent_platform/domain/onboarding/dispatch.py
from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class DispatchState(StrEnum):
    PENDING = "PENDING"
    CLAIMED = "CLAIMED"
    ACCEPTED = "ACCEPTED"
    RETRYABLE = "RETRYABLE"
    TERMINAL = "TERMINAL"


class WorkflowDispatch(BaseModel):
    """Durable command to start one onboarding workflow execution."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: UUID
    tenant_id: str = Field(min_length=1, max_length=64)
    request_id: UUID
    revision: int = Field(ge=1)
    job_id: UUID
    workflow_id: str = Field(min_length=1, max_length=256)
    schema_version: str = Field(min_length=1, max_length=32)
    state: DispatchState
    attempts: int = Field(default=0, ge=0)
    next_attempt_at: datetime | None = None
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    temporal_run_id: str | None = None
    last_error_code: str | None = None
    created_at: datetime
    updated_at: datetime
```

**Alternative considered:** Start Temporal directly from the API after committing database state. Rejected because process failure between commit and start strands the request. Start before commit risks an orphan workflow. Kafka-only dispatch is also rejected as the database commit is the authority and no dual-write transaction exists.

**Rationale:** One same-database commit records both business intent and dispatch intent. Deterministic workflow IDs make dispatcher retries safe; reconciliation covers ambiguous start acknowledgements.

**Trade-offs:** The dispatcher and reconciler are production-critical components with lease tuning, backoff, monitoring, and workflow-version compatibility responsibilities. They do not make external provider actions atomic.

---

### D7: Complete store registry and per-resource deletion manifest

**Decision:** Cleanup is a durable exact-plan workflow. Each deployment has a versioned registry of required store adapters. A preview must query every enabled/required store and produce a complete inventory; missing adapter, incomplete enumeration, provider timeout, stale inventory, or unknown resource ownership blocks approval. The manifest classifies every resource as tenant-owned, shared/reference-counted, derived/cache, customer-owned external, or platform audit/retention and selects one action: `DELETE`, `DETACH`, `REVOKE`, `RETAIN`, or `EXPIRE`.

The deployment inventory covers PostgreSQL tables and shared references, object/artifact storage including versions, search/runtime evidence, graph/topology projections, Kafka messages, Temporal workflows/history/schedules, caches, operator-home exports/config artifacts, secret grants/material, backups/replicas, audit records, and external customer systems. Not every store is active in every deployment; the registered deployment set is authoritative. External customer data is never deleted by IAP unless a separate, explicitly authorized product contract makes IAP the data owner.

Each manifest entry is keyed by `(manifest_id, store_id, resource_type, resource_ref)` and records store adapter/version, scope, ownership class, enumerator version, enumeration time/completeness, count, retention/legal-hold decision, backup disposition, action, and per-resource idempotency key. The immutable plan hash covers the canonical manifest contents, including scope, entries, policy/catalog/store-registry versions, backup evidence, requester, required approver identities, and expiry; `canonical_plan_hash` itself is excluded from the hash input to avoid self-reference. Execution status, attempts, completion evidence, and residual reasons are append-only outcome records keyed to manifest entries and cannot rewrite the approved plan.

```python
# src/investigation_agent_platform/domain/onboarding/deletion.py
from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from investigation_agent_platform.application.onboarding.intake import (
    DatasourceSpec,
    KnowledgeSourceSpec,
    RepoRef,
    SecretReference,
)


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


class StoreResourcePlan(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

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
    model_config = ConfigDict(frozen=True, extra="forbid")

    store_id: str
    adapter_version: str
    enumerated_at: datetime
    completeness: InventoryCompleteness
    resource_count: int = Field(ge=0)
    inventory_version: str
    plan_entries: tuple[StoreResourcePlan, ...]
    diagnostic_code: str | None = None


class DeletionManifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    manifest_id: UUID
    tenant_id: str
    application_id: str | None
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
```

**Alternative considered:** Use a flat resource ID list and aggregate counts, or delete by tenant prefix in each provider. Rejected because IDs are not globally unique, counts cannot prove store enumeration, prefixes may cover siblings/shared resources, and shared reference data needs ownership/refcount semantics.

**Rationale:** Exact approval is useful only when the plan is complete. A deployment-level registry plus completeness markers makes omissions and provider outages visible instead of silently successful.

**Trade-offs:** Store adapters must implement enumerate/plan/apply/verify and keep schemas versioned. Some immutable backups or platform audit records cannot be physically deleted immediately; the manifest must state retention disposition and customer-access boundary instead of falsely claiming physical erasure.

---

### D8: Canonical approval payload and atomic one-time consumption

**Decision:** Approval uses a cryptographically random one-time bearer token plus one canonical persisted approval payload. The payload includes manifest ID, canonical plan hash, tenant/app/environment scope, requester, actual approver, policy version, store-registry version, expiry, and a random nonce. The server stores only a SHA-256 digest of the random token and the canonical payload digest; the token itself is returned once and never logged. At approval, recompute the canonical payload digest with a single shared function, validate the actual authenticated approver capability and separation of duties, compare digests in constant time, and atomically consume the approval with a conditional database update. A public SHA-256 hash is not authorization; permission comes from the authenticated principal, persisted approval state, token secrecy, and atomic one-time consumption. Expiry is checked against server time; execution revalidates the manifest hash and active hold status before each destructive stage. If manifest plan changes, invalidate and require new approval.

```python
# src/investigation_agent_platform/application/onboarding/approval.py
from __future__ import annotations

import hashlib
import json
import secrets
from datetime import UTC, datetime
from typing import Any


def canonical_approval_digest(payload: dict[str, Any]) -> str:
    """Hash canonical approval fields using the same serializer at creation and validation."""
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_approval_payload(
    *,
    manifest_id: str,
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
) -> dict[str, Any]:
    """Build the only payload accepted by mint and validate operations."""
    if expires_at.tzinfo is None:
        raise ValueError("expires_at must be timezone-aware")
    if not nonce:
        raise ValueError("nonce must not be empty")
    return {
        "manifest_id": manifest_id,
        "plan_hash": plan_hash,
        "tenant_id": tenant_id,
        "application_id": application_id,
        "environment": environment,
        "requester_id": requester_id,
        "approver_id": approver_id,
        "policy_version": policy_version,
        "store_registry_version": store_registry_version,
        "expires_at": expires_at.astimezone(UTC).isoformat(),
        "nonce": nonce,
    }


def mint_approval_token() -> tuple[str, str]:
    """Return one-time token and stored token digest. Never persist or log the raw token."""
    token = secrets.token_urlsafe(32)
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return token, digest


def approval_matches(
    *,
    presented_token: str,
    stored_token_digest: str,
    payload: dict[str, Any],
    stored_payload_digest: str,
    authenticated_approver_id: str,
) -> bool:
    """Validate exact payload, actual approver identity, token, and server-time expiry."""
    if payload.get("approver_id") != authenticated_approver_id:
        return False
    expiry = datetime.fromisoformat(str(payload["expires_at"]))
    if expiry.tzinfo is None or datetime.now(UTC) >= expiry.astimezone(UTC):
        return False
    token_digest = hashlib.sha256(presented_token.encode("utf-8")).hexdigest()
    payload_digest = canonical_approval_digest(payload)
    return secrets.compare_digest(token_digest, stored_token_digest) and secrets.compare_digest(
        payload_digest, stored_payload_digest
    )
```

**Alternative considered:** A public digest alone authorizing the destructive operation. Rejected because an unkeyed hash of public plan fields is not a bearer secret or authorization proof. A signed bearer token may be used instead only if it has a vetted issuer, audience, expiry, nonce, canonical claims, and atomic server-side replay prevention.

**Rationale:** One canonical payload used by both creation and validation prevents parameter mismatch. Atomic consumption—not a `used` boolean read in application memory—enforces one-time approval under concurrency.

**Trade-offs:** Canonical schema/versioning, nonce/token-digest persistence, and atomic consumption are required. SHA-256 digests do not provide non-repudiation; authorization and append-only audit remain separate controls.

---

### D9: Executable API/CLI, policy, and operational controls

**Decision:** REST and CLI call the same application services and share versioned request/response contracts. Every mutating request includes an idempotency key and expected revision. API replies `202 Accepted` only after the request, job, audit linkage, and outbox dispatch intent commit. Status reports request/job states separately and gives redacted blockers and next actions. Cleanup request returns an immutable manifest ID/hash and token/approval receipt only after complete store enumeration; approval starts purge only after atomic consumption. Rate limiting uses pre-auth global/IP controls and post-auth principal/tenant/global limits. All work is bounded by item sizes, provider timeouts, worker concurrency, and per-tenant quotas.

**Alternative considered:** Independent CLI and REST implementations or examples that return placeholder job/token IDs. Rejected because they diverge and can falsely acknowledge work.

**Rationale:** Contract conformance ensures interfaces reflect actual authorization, dispatch, state, and cleanup outcomes.

**Trade-offs:** Every interface change must update OpenAPI, CLI behavior, conformance fixtures, and migration compatibility. An explicit capability catalog adds validation and reporting obligations.

---

### D10: Eligibility, isolation, retention, and operability are enforceable policy

**Decision:** Eligibility resolves data region, allowed providers, isolation class, quotas, encryption-key policy, supported capabilities, and service tier before any outbound or mutating side effect. Tenant context is carried and validated through API, workflow, event, scheduled task, cache, search, object, and provider calls. Relational isolation uses RLS plus repository predicates and audited app-role permissions. Retention, legal hold, backup encryption/verification, restore, and audit retention have named owners. Per-store cleanup action and evidence are captured by the registry/manifest contract. Structured telemetry carries correlation/request/job/step IDs and bounded result codes without secrets or provider payloads.

**Alternative considered:** Treat shared-fleet constraints as documentation only. Rejected because unsupported residency/isolation cannot be mitigated by a warning after onboarding.

**Rationale:** Placement and security policy must gate onboarding and have durable evidence.

**Trade-offs:** Product, security, compliance, and SRE owners must supply real policy and workload assumptions. Until then, unknown requirements reject activation rather than defaulting to approval.

## Data Storage

The control store owns registry references, onboarding request revisions, step records, outbox dispatch records, approval receipts, store registry versions, deletion manifests, and append-only manifest outcomes. Existing profile tables remain profile-service authority. Temporal is authoritative for workflow execution history; the existing job table remains a tenant-scoped read model. External provider resources remain provider-owned and are referenced, not copied as authoritative state.

The following SQLAlchemy shapes are conceptual design contracts. Implementation must use the repository's declarative base, explicit foreign keys where supported, RLS policies, app-role grants, timestamps/defaults, and migration compatibility conventions. No JSON blob may replace validation of the versioned manifest or dispatch schema.

```python
# src/investigation_agent_platform/infrastructure/persistence/models_onboarding.py
from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, Index, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column


class WorkflowDispatchORM:
    """Transactional intent to start one Temporal workflow revision."""

    __tablename__ = "onboarding_dispatches"
    __table_args__ = (
        UniqueConstraint("tenant_id", "workflow_id", name="uq_onboarding_dispatch_workflow"),
        UniqueConstraint("event_id", name="uq_onboarding_dispatch_event"),
        Index("ix_onboarding_dispatch_pending", "state", "next_attempt_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    event_id: Mapped[UUID] = mapped_column(nullable=False)
    request_id: Mapped[UUID] = mapped_column(nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    job_id: Mapped[UUID] = mapped_column(nullable=False)
    workflow_id: Mapped[str] = mapped_column(String(256), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(32), nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False, default="PENDING")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    temporal_run_id: Mapped[str | None] = mapped_column(String(256))
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class StoreRegistryORM:
    """Versioned deployment inventory declaring required cleanup adapters."""

    __tablename__ = "onboarding_store_registry"
    __table_args__ = (
        UniqueConstraint("deployment_id", "store_id", "registry_version", name="uq_store_registry_version"),
        Index("ix_store_registry_deployment", "deployment_id", "enabled"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    deployment_id: Mapped[str] = mapped_column(String(128), nullable=False)
    store_id: Mapped[str] = mapped_column(String(128), nullable=False)
    store_type: Mapped[str] = mapped_column(String(64), nullable=False)
    adapter_version: Mapped[str] = mapped_column(String(32), nullable=False)
    registry_version: Mapped[str] = mapped_column(String(32), nullable=False)
    required_for_deletion: Mapped[bool] = mapped_column(nullable=False, default=True)
    enabled: Mapped[bool] = mapped_column(nullable=False, default=True)
    capabilities_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DeletionManifestORM:
    """Immutable approval plan; execution facts are stored as append-only outcomes."""

    __tablename__ = "onboarding_deletion_manifests"
    __table_args__ = (
        UniqueConstraint("tenant_id", "manifest_id", name="uq_onboarding_manifest"),
        Index("ix_onboarding_manifest_state", "tenant_id", "state"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest_id: Mapped[UUID] = mapped_column(nullable=False)
    application_id: Mapped[str | None] = mapped_column(String(128))
    environment: Mapped[str] = mapped_column(String(64), nullable=False)
    plan_version: Mapped[int] = mapped_column(Integer, nullable=False)
    plan_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    store_registry_version: Mapped[str] = mapped_column(String(32), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(32), nullable=False)
    manifest_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False)
    requester_id: Mapped[str] = mapped_column(String(256), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DeletionApprovalORM:
    """Persisted approval payload and atomic one-time consumption state."""

    __tablename__ = "onboarding_deletion_approvals"
    __table_args__ = (
        UniqueConstraint("tenant_id", "manifest_id", "nonce", name="uq_onboarding_approval_nonce"),
        Index("ix_onboarding_approval_manifest", "tenant_id", "manifest_id", "consumed_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest_id: Mapped[UUID] = mapped_column(nullable=False)
    approver_id: Mapped[str] = mapped_column(String(256), nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    nonce: Mapped[str] = mapped_column(String(128), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DeletionOutcomeORM:
    """Append-only execution status for one approved manifest resource entry."""

    __tablename__ = "onboarding_deletion_outcomes"
    __table_args__ = (
        UniqueConstraint("tenant_id", "manifest_id", "entry_key", "attempt", name="uq_deletion_outcome_attempt"),
        Index("ix_deletion_outcome_manifest", "tenant_id", "manifest_id", "state"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest_id: Mapped[UUID] = mapped_column(nullable=False)
    entry_key: Mapped[str] = mapped_column(String(256), nullable=False)
    store_id: Mapped[str] = mapped_column(String(128), nullable=False)
    action: Mapped[str] = mapped_column(String(24), nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    completion_evidence_ref: Mapped[str | None] = mapped_column(String(512))
    residual_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
```

**Structural requirements:** All tenant-bearing tables receive RLS policies and app-role grants in the same migration; every repository uses tenant-scoped access. Registry configuration is deployment-scoped and admin-managed. Manifest JSON is parsed against a strict versioned domain schema on write and read. Deletion outcomes never mutate the approved manifest.

## Data Structures

Input and output schemas are separate. Unknown fields are forbidden. API intake contains no secret values, filesystem paths, arbitrary destination URLs, approval digest, or derived resource IDs. The CLI export can include public references, never credential material.

The following public contract models complete the principal request and response surface. Domain validation also enforces item-count/size limits and cross-field invariants from the catalog.

```python
# src/investigation_agent_platform/api/v1/schemas/tenants.py
from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class IntakePutRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_slug: str = Field(min_length=1, max_length=128)
    application_id: str = Field(min_length=1, max_length=128)
    environment: str = Field(min_length=1, max_length=64)
    repos: list[RepoRef] = Field(default_factory=list, max_length=64)
    credential_refs: dict[str, SecretReference] = Field(default_factory=dict, max_length=64)
    datasources: list[DatasourceSpec] = Field(default_factory=list, max_length=64)
    knowledge_sources: list[KnowledgeSourceSpec] = Field(default_factory=list, max_length=64)
    sources: list[SourceCapabilityInput] = Field(default_factory=list, max_length=256)
    service_names: list[str] = Field(default_factory=list, max_length=256)
    log_indices: list[str] = Field(default_factory=list, max_length=256)
    trace_backend: str = Field(default="", max_length=128)
    expected_revision: int = Field(ge=0)
    schema_version: str = Field(min_length=1, max_length=32)


class CapabilityStatusRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capability_key: str
    status: str
    required: bool
    probe_at: datetime | None = None
    freshness_expires_at: datetime | None = None
    diagnostic_code: str | None = None


class CapabilityCatalogEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    capability_key: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=32)
    source_class: str = Field(min_length=1, max_length=64)
    owner: str = Field(min_length=1, max_length=128)
    scope_level: str = Field(min_length=1, max_length=32)
    required_capability: str = Field(min_length=1, max_length=128)
    policy_by_tier: dict[str, str] = Field(max_length=32)
    readiness_probe: str = Field(min_length=1, max_length=128)
    freshness_seconds: int = Field(ge=0)
    retry_policy_version: str = Field(min_length=1, max_length=32)
    deletion_action: str = Field(min_length=1, max_length=32)


class SourceCapabilityInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capability_key: str = Field(min_length=1, max_length=128)
    provider_key: str = Field(min_length=1, max_length=64)
    public_config: dict[str, Any] = Field(default_factory=dict)


class OnboardingRequestRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    request_id: UUID
    revision: int
    content_hash: str
    state: str
    state_version: int
    policy_version: str
    catalog_version: str
    capabilities: list[CapabilityStatusRead]
    blocking_codes: list[str]
    created_at: datetime
    updated_at: datetime


class IntakePutRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    request_id: UUID
    revision: int
    content_hash: str
    accepted: bool
    validation_codes: list[str]


class ApplyResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    request_id: UUID
    revision: int
    job_id: UUID
    dispatch_state: str
    status_url: str


class ApprovalReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    manifest_id: UUID
    plan_hash: str
    approval_id: UUID
    expires_at: datetime
    consumed: bool


class ApprovalPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

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


class DeletionRequestRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    manifest_id: UUID
    tenant_id: str
    application_id: str | None
    environment: str
    plan_hash: str
    store_registry_version: str
    complete: bool
    inventory: list[StoreInventoryResult]
    blocking_codes: list[str]
    expires_at: datetime


class JobRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: UUID
    tenant_id: str
    kind: str
    state: str
    progress: int = Field(ge=0)
    total: int = Field(ge=0)
    request_id: UUID | None = None
    revision: int | None = None
    created_at: datetime
    updated_at: datetime
```

The domain models defined in D2, D5, D7, and D8 are also required: `TenantIntake`, `RepoRef`, `SecretReference`, `DatasourceSpec`, `KnowledgeSourceSpec`, `CapabilityCatalogEntry`, `ProbeEvidence`, `WorkflowDispatch`, `StoreResourcePlan`, `StoreInventoryResult`, `DeletionManifest`, `ApprovalPayload`, and per-entry deletion outcomes. All fields require strict bounds/versioning. The store registry is operator-controlled deployment configuration, never tenant-supplied intake.

## Interfaces

### REST API — onboarding

| Method | Path | Request | Response | Description |
|---|---|---|---|---|
| `POST` | `/v1/onboarding-requests` | Typed intake + `Idempotency-Key` | `ApplyResponse` | Reserve/create request revision and enqueue atomically. |
| `PUT` | `/v1/onboarding-requests/{id}/intake` | `IntakePutRequest` + `If-Match` revision | `IntakePutRead` | Create next immutable revision; stale revision conflicts. |
| `GET` | `/v1/onboarding-requests/{id}` | — | `OnboardingRequestRead` | Tenant-authorized progress and blockers. |
| `POST` | `/v1/onboarding-requests/{id}:approve` | Revision + policy hash | Approval receipt | Approve exact request revision, subject to roles/separation. |
| `POST` | `/v1/onboarding-requests/{id}:retry` | Step selectors + idempotency key | `ApplyResponse` | Resume/reconcile selected safe steps for the same revision. |
| `POST` | `/v1/onboarding-requests/{id}:cancel` | Expected version | State receipt | Request workflow cancellation and compensation. |
| `POST` | `/v1/onboarding-requests/{id}:suspend` | Reason + expected version | State receipt | Suspend tenant access without deleting data. |
| `POST` | `/v1/onboarding-requests/{id}:resume` | Expected version | Job receipt | Re-evaluate required readiness gates before resuming. |
| `POST` | `/v1/tenants/{tenant_id}:deletion-request` | App/environment scope | Manifest preview | Enumerate all required stores; no deletion side effect. |
| `POST` | `/v1/deletion-requests/{manifest_id}:approve` | Manifest hash + approval token | Job receipt | Atomically consume approval and enqueue purge. |
| `GET` | `/v1/deletion-requests/{manifest_id}` | — | `DeletionRequestRead` | Show per-store status and residuals, tenant-authorized. |
| `GET` | `/v1/tenants/{tenant_id}/capabilities` | — | `CapabilityStatusRead[]` | Show policy-resolved supported, required, optional, and unsupported capabilities. |
| `GET` | `/v1/jobs/{job_id}` | — | `JobRead` | Tenant-scoped workflow read model. |

Errors use the platform envelope and stable codes, including `CAPABILITY_MISSING`, `OPERATOR_ROLE_REQUIRED`, `SEPARATION_OF_DUTIES`, `STALE_REVISION`, `IDEMPOTENCY_CONFLICT`, `DISPATCH_PENDING`, `PLAN_CHANGED`, `STORE_INVENTORY_INCOMPLETE`, `LEGAL_HOLD_BLOCKS`, `APPROVAL_INVALID`, `APPROVAL_EXPIRED`, and `CLEANUP_RESIDUALS`. A dispatch pending response still refers to a durable request/job; it never fabricates a workflow-start success.

### CLI commands

| Command | Arguments | Description |
|---|---|---|
| `onboard capture` | Tenant/app/environment, source options, output path | Validate and export intake only. |
| `onboard apply` | Intake artifact, idempotency key, API endpoint | Submit immutable intake revision and poll durable job. |
| `onboard attach` | Tenant/app, repository, expected revision, idempotency key | Create an immutable profile/config revision. |
| `onboard status` | Tenant/request/job ID, `--json` | Show request, job, readiness, and per-capability status. |
| `onboard suspend/resume` | Tenant/request, reason, expected version | Invoke capability-guarded lifecycle transition. |
| `onboard cleanup-plan` | Tenant, optional app/environment | Display store coverage, exact manifest, holds, and resource counts. |
| `onboard cleanup-approve` | Manifest ID/hash, approval credential | Submit authorized approval; no typed `YES` substitutes for role checks. |

CLI uses the approved token mechanism and is a thin client over the same service/API contract. Output is readable without color and supports JSON for automation. Cleanup preview must show incomplete stores and refuse approval; it does not hide them behind aggregate counts.

## Accessibility

### CLI-specific requirements

- Every command and option has descriptive `--help`; output is parseable without color and status has JSON output.
- Errors name the failing check and safe next action without exposing another tenant's existence or resource inventory.
- Cleanup summaries provide explicit textual store names, action, hold status, and residual counts; color is never the only status indicator.

### REST-specific requirements

- Responses use typed schemas and stable error codes with actionable detail; no free-form error text is required for client branching.
- Long operations return durable job identifiers only after durable acceptance; progress polling is consistent and tenant-authorized.

## Implementation Detail

`POST onboarding request` resolves canonical tenant and caller capability, validates the intake against the catalog and policy snapshot, reserves the idempotency key, creates request/job/audit/outbox rows in one transaction, and returns the durable request/job IDs. The dispatcher claims outbox rows using a database lease, starts Temporal with the deterministic workflow ID, records Temporal run ID, and marks the dispatch accepted. A periodic reconciler finds expired leases, pending jobs without workflows, and inconsistent read models.

The workflow reads the exact immutable request revision and policy/catalog versions, then executes pinned dependency steps. Every step has a stable idempotency key, bounded timeout/retry policy, provider resource reference, and durable outcome. A timeout after external success triggers discover-by-key before retry. Compensation is a separate idempotent workflow action. Activation is a guarded control-store/registry transition after all mandatory catalog entries have fresh `VERIFIED` evidence.

Deletion preview asks each required store adapter to enumerate resources. Each response is recorded with adapter/version, scope, completeness, inventory time, count, and plan entries. Approval is unavailable unless all required stores returned complete inventories and legal-hold/retention policy permits the selected actions. A cryptographically random approval token is returned once; only its digest and canonical approval-payload digest are persisted. The authenticated approver, exact plan hash, scope, expiry, and nonce are verified consistently. A transaction conditionally marks the approval consumed and creates the purge job/outbox event. Purge revalidates holds and plan hash before each destructive stage. Per-resource outcomes append to the outcome table. A terminal deletion state requires every entry to be `DONE`, `RETAINED_BY_POLICY`, or an explicitly approved residual exception; unenumerated or unavailable required stores are never treated as empty.

Structured telemetry includes correlation/request/job/step IDs, tenant ID under the established restricted logging policy, bounded outcome codes, duration, and counts only. Secrets, raw intake, resource payloads, approval tokens, raw provider responses, and full external resource IDs are excluded from logs and metrics. Audit entries preserve actor, capability, decision, policy version, request/manifest hash, and state transition under a separately defined retention policy.

## Migrations

1. Add tenant registry/reservation and onboarding request revision tables with uniqueness, optimistic concurrency, RLS, app-role grants, and no backfill that guesses canonical identity.
2. Add workflow dispatch/outbox table and indexes in the same migration sequence as request/job transaction wiring; protect the unique workflow ID per tenant/request/revision.
3. Add step ledger, approval, store registry, immutable deletion manifest, and append-only deletion outcome tables with tenant-scoped policies.
4. Deploy additive schema and compatible readers before enabling routes/workers. Start with disabled flags and shadow validation; do not send traffic to a worker that cannot read the current schema version.
5. Preserve legacy profiles and existing job records; onboarding adopts existing tenants explicitly through a reservation/link operation.
6. Rollback disables new intake/dispatch/cleanup routes but retains durable request, dispatch, and manifest records for safe forward recovery. Never roll back by deleting pending intent or approved manifests.

## Testing Philosophy

### Capability catalog and lifecycle matrix

For every catalog entry, test supported/unsupported tier, scope binding, required and optional readiness, freshness, capability enforcement, transition, retry, and deletion action. Include code-only, telemetry-enabled, and datasource-enabled synthetic tenants. Verify every intake field has exactly one owner and every lifecycle action has a guarded API and operational procedure.

### Outbox dispatch and Temporal recovery

Inject process failure before transaction commit, after commit before dispatch, after Temporal start before acknowledgement, during lease expiry, during Temporal outage, and during reconciler restart. Verify rollback produces no workflow; committed outbox intent is eventually started or terminally escalated; duplicate start uses the same workflow ID/run; accepted jobs are never stranded; and provider side effects do not duplicate.

### Deletion store coverage and manifest correctness

Compare the enabled deployment registry with every generated manifest. Missing adapters, stale registry versions, partial enumeration, provider timeouts, and unknown ownership must block approval. Test exact scope/hash binding, shared-reference counts, object versions, caches, schedules/workflows, retained backups, legal holds, external customer-owned data, and sibling app safety. Require per-resource completion evidence or visible residual status.

### Approval and concurrency

Golden tests use the same canonical payload for digest creation and verification. Mutating any field, expiry, token, nonce, plan, scope, policy, store registry, requester, or approver must fail. Test expired, reused, wrong-tenant, unauthorized, self-approved, concurrent, and stale-plan cases; at most one purge job may be created.

### Isolation and API/CLI conformance

Run capability matrices against every endpoint, including ordinary users, platform operators, delegated users, bootstrap claims, support/break-glass, and two-person approval. Prove tenant A cannot reach tenant B through REST, jobs, events, schedules, caches, search, artifacts, RLS, or cleanup. CLI and REST execute identical fixtures under production-style JWT authentication and produce equivalent decisions.

### Operational readiness

Load test agreed tenant count, request burst, connector fan-out, and provider limits. Verify fairness and backpressure, queue age and stuck-work alerts, retry budgets, backup restore, and measured RTO/RPO before enabling production cleanup. Runbooks must cover dispatch repair, workflow resume/cancel, incomplete store inventory, residual cleanup, and approval revocation.

## Documentation Plan

### `docs/IAP-architecture-v1.0.md`

**Audience:** Operators, developers, and reviewers

Document the scope, component ownership, tenant and request states, capability catalog, transactional outbox sequence, Temporal workflow ID and reconciliation contract, store registry and deletion manifest, approval binding, metrics, flags, and deployment constraints. Include a diagram showing the shared database transaction and asynchronous dispatcher.

### Operator onboarding and cleanup runbook

**Audience:** Platform operators and on-call responders

Document reservation/bootstrap, intake validation, approval, apply/status, safe retry/cancel/suspend/resume, store-registry health, cleanup preview, hold handling, approval, purge progress, residual remediation, restore, and escalation. Include explicit instructions that external customer data is detached rather than deleted.

### API reference and CLI help

**Audience:** Automation clients and operators

Document idempotency and revision behavior, capability requirements, job status, dispatch-pending semantics, per-store cleanup preview, approval expiration/replay, error codes, rate limits, and JSON output. Examples must run against a JWT-enabled test environment and cannot use placeholder identifiers.

### Data-retention and store-registry catalog

**Audience:** Security, privacy, compliance, data owners, and SRE

List each deployment store, owner, tenant-scoping method, resource classes, enumeration adapter/version, retention/legal-hold behavior, backup/restore behavior, deletion action, evidence, and residual owner. Update registry and manifest compatibility together.

## Risks / Trade-offs

### Capability catalog drift

**Risk:** Adapter additions or product-tier changes can update intake without updating readiness and deletion policy.

**Mitigation:** Require catalog schema validation and tests that fail when an accepted intake capability lacks authorization, readiness, ownership, and deletion mappings.

### Outbox and reconciler complexity

**Risk:** Lease races, repeated workflow start, or stale job projections can create duplicate or stuck executions.

**Mitigation:** Use unique deterministic workflow IDs, atomic leases, bounded retries, idempotent start handling, and reconciliation metrics; require crash-window tests before rollout.

### Incomplete store enumeration

**Risk:** An adapter outage or new unregistered store can leave data behind while deletion appears successful.

**Mitigation:** Compare manifests to a versioned deployment registry and block approval/completion when required stores are missing or enumeration is incomplete.

### Shared references and immutable backups

**Risk:** Deleting a tenant may destroy shared repository evidence, while immutable backups may retain deleted material until expiry.

**Mitigation:** Track ownership/reference counts and distinguish `DETACH`, `DELETE`, and `RETAIN`; document backup expiry and deletion-tombstone replay before claiming purge completion.

### Approval token misuse or drift

**Risk:** Inconsistent canonicalization, token replay, or app/policy changes may invalidate or broaden a previously approved plan.

**Mitigation:** One canonical versioned payload, server-side expiry, atomic consumption, exact plan hash, separation of duties, and mutation/expiry/replay tests.

### Operational scale and provider limits

**Risk:** Onboarding bursts or slow external providers can starve shared workers and create retry storms.

**Mitigation:** Use per-tenant/global concurrency and cost limits, provider-aware backoff, fair scheduling, circuit breakers, and approved workload tests.
