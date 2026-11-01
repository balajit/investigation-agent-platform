# scripts/gate_inject_claims.py
"""G-T6-D27: injected-claim harness for L14-L16 (generalizes live_inject_not_checkable.py).

The live model chooses its own claim groups, so the H3/H4 shapes cannot be
forced live. This driver runs the NOT_CHECKABLE path in-process against live
Postgres and worker code: create (CREATED) -> operator pin stamp -> banked
range reads (real evidence rows) -> formulated hypothesis with injected claims
-> real verify_root_cause_activity -> real conclude_investigation_activity ->
API-shaped reads -> score_supported -> score_assessments (H2).

Writes rows: requires owner approval. Pure helpers (binds, score_assessments,
plan_cells) are unit-tested without a stack.
"""

from __future__ import annotations

import sys
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5

ROOT = Path(__file__).resolve().parents[1]
import os as _os

sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests" / "unit"))
# G-T6 cross-tree runs: point at the OLD tree's src so the injected run
# exercises the old verify/conclude code. Grading stays on the current tree
# (frozen rubric): use --no-grade here and grade the saved views later.
if _os.environ.get("IAP_OLD_SRC"):
    sys.path.insert(0, _os.environ["IAP_OLD_SRC"])


def binds(assessment: dict, group: dict) -> bool:
    """A persisted assessment that carries evidence for this group's target (not an unresolved placeholder)."""
    target = assessment.get("claim_target") or {}
    want = group["target"]
    return (
        assessment.get("evidence_id") is not None
        and target.get("symbol") == want.get("symbol")
        and target.get("file_path") == want.get("file_path")
    )


def score_assessments(case: dict, assessments: list[dict]) -> tuple[bool, str]:
    """H2: each grounded group's evidence binding must survive its sibling (no overwrite)."""
    for group in case["expect"]["claim_groups"]:
        if group.get("grounded") and not any(binds(a, group) for a in assessments):
            return (False, f"binding for {group['target'].get('symbol')} was overwritten or never persisted")
    return (True, "")


def plan_cells(seed: int, repeats: int = 3,
               trees: tuple[str, str] = ("230d34b", "d7a3969"),
               pins: tuple[str, str] = ("cbv1", "cbv3")) -> list[dict]:
    """GT6.10: the 2x2 plan. Every cell x repeat gets a unique fresh tenant;
    the order is reproducible from the seed."""
    import random

    rng = random.Random(seed)
    cells = []
    for tree in trees:
        for pin in pins:
            for rep in range(1, repeats + 1):
                cells.append({
                    "tree": tree, "pin": pin, "repeat": rep,
                    "tenant": f"n-gt6-{tree[:7]}-{pin}-r{rep}",
                    "fixtures": ["L14", "L15", "L16"],
                })
    rng.shuffle(cells)
    return cells


def _combined_statement(case: dict) -> str:
    parts = []
    for group in case["expect"]["claim_groups"]:
        statement = str(group.get("statement") or "")
        if not statement:
            target = group.get("target") or {}
            symbol = target.get("symbol", "?")
            path = target.get("file_path", "?")
            if group.get("kind") == "DEFINITION":
                statement = f"{symbol} is defined in {path}"
            elif group.get("kind") == "CALL_EDGE":
                statement = f"{symbol} calls its counterpart"
            else:
                statement = f"{symbol} in {path}"
        parts.append(statement)
    return " ".join(parts)


async def _setup_ctx():  # type: ignore[no-untyped-def]
    from investigation_agent_platform.api.dependencies import AppContext, set_app_context
    from investigation_agent_platform.bootstrap.buffered_dev import (
        ensure_buffered_dev_context,
    )
    from investigation_agent_platform.infrastructure.configuration.config import (
        load_application_config_from_env,
    )

    config = load_application_config_from_env()
    ctx = AppContext()
    ctx = await ensure_buffered_dev_context(ctx, config)
    set_app_context(ctx)
    return ctx


def _bank_row(tenant: str, inv_id, group: dict):  # type: ignore[no-untyped-def]
    from test_claim_binding_phase3 import _code_row


    target = group.get("target") or {}
    symbol = str(target.get("symbol", "handleRequest"))
    path = str(target.get("file_path", "src/alpha.py"))
    row = _code_row(
        "GET_CODE", path, "abc123",
        f"40| def {symbol}(req):\n41|     validate(req)\n42|     return dispatch(req)",
        start=40, end=42, read_mode="range",
    )
    return row.model_copy(update={
        "tenant_id": tenant, "investigation_id": inv_id,
        "evidence_id": uuid4(),
    })


