"""Replay-safe Temporal workflows for S7 continuation attempts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from investigation_agent_platform.application.worker.activities.continuation import (
        continuation_reasoning_activity,
        execute_continuation_activity,
    )
    from investigation_agent_platform.application.worker.activities.inputs import (
        ContinuationReasoningInput,
        ExecuteContinuationInput,
    )
from temporalio.workflow import ParentClosePolicy


@dataclass
class ContinuationWorkflowInput:
    tenant_id: str
    continuation_id: str
    principal_id: str
    correlation_id: str | None = None


@dataclass
class ContinuationReasoningWorkflowInput:
    tenant_id: str
    investigation_id: str
    continuation_id: str
    principal_id: str
    correlation_id: str | None = None
    loop_version: str = ""


@workflow.defn
class ContinuationWorkflow:
    @workflow.run
    async def run(self, input_data: ContinuationWorkflowInput) -> dict[str, object]:
        result = await workflow.execute_activity(
            execute_continuation_activity,
            ExecuteContinuationInput(
                tenant_id=input_data.tenant_id,
                continuation_id=input_data.continuation_id,
                principal_id=input_data.principal_id,
                correlation_id=input_data.correlation_id,
            ),
            start_to_close_timeout=timedelta(seconds=120),
            retry_policy=RetryPolicy(maximum_attempts=1),
        )
        if result.success and (result.data or {}).get("reasoning_lease_acquired"):
            await workflow.execute_child_workflow(
                ContinuationReasoningWorkflow.run,
                ContinuationReasoningWorkflowInput(
                    tenant_id=input_data.tenant_id,
                    investigation_id=str((result.data or {}).get("investigation_id", "")),
                    continuation_id=input_data.continuation_id,
                    principal_id=input_data.principal_id,
                    correlation_id=input_data.correlation_id,
                ),
                id=(
                    f"wf-continuation-reasoning-{input_data.continuation_id}-a"
                    f"{int((result.data or {}).get('attempt_count', 0) or 0)}"
                ),
                parent_close_policy=ParentClosePolicy.ABANDON,
            )
        return {"success": result.success, **(result.data or {})}


@workflow.defn
class ContinuationReasoningWorkflow:
    @workflow.run
    async def run(self, input_data: ContinuationReasoningWorkflowInput) -> dict[str, object]:
        result = await workflow.execute_activity(
            continuation_reasoning_activity,
            ContinuationReasoningInput(
                tenant_id=input_data.tenant_id,
                investigation_id=input_data.investigation_id,
                continuation_id=input_data.continuation_id,
                principal_id=input_data.principal_id,
                correlation_id=input_data.correlation_id,
                loop_version=input_data.loop_version,
            ),
            start_to_close_timeout=timedelta(seconds=360),
            retry_policy=RetryPolicy(maximum_attempts=1),
        )
        return {"success": result.success, **(result.data or {})}
