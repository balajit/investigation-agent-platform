# tests/unit/test_part10_phase2.py
"""Part 10 Phase 2: mapping registry, shipped YAMLs, config wiring."""

from pathlib import Path

import pytest
import yaml

from investigation_agent_platform.domain.common.exceptions import PlatformConfigurationError
from investigation_agent_platform.domain.observability.mapping import (
    SeverityStrategy,
    TenantScopeStrategy,
    generic_ecs_mapping,
)
from investigation_agent_platform.infrastructure.configuration.mapping_registry import (
    MappingProfileRegistry,
    load_mapping_profiles,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SHIPPED_DIR = REPO_ROOT / "config" / "observability-mappings"


def _write_yaml(directory: Path, name: str, document: dict) -> Path:
    path = directory / name
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    return path


def _minimal_document(source_id="test-source", patterns=None, **overrides) -> dict:
    document = {
        "source_id": source_id,
        "index_patterns": patterns if patterns is not None else [f"logz-{source_id}-es-*"],
        "tenant_scope": {"strategy": "none_required"},
        "timestamp": {"candidates": ["@timestamp"], "value_type": "date_iso"},
        "severity": {"strategy": "unsupported"},
        "top_level_allowlist": ["@timestamp", "message"],
    }
    document.update(overrides)
    return document


def test_shipped_gen_mappings_load_and_validate():
    registry = load_mapping_profiles(SHIPPED_DIR)
    assert {"gen-verbose", "gen-root-cause"} <= registry.source_ids
    verbose = registry.resolve("gen-verbose")
    assert verbose.timestamp.candidates == ["@timestamp"]
    assert verbose.service is not None
    assert verbose.service.candidates == ["appName", "Service", "System", "originServiceName"]
    assert verbose.severity.strategy == SeverityStrategy.ERROR_FLAG
    assert verbose.severity.error_field == "error"
    assert set(verbose.identifiers) >= {"trace_id", "session_id", "transaction_key"}
    assert verbose.code_location is None and verbose.sql_statement is None
    assert verbose.tenant_scope.strategy == TenantScopeStrategy.NONE_REQUIRED
    assert verbose.sort.field == "@timestamp"
    assert "RequestBody" not in verbose.top_level_allowlist
    assert "ResponseBody" not in verbose.top_level_allowlist

    root_cause = registry.resolve("gen-root-cause")
    assert root_cause.timestamp.value_type.value == "date_epoch_millis"
    assert root_cause.timestamp.candidates == ["updatedAt", "createdAt"]
    assert root_cause.severity.strategy == SeverityStrategy.UNSUPPORTED
    assert root_cause.service is None
    assert "@timestamp" not in root_cause.top_level_allowlist


def test_resolve_none_is_generic_ecs_and_unknown_fails_closed(tmp_path):
    registry = load_mapping_profiles(tmp_path)
    assert len(registry) == 0
    resolved = registry.resolve(None)
    assert resolved.source_id == "generic-ecs"
    assert resolved is generic_ecs_mapping() or resolved == generic_ecs_mapping()
    with pytest.raises(PlatformConfigurationError):
        registry.resolve("no-such-source")


def test_missing_directory_yields_empty_registry(tmp_path):
    registry = load_mapping_profiles(tmp_path / "does-not-exist")
    assert len(registry) == 0
    assert registry.resolve(None).source_id == "generic-ecs"


def test_malformed_yaml_and_schema_violations_reject(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("source_id: [unclosed\n", encoding="utf-8")
    with pytest.raises(PlatformConfigurationError):
        load_mapping_profiles(tmp_path)

    bad.unlink()
    document = _minimal_document()
    del document["severity"]
    _write_yaml(tmp_path, "incomplete.yaml", document)
    with pytest.raises(PlatformConfigurationError):
        load_mapping_profiles(tmp_path)


def test_typo_keys_reject_via_forbid(tmp_path):
    document = _minimal_document()
    document["timestamps"] = document.pop("timestamp")
    _write_yaml(tmp_path, "typo.yaml", document)
    with pytest.raises(PlatformConfigurationError):
        load_mapping_profiles(tmp_path)


def test_duplicate_source_id_rejects(tmp_path):
    _write_yaml(tmp_path, "a.yaml", _minimal_document(source_id="dup"))
    _write_yaml(tmp_path, "b.yaml", _minimal_document(source_id="dup"))
    with pytest.raises(PlatformConfigurationError):
        load_mapping_profiles(tmp_path)


def test_overlapping_index_pattern_rejects(tmp_path):
    _write_yaml(tmp_path, "a.yaml", _minimal_document(source_id="a", patterns=["logz-x-*"]))
    _write_yaml(tmp_path, "b.yaml", _minimal_document(source_id="b", patterns=["logz-x-*"]))
    with pytest.raises(PlatformConfigurationError):
        load_mapping_profiles(tmp_path)


def test_empty_patterns_and_legacy_flag_reject(tmp_path):
    _write_yaml(tmp_path, "empty.yaml", _minimal_document(patterns=[]))
    with pytest.raises(PlatformConfigurationError):
        load_mapping_profiles(tmp_path)
    for child in tmp_path.iterdir():
        child.unlink()
    document = _minimal_document()
    document["legacy_index_synthesis"] = True
    _write_yaml(tmp_path, "legacy.yaml", document)
    with pytest.raises(PlatformConfigurationError):
        load_mapping_profiles(tmp_path)
    for child in tmp_path.iterdir():
        child.unlink()
    document = _minimal_document(source_id="generic-ecs")
    _write_yaml(tmp_path, "shadow.yaml", document)
    with pytest.raises(PlatformConfigurationError):
        load_mapping_profiles(tmp_path)


def test_builtin_id_resolves_without_registry():
    from investigation_agent_platform.infrastructure.configuration.mapping_registry import (
        MappingProfileRegistry,
    )

    empty = MappingProfileRegistry({})
    assert empty.resolve("generic-ecs").source_id == "generic-ecs"
    assert empty.resolve(None).source_id == "generic-ecs"


def test_evidence_config_mapping_path_default_and_env(monkeypatch):
    from investigation_agent_platform.infrastructure.configuration.config import (
        _evidence_config_from_env,
    )

    monkeypatch.delenv("IAP_MAPPING_PROFILES_PATH", raising=False)
    assert _evidence_config_from_env().mapping_profiles_path == "config/observability-mappings"
    monkeypatch.setenv("IAP_MAPPING_PROFILES_PATH", "custom/dir")
    assert _evidence_config_from_env().mapping_profiles_path == "custom/dir"


def test_registry_is_plain_container():
    empty = MappingProfileRegistry({})
    assert len(empty) == 0 and empty.source_ids == frozenset()
