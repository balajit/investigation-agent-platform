#!/usr/bin/env python3
"""Live battery runner: run investigations, verify with the conclusion-support
rubric, clean up after itself, and spit out a diagnostic report.

The 10-run plan (default): all live fixtures once, plus repeats of
L1/L2/L4/L5 for variance. Re-runnable: prior battery rows for the tenant are deleted before the
sweep (fresh lineage, no serve_cached ghosts). Post-run rows are always
kept for forensics — wiping is the next run's cleanup-prior, never this
run's teardown.

Verification reuses score_supported from tranche_b_battery (single rubric,
no drift) plus citation-integrity cross-checks.

Usage:
  uv run python scripts/live_battery.py [--api-base URL] [--tenant ID]
      [--only L1,L2] [--repeat-each N] [--timeout SECS]
      [--force-cleanup] [--report PATH] [--no-cleanup-prior]

Env: layered repo env expected (./.env.local then ~/bin/.iap.env.local),
notably IAP_DATABASE_URI for cleanup. Never prints secrets.
"""

from __future__ import annotations

import argparse
import datetime
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

from tranche_b_battery import (
    APP_ID,
    TERMINAL,
    api,
    load_cases,
    score_supported,
)

DEFAULT_TENANT = "n-batt"
REPEAT_SET = ["L1", "L2", "L4", "L5"]
RUN_TAG_PREFIX = "live-battery"


def sh(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=20, check=False).stdout.strip()
    except Exception:
        return "unknown"


def run_id() -> str:
    return datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%S")


def db_dsn() -> str:
    uri = os.environ.get("IAP_DATABASE_URI", "")
    return re.sub(r"^postgresql\+asyncpg://", "postgresql://", uri)


def ensure_profile(api_base: str, tenant: str, app_id: str) -> None:
    _, data = api(api_base, tenant, "GET", "/api/v1/profiles")
    ids = {p.get("id") for p in (data.get("items") or [])}
    if app_id in ids:
        print(f"  profile {app_id} present for {tenant}")
        return
    from investigation_agent_platform.infrastructure.configuration.config import (
        load_application_profile_from_file,
    )

    profile = load_application_profile_from_file("config/profiles/tranche-b-offer.yaml")
    body = profile.model_dump(mode="json")
    body["version"] = 1
    body["tenant_id"] = tenant
    body["application_id"] = app_id
    st, data = api(api_base, tenant, "POST", "/api/v1/profiles", body)
    if st not in (200, 201):
        raise RuntimeError(f"profile create failed: {st} {str(data)[:200]}")
    print(f"  profile {app_id} created for {tenant} (v1)")


