# src/investigation_agent_platform/application/onboarding/activities.py
"""Phase 2 dispatch activities: the only places that touch control state.

Activities are methods on ``OnboardingActivities`` so the Temporal worker can
register a bound instance while unit tests instantiate the same class with
fakes. Workflow code references activity methods directly (typed, refactor
safe); all inputs are Temporal-payload-safe (str/int/bool/None only).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid5

from temporalio import activity

from investigation_agent_platform.application.onboarding.steps import (
    AnalyzeCoverageInput,
)


@dataclass(frozen=True)
class VerifyRevisionInput:
    tenant_id: str
    request_id: str
    revision: int
    execution_generation: int


@dataclass(frozen=True)
class RevisionSnapshot:
    tenant_id: str
    request_id: str
    revision: int
    execution_generation: int
    content_hash: str
    state: str
    proceed: bool
    reason: str


@dataclass(frozen=True)
class HeartbeatInput:
    tenant_id: str
    request_id: str
    revision: int


@dataclass(frozen=True)
class HeartbeatOutcome:
    alive: bool
    cancel_requested: bool
    detail: str = ""


@dataclass(frozen=True)
class CompleteCancellationInput:
    tenant_id: str
    dispatch_id: str
    request_id: str
    revision: int


@dataclass(frozen=True)
class TerminalOutcome:
    terminal: bool
    error_code: str = ""


@dataclass(frozen=True)
class RecordTerminalInput:
    tenant_id: str
    dispatch_id: str
    error_code: str
    fencing_token: int | None = None


def _uuid(value: str | UUID) -> UUID:
    """Coerce activity string IDs to UUIDs. Invalid input fails closed."""
    return value if isinstance(value, UUID) else UUID(str(value))


class OnboardingActivities:
    """Activity implementations; constructed with repository access."""

    def __init__(
        self,
        dispatch_repository: Any,
        *,
        probe_repository: Any | None = None,
        coverage_repository: Any | None = None,
    ) -> None:
        self._dispatches = dispatch_repository
        self._probes = probe_repository
        self._coverage = coverage_repository

    @activity.defn(name="verify_revision")
    async def verify_revision(self, inp: VerifyRevisionInput) -> RevisionSnapshot:
        """Confirm the revision the workflow was started for is dispatchable.

        Pure read plus judgment-free rule: proceed only from QUEUED (or route
        CANCEL_REQUESTED to the cancel path, never to work).
        """
        row = await self._dispatches.read_request_row(
            inp.tenant_id, _uuid(inp.request_id), inp.revision
        )
        if row is None:
            return RevisionSnapshot(
                tenant_id=inp.tenant_id,
                request_id=inp.request_id,
                revision=inp.revision,
                execution_generation=inp.execution_generation,
                content_hash="",
                state="missing",
                proceed=False,
                reason="request revision not found",
            )
        state = str(row.get("state", ""))
        content_hash = str(row.get("content_hash", ""))
        if state == "CANCEL_REQUESTED":
            return RevisionSnapshot(
                tenant_id=inp.tenant_id,
                request_id=inp.request_id,
                revision=inp.revision,
                execution_generation=inp.execution_generation,
                content_hash=content_hash,
                state=state,
                proceed=False,
                reason="cancellation requested",
            )
        if state != "QUEUED":
            return RevisionSnapshot(
                tenant_id=inp.tenant_id,
                request_id=inp.request_id,
                revision=inp.revision,
                execution_generation=inp.execution_generation,
                content_hash=content_hash,
                state=state,
                proceed=False,
                reason=f"request is {state}, not QUEUED",
            )
        return RevisionSnapshot(
            tenant_id=inp.tenant_id,
            request_id=inp.request_id,
            revision=inp.revision,
            execution_generation=inp.execution_generation,
            content_hash=content_hash,
            state=state,
            proceed=True,
            reason="revision verified dispatchable",
        )

    @activity.defn(name="heartbeat")
    async def heartbeat(self, inp: HeartbeatInput) -> HeartbeatOutcome:
        """Cancellation checkpoint and liveness evidence. Read-only by design.

        Heartbeats never mutate dispatch state: the reconciler owns staleness
        decisions from fencing tokens and workflow descriptions, not from a
        heartbeat table that could itself go stale.
        """
        state = await self._dispatches.read_request_state(
            inp.tenant_id, _uuid(inp.request_id), inp.revision
        )
        if state is None:
            return HeartbeatOutcome(alive=False, cancel_requested=False, detail="request missing")
        if state in ("CANCEL_REQUESTED", "CANCELED"):
            return HeartbeatOutcome(alive=True, cancel_requested=True, detail=f"request is {state}")
        if state != "QUEUED":
            return HeartbeatOutcome(
                alive=False, cancel_requested=False, detail=f"request is {state}"
            )
        return HeartbeatOutcome(alive=True, cancel_requested=False, detail="progressing")

    @activity.defn(name="complete_cancellation")
    async def complete_cancellation(self, inp: CompleteCancellationInput) -> TerminalOutcome:
        """Idempotent cancellation completion: dispatch TERMINAL, request CANCELED.

        Safe to retry and safe when partially applied: both transitions are
        conditional writes that converge on the same end state.
        """
        dispatch_uuid = _uuid(inp.dispatch_id)
        request_uuid = _uuid(inp.request_id)
        await self._dispatches.mark_terminal(inp.tenant_id, dispatch_uuid, "canceled")
        transitioned = await self._dispatches.complete_request_cancellation(
            inp.tenant_id, request_uuid, inp.revision
        )
        if transitioned:
            return TerminalOutcome(terminal=True, error_code="canceled")
        state = await self._dispatches.read_request_state(inp.tenant_id, request_uuid, inp.revision)
        if state == "CANCELED":
            return TerminalOutcome(terminal=True, error_code="canceled")
        return TerminalOutcome(terminal=False, error_code="cancel-not-applicable")

    @activity.defn(name="record_terminal")
    async def record_terminal(self, inp: RecordTerminalInput) -> TerminalOutcome:
        """Record a terminal outcome with fencing. Stale holders fail closed."""
        if not inp.error_code:
            raise ValueError("error_code must be non-empty")
        marked = await self._dispatches.mark_terminal(
            inp.tenant_id, _uuid(inp.dispatch_id), inp.error_code, inp.fencing_token
        )
        if not marked:
            return TerminalOutcome(terminal=False, error_code="stale-holder")
        return TerminalOutcome(terminal=True, error_code=inp.error_code)

    @activity.defn(name="analyze_coverage")
    async def analyze_coverage(self, inputs: AnalyzeCoverageInput) -> dict[str, Any]:
        """DEP-07 analysis slot: deterministic assembly + workflow filing.

        No model judgment runs here: verdicts are derived mechanically from
        stored probe evidence (VERIFIED probes become VERIFIED claims about
        the probe outcome itself), assembled through the agent's validated
        contract, and filed under workflow authority with the dispatch
        fencing token. Empty evidence fails closed -- nothing is filed.
        Retries of the same generation reassemble byte-identically (stable
        report_id, recomputed hashes) and replay as success.
        """
        from investigation_agent_platform.application.onboarding.adapters import (
            AdapterScope,
            ProbeOutcome,
        )
        from investigation_agent_platform.application.onboarding.catalog import (
            V01_CATALOG,
        )
        from investigation_agent_platform.application.onboarding_agent.agent.analysis import (
            assemble_coverage_report,
        )
        from investigation_agent_platform.application.onboarding_agent.contracts.coverage import (
            CapabilityDisposition,
            Claim,
            Confidence,
            EvidencePointer,
            ServiceVerdict,
            SupportTier,
        )
        from investigation_agent_platform.application.onboarding_agent.contracts.linkage import (
            canonical_json_bytes,
            sha256_hex,
        )

        if self._probes is None or self._coverage is None:
            raise ValueError("probe and coverage repositories are not configured")
        if inputs.generation < 1:
            raise ValueError("generation must be >= 1")
        request_uuid = _uuid(inputs.request_id)
        row = await self._dispatches.read_request_row(
            inputs.tenant_id, request_uuid, inputs.revision
        )
        if row is None:
            raise ValueError("request revision not found")
        dispatch = None
        if inputs.dispatch_id:
            dispatch = await self._dispatches.read_dispatch(
                inputs.tenant_id, _uuid(inputs.dispatch_id)
            )
        fencing_token = 0
        worker_identity = "workflow:unassigned"
        if dispatch is not None:
            fencing_token = int(dispatch.get("lease_fencing_token") or 0)
            workflow_id = str(dispatch.get("workflow_id") or inputs.dispatch_id)
            worker_identity = f"workflow:{workflow_id}"[:128]
        scope = AdapterScope(
            tenant_id=inputs.tenant_id,
            application_id=str(row.get("application_id", "")),
            environment=str(row.get("environment", "")),
            request_id=request_uuid,
            revision=inputs.revision,
            policy_version=str(row.get("policy_version", "")),
            catalog_version=str(row.get("catalog_version", "")),
        )
        stored = await self._probes.list_evidence_for(scope)
        if not stored:
            raise ValueError("no probe evidence for this request pin; nothing to file")

        sniffer_outputs: list[dict[str, Any]] = []
        verdicts: list[ServiceVerdict] = []
        for item in stored:
            try:
                entry = V01_CATALOG.entry_for(item.capability_key)
            except KeyError as exc:
                raise ValueError(
                    f"probe evidence for uncatalogued capability {item.capability_key!r}"
                ) from exc
            probe_bytes = canonical_json_bytes(
                {
                    "adapter": item.adapter,
                    "adapter_version": item.adapter_version,
                    "capability_key": item.capability_key,
                    "outcome": item.outcome,
                    "diagnostic_code": item.diagnostic_code,
                    "tenant_id": item.tenant_id,
                    "request_id": str(item.request_id),
                    "revision": item.revision,
                }
            )
            content_hash = sha256_hex(probe_bytes)
            sniffer_outputs.append(
                {
                    "content_hash": content_hash,
                    "tool": item.adapter,
                    "tool_version": item.adapter_version,
                    "schema_version": "probe/v1",
                }
            )
            pointer = EvidencePointer(
                output_hash=content_hash,
                field_path=f"probe.{item.capability_key}",
            )
            if item.outcome == ProbeOutcome.VERIFIED.value:
                claims = [
                    Claim(
                        statement=f"{item.capability_key} probe VERIFIED",
                        confidence=Confidence.VERIFIED,
                        evidence=[pointer],
                    )
                ]
            else:
                claims = [
                    Claim(
                        statement=(
                            f"{item.capability_key} probe {item.outcome}: {item.diagnostic_code}"
                        ),
                        confidence=Confidence.PROJECTED,
                        confirming_probe=f"rerun {item.adapter} probe for {item.capability_key}",
                        evidence=[pointer],
                    )
                ]
            verdicts.append(
                ServiceVerdict(
                    service=item.capability_key,
                    languages=[],
                    tier=SupportTier.GENERIC,
                    dispositions=[
                        CapabilityDisposition(
                            capability=item.capability_key,
                            disposition=entry.policy_by_tier["standard"].value,
                            reasons=[f"catalog {entry.version}"],
                        )
                    ],
                    claims=claims,
                )
            )

        report_id = uuid5(
            request_uuid, f"onboarding-coverage:{inputs.revision}:{inputs.generation}"
        )
        report = assemble_coverage_report(
            report_id=report_id,
            request_id=request_uuid,
            revision=inputs.revision,
            generation=inputs.generation,
            report_version="coverage/v1",
            registry_version="support-registry-v0.1",
            catalog_version=scope.catalog_version,
            estate_sha=inputs.snapshot_reference,
            analyzer_version="probe-deterministic-v1",
            model_version=None,
            sniffer_outputs=sniffer_outputs,
            verdicts=verdicts,
            limitations=[
                "probe-only assessment; no sniffer outputs ingested",
                "verdicts describe probe outcomes, not estate support depth",
            ],
        )
        filed = await self._coverage.file_report(
            tenant_id=inputs.tenant_id,
            request_id=request_uuid,
            revision=inputs.revision,
            generation=inputs.generation,
            report_body=report.model_dump(mode="json"),
            sniffer_outputs=sniffer_outputs,
            estate_sha=inputs.snapshot_reference,
            registry_version="support-registry-v0.1",
            catalog_version=scope.catalog_version,
            worker_id=worker_identity,
            fencing_token=fencing_token,
        )
        return {
            "filed": True,
            "report_id": str(filed.report_id),
            "generation": inputs.generation,
            "report_content_hash": filed.report_content_hash,
        }
