# Design: S7 Exact-Fetch Continuation v1.1

## Context

The current S7 handoff model records `next_fetch`, `price`, and a free-form `resume_pointer` in the conclusion JSONB payload. The active workflow `resume` signal only clears the paused flag. The `RESTART_CONTINUE` API creates a new investigation version and copies evidence and hypotheses, but it does not consume a persisted handoff or dispatch the exact blocked fetch. The existing execution activity already provides the normal action authorization and provider boundary that continuation must reuse.

This design adds an explicit continuation record and dispatch path without introducing a queue, daemon, external workflow engine, or second provider-execution path. The continuation is a durable loop edge from a terminal handoff back to one exact authorized action.

No `docs/dev/architecture.md` exists in the project. The architecture update is therefore specified in the Documentation Plan rather than applied here.

## References

- `docs/design/DES-S7-handoff-v1.0.md` — Defines terminal handoff states, concrete fetch semantics, pricing, and resumable work-order intent.
- `professor/luna_iap_rev.md` §6.1 WP-04 — Requires exact blocked-fetch continuation, secure authorization, and idempotent execution.
- `professor/luna_iap_rev.md` §7 Phase 4 — Requires a typed/versioned payload, persistence, API, Temporal boundaries, pointer consumption, retries, auditability, and adversarial scenarios.
- `src/investigation_agent_platform/domain/finding/models.py` — Current handoff states, concrete-fetch derivation, and pricing rules.
- `src/investigation_agent_platform/api/v1/routers/events.py` — Current pause/resume API and workflow signaling behavior.
- `src/investigation_agent_platform/api/v1/routers/investigations.py` — Current reprocess API and terminal-version behavior.
- `src/investigation_agent_platform/application/investigation/services.py` — Current `RESTART_CONTINUE` artifact-copy implementation.
- `src/investigation_agent_platform/application/worker/workflows/run_investigation.py` — Current workflow signals, identity, and loop state.
- `src/investigation_agent_platform/application/worker/activities/execution.py` — Existing normal action authorization and provider execution boundary.
- `src/investigation_agent_platform/application/worker/activities/inputs.py` — Existing typed Temporal activity input conventions.
- `src/investigation_agent_platform/infrastructure/persistence/finding_repository.py` — Existing handoff persistence and tenant-scoped conclusion access.

## Goals / Non-Goals

**Goals:**

- Persist one typed, versioned continuation payload for a resumable S7 handoff.
- Bind the continuation to tenant, investigation, conclusion, and exact blocked action parameters.
- Require the continuation action to pass the same authorization and provider path as an ordinary execution action.
- Provide a tenant-authorized API that atomically claims a continuation and dispatches it through Temporal.
- Make duplicate requests idempotent and prevent concurrent double execution.
- Preserve the original handoff and conclusion as honest historical records when continuation fails.
- Record success, failure, authorization denial, provider failure, and stale-pointer outcomes audibly without logging secrets or prompt contents.
- Keep the implementation session-first and use the existing Temporal workflow and activity infrastructure.

**Non-Goals:**

- Do not make the current generic `resume` signal execute an arbitrary action.
- Do not treat `RESTART_CONTINUE` artifact copying as exact-fetch continuation.
- Do not create a queue, daemon, scheduler, external orchestration service, or second action executor.
- Do not mutate the original terminal conclusion into a successful conclusion before the continuation has completed and the normal conclusion path has committed.
- Do not add a generic “run any action” API.
- Do not authorize implementation, migration execution, deployment, or release.

## Decisions

### D1: Use a dedicated typed continuation record with composite conclusion identity

**Decision:** Persist a continuation record separate from the conclusion JSONB payload. The conclusion keeps the original `S7Handoff`; the continuation record is the executable, single-use loop edge. `InvestigationConclusion` has no domain `id`; its identity is `(tenant_id, investigation_id, handoff_version)`. The continuation therefore stores `tenant_id`, `investigation_id`, and `handoff_version` rather than a nonexistent `conclusion_id`.

