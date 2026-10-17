# src/investigation_agent_platform/infrastructure/persistence/evidence_repository.py
"""SQLAlchemy evidence repository."""

import logging
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert

from investigation_agent_platform.api.errors import sanitize_extra
from investigation_agent_platform.domain.evidence.models import (
    REFERENCE_LIFECYCLE_TYPES,
    Evidence,
)
from investigation_agent_platform.infrastructure.persistence.models import (
    EvidenceBankInterestORM,
    EvidenceORM,
)
from investigation_agent_platform.infrastructure.persistence.rls import rls_session

logger = logging.getLogger(__name__)


class SqlAlchemyEvidenceRepository:
    """PostgreSQL-backed EvidenceRepository over the evidence table with strict tenant scoping."""

    def __init__(self, db_session_factory: Any) -> None:
        self._session_factory = db_session_factory

    @staticmethod
    def _to_orm(evidence: Evidence, investigation_id: UUID | None = None) -> EvidenceORM:
        return EvidenceORM(
            investigation_id=investigation_id,
            tenant_id=evidence.tenant_id,
            type=evidence.evidence_type.value,
            source_type=evidence.provider,
            source_id=evidence.source,
            observed_at=evidence.observed_at,
            retrieved_at=evidence.retrieved_at,
            summary=evidence.summary,
            content_reference=evidence.content_uri or None,
            classification=evidence.classification.value,
            is_sanitized=evidence.is_redacted,
            evidence_key=str(evidence.evidence_id),
            fingerprint=evidence.fingerprint,
            payload_json=evidence.model_dump(mode="json"),
        )

    @staticmethod
    def _from_orm(row: EvidenceORM) -> Evidence:
        return Evidence.model_validate(row.payload_json)

    @staticmethod
    def _values(
        evidence: Evidence, tenant_id: str, investigation_id: UUID | None
    ) -> dict[str, Any]:
        return {
            "investigation_id": investigation_id,
            "tenant_id": tenant_id,
            "type": evidence.evidence_type.value,
            "source_type": evidence.provider,
            "source_id": evidence.source,
            "observed_at": evidence.observed_at,
            "retrieved_at": evidence.retrieved_at,
            "summary": evidence.summary,
            "content_reference": evidence.content_uri or None,
            "classification": evidence.classification.value,
            "is_sanitized": evidence.is_redacted,
            "evidence_key": str(evidence.evidence_id),
            "fingerprint": evidence.fingerprint,
            "payload_json": evidence.model_dump(mode="json"),
            "lifecycle_scope": SqlAlchemyEvidenceRepository._scope_for(evidence),
        }

    @staticmethod
    def _scope_for(evidence: Evidence) -> str:
        """Reference types are repo-scoped (shared); everything else is owned."""
        etype = getattr(evidence.evidence_type, "value", evidence.evidence_type)
        return "reference" if str(etype) in REFERENCE_LIFECYCLE_TYPES else "observational"

    @staticmethod
    @staticmethod
    async def _record_interest(
        session: Any, tenant_id: str, investigation_id: UUID, evidence_key: str
    ) -> None:
        """Idempotent bank-interest (PK conflict = already interested)."""
        stmt = (
            insert(EvidenceBankInterestORM)
            .values(
                tenant_id=tenant_id,
                investigation_id=investigation_id,
                evidence_key=evidence_key,
            )
            .on_conflict_do_nothing()
        )
        await session.execute(stmt)

    async def _resolve_conflict(
        self, session: Any, tenant_id: str, investigation_id: UUID | None, evidence: Evidence
    ) -> str:
        """Classify a conflicting row: same-investigation duplicate vs shared reference.

        Looks up the holder by fingerprint; a reference-type row held by
        ANOTHER investigation records bank-interest and reports shared.
        Same-holder (or unresolvable holder) stays deduplicated.
        """
        try:
            result = await session.execute(
                select(EvidenceORM.investigation_id, EvidenceORM.evidence_key).where(
                    EvidenceORM.tenant_id == tenant_id,
                    EvidenceORM.fingerprint == evidence.fingerprint,
                )
            )
            holder = result.first()
            etype = getattr(evidence.evidence_type, "value", evidence.evidence_type)
            if (
                holder is not None
                and investigation_id is not None
                and holder[0] != investigation_id
                and str(etype) in REFERENCE_LIFECYCLE_TYPES
            ):
                await self._record_interest(session, tenant_id, investigation_id, holder[1])
                return "shared"
            return "deduplicated"
        except Exception as exc:
            logger.warning("conflict resolution failed open as deduplicated: %s", exc)
            return "deduplicated"

    async def save(
        self, tenant_id: str, evidence: Evidence, investigation_id: UUID | None = None
    ) -> str:
        """Persist one row. Returns inserted|deduplicated|shared.

        G6: on fingerprint/key conflict with ANOTHER investigation's row of a
        reference type, record bank-interest instead of dropping — the caller
        is now a reader of the canonical row. Same-investigation conflicts
        stay deduplicated (current behavior preserved).
        """
        try:
            async with rls_session(self._session_factory, tenant_id) as session:
                # Atomic upsert: a bare ON CONFLICT DO NOTHING covers every
                # unique constraint (tenant+key AND tenant+fingerprint) in one
                # statement. The pre-check-then-insert this replaces raced when
                # parallel fetches recorded the same deterministic evidence_id
                # (uuid5 over index+doc), killing runs with
                # uq_evidence_tenant_key violations (WORKFLOW_ERROR, 2026-10-16).
                stmt = insert(EvidenceORM).values(
                    **self._values(evidence, tenant_id, investigation_id)
                )
                stmt = stmt.on_conflict_do_nothing()
                result = await session.execute(stmt)
                if result.rowcount:
                    outcome = "inserted"
                else:
                    outcome = await self._resolve_conflict(
                        session, tenant_id, investigation_id, evidence
                    )
        except Exception as exc:
            logger.exception(
                "evidence save failed",
                extra=sanitize_extra(
                    {
                        "tenant_id": tenant_id,
                        "investigation_id": str(investigation_id),
                        "event": "evidence.save",
                        "outcome": "failed",
                        "evidence_id": str(evidence.evidence_id),
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    }
                ),
            )
            raise
        logger.info(
            "evidence saved",
            extra=sanitize_extra(
                {
                    "tenant_id": tenant_id,
                    "investigation_id": str(investigation_id),
                    "event": "evidence.save",
                    "outcome": outcome,
                    "evidence_id": str(evidence.evidence_id),
                    "fingerprint": evidence.fingerprint,
                }
            ),
        )
        return outcome

    async def save_batch(
        self, tenant_id: str, evidence_list: list[Evidence], investigation_id: UUID | None = None
    ) -> dict[str, int]:
        # TO_DO-015: constant-query-count batch — bulk INSERT plus a bounded
        # set of constant lookups (pre/post holder maps + one interest
        # insert), regardless of batch size. The bare conflict target covers
        # tenant+key as well as tenant+fingerprint, so parallel duplicate
        # fetches converge instead of violating uq_evidence_tenant_key.
        # G6: collided reference rows held elsewhere record bank-interest
        # (shared) instead of vanishing; same-holder collisions stay
        # deduplicated.
        unique: dict[str, Evidence] = {}
        for evidence in evidence_list:
            unique.setdefault(evidence.fingerprint, evidence)
        batch_size = len(evidence_list)
        if not unique:
            logger.info(
                "evidence batch saved",
                extra=sanitize_extra(
                    {
                        "tenant_id": tenant_id,
                        "investigation_id": str(investigation_id),
                        "event": "evidence.save_batch",
                        "outcome": "inserted",
                        "batch_size": batch_size,
                        "unique_count": 0,
                        "inserted_count": 0,
                    }
                ),
            )
            return {"inserted": 0, "shared": 0, "deduplicated": 0}
        try:
            async with rls_session(self._session_factory, tenant_id) as session:
                stmt = insert(EvidenceORM).values(
                    [self._values(e, tenant_id, investigation_id) for e in unique.values()]
                )
                stmt = stmt.on_conflict_do_nothing()
                result = await session.execute(stmt)
                inserted_count = max(result.rowcount or 0, 0)
                # Post-map only (no pre-map): every fingerprint in the batch
                # resolves to a holder row after our insert — ours (fresh or
                # same-investigation re-save) or another investigation's
                # (shared reference or observational duplicate). This is exact
                # even under same-tick parallel inserts, in one extra query.
                holders = await self._holder_map(session, tenant_id, list(unique))
                shared = 0
                interests: list[dict[str, Any]] = []
                for fingerprint, evidence in unique.items():
                    holder = holders.get(fingerprint)
                    if holder is None:
                        continue  # defensive; counted via rowcount
                    holder_inv, holder_key = holder
                    if investigation_id is not None and holder_inv != investigation_id:
                        etype = getattr(
                            evidence.evidence_type, "value", evidence.evidence_type
                        )
                        if str(etype) in REFERENCE_LIFECYCLE_TYPES:
                            shared += 1
                            interests.append(
                                {
                                    "tenant_id": tenant_id,
                                    "investigation_id": investigation_id,
                                    "evidence_key": holder_key,
                                }
                            )
                if interests:
                    istmt = (
                        insert(EvidenceBankInterestORM)
                        .values(interests)
                        .on_conflict_do_nothing()
                    )
                    await session.execute(istmt)
                await session.commit()
        except Exception as exc:
            logger.exception(
                "evidence batch save failed",
                extra=sanitize_extra(
                    {
                        "tenant_id": tenant_id,
                        "investigation_id": str(investigation_id),
                        "event": "evidence.save_batch",
                        "outcome": "failed",
                        "batch_size": batch_size,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    }
                ),
            )
            raise
        # inserted from rowcount; shared/deduplicated from the holder map.
        # (Rows we inserted ourselves appear held-by-us → deduplicated bucket
        # only when re-seen; first write counts via rowcount.)
        shared_only = max(shared, 0)
        logger.info(
            "evidence batch saved",
            extra=sanitize_extra(
                {
                    "tenant_id": tenant_id,
                    "investigation_id": str(investigation_id),
                    "event": "evidence.save_batch",
                    "outcome": "inserted",
                    "batch_size": batch_size,
                    "unique_count": len(unique),
                    "inserted_count": inserted_count,
                    "shared_count": shared_only,
                }
            ),
        )
        return {
            "inserted": inserted_count,
            "shared": shared_only,
            "deduplicated": max(len(unique) - inserted_count - shared_only, 0),
        }

    @staticmethod
    async def _holder_map(
        session: Any, tenant_id: str, fingerprints: list[str]
    ) -> dict[str, tuple[Any, str]]:
        """fingerprint → (holder investigation_id, evidence_key), one query."""
        if not fingerprints:
            return {}
        rows = await session.execute(
            select(
                EvidenceORM.fingerprint,
                EvidenceORM.investigation_id,
                EvidenceORM.evidence_key,
            ).where(
                EvidenceORM.tenant_id == tenant_id,
                EvidenceORM.fingerprint.in_(fingerprints),
            )
        )
        return {row[0]: (row[1], row[2]) for row in rows.all()}

    async def get_by_id(self, tenant_id: str, evidence_id: UUID) -> Evidence | None:
        async with rls_session(self._session_factory, tenant_id) as session:
            result = await session.scalars(
                select(EvidenceORM).where(
                    EvidenceORM.tenant_id == tenant_id,
                    EvidenceORM.evidence_key == str(evidence_id),
                )
            )
            row = result.first()
            return self._from_orm(row) if row else None

    async def get_by_ids(self, tenant_id: str, evidence_ids: list[UUID]) -> list[Evidence]:
        async with rls_session(self._session_factory, tenant_id) as session:
            result = await session.scalars(
                select(EvidenceORM).where(
                    EvidenceORM.tenant_id == tenant_id,
                    EvidenceORM.evidence_key.in_([str(e) for e in evidence_ids]),
                )
            )
            return [self._from_orm(row) for row in result.all()]

    async def find_by_investigation_id(
        self, tenant_id: str, investigation_id: UUID
    ) -> list[Evidence]:
        """Owned rows plus shared reference rows (bank-interest UNION).

        G6: a run sees canonical code rows banked by earlier investigations
        in the same tenant — never starved by tenant-wide dedup. Interest
        rows are tenant-matched at insert; the join re-checks tenant.
        """
        async with rls_session(self._session_factory, tenant_id) as session:
            owned = await session.scalars(
                select(EvidenceORM).where(
                    EvidenceORM.tenant_id == tenant_id,
                    EvidenceORM.investigation_id == investigation_id,
                )
            )
            items = list(owned.all())
            seen_keys = {row.evidence_key for row in items}
            interest_keys = await session.scalars(
                select(EvidenceBankInterestORM.evidence_key).where(
                    EvidenceBankInterestORM.tenant_id == tenant_id,
                    EvidenceBankInterestORM.investigation_id == investigation_id,
                )
            )
            shared_keys = [key for key in interest_keys.all() if key not in seen_keys]
            if shared_keys:
                shared = await session.scalars(
                    select(EvidenceORM).where(
                        EvidenceORM.tenant_id == tenant_id,
                        EvidenceORM.evidence_key.in_(shared_keys),
                    )
                )
                items.extend(shared.all())
            return [self._from_orm(row) for row in items]

    async def sharing_info(
        self, tenant_id: str, evidence_keys: list[str]
    ) -> dict[str, dict[str, Any]]:
        """Which investigations banked interest in these rows (visibility).

        Returns key → {shared: bool, investigations: [uuid-str]}. Powers the
        `shared` marker on API evidence views.
        """
        if not evidence_keys:
            return {}
        async with rls_session(self._session_factory, tenant_id) as session:
            rows = await session.execute(
                select(
                    EvidenceBankInterestORM.evidence_key,
                    EvidenceBankInterestORM.investigation_id,
                ).where(
                    EvidenceBankInterestORM.tenant_id == tenant_id,
                    EvidenceBankInterestORM.evidence_key.in_(evidence_keys),
                )
            )
            info: dict[str, dict[str, Any]] = {}
            for key, inv_id in rows.all():
                entry = info.setdefault(key, {"shared": True, "investigations": []})
                entry["investigations"].append(str(inv_id))
            return info

    async def purge_expired(self, tenant_id: str, cutoff: datetime, limit: int = 500) -> int:
        """Hard-delete orphan evidence rows (NULL investigation) at/before cutoff.

        Rows attached to an investigation are never purged standalone — they
        cascade with their investigation's retention purge.
        """
        try:
            async with rls_session(self._session_factory, tenant_id) as session:
                ids = (
                    await session.scalars(
                        select(EvidenceORM.id)
                        .where(
                            EvidenceORM.tenant_id == tenant_id,
                            EvidenceORM.investigation_id.is_(None),
                            EvidenceORM.created_at <= cutoff,
                        )
                        .order_by(EvidenceORM.created_at)
                        .limit(max(1, min(limit, 5000)))
                    )
                ).all()
                if not ids:
                    purged = 0
                else:
                    result = await session.execute(
                        delete(EvidenceORM).where(
                            EvidenceORM.tenant_id == tenant_id,
                            EvidenceORM.id.in_(ids),
                        )
                    )
                    purged = int(result.rowcount or 0)
        except Exception as exc:
            logger.exception(
                "evidence purge failed",
                extra=sanitize_extra(
                    {
                        "tenant_id": tenant_id,
                        "event": "evidence.purge_expired",
                        "outcome": "failed",
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    }
                ),
            )
            raise
        logger.info(
            "evidence purge completed",
            extra=sanitize_extra(
                {
                    "tenant_id": tenant_id,
                    "event": "evidence.purge_expired",
                    "outcome": "purged",
                    "purged_count": purged,
                }
            ),
        )
        return purged
