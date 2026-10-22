# Design: Tenant Onboarding for IAP (Revision 2)

IAP tenant configuration onboarding with an explicit trust boundary: canonical tenant identity plus capability authorization, a durable shared control store with immutable request revisions and a guarded state machine, typed secret references with no filesystem paths in API intake, real adapter probes behind mandatory readiness gates, exact-plan cleanup with atomic approval, and executable CLI plus REST contracts. No new runtime dependency beyond the existing durable workflow platform.

## Scope and trust boundary

This module is IAP tenant configuration onboarding. It owns request coordination and status for bringing a tenant's repositories, telemetry, datasources, config servers, and read-only knowledge corpora under investigation. It does not own customer identity, subscriptions, entitlements, billing, DNS, certificates, infrastructure, or network provisioning. Those systems remain authoritative for their own resources; where onboarding needs them, it coordinates through their interfaces and records their references and decisions, never duplicating their state.

Tenant identity is a stable, globally unique security boundary issued by the tenant registry in this design. Application and environment are child scopes of a tenant. Operator scope (organization, application, environment files on the operator host) is an offline capture and export format, never a shared-fleet authority. All API and worker processes read tenant, intake, approval, and step state from the shared control store; replicas never read unversioned operator-host files as authority.

## Context

IAP investigates tenant-scoped systems but has never had a cleaner onboarding strategy. Today there is no single tenant-init script: onboarding is composed ad hoc from repository ingestion, bulk profile provisioning, single-application generation, and file or directory seeding, while cleanup is only the battery-scoped investigations wiper. Tenant identity arrives via `X-Tenant-ID` with token-claim verification in `src/investigation_agent_platform/api/tenant.py:13-243`, isolation rests on Postgres RLS plus per-repository tenant predicates (`infrastructure/persistence/rls.py:20-43`), and application profiles follow an immutable-revision lifecycle (`DRAFT → ACTIVE → DEPRECATED → ARCHIVED` in `domain/profile/models.py:26-54`) with generation from repository detection, parsing, config-server precedence, and operator overlay (`integrations/profile_gen/`). Any string matching the tenant pattern becomes a tenant on first write, since no allowlist, reservation, or offboarding flow exists. Revision 1 of this design introduced the intake contract, per-class adapters, tiered cleanup, and CLI plus REST parity, and a subsequent architecture review found its authorization, shared-state, secret-handling, cleanup-binding, lifecycle, recovery, probe, contract, isolation, retention, and operability gaps. This revision closes those gaps in the design before any implementation begins.

### `docs/dev/architecture.md` changes

No `docs/dev/architecture.md` exists in the project. On implementation, update `docs/IAP-architecture-v1.0.md` with a tenant-onboarding section covering the narrowed scope and trust boundary, the tenant registry and capability model, the control-store schema and state machine, the typed secret-reference contract, the deletion-manifest lifecycle, and the CLI plus REST parity rule with executable contract tests. Record each rollout flag and its default alongside the operator-home layout so operators can reason about onboarding without reading code.

## References

- [docs/IAP-architecture-v1.0.md](./IAP-architecture-v1.0.md) — Current IAP architecture, operator scopes, shared-fleet topology, profile lifecycle, and verification states this design builds on.
- [docs/BATTERY_LIVE_PROTOCOL.md](./BATTERY_LIVE_PROTOCOL.md) — Live battery execution and grading protocol reused for post-onboarding smoke verification.
- [config/env-matrix.md](../config/env-matrix.md) — Per-environment contract for every `IAP_*` variable referenced by the onboarding flow.

## Goals / Non-Goals

**Goals:**

- Explicit narrowed scope with named external authorities for identity, entitlement, infrastructure, and credential material.
- Canonical tenant identity with immutable internal ids, mutable display slugs, reservation, and lifecycle states.
- Capability authorization separating tenant context from onboarding, configuration, approval, and cleanup permission, with a bootstrap flow for claim-less new tenants.
- Durable shared control store with immutable request revisions, optimistic concurrency, step ledger, idempotent dispatch, and reconciliation.
- Typed secret references scoped to tenant, source, and environment, with no filesystem paths and no secret values in API intake.
- Constrained outbound integration: structured identifiers, destination allowlists, commit pinning, and no build-script execution.
- Real adapter probes with typed outcomes behind mandatory readiness gates; activation only on durable, version-checked success.
- Exact-plan cleanup with atomic approval consumption, dual control where policy requires, legal-hold enforcement, verified backups, tombstones, and per-system purge with reconciliation.
- Executable CLI and REST contracts proven by conformance suites, with rate limits, abuse controls, quotas, and operational telemetry.

**Non-Goals:**

- Customer identity provisioning, SSO configuration, billing, subscriptions, DNS, certificates, or infrastructure and network provisioning — external authorities, coordinated with, never owned.
- Write paths into knowledge systems (ticket filing or page updates) — read-only retrieval corpora only; updates arrive as a separate flow with its own trust boundary.
- Per-tenant infrastructure segregation, dedicated namespaces, or provider isolation — documented placement classes with eligibility rejection instead.
- Tenant migration between hosting classes, self-service onboarding, and dedicated or hybrid tiers — parked with explicit entry conditions.
- New runtime dependencies beyond the existing durable workflow platform and shared relational store.

## Decisions

### D1: Canonical tenant identity with capability authorization and a bootstrap flow

