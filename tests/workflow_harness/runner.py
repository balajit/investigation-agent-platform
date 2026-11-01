"""D3: drive the real RunInvestigationWorkflow against stub activities."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

from stubs import Script, build_stubs
from temporalio.client import WorkflowHandle
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from investigation_agent_platform.application.worker.workflows.run_investigation import (
    RunInvestigationInput,
    RunInvestigationWorkflow,
)


async def start(
    env: WorkflowEnvironment,
    script: Script,
    *,
    on_activity=None,
    **input_overrides: Any,
) -> tuple[WorkflowHandle, Worker]:
    queue = f"harness-{uuid.uuid4().hex[:8]}"
    worker = Worker(
        env.client,
        task_queue=queue,
        workflows=[RunInvestigationWorkflow],
        activities=build_stubs(script, on_activity=on_activity),
    )
    inv = str(uuid.uuid4())
    payload = RunInvestigationInput(
        application_id="app",
        tenant_id="t",
        # Code-shaped so the floor force can fire on blocked D6 turns
        # (dotted path + code keyword); tests may override.
        description="Where is SampleOrderFilter implemented? "
        "service/src/main/java/com/sample/orders/filter/SampleOrderFilter.java",
        investigation_id=inv,
        **input_overrides,
    )
    return (
        await env.client.start_workflow(
            RunInvestigationWorkflow.run,
            payload,
            id=f"wf-{inv}",
            task_queue=queue,
            # Mirror production dispatch: explicit outer bounds so no
            # server default can kill a run mid-test. Workflow tasks never
            # time out: activity calls carry their own start_to_close, and
            # schedule_to_close bounds are applied per-activity by T5-D10.
            # Tests may override (T5.11 disables the run timeout so the old
            # unbounded pause genuinely hangs until terminate).
            execution_timeout=input_overrides.pop(
                "execution_timeout", timedelta(seconds=7200)
            ),
            run_timeout=input_overrides.pop("run_timeout", timedelta(seconds=7100)),
            task_timeout=input_overrides.pop("task_timeout", timedelta(seconds=600)),
        ),
        worker,
    )
