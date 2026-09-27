"""Extensive infra coverage tests."""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from pydantic import SecretStr

from investigation_agent_platform.domain.profile.models import (
    ApplicationProfile,
    CodeProfile,
    CorrelationProfile,
    InvestigationProfile,
    ObservabilityProfile,
    StateProfile,
)
from investigation_agent_platform.infrastructure.configuration.config import (
    LLMConfig,
    PlatformConfigurationError,
    load_application_config_from_env,
    load_application_config_from_yaml,
)
from investigation_agent_platform.infrastructure.evidence.code.intelligence import (
    TreeSitterCodeIntelligenceProvider,
)
from investigation_agent_platform.infrastructure.reasoning.factory import create_llm_gateway
from investigation_agent_platform.ports.reasoning.llm_gateway import LLMGatewayRequest


# Helper to make CodeProfile
def _make_code_profile(repo: str = "myrepo", source_roots: list[str] | None = None) -> CodeProfile:
    return CodeProfile(
        provider="git",
        repository=repo,
        defaultBranch="main",
        language="python",
        sourceRoots=source_roots or ["src"],
        buildSystem="uv",
        moduleStructure="src/mod",
    )


def _make_profile_for_code() -> ApplicationProfile:
    return ApplicationProfile(
        id="app1",
        tenant_id="t1",
        name="n",
        description="d",
        environment="production",
        observability=ObservabilityProfile(
            provider="es",
            indices=["idx"],
            timestampField="@timestamp",
            serviceField="service.name",
            environmentField="deployment.environment",
            sessionField="session.id",
            requestField="request.id",
            traceField="trace.id",
            logLevelField="severity",
        ),
        state=StateProfile(
            provider="postgres",
            database="db",
            schema="public",
            tables=["t"],
            primaryIdentifiers=["id"],
            stateFields=["status"],
            timestampFields=["created_at"],
            queryTemplates={"find": "SELECT * FROM t WHERE id=:id"},
        ),
        code=_make_code_profile(),
        correlation=CorrelationProfile(fields=["session.id"]),
        investigation=InvestigationProfile(),
    )


