# src/investigation_agent_platform/infrastructure/persistence/role_guard.py
"""Runtime role + RLS coverage inspection (review R11-D1)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from sqlalchemy import text


class RoleVerdict(StrEnum):
    OK = "ok"
    WARN = "warn"
    REFUSE = "refuse"


@dataclass(frozen=True)
class RoleReport:
    role: str
    superuser: bool
    bypass_rls: bool
    unprotected_tables: tuple[str, ...]


_ROLE_SQL = "SELECT rolname, rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"
_COVERAGE_SQL = """
SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relkind = 'r'
  AND (NOT c.relrowsecurity OR NOT c.relforcerowsecurity)
  AND EXISTS (SELECT 1 FROM information_schema.columns k
              WHERE k.table_schema = 'public' AND k.table_name = c.relname AND k.column_name = 'tenant_id')
ORDER BY 1
"""


async def inspect_runtime_role(engine: Any) -> RoleReport | None:
    try:
        async with engine.connect() as conn:
            role = (await conn.execute(text(_ROLE_SQL))).one()
            tables = tuple(r[0] for r in await conn.execute(text(_COVERAGE_SQL)))
        return RoleReport(str(role[0]), bool(role[1]), bool(role[2]), tables)
    except Exception:
        return None


def evaluate_role(report: RoleReport | None, mode: str) -> RoleVerdict:
    if mode == "off":
        return RoleVerdict.OK
    privileged = report is None or report.superuser or report.bypass_rls
    gaps = bool(report and report.unprotected_tables)
    if not privileged and not gaps:
        return RoleVerdict.OK
    return RoleVerdict.REFUSE if mode == "enforce" else RoleVerdict.WARN
