# src/investigation_agent_platform/application/investigation/planner.py
import logging
from datetime import UTC, datetime
from enum import Enum
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from investigation_agent_platform.domain.common.exceptions import ExecutionError
from investigation_agent_platform.domain.evidence.models import EvidenceType
from investigation_agent_platform.domain.investigation.models import InvestigationObjective

logger = logging.getLogger(__name__)


class StepStatus(str, Enum):
    PENDING = "PENDING"
    ACTIVE = "ACTIVE"
    COMPLETED = "COMPLETED"
    SKIPPED = "SKIPPED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"


class InvestigationStepType(str, Enum):
    ESTABLISH_CONTEXT = "ESTABLISH_CONTEXT"
    CORRELATE_IDENTIFIERS = "CORRELATE_IDENTIFIERS"
    SEARCH_RUNTIME = "SEARCH_RUNTIME"
    INSPECT_APPLICATION_STATE = "INSPECT_APPLICATION_STATE"
    INSPECT_SOURCE = "INSPECT_SOURCE"
    INSPECT_HISTORY = "INSPECT_HISTORY"
    FORM_HYPOTHESIS = "FORM_HYPOTHESIS"
    TEST_HYPOTHESIS = "TEST_HYPOTHESIS"
    CHECK_CONTRADICTIONS = "CHECK_CONTRADICTIONS"
    VERIFY_ROOT_CAUSE = "VERIFY_ROOT_CAUSE"
    CONCLUDE = "CONCLUDE"


class InvestigationStep(BaseModel):
    step_id: UUID = Field(default_factory=uuid4)
    step_type: InvestigationStepType
    objective: str
    status: StepStatus = Field(default=StepStatus.PENDING)
    required_evidence_types: list[EvidenceType] = Field(default_factory=list)
    related_hypothesis_ids: list[UUID] = Field(default_factory=list)
    dependencies: list[UUID] = Field(default_factory=list)


class InvestigationPlan(BaseModel):
    plan_id: UUID = Field(default_factory=uuid4)
    objective: InvestigationObjective
    steps: list[InvestigationStep] = Field(default_factory=list)
    active_step_id: UUID | None = None
    revision_count: int = Field(default=0, ge=0)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    revision_reason: str | None = None


class InvestigationPlanner:
    """Produces bounded execution plans, validates dependency DAGs, and performs adaptive replanning."""

    MAX_STEPS = 25
    MAX_REVISIONS = 5
    MAX_GAPS_PER_REPLAN = 8

    @staticmethod
    def validate_dag(plan: InvestigationPlan) -> bool:
        """Validates that step dependencies form a valid Acyclic Directed Graph (DAG)."""
        step_map: dict[UUID, InvestigationStep] = {s.step_id: s for s in plan.steps}

        visited: set[UUID] = set()
        rec_stack: set[UUID] = set()

        def is_cyclic(step_id: UUID) -> bool:
            visited.add(step_id)
            rec_stack.add(step_id)
            step = step_map.get(step_id)
            if step:
                for dep in step.dependencies:
                    if dep not in step_map:
                        raise ExecutionError(f"Step {step_id} depends on non-existent step {dep}")
                    if dep not in visited:
                        if is_cyclic(dep):
                            return True
                    elif dep in rec_stack:
                        return True
            rec_stack.remove(step_id)
            return False

        for step in plan.steps:
            if step.step_id not in visited:
                if is_cyclic(step.step_id):
                    logger.error(
                        "Cycle detected in investigation plan DAG",
                        extra={"plan_id": str(plan.plan_id)},
                    )
                    return False
        return True

    def get_next_executable_steps(self, plan: InvestigationPlan) -> list[InvestigationStep]:
        """Returns pending steps whose dependencies are fully COMPLETED."""
        completed_step_ids: set[UUID] = {
            s.step_id for s in plan.steps if s.status == StepStatus.COMPLETED
        }
        executable: list[InvestigationStep] = []

        for step in plan.steps:
            if step.status == StepStatus.PENDING:
                if all(dep_id in completed_step_ids for dep_id in step.dependencies):
                    executable.append(step)

        return executable

    def replan(
        self, plan: InvestigationPlan, evidence_gaps: list[str], reason: str
    ) -> InvestigationPlan:
        """Adaptively updates plan steps based on newly identified information gaps.

        F-053: hard guards against plan explosion — revision count, per-replan
        gap count, total step count, and semantic dedup of equivalent gaps.
        """
        if plan.revision_count >= self.MAX_REVISIONS:
            raise ExecutionError(
                f"Maximum replan revisions ({self.MAX_REVISIONS}) exceeded; refusing to expand plan"
            )
        # Deduplicate semantically equivalent gaps (normalized, case-insensitive).
        seen: set[str] = set()
        unique_gaps: list[str] = []
        for gap in evidence_gaps:
            normalized = " ".join(gap.split()).lower()
            if normalized and normalized not in seen:
                seen.add(normalized)
                unique_gaps.append(gap)
        if len(unique_gaps) > self.MAX_GAPS_PER_REPLAN:
            raise ExecutionError(
                f"Replan requested {len(unique_gaps)} new steps; maximum is {self.MAX_GAPS_PER_REPLAN}"
            )
        new_steps: list[InvestigationStep] = list(plan.steps)
        now = datetime.now(UTC)

        existing_objectives = {" ".join(s.objective.split()).lower() for s in new_steps}
        for gap in unique_gaps:
            objective = f"Address gap: {gap}"
            if " ".join(objective.split()).lower() in existing_objectives:
                continue
            if len(new_steps) >= self.MAX_STEPS:
                raise ExecutionError(
                    f"Maximum plan steps ({self.MAX_STEPS}) reached; refusing to expand plan"
                )
            new_step = InvestigationStep(
                step_type=InvestigationStepType.SEARCH_RUNTIME,
                objective=objective,
                dependencies=[plan.active_step_id] if plan.active_step_id else [],
            )
            new_steps.append(new_step)
            existing_objectives.add(" ".join(objective.split()).lower())

        updated_plan = plan.model_copy(
            update={
                "steps": new_steps,
                "revision_count": plan.revision_count + 1,
                "updated_at": now,
                "revision_reason": reason,
            }
        )

        if not self.validate_dag(updated_plan):
            raise ExecutionError("Replanning generated an invalid cyclic dependency graph")

        return updated_plan
