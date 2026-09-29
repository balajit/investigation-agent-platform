# tests/unit/test_topology_coverage.py
"""Layer 3 topology bounded context coverage (prompt1_v1.md testing requirements).

Covers: domain model validation, deterministic node identity, in-memory
adapter contracts (tenant/revision isolation, idempotent ingestion,
deterministic lookup), dual-frame attribution policy branches, profile-backed
repository registry, evidence-gateway integration, Neo4j adapter failure
classification (mocked driver), and the workflow investigation-identity
invariant regression test.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from investigation_agent_platform.api.dependencies import AppContext, set_app_context
from investigation_agent_platform.domain.common.exceptions import (
    TopologyAmbiguousMatchError,
    TopologyNotConfiguredError,
    TopologySnapshotNotReadyError,
)


def _node(
    tenant_id: str = "tenant-a",
    repository_id: str = "repo-1",
    revision: str = "abc123",
    file_path: str = "src/mod.py",
    start_line: int = 1,
    end_line: int = 10,
    qualified_name: str = "mod.foo",
    name: str = "foo",
) -> object:
    from investigation_agent_platform.domain.topology.models import (
        ASTNodeIdentity,
        TopologyNodeType,
    )

    return ASTNodeIdentity.create(
        tenant_id=tenant_id,
        repository_id=repository_id,
        revision=revision,
        name=name,
        qualified_name=qualified_name,
        node_type=TopologyNodeType.FUNCTION,
        file_path=file_path,
        start_line=start_line,
        end_line=end_line,
    )


def _payload(
    tenant_id: str = "tenant-a",
    repository_id: str = "repo-1",
    revision: str = "abc123",
    **overrides,
):  # type: ignore[no-untyped-def]
    from investigation_agent_platform.domain.topology.models import ASTTopologyPayload

    node = _node(tenant_id, repository_id, revision)
    base = {
        "schema_version": "v1",
        "parser_version": "p1",
        "tenant_id": tenant_id,
        "application_id": "app-1",
        "repository_id": repository_id,
        "revision": revision,
        "snapshot_id": uuid.uuid4(),
        "ast_nodes": [node],
        "payload_hash": "hash-1",
    }
    base.update(overrides)
    return ASTTopologyPayload(**base)


# ===========================================================================
# Domain models
# ===========================================================================


class TestTopologyDomainModels:
    def test_deterministic_node_id(self) -> None:
        n1 = _node()
        n2 = _node()
        assert n1.node_id == n2.node_id

    def test_node_id_differs_across_tenant_revision(self) -> None:
        assert _node().node_id != _node(tenant_id="tenant-b").node_id
        assert _node().node_id != _node(revision="def456").node_id
        assert _node().node_id != _node(start_line=2, end_line=11).node_id

    def test_path_traversal_rejected(self) -> None:
        with pytest.raises(ValueError):
            _node(file_path="../etc/passwd")
        with pytest.raises(ValueError):
            _node(file_path="/abs/path.py")

    def test_inverted_range_rejected(self) -> None:
        from investigation_agent_platform.domain.topology.models import (
            ASTNodeIdentity,
            TopologyNodeType,
        )

        with pytest.raises(ValidationError):
            ASTNodeIdentity(
                tenant_id="t",
                repository_id="r",
                revision="rev",
                node_id=uuid.uuid4(),
                name="f",
                qualified_name="m.f",
                node_type=TopologyNodeType.FUNCTION,
                file_path="a.py",
                start_line=10,
                end_line=5,
            )

    def test_payload_rejects_cross_tenant_node(self) -> None:

        node = _node(tenant_id="tenant-b")
        with pytest.raises(ValidationError):
            _payload(ast_nodes=[node])

    def test_payload_rejects_dangling_caller(self) -> None:
        from investigation_agent_platform.domain.topology.models import (
            CallEdgeInput,
            CallResolutionStatus,
        )

        node = _node()
        with pytest.raises(ValidationError):
            _payload(
                ast_nodes=[node],
                calls=[
                    CallEdgeInput(
                        caller_node_id=uuid.uuid4(),
                        file_path="src/mod.py",
                        line_number=2,
                        resolution_status=CallResolutionStatus.UNRESOLVED,
                    )
                ],
            )

    def test_resolved_call_requires_callee(self) -> None:
        from investigation_agent_platform.domain.topology.models import (
            CallEdgeInput,
            CallResolutionStatus,
        )

        with pytest.raises(ValidationError):
            CallEdgeInput(
                caller_node_id=uuid.uuid4(),
                file_path="a.py",
                line_number=1,
                resolution_status=CallResolutionStatus.RESOLVED,
            )

    def test_owner_email_not_identity(self) -> None:
        # owner_team_id is required; contact email is optional metadata only.
        from investigation_agent_platform.domain.topology.models import DomainIdentity

        with pytest.raises(ValidationError):
            DomainIdentity(tenant_id="t", domain_id="d", name="n")  # type: ignore[call-arg]
        ok = DomainIdentity(tenant_id="t", domain_id="d", name="n", owner_team_id="team-1")
        assert ok.contact_email is None


# ===========================================================================
# In-memory adapter
# ===========================================================================


class TestInMemoryTopologyAdapter:
    @pytest.mark.asyncio
    async def test_ingest_and_idempotent_replay(self) -> None:
        from investigation_agent_platform.infrastructure.topology.in_memory import (
            InMemoryTopologyAdapter,
        )

        adapter = InMemoryTopologyAdapter()
        payload = _payload()
        first = await adapter.ingest(payload)
        assert first.status.value == "READY"
        assert first.node_count == 1
        second = await adapter.ingest(payload)
        assert second.status.value == "READY"
        assert second.snapshot_id == first.snapshot_id

    @pytest.mark.asyncio
    async def test_lookup_most_specific_match(self) -> None:
        from investigation_agent_platform.infrastructure.topology.in_memory import (
            InMemoryTopologyAdapter,
        )

        adapter = InMemoryTopologyAdapter()
        outer = _node(start_line=1, end_line=20, qualified_name="mod.outer", name="outer")
        inner = _node(start_line=5, end_line=8, qualified_name="mod.outer.inner", name="inner")
        await adapter.ingest(_payload(ast_nodes=[outer, inner]))
        result = await adapter.resolve_source_location(
            tenant_id="tenant-a",
            application_id="app-1",
            investigation_id=uuid.uuid4(),
            repository_id="repo-1",
            revision="abc123",
            file_path="src/mod.py",
            line_number=6,
        )
        assert result.matched_node_id == inner.node_id

    @pytest.mark.asyncio
    async def test_lookup_tenant_isolation(self) -> None:
        from investigation_agent_platform.infrastructure.topology.in_memory import (
            InMemoryTopologyAdapter,
        )

        adapter = InMemoryTopologyAdapter()
        await adapter.ingest(_payload())
        with pytest.raises(TopologyNotConfiguredError):
            await adapter.resolve_source_location(
                tenant_id="tenant-b",
                application_id="app-1",
                investigation_id=uuid.uuid4(),
                repository_id="repo-1",
                revision="abc123",
                file_path="src/mod.py",
                line_number=2,
            )

    @pytest.mark.asyncio
    async def test_lookup_revision_isolation(self) -> None:
        from investigation_agent_platform.infrastructure.topology.in_memory import (
            InMemoryTopologyAdapter,
        )

        adapter = InMemoryTopologyAdapter()
        await adapter.ingest(_payload())
        with pytest.raises(TopologyNotConfiguredError):
            await adapter.resolve_source_location(
                tenant_id="tenant-a",
                application_id="app-1",
                investigation_id=uuid.uuid4(),
                repository_id="repo-1",
                revision="other-rev",
                file_path="src/mod.py",
                line_number=2,
            )

    @pytest.mark.asyncio
    async def test_lookup_ambiguous_match(self) -> None:
        from investigation_agent_platform.infrastructure.topology.in_memory import (
            InMemoryTopologyAdapter,
        )

        adapter = InMemoryTopologyAdapter()
        n1 = _node(qualified_name="mod.a", name="a")
        n2 = _node(qualified_name="mod.b", name="b")
        await adapter.ingest(_payload(ast_nodes=[n1, n2]))
        with pytest.raises(TopologyAmbiguousMatchError):
            await adapter.resolve_source_location(
                tenant_id="tenant-a",
                application_id="app-1",
                investigation_id=uuid.uuid4(),
                repository_id="repo-1",
                revision="abc123",
                file_path="src/mod.py",
                line_number=3,
            )

    @pytest.mark.asyncio
    async def test_lookup_unready_snapshot(self) -> None:
        from investigation_agent_platform.infrastructure.topology.in_memory import (
            InMemoryTopologyAdapter,
        )

        adapter = InMemoryTopologyAdapter()
        adapter._snapshots[("tenant-a", "repo-1", "abc123")] = {
            "snapshot_id": uuid.uuid4(),
            "status": "INGESTING",
            "payload_hash": "h",
        }
        with pytest.raises(TopologySnapshotNotReadyError):
            await adapter.resolve_source_location(
                tenant_id="tenant-a",
                application_id="app-1",
                investigation_id=uuid.uuid4(),
                repository_id="repo-1",
                revision="abc123",
                file_path="src/mod.py",
                line_number=2,
            )

    @pytest.mark.asyncio
    async def test_lookup_falls_back_to_repository(self) -> None:
        from investigation_agent_platform.domain.topology.models import AttributionFallbackLevel
        from investigation_agent_platform.infrastructure.topology.in_memory import (
            InMemoryTopologyAdapter,
        )

        adapter = InMemoryTopologyAdapter()
        await adapter.ingest(_payload())
        result = await adapter.resolve_source_location(
            tenant_id="tenant-a",
            application_id="app-1",
            investigation_id=uuid.uuid4(),
            repository_id="repo-1",
            revision="abc123",
            file_path="src/other.py",
            line_number=1,
        )
        assert result.matched_node_id is None
        assert result.fallback_level == AttributionFallbackLevel.REPOSITORY


# ===========================================================================
# FailureAttributionService policy branches
# ===========================================================================


def _seed_repo(
    adapter, tenant_id="tenant-a", repository_id="repo-1", repo_type=None, ownership=None
):  # type: ignore[no-untyped-def]
    from investigation_agent_platform.domain.topology.models import (
        RepositoryIdentity,
        RepositoryType,
    )

    repo = RepositoryIdentity(
        tenant_id=tenant_id,
        repository_id=repository_id,
        name=repository_id,
        locator=repository_id,
        git_org_id="org-1",
        repository_type=repo_type or RepositoryType.SERVICE,
    )
    adapter._repositories_by_id[(tenant_id, repository_id)] = repo
    if ownership:
        adapter.set_ownership_path(tenant_id, repository_id, ownership)
    return repo


def _frame(repository_id="repo-1", revision="abc123", file_path="src/mod.py", line_number=2):  # type: ignore[no-untyped-def]
    from investigation_agent_platform.domain.topology.models import StackFrameLocation

    return StackFrameLocation(
        repository_id=repository_id, revision=revision, file_path=file_path, line_number=line_number
    )


class TestFailureAttributionService:
    async def _service(self, repo_type, with_caller=True):  # type: ignore[no-untyped-def]
        from investigation_agent_platform.application.topology.attribution import (
            FailureAttributionService,
        )
        from investigation_agent_platform.infrastructure.topology.in_memory import (
            InMemoryTopologyAdapter,
        )

        adapter = InMemoryTopologyAdapter()
        node = _node()
        await adapter.ingest(_payload(ast_nodes=[node]))
        _seed_repo(
            adapter, repository_id="repo-1", repo_type=repo_type, ownership=["org-1", "domain-a"]
        )
        if with_caller:
            _seed_repo(
                adapter,
                repository_id="repo-2",
                repo_type=repo_type,
                ownership=["org-1", "domain-b"],
            )
        service = FailureAttributionService(attribution_port=adapter, evidence_repo=None)
        return service, adapter

    @pytest.mark.asyncio
    async def test_non_library_defect(self) -> None:
        from investigation_agent_platform.domain.topology.models import (
            AttributionClassification,
            RepositoryType,
        )

        service, _ = await self._service(RepositoryType.SERVICE, with_caller=False)
        result = await service.attribute_failure(
            tenant_id="tenant-a",
            application_id="app-1",
            investigation_id=uuid.uuid4(),
            failure_frame=_frame(),
            caller_frame=None,
        )
        assert result.classification == AttributionClassification.NON_LIBRARY_DEFECT
        assert result.proposed_culprit_domain_id == "domain-a"

    @pytest.mark.asyncio
    async def test_library_defect(self) -> None:
        from investigation_agent_platform.domain.topology.models import (
            AttributionClassification,
            RepositoryType,
        )

        service, _ = await self._service(RepositoryType.SHARED_LIBRARY)
        result = await service.attribute_failure(
            tenant_id="tenant-a",
            application_id="app-1",
            investigation_id=uuid.uuid4(),
            failure_frame=_frame(),
            caller_frame=_frame(repository_id="repo-1"),
            caller_input_violates_contract=False,
        )
        assert result.classification == AttributionClassification.LIBRARY_DEFECT

    @pytest.mark.asyncio
    async def test_caller_misuse(self) -> None:
        from investigation_agent_platform.domain.topology.models import (
            AttributionClassification,
            RepositoryType,
        )

        service, _ = await self._service(RepositoryType.SHARED_LIBRARY)
        result = await service.attribute_failure(
            tenant_id="tenant-a",
            application_id="app-1",
            investigation_id=uuid.uuid4(),
            failure_frame=_frame(),
            caller_frame=_frame(repository_id="repo-1"),
            caller_input_violates_contract=True,
        )
        assert result.classification == AttributionClassification.CALLER_MISUSE

    @pytest.mark.asyncio
    async def test_inconclusive_without_runtime_signal(self) -> None:
        from investigation_agent_platform.domain.topology.models import (
            AttributionClassification,
            RepositoryType,
        )

        service, _ = await self._service(RepositoryType.SHARED_LIBRARY)
        result = await service.attribute_failure(
            tenant_id="tenant-a",
            application_id="app-1",
            investigation_id=uuid.uuid4(),
            failure_frame=_frame(),
            caller_frame=_frame(repository_id="repo-1"),
            caller_input_violates_contract=None,
        )
        assert result.classification == AttributionClassification.INCONCLUSIVE
        assert result.confidence == 0.0

    @pytest.mark.asyncio
    async def test_inconclusive_without_caller_frame(self) -> None:
        from investigation_agent_platform.domain.topology.models import (
            AttributionClassification,
            RepositoryType,
        )

        service, _ = await self._service(RepositoryType.SHARED_LIBRARY, with_caller=False)
        result = await service.attribute_failure(
            tenant_id="tenant-a",
            application_id="app-1",
            investigation_id=uuid.uuid4(),
            failure_frame=_frame(),
            caller_frame=None,
        )
        assert result.classification == AttributionClassification.INCONCLUSIVE

    @pytest.mark.asyncio
    async def test_result_persisted_as_evidence(self) -> None:
        from investigation_agent_platform.application.topology.attribution import (
            FailureAttributionService,
        )
        from investigation_agent_platform.domain.topology.models import RepositoryType
        from investigation_agent_platform.infrastructure.topology.in_memory import (
            InMemoryTopologyAdapter,
        )

        adapter = InMemoryTopologyAdapter()
        await adapter.ingest(_payload(ast_nodes=[_node()]))
        _seed_repo(adapter, repo_type=RepositoryType.SERVICE, ownership=["org-1", "domain-a"])
        evidence_repo = MagicMock()
        evidence_repo.save = AsyncMock(return_value=None)
        service = FailureAttributionService(attribution_port=adapter, evidence_repo=evidence_repo)
        inv_id = uuid.uuid4()
        await service.attribute_failure(
            tenant_id="tenant-a",
            application_id="app-1",
            investigation_id=inv_id,
            failure_frame=_frame(),
            caller_frame=None,
        )
        evidence_repo.save.assert_awaited_once()
        assert evidence_repo.save.await_args.args[2] == inv_id
        evidence = evidence_repo.save.await_args.args[1]
        assert evidence.provenance.tenant_id == "tenant-a"
        assert evidence.provenance.investigation_id == inv_id
        assert evidence.fingerprint


# ===========================================================================
# Profile-backed repository registry
# ===========================================================================


class TestProfileBackedRepositoryRegistry:
    @pytest.mark.asyncio
    async def test_resolves_linked_repository(self) -> None:
        from investigation_agent_platform.infrastructure.topology.profile_repository_registry import (
            ProfileBackedRepositoryRegistry,
        )

        profile_repo = MagicMock()
        profile = MagicMock()
        profile.code_configuration.repository = "org/svc"
        profile.code_configuration.repository_id = "repo-1"
        profile.code_configuration.default_branch = "main"
        profile_repo.get_by_application_id = AsyncMock(return_value=profile)
        registry = ProfileBackedRepositoryRegistry(profile_repo)
        repo = await registry.resolve_for_application("tenant-a", "app-1")
        assert repo is not None
        assert repo.repository_id == "repo-1"
        assert repo.tenant_id == "tenant-a"

    @pytest.mark.asyncio
    async def test_unlinked_profile_returns_none(self) -> None:
        from investigation_agent_platform.infrastructure.topology.profile_repository_registry import (
            ProfileBackedRepositoryRegistry,
        )

        profile_repo = MagicMock()
        profile = MagicMock()
        profile.code_configuration.repository = "org/svc"
        profile.code_configuration.repository_id = None
        profile.code_configuration.default_branch = "main"
        profile_repo.get_by_application_id = AsyncMock(return_value=profile)
        registry = ProfileBackedRepositoryRegistry(profile_repo)
        assert await registry.resolve_for_application("tenant-a", "app-1") is None

    @pytest.mark.asyncio
    async def test_missing_profile_returns_none(self) -> None:
        from investigation_agent_platform.infrastructure.topology.profile_repository_registry import (
            ProfileBackedRepositoryRegistry,
        )

        profile_repo = MagicMock()
        profile_repo.get_by_application_id = AsyncMock(return_value=None)
        registry = ProfileBackedRepositoryRegistry(profile_repo)
        assert await registry.resolve_for_application("tenant-a", "nope") is None


# ===========================================================================
# Evidence gateway integration
# ===========================================================================


class TestGatewayTopologyIntegration:
    def _gateway(self, **overrides):  # type: ignore[no-untyped-def]
        from investigation_agent_platform.application.evidence.gateway import AsyncEvidenceGateway

        kwargs = {
            "provider_selector": MagicMock(),
            "query_safety_policy": MagicMock(),
            "sanitizer": MagicMock(),
        }
        kwargs.update(overrides)
        return AsyncEvidenceGateway(**kwargs)

    @pytest.mark.asyncio
    async def test_unwired_gateway_raises_not_configured(self) -> None:
        gateway = self._gateway()
        with pytest.raises(TopologyNotConfiguredError):
            await gateway.resolve_code_ownership(
                tenant_id="tenant-a",
                investigation_id=uuid.uuid4(),
                application_id="app-1",
                file_path="a.py",
                line_number=1,
            )
        with pytest.raises(TopologyNotConfiguredError):
            await gateway.attribute_failure_frames(
                tenant_id="tenant-a",
                investigation_id=uuid.uuid4(),
                application_id="app-1",
                failure_frame=_frame(),
            )

    @pytest.mark.asyncio
    async def test_wired_gateway_delegates(self) -> None:
        from investigation_agent_platform.infrastructure.topology.in_memory import (
            InMemoryTopologyAdapter,
        )

        adapter = InMemoryTopologyAdapter()
        await adapter.ingest(_payload(ast_nodes=[_node()]))
        _seed_repo(adapter, ownership=["org-1", "domain-a"])
        registry = MagicMock()
        from investigation_agent_platform.domain.topology.models import (
            RepositoryIdentity,
            RepositoryType,
        )

        registry.resolve_for_application = AsyncMock(
            return_value=RepositoryIdentity(
                tenant_id="tenant-a",
                repository_id="repo-1",
                name="repo-1",
                locator="repo-1",
                git_org_id="org-1",
                repository_type=RepositoryType.SERVICE,
            )
        )
        from investigation_agent_platform.application.topology.attribution import (
            FailureAttributionService,
        )

        service = FailureAttributionService(attribution_port=adapter, evidence_repo=None)
        gateway = self._gateway(repository_registry=registry, failure_attribution_service=service)
        result = await gateway.resolve_code_ownership(
            tenant_id="tenant-a",
            investigation_id=uuid.uuid4(),
            application_id="app-1",
            file_path="src/mod.py",
            line_number=2,
            revision="abc123",
        )
        assert result.matched_file_path == "src/mod.py"


# ===========================================================================
# Neo4j adapter failure classification (mocked driver — no live Neo4j needed)
# ===========================================================================


class TestNeo4jAdapter:
    def _adapter(self):  # type: ignore[no-untyped-def]
        from investigation_agent_platform.infrastructure.configuration.config import TopologyConfig
        from investigation_agent_platform.infrastructure.topology.neo4j_adapter import (
            Neo4jTopologyAdapter,
        )

        return Neo4jTopologyAdapter(TopologyConfig(enabled=True))

    @pytest.mark.asyncio
    async def test_operations_require_connection(self) -> None:
        from investigation_agent_platform.domain.common.exceptions import (
            TopologyProviderUnavailableError,
        )

        adapter = self._adapter()
        assert await adapter.health_check() is False
        with pytest.raises(TopologyProviderUnavailableError):
            await adapter.resolve_source_location(
                tenant_id="t",
                application_id="a",
                investigation_id=uuid.uuid4(),
                repository_id="r",
                revision="rev",
                file_path="a.py",
                line_number=1,
            )

    @pytest.mark.asyncio
    async def test_transient_error_classification(self) -> None:
        from investigation_agent_platform.infrastructure.topology.neo4j_adapter import (
            _is_transient_neo4j_error,
        )

        transient = MagicMock()
        transient.code = "Neo.TransientError.Transaction.DeadlockDetected"
        assert _is_transient_neo4j_error(transient) is True
        fatal = MagicMock()
        fatal.code = "Neo.ClientError.Schema.ConstraintValidationFailed"
        assert _is_transient_neo4j_error(fatal) is False
        assert _is_transient_neo4j_error(RuntimeError("no code")) is False

    @pytest.mark.asyncio
    async def test_ingest_marks_failed_on_generic_error(self) -> None:

        adapter = self._adapter()
        session = AsyncMock()
        session.execute_write = AsyncMock(side_effect=RuntimeError("boom"))
        session_context = MagicMock()
        session_context.__aenter__ = AsyncMock(return_value=session)
        session_context.__aexit__ = AsyncMock(return_value=None)
        driver = MagicMock()
        driver.session = MagicMock(return_value=session_context)
        adapter._driver = driver
        result = await adapter.ingest(_payload())
        assert result.status.value == "FAILED"
        assert result.error_summary is not None


# ===========================================================================
# Workflow investigation-identity invariant (hard prerequisite regression)
# ===========================================================================


class TestWorkflowInvestigationIdentity:
    @pytest.mark.asyncio
    async def test_activity_loads_existing_investigation(self) -> None:
        from investigation_agent_platform.application.worker.activities import (
            CreateInvestigationInput,
            create_investigation_activity,
        )
        from investigation_agent_platform.domain.investigation.models import InvestigationRequest

        ctx = AppContext()
        set_app_context(ctx)
        req = InvestigationRequest(
            application_id="example-app",
            problem_description="identity check",
            session_id="sess-identity",
            requested_by="tester",
        )
        created = await ctx.create_investigation_service().execute(req, tenant_id="tenant-a")

        out = await create_investigation_activity(
            CreateInvestigationInput(
                application_id="example-app",
                session_id="sess-identity",
                tenant_id="tenant-a",
                description="identity check",
                investigation_id=str(created.id),
            )
        )
        # Same aggregate — not a second investigation under a new ID.
        assert out.investigation_id == str(created.id)
        assert out.status == created.status.value

    @pytest.mark.asyncio
    async def test_activity_rejects_unknown_investigation(self) -> None:
        try:
            from temporalio.exceptions import ApplicationFailure  # type: ignore[attr-defined]
        except ImportError:
            from temporalio.exceptions import (
                ApplicationError as ApplicationFailure,  # type: ignore[assignment,no-redef]
            )

        from investigation_agent_platform.application.worker.activities import (
            CreateInvestigationInput,
            create_investigation_activity,
        )

        ctx = AppContext()
        set_app_context(ctx)
        with pytest.raises(ApplicationFailure) as exc_info:
            await create_investigation_activity(
                CreateInvestigationInput(
                    application_id="example-app",
                    tenant_id="tenant-a",
                    investigation_id=str(uuid.uuid4()),
                )
            )
        assert exc_info.value.type == "InvestigationNotFoundError"

    def test_workflow_input_carries_investigation_id(self) -> None:
        from investigation_agent_platform.application.worker.workflows import RunInvestigationInput

        wf_input = RunInvestigationInput(
            application_id="example-app", tenant_id="tenant-a", investigation_id="inv-123"
        )
        assert wf_input.investigation_id == "inv-123"


# ===========================================================================
# ISSUE-3: macro/micro split
# ===========================================================================


class TestMacroMicroSplit:
    def test_macro_mapping_contents(self) -> None:
        from investigation_agent_platform.domain.topology.models import (
            MACRO_NODE_TYPES,
            TopologyNodeType,
        )

        assert TopologyNodeType.FIELD not in MACRO_NODE_TYPES
        for member in (
            TopologyNodeType.MODULE,
            TopologyNodeType.CLASS,
            TopologyNodeType.INTERFACE,
            TopologyNodeType.METHOD,
            TopologyNodeType.FUNCTION,
            TopologyNodeType.CONSTRUCTOR,
            TopologyNodeType.ENUM,
            TopologyNodeType.ROUTE,
            TopologyNodeType.MESSAGE_HANDLER,
        ):
            assert member in MACRO_NODE_TYPES

    def test_granularity_derived_from_type(self) -> None:
        from investigation_agent_platform.domain.topology.models import (
            ASTNodeIdentity,
            TopologyNodeType,
        )

        assert (
            ASTNodeIdentity.create(
                tenant_id="t",
                repository_id="r",
                revision="rev",
                name="x",
                qualified_name="x",
                node_type=TopologyNodeType.FIELD,
                file_path="a.py",
                start_line=1,
                end_line=1,
            ).granularity
            == "micro"
        )
        assert (
            ASTNodeIdentity.create(
                tenant_id="t",
                repository_id="r",
                revision="rev",
                name="x",
                qualified_name="x",
                node_type=TopologyNodeType.FUNCTION,
                file_path="a.py",
                start_line=1,
                end_line=5,
            ).granularity
            == "macro"
        )

    def test_granularity_mismatch_corrected(self) -> None:
        from investigation_agent_platform.domain.topology.models import (
            ASTNodeIdentity,
            TopologyNodeType,
        )

        # Explicit wrong value at construction is corrected to the mapping.
        node = ASTNodeIdentity(
            tenant_id="t",
            repository_id="r",
            revision="rev",
            node_id=uuid.uuid4(),
            name="x",
            qualified_name="x",
            node_type=TopologyNodeType.FIELD,
            file_path="a.py",
            start_line=1,
            end_line=1,
            granularity="macro",
        )
        assert node.granularity == "micro"

    @pytest.mark.asyncio
    async def test_ingest_filters_micro_nodes(self) -> None:
        from investigation_agent_platform.domain.topology.models import (
            ASTNodeIdentity,
            TopologyNodeType,
        )
        from investigation_agent_platform.infrastructure.topology.in_memory import (
            InMemoryTopologyAdapter,
        )

        macro = _node()
        micro = ASTNodeIdentity.create(
            tenant_id="tenant-a",
            repository_id="repo-1",
            revision="abc123",
            name="tmp",
            qualified_name="mod.foo.tmp",
            node_type=TopologyNodeType.FIELD,
            file_path="src/mod.py",
            start_line=2,
            end_line=2,
        )
        adapter = InMemoryTopologyAdapter()
        result = await adapter.ingest(_payload(ast_nodes=[macro, micro]))
        assert result.status.value == "READY"
        assert result.node_count == 1
        assert result.micro_skipped_count == 1

    @pytest.mark.asyncio
    async def test_ingest_skips_micro_edges(self) -> None:
        from investigation_agent_platform.domain.topology.models import (
            ASTNodeIdentity,
            CallEdgeInput,
            CallResolutionStatus,
            TopologyNodeType,
        )
        from investigation_agent_platform.infrastructure.topology.in_memory import (
            InMemoryTopologyAdapter,
        )

        macro = _node()
        micro = ASTNodeIdentity.create(
            tenant_id="tenant-a",
            repository_id="repo-1",
            revision="abc123",
            name="tmp",
            qualified_name="mod.foo.tmp",
            node_type=TopologyNodeType.FIELD,
            file_path="src/mod.py",
            start_line=2,
            end_line=2,
        )
        adapter = InMemoryTopologyAdapter()
        # Edge from a micro node: skipped, ingestion still succeeds.
        # (Genuinely dangling callers are rejected earlier by payload
        # validation, so the adapter only ever sees this case.)
        result = await adapter.ingest(
            _payload(
                ast_nodes=[macro, micro],
                calls=[
                    CallEdgeInput(
                        caller_node_id=micro.node_id,
                        callee_node_id=macro.node_id,
                        call_site_file="src/mod.py",
                        call_site_line=2,
                        resolution_status=CallResolutionStatus.RESOLVED,
                    )
                ],
            )
        )
        assert result.status.value == "READY"
        assert result.micro_skipped_count == 2  # 1 node + 1 edge

    @pytest.mark.asyncio
    async def test_neo4j_ingest_filters_and_marks_granularity(self) -> None:
        from investigation_agent_platform.domain.topology.models import (
            ASTNodeIdentity,
            TopologyNodeType,
        )

        adapter = TestNeo4jAdapter()._adapter()
        recorded: list[tuple[str, dict]] = []

        async def fake_run(query, **kwargs):  # type: ignore[no-untyped-def]
            recorded.append((query, kwargs))
            result = MagicMock()
            result.single = AsyncMock(return_value=None)
            return result

        rec_tx = MagicMock()
        rec_tx.run = fake_run

        async def fake_execute_write(fn, *args):  # type: ignore[no-untyped-def]
            return await fn(rec_tx, *args)

        session = AsyncMock()
        session.execute_read = AsyncMock(return_value=None)
        session.execute_write = fake_execute_write
        session_context = MagicMock()
        session_context.__aenter__ = AsyncMock(return_value=session)
        session_context.__aexit__ = AsyncMock(return_value=None)
        driver = MagicMock()
        driver.session = MagicMock(return_value=session_context)
        adapter._driver = driver

        micro = ASTNodeIdentity.create(
            tenant_id="tenant-a",
            repository_id="repo-1",
            revision="abc123",
            name="tmp",
            qualified_name="mod.foo.tmp",
            node_type=TopologyNodeType.FIELD,
            file_path="src/mod.py",
            start_line=2,
            end_line=2,
        )
        result = await adapter.ingest(_payload(ast_nodes=[_node(), micro]))
        assert result.status.value == "READY"
        assert result.node_count == 1
        assert result.micro_skipped_count == 1
        node_rows = [
            row
            for query, kwargs in recorded
            if "MERGE (n:ASTNode" in query
            for row in kwargs["rows"]
        ]
        assert node_rows and all(r["granularity"] == "macro" for r in node_rows)


class TestMicroResolver:
    def _resolver(self, tmp_path, **kwargs):  # type: ignore[no-untyped-def]
        from investigation_agent_platform.infrastructure.evidence.code.micro import (
            MicroSymbolResolver,
        )

        return MicroSymbolResolver(repo_base_path=str(tmp_path), **kwargs)

    @pytest.mark.asyncio
    async def test_resolves_innermost_symbol(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        (tmp_path / "mod.py").write_text(
            "def outer():\n    def inner():\n        return 1\n    return inner()\n",
            encoding="utf-8",
        )
        resolver = self._resolver(tmp_path)
        match = await resolver.resolve_micro_symbol(
            tenant_id="tenant-a",
            repository_id="repo-1",
            locator=".",
            revision="abc1234",
            file_path="mod.py",
            line_number=3,
        )
        assert match is not None
        assert match.node.name == "inner"
        assert match.node.tenant_id == "tenant-a"
        assert match.node.repository_id == "repo-1"
        assert match.node.granularity in ("macro", "micro")

    @pytest.mark.asyncio
    async def test_tenant_denied_with_allowlist(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        from investigation_agent_platform.domain.common.exceptions import (
            SecurityPolicyViolationException,
        )

        (tmp_path / "mod.py").write_text("x = 1\n", encoding="utf-8")
        resolver = self._resolver(tmp_path, tenant_allowlist={"tenant-a": {"other-repo"}})
        with pytest.raises(SecurityPolicyViolationException):
            await resolver.resolve_micro_symbol(
                tenant_id="tenant-a",
                repository_id="repo-1",
                locator="myrepo",
                revision="abc1234",
                file_path="mod.py",
                line_number=1,
            )

    @pytest.mark.asyncio
    async def test_traversal_rejected(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        resolver = self._resolver(tmp_path)
        assert (
            await resolver.resolve_micro_symbol(
                tenant_id="tenant-a",
                repository_id="repo-1",
                locator=".",
                revision="abc1234",
                file_path="../escape.py",
                line_number=1,
            )
            is None
        )

    @pytest.mark.asyncio
    async def test_missing_or_oversize_file_returns_none(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        import investigation_agent_platform.infrastructure.evidence.code.micro as micro_mod

        resolver = self._resolver(tmp_path)
        assert (
            await resolver.resolve_micro_symbol(
                tenant_id="tenant-a",
                repository_id="repo-1",
                locator=".",
                revision="abc1234",
                file_path="nope.py",
                line_number=1,
            )
            is None
        )
        (tmp_path / "big.py").write_text("x = 1\n", encoding="utf-8")
        original = micro_mod.MAX_FILE_BYTES
        micro_mod.MAX_FILE_BYTES = 4
        try:
            assert (
                await resolver.resolve_micro_symbol(
                    tenant_id="tenant-a",
                    repository_id="repo-1",
                    locator=".",
                    revision="abc1234",
                    file_path="big.py",
                    line_number=1,
                )
                is None
            )
        finally:
            micro_mod.MAX_FILE_BYTES = original

    @pytest.mark.asyncio
    async def test_revision_unverified_without_git(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        (tmp_path / "mod.py").write_text("def foo():\n    return 1\n", encoding="utf-8")
        resolver = self._resolver(tmp_path)
        match = await resolver.resolve_micro_symbol(
            tenant_id="tenant-a",
            repository_id="repo-1",
            locator=".",
            revision="abc1234",
            file_path="mod.py",
            line_number=1,
        )
        assert match is not None
        assert match.revision_verified is False

    def test_verify_revision_true_in_git_repo(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        import subprocess

        resolver = self._resolver(tmp_path)
        try:
            subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, timeout=15)
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(tmp_path),
                    "-c",
                    "user.name=t",
                    "-c",
                    "user.email=t@e",
                    "commit",
                    "-q",
                    "--allow-empty",
                    "-m",
                    "init",
                ],
                check=True,
                timeout=15,
            )
            head = (
                subprocess.run(
                    ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
                    capture_output=True,
                    check=True,
                    timeout=15,
                )
                .stdout.decode()
                .strip()
            )
        except Exception:
            pytest.skip("git unavailable for revision verification test")
        assert resolver._verify_revision(tmp_path, head) is True
        assert resolver._verify_revision(tmp_path, "deadbee" * 5) is False


# ===========================================================================
# ISSUE-6: CODEOWNERS tier + graceful hierarchy
# ===========================================================================


class TestCodeownersResolver:
    def _resolver(self, tmp_path, **kwargs):  # type: ignore[no-untyped-def]
        from investigation_agent_platform.infrastructure.evidence.code.codeowners import (
            CodeownersResolver,
        )

        return CodeownersResolver(repo_base_path=str(tmp_path), **kwargs)

    def test_parse_last_match_wins(self) -> None:
        from investigation_agent_platform.infrastructure.evidence.code.codeowners import (
            match_owner,
            parse_codeowners,
        )

        rules = parse_codeowners("# comment\n\n*.py @team-a\nsrc/payments/ @team-pay @team-lead\n")
        assert match_owner(rules, "src/other/x.py") == "team-a"
        assert match_owner(rules, "src/payments/svc.py") == "team-lead"
        assert match_owner(rules, "README.md") is None

    def test_double_star_and_question_mark(self) -> None:
        from investigation_agent_platform.infrastructure.evidence.code.codeowners import (
            match_owner,
            parse_codeowners,
        )

        rules = parse_codeowners("**/migrations/*.py @team-db\ndocs/?eadme.md @team-docs\n")
        assert match_owner(rules, "a/b/migrations/001.py") == "team-db"
        assert match_owner(rules, "docs/readme.md") == "team-docs"
        assert match_owner(rules, "docs/sub/readme.md") is None

    @pytest.mark.asyncio
    async def test_resolve_from_repo_file(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        (tmp_path / "myrepo").mkdir()
        (tmp_path / "myrepo" / ".github").mkdir()
        (tmp_path / "myrepo" / ".github" / "CODEOWNERS").write_text(
            "*.py @team-a\n", encoding="utf-8"
        )
        (tmp_path / "myrepo" / "svc.py").write_text("x = 1\n", encoding="utf-8")
        resolver = self._resolver(tmp_path)
        assert await resolver.resolve_owner("tenant-a", "myrepo", "svc.py") == "team-a"

    @pytest.mark.asyncio
    async def test_tenant_denied_and_traversal_rejected(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        from investigation_agent_platform.domain.common.exceptions import (
            SecurityPolicyViolationException,
        )

        resolver = self._resolver(tmp_path, tenant_allowlist={"tenant-a": {"other"}})
        with pytest.raises(SecurityPolicyViolationException):
            await resolver.resolve_owner("tenant-a", "myrepo", "svc.py")
        resolver2 = self._resolver(tmp_path)
        assert await resolver2.resolve_owner("tenant-a", ".", "../x.py") is None
        with pytest.raises(SecurityPolicyViolationException):
            await resolver2.resolve_owner("anonymous", ".", "svc.py")

    @pytest.mark.asyncio
    async def test_missing_file_returns_none(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        resolver = self._resolver(tmp_path)
        assert await resolver.resolve_owner("tenant-a", ".", "svc.py") is None


class TestGracefulFallbackChain:
    async def _service_with_tiers(self, tmp_path, repo_type, ownership=None):  # type: ignore[no-untyped-def]
        from investigation_agent_platform.application.topology.attribution import (
            FailureAttributionService,
        )
        from investigation_agent_platform.domain.topology.models import (
            RepositoryIdentity,
        )
        from investigation_agent_platform.infrastructure.evidence.code.codeowners import (
            CodeownersResolver,
        )
        from investigation_agent_platform.infrastructure.evidence.code.micro import (
            MicroSymbolResolver,
        )
        from investigation_agent_platform.infrastructure.topology.in_memory import (
            InMemoryTopologyAdapter,
        )

        adapter = InMemoryTopologyAdapter()
        await adapter.ingest(_payload(ast_nodes=[_node()]))
        _seed_repo(
            adapter,
            repository_id="repo-1",
            repo_type=repo_type,
            ownership=ownership,
        )
        registry = MagicMock()
        registry.resolve_for_application = AsyncMock(
            return_value=RepositoryIdentity(
                tenant_id="tenant-a",
                repository_id="repo-1",
                name="myrepo",
                locator="myrepo",
                git_org_id="org-1",
                repository_type=repo_type,
            )
        )
        codeowners = CodeownersResolver(repo_base_path=str(tmp_path))
        micro = MicroSymbolResolver(repo_base_path=str(tmp_path))
        service = FailureAttributionService(
            attribution_port=adapter,
            evidence_repo=None,
            repository_registry=registry,
            codeowners_resolver=codeowners,
            micro_resolver=micro,
        )
        return service

    def _write_repo(self, tmp_path, codeowners=None, code=None):  # type: ignore[no-untyped-def]
        repo = tmp_path / "myrepo"
        repo.mkdir(exist_ok=True)
        if code is not None:
            (repo / "svc.py").write_text(code, encoding="utf-8")
        if codeowners is not None:
            (repo / "CODEOWNERS").write_text(codeowners, encoding="utf-8")
        return repo

    @pytest.mark.asyncio
    async def test_tier_walk_repo_fallback_to_codeowners(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        from investigation_agent_platform.domain.topology.models import (
            AttributionClassification,
            AttributionFallbackLevel,
            RepositoryType,
        )

        # No AST node matches other.py and no repo ownership mapping ->
        # CODEOWNERS tier names the team with capped confidence.
        self._write_repo(tmp_path, codeowners="*.py @team-pay\n", code="x = 1\n")
        service = await self._service_with_tiers(tmp_path, RepositoryType.SERVICE, ownership=None)
        result = await service.attribute_failure(
            tenant_id="tenant-a",
            application_id="app-1",
            investigation_id=uuid.uuid4(),
            failure_frame=_frame(file_path="other.py", line_number=1),
            caller_frame=None,
        )
        assert result.classification == AttributionClassification.NON_LIBRARY_DEFECT
        assert result.proposed_culprit_domain_id == "team-pay"
        assert result.confidence <= 0.3
        assert result.fallback_level == AttributionFallbackLevel.CODEOWNERS
        assert result.ownership_source == "codeowners"
        assert any("CODEOWNERS" in limitation for limitation in result.limitations)

    @pytest.mark.asyncio
    async def test_static_owner_wins_over_codeowners(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        from investigation_agent_platform.domain.topology.models import (
            AttributionClassification,
            RepositoryType,
        )

        self._write_repo(tmp_path, codeowners="*.py @team-other\n")
        service = await self._service_with_tiers(
            tmp_path, RepositoryType.SERVICE, ownership=["org-1", "domain-a"]
        )
        result = await service.attribute_failure(
            tenant_id="tenant-a",
            application_id="app-1",
            investigation_id=uuid.uuid4(),
            failure_frame=_frame(),
            caller_frame=None,
        )
        assert result.classification == AttributionClassification.NON_LIBRARY_DEFECT
        assert result.proposed_culprit_domain_id == "domain-a"
        assert result.ownership_source == "static"
        assert result.confidence == 0.7

    @pytest.mark.asyncio
    async def test_micro_tier_precision_and_flag(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        from investigation_agent_platform.domain.topology.models import RepositoryType

        # Empty the macro graph for other.py so the port falls back, then let
        # micro parsing locate the symbol on demand.
        self._write_repo(tmp_path, code="x = 1\n")
        (tmp_path / "myrepo" / "other.py").write_text(
            "def handler():\n    raise ValueError('x')\n", encoding="utf-8"
        )
        service = await self._service_with_tiers(
            tmp_path, RepositoryType.SERVICE, ownership=["org-1", "domain-a"]
        )
        result = await service.attribute_failure(
            tenant_id="tenant-a",
            application_id="app-1",
            investigation_id=uuid.uuid4(),
            failure_frame=_frame(file_path="other.py", line_number=1),
            caller_frame=None,
        )
        assert result.resolution_tier == "micro"
        assert result.matched_file_path == "other.py"
        assert result.proposed_culprit_domain_id == "domain-a"

    @pytest.mark.asyncio
    async def test_shared_lib_ambiguous_keeps_verdict_withheld(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        from investigation_agent_platform.domain.topology.models import (
            AttributionClassification,
            RepositoryType,
        )

        self._write_repo(tmp_path, codeowners="*.py @team-lib\n")
        service = await self._service_with_tiers(
            tmp_path, RepositoryType.SHARED_LIBRARY, ownership=None
        )
        result = await service.attribute_failure(
            tenant_id="tenant-a",
            application_id="app-1",
            investigation_id=uuid.uuid4(),
            failure_frame=_frame(),
            caller_frame=None,
            caller_input_violates_contract=None,
        )
        # Documented deviation: verdict withheld (defect vs misuse unknowable),
        # but the CODEOWNERS owner is preserved as a lead.
        assert result.classification == AttributionClassification.INCONCLUSIVE
        assert result.confidence == 0.0
        assert "team-lib" in result.alternatives
        assert any("CODEOWNERS" in limitation for limitation in result.limitations)

    @pytest.mark.asyncio
    async def test_macro_cap_blocks_conclusion_gate(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        from investigation_agent_platform.application.investigation.verification import (
            ConclusionGate,
            InvestigationConfidence,
            RootCauseVerificationPolicy,
            VerificationResult,
            VerificationStatus,
        )
        from investigation_agent_platform.domain.topology.models import (
            RepositoryType,
        )

        self._write_repo(tmp_path, codeowners="*.py @team-pay\n")
        service = await self._service_with_tiers(tmp_path, RepositoryType.SERVICE, ownership=None)
        result = await service.attribute_failure(
            tenant_id="tenant-a",
            application_id="app-1",
            investigation_id=uuid.uuid4(),
            failure_frame=_frame(file_path="other.py", line_number=1),
            caller_frame=None,
        )
        assert result.confidence <= 0.3
        gate = ConclusionGate()
        verification = VerificationResult(
            hypothesis_id=uuid.uuid4(),
            status=VerificationStatus.VERIFIED,
            corroborating_evidence_ids=[],
            explanation="macro-attributed only",
            confidence=InvestigationConfidence(
                coverage_score=result.confidence,
                reliability_score=result.confidence,
                causal_score=result.confidence,
                contradiction_penalty=0.0,
            ),
        )
        decision = await gate.evaluate(
            tenant_id="tenant-a",
            investigation_id=uuid.uuid4(),
            verification=verification,
            contradictions=[],
            causal_chain=[],
            evidence_items=[],
            policy=RootCauseVerificationPolicy(),
        )
        assert decision.approved is False

    @pytest.mark.asyncio
    async def test_rule_version_bumped(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        from investigation_agent_platform.domain.topology.models import RepositoryType

        service = await self._service_with_tiers(
            tmp_path, RepositoryType.SERVICE, ownership=["org-1", "domain-a"]
        )
        result = await service.attribute_failure(
            tenant_id="tenant-a",
            application_id="app-1",
            investigation_id=uuid.uuid4(),
            failure_frame=_frame(),
            caller_frame=None,
        )
        assert result.attribution_rule_version == "v2"


# ===========================================================================
# ISSUE-7: Neo4j domain ownership (git org + CODEOWNERS)
# ===========================================================================


class TestOwnershipDerivation:
    def test_derive_git_org_from_https_url(self) -> None:
        from investigation_agent_platform.infrastructure.topology.ownership import (
            derive_git_organization,
        )

        org = derive_git_organization("tenant-a", "https://github.com/acme/checkout-service.git")
        assert org is not None
        assert org.git_org_id == "github:acme"
        assert org.name == "acme"
        assert org.provider == "github"
        assert org.domain_id is None

    def test_derive_git_org_from_scp_locator(self) -> None:
        from investigation_agent_platform.infrastructure.topology.ownership import (
            derive_git_organization,
        )

        org = derive_git_organization("tenant-a", "git@github.com:acme/checkout-service.git")
        assert org is not None
        assert org.git_org_id == "github:acme"

    def test_derive_git_org_from_bare_org_repo(self) -> None:
        from investigation_agent_platform.infrastructure.topology.ownership import (
            derive_git_organization,
        )

        org = derive_git_organization("tenant-a", "acme/checkout-service")
        assert org is not None
        assert org.name == "acme"
        assert org.provider == "git"

    def test_derive_git_org_rejects_filesystem_paths(self) -> None:
        from investigation_agent_platform.infrastructure.topology.ownership import (
            derive_git_organization,
        )

        assert derive_git_organization("tenant-a", "/repos/checkout-service") is None
        assert derive_git_organization("tenant-a", "~/repos/checkout-service") is None
        assert derive_git_organization("tenant-a", "checkout-service") is None
        assert derive_git_organization("tenant-a", "") is None

    def test_derive_repository_domain_from_codeowners_catchall(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        from investigation_agent_platform.infrastructure.topology.ownership import (
            derive_repository_domain,
        )

        (tmp_path / "CODEOWNERS").write_text(
            "* @team-platform\n/payments/ @team-payments\n", encoding="utf-8"
        )
        assert derive_repository_domain(tmp_path) == "team-platform"

    def test_derive_repository_domain_none_without_catchall(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        from investigation_agent_platform.infrastructure.topology.ownership import (
            derive_repository_domain,
        )

        (tmp_path / "CODEOWNERS").write_text("/payments/ @team-payments\n", encoding="utf-8")
        assert derive_repository_domain(tmp_path) is None

    def test_derive_repository_domain_none_without_codeowners_file(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        from investigation_agent_platform.infrastructure.topology.ownership import (
            derive_repository_domain,
        )

        assert derive_repository_domain(tmp_path) is None


class TestNeo4jOwnershipWrite:
    def _adapter(self):  # type: ignore[no-untyped-def]
        from investigation_agent_platform.infrastructure.configuration.config import TopologyConfig
        from investigation_agent_platform.infrastructure.topology.neo4j_adapter import (
            Neo4jTopologyAdapter,
        )

        return Neo4jTopologyAdapter(TopologyConfig(enabled=True))

    def _mock_driver(self, session: AsyncMock) -> MagicMock:
        session_context = MagicMock()
        session_context.__aenter__ = AsyncMock(return_value=session)
        session_context.__aexit__ = AsyncMock(return_value=None)
        driver = MagicMock()
        driver.session = MagicMock(return_value=session_context)
        return driver

    @pytest.mark.asyncio
    async def test_register_ownership_writes_repository_org_and_domain(self) -> None:
        from investigation_agent_platform.domain.topology.models import (
            GitOrganizationIdentity,
            RepositoryIdentity,
            RepositoryType,
        )

        adapter = self._adapter()
        session = AsyncMock()
        session.execute_write = AsyncMock(side_effect=lambda fn, *a: fn(AsyncMock(), *a))
        adapter._driver = self._mock_driver(session)

        repository = RepositoryIdentity(
            tenant_id="tenant-a",
            repository_id="repo-1",
            name="checkout-service",
            locator="https://github.com/acme/checkout-service",
            git_org_id="github:acme",
            repository_type=RepositoryType.SERVICE,
        )
        git_org = GitOrganizationIdentity(
            tenant_id="tenant-a", git_org_id="github:acme", name="acme", provider="github"
        )
        await adapter.register_ownership(repository, git_org, "team-platform")
        session.execute_write.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_register_ownership_no_org_no_domain_still_registers_repo(self) -> None:
        from investigation_agent_platform.domain.topology.models import (
            RepositoryIdentity,
            RepositoryType,
        )

        adapter = self._adapter()
        session = AsyncMock()
        write_calls: list[object] = []

        async def _capture(fn, *a):  # type: ignore[no-untyped-def]
            tx = AsyncMock()
            write_calls.append((fn, a))
            return await fn(tx, *a)

        session.execute_write = AsyncMock(side_effect=_capture)
        adapter._driver = self._mock_driver(session)

        repository = RepositoryIdentity(
            tenant_id="tenant-a",
            repository_id="repo-1",
            name="checkout-service",
            locator="/repos/checkout-service",
            git_org_id="tenant-a:unassigned",
            repository_type=RepositoryType.UNKNOWN,
        )
        await adapter.register_ownership(repository, None, None)
        assert len(write_calls) == 1
        # Only the Repository MERGE ran; git_org is None so no further
        # `tx.run` for GitOrganization/Domain should have been reached.
        fn, args = write_calls[0]
        tx = AsyncMock()
        await fn(tx, *args)
        assert tx.run.await_count == 1

    @pytest.mark.asyncio
    async def test_resolve_ownership_path_traverses_repo_org_domain(self) -> None:
        adapter = self._adapter()
        session = AsyncMock()
        result = AsyncMock()
        result.single = AsyncMock(
            return_value={"git_org_id": "github:acme", "domain_id": "team-platform"}
        )
        session.run = AsyncMock(return_value=result)
        adapter._driver = self._mock_driver(session)

        path, domain_id, git_org_id = await adapter._resolve_ownership_path("tenant-a", "repo-1")
        assert git_org_id == "github:acme"
        assert domain_id == "team-platform"
        assert path == ["github:acme", "team-platform"]

    @pytest.mark.asyncio
    async def test_resolve_ownership_path_partial_chain(self) -> None:
        """GitOrganization registered but no Domain link — domain_id stays None."""
        adapter = self._adapter()
        session = AsyncMock()
        result = AsyncMock()
        result.single = AsyncMock(return_value={"git_org_id": "github:acme", "domain_id": None})
        session.run = AsyncMock(return_value=result)
        adapter._driver = self._mock_driver(session)

        path, domain_id, git_org_id = await adapter._resolve_ownership_path("tenant-a", "repo-1")
        assert git_org_id == "github:acme"
        assert domain_id is None
        assert path == ["github:acme"]

    @pytest.mark.asyncio
    async def test_resolve_source_location_no_match_includes_ownership_and_real_snapshot(
        self,
    ) -> None:
        """REPOSITORY fallback (no AST match) must return the real snapshot_id
        and traverse the ownership chain, not nil/empty values."""
        from investigation_agent_platform.domain.topology.models import AttributionFallbackLevel

        adapter = self._adapter()
        snapshot_id = str(uuid.uuid4())

        async def _execute_read(fn, *args):  # type: ignore[no-untyped-def]
            if fn is adapter._read_snapshot:
                return {
                    "status": "READY",
                    "payload_hash": "h1",
                    "snapshot_id": snapshot_id,
                }
            if fn is adapter._match_source_location:
                return []
            raise AssertionError("unexpected execute_read call")

        session = AsyncMock()
        session.execute_read = AsyncMock(side_effect=_execute_read)

        ownership_result = AsyncMock()
        ownership_result.single = AsyncMock(
            return_value={"git_org_id": "github:acme", "domain_id": "team-platform"}
        )
        repo_type_result = AsyncMock()
        repo_type_result.single = AsyncMock(return_value={"repository_type": "SERVICE"})
        session.run = AsyncMock(side_effect=[repo_type_result, ownership_result])
        adapter._driver = self._mock_driver(session)

        result = await adapter.resolve_source_location(
            tenant_id="tenant-a",
            application_id="app-1",
            investigation_id=uuid.uuid4(),
            repository_id="repo-1",
            revision="rev1",
            file_path="a.py",
            line_number=1,
        )
        assert result.fallback_level == AttributionFallbackLevel.REPOSITORY
        assert result.domain_id == "team-platform"
        assert result.git_org_id == "github:acme"
        assert result.ownership_path == ["github:acme", "team-platform"]
        assert result.snapshot_id == uuid.UUID(snapshot_id)


class TestInMemoryOwnershipParity:
    @pytest.mark.asyncio
    async def test_register_ownership_populates_domain_and_git_org(self) -> None:
        from investigation_agent_platform.domain.topology.models import (
            AttributionFallbackLevel,
            GitOrganizationIdentity,
            RepositoryIdentity,
            RepositoryType,
        )
        from investigation_agent_platform.infrastructure.topology.in_memory import (
            InMemoryTopologyAdapter,
        )

        adapter = InMemoryTopologyAdapter()
        repository = RepositoryIdentity(
            tenant_id="tenant-a",
            repository_id="repo-1",
            name="svc",
            locator="acme/svc",
            git_org_id="git:acme",
            repository_type=RepositoryType.SERVICE,
        )
        git_org = GitOrganizationIdentity(
            tenant_id="tenant-a", git_org_id="git:acme", name="acme", provider="git"
        )
        await adapter.register_ownership(repository, git_org, "team-platform")

        payload = _payload(tenant_id="tenant-a", repository_id="repo-1", revision="rev1")
        await adapter.ingest(payload)
        result = await adapter.resolve_source_location(
            tenant_id="tenant-a",
            application_id="app-1",
            investigation_id=uuid.uuid4(),
            repository_id="repo-1",
            revision="rev1",
            file_path="no/such/file.py",
            line_number=1,
        )
        assert result.fallback_level == AttributionFallbackLevel.REPOSITORY
        assert result.git_org_id == "git:acme"
        assert result.domain_id == "team-platform"
        assert result.ownership_path == ["git:acme", "team-platform"]

    @pytest.mark.asyncio
    async def test_register_ownership_org_only_no_domain(self) -> None:
        from investigation_agent_platform.domain.topology.models import (
            GitOrganizationIdentity,
            RepositoryIdentity,
            RepositoryType,
        )
        from investigation_agent_platform.infrastructure.topology.in_memory import (
            InMemoryTopologyAdapter,
        )

        adapter = InMemoryTopologyAdapter()
        repository = RepositoryIdentity(
            tenant_id="tenant-a",
            repository_id="repo-1",
            name="svc",
            locator="acme/svc",
            git_org_id="git:acme",
            repository_type=RepositoryType.SERVICE,
        )
        git_org = GitOrganizationIdentity(
            tenant_id="tenant-a", git_org_id="git:acme", name="acme", provider="git"
        )
        await adapter.register_ownership(repository, git_org, None)

        payload = _payload(tenant_id="tenant-a", repository_id="repo-1", revision="rev1")
        await adapter.ingest(payload)
        result = await adapter.resolve_source_location(
            tenant_id="tenant-a",
            application_id="app-1",
            investigation_id=uuid.uuid4(),
            repository_id="repo-1",
            revision="rev1",
            file_path="no/such/file.py",
            line_number=1,
        )
        assert result.git_org_id == "git:acme"
        assert result.domain_id is None


class TestProfileRegistryOwnershipWriteThrough:
    @pytest.mark.asyncio
    async def test_derives_git_org_id_from_locator(self) -> None:
        from investigation_agent_platform.infrastructure.topology.profile_repository_registry import (
            ProfileBackedRepositoryRegistry,
        )

        profile_repo = MagicMock()
        profile = MagicMock()
        profile.code_configuration.repository = "https://github.com/acme/checkout-service"
        profile.code_configuration.repository_id = "repo-1"
        profile.code_configuration.default_branch = "main"
        profile_repo.get_by_application_id = AsyncMock(return_value=profile)

        registry = ProfileBackedRepositoryRegistry(profile_repo)
        repo = await registry.resolve_for_application("tenant-a", "app-1")
        assert repo is not None
        assert repo.git_org_id == "github:acme"

    @pytest.mark.asyncio
    async def test_falls_back_to_unassigned_for_unparseable_locator(self) -> None:
        from investigation_agent_platform.infrastructure.topology.profile_repository_registry import (
            ProfileBackedRepositoryRegistry,
        )

        profile_repo = MagicMock()
        profile = MagicMock()
        profile.code_configuration.repository = "/local/checkout-service"
        profile.code_configuration.repository_id = "repo-1"
        profile.code_configuration.default_branch = "main"
        profile_repo.get_by_application_id = AsyncMock(return_value=profile)

        registry = ProfileBackedRepositoryRegistry(profile_repo)
        repo = await registry.resolve_for_application("tenant-a", "app-1")
        assert repo is not None
        assert repo.git_org_id == "tenant-a:unassigned"

    @pytest.mark.asyncio
    async def test_writes_through_to_ownership_registry_when_configured(self) -> None:
        from investigation_agent_platform.infrastructure.topology.profile_repository_registry import (
            ProfileBackedRepositoryRegistry,
        )

        profile_repo = MagicMock()
        profile = MagicMock()
        profile.code_configuration.repository = "https://github.com/acme/checkout-service"
        profile.code_configuration.repository_id = "repo-1"
        profile.code_configuration.default_branch = "main"
        profile_repo.get_by_application_id = AsyncMock(return_value=profile)

        ownership_registry = MagicMock()
        ownership_registry.register_ownership = AsyncMock()

        registry = ProfileBackedRepositoryRegistry(
            profile_repo, ownership_registry=ownership_registry
        )
        await registry.resolve_for_application("tenant-a", "app-1")
        ownership_registry.register_ownership.assert_awaited_once()
        _, kwargs = ownership_registry.register_ownership.await_args
        assert kwargs["git_org"].git_org_id == "github:acme"

    @pytest.mark.asyncio
    async def test_no_ownership_registry_configured_is_a_noop(self) -> None:
        from investigation_agent_platform.infrastructure.topology.profile_repository_registry import (
            ProfileBackedRepositoryRegistry,
        )

        profile_repo = MagicMock()
        profile = MagicMock()
        profile.code_configuration.repository = "https://github.com/acme/checkout-service"
        profile.code_configuration.repository_id = "repo-1"
        profile.code_configuration.default_branch = "main"
        profile_repo.get_by_application_id = AsyncMock(return_value=profile)

        registry = ProfileBackedRepositoryRegistry(profile_repo)
        # Should not raise even though no ownership_registry is configured.
        repo = await registry.resolve_for_application("tenant-a", "app-1")
        assert repo is not None

    @pytest.mark.asyncio
    async def test_derives_domain_from_repo_checkout_codeowners(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        from investigation_agent_platform.infrastructure.topology.profile_repository_registry import (
            ProfileBackedRepositoryRegistry,
        )

        repo_dir = tmp_path / "acme-checkout-service"
        repo_dir.mkdir()
        (repo_dir / "CODEOWNERS").write_text("* @team-platform\n", encoding="utf-8")

        profile_repo = MagicMock()
        profile = MagicMock()
        profile.code_configuration.repository = "acme-checkout-service"
        profile.code_configuration.repository_id = "repo-1"
        profile.code_configuration.default_branch = "main"
        profile_repo.get_by_application_id = AsyncMock(return_value=profile)

        ownership_registry = MagicMock()
        ownership_registry.register_ownership = AsyncMock()

        registry = ProfileBackedRepositoryRegistry(
            profile_repo,
            ownership_registry=ownership_registry,
            repo_base_path=str(tmp_path),
        )
        await registry.resolve_for_application("tenant-a", "app-1")
        _, kwargs = ownership_registry.register_ownership.await_args
        assert kwargs["domain_id"] == "team-platform"


# ===========================================================================
# ISSUE-4: snapshot retention and garbage collection
# ===========================================================================


def _descriptor(
    tenant_id: str = "tenant-a",
    repository_id: str = "repo-1",
    revision: str = "rev-1",
    status=None,  # type: ignore[no-untyped-def]
    ingested_at=None,  # type: ignore[no-untyped-def]
):
    from investigation_agent_platform.domain.topology.models import (
        SnapshotDescriptor,
        TopologySnapshotStatus,
    )

    return SnapshotDescriptor(
        tenant_id=tenant_id,
        repository_id=repository_id,
        revision=revision,
        status=status or TopologySnapshotStatus.READY,
        ingested_at=ingested_at or datetime.now(UTC),
    )


class TestRetentionClassification:
    def test_pinned_revision_never_collectible_even_if_old(self) -> None:
        from investigation_agent_platform.application.topology.retention import (
            classify_snapshots,
        )

        old = datetime.now(UTC) - timedelta(days=365)
        snapshots = [_descriptor(revision="rev-old", ingested_at=old)]
        result = classify_snapshots(
            snapshots,
            pinned_revisions={"rev-old"},
            now=datetime.now(UTC),
            retention_window_days=30,
            retention_max_snapshots_per_repository=1,
        )
        assert result.collectible == []
        assert result.pinned == snapshots

    def test_non_ready_or_superseded_status_never_collectible(self) -> None:
        from investigation_agent_platform.application.topology.retention import (
            classify_snapshots,
        )
        from investigation_agent_platform.domain.topology.models import TopologySnapshotStatus

        old = datetime.now(UTC) - timedelta(days=365)
        snapshots = [
            _descriptor(revision="pending", status=TopologySnapshotStatus.PENDING, ingested_at=old),
            _descriptor(
                revision="ingesting", status=TopologySnapshotStatus.INGESTING, ingested_at=old
            ),
            _descriptor(revision="failed", status=TopologySnapshotStatus.FAILED, ingested_at=old),
            _descriptor(
                revision="collected", status=TopologySnapshotStatus.COLLECTED, ingested_at=old
            ),
        ]
        result = classify_snapshots(
            snapshots,
            pinned_revisions=set(),
            now=datetime.now(UTC),
            retention_window_days=30,
            retention_max_snapshots_per_repository=1,
        )
        assert result.collectible == []
        assert len(result.pinned) == 4

    def test_within_window_kept_regardless_of_lru_bound(self) -> None:
        from investigation_agent_platform.application.topology.retention import (
            classify_snapshots,
        )

        now = datetime.now(UTC)
        # 5 snapshots all within the 30-day window, LRU bound of 1 — the
        # window rule keeps all of them.
        snapshots = [
            _descriptor(revision=f"rev-{i}", ingested_at=now - timedelta(days=i)) for i in range(5)
        ]
        result = classify_snapshots(
            snapshots,
            pinned_revisions=set(),
            now=now,
            retention_window_days=30,
            retention_max_snapshots_per_repository=1,
        )
        assert result.collectible == []
        assert len(result.pinned) == 5

    def test_lru_bound_keeps_newest_n_beyond_window(self) -> None:
        from investigation_agent_platform.application.topology.retention import (
            classify_snapshots,
        )

        now = datetime.now(UTC)
        # All older than the window (60 days); LRU bound of 2 keeps the 2
        # most recent, the rest are collectible.
        snapshots = [
            _descriptor(revision=f"rev-{i}", ingested_at=now - timedelta(days=60 + i))
            for i in range(5)
        ]
        result = classify_snapshots(
            snapshots,
            pinned_revisions=set(),
            now=now,
            retention_window_days=30,
            retention_max_snapshots_per_repository=2,
        )
        collected_revisions = {s.revision for s in result.collectible}
        assert collected_revisions == {"rev-2", "rev-3", "rev-4"}
        kept_revisions = {s.revision for s in result.pinned}
        assert kept_revisions == {"rev-0", "rev-1"}

    def test_combination_pinned_window_and_lru_all_respected(self) -> None:
        from investigation_agent_platform.application.topology.retention import (
            classify_snapshots,
        )

        now = datetime.now(UTC)
        snapshots = [
            _descriptor(revision="pinned-old", ingested_at=now - timedelta(days=400)),
            _descriptor(revision="recent", ingested_at=now - timedelta(days=1)),
            _descriptor(revision="lru-kept", ingested_at=now - timedelta(days=60)),
            _descriptor(revision="collectible-1", ingested_at=now - timedelta(days=61)),
            _descriptor(revision="collectible-2", ingested_at=now - timedelta(days=62)),
        ]
        result = classify_snapshots(
            snapshots,
            pinned_revisions={"pinned-old"},
            now=now,
            retention_window_days=30,
            retention_max_snapshots_per_repository=2,
        )
        collectible = {s.revision for s in result.collectible}
        pinned = {s.revision for s in result.pinned}
        assert collectible == {"collectible-1", "collectible-2"}
        assert pinned == {"pinned-old", "recent", "lru-kept"}


class TestNeo4jSnapshotCollection:
    def _adapter(self):  # type: ignore[no-untyped-def]
        from investigation_agent_platform.infrastructure.configuration.config import TopologyConfig
        from investigation_agent_platform.infrastructure.topology.neo4j_adapter import (
            Neo4jTopologyAdapter,
        )

        return Neo4jTopologyAdapter(TopologyConfig(enabled=True))

    def _mock_driver(self, session: AsyncMock) -> MagicMock:
        session_context = MagicMock()
        session_context.__aenter__ = AsyncMock(return_value=session)
        session_context.__aexit__ = AsyncMock(return_value=None)
        driver = MagicMock()
        driver.session = MagicMock(return_value=session_context)
        return driver

    @pytest.mark.asyncio
    async def test_list_snapshots_maps_records(self) -> None:
        from investigation_agent_platform.domain.topology.models import TopologySnapshotStatus

        adapter = self._adapter()
        session = AsyncMock()
        result = AsyncMock()
        now_iso = datetime.now(UTC).isoformat()

        async def _aiter():  # type: ignore[no-untyped-def]
            for row in (
                {"revision": "rev-1", "status": "READY", "ingested_at": now_iso},
                {"revision": "rev-2", "status": "SUPERSEDED", "ingested_at": None},
            ):
                yield row

        result.__aiter__ = lambda self: _aiter()
        session.run = AsyncMock(return_value=result)
        adapter._driver = self._mock_driver(session)

        descriptors = await adapter.list_snapshots("tenant-a", "repo-1")
        assert {d.revision for d in descriptors} == {"rev-1", "rev-2"}
        by_rev = {d.revision: d for d in descriptors}
        assert by_rev["rev-1"].status == TopologySnapshotStatus.READY
        assert by_rev["rev-2"].status == TopologySnapshotStatus.SUPERSEDED
        # Missing ingested_at (legacy snapshot) fails closed to "now", never crashes.
        assert by_rev["rev-2"].ingested_at is not None

    @pytest.mark.asyncio
    async def test_collect_snapshot_deletes_subgraph_and_marks_collected(self) -> None:
        adapter = self._adapter()
        session = AsyncMock()
        session.execute_read = AsyncMock(
            return_value={"status": "READY", "payload_hash": "h", "snapshot_id": str(uuid.uuid4())}
        )

        delete_result = AsyncMock()
        delete_result.single = AsyncMock(return_value={"deleted_nodes": 12, "deleted_edges": 20})

        async def _execute_write(fn, *args):  # type: ignore[no-untyped-def]
            tx = AsyncMock()
            tx.run = AsyncMock(return_value=delete_result)
            return await fn(tx, *args)

        session.execute_write = AsyncMock(side_effect=_execute_write)
        adapter._driver = self._mock_driver(session)

        result = await adapter.collect_snapshot("tenant-a", "repo-1", "rev-1")
        assert result.already_collected is False
        assert result.deleted_node_count == 12
        assert result.deleted_edge_count == 20

    @pytest.mark.asyncio
    async def test_collect_snapshot_is_idempotent(self) -> None:
        adapter = self._adapter()
        session = AsyncMock()
        session.execute_read = AsyncMock(
            return_value={"status": "COLLECTED", "payload_hash": "h", "snapshot_id": None}
        )
        session.execute_write = AsyncMock()
        adapter._driver = self._mock_driver(session)

        result = await adapter.collect_snapshot("tenant-a", "repo-1", "rev-1")
        assert result.already_collected is True
        assert result.deleted_node_count == 0
        session.execute_write.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_collect_snapshot_unknown_raises(self) -> None:
        adapter = self._adapter()
        session = AsyncMock()
        session.execute_read = AsyncMock(return_value=None)
        adapter._driver = self._mock_driver(session)

        with pytest.raises(TopologyNotConfiguredError):
            await adapter.collect_snapshot("tenant-a", "repo-1", "nope")


class TestInMemorySnapshotCollectionParity:
    @pytest.mark.asyncio
    async def test_list_and_collect_snapshot(self) -> None:
        from investigation_agent_platform.domain.topology.models import (
            AttributionFallbackLevel,
            TopologySnapshotStatus,
        )
        from investigation_agent_platform.infrastructure.topology.in_memory import (
            InMemoryTopologyAdapter,
        )

        adapter = InMemoryTopologyAdapter()
        payload = _payload(tenant_id="tenant-a", repository_id="repo-1", revision="rev-1")
        await adapter.ingest(payload)

        descriptors = await adapter.list_snapshots("tenant-a", "repo-1")
        assert len(descriptors) == 1
        assert descriptors[0].status == TopologySnapshotStatus.READY
        assert descriptors[0].ingested_at is not None

        result = await adapter.collect_snapshot("tenant-a", "repo-1", "rev-1")
        assert result.already_collected is False
        assert result.deleted_node_count == 1

        # Idempotent retry.
        retry = await adapter.collect_snapshot("tenant-a", "repo-1", "rev-1")
        assert retry.already_collected is True
        assert retry.deleted_node_count == 0

        # Live queries against the collected revision fail (not silently
        # reused) — the caller must rely on embedded evidence instead.
        with pytest.raises(TopologySnapshotNotReadyError):
            await adapter.resolve_source_location(
                tenant_id="tenant-a",
                application_id="app-1",
                investigation_id=uuid.uuid4(),
                repository_id="repo-1",
                revision="rev-1",
                file_path="src/mod.py",
                line_number=2,
            )
        descriptors_after = await adapter.list_snapshots("tenant-a", "repo-1")
        assert descriptors_after[0].status == TopologySnapshotStatus.COLLECTED
        assert AttributionFallbackLevel.AST_NODE  # sanity import retained

    @pytest.mark.asyncio
    async def test_collect_unknown_snapshot_raises(self) -> None:
        from investigation_agent_platform.infrastructure.topology.in_memory import (
            InMemoryTopologyAdapter,
        )

        adapter = InMemoryTopologyAdapter()
        with pytest.raises(TopologyNotConfiguredError):
            await adapter.collect_snapshot("tenant-a", "repo-1", "nope")


class TestPinnedRevisionsProvider:
    @pytest.mark.asyncio
    async def test_pins_only_revisions_referenced_by_open_investigations(self) -> None:
        from investigation_agent_platform.application.topology.pinned_revisions import (
            EvidenceBackedPinnedRevisionsProvider,
        )

        open_id = uuid.uuid4()
        investigation_repo = MagicMock()
        investigation_repo.list_open_ids = AsyncMock(return_value=[open_id])

        topo_evidence = MagicMock()
        topo_evidence.provider = "TOPOLOGY_ATTRIBUTION"
        topo_evidence.source = "topology://repo-1/rev-abc"
        other_evidence = MagicMock()
        other_evidence.provider = "LOG_SEARCH"
        other_evidence.source = "elastic://logs/1"

        evidence_repo = MagicMock()
        evidence_repo.find_by_investigation_id = AsyncMock(
            return_value=[topo_evidence, other_evidence]
        )

        provider = EvidenceBackedPinnedRevisionsProvider(investigation_repo, evidence_repo)
        pinned = await provider.list_pinned_revisions("tenant-a", "repo-1")
        assert pinned == {"rev-abc"}

    @pytest.mark.asyncio
    async def test_ignores_evidence_for_a_different_repository(self) -> None:
        from investigation_agent_platform.application.topology.pinned_revisions import (
            EvidenceBackedPinnedRevisionsProvider,
        )

        open_id = uuid.uuid4()
        investigation_repo = MagicMock()
        investigation_repo.list_open_ids = AsyncMock(return_value=[open_id])

        other_repo_evidence = MagicMock()
        other_repo_evidence.provider = "TOPOLOGY_ATTRIBUTION"
        other_repo_evidence.source = "topology://repo-OTHER/rev-xyz"

        evidence_repo = MagicMock()
        evidence_repo.find_by_investigation_id = AsyncMock(return_value=[other_repo_evidence])

        provider = EvidenceBackedPinnedRevisionsProvider(investigation_repo, evidence_repo)
        pinned = await provider.list_pinned_revisions("tenant-a", "repo-1")
        assert pinned == set()

    @pytest.mark.asyncio
    async def test_no_open_investigations_pins_nothing(self) -> None:
        from investigation_agent_platform.application.topology.pinned_revisions import (
            EvidenceBackedPinnedRevisionsProvider,
        )

        investigation_repo = MagicMock()
        investigation_repo.list_open_ids = AsyncMock(return_value=[])
        evidence_repo = MagicMock()
        evidence_repo.find_by_investigation_id = AsyncMock(return_value=[])

        provider = EvidenceBackedPinnedRevisionsProvider(investigation_repo, evidence_repo)
        pinned = await provider.list_pinned_revisions("tenant-a", "repo-1")
        assert pinned == set()


class TestPostCollectionReadability:
    """Acceptance criterion: a concluded investigation's attribution evidence
    still resolves after its snapshot is collected — because the evidence
    embeds its own snapshot_id/revision/ownership path rather than depending
    on a live query."""

    def test_attribution_evidence_is_self_contained(self) -> None:
        from investigation_agent_platform.application.topology.attribution import (
            FailureAttributionService,
        )
        from investigation_agent_platform.domain.topology.models import (
            AttributionClassification,
            AttributionFallbackLevel,
            DomainAttributionResult,
        )

        result = DomainAttributionResult(
            tenant_id="tenant-a",
            application_id="app-1",
            investigation_id=uuid.uuid4(),
            repository_id="repo-1",
            revision="rev-1",
            matched_file_path="src/mod.py",
            classification=AttributionClassification.LIBRARY_DEFECT,
            confidence=0.9,
            snapshot_id=uuid.uuid4(),
            ownership_path=["github:acme", "team-platform"],
            fallback_level=AttributionFallbackLevel.AST_NODE,
        )
        evidence = FailureAttributionService._to_evidence(result)

        # The full attribution payload — including snapshot_id, revision,
        # and ownership_path — is embedded in content_snippet. A collected
        # (deleted) live snapshot never needs to be re-queried to interpret
        # this evidence.
        assert str(result.snapshot_id) in evidence.content_snippet
        assert "team-platform" in evidence.content_snippet
        assert evidence.provenance.source_location.revision == "rev-1"
        assert evidence.provenance.source_location.identifier == "repo-1@rev-1"


class TestCollectSnapshotsActivity:
    @pytest.mark.asyncio
    async def test_disabled_retention_reports_error_not_exception(self) -> None:
        from investigation_agent_platform.application.worker.activities import (
            CollectSnapshotsInput,
            collect_snapshots_activity,
        )
        from investigation_agent_platform.infrastructure.topology.in_memory import (
            InMemoryTopologyAdapter,
        )

        ctx = AppContext()
        ctx.topology_adapter = InMemoryTopologyAdapter()  # type: ignore[attr-defined]

        class _Cfg:
            retention_enabled = False

        ctx.topology_config = _Cfg()  # type: ignore[attr-defined]
        set_app_context(ctx)

        output = await collect_snapshots_activity(
            CollectSnapshotsInput(tenant_id="tenant-a", repository_id="repo-1")
        )
        assert output.error == "retention is not enabled"
        assert output.collected_revisions == []

    @pytest.mark.asyncio
    async def test_no_topology_adapter_reports_error(self) -> None:
        from investigation_agent_platform.application.worker.activities import (
            CollectSnapshotsInput,
            collect_snapshots_activity,
        )

        ctx = AppContext()
        ctx.topology_adapter = None  # type: ignore[attr-defined]
        set_app_context(ctx)
        output = await collect_snapshots_activity(
            CollectSnapshotsInput(tenant_id="tenant-a", repository_id="repo-1")
        )
        assert output.error == "topology_adapter is not configured"

    @pytest.mark.asyncio
    async def test_missing_ids_reports_error(self) -> None:
        from investigation_agent_platform.application.worker.activities import (
            CollectSnapshotsInput,
            collect_snapshots_activity,
        )

        output = await collect_snapshots_activity(
            CollectSnapshotsInput(tenant_id="", repository_id="")
        )
        assert output.error == "tenant_id and repository_id required"

    @pytest.mark.asyncio
    async def test_end_to_end_collects_unpinned_old_snapshot(self) -> None:
        from investigation_agent_platform.application.worker.activities import (
            CollectSnapshotsInput,
            collect_snapshots_activity,
        )
        from investigation_agent_platform.infrastructure.topology.in_memory import (
            InMemoryTopologyAdapter,
        )

        adapter = InMemoryTopologyAdapter()
        payload = _payload(tenant_id="tenant-a", repository_id="repo-1", revision="rev-old")
        await adapter.ingest(payload)
        # Force the snapshot to look old enough to be beyond both the
        # window and the (default) LRU bound.
        key = ("tenant-a", "repo-1", "rev-old")
        adapter._snapshots[key]["ingested_at"] = datetime.now(UTC) - timedelta(days=90)

        ctx = AppContext()
        ctx.topology_adapter = adapter  # type: ignore[attr-defined]

        class _Cfg:
            retention_enabled = True
            retention_window_days = 30
            retention_max_snapshots_per_repository = 0

        ctx.topology_config = _Cfg()  # type: ignore[attr-defined]
        set_app_context(ctx)

        output = await collect_snapshots_activity(
            CollectSnapshotsInput(tenant_id="tenant-a", repository_id="repo-1")
        )
        assert output.error is None
        assert output.collected_revisions == ["rev-old"]
        assert output.deleted_node_count >= 1

        # Second pass is idempotent: nothing left to collect.
        output2 = await collect_snapshots_activity(
            CollectSnapshotsInput(tenant_id="tenant-a", repository_id="repo-1")
        )
        assert output2.collected_revisions == []


# ===========================================================================
# ISSUE-5: cross-repository hops via runtime trace evidence
# ===========================================================================


def _route_node(
    tenant_id: str = "tenant-a",
    repository_id: str = "repo-1",
    revision: str = "abc123",
    file_path: str = "src/client.py",
):
    from investigation_agent_platform.domain.topology.models import (
        ASTNodeIdentity,
        TopologyNodeType,
    )

    return ASTNodeIdentity.create(
        tenant_id=tenant_id,
        repository_id=repository_id,
        revision=revision,
        name="checkout_route",
        qualified_name="checkout.route",
        node_type=TopologyNodeType.ROUTE,
        file_path=file_path,
        start_line=1,
        end_line=10,
    )


class _FakeTraceHopResolver:
    """Only ever resolves a target for the exact ``allowed_tenant_id`` —
    simulates tenant-scoped evidence retrieval without a real gateway."""

    def __init__(self, target, allowed_tenant_id: str = "tenant-a"):  # type: ignore[no-untyped-def]
        self._target = target
        self._allowed_tenant_id = allowed_tenant_id

    async def resolve_target_service(
        self,
        tenant_id,
        investigation_id,
        application_id,
        environment,
        trace_id,
        source_span_id,
    ):  # type: ignore[no-untyped-def]
        if tenant_id != self._allowed_tenant_id:
            return None
        return self._target


class _FakeServiceRegistry:
    def __init__(self, mapping: dict[tuple[str, str], object]) -> None:
        self._mapping = mapping

    async def resolve_for_application(self, tenant_id, application_id):  # type: ignore[no-untyped-def]
        return None

    async def resolve_for_service_name(self, tenant_id, service_name):  # type: ignore[no-untyped-def]
        return self._mapping.get((tenant_id, service_name))


class TestCrossRepositoryHop:
    async def _service_with_hop(self, target, allowed_tenant_id: str = "tenant-a"):  # type: ignore[no-untyped-def]
        from investigation_agent_platform.application.topology.attribution import (
            FailureAttributionService,
        )
        from investigation_agent_platform.domain.topology.models import RepositoryIdentity
        from investigation_agent_platform.infrastructure.topology.in_memory import (
            InMemoryTopologyAdapter,
        )

        adapter = InMemoryTopologyAdapter()
        route_node = _route_node()
        await adapter.ingest(_payload(ast_nodes=[route_node]))
        _seed_repo(adapter, repository_id="repo-1", ownership=["org-1", "domain-checkout"])

        # repo-2 (payments-library): no ingested AST nodes, only ownership —
        # the hop resolves the target's REPOSITORY-tier domain, not a
        # symbol-precise location.
        payments_payload = _payload(
            tenant_id="tenant-a", repository_id="repo-2", revision="main", ast_nodes=[]
        )
        await adapter.ingest(payments_payload)
        payments_repo = _seed_repo(
            adapter, repository_id="repo-2", ownership=["org-1", "team-payments"]
        )

        registry = _FakeServiceRegistry({("tenant-a", "payments"): payments_repo})
        resolver = _FakeTraceHopResolver(target, allowed_tenant_id=allowed_tenant_id)
        service = FailureAttributionService(
            attribution_port=adapter,
            evidence_repo=None,
            repository_registry=registry,
            trace_hop_resolver=resolver,
        )
        return service, adapter, RepositoryIdentity, route_node

    @pytest.mark.asyncio
    async def test_hop_corroborated_by_trace_resolves_target_domain(self) -> None:
        from investigation_agent_platform.domain.topology.models import TraceHopTarget

        target = TraceHopTarget(
            target_service="payments", target_span_id="span-2", parent_span_id="span-1"
        )
        service, _adapter, _RepositoryIdentity, _route_node_obj = await self._service_with_hop(
            target
        )

        result = await service.attribute_failure(
            tenant_id="tenant-a",
            application_id="app-1",
            investigation_id=uuid.uuid4(),
            failure_frame=_frame(repository_id="repo-1", file_path="src/client.py", line_number=2),
            caller_frame=None,
            trace_id="trace-abc",
            span_id="span-1",
        )

        assert len(result.hops) == 1
        hop = result.hops[0]
        assert hop.target_service == "payments"
        assert hop.target_repository_id == "repo-2"
        assert hop.target_revision == "main"
        assert hop.target_domain_id == "team-payments"
        assert hop.trace_id == "trace-abc"
        assert hop.target_span_id == "span-2"
        assert any("cross-repository hop" in limitation for limitation in result.limitations)

    @pytest.mark.asyncio
    async def test_no_trace_id_stays_single_repository(self) -> None:
        from investigation_agent_platform.domain.topology.models import TraceHopTarget

        target = TraceHopTarget(
            target_service="payments", target_span_id="span-2", parent_span_id="span-1"
        )
        service, _adapter, _RepositoryIdentity, _route_node_obj = await self._service_with_hop(
            target
        )

        result = await service.attribute_failure(
            tenant_id="tenant-a",
            application_id="app-1",
            investigation_id=uuid.uuid4(),
            failure_frame=_frame(repository_id="repo-1", file_path="src/client.py", line_number=2),
            caller_frame=None,
            # No trace_id supplied at all.
        )
        assert result.hops == []

    @pytest.mark.asyncio
    async def test_no_corroborating_evidence_no_hop(self) -> None:
        service, _adapter, _RepositoryIdentity, _route_node_obj = await self._service_with_hop(
            None  # resolver finds no other-service span
        )

        result = await service.attribute_failure(
            tenant_id="tenant-a",
            application_id="app-1",
            investigation_id=uuid.uuid4(),
            failure_frame=_frame(repository_id="repo-1", file_path="src/client.py", line_number=2),
            caller_frame=None,
            trace_id="trace-abc",
            span_id="span-1",
        )
        assert result.hops == []
        assert not any("cross-repository hop" in limitation for limitation in result.limitations)

    @pytest.mark.asyncio
    async def test_non_route_node_never_attempts_hop(self) -> None:
        """A FUNCTION node (not ROUTE/MESSAGE_HANDLER) never triggers a hop
        attempt, even with a trace_id and a resolver that would happily hop."""
        from investigation_agent_platform.domain.topology.models import TraceHopTarget

        target = TraceHopTarget(
            target_service="payments", target_span_id="span-2", parent_span_id="span-1"
        )
        from investigation_agent_platform.application.topology.attribution import (
            FailureAttributionService,
        )
        from investigation_agent_platform.infrastructure.topology.in_memory import (
            InMemoryTopologyAdapter,
        )

        adapter = InMemoryTopologyAdapter()
        await adapter.ingest(_payload(ast_nodes=[_node()]))  # plain FUNCTION node
        _seed_repo(adapter, repository_id="repo-1", ownership=["org-1", "domain-checkout"])
        registry = _FakeServiceRegistry({})
        resolver = _FakeTraceHopResolver(target)
        service = FailureAttributionService(
            attribution_port=adapter,
            evidence_repo=None,
            repository_registry=registry,
            trace_hop_resolver=resolver,
        )

        result = await service.attribute_failure(
            tenant_id="tenant-a",
            application_id="app-1",
            investigation_id=uuid.uuid4(),
            failure_frame=_frame(),
            caller_frame=None,
            trace_id="trace-abc",
            span_id="span-1",
        )
        assert result.hops == []

    @pytest.mark.asyncio
    async def test_wrong_tenant_trace_evidence_never_triggers_hop(self) -> None:
        """Trace evidence resolvable only for a different tenant must never
        produce a hop for this tenant (tenant isolation)."""
        from investigation_agent_platform.domain.topology.models import TraceHopTarget

        target = TraceHopTarget(
            target_service="payments", target_span_id="span-2", parent_span_id="span-1"
        )
        service, _adapter, _RepositoryIdentity, _route_node_obj = await self._service_with_hop(
            target, allowed_tenant_id="tenant-OTHER"
        )

        result = await service.attribute_failure(
            tenant_id="tenant-a",
            application_id="app-1",
            investigation_id=uuid.uuid4(),
            failure_frame=_frame(repository_id="repo-1", file_path="src/client.py", line_number=2),
            caller_frame=None,
            trace_id="trace-abc",
            span_id="span-1",
        )
        assert result.hops == []

    @pytest.mark.asyncio
    async def test_no_target_repository_mapping_no_hop(self) -> None:
        """Trace evidence corroborates a target service, but no repository
        is registered under that service name — no hop, no fabrication."""
        from investigation_agent_platform.application.topology.attribution import (
            FailureAttributionService,
        )
        from investigation_agent_platform.domain.topology.models import TraceHopTarget
        from investigation_agent_platform.infrastructure.topology.in_memory import (
            InMemoryTopologyAdapter,
        )

        adapter = InMemoryTopologyAdapter()
        await adapter.ingest(_payload(ast_nodes=[_route_node()]))
        _seed_repo(adapter, repository_id="repo-1", ownership=["org-1", "domain-checkout"])
        registry = _FakeServiceRegistry({})  # empty: "payments" never registered
        target = TraceHopTarget(
            target_service="payments", target_span_id="span-2", parent_span_id="span-1"
        )
        service = FailureAttributionService(
            attribution_port=adapter,
            evidence_repo=None,
            repository_registry=registry,
            trace_hop_resolver=_FakeTraceHopResolver(target),
        )

        result = await service.attribute_failure(
            tenant_id="tenant-a",
            application_id="app-1",
            investigation_id=uuid.uuid4(),
            failure_frame=_frame(repository_id="repo-1", file_path="src/client.py", line_number=2),
            caller_frame=None,
            trace_id="trace-abc",
            span_id="span-1",
        )
        assert result.hops == []


class TestGatewayTraceHopResolver:
    @pytest.mark.asyncio
    async def test_resolves_single_other_service_span(self) -> None:
        from investigation_agent_platform.infrastructure.topology.trace_hop import (
            GatewayTraceHopResolver,
        )

        item1 = MagicMock()
        item1.attributes = {"span": {"id": "span-1"}, "service": {"name": "checkout"}}
        item2 = MagicMock()
        item2.attributes = {
            "span": {"id": "span-2"},
            "service": {"name": "payments"},
            "parent": {"id": "span-1"},
        }
        gateway = MagicMock()
        gateway.search_runtime_evidence = AsyncMock(return_value=MagicMock(items=[item1, item2]))
        resolver = GatewayTraceHopResolver(gateway)

        target = await resolver.resolve_target_service(
            tenant_id="tenant-a",
            investigation_id=uuid.uuid4(),
            application_id="app-1",
            environment="production",
            trace_id="trace-abc",
            source_span_id="span-1",
        )
        assert target is not None
        assert target.target_service == "payments"
        assert target.target_span_id == "span-2"
        assert target.parent_span_id == "span-1"

    @pytest.mark.asyncio
    async def test_flat_dotted_attribute_keys_also_resolve(self) -> None:
        from investigation_agent_platform.infrastructure.topology.trace_hop import (
            GatewayTraceHopResolver,
        )

        item = MagicMock()
        item.attributes = {"span.id": "span-2", "service.name": "payments"}
        gateway = MagicMock()
        gateway.search_runtime_evidence = AsyncMock(return_value=MagicMock(items=[item]))
        resolver = GatewayTraceHopResolver(gateway)

        target = await resolver.resolve_target_service(
            tenant_id="tenant-a",
            investigation_id=uuid.uuid4(),
            application_id="app-1",
            environment="production",
            trace_id="trace-abc",
            source_span_id="span-1",
        )
        assert target is not None
        assert target.target_service == "payments"

    @pytest.mark.asyncio
    async def test_no_other_service_span_returns_none(self) -> None:
        from investigation_agent_platform.infrastructure.topology.trace_hop import (
            GatewayTraceHopResolver,
        )

        item = MagicMock()
        item.attributes = {"span": {"id": "span-1"}, "service": {"name": "checkout"}}
        gateway = MagicMock()
        gateway.search_runtime_evidence = AsyncMock(return_value=MagicMock(items=[item]))
        resolver = GatewayTraceHopResolver(gateway)

        target = await resolver.resolve_target_service(
            tenant_id="tenant-a",
            investigation_id=uuid.uuid4(),
            application_id="app-1",
            environment="production",
            trace_id="trace-abc",
            source_span_id="span-1",
        )
        assert target is None

    @pytest.mark.asyncio
    async def test_ambiguous_multiple_target_spans_returns_none(self) -> None:
        from investigation_agent_platform.infrastructure.topology.trace_hop import (
            GatewayTraceHopResolver,
        )

        item2 = MagicMock()
        item2.attributes = {"span": {"id": "span-2"}, "service": {"name": "payments"}}
        item3 = MagicMock()
        item3.attributes = {"span": {"id": "span-3"}, "service": {"name": "notifications"}}
        gateway = MagicMock()
        gateway.search_runtime_evidence = AsyncMock(return_value=MagicMock(items=[item2, item3]))
        resolver = GatewayTraceHopResolver(gateway)

        target = await resolver.resolve_target_service(
            tenant_id="tenant-a",
            investigation_id=uuid.uuid4(),
            application_id="app-1",
            environment="production",
            trace_id="trace-abc",
            source_span_id="span-1",
        )
        assert target is None

    @pytest.mark.asyncio
    async def test_empty_trace_id_returns_none_without_querying(self) -> None:
        from investigation_agent_platform.infrastructure.topology.trace_hop import (
            GatewayTraceHopResolver,
        )

        gateway = MagicMock()
        gateway.search_runtime_evidence = AsyncMock()
        resolver = GatewayTraceHopResolver(gateway)

        target = await resolver.resolve_target_service(
            tenant_id="tenant-a",
            investigation_id=uuid.uuid4(),
            application_id="app-1",
            environment="production",
            trace_id="",
            source_span_id=None,
        )
        assert target is None
        gateway.search_runtime_evidence.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_gateway_failure_returns_none_not_raises(self) -> None:
        from investigation_agent_platform.infrastructure.topology.trace_hop import (
            GatewayTraceHopResolver,
        )

        gateway = MagicMock()
        gateway.search_runtime_evidence = AsyncMock(side_effect=RuntimeError("es down"))
        resolver = GatewayTraceHopResolver(gateway)

        target = await resolver.resolve_target_service(
            tenant_id="tenant-a",
            investigation_id=uuid.uuid4(),
            application_id="app-1",
            environment="production",
            trace_id="trace-abc",
            source_span_id="span-1",
        )
        assert target is None


class TestServiceNameRepositoryMapping:
    @pytest.mark.asyncio
    async def test_profile_backed_registry_resolves_by_service_name(self) -> None:
        from investigation_agent_platform.infrastructure.topology.profile_repository_registry import (
            ProfileBackedRepositoryRegistry,
        )

        profile = MagicMock()
        profile.id = "app-1"
        profile.code_configuration.repository = "https://github.com/acme/payments-library"
        profile.code_configuration.repository_id = "repo-2"
        profile.code_configuration.default_branch = "main"
        profile.code_configuration.service_name = "payments"

        profile_repo = MagicMock()
        profile_repo.list = AsyncMock(return_value=[profile])

        registry = ProfileBackedRepositoryRegistry(profile_repo)
        repo = await registry.resolve_for_service_name("tenant-a", "payments")
        assert repo is not None
        assert repo.repository_id == "repo-2"

    @pytest.mark.asyncio
    async def test_unknown_service_name_returns_none(self) -> None:
        from investigation_agent_platform.infrastructure.topology.profile_repository_registry import (
            ProfileBackedRepositoryRegistry,
        )

        profile_repo = MagicMock()
        profile_repo.list = AsyncMock(return_value=[])

        registry = ProfileBackedRepositoryRegistry(profile_repo)
        repo = await registry.resolve_for_service_name("tenant-a", "payments")
        assert repo is None

    @pytest.mark.asyncio
    async def test_in_memory_adapter_service_name_parity(self) -> None:
        from investigation_agent_platform.domain.topology.models import (
            RepositoryIdentity,
            RepositoryType,
        )
        from investigation_agent_platform.infrastructure.topology.in_memory import (
            InMemoryTopologyAdapter,
        )

        adapter = InMemoryTopologyAdapter()
        repo = RepositoryIdentity(
            tenant_id="tenant-a",
            repository_id="repo-2",
            name="payments-library",
            locator="acme/payments-library",
            git_org_id="git:acme",
            repository_type=RepositoryType.SHARED_LIBRARY,
        )
        adapter.register_service_repository("tenant-a", "payments", repo)

        resolved = await adapter.resolve_for_service_name("tenant-a", "payments")
        assert resolved is not None
        assert resolved.repository_id == "repo-2"
        assert await adapter.resolve_for_service_name("tenant-b", "payments") is None


# ===========================================================================
# Neo4j batch writers: REFERENCES + ACCESSES_TABLE (D5 ISSUE-1/2)
# ===========================================================================


class TestNeo4jEdgeBatches:
    def _two_nodes(self):  # type: ignore[no-untyped-def]
        return _node(), _node(
            qualified_name="mod.bar",
            name="bar",
            start_line=20,
            end_line=25,
        )

    @pytest.mark.asyncio
    async def test_reference_batch_writes_merge(self) -> None:
        from unittest.mock import AsyncMock

        from investigation_agent_platform.domain.topology.models import ReferenceEdgeInput
        from investigation_agent_platform.infrastructure.topology.neo4j_adapter import (
            Neo4jTopologyAdapter,
        )

        a, b = self._two_nodes()
        payload = _payload(ast_nodes=[a, b])
        ref = ReferenceEdgeInput(
            source_node_id=a.node_id,
            target_node_id=b.node_id,
            reference_type="IMPORTS",
            file_path="src/mod.py",
            line_number=1,
            confidence=0.8,
        )
        tx = AsyncMock()
        await Neo4jTopologyAdapter._write_reference_batch(tx, payload, [ref])
        stmt = tx.run.call_args[0][0]
        assert "MERGE (source)-[r:REFERENCES]->(target)" in stmt
        assert "tenant_id" in stmt and "revision" in stmt

    @pytest.mark.asyncio
    async def test_reference_batch_empty_noop(self) -> None:
        from unittest.mock import AsyncMock

        from investigation_agent_platform.infrastructure.topology.neo4j_adapter import (
            Neo4jTopologyAdapter,
        )

        tx = AsyncMock()
        await Neo4jTopologyAdapter._write_reference_batch(tx, _payload(), [])
        tx.run.assert_not_called()

    @pytest.mark.asyncio
    async def test_db_access_batch_writes_merge(self) -> None:
        from unittest.mock import AsyncMock

        from investigation_agent_platform.domain.topology.models import DatabaseAccessEdgeInput
        from investigation_agent_platform.infrastructure.topology.neo4j_adapter import (
            Neo4jTopologyAdapter,
        )

        (a, _) = self._two_nodes()
        payload = _payload(ast_nodes=[a])
        edge = DatabaseAccessEdgeInput(
            source_node_id=a.node_id,
            target_entity_or_table="orders",
            operation_type="SELECT",
            query_fingerprint="fp",
        )
        tx = AsyncMock()
        await Neo4jTopologyAdapter._write_db_access_batch(tx, payload, [edge])
        stmt = tx.run.call_args[0][0]
        assert "MERGE (t:DatabaseTable {tenant_id: $tenant_id, table_name: row.table_name})" in stmt
        assert "MERGE (source)-[e:ACCESSES_TABLE]->(t)" in stmt

    @pytest.mark.asyncio
    async def test_db_access_batch_empty_noop(self) -> None:
        from unittest.mock import AsyncMock

        from investigation_agent_platform.infrastructure.topology.neo4j_adapter import (
            Neo4jTopologyAdapter,
        )

        tx = AsyncMock()
        await Neo4jTopologyAdapter._write_db_access_batch(tx, _payload(), [])
        tx.run.assert_not_called()
