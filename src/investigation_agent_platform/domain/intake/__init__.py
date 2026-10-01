# src/investigation_agent_platform/domain/intake/__init__.py
"""Bulk intake domain (Part 11.9)."""

from investigation_agent_platform.domain.intake.batch import (
    BATCH_INTAKE_CONTRACT_VERSION,
    BATCH_RETENTION_DAYS,
    CAN_RECORD_THRESHOLD,
    MAX_ACTIVE_CHILDREN,
    MAX_BATCH_PAYLOAD_BYTES,
    MAX_BATCH_RECORDS,
    BatchIntakeRecord,
    BatchIntakeRequest,
    BatchIntakeResult,
    BatchRecordResult,
    BatchRecordStatus,
    child_workflow_id,
)

__all__ = [
    "BATCH_INTAKE_CONTRACT_VERSION",
    "BATCH_RETENTION_DAYS",
    "CAN_RECORD_THRESHOLD",
    "MAX_ACTIVE_CHILDREN",
    "MAX_BATCH_PAYLOAD_BYTES",
    "MAX_BATCH_RECORDS",
    "BatchIntakeRecord",
    "BatchIntakeRequest",
    "BatchIntakeResult",
    "BatchRecordResult",
    "BatchRecordStatus",
    "child_workflow_id",
]