```python
# src/investigation_agent_platform/domain/continuation/models.py
class ContinuationStatus(StrEnum):
    PENDING = "PENDING"
    CLAIMED = "CLAIMED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    DENIED = "DENIED"
    EXPIRED = "EXPIRED"
    SUPERSEDED = "SUPERSEDED"


S7_CONTINUATION_VERSION_CURRENT = "s7-continuation-v1"
S7_CONTINUATION_MAX_ATTEMPTS = 20
S7_CONTINUATION_TTL_SECONDS = 7 * 24 * 60 * 60


class S7Continuation(BaseModel):
    model_config = ConfigDict(frozen=True)

    continuation_id: UUID
    version: str = Field(default=S7_CONTINUATION_VERSION_CURRENT, max_length=32)
    tenant_id: str = Field(..., max_length=128)
    investigation_id: UUID
    handoff_version: str = Field(..., max_length=16)
    source_handoff_state: HandoffState
    action: str = Field(..., max_length=64)
    parameters: dict[str, Any]
    target: str = Field(..., max_length=1024)
    price: float = Field(..., gt=0.0, le=MAX_FETCH_PRICE)
    idempotency_key: str = Field(..., min_length=16, max_length=128)
    status: ContinuationStatus = ContinuationStatus.PENDING
    attempt_count: int = Field(default=0, ge=0, le=S7_CONTINUATION_MAX_ATTEMPTS)
    created_at: datetime
    expires_at: datetime
    claimed_at: datetime | None = None
    completed_at: datetime | None = None
    last_error_code: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def validate_payload(self) -> "S7Continuation":
        # Only these states represent an exact acquisition edge. BUDGET_DEFERRED
        # must already have a concrete priced fetch; TELEMETRY/CAPABILITY may
        # carry an exact fetch when the blocked capability is restored, and D2
        # performs the concrete+priced check before creating this record.
        if self.source_handoff_state not in {
            HandoffState.BUDGET_DEFERRED,
            HandoffState.TELEMETRY_DEFERRED,
            HandoffState.CAPABILITY_DENIED,
        }:
            raise ValueError("only resumable S7 handoff states may create continuations")
        if not self.target.strip():
            raise ValueError("continuation target is required")
        allowed = {member.value for member in ActionType}
        if self.action not in allowed:
            raise ValueError("continuation action is not a known action type")
        return self
```

The implementation must use the existing concrete-fetch derivation and the real `ActionType` enum, then apply the current `capability_policy` authorization before execution. `parameters` is the exact normalized action parameter object that will be passed to the normal execution path; it is not reconstructed from `resume_pointer`.

**Alternative considered:** Store only a richer `resume_pointer` in the conclusion JSONB. Rejected because free-form text cannot provide typed validation, compare-and-set consumption, exact parameter identity, or safe idempotency.

**Rationale:** A separate record gives the API and worker a durable state machine while leaving the original conclusion immutable and auditable.

**Trade-offs:** This requires one migration and one repository, but avoids overloading the conclusion schema and avoids treating historical terminal state as mutable work state. Composite conclusion identity is less convenient than a row UUID, but it matches the current domain contract and avoids inventing a domain identifier that the read path does not expose.

---

### D2: Bind continuation identity to tenant, investigation, handoff version, and exact action

**Decision:** A continuation is created only from a persisted conclusion identified by `(tenant_id, investigation_id, handoff_version)` whose handoff is one of `BUDGET_DEFERRED`, `TELEMETRY_DEFERRED`, or `CAPABILITY_DENIED`, and whose `next_fetch` is concrete and positively priced. `BUDGET_DEFERRED` already requires that exact fetch by the S7 validator; the other two states may carry a resumable pointer, but continuation creation still refuses them unless a concrete positive-priced fetch is present. The service derives and validates:

```python
# src/investigation_agent_platform/application/investigation/continuation.py
def continuation_idempotency_key(
    tenant_id: str,
    investigation_id: UUID,
    handoff_version: str,
    action: str,
    parameters: Mapping[str, Any],
) -> str:
    material = json.dumps(
        {
            "tenant_id": tenant_id,
            "investigation_id": str(investigation_id),
            "handoff_version": handoff_version,
            "action": action,
            "parameters": parameters,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(material).hexdigest()
```

