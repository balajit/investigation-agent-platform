# tests/unit/test_part9_phase1.py
"""Part 9 Phase 1: config fail-closed, request contracts, exceptions, completeness, migration."""

import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from investigation_agent_platform.domain.common.exceptions import (
    InvalidCursorException,
    PlatformConfigurationError,
    ProviderTimeoutException,
    ProviderUnavailableException,
)
from investigation_agent_platform.domain.evidence.completeness import (
    CompletenessReason,
    EvidenceCompleteness,
)
from investigation_agent_platform.domain.evidence.models import Evidence, EvidenceType, LogSeverity
from investigation_agent_platform.domain.evidence.requests import (
    RuntimeEvidenceRequest,
)
from investigation_agent_platform.domain.provenance.models import (
    EvidenceFreshness,
    EvidenceProvenance,
    QueryFingerprint,
    SourceLocation,
)


def _base_kwargs(**overrides):
    kwargs = {
        "environment": "production",
        "limit": 100,
    }
    kwargs.update(overrides)
    return kwargs


# --- RuntimeEvidenceRequest contracts ----------------------------------------


def test_valid_minimal_request():
    req = RuntimeEvidenceRequest(**_base_kwargs())
    assert req.limit == 100
    assert req.severities == []


def test_limit_bounds():
    RuntimeEvidenceRequest(**_base_kwargs(limit=1))
    RuntimeEvidenceRequest(**_base_kwargs(limit=200))
    with pytest.raises(ValidationError):
        RuntimeEvidenceRequest(**_base_kwargs(limit=0))
    with pytest.raises(ValidationError):
        RuntimeEvidenceRequest(**_base_kwargs(limit=201))


def test_keyword_bounds():
    RuntimeEvidenceRequest(**_base_kwargs(keywords=["a"] * 20))
    with pytest.raises(ValidationError):
        RuntimeEvidenceRequest(**_base_kwargs(keywords=["a"] * 21))
    with pytest.raises(ValidationError):
        RuntimeEvidenceRequest(**_base_kwargs(keywords=["x" * 201]))
    with pytest.raises(ValidationError):
        RuntimeEvidenceRequest(**_base_kwargs(keywords=[""]))
    RuntimeEvidenceRequest(**_base_kwargs(keywords=["y" * 200]))


def test_service_bounds():
    RuntimeEvidenceRequest(**_base_kwargs(services=["s"] * 10))
    with pytest.raises(ValidationError):
        RuntimeEvidenceRequest(**_base_kwargs(services=["s"] * 11))


def test_severity_strict_enum():
    req = RuntimeEvidenceRequest(**_base_kwargs(severities=["ERROR", "WARN"]))
    assert req.severities == [LogSeverity.ERROR, LogSeverity.WARN]
    # Unknown values must reject, never silently broaden the query.
    with pytest.raises(ValidationError):
        RuntimeEvidenceRequest(**_base_kwargs(severities=["NOPE"]))
    with pytest.raises(ValidationError):
        RuntimeEvidenceRequest(**_base_kwargs(severities=["error"]))


def test_identifier_shape_bounds_without_static_allowlist():
    # Part 10: key allowlisting is per-mapping at the provider boundary
    # (validate_identifier_items); domain construction enforces shape only.
    req = RuntimeEvidenceRequest(**_base_kwargs(identifiers={"trace_id": "abc"}))
    assert req.identifiers == {"trace_id": "abc"}
    # Unknown keys now construct fine — the boundary rejects them.
    req = RuntimeEvidenceRequest(**_base_kwargs(identifiers={"_source.password": "x"}))
    assert req.identifiers == {"_source.password": "x"}
    with pytest.raises(ValidationError):
        RuntimeEvidenceRequest(**_base_kwargs(identifiers={"trace_id": ""}))
    with pytest.raises(ValidationError):
        RuntimeEvidenceRequest(**_base_kwargs(identifiers={"trace_id": "x" * 501}))


