# src/investigation_agent_platform/infrastructure/evidence/state/oracle.py
import asyncio
import hashlib
import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import sqlglot
import sqlglot.expressions as exp
from opentelemetry import trace
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from investigation_agent_platform.domain.common.exceptions import (
    ExecutionError,
    SecurityPolicyViolationException,
)
from investigation_agent_platform.domain.evidence.models import (
    ClassificationLevel,
    Evidence,
    EvidenceType,
)
from investigation_agent_platform.domain.evidence.requests import ApplicationStateRequest
from investigation_agent_platform.domain.profile.models import StateProfile
from investigation_agent_platform.domain.provenance.models import (
    EvidenceFreshness,
    EvidenceProvenance,
    QueryFingerprint,
    SourceLocation,
)
from investigation_agent_platform.ports.evidence.gateway import EvidenceQueryResult

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)

# F-044: provider-side bounds independent of the caller. Template allow-list
# validation (SqlglotTemplateValidator) is necessary but not sufficient —
# production safety additionally requires a hard row ceiling, a database-level
# statement timeout, and least-privilege execution.
ORACLE_MAX_ROWS = 200
ORACLE_STATEMENT_TIMEOUT_SECONDS = 25


class SqlglotTemplateValidator:
    """Validates pre-defined SQL templates using sqlglot AST parsing for SELECT-only bounds."""

    def __init__(self, allowed_tables: set[str]) -> None:
        self._allowed_tables = {t.lower() for t in allowed_tables}

    def validate_and_bound_template(self, sql_template: str, max_rows: int) -> str:
        try:
            parsed = sqlglot.parse_one(sql_template, read="oracle")
        except Exception as exc:
            raise SecurityPolicyViolationException(
                f"Invalid SQL syntax in template: {exc}"
            ) from exc

        if not isinstance(parsed, exp.Select):
            raise SecurityPolicyViolationException(
                "State queries must strictly be SELECT statements."
            )

        tables = {t.name.lower() for t in parsed.find_all(exp.Table) if t.name}
        if self._allowed_tables and not tables.issubset(self._allowed_tables):
            unauthorized = tables - self._allowed_tables
            raise SecurityPolicyViolationException(
                f"Template accesses unauthorized tables: {unauthorized}"
            )

        return parsed.limit(max_rows).sql(dialect="oracle")