The service must reject a request when the supplied continuation ID is not owned by the tenant, does not point to the requested investigation, does not match the stored handoff version, is not pending, or has an action/parameters digest different from the stored payload. The JSON canonicalization is explicit: sorted object keys, compact separators, UTF-8/ASCII-safe encoding, and `str()` conversion for UUID-like values before SHA-256.

**Alternative considered:** Accept a caller-supplied action and parameters while using the handoff only as a hint. Rejected because that would create a generic action endpoint and allow an operator to execute an action that the original evidence gap did not authorize.

**Rationale:** Exact identity prevents cross-tenant pointer reuse, parameter substitution, stale conclusion use, and action confusion.

**Trade-offs:** If the underlying provider configuration changes, the original exact action remains the requested action, but normal authorization may deny it. The original handoff remains honest in that case.

---

### D3: Claim with a database compare-and-set before Temporal dispatch

**Decision:** The API claims `PENDING` to `CLAIMED` in a tenant-scoped transaction before starting the dedicated continuation workflow. The claim update includes `continuation_id`, `tenant_id`, `investigation_id`, `handoff_version`, `status = PENDING`, `attempt_count < S7_CONTINUATION_MAX_ATTEMPTS`, and `idempotency_key`. A unique constraint prevents duplicate logical continuations.

```sql
-- conceptual SQL; implementation uses the repository's SQLAlchemy style
UPDATE s7_continuations
SET status = 'CLAIMED', claimed_at = :now, attempt_count = attempt_count + 1
WHERE continuation_id = :continuation_id
  AND tenant_id = :tenant_id
  AND investigation_id = :investigation_id
  AND handoff_version = :handoff_version
  AND status = 'PENDING'
  AND attempt_count < 20
  AND expires_at > :now
  AND idempotency_key = :idempotency_key;
```

If the update affects zero rows, the service reads the existing row under the same tenant scope. A `CLAIMED` row with the same idempotency key returns the existing dispatch acknowledgment; a completed or terminal row returns its recorded outcome; an expired row transitions to `EXPIRED`; a mismatch is `409 CONFLICT` without revealing cross-tenant existence. A concurrent create uses insert-or-read-existing on the same tenant/investigation/idempotency unique key and returns the existing row when the canonical payload matches; a payload mismatch is `409 CONFLICT`.

The API must not mark `SUCCEEDED` before the activity commits its evidence through the normal execution path. If Temporal dispatch fails after the claim, a reconciliation-safe failure transition records `FAILED` with `DISPATCH_UNAVAILABLE`; it never silently resets to `PENDING`.

**Alternative considered:** Claim in application memory and dispatch first. Rejected because concurrent API requests and process restarts would permit duplicate execution.

**Rationale:** Database compare-and-set is the existing persistence system's durable concurrency boundary and is deterministic under retries.

**Trade-offs:** A dispatch failure leaves a terminal continuation record requiring explicit retry policy or operator action. That is safer than duplicate provider calls; a bounded retry can be implemented against the same idempotency key.

---

### D4: Start a dedicated continuation workflow and reuse the normal execution path

**Decision:** Continuation dispatch starts a dedicated Temporal workflow with ID `wf-continuation-{continuation_id}`. It carries only the stored continuation ID and tenant identity in its input; the workflow activity reloads the durable record and calls the existing `execute_action_activity` boundary. The update/signal alternative is rejected because the original investigation workflow is terminal and its handle is unavailable after completion; the current `/resume` path only works for an active `PAUSED` workflow. The dedicated ID cannot collide with `wf-investigation-{investigation_id}`.

The execution path is:

```text
POST /investigations/{id}/continuations/{continuation_id}/execute
  -> tenant + principal authorization
  -> load and validate continuation
  -> compare-and-set PENDING -> CLAIMED
   -> start wf-continuation-{continuation_id}
  -> load continuation by tenant/id
  -> normal action authorization
  -> execute_action_activity(ExecuteActionInput from stored payload)
  -> bank evidence using the original investigation id
  -> mark continuation SUCCEEDED or FAILED
  -> normal reason/verify/conclude loop remains responsible for a new conclusion
```

