# src/investigation_agent_platform/application/intake/batch_service.py
"""Bulk intake application service (Part 11.9).

Ingest validates every record pre-dispatch (cap, duplicate external keys,
profile resolution, parameters against the resolved profile schema, record
quotas), then persists the batch job + record rows + investigations
transactionally-per-store before any workflow starts. The workflow only
dispatches children for PENDING rows; resume and partial retries re-read
row state, so re-invocation is idempotent.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from investigation_agent_platform.domain.common.background_job import (
    BackgroundJob,
    BackgroundJobStatus,
)
from investigation_agent_platform.domain.common.extension import CapabilityScope
from investigation_agent_platform.domain.common.quotas import QuotaPolicy
from investigation_agent_platform.domain.intake.batch import (
    BATCH_INTAKE_CONTRACT_VERSION,
    BATCH_RETENTION_DAYS,
    MAX_BATCH_PAYLOAD_BYTES,
    MAX_BATCH_RECORDS,
    BatchIntakeRecord,
    BatchIntakeResult,
    BatchRecordResult,
    BatchRecordStatus,
)

logger = logging.getLogger(__name__)

BULK_INTAKE_BUILDER_PLUGIN_ID = "bulk-intake-service"
BULK_INTAKE_BUILDER_VERSION = "1.0"

_MAX_PARAM_KEYS = 50
_MAX_PARAM_KEY_CHARS = 128
_MAX_PARAM_BYTES = 8192


class BatchValidationError(ValueError):
    """Raised when batch records fail pre-dispatch validation."""


class BatchQuotaExceededError(Exception):
    """Raised when record quotas reject a batch pre-dispatch."""

    def __init__(self, tenant_id: str, detail: str = "") -> None:
        super().__init__(f"Batch quota exceeded for tenant {tenant_id}: {detail}")
        self.tenant_id = tenant_id


def validate_parameters(
    parameters: dict[str, Any], schema: dict[str, Any] | None, record_label: str
) -> None:
    """Validate record parameters: structural bounds always, profile schema when set.

    The schema subset honors `type: object`, `required`, `properties` field
    rules (`type`, `enum`, `maxLength`, `minimum`, `maximum`), and
    `additionalProperties: false`. Unknown keywords are ignored (documented
    subset); values must be JSON scalars or scalar lists in all cases.
    """
    if len(parameters) > _MAX_PARAM_KEYS:
        raise BatchValidationError(f"{record_label}: too many parameters")
    serialized = json.dumps(parameters, separators=(",", ":"))
    if len(serialized.encode()) > _MAX_PARAM_BYTES:
        raise BatchValidationError(f"{record_label}: parameters exceed 8KB")
    for key, value in parameters.items():
        if len(key) > _MAX_PARAM_KEY_CHARS:
            raise BatchValidationError(f"{record_label}: parameter name too long")
        _check_scalar(value, f"{record_label}.parameters.{key}")
    if not schema:
        return
    if not isinstance(schema, dict):
        raise BatchValidationError(f"{record_label}: invalid parameters schema")
    if schema.get("type", "object") != "object":
        raise BatchValidationError(f"{record_label}: parameters must be an object")
    for required in schema.get("required", []) or []:
        if required not in parameters:
            raise BatchValidationError(f"{record_label}: missing required parameter {required!r}")
    properties = schema.get("properties", {}) or {}
    for key, value in parameters.items():
        rule = properties.get(key, {})
        if not isinstance(rule, dict):
            continue
        _check_rule(value, rule, f"{record_label}.parameters.{key}")
    if schema.get("additionalProperties") is False:
        unknown = set(parameters) - set(properties)
        if unknown:
            raise BatchValidationError(f"{record_label}: unknown parameters {sorted(unknown)}")


def _check_scalar(value: Any, label: str) -> None:
    if value is None or isinstance(value, (str, int, float, bool)):
        return
    if isinstance(value, list) and all(
        item is None or isinstance(item, (str, int, float, bool)) for item in value
    ):
        return
    raise BatchValidationError(f"{label}: must be a JSON scalar or scalar list")


def _check_rule(value: Any, rule: dict[str, Any], label: str) -> None:
    expected = rule.get("type")
    if expected == "string" and not isinstance(value, str):
        raise BatchValidationError(f"{label}: must be a string")
    if expected == "integer" and not (isinstance(value, int) and not isinstance(value, bool)):
        raise BatchValidationError(f"{label}: must be an integer")
    if expected == "number" and not (
        isinstance(value, (int, float)) and not isinstance(value, bool)
    ):
        raise BatchValidationError(f"{label}: must be a number")
    if expected == "boolean" and not isinstance(value, bool):
        raise BatchValidationError(f"{label}: must be a boolean")
    if "enum" in rule and value not in rule["enum"]:
        raise BatchValidationError(f"{label}: not an allowed value")
    if isinstance(value, str) and "maxLength" in rule and len(value) > int(rule["maxLength"]):
        raise BatchValidationError(f"{label}: exceeds maxLength")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in rule and value < rule["minimum"]:
            raise BatchValidationError(f"{label}: below minimum")
        if "maximum" in rule and value > rule["maximum"]:
            raise BatchValidationError(f"{label}: above maximum")


class BulkIntakeService:
    """Batch ingest + read model over BackgroundJob + record rows."""

    def __init__(
        self,
        batch_repo: Any,
        job_repo: Any,
        profile_repo: Any,
        investigation_creator: Callable[..., Any] | None = None,
        quota_enforcer: Any | None = None,
        quota_policy: QuotaPolicy | None = None,
    ) -> None:
        self._batch_repo = batch_repo
        self._job_repo = job_repo
        self._profile_repo = profile_repo
        self._investigation_creator = investigation_creator
        self._quota_enforcer = quota_enforcer
        self._quota_policy = quota_policy

    # -- ingest ----------------------------------------------------------

    async def ingest_batch(
        self,
        tenant_id: str,
        records: list[BatchIntakeRecord],
        requested_by: str,
    ) -> tuple[BackgroundJob, list[BatchRecordResult]]:
        """Validate, persist, and create investigations for one batch."""
        if not records:
            raise BatchValidationError("Batch must contain at least one record")
        if len(records) > MAX_BATCH_RECORDS:
            raise BatchValidationError(
                f"Batch exceeds the {MAX_BATCH_RECORDS}-record cap; "
                "partition larger imports into multiple jobs"
            )
        payload_bytes = len(json.dumps([r.model_dump(mode="json") for r in records]).encode())
        if payload_bytes > MAX_BATCH_PAYLOAD_BYTES:
            raise BatchValidationError("Batch payload exceeds the 1MB limit")
        seen_keys: set[str] = set()
        for record in records:
            if record.external_key in seen_keys:
                raise BatchValidationError(
                    f"Duplicate external key {record.external_key!r} in batch"
                )
            seen_keys.add(record.external_key)
        if self._quota_enforcer is not None and self._quota_policy is not None:
            scope = CapabilityScope(tenant_id=tenant_id)
            for record in records:
                decision = await self._quota_enforcer.check(
                    scope, "batch.record", self._quota_policy
                )
                if not decision.allowed:
                    raise BatchQuotaExceededError(tenant_id, "max_batch_records")
                try:
                    await self._validate_record(tenant_id, record)
                finally:
                    await self._quota_enforcer.release(scope, "batch.record")
        else:
            for record in records:
                await self._validate_record(tenant_id, record)

        job = BackgroundJob(
            id=uuid4(),
            tenant_id=tenant_id,
            kind="batch-intake",
            created_by=requested_by,
            input_ref=json.dumps(
                {"records": len(records), "contract_version": BATCH_INTAKE_CONTRACT_VERSION}
            )[:1024],
            retention_class="batch",
            quota_class="batch",
        )
        await self._job_repo.create(job)
        rows = [
            BatchRecordResult(
                record_index=index,
                external_key=record.external_key,
                application_id=record.application_id,
                status=BatchRecordStatus.PENDING,
            )
            for index, record in enumerate(records)
        ]
        await self._batch_repo.create_records(tenant_id, job.id, rows)
        if self._investigation_creator is not None:
            for index, record in enumerate(records):
                investigation = await self._investigation_creator(tenant_id, record)
                rows[index] = rows[index].model_copy(update={"investigation_id": investigation.id})
                await self._batch_repo.save_record(tenant_id, job.id, rows[index])
        return job, rows

    async def _validate_record(self, tenant_id: str, record: BatchIntakeRecord) -> None:
        """Resolve the profile and validate parameters (fail-closed)."""
        profile = await self._require_profile(tenant_id, record.application_id)
        schema = None
        if profile is not None:
            investigation_config = getattr(profile, "investigation_configuration", None)
            schema = getattr(investigation_config, "parameters_schema", None)
        validate_parameters(dict(record.parameters), schema, f"record {record.external_key!r}")

    async def _require_profile(self, tenant_id: str, application_id: str) -> Any:
        """Resolve the application profile; unknown applications fail closed."""
        if self._profile_repo is None:
            return None
        getter = getattr(self._profile_repo, "get_by_application_id", None)
        if getter is None:
            return None
        try:
            profile = await getter(tenant_id, application_id)
        except TypeError:
            profile = await getter(tenant_id, application_id, None)
        if profile is None:
            raise BatchValidationError(
                f"Unknown application {application_id!r} for tenant {tenant_id!r}"
            )
        return profile

    # -- read model --------------------------------------------------------

    async def get_batch(
        self, tenant_id: str, job_id: UUID, limit: int = 100, offset: int = 0
    ) -> tuple[BackgroundJob | None, list[BatchRecordResult], int]:
        """Batch job + paginated child records (404 when unknown/foreign)."""
        job: BackgroundJob | None = await self._job_repo.get_by_id(tenant_id, job_id)
        if job is None or getattr(job, "tenant_id", tenant_id) != tenant_id:
            return None, [], 0
        records, total = await self._batch_repo.list_records(tenant_id, job_id, limit, offset)
        return job, records, total

    async def summarize(self, tenant_id: str, job_id: UUID) -> BatchIntakeResult | None:
        """Aggregate counts over record rows (Part 11.9 result model)."""
        job: BackgroundJob | None = await self._job_repo.get_by_id(tenant_id, job_id)
        if job is None or getattr(job, "tenant_id", tenant_id) != tenant_id:
            return None
        records, _ = await self._batch_repo.list_records(tenant_id, job_id, limit=500, offset=0)
        counts = {status: 0 for status in BatchRecordStatus}
        for record in records:
            counts[record.status] += 1
        return BatchIntakeResult(
            job_id=job_id,
            tenant_id=tenant_id,
            total=len(records),
            succeeded=counts[BatchRecordStatus.DONE],
            failed=counts[BatchRecordStatus.FAILED],
            awaiting_input=counts[BatchRecordStatus.AWAITING_INPUT],
            canceled=counts[BatchRecordStatus.CANCELED],
        )

    # -- lifecycle -----------------------------------------------------------

    async def cancel_batch(self, tenant_id: str, job_id: UUID) -> BackgroundJob | None:
        """Mark the job CANCEL_REQUESTED (parent cancellation fans out to
        live children explicitly; see cancel_batch_children_activity)."""
        job: BackgroundJob | None = await self._job_repo.get_by_id(tenant_id, job_id)
        if job is None or getattr(job, "tenant_id", tenant_id) != tenant_id:
            return None
        if job.is_terminal:
            return job
        updated = job.model_copy(
            update={"status": BackgroundJobStatus.CANCEL_REQUESTED, "version": job.version + 1}
        )
        await self._job_repo.save(tenant_id, updated, job.version)
        return updated

    async def retry_failed(self, tenant_id: str, job_id: UUID) -> int:
        """Reset FAILED rows to PENDING for a partial retry; returns count."""
        job: BackgroundJob | None = await self._job_repo.get_by_id(tenant_id, job_id)
        if job is None or getattr(job, "tenant_id", tenant_id) != tenant_id:
            return 0
        attempt = getattr(job, "attempt", 0) + 1
        reset: int = await self._batch_repo.reset_failed(tenant_id, job_id, attempt)
        if reset:
            updated = job.model_copy(update={"attempt": attempt, "version": job.version + 1})
            await self._job_repo.save(tenant_id, updated, job.version)
        return reset

    async def purge_batch(self, tenant_id: str, job_id: UUID, retention_days: int = 90) -> bool:
        """Delete record rows for terminal batches past retention.

        Returns False when the job is missing, non-terminal, fresh, or has
        surviving rows (fail-safe: never report purged while rows remain).
        """

        job: BackgroundJob | None = await self._job_repo.get_by_id(tenant_id, job_id)
        if job is None or getattr(job, "tenant_id", tenant_id) != tenant_id:
            return False
        if not job.is_terminal:
            return False
        created: datetime = job.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        age_days = (datetime.now(UTC) - created).days
        if age_days <= max(1, retention_days or BATCH_RETENTION_DAYS):
            return False
        removed: int = await self._batch_repo.delete_job_records(tenant_id, job_id)
        _, remaining = await self._batch_repo.list_records(tenant_id, job_id, limit=1, offset=0)
        remaining_count: int = remaining
        return removed > 0 and remaining_count == 0

    @staticmethod
    def request_digest(records: list[BatchIntakeRecord]) -> str:
        """Canonical hash of a batch request (idempotency binding)."""
        canonical = json.dumps(
            [record.model_dump(mode="json") for record in records],
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode()).hexdigest()
