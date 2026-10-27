# scripts/live_inject_not_checkable.py
"""Injected-claim live test, Phase 4 opt 2 (owner-approved).

Proves the NOT_CHECKABLE retrieval-skip path end to end on the LIVE stack
(real Postgres, real worker code, real battery scorer) without depending on
the model declaring NOT_CHECKABLE (it never does live: 3 PARTIAL data
points). The ONLY injected part is claim formulation — the precise unit
already pinned by 75 unit tests. Everything downstream runs live:
seed (repos) -> verify_root_cause_activity -> conclude_investigation_activity
-> persisted VERIFY_EVALUATION markers -> battery score_supported.

Scratch only: tenant ``phase4-inject``. Run under tmp/launch-spot.sh so env
layers ./.env.local then ~/bin/.iap.env.local. Adopted 2026-10-27 (owner
decision J) as the live proof for the NOT_CHECKABLE path: L13 is demoted
to offline grading, and this injected-claim test proves the path
end-to-end (opinion-only INCONCLUSIVE, marker extras, conclude
abstention, scorer PASS).

Probes:
  P1 opinion-only hypothesis -> verify marker not_checkable_count==1,
      claim_gaps==[], conclude ABSTAIN + NOT_CHECKABLE_SKIPPED, scorer PASS.
  P2 mixed hypothesis (cited DEFINITION + uncited DEFINITION + uncited
      NOT_CHECKABLE) -> gap feed carries the uncited DEFINITION gap,
      not_checkable inventory carries the opinion group (verify-level).
D6 fast path (reason_data predicate) is NOT exercised here — no model turn
runs. It stays unit-pinned (TestFastPathPredicate + cascade-guards pin).
"""
from __future__ import annotations

import asyncio
import sys
import uuid
from datetime import UTC, datetime, timedelta

REPO_ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

TENANT = "phase4-inject"
NOW = datetime.now(UTC)

from investigation_agent_platform.domain.hypothesis.models import (
    AssessmentStrength,
    AssessmentType,
    ClaimKind,
    ClaimTarget,
    Hypothesis,
    HypothesisEvidenceAssessment,
)

RESULTS: dict = {"probes": {}, "errors": []}


def _record(probe: str, ok: bool, detail: str) -> None:
    RESULTS["probes"][probe] = {"ok": ok, "detail": detail}
    print(f"[{probe}] {'PASS' if ok else 'FAIL'}: {detail}", flush=True)


async def _setup_ctx():
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


def _inv(slug: str, problem: str):
    from investigation_agent_platform.domain.investigation.models import (
        Investigation,
        InvestigationContext,
        InvestigationRequest,
        InvestigationStatus,
    )

    start = NOW - timedelta(hours=1)
    return Investigation(
        id=uuid.uuid5(uuid.NAMESPACE_URL, f"iap-inject:{TENANT}:{slug}"),
        session_id=f"inject-{slug}",
        application_id="example-app",
        tenant_id=TENANT,
        request=InvestigationRequest(
            application_id="example-app",
            problem_description=problem,
            session_id=f"inject-{slug}",
            requested_by="live-inject-test",
        ),
        context=InvestigationContext(
            environment="production",
            time_window=(start - timedelta(hours=1), start),
        ),
        status=InvestigationStatus.INVESTIGATING,
        created_at=start,
        updated_at=NOW,
        version=1,
        metadata={"claim_binding_version": "cbv2"},
    )


def _asm(hyp_id, kind, target, evidence_id, reason):
    return HypothesisEvidenceAssessment(
        hypothesis_id=hyp_id,
        evidence_id=evidence_id,
        assessment=AssessmentType.NEUTRAL,
        strength=AssessmentStrength.WEAK,
        reason=reason,
        claim_kind=kind,
        claim_target=target,
    )


def _evidence(inv_id, idx: int, title: str, summary: str):
    from investigation_agent_platform.domain.evidence.models import (
        Evidence,
        EvidenceType,
    )
    from investigation_agent_platform.domain.provenance.models import (
        EvidenceFreshness,
        EvidenceProvenance,
        QueryFingerprint,
        SourceLocation,
    )

    return Evidence(
        tenant_id=TENANT,
        investigation_id=inv_id,
        evidence_type=EvidenceType.SOURCE_CODE,
        provider="live-inject-test",
        source=f"urn:inject:{idx}",
        title=title,
        summary=summary,
        content_snippet="def filter_orders(orders):\n    return [o for o in orders if o.active]",
        fingerprint=f"inject-fp-{idx}",
        observed_at=NOW - timedelta(minutes=30),
        retrieved_at=NOW,
        provenance=EvidenceProvenance(
            tenant_id=TENANT,
            investigation_id=inv_id,
            provider_type="live-inject-test",
            requested_provider_id="live-inject-test",
            actual_provider_id="live-inject-test",
            source_system="live-inject-test",
            retrieval_timestamp=NOW,
            query_fingerprint=QueryFingerprint(
                provider_type="live-inject-test",
                operation="seed",
                normalized_query_hash="inject",
            ),
            source_location=SourceLocation(system="live-inject-test", identifier="seed"),
        ),
        freshness=EvidenceFreshness(observed_at=NOW - timedelta(minutes=30), retrieved_at=NOW),
    )


