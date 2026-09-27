# src/investigation_agent_platform/infrastructure/evidence/logs/elastic_bridge.py
import logging
from typing import Any
from uuid import UUID

import sqlglot
import sqlglot.expressions as exp

from investigation_agent_platform.domain.evidence.requests import (
    RuntimeEvidenceRequest,
    TimeRange,
)
from investigation_agent_platform.domain.profile.models import ObservabilityProfile
from investigation_agent_platform.infrastructure.evidence.runtime.elastic import (
    AsyncElasticAdapter,
)

logger = logging.getLogger(__name__)


class ElasticIncidentBridge:
    """Bridge service that extracts code symbols and database lineage
    from runtime Evidence collected by AsyncElasticAdapter."""

    def __init__(self, elastic_adapter: AsyncElasticAdapter) -> None:
        self._adapter = elastic_adapter

    async def extract_runtime_telemetry_for_trace(
        self,
        tenant_id: str,
        investigation_id: UUID,
        trace_id: str,
        profile: ObservabilityProfile,
        environment: str = "production",
        time_range: TimeRange | None = None,
    ) -> dict[str, Any]:
        """Queries Elastic via AsyncElasticAdapter and extracts call stacks + SQL lineage."""

        # 1. Delegate query execution to AsyncElasticAdapter (reusing limits & security policies)
        request = RuntimeEvidenceRequest(
            environment=environment,
            identifiers={"trace_id": trace_id},
            time_range=time_range,
            limit=100,
        )

        result = await self._adapter.search_runtime_evidence(
            tenant_id=tenant_id,
            investigation_id=investigation_id,
            request=request,
            profile=profile,
        )

        telemetry: dict[str, Any] = {
            "trace_id": trace_id,
            "code_locations": [],
            "accessed_tables": set(),
            "raw_sql_statements": [],
        }

        # 2. Extract structural nodes from returned Evidence models
        for item in result.items:
            attrs = item.attributes or {}

            # A. Extract code location from Elastic Common Schema (ECS)
            code_info = attrs.get("code") or attrs.get("log", {}).get("origin", {})
            if isinstance(code_info, dict):
                func = code_info.get("function") or code_info.get("log.origin.function")
                file_path = code_info.get("filepath") or code_info.get("file", {}).get("name")
                line = code_info.get("lineno") or code_info.get("file", {}).get("line")

                if func or file_path:
                    telemetry["code_locations"].append(
                        {
                            "function": func,
                            "file_path": file_path,
                            "line": line,
                        }
                    )

            # B. Extract and parse executed SQL queries using SQLGlot
            db_info = attrs.get("db") or {}
            sql_statement = db_info.get("statement") if isinstance(db_info, dict) else None

            if sql_statement:
                telemetry["raw_sql_statements"].append(sql_statement)
                try:
                    parsed = sqlglot.parse_one(sql_statement)
                    for table in parsed.find_all(exp.Table):
                        if table.name:
                            telemetry["accessed_tables"].add(table.name.lower())
                except Exception as exc:
                    logger.debug("Failed to parse runtime SQL log: %s", exc)

        telemetry["accessed_tables"] = list(telemetry["accessed_tables"])
        return telemetry
