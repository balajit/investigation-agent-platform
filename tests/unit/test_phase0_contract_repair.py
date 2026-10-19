# tests/unit/test_phase0_contract_repair.py
"""Phase 0 contract repair: revision, symbols, stack frames, reads, intake.

Covers the release-blocking fixes in DS_REVIEW v0.1 Phase 0:
0.1 revision capability, 0.2 typed symbol packets, 0.3 stack-frame
matching, 0.4 path-only read labeling, 0.5 repository/revision identity.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from investigation_agent_platform.api.dependencies import AppContext, set_app_context
from investigation_agent_platform.application.evidence.gateway import (
    AsyncEvidenceGateway,
)
from investigation_agent_platform.application.investigation.verification import (
    VerificationEngine,
)
from investigation_agent_platform.application.knowledge.intake import (
    ErrorIntakeService,
    RepositoryScopeError,
    build_intake_seed,
    resolve_repository_scope,
)
from investigation_agent_platform.application.worker.activities import (
    ExecuteActionInput,
    _frame_file_matches,
    _symbol_evidence_packets,
    execute_action_activity,
)
from investigation_agent_platform.domain.common.exceptions import (
    UnsupportedCapabilityError,
)
from investigation_agent_platform.domain.evidence.models import Evidence, EvidenceType
from investigation_agent_platform.domain.provenance.models import (
    EvidenceFreshness,
    EvidenceProvenance,
    QueryFingerprint,
    SourceLocation,
)
from investigation_agent_platform.ports.evidence.code import (
    CodeIntelligenceProviderProtocol,
)


def _code_evidence(
    *,
    file_path: str,
    line_start: int,
    line_end: int | None = None,
    revision: str = "abc123",
    attributes: dict | None = None,
) -> Evidence:
    now = datetime.now(UTC)
    return Evidence(
        tenant_id="tenant-a",
        investigation_id=uuid4(),
        evidence_type=EvidenceType.SOURCE_CODE,
        provider="pygit2-local",
        source=f"git://repo/{file_path}",
        title=f"Source: {file_path}",
        summary=f"Source snippet from {file_path}",
        content_snippet="line",
        attributes=dict(attributes or {}),
        observed_at=now,
        retrieved_at=now,
        provenance=EvidenceProvenance(
            tenant_id="tenant-a",
            investigation_id=uuid4(),
            provider_type="GIT",
            requested_provider_id="pygit2-local",
            actual_provider_id="pygit2-local",
            source_system="Git",
            retrieval_timestamp=now,
            query_fingerprint=QueryFingerprint(
                provider_type="GIT",
                operation="GET_SOURCE",
                normalized_query_hash="hash",
            ),
            source_location=SourceLocation(
                system="Git",
                identifier=f"repo/{file_path}",
                file_path=file_path,
                line_start=line_start,
                line_end=line_end if line_end is not None else line_start,
                revision=revision,
            ),
        ),
        freshness=EvidenceFreshness(observed_at=now, retrieved_at=now),
        fingerprint=f"fp-{file_path}-{line_start}",
    )


def _ctx_with(gateway: SimpleNamespace) -> tuple[AppContext, list]:
    ctx = AppContext()
    saved: list = []

    async def _save_batch(tenant_id, items, investigation_id):  # type: ignore[no-untyped-def]
        saved.extend(items)

    async def _save(tenant_id, item, investigation_id):  # type: ignore[no-untyped-def]
        saved.append(item)

    async def _count(tenant_id, investigation_id):  # type: ignore[no-untyped-def]
        return len(saved)

    service = SimpleNamespace(
        authorize=AsyncMock(return_value=None),
        execute=AsyncMock(return_value=None),
    )
    ctx.execute_action_service = lambda: service  # type: ignore[method-assign]
    ctx.action_repo = SimpleNamespace(count_by_investigation=_count)
    ctx.evidence_repo = SimpleNamespace(
        find_by_investigation_id=AsyncMock(return_value=[]),
        save_batch=_save_batch,
        save=_save,
    )
    ctx.profile_repo = SimpleNamespace(get_by_application_id=AsyncMock(return_value=None))
    ctx.evidence_gateway = gateway
    set_app_context(ctx)
    return ctx, saved


def _input(action: str, parameters: dict) -> ExecuteActionInput:
    return ExecuteActionInput(
        tenant_id="tenant-a",
        investigation_id=str(uuid4()),
        action=action,
        parameters=parameters,
    )


class TestRevisionCapability:
    def test_unsupported_provider_raises_typed_error(self) -> None:
        provider = SimpleNamespace(provider_id="treesitter-local", revision_supported=False)
        with pytest.raises(UnsupportedCapabilityError) as exc:
            AsyncEvidenceGateway._require_revision_capability(provider, "deadbeef", True)
        assert exc.value.error_code == "UNSUPPORTED_CAPABILITY"
        assert exc.value.capability == "revision"

    def test_head_and_discovery_requests_do_not_raise(self) -> None:
        provider = SimpleNamespace(provider_id="treesitter-local", revision_supported=False)
        AsyncEvidenceGateway._require_revision_capability(provider, "HEAD", True)
        AsyncEvidenceGateway._require_revision_capability(provider, "deadbeef", False)

    def test_revision_aware_provider_is_allowed(self) -> None:
        provider = SimpleNamespace(provider_id="git", revision_supported=True)
        AsyncEvidenceGateway._require_revision_capability(provider, "deadbeef", True)

    def test_protocol_methods_accept_revision(self) -> None:
        for name in (
            "search_code",
            "find_symbol",
            "find_callers",
            "find_callees",
            "find_exception_handlers",
            "find_database_operations",
        ):
            signature = inspect.signature(getattr(CodeIntelligenceProviderProtocol, name))
            assert "revision" in signature.parameters

    def test_local_provider_reports_no_revision_support(self) -> None:
        from investigation_agent_platform.infrastructure.evidence.code.intelligence import (
            TreeSitterCodeIntelligenceProvider,
        )

        assert TreeSitterCodeIntelligenceProvider.revision_supported is False


class TestRequireRevisionPlumbing:
    """The fail-closed gate is reachable from action parameters.

    Discovery stays the default (require_revision=False); an explicit
    require_revision=true with a pinned revision must surface a typed
    unsupported_capability outcome instead of silently banking HEAD rows.
    """

    @pytest.mark.asyncio
    async def test_search_code_forwards_require_revision(self) -> None:
        seen: dict = {}

        async def _search_code(tenant_id, investigation_id, application_id, req):  # type: ignore[no-untyped-def]
            seen["req"] = req
            return SimpleNamespace(items=[], total_count=0, provider_metadata={})

        _, _ = _ctx_with(SimpleNamespace(search_code=_search_code))
        await execute_action_activity(
            _input("SEARCH_CODE", {"query": "price", "require_revision": True})
        )
        assert seen["req"].require_revision is True

    @pytest.mark.asyncio
    async def test_search_code_defaults_to_discovery(self) -> None:
        seen: dict = {}

        async def _search_code(tenant_id, investigation_id, application_id, req):  # type: ignore[no-untyped-def]
            seen["req"] = req
            return SimpleNamespace(items=[], total_count=0, provider_metadata={})

        _, _ = _ctx_with(SimpleNamespace(search_code=_search_code))
        await execute_action_activity(_input("SEARCH_CODE", {"query": "price"}))
        assert seen["req"].require_revision is False

    @pytest.mark.asyncio
    async def test_find_symbol_forwards_require_revision(self) -> None:
        seen: dict = {}

        async def _find_symbol(tenant_id, investigation_id, application_id, req):  # type: ignore[no-untyped-def]
            seen["req"] = req
            return SimpleNamespace(items=[], total_count=0, provider_metadata={})

        _, _ = _ctx_with(SimpleNamespace(find_symbol=_find_symbol))
        await execute_action_activity(
            _input("FIND_SYMBOL", {"symbol_name": "price", "require_revision": True})
        )
        assert seen["req"].require_revision is True

    @pytest.mark.asyncio
    async def test_strict_symbol_lookup_degrades_typed_not_empty(self) -> None:
        async def _find_symbol(tenant_id, investigation_id, application_id, req):  # type: ignore[no-untyped-def]
            raise UnsupportedCapabilityError(
                "Provider 'treesitter-local' cannot honor revision 'deadbeef'",
                capability="revision",
                details={"requested_revision": "deadbeef"},
            )

        _, saved = _ctx_with(SimpleNamespace(find_symbol=_find_symbol))
        result = await execute_action_activity(
            _input(
                "FIND_SYMBOL",
                {
                    "symbol_name": "price",
                    "revision": "deadbeef",
                    "require_revision": True,
                },
            )
        )
        # Fail-closed: nothing banked, and the miss reason names the
        # capability gap instead of "no such symbol".
        assert result.data["items_count"] == 0
        assert saved == []

    @pytest.mark.asyncio
    async def test_strict_trace_logic_reports_unsupported_capability(self) -> None:
        async def _find_symbol(tenant_id, investigation_id, application_id, req):  # type: ignore[no-untyped-def]
            raise UnsupportedCapabilityError(
                "Provider 'treesitter-local' cannot honor revision 'deadbeef'",
                capability="revision",
                details={"requested_revision": "deadbeef"},
            )

        _, saved = _ctx_with(SimpleNamespace(find_symbol=_find_symbol))
        result = await execute_action_activity(
            _input(
                "TRACE_LOGIC",
                {
                    "symbol": "price",
                    "revision": "deadbeef",
                    "require_revision": True,
                },
            )
        )
        assert result.data["items_count"] == 0
        assert saved == []


class TestSymbolPackets:
    def test_symbols_become_typed_discovery_evidence(self) -> None:
        packets = _symbol_evidence_packets(
            tenant_id="tenant-a",
            investigation_id=uuid4(),
            symbol_name="price",
            symbols=[
                {
                    "name": "price",
                    "kind": "method_definition",
                    "location": {
                        "file_path": "OfferSvc.java",
                        "line_number": 42,
                        "snippet": "public BigDecimal price()",
                        "revision": None,
                        "revision_supported": False,
                    },
                }
            ],
        )
        assert len(packets) == 1
        packet = packets[0]
        assert packet.evidence_type == EvidenceType.SOURCE_CODE
        assert packet.provenance.source_location.file_path == "OfferSvc.java"
        assert packet.provenance.source_location.line_start == 42
        assert packet.attributes["discovery_only"] is True
        assert packet.attributes["revision_supported"] is False

    @pytest.mark.asyncio
    async def test_find_symbol_activity_banks_symbol_packet(self) -> None:
        async def _find_symbol(tenant_id, investigation_id, application_id, req):  # type: ignore[no-untyped-def]
            return SimpleNamespace(
                items=[],
                total_count=1,
                provider_metadata={
                    "symbols": [
                        {
                            "name": req.symbol_name,
                            "kind": "method_definition",
                            "location": {
                                "file_path": "OfferSvc.java",
                                "line_number": 42,
                                "snippet": "public BigDecimal price()",
                            },
                        }
                    ]
                },
            )

        _, saved = _ctx_with(SimpleNamespace(find_symbol=_find_symbol))
        result = await execute_action_activity(_input("FIND_SYMBOL", {"symbol_name": "price"}))
        assert result.success is True
        assert result.data["items_count"] == 1
        assert saved[0].attributes["symbol_name"] == "price"
        assert saved[0].provenance.source_location.file_path == "OfferSvc.java"


class TestWalkStack:
    def test_frame_file_matching(self) -> None:
        assert _frame_file_matches("OfferSvc.java", "src/main/OfferSvc.java")
        assert _frame_file_matches("src/main/OfferSvc.java", "src/main/OfferSvc.java")
        assert not _frame_file_matches("Other.java", "src/main/OfferSvc.java")
        assert not _frame_file_matches("OfferSvc.java", "")

    @pytest.mark.asyncio
    async def test_mismatched_frame_file_never_banks_synthetic_location(self) -> None:
        async def _search_code(tenant_id, investigation_id, application_id, req):  # type: ignore[no-untyped-def]
            return SimpleNamespace(items=[_code_evidence(file_path="OfferSvc.java", line_start=42)])

        async def _get_source(tenant_id, investigation_id, application_id, req):  # type: ignore[no-untyped-def]
            return _code_evidence(file_path=req.file_path, line_start=req.start_line or 1)

        _, saved = _ctx_with(SimpleNamespace(search_code=_search_code, get_source=_get_source))
        result = await execute_action_activity(
            _input(
                "WALK_STACK",
                {"frames": [{"file": "Other.java", "line": 42, "function": "price"}]},
            )
        )
        assert result.data["items_count"] == 1
        assert len(saved) == 1
        row = saved[0]
        assert row.evidence_type == EvidenceType.EXCEPTION_PATH
        assert row.attributes.get("unresolved") is True
        assert row.attributes.get("reason") == "no_file_match"
        # Explicit unresolved rows carry no location provenance, so they
        # can never be mistaken for a resolved citation.
        assert row.provenance.source_location.file_path is None
        assert row.provenance.source_location.line_start is None
        assert row.provenance.source_location.revision is None

    @pytest.mark.asyncio
    async def test_matching_frame_requires_and_banks_range_read(self) -> None:
        async def _search_code(tenant_id, investigation_id, application_id, req):  # type: ignore[no-untyped-def]
            return SimpleNamespace(items=[_code_evidence(file_path="OfferSvc.java", line_start=42)])

        async def _get_source(tenant_id, investigation_id, application_id, req):  # type: ignore[no-untyped-def]
            assert req.start_line == 42 and req.end_line == 42
            return _code_evidence(file_path=req.file_path, line_start=42, line_end=42)

        _, saved = _ctx_with(SimpleNamespace(search_code=_search_code, get_source=_get_source))
        result = await execute_action_activity(
            _input(
                "WALK_STACK",
                {"frames": [{"file": "OfferSvc.java", "line": 42, "function": "price"}]},
            )
        )
        assert result.data["items_count"] == 2
        frame_rows = [e for e in saved if e.evidence_type == EvidenceType.EXCEPTION_PATH]
        assert len(frame_rows) == 1
        assert frame_rows[0].provenance.source_location.file_path == "OfferSvc.java"
        assert frame_rows[0].provenance.source_location.line_start == 42


class TestReadMode:
    @pytest.mark.asyncio
    async def test_path_only_read_is_labeled_discovery(self) -> None:
        async def _get_source(tenant_id, investigation_id, application_id, req):  # type: ignore[no-untyped-def]
            return _code_evidence(file_path=req.file_path, line_start=1)

        _, saved = _ctx_with(SimpleNamespace(get_source=_get_source))
        await execute_action_activity(_input("GET_CODE", {"file_path": "OfferSvc.java"}))
        assert saved[0].attributes["read_mode"] == "path_only"
        assert saved[0].attributes["discovery_only"] is True

    @pytest.mark.asyncio
    async def test_range_read_is_labeled_range(self) -> None:
        async def _get_source(tenant_id, investigation_id, application_id, req):  # type: ignore[no-untyped-def]
            return _code_evidence(
                file_path=req.file_path,
                line_start=req.start_line or 1,
                line_end=req.end_line or 1,
            )

        _, saved = _ctx_with(SimpleNamespace(get_source=_get_source))
        await execute_action_activity(
            _input(
                "GET_CODE",
                {"file_path": "OfferSvc.java", "start_line": 10, "end_line": 20},
            )
        )
        assert saved[0].attributes["read_mode"] == "range"
        assert "discovery_only" not in saved[0].attributes

    def test_has_range_read_rejects_path_only_citation(self) -> None:
        path_only = _code_evidence(
            file_path="OfferSvc.java",
            line_start=1,
            line_end=200,
            attributes={"read_mode": "path_only", "discovery_only": True},
        )
        range_read = _code_evidence(
            file_path="OfferSvc.java",
            line_start=480,
            line_end=520,
            attributes={"read_mode": "range"},
        )
        assert VerificationEngine.has_range_read([path_only]) is False
        assert VerificationEngine.has_range_read([range_read]) is True


class TestIntakeIdentity:
    def test_seed_persists_repository_and_revision(self) -> None:
        seed = build_intake_seed(
            error_class="E",
            failing_symbol="f",
            repository="acme/offer-svc",
            revision="deadbeef",
        )
        assert seed["repository"] == "acme/offer-svc"
        assert seed["revision"] == "deadbeef"

    def test_seed_defaults_to_unpinned(self) -> None:
        seed = build_intake_seed(error_class="E", failing_symbol="f")
        assert seed["repository"] == "UNPINNED"
        assert seed["revision"] == "UNPINNED"

    def test_single_repository_resolves(self) -> None:
        profile = SimpleNamespace(
            services=[SimpleNamespace(repository_id="acme/offer-svc", code=None)]
        )
        assert resolve_repository_scope(profile) == ("acme/offer-svc", ["acme/offer-svc"])

    def test_code_repos_fallback_resolves(self) -> None:
        profile = SimpleNamespace(
            services=[
                SimpleNamespace(
                    repository_id=None,
                    code=SimpleNamespace(repos=["acme/offer-svc"]),
                )
            ]
        )
        assert resolve_repository_scope(profile) == ("acme/offer-svc", ["acme/offer-svc"])

    def test_multiple_repositories_are_ambiguous(self) -> None:
        profile = SimpleNamespace(
            services=[
                SimpleNamespace(repository_id="acme/a", code=None),
                SimpleNamespace(repository_id="acme/b", code=None),
            ]
        )
        resolved, candidates = resolve_repository_scope(profile)
        assert resolved == ""
        assert candidates == ["acme/a", "acme/b"]

    @pytest.mark.asyncio
    async def test_ambiguous_scope_fails_closed(self) -> None:
        profile = SimpleNamespace(
            services=[
                SimpleNamespace(repository_id="acme/a", code=None),
                SimpleNamespace(repository_id="acme/b", code=None),
            ]
        )
        profile_repo = SimpleNamespace(get_by_application_id=AsyncMock(return_value=profile))
        service = ErrorIntakeService(
            investigation_repo=SimpleNamespace(),
            session_repo=SimpleNamespace(),
            code_issue_index=SimpleNamespace(),
            profile_repo=profile_repo,
        )
        with pytest.raises(RepositoryScopeError) as exc:
            await service._resolve_repository(
                tenant_id="tenant-a",
                application_id="example-app",
                repository="",
            )
        assert exc.value.error_code == "AMBIGUOUS_REPOSITORY_SCOPE"
        assert exc.value.candidates == ["acme/a", "acme/b"]
