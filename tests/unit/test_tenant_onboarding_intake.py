"""Adversarial validation tests for the Phase 1B tenant intake contract."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from pydantic import ValidationError

from investigation_agent_platform.application.onboarding.intake import (
    PROVISIONAL_V01_PROVIDER_KEYS,
    IntakeSecretReference,
    OnboardingSourceKind,
    RepositoryReference,
    SourceReference,
    TenantIntakeEnvelope,
    TenantIntakeV1,
)
from investigation_agent_platform.application.onboarding_agent.contracts.linkage import (
    ReportLinkage,
)


def test_valid_versioned_intake_and_readonly_secret_reference() -> None:
    intake = TenantIntakeV1(
        application_id="app-a",
        environment="staging",
        repositories=(
            RepositoryReference(
                provider_key="sample-git-host",
                repository_id="team/service",
                ref="refs/heads/main",
            ),
        ),
        sources=(
            SourceReference(
                source_kind=OnboardingSourceKind.DATASOURCE,
                provider_key="sample-database-provider",
                resource_id="cluster-123/database-a",
                secret_references=(
                    IntakeSecretReference(provider="manager", name="DB_READ_TOKEN"),
                ),
            ),
        ),
    )
    assert intake.schema_version == "tenant-intake/v1"
    assert intake.sources[0].access_mode == "READ_ONLY"
    assert intake.sources[0].secret_references[0].access_mode == "READ_ONLY"


@pytest.mark.parametrize(
    "repository_id",
    [
        "/etc/passwd",
        "C:\\Windows\\system.ini",
        "https://host.invalid/team/repo",
        "team/../repo",
        "../team/repo",
        "~/.ssh/id_rsa",
    ],
)
def test_repository_identifier_rejects_paths_and_urls(repository_id: str) -> None:
    with pytest.raises(ValidationError):
        RepositoryReference(provider_key="sample-git-host", repository_id=repository_id)


@pytest.mark.parametrize(
    "resource_id",
    [
        "/var/log/app.log",
        "C:\\logs\\app.log",
        "file:///var/log/app.log",
        "https://host.invalid/api",
        "logs/../../secrets",
    ],
)
def test_source_resource_rejects_paths_and_arbitrary_urls(resource_id: str) -> None:
    with pytest.raises(ValidationError):
        SourceReference(
            source_kind=OnboardingSourceKind.LOGS,
            provider_key="sample-database-provider",
            resource_id=resource_id,
        )


@pytest.mark.parametrize("secret_name", ["hunter2", "token=abc123", "../secret", "C:\\secret"])
def test_secret_reference_rejects_values_and_paths(secret_name: str) -> None:
    with pytest.raises(ValidationError):
        IntakeSecretReference(provider="manager", name=secret_name)


def test_secret_reference_forbids_unknown_or_write_capable_fields() -> None:
    with pytest.raises(ValidationError):
        IntakeSecretReference(provider="manager", name="DB_PASSWORD", value="secret")  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        IntakeSecretReference(
            provider="manager",
            name="DB_PASSWORD",
            access_mode="WRITE",  # type: ignore[arg-type]
        )


def test_schema_and_source_models_forbid_unknown_fields_and_versions() -> None:
    with pytest.raises(ValidationError):
        TenantIntakeV1(
            schema_version="tenant-intake/v2",  # type: ignore[arg-type]
            application_id="app-a",
            environment="test",
        )
    with pytest.raises(ValidationError):
        TenantIntakeV1(
            application_id="app-a",
            environment="test",
            arbitrary_url="https://host.invalid",  # type: ignore[call-arg]
        )


def test_report_reference_is_linked_not_embedded_in_intake_hash() -> None:
    intake = TenantIntakeV1(
        application_id="app-a",
        environment="test",
        repositories=(RepositoryReference(provider_key="sample-git-host", repository_id="team/service"),),
    )
    plain = TenantIntakeEnvelope(intake=intake)
    linked = TenantIntakeEnvelope(
        intake=intake,
        coverage_report=ReportLinkage(
            request_id=uuid4(),
            revision=1,
            generation=2,
            report_id=uuid4(),
            report_content_hash="a" * 64,
        ),
    )
    assert plain.intake_content_hash == linked.intake_content_hash


def test_provisional_provider_keys_are_pinned_to_approved_sample_set() -> None:
    assert PROVISIONAL_V01_PROVIDER_KEYS == frozenset(
        {"sample-git-host", "sample-database-provider"}
    )


def test_unknown_provider_key_is_rejected() -> None:
    with pytest.raises(ValidationError, match="provisional v0.1 provider set"):
        TenantIntakeV1(
            application_id="app-a",
            environment="test",
            repositories=(
                RepositoryReference(provider_key="other-git-host", repository_id="team/service"),
            ),
        )


def test_example_provider_keys_match_provisional_set() -> None:
    example_path = (
        Path(__file__).resolve().parents[2] / "config" / "tenant-id.env.example.yaml"
    )
    example = yaml.safe_load(example_path.read_text(encoding="utf-8"))
    provider_keys = {
        *(repository["provider_key"] for repository in example.get("repositories", [])),
        *(source["provider_key"] for source in example.get("sources", [])),
    }
    assert provider_keys <= PROVISIONAL_V01_PROVIDER_KEYS
