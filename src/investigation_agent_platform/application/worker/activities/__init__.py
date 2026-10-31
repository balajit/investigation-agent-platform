"""Backward-compatible re-export shim for the worker activities split.

Pure move: every name previously defined in ``worker/activities.py`` now
lives in a submodule; this package re-exports the full namespace so all
existing import sites keep working unchanged.
"""

from __future__ import annotations

# ruff: noqa: F401  # re-export shim
import asyncio
import hashlib
import json
import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from temporalio import activity

from investigation_agent_platform.domain.common.exceptions import PlatformConfigurationError
from investigation_agent_platform.domain.investigation.models import ActionType
from investigation_agent_platform.domain.proof.definition_dispatch import (
    load_reviewed_definition_table,
)
from investigation_agent_platform.domain.proof.definition_shadow import (
    ShadowError,
    attach_shadow_observations,
    pair_receipt,
    resolve_definition_mode,
    run_shadow_turn,
    summarize_shadow_definitions,
    telemetry_omission_flagged,
)
from investigation_agent_platform.infrastructure.observability.tracing import traced

try:
    from temporalio.exceptions import ApplicationFailure  # type: ignore[attr-defined]
except ImportError:
    from temporalio.exceptions import (
        ApplicationError as ApplicationFailure,  # type: ignore[assignment, no-redef]
    )

from .analytics import (
    RunReindexInput,
    RunReportInput,
    UpdateReindexJobInput,
    UpdateReportJobInput,
    _link_report_result,
    run_clustering_activity,
    run_reindex_activity,
    run_report_activity,
    update_clustering_job_activity,
    update_reindex_job_activity,
    update_report_job_activity,
)
from .batch import (
    CancelBatchChildrenInput,
    LoadBatchRecordsInput,
    MarkBatchRecordResultInput,
    MarkBatchRecordsInput,
    ResolveBatchStragglersInput,
    SummarizeBatchInput,
    UpdateBatchJobInput,
    _batch_uuid,
    cancel_batch_children_activity,
    load_batch_records_activity,
    mark_batch_record_result_activity,
    mark_batch_records_activity,
    resolve_batch_stragglers_activity,
    summarize_batch_activity,
    update_batch_job_activity,
)
from .common import (
    _ACTION_REQUIRED_EVIDENCE_TYPE_LOCAL,
    _BUDGET_FIELDS,
    _DOMAIN_TRANSITION_GRAPH,
    _EFFECTIVE_LIMIT_FIELDS,
    _KNOWN_LIMITATION_CODES,
    _LOG_BYPASS_TYPES,
    _abandon_reservation,
    _activity_extra,
    _activity_outcome,
    _application_failure_from_exc,
    _coerce_root_ids,
    _coerced_data,
    _complete_reservation,
    _decision_llm_usage,
    _domain_transition_graph,
    _emit_telemetry,
    _emit_verify_telemetry,
    _evidence_type_name,
    _hub_orientation_for,
    _idempotency_store_for,
    _latest_fuzzy_orientation,
    _limitation_kind,
    _log_extra,
    _normalize_actor,
    _persist_log_exemplars,
    _request_hash,
    _reserve_or_replay,
    _sanitize,
    _split_log_bypass,
    _story_debug,
    _story_truncate,
    apply_effective_limits,
    classify_provider_failure,
    classify_reasoning_failure,
    overlay_evidence_budget,
)
from .execution import (
    _absence_proof_row,
    _banked_positive,
    _caller_chain_targets,
    _code_absence_scope,
    _frame_file_matches,
    _fuse_journey_findings,
    _shape_search_keywords,
    _stack_fingerprint,
    _symbol_evidence_packets,
    execute_action_activity,
    fallback_call_graph_to_search,
)
from .fulfillment import (
    PromoteFulfillmentEvidenceInput,
    RecordInputRequirementInput,
    RunClusteringInput,
    SetInputRequirementStateInput,
    UpdateClusteringJobInput,
    promote_fulfillment_evidence_activity,
    record_input_requirement_activity,
    set_input_requirement_state_activity,
)
from .inputs import (
    CaptureKnowledgeInput,
    CheckpointInput,
    ConcludeInvestigationInput,
    CreateInvestigationInput,
    CreateInvestigationOutput,
    ExecuteActionInput,
    GenericActivityResult,
    PublishEventInput,
    ReasonInput,
    RetrieveEvidenceInput,
    RetrieveKnowledgeInput,
    TransitionInvestigationInput,
    VerifyRootCauseInput,
    _caller_edge_candidates_from_evidence,
    _count_evidence_banked,
    _count_hypothesis_lifecycle,
    _derive_loop_telemetry,
    _failure_code_for,
    _get_ctx,
    _select_verification_policy,
    _stamp_failure,
    _stamp_loop_telemetry,
)
from .knowledge import (
    _REFDOC_QUERY_STOPWORDS,
    SweepKnowledgeInput,
    _application_id_for,
    _caller_fingerprints_for,
    _intake_seed_for,
    _recall_reference_docs,
    _shape_refdoc_query,
    _shape_reference_hits,
    capture_knowledge_activity,
    retrieve_knowledge_activity,
    sweep_knowledge_activity,
)
from .lifecycle import (
    _find_transition_path,
    _gate_conclusion_or_fail,
    _transition_via_path,
    checkpoint_activity,
    conclude_investigation_activity,
    publish_event_activity,
    transition_investigation_activity,
)
from .reasoning import (
    create_investigation_activity,
    reason_activity,
    retrieve_evidence_activity,
    select_reasoning_evidence,
)
from .topology import (
    CollectSnapshotsInput,
    CollectSnapshotsOutput,
    ProjectTopologyInput,
    collect_snapshots_activity,
    project_topology_activity,
)
from .verification import (
    _DEFINITION_DISPATCH_REGISTRY,
    _DEFINITION_DISPATCH_TABLE,
    _load_definition_dispatch_table,
    reload_definition_dispatch_table,
    verify_root_cause_activity,
)

logger = logging.getLogger(__name__)
