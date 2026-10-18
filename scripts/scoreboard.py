#!/usr/bin/env python3
"""Self-triage scoreboard: runs, outcomes, evidence means (Tier 2 item 5).

Reads the platform Postgres directly (SQLAlchemy models for table/column
names — never hand-guessed schema). Prints Markdown for pasting into the
architecture doc's scoreboard section.

Usage:
  set -a && source .env.local && source ~/bin/.iap.env.local && set +a
  uv run python scripts/scoreboard.py [--tenant ID] [--days N] [--app ID]
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta

_REPO_ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))


def _uri() -> str:
    uri = os.environ.get("IAP_DATABASE_URI", "")
    return uri.replace("postgresql+psycopg2://", "postgresql+asyncpg://").replace(
        "postgresql://", "postgresql+asyncpg://"
    )


async def main() -> int:
    ap = argparse.ArgumentParser(description="IAP self-triage scoreboard.")
    ap.add_argument("--tenant", default="n-a")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--app", default="")
    args = ap.parse_args()

    from sqlalchemy import func, select
    from sqlalchemy.ext.asyncio import create_async_engine

    # Resolve the conclusions ORM by table name (model class name may vary).
    from investigation_agent_platform.infrastructure.persistence import models
    from investigation_agent_platform.infrastructure.persistence.models import (
        EvidenceORM,
        InvestigationORM,
    )

    conclusion_orm = None
    for name in dir(models):
        obj = getattr(models, name)
        if (
            isinstance(obj, type)
            and getattr(obj, "__tablename__", "") == "investigation_conclusions"
        ):
            conclusion_orm = obj
            break

    eng = create_async_engine(_uri())
    try:
        from sqlalchemy.ext.asyncio import AsyncSession
        from sqlalchemy.orm import sessionmaker

        maker = sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)
        since = datetime.now(UTC) - timedelta(days=args.days)
        async with maker() as session:
            stmt = (
                select(
                    InvestigationORM.application_id,
                    InvestigationORM.status,
                    func.count(InvestigationORM.id),
                )
                .where(InvestigationORM.tenant_id == args.tenant)
                .where(InvestigationORM.created_at >= since)
            )
            if args.app:
                stmt = stmt.where(InvestigationORM.application_id == args.app)
            rows = (await session.execute(stmt.group_by(
                InvestigationORM.application_id, InvestigationORM.status
            ))).fetchall()
            ev_stmt = (
                select(
                    InvestigationORM.application_id,
                    func.count(EvidenceORM.id),
                )
                .join(EvidenceORM, EvidenceORM.investigation_id == InvestigationORM.id)
                .where(InvestigationORM.tenant_id == args.tenant)
                .where(InvestigationORM.created_at >= since)
            )
            if args.app:
                ev_stmt = ev_stmt.where(InvestigationORM.application_id == args.app)
            ev_rows = (await session.execute(ev_stmt.group_by(
                InvestigationORM.application_id
            ))).fetchall()
            concl_rows = []
            if conclusion_orm is not None:
                concl_stmt = (
                    select(
                        InvestigationORM.application_id,
                        conclusion_orm.status,
                        func.count(conclusion_orm.id),
                    )
                    .join(
                        conclusion_orm,
                        conclusion_orm.investigation_id == InvestigationORM.id,
                    )
                    .where(InvestigationORM.tenant_id == args.tenant)
                    .where(InvestigationORM.created_at >= since)
                )
                if args.app:
                    concl_stmt = concl_stmt.where(
                        InvestigationORM.application_id == args.app
                    )
                concl_rows = (await session.execute(concl_stmt.group_by(
                    InvestigationORM.application_id, conclusion_orm.status
                ))).fetchall()
    finally:
        await eng.dispose()

    by_app: dict[str, Counter] = {}
    for app_id, status, count in rows:
        by_app.setdefault(app_id, Counter())[f"run:{status}"] += count
    for app_id, count in ev_rows:
        by_app.setdefault(app_id, Counter())["evidence_rows"] += count
    for app_id, status, count in concl_rows:
        by_app.setdefault(app_id, Counter())[f"conclusion:{status}"] += count

    print(f"# IAP self-triage scoreboard (tenant {args.tenant}, last {args.days}d)")
    print()
    print("| app | runs | by status | conclusions | mean evidence/run |")
    print("| --- | --- | --- | --- | --- |")
    for app_id in sorted(by_app):
        counts = by_app[app_id]
        runs = sum(v for k, v in counts.items() if k.startswith("run:"))
        statuses = ", ".join(
            f"{k[4:]}={v}" for k, v in sorted(counts.items()) if k.startswith("run:")
        )
        conclusions = ", ".join(
            f"{k[11:]}={v}" for k, v in sorted(counts.items()) if k.startswith("conclusion:")
        ) or "-"
        mean_ev = (counts.get("evidence_rows", 0) / runs) if runs else 0
        print(f"| {app_id} | {runs} | {statuses} | {conclusions} | {mean_ev:.1f} |")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
