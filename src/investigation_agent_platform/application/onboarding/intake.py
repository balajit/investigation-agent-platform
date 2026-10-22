"""Strict, versioned tenant onboarding intake schemas (Phase 1B)."""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from investigation_agent_platform.application.onboarding_agent.contracts.linkage import (
    ReportLinkage,
    canonical_json_bytes,
    sha256_hex,
)

_PROVIDER_KEY = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
_REPOSITORY_ID = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}/[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$"
)
_RESOURCE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,255}$")
_URL_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
_SECRET_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")

PROVISIONAL_V01_PROVIDER_KEYS = frozenset({"sample-git-host", "sample-database-provider"})
"""Provider keys transcribed from the shipped example YAML.

This set is provisional and sample-only for development/test DRAFT persistence.
It must be replaced by the approved catalog before production; see the ledger
capability-to-lifecycle matrix item.
"""


class OnboardingSourceKind(StrEnum):
    """Initially supported IAP configuration source categories."""

    LOGS = "logs"
    TRACES = "traces"
    METRICS = "metrics"
    DATASOURCE = "datasource"
    CONFIG_SERVER = "config_server"
    KNOWLEDGE_CORPUS = "knowledge_corpus"


class IntakeSecretReference(BaseModel):
    """Typed secret name with an explicitly read-only use contract."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: Literal["env", "manager"]
    name: str = Field(min_length=1, max_length=128)
    access_mode: Literal["READ_ONLY"] = "READ_ONLY"

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if not _SECRET_NAME.fullmatch(value):
            raise ValueError("secret name must be an allowlisted identifier, never a value or path")
        return value


class RepositoryReference(BaseModel):
    """Provider identifier and optional mutable ref; approval pins its commit."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider_key: str = Field(min_length=1, max_length=32)
    repository_id: str = Field(min_length=3, max_length=257)
    ref: str | None = Field(default=None, min_length=1, max_length=128)

    @field_validator("provider_key")
    @classmethod
    def validate_provider_key(cls, value: str) -> str:
        if not _PROVIDER_KEY.fullmatch(value):
            raise ValueError("provider_key must be a structured allowlist key")
        if value not in PROVISIONAL_V01_PROVIDER_KEYS:
            raise ValueError("provider_key is not in the provisional v0.1 provider set")
        return value

    @field_validator("repository_id")
    @classmethod
    def validate_repository_id(cls, value: str) -> str:
        if not _REPOSITORY_ID.fullmatch(value) or ".." in value or "\\" in value:
            raise ValueError("repository_id must be a structured provider identifier")
        if _URL_SCHEME.match(value) or value.startswith(("/", "~")):
            raise ValueError("repository_id cannot be a URL or filesystem path")
        return value

    @field_validator("ref")
    @classmethod
    def validate_ref(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if (
            _URL_SCHEME.match(value)
            or value.startswith(("/", "\\", "~"))
            or "\\" in value
            or ".." in value.split("/")
        ):
            raise ValueError("repository ref cannot be a URL or filesystem path")
        return value


class SourceReference(BaseModel):
    """Structured, scoped reference to one external source configuration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_kind: OnboardingSourceKind
    provider_key: str = Field(min_length=1, max_length=32)
    resource_id: str = Field(min_length=1, max_length=256)
    access_mode: Literal["READ_ONLY"] = "READ_ONLY"
    secret_references: tuple[IntakeSecretReference, ...] = Field(default_factory=tuple, max_length=16)

    @field_validator("provider_key")
    @classmethod
    def validate_provider_key(cls, value: str) -> str:
        if not _PROVIDER_KEY.fullmatch(value):
            raise ValueError("provider_key must be a structured allowlist key")
        if value not in PROVISIONAL_V01_PROVIDER_KEYS:
            raise ValueError("provider_key is not in the provisional v0.1 provider set")
        return value

    @field_validator("resource_id")
    @classmethod
    def validate_resource_id(cls, value: str) -> str:
        if (
            not _RESOURCE_ID.fullmatch(value)
            or _URL_SCHEME.match(value)
            or value.startswith(("/", "\\", "~"))
            or "\\" in value
            or any(part == ".." for part in value.split("/"))
        ):
            raise ValueError("resource_id must be structured, not a URL or filesystem path")
        return value


class TenantIntakeV1(BaseModel):
    """Operator-supplied configuration only; contains no server paths/secrets."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["tenant-intake/v1"] = "tenant-intake/v1"
    application_id: str = Field(min_length=1, max_length=128)
    environment: str = Field(min_length=1, max_length=64)
    repositories: tuple[RepositoryReference, ...] = Field(default_factory=tuple, max_length=32)
    sources: tuple[SourceReference, ...] = Field(default_factory=tuple, max_length=128)

    @field_validator("repositories")
    @classmethod
    def validate_repository_providers(
        cls, values: tuple[RepositoryReference, ...]
    ) -> tuple[RepositoryReference, ...]:
        for repository in values:
            if repository.provider_key not in PROVISIONAL_V01_PROVIDER_KEYS:
                raise ValueError("repository provider key is not in the provisional v0.1 provider set")
        return values

    @field_validator("sources")
    @classmethod
    def validate_source_providers(
        cls, values: tuple[SourceReference, ...]
    ) -> tuple[SourceReference, ...]:
        for source in values:
            if source.provider_key not in PROVISIONAL_V01_PROVIDER_KEYS:
                raise ValueError("source provider key is not in the provisional v0.1 provider set")
        return values


class TenantIntakeEnvelope(BaseModel):
    """Intake and linked report reference are separate hash domains."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    intake: TenantIntakeV1
    coverage_report: ReportLinkage | None = None

    @property
    def intake_content_hash(self) -> str:
        """Hash only intake; a linked derived report never changes request hash."""
        intake_json = self.intake.model_dump(mode="json", exclude_none=True)
        return sha256_hex(canonical_json_bytes(intake_json))
