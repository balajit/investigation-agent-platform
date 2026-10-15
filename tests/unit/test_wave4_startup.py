# tests/unit/test_wave4_startup.py
"""Wave 4 tests: single-source environment, unknown-var scan, file logging."""

from __future__ import annotations

import json
import logging

from investigation_agent_platform.infrastructure.configuration.layers import (
    effective_environment,
    find_unknown_vars,
)


class TestEffectiveEnvironment:
    def test_config_wins_over_env(self, monkeypatch) -> None:
        from types import SimpleNamespace

        monkeypatch.setenv("IAP_ENVIRONMENT", "development")
        assert effective_environment(SimpleNamespace(environment="production")) == "production"

    def test_no_config_no_env_is_development(self, monkeypatch) -> None:
        monkeypatch.delenv("IAP_ENVIRONMENT", raising=False)
        assert effective_environment(None) == "development"

    def test_no_config_env_production(self, monkeypatch) -> None:
        monkeypatch.setenv("IAP_ENVIRONMENT", "production")
        assert effective_environment(None) == "production"

    def test_unset_loader_default_still_production(self, monkeypatch) -> None:
        # Mirrors load_application_config_from_env: unset means production.
        import os

        monkeypatch.delenv("IAP_ENVIRONMENT", raising=False)
        assert os.environ.get("IAP_ENVIRONMENT", "production") == "production"


class TestUnknownVars:
    def test_typo_reported(self, monkeypatch) -> None:
        monkeypatch.setenv("IAP_TYPO_EXAMPLE", "1")
        assert "IAP_TYPO_EXAMPLE" in find_unknown_vars()

    def test_known_and_dynamic_allowed(self, monkeypatch) -> None:
        monkeypatch.setenv("IAP_TEMPORAL_HOST", "x")
        monkeypatch.setenv("IAP_DS_TENANT_A_PRIMARY", "x")
        monkeypatch.setenv("IAP_SECRET_DB_PROD", "x")
        found = find_unknown_vars()
        assert "IAP_TEMPORAL_HOST" not in found
        assert "IAP_DS_TENANT_A_PRIMARY" not in found
        assert "IAP_SECRET_DB_PROD" not in found


class TestFileLogging:
    def test_json_file_handler_created(self, tmp_path, monkeypatch) -> None:
        from investigation_agent_platform.infrastructure.telemetry import logging as logpipe

        yaml_path = tmp_path / "logging.yaml"
        yaml_path.write_text(
            "handlers:\n  console:\n    enabled: false\n  file:\n    enabled: true\n"
            f'    path: "{tmp_path}/iap.log"\n    max_bytes: 1048576\n    backup_count: 2\n'
            "root:\n  level: INFO\n",
            encoding="utf-8",
        )
        monkeypatch.delenv("IAP_LOG_FILE", raising=False)
        monkeypatch.delenv("IAP_LOG_LEVEL", raising=False)
        target = logpipe.configure_logging(yaml_path=yaml_path)
        assert target is not None and target.is_file()

    def test_exception_record_is_json_with_exc(self, tmp_path) -> None:
        from investigation_agent_platform.infrastructure.telemetry import logging as logpipe

        target = tmp_path / "exc.log"
        logpipe.configure_logging(yaml_path=tmp_path / "missing.yaml", log_file=str(target))
        probe = logging.getLogger("iap.wave4.probe")
        try:
            raise ValueError("boom")
        except ValueError:
            probe.exception("probe failed", extra={"event": "wave4_probe"})
        lines = target.read_text(encoding="utf-8").strip().splitlines()
        assert lines, "expected log lines in file"
        # Note: ProcessorFormatter renders the stdlib message as "event", so
        # the machine key travels in the message slot for foreign records.
        record = json.loads(lines[-1])
        assert "probe failed" in str(record.get("event", record))
        assert "ValueError" in str(record.get("exception", record))
        assert record.get("logger") == "iap.wave4.probe"