The activity must reject any payload whose tenant, investigation, action, parameters digest, or continuation version differs from the durable record. It must not accept action parameters from an untrusted signal body.

**Alternative considered:** Signal/update `RunInvestigationWorkflow.resume`. Rejected as non-functional for the completed workflows that own S7 terminal handoffs: a terminal Temporal workflow has no live handle to signal. A new workflow is therefore required, not merely preferable.

**Rationale:** The existing execution activity already centralizes provider authorization, action allowlisting, tenant context, evidence banking, and failure typing.

**Trade-offs:** A continuation starts a second short-lived workflow rather than continuing the completed investigation workflow. This is explicit and auditable, requires a reserved ID scheme and Temporal registration, and does not introduce a second provider executor.

---

### D5: Preserve the original handoff and create a new terminal conclusion only through the normal loop

**Decision:** Successful continuation only records successful fetch execution and continuation completion. It does not rewrite the original conclusion. The next verification/reasoning cycle must produce a new conclusion using the normal lifecycle/UoW path. Failed, denied, expired, or provider-error continuation records retain the original handoff and store a typed continuation outcome.

The new conclusion, if produced, references `continuation_id` in `recommended_next_steps` as a bounded audit string such as `Continuation <uuid> completed action <ActionType>`. No `metadata` field is added because `InvestigationConclusion` has none. It must not overwrite the original conclusion in a way that erases the prior terminal state.

**Alternative considered:** Mutate the original conclusion to `INVESTIGATING` before dispatch. Rejected because terminal conclusion immutability and historical auditability would be lost.

**Rationale:** A failed continuation is evidence about the attempted next step, not evidence that the original conclusion was wrong.

**Trade-offs:** Consumers must understand that a continuation record and its original conclusion are separate lifecycle records until a later conclusion is durably committed.

## Data Storage

Add a migration-backed `s7_continuations` table. Use the repository's existing tenant-scoped SQLAlchemy/RLS conventions.

```python
# src/investigation_agent_platform/infrastructure/persistence/models.py
class S7ContinuationORM(Base):
    __tablename__ = "s7_continuations"

    continuation_id: Mapped[UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    investigation_id: Mapped[UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    handoff_version: Mapped[str] = mapped_column(String(16), nullable=False)
    source_handoff_state: Mapped[str] = mapped_column(String(32), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    parameters: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    target: Mapped[str] = mapped_column(String(1024), nullable=False)
    price: Mapped[float] = mapped_column(Numeric(10, 2, asdecimal=False), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(64))

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "investigation_id", "idempotency_key",
            name="uq_s7_continuation_tenant_investigation_key",
        ),
        CheckConstraint("price > 0", name="ck_s7_continuation_positive_price"),
        CheckConstraint("attempt_count >= 0 AND attempt_count <= 20", name="ck_s7_continuation_attempt_bounds"),
    )
```

The table requires tenant RLS policy, tenant-scoped indexes, and audit-safe status transitions. There is deliberately no `conclusion_id` FK because the domain conclusion is identified by `(tenant_id, investigation_id, handoff_version)`, while the ORM row UUID is not exposed by the domain read model. The service verifies that composite identity against the current conclusion before insert and claim. `Numeric(..., asdecimal=False)` keeps the ORM boundary as a Python float, matching the domain `price` field. No original conclusion row is deleted or mutated by continuation creation.

## Data Structures

```python
# src/investigation_agent_platform/application/worker/activities/inputs.py
@dataclass
class ExecuteContinuationInput:
    tenant_id: str
    continuation_id: str
    principal_id: str
    correlation_id: str | None = None  # API request correlation ID; audit metadata only


@dataclass
class ExecuteContinuationOutput:
    continuation_id: str
    status: str
    action: str
    evidence_added: int = 0
    error_code: str | None = None
```