async def run_fixture(case: dict, tenant: str, pin: str, grade: bool = True) -> dict:
    """Run one fixture end to end through the real verify + conclude path."""

    from investigation_agent_platform.application.worker.activities import (
        ConcludeInvestigationInput,
        VerifyRootCauseInput,
        conclude_investigation_activity,
        verify_root_cause_activity,
    )
    from investigation_agent_platform.domain.hypothesis.models import (
        AssessmentStrength,
        AssessmentType,
        ClaimKind,
        ClaimTarget,
        Hypothesis,
        HypothesisEvidenceAssessment,
    )
    from investigation_agent_platform.domain.investigation.models import (
        Investigation,
        InvestigationContext,
        InvestigationRequest,
        InvestigationStatus,
    )

    ctx = await _setup_ctx()
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    inv = Investigation(
        id=uuid5(NAMESPACE_URL, f"iap-gate-inject:{tenant}:{case['id']}:{pin}"),
        session_id=f"inject-{case['id']}-{pin}",
        application_id="spring-sample-service",
        tenant_id=tenant,
        request=InvestigationRequest(
            application_id="spring-sample-service",
            problem_description=str(case.get("symptom", "")),
            session_id=f"inject-{case['id']}-{pin}",
            requested_by="gate-inject-claims",
        ),
        context=InvestigationContext(environment="dev", time_window=(now - timedelta(hours=1), now)),
        status=InvestigationStatus.CREATED,
        created_at=now,
        updated_at=now,
        version=1,
        # The pin rides on the entity at creation (same contract the operator
        # stamp establishes; the battery's stamp path stays the live-proven
        # one). Raw-SQL stamping is avoided here: on a just-created row the
        # buffered-dev create path can leave a jsonb scalar null, which the
        # stamp's || then array-merges into [null, {...}] (seen live).
        metadata={"claim_binding_version": pin},
    )
    if await ctx.investigation_repo.get_by_id(tenant, inv.id) is None:
        await ctx.investigation_repo.create(tenant, inv)
    _read_back = await ctx.investigation_repo.get_by_id(tenant, inv.id)
    stamped = (_read_back is not None and (_read_back.metadata or {}).get("claim_binding_version") == pin)

    rows = {}
    for group in case["expect"]["claim_groups"]:
        if group.get("grounded"):
            row = _bank_row(tenant, inv.id, group)
            await ctx.evidence_repo.save(tenant, row, inv.id)
            rows[id(group)] = row
    # Retrieval-attempt marker per banked row (the shape the claim-binding
    # path reads; mirrors the gate-probe harness).
    from investigation_agent_platform.domain.timeline.models import (
        TimelineEvent,
        TimelineEventType,
    )

    for group in case["expect"]["claim_groups"]:
        row = rows.get(id(group))
        if row is None:
            continue
        target = group.get("target") or {}
        await ctx.timeline_repo.append(
            tenant,
            TimelineEvent(
                tenant_id=tenant,
                investigation_id=inv.id,
                timestamp=now,
                event_type=TimelineEventType.RETRIEVAL_ATTEMPT,
                description=f"injected range read for {target.get('symbol', '?')}",
                evidence_ids=[row.evidence_id],
                attributes={
                    "attempt": {
                        "search_query": str(target.get("symbol", "")),
                        "scope": None,
                        "outcome": "ROWS_RETURNED",
                    },
                    "evidence_ids": [str(row.evidence_id)],
                    "attempt_id": f"att-{uuid4().hex[:8]}",
                },
            ),
            inv.id,
        )

    asms = []
    for group in case["expect"]["claim_groups"]:
        kind = ClaimKind(str(group.get("kind", "DEFINITION")))
        row = rows.get(id(group))
        target = group.get("target") or {}
        hyp_id = uuid4()  # placeholder, replaced below
        asms.append((group, row, kind, target, hyp_id))

    hyp = Hypothesis(
        tenant_id=tenant,
        investigation_id=inv.id,
        title=_combined_statement(case)[:256],
        description=_combined_statement(case),
        statement=_combined_statement(case),
        status="PROPOSED",
    )
    final_asms = []
    for group, row, kind, target, _ in asms:
        final_asms.append(HypothesisEvidenceAssessment(
            hypothesis_id=hyp.id,
            evidence_id=row.evidence_id if row is not None else None,
            assessment=AssessmentType.SUPPORTS if row is not None else AssessmentType.NEUTRAL,
            strength=AssessmentStrength.STRONG if row is not None else AssessmentStrength.WEAK,
            reason="injected claim group",
            claim_kind=kind,
            claim_target=ClaimTarget(
                symbol=str(target.get("symbol") or "") or None,
                file_path=str(target.get("file_path") or "") or None,
            ),
        ))
    hyp = hyp.model_copy(update={
        "assessments": final_asms,
        "supporting_evidence_ids": [r.evidence_id for r in rows.values()],
    })
    await ctx.hypothesis_repo.save(tenant, hyp, inv.id, expected_version=0)

    vres = await verify_root_cause_activity(
        VerifyRootCauseInput(tenant_id=tenant, investigation_id=str(inv.id))
    )
    cres = await conclude_investigation_activity(
        ConcludeInvestigationInput(tenant_id=tenant, investigation_id=str(inv.id), status="COMPLETED")
    )

    concl = await ctx.finding_repo.get_conclusion(tenant, inv.id)
    findings, _ = await ctx.finding_repo.list_findings(tenant, inv.id, limit=50)
    evidence_rows = await ctx.evidence_repo.find_by_investigation_id(tenant, inv.id)
    hyps = await ctx.hypothesis_repo.find_by_investigation_id(tenant, inv.id)
    hyp_row = next((h for h in hyps if str(h.id) == str(hyp.id)), hyps[0] if hyps else None)

    def _dump(row):  # type: ignore[no-untyped-def]
        try:
            return row.model_dump(mode="json")
        except Exception:
            return dict(getattr(row, "__dict__", {}))

    concl_d = _dump(concl) if concl is not None else {}
    findings_d = {"items": [_dump(f) for f in findings or []]}
    evidence_d = {"items": [_dump(e) for e in evidence_rows or []]}
    hyps_d = {"items": [_dump(h) for h in hyps or []]}
    inv_d = {"metadata": {"loop_telemetry": {}}}
    if grade:
        from tranche_b_battery import score_supported

        verdict, detail = score_supported(
            case, concl_d, findings_d, evidence_d, hyps_d, investigation=inv_d,
        )
    else:
        verdict, detail = "UNGRADED", "views saved; grade with the frozen rubric later"
    stored_asms = [
        {"claim_target": (a.claim_target.model_dump(mode="json") if getattr(a, "claim_target", None) is not None else {}),
         "evidence_id": str(a.evidence_id) if getattr(a, "evidence_id", None) is not None else None}
        for a in (getattr(hyp_row, "assessments", None) or [])
    ] if hyp_row is not None else []
    h2_ok, h2_detail = score_assessments(case, stored_asms)
    return {
        "case": case.get("id"), "verdict": verdict, "detail": detail,
        "h2": h2_ok, "h2_detail": h2_detail,
        "stamped": stamped,
        "verify_success": bool(getattr(vres, "success", False)),
        "conclude_status": str(getattr(cres, "status", "")),
        "limitations": list((concl_d or {}).get("limitations", [])),
        "root_cause": str((concl_d or {}).get("root_cause", "")),
        # Frozen-rubric grading reads these (cross-tree: grade later).
        "views": {
            "conclusion": concl_d, "findings": findings_d,
            "evidence": evidence_d, "hypotheses": hyps_d,
            "investigation": inv_d,
        },
    }