**Decision:** The tenant registry issues an immutable opaque internal tenant id at reservation time; display slugs stay mutable metadata with normalization, alias rules, and retired-state tombstones. Authentication stays token-based, but every onboarding, configuration, approval, and cleanup operation additionally requires an explicit capability (`tenant:onboard`, `tenant:configure`, `tenant:approve`, `tenant:cleanup`, `tenant:read`) bound to principal, tenant, operation, and environment, with issuer-backed roles. New tenants without an identity-provider claim are created through a platform-operator invitation flow that reserves the canonical id first; self-claim of an unreserved id always fails. Tenant display names are never used for path construction or authorization; all filesystem and storage paths derive from the canonical id behind a resolved-path containment guard.

```python
# src/investigation_agent_platform/application/onboarding/authorization.py
"""Tenant authorization: canonical identity plus capability checks. No I/O except registry access."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)

SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
MAX_SLUG_LENGTH = 128

ONBOARD = "tenant:onboard"
CONFIGURE = "tenant:configure"
APPROVE = "tenant:approve"
CLEANUP = "tenant:cleanup"
READ = "tenant:read"


class TenantAuthorizationError(Exception):
    """Raised when identity, capability, or scope checks fail. Fail closed."""


def normalize_slug(raw: str) -> str:
    """Canonicalize a display slug. Lowercase, trimmed, strict charset. Never used as an id."""
    candidate = (raw or "").strip().lower()
    if not candidate or len(candidate) > MAX_SLUG_LENGTH:
        raise TenantAuthorizationError("Tenant slug is empty or exceeds 128 characters.")
    if SLUG_PATTERN.fullmatch(candidate) is None:
        raise TenantAuthorizationError("Tenant slug uses characters outside [a-z0-9._-].")
    return candidate


@dataclass(frozen=True)
class AuthDecision:
    """One auditable authorization outcome. Denials never leak tenant existence."""

    allowed: bool
    principal_id: str
    tenant_id: str
    capability: str
    reason: str = ""


def check_capability(
    principal_capabilities: frozenset[str],
    principal_id: str,
    tenant_id: str,
    capability: str,
    is_platform_operator: bool,
) -> AuthDecision:
    """Function-level authorization. Tenant claim equality is necessary but never sufficient."""
    if capability in (ONBOARD, CLEANUP, APPROVE) and not is_platform_operator:
        logger.warning("denied privileged onboarding action for non-operator principal.")
        return AuthDecision(False, principal_id, tenant_id, capability, reason="operator_role_required")
    if capability not in principal_capabilities:
        return AuthDecision(False, principal_id, tenant_id, capability, reason="capability_missing")
    return AuthDecision(True, principal_id, tenant_id, capability)
```

**Alternative considered:** Token tenant-claim equality plus an operator-host allowlist file. Rejected — claim equality proves tenant context, not permission for privileged functions; local files diverge across fleet replicas and cannot arbitrate concurrent reservations.

**Rationale:** Identity answers who the tenant is, capabilities answer what the caller may do to it, and the registry answers whether it exists and is reserved. Collapsing any two reintroduces self-approval, cross-tenant creation, or bootstrap deadlock.

**Trade-offs:** A registry and role model are new shared state to own and audit. Display slugs need alias and tombstone handling so renames preserve audit continuity.

---

### D2: Intake as immutable versioned revisions in the shared control store

**Decision:** Operator YAML remains the offline capture format, but authority lives in the control store: `PUT intake` validates a typed request, rejects secret values and filesystem paths, verifies repository and provider references, and stores an immutable revision with content hash, schema version, policy snapshot, and actor. Files are an export of a revision, or an upload referenced by hash — never read as worker-side authority. Every mutation carries an expected revision for optimistic concurrency; stale writes conflict, and jobs execute exactly the pinned revision, policy version, source commits, and adapter versions they were approved against.

