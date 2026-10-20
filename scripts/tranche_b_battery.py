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
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

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
        line = raw.split("| ", 1)[1] if ("| " in raw and raw.split("|", 1)[0].strip().isdigit()) else raw
        for name in names:
            if not name:
                continue
            for match in re.finditer(
                r"(?<![A-Za-z0-9_])" + re.escape(name) + r"\s*\(", line
            ):
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
    shown = any(_call_shown(callee, str(r.get("content_snippet") or "")) for r in rows)
    if callee and not shown:
        return (False, f"no cited row shows a call to '{callee}'")
    corpus = root + "\n" + "\n".join(_row_texts(r) for r in rows)
    for caller in expect.get("expect_any") or []:
        if caller.lower() not in corpus.lower():
            return (False, f"expected caller '{caller}' not grounded")
    return (True, "")


def _check_history(expect: dict, root: str, rows: list) -> tuple:
    want = expect.get("cited_path", "")
    if want and want.lower() not in root.lower():
        return (False, f"conclusion names no {want}")
    if not any(str(r.get("evidence_type", "") or "").upper() == "COMMIT_HISTORY" for r in rows):
        return (False, "no cited commit row")
    return (True, "")


def _check_values(expect: dict, root: str, rows: list) -> tuple:
    snippets = [str(r.get("content_snippet") or "") for r in rows]
    for value in expect.get("expect_values") or []:
        if value.lower() not in root.lower():
            return (False, f"value '{value}' not in conclusion")
        if not any(value.lower() in s.lower() for s in snippets):
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


def score_supported(case: dict, conclusion: dict, findings: dict, evidence: dict, hyps: dict) -> tuple:
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
            ok, reason = _check_no_failure(rows)
            if not ok:
                return ("PARTIAL", reason)
            return ("PASS", f"honest abstention ({cstatus})")
        if cstatus in ANSWERED_SET:
            return ("FAIL", "fabricated answer to unanswerable")
        return ("FAIL", f"status={cstatus or 'missing'}")
    if cstatus not in ANSWERED_SET:
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
    args = ap.parse_args()
    only = {c.strip() for c in args.only.split(",") if c.strip()}
    results = []
    skipped = []
    for case in CASES:
        if only and case["id"] not in only:
            continue
        if not case.get("live", True):
            skipped.append(case["id"])
            print(f"--- {case['id']}: SKIP (offline-only fixture)", flush=True)
            continue
        print(f"--- {case['id']}: {case['symptom'][:70]}", flush=True)
        st, data = api(
            args.api_base, args.tenant, "POST", "/api/v1/investigations",
            {"application_id": APP_ID, "problem_description": case["symptom"],
             "initial_context": case["context"]},
        )
        iid = data.get("id")
        if not iid:
            print(f"  CREATE FAILED: {st} {str(data)[:200]}")
            results.append((case["id"], "FAIL", "create failed", "-"))
            continue
        api(args.api_base, args.tenant, "POST", f"/api/v1/investigations/{iid}/start")
        try:
            inv = poll(args.api_base, args.tenant, iid, args.timeout)
        except TimeoutError as exc:
            print(f"  {exc}")
            results.append((case["id"], "FAIL", "timeout", iid))
            continue
        if inv.get("status") == "FAILED":
            results.append((case["id"], "FAIL", "run FAILED", iid))
            print(f"  run FAILED {iid}")
            continue
        verdict, detail = score_case(args.api_base, args.tenant, case, inv)
        print(f"  {verdict}: {detail} ({iid[:8]})")
        results.append((case["id"], verdict, detail, iid))
    print("\nTranche B scoreboard (tenant n-a, app spring-sample-service):")
    for cid, verdict, detail, iid in results:
        print(f"  {cid}: {verdict} — {detail} [{iid[:8] if len(iid) > 8 else iid}]")
    counts = {}
    for _, verdict, _, _ in results:
        counts[verdict] = counts.get(verdict, 0) + 1
    print(f"Totals: {counts} (skipped offline-only: {skipped})")
    print("Note: L6 (logs) / L7 (refdocs) N/A — code-only application by design.")
    # PARTIAL never counts as pass: a right answer on wrong evidence is a
    # grading failure, not a near-miss.
    bad = counts.get("FAIL", 0) + counts.get("PARTIAL", 0)
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