```python
# src/investigation_agent_platform/api/v1/schemas/continuations.py
class ContinuationExecuteResponse(BaseModel):
    continuation_id: UUID
    investigation_id: UUID
    status: ContinuationStatus
    action: str
    target: str
    price: float
    idempotent_replay: bool = False
    error_code: str | None = None
```

The request carries no action or parameter fields. The server loads the stored payload and exposes only the non-sensitive target and status in the response. Provider credentials, prompt content, evidence content, and raw parameters must not be returned by this endpoint.

## Interfaces

### REST API — Execute continuation

| Method | Path | Request | Response | Description |
| --- | --- | --- | --- | --- |
| `POST` | `/investigations/{investigation_id}/continuations/{continuation_id}/execute` | No body | `ContinuationExecuteResponse` | Claim and execute the exact stored blocked fetch for the tenant-authorized principal |

Expected errors:

| Code | HTTP | Meaning |
| --- | ---: | --- |
| `INVALID_INVESTIGATION_ID` | 400 | Path identifier is malformed |
| `CONTINUATION_NOT_FOUND` | 404 | No continuation exists in the caller's tenant scope |
| `CONTINUATION_MISMATCH` | 409 | Continuation does not belong to the requested investigation or stored digest differs |
| `CONTINUATION_ALREADY_COMPLETED` | 409 | Continuation has a terminal outcome; response may include the recorded outcome |
| `CONTINUATION_EXPIRED` | 409 | TTL or source handoff version is no longer valid |
| `CONTINUATION_AUTHORIZATION_DENIED` | 403 | Principal cannot execute the stored action or provider capability is unavailable |
| `CONTINUATION_DISPATCH_UNAVAILABLE` | 503 | Temporal dispatch failed after a durable claim |

The endpoint must use `require_tenant` and `require_principal`, load through a tenant-scoped repository, and perform no cross-tenant existence disclosure.

### Temporal boundary

Register a dedicated continuation workflow with ID `wf-continuation-{continuation_id}` and its activity. The workflow input contains only the continuation ID and tenant identity; the activity reloads the durable record by tenant context and reconstructs the exact `ExecuteActionInput`. The activity then calls the existing `execute_action_activity` boundary or a shared internal function used by it, without duplicating provider dispatch.

## Accessibility

### REST and CLI-facing diagnostics

The continuation API is machine-facing. Error responses must contain stable codes and actionable, non-sensitive messages. Responses must not require color, timing, or visual interpretation. If a future CLI wrapper is added, `--help` and `--json` output must expose continuation ID, status, target, and error code without secrets or raw evidence.

## Implementation Detail

### Continuation creation

Continuation creation uses an explicit post-conclusion service transaction, not an assumed universal UoW hook. The verdict path currently uses `PostgresConclusionUnitOfWork.commit_verdict`; the abstention path at `lifecycle.py:1054` and the epistemic path at `lifecycle.py:3030` call `finding_repo.save_conclusion` directly. Because resumable deferrals are born on those abstention paths, WP-04 selects a second-transaction plus reconciliation posture rather than pretending H14 covers every path.

The service sequence is: persist the conclusion using the existing lifecycle path; in the same application operation, read back the tenant-scoped conclusion and attempt an idempotent continuation insert using the composite identity and canonical payload; if the insert fails after the conclusion commit, record `CONTINUATION_CREATE_FAILED` in the existing transition/audit path and leave the original handoff unchanged. The execute endpoint refuses any handoff without a continuation row and reports it as non-resumable. This decision is consistent with the Transaction Boundary Split risk below; WP-04 does not expand the H14 UoW signature.

The creation service must:

1. Require `source_handoff_state` in the resumable set.
2. Require `next_fetch` to satisfy `names_concrete_fetch_target`.
3. Derive the action and normalized parameters from the typed gap target, never from prose.
4. Verify `price_next_fetch(next_fetch) > 0` and equals the stored price.
5. Bind tenant, investigation, conclusion, and handoff version.
6. Compute the idempotency key from canonical JSON.
7. Insert one `PENDING` record with `expires_at = created_at + S7_CONTINUATION_TTL_SECONDS`, using insert-or-read-existing on the unique idempotency key.