def run_case(api_base, tenant, app_id, case, rep, timeout, run_tag):
    label = f"{case['id']}#{rep}"
    t0 = time.time()
    rec: dict = {"case": case["id"], "rep": rep, "label": label}
    body = {
        "application_id": app_id,
        "problem_description": case["symptom"],
        "initial_context": f"{case.get('context', '')} [{RUN_TAG_PREFIX}:{run_tag}:{case['id']}]",
        "session_id": f"{RUN_TAG_PREFIX}-{run_tag}",
    }
    try:
        st, data = api(api_base, tenant, "POST", "/api/v1/investigations", body)
        iid = (data or {}).get("id")
        if not iid:
            rec.update(status="CREATE_FAILED", verdict="FAIL", detail=f"create {st}")
            return rec
        rec["iid"] = iid
        st, _ = api(api_base, tenant, "POST", f"/api/v1/investigations/{iid}/start")
        if st == 409:
            _, inv = api(api_base, tenant, "GET", f"/api/v1/investigations/{iid}")
            status = (inv.get("investigation") or {}).get("status", "?")
            if status == "CANCELLED":
                # Same cached-serve follow as the post-poll CANCELLED path:
                # a serve_cached create is already CANCELLED here, so start
                # 409s without the run ever polling. Grade the primary.
                followed, follow_note = _cached_primary(api_base, tenant, iid, timeout)
                if followed is not None:
                    pid, concl, findings, evidence, hyps = followed
                    verdict, detail = score_supported(case, concl, findings, evidence, hyps)
                    rec.update(
                        verdict=verdict,
                        detail=f"served cached from {pid[:8]}: {detail}",
                    )
                    rec.update(diagnose(concl, findings, evidence, hyps))
                    rec["duration_s"] = round(time.time() - t0, 1)
                    rec["cached_from"] = pid
                    return rec
                rec.update(
                    status=status,
                    verdict="FAIL",
                    detail=f"not startable ({status}; {follow_note})",
                )
                rec["follow_note"] = follow_note
                rec["duration_s"] = round(time.time() - t0, 1)
                return rec
            rec.update(status=status, verdict="FAIL", detail=f"not startable ({status})")
            rec["duration_s"] = round(time.time() - t0, 1)
            return rec
        inv = poll(api_base, tenant, iid, timeout)
    except TimeoutError as exc:
        rec.update(status="TIMEOUT", verdict="FAIL", detail=str(exc)[:160])
        rec["duration_s"] = round(time.time() - t0, 1)
        return rec
    status = inv.get("status", "?")
    rec["status"] = status
    if status == "FAILED":
        _, det = api(api_base, tenant, "GET", f"/api/v1/investigations/{iid}")
        meta = ((det.get("investigation") or {}).get("metadata")) or {}
        rec["failure"] = meta.get("failure")
    if status == "CANCELLED":
        # serve_cached is the product working (a re-verified prior
        # conclusion was served instead of re-running): follow the cached
        # primary and grade THAT answer rather than failing the run for
        # not running. A duplicate of an active run follows the primary
        # the same way once it lands. Anything else stays not-startable FAIL.
        followed, follow_note = _cached_primary(api_base, tenant, iid, timeout)
        if followed is not None:
            pid, concl, findings, evidence, hyps = followed
            verdict, detail = score_supported(case, concl, findings, evidence, hyps)
            rec.update(verdict=verdict, detail=f"served cached from {pid[:8]}: {detail}")
            rec.update(diagnose(concl, findings, evidence, hyps))
            rec["duration_s"] = round(time.time() - t0, 1)
            rec["cached_from"] = pid
            return rec
        rec.update(status=status, verdict="FAIL", detail=f"not startable ({status})")
        rec["follow_note"] = follow_note
        return rec
    _, conclusion = api(api_base, tenant, "GET", f"/api/v1/investigations/{iid}/conclusion")
    _, findings = api(api_base, tenant, "GET", f"/api/v1/investigations/{iid}/findings")
    _, evidence = api(api_base, tenant, "GET", f"/api/v1/investigations/{iid}/evidence?limit=100")
    _, hyps = api(api_base, tenant, "GET", f"/api/v1/investigations/{iid}/hypotheses")
    concl = conclusion.get("conclusion") or {}
    verdict, detail = score_supported(case, concl, findings, evidence, hyps)
    rec.update(verdict=verdict, detail=detail)
    rec.update(diagnose(concl, findings, evidence, hyps))
    rec["duration_s"] = round(time.time() - t0, 1)
    return rec


def analyze_db(dsn: str, records: list) -> None:
    """Enrich records with database-level diagnostics (in place).

    The API payloads show the published outcome; the database shows how
    the run got there: evidence depth by type, hypothesis claim kinds,
    revision spread across code rows (drift), unresolved frames, absence
    proofs, durations, and failure/disposition metadata. Fail-open: if
    the database is unreachable, records keep API-only diagnostics.
    """
    import asyncio

    async def _run():
        import asyncpg

        conn = await asyncpg.connect(dsn)
        try:
            for rec in records:
                iid = rec.get("iid")
                if not iid:
                    continue
                try:
                    rec["db"] = await _analyze_one(conn, iid)
                except Exception as exc:
                    rec["db"] = {"error": f"{type(exc).__name__}: {str(exc)[:160]}"}
        finally:
            await conn.close()

    try:
        asyncio.run(_run())
    except Exception as exc:
        print(f"db analysis unavailable ({type(exc).__name__}); API-only report")


