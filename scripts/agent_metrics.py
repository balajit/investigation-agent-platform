#!/usr/bin/env python3
"""Agent-loop measurements: forced-rate, readiness calibration, auto-claim rate.

Phase-A/H-measurement item (measurement before Phase 2). Reads the platform
Postgres directly (same pattern as scripts/scoreboard.py — SQLAlchemy model
classes for table/column names, never hand-guessed schema). Parses the
persisted per-turn trace one-liners (`reason: action=... readiness=...
forced=[...] outcome=[...]`, written by the workflow trace formatter) plus
hypothesis/conclusion rows. No schema change, no new writes.

Usage:
  set -a && source .env.local && source ~/bin/.iap.env.local && set +a
  uv run python scripts/agent_metrics.py [--tenant ID] [--days N] [--app ID]

Interpreting the output:
  - forced_rate: fraction of reason turns whose action was workflow-forced.
    High sustained rate => the workflow routes around the agent; tune
    prompts/forcing before touching the reasoner.
  - readiness calibration: max per-run readiness vs conclusion status.
    Runs with max readiness >= 0.8 but no ROOT_CAUSE_ESTABLISHED conclusion
    are the overconfident set the (unbuilt) critic would overturn.
  - unbound_hypothesis_rate: hypotheses with no assessments and no supporting
    IDs. Terminal unbound rows (SUPPORTED/VERIFIED/...) indicate claims that
    verified without bindings — the auto-CAUSAL fallback footprint.
  - stale_score_rate: hypotheses still on 0.5/0.5 support/confidence.
    Should fall as VERIFY verdict persistence (§62) takes effect on new runs.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from datetime import UTC, datetime, timedelta

_REPO_ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

_REASON_MARKER = "reason: action="
_FORCED_RE = re.compile(r"forced=\[([^\]]*)\]")
_READINESS_RE = re.compile(r"readiness=([0-9]*\.?[0-9]+|\?)")
_ACTION_RE = re.compile(r"reason: action=(\S+)")


def parse_reason_trace(description: str) -> dict | None:
    """Parse one persisted per-turn trace one-liner.

    The trace rides inside STATE_CHANGE descriptions
    ("... Iteration 3 verifying (reason: action=... readiness=... ...)"),
    so the marker is searched anywhere in the row, not just at the start.
    Returns action/readiness/forced scalars, or None when the row carries
    no reason trace. Pure and deterministic (unit-tested).
    """
    if not isinstance(description, str) or _REASON_MARKER not in description:
        return None
    action_match = _ACTION_RE.search(description)
    readiness: float | None = None
    readiness_match = _READINESS_RE.search(description)
    if readiness_match and readiness_match.group(1) != "?":
        try:
            readiness = float(readiness_match.group(1))
        except ValueError:
            readiness = None
    forced: list[str] = []
    forced_match = _FORCED_RE.search(description)
    if forced_match and forced_match.group(1).strip():
        forced = [b.strip() for b in forced_match.group(1).split(",") if b.strip()]
    return {
        "action": action_match.group(1) if action_match else "?",
        "readiness": readiness,
        "forced": forced,
    }


def is_auto_claim_shape(assessments: list[dict]) -> bool:
    """True when a hypothesis row carries no bound evidence at all.

    Auto-typed DecisionClaims (unbound CAUSAL) produce no assessment rows —
    assessments are created one per bound evidence ID — so a row with zero
    assessments and zero supporting IDs is the persisted footprint of an
    unbound claim. Documented proxy, not a proof of auto-typing.
    """
    return not assessments


def _uri() -> str:
    uri = os.environ.get("IAP_DATABASE_URI", "")
    return uri.replace("postgresql+psycopg2://", "postgresql+asyncpg://").replace(
        "postgresql://", "postgresql+asyncpg://"
    )


async def main() -> int:
    ap = argparse.ArgumentParser(description="IAP agent-loop measurements.")
    ap.add_argument("--tenant", default="n-a")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--app", default="")
    args = ap.parse_args()

    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from sqlalchemy.orm import sessionmaker

    from investigation_agent_platform.infrastructure.persistence import models
    from investigation_agent_platform.infrastructure.persistence.models import (
        InvestigationORM,
        TimelineEventORM,
    )

    hypothesis_orm = None
    conclusion_orm = None
    for name in dir(models):
        obj = getattr(models, name)
        if not isinstance(obj, type):
            continue
        if getattr(obj, "__tablename__", "") == "hypotheses":
            hypothesis_orm = obj
        elif getattr(obj, "__tablename__", "") == "investigation_conclusions":
            conclusion_orm = obj

    eng = create_async_engine(_uri())
    try:
        maker = sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)
        since = datetime.now(UTC) - timedelta(days=args.days)
        async with maker() as session:
            inv_stmt = select(InvestigationORM).where(
                InvestigationORM.tenant_id == args.tenant,
                InvestigationORM.created_at >= since,
            )
            if args.app:
                inv_stmt = inv_stmt.where(InvestigationORM.application_id == args.app)
            investigations = list((await session.execute(inv_stmt)).scalars().all())
            inv_ids = [inv.id for inv in investigations]

            tl_stmt = (
                select(TimelineEventORM)
                .where(TimelineEventORM.tenant_id == args.tenant)
                .where(TimelineEventORM.timestamp >= since)
            )
            if inv_ids:
                tl_stmt = tl_stmt.where(TimelineEventORM.investigation_id.in_(inv_ids))
            timelines = list((await session.execute(tl_stmt)).scalars().all())

            hyp_rows: list = []
            if hypothesis_orm is not None and inv_ids:
                hyp_stmt = select(hypothesis_orm).where(
                    hypothesis_orm.tenant_id == args.tenant,
                    hypothesis_orm.investigation_id.in_(inv_ids),
                )
                hyp_rows = list((await session.execute(hyp_stmt)).scalars().all())

            concl_by_inv: dict = {}
            if conclusion_orm is not None and inv_ids:
                concl_stmt = select(conclusion_orm).where(
                    conclusion_orm.tenant_id == args.tenant,
                    conclusion_orm.investigation_id.in_(inv_ids),
                )
                for row in (await session.execute(concl_stmt)).scalars().all():
                    concl_by_inv[str(row.investigation_id)] = row
    finally:
        await eng.dispose()

    # --- forced-rate + readiness calibration from reason traces ---
    traces_by_inv: dict[str, list[dict]] = {}
    for event in timelines:
        parsed = parse_reason_trace(getattr(event, "description", "") or "")
        if parsed is None:
            continue
        traces_by_inv.setdefault(str(event.investigation_id), []).append(parsed)

    forced_turns = sum(1 for traces in traces_by_inv.values() for t in traces if t["forced"])
    total_turns = sum(len(traces) for traces in traces_by_inv.values())

    overconfident_runs = 0
    established_runs = 0
    max_readiness_by_inv: dict[str, float] = {}
    for inv_id, traces in traces_by_inv.items():
        readings = [t["readiness"] for t in traces if t["readiness"] is not None]
        peak = max(readings) if readings else 0.0
        max_readiness_by_inv[inv_id] = peak
        concl = concl_by_inv.get(inv_id)
        established = (
            concl is not None
            and str(getattr(concl, "status", "")) == "ROOT_CAUSE_ESTABLISHED"
        )
        if established:
            established_runs += 1
        elif peak >= 0.8:
            overconfident_runs += 1

    # --- auto-claim + stale-score rates from hypothesis rows ---
    unbound = 0
    unbound_terminal = 0
    stale_scores = 0
    terminal_states = {"SUPPORTED", "VERIFIED", "CONTRADICTED", "REJECTED"}
    for hyp in hyp_rows:
        assessments = list(getattr(hyp, "assessments_json", None) or [])
        supporting = list(getattr(hyp, "supporting_evidence_ids", None) or [])
        status = str(getattr(hyp, "status", "") or "")
        if is_auto_claim_shape(assessments) and not supporting:
            unbound += 1
            if status in terminal_states:
                unbound_terminal += 1
        if float(getattr(hyp, "support_score", 0.5) or 0.0) == 0.5 and float(
            getattr(hyp, "confidence_score", 0.5) or 0.0
        ) == 0.5:
            stale_scores += 1

    n_hyp = len(hyp_rows)
    print("## Agent-loop measurements")
    print(f"investigations: {len(investigations)} (with reason traces: {len(traces_by_inv)})")
    print(f"reason turns: {total_turns}, forced turns: {forced_turns}", end="")
    print(f" => forced_rate={forced_turns / total_turns:.2f}" if total_turns else " => forced_rate=n/a")
    print(f"established conclusions: {established_runs}, overconfident runs (peak>=0.8, not established): {overconfident_runs}")
    print(f"hypotheses: {n_hyp}, unbound: {unbound}", end="")
    print(f" => unbound_rate={unbound / n_hyp:.2f}" if n_hyp else " => unbound_rate=n/a")
    print(f"terminal unbound: {unbound_terminal}, stale 0.5/0.5 scores: {stale_scores}", end="")
    print(f" => stale_score_rate={stale_scores / n_hyp:.2f}" if n_hyp else "")
    print("caveat: unbound is a proxy (zero assessments + zero supporting IDs);")
    print("caveat: pre-§62 rows predate VERIFY verdict persistence, expect stale scores on old runs.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