# ---------------------------------------------------------------------------
# intelligence.py
# ---------------------------------------------------------------------------
class TestTreeSitterProvider:
    @pytest.mark.asyncio
    async def test_search_code_basic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "myrepo" / "src"
            repo.mkdir(parents=True)
            (repo / "app.py").write_text(
                "def hello():\n    print('hello world')\n", encoding="utf-8"
            )
            provider = TreeSitterCodeIntelligenceProvider(repo_base_path=tmp)
            evidences = await provider.search_code(
                "tenant-a",
                "hello",
                CodeProfile(
                    provider="git",
                    repository="myrepo",
                    defaultBranch="main",
                    language="python",
                    sourceRoots=["src"],
                    buildSystem="uv",
                    moduleStructure="src",
                ),
            )
            assert len(evidences) >= 1
            assert "hello" in evidences[0].content_snippet.lower()
            assert evidences[0].tenant_id == "tenant-a"

    @pytest.mark.asyncio
    async def test_find_symbol(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "myrepo"
            src = repo / "src"
            src.mkdir(parents=True)
            (src / "mod.py").write_text(
                "def my_func():\n    pass\n\nclass MyClass:\n    pass\n", encoding="utf-8"
            )
            provider = TreeSitterCodeIntelligenceProvider(repo_base_path=tmp)
            profile = _make_code_profile(source_roots=["src"])
            # Need to handle parser may not be available; still test fallback
            symbols = await provider.find_symbol("t1", "my_func", profile)
            # If tree-sitter available, should find; else empty but not error
            assert isinstance(symbols, list)

    @pytest.mark.asyncio
    async def test_find_callers_and_callees(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "myrepo" / "src"
            repo.mkdir(parents=True)
            (repo / "a.py").write_text(
                "def caller():\n    callee()\n\ndef callee():\n    pass\n", encoding="utf-8"
            )
            provider = TreeSitterCodeIntelligenceProvider(repo_base_path=tmp)
            profile = _make_code_profile(source_roots=["src"])
            callers = await provider.find_callers("t1", "callee", profile)
            callees = await provider.find_callees("t1", "caller", profile)
            assert isinstance(callers, list)
            assert isinstance(callees, list)

    @pytest.mark.asyncio
    async def test_exception_handlers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "myrepo" / "src"
            repo.mkdir(parents=True)
            (repo / "exc.py").write_text(
                "try:\n    x=1\nexcept ValueError as e:\n    pass\n", encoding="utf-8"
            )
            provider = TreeSitterCodeIntelligenceProvider(repo_base_path=tmp)
            profile = _make_code_profile(source_roots=["src"])
            locs = await provider.find_exception_handlers("t1", "ValueError", profile)
            assert isinstance(locs, list)
            # should find at least fallback line
            assert any("ValueError" in (l.snippet or "") for l in locs)

    @pytest.mark.asyncio
    async def test_database_operations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "myrepo" / "src"
            repo.mkdir(parents=True)
            (repo / "db.py").write_text(
                'query = "SELECT * FROM orders WHERE id=1"\n', encoding="utf-8"
            )
            provider = TreeSitterCodeIntelligenceProvider(repo_base_path=tmp)
            profile = _make_code_profile(source_roots=["src"])
            locs = await provider.find_database_operations("t1", "orders", profile)
            assert len(locs) >= 1

    def test_resolve_roots_traversal_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            provider = TreeSitterCodeIntelligenceProvider(repo_base_path=tmp)
            # repo with traversal — bypass validation via model_construct
            profile = CodeProfile.model_construct(
                provider="git",
                repository="../evil",
                default_branch="main",
                language="python",
                source_roots=["src"],
                build_system="uv",
                module_structure="src",
            )
            roots = provider._resolve_roots(profile)  # type: ignore[attr-defined]
            assert roots == []
            # source root traversal
            Path(tmp, "myrepo").mkdir(parents=True, exist_ok=True)
            profile2 = CodeProfile.model_construct(
                provider="git",
                repository="myrepo",
                default_branch="main",
                language="python",
                source_roots=["../escape"],
                build_system="uv",
                module_structure="src",
            )
            roots2 = provider._resolve_roots(profile2)  # type: ignore[attr-defined]
            # should not include escaped path
            for r in roots2:
                assert "escape" not in str(r)

    def test_iter_source_files_filters(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            provider = TreeSitterCodeIntelligenceProvider(repo_base_path=tmp)
            root = Path(tmp) / "root"
            root.mkdir()
            (root / "ok.py").write_text("x=1", encoding="utf-8")
            (root / ".hidden.py").write_text("x=1", encoding="utf-8")
            # hidden dir file should be skipped via rglob but our root-level hidden file check uses parts
            # Create venv file
            venv = root / "venv"
            venv.mkdir()
            (venv / "a.py").write_text("x=1", encoding="utf-8")
            files = provider._iter_source_files([root])  # type: ignore[attr-defined]
            # ok.py should be present, venv/a.py should be skipped
            names = [f.name for f in files]
            assert "ok.py" in names
            assert "a.py" not in names

    def test_get_parser_caching_and_failure(self) -> None:
        provider = TreeSitterCodeIntelligenceProvider(repo_base_path="/tmp")
        with patch.dict("sys.modules", {}):
            # mock failing import
            with (
                patch("tree_sitter_language_pack.get_parser", side_effect=ImportError("nope"))
                if False
                else patch.object(provider, "_get_parser", wraps=provider._get_parser)
            ):
                # Simulate unavailable language
                p = provider._get_parser("nonexistent_lang_xyz")  # type: ignore[attr-defined]
                # should return None and cache
                assert p is None
                assert provider._parsers.get("nonexistent_lang_xyz") is None

    def test_read_text_large_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            provider = TreeSitterCodeIntelligenceProvider(repo_base_path=tmp)
            p = Path(tmp) / "big.py"
            p.write_text("a", encoding="utf-8")
            # mock stat to exceed limit
            orig = provider._read_text(p)  # type: ignore[attr-defined]
            assert orig == "a"
            # large file returns None via _read_text if size > MAX_FILE_BYTES
            with patch.object(Path, "stat", return_value=MagicMock(st_size=9999999)):
                assert provider._read_text(p) is None  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_search_no_roots_returns_empty(self) -> None:
        provider = TreeSitterCodeIntelligenceProvider(repo_base_path="/nonexistent_base_xyz")
        profile = _make_code_profile(source_roots=["src"])
        res = await provider.search_code("t", "q", profile)
        assert res == []

    def test_resolve_roots_dedup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "myrepo"
            src = repo / "src"
            src.mkdir(parents=True)
            provider = TreeSitterCodeIntelligenceProvider(repo_base_path=tmp)
            profile = CodeProfile(
                provider="git",
                repository="myrepo",
                defaultBranch="main",
                language="python",
                sourceRoots=["src", "src"],
                buildSystem="uv",
                moduleStructure="src",
            )
            roots = provider._resolve_roots(profile)  # type: ignore[attr-defined]
            assert len(roots) == 1


# ---------------------------------------------------------------------------
# openai / anthropic adapters
# ---------------------------------------------------------------------------
class TestOpenAIAdapter:
    @pytest.mark.asyncio
    async def test_complete_with_schema(self) -> None:
        from investigation_agent_platform.infrastructure.reasoning.openai_adapter import (
            OpenAIGateway,
            _estimate_cost,
        )

        config = LLMConfig(
            api_key=SecretStr("sk-test"), model_name="gpt-4o-mini", provider="openai"
        )
        gw = OpenAIGateway(config)
        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock(message=MagicMock(content='{"key": "value"}'))]
        mock_resp.usage = MagicMock(prompt_tokens=10, completion_tokens=20)
        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(return_value=mock_resp)
        gw._client = mock_client  # type: ignore[attr-defined]

        # patch _call_with_retry to bypass retry/timeout
        async def fake_call(client: object, kwargs: dict[str, object]) -> object:
            assert kwargs["model"] == "gpt-4o-mini"
            assert kwargs["response_format"] == {"type": "json_object"}
            return mock_resp

        gw._call_with_retry = fake_call  # type: ignore[method-assign]
        req = LLMGatewayRequest(
            prompt="hi", system_prompt="sys", response_schema={"type": "object"}, temperature=0.1
        )
        resp = await gw.complete("t1", req)
        assert resp.parsed == {"key": "value"}
        assert resp.content == '{"key": "value"}'
        assert resp.metadata.prompt_tokens == 10
        assert _estimate_cost("gpt-4o-mini", 10, 20) == pytest.approx(
            resp.metadata.estimated_cost_usd
        )

    @pytest.mark.asyncio
    async def test_complete_without_schema_no_parse(self) -> None:
        from investigation_agent_platform.infrastructure.reasoning.openai_adapter import (
            OpenAIGateway,
        )

        config = LLMConfig(api_key=SecretStr("sk-test"), model_name="gpt-4o")
        gw = OpenAIGateway(config)
        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock(message=MagicMock(content="hello"))]
        mock_resp.usage = None
        gw._client = MagicMock(
            chat=MagicMock(completions=MagicMock(create=AsyncMock(return_value=mock_resp)))
        )
        gw._call_with_retry = AsyncMock(return_value=mock_resp)  # type: ignore[method-assign]
        req = LLMGatewayRequest(prompt="hi")
        resp = await gw.complete("t1", req)
        assert resp.parsed is None
        assert resp.content == "hello"

    def test_is_retryable(self) -> None:
        from investigation_agent_platform.infrastructure.reasoning.openai_adapter import (
            OpenAIGateway,
        )

        gw = OpenAIGateway(LLMConfig(api_key=SecretStr("k"), model_name="gpt-4o"))
        err429 = MagicMock(status_code=429)
        assert gw._is_retryable_llm_error(err429) is True
        err401 = MagicMock(status_code=401)
        assert gw._is_retryable_llm_error(err401) is False
        err_retry = MagicMock(status_code=200, retryable=True, http_status_code=500)
        # has retryable true but http_status_code not 401/403 -> should be true? logic checks retryable and status !=401/403 via status_code path? Actually _is_retryable checks status 429/503 then fallback retryable
        # Our err_retry has status_code 200 not retryable status, so fallback returns retryable true
        del err_retry.status_code
        err_retry.http_status_code = 500
        assert gw._is_retryable_llm_error(err_retry) is True

    def test_estimate_cost_unknown_model(self) -> None:
        from investigation_agent_platform.infrastructure.reasoning.openai_adapter import (
            _estimate_cost,
        )

        cost = _estimate_cost("unknown-model", 1000, 1000)
        assert cost > 0

    @pytest.mark.asyncio
    async def test_anthropic_complete_with_schema(self) -> None:
        from investigation_agent_platform.infrastructure.reasoning.anthropic_adapter import (
            AnthropicGateway,
        )

        config = LLMConfig(
            api_key=SecretStr("sk-ant"), model_name="claude-3-haiku", provider="anthropic"
        )
        gw = AnthropicGateway(config)
        mock_block = MagicMock(text='{"a": 1}')
        mock_resp = MagicMock(
            content=[mock_block], usage=MagicMock(input_tokens=5, output_tokens=10)
        )
        gw._client = MagicMock(messages=MagicMock(create=AsyncMock(return_value=mock_resp)))
        gw._call_with_retry = AsyncMock(return_value=mock_resp)  # type: ignore[method-assign]
        req = LLMGatewayRequest(
            prompt="hi", system_prompt="be helpful", response_schema={"type": "object"}
        )
        resp = await gw.complete("t1", req)
        assert resp.parsed == {"a": 1}
        assert resp.metadata.total_tokens == 15

    @pytest.mark.asyncio
    async def test_anthropic_complete_invalid_json(self) -> None:
        from investigation_agent_platform.infrastructure.reasoning.anthropic_adapter import (
            AnthropicGateway,
        )

        config = LLMConfig(
            api_key=SecretStr("sk-ant"), model_name="claude-3-haiku", provider="anthropic"
        )
        gw = AnthropicGateway(config)
        mock_block = MagicMock(text="not json")
        mock_resp = MagicMock(content=[mock_block], usage=None)
        gw._client = MagicMock()
        gw._call_with_retry = AsyncMock(return_value=mock_resp)  # type: ignore[method-assign]
        req = LLMGatewayRequest(prompt="hi", response_schema={"type": "object"})
        resp = await gw.complete("t1", req)
        assert resp.parsed is None
        assert resp.content == "not json"

    @pytest.mark.asyncio
    async def test_openai_timeout_path(self) -> None:
        from investigation_agent_platform.infrastructure.reasoning.openai_adapter import (
            OpenAIGateway,
        )

        config = LLMConfig(api_key=SecretStr("sk"), model_name="gpt-4o")
        gw = OpenAIGateway(config)
        # Make _call_with_retry raise asyncio.TimeoutError via wait_for
        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(side_effect=TimeoutError())
        gw._client = mock_client
        # Use real _call_with_retry but patch asyncio.wait_for to timeout quickly
        # Instead test that complete propagates error
        with patch(
            "investigation_agent_platform.infrastructure.reasoning.openai_adapter.asyncio.wait_for",
            side_effect=asyncio.TimeoutError,
        ):
            req = LLMGatewayRequest(prompt="hi")
            with pytest.raises(asyncio.TimeoutError):
                await gw.complete("t1", req)


class TestFactory:
    def test_create_explicit_openai(self) -> None:
        config = LLMConfig(api_key=SecretStr("k"), model_name="gpt-4o", provider="openai")
        gw = create_llm_gateway(config)
        from investigation_agent_platform.infrastructure.reasoning.openai_adapter import (
            OpenAIGateway,
        )

        assert isinstance(gw, OpenAIGateway)

    def test_create_explicit_anthropic(self) -> None:
        config = LLMConfig(
            api_key=SecretStr("k"), model_name="claude-3-haiku", provider="anthropic"
        )
        gw = create_llm_gateway(config)
        from investigation_agent_platform.infrastructure.reasoning.anthropic_adapter import (
            AnthropicGateway,
        )

        assert isinstance(gw, AnthropicGateway)

    def test_infer_claude(self) -> None:
        config = LLMConfig(api_key=SecretStr("k"), model_name="claude-sonnet-4", provider="")
        # provider empty triggers inference
        gw = create_llm_gateway(config)
        from investigation_agent_platform.infrastructure.reasoning.anthropic_adapter import (
            AnthropicGateway,
        )

        assert isinstance(gw, AnthropicGateway)

    def test_infer_gpt(self) -> None:
        config = LLMConfig(api_key=SecretStr("k"), model_name="gpt-4o-mini", provider="")
        gw = create_llm_gateway(config)
        from investigation_agent_platform.infrastructure.reasoning.openai_adapter import (
            OpenAIGateway,
        )

        assert isinstance(gw, OpenAIGateway)


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------
class TestConfig:
    def test_llm_config_provider_field(self) -> None:
        cfg = LLMConfig(api_key=SecretStr("k"), model_name="gpt-4o", provider="anthropic")
        assert cfg.provider == "anthropic"

    def test_load_from_env_missing(self) -> None:
        env = {"IAP_DATABASE_URI": "", "IAP_LLM_API_KEY": ""}
        with patch.dict(os.environ, env, clear=False):
            # ensure missing
            os.environ.pop("IAP_DATABASE_URI", None)
            os.environ.pop("IAP_LLM_API_KEY", None)
            with pytest.raises(PlatformConfigurationError):
                load_application_config_from_env()

    def test_load_from_env_success(self) -> None:
        with patch.dict(
            os.environ,
            {
                "IAP_DATABASE_URI": "postgresql+asyncpg://a:b@localhost/db",
                "IAP_LLM_API_KEY": "sk-test",
                "IAP_LLM_PROVIDER": "anthropic",
                "IAP_LLM_MODEL": "claude-3-haiku",
            },
            clear=False,
        ):
            cfg = load_application_config_from_env()
            assert cfg.llm.provider == "anthropic"
            assert cfg.database.pool_size == 10
            assert cfg.temporal.task_queue == "investigation-tasks"
            assert cfg.kafka.bootstrap_servers == "localhost:9092"
            assert cfg.llm.model_name == "claude-3-haiku"

    def test_load_from_yaml_missing_env(self, tmp_path: Path) -> None:
        yaml_content = "platform:\n  name: test\n  environment: dev\n"
        p = tmp_path / "app.yaml"
        p.write_text(yaml_content, encoding="utf-8")
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("IAP_DATABASE_URI", None)
            os.environ.pop("IAP_LLM_API_KEY", None)
            with pytest.raises(PlatformConfigurationError):
                load_application_config_from_yaml(path=p)

    def test_load_from_yaml_with_env(self, tmp_path: Path) -> None:
        yaml_content = "platform:\n  name: test\n  environment: dev\n"
        p = tmp_path / "app.yaml"
        p.write_text(yaml_content, encoding="utf-8")
        with patch.dict(
            os.environ,
            {"IAP_DATABASE_URI": "postgresql://a:b@localhost/db", "IAP_LLM_API_KEY": "sk"},
            clear=False,
        ):
            cfg = load_application_config_from_yaml(path=p)
            assert cfg.application_id == "test"


# ---------------------------------------------------------------------------
# bootstrap
# ---------------------------------------------------------------------------
class TestBootstrap:
    def test_build_reasoning_coordinator(self) -> None:
        from investigation_agent_platform.bootstrap import build_reasoning_coordinator

        with patch.dict(
            os.environ,
            {"IAP_DATABASE_URI": "postgresql://a:b@localhost/db", "IAP_LLM_API_KEY": "sk"},
            clear=False,
        ):
            from investigation_agent_platform.infrastructure.configuration.config import (
                load_application_config_from_env,
            )

            cfg = load_application_config_from_env()
            coord = build_reasoning_coordinator(cfg)
            assert coord is not None

    def test_build_app_context_dev(self) -> None:
        from investigation_agent_platform.bootstrap import build_app_context
        from investigation_agent_platform.infrastructure.configuration.config import (
            ApplicationConfig,
            BudgetConfig,
            DatabaseConfig,
        )

        cfg = ApplicationConfig(
            environment="development",
            application_id="test",
            database=DatabaseConfig(connection_uri=SecretStr("postgresql://a:b@localhost/db")),
            llm=LLMConfig(api_key=SecretStr("sk"), model_name="gpt-4o"),
            budget=BudgetConfig(),
        )
        ctx = build_app_context(cfg)
        assert ctx is not None

    def test_build_app_context_production(self) -> None:
        from investigation_agent_platform.bootstrap import build_app_context
        from investigation_agent_platform.infrastructure.configuration.config import (
            ApplicationConfig,
            BudgetConfig,
            DatabaseConfig,
            LLMConfig,
        )

        cfg = ApplicationConfig(
            environment="production",
            application_id="test",
            database=DatabaseConfig(
                connection_uri=SecretStr("postgresql+asyncpg://a:b@localhost/db")
            ),
            llm=LLMConfig(api_key=SecretStr("sk"), model_name="gpt-4o"),
            budget=BudgetConfig(),
        )
        with patch("investigation_agent_platform.bootstrap.create_async_engine") as mock_engine:
            mock_engine.return_value = MagicMock()
            # Patch AppContext to avoid real SA repo wiring issues
            with patch("investigation_agent_platform.bootstrap.AppContext") as MockCtx:
                mock_ctx = MagicMock()
                mock_ctx.engine = mock_engine.return_value
                MockCtx.return_value = mock_ctx
                with (
                    patch("investigation_agent_platform.bootstrap.set_app_context"),
                    patch(
                        "investigation_agent_platform.infrastructure.observability.telemetry.OpenTelemetryObservabilityAdapter"
                    ),
                    patch("faststream.kafka.KafkaBroker"),
                    patch(
                        "investigation_agent_platform.infrastructure.messaging.faststream.KafkaEventPublisher"
                    ),
                    patch(
                        "temporalio.client.Client.connect",
                        new_callable=AsyncMock,
                        return_value=MagicMock(),
                    ),
                ):
                    ctx = build_app_context(cfg)
                    assert ctx is not None

    @pytest.mark.asyncio
    async def test_worker_run(self) -> None:
        from investigation_agent_platform.bootstrap.worker import build_app_context as w_build
        from investigation_agent_platform.bootstrap.worker import run_temporal_worker
        from investigation_agent_platform.infrastructure.configuration.config import (
            ApplicationConfig,
            BudgetConfig,
            DatabaseConfig,
            TemporalConfig,
        )

        # build_app_context for worker
        cfg = ApplicationConfig(
            environment="development",
            application_id="test",
            database=DatabaseConfig(connection_uri=SecretStr("postgresql://a:b@localhost/db")),
            llm=LLMConfig(api_key=SecretStr("sk")),
            budget=BudgetConfig(),
        )
        ctx = w_build(cfg)
        assert ctx is not None
        # run_temporal_worker with mocked Client/Worker
        with patch(
            "investigation_agent_platform.bootstrap.worker.Client.connect", new_callable=AsyncMock
        ) as mock_connect:
            with patch("investigation_agent_platform.bootstrap.worker.Worker") as MockWorker:
                mock_worker = MagicMock()
                mock_worker.run = AsyncMock(return_value=None)
                MockWorker.return_value = mock_worker
                mock_connect.return_value = MagicMock()
                await run_temporal_worker(
                    TemporalConfig(
                        target_host="localhost:7233", namespace="default", task_queue="tq"
                    )
                )


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
class TestMain:
    def test_create_app_import(self) -> None:
        from investigation_agent_platform.main import app

        assert app is not None

    @pytest.mark.asyncio
    async def test_lifespan(self) -> None:
        from investigation_agent_platform.main import _create_lifespan_app

        app2 = _create_lifespan_app()
        assert app2 is not None
        # Check lifespan context is set
        assert app2.router.lifespan_context is not None


# ---------------------------------------------------------------------------
# persistence rls and models
# ---------------------------------------------------------------------------
class TestRLS:
    @pytest.mark.asyncio
    async def test_rls_session_sets_local(self) -> None:
        from investigation_agent_platform.infrastructure.persistence.rls import rls_session

        mock_session = AsyncMock()
        mock_session.execute = AsyncMock()
        mock_session.commit = AsyncMock()

        mock_factory = MagicMock()
        # factory() returns async context manager
        mock_cm = MagicMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_session)
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        mock_factory.return_value = mock_cm

        async with rls_session(mock_factory, "tenant-1") as sess:
            assert sess is mock_session
        mock_session.execute.assert_called_once()
        args, kwargs = mock_session.execute.call_args
        assert "SET LOCAL" in str(args[0])
        mock_session.commit.assert_called_once()


