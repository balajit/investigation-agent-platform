# src/investigation_agent_platform/application/investigation/guardrails.py
import hashlib
import json
import logging
from datetime import UTC, datetime
from enum import Enum
from typing import Protocol
from uuid import UUID, uuid4

from opentelemetry import trace
from pydantic import BaseModel, ConfigDict, Field

from investigation_agent_platform.domain.common.exceptions import SecurityPolicyViolationException
from investigation_agent_platform.domain.entity.models import InvestigationEntity
from investigation_agent_platform.domain.investigation.models import (
    CorrelationVector,
    EvidenceManifest,
    InvestigationAction,
)

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)


class ActionExecutionStatus(str, Enum):
    SUCCESS = "SUCCESS"
    PARTIAL = "PARTIAL"
    NO_RESULTS = "NO_RESULTS"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    CANCELLED = "CANCELLED"


class InvestigationBudget(BaseModel):
    """Consolidated immutable budget policy defining limits across all dimension resources."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_tool_calls: int = Field(default=50, ge=1)
    max_reasoning_calls: int = Field(default=20, ge=1)
    max_duration_seconds: float = Field(default=1800.0, ge=10.0)
    max_tokens: int = Field(default=1000000, ge=1000)
    max_cost_usd: float = Field(default=25.0, ge=0.1)


class ActionExecutionContext(BaseModel):
    investigation_id: UUID
    execution_id: UUID = Field(default_factory=uuid4)
    application_id: str
    current_time: datetime = Field(default_factory=lambda: datetime.now(UTC))
    remaining_budget_tool_calls: int
    authorization_tenant_id: str
    capability_permissions: list[str] = Field(default_factory=list)


class ActionExecutionResult(BaseModel):
    execution_id: UUID
    status: ActionExecutionStatus
    evidence: list[EvidenceManifest] = Field(default_factory=list)
    entities: list[InvestigationEntity] = Field(default_factory=list)
    relationships: list[CorrelationVector] = Field(default_factory=list)
    facts_discovered: list[str] = Field(default_factory=list)
    error_message: str | None = None
    execution_duration_ms: float = Field(..., ge=0.0)


class ActionValidationResult(BaseModel):
    is_valid: bool
    rejection_reason: str | None = None
    fingerprint: str


class ActionValidator(Protocol):
    def validate_action(
        self, action: InvestigationAction, context: ActionExecutionContext
    ) -> ActionValidationResult: ...


class InvestigationLoopGuard(BaseModel):
    max_repeated_actions: int = Field(default=2, ge=1)
    max_no_info_cycles: int = Field(default=3, ge=1)
    action_fingerprint_counts: dict[str, int] = Field(default_factory=dict)
    consecutive_no_info_cycles: int = Field(default=0, ge=0)

    @staticmethod
    def compute_fingerprint(action: InvestigationAction, investigation_id: UUID) -> str:
        """Computes deterministic SHA-256 hash matching InvestigationAction.execution_hash."""
        canonical_params = json.dumps(action.parameters, sort_keys=True)
        payload = f"{action.action_type.value}:{canonical_params}:{investigation_id}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def evaluate_action(
        self, fingerprint: str, force_refresh: bool = False, is_authorized_refresh: bool = False
    ) -> bool:
        with tracer.start_as_current_span("InvestigationLoopGuard.evaluate_action"):
            count = self.action_fingerprint_counts.get(fingerprint, 0)
            if count >= self.max_repeated_actions:
                if force_refresh and is_authorized_refresh:
                    logger.info(
                        "Force refresh bypass authorized for loop guard",
                        extra={"fingerprint": fingerprint},
                    )
                    return True
                logger.warning(
                    "Action rejected by loop guard: duplicate threshold reached",
                    extra={
                        "fingerprint": fingerprint,
                        "count": count,
                        "limit": self.max_repeated_actions,
                    },
                )
                return False
            return True

    def record_execution_outcome(self, fingerprint: str, new_info_discovered: bool) -> None:
        self.action_fingerprint_counts[fingerprint] = (
            self.action_fingerprint_counts.get(fingerprint, 0) + 1
        )
        if new_info_discovered:
            self.consecutive_no_info_cycles = 0
        else:
            self.consecutive_no_info_cycles += 1

    def is_in_no_info_loop(self) -> bool:
        return self.consecutive_no_info_cycles >= self.max_no_info_cycles

    def check_budget_exceeded(
        self,
        budget: InvestigationBudget,
        consumed_tool_calls: int,
        consumed_reasoning_calls: int,
        consumed_tokens: int,
        consumed_cost_usd: float,
        elapsed_seconds: float,
    ) -> None:
        if consumed_tool_calls >= budget.max_tool_calls:
            raise SecurityPolicyViolationException("Tool call budget exhausted")
        if consumed_reasoning_calls >= budget.max_reasoning_calls:
            raise SecurityPolicyViolationException("Reasoning call budget exhausted")
        if consumed_tokens >= budget.max_tokens:
            raise SecurityPolicyViolationException("Token budget exhausted")
        if consumed_cost_usd >= budget.max_cost_usd:
            raise SecurityPolicyViolationException("Dollar budget exhausted")
        if elapsed_seconds >= budget.max_duration_seconds:
            raise SecurityPolicyViolationException("Duration budget exhausted")

    def enforce_source_roots(self, file_path: str, allowed_roots: list[str]) -> None:
        """Strict sourceRoots allow-list enforcement."""
        from urllib.parse import unquote as _unquote

        decoded = _unquote(file_path).replace("\\", "/")
        if ".." in decoded or "\x00" in file_path or file_path.startswith("~"):
            raise SecurityPolicyViolationException("Path traversal detected")
        if not allowed_roots:
            return
        normalized = f"/{file_path.lstrip('/')}"
        for root in allowed_roots:
            root_norm = f"/{root.strip('/')}".rstrip("/") or "/"
            if normalized == root_norm or normalized.startswith(f"{root_norm}/"):
                return
        raise SecurityPolicyViolationException("File path outside allowed source roots")

    def enforce_query_template_allowlist(
        self, template_id: str, allowed_templates: dict[str, str]
    ) -> None:
        """Strict queryTemplates allow-list enforcement."""
        if template_id not in allowed_templates:
            raise SecurityPolicyViolationException(
                f"Query template '{template_id}' not in allow-list"
            )