```python
# src/investigation_agent_platform/application/onboarding/intake.py
"""Intake contracts. References only; no secret values and no filesystem paths."""
from __future__ import annotations

from pydantic import BaseModel, Field


class RepoRef(BaseModel):
    """One repository the tenant's services are discovered from. Identifiers, not URLs."""

    provider: str = Field(default="github", description="Source provider key from the destination allowlist.")
    org: str = Field(description="Repository organization or owner.")
    name: str = Field(description="Repository name.")
    branch: str = Field(default="main", description="Branch resolved to a pinned commit at approval.")
    pinned_commit: str = Field(default="", description="Immutable commit SHA recorded at approval time.")
    app: str = Field(default="", description="Application this repository belongs to; empty means default app.")


class SecretReference(BaseModel):
    """Typed secret pointer. A capability, not inert text; authorized per tenant, source, environment."""

    provider: str = Field(description="Secret-store provider key.")
    secret_id: str = Field(description="Opaque secret identifier. Never a value, never a path.")
    version_policy: str = Field(
        default="latest-verified",
        description="Resolution policy: a pinned version or the latest verified version.",
    )
    read_only: bool = Field(default=True, description="Only read-only connector credentials are admissible.")


class DatasourceSpec(BaseModel):
    """One datasource declaration. Connection by authorized reference only."""

    id: str = Field(description="Datasource identity within the application.")
    kind: str = Field(description="Store kind, e.g. postgres, oracle, redis, kafka, s3, file_log.")
    role: str = Field(default="state", description="Datasource role.")
    connection_ref: SecretReference = Field(description="Authorized secret reference. Values are rejected.")
    read_only: bool = Field(default=True)
    allowed_tables: list[str] = Field(default_factory=list)


class KnowledgeSourceSpec(BaseModel):
    """One read-only knowledge corpus, e.g. issue tracker or documentation space."""

    kind: str = Field(description="Source kind: issues, wiki, docs, or runbooks.")
    provider: str = Field(default="internal", description="Provider key from the destination allowlist.")
    space_keys: list[str] = Field(default_factory=list, description="Spaces or projects to index.")
    mode: str = Field(default="read", description="Always read in this phase. Writers do not exist yet.")
    refresh_cadence: str = Field(default="daily")


class TenantIntake(BaseModel):
    """Complete operator input for one tenant. Stored as an immutable revision."""

    canonical_tenant_id: str = Field(description="Immutable internal tenant id from the registry.")
    display_slug: str = Field(description="Mutable display slug.")
    environment: str = Field(default="development")
    repos: list[RepoRef] = Field(default_factory=list)
    credential_refs: dict[str, SecretReference] = Field(
        default_factory=dict,
        description="Named secret references by purpose. No paths, no values.",
    )
    log_indices: list[str] = Field(default_factory=list)
    service_names: list[str] = Field(default_factory=list)
    trace_backend: str = Field(default="")
    datasources: list[DatasourceSpec] = Field(default_factory=list)
    knowledge_sources: list[KnowledgeSourceSpec] = Field(default_factory=list)
    auto_promote: bool = Field(default=False)
    budgets: dict[str, int] = Field(
        default_factory=lambda: {"max_evidence": 25, "iterations": 10, "tool_calls": 50},
    )


TENANT_INTAKE_EXAMPLE = """\
# tenant-id.env.example.yaml — copy, fill in, and submit via PUT intake.
canonical_tenant_id: ""
display_slug: tenant-a
environment: development
repos:
  - provider: github
    org: example
    name: example-service
    branch: main
    app: ""
credential_refs: {}
log_indices: []
service_names: []
trace_backend: ""
datasources: []
knowledge_sources: []
auto_promote: false
budgets: {max_evidence: 25, iterations: 10, tool_calls: 50}
"""
```

**Alternative considered:** Operator-host files as the shared authority with locking and replication semantics. Rejected — files cannot give replicas a consistent versioned source of truth with optimistic concurrency, and worker-side reads of unversioned files lose replayability.

**Rationale:** Reproducibility and safe retries require the exact approved input to remain stable throughout execution. Content-hash revisions plus expected-version writes give every replica, worker, and retry the same immutable input.

**Trade-offs:** A control-store schema and revision lifecycle are new persistence to own. Offline capture stays file-shaped so operators keep a reviewable artifact.

---

### D3: Tenant lifecycle distinct from profile lifecycle and job state

**Decision:** Three correlated but separate state machines: tenant lifecycle (registry-owned), onboarding request lifecycle (control-store-owned), and background job execution (workflow-owned read model). Onboarding request states are `DRAFT`, `VALIDATING`, `AWAITING_APPROVAL`, `QUEUED`, `PROVISIONING`, `READINESS_CHECK`, `ACTIVE`, `FAILED_RETRYABLE`, `FAILED_TERMINAL`, `CANCEL_REQUESTED`, `CANCELED`, `DEPROVISIONING`, `DEPROVISIONED`. Activation requires all mandatory gates and a durably committed transition; profile `ACTIVE` never implies tenant `ACTIVE`. Retries resume or reconcile the same revision; changed inputs create a new revision and invalidate affected approvals. Cancel and deprovision races resolve deterministically with full audit.

**Alternative considered:** A single `apply` job plus profile revision state as the lifecycle. Rejected — job state cannot express approvals, tenant activation, suspension, or deprovisioning, and conflates execution retries with business transitions.

**Rationale:** Long-running provisioning needs explicit state and guarded activation. Separate machines keep each owner authoritative for its own transitions while staying correlated by request id.

**Trade-offs:** Three machines need a transition table and cross-machine correlation tests. Guards are policy-driven so tenants without approval or cleanup states skip them without special cases.

---

### D4: Real adapter probes with typed outcomes and a mandatory readiness policy

**Decision:** Every adapter implements the same four-step pipeline with typed per-stage outcomes (`VERIFIED`, `DEGRADED`, `FAILED`, `UNSUPPORTED`, `SKIPPED`, `NOT_CONFIGURED`), probe identity, scope, timestamp, freshness, auth mode, and redacted diagnostic codes. Verification is an actual scoped read-only probe, never a default boolean. Unknown source kinds and omitted mandatory adapters block readiness. Transient, permanent, and unknown failures classify differently for retry. Request-scoped adapter instances keep the pipeline free of shared mutable state.

