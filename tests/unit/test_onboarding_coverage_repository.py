"""Unit tests for immutable coverage report filing and intake linkage."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Self, cast
from uuid import UUID, uuid4

import pytest

from investigation_agent_platform.application.onboarding.intake import (
    TenantIntakeEnvelope,
    TenantIntakeV1,
)
from investigation_agent_platform.application.onboarding_agent.contracts.linkage import (
    ReportLinkage,
    input_manifest_hash,
    report_content_hash,
)
from investigation_agent_platform.infrastructure.persistence.models import CoverageReportORM
from investigation_agent_platform.infrastructure.persistence.onboarding_coverage_repository import (
    SqlAlchemyOnboardingCoverageRepository,
)


def _outputs(content_hash: str = "a" * 64) -> list[Mapping[str, Any]]:
    return [
        {
            "content_hash": content_hash,
            "tool": "census",
            "tool_version": "1.0",
            "schema_version": "sniffer/v1",
        }
    ]


def _report(
    request_id: UUID,
    *,
    generation: int = 1,
    content_hash: str = "a" * 64,
    verdict: str = "python",
    report_id: str | None = None,
) -> dict[str, Any]:
    manifest_hash = input_manifest_hash(
        sniffer_outputs=_outputs(content_hash),
        estate_sha="estate-1",
        registry_version="registry-1",
        catalog_version="catalog-1",
        generation=generation,
    )
    body: dict[str, Any] = {
        "report_id": report_id or str(uuid4()),
        "request_id": str(request_id),
        "revision": 1,
        "generation": generation,
        "report_version": "coverage/v1",
        "registry_version": "registry-1",
        "catalog_version": "catalog-1",
        "estate_sha": "estate-1",
        "input_manifest_hash": manifest_hash,
        "analyzer_version": "analyzer-1",
        "verdicts": [{"service": verdict, "tier": "generic"}],
        "bridges": [],
        "gaps": [],
        "remediation": [],
        "limitations": ["unit test fixture"],
    }
    body["report_content_hash"] = report_content_hash(body)
    return body


class _FakeSession:
    def __init__(self, rows: dict[tuple[str, UUID, int, int], Any], audits: list[dict[str, Any]]):
        self.rows = rows
        self.audits = audits

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    def begin(self) -> Self:
        return self

    async def execute(self, statement: Any, params: dict[str, Any] | None = None) -> Any:
        sql = str(statement)
        if "record_onboarding_coverage_filing" in sql:
            assert params is not None
            self.audits.append(dict(params))
        return None

    async def scalar(self, statement: Any) -> Any:
        if getattr(statement, "is_insert", False):
            values = {
                key.key: value.value if hasattr(value, "value") else value
                for key, value in statement._values.items()
            }
            key = (
                values["tenant_id"],
                UUID(str(values["request_id"])),
                values["revision"],
                values["generation"],
            )
            if key in self.rows:
                return None
            row = CoverageReportORM(**values)
            self.rows[key] = row
            return row.id
        from sqlalchemy.sql import visitors

        values = {
            cast(Any, element).left.key: cast(Any, element).right.value
            for element in visitors.iterate(statement.whereclause)
            if hasattr(element, "left")
            and hasattr(cast(Any, element).left, "key")
            and hasattr(cast(Any, element).right, "value")
        }
        key = (
            values["tenant_id"],
            UUID(str(values["request_id"])),
            values["revision"],
            values["generation"],
        )
        return self.rows.get(key)


class _FakeSessionFactory:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, UUID, int, int], Any] = {}
        self.audits: list[dict[str, Any]] = []

    def __call__(self) -> _FakeSession:
        return _FakeSession(self.rows, self.audits)


@pytest.mark.asyncio
async def test_duplicate_filing_returns_same_identity_and_writes_one_audit() -> None:
    factory = _FakeSessionFactory()
    repository = SqlAlchemyOnboardingCoverageRepository(factory)
    request_id = uuid4()
    first = await repository.file_report(
        tenant_id="tenant-a",
        request_id=request_id,
        revision=1,
        generation=1,
        report_body=_report(request_id),
        sniffer_outputs=_outputs(),
        estate_sha="estate-1",
        registry_version="registry-1",
        catalog_version="catalog-1",
        worker_id="worker-1",
        fencing_token=7,
    )
    second = await repository.file_report(
        tenant_id="tenant-a",
        request_id=request_id,
        revision=1,
        generation=1,
        report_body=_report(request_id, report_id=str(first.id)),
        sniffer_outputs=_outputs(),
        estate_sha="estate-1",
        registry_version="registry-1",
        catalog_version="catalog-1",
        worker_id="worker-1",
        fencing_token=7,
    )

    assert first.id == second.id
    assert len(factory.rows) == 1
    assert len(factory.audits) == 1
    assert factory.audits[0]["fencing_token"] == 7


@pytest.mark.asyncio
async def test_generation_reuses_identity_only_for_same_hashes() -> None:
    factory = _FakeSessionFactory()
    repository = SqlAlchemyOnboardingCoverageRepository(factory)
    request_id = uuid4()
    first = await repository.file_report(
        tenant_id="tenant-a",
        request_id=request_id,
        revision=1,
        generation=1,
        report_body=_report(request_id, generation=1),
        sniffer_outputs=_outputs(),
        estate_sha="estate-1",
        registry_version="registry-1",
        catalog_version="catalog-1",
        worker_id="worker-1",
        fencing_token=1,
    )
    second = await repository.file_report(
        tenant_id="tenant-a",
        request_id=request_id,
        revision=1,
        generation=2,
        report_body=_report(request_id, generation=2, content_hash="b" * 64, verdict="java"),
        sniffer_outputs=_outputs("b" * 64),
        estate_sha="estate-1",
        registry_version="registry-1",
        catalog_version="catalog-1",
        worker_id="worker-1",
        fencing_token=1,
    )

    assert first.id != second.id
    assert second.generation == 2
    assert len(factory.rows) == 2
    assert len(factory.audits) == 2


@pytest.mark.asyncio
async def test_tampered_content_hash_is_rejected_before_database_access() -> None:
    factory = _FakeSessionFactory()
    repository = SqlAlchemyOnboardingCoverageRepository(factory)
    request_id = uuid4()
    body = _report(request_id)
    body["verdicts"] = [{"service": "tampered", "tier": "generic"}]

    with pytest.raises(ValueError, match="report_content_hash"):
        await repository.file_report(
            tenant_id="tenant-a",
            request_id=request_id,
            revision=1,
            generation=1,
            report_body=body,
            sniffer_outputs=_outputs(),
            estate_sha="estate-1",
            registry_version="registry-1",
            catalog_version="catalog-1",
            worker_id="worker-1",
            fencing_token=1,
        )
    assert not factory.rows


@pytest.mark.asyncio
async def test_float_input_is_rejected_by_shared_canonicalizer() -> None:
    factory = _FakeSessionFactory()
    repository = SqlAlchemyOnboardingCoverageRepository(factory)
    request_id = uuid4()
    body = _report(request_id)
    body["verdicts"] = [{"service": "python", "tier": "generic", "score": 0.5}]

    with pytest.raises(TypeError, match="float values"):
        await repository.file_report(
            tenant_id="tenant-a",
            request_id=request_id,
            revision=1,
            generation=1,
            report_body=body,
            sniffer_outputs=_outputs(),
            estate_sha="estate-1",
            registry_version="registry-1",
            catalog_version="catalog-1",
            worker_id="worker-1",
            fencing_token=1,
        )


def test_intake_hash_excludes_linked_report_reference() -> None:
    intake = TenantIntakeV1(application_id="app-a", environment="test")
    baseline = TenantIntakeEnvelope(intake=intake)
    linked = TenantIntakeEnvelope(
        intake=intake,
        coverage_report=ReportLinkage(
            request_id=uuid4(),
            revision=1,
            generation=1,
            report_id=uuid4(),
            report_content_hash="a" * 64,
        ),
    )

    assert linked.intake_content_hash == baseline.intake_content_hash
