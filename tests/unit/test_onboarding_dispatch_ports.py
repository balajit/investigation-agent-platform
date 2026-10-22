"""Contract-only tests for the pre-Phase-2 workflow boundary."""

from __future__ import annotations

from uuid import uuid4

from investigation_agent_platform.application.onboarding.dispatch_ports import (
    PinnedWorkflowInput,
    WorkflowStartReceipt,
)


def test_workflow_input_is_pinned_to_request_revision_and_generation() -> None:
    request_id = uuid4()
    workflow_input = PinnedWorkflowInput(
        tenant_id="tenant-a",
        request_id=request_id,
        revision=2,
        execution_generation=3,
        workflow_id="onboard-opaque-id",
        input_reference="request-snapshot-ref",
        input_version="sha256:abc123",
        schema_version="tenant-intake/v1",
    )
    assert workflow_input.request_id == request_id
    assert workflow_input.revision == 2
    assert workflow_input.execution_generation == 3
    assert workflow_input.input_version == "sha256:abc123"


def test_start_receipt_is_execution_evidence_not_a_lifecycle_transition() -> None:
    receipt = WorkflowStartReceipt(
        workflow_id="onboard-opaque-id",
        run_id="run-opaque-id",
        input_reference="request-snapshot-ref",
        input_version="sha256:abc123",
    )
    assert receipt.run_id == "run-opaque-id"
    assert not hasattr(receipt, "state")
    assert not hasattr(receipt, "dispatch_state")