```python
# src/investigation_agent_platform/application/onboarding/adapters.py
"""Adapter protocol with typed outcomes. No shared mutable state across runs."""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field


class ProbeOutcome(str, Enum):
    """Terminal probe states. Only VERIFIED satisfies a mandatory gate."""

    VERIFIED = "VERIFIED"
    DEGRADED = "DEGRADED"
    FAILED = "FAILED"
    UNSUPPORTED = "UNSUPPORTED"
    SKIPPED = "SKIPPED"
    NOT_CONFIGURED = "NOT_CONFIGURED"


class ProbeEvidence(BaseModel):
    """What a probe proved, with provenance and redacted diagnostics."""

    source_kind: str = Field(description="Adapter kind that ran the probe.")
    outcome: ProbeOutcome = Field(description="Terminal probe outcome.")
    scope: str = Field(description="Tenant and source scope the probe ran against.")
    probed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    auth_mode: str = Field(default="secret-reference", description="How the adapter authenticated.")
    diagnostic_code: str = Field(default="", description="Closed-vocabulary code; never a secret or payload.")


class AdapterResult(BaseModel):
    """Immutable per-stage result. Stages return new instances, never mutate."""

    source_kind: str = Field(description="Adapter kind.")
    bound: dict = Field(default_factory=dict)
    needs_review: list[str] = Field(default_factory=list)
    probe: ProbeEvidence | None = Field(default=None)
    blocking: bool = Field(default=False)


class SourceAdapter:
    """Four-step pipeline. Fixed order; unknown kinds fail closed at lookup."""

    kind: str = "base"

    def discover(self, spec: dict) -> AdapterResult:
        """Enumerate what the source exposes without inventing names."""
        raise NotImplementedError

    def normalize(self, discovered: AdapterResult) -> AdapterResult:
        """Convert to profile-slot shapes with redactions. Returns a new instance."""
        raise NotImplementedError

    def bind(self, normalized: AdapterResult, tenant_id: str) -> AdapterResult:
        """Attach provenance and cross-links for the tenant. Returns a new instance."""
        raise NotImplementedError

    def verify(self, bound: AdapterResult) -> AdapterResult:
        """Run the actual scoped probe. Returns a new instance with probe evidence."""
        raise NotImplementedError

    def run(self, spec: dict, tenant_id: str) -> AdapterResult:
        """Fixed four-step sequence with no per-adapter reordering."""
        result = self.discover(spec)
        result = self.normalize(result)
        result = self.bind(result, tenant_id)
        return self.verify(result)


_ADAPTERS: dict[str, SourceAdapter] = {}


def register_adapter(adapter: SourceAdapter) -> None:
    """Explicit registration. No implicit discovery, no silent unknowns."""
    _ADAPTERS[adapter.kind] = adapter


def adapter_for(kind: str) -> SourceAdapter:
    """Lookup with fail-closed unknown kinds."""
    try:
        return _ADAPTERS[kind]
    except KeyError:
        raise ValueError(f"Unknown source kind '{kind}'.") from None
```

**Alternative considered:** Boolean `verified` flags defaulting to true when nothing blocks. Rejected — that is fabricated readiness, the exact failure the review identifies.

**Rationale:** Readiness is only as reliable as the evidence each adapter emits. Typed outcomes with probe provenance make mandatory gates enforceable and optional integrations explicitly skippable per policy.

**Trade-offs:** Each mandatory adapter needs healthy, wrong-tenant, unauthorized, timeout, stale-credential, partial-response, and outage contract tests. The suite is large but each case is small and hermetic.

---

### D5: Constrained outbound integration with commit pinning

**Decision:** Intake accepts structured provider, organization, and repository identifiers plus knowledge-source space keys — never arbitrary fetch URLs. Tenant- and environment-specific destination allowlists gate every outbound call, with private, link-local, and reserved ranges blocked after DNS resolution, revalidation on redirect and connect, response size and timeout caps, and no credential forwarding across hosts. Repositories resolve to immutable commit SHAs at approval and record content hash and tool versions. Repository content is untrusted data; build scripts are never executed during generation.

**Alternative considered:** Free-form URLs with post-fetch validation. Rejected — validation after contact is too late for metadata endpoints and malicious redirect targets.

**Rationale:** Server-side fetch and clone turn onboarding into an outbound network and supply-chain boundary. Policy must decide before contact, and reproducibility demands immutable pins.

**Trade-offs:** Allowlists need per-environment maintenance, and branch-to-SHA resolution adds an approval-time step. Mutable-branch workflows keep working; they just pin at the gate that matters.

---

### D6: Exact-plan cleanup with atomic approval and recoverable purge

**Decision:** Cleanup is a durable deletion request bound to a canonical immutable plan: tenant, application, environment, exact resource ids, counts, retention and legal-hold status, data classification, plan version and hash, backup id, requester and approver identities, and short expiry. Approval is consumed atomically under uniqueness and transaction semantics; any plan change invalidates approval and requires reapproval. App-scoped cleanup cannot touch sibling applications or shared operator paths. Legal hold blocks affected deletion. Backup creation and restore evidence precede destructive steps. Purge runs per system with progress, retry, and reconciliation, records tombstones, and reports residual resources explicitly — deletion is never claimed complete while residuals remain. Audit trails and tombstones are retained separately from customer content under their own policy.

```python
# src/investigation_agent_platform/application/onboarding/cleanup.py
"""Deletion manifest planning. Execution lives in the workflow; this binds approval to scope."""
from __future__ import annotations

import hashlib
import json
import secrets
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class DeletionManifest:
    """Canonical immutable deletion plan. Frozen at approval time."""

    tenant_id: str
    app_id: str
    environment: str
    resource_ids: tuple[str, ...]
    counts: tuple[tuple[str, int], ...]
    plan_version: int
    backup_id: str
    requester: str
    approver: str
    expires_at: float


def manifest_hash(manifest: DeletionManifest) -> str:
    """Content hash binding approval to the exact plan. Any field change changes the hash."""
    payload = {
        "tenant_id": manifest.tenant_id,
        "app_id": manifest.app_id,
        "environment": manifest.environment,
        "resource_ids": list(manifest.resource_ids),
        "counts": [list(pair) for pair in manifest.counts],
        "plan_version": manifest.plan_version,
        "backup_id": manifest.backup_id,
        "requester": manifest.requester,
        "approver": manifest.approver,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def mint_approval(manifest: DeletionManifest, ttl_seconds: int = 600) -> tuple[str, str]:
    """Mint a single-use approval token bound to the manifest hash."""
    raw = secrets.token_urlsafe(32)
    binding = manifest_hash(manifest) + ":" + str(int(time.time() + ttl_seconds))
    digest = hashlib.sha256((raw + binding).encode("utf-8")).hexdigest()
    return raw, digest


def approval_valid(
    presented: str,
    stored_digest: str,
    manifest: DeletionManifest,
    approved_hash: str,
    now: float,
    used: bool,
) -> bool:
    """Approval is valid only for the exact manifest, inside expiry, consumed once."""
    if used or now > manifest.expires_at:
        return False
    if manifest_hash(manifest) != approved_hash:
        return False
    candidate = hashlib.sha256((presented + approved_hash).encode("utf-8")).hexdigest()
    return secrets.compare_digest(candidate, stored_digest)
```