class AsyncOracleStateAdapter:
    """Asynchronous Oracle state evidence adapter executing safe parameterized query templates."""

    def __init__(
        self,
        engine: AsyncEngine,
        template_repository: dict[str, str],
        validator: SqlglotTemplateValidator,
        provider_id: str = "oracle-primary",
        query_timeout: float = 30.0,
    ) -> None:
        self._engine = engine
        self._templates = template_repository
        self._validator = validator
        self._provider_id = provider_id
        self._query_timeout = query_timeout

    async def get_application_state(
        self,
        tenant_id: str,
        investigation_id: UUID,
        request: ApplicationStateRequest,
        profile: StateProfile,
        correlation_id: str | None = None,
    ) -> EvidenceQueryResult:
        with tracer.start_as_current_span("AsyncOracleStateAdapter.get_application_state") as span:
            span.set_attribute("tenant_id", tenant_id)
            span.set_attribute("template_id", request.template_id)
            if correlation_id:
                span.set_attribute("correlation_id", correlation_id)
            span.set_attribute("investigation_id", str(investigation_id))

            if request.template_id not in self._templates:
                raise SecurityPolicyViolationException(
                    f"Unregistered state template ID: {request.template_id}"
                )

            # F-044: clamp the caller-requested row limit at the provider ceiling.
            effective_limit = min(request.limit, ORACLE_MAX_ROWS)
            raw_template = self._templates[request.template_id]
            bounded_sql = self._validator.validate_and_bound_template(
                raw_template, max_rows=effective_limit
            )

            params = {**request.parameters, "tenant_id": tenant_id}

            try:
                async with self._engine.connect() as conn:
                    # Database-level statement timeout as the backstop behind
                    # the asyncio wait below — a runaway template cannot hold
                    # a pooled connection indefinitely.
                    try:
                        await conn.execute(
                            text(
                                f"SET LOCAL statement_timeout = '{ORACLE_STATEMENT_TIMEOUT_SECONDS}s'"
                            )
                        )
                    except Exception:
                        pass  # non-Postgres backends (e.g. Oracle) may not support SET LOCAL
                    result = await asyncio.wait_for(
                        conn.execute(text(bounded_sql), params),
                        timeout=self._query_timeout,
                    )
                    rows = [dict(r) for r in result.mappings().all()][:effective_limit]
            except TimeoutError as exc:
                logger.error(
                    "Database query timed out",
                    exc_info=exc,
                    extra={
                        "context": {
                            "template_id": request.template_id,
                            "tenant_id": tenant_id,
                            "correlation_id": correlation_id,
                        }
                    },
                )
                raise ExecutionError(
                    f"Database query timed out after {self._query_timeout}s"
                ) from exc
            except Exception as exc:
                logger.error(
                    "Database query execution error",
                    exc_info=exc,
                    extra={
                        "context": {
                            "template_id": request.template_id,
                            "tenant_id": tenant_id,
                            "correlation_id": correlation_id,
                        }
                    },
                )
                raise ExecutionError(f"Database execution failed: {exc}") from exc

            items = [
                self._map_row_to_evidence(row, request.template_id, tenant_id, investigation_id)
                for row in rows
            ]
            return EvidenceQueryResult(items=items, total_count=len(items))

    async def search_application_state(
        self,
        tenant_id: str,
        investigation_id: UUID,
        request: ApplicationStateRequest,
        profile: StateProfile,
        correlation_id: str | None = None,
    ) -> EvidenceQueryResult:
        return await self.get_application_state(
            tenant_id, investigation_id, request, profile, correlation_id=correlation_id
        )

    def _map_row_to_evidence(
        self, row: dict[str, Any], template_id: str, tenant_id: str, investigation_id: UUID
    ) -> Evidence:
        now = datetime.now(UTC)
        serialized_row = json.dumps(row, sort_keys=True, default=str)
        row_hash = hashlib.sha256(
            f"{tenant_id}:{template_id}:{serialized_row}".encode()
        ).hexdigest()
        row_id = str(row.get("id", row_hash[:16]))

        prov = EvidenceProvenance(
            tenant_id=tenant_id,
            investigation_id=investigation_id,
            provider_type="ORACLE",
            requested_provider_id=self._provider_id,
            actual_provider_id=self._provider_id,
            source_system="OracleDB",
            retrieval_timestamp=now,
            query_fingerprint=QueryFingerprint(
                provider_type="ORACLE", operation="TEMPLATE", normalized_query_hash=template_id
            ),
            source_location=SourceLocation(system="OracleDB", identifier=template_id),
        )
        fresh = EvidenceFreshness(observed_at=now, retrieved_at=now)

        return Evidence(
            tenant_id=tenant_id,
            investigation_id=prov.investigation_id,
            evidence_id=uuid.uuid5(uuid.NAMESPACE_DNS, f"oracle:{template_id}:{row_id}"),
            evidence_type=EvidenceType.DATABASE_STATE,
            provider="ORACLE",
            source=f"oracle://{template_id}/{row_id}",
            title=f"State Row [{template_id}]",
            summary=f"Database state snapshot for template {template_id}",
            content_snippet=json.dumps(row, sort_keys=True, default=str)[:4000],
            content_uri=f"oracle://{template_id}/{row_id}",
            fingerprint=row_hash,
            classification=ClassificationLevel.INTERNAL,
            observed_at=now,
            retrieved_at=now,
            attributes={**row, "tenant_id": tenant_id},
            provenance=prov,
            freshness=fresh,
        )
