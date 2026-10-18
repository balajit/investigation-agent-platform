# src/investigation_agent_platform/infrastructure/persistence/checkpoint_repository.py
"""SQLAlchemy-backed durable checkpoint repository (F-011, F-025)."""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any
from uuid import UUID

from sqlalchemy import select

from investigation_agent_platform.infrastructure.persistence.models import CheckpointORM
from investigation_agent_platform.infrastructure.persistence.rls import rls_session

logger = logging.getLogger(__name__)

# F-025: checkpoint payload schema version. Bump when the snapshot shape
# changes; readers refuse incompatible versions instead of blind validation.
CHECKPOINT_SCHEMA_VERSION = "v1"
APP_VERSION = "0.1.0"


class SqlAlchemyCheckpointRepository:
    """PostgreSQL-backed CheckpointRepository over investigation_checkpoints.

    Writes are monotonic (checkpoint_version strictly increases per
    investigation) and idempotent under retry: a duplicate save for a step
    that already has an equal-or-higher version is a no-op rather than an
    error, since Temporal activities may execute more than once.
    """

    def __init__(self, db_session_factory: Any) -> None:
        self._session_factory = db_session_factory

    async def save_checkpoint(
        self,
        tenant_id: str,
        investigation_id: UUID,
        step_number: int,
        state_snapshot: dict[str, Any],
    ) -> None:
        async with rls_session(self._session_factory, tenant_id) as session:
            result = await session.scalars(
                select(CheckpointORM)
                .where(
                    CheckpointORM.tenant_id == tenant_id,
                    CheckpointORM.investigation_id == investigation_id,
                )
                .order_by(CheckpointORM.checkpoint_version.desc())
                .limit(1)
            )
            latest = result.first()
            next_version = (latest.checkpoint_version + 1) if latest is not None else 1
            if (
                latest is not None
                and step_number <= latest.current_step
                and latest.checkpoint_version >= 1
            ):
                # Monotonicity guard: never regress the recorded step for this
                # investigation (safe replay of an already-applied checkpoint).
                if step_number < latest.current_step:
                    return
            row = CheckpointORM(
                tenant_id=tenant_id,
                investigation_id=investigation_id,
                current_phase=str(state_snapshot.get("phase", "")),
                current_step=step_number,
                agent_iteration=int(state_snapshot.get("iteration", 0)),
                budget_state_json=state_snapshot.get("budget", {}) or {},
                last_action_json=state_snapshot,
                checkpoint_version=next_version,
                schema_version=CHECKPOINT_SCHEMA_VERSION,
                app_version=APP_VERSION,
                state_hash=hashlib.sha256(
                    json.dumps(state_snapshot, sort_keys=True, default=str).encode("utf-8")
                ).hexdigest(),
            )
            session.add(row)

    async def get_latest_checkpoint(
        self, tenant_id: str, investigation_id: UUID
    ) -> dict[str, Any] | None:
        async with rls_session(self._session_factory, tenant_id) as session:
            result = await session.scalars(
                select(CheckpointORM)
                .where(
                    CheckpointORM.tenant_id == tenant_id,
                    CheckpointORM.investigation_id == investigation_id,
                )
                .order_by(CheckpointORM.checkpoint_version.desc())
                .limit(1)
            )
            row = result.first()
            if row is None:
                return None
            # F-025: refuse to hydrate snapshots written by an incompatible
            # schema version — replaying across breaking versions silently
            # corrupts workflow state.
            if (row.schema_version or "v1") != CHECKPOINT_SCHEMA_VERSION:
                logger.error(
                    "Checkpoint schema mismatch; refusing to hydrate",
                    extra={
                        "tenant_id": tenant_id,
                        "investigation_id": str(investigation_id),
                        "stored_schema": row.schema_version,
                        "expected_schema": CHECKPOINT_SCHEMA_VERSION,
                    },
                )
                return None
            return row.last_action_json