**Alternative considered:** Cascade delete plus filesystem removal behind a confirmation prompt. Rejected — unrecoverable, unscoped across distributed stores, blind to holds and shared references, and replayable.

**Rationale:** Confirmation is meaningful only when it approves the exact operation that executes. Distributed deletion cannot be rolled back by a local transaction, so the design carries the plan, the backup proof, and the per-system reconciliation instead.

**Trade-offs:** Manifest authoring, backup verification, and reconciliation workers are real implementation cost. App-scoped precision requires exact resource enumeration per store up front.

---

### D7: Durable orchestration with step ledger, idempotent dispatch, and reconciliation

**Decision:** Apply, attach, and purge run as deterministic workflows on the existing durable workflow platform with one stable workflow id per onboarding request revision and a durable step ledger recording step name and version, idempotency key, attempt, status, external resource reference, timestamps, and sanitized failure classification. Workflow start publishes through a transactional outbox or equivalent dispatcher with a reconciler for orphaned jobs and provider resources. Each adapter operation is idempotent or discover-before-create; compensation is a separate idempotent action, never a rollback. Request keys identify tenant, operation, immutable request revision, and target application and environment; same key with the same payload replays, same key with different payload conflicts. Retries use bounded backoff with jitter and honor provider retry guidance.

**Alternative considered:** Database-backed custom state machine driving direct provider calls. Rejected — duplicates established durable execution, dispatch recovery, and cancellation semantics while increasing recovery burden.

**Rationale:** At-least-once delivery and provider timeouts are normal. Retries alone do not provide exactly-once provisioning; the ledger plus outbox plus reconciler does, without inventing a second runtime.

**Trade-offs:** Workflow versioning discipline and step-ledger storage are permanent maintenance. Failure-injection coverage must prove crash-before and crash-after behavior at every side-effect boundary.

---

### D8: Executable API and CLI contracts with layered abuse controls

**Decision:** The tenants router exposes intake, apply, attach, status, and the two-step cleanup handshake with full request and response models including idempotency keys, expected intake revisions, cleanup plan and confirmation payloads, job status, typed errors, and cancellation and retry semantics. The CLI authenticates through the approved credential mechanism, calls the same application service surface as the API, displays the exact plan before confirmation, and polls actual job state. Rate limits layer as pre-auth global and address-based caps independent of tenant claims, plus post-auth principal, tenant, and global operation caps with stable retry guidance; expensive operations require an authorized capability and an idempotency key. OpenAPI contract tests and CLI-versus-REST conformance suites run the same fixtures and produce equal decisions against a token-authenticated test deployment.

**Alternative considered:** Documenting CLI and REST as parallel surfaces with illustrative snippets. Rejected — aspirational examples diverge from behavior and get copied into production with placeholder semantics.

**Rationale:** An API parity claim needs a single executable contract. Conformance suites make parity a test result instead of a documentation assertion.

**Trade-offs:** Contract suites are new test surface to maintain across every endpoint change. Strict rate limiting can throttle legitimate bulk onboarding, which the quota policy must size explicitly.

---

### D9: Eligibility, isolation, retention, and operability as enforceable policy

**Decision:** Intake approval evaluates a placement and eligibility policy — data region, allowed providers, isolation class, quotas, storage topology, encryption-key policy, and service capability — before any clone, credential resolution, or external write; unsupported combinations fail with evidence recorded. Tenant context propagates signed and validated through jobs, events, schedules, cache and search keys, artifacts, and audit. Rate limiting is fixed to verified-principal or global buckets; unverified tenant headers never key quotas. Retention, legal-hold, backup encryption and verification, restore drills, and audit retention are defined with owners, and deletion reconciles every registered store including indices, graph data, backups, and shared-reference artifacts with reference accounting. Operational telemetry uses tenant-safe structured events with correlation, request, job, and step ids; dashboards and alerts cover accepted, active, and terminal counts, durations, queue age, retry rate, stuck workflows, and dependency outcomes before production launch.

**Alternative considered:** Documenting shared-fleet limits as guidance while accepting all tenants. Rejected — guidance does not prevent a regulated tenant from landing in an unsupported topology.

**Rationale:** Placement, retention, and quota decisions are only real when they gate provisioning. Evidence of each decision persists with policy version, approver, and exceptions so later audits reconstruct why a tenant sits where it sits.

**Trade-offs:** Policy authoring needs product, security, and compliance owners and workload data that may not exist yet; until supplied, the policy ships with conservative defaults and explicit unknowns rather than silent acceptance.