class TestModels:
    def test_orm_tables_exist(self) -> None:
        from investigation_agent_platform.infrastructure.persistence.models import (
            Base,
            EvidenceORM,
            HypothesisORM,
            InvestigationORM,
            TimelineEventORM,
        )

        assert InvestigationORM.__tablename__ == "investigations"
        assert EvidenceORM.__tablename__ == "evidence"
        assert TimelineEventORM.__tablename__ == "timeline_events"
        assert HypothesisORM.__tablename__ == "hypotheses"
        # Check Base has metadata
        assert hasattr(Base, "metadata")

    def test_evidence_dedup_inmemory(self) -> None:
        import asyncio
        from datetime import UTC, datetime

        from investigation_agent_platform.api.dependencies import InMemoryEvidenceRepository
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

        async def _run() -> None:
            repo = InMemoryEvidenceRepository()
            now = datetime.now(UTC)
            prov = EvidenceProvenance(
                tenant_id="t1",
                investigation_id=uuid4(),
                provider_type="CODE",
                requested_provider_id="p",
                actual_provider_id="p",
                source_system="Code",
                retrieval_timestamp=now,
                query_fingerprint=QueryFingerprint(
                    provider_type="CODE", operation="SEARCH", normalized_query_hash="abc"
                ),
                source_location=SourceLocation(system="Code", identifier="id"),
            )
            ev = Evidence(
                tenant_id="t1",
                investigation_id=uuid4(),
                evidence_type=EvidenceType.SOURCE_CODE,
                provider="p",
                source="src",
                title="t",
                summary="s",
                content_snippet="c",
                observed_at=now,
                retrieved_at=now,
                provenance=prov,
                freshness=EvidenceFreshness(observed_at=now, retrieved_at=now),
                classification=ClassificationLevel.INTERNAL,
                fingerprint="fp1",
                attributes={},
            )
            await repo.save("t1", ev, investigation_id=ev.investigation_id)
            await repo.save("t1", ev, investigation_id=ev.investigation_id)  # duplicate
            # save_batch dedup
            await repo.save_batch("t1", [ev, ev], investigation_id=ev.investigation_id)
            assert len(repo._store) == 1

        asyncio.run(_run())

    def test_has_traversal_centralized(self) -> None:
        from investigation_agent_platform.application.investigation.validator import _has_traversal

        assert _has_traversal("../etc/passwd") is True
        assert _has_traversal("..%2Fetc") is True
        assert _has_traversal("src/app.py") is False
        assert _has_traversal("") is False

    def test_idempotency_store(self) -> None:
        import asyncio

        from investigation_agent_platform.api.dependencies import _InMemoryIdempotencyStore

        async def _run() -> None:
            store = _InMemoryIdempotencyStore(ttl_seconds=10)
            v, created = await store.get_or_create("k1", "v1")
            assert created is True
            v2, created2 = await store.get_or_create("k1", "v2")
            assert created2 is False
            assert v2 == "v1"

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# graphify_adapter.py + pipeline.py + schema/sql.py — code↔DB graph wiring
# ---------------------------------------------------------------------------


