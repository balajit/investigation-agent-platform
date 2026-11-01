"""D3: drive the real RunInvestigationWorkflow against stub activities."""

from __future__ import annotations

import uuid
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
        description="Where is SampleOrderFilter implemented?",
        investigation_id=inv,
        **input_overrides,
    )
    return (
        await env.client.start_workflow(
            RunInvestigationWorkflow.run, payload, id=f"wf-{inv}", task_queue=queue
        ),
        worker,
    )