async def _analyze_one(conn, iid: str) -> dict:
    import json as _json

    def _j(value, default):
        if value is None:
            return default
        if isinstance(value, (dict, list)):
            return value
        try:
            return _json.loads(value)
        except Exception:
            return default

    out: dict = {}
    inv = await conn.fetchrow(
        "SELECT status, started_at, completed_at, metadata_json "
        "FROM investigations WHERE id::text = $1",
        iid,
    )
    if inv is None:
        return {"error": "investigation row missing"}
    out["status"] = inv["status"]
    if inv["started_at"] and inv["completed_at"]:
        out["duration_s_db"] = round(
            (inv["completed_at"] - inv["started_at"]).total_seconds(), 1
        )
    meta = _j(inv["metadata_json"], {})
    out["failure"] = meta.get("failure")
    disp = _j(meta.get("disposition"), {})
    out["disposition"] = {k: disp.get(k) for k in ("action", "reason")}
    ev_rows = await conn.fetch(
        "SELECT type, payload_json FROM evidence WHERE investigation_id::text = $1", iid
    )
    by_type: dict = {}
    revisions: set = set()
    read_modes: dict = {}
    unresolved = 0
    absence_proofs = 0
    for etype, raw_payload in ev_rows:
        by_type[etype] = by_type.get(etype, 0) + 1
        payload = _j(raw_payload, {})
        attrs = _j(payload.get("attributes"), {})
        loc = _j((payload.get("provenance") or {}).get("source_location"), {})
        if etype in ("SOURCE_CODE", "EXCEPTION_PATH"):
            if loc.get("revision"):
                revisions.add(str(loc["revision"])[:12])
            mode = attrs.get("read_mode")
            if not mode:
                mode = "path_only" if attrs.get("discovery_only") else "unstamped"
            read_modes[mode] = read_modes.get(mode, 0) + 1
            if attrs.get("unresolved"):
                unresolved += 1
        if attrs.get("match_count") == 0 and attrs.get("search_scope"):
            absence_proofs += 1
    out["evidence_by_type_db"] = by_type
    out["evidence_total_db"] = len(ev_rows)
    out["code_revisions"] = sorted(revisions)
    out["revision_drift"] = len(revisions) > 1
    out["read_modes"] = read_modes
    out["unresolved_frames"] = unresolved
    out["absence_proofs"] = absence_proofs
    hyps = await conn.fetch(
        "SELECT statement, status, supporting_evidence_ids, refuting_evidence_ids,"
        " assessments_json, causal_steps_json FROM hypotheses"
        " WHERE investigation_id::text = $1",
        iid,
    )
    out["hypotheses_total"] = len(hyps)
    statuses: dict = {}
    kinds: dict = {}
    supported = 0
    with_chain = 0
    statements = []
    for statement, status, supporting, refuting, raw_assessments, steps in hyps:
        statuses[status] = statuses.get(status, 0) + 1
        if supporting:
            supported += 1
        if _j(steps, []):
            with_chain += 1
        statements.append(f"{status}: {str(statement or '')[:100]}")
        for asm in _j(raw_assessments, []):
            kind = (_j(asm, {}).get("claim_kind")) or "untyped"
            kinds[kind] = kinds.get(kind, 0) + 1
    out["hyp_statuses"] = statuses
    out["claim_kinds"] = kinds
    out["supported_hyps"] = supported
    out["hyps_with_chain"] = with_chain
    out["hypotheses"] = statements[:8]
    concl = await conn.fetchrow(
        "SELECT status, limitations, next_check,"
        " jsonb_array_length(supporting_evidence_ids) AS n_support"
        " FROM investigation_conclusions"
        " WHERE investigation_id::text = $1",
        iid,
    )
    if concl is not None:
        out["conclusion_status"] = concl["status"]
        out["limitations_total"] = len(_j(concl["limitations"], []))
        out["has_next_check"] = bool(concl["next_check"])
        out["n_support_db"] = concl["n_support"]
    return out