## Data Storage

New persistence is limited to the onboarding control store. Intake files remain the offline capture format with content hashes recorded on immutable revisions. The job read model reuses the existing durable job infrastructure; the store below is the queryable authority for requests, steps, approvals, manifests, and audit linkage, with existing profile tables untouched.

```python
# src/investigation_agent_platform/infrastructure/persistence/models.py (additions)
"""Onboarding control-store tables. Tenant id columns carry RLS policies like existing tables."""
from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column


class TenantRegistryORM:
    """Canonical tenant identity. Immutable internal id; slugs are mutable metadata."""

    __tablename__ = "tenant_registry"
    __table_args__ = (UniqueConstraint("canonical_id", name="uq_tenant_registry_canonical"),)

    __tablename__: str = "tenant_registry"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    canonical_id: str = mapped_column(String(64))
    display_slug: Mapped[str] = mapped_column(String(128))
    owner: Mapped[str] = mapped_column(String(256), default="")
    status: Mapped[str] = mapped_column(String(16), default="ACTIVE")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class OnboardingRequestORM:
    """Immutable request revisions with optimistic concurrency and lifecycle state."""

    __tablename__ = "onboarding_requests"
    __table_args__ = (
        UniqueConstraint("tenant_id", "request_id", "revision", name="uq_onboarding_request_revision"),
        Index("ix_onboarding_request_tenant", "tenant_id", "request_id"),
    )

    __tablename__: str = "onboarding_requests"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    tenant_id: Mapped[str] = mapped_column(String(64))
    request_id: Mapped[str] = mapped_column(String(64))
    revision: Mapped[int] = mapped_column()
    content_hash: Mapped[str] = mapped_column(String(64))
    intake: Mapped[dict] = mapped_column(JSONB)
    policy_version: Mapped[str] = mapped_column(String(32))
    state: Mapped[str] = mapped_column(String(24))
    approvals: Mapped[list] = mapped_column(JSONB, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class OnboardingStepORM:
    """Durable step ledger. One row per step attempt with external resource references."""

    __tablename__ = "onboarding_steps"
    __table_args__ = (
        UniqueConstraint("tenant_id", "request_id", "revision", "step_name", "attempt", name="uq_onboarding_step"),
        Index("ix_onboarding_step_request", "tenant_id", "request_id", "revision"),
    )

    __tablename__: str = "onboarding_steps"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    tenant_id: Mapped[str] = mapped_column(String(64))
    request_id: Mapped[str] = mapped_column(String(64))
    revision: Mapped[int] = mapped_column()
    step_name: Mapped[str] = mapped_column(String(128))
    step_version: Mapped[str] = mapped_column(String(32))
    idempotency_key: Mapped[str] = mapped_column(String(256))
    attempt: Mapped[int] = mapped_column(default=1)
    status: Mapped[str] = mapped_column(String(24))
    external_resource_ref: Mapped[str] = mapped_column(String(512), default="")
    failure_class: Mapped[str] = mapped_column(String(32), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class DeletionManifestORM:
    """Immutable deletion plans with atomic approval consumption and per-system outcomes."""

    __tablename__ = "deletion_manifests"
    __table_args__ = (
        UniqueConstraint("tenant_id", "manifest_id", name="uq_deletion_manifest"),
        Index("ix_deletion_manifest_tenant", "tenant_id", "state"),
    )

    __tablename__: str = "deletion_manifests"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    tenant_id: Mapped[str] = mapped_column(String(64))
    manifest_id: Mapped[str] = mapped_column(String(64))
    plan_hash: Mapped[str] = mapped_column(String(64))
    manifest: Mapped[dict] = mapped_column(JSONB)
    state: Mapped[str] = mapped_column(String(24))
    approval_digest: Mapped[str] = mapped_column(String(64), default="")
    systems_outcome: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
```

## Data Structures

Input and output schemas are separate models. Intake creation (`TenantIntake`, `RepoRef`, `SecretReference`, `DatasourceSpec`, `KnowledgeSourceSpec` in D2) is never reused as a read output. Outputs below carry audit fields (content hash, revision, plan hash, job identity, doomed counts, token lifetime) that inputs must not accept. All schemas use `Field(...)` with descriptions. No schema member accepts secret values or filesystem paths; a future write flow requires new schemas and a new review rather than reusing these.

