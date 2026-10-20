#!/usr/bin/env python3
"""Standalone battery cleanup: delete investigation rows for a tenant+app.

Dry-run is the default: prints per-table doomed-row counts and exits
without deleting. Pass --execute to actually delete. Children cascade
from investigations via ON DELETE CASCADE FKs, so one DELETE covers
evidence, hypotheses, findings, conclusions, sessions, and the rest.

Untagged rows are deleted only for the documented battery tenant, or
with --force-cleanup anywhere else — tagged (live-battery-*) sessions
are always safe to delete. live_battery.py imports cleanup_scope()
instead of carrying its own copy.

Usage:
  uv run python scripts/clean_battery.py [--tenant ID] [--app-id ID]
      [--tag-prefix PREFIX] [--force-cleanup] [--execute]
"""

from __future__ import annotations

import argparse
import os
import re
import sys

DEFAULT_TENANT = "n-batt"
DEFAULT_APP_ID = "spring-sample-service"
DEFAULT_TAG_PREFIX = "live-battery"

# Tables counted for the dry-run audit (children of investigations).
CHILD_TABLES = [
    "evidence",
    "hypotheses",
    "findings",
    "investigation_conclusions",
    "investigation_sessions",
    "investigation_tool_executions",
    "investigation_contradictions",
    "investigation_transitions",
]


def decide_scope(tenant: str, force_cleanup: bool) -> str:
    """'all' (tenant+app rows) or 'tagged' (battery sessions only).

    Pure: unit-tested without a database. Untagged rows belong to
    whoever ran them — only the documented battery tenant is wiped
    wholesale without an explicit force flag.
    """
    if tenant == DEFAULT_TENANT or force_cleanup:
        return "all"
    return "tagged"


def db_dsn() -> str:
    uri = os.environ.get("IAP_DATABASE_URI", "")
    return re.sub(r"^postgresql\+asyncpg://", "postgresql://", uri)


async def count_scope(conn, tenant: str, app_id: str, tag_prefix: str, scope: str) -> dict:
    """Doomed-row counts per table (read-only audit for dry-run)."""
    counts: dict = {}
    if scope == "all":
        where = "tenant_id=$1 AND application_id=$2"
        args: tuple = (tenant, app_id)
    else:
        where = "tenant_id=$1 AND application_id=$2 AND session_id LIKE $3"
        args = (tenant, app_id, f"{tag_prefix}-%")
    for table in ["investigations", *CHILD_TABLES]:
        try:
            if table == "investigations":
                counts[table] = await conn.fetchval(
                    f"SELECT count(*) FROM {table} WHERE {where}", *args
                )
            else:
                counts[table] = await conn.fetchval(
                    f"SELECT count(*) FROM {table} WHERE investigation_id IN "
                    f"(SELECT id FROM investigations WHERE {where})",
                    *args,
                )
        except Exception:
            counts[table] = "unknown"
    return counts


async def cleanup_scope(
    dsn: str, tenant: str, app_id: str, tag_prefix: str, force_cleanup: bool
) -> int:
    """Delete prior battery-scope investigations. Returns rows removed."""
    import asyncpg

    scope = decide_scope(tenant, force_cleanup)
    conn = await asyncpg.connect(dsn)
    try:
        if scope == "all":
            res = await conn.execute(
                "DELETE FROM investigations WHERE tenant_id=$1 AND application_id=$2",
                tenant,
                app_id,
            )
        else:
            res = await conn.execute(
                "DELETE FROM investigations WHERE tenant_id=$1 AND application_id=$2"
                " AND session_id LIKE $3",
                tenant,
                app_id,
                f"{tag_prefix}-%",
            )
        match = re.search(r"DELETE (\d+)", res)
        return int(match.group(1)) if match else 0
    finally:
        await conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="Delete battery investigation rows.")
    ap.add_argument("--tenant", default=DEFAULT_TENANT)
    ap.add_argument("--app-id", default=DEFAULT_APP_ID)
    ap.add_argument("--tag-prefix", default=DEFAULT_TAG_PREFIX)
    ap.add_argument("--force-cleanup", action="store_true")
    ap.add_argument("--execute", action="store_true",
                    help="Actually delete. Without it: dry-run audit only.")
    args = ap.parse_args()

    import asyncio

    import asyncpg

    dsn = db_dsn()
    if not dsn:
        print("no IAP_DATABASE_URI in environment", flush=True)
        return 1
    scope = decide_scope(args.tenant, args.force_cleanup)

    async def _run():
        conn = await asyncpg.connect(dsn)
        try:
            return await count_scope(conn, args.tenant, args.app_id, args.tag_prefix, scope)
        finally:
            await conn.close()

    try:
        counts = asyncio.run(_run())
    except Exception as exc:
        print(f"cleanup audit failed: {type(exc).__name__}: {str(exc)[:160]}", flush=True)
        return 1
    print(f"scope={scope} tenant={args.tenant} app={args.app_id}", flush=True)
    for table, count in counts.items():
        print(f"  {table}: {count}", flush=True)
    if not args.execute:
        print("dry run: nothing deleted (pass --execute to delete)", flush=True)
        return 0

    async def _wipe():
        return await cleanup_scope(dsn, args.tenant, args.app_id, args.tag_prefix, args.force_cleanup)

    try:
        removed = asyncio.run(_wipe())
    except Exception as exc:
        print(f"cleanup failed: {type(exc).__name__}: {str(exc)[:160]}", flush=True)
        return 1
    print(f"deleted {removed} investigation row(s) (children cascaded)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
