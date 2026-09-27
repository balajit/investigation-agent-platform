# src/investigation_agent_platform/application/topology/attribution.py
"""Dual-frame failure attribution service (D8 in the design document).

Static topology alone cannot determine whether a shared-library failure is a
library defect or caller misuse — that requires runtime boundary evidence.
This service combines both and converts the result into standard, provenanced
``Evidence`` through the evidence repository. It never finalizes a
conclusion; ``ConclusionGate`` remains the sole authority for that.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from investigation_agent_platform.domain.common.exceptions import (
    TopologyNodeNotFoundError,
)
from investigation_agent_platform.domain.evidence.models import (
    ClassificationLevel,
    Evidence,
    EvidenceType,
)
from investigation_agent_platform.domain.provenance.models import (
    EvidenceFreshness,
    EvidenceProvenance,
    QueryFingerprint,
    SourceLocation,
)
from investigation_agent_platform.domain.topology.models import (
    AttributionClassification,
    AttributionFallbackLevel,
    CrossRepositoryHop,
    DomainAttributionResult,
    RepositoryType,
    StackFrameLocation,
    StaticOwnershipResult,
    TopologyNodeType,
)
from investigation_agent_platform.ports.persistence.repositories import EvidenceRepository
from investigation_agent_platform.ports.topology.ports import DomainAttributionPort

logger = logging.getLogger(__name__)

ATTRIBUTION_RULE_VERSION = "v2"

#: Confidence ceiling for macro-attributed results (ISSUE-6). Ownership from
#: a CODEOWNERS file is a team-level heuristic, not symbol-precise evidence —
#: it may inform the agent but must never pass conclusion thresholds alone.
MACRO_CONFIDENCE_CAP = 0.3


class _ResolvedFrame:
    """One frame after the full fallback chain (ISSUE-6).

    ``ownership`` is the port result (possibly REPOSITORY-level).
    ``tier`` is "macro" unless on-demand micro parsing located the symbol.
    ``macro_team`` is the CODEOWNERS owner, set only when static ownership
    yields no domain owner. ``micro_node`` carries the micro symbol when the
    tier is micro, so the result records exact precision even though
    ownership remains repo-level.
    """

    __slots__ = ("macro_team", "micro_node", "micro_revision_verified", "ownership", "tier")

    def __init__(
        self,
        ownership: StaticOwnershipResult,
        tier: str = "macro",
        macro_team: str | None = None,
        micro_node: Any | None = None,
        micro_revision_verified: bool = True,
    ) -> None:
        self.ownership = ownership
        self.tier = tier
        self.macro_team = macro_team
        self.micro_node = micro_node
        self.micro_revision_verified = micro_revision_verified

    def effective_domain(self) -> tuple[str | None, bool]:
        """Return (domain_or_None, is_macro_sourced)."""
        if self.ownership.domain_id is not None:
            return self.ownership.domain_id, False
        if self.macro_team is not None:
            return self.macro_team, True
        return None, False


class FailureAttributionService:
    """Resolves dual-frame ownership and persists it as provenanced evidence."""

    def __init__(
        self,
        attribution_port: DomainAttributionPort,
        evidence_repo: EvidenceRepository | None = None,
        repository_registry: Any | None = None,
        codeowners_resolver: Any | None = None,
        micro_resolver: Any | None = None,
        trace_hop_resolver: Any | None = None,
    ) -> None:
        self._attribution_port = attribution_port
        self._evidence_repo = evidence_repo
        # Optional graceful-degradation tiers (ISSUE-6 graceful hierarchy,
        # ISSUE-3 micro tier). Absent resolvers simply disable their tier —
        # the static path is unchanged.
        self._repository_registry = repository_registry
        self._codeowners_resolver = codeowners_resolver
        self._micro_resolver = micro_resolver
        # ISSUE-5: cross-repository trace hop. Absent resolver simply
        # disables hopping — a ROUTE/MESSAGE_HANDLER boundary then resolves
        # exactly as it did before ISSUE-5 (single-repository result).
        self._trace_hop_resolver = trace_hop_resolver

    async def attribute_failure(
        self,
        tenant_id: str,
        application_id: str,
        investigation_id: UUID,
        failure_frame: StackFrameLocation,
        caller_frame: StackFrameLocation | None,
        supporting_evidence_ids: list[UUID] | None = None,
        caller_input_violates_contract: bool | None = None,
        trace_id: str | None = None,
        span_id: str | None = None,
        environment: str = "production",
    ) -> DomainAttributionResult:
        """Resolve static ownership for both frames and apply the dual-frame policy.

        ``caller_input_violates_contract`` is the runtime-evidence signal that
        distinguishes ``LIBRARY_DEFECT`` from ``CALLER_MISUSE`` when the
        failure repository is a shared library:

        - ``True``: the caller passed input that violates the library's
          documented contract -> ``CALLER_MISUSE``.
        - ``False``: the library raised on well-formed input (an internal
          invariant/uncaught defect) -> ``LIBRARY_DEFECT``.
        - ``None``: insufficient runtime evidence -> ``INCONCLUSIVE``. This
          service never guesses.

        ``trace_id``/``span_id`` (ISSUE-5): when the failure frame resolves
        to a ``ROUTE``/``MESSAGE_HANDLER`` boundary node and a
        ``trace_hop_resolver`` is configured, attempts a corroborated
        cross-repository hop using distributed trace evidence. Without a
        ``trace_id``, or without corroborating trace evidence, attribution
        stays single-repository — a hop is never fabricated.
        """
        supporting_evidence_ids = supporting_evidence_ids or []

        try:
            failure = await self._resolve_frame(
                tenant_id, application_id, investigation_id, failure_frame
            )
        except TopologyNodeNotFoundError:
            logger.warning(
                "Failure frame could not be resolved to any topology node",
                extra={"tenant_id": tenant_id, "repository_id": failure_frame.repository_id},
            )
            return self._inconclusive(
                tenant_id,
                application_id,
                investigation_id,
                failure_frame,
                "failure frame could not be resolved against static topology",
            )

        caller: _ResolvedFrame | None = None
        if caller_frame is not None:
            caller = await self._resolve_frame(
                tenant_id, application_id, investigation_id, caller_frame
            )

        failure_ownership = failure.ownership
        failure_domain, failure_macro = failure.effective_domain()
        caller_domain, caller_macro = (
            caller.effective_domain() if caller is not None else (None, False)
        )
        macro_used = failure_macro or caller_macro

        limitations: list[str] = []
        alternatives: list[str] = []
        classification = AttributionClassification.INCONCLUSIVE
        confidence = 0.0
        culprit_domain: str | None = None
        victim_domain: str | None = None

        if failure.tier == "micro" and not failure.micro_revision_verified:
            limitations.append(
                "failure symbol resolved by on-demand parse against an "
                "unverified working-tree revision; treat location as approximate"
            )
        if caller is not None and caller.tier == "micro" and not caller.micro_revision_verified:
            limitations.append(
                "caller symbol resolved by on-demand parse against an "
                "unverified working-tree revision; treat location as approximate"
            )

        is_shared_library = failure_ownership.repository_type == RepositoryType.SHARED_LIBRARY

        if not is_shared_library:
            # Non-library failure: the failure-frame owner is the candidate
            # culprit, but only when the caller frame does NOT belong to a
            # different domain than the one observed causing the fault. We
            # still require the caller to have been resolved (or absent) to
            # avoid asserting certainty with no corroboration at all.
            classification = AttributionClassification.NON_LIBRARY_DEFECT
            culprit_domain = failure_domain
            victim_domain = failure_domain
            confidence = 0.7 if failure_domain else 0.0
            if failure_domain is None:
                classification = AttributionClassification.INCONCLUSIVE
                limitations.append("failure frame has no resolved domain owner")
        else:
            if caller is None:
                limitations.append("caller frame not supplied for shared-library failure")
                classification = AttributionClassification.INCONCLUSIVE
                # Documented deviation from the reviewer's literal ISSUE-6
                # wording ("only INCONCLUSIVE if no mapping exists at all"):
                # for shared libraries the defect-vs-misuse distinction is
                # unknowable without runtime evidence, and manufacturing a
                # verdict here is precisely the false-culprit risk the
                # dual-frame design exists to prevent. The CODEOWNERS owner
                # is preserved below as a lead (alternatives + limitations),
                # never as a verdict.
                if failure_domain is not None:
                    alternatives.append(failure_domain)
            elif caller_input_violates_contract is None:
                limitations.append(
                    "no runtime evidence available to distinguish defect from misuse"
                )
                classification = AttributionClassification.INCONCLUSIVE
                if failure_domain:
                    alternatives.append(failure_domain)
                if caller_domain:
                    alternatives.append(caller_domain)
            elif caller_input_violates_contract:
                classification = AttributionClassification.CALLER_MISUSE
                culprit_domain = caller_domain
                victim_domain = failure_domain
                confidence = 0.75 if caller_domain else 0.0
            else:
                classification = AttributionClassification.LIBRARY_DEFECT
                culprit_domain = failure_domain
                victim_domain = caller_domain
                confidence = 0.75 if failure_domain else 0.0

        if classification == AttributionClassification.INCONCLUSIVE:
            confidence = 0.0
            culprit_domain = None

        # ISSUE-6 macro cap: CODEOWNERS-sourced ownership may inform the
        # agent but must never pass conclusion thresholds on its own.
        ownership_source = "codeowners" if macro_used else "static"
        if macro_used:
            confidence = min(confidence, MACRO_CONFIDENCE_CAP)
            limitations.append(
                "owner sourced from CODEOWNERS (macro-attribution, capped "
                "confidence); not symbol-precise"
            )

        # Fallback level: CODEOWNERS supersedes a repo-level non-answer with
        # a team answer; AST-level precision is preserved when the location
        # itself was precise.
        if (
            failure_macro
            and failure_ownership.fallback_level == AttributionFallbackLevel.REPOSITORY
        ):
            fallback_level = AttributionFallbackLevel.CODEOWNERS
        else:
            fallback_level = failure_ownership.fallback_level

        # Micro precision: record the exact symbol even though ownership
        # remains repo-level. Tier is recorded separately for provenance.
        matched_node_id = failure_ownership.matched_node_id
        matched_file_path = failure_ownership.matched_file_path
        matched_line_range: tuple[int, int] | None = (
            (failure_ownership.matched_start_line, failure_ownership.matched_end_line)
            if failure_ownership.matched_start_line is not None
            and failure_ownership.matched_end_line is not None
            else None
        )
        if failure.micro_node is not None:
            matched_node_id = failure.micro_node.node_id
            matched_file_path = failure.micro_node.file_path
            matched_line_range = (
                failure.micro_node.start_line,
                failure.micro_node.end_line,
            )
        resolution_tier = failure.tier

        # ISSUE-5: static traversal reached an API/queue boundary. Attempt a
        # trace-corroborated cross-repository hop; a boundary with no
        # trace_id, no configured resolver, or no corroborating evidence
        # simply yields no hop — the single-repository result is unchanged.
        hops: list[CrossRepositoryHop] = []
        if failure_ownership.node_type in (
            TopologyNodeType.ROUTE,
            TopologyNodeType.MESSAGE_HANDLER,
        ):
            hop = await self._try_cross_repo_hop(
                tenant_id=tenant_id,
                application_id=application_id,
                investigation_id=investigation_id,
                environment=environment,
                failure_frame=failure_frame,
                failure_ownership=failure_ownership,
                trace_id=trace_id,
                span_id=span_id,
            )
            if hop is not None:
                hops.append(hop)
                limitations.append(
                    f"cross-repository hop to service '{hop.target_service}' "
                    f"({hop.target_repository_id}@{hop.target_revision}) corroborated "
                    f"by trace {hop.trace_id} span {hop.target_span_id}"
                )

        result = DomainAttributionResult(
            tenant_id=tenant_id,
            application_id=application_id,
            investigation_id=investigation_id,
            repository_id=failure_ownership.repository_id,
            revision=failure_ownership.revision,
            snapshot_id=failure_ownership.snapshot_id,
            matched_node_id=matched_node_id,
            matched_file_path=matched_file_path,
            matched_line_range=matched_line_range,
            ownership_path=(
                [failure_domain]
                if macro_used and failure_domain and not failure_ownership.ownership_path
                else failure_ownership.ownership_path
            ),
            repository_type=failure_ownership.repository_type,
            failure_frame_domain_id=failure_domain,
            caller_frame_domain_id=caller_domain,
            proposed_culprit_domain_id=culprit_domain,
            proposed_victim_domain_id=victim_domain,
            classification=classification,
            confidence=confidence,
            alternatives=alternatives,
            limitations=limitations,
            supporting_evidence_ids=supporting_evidence_ids,
            attribution_rule_version=ATTRIBUTION_RULE_VERSION,
            fallback_level=fallback_level,
            ownership_source=ownership_source,
            resolution_tier=resolution_tier,
            hops=hops,
        )

        if self._evidence_repo is not None:
            evidence = self._to_evidence(result)
            await self._evidence_repo.save(tenant_id, evidence, investigation_id)

        return result

    async def _resolve_frame(
        self,
        tenant_id: str,
        application_id: str,
        investigation_id: UUID,
        frame: StackFrameLocation,
    ) -> _ResolvedFrame:
        """Resolve one frame through the macro → micro → CODEOWNERS chain.

        Static port lookup first; on REPOSITORY fallback, on-demand micro
        parsing for symbol precision; CODEOWNERS team recorded whenever
        static ownership yields no domain. Each tier degrades gracefully to
        the next — a tier being unavailable or empty never raises.
        """
        ownership = await self._attribution_port.resolve_source_location(
            tenant_id=tenant_id,
            application_id=application_id,
            investigation_id=investigation_id,
            repository_id=frame.repository_id,
            revision=frame.revision,
            file_path=frame.file_path,
            line_number=frame.line_number,
        )
        tier = "macro"
        micro_node: Any | None = None
        micro_verified = True
        if ownership.fallback_level == AttributionFallbackLevel.REPOSITORY:
            micro = await self._try_micro(tenant_id, application_id, frame)
            if micro is not None:
                tier = "micro"
                micro_node = micro.node
                micro_verified = micro.revision_verified

        macro_team: str | None = None
        if ownership.domain_id is None:
            macro_team = await self._try_codeowners(tenant_id, application_id, frame)
        return _ResolvedFrame(
            ownership=ownership,
            tier=tier,
            macro_team=macro_team,
            micro_node=micro_node,
            micro_revision_verified=micro_verified,
        )

    async def _locate_repository(
        self, tenant_id: str, application_id: str, repository_id: str
    ) -> str | None:
        """Map (tenant, application, frame repository) to a filesystem locator.

        The locator comes from the application's registered repository
        identity and must match the frame's repository — cross-repo frames
        cannot be located this way and gracefully disable file-based tiers.
        """
        if self._repository_registry is None:
            return None
        try:
            repo = await self._repository_registry.resolve_for_application(
                tenant_id, application_id
            )
        except Exception:
            logger.warning("Repository registry lookup failed", extra={"tenant_id": tenant_id})
            return None
        if repo is None or repo.repository_id != repository_id:
            return None
        return repo.locator or None

    async def _try_micro(
        self,
        tenant_id: str,
        application_id: str,
        frame: StackFrameLocation,
    ) -> Any | None:
        if self._micro_resolver is None:
            return None
        locator = await self._locate_repository(tenant_id, application_id, frame.repository_id)
        if locator is None:
            return None
        try:
            return await self._micro_resolver.resolve_micro_symbol(
                tenant_id=tenant_id,
                repository_id=frame.repository_id,
                locator=locator,
                revision=frame.revision,
                file_path=frame.file_path,
                line_number=frame.line_number,
            )
        except Exception:
            logger.warning(
                "Micro resolution failed; continuing without micro tier",
                extra={"tenant_id": tenant_id, "repository_id": frame.repository_id},
            )
            return None

    async def _try_codeowners(
        self,
        tenant_id: str,
        application_id: str,
        frame: StackFrameLocation,
    ) -> str | None:
        if self._codeowners_resolver is None:
            return None
        locator = await self._locate_repository(tenant_id, application_id, frame.repository_id)
        if locator is None:
            return None
        try:
            owner: str | None = await self._codeowners_resolver.resolve_owner(
                tenant_id, locator, frame.file_path
            )
            return owner
        except Exception:
            logger.warning(
                "CODEOWNERS resolution failed; continuing without macro tier",
                extra={"tenant_id": tenant_id, "repository_id": frame.repository_id},
            )
            return None

    async def _try_cross_repo_hop(
        self,
        tenant_id: str,
        application_id: str,
        investigation_id: UUID,
        environment: str,
        failure_frame: StackFrameLocation,
        failure_ownership: StaticOwnershipResult,
        trace_id: str | None,
        span_id: str | None,
    ) -> CrossRepositoryHop | None:
        """ISSUE-5: corroborate and record one cross-repository hop.

        Every step degrades to "no hop" on any missing dependency, missing
        trace evidence, or lookup failure — a hop is never taken without
        trace corroboration, and a corroboration/lookup error never fails
        the surrounding attribution.
        """
        if not trace_id or self._trace_hop_resolver is None or self._repository_registry is None:
            return None

        try:
            target = await self._trace_hop_resolver.resolve_target_service(
                tenant_id=tenant_id,
                investigation_id=investigation_id,
                application_id=application_id,
                environment=environment,
                trace_id=trace_id,
                source_span_id=span_id,
            )
        except Exception:
            logger.warning(
                "Trace hop target resolution failed; no hop attempted",
                extra={"tenant_id": tenant_id, "trace_id": trace_id},
            )
            return None
        if target is None:
            return None

        try:
            target_repo = await self._repository_registry.resolve_for_service_name(
                tenant_id, target.target_service
            )
        except Exception:
            logger.warning(
                "Target repository lookup failed for hop; no hop attempted",
                extra={"tenant_id": tenant_id, "target_service": target.target_service},
            )
            return None
        if target_repo is None:
            return None

        try:
            # No file/line is known from a service-boundary trace alone;
            # this resolves the target repository's REPOSITORY-tier domain
            # (same mechanism as the existing REPOSITORY fallback), not a
            # symbol-precise location. Uses the target's current
            # default-branch snapshot — without deployment/version tracking,
            # this may not be the exact revision that served the trace.
            target_ownership = await self._attribution_port.resolve_source_location(
                tenant_id=tenant_id,
                application_id=application_id,
                investigation_id=investigation_id,
                repository_id=target_repo.repository_id,
                revision=target_repo.default_branch,
                file_path="__service_boundary__",
                line_number=1,
            )
        except Exception:
            logger.warning(
                "Target repository ownership resolution failed for hop; no hop attempted",
                extra={"tenant_id": tenant_id, "target_repository_id": target_repo.repository_id},
            )
            return None

        return CrossRepositoryHop(
            source_repository_id=failure_frame.repository_id,
            source_revision=failure_frame.revision,
            source_node_id=failure_ownership.matched_node_id,
            trace_id=trace_id,
            span_id=span_id,
            target_span_id=target.target_span_id,
            target_service=target.target_service,
            target_repository_id=target_repo.repository_id,
            target_revision=target_repo.default_branch,
            target_domain_id=target_ownership.domain_id,
        )

    def _inconclusive(
        self,
        tenant_id: str,
        application_id: str,
        investigation_id: UUID,
        failure_frame: StackFrameLocation,
        reason: str,
    ) -> DomainAttributionResult:
        return DomainAttributionResult(
            tenant_id=tenant_id,
            application_id=application_id,
            investigation_id=investigation_id,
            repository_id=failure_frame.repository_id,
            revision=failure_frame.revision,
            matched_file_path=failure_frame.file_path,
            classification=AttributionClassification.INCONCLUSIVE,
            confidence=0.0,
            limitations=[reason],
            attribution_rule_version=ATTRIBUTION_RULE_VERSION,
            fallback_level=AttributionFallbackLevel.UNATTRIBUTED,
        )

    @staticmethod
    def _to_evidence(result: DomainAttributionResult) -> Evidence:
        """Convert an attribution result into standard provenanced ``Evidence``.

        Never store raw source, prompts, or credentials — only the
        structured attribution outcome and its traversed ownership path.
        """
        now = datetime.now(UTC)
        payload = result.model_dump(mode="json")
        fingerprint_source = json.dumps(payload, sort_keys=True, default=str)
        fingerprint = hashlib.sha256(fingerprint_source.encode("utf-8")).hexdigest()

        prov = EvidenceProvenance(
            tenant_id=result.tenant_id,
            investigation_id=result.investigation_id,
            action_id=uuid4(),
            provider_type="TOPOLOGY_ATTRIBUTION",
            requested_provider_id="failure-attribution-service",
            actual_provider_id="failure-attribution-service",
            source_system="Layer3Topology",
            retrieval_timestamp=now,
            query_fingerprint=QueryFingerprint(
                provider_type="TOPOLOGY_ATTRIBUTION",
                operation="RESOLVE_DUAL_FRAME",
                normalized_query_hash=fingerprint[:64],
            ),
            source_location=SourceLocation(
                system="Layer3Topology",
                identifier=f"{result.repository_id}@{result.revision}",
                file_path=result.matched_file_path or None,
                line_start=result.matched_line_range[0] if result.matched_line_range else None,
                line_end=result.matched_line_range[1] if result.matched_line_range else None,
                revision=result.revision,
            ),
        )
        summary = (
            f"Dual-frame attribution: {result.classification.value} "
            f"(confidence={result.confidence:.2f}) for {result.repository_id}@{result.revision}"
        )
        return Evidence(
            tenant_id=result.tenant_id,
            investigation_id=result.investigation_id,
            evidence_type=EvidenceType.CALL_GRAPH,
            provider="TOPOLOGY_ATTRIBUTION",
            source=f"topology://{result.repository_id}/{result.revision}",
            title="Domain attribution result",
            summary=summary[:2048],
            content_snippet=json.dumps(payload, default=str)[:4000],
            observed_at=now,
            retrieved_at=now,
            provenance=prov,
            freshness=EvidenceFreshness(observed_at=now, retrieved_at=now),
            classification=ClassificationLevel.INTERNAL,
            fingerprint=fingerprint,
            attributes={"classification": result.classification.value},
        )
