# tests/unit/test_file_log_provider.py
"""File-log evidence provider tests (generic, profile-driven)."""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from pydantic import SecretStr

from investigation_agent_platform.domain.evidence.requests import RuntimeEvidenceRequest, TimeRange
from investigation_agent_platform.domain.profile.models import (
    ApplicationProfile,
    Datasource,
    DatasourceKind,
    FileLogDatasourceSpec,
)
from investigation_agent_platform.infrastructure.evidence.logs.file_provider import (
    FileLogAdapter,
    FileLogSettings,
    _parse_line,
)


def _profile(paths: list[str]) -> ApplicationProfile:
    return ApplicationProfile.model_validate(
        {
            "id": "app-under-test",
            "tenant_id": "tenant-a",
            "name": "App Under Test",
            "description": "test",
            "environment": "development",
            "datasources": [
                {
                    "id": "applogs",
                    "name": "app logs",
                    "kind": "file_log",
                    "role": "readonly",
                    "file_log": {"paths": paths},
                }
            ],
        }
    )


def _adapter(tmp_path, **overrides):  # type: ignore[no-untyped-def]
    settings = FileLogSettings(
        roots=["."],
        cursor_signing_key=SecretStr("test-signing-key"),
        **overrides,
    )
    return FileLogAdapter(settings=settings, project_root=tmp_path)


def _request(**overrides):  # type: ignore[no-untyped-def]
    base: dict = {"environment": "development"}
    base.update(overrides)
    return RuntimeEvidenceRequest(**base)


class TestParseLine:
    def test_meta_telemetry_shape(self) -> None:
        line = json.dumps(
            {
                "timestamp": "2026-10-15T18:00:00Z",
                "level": "ERROR",
                "logger": "myapp.handler",
                "message": "boom happened",
                "service": {"name": "myapp", "environment": "dev"},
                "code": {"file": "h.py", "line": 42, "function": "run"},
                "trace_id": "abc123",
                "exception": {"details": "Traceback... ValueError: boom"},
                "extra": {"tenant_id": "t1"},
            }
        )
        parsed = _parse_line(line)
        assert parsed.level == "ERROR"
        assert parsed.service == "myapp"
        assert parsed.function == "run"
        assert parsed.trace_id == "abc123"
        assert "ValueError" in (parsed.exception_text or "")
        assert parsed.log_tenant_hint == "t1"

    def test_structlog_shape(self) -> None:
        line = json.dumps(
            {
                "event": "request failed",
                "level": "warning",
                "logger": "api.router",
                "timestamp": "2026-10-15T18:00:00+00:00",
            }
        )
        parsed = _parse_line(line)
        assert parsed.message == "request failed"
        assert parsed.level == "WARNING"

    def test_plain_text_fallback(self) -> None:
        parsed = _parse_line("plain old log line")
        assert parsed.message == "plain old log line"
        assert parsed.level == "INFO"


