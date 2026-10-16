# tests/unit/test_profile_consumers.py
"""Phase 3 consumer-adoption tests: profile-driven platform behavior."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from investigation_agent_platform.api.dependencies import _DEFAULT_PROFILE, AppContext
from investigation_agent_platform.domain.common.exceptions import DomainValidationException


@pytest.fixture
def _restore_context():
    import investigation_agent_platform.api.dependencies as deps

    prev = deps._context
    yield
    deps._context = prev


def _iid():
    return uuid4()


class _InvestigationRepo:
    def __init__(self, app_id="example-app"):
        self._app_id = app_id

    async def get_by_id(self, tenant_id, investigation_id):
        return SimpleNamespace(
            id=investigation_id, tenant_id=tenant_id, application_id=self._app_id
        )


class _ProfileRepo:
    def __init__(self, profile):
        self._profile = profile

    async def get_by_application_id(self, tenant_id, application_id, version=None):
        return self._profile

    async def list(self, tenant_id, status=None):
        return [self._profile]

    async def list_revisions(self, tenant_id, application_id):
        return [self._profile]


def _request(services=None, environment="production"):
    from investigation_agent_platform.domain.evidence.requests import RuntimeEvidenceRequest

    return RuntimeEvidenceRequest(environment=environment, services=services or [])


class TestServiceAllowlist:
    def _service(self, gateway):
        from investigation_agent_platform.application.investigation.runtime_evidence import (
            RuntimeEvidenceService,
        )

        return RuntimeEvidenceService(
            gateway=gateway,
            investigation_repo=_InvestigationRepo(),
            evidence_repo=SimpleNamespace(save_batch=None),
            profile_repo=_ProfileRepo(_DEFAULT_PROFILE),
        )

    @pytest.mark.asyncio
    async def test_unknown_service_rejected_fail_closed(self) -> None:
        from investigation_agent_platform.application.investigation.context import (
            InvestigationScope,
        )

        gateway = SimpleNamespace()
        service = self._service(gateway)
        scope = InvestigationScope.create(tenant_id="tenant-a", investigation_id=_iid())
        with pytest.raises(DomainValidationException, match="Unknown services"):
            await service.search(scope, _request(services=["ghost-svc"]))

    @pytest.mark.asyncio
    async def test_known_service_passes_through(self) -> None:
        from investigation_agent_platform.application.evidence.gateway import (
            EvidenceQueryResult,
        )
        from investigation_agent_platform.application.investigation.context import (
            InvestigationScope,
        )

        calls = []

        async def _search(tenant_id, investigation_id, application_id, request):
            calls.append(request)
            return EvidenceQueryResult(items=[], has_more=False, total_count=0)

        gateway = SimpleNamespace(search_runtime_evidence=_search)
        service = self._service(gateway)
        # evidence_repo.save_batch must be awaitable for the hit path (no hits here).
        service._evidence_repo = SimpleNamespace(
            save_batch=lambda *a, **k: _noop_coro(),
        )
        scope = InvestigationScope.create(tenant_id="tenant-a", investigation_id=_iid())
        await service.search(scope, _request(services=["payments-service"]))
        assert len(calls) == 1
        assert calls[0].mapping_source_id == "generic-ecs"


async def _noop_coro():
    return None


class TestEvidenceBudgetOverlay:
    def test_applies_known_positive_ints(self) -> None:
        from investigation_agent_platform.application.worker.activities import (
            overlay_evidence_budget,
        )
        from investigation_agent_platform.domain.investigation.models import InvestigationLimits

        profile = _DEFAULT_PROFILE.model_copy(
            update={
                "investigation": _DEFAULT_PROFILE.investigation.model_copy(
                    update={"evidence_budget": {"max_tool_calls": 7, "max_evidence_items": 3}}
                )
            }
        )
        out = overlay_evidence_budget(InvestigationLimits(), profile)
        assert out.max_tool_calls == 7
        assert out.max_evidence_items == 3
        # The profile's canonical maximumHypotheses applies when the
        # evidence_budget dict does not set max_hypotheses.
        assert out.max_hypotheses == _DEFAULT_PROFILE.investigation.max_hypotheses

    def test_ignores_unknown_and_invalid_keys(self) -> None:
        from investigation_agent_platform.application.worker.activities import (
            overlay_evidence_budget,
        )
        from investigation_agent_platform.domain.investigation.models import InvestigationLimits

        profile = _DEFAULT_PROFILE.model_copy(
            update={
                "investigation": _DEFAULT_PROFILE.investigation.model_copy(
                    update={
                        "evidence_budget": {
                            "max_tool_calls": 0,
                            "nope": 5,
                            "max_evidence_items": -2,
                        }
                    }
                )
            }
        )
        out = overlay_evidence_budget(InvestigationLimits(), profile)
        assert out.max_tool_calls == InvestigationLimits().max_tool_calls
        assert out.max_evidence_items == InvestigationLimits().max_evidence_items
        assert out.max_hypotheses == _DEFAULT_PROFILE.investigation.max_hypotheses

    def test_absent_budget_keeps_defaults(self) -> None:
        from investigation_agent_platform.application.worker.activities import (
            overlay_evidence_budget,
        )
        from investigation_agent_platform.domain.investigation.models import InvestigationLimits

        out = overlay_evidence_budget(InvestigationLimits(), _DEFAULT_PROFILE)
        assert out.max_tool_calls == InvestigationLimits().max_tool_calls
        assert out.max_evidence_items == InvestigationLimits().max_evidence_items
        assert out.max_hypotheses == _DEFAULT_PROFILE.investigation.max_hypotheses


class TestChatClassificationCeiling:
    def _service(self, profile=None):
        from investigation_agent_platform.application.chat.chat_service import ChatService

        ctx = AppContext()
        return ChatService(
            ctx.chat_repo,
            profile_repo=_ProfileRepo(profile) if profile is not None else None,
        )

    @pytest.mark.asyncio
    async def test_restricted_clamped_to_internal(self, _restore_context: None) -> None:
        profile = _DEFAULT_PROFILE.model_copy(update={"data_classification": "INTERNAL"})
        service = self._service(profile)
        session = await service.create_session(
            "tenant-a", application_id="example-app", classification="RESTRICTED"
        )
        assert session.classification == "INTERNAL"

    @pytest.mark.asyncio
    async def test_lower_classification_untouched(self, _restore_context: None) -> None:
        profile = _DEFAULT_PROFILE.model_copy(update={"data_classification": "CONFIDENTIAL"})
        service = self._service(profile)
        session = await service.create_session(
            "tenant-a", application_id="example-app", classification="INTERNAL"
        )
        assert session.classification == "INTERNAL"

    @pytest.mark.asyncio
    async def test_no_profile_repo_no_clamp(self, _restore_context: None) -> None:
        service = self._service()
        session = await service.create_session("tenant-a", classification="RESTRICTED")
        assert session.classification == "RESTRICTED"


class TestReasonApplicationContext:
    @pytest.mark.asyncio
    async def test_context_block_carries_domain_planes_services(self) -> None:
        from investigation_agent_platform.application.investigation.reasoning import (
            ReasoningCoordinator,
        )

        coord = ReasoningCoordinator(
            prompt_safety_policy=SimpleNamespace(validate_prompt_safety=None),
            profile_repo=_ProfileRepo(
                _DEFAULT_PROFILE.model_copy(
                    update={
                        "domain": "payments",
                        "planes": ["backend"],
                        "data_classification": "INTERNAL",
                    }
                )
            ),
        )
        block = await coord._application_context_block("tenant-a", "example-app")
        assert block["services"] == [{"name": "payments-service", "role": "edge"}]
        assert block["domain"] == "payments"
        assert block["planes"] == ["backend"]
        assert block["data_classification"] == "INTERNAL"

    @pytest.mark.asyncio
    async def test_no_repo_yields_empty_block(self) -> None:
        from investigation_agent_platform.application.investigation.reasoning import (
            ReasoningCoordinator,
        )

        coord = ReasoningCoordinator(
            prompt_safety_policy=SimpleNamespace(validate_prompt_safety=None),
        )
        assert await coord._application_context_block("tenant-a", "x") == {}

    @pytest.mark.asyncio
    async def test_state_templates_carry_bind_parameters(self) -> None:
        from types import SimpleNamespace

        from investigation_agent_platform.application.investigation.reasoning import (
            ReasoningCoordinator,
        )

        profile = SimpleNamespace(
            domain=None,
            planes=[],
            data_classification=None,
            services=[],
            datasources=[
                SimpleNamespace(
                    id="primary",
                    kind="postgres",
                    query_templates={
                        "by_status": SimpleNamespace(
                            description="rows by status",
                            sql="SELECT id FROM t WHERE tenant_id = :tenant_id AND status = :status",
                        ),
                    },
                )
            ],
        )
        coord = ReasoningCoordinator(
            prompt_safety_policy=SimpleNamespace(validate_prompt_safety=None),
            profile_repo=_ProfileRepo(profile),
        )
        block = await coord._application_context_block("tenant-a", "example-app")
        assert block["state_templates"] == [
            {
                "template_id": "primary.by_status",
                "datasource_kind": "postgres",
                "description": "rows by status",
                "bind_parameters": ["status"],
            }
        ]


class TestKnowledgeConfigExplicitOnly:
    @pytest.mark.asyncio
    async def test_default_profile_has_no_confidence_floor(self) -> None:
        from investigation_agent_platform.application.knowledge.retrieve import (
            KnowledgeRetrievalService,
        )

        service = KnowledgeRetrievalService(
            artifact_repo=SimpleNamespace(), profile_repo=_ProfileRepo(_DEFAULT_PROFILE)
        )
        assert await service._confidence_floor("tenant-a", "example-app") is None

    @pytest.mark.asyncio
    async def test_explicit_min_confidence_applies(self) -> None:
        from investigation_agent_platform.application.knowledge.retrieve import (
            KnowledgeRetrievalService,
        )
        from investigation_agent_platform.domain.profile.models import KnowledgeConfig

        profile = _DEFAULT_PROFILE.model_copy(
            update={"knowledge": KnowledgeConfig(min_confidence=0.85)}
        )
        service = KnowledgeRetrievalService(
            artifact_repo=SimpleNamespace(), profile_repo=_ProfileRepo(profile)
        )
        assert await service._confidence_floor("tenant-a", "example-app") == 0.85
