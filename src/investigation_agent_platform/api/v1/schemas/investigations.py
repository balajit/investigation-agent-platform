# src/investigation_agent_platform/api/v1/schemas/investigations.py
"""Response models for the investigations router (incl. POST /intake/errors).

Non-secret resources use their domain models directly as response models —
identical wire by construction, with no view/domain drift class. Only
hand-built dict shapes get dedicated views here.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from investigation_agent_platform.domain.finding.models import InvestigationConclusion
from investigation_agent_platform.domain.investigation.input_requirements import InputRequirement
from investigation_agent_platform.domain.investigation.models import (
    IncidentSeverity,
    Investigation,
    InvestigationPriority,
    InvestigationRequest,
)


class CreateInvestigationBody(BaseModel):
    """POST /investigations request body (input model; response is CreateInvestigationResponse)."""

    # F-061: edge payload bounds — oversized descriptions/parameter blobs are
    # rejected at the API boundary before they reach services, workflows, or
    # model prompts. Bounds are kept <= the domain InvestigationRequest bounds
    # (application/session/requested_by 128, problem_description 4096) so
    # boundary-valid payloads can never 500 in domain re-validation.
    application_id: str = Field(
        ..., min_length=1, max_length=128, description="Target application profile identifier"
    )
    problem_description: str = Field(
        ...,
        min_length=1,
        max_length=4096,
        description="Description of the observed incident or system anomaly",
    )
    session_id: str | None = Field(
        default=None, max_length=128, description="Optional telemetry session correlation ID"
    )
    priority: InvestigationPriority = Field(
        default=InvestigationPriority.NORMAL,
        description="Investigation priority level (LOW, NORMAL, HIGH, CRITICAL)",
    )
    requested_by: str = Field(
        default="api-user",
        max_length=128,
        description="Requesting principal recorded when auth supplies none",
    )
    parameters: dict[str, Any] = Field(
        default_factory=dict, max_length=50, description="Custom parameters for analysis workflow"
    )

    def to_request(self, requested_by: str) -> InvestigationRequest:
        return InvestigationRequest(
            application_id=self.application_id,
            problem_description=self.problem_description,
            session_id=self.session_id or f"sess_{self.application_id}",
            requested_by=requested_by,
            priority=self.priority,
            parameters=self.parameters,
        )


class ReprocessBody(BaseModel):
    """POST /investigations/{id}/reprocess request body.

    `mode` selects versioning semantics (RESTART_FRESH always versions;
    RESTART_CONTINUE versions terminal originals and copies artifacts).
    `budget_multiplier` scales the run budget (int 1-10; tokens hard-capped
    at 1M per run). Only validated values reach the service — anything else
    422s at this boundary.
    """

    model_config = ConfigDict(extra="forbid")

    mode: Literal["RESTART_FRESH", "RESTART_CONTINUE"] = Field(
        ...,
        description="RESTART_FRESH (new version, no artifacts) or RESTART_CONTINUE (new version + copied artifacts)",
    )
    budget_multiplier: int = Field(
        default=1, ge=1, le=10, description="Budget scale factor N in [1, 10]"
    )
    reason: str = Field(
        default="", max_length=1000, description="Operator reason recorded in the audit trail"
    )


class FulfillInputBody(BaseModel):
    """POST .../input-requirements/{requirement_id}/fulfill request body."""

    requirement_version: int = Field(
        ..., ge=1, description="Optimistic-concurrency version of the pending requirement"
    )
    data: dict[str, Any] = Field(
        ...,
        max_length=200,
        description="Fulfillment payload validated against the requirement JSON Schema",
    )


class InvestigationListItemView(BaseModel):
    """List row: the six keys the handler projects (datetimes as ISO strings)."""

    model_config = ConfigDict(extra="forbid")

    investigation_id: str
    status: str
    phase: str
    application_id: str
    created_at: str | None
    updated_at: str | None


class CreateInvestigationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    session_id: str
    application_id: str
    status: str
    tenant_id: str
    investigation: Investigation
    # §2 disposition of this request against similar past runs (action,
    # target, link, seed, re-verify verdict). Always present; FRESH means
    # no similar past run was found.
    disposition: dict[str, Any] | None = None


class DispatchAck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    investigation_id: str
    status: str
    workflow_id: str
    correlation_id: str | None
    message: str


class InvestigationDetailResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    investigation: Investigation
    pending_requirements: list[InputRequirement]
    # §2.4: inbound + outbound investigation-family links (DUPLICATE_OF,
    # FOLLOW_UP_OF, RETRY_OF, POSSIBLY_RELATED, RELATES_TO). Empty when the
    # link writer is unwired or the investigation stands alone.
    links: list[dict[str, Any]] = Field(default_factory=list)


class ConclusionResponse(BaseModel):
    """`conclusion` is explicitly null while unconcluded — preserved as null.

    `failure` carries the terminal failure record for FAILED investigations
    (code, workflow reason, timestamp) so a bare FAILED is diagnosable
    without worker-log access; null otherwise.
    """

    model_config = ConfigDict(extra="forbid")

    investigation_id: str
    status: str
    conclusion: InvestigationConclusion | None
    failure: dict[str, Any] | None = None


class FulfillAck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    investigation_id: str
    requirement_id: str
    status: str
    update: dict[str, Any]


class StackFrame(BaseModel):
    """One parsed stack frame (D1). Generic shape for Python tracebacks,
    Java stack traces, and other frame-listing runtimes."""

    model_config = ConfigDict(extra="forbid")

    file: str | None = Field(default=None, max_length=1024, description="Source file for the frame")
    line: int | None = Field(default=None, ge=1, description="1-indexed line number")
    function: str | None = Field(
        default=None, max_length=1024, description="Function/method for the frame"
    )


class ErrorIntakeBody(BaseModel):
    """POST /intake/errors request body. Tenant NEVER comes from the body — it is
    derived exclusively from verified authentication (D8 merge safety)."""

    application_id: str = Field(
        ..., min_length=1, max_length=128, description="Target application profile identifier"
    )
    error_class: str = Field(
        ...,
        min_length=1,
        max_length=1024,
        description="Exception class name of the forwarded error",
    )
    repository: str = Field(
        default="", max_length=512, description="Source repository of the failing code"
    )
    revision: str = Field(
        default="", max_length=128, description="Source revision of the failing code"
    )
    failing_symbol: str = Field(default="", max_length=1024, description="Failing code symbol")
    caller_symbol: str | None = Field(
        default=None, max_length=1024, description="Calling code symbol when known"
    )
    top_frame_file: str | None = Field(
        default=None, max_length=1024, description="Top stack-frame file when known"
    )
    problem_description: str = Field(
        default="", max_length=8000, description="Human description of the observed failure"
    )
    session_id: str | None = Field(
        default=None, max_length=256, description="Optional telemetry session correlation ID"
    )
    severity: IncidentSeverity = Field(
        default=IncidentSeverity.ERROR, description="Error severity label"
    )
    log_refs: list[str] = Field(
        default_factory=list, max_length=50, description="Log references attached to the error"
    )
    trace_refs: list[str] = Field(
        default_factory=list, max_length=50, description="Trace references attached to the error"
    )
    frames: list[StackFrame] = Field(
        default_factory=list, max_length=20, description="Parsed stack frames, outermost first"
    )
    expected: dict[str, Any] = Field(
        default_factory=dict, description="Golden expected output (silent-failure comparison)"
    )
    actual: dict[str, Any] = Field(
        default_factory=dict, description="Observed actual output (silent-failure comparison)"
    )


class CancelAck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    investigation_id: str
    status: str
    reason: str


class ErrorIntakeAck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    investigation_id: str
    status: str
    workflow_id: str
    correlation_id: str | None
    message: str
    session_number: int
    merged: bool
    code_issue_fingerprint: str
