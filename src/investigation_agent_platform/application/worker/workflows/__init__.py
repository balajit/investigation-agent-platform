"""Backward-compatible re-export shim for the worker workflows split.

Pure move: every name previously defined in ``worker/workflows.py`` now
lives in a submodule; this package re-exports the full namespace so all
existing import sites keep working unchanged.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

# ruff: noqa: F401  # re-export shim
from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError
from temporalio.workflow import ParentClosePolicy

from investigation_agent_platform.application.evidence.retrieval_ledger import (
    RETRYABLE_ACTIONS,
    WP_A3_DEFAULT_MAX_ATTEMPTS,
    _normalize_scope,
    directive_consumed,
    recorded_attempt_summaries,
)
from investigation_agent_platform.application.investigation.shadow_capture import (
    build_shadow_snapshot,
    capture_turn,
    summarize_trace,
    verification_state,
)
from investigation_agent_platform.application.worker.activities import (
    CancelBatchChildrenInput,
    CaptureKnowledgeInput,
    CheckpointInput,
    CollectSnapshotsInput,
    CollectSnapshotsOutput,
    ConcludeInvestigationInput,
    CreateInvestigationInput,
    ExecuteActionInput,
    LoadBatchRecordsInput,
    MarkBatchRecordResultInput,
    MarkBatchRecordsInput,
    ProjectTopologyInput,
    PromoteFulfillmentEvidenceInput,
    PublishEventInput,
    ReasonInput,
    RecordInputRequirementInput,
    ResolveBatchStragglersInput,
    RetrieveEvidenceInput,
    RetrieveKnowledgeInput,
    RunClusteringInput,
    RunReindexInput,
    RunReportInput,
    SetInputRequirementStateInput,
    SummarizeBatchInput,
    SweepKnowledgeInput,
    TransitionInvestigationInput,
    UpdateBatchJobInput,
    UpdateClusteringJobInput,
    UpdateReindexJobInput,
    UpdateReportJobInput,
    VerifyRootCauseInput,
    cancel_batch_children_activity,
    capture_knowledge_activity,
    checkpoint_activity,
    collect_snapshots_activity,
    conclude_investigation_activity,
    create_investigation_activity,
    execute_action_activity,
    load_batch_records_activity,
    mark_batch_record_result_activity,
    mark_batch_records_activity,
    project_topology_activity,
    promote_fulfillment_evidence_activity,
    publish_event_activity,
    reason_activity,
    record_input_requirement_activity,
    resolve_batch_stragglers_activity,
    retrieve_evidence_activity,
    retrieve_knowledge_activity,
    run_clustering_activity,
    run_reindex_activity,
    run_report_activity,
    set_input_requirement_state_activity,
    summarize_batch_activity,
    sweep_knowledge_activity,
    transition_investigation_activity,
    update_batch_job_activity,
    update_clustering_job_activity,
    update_reindex_job_activity,
    update_report_job_activity,
    verify_root_cause_activity,
)
from investigation_agent_platform.domain.common.background_job import BackgroundJobStatus
from investigation_agent_platform.domain.intake.batch import MAX_ACTIVE_CHILDREN, child_workflow_id
from investigation_agent_platform.domain.proof.definition_shadow import (
    build_correlation_envelope,
    pair_receipt,
    summarize_shadow_definitions,
)

from .analytics import (
    AggregateReportInput,
    AggregateReportResult,
    AggregateReportWorkflow,
    FindingClusteringInput,
    FindingClusteringResult,
    FindingClusteringWorkflow,
)
from .bulk_intake import (
    BulkIntakeInput,
    BulkIntakeResult,
    BulkIntakeWorkflow,
    should_continue_as_new,
)
from .forcing import (
    _FLOOR_MIN_EVIDENCE,
    _FLOOR_TERMINAL_ACTIONS,
    _LOG_FORCE_ERROR_PATTERNS,
    _LOG_FORCE_FROM_ACTIONS,
    _LOG_FORCE_STOPWORDS,
    _REPLAN_ALTERNATIVES,
    _REPLAN_STOPWORDS,
    _REREAD_DEFINITION_PATTERNS,
    _REREAD_GET_OPERATIONS,
    _REREAD_PATH_TOKEN_RE,
    _ROUTE_PLANS,
    _SYNTHESIS_MIN_ROWS,
    _TRIAGE_CONFIG_PATTERNS,
    _TRIAGE_DATA_PATTERNS,
    _TRIAGE_TRANSIENT_PATTERNS,
    _WALK_STACK_CAP_ACTIONS,
    _absence_disconfirmation_target,
    _callsite_target,
    _carry_ledger_directive,
    _caused_by_idempotency_wedge,
    _classify_failure,
    _degraded_empty_result,
    _description_keywords,
    _explore_floor_target,
    _investigation_plan,
    _is_auth_blocked,
    _reason_summary,
    _reread_target,
    _resolve_max_iterations,
    _should_force_formulate,
    _should_force_ledger_requery,
    _should_force_log_search,
    _should_force_replan,
    _should_force_synthesis,
    _should_force_walk_stack,
    _source_value_target,
)
from .indexing import ReferenceDocsIndexInput, ReferenceDocsIndexResult, ReferenceDocsIndexWorkflow
from .maintenance import (
    KnowledgeArtifactJanitorInput,
    KnowledgeArtifactJanitorResult,
    KnowledgeArtifactJanitorWorkflow,
    TopologyProjectionInput,
    TopologyProjectionResult,
    TopologyProjectionWorkflow,
    TopologySnapshotRetentionInput,
    TopologySnapshotRetentionResult,
    TopologySnapshotRetentionWorkflow,
)
from .run_investigation import (
    RunInvestigationInput,
    RunInvestigationResult,
    RunInvestigationWorkflow,
)
