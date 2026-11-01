"""D3: stub activities registered under the real names.

Stubs record their inputs, which is how tests assert "execute was not
called on the D6 turn" and "conclude received these statements".
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from temporalio import activity

from investigation_agent_platform.application.worker.activities import (
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
)


@dataclass
class Script:
    """Per-turn scripted activity results plus a call log (plain data; the stubs close over it)."""

    reason: list[dict[str, Any]] = field(default_factory=list)
    execute: Callable[[ExecuteActionInput], dict[str, Any]] | None = None
    verify: list[dict[str, Any]] = field(default_factory=list)
    # T12.12: when set, the capture stub calls it (may raise to prove the
    # workflow survives a post-conclude capture failure).
    capture: Callable[[CaptureKnowledgeInput], dict[str, Any]] | None = None
    calls: list[tuple[str, Any]] = field(default_factory=list)


def build_stubs(
    script: Script, *, on_activity: Callable[[str], Any] | None = None
) -> list[Any]:
    """Activities registered under the real names."""

    async def _tick(name: str) -> None:
        if on_activity is not None:
            await on_activity(name)

    @activity.defn(name="create_investigation_activity")
    async def create(p: CreateInvestigationInput) -> CreateInvestigationOutput:
        from investigation_agent_platform.domain.investigation.loop_version import (
            LOOP_VERSION_CURRENT,
        )

        script.calls.append(("create", p))
        # F10: the real activity records the resolved pin; an unmarked
        # input resolves to current, exactly like _pinned_loop_version.
        return CreateInvestigationOutput(
            investigation_id=p.investigation_id or "inv-harness",
            status="CREATED",
            loop_version=LOOP_VERSION_CURRENT,
        )

    @activity.defn(name="transition_investigation_activity")
    async def transition(p: TransitionInvestigationInput) -> GenericActivityResult:
        script.calls.append(("transition", p.target_status))
        return GenericActivityResult(success=True, data={})

    @activity.defn(name="retrieve_knowledge_activity")
    async def knowledge(p: RetrieveKnowledgeInput) -> GenericActivityResult:
        return GenericActivityResult(success=True, data={})

    @activity.defn(name="reason_activity")
    async def reason(p: ReasonInput) -> GenericActivityResult:
        script.calls.append(("reason", p.iteration))
        await _tick("reason")
        data = dict(script.reason[min(p.iteration, len(script.reason) - 1)])
        # Production normalizes through the Decision model
        # (hypotheses_evaluated: list[str]); the harness stub emits the
        # same shape so payload conversion matches the real wire.
        hyps = data.get("hypotheses_evaluated") or []
        if isinstance(hyps, list):
            data["hypotheses_evaluated"] = [
                h.get("statement") or h.get("text") if isinstance(h, dict) else h
                for h in hyps
            ]
        return GenericActivityResult(success=True, data=data)

    @activity.defn(name="execute_action_activity")
    async def execute(p: ExecuteActionInput) -> GenericActivityResult:
        script.calls.append(("execute", p))
        await _tick("execute")
        data = (
            script.execute(p)
            if script.execute
            else {"action_outcome": {"status": "EMPTY", "evidence_ids": []}}
        )
        return GenericActivityResult(success=True, data=data)

    @activity.defn(name="retrieve_evidence_activity")
    async def retrieve(p: RetrieveEvidenceInput) -> GenericActivityResult:
        return GenericActivityResult(success=True, data={})

    @activity.defn(name="verify_root_cause_activity")
    async def verify(p: VerifyRootCauseInput) -> GenericActivityResult:
        script.calls.append(("verify", p.iteration))
        await _tick("verify")
        return GenericActivityResult(
            success=True,
            data=dict(
                script.verify[min(p.iteration, len(script.verify) - 1)]
                if script.verify
                else {"verified": False}
            ),
        )

    @activity.defn(name="checkpoint_activity")
    async def checkpoint(p: CheckpointInput) -> GenericActivityResult:
        return GenericActivityResult(success=True, data={})

    @activity.defn(name="conclude_investigation_activity")
    async def conclude(p: ConcludeInvestigationInput) -> GenericActivityResult:
        script.calls.append(("conclude", p))
        return GenericActivityResult(success=True, data={"status": p.status})

    @activity.defn(name="capture_knowledge_activity")
    async def capture(p: CaptureKnowledgeInput) -> GenericActivityResult:
        script.calls.append(("capture", p))
        if script.capture is not None:
            return GenericActivityResult(success=True, data=script.capture(p))
        return GenericActivityResult(success=True, data={})

    @activity.defn(name="publish_event_activity")
    async def publish(p: PublishEventInput) -> GenericActivityResult:
        script.calls.append(("publish", p))
        return GenericActivityResult(success=True, data={})

    return [
        create,
        transition,
        knowledge,
        reason,
        execute,
        retrieve,
        verify,
        checkpoint,
        conclude,
        capture,
        publish,
    ]