def _cached_primary(api_base, tenant, iid, timeout=600):
    """(primary_id, conclusion, findings, evidence, hyps) or (None, note).

    A CANCELLED investigation whose disposition served a re-verified
    cached conclusion names the primary in ``primary_target_id``
    (``to_metadata``) with a human ``reason`` ("... duplicate of <uuid>").
    Prefer the structured field; fall back to the reason-text regex for
    older rows. Fetches retry once on transport failure (conclusion GETs
    must not die on a blip after the investigation did all the work).

    P0 create-race fix: a CANCELLED duplicate of an ACTIVE run is also
    followed — the primary is polled to terminal first, then graded, so
    duplicate dispatches score the answer instead of failing as
    "not startable".
    """
    def _get(path, tries=2):
        last = None
        for _ in range(tries):
            try:
                return api(api_base, tenant, "GET", path)
            except Exception as exc:
                last = exc
        raise last

    try:
        _, det = _get(f"/api/v1/investigations/{iid}")
        meta = ((det.get("investigation") or {}).get("metadata")) or {}
        disp = meta.get("disposition") or {}
        action = disp.get("action")
        if action not in ("serve_cached", "duplicate"):
            return None, f"disposition={action}"
        pid = disp.get("primary_target_id") or ""
        if not re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
            str(pid),
        ):
            match = re.search(
                r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                str(disp.get("reason", "")),
            )
            if not match:
                return None, "no primary uuid in reason"
            pid = match.group(0)
        if action == "duplicate":
            # The primary was still running when this duplicate closed:
            # wait it out, then grade its answer.
            try:
                poll(api_base, tenant, pid, timeout)
            except TimeoutError as exc:
                return None, f"primary not terminal: {exc}"
            except Exception as exc:
                return None, f"primary poll failed: {type(exc).__name__}: {str(exc)[:160]}"
        _, conclusion = _get(f"/api/v1/investigations/{pid}/conclusion")
        _, findings = _get(f"/api/v1/investigations/{pid}/findings")
        _, evidence = _get(f"/api/v1/investigations/{pid}/evidence?limit=100")
        _, hyps = _get(f"/api/v1/investigations/{pid}/hypotheses")
        if not (conclusion.get("conclusion") or {}).get("status"):
            return None, "primary conclusion missing"
        return (pid, conclusion.get("conclusion") or {}, findings, evidence, hyps), ""
    except Exception as exc:
        return None, f"{type(exc).__name__}: {str(exc)[:160]}"


def poll(api_base, tenant, iid, timeout):
    t0 = time.time()
    while True:
        _, data = api(api_base, tenant, "GET", f"/api/v1/investigations/{iid}")
        inv = data.get("investigation", {})
        if inv.get("status") in TERMINAL:
            return inv
        if time.time() - t0 > timeout:
            raise TimeoutError(f"{iid} not terminal after {timeout}s")
        time.sleep(15)