def test_identifier_boundary_rejects_unknown_keys():
    from investigation_agent_platform.domain.common.exceptions import DomainValidationException
    from investigation_agent_platform.domain.observability.mapping import generic_ecs_mapping
    from investigation_agent_platform.infrastructure.evidence.runtime.validation import (
        validate_identifier_items,
    )

    mapping = generic_ecs_mapping()
    assert validate_identifier_items({"trace_id": "abc"}, mapping) == [("trace_id", "abc")]
    with pytest.raises(DomainValidationException):
        validate_identifier_items({"_source.password": "x"}, mapping)
    with pytest.raises(DomainValidationException):
        validate_identifier_items({"trace_id ": "x"}, mapping)


def test_environment_pattern():
    RuntimeEvidenceRequest(**_base_kwargs(environment="production"))
    RuntimeEvidenceRequest(**_base_kwargs(environment="staging-2"))
    with pytest.raises(ValidationError):
        RuntimeEvidenceRequest(**_base_kwargs(environment="prod-*"))
    with pytest.raises(ValidationError):
        RuntimeEvidenceRequest(**_base_kwargs(environment="prod/x"))


def test_cursor_length():
    RuntimeEvidenceRequest(**_base_kwargs(cursor="c" * 4096))
    with pytest.raises(ValidationError):
        RuntimeEvidenceRequest(**_base_kwargs(cursor="c" * 4097))


# Part 10: the ALLOWED_LABEL_KEYS re-export is removed with the static
# allowlist; the boundary check lives in validate_identifier_items
# (covered by test_identifier_boundary_rejects_unknown_keys above).


# --- Exceptions -----------------------------------------------------------------


def test_exception_contracts():
    assert InvalidCursorException("bad").error_code == "INVALID_CURSOR"
    assert InvalidCursorException("bad").http_status_code == 400
    assert InvalidCursorException("bad").retryable is False
    assert ProviderTimeoutException("slow").error_code == "PROVIDER_TIMEOUT"
    assert ProviderTimeoutException("slow").retryable is True
    assert ProviderUnavailableException("down").error_code == "PROVIDER_UNAVAILABLE"
    assert ProviderUnavailableException("down").http_status_code == 503


# --- Completeness -----------------------------------------------------------------


def test_completeness_invariants():
    EvidenceCompleteness(complete=True, returned_count=0, examined_count=0)
    EvidenceCompleteness(
        complete=False,
        truncated=False,
        reason=CompletenessReason.PAGINATION_AVAILABLE,
        returned_count=25,
        examined_count=25,
    )
    EvidenceCompleteness(
        complete=False,
        truncated=True,
        reason=CompletenessReason.PROVIDER_LIMIT,
        returned_count=100,
        examined_count=100,
    )
    with pytest.raises(ValidationError):
        EvidenceCompleteness(complete=True, truncated=True, reason=None)
    with pytest.raises(ValidationError):
        EvidenceCompleteness(complete=True, reason=CompletenessReason.PROVIDER_LIMIT)
    with pytest.raises(ValidationError):
        EvidenceCompleteness(complete=False, truncated=True, reason=None)


# --- Nullable observed_at ------------------------------------------------------------


def _make_evidence(observed_at):
    now = datetime.now(UTC)
    iid = uuid.uuid4()
    return Evidence(
        tenant_id="tenant-a",
        investigation_id=iid,
        evidence_type=EvidenceType.LOG,
        provider="test-provider",
        source="urn:test:logs",
        title="t",
        summary="s",
        fingerprint="fp-1",
        observed_at=observed_at,
        retrieved_at=now,
        provenance=EvidenceProvenance(
            tenant_id="tenant-a",
            investigation_id=iid,
            provider_type="test",
            requested_provider_id="test",
            actual_provider_id="test",
            source_system="test",
            retrieval_timestamp=now,
            query_fingerprint=QueryFingerprint(
                provider_type="test", operation="test", normalized_query_hash="abc"
            ),
            source_location=SourceLocation(system="test", identifier="id"),
        ),
        freshness=EvidenceFreshness(observed_at=observed_at, retrieved_at=now),
    )


