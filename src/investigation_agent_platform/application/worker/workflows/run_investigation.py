# src/investigation_agent_platform/application/worker/workflows/run_investigation.py
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError

with workflow.unsafe.imports_passed_through():
    pass

with workflow.unsafe.imports_passed_through():
    from investigation_agent_platform.application.worker.activities import (
        CaptureKnowledgeInput,
        CheckpointInput,
        ConcludeInvestigationInput,
        CreateInvestigationInput,
        ExecuteActionInput,
        PromoteFulfillmentEvidenceInput,
        PublishEventInput,
        ReasonInput,
        RecordInputRequirementInput,
        RetrieveEvidenceInput,
        RetrieveKnowledgeInput,
        SetInputRequirementStateInput,
        TransitionInvestigationInput,
        VerifyRootCauseInput,
        capture_knowledge_activity,
        checkpoint_activity,
        conclude_investigation_activity,
        create_investigation_activity,
        execute_action_activity,
        promote_fulfillment_evidence_activity,
        publish_event_activity,
        reason_activity,
        record_input_requirement_activity,
        retrieve_evidence_activity,
        retrieve_knowledge_activity,
        set_input_requirement_state_activity,
        transition_investigation_activity,
        verify_root_cause_activity,
    )

with workflow.unsafe.imports_passed_through():
    # Story envelope keys for workflow records (WFLW): literal comp/func/
    # stage/step/outcome keys merged into the record extra — same contract
    # as activities but with no helper call and no tick()/elapsed_ms, so
    # workflow code stays replay-deterministic (no clock reads, no new
    # logger API surface). WARNING+ records always stay at level.
    from investigation_agent_platform.application.worker.activities.common import (
        COMP_WFLW,
        OUTCOME_FAILED,
        OUTCOME_SKIPPED,
    )

with workflow.unsafe.imports_passed_through():
    # G6-measure shadow capture: pure compute-and-log helpers (no I/O,
    # no clock, no execution). Imported for the per-turn shadow replay;
    # the workflow never branches on its output.
    from investigation_agent_platform.application.investigation.shadow_capture import (
        build_shadow_snapshot,
        capture_turn,
        summarize_trace,
        verification_state,
    )

    # D3 Phase 0 shadow definitions: pure receipt pairing + summary
    # (canonical digests only — deterministic, replay-safe). The
    # workflow never branches on the paired output.
    from investigation_agent_platform.domain.proof.definition_shadow import (
        build_correlation_envelope,
        pair_receipt,
        summarize_shadow_definitions,
    )


from .forcing import (
    PostReasonCascadeState,
    _absence_disconfirmation_target,
    _callsite_target,
    _carry_claim_gap_feed,
    _carry_requery_order,
    _caused_by_idempotency_wedge,
    _classify_failure,
    _degraded_empty_result,
    _explore_floor_target,
    _forced_conclude_break,
    _investigation_plan,
    _is_auth_blocked,
    _reason_summary,
    _reread_target,
    _resolve_max_iterations,
    _should_force_conclude_not_checkable,
    _should_force_formulate,
    _should_force_synthesis,
    _should_force_walk_stack,
    apply_post_reason_cascade,
)


@dataclass
class RunInvestigationInput:
    application_id: str
    session_id: str | None = None
    tenant_id: str = ""
    description: str | None = None
    correlation_id: str | None = None
    # The investigation was already created by the API before the workflow was
    # started (`wf-investigation-{investigation_id}`). This field is the
    # single source of truth for the investigation identity across the whole
    # run: API response, workflow ID, activity inputs, checkpoints, evidence,
    # and topology attribution must all refer to this ID. When absent (legacy
    # callers / tests), the workflow falls back to creating a new investigation.
    investigation_id: str | None = None
    # Iteration budget resolved by the API at dispatch from the effective
    # limits (global defaults plus the profile evidence_budget overlay).
    # None (legacy callers) means the local guard default.
    max_iterations: int | None = None


@dataclass
class RunInvestigationResult:
    investigation_id: str = ""
    status: str = ""
    error: str | None = None