def diagnose(concl, findings, evidence, hyps):
    by_id = {}
    for row in (evidence or {}).get("items", []) or []:
        try:
            by_id[str(row.get("evidence_id", ""))] = row
        except Exception:
            continue
    supporting = set((concl or {}).get("supporting_evidence_ids") or [])
    for hid in (concl or {}).get("supporting_hypothesis_ids") or []:
        for hyp in (hyps or {}).get("items", []) or []:
            if str(hyp.get("id", "")) == str(hid):
                supporting.update(hyp.get("supporting_evidence_ids") or [])
    unresolvable = sorted(i for i in supporting if i not in by_id)
    by_type: dict = {}
    for row in (evidence or {}).get("items", []) or []:
        t = str(row.get("evidence_type", "?"))
        by_type[t] = by_type.get(t, 0) + 1
    hyp_list = []
    for hyp in (hyps or {}).get("items", []) or []:
        hyp_list.append(
            f"{hyp.get('status', '?')}: {str(hyp.get('statement', ''))[:100]}"
        )
    return {
        "conclusion_status": (concl or {}).get("status", "?"),
        "root_cause": str((concl or {}).get("root_cause") or "")[:220],
        "evidence_by_type": by_type,
        "evidence_total": len((evidence or {}).get("items", []) or []),
        "hypotheses": hyp_list,
        "limitations": list((concl or {}).get("limitations") or []),
        "next_check": str((concl or {}).get("next_check") or "")[:220],
        "unresolvable_citations": unresolvable,
    }


def _failure_bucket(rec: dict) -> str:
    """Heuristic five-bucket tag (REV_BATTERY Phase 3b, report-only).

    Buckets: retrieval / target-resolution / proof-construction / policy /
    genuine-insufficiency. Uses API + DB diagnostics already in the record;
    never re-runs verification. Refine as the structured trace lands.
    """
    db = rec.get("db") or {}
    kinds = db.get("claim_kinds") or {}
    ev_types = rec.get("evidence_by_type") or {}
    hyps = rec.get("hypotheses") or []
    n_ev = rec.get("evidence_total", "?")
    try:
        n_ev = int(n_ev)
    except (TypeError, ValueError):
        n_ev = -1
    if n_ev == 0:
        return "retrieval — no evidence banked"
    if "CALL_EDGE" in kinds:
        return "proof-construction — call candidates banked but no verified call-site binding (see bound vs banked rows)"
    if "ABSENCE" in kinds and (db.get("absence_proofs") or 0) > 0:
        return "policy — scoped absence proof banked but heuristic score / chain gate abstains"
    if "BEHAVIOR" in kinds and not any(
        t in ev_types for t in ("RUNTIME_LOG", "RUNTIME_TRACE", "DATABASE_STATE", "METRIC", "LOG", "TRACE")
    ):
        return "policy — behavior claim with code only, no runtime observation"
    if "CAUSAL" in kinds and (db.get("hyps_with_chain") or 0) == 0:
        return "proof-construction — causal claim with no bound causal chain"
    if not hyps:
        return "retrieval — no hypotheses formulated"
    return "genuine-insufficiency — see limitation / next_check"


