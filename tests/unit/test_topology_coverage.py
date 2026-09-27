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