class TestSearch:
    def _write(self, tmp_path, name: str, lines: list[str]):  # type: ignore[no-untyped-def]
        target = tmp_path / name
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return target

    @pytest.mark.asyncio
    async def test_keyword_filter_and_evidence_shape(self, tmp_path) -> None:
        self._write(
            tmp_path,
            "app.log",
            [
                json.dumps({"level": "INFO", "logger": "a", "message": "startup ok"}),
                json.dumps({"level": "ERROR", "logger": "a", "message": "checkout failed"}),
                "not json at all",
            ],
        )
        adapter = _adapter(tmp_path)
        result = await adapter.search_runtime_evidence(
            "tenant-a", uuid4(), _request(keywords=["checkout"]), _profile(["*.log"])
        )
        assert result.total_count == 1
        item = result.items[0]
        assert "checkout failed" in item.summary
        assert item.attributes["file"] == "app.log"
        assert item.fingerprint

    @pytest.mark.asyncio
    async def test_no_declared_sources_serves_nothing(self, tmp_path) -> None:
        self._write(tmp_path, "app.log", ['{"message": "hi"}'])
        adapter = _adapter(tmp_path)
        profile = ApplicationProfile.model_validate(
            {"id": "x", "tenant_id": "t", "name": "X", "description": "t", "environment": "development", "datasources": []}
        )
        result = await adapter.search_runtime_evidence(
            "t", uuid4(), _request(), profile
        )
        assert result.total_count == 0

    @pytest.mark.asyncio
    async def test_escape_rejected(self, tmp_path) -> None:
        outside = tmp_path.parent / "secret.log"
        outside.write_text('{"message": "top secret"}\n', encoding="utf-8")
        try:
            adapter = _adapter(tmp_path)
            result = await adapter.search_runtime_evidence(
                "t", uuid4(), _request(), _profile(["../secret.log"])
            )
            assert result.total_count == 0
        finally:
            outside.unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_severity_filter(self, tmp_path) -> None:
        self._write(
            tmp_path,
            "app.log",
            [
                json.dumps({"level": "INFO", "message": "a"}),
                json.dumps({"level": "ERROR", "message": "b"}),
            ],
        )
        adapter = _adapter(tmp_path)
        result = await adapter.search_runtime_evidence(
            "t", uuid4(), _request(severities=["ERROR"]), _profile(["*.log"])
        )
        assert result.total_count == 1

    @pytest.mark.asyncio
    async def test_cursor_pagination(self, tmp_path) -> None:
        self._write(
            tmp_path, "app.log", [json.dumps({"message": f"m{i}"}) for i in range(5)]
        )
        adapter = _adapter(tmp_path)
        first = await adapter.search_runtime_evidence(
            "t", uuid4(), _request(limit=2), _profile(["*.log"])
        )
        assert first.total_count == 5
        assert first.has_more and first.cursor
        second = await adapter.search_runtime_evidence(
            "t", uuid4(), _request(limit=2, cursor=first.cursor), _profile(["*.log"])
        )
        assert len(second.items) == 2
        assert {i.summary for i in first.items}.isdisjoint({i.summary for i in second.items})

    @pytest.mark.asyncio
    async def test_multi_keyword_or_semantics_with_ranking(self, tmp_path) -> None:
        # Regression: multi-facet incident queries must not require EVERY
        # keyword in a single line (that guaranteed empty results against
        # one-line-per-request access logs). OR match, most hits first.
        self._write(
            tmp_path,
            "app.log",
            [
                json.dumps({"message": "alpha only"}),
                json.dumps({"message": "alpha and beta together"}),
                json.dumps({"message": "nothing relevant"}),
            ],
        )
        adapter = _adapter(tmp_path)
        result = await adapter.search_runtime_evidence(
            "t", uuid4(), _request(keywords=["alpha", "beta"]), _profile(["*.log"])
        )
        assert result.total_count == 2
        assert "together" in result.items[0].summary
        assert "alpha only" in result.items[1].summary

    @pytest.mark.asyncio
    async def test_incident_query_matches_access_log_line(self, tmp_path) -> None:
        # The self-investigation query ["profile", "iap", "tenant"] matched
        # zero of 7,911 lines; a single discriminating term matched 178.
        self._write(
            tmp_path,
            "iap.log",
            [
                json.dumps(
                    {
                        "timestamp": "2026-10-15T19:50:00Z",
                        "level": "INFO",
                        "logger": "api.app",
                        "message": "GET /api/v1/profiles/investigation-agent-platform HTTP/1.1 404",
                    }
                ),
            ],
        )
        adapter = _adapter(tmp_path)
        result = await adapter.search_runtime_evidence(
            "n-a",
            uuid4(),
            _request(keywords=["profile", "iap", "tenant"]),
            _profile(["iap.log"]),
        )
        assert result.total_count == 1
        assert "404" in result.items[0].summary

    @pytest.mark.asyncio
    async def test_time_range_filters(self, tmp_path) -> None:
        from datetime import datetime

        self._write(
            tmp_path,
            "app.log",
            [
                json.dumps({"timestamp": "2026-10-15T18:00:00Z", "message": "old"}),
                json.dumps({"timestamp": "2026-10-15T19:50:00Z", "message": "new"}),
            ],
        )
        adapter = _adapter(tmp_path)
        result = await adapter.search_runtime_evidence(
            "t",
            uuid4(),
            _request(
                time_range=TimeRange(
                    start_time=datetime.fromisoformat("2026-10-15T19:00:00+00:00"),
                    end_time=datetime.fromisoformat("2026-10-15T20:00:00+00:00"),
                )
            ),
            _profile(["*.log"]),
        )
        assert result.total_count == 1
        assert "new" in result.items[0].summary

    @pytest.mark.asyncio
    async def test_get_by_id_roundtrip(self, tmp_path) -> None:
        self._write(tmp_path, "app.log", [json.dumps({"message": "needle"})])
        adapter = _adapter(tmp_path)
        profile = _profile(["*.log"])
        result = await adapter.search_runtime_evidence(
            "t", uuid4(), _request(keywords=["needle"]), profile
        )
        adapter.profile = profile
        fetched = await adapter.get_runtime_evidence(
            "t", uuid4(), result.items[0].evidence_id, "development", None  # type: ignore[arg-type]
        )
        assert fetched.evidence_id == result.items[0].evidence_id


class TestDomain:
    def test_file_log_kind_parses(self) -> None:
        ds = Datasource.model_validate(
            {
                "id": "logs",
                "name": "logs",
                "kind": "file_log",
                "role": "readonly",
                "file_log": {"paths": ["*.log"]},
            }
        )
        assert ds.kind == DatasourceKind.FILE_LOG
        assert isinstance(ds.file_log, FileLogDatasourceSpec)