def test_evidence_accepts_unknown_observation_time():
    ev = _make_evidence(None)
    assert ev.observed_at is None
    assert ev.freshness.observed_at is None
    ev2 = _make_evidence(datetime.now(UTC))
    assert ev2.observed_at is not None


# --- Config fail-closed ------------------------------------------------------------


def _clean_env(monkeypatch):
    monkeypatch.setenv("IAP_DATABASE_URI", "postgresql://test/test")
    monkeypatch.setenv("IAP_LLM_API_KEY", "test-key")
    for var in (
        "IAP_ELASTIC_CURSOR_SIGNING_KEY",
        "IAP_ELASTICSEARCH_MAX_HITS",
        "IAP_ELASTICSEARCH_MAX_TIME_WINDOW_SECONDS",
        "IAP_ELASTICSEARCH_MAX_KEYWORD_TERMS",
        "IAP_ELASTICSEARCH_MAX_SERVICES",
        "IAP_ELASTICSEARCH_QUERY_TIMEOUT",
        "IAP_ELASTIC_CURSOR_TTL_SECONDS",
    ):
        monkeypatch.delenv(var, raising=False)


def test_production_boot_halts_without_cursor_key(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("IAP_ENVIRONMENT", "production")
    from investigation_agent_platform.infrastructure.configuration.config import (
        load_application_config_from_env,
    )

    with pytest.raises(PlatformConfigurationError, match="IAP_ELASTIC_CURSOR_SIGNING_KEY"):
        load_application_config_from_env()


def test_nonproduction_boots_without_cursor_key(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("IAP_ENVIRONMENT", "development")
    from investigation_agent_platform.infrastructure.configuration.config import (
        load_application_config_from_env,
    )

    cfg = load_application_config_from_env()
    assert cfg.evidence.cursor_signing_key.get_secret_value() == ""
    assert cfg.evidence.elastic_max_hits == 200
    assert cfg.evidence.elastic_max_time_window_seconds == 7 * 24 * 3600
    assert cfg.evidence.elastic_max_keyword_terms == 20
    assert cfg.evidence.elastic_max_services == 10
    assert cfg.evidence.elastic_query_timeout_seconds == 25.0
    assert cfg.evidence.cursor_ttl_seconds == 3600


def test_cursor_key_loaded_when_configured(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("IAP_ENVIRONMENT", "production")
    monkeypatch.setenv("IAP_ELASTIC_CURSOR_SIGNING_KEY", "test-signing-key")
    from investigation_agent_platform.infrastructure.configuration.config import (
        load_application_config_from_env,
    )

    cfg = load_application_config_from_env()
    assert cfg.evidence.cursor_signing_key.get_secret_value() == "test-signing-key"


def test_operator_cannot_raise_security_ceilings(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("IAP_ENVIRONMENT", "development")
    monkeypatch.setenv("IAP_ELASTICSEARCH_MAX_HITS", "500")
    from investigation_agent_platform.infrastructure.configuration.config import (
        load_application_config_from_env,
    )

    with pytest.raises(PlatformConfigurationError):
        load_application_config_from_env()


def test_operator_may_lower_ceilings(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("IAP_ENVIRONMENT", "development")
    monkeypatch.setenv("IAP_ELASTICSEARCH_MAX_HITS", "50")
    from investigation_agent_platform.infrastructure.configuration.config import (
        load_application_config_from_env,
    )

    cfg = load_application_config_from_env()
    assert cfg.evidence.elastic_max_hits == 50


# --- Migration chain ------------------------------------------------------------


def test_migration_008_chain():
    import importlib.util
    from pathlib import Path

    path = (
        Path(__file__).resolve().parents[2]
        / "migrations"
        / "versions"
        / "008_evidence_observed_nullable.py"
    )
    assert path.is_file()
    spec = importlib.util.spec_from_file_location("migration_008", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.revision == "008_evidence_observed_nullable"
    assert mod.down_revision == "007_profile_id_128"


def test_evidence_orm_observed_at_nullable():
    from investigation_agent_platform.infrastructure.persistence.models import EvidenceORM

    assert EvidenceORM.__table__.c.observed_at.nullable is True
