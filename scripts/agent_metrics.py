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


def format_repair_rate(applied: int, shadow: int, checkable_total: int) -> str:
    """Battery repair series (pure; Phase 3 design: applied repairs over
    checkable claims, numerator + denominator).

    Finding F: the denominator is checkable claim groups (binding rows),
    not applied+shadow records — the latter measures disposition mix, not
    repair frequency. Zero checkable claims yields n/a, never a division
    failure.
    """
    try:
        applied_n = max(0, int(applied or 0))
    except (TypeError, ValueError):
        applied_n = 0
    try:
        shadow_n = max(0, int(shadow or 0))
    except (TypeError, ValueError):
        shadow_n = 0
    try:
        checkable_n = max(0, int(checkable_total or 0))
    except (TypeError, ValueError):
        checkable_n = 0
    if checkable_n:
        return (
            f"repairs: applied={applied_n} shadow={shadow_n}"
            f" => repair_rate={applied_n / checkable_n:.2f}"
            f" ({applied_n}/{checkable_n} checkable)"
        )
    if applied_n + shadow_n:
        return (
            f"repairs: applied={applied_n} shadow={shadow_n}"
            " => repair_rate=n/a (no checkable claims in window)"
        )
    return "repairs: n/a (no repair records in window)"


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
            concl is not None and str(getattr(concl, "status", "")) == "ROOT_CAUSE_ESTABLISHED"
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
        if (
            float(getattr(hyp, "support_score", 0.5) or 0.0) == 0.5
            and float(getattr(hyp, "confidence_score", 0.5) or 0.0) == 0.5
        ):
            stale_scores += 1

    # --- Phase 1 (claim binding) rates from VERIFY_EVALUATION rows ---
    _BINDING_KINDS = {
        "DEFINITION",
        "CALL_EDGE",
        "BEHAVIOR",
        "ABSENCE",
        "HISTORY",
        "CAUSAL",
        "UNTYPED",
    }
    _binding_total = 0
    _binding_evaporated = 0
    _binding_unmapped = 0
    _binding_claim_local = 0
    _binding_by_kind: dict[str, dict[str, int]] = {}
    _history_outcomes: dict[str, int] = {}
    _verdict_by_kind: dict[str, dict[str, int]] = {}
    _subject_outcomes: dict[str, int] = {}
    _stored_ledgers = 0
    _stored_linked = 0
    # Phase 3 (claim binding): applied/shadow repair counts from the
    # persisted `repaired` dumps (numerator + denominator for repair_rate).
    _repair_applied = 0
    _repair_shadow = 0
    _repair_truncated_markers = 0
    # Phase 4 (claim binding): NOT_CHECKABLE denominator inputs from the
    # marker extras (exact counts; identities truncated per flag).
    _not_checkable_count = 0
    _not_checkable_truncated_markers = 0
    for event in timelines:
        try:
            _etype = getattr(event, "event_type", None)
            _tname = str(getattr(_etype, "value", _etype))
        except Exception:
            continue
        if _tname != "VERIFY_EVALUATION":
            continue
        try:
            _attrs = getattr(event, "attributes", None) or {}
            if not isinstance(_attrs, dict):
                continue
            for _b in list(_attrs.get("bindings") or []):
                if not isinstance(_b, dict):
                    continue
                _binding_total += 1
                try:
                    _key = str(_b.get("claim_key") or "")
                    _kind = _key.rsplit("|", 1)[-1].strip() if "|" in _key else "UNTYPED"
                except Exception:
                    _kind = "UNTYPED"
                if _kind not in _BINDING_KINDS:
                    _kind = "UNTYPED"
                _slot = _binding_by_kind.setdefault(
                    _kind, {"claim_local": 0, "bank_fallback": 0, "evaporated": 0, "unmapped": 0}
                )
                if list(_b.get("claim_local") or []):
                    _binding_claim_local += 1
                    _slot["claim_local"] += 1
                elif list(_b.get("bank_fallback") or []):
                    _slot["bank_fallback"] += 1
                elif list(_b.get("unknown") or []):
                    _binding_evaporated += 1
                    _slot["evaporated"] += 1
                elif bool(_b.get("unmapped", False)):
                    _binding_unmapped += 1
                    _slot["unmapped"] += 1
            for _rep in list(_attrs.get("repaired") or []):
                if not isinstance(_rep, dict):
                    continue
                try:
                    _ro = str(_rep.get("outcome") or "").strip()
                except Exception:
                    _ro = ""
                if _ro == "applied":
                    _repair_applied += 1
                else:
                    _repair_shadow += 1
            try:
                if bool(_attrs.get("repaired_truncated", False)):
                    _repair_truncated_markers += 1
            except Exception:
                pass
            # Phase 4: not_checkable_count is exact per marker (D7); the
            # checkable total is the binding-row count (NOT_CHECKABLE
            # groups are excluded from binding by D5).
            try:
                _nc = int(_attrs.get("not_checkable_count", 0) or 0)
            except (TypeError, ValueError):
                _nc = 0
            if _nc:
                _not_checkable_count += _nc
            try:
                if bool(_attrs.get("not_checkable_truncated", False)):
                    _not_checkable_truncated_markers += 1
            except Exception:
                pass
            for _l in list(_attrs.get("ledgers") or []):
                if not isinstance(_l, dict):
                    continue
                try:
                    _qs = str(_l.get("query_source") or "derived").strip()
                except Exception:
                    _qs = "derived"
                if _qs != "stored":
                    continue
                _stored_ledgers += 1
                try:
                    _ls = str(_l.get("link_source") or "").strip()
                except Exception:
                    _ls = ""
                if _ls == "stored_query_match":
                    _stored_linked += 1
            try:
                _ev = int(_attrs.get("evaporated_count", 0) or 0)
            except (TypeError, ValueError):
                _ev = 0
            if _ev:
                _history_outcomes["_evals_with_evaporated"] = (
                    _history_outcomes.get("_evals_with_evaporated", 0) + 1
                )
            for _m in list(_attrs.get("missing_obligations") or []):
                if not isinstance(_m, dict):
                    continue
                try:
                    _mk = str(_m.get("claim_kind") or "UNTYPED")
                except Exception:
                    _mk = "UNTYPED"
                if _mk not in _BINDING_KINDS:
                    _mk = "UNTYPED"
                _history_outcomes[f"missing:{_mk}"] = _history_outcomes.get(f"missing:{_mk}", 0) + 1
            try:
                _st = str(_attrs.get("verification_status", "") or "").strip().upper()
            except Exception:
                _st = ""
            try:
                _tier = str(_attrs.get("verification_tier", "") or "").strip().lower()
            except Exception:
                _tier = ""
            _label = _st
            if _st == "VERIFIED" and _tier in ("established", "likely"):
                _label = f"VERIFIED_{_tier.upper()}"
            if _st:
                _seen_kinds: set[str] = set()
                for _b in list(_attrs.get("bindings") or []):
                    if not isinstance(_b, dict):
                        continue
                    try:
                        _bk = str(_b.get("claim_key") or "")
                        _kk = _bk.rsplit("|", 1)[-1].strip() if "|" in _bk else "UNTYPED"
                    except Exception:
                        _kk = "UNTYPED"
                    if _kk not in _BINDING_KINDS:
                        _kk = "UNTYPED"
                    _seen_kinds.add(_kk)
                for _kk in _seen_kinds or {"UNTYPED"}:
                    _slot = _verdict_by_kind.setdefault(_kk, {})
                    _slot[_label] = _slot.get(_label, 0) + 1
                    _slot["n"] = _slot.get("n", 0) + 1
            try:
                _so = str(_attrs.get("history_subject_outcome", "") or "").strip()
            except Exception:
                _so = ""
            if _so:
                _subject_outcomes[_so] = _subject_outcomes.get(_so, 0) + 1
        except Exception:
            continue

    n_hyp = len(hyp_rows)
    print("## Agent-loop measurements")
    print(f"investigations: {len(investigations)} (with reason traces: {len(traces_by_inv)})")
    print(f"reason turns: {total_turns}, forced turns: {forced_turns}", end="")
    print(
        f" => forced_rate={forced_turns / total_turns:.2f}"
        if total_turns
        else " => forced_rate=n/a"
    )
    print(
        f"established conclusions: {established_runs}, overconfident runs (peak>=0.8, not established): {overconfident_runs}"
    )
    print(f"hypotheses: {n_hyp}, unbound: {unbound}", end="")
    print(f" => unbound_rate={unbound / n_hyp:.2f}" if n_hyp else " => unbound_rate=n/a")
    print(f"terminal unbound: {unbound_terminal}, stale 0.5/0.5 scores: {stale_scores}", end="")
    print(f" => stale_score_rate={stale_scores / n_hyp:.2f}" if n_hyp else "")
    print("caveat: unbound is a proxy (zero assessments + zero supporting IDs);")
    print(
        "caveat: pre-§62 rows predate VERIFY verdict persistence, expect stale scores on old runs."
    )
    print(f"claim bindings measured: {_binding_total}", end="")
    if _binding_total:
        print(
            f" => evaporated_rate={_binding_evaporated / _binding_total:.2f}"
            f" ({_binding_evaporated}/{_binding_total}),"
            f" unmapped_rate={_binding_unmapped / _binding_total:.2f}"
            f" ({_binding_unmapped}/{_binding_total}),"
            f" claim_local_rate={_binding_claim_local / _binding_total:.2f}"
            f" ({_binding_claim_local}/{_binding_total})"
        )
        for _k in sorted(_binding_by_kind):
            _s = _binding_by_kind[_k]
            _kt = sum(_s.values())
            print(
                f"  kind {_k}: n={_kt} local={_s['claim_local']}"
                f" fallback={_s['bank_fallback']} evaporated={_s['evaporated']}"
                f" unmapped={_s['unmapped']}"
            )
    else:
        print(" => evaporated_rate=n/a (no VERIFY_EVALUATION bindings in window)")
    if _stored_ledgers:
        print(
            f"stored queries: n={_stored_ledgers} linked={_stored_linked}"
            f" => stored_query_rate={_stored_linked / _stored_ledgers:.2f}"
            f" ({_stored_linked}/{_stored_ledgers})"
        )
    else:
        print("stored queries: n/a (no stored-sourced ledgers in window)")
    # Finding F: denominator is checkable binding rows (= _binding_total;
    # bindings exclude NOT_CHECKABLE by D5), not applied+shadow records.
    _repair_line = format_repair_rate(_repair_applied, _repair_shadow, _binding_total)
    if _repair_truncated_markers:
        _repair_line += f" [TRUNCATED markers: {_repair_truncated_markers} — gate blocked]"
    print(_repair_line)
    # Phase 4 (claim binding): checkable-only denominator + opinion rate.
    # checkable_binding_total is the binding-row count (bindings exclude
    # NOT_CHECKABLE by D5); not_checkable_count rides the marker extras.
    _checkable_total = _binding_total
    _nc_denom = _checkable_total + _not_checkable_count
    if _nc_denom:
        print(
            f"checkable binding groups: {_checkable_total},"
            f" not_checkable: {_not_checkable_count}"
            f" => not_checkable_rate={_not_checkable_count / _nc_denom:.2f}"
            f" ({_not_checkable_count}/{_nc_denom})"
            + (
                f" [TRUNCATED markers: {_not_checkable_truncated_markers} — counts exact]"
                if _not_checkable_truncated_markers
                else ""
            )
        )
    else:
        print("checkable binding groups: n/a (no typed groups in window)")
    if _history_outcomes:
        print(
            "binding missing/evaporation events:",
            ", ".join(f"{_k}={_v}" for _k, _v in sorted(_history_outcomes.items())),
        )
    print("caveat: binding rates read persisted VERIFY_EVALUATION rows only;")
    if _verdict_by_kind:
        print("verdict split per binding kind (n = evals with >=1 binding of kind):")
        for _kk in sorted(_verdict_by_kind):
            _slot = _verdict_by_kind[_kk]
            _parts = " ".join(
                f"{_s}={_slot.get(_s, 0)}"
                for _s in (
                    "VERIFIED_ESTABLISHED",
                    "VERIFIED_LIKELY",
                    "VERIFIED",
                    "INCONCLUSIVE",
                    "SUPPORTED",
                    "CONTRADICTED",
                )
                if _slot.get(_s, 0)
            )
            print(f"  kind {_kk}: n={_slot.get('n', 0)} {_parts}".rstrip())
    else:
        print("verdict split per binding kind: n/a (no VERIFY_EVALUATION rows in window)")
    if _subject_outcomes:
        _so_total = sum(_subject_outcomes.values())
        _so_parts = " ".join(f"{_k}={_v}" for _k, _v in sorted(_subject_outcomes.items()))
        print(f"history subjects: n={_so_total} {_so_parts}")
    else:
        print("history subjects: n/a (no HISTORY VERIFIED markers in window)")
    print("caveat: print numerator plus denominator, never a bare percentage.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
