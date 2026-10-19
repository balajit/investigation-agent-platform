# src/investigation_agent_platform/domain/analysis/db_extractor.py
"""DB hub extractor SQL builders + fingerprint (Phase 2).

Dialect-routed catalog queries. No DB I/O here — builders return SQL strings;
adapters execute via existing MCP/state paths. Fingerprint gates rebuilds.
"""

from __future__ import annotations

import hashlib
import json


def oracle_fk_sql(schema: str) -> str:
    s = schema.replace("'", "''")
    return (
        'SELECT c.constraint_name, cc.column_name, c.table_name child_table, '
        'cc2.table_name parent_table, cc2.column_name parent_column '
        f'FROM ALL_CONSTRAINTS c JOIN ALL_CONS_COLUMNS cc ON c.constraint_name = cc.constraint_name '
        f'JOIN ALL_CONS_COLUMNS cc2 ON c.r_constraint_name = cc2.constraint_name '
        f"WHERE c.constraint_type = 'R' AND c.owner = '{s}' "
        "ORDER BY c.constraint_name, cc.position"
    )


def oracle_table_rows_sql(schema: str) -> str:
    s = schema.replace("'", "''")
    return f"SELECT table_name, num_rows FROM ALL_TABLES WHERE owner = '{s}' ORDER BY table_name"


def postgres_fk_sql(schema: str) -> str:
    s = schema.replace("'", "''")
    return (
        "SELECT tc.constraint_name, kcu.column_name, tc.table_name child_table, "
        "ccu.table_name parent_table, ccu.column_name parent_column "
        "FROM information_schema.table_constraints tc "
        "JOIN information_schema.key_column_usage kcu ON tc.constraint_name = kcu.constraint_name "
        "JOIN information_schema.constraint_column_usage ccu ON tc.constraint_name = ccu.constraint_name "
        f"WHERE tc.constraint_type = 'FOREIGN KEY' AND tc.table_schema = '{s}' "
        "ORDER BY tc.constraint_name, kcu.ordinal_position"
    )


def postgres_table_rows_sql(schema: str) -> str:
    s = schema.replace("'", "''")
    return (
        "SELECT relname table_name, reltuples::bigint num_rows FROM pg_class "
        f"JOIN pg_namespace ON pg_namespace.oid = pg_class.relnamespace WHERE nspname = '{s}' "
        "AND relkind = 'r' ORDER BY relname"
    )


def extractor_fingerprint(table_counts: dict[str, int], constraint_hash: str) -> str:
    """Fingerprint (COUNT map + constraint hash) for rebuild gating."""
    canonical = json.dumps({"counts": dict(sorted(table_counts.items())), "constraints": constraint_hash}, sort_keys=True, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(canonical.encode()).hexdigest()}"


def verified_count_sql(dialect: str, table: str) -> str:
    """Verified COUNT(*) on hub candidates — NUM_ROWS is stats-based."""
    if dialect not in ("oracle", "postgres"):
        raise ValueError(f"unsupported dialect: {dialect!r}")
    t = table.replace('"', '""')
    return f'SELECT COUNT(*) cnt FROM "{t}"'