### Claim and dispatch service

The service must expose `claim_and_dispatch(tenant_id, continuation_id, investigation_id, principal_id, correlation_id)`. It loads the row under tenant scope, authorizes the stored action against the principal and current capability profile, and executes the compare-and-set including investigation and handoff version. Only after the claim commits does it start `wf-continuation-{continuation_id}`. A repeated request with the same key returns the existing status; a different key or mismatched investigation returns `409`.

### Activity execution

The continuation activity receives only the ID and tenant identity. It loads the record, confirms `CLAIMED`, confirms the attempt has not exceeded the bounded retry count, and creates `ExecuteActionInput` from durable fields. It calls the existing action executor with the original `investigation_id`, tenant, action, parameters, loop version, and continuation context. Evidence rows retain the original investigation identity and include a continuation reference in workflow-only metadata where the existing evidence model permits it.

On success, update the row to `SUCCEEDED` with completion time and evidence count. On typed authorization denial, update to `DENIED`. On provider failure, update to `FAILED` with a typed error code and leave the original handoff unchanged. On transient Temporal/activity failure, use the existing retry policy only when the action is idempotent and the continuation row remains the same claimed attempt; otherwise record failure and require explicit retry using the same idempotency key.

### Security and authorization

Tenant ID comes from the authenticated request dependency, never from request body or caller-supplied continuation payload. The action allowlist is derived from the typed claim and current capability policy. The continuation cannot change repository, revision, path, symbol, query, provider, or scope. The service must verify the original handoff's tenant and investigation ownership before claim. Logs include IDs, action name, status, and error code only; no secrets, prompt text, provider response, or raw evidence content.

### Expiry and stale pointers

Continuation records carry `expires_at`, computed from the application clock at creation. Before claim, the service checks `now < expires_at`, the source conclusion still exists, and the handoff version matches. A stale pointer becomes `EXPIRED`, not `FRONTIER_EXHAUSTED` or success. When a later conclusion for the same investigation has a different handoff version, the conclusion/continuation creation service marks any older `PENDING` continuation `SUPERSEDED` before inserting the new one. `SUPERSEDED` is written only by that version-change operation; `EXPIRED` is written by the claim service when the clock crosses `expires_at`.

## Migrations

1. Add the `s7_continuations` ORM model and repository interfaces without changing current handoff JSONB fields.
2. Add the migration creating the table, constraints, tenant indexes, and RLS policy. Add the unique tenant/investigation/idempotency constraint.
3. Add repository methods for tenant-scoped insert, get, compare-and-set claim, success/failure transition, and terminal readback.
4. Add the selected post-conclusion continuation creation/reconciliation service to all three lifecycle creation sites: verdict, abstention, and epistemic. Do not expand the H14 UoW signature in this WP.
5. Register the dedicated continuation workflow/activity and reserve `wf-continuation-{continuation_id}` IDs.
6. Add the API route and insert-or-read-existing create behavior.
7. Do not add readers for unsupported legacy continuation formats. Existing conclusions without a continuation row remain non-resumable and are reported honestly.

No migration should be executed as part of this design-only authorization.

## Testing Philosophy

### Domain and schema validation

Test that only resumable handoff states create continuations, concrete fetches are required, prices are positive and deterministic, action parameters are canonicalized, and malformed or unsupported payloads are rejected before persistence.

### Tenant and authorization boundaries

Test that tenant A cannot read, claim, execute, or infer the existence of tenant B's continuation. Test principal denial, capability denial, stale handoff version, action substitution, repository/revision substitution, and cross-investigation pointer reuse.

### Concurrency and idempotency

Run concurrent claim attempts and verify exactly one `PENDING -> CLAIMED` transition. Repeat the same request after claim, success, failure, and dispatch failure and verify deterministic acknowledgments with no duplicate provider action. Use the same idempotency key across Temporal retry and activity retry.

### Exact-fetch execution