async def main() -> int:
    from investigation_agent_platform.application.worker.activities import (
        ConcludeInvestigationInput,
        VerifyRootCauseInput,
        conclude_investigation_activity,
        verify_root_cause_activity,
    )

    ctx = await _setup_ctx()
    for repo_name in (
        "investigation_repo",
        "evidence_repo",
        "hypothesis_repo",
        "timeline_repo",
        "finding_repo",
    ):
        if getattr(ctx, repo_name, None) is None:
            msg = f"context missing {repo_name}; aborting (no silent fallback)"
            print(msg, flush=True)
            RESULTS["errors"].append(msg)
            return 2

    # -- P1: opinion-only -------------------------------------------------
    inv1 = _inv("opinion", "Is the order filtering implementation beautiful?")
    if await ctx.investigation_repo.get_by_id(TENANT, inv1.id) is None:
        await ctx.investigation_repo.create(TENANT, inv1)
    h1 = Hypothesis(
        tenant_id=TENANT,
        investigation_id=inv1.id,
        title="Beauty is best practice",
        description="Beauty is best practice",
        statement="The order filtering implementation follows best practice",
        supporting_evidence_ids=[],
        assessments=[],
    )
    h1 = h1.model_copy(
        update={
            "assessments": [
                _asm(
                    h1.id,
                    ClaimKind.NOT_CHECKABLE,
                    ClaimTarget(scope="order-filtering-style"),
                    None,
                    "taste judgment; no observable could settle it",
                )
            ]
        }
    )
    if not await ctx.hypothesis_repo.find_by_investigation_id(TENANT, inv1.id):
        await ctx.hypothesis_repo.save(TENANT, h1, inv1.id, 0)
    else:
        h1 = (await ctx.hypothesis_repo.find_by_investigation_id(TENANT, inv1.id))[0]

    v1 = await verify_root_cause_activity(
        VerifyRootCauseInput(tenant_id=TENANT, investigation_id=str(inv1.id))
    )
    vd1 = dict(v1.data or {})
    _record(
        "P1-verify-shape",
        bool(v1.success) and isinstance(vd1.get("claim_gaps"), list),
        f"success={v1.success} claim_gaps={vd1.get('claim_gaps')!r} "
        f"status={vd1.get('status')!r}",
    )

    tl1 = await ctx.timeline_repo.find_by_investigation_id(TENANT, inv1.id)
    marks1 = [
        e for e in tl1 if str(getattr(e, "event_type", "")) == "VERIFY_EVALUATION"
    ]
    attrs1 = dict(getattr(marks1[-1], "attributes", {}) or {}) if marks1 else {}
    groups1 = list(attrs1.get("not_checkable_groups") or [])
    _record(
        "P1-marker-extras",
        attrs1.get("not_checkable_count") == 1
        and len(groups1) == 1
        and str(groups1[0]).startswith("NOT_CHECKABLE|"),
        f"markers={len(marks1)} not_checkable_count={attrs1.get('not_checkable_count')!r} "
        f"groups={groups1!r}",
    )

    # Mirrors the fixed production path: after a D6 break the workflow
    # requests COMPLETED (not FAILED) so the gate abstains with the
    # NOT_CHECKABLE_SKIPPED marker instead of the epistemic FAILED path.
    c1 = await conclude_investigation_activity(
        ConcludeInvestigationInput(
            tenant_id=TENANT,
            investigation_id=str(inv1.id),
            status="COMPLETED",
            reason="",
        )
    )
    concl1 = await ctx.finding_repo.get_conclusion(TENANT, inv1.id)
    cstatus = str(getattr(getattr(concl1, "status", ""), "value", getattr(concl1, "status", "")) or "")
    lims = list(getattr(concl1, "limitations", None) or [])
    sup = list(getattr(concl1, "supporting_evidence_ids", None) or [])
    _record(
        "P1-conclude-abstain",
        bool(c1.success)
        and cstatus in ("INSUFFICIENT_EVIDENCE", "NO_ROOT_CAUSE_FOUND")
        and "NOT_CHECKABLE_SKIPPED" in lims
        and not sup,
        f"conclude_ok={c1.success} status={cstatus!r} limitations={lims!r} supporting={sup!r}",
    )

    import tranche_b_battery as battery

    verdict, why = battery.score_supported(
        {"expect": {"status": "ABSTAIN", "claim": "not_checkable"}},
        {
            "status": cstatus,
            "root_cause": getattr(concl1, "root_cause", None),
            "limitations": lims,
            "supporting_evidence_ids": [str(s) for s in sup],
            "supporting_hypothesis_ids": [
                str(s) for s in (getattr(concl1, "supporting_hypothesis_ids", None) or [])
            ],
        },
        {},
        {"items": []},
        {
            "items": [
                {
                    "id": str(h1.id),
                    "statement": h1.statement,
                    "supporting_evidence_ids": [str(s) for s in (h1.supporting_evidence_ids or [])],
                }
            ]
        },
    )
    _record("P1-scorer", verdict == "PASS", f"score_supported={verdict!r} ({why})")

    # -- P2: mixed (cited DEFINITION + uncited DEFINITION + opinion) ------
    inv2 = _inv("mixed", "Where is filter_orders defined, and is it best practice?")
    if await ctx.investigation_repo.get_by_id(TENANT, inv2.id) is None:
        await ctx.investigation_repo.create(TENANT, inv2)
    ev = _evidence(inv2.id, 0, "filter_orders source", "filter_orders defined in src/orders.py")
    if not await ctx.evidence_repo.find_by_investigation_id(TENANT, inv2.id):
        await ctx.evidence_repo.save(TENANT, ev, inv2.id)
    else:
        ev = (await ctx.evidence_repo.find_by_investigation_id(TENANT, inv2.id))[0]
    h2 = Hypothesis(
        tenant_id=TENANT,
        investigation_id=inv2.id,
        title="Definition plus taste",
        description="Definition plus taste",
        statement="filter_orders is defined in src/orders.py and follows best practice",
        supporting_evidence_ids=[ev.evidence_id],
        assessments=[],
    )
    h2 = h2.model_copy(
        update={
            "assessments": [
                _asm(
                    h2.id,
                    ClaimKind.DEFINITION,
                    ClaimTarget(symbol="filter_orders", file_path="src/orders.py"),
                    ev.evidence_id,
                    "cited row defines the symbol",
                ),
                _asm(
                    h2.id,
                    ClaimKind.DEFINITION,
                    ClaimTarget(symbol="OrderFilter", file_path="src/filters.py"),
                    None,
                    "uncited second definition surface",
                ),
                _asm(
                    h2.id,
                    ClaimKind.NOT_CHECKABLE,
                    ClaimTarget(scope="order-filtering-style"),
                    None,
                    "taste judgment; no observable could settle it",
                ),
            ]
        }
    )
    if not await ctx.hypothesis_repo.find_by_investigation_id(TENANT, inv2.id):
        await ctx.hypothesis_repo.save(TENANT, h2, inv2.id, 0)

    v2 = await verify_root_cause_activity(
        VerifyRootCauseInput(tenant_id=TENANT, investigation_id=str(inv2.id))
    )
    vd2 = dict(v2.data or {})
    gaps2 = vd2.get("claim_gaps") or []
    gap_kinds = sorted({str(g.get("kind")) for g in gaps2 if isinstance(g, dict)})
    tl2 = await ctx.timeline_repo.find_by_investigation_id(TENANT, inv2.id)
    marks2 = [e for e in tl2 if str(getattr(e, "event_type", "")) == "VERIFY_EVALUATION"]
    attrs2 = dict(getattr(marks2[-1], "attributes", {}) or {}) if marks2 else {}
    _record(
        "P2-gap-feed",
        bool(v2.success)
        and gap_kinds == ["DEFINITION"]
        and all(str(g.get("target_key")) != "NO_TARGET" for g in gaps2),
        f"success={v2.success} gaps={[(g.get('kind'), g.get('gap_id')) for g in gaps2 if isinstance(g, dict)]!r}",
    )
    _record(
        "P2-marker-inventory",
        attrs2.get("not_checkable_count") == 1,
        f"markers={len(marks2)} not_checkable_count={attrs2.get('not_checkable_count')!r} "
        f"groups={attrs2.get('not_checkable_groups')!r}",
    )

    ok = all(p["ok"] for p in RESULTS["probes"].values()) and not RESULTS["errors"]
    print(f"OVERALL: {'PASS' if ok else 'FAIL'}", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
