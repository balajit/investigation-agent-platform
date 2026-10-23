"""Tenant-scoped, idempotent filing for onboarding coverage reports."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert

from investigation_agent_platform.application.onboarding_agent.contracts.coverage import (
    CoverageReport,
)
from investigation_agent_platform.application.onboarding_agent.contracts.linkage import (
    input_manifest_hash,
    report_content_hash,
)
from investigation_agent_platform.infrastructure.persistence.models import CoverageReportORM


class CoverageReportConflictError(ValueError):
    """A generation coordinate already exists with different report content."""


def _report_json_value(value: Any) -> Any:
    """Normalize UUIDs at the repository boundary before shared JSON hashing."""
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Mapping):
        return {key: _report_json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_report_json_value(item) for item in value]
    return value


class SqlAlchemyOnboardingCoverageRepository:
    """File immutable reports and their attribution audit in one transaction."""

    def __init__(self, session_factory: Any) -> None:
        self._session_factory = session_factory

    async def file_report(
        self,
        *,
        tenant_id: str,
        request_id: UUID,
        revision: int,
        generation: int,
        report_body: Mapping[str, Any],
        sniffer_outputs: list[Mapping[str, Any]],
        estate_sha: str,
        registry_version: str,
        catalog_version: str,
        worker_id: str,
        fencing_token: int,
    ) -> CoverageReportORM:
        """Insert once per scope/generation, or return the matching existing row.

        Both hashes are recomputed from the supplied content; callers cannot
        choose or override either persisted digest. A duplicate coordinate is
        a successful replay only when its recomputed hashes match.
        """
        if not tenant_id or len(tenant_id) > 64:
            raise ValueError("tenant_id must be non-empty and at most 64 characters")
        if revision < 1 or generation < 1:
            raise ValueError("revision and generation must be >= 1")
        if not worker_id or len(worker_id) > 128:
            raise ValueError("worker_id must be non-empty and at most 128 characters")
        if fencing_token < 0:
            raise ValueError("fencing_token must be non-negative")

        computed_input_hash = input_manifest_hash(
            sniffer_outputs=sniffer_outputs,
            estate_sha=estate_sha,
            registry_version=registry_version,
            catalog_version=catalog_version,
            generation=generation,
        )
        report_id = UUID(str(report_body.get("report_id", uuid4())))
        declared_input_hash = report_body.get("input_manifest_hash")
        declared_content_hash = report_body.get("report_content_hash")
        if declared_input_hash != computed_input_hash:
            raise ValueError("input_manifest_hash does not match the canonical input manifest")
        candidate = _report_json_value(report_body)
        candidate.update(
            {
                "report_id": str(report_id),
                "request_id": str(request_id),
                "revision": revision,
                "generation": generation,
                "registry_version": registry_version,
                "catalog_version": catalog_version,
                "estate_sha": estate_sha,
                "input_manifest_hash": computed_input_hash,
            }
        )
        candidate.pop("report_content_hash", None)
        computed_content_hash = report_content_hash(candidate)
        if declared_content_hash != computed_content_hash:
            raise ValueError("report_content_hash does not match the canonical report body")
        candidate["report_content_hash"] = computed_content_hash
        validated = CoverageReport.model_validate(candidate)
        report_json = validated.model_dump(mode="json")

        async with self._session_factory() as session:
            async with session.begin():
                await session.execute(
                    text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
                    {"tenant_id": tenant_id},
                )
                await session.execute(
                    text("SELECT set_config('iap.worker_id', :worker_id, true)"),
                    {"worker_id": worker_id},
                )

                statement = (
                    insert(CoverageReportORM)
                    .values(
                        id=report_id,
                        tenant_id=tenant_id,
                        request_id=request_id,
                        revision=revision,
                        generation=generation,
                        report_version=validated.report_version,
                        registry_version=registry_version,
                        catalog_version=catalog_version,
                        estate_sha=estate_sha,
                        input_manifest_hash=computed_input_hash,
                        report_content_hash=computed_content_hash,
                        analyzer_version=validated.analyzer_version,
                        model_version=validated.model_version,
                        report_json=report_json,
                    )
                    .on_conflict_do_nothing(
                        constraint="uq_coverage_report_request_generation"
                    )
                    .returning(CoverageReportORM.id)
                )
                inserted_id = await session.scalar(statement)
                row = await session.scalar(
                    select(CoverageReportORM).where(
                        CoverageReportORM.tenant_id == tenant_id,
                        CoverageReportORM.request_id == request_id,
                        CoverageReportORM.revision == revision,
                        CoverageReportORM.generation == generation,
                    )
                )
                if row is None:
                    raise RuntimeError("coverage report insert did not produce a readable row")
                if (
                    row.input_manifest_hash != computed_input_hash
                    or row.report_content_hash != computed_content_hash
                ):
                    raise CoverageReportConflictError(
                        "coverage report generation already exists with different input or content"
                    )

                if inserted_id is not None:
                    await session.execute(
                        text(
                            "SELECT record_onboarding_coverage_filing("
                            ":tenant_id, :request_id, :revision, :generation, "
                            ":fencing_token, :report_content_hash)"
                        ),
                        {
                            "tenant_id": tenant_id,
                            "request_id": request_id,
                            "revision": revision,
                            "generation": generation,
                            "fencing_token": fencing_token,
                            "report_content_hash": computed_content_hash,
                        },
                    )
                return cast(CoverageReportORM, row)

    async def read_report(
        self, tenant_id: str, request_id: UUID, revision: int, generation: int
    ) -> CoverageReportORM | None:
        """Return one filed report by linkage, or None (DEP-10 report GET)."""
        if not tenant_id or len(tenant_id) > 64:
            raise ValueError("tenant_id must be non-empty and at most 64 characters")
        if revision < 1 or generation < 1:
            raise ValueError("revision and generation must be >= 1")
        async with self._session_factory() as session:
            await session.execute(
                text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
                {"tenant_id": tenant_id},
            )
            return cast(
                CoverageReportORM | None,
                await session.scalar(
                    select(CoverageReportORM).where(
                        CoverageReportORM.tenant_id == tenant_id,
                        CoverageReportORM.request_id == request_id,
                        CoverageReportORM.revision == revision,
                        CoverageReportORM.generation == generation,
                    )
                ),
            )
