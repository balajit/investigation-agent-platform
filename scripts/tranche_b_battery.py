#!/usr/bin/env python3
"""Tranche B calibration battery: spring-sample-service (tenant n-a).

Files one investigation per case against application_id=spring-sample-service,
polls to terminal via the API (no arbitrary sleeps), and scores each case
PASS / PARTIAL / FAIL with evidence links.

Usage:
  uv run python scripts/tranche_b_battery.py [--api-base URL] [--tenant ID]
      [--timeout SECS] [--only L1,L4]

Scorer-known expectations are pinned below (grounded in the synthetic
Spring checkout built by scripts/build-sample-checkout.py).
Cases are code-navigation only: log/refdoc legs are out of scope (marked N/A).
The profile additionally declares Oracle state, which cases never query.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from investigation_agent_platform.domain.finding.models import names_concrete_fetch_target

TERMINAL = {"COMPLETED", "FAILED", "CANCELLED"}
APP_ID = "spring-sample-service"

ANSWERED_SET = {"ANSWERED", "ROOT_CAUSE_ESTABLISHED", "ROOT_CAUSE_LIKELY"}
ABSTAIN_SET = {"INSUFFICIENT_EVIDENCE", "NO_ROOT_CAUSE_FOUND"}
# Rows carrying these miss reasons are infrastructure failures: they may
# appear in the corpus but must never support a conclusion.
FAILURE_REASONS = {"timeout", "auth_failed", "provider_unavailable", "internal_error"}


def load_cases() -> list[dict]:
    """Case definitions shared by the live battery and the offline grader tests.

    Single source of truth: tests/fixtures/battery_cases/*.json. Cases with
    live=false run only in the offline grader suite (they need fixtures the
    sample checkout cannot produce: multi-commit history, provider outages).
    """
    root = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "battery_cases"
    if not root.is_dir():
        raise FileNotFoundError(f"battery case fixtures missing: {root}")
    cases = []
    for path in sorted(root.glob("*.json")):
        with open(path, encoding="utf-8") as fh:
            cases.append(json.load(fh))
    return cases


CASES = load_cases()


def api(base: str, tenant: str, method: str, path: str, body=None, timeout=30):
    url = base + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"X-Tenant-ID": tenant, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode() or "{}")
        except Exception:
            return exc.code, {}


def poll(base: str, tenant: str, iid: str, timeout: int) -> dict:
    t0 = time.time()
    while True:
        _, data = api(base, tenant, "GET", f"/api/v1/investigations/{iid}")
        inv = data.get("investigation", {})
        if inv.get("status") in TERMINAL:
            return inv
        if time.time() - t0 > timeout:
            raise TimeoutError(f"{iid} not terminal after {timeout}s")
        time.sleep(15)


# Operator-side measurement stamp. The pin travels on the investigation
# entity (metadata["claim_binding_version"]) exactly as the repo contract
# requires; it is written directly by the operator between create and start
# rather than through a public API field, so no tenant can choose its own
# verification semantics.
CLAIM_BINDING_STAMP_SQL = (
    "UPDATE investigations "
    "SET metadata_json = coalesce(metadata_json, '{}'::jsonb) "
    "|| jsonb_build_object('claim_binding_version', $1::text) "
    "WHERE id = $2::uuid AND tenant_id = $3 AND status = 'CREATED'"
)


def known_claim_binding_versions() -> list[str]:
    from investigation_agent_platform.application.investigation.verification import (
        KNOWN_CLAIM_BINDING_VERSIONS,
    )

    return sorted(KNOWN_CLAIM_BINDING_VERSIONS)


def stamp_claim_binding_version(tenant: str, iid: str, version: str) -> bool:
    """Pin ``version`` on a CREATED investigation. True when exactly one row changed.

    Needs IAP_DATABASE_URI (operator environment; never printed). A run the
    disposition already closed (status != CREATED) is not stamped and
    returns False.
    """
    import asyncio

    import asyncpg

    uri = os.environ.get("IAP_DATABASE_URI", "")
    dsn = re.sub(r"^postgresql\+asyncpg://", "postgresql://", uri)
    if not dsn:
        raise RuntimeError("IAP_DATABASE_URI is required to stamp --claim-binding-version")

    async def _run() -> bool:
        conn = await asyncpg.connect(dsn)
        try:
            await conn.execute("SELECT set_config('app.tenant_id', $1, false)", tenant)
            result = await conn.execute(CLAIM_BINDING_STAMP_SQL, version, iid, tenant)
            return str(result).endswith(" 1")
        finally:
            await conn.close()

    return asyncio.run(_run())


def pinned_claim_binding_version(base: str, tenant: str, iid: str) -> str:
    """The claim-binding pin recorded on a graded investigation, or 'unknown'."""
    try:
        _, data = api(base, tenant, "GET", f"/api/v1/investigations/{iid}")
        meta = (data.get("investigation") or {}).get("metadata") or {}
        return str(meta.get("claim_binding_version") or "unmarked")
    except Exception:
        return "unknown"


def _rows_by_id(evidence: dict) -> dict:
    by_id = {}
    for row in (evidence or {}).get("items", []) or []:
        try:
            by_id[str(row.get("evidence_id", ""))] = row
        except Exception:
            continue
    return by_id


def _supporting_rows(conclusion: dict, hypotheses: dict, by_id: dict) -> list:
    ids: set = set((conclusion or {}).get("supporting_evidence_ids") or [])
    hyps_by_id = {}
    for hyp in (hypotheses or {}).get("items", []) or []:
        try:
            hyps_by_id[str(hyp.get("id", ""))] = hyp
        except Exception:
            continue
    for hid in (conclusion or {}).get("supporting_hypothesis_ids") or []:
        hyp = hyps_by_id.get(str(hid))
        if hyp:
            ids.update(hyp.get("supporting_evidence_ids") or [])
    return [by_id[i] for i in ids if i in by_id]


def _row_location(row: dict) -> tuple:
    prov = ((row or {}).get("provenance") or {}).get("source_location") or {}
    return (
        str(prov.get("file_path") or ""),
        prov.get("line_start"),
        prov.get("line_end"),
        str(prov.get("revision") or ""),
    )


def _path_matches(want: str, got: str) -> bool:
    w = (want or "").replace("\\", "/").strip().strip("/")
    g = (got or "").replace("\\", "/").strip().strip("/")
    if not w or not g:
        return False
    if w == g:
        return True
    if "/" in w:
        return g.endswith("/" + w)
    return g.rsplit("/", 1)[-1] == w


def _word_in(name: str, text: str) -> bool:
    if not name:
        return False
    return (
        re.search(r"(?<![A-Za-z0-9_])" + re.escape(name) + r"(?![A-Za-z0-9_])", text or "")
        is not None
    )


_DECLARATION_PREFIX = re.compile(
    r"(?:^|\s)(?:async\s+)?(?:def|class|function|func|fn|void|public|private|protected|static)\s+[\w.<>\[\], ]*$"
)


def _call_shown(callee: str, snippet: str) -> bool:
    """A call expression for callee appears (declarations excluded)."""
    names = {callee}
    for sep in ("::", ".", "#"):
        if sep in callee:
            names.add(callee.rsplit(sep, 1)[-1])
    for raw in (snippet or "").splitlines():
        line = (
            raw.split("| ", 1)[1]
            if ("| " in raw and raw.split("|", 1)[0].strip().isdigit())
            else raw
        )
        for name in names:
            if not name:
                continue
            for match in re.finditer(r"(?<![A-Za-z0-9_])" + re.escape(name) + r"\s*\(", line):
                if _DECLARATION_PREFIX.search(line[: match.start()]):
                    continue
                return True
    return False


def _row_texts(row: dict) -> str:
    parts = [str(row.get(k, "") or "") for k in ("title", "summary", "content_snippet", "source")]
    file_path, _, _, _ = _row_location(row)
    parts.append(file_path)
    return "\n".join(parts)


def _check_definition(expect: dict, root: str, rows: list) -> tuple:
    want = expect.get("cited_path", "")
    if want.lower() not in root.lower():
        return (False, f"conclusion names no {want}")
    symbol = expect.get("expect_symbol", "")
    for row in rows:
        file_path, line_start, _, _ = _row_location(row)
        if not _path_matches(want, file_path) or not isinstance(line_start, int):
            continue
        if symbol and not _word_in(symbol, str(row.get("content_snippet") or "")):
            continue
        return (True, "")
    return (False, f"no cited range read of {want}")


def _check_callers(expect: dict, root: str, rows: list) -> tuple:
    callee = expect.get("callee", "")
    callee_method = str(expect.get("callee_method") or "").strip()
    candidates = [str(c) for c in (expect.get("expect_any") or []) if str(c).strip()]
    grounded: list[str] = []
    for caller in candidates:
        for row in rows:
            file_path, line_start, line_end, revision = _row_location(row)
            snippet = str(row.get("content_snippet") or "")
            invocation = callee_method or callee
            caller_stem = caller.rsplit(".", 1)[-1]
            source_evidence = str(row.get("evidence_type", "") or "").upper() in {
                "SOURCE_CODE",
                "EXCEPTION_PATH",
            }
            caller_file_match = caller_stem.lower() in file_path.lower()
            if (
                caller_stem.lower() in _row_texts(row).lower()
                and callee
                and source_evidence
                and _call_shown(invocation, snippet)
                and isinstance(line_start, int)
                and isinstance(line_end, int)
                and bool(revision)
                and caller_file_match
            ):
                grounded.append(caller)
                break
    if not grounded:
        if not candidates:
            return (False, "no expected caller candidates configured")
        return (False, f"no expected caller has a cited range showing a call to '{callee}'")
    return (True, "")


def _check_history(expect: dict, root: str, rows: list) -> tuple:
    want = expect.get("cited_path", "")
    if want and want.lower() not in root.lower():
        return (False, f"conclusion names no {want}")
    commits = [
        row
        for row in rows
        if str(row.get("evidence_type", "") or "").upper() == "COMMIT_HISTORY"
        and (not want or _path_matches(want, _row_location(row)[0]))
    ]
    if not commits:
        return (False, "no cited commit row")
    if not any(
        (subject := str(row.get("summary") or "").strip())
        and any(
            len(token) >= 4 and token.lower() in root.lower()
            for token in re.findall(r"[A-Za-z0-9_-]+", subject)
        )
        for row in commits
    ):
        return (False, "conclusion does not describe a cited commit subject")
    return (True, "")


def _check_values(expect: dict, root: str, rows: list) -> tuple:
    # A cited row's identity (file path) is evidence of WHERE, same as its
    # snippet is evidence of what — method-anchored windows legitimately omit
    # the enclosing class declaration (live L5: the 6-19 window never names
    # LegacyCodeMapper in prose, but its path does). Same helper as the
    # caller check uses (_row_texts), so prose-against-row matching is one
    # rule everywhere instead of two.
    texts = [_row_texts(r) for r in rows]
    for value in expect.get("expect_values") or []:
        if value.lower() not in root.lower():
            return (False, f"value '{value}' not in conclusion")
        if not any(value.lower() in s.lower() for s in texts):
            return (False, f"value '{value}' in prose but no cited row shows it")
    return (True, "")


def _check_absence(expect: dict, root: str, rows: list) -> tuple:
    subject = expect.get("absent_subject", "")
    scope = str(expect.get("scope") or "").strip().lower()
    if subject and not _word_in(subject, root):
        return (False, f"conclusion states no '{subject}'")
    if not scope:
        return (False, "absence claim states no scope")
    for row in rows:
        attrs = row.get("attributes") or {}
        if not isinstance(attrs, dict):
            continue
        proof_scope = str(attrs.get("search_scope", "") or "").strip().lower()
        if attrs.get("match_count") != 0 or not proof_scope:
            continue
        if proof_scope == scope or scope in proof_scope:
            return (True, "")
    return (False, f"no scoped empty-search proof covers '{scope}'")


def _proven_absence_insufficient(expect: dict, root: str, hyps: dict, evidence: dict) -> tuple:
    """Proven-absence INSUFFICIENT: an abstention backed by a banked scoped
    empty-search proof passes (DES-S6: proven-absence INSUFFICIENT passes;
    unbound INSUFFICIENT routes into spread, never into a stop). Anything
    else stays a status FAIL — the proof, not the status alone, earns it."""
    if str(expect.get("claim") or "") != "absence":
        return (False, "not an absence claim")
    subject = expect.get("absent_subject", "")
    scope = str(expect.get("scope") or "").strip().lower()
    if not scope:
        return (False, "absence claim states no scope")
    if subject:
        # Abstention conclusions carry root_cause=None, so the subject may
        # live in the hypothesis statement that drove the scoped search.
        texts = [root]
        for hyp in (hyps or {}).get("items") or []:
            try:
                texts.append(str(hyp.get("statement") or ""))
            except Exception:
                continue
        if not any(_word_in(subject, t) for t in texts):
            return (False, f"abstention states no '{subject}'")
    for row in (evidence or {}).get("items", []) or []:
        attrs = row.get("attributes") or {}
        if not isinstance(attrs, dict):
            continue
        proof_scope = str(attrs.get("search_scope", "") or "").strip().lower()
        if attrs.get("match_count") != 0 or not proof_scope:
            continue
        if proof_scope == scope or scope in proof_scope:
            return (True, "proven-absence INSUFFICIENT (scoped empty-search proof banked)")
    return (False, f"no scoped empty-search proof covers '{scope}'")


def _check_no_failure(rows: list) -> tuple:
    for row in rows:
        attrs = row.get("attributes") or {}
        reason = str((attrs if isinstance(attrs, dict) else {}).get("miss_reason", "") or "")
        if reason in FAILURE_REASONS:
            return (False, f"cites failure row ({reason})")
    return (True, "")


def _check_revision(expect: dict, rows: list) -> tuple:
    incident = str(expect.get("incident_revision") or "").strip()
    if not incident:
        return (True, "")
    for row in rows:
        etype = str(row.get("evidence_type", "") or "").upper()
        if etype not in ("SOURCE_CODE", "EXCEPTION_PATH"):
            continue
        _, _, _, revision = _row_location(row)
        if revision != incident:
            return (False, f"cited row at {revision or 'unknown'} revision, want {incident}")
    return (True, "")


def _check_rival_exemption(case: dict, expect: dict, conclusion: dict, hypotheses: dict) -> tuple:
    if not expect.get("rival_untested_exempt"):
        return (True, "")
    statements = set()
    for hyp in (hypotheses or {}).get("items", []) or []:
        text = str(hyp.get("statement", "") or "").strip()
        if text:
            statements.add(text)
    if len(statements) < 2:
        return (False, "exemption case needs rival hypotheses present")
    limitations = " ".join(str(l) for l in (conclusion or {}).get("limitations") or [])
    if "RIVAL_UNTESTED" in limitations:
        return (False, "location answer demoted by untested rival (exemption violated)")
    return (True, "")


def score_supported(
    case: dict, conclusion: dict, findings: dict, evidence: dict, hyps: dict
) -> tuple:
    """Grade the conclusion, not the corpus.

    PASS requires the expected answer status plus every applicable
    dimension (citation, revision, range, rival, scope). A right answer on
    wrong evidence is PARTIAL with the failing dimension named; a wrong
    answer status is FAIL. Pure on dicts — offline-testable.
    """
    expect = case.get("expect", {}) or {}
    want_status = expect.get("status", "ANSWERED")
    concl = conclusion or {}
    cstatus = str(concl.get("status", "") or "")
    root = str(concl.get("root_cause") or "")
    if want_status == "ABSTAIN":
        if cstatus in ABSTAIN_SET:
            rows = _supporting_rows(concl, hyps, _rows_by_id(evidence))
            # Phase 4 (claim binding): opinion-probe dimension. The
            # hypothesis API items carry no assessments, so the check is
            # made on the conclusion surface the verify activity controls:
            # ABSTAIN + zero supporting rows (retrieval skipped) + the
            # NOT_CHECKABLE_SKIPPED limitation the conclude path emits.
            if str((expect or {}).get("claim", "") or "") == "not_checkable":
                if rows:
                    return ("PARTIAL", f"cites {len(rows)} supporting rows for an opinion claim")
                limitations = list((concl or {}).get("limitations") or [])
                if "NOT_CHECKABLE_SKIPPED" not in limitations:
                    return ("PARTIAL", "abstained without NOT_CHECKABLE_SKIPPED limitation")
                ok, reason = _check_no_failure(rows)
                if not ok:
                    return ("PARTIAL", reason)
                return ("PASS", f"honest abstention ({cstatus}), retrieval skipped")
            ok, reason = _check_no_failure(rows)
            if not ok:
                return ("PARTIAL", reason)
            return ("PASS", f"honest abstention ({cstatus})")
        if cstatus in ANSWERED_SET:
            return ("FAIL", "fabricated answer to unanswerable")
        return ("FAIL", f"status={cstatus or 'missing'}")
    if cstatus not in ANSWERED_SET:
        if cstatus in ABSTAIN_SET:
            ok, reason = _proven_absence_insufficient(expect, root, hyps, evidence)
            if ok:
                return ("PASS", reason)
        return ("FAIL", f"status={cstatus or 'missing'}, want answered")

    by_id = _rows_by_id(evidence)
    rows = _supporting_rows(concl, hyps, by_id)
    if not rows:
        return ("PARTIAL", "answered with no citable supporting rows")
    claim = expect.get("claim", "definition")
    checks = {
        "definition": lambda: _check_definition(expect, root, rows),
        "callers": lambda: _check_callers(expect, root, rows),
        "history": lambda: _check_history(expect, root, rows),
        "values": lambda: _check_values(expect, root, rows),
        "absence": lambda: _check_absence(expect, root, rows),
        "no_failure_citation": lambda: _check_no_failure(rows),
    }
    check = checks.get(claim)
    if check is None:
        return ("FAIL", f"unknown claim kind '{claim}'")
    ok, reason = check()
    if not ok:
        return ("PARTIAL", reason)
    ok, reason = _check_revision(expect, rows)
    if not ok:
        return ("PARTIAL", reason)
    ok, reason = _check_rival_exemption(case, expect, concl, hyps)
    if not ok:
        return ("PARTIAL", reason)
    ok, reason = _check_no_failure(rows)
    if not ok:
        return ("PARTIAL", reason)
    return ("PASS", "conclusion supported by cited evidence")


def _concrete_handoff(concl: dict) -> tuple[bool, str]:
    """True when an abstention carries a priced concrete next fetch or a ruled-out rival.

    Consumes the S7 handoff fields: ``handoff.next_fetch`` must name a
    retrieval action or file (the domain's single definition of concrete,
    ``names_concrete_fetch_target``) AND ``handoff.price`` must be a positive
    number (tool-call budget units). A concrete but unpriced fetch is not a
    priced handoff. Alternatively a non-empty ``eliminated_rivals`` record
    counts. State labels, the ``n/a`` placeholder and ``next_check`` template
    prose never count - the review (T2) found that counting them made every
    abstention a "handoff".
    """
    handoff = concl.get("handoff") or {}
    if not isinstance(handoff, dict):
        handoff = {}
    fetch = str(handoff.get("next_fetch") or "").strip()
    if fetch and fetch.lower() != "n/a":
        if not names_concrete_fetch_target(fetch):
            return (False, f"next_fetch is generic template text: {fetch[:60]}")
        price = handoff.get("price")
        if isinstance(price, bool) or not isinstance(price, int | float) or not price > 0:
            return (False, f"next_fetch is concrete but unpriced: {fetch[:60]}")
        return (True, f"next_fetch (price {price:g}): {fetch[:80]}")
    rivals = concl.get("eliminated_rivals") or []
    if isinstance(rivals, list) and rivals:
        return (True, f"{len(rivals)} eliminated rival(s)")
    return (False, "no next_fetch and no eliminated rivals")


def score_g1(conclusion: dict | None, evidence: dict | None) -> tuple[bool, str, str]:
    """G1: bound-or-priced-handoff, per case. Pure on API-shaped dicts.

    Returns (satisfied, kind, detail) where kind is ``bound``, ``handoff``,
    or ``miss``.

    - ``bound``: an answered conclusion with at least one cited
      ``supporting_evidence_ids`` entry that resolves to a returned evidence
      row, none of which is an infrastructure-failure row. Evidence reached
      only through a linked hypothesis does NOT count (T8: abstention prose
      and hypothesis links are unverified).
    - ``handoff``: an abstention carrying a concrete, priced next fetch or an
      elimination record (see ``_concrete_handoff``).
    - ``miss``: anything else, including a missing conclusion, an answered
      conclusion with no resolvable citation, and filler-only abstentions.
    """
    concl = conclusion or {}
    status = str(concl.get("status", "") or "")
    if not status:
        return (False, "miss", "no conclusion")
    if status in ANSWERED_SET:
        by_id = _rows_by_id(evidence or {})
        cited = [
            by_id[str(eid)]
            for eid in (concl.get("supporting_evidence_ids") or [])
            if str(eid) in by_id
        ]
        if not cited:
            return (False, "miss", "answered with no resolvable cited row")
        ok, reason = _check_no_failure(cited)
        if not ok:
            return (False, "miss", f"bound only by failure row ({reason})")
        return (True, "bound", f"{len(cited)} cited row(s)")
    if status in ABSTAIN_SET:
        ok, detail = _concrete_handoff(concl)
        return (ok, "handoff" if ok else "miss", detail)
    return (False, "miss", f"status={status}")


def g1_for_investigation(base: str, tenant: str, iid: str) -> tuple[bool, str, str]:
    """Fetch the conclusion and cited evidence for one run and score G1."""
    _, conclusion = api(base, tenant, "GET", f"/api/v1/investigations/{iid}/conclusion")
    _, evidence = api(base, tenant, "GET", f"/api/v1/investigations/{iid}/evidence?limit=100")
    return score_g1(conclusion.get("conclusion") or {}, evidence)


def g1_summary(g1_rows: list[tuple[str, bool, str, str]]) -> dict:
    """Aggregate per-case G1 rows (case_id, ok, kind, detail) into a rate."""
    total = len(g1_rows)
    kinds: dict[str, int] = {}
    for _, _, kind, _ in g1_rows:
        kinds[kind] = kinds.get(kind, 0) + 1
    satisfied = sum(1 for _, ok, _, _ in g1_rows if ok)
    return {
        "satisfied": satisfied,
        "total": total,
        "rate": (satisfied / total) if total else 0.0,
        "kinds": kinds,
    }


def _follow_primary(base: str, tenant: str, case: dict, inv: dict, timeout: int) -> tuple | None:
    """Grade a CANCELLED duplicate via its primary (P0 create-race fix).

    Returns (primary_id, verdict, detail) or None when the close names no
    followable primary. Mirrors live_battery._cached_primary for the
    serve_cached/duplicate actions: duplicate-of-active primaries are
    polled to terminal first, then scored.
    """
    meta = (inv.get("metadata") or {}) if isinstance(inv, dict) else {}
    disp = meta.get("disposition") or {}
    action = disp.get("action")
    if action not in ("serve_cached", "duplicate"):
        return None
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
            return None
        pid = match.group(0)
    if action == "duplicate":
        try:
            poll(base, tenant, pid, timeout)
        except Exception:
            return None
    verdict, detail = score_case(base, tenant, case, {"id": pid})
    return pid, verdict, detail


def score_case(base: str, tenant: str, case: dict, inv: dict) -> tuple[str, str]:
    iid = inv["id"]
    _, conclusion = api(base, tenant, "GET", f"/api/v1/investigations/{iid}/conclusion")
    _, findings = api(base, tenant, "GET", f"/api/v1/investigations/{iid}/findings")
    _, evidence = api(base, tenant, "GET", f"/api/v1/investigations/{iid}/evidence?limit=100")
    _, hyps = api(base, tenant, "GET", f"/api/v1/investigations/{iid}/hypotheses")
    concl = conclusion.get("conclusion") or {}
    return score_supported(case, concl, findings, evidence, hyps)


def main() -> int:
    ap = argparse.ArgumentParser(description="Tranche B battery runner.")
    ap.add_argument("--api-base", default="http://localhost:8080")
    ap.add_argument("--tenant", default="n-a")
    ap.add_argument("--timeout", type=int, default=600)
    ap.add_argument("--only", default="", help="Comma-separated case IDs, e.g. L1,L4")
    ap.add_argument(
        "--claim-binding-version",
        default=None,
        choices=known_claim_binding_versions(),
        help=(
            "Pin this claim-binding version on each new investigation before it starts "
            "(operator DB stamp; needs IAP_DATABASE_URI). Default: leave the server default."
        ),
    )
    args = ap.parse_args()
    only = {c.strip() for c in args.only.split(",") if c.strip()}
    results = []
    skipped = []
    g1_rows: list[tuple[str, bool, str, str]] = []
    # case id -> (graded investigation id, recorded claim-binding pin)
    pins: dict[str, tuple[str, str]] = {}
    for case in CASES:
        if only and case["id"] not in only:
            continue
        if not case.get("live", True):
            skipped.append(case["id"])
            print(f"--- {case['id']}: SKIP (offline-only fixture)", flush=True)
            continue
        print(f"--- {case['id']}: {case['symptom'][:70]}", flush=True)
        st, data = api(
            args.api_base,
            args.tenant,
            "POST",
            "/api/v1/investigations",
            {
                "application_id": APP_ID,
                "problem_description": case["symptom"],
                "initial_context": case["context"],
            },
        )
        iid = data.get("id")
        if not iid:
            print(f"  CREATE FAILED: {st} {str(data)[:200]}")
            results.append((case["id"], "FAIL", "create failed", "-"))
            g1_rows.append((case["id"], False, "miss", "create failed"))
            continue
        if args.claim_binding_version:
            stamped = stamp_claim_binding_version(args.tenant, iid, args.claim_binding_version)
            print(f"  pin {args.claim_binding_version}: {'stamped' if stamped else 'NOT stamped'}")
        api(args.api_base, args.tenant, "POST", f"/api/v1/investigations/{iid}/start")
        try:
            inv = poll(args.api_base, args.tenant, iid, args.timeout)
        except TimeoutError as exc:
            print(f"  {exc}")
            results.append((case["id"], "FAIL", "timeout", iid))
            g1_rows.append((case["id"], False, "miss", "timeout"))
            continue
        if inv.get("status") == "CANCELLED":
            # P0 create-race fix: a duplicate-of-active close must follow
            # the primary and grade its answer, not fail as "status=missing".
            followed = _follow_primary(args.api_base, args.tenant, case, inv, args.timeout)
            if followed is not None:
                pid, verdict, detail = followed
                print(f"  {verdict}: followed primary {pid[:8]}: {detail} ({iid[:8]})")
                results.append((case["id"], verdict, detail, iid))
                pins[case["id"]] = (
                    pid,
                    pinned_claim_binding_version(args.api_base, args.tenant, pid),
                )
                g1_ok, g1_kind, g1_detail = g1_for_investigation(args.api_base, args.tenant, pid)
                g1_rows.append((case["id"], g1_ok, g1_kind, g1_detail))
                continue
            results.append((case["id"], "FAIL", "run CANCELLED, no followable primary", iid))
            g1_rows.append((case["id"], False, "miss", "run CANCELLED, no followable primary"))
            print(f"  run CANCELLED, no followable primary {iid}")
            continue
        if inv.get("status") == "FAILED":
            results.append((case["id"], "FAIL", "run FAILED", iid))
            g1_rows.append((case["id"], False, "miss", "run FAILED"))
            print(f"  run FAILED {iid}")
            continue
        verdict, detail = score_case(args.api_base, args.tenant, case, inv)
        print(f"  {verdict}: {detail} ({iid[:8]})")
        results.append((case["id"], verdict, detail, iid))
        pins[case["id"]] = (
            iid,
            pinned_claim_binding_version(args.api_base, args.tenant, iid),
        )
        g1_ok, g1_kind, g1_detail = g1_for_investigation(args.api_base, args.tenant, iid)
        g1_rows.append((case["id"], g1_ok, g1_kind, g1_detail))
    print("\nTranche B scoreboard (tenant n-a, app spring-sample-service):")
    for cid, verdict, detail, iid in results:
        print(f"  {cid}: {verdict} — {detail} [{iid[:8] if len(iid) > 8 else iid}]")
    counts = {}
    for _, verdict, _, _ in results:
        counts[verdict] = counts.get(verdict, 0) + 1
    print(f"Totals: {counts} (skipped offline-only: {skipped})")
    g1 = g1_summary(g1_rows)
    print(
        f"G1 (bound-or-concrete-handoff): {g1['satisfied']}/{g1['total']} "
        f"= {g1['rate']:.0%}  kinds={g1['kinds']}"
    )
    for cid, ok, kind, detail in g1_rows:
        print(f"  G1 {cid}: {'OK ' if ok else 'MISS'} [{kind}] {detail}")
    if pins:
        print("Claim-binding pin of each GRADED investigation:")
        for cid, (gid, pin) in pins.items():
            flag = ""
            if args.claim_binding_version and pin != args.claim_binding_version:
                flag = f"  <-- MISMATCH (wanted {args.claim_binding_version}; run was reused or not stamped)"
            print(f"  {cid}: {pin} [{gid[:8]}]{flag}")
    print("Note: L6 (logs) / L7 (refdocs) N/A — code-only application by design.")
    # PARTIAL never counts as pass: a right answer on wrong evidence is a
    # grading failure, not a near-miss.
    bad = counts.get("FAIL", 0) + counts.get("PARTIAL", 0)
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