```python
# src/investigation_agent_platform/api/v1/schemas/tenants.py
"""Tenant onboarding request and response schemas. References only, never secret values."""
from __future__ import annotations

from pydantic import BaseModel, Field

from investigation_agent_platform.application.onboarding.intake import (
    DatasourceSpec,
    KnowledgeSourceSpec,
    RepoRef,
    SecretReference,
)


class IntakePutRequest(BaseModel):
    """PUT intake body. Mirrors the intake file content; paths and values are rejected."""

    display_slug: str = Field(description="Mutable display slug for the tenant.")
    environment: str = Field(default="development")
    repos: list[RepoRef] = Field(default_factory=list)
    credential_refs: dict[str, SecretReference] = Field(default_factory=dict)
    log_indices: list[str] = Field(default_factory=list)
    service_names: list[str] = Field(default_factory=list)
    trace_backend: str = Field(default="")
    datasources: list[DatasourceSpec] = Field(default_factory=list)
    knowledge_sources: list[KnowledgeSourceSpec] = Field(default_factory=list)
    auto_promote: bool = Field(default=False)
    expected_revision: int = Field(default=0, description="Optimistic concurrency precondition.")


class IntakeRead(BaseModel):
    """Stored intake view with revision, hash, and validation outcome."""

    canonical_tenant_id: str = Field(description="Immutable internal tenant id.")
    revision: int = Field(description="Immutable revision number.")
    content_hash: str = Field(description="SHA-256 of the validated intake content.")
    stored: bool = Field(description="True when persisted to the control store.")
    blocking_issues: list[str] = Field(default_factory=list)


class ApplyResponse(BaseModel):
    """POST apply response. Long work runs under the returned job identity."""

    canonical_tenant_id: str = Field(description="Immutable internal tenant id.")
    request_id: str = Field(description="Onboarding request identity.")
    revision: int = Field(description="Revision the job executes.")
    job_id: str = Field(description="Job identity for polling.")


class CleanupRequestResponse(BaseModel):
    """POST cleanup:request response. Doomed plan plus the single-use token."""

    canonical_tenant_id: str = Field(description="Immutable internal tenant id.")
    manifest_id: str = Field(description="Deletion manifest identity.")
    plan_hash: str = Field(description="Content hash binding the approval to the exact plan.")
    doomed_counts: dict[str, int] = Field(default_factory=dict)
    confirmation_token: str = Field(description="Single-use expiring token for confirm.")
    expires_in_seconds: int = Field(description="Token lifetime.")


class CleanupConfirmRequest(BaseModel):
    """POST cleanup:confirm body. Token plus the exact plan hash being approved."""

    manifest_id: str = Field(description="Deletion manifest identity.")
    plan_hash: str = Field(description="Must equal the stored manifest hash.")
    confirmation_token: str = Field(description="Single-use token from cleanup:request.")
```

## Interfaces

### REST API — tenants

| Method | Path | Request | Response | Description |
|---|---|---|---|---|
| `POST` | `/v1/onboarding-requests` | `IntakePutRequest` + `Idempotency-Key` | `ApplyResponse` | Submit intake; returns request, revision, job |
| `GET` | `/v1/onboarding-requests/{request_id}` | — | `IntakeRead` + state | Tenant-authorized status, blockers, next action |
| `POST` | `/v1/onboarding-requests/{request_id}:approve` | `{revision, plan_hash}` | Approval receipt | Privileged approval of an exact revision |
| `POST` | `/v1/onboarding-requests/{request_id}:retry` | `{steps[]}` | `ApplyResponse` | Retry or reconcile selected failed steps |
| `POST` | `/v1/onboarding-requests/{request_id}:cancel` | — | Cancellation receipt | Cancel; workflow reports residual status |
| `POST` | `/v1/tenants/{tenant_id}:deletion-request` | scope + tier | `CleanupRequestResponse` | Preview manifest plus token |
| `POST` | `/v1/deletion-requests/{id}:approve` | `CleanupConfirmRequest` | Job receipt | Consume token atomically; start purge |
| `GET` | `/v1/jobs/{job_id}` | — | Job read model | Tenant-scoped versioned progress |

Error responses use the closed envelopes: `TENANT_MISMATCH` (403), `AUTH_TENANT_REQUIRED` (401), `TENANT_NOT_ALLOWLISTED` (403), `CAPABILITY_MISSING` (403), `OPERATOR_ROLE_REQUIRED` (403), `UNKNOWN_SOURCE_KIND` (400), `STALE_REVISION` (409), `CONFIRMATION_INVALID` (409), `PLAN_CHANGED` (409), `LEGAL_HOLD_BLOCKS` (423). Denials are audited without leaking tenant existence unnecessarily.

### CLI commands

| Command | Arguments | Description |
|---|---|---|
| `capture` | `--tenant, --repo (repeatable), --branch, --oracle-yaml, --dry-run` | Validate intake locally; write revisioned artifact unless dry-run |
| `apply` | `--tenant, --api-base, --idempotency-key` | Submit intake; poll job to terminal state |
| `attach` | `--tenant, --repo, --app, --api-base` | New revision with added repository; poll to terminal state |
| `status` | `--tenant, --api-base, --json` | Request, revision, blockers, health; machine-readable with `--json` |
| `cleanup-request` | `--tenant, --app, --full, --api-base` | Print manifest and counts; receive token |
| `cleanup-approve` | `--tenant, --manifest, --token, --api-base` | Consume token; start purge job |

## Accessibility

### CLI-specific requirements

- **Help text:** Every command and flag carries descriptive help surfaced via `--help`. Output is parseable and readable without color; destructive previews print doomed counts as plain text before any approval step.
- **Error messages:** Errors name the failing check (allowlist, capability, intake validation, credential reference, plan hash, token state, legal hold) and the corrective action, never bare codes.
- **Structured output:** `status` supports `--json` for machine-readable output so operators can integrate with assistive tooling and scripts.

### REST-specific requirements

- **Error envelopes:** All failures use the closed vocabulary above with human-readable detail strings; clients never parse free-form prose to branch logic.
- **Long operations:** `apply`, `attach`, and purge return `202 Accepted` with job identities immediately; progress polls `GET /v1/jobs/{job_id}` with terminal states announced in the response body.

## Implementation Detail

Implementation order is the companion implementation plan. Engineering rules apply across all phases: intake validation threads through the tenants router into the control store before any clone, credential resolution, or external write; the policy and eligibility check runs before side effects; adapters run as request-scoped instances under bounded timeouts and concurrency; the workflow persists step outcomes with idempotency keys and external resource references; telemetry uses additive fail-open hooks with closed label vocabularies so a metrics failure can never fail provisioning; every privileged route re-checks capability and approval binding rather than trusting earlier checks.