def build_report(meta, records):
    lines = []
    lines.append(f"# Live battery report — {meta['ts']}")
    lines.append("")
    lines.append(f"- api: {meta['api_base']} | tenant: {meta['tenant']} | app: {meta['app_id']}")
    lines.append(f"- git: {meta['git_sha']} | prompt: {meta['prompt_version']} | llm: {meta['llm']}")
    lines.append(f"- run tag: {meta['run_tag']}")
    lines.append("")
    lines.append("| case | rep | verdict | status | detail | dur_s | ev | hyps |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for r in records:
        lines.append(
            f"| {r['case']} | {r.get('rep', 1)} | {r.get('verdict', '?')} | "
            f"{r.get('status', '?')} | {r.get('detail', '')[:80]} | "
            f"{r.get('duration_s', '?')} | {r.get('evidence_total', '?')} | "
            f"{len(r.get('hypotheses', []))} |"
        )
    counts: dict = {}
    for r in records:
        counts[r.get("verdict", "?")] = counts.get(r.get("verdict", "?"), 0) + 1
    lines.append("")
    lines.append(f"Totals: {counts}")
    # variance: repeated cases
    seen: dict = {}
    for r in records:
        seen.setdefault(r["case"], []).append(r.get("verdict", "?"))
    lines.append("")
    lines.append("## Variance (repeated cases)")
    repeated = {cid: verdicts for cid, verdicts in sorted(seen.items()) if len(verdicts) > 1}
    if not repeated:
        lines.append("- no repeats in this run")
    for cid, verdicts in repeated.items():
        flag = "STABLE" if len(set(verdicts)) == 1 else "VARIES"
        lines.append(f"- {cid}: {verdicts} → {flag}")
    # failure taxonomy
    lines.append("")
    lines.append("## Failure taxonomy")
    for r in records:
        if r.get("verdict") in ("FAIL", "PARTIAL"):
            cause = r.get("detail", "")
            if r.get("failure"):
                cause = f"run failed: {r['failure']}"
            lines.append(f"- {r['label']}: {r.get('status', '?')} — {cause[:160]}")
    # per-case diagnostics
    lines.append("")
    lines.append("## Per-case diagnostics")
    for r in records:
        verdict = r.get("verdict", "?")
        status = r.get("status", "?")
        lines.append("")
        lines.append(f"### {r['label']} — {verdict} ({status})")
        if r.get("root_cause"):
            lines.append(f"- root_cause: {r['root_cause'][:200]}")
        lines.append(f"- evidence: {r.get('evidence_by_type', {})}")
        for h in r.get("hypotheses", [])[:6]:
            lines.append(f"- hyp: {h[:140]}")
        for lim in r.get("limitations", [])[:4]:
            lines.append(f"- limitation: {str(lim)[:160]}")
        if r.get("next_check"):
            lines.append(f"- next_check: {r['next_check'][:160]}")
        if r.get("unresolvable_citations"):
            lines.append(f"- UNRESOLVABLE CITATIONS: {r['unresolvable_citations']}")
        if r.get("failure"):
            lines.append(f"- failure: {r['failure']}")
        if r.get("follow_note"):
            lines.append(f"- follow_note: {r['follow_note']}")
        if r.get("cached_from"):
            lines.append(f"- cached_from: {r['cached_from']}")
        db = r.get("db")
        if db:
            lines.append(f"- db: duration_s={db.get('duration_s_db', '?')} "
                         f"ev_total={db.get('evidence_total_db', '?')} "
                         f"hyps={db.get('hypotheses_total', '?')} "
                         f"supported={db.get('supported_hyps', '?')} "
                         f"chains={db.get('hyps_with_chain', '?')}")
            if db.get("code_revisions"):
                flag = " DRIFT" if db.get("revision_drift") else ""
                lines.append(f"- db: code revisions={db['code_revisions']}{flag}")
            if db.get("read_modes"):
                lines.append(f"- db: read_modes={db['read_modes']}")
            if db.get("claim_kinds"):
                lines.append(f"- db: claim_kinds={db['claim_kinds']}")
            if db.get("unresolved_frames"):
                lines.append(f"- db: unresolved_frames={db['unresolved_frames']}")
            if db.get("absence_proofs"):
                lines.append(f"- db: absence_proofs={db['absence_proofs']}")
            if db.get("failure"):
                lines.append(f"- db failure: {db['failure']}")
            if isinstance(db.get("disposition"), dict) and db["disposition"].get("action"):
                lines.append(f"- db disposition: {db['disposition']}")
            if db.get("error"):
                lines.append(f"- db error: {db['error']}")
            if verdict == "FAIL":
                lines.append(f"- bucket: {_failure_bucket(r)}")
    lines.append("")
    # cross-run DB aggregates
    kind_totals: dict = {}
    drift_cases = []
    untyped_runs = []
    for r in records:
        db = r.get("db") or {}
        for kind, n in (db.get("claim_kinds") or {}).items():
            kind_totals[kind] = kind_totals.get(kind, 0) + n
        if db.get("revision_drift"):
            drift_cases.append(r["label"])
        if (db.get("claim_kinds") or {}).get("untyped"):
            untyped_runs.append(r["label"])
    lines.append("## DB aggregates (diagnosis)")
    lines.append(f"- claim kinds across runs: {kind_totals or 'none recorded'}")
    lines.append(f"- revision drift in: {drift_cases or 'none'}")
    lines.append(f"- runs with untyped assessments: {untyped_runs or 'none'}")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="Live battery: run, verify, clean, report.")
    ap.add_argument("--api-base", default="http://localhost:8080")
    ap.add_argument("--tenant", default=DEFAULT_TENANT)
    ap.add_argument("--app-id", default=APP_ID)
    ap.add_argument("--only", default="", help="Comma-separated case IDs, one run each")
    ap.add_argument("--repeat-each", type=int, default=1)
    ap.add_argument("--timeout", type=int, default=600)
    ap.add_argument("--report", default="")
    ap.add_argument("--no-cleanup-prior", action="store_true")
    ap.add_argument("--force-cleanup", action="store_true")
    args = ap.parse_args()

    tag = run_id()
    all_cases = [c for c in load_cases()]
    if args.only:
        wanted = {c.strip() for c in args.only.split(",") if c.strip()}
        plan = [(c, 1) for c in all_cases if c["id"] in wanted]
    else:
        live = [c for c in all_cases if c.get("live", True)]
        plan = [(c, 1) for c in live]
        for cid in REPEAT_SET:
            matches = [c for c in live if c["id"] == cid]
            plan.extend((c, 2) for c in matches)
        if args.repeat_each > 1:
            plan = [(c, r) for (c, _) in plan for r in range(1, args.repeat_each + 1)]
    print(f"plan: {[(c['id'], r) for c, r in plan]} (tag {tag})", flush=True)

    if not args.no_cleanup_prior:
        import asyncio

        from clean_battery import cleanup_scope

        async def _clean():
            dsn = db_dsn()
            if not dsn:
                print("cleanup: no IAP_DATABASE_URI, skipping")
                return 0
            return await cleanup_scope(
                dsn, args.tenant, args.app_id, RUN_TAG_PREFIX, args.force_cleanup
            )

        print(f"cleanup prior: {asyncio.run(_clean())} row(s) removed", flush=True)

    ensure_profile(args.api_base, args.tenant, args.app_id)

    records = []
    for case, rep in plan:
        print(f"--- {case['id']}#{rep}: {case['symptom'][:70]}", flush=True)
        rec = run_case(args.api_base, args.tenant, args.app_id, case, rep, args.timeout, tag)
        print(f"  {rec.get('verdict', '?')}: {rec.get('detail', '')[:100]}", flush=True)
        records.append(rec)

    meta = {
        "ts": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
        "api_base": args.api_base,
        "tenant": args.tenant,
        "app_id": args.app_id,
        "run_tag": tag,
        "git_sha": sh(["git", "rev-parse", "--short", "HEAD"]),
        "prompt_version": _prompt_version(),
        "llm": os.environ.get("IAP_LLM_PROVIDER", "") or os.environ.get("IAP_LLM_MODEL", ""),
    }
    if db_dsn():
        print("db analysis...", flush=True)
        analyze_db(db_dsn(), records)
    else:
        print("db analysis skipped (no IAP_DATABASE_URI)")
    report = build_report(meta, records)
    print()
    print(report, flush=True)
    out = args.report or str(ROOT / "reports" / f"live_battery_{tag}.md")
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(report, encoding="utf-8")
    print(f"\nreport written: {out}")

    bad = sum(1 for r in records if r.get("verdict") in ("FAIL", "PARTIAL"))
    return 0 if bad == 0 else 1


def _prompt_version() -> str:
    try:
        text = (ROOT / "src" / "investigation_agent_platform" / "application"
                / "investigation" / "context_compiler.py").read_text()
        m = re.search(r'REASONER_PROMPT_VERSION\s*=\s*"([^"]+)"', text)
        return m.group(1) if m else "unknown"
    except Exception:
        return "unknown"


if __name__ == "__main__":
    sys.exit(main())