@workflow.defn
class RunInvestigationWorkflow:
    def __init__(self) -> None:
        self._is_paused = False
        self._is_cancelled = False
        # Part 11.5: pending/fulfilled input requirement state. Plain JSON-able
        # dicts only, so a future Continue-As-New carries them without custom
        # serialization. The durable record of truth is the input_requirements
        # table; this memory is just the wait gate.
        self._pending_requirement: dict[str, Any] | None = None
        self._fulfilled_input: dict[str, Any] | None = None

    @workflow.signal
    async def pause(self) -> None:
        self._is_paused = True

    @workflow.signal
    async def resume(self) -> None:
        self._is_paused = False

    @workflow.signal
    async def cancel(self) -> None:
        self._is_cancelled = True

    @workflow.update
    async def fulfill_input(
        self, requirement_id: str, version: int, data: dict[str, Any]
    ) -> dict[str, Any]:
        """Accept caller-supplied input for the pending requirement.

        Validation split: the API layer validates auth, tenant, JSON Schema,
        size, and the durable compare-and-set before invoking this Update; the
        validator below re-checks id/version against in-memory workflow state
        (validators cannot do I/O). Identical replays overwrite with identical
        content (no-op); conflicting content for the same version never
        reaches here because the API rejects it with 409 first.
        """
        self._fulfilled_input = {
            "requirement_id": requirement_id,
            "version": version,
            "data": data,
        }
        return {
            "accepted": True,
            "requirement_id": requirement_id,
            "version": version,
        }

    @fulfill_input.validator
    def validate_fulfill_input(
        self, requirement_id: str, version: int, data: dict[str, Any]
    ) -> None:
        pending = self._pending_requirement
        if pending is None:
            raise ApplicationError("no pending input requirement", non_retryable=True)
        if requirement_id != pending.get("requirement_id") or version != pending.get(
            "requirement_version"
        ):
            raise ApplicationError("stale or unknown input requirement", non_retryable=True)

    @workflow.run
    async def run(self, input_data: RunInvestigationInput) -> RunInvestigationResult:
        """Entry point: reconcile-then-report wrapper around the main loop.

        An unhandled exception anywhere in the loop previously failed the
        Temporal execution while the investigation aggregate stayed stranded
        in a non-terminal state (no FAILED marking, no audit record, control
        verbs 502ing against a closed execution). Now the aggregate is
        transitioned to FAILED first, and the execution reports the failure
        instead of dying silently. Cancellation always propagates.
        """
        try:
            return await self._run_inner(input_data)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            workflow.logger.warning(
                "RunInvestigationWorkflow unhandled error; reconciling to FAILED: %s",
                type(exc).__name__,
                extra={
                    "tenant_id": input_data.tenant_id,
                    "investigation_id": input_data.investigation_id,
                    "event": "workflow unhandled error",
                    "comp": COMP_WFLW,
                    "func": "RunInvestigationWorkflow.run",
                    "stage": "execute",
                    "step": "reconcile",
                    "outcome": OUTCOME_FAILED,
                    "error": type(exc).__name__,
                },
            )
            # Surface the proximate activity cause (type + message only, no
            # payloads) so the failure record is diagnosable via the API.
            cause = getattr(exc, "cause", None)
            cause_type = type(cause).__name__ if cause is not None else type(exc).__name__
            cause_msg = str(getattr(cause, "message", "") or "")[:200]
            try:
                await workflow.execute_activity(
                    transition_investigation_activity,
                    TransitionInvestigationInput(
                        tenant_id=input_data.tenant_id,
                        investigation_id=input_data.investigation_id,
                        target_status="FAILED",
                        reason=f"Workflow error reconciled: {cause_type}",
                        failure_code="WORKFLOW_ERROR",
                        failure_reason=f"{cause_type}: {cause_msg}".strip(": "),
                    ),
                    start_to_close_timeout=timedelta(seconds=30),
                )
            except Exception as reconcile_exc:
                workflow.logger.warning(
                    "FAILED reconciliation transition failed: %s",
                    type(reconcile_exc).__name__,
                    extra={
                        "tenant_id": input_data.tenant_id,
                        "investigation_id": input_data.investigation_id,
                        "event": "workflow reconcile failed",
                        "comp": COMP_WFLW,
                        "func": "RunInvestigationWorkflow.run",
                        "stage": "execute",
                        "step": "reconcile",
                        "outcome": OUTCOME_FAILED,
                        "error": type(reconcile_exc).__name__,
                    },
                )
            return RunInvestigationResult(
                investigation_id=input_data.investigation_id,
                status="FAILED",
                error=f"workflow_error:{type(exc).__name__}",
            )

    async def _run_inner(self, input_data: RunInvestigationInput) -> RunInvestigationResult:
        retry_policy = RetryPolicy(
            maximum_attempts=3,
            non_retryable_error_types=[
                "SecurityPolicyViolationException",
                "DomainValidationException",
                "InvalidHypothesisException",
                "PlatformConfigurationError",
            ],
        )

        # 1. Initialize investigation (state authority: persisted via InvestigationRepository).
        # F-IDENTITY: if the API already created the investigation, load it
        # instead of creating a second aggregate under a different ID.
        create_res = await workflow.execute_activity(
            create_investigation_activity,
            CreateInvestigationInput(
                application_id=input_data.application_id,
                session_id=input_data.session_id,
                tenant_id=input_data.tenant_id,
                description=input_data.description,
                investigation_id=input_data.investigation_id,
            ),
            start_to_close_timeout=timedelta(seconds=60),
            retry_policy=retry_policy,
        )

        inv_id = create_res.investigation_id
        tenant_id = input_data.tenant_id

        # P0 create-race fix: a CANCELLED investigation is not startable by
        # construction (duplicate of an active run, served-cached, or
        # declined — the start gate allows CREATED/FAILED/RUNNING only).
        # Report CANCELED with the disposition link instead of driving
        # CREATED→CONTEXTUALIZING transitions that must fail and reconciling
        # to FAILED, which poisoned battery tallies on duplicate dispatches.
        if create_res.status == "CANCELLED":
            _action = getattr(create_res, "disposition_action", None) or "unknown"
            _pid = getattr(create_res, "primary_target_id", None) or "none"
            workflow.logger.info(
                "RunInvestigationWorkflow not startable; reporting CANCELED: %s",
                inv_id,
                extra={
                    "tenant_id": tenant_id,
                    "investigation_id": inv_id,
                    "event": "workflow not startable",
                    "comp": COMP_WFLW,
                    "func": "RunInvestigationWorkflow._run_inner",
                    "stage": "intake",
                    "step": "create",
                    "outcome": "CANCELED",
                    "disposition_action": str(_action),
                    "primary_target_id": str(_pid),
                },
            )
            return RunInvestigationResult(
                investigation_id=inv_id,
                status="CANCELED",
                error=f"not_startable:CANCELLED:disposition={_action}:primary={_pid}",
            )

        async def _transition(target: str, reason: str) -> None:
            await workflow.execute_activity(
                transition_investigation_activity,
                TransitionInvestigationInput(
                    tenant_id=tenant_id,
                    investigation_id=inv_id,
                    target_status=target,
                    reason=reason,
                ),
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=retry_policy,
            )

        # Drive state machine: CREATED -> CONTEXTUALIZING -> INVESTIGATING
        await _transition("CONTEXTUALIZING", "Workflow started contextualizing")
        await _transition("INVESTIGATING", "Workflow entering investigation loop")

        async def _maybe_handle_input_required(
            result_data: dict[str, Any], resume_status: str
        ) -> str:
            """Handle an activity's ``input_required`` marker (Part 11.5).

            Returns "continue", "cancelled", or "expired". A pause does NOT
            block input fulfillment (pause halts work, not data delivery);
            cancellation and expiry do terminate the wait. On expiry the
            investigation transitions FAILED fail-closed: proceeding without
            required input would fabricate conclusions.
            """
            marker = result_data.get("input_required")
            if not marker or not isinstance(marker, dict):
                return "continue"
            record_res = await workflow.execute_activity(
                record_input_requirement_activity,
                RecordInputRequirementInput(
                    tenant_id=tenant_id,
                    investigation_id=inv_id,
                    reason=str(marker.get("reason", "Additional input required")),
                    json_schema=dict(marker.get("json_schema", {}) or {}),
                    classification=str(marker.get("classification", "INTERNAL")),
                    resume_status=resume_status,
                    promote_to_evidence=bool(marker.get("promote_to_evidence", False)),
                    expires_in_seconds=marker.get("expires_in_seconds"),
                ),
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=retry_policy,
            )
            if not record_res.success:
                workflow.logger.warning(
                    "input requirement recording failed: %s",
                    record_res.error,
                    extra={
                        "tenant_id": tenant_id,
                        "investigation_id": inv_id,
                        "event": "workflow input requirement record failed",
                        "comp": COMP_WFLW,
                        "func": "RunInvestigationWorkflow._run_inner",
                        "stage": "execute",
                        "step": "await_input",
                        "outcome": OUTCOME_SKIPPED,
                        "error": str(record_res.error),
                    },
                )
                return "continue"
            req_id = str(record_res.data.get("requirement_id", ""))
            req_version = int(record_res.data.get("requirement_version", 1))
            raw_timeout = record_res.data.get("wait_timeout_seconds")
            wait_timeout = float(raw_timeout) if isinstance(raw_timeout, (int, float)) else None
            await _transition("AWAITING_INPUT", f"Waiting for input: {req_id}")
            self._pending_requirement = {
                "requirement_id": req_id,
                "requirement_version": req_version,
            }
            self._fulfilled_input = None
            try:
                await workflow.wait_condition(
                    lambda: self._fulfilled_input is not None or self._is_cancelled,
                    timeout=wait_timeout,
                )
            except TimeoutError:
                await workflow.execute_activity(
                    set_input_requirement_state_activity,
                    SetInputRequirementStateInput(
                        tenant_id=tenant_id,
                        requirement_id=req_id,
                        expected_version=req_version,
                        state="EXPIRED",
                    ),
                    start_to_close_timeout=timedelta(seconds=30),
                    retry_policy=retry_policy,
                )
                self._pending_requirement = None
                await _transition("FAILED", f"Input requirement {req_id} expired")
                return "expired"
            if self._is_cancelled and self._fulfilled_input is None:
                await workflow.execute_activity(
                    set_input_requirement_state_activity,
                    SetInputRequirementStateInput(
                        tenant_id=tenant_id,
                        requirement_id=req_id,
                        expected_version=req_version,
                        state="CANCELLED",
                    ),
                    start_to_close_timeout=timedelta(seconds=30),
                    retry_policy=retry_policy,
                )
                self._pending_requirement = None
                return "cancelled"
            fulfilled: dict[str, Any] = self._fulfilled_input or {}
            await workflow.execute_activity(
                set_input_requirement_state_activity,
                SetInputRequirementStateInput(
                    tenant_id=tenant_id,
                    requirement_id=req_id,
                    expected_version=req_version,
                    state="FULFILLED",
                ),
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=retry_policy,
            )
            if record_res.data.get("promote_to_evidence"):
                await workflow.execute_activity(
                    promote_fulfillment_evidence_activity,
                    PromoteFulfillmentEvidenceInput(
                        tenant_id=tenant_id,
                        investigation_id=inv_id,
                        requirement_id=req_id,
                        data=dict(fulfilled.get("data", {}) or {}),
                        classification=str(marker.get("classification", "INTERNAL")),
                    ),
                    start_to_close_timeout=timedelta(seconds=60),
                    retry_policy=retry_policy,
                )
            resume = str(record_res.data.get("resume_status", resume_status))
            self._pending_requirement = None
            self._fulfilled_input = None
            await _transition(resume, f"Input {req_id} fulfilled; resuming")
            return "continue"

        # Budget guard: wall-clock + token/cost + max iterations.
        # Deterministic: use workflow.now() would be ideal but workflow.time is not available in all SDKs;
        # we rely on iteration cap + InvestigationBudget signal from reason/execute metadata.
        # The iteration cap resolves from RunInvestigationInput (API-resolved
        # from effective limits: global defaults plus profile overlay), with
        # the local guard as the backstop for legacy callers. Do not tune
        # investigation budgets here; change the domain defaults or the
        # profile instead.
        WORKFLOW_LOCAL_ITERATION_GUARD = 10
        # Legacy callers omit max_iterations: the guard above is the default,
        # so resolve against it explicitly instead of a second literal.
        requested = getattr(input_data, "max_iterations", None)
        max_iterations = _resolve_max_iterations(
            requested if requested is not None else WORKFLOW_LOCAL_ITERATION_GUARD
        )
        # Lazy import for type safety without failing workflow determinism checks.
        # Budget is evaluated via activity results; workflow keeps local iteration guard.
        iteration = 0
        verified = False
        last_error: str | None = None
        # Phase 4 (claim binding): D6 fast-path exit flag. Set when the
        # opinion-only break fires; concluding then requests COMPLETED
        # so the gate routes through _persist_abstention with
        # NOT_CHECKABLE_SKIPPED (D5/D7) instead of the epistemic FAILED
        # path. Plain bool, replay-safe.
        _d6_concluded = False
        # Prior reason decisions (plain JSON scalars). Rebuilt deterministically
        # from activity results on replay; fed back into ReasonInput so the
        # model sees repetition and escalates capability instead of looping
        # the same empty action to the iteration cap.
        action_history: list[dict[str, Any]] = []
        # G6-measure shadow trace (plain JSON scalars, replay-safe): one
        # per-turn (recorded, shadow-selected) record appended by the
        # capture hook. Compute-only; the loop never reads this list.
        shadow_trace: list[dict[str, Any]] = []
        # D3 Phase 0 definition-shadow envelopes (plain JSON scalars,
        # replay-safe): verify-turn observations paired at receipt.
        # Compute-only; the loop never branches on this list. T3
        # (conclude-gate) observations travel co-located in the conclude
        # activity result and are paired analysis-side; they cannot ride
        # this run's loop_telemetry (built before conclude runs).
        definition_shadow_trace: list[dict[str, Any]] = []
        definition_receipt_index = 0
        definition_shadow_duplicates = 0
        # Cumulative LLM spend across iterations (plain scalars, replay-safe).
        # Fed into each checkpoint so token/cost totals survive worker
        # restarts; the reason activity enforces the per-investigation cap.
        llm_tokens_total = 0
        llm_cost_total = 0.0
        # G8 synthesis tracking (plain scalars, replay-safe): committed
        # formulations, banked evidence total, and forced-formulate count.
        formulated_count = 0
        evidence_banked_total = 0
        forced_formulate_count = 0
        forced_reread_count = 0
        forced_log_search_count = 0
        forced_walk_stack_count = 0
        forced_replan_count = 0
        forced_synthesis_count = 0
        forced_floor_count = 0
        forced_callsite_count = 0
        forced_absence_disconfirmation_count = 0
        # Phase 4 (claim binding): smallest-fetch per-kind counters
        # (cap 2 each, hard-coded >= 2 at the call site per precedent).
        forced_smallest_fetch_definition_count = 0
        forced_smallest_fetch_history_count = 0
        forced_smallest_fetch_behavior_count = 0
        forced_smallest_fetch_absence_count = 0
        forced_smallest_fetch_causal_count = 0
        overconfident_turns = 0
        defer_repairs_used = 0
        max_defer_repairs = 2
        # Phase 1 (claim binding): cumulative per-turn binding counts from
        # verify result data (plain scalars, replay-safe). Compute-only;
        # the loop never branches on these totals.
        claim_binding_kept_total = 0
        claim_binding_evaporated_total = 0
        claim_binding_unmapped_total = 0
        claim_binding_version_seen = ""
        # Phase 3 (claim binding): cumulative applied/shadow repair counts.
        claim_binding_repaired_total = 0
        claim_binding_repair_shadow_total = 0
        # D3: triage route computed once at iteration 0 from the complaint
        # + intake seed; guides the reasoner, never decides alone.
        triage_route = "UNKNOWN"
        # P6 2b-i: previous turn's point of focus (plain JSON dict or
        # None). Rebuilt every turn from reason output; replay-safe
        # because it derives from activity results, never wall-clock.
        _prev_focus: dict | None = None

        # 2. Main Investigation Loop — state authority is the workflow + checkpoint repo.
        while iteration < max_iterations and not self._is_cancelled:
            # Handle pause/resume/cancel signals.
            if self._is_paused:
                # TO_DO-013: a pause signal is a lifecycle event, not just an
                # in-memory flag — persist PAUSED through the transition
                # activity so investigation_transitions/timeline show it.
                # Resuming needs no explicit edge back: the next _transition
                # path-finds from PAUSED via the domain-derived graph (e.g.
                # PAUSED -> INVESTIGATING -> CORRELATING). Already-PAUSED is a
                # no-op inside the activity.
                await _transition("PAUSED", "Workflow pause signal handled")
            await workflow.wait_condition(lambda: not self._is_paused)
            if self._is_cancelled:
                break

            # Part 6 Slice 0: validity-gated knowledge retrieval before
            # reasoning. Degrades to unassisted reasoning on store trouble;
            # never fails the investigation. Result captured for reason
            # orientation (B-6: refdoc hits ride into the prompt as
            # orientation notes, never as evidence).
            knowledge_res = await workflow.execute_activity(
                retrieve_knowledge_activity,
                RetrieveKnowledgeInput(
                    tenant_id=tenant_id,
                    investigation_id=inv_id,
                    query=input_data.description,
                ),
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=retry_policy,
            )
            try:
                knowledge_orientation = list(
                    (knowledge_res.data or {}).get("reference_docs", []) or []
                )
            except Exception:
                knowledge_orientation = []
            # D2: intake seed for first-turn stack-walk forcing.
            try:
                _seed_raw = (knowledge_res.data or {}).get("intake_seed", None)
                knowledge_seed = dict(_seed_raw) if isinstance(_seed_raw, dict) else {}
            except Exception:
                knowledge_seed = {}
            # D3: triage once at iteration 0 (seed + complaint are run-fixed).
            if iteration == 0:
                try:
                    triage_route = _classify_failure(input_data.description, knowledge_seed)
                except Exception:
                    triage_route = "UNKNOWN"
                # E1: strategy skeleton recorded once; rides history (the
                # model sees it every turn) and telemetry.
                try:
                    investigation_plan = _investigation_plan(triage_route, knowledge_seed)
                except Exception:
                    investigation_plan = []

            # Reason — produces next action and readiness score.
            reason_res = await workflow.execute_activity(
                reason_activity,
                ReasonInput(
                    tenant_id=tenant_id,
                    investigation_id=inv_id,
                    iteration=iteration,
                    recent_actions=action_history[-10:],
                    knowledge_orientation=knowledge_orientation,
                ),
                start_to_close_timeout=timedelta(seconds=120),
                retry_policy=retry_policy,
            )
            # Budget guard: if reason signals budget exceeded, break.
            if reason_res.data.get("budget_exceeded"):
                last_error = "budget_exceeded"
                break
            # Phase 3.4: reasoning outage is a typed platform failure with
            # its cause — never a default QUERY_STATE that loops toward a
            # causeless abstention. Terminal handling records last_error.
            if reason_res.data.get("reasoning_unavailable"):
                _ru_reason = str(reason_res.data.get("reason", "") or "")[:200]
                last_error = (
                    f"reasoning_unavailable:{_ru_reason}" if _ru_reason else "reasoning_unavailable"
                )
                break
            # G8: synthesis forcing — thinking committed before more gathering.
            _reason_data = reason_res.data or {}
            _defer = _reason_data.get("defer_gathering")
            _is_defer = _reason_data.get("disposition") == "DEFER_GATHERING"
            if _is_defer:
                if not isinstance(_defer, dict):
                    last_error = "defer_gathering_invalid"
                    break
                if defer_repairs_used >= max_defer_repairs:
                    last_error = "defer_repair_budget_exhausted"
                    try:
                        workflow.logger.debug(
                            "story.defer_exhausted iteration=%s used=%s max=%s action=%s",
                            iteration,
                            defer_repairs_used,
                            max_defer_repairs,
                            str(
                                (_defer or {}).get("next_action", "?")
                                if isinstance(_defer, dict)
                                else "?"
                            ),
                        )
                    except Exception:
                        pass
                    break
                _defer_action = str(_defer.get("next_action", "") or "").upper()
                if _defer_action in {
                    "",
                    "FORMULATE_HYPOTHESIS",
                    "VERIFY_HYPOTHESIS",
                    "CONCLUDE",
                }:
                    last_error = "defer_gathering_invalid"
                    break
                _reason_data = dict(_reason_data)
                _reason_data["next_action"] = _defer_action
                _reason_data["action_parameters"] = dict(_defer.get("next_action_parameters") or {})
                _reason_data["hypotheses_evaluated"] = []
                _reason_data["claims"] = []
                _reason_data["defer_repairs_used"] = defer_repairs_used + 1
                defer_repairs_used += 1
                _reason_data["defer_turn"] = True
                try:
                    workflow.logger.debug(
                        "story.defer_accepted iteration=%s repair=%s action=%s",
                        iteration,
                        defer_repairs_used,
                        str(_defer_action or "?"),
                    )
                except Exception:
                    pass
            # Phase 4 (claim binding): opinion-only fast path. First
            # position — every downstream override below is guarded by
            # _d6_fired, so nothing downstream can change the outcome.
            # When every evaluated statement is matched by NOT_CHECKABLE
            # claim dicts, force CONCLUDE with readiness 1.0; the loop
            # exit below skips execute/retrieve/verify, and concluding
            # persists the honest abstention (D5/D7).
            _d6_fired = bool(not _is_defer and _should_force_conclude_not_checkable(_reason_data))
            if _d6_fired:
                _reason_data = dict(_reason_data)
                _reason_data["next_action"] = "CONCLUDE"
                _reason_data["action_parameters"] = {}
                _reason_data["conclusion_readiness"] = 1.0
                _reason_data["forced_not_checkable"] = True
            # G6-measure: freeze the model's pre-override proposal for the
            # shadow owed recomputation below. Trigger helpers read the
            # proposed action; after the cascade _reason_data carries the
            # override, which would mask the debt the cascade itself saw.
            _shadow_proposal = dict(_reason_data)
            _absence_target = None
            if not _is_defer and not _d6_fired:
                _absence_target = _absence_disconfirmation_target(
                    _reason_data,
                    getattr(reason_res, "data", {}).get("evidence", [])
                    if isinstance(getattr(reason_res, "data", {}), dict)
                    else [],
                    action_history,
                    forced_absence_disconfirmation_count,
                )
            if _absence_target is not None:
                forced_absence_disconfirmation_count += 1
                _reason_data = dict(_reason_data)
                _reason_data["next_action"] = _absence_target["next_action"]
                _reason_data["action_parameters"] = _absence_target["action_parameters"]
                _reason_data["forced_absence_disconfirmation"] = True
            _absence_forced = bool(_reason_data.get("forced_absence_disconfirmation"))
            # 2b seam: the post-reason gathering cascade is pure and lives
            # in forcing.apply_post_reason_cascade (unit-tested); the
            # workflow supplies activity results and counters, then writes
            # the outputs back. Pre-cascade defer/D6/absence outcomes above
            # are cascade inputs; explicit-frame walk-stack still wins.
            _cascade_state = apply_post_reason_cascade(
                PostReasonCascadeState(
                    reason_data=_reason_data,
                    action_history=action_history or [],
                    evidence_banked_total=evidence_banked_total,
                    triage_route=triage_route,
                    description=input_data.description,
                    knowledge_seed=knowledge_seed,
                    max_iterations=max_iterations,
                    iteration=iteration,
                    prev_focus=_prev_focus,
                    is_defer=bool(_is_defer),
                    d6_fired=bool(_d6_fired),
                    absence_forced=bool(_absence_forced),
                    counts={
                        "walk_stack": forced_walk_stack_count,
                        "floor": forced_floor_count,
                        "formulate": forced_formulate_count,
                        "synthesis": forced_synthesis_count,
                        "reread": forced_reread_count,
                        "callsite": forced_callsite_count,
                        "log_search": forced_log_search_count,
                        "replan": forced_replan_count,
                        "smallest_fetch_definition": forced_smallest_fetch_definition_count,
                        "smallest_fetch_history": forced_smallest_fetch_history_count,
                        "smallest_fetch_behavior": forced_smallest_fetch_behavior_count,
                        "smallest_fetch_absence": forced_smallest_fetch_absence_count,
                        "smallest_fetch_causal": forced_smallest_fetch_causal_count,
                    },
                    formulated_count=formulated_count,
                    tenant_id=tenant_id,
                    investigation_id=str(inv_id),
                )
            )
            _reason_data = dict(_cascade_state.reason_data or {})
            _prev_focus = _cascade_state.prev_focus
            _consumed_order_ref = _cascade_state.consumed_order_ref
            try:
                _cc = _cascade_state.counts if isinstance(_cascade_state.counts, dict) else {}
                forced_walk_stack_count = int(_cc.get("walk_stack", 0) or 0)
                forced_floor_count = int(_cc.get("floor", 0) or 0)
                forced_formulate_count = int(_cc.get("formulate", 0) or 0)
                forced_synthesis_count = int(_cc.get("synthesis", 0) or 0)
                forced_reread_count = int(_cc.get("reread", 0) or 0)
                forced_callsite_count = int(_cc.get("callsite", 0) or 0)
                forced_log_search_count = int(_cc.get("log_search", 0) or 0)
                forced_replan_count = int(_cc.get("replan", 0) or 0)
                forced_smallest_fetch_definition_count = int(_cc.get("smallest_fetch_definition", 0) or 0)
                forced_smallest_fetch_history_count = int(_cc.get("smallest_fetch_history", 0) or 0)
                forced_smallest_fetch_behavior_count = int(_cc.get("smallest_fetch_behavior", 0) or 0)
                forced_smallest_fetch_absence_count = int(_cc.get("smallest_fetch_absence", 0) or 0)
                forced_smallest_fetch_causal_count = int(_cc.get("smallest_fetch_causal", 0) or 0)
            except (TypeError, ValueError):
                pass
            # Debug story: forcing cascade outcome. Compares the model's
            # pre-override proposal (_shadow_proposal) with the post-cascade
            # dispatch (_reason_data). Deterministic: both derive from
            # reason output + history. Story is joined with activity-level
            # story.reason_out / story.execute_in by iteration.
            try:
                _forced_flags = sorted(
                    k
                    for k in (
                        "forced_absence_disconfirmation",
                        "forced_walk_stack",
                        "forced_floor",
                        "forced_formulate",
                        "forced_reread",
                        "forced_pivot_recollect",
                        "forced_callsite",
                        "forced_log_search",
                        "forced_replan",
                        "forced_not_checkable",
                        "forced_smallest_fetch",
                    )
                    if _reason_data.get(k)
                )
                workflow.logger.debug(
                    "story.forcing_outcome iteration=%s proposal=%s final=%s forced=%s defer=%s",
                    iteration,
                    str((_shadow_proposal or {}).get("next_action", "?")),
                    str((_reason_data or {}).get("next_action", "?")),
                    ",".join(_forced_flags) if _forced_flags else "none",
                    bool(_is_defer),
                )
            except Exception:
                pass
            # G6-measure shadow capture (Option A): compute-only, never acts.
            # Replays the frozen turn snapshot through the shadow policy and
            # appends the (recorded, shadow) pair to shadow_trace. Owed
            # flags re-evaluate the pure trigger helpers against the
            # pre-override proposal with caps bypassed (count=0) — same
            # functions, same proposal the cascade decided on, no drift.
            # Any failure records a marker; the loop proceeds identically
            # either way.
            try:
                _unverified_count, _verified_present = verification_state(
                    _reason_data.get("hypotheses_evaluated")
                )
                _formulate_owed_now = _should_force_formulate(
                    iteration,
                    formulated_count,
                    _reason_data.get("hypotheses_evaluated"),
                    evidence_banked_total,
                    0,
                )
                _shadow_snapshot = build_shadow_snapshot(
                    iteration=iteration,
                    max_iterations=max_iterations,
                    actions_used=len(action_history),
                    action_limit=max_iterations,
                    tokens_used=llm_tokens_total,
                    token_limit=0,
                    evidence_banked_total=evidence_banked_total,
                    formulated_count=formulated_count,
                    unverified_count=_unverified_count,
                    verified_present=_verified_present,
                    reasoning_available=True,
                    stack_walk_owed=_should_force_walk_stack(
                        knowledge_seed, _shadow_proposal, action_history, 0
                    )
                    is not None,
                    below_evidence_floor=_explore_floor_target(
                        _shadow_proposal,
                        evidence_banked_total,
                        triage_route,
                        input_data.description,
                        knowledge_seed,
                        0,
                    )
                    is not None,
                    formulate_owed=_formulate_owed_now,
                    synthesis_owed=_should_force_synthesis(
                        action_history,
                        evidence_banked_total,
                        formulated_count,
                        0,
                        _shadow_proposal,
                    ),
                    reread_path_present=_reread_target(_shadow_proposal, action_history, 0)
                    is not None,
                    callsite_owed=_callsite_target(_shadow_proposal, action_history, 0) is not None,
                    forcing_used={
                        "walk_stack": forced_walk_stack_count,
                        "floor": forced_floor_count,
                        "formulate": forced_formulate_count,
                        "synthesis": forced_formulate_count,
                        "reread": forced_reread_count,
                        "callsite": forced_callsite_count,
                        "smallest_fetch": (
                            forced_smallest_fetch_definition_count
                            + forced_smallest_fetch_history_count
                            + forced_smallest_fetch_behavior_count
                            + forced_smallest_fetch_absence_count
                            + forced_smallest_fetch_causal_count
                        ),
                    },
                    action_history=action_history,
                )
                _shadow_turn = capture_turn(
                    _shadow_snapshot,
                    recorded_action=str((_reason_data or {}).get("next_action", "?") or "?"),
                )
                _shadow_turn["iteration"] = iteration
                # Blind-retry classification inputs (§4.2, frozen
                # definition): cumulative evidence for delta computation
                # across stamped turns, the previous turn's outcome, and
                # the recorded action's parameters (capped) for the
                # changed-input judgment. All loop-local reads.
                _shadow_turn["evidence_banked_total"] = evidence_banked_total
                _prev = action_history[-1] if action_history else {}
                _shadow_turn["prev_outcome"] = (
                    str((_prev or {}).get("outcome_status", "") or "")
                    if isinstance(_prev, dict)
                    else ""
                )
                _raw_params = _reason_data.get("action_parameters", {})
                _shadow_turn["params"] = (
                    {str(k): str(v)[:200] for k, v in _raw_params.items()}
                    if isinstance(_raw_params, dict)
                    else {}
                )
                _shadow_turn["forced"] = sorted(
                    short
                    for key, short in (
                        ("forced_walk_stack", "walk_stack"),
                        ("forced_floor", "floor"),
                        ("forced_formulate", "formulate"),
                        ("forced_reread", "reread"),
                        ("forced_callsite", "callsite"),
                        ("forced_log_search", "log_search"),
                        ("forced_replan", "replan"),
                        ("forced_absence_disconfirmation", "absence_disconfirmation"),
                        # Phase 4 (claim binding): smallest-fetch + opinion
                        # fast path ride the same short-name list.
                        ("forced_smallest_fetch", "smallest_fetch"),
                        ("forced_not_checkable", "not_checkable"),
                    )
                    if _reason_data.get(key)
                )
                # Synthesis shares the loop's formulate counter and flag:
                # a set forced_formulate with formulate_owed False means
                # the synthesis rule fired it (the only other setter).
                if _reason_data.get("forced_formulate") and not _formulate_owed_now:
                    _shadow_turn["forced"].append("synthesis")
                    _shadow_turn["forced"] = sorted(_shadow_turn["forced"])
                _shadow_turn["owed"] = {
                    "walk_stack": _shadow_snapshot.stack_walk_owed,
                    "floor": _shadow_snapshot.below_evidence_floor,
                    "formulate": _shadow_snapshot.formulate_owed,
                    "synthesis": _shadow_snapshot.synthesis_owed,
                    "reread": _shadow_snapshot.reread_path_present,
                    "callsite": _shadow_snapshot.callsite_owed,
                }
                shadow_trace.append(_shadow_turn)
            except Exception:
                shadow_trace.append({"iteration": iteration, "capture_error": True})
            _hyps = _reason_data.get("hypotheses_evaluated") or []
            _obss = _reason_data.get("observations") or []
            action_history.append(
                {
                    "iteration": iteration,
                    "action": str(_reason_data.get("next_action", "?")),
                    "readiness": _reason_data.get("conclusion_readiness"),
                    "hypotheses": len(_hyps) if isinstance(_hyps, list) else 0,
                    "observations": len(_obss) if isinstance(_obss, list) else 0,
                    "disposition": _reason_data.get("disposition"),
                    "defer_repairs_used": _reason_data.get("defer_repairs_used"),
                    "defer_reason": (
                        (_reason_data.get("defer_gathering") or {}).get("reason")
                        if isinstance(_reason_data.get("defer_gathering"), dict)
                        else None
                    ),
                    "forced_absence_disconfirmation": bool(
                        _reason_data.get("forced_absence_disconfirmation", False)
                    ),
                    "triage_route": triage_route,
                    # E1: strategy skeleton (iteration 0 only).
                    "plan": list(investigation_plan) if iteration == 0 else [],
                }
            )
            try:
                llm_tokens_total += int(_reason_data.get("llm_tokens", 0) or 0)
                llm_cost_total += float(_reason_data.get("llm_cost", 0.0) or 0.0)
            except (TypeError, ValueError):
                pass

            # Part 11.5: an activity may declare it needs caller-supplied
            # input before the run can proceed.
            input_state = await _maybe_handle_input_required(reason_res.data, "INVESTIGATING")
            if input_state == "cancelled":
                break
            if input_state == "expired":
                last_error = "input_expired"
                break

            # Execute proposed action (EvidenceGateway dispatch + sanitization + persistence).
            # G8: dispatch honors the synthesis-forced override in _reason_data.
            # Phase 4 (claim binding): opinion-only fast path exit. Skip
            # execute/retrieve/verify for this turn; fall to CONCLUDING,
            # which requests COMPLETED and routes through the gate into
            # _persist_abstention with NOT_CHECKABLE_SKIPPED (D5/D7).
            if _forced_conclude_break(_reason_data):
                _d6_concluded = True
                break
            _dispatched_action = str(_reason_data.get("next_action", "QUERY_STATE"))
            try:
                execute_res = await workflow.execute_activity(
                    execute_action_activity,
                    ExecuteActionInput(
                        tenant_id=tenant_id,
                        investigation_id=inv_id,
                        action=_dispatched_action,
                        parameters=dict(_reason_data.get("action_parameters", {})),
                        iteration=iteration,
                        hypotheses=list(_reason_data.get("hypotheses_evaluated", []) or []),
                        claims=[
                            {**claim, "evidence_ids": list(claim.get("evidence_ids", []) or [])}
                            for claim in (_reason_data.get("claims", []) or [])
                            if isinstance(claim, dict)
                        ],
                        caller_edge_candidates=list(
                            _reason_data.get("caller_edge_candidates", []) or []
                        ),
                        # Phase 4 (claim binding): workflow-only bank
                        # context — identity travels beside params, never
                        # inside them (D4).
                        smallest_fetch_context=(
                            dict(_reason_data.get("smallest_fetch_context") or {})
                            if isinstance(_reason_data.get("smallest_fetch_context"), dict)
                            else None
                        ),
                    ),
                    start_to_close_timeout=timedelta(seconds=60),
                    retry_policy=retry_policy,
                )
            except ActivityError as exc:
                # Tranche B doctrine: an idempotency wedge (slow owner still
                # holding the key past the 3-attempt budget) degrades this
                # turn to EMPTY instead of reconciling the run FAILED. The
                # SDK surfaces activity failures as ActivityError wrapping
                # the ApplicationError, so walk the cause chain for the
                # wedge type; anything else re-raises.
                if not _caused_by_idempotency_wedge(exc):
                    raise
                execute_res = _degraded_empty_result(_dispatched_action, "idempotency_owner_busy")

            input_state = await _maybe_handle_input_required(execute_res.data, "INVESTIGATING")
            if input_state == "cancelled":
                break
            if input_state == "expired":
                last_error = "input_expired"
                break
            # Budget guard (execute side): the ledger routes here instead of
            # raising, so break to CONCLUDING with the gathered corpus.
            if execute_res.data.get("budget_exceeded"):
                last_error = "budget_exceeded"
                break
            # P0-2: close the loop on the pre-execution history entry — record
            # what was actually attempted and what it produced, so the next
            # reasoning turn can distinguish success/empty/degraded/failed.
            if action_history:
                _exec_data = execute_res.data or {}
                _outcome = _exec_data.get("action_outcome") or {}
                _params = dict(reason_res.data.get("action_parameters", {}) or {})
                # G3: same-turn retrieval summary for the verify-time
                # fallback (actions not yet flushed to the timeline table)
                # and the cascade-time budget re-check. All JSON scalars,
                # Temporal replay-safe. The attempt rides inside
                # action_outcome (not top-level result data); only the
                # stable attempt_id is top-level.
                _exec_attempt = _outcome.get("retrieval_attempt")
                if not isinstance(_exec_attempt, dict):
                    _exec_attempt = {}
                try:
                    _exec_truncated = int(_exec_attempt.get("truncated_rows") or 0)
                except (TypeError, ValueError):
                    _exec_truncated = 0
                _exec_provider_total = _exec_attempt.get("provider_total_count")
                try:
                    _exec_provider_total = (
                        int(_exec_provider_total) if _exec_provider_total is not None else None
                    )
                except (TypeError, ValueError):
                    _exec_provider_total = None
                _exec_banked = _exec_attempt.get("banked_count")
                try:
                    _exec_banked = int(_exec_banked) if _exec_banked is not None else None
                except (TypeError, ValueError):
                    _exec_banked = None
                action_history[-1].update(
                    {
                        "parameters": {k: str(v)[:200] for k, v in _params.items()},
                        "outcome_status": _outcome.get("status"),
                        "items_count": _outcome.get("item_count", _exec_data.get("items_count", 0)),
                        "evidence_added": len(_outcome.get("evidence_ids", []) or []),
                        "miss_reason": _outcome.get("miss_reason"),
                        "sources": _outcome.get("sources", []),
                        "forced_formulate": bool(_reason_data.get("forced_formulate", False)),
                        "forced_reread": bool(_reason_data.get("forced_reread", False)),
                        "forced_log_search": bool(_reason_data.get("forced_log_search", False)),
                        "forced_walk_stack": bool(_reason_data.get("forced_walk_stack", False)),
                        "forced_pivot_recollect": bool(
                            _reason_data.get("forced_pivot_recollect", False)
                        ),
                        "forced_replan": bool(_reason_data.get("forced_replan", False)),
                        "forced_floor": bool(_reason_data.get("forced_floor", False)),
                        "forced_callsite": bool(_reason_data.get("forced_callsite", False)),
                        # Phase 4 (claim binding): smallest-fetch stamps for
                        # history dedupe (gap_id-keyed, no param parsing).
                        "forced_smallest_fetch": bool(
                            _reason_data.get("forced_smallest_fetch", False)
                        ),
                        "forced_smallest_fetch_kind": str(
                            _reason_data.get("forced_smallest_fetch_kind", "") or ""
                        ),
                        "forced_smallest_fetch_gap_id": str(
                            _reason_data.get("forced_smallest_fetch_gap_id", "") or ""
                        ),
                        "forced_not_checkable": bool(
                            _reason_data.get("forced_not_checkable", False)
                        ),
                        # Union of both lines' history keys: defer/absence-disconf
                        # flags (L2-D3-fix) plus requery/attempt/retrieval
                        # telemetry (g4-closure).
                        "disposition": _reason_data.get("disposition"),
                        "defer_repairs_used": _reason_data.get("defer_repairs_used"),
                        "forced_absence_disconfirmation": bool(
                            _reason_data.get("forced_absence_disconfirmation", False)
                        ),
                        "forced_requery": False,
                        "attempt_id": _exec_data.get("attempt_id"),
                        "retrieval_attempt": dict(_exec_attempt) if _exec_attempt else None,
                        "evidence_ids": [str(e) for e in (_outcome.get("evidence_ids") or [])][:25],
                        "truncated_rows": _exec_truncated,
                        "provider_total_count": _exec_provider_total,
                        "banked_count": _exec_banked,
                    }
                )
                # G3: stamp the dispatched turn and retire the order in
                # place (exactly once). A failed/degraded dispatch still
                # stamps — the carrier stays consumed, recovery waits for a
                # fresh evaluation, never a tight retry loop.
                if _reason_data.get("forced_requery") and isinstance(_consumed_order_ref, dict):
                    _consumed_order_ref["consumed"] = True
                    action_history[-1]["forced_requery"] = {
                        "claim_ref": str(_consumed_order_ref.get("claim_ref") or ""),
                        "query": str(_consumed_order_ref.get("query") or ""),
                        "scope": str(_consumed_order_ref.get("scope") or ""),
                        "turn": int(iteration),
                        "outcome_status": _outcome.get("status"),
                    }
                # D4 fuzzy orientation rides history (not the prompt): the
                # next reason turn picks up the latest hint as non-citable
                # steering. Only set when present to keep history lean.
                if isinstance(_outcome.get("fuzzy_orientation"), dict):
                    action_history[-1]["fuzzy_orientation"] = _outcome["fuzzy_orientation"]
                # G8: track synthesis state for the forcing rule.
                if _dispatched_action == "FORMULATE_HYPOTHESIS":
                    formulated_count += 1
                try:
                    evidence_banked_total += len(_outcome.get("evidence_ids", []) or [])
                except TypeError:
                    pass

            # Phase 3.1: provider auth block halts the loop before any
            # further retrieval. Fail loudly with the cause (terminal
            # handling records last_error) instead of looping toward a
            # thin answer. Reads execute_res directly: _exec_data only
            # exists when history was non-empty.
            if _is_auth_blocked(execute_res.data or {}):
                last_error = "provider_blocked:auth_failed"
                break

            # Retrieve evidence (EvidenceGateway).
            retrieve_res = await workflow.execute_activity(
                retrieve_evidence_activity,
                RetrieveEvidenceInput(
                    tenant_id=tenant_id,
                    investigation_id=inv_id,
                    application_id=input_data.application_id,
                ),
                start_to_close_timeout=timedelta(seconds=60),
                retry_policy=retry_policy,
            )

            input_state = await _maybe_handle_input_required(retrieve_res.data, "INVESTIGATING")
            if input_state == "cancelled":
                break
            if input_state == "expired":
                last_error = "input_expired"
                break

            if _is_defer or _reason_data.get("forced_absence_disconfirmation"):
                # A defer turn gathers exactly once and returns to reasoning;
                # a disconfirmation read must not verify the stale ABSENCE claim
                # on the same turn.
                iteration += 1
                continue

            # Drive correlation/hypothesis/verification phases via state machine.
            # The reason decision rides along in each description so the
            # timeline read path shows what the reasoner attempted per
            # iteration (action, readiness, hypothesis/observation counts) —
            # previously only bare phase names were recorded and the
            # hypothesize step was a black box to API consumers.
            # C-1: merge the enriched history entry (outcome, forced flags)
            # over the raw decision so the timeline trace shows what was
            # attempted AND what it produced. Pre-execution keys survive
            # (disjoint names); absent enrichment renders the old shape.
            _decision_summary = _reason_summary(
                {**(reason_res.data or {}), **(action_history[-1] if action_history else {})}
            )
            await _transition(
                "CORRELATING", f"Iteration {iteration} correlating ({_decision_summary})"
            )
            await _transition(
                "HYPOTHESIZING", f"Iteration {iteration} hypothesizing ({_decision_summary})"
            )
            await _transition("VERIFYING", f"Iteration {iteration} verifying ({_decision_summary})")

            # Verify root cause (VerificationEngine).
            verify_res = await workflow.execute_activity(
                verify_root_cause_activity,
                VerifyRootCauseInput(
                    tenant_id=tenant_id,
                    investigation_id=inv_id,
                    triage_route=triage_route,
                    recent_actions=action_history[-10:],
                    iteration=iteration,
                ),
                start_to_close_timeout=timedelta(seconds=60),
                retry_policy=retry_policy,
            )
            # G3: carry the verify-issued re-retrieval as a work
            # order (D3 lifecycle step 2, 2b step 5). ONE new history
            # entry per directive — never merged into
            # action_history[-1], which the post-execution update owns.
            # The next turn's cascade dispatches from the carriers
            # (never the truncated recent_actions view).
            _requery_order = _carry_requery_order(
                verify_res.data if isinstance(verify_res.data, dict) else {},
                iteration,
            )
            if _requery_order is not None:
                action_history.append(_requery_order)
            # Phase 4 (claim binding): carry the verify-issued claim-gap
            # feed (D2 carrier). ONE new history entry per feed — never
            # merged into action_history[-1]. The next turn's cascade
            # reads only the newest claim_gap_feed entry.
            _gap_marker = _carry_claim_gap_feed(
                verify_res.data if isinstance(verify_res.data, dict) else {},
                iteration,
            )
            if _gap_marker is not None:
                action_history.append(_gap_marker)
            # D3 Phase 0 shadow receipt pairing (compute-only; the loop
            # never branches on the result). T1 observations ride the
            # additive result key; each is paired with this turn's native
            # outcome into a tamper-evident envelope. Failures append a
            # capture-error envelope, never alter control flow.
            try:
                _verify_data = verify_res.data if isinstance(verify_res.data, dict) else {}
                _shadow_obs = _verify_data.get("shadow_definitions", [])
                if isinstance(_shadow_obs, list) and _shadow_obs:
                    _native_outcome = {
                        "verified": bool(_verify_data.get("verified", False)),
                        "status": str(_verify_data.get("status", "") or ""),
                        "confidence": _verify_data.get("confidence"),
                        "hypothesis_id": str(_verify_data.get("hypothesis_id", "") or ""),
                        "producing_path": str(_verify_data.get("producing_path", "") or ""),
                    }
                    _shadow_identity = _verify_data.get("shadow_activity_identity", {})
                    if not isinstance(_shadow_identity, dict):
                        _shadow_identity = {}
                    _new_envelopes, _new_duplicates = pair_receipt(
                        [o for o in _shadow_obs if isinstance(o, dict)],
                        iteration,
                        definition_receipt_index,
                        _native_outcome,
                        _shadow_identity,
                        protocol_id="v1.9+A1+A2",
                        amendment_id="DEC-2026-10-26-A2",
                    )
                    definition_shadow_trace.extend(_new_envelopes)
                    definition_receipt_index += len(_new_envelopes)
                    definition_shadow_duplicates += int(_new_duplicates or 0)
            except Exception:
                # A pairing failure must never silently drop observations:
                # log loudly and record one capture-error envelope with
                # fully static content, so the loss is counted in
                # capture-health (summary capture_errors) instead of
                # vanishing. Control flow is never altered.
                workflow.logger.exception(
                    "definition shadow receipt pairing failed; recording capture-error envelope",
                    extra={
                        "tenant_id": tenant_id,
                        "investigation_id": inv_id,
                        "event": "workflow shadow pairing failed",
                        "comp": COMP_WFLW,
                        "func": "RunInvestigationWorkflow._run_inner",
                        "stage": "verify",
                        "step": "pair_shadow",
                        "outcome": OUTCOME_SKIPPED,
                    },
                )
                _new_envelopes = [
                    build_correlation_envelope(
                        {
                            "capture_error": True,
                            "reason": "pairing-fault",
                            "input_set": "BOUND_FULL",
                        },
                        iteration,
                        definition_receipt_index,
                        {
                            "verified": False,
                            "status": "pairing-fault",
                            "confidence": None,
                            "hypothesis_id": "",
                            "producing_path": "",
                        },
                        {},
                    )
                ]
                definition_shadow_trace.extend(_new_envelopes)
                definition_receipt_index += len(_new_envelopes)

            # Phase 1 (claim binding): accumulate bounded per-turn counts
            # from the verify result data (cbv2 runs only carry the keys;
            # legacy runs contribute zero, preserving their totals).
            # Plain-scalar accumulation from activity results: replay-safe.
            try:
                _verify_data = verify_res.data if isinstance(verify_res.data, dict) else {}
                claim_binding_kept_total += int(_verify_data.get("claim_binding_kept", 0) or 0)
                claim_binding_evaporated_total += int(
                    _verify_data.get("claim_binding_evaporated", 0) or 0
                )
                claim_binding_unmapped_total += int(
                    _verify_data.get("claim_binding_unmapped", 0) or 0
                )
                claim_binding_repaired_total += int(
                    _verify_data.get("claim_binding_repaired", 0) or 0
                )
                claim_binding_repair_shadow_total += int(
                    _verify_data.get("claim_binding_repair_shadow", 0) or 0
                )
                _seen_version = str(_verify_data.get("claim_binding_version", "") or "")
                if _seen_version:
                    claim_binding_version_seen = _seen_version
            except Exception:
                pass

            input_state = await _maybe_handle_input_required(verify_res.data, "VERIFYING")
            if input_state == "cancelled":
                break
            if input_state == "expired":
                last_error = "input_expired"
                break

            # Checkpoint — persist InvestigationState snapshot via CheckpointRepository.
            await workflow.execute_activity(
                checkpoint_activity,
                CheckpointInput(
                    tenant_id=tenant_id,
                    investigation_id=inv_id,
                    step=iteration,
                    usage_totals={
                        "llm_tokens": llm_tokens_total,
                        "llm_cost": llm_cost_total,
                    },
                ),
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=retry_policy,
            )

            if verify_res.data.get("verified"):
                verified = True
                break

            # Tier-3 critic instrumentation (zero-cost divergence proxy):
            # the model pushed to verify/conclude with high self-reported
            # readiness, but verification refused. Counts the turns a
            # critic would have overturned; a high rate across runs is the
            # build signal for Shape-1 critic. No extra LLM calls.
            try:
                _proposed = str((_reason_data or {}).get("next_action", "") or "").upper()
                _readiness = (reason_res.data or {}).get("conclusion_readiness", 0.0)
                _readiness_value = float(_readiness or 0.0)
            except (TypeError, ValueError):
                _proposed, _readiness_value = "", 0.0
            if _proposed in ("VERIFY_HYPOTHESIS", "CONCLUDE") or _readiness_value >= 0.8:
                overconfident_turns += 1

            # Loop back: VERIFYING -> HYPOTHESIZING or INVESTIGATING for next iteration
            if iteration < max_iterations - 1 and not self._is_cancelled:
                await _transition(
                    "HYPOTHESIZING", f"Iteration {iteration} loop back to hypothesizing"
                )

            iteration += 1

        # Debug story: loop-exit ledger. One line per run closing the story:
        # why the loop stopped + final counters. Joined by investigation_id.
        try:
            workflow.logger.debug(
                "story.loop_exit iterations=%s last_error=%s formulated=%s banked=%s defer=%s/%s forced_callsite=%s forced_reread=%s forced_formulate=%s",
                iteration,
                str(last_error or "none"),
                formulated_count,
                evidence_banked_total,
                defer_repairs_used,
                max_defer_repairs,
                forced_callsite_count,
                forced_reread_count,
                forced_formulate_count,
            )
        except Exception:
            pass
        # 3. Conclude & Publish Events (EventPublisher on completion).
        await _transition("CONCLUDING", "Workflow entering concluding phase")
        if self._is_cancelled:
            final_status = "CANCELLED"
        elif _d6_concluded:
            # Phase 4 (claim binding): the opinion-only fast path broke out
            # before verify, so verified is False by construction — but the
            # run concluded honestly, not failed. Request COMPLETED so the
            # gate abstains with NOT_CHECKABLE_SKIPPED (D5/D7) rather than
            # taking the epistemic FAILED path.
            final_status = "COMPLETED"
        elif verified:
            final_status = "COMPLETED"
        elif last_error in {"budget_exceeded", "defer_repair_budget_exhausted"}:
            final_status = "FAILED"
        else:
            # Not verified: reached max_iterations or readiness threshold without verification -> FAILED with limitations
            final_status = "FAILED"
            if last_error is None:
                last_error = "verification_failed_or_limit_reached"

        await workflow.execute_activity(
            conclude_investigation_activity,
            ConcludeInvestigationInput(
                tenant_id=tenant_id,
                investigation_id=inv_id,
                status=final_status,
                reason=last_error or "",
                # D5: triage route selects the runtime preset at verify +
                # conclude for failure-shaped runs.
                triage_route=triage_route,
                # D3 T3 receipt pairing: continue the run's ordered
                # receipt chain so conclude-gate envelopes pair at the
                # conclude return and ride the stamped telemetry.
                definition_receipt_start=definition_receipt_index,
                conclude_iteration=iteration,
                # C-2: loop counters ride into metadata["loop_telemetry"]
                # at conclude (plain scalars — replay-safe).
                loop_telemetry={
                    "llm_tokens_total": llm_tokens_total,
                    "llm_cost_total": llm_cost_total,
                    "formulated_count": formulated_count,
                    "evidence_banked_total": evidence_banked_total,
                    "forced_formulate_count": forced_formulate_count,
                    "forced_reread_count": forced_reread_count,
                    "forced_log_search_count": forced_log_search_count,
                    "forced_walk_stack_count": forced_walk_stack_count,
                    "forced_replan_count": forced_replan_count,
                    "forced_synthesis_count": forced_synthesis_count,
                    "overconfident_turns": overconfident_turns,
                    "defer_repairs_used": defer_repairs_used,
                    "defer_repairs_limit": max_defer_repairs,
                    # Phase 4 (claim binding): smallest-fetch per-kind fires.
                    "forced_smallest_fetch_definition_count": forced_smallest_fetch_definition_count,
                    "forced_smallest_fetch_history_count": forced_smallest_fetch_history_count,
                    "forced_smallest_fetch_behavior_count": forced_smallest_fetch_behavior_count,
                    "forced_smallest_fetch_absence_count": forced_smallest_fetch_absence_count,
                    "forced_smallest_fetch_causal_count": forced_smallest_fetch_causal_count,
                    "forced_absence_disconfirmation_count": forced_absence_disconfirmation_count,
                    "claim_binding_kept": claim_binding_kept_total,
                    "claim_binding_evaporated": claim_binding_evaporated_total,
                    "claim_binding_unmapped": claim_binding_unmapped_total,
                    "claim_binding_version": claim_binding_version_seen,
                    "claim_binding_repaired": claim_binding_repaired_total,
                    "claim_binding_repair_shadow": claim_binding_repair_shadow_total,
                    "iterations": iteration,
                    "triage_route": triage_route,
                    "plan": list(investigation_plan),
                    # G6-measure: per-turn shadow records (JSON scalars) +
                    # summary; consumed by the operator, never by the loop.
                    "shadow_trace": list(shadow_trace),
                    "shadow_summary": summarize_trace(shadow_trace),
                    # D3 Phase 0: verify-turn shadow-definition envelopes
                    # (paired at receipt) + summary counts. T3 conclude-gate
                    # envelopes pair at the conclude return inside the
                    # conclude activity (it alone holds the gate notes) and
                    # merge into the stamped telemetry there, continuing
                    # the same receipt chain.
                    "definition_shadow_trace": list(definition_shadow_trace),
                    "shadow_definition_summary": summarize_shadow_definitions(
                        [
                            e.get("observation", e)
                            for e in definition_shadow_trace
                            if isinstance(e, dict)
                        ],
                        0,
                        definition_shadow_duplicates,
                        0,
                    ),
                },
            ),
            start_to_close_timeout=timedelta(seconds=60),
            retry_policy=retry_policy,
        )

        # Part 6 Slice 0: distill concluded state into knowledge envelopes.
        # Capture failures never fail the investigation.
        await workflow.execute_activity(
            capture_knowledge_activity,
            CaptureKnowledgeInput(tenant_id=tenant_id, investigation_id=inv_id),
            start_to_close_timeout=timedelta(seconds=60),
            retry_policy=retry_policy,
        )

        await workflow.execute_activity(
            publish_event_activity,
            PublishEventInput(
                tenant_id=tenant_id, investigation_id=inv_id, event="INVESTIGATION_FINISHED"
            ),
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=retry_policy,
        )

        return RunInvestigationResult(
            investigation_id=inv_id, status=final_status, error=last_error
        )