## Migrations

Creation order: tenant registry and onboarding control-store tables first (`tenant_registry`, `onboarding_requests`, `onboarding_steps`, `deletion_manifests`), with RLS policies matching existing tables and app-role grants audited, then router and workflow wiring against them. Backfill: none — existing tenants adopt through a reservation operation that mints canonical ids and links current profiles, never through silent default rows. Compatibility note: served-cached conclusions and existing profile revisions are untouched; new optional JSON keys degrade to labeled fallbacks on old readers.

## Testing Philosophy

### Authorization matrix

Prove ordinary tenant users cannot apply privileged onboarding, alter authorization policy, approve their own regulated onboarding, or clean another tenant, while explicitly authorized roles perform only their allowed operations. Cover the bootstrap flow so a new tenant without a claim can be reserved but never self-claimed, and audit every denial without leaking tenant existence.

### Path and outbound safety

Exercise traversal, encoded separators, symlink escapes, and absolute-path inputs against the containment guard, plus DNS rebinding, redirect-to-private targets, unusual encodings, oversized responses, branch movement, and shell metacharacters against destination policy. Denials happen before contact or filesystem access, and every generated profile records immutable source revisions.

### Secrets handling

Attempt cross-tenant, wrong-environment, wrong-source, expired, and write-capable secret references and confirm fail-closed behavior. Attempt secret values in intake, job inputs, logs, backups, and API responses and confirm rejection and redaction at every boundary, with deterministic rotation and version behavior.

### Lifecycle and concurrency

Cover invalid transitions, stale-version writes, activation with incomplete gates, retry resumption against the same revision, changed-input new revisions with approval invalidation, and cancel and deprovision races. Concurrent attach tests must never lose updates, and replay must reproduce the same plan from pinned inputs.

### Recovery and deletion

Inject crashes before and after each side effect and at dispatch boundaries; prove replay duplicates no external resource, uncertain outcomes reconcile by provider identity, stuck jobs alert with explicit resume or terminate actions, and deletion manifests reconcile every registered store with residual reporting. Restore drills pass before any destructive phase ships.

### Contract conformance

Run OpenAPI schema and status-code suites plus CLI-versus-REST conformance on identical fixtures producing equal decisions, against a token-authenticated test deployment. Example flows must pass end to end and never acknowledge uncreated jobs.

### Isolation and abuse

Demonstrate tenant A cannot reach tenant B data through REST, jobs, schedules, cache, search, artifacts, admin flows, or operator scopes, with app-role RLS tests and spoofed-header rate-limit tests. Bound accepted work with per-principal, per-tenant, and global caps and prove throttling returns stable retry guidance.

## Documentation Plan

### `docs/IAP-architecture-v1.0.md` tenant-onboarding section

**Audience:** Operators and reviewers

Document the narrowed scope and trust boundary, tenant registry and capability model, control-store schema and state machine, typed secret-reference contract, deletion-manifest lifecycle, and CLI plus REST parity rule with executable contract tests. Include the operator-home layout, the allowlist lifecycle, and every rollout flag with defaults.

### Operator runbook for onboarding

**Audience:** Operators

Provide the copy-paste sequence: reserve the tenant, prepare credential references, run `capture --dry-run` until green, submit intake, complete eligibility and approvals, run `apply`, complete `needs_review`, verify readiness, and activate, with the attach sequence for new repositories and the backup-first deletion sequence with tier guidance and restore pointers.

### `tenant-id.env.example.yaml` header

**Audience:** Operators

Keep the template self-documenting: every field carries a comment explaining its source (operator-typed, registry-issued, or generated) and every default states why it is safe. No secret values, no filesystem paths, and no secret-shaped examples anywhere in the file.

### API reference for the tenants surface

**Audience:** Developers and integrators

Document each endpoint with request and response models, idempotency and revision semantics, job polling, token lifetimes, error envelopes, and rate-limit behavior, with curl examples for intake, approval, status, and the two-step cleanup handshake.

## Risks / Trade-offs

### Registry and role model as new shared state

**Risk:** The tenant registry and capability model are new authorities to operate, with alias, tombstone, and reservation semantics that can surprise operators during renames or retirements.

**Mitigation:** Renames are alias operations preserving the canonical id and audit continuity; removals retire rather than delete; every denial and reservation is audited with actor, decision, and reason.

### Control-store and workflow maintenance

**Risk:** The request aggregate, step ledger, outbox dispatcher, and reconciler are permanent maintenance with versioning discipline across workflow definitions.

**Mitigation:** State machines are policy-driven so simple tenants skip approval and cleanup states; failure-injection suites prove recovery on every boundary before each phase ships.

### Policy data may not exist yet

**Risk:** Eligibility, residency, quota, and compliance inputs need product, security, and compliance owners plus workload data that may be unavailable at build time.

**Mitigation:** Ship conservative defaults with explicit unknowns and pre-side-effect rejection; record policy version, decision, evidence, approver, and exceptions so later data tightens decisions without schema churn.

### Shared-fleet limits persist

**Risk:** Singletons for code root, scheduler namespace, topics, and model provider remain, with noisy-neighbor and residency implications no onboarding design can remove.

**Mitigation:** Eligibility rejects what the fleet cannot enforce, context propagates signed and validated end to end, and dedicated tiers stay an explicit parked future with entry conditions rather than a silent gap.