def _load_cases() -> list[dict]:
    """Fixture cases straight from JSON (tree-agnostic: never imports the grader)."""
    import json as _json

    out = []
    for path in sorted((ROOT / "tests" / "fixtures" / "battery_cases").glob("*.json")):
        out.append(_json.loads(path.read_text(encoding="utf-8")))
    return out


def grade_records(records: list[dict], case_by_id: dict) -> list[dict]:
    """Score saved views with the CURRENT (frozen-rubric) grader."""
    from tranche_b_battery import score_supported

    out = []
    for rec in records:
        views = rec.get("views") or {}
        case = case_by_id.get(rec.get("case"), {})
        verdict, detail = score_supported(
            case, views.get("conclusion") or {}, views.get("findings") or {},
            views.get("evidence") or {}, views.get("hypotheses") or {},
            investigation=views.get("investigation") or {},
        )
        out.append({**rec, "verdict": verdict, "detail": detail})
    return out


def main() -> int:
    import argparse
    import asyncio
    import json

    ap = argparse.ArgumentParser(description="G-T6 injected-claim harness (writes rows; needs owner approval).")
    ap.add_argument("--tenant", default="")
    ap.add_argument("--pin", default="cbv1", choices=["cbv1", "cbv2", "cbv3"])
    ap.add_argument("--only", default="L14,L15,L16")
    ap.add_argument("--json", default="")
    ap.add_argument("--no-grade", action="store_true", help="Save views only; grade later with --grade-records.")
    ap.add_argument("--grade-records", default="", help="Score a saved views JSON with the frozen rubric.")
    args = ap.parse_args()

    if args.grade_records:
        from pathlib import Path as _Path

        import battery_stats  # noqa: F401 (rubric lives here for the report)

        raw = json.loads(_Path(args.grade_records).read_text(encoding="utf-8"))
        rows = raw if isinstance(raw, list) else raw.get("records", [])
        cases = {c["id"]: c for c in _load_cases()}
        out = grade_records(rows, cases)
        print(json.dumps([{"case": r["case"], "verdict": r["verdict"], "detail": r["detail"][:120],
                           "h2": r.get("h2")} for r in out], indent=2))
        if args.json:
            with open(args.json, "w", encoding="utf-8") as fh:
                json.dump(out, fh, indent=2, default=str)
        bad = sum(1 for r in out if r["verdict"] in ("FAIL",) or not r.get("h2"))
        return 0 if bad == 0 else 1

    if not args.tenant:
        ap.error("--tenant is required for live runs")
    cases = {c["id"]: c for c in _load_cases()}
    selected = [c.strip() for c in args.only.split(",") if c.strip()]
    out = []
    for cid in selected:
        rec = asyncio.run(run_fixture(cases[cid], args.tenant, args.pin, grade=not args.no_grade))
        print(f"[{cid}] {rec['verdict']}: {rec['detail']} h2={rec['h2']}", flush=True)
        out.append(rec)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(out, fh, indent=2, default=str)
    bad = sum(1 for r in out if r["verdict"] in ("FAIL",) or not r["h2"])
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
