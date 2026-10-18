# tests/integration/spring_config/test_derivation.py
"""Derivation mapping tests (pure function, fixture properties)."""

from investigation_agent_platform.integrations.spring_config.derivation import (
    derive_profile,
    parse_jdbc_url,
)
from investigation_agent_platform.integrations.spring_config.models import (
    ConfigResult,
    SpringBinding,
)


def _binding(**overrides):
    kwargs = {
        "server_url": "https://cfg.example",
        "application": "shop",
        "profile": "prod",
    }
    kwargs.update(overrides)
    return SpringBinding(**kwargs)


def test_parse_jdbc_variants() -> None:
    assert parse_jdbc_url("jdbc:postgresql://db.internal:5432/shopdb") == (
        "postgres",
        "db.internal",
        "shopdb",
    )
    assert parse_jdbc_url("jdbc:oracle:thin:@db.internal:1521:ORCL") == (
        "oracle",
        "db.internal",
        "ORCL",
    )
    assert parse_jdbc_url("jdbc:mysql://db.internal/shopdb") == ("mysql", "db.internal", "shopdb")
    assert parse_jdbc_url("jdbc:sqlserver://db.internal;databaseName=shop") == (
        "sqlserver",
        "db.internal",
        "shop",
    )
    assert parse_jdbc_url("not-a-url") == ("other", None, None)


def test_parse_jdbc_strips_credentials() -> None:
    kind, host, database = parse_jdbc_url("jdbc:postgresql://app:s3cr3t@db.internal/shopdb")
    assert kind == "postgres"
    assert host == "db.internal"
    assert database == "shopdb"


def test_full_derivation_is_draft_without_secrets() -> None:
    result = ConfigResult(
        application="shop",
        profile="prod",
        properties={
            "spring.application.name": "checkout",
            "spring.profiles.active": "prod",
            "spring.datasource.url": "jdbc:postgresql://db.internal:5432/shopdb",
            "server.port": "8080",
            "management.zipkin.base-url": "https://zipkin.internal",
            "spring.kafka.bootstrap-servers": "kafka.internal:9092",
            "spring.kafka.template.default-topic": "orders",
            "cloud.aws.s3.bucket": "shop-evidence",
            "spring.datasource.password": "s3cr3t",
            "custom.vendor.flag": "on",
        },
        property_sources=("remote",),
    )
    profile, report = derive_profile("tenant-a", "checkout-app", _binding(), result)
    assert profile.status.value == "DRAFT"
    assert profile.tenant_id == "tenant-a"
    assert profile.id == "checkout-app"
    assert profile.environment == "production"
    assert [s.name for s in profile.services] == ["checkout"]
    assert profile.services[0].telemetry.trace_backend == "zipkin"
    kinds = {ds.id: ds.kind.value for ds in profile.datasources}
    assert kinds["primary"] == "postgres"
    assert kinds["kafka"] == "kafka"
    assert kinds["s3"] == "s3"
    assert all((ds.connection_ref or "") != "s3cr3t" for ds in profile.datasources)
    assert "custom.vendor.flag" in report.unmapped_keys
    assert "spring.datasource.password" in report.dropped_sensitive_keys
    assert "datasources[primary].connection_ref" in report.needs_review
    dumped = profile.model_dump_json()
    assert "s3cr3t" not in dumped


def test_minimal_derivation_single_unnamed_service() -> None:
    result = ConfigResult(application="shop", profile="prod", properties={})
    profile, _ = derive_profile("tenant-a", "app-x", _binding(), result)
    assert profile.status.value == "DRAFT"
    assert [s.name for s in profile.services] == ["app-x"]
    assert profile.datasources == []
    assert profile.environment == "production"


def test_staging_alias_environment() -> None:
    result = ConfigResult(
        application="shop",
        profile="staging",
        properties={"spring.profiles.active": "staging-eu"},
    )
    profile, _ = derive_profile("tenant-a", "app-x", _binding(), result)
    assert profile.environment == "staging"
    assert "staging-eu" in profile.environments