Verify the activity reconstructs the exact stored action and parameters, invokes the normal execution authorization path, banks evidence against the original investigation, and never accepts action data from the signal or API body.

### Failure and recovery

Exercise provider timeout, authorization denial, unavailable provider, malformed stored payload, Temporal dispatch failure, activity retry, partial evidence banking, expired pointer, and source conclusion replacement. The original handoff must remain unchanged whenever continuation does not produce a new committed conclusion.

### Temporal replay

Use the repository's Temporal test conventions to replay the continuation workflow with deterministic activity inputs and recorded outcomes. Verify no wall-clock, random UUID, external I/O, or environment-dependent value is read inside workflow code. Use the existing `pytest.skip` convention from `tests/workflow_harness/conftest.py:26` when the test server is unavailable; do not invent a separate status marker.

### Integration acceptance

Use a real tenant-scoped database and production-equivalent worker composition to prove these concrete cases: (A) exact successful fetch and fresh-context evidence readback; (B) capability denial with no provider call and unchanged handoff; (C) typed provider failure with unchanged handoff; (D) concurrent duplicate claim with one provider execution; (E) cross-tenant pointer denial without an existence oracle; (F) stale handoff version and `SUPERSEDED`; (G) clock-past-`expires_at` and `EXPIRED`; (H) Temporal start failure after claim with `DISPATCH_UNAVAILABLE` and no silent reset; and (I) malformed legacy row refused as non-resumable. These are the WP-04/Phase 4 acceptance cases; no separate "Phase 9" artifact is assumed.

## Documentation Plan

### `docs/design/IAP-S7-continuation-implementation-design-v1.1.md`

**Audience:** Implementers and reviewers. This artifact is the implementation contract and must be retained with the accepted design decision and test evidence.

### `docs/dev/architecture.md`

**Audience:** Maintainers and operators. No such file exists currently. If the architecture document is created later, add the continuation record/repository, API claim boundary, Temporal continuation workflow/activity, and normal execution-path reuse to the system and sequence diagrams. Explicitly show that the original conclusion remains immutable until a later normal conclusion commit.

### `docs/audits/IAP-S1-S7-remediation-findings.md`

**Audience:** Reviewers. After implementation, update WP-04 from design-authorized to implementation status and record exact migration, API, Temporal, tenant, idempotency, and live acceptance evidence.

## Risks / Trade-offs

### Duplicate provider execution

**Risk:** A dispatch retry or concurrent API request could execute the same provider action twice.

**Mitigation:** Use a tenant-scoped database compare-and-set, deterministic idempotency key, one claimed attempt record, and an action-level idempotency contract before enabling retries.

### Stale or changed capability

**Risk:** The capability that blocked the original fetch may remain unavailable or may change after the handoff.

**Mitigation:** Reauthorize at claim and activity time. Record `DENIED` or `FAILED` with a typed code while preserving the original handoff. Never silently substitute a different action.

### Partial evidence banking

**Risk:** A provider may return partial evidence before an activity failure.

**Mitigation:** Use the existing evidence idempotency/fingerprint path, record the continuation attempt, and let the next normal verification cycle evaluate the actual banked evidence. Do not mark the continuation successful solely because an activity started.

### Legacy malformed handoffs

**Risk:** Existing JSONB rows may not satisfy the new typed continuation contract.

**Mitigation:** Do not create continuations from malformed rows. Return an explicit non-resumable/stale result and require a new conclusion or owner-approved remediation; do not add a permissive legacy reader.

### Transaction boundary split

**Risk:** If conclusion and continuation creation use different transactions, a conclusion could advertise a resumable handoff without a continuation record.

**Mitigation:** Extend the existing conclusion UoW to include continuation creation atomically, or block the feature until a tested compensating transaction is approved. Do not expose the execute endpoint for unbacked handoffs.

### Workflow authority expansion

**Risk:** Passing arbitrary action data through a Temporal signal could turn continuation into an unrestricted execution API.

**Mitigation:** Signals carry only a continuation ID. The worker reloads and validates the durable typed record, then calls the existing action authorization/execution path.