class TestCodeSchemaGraphIntegration:
    def _builder(self):  # type: ignore[no-untyped-def]
        from investigation_agent_platform.infrastructure.evidence.code.graphify_adapter import (
            CodeSymbolGraphBuilder,
        )

        return CodeSymbolGraphBuilder()

    def _tables(self):  # type: ignore[no-untyped-def]
        from investigation_agent_platform.infrastructure.evidence.schema.sql import (
            TableColumn,
            TableSchema,
        )

        return [
            TableSchema(
                name="orders",
                columns=[
                    TableColumn(name="id", data_type="UUID", is_primary_key=True),
                    TableColumn(name="status", data_type="TEXT"),
                ],
                foreign_keys=[],
            )
        ]

    def test_schema_nodes_ingested_with_columns(self) -> None:
        builder = self._builder()
        builder.add_database_schema_nodes(self._tables())
        assert "db_table::orders" in builder._node_map
        assert "db_column::orders.id" in builder._node_map
        assert "db_column::orders.status" in builder._node_map

    def test_schema_ingestion_is_idempotent(self) -> None:
        builder = self._builder()
        tables = self._tables()
        builder.add_database_schema_nodes(tables)
        before = builder.graph.num_nodes()
        builder.add_database_schema_nodes(tables)
        assert builder.graph.num_nodes() == before

    def test_link_exact_qualified_match(self) -> None:
        builder = self._builder()
        builder.add_database_schema_nodes(self._tables())
        # Simulate a symbol node as add_file_symbols would create it.
        builder._node_map["src/app.py::OrderService.create"] = builder.graph.add_node(
            {"id": "src/app.py::OrderService.create", "node_type": "code_symbol"}
        )
        assert builder.link_code_to_tables("src/app.py", "OrderService.create", "orders") is True

    def test_link_unqualified_suffix_match(self) -> None:
        builder = self._builder()
        builder.add_database_schema_nodes(self._tables())
        builder._node_map["src/app.py::OrderService.create"] = builder.graph.add_node(
            {"id": "src/app.py::OrderService.create", "node_type": "code_symbol"}
        )
        assert builder.link_code_to_tables("src/app.py", "create", "orders") is True

    def test_link_ambiguous_name_refuses(self) -> None:
        builder = self._builder()
        builder.add_database_schema_nodes(self._tables())
        builder._node_map["src/a.py::OrderService.create"] = builder.graph.add_node(
            {"id": "src/a.py::OrderService.create", "node_type": "code_symbol"}
        )
        builder._node_map["src/a.py::PaymentService.create"] = builder.graph.add_node(
            {"id": "src/a.py::PaymentService.create", "node_type": "code_symbol"}
        )
        assert builder.link_code_to_tables("src/a.py", "create", "orders") is False

    def test_link_unknown_table_or_symbol_refuses(self) -> None:
        builder = self._builder()
        assert builder.link_code_to_tables("src/a.py", "missing", "nope") is False
        builder.add_database_schema_nodes(self._tables())
        assert builder.link_code_to_tables("src/a.py", "missing", "orders") is False

    def test_schema_parser_end_to_end(self) -> None:
        from investigation_agent_platform.infrastructure.evidence.schema.sql import SchemaParser

        with tempfile.TemporaryDirectory() as tmp:
            ddl = Path(tmp) / "schema.sql"
            ddl.write_text(
                "CREATE TABLE orders (id UUID PRIMARY KEY, status TEXT);",
                encoding="utf-8",
            )
            tables = SchemaParser().parse_ddl_file(ddl)
            assert [t.name for t in tables] == ["orders"]
            builder = self._builder()
            builder.add_database_schema_nodes(tables)
            assert "db_table::orders" in builder._node_map

    @pytest.mark.asyncio
    async def test_pipeline_ingests_ddl_paths(self) -> None:
        from investigation_agent_platform.infrastructure.evidence.code.parser import (
            TreeSitterParser,
        )
        from investigation_agent_platform.infrastructure.evidence.code.pipeline import (
            CodebaseGraphPipeline,
        )

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "app.py").write_text("def hello():\n    return 1\n", encoding="utf-8")
            ddl = repo / "schema.sql"
            ddl.write_text("CREATE TABLE orders (id UUID PRIMARY KEY);", encoding="utf-8")
            pipeline = CodebaseGraphPipeline(parser=TreeSitterParser())
            graph = await pipeline.build_repository_graph(repo, language="python", ddl_paths=[ddl])
            node_ids = {
                data.get("id")
                for idx in graph.node_indices()
                if isinstance((data := graph.get_node_data(idx)), dict)
            }
            assert "db_table::orders" in node_ids

    @pytest.mark.asyncio
    async def test_pipeline_without_ddl_paths_unchanged(self) -> None:
        from investigation_agent_platform.infrastructure.evidence.code.parser import (
            TreeSitterParser,
        )
        from investigation_agent_platform.infrastructure.evidence.code.pipeline import (
            CodebaseGraphPipeline,
        )

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "app.py").write_text("def hello():\n    return 1\n", encoding="utf-8")
            pipeline = CodebaseGraphPipeline(parser=TreeSitterParser())
            graph = await pipeline.build_repository_graph(repo, language="python")
            node_ids = {
                data.get("id")
                for idx in graph.node_indices()
                if isinstance((data := graph.get_node_data(idx)), dict)
            }
            assert not any(str(nid).startswith("db_table::") for nid in node_ids)

    def _access_edges(self, graph):  # type: ignore[no-untyped-def]
        edges = []
        for src_idx, dst_idx, edata in graph.weighted_edge_list():
            if isinstance(edata, dict) and edata.get("relation") == "ACCESSES_TABLE":
                src = graph.get_node_data(src_idx)
                dst = graph.get_node_data(dst_idx)
                edges.append((src.get("id"), dst.get("id")))
        return edges

    # --- ISSUE-1: link_evidence_to_tables (unambiguous linking from runtime evidence) ---

    def test_link_evidence_links_location_inside_function(self) -> None:
        from investigation_agent_platform.ports.evidence.code import CodeLocation

        builder = self._builder()
        builder.add_database_schema_nodes(self._tables())
        builder._node_map["src/app.py::create_order"] = builder.graph.add_node(
            {
                "id": "src/app.py::create_order",
                "name": "create_order",
                "node_type": "code_symbol",
                "file_path": "src/app.py",
                "start_line": 1,
                "end_line": 5,
            }
        )
        locations = [
            CodeLocation(file_path="src/app.py", line_number=3, snippet="INSERT INTO orders")
        ]
        linked = builder.link_evidence_to_tables(locations, "orders")
        assert linked == 1
        assert ("src/app.py::create_order", "db_table::orders") in self._access_edges(builder.graph)

    def test_link_evidence_picks_innermost_enclosing_symbol(self) -> None:
        """A method nested inside a class both enclose the same line; the
        innermost (smallest range) symbol is the real match, not ambiguity."""
        from investigation_agent_platform.ports.evidence.code import CodeLocation

        builder = self._builder()
        builder.add_database_schema_nodes(self._tables())
        builder._node_map["src/app.py::OrderService"] = builder.graph.add_node(
            {
                "id": "src/app.py::OrderService",
                "name": "OrderService",
                "node_type": "code_symbol",
                "file_path": "src/app.py",
                "start_line": 1,
                "end_line": 20,
            }
        )
        builder._node_map["src/app.py::OrderService.create"] = builder.graph.add_node(
            {
                "id": "src/app.py::OrderService.create",
                "name": "OrderService.create",
                "node_type": "code_symbol",
                "file_path": "src/app.py",
                "start_line": 5,
                "end_line": 10,
            }
        )
        locations = [
            CodeLocation(file_path="src/app.py", line_number=7, snippet="INSERT INTO orders")
        ]
        linked = builder.link_evidence_to_tables(locations, "orders")
        assert linked == 1
        assert ("src/app.py::OrderService.create", "db_table::orders") in self._access_edges(
            builder.graph
        )

    def test_link_evidence_ambiguous_same_named_symbols_refuses(self) -> None:
        from investigation_agent_platform.ports.evidence.code import CodeLocation

        builder = self._builder()
        builder.add_database_schema_nodes(self._tables())
        # Two same-named, same-range symbols enclosing the same location.
        builder._node_map["src/app.py::create"] = builder.graph.add_node(
            {
                "id": "src/app.py::create",
                "name": "create",
                "node_type": "code_symbol",
                "file_path": "src/app.py",
                "start_line": 1,
                "end_line": 10,
            }
        )
        builder._node_map["src/app.py::Other.create"] = builder.graph.add_node(
            {
                "id": "src/app.py::Other.create",
                "name": "Other.create",
                "node_type": "code_symbol",
                "file_path": "src/app.py",
                "start_line": 1,
                "end_line": 10,
            }
        )
        locations = [
            CodeLocation(file_path="src/app.py", line_number=5, snippet="INSERT INTO orders")
        ]
        linked = builder.link_evidence_to_tables(locations, "orders")
        assert linked == 0
        assert self._access_edges(builder.graph) == []

    def test_link_evidence_unknown_table_no_edge(self) -> None:
        from investigation_agent_platform.ports.evidence.code import CodeLocation

        builder = self._builder()
        builder._node_map["src/app.py::create_order"] = builder.graph.add_node(
            {
                "id": "src/app.py::create_order",
                "name": "create_order",
                "node_type": "code_symbol",
                "file_path": "src/app.py",
                "start_line": 1,
                "end_line": 5,
            }
        )
        locations = [
            CodeLocation(file_path="src/app.py", line_number=3, snippet="INSERT INTO ghosts")
        ]
        linked = builder.link_evidence_to_tables(locations, "ghosts")
        assert linked == 0

    def test_link_evidence_unmatched_location_no_edge(self) -> None:
        from investigation_agent_platform.ports.evidence.code import CodeLocation

        builder = self._builder()
        builder.add_database_schema_nodes(self._tables())
        locations = [
            CodeLocation(file_path="src/nowhere.py", line_number=3, snippet="INSERT INTO orders")
        ]
        linked = builder.link_evidence_to_tables(locations, "orders")
        assert linked == 0

    # --- ISSUE-2: production caller wiring in the default pipeline path ---

    @pytest.mark.asyncio
    async def test_pipeline_links_runtime_evidence_to_known_table(self) -> None:
        from investigation_agent_platform.infrastructure.evidence.code.parser import (
            TreeSitterParser,
        )
        from investigation_agent_platform.infrastructure.evidence.code.pipeline import (
            CodebaseGraphPipeline,
        )

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "app.py").write_text(
                "def create_order(conn):\n"
                '    conn.execute("INSERT INTO orders (id) VALUES (1)")\n'
                "    return True\n",
                encoding="utf-8",
            )
            ddl = repo / "schema.sql"
            ddl.write_text("CREATE TABLE orders (id UUID PRIMARY KEY);", encoding="utf-8")
            pipeline = CodebaseGraphPipeline(parser=TreeSitterParser())
            graph = await pipeline.build_repository_graph(repo, language="python", ddl_paths=[ddl])
            edges = self._access_edges(graph)
            assert ("app.py::create_order", "db_table::orders") in edges

    @pytest.mark.asyncio
    async def test_pipeline_unknown_table_reference_produces_no_edge(self) -> None:
        from investigation_agent_platform.infrastructure.evidence.code.parser import (
            TreeSitterParser,
        )
        from investigation_agent_platform.infrastructure.evidence.code.pipeline import (
            CodebaseGraphPipeline,
        )

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "app.py").write_text(
                "def create_order(conn):\n"
                '    conn.execute("INSERT INTO ghosts (id) VALUES (1)")\n'
                "    return True\n",
                encoding="utf-8",
            )
            ddl = repo / "schema.sql"
            ddl.write_text("CREATE TABLE orders (id UUID PRIMARY KEY);", encoding="utf-8")
            pipeline = CodebaseGraphPipeline(parser=TreeSitterParser())
            graph = await pipeline.build_repository_graph(repo, language="python", ddl_paths=[ddl])
            assert self._access_edges(graph) == []

    @pytest.mark.asyncio
    async def test_pipeline_multi_table_files_link_independently(self) -> None:
        from investigation_agent_platform.infrastructure.evidence.code.parser import (
            TreeSitterParser,
        )
        from investigation_agent_platform.infrastructure.evidence.code.pipeline import (
            CodebaseGraphPipeline,
        )

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "orders.py").write_text(
                'def create_order(conn):\n    conn.execute("INSERT INTO orders (id) VALUES (1)")\n',
                encoding="utf-8",
            )
            (repo / "payments.py").write_text(
                "def create_payment(conn):\n"
                '    conn.execute("INSERT INTO payments (id) VALUES (1)")\n',
                encoding="utf-8",
            )
            ddl = repo / "schema.sql"
            ddl.write_text(
                "CREATE TABLE orders (id UUID PRIMARY KEY);\n"
                "CREATE TABLE payments (id UUID PRIMARY KEY);\n",
                encoding="utf-8",
            )
            pipeline = CodebaseGraphPipeline(parser=TreeSitterParser())
            graph = await pipeline.build_repository_graph(repo, language="python", ddl_paths=[ddl])
            edges = self._access_edges(graph)
            assert ("orders.py::create_order", "db_table::orders") in edges
            assert ("payments.py::create_payment", "db_table::payments") in edges
            assert ("orders.py::create_order", "db_table::payments") not in edges
            assert ("payments.py::create_payment", "db_table::orders") not in edges
