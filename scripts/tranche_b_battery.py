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
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

TERMINAL = {"COMPLETED", "FAILED", "CANCELLED"}
APP_ID = "spring-sample-service"

CASES = [
    {
        "id": "L1",
        "symptom": "Where is SampleOrderFilter implemented?",
        "context": "Tranche B L1: symbol lookup must resolve to the filter class.",
        "expect_path": "SampleOrderFilter.java",
        "score": "location",
    },
    {
        "id": "L2",
        "symptom": "What calls the order service handler?",
        "context": "Tranche B L2: caller enumeration for OrderServiceHandler.",
        "expect_any": ["OrderQualifyHelper", "OrderServiceRestProxy"],
        "score": "mentions",
    },
    {
        "id": "L3",
        "symptom": "What changed in OrderServiceImpl.java recently?",
        "context": "Tranche B L3: commit history for the service implementation.",
        "expect_evidence_type": "COMMIT_HISTORY",
        "score": "evidence_type",
    },
    {
        "id": "L4",
        "symptom": (
            "I looked in service/src/main/java/com/sample/orders/"
            "filter/SampleOrderfilter.java and it does not exist — "
            "where is it really?"
        ),
        "context": "Tranche B L4: typo path (lowercase f) must correct.",
        "expect_path": "SampleOrderFilter.java",
        "score": "location",
    },
    {
        "id": "L5",
        "symptom": (
            "How does a null orderType affect order filtering? "
            "Which default applies and where?"
        ),
        "context": "Tranche B L5: multi-hop synthesis across the null-default chain.",
        "expect_any": ["STANDARD", "orderType", "LegacyCodeMapper"],
        "score": "mentions",
    },
    {
        "id": "L8",
        "symptom": "Where is the quantum module?",
        "context": "Tranche B L8: unanswerable — must abstain honestly.",
        "score": "abstention",
    },
]


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


def texts_of(*blobs: str) -> str:
    return "\n".join(blobs).lower()


def score_case(base: str, tenant: str, case: dict, inv: dict) -> tuple[str, str]:
    iid = inv["id"]
    _, conclusion = api(base, tenant, "GET", f"/api/v1/investigations/{iid}/conclusion")
    _, findings = api(base, tenant, "GET", f"/api/v1/investigations/{iid}/findings")
    _, evidence = api(base, tenant, "GET", f"/api/v1/investigations/{iid}/evidence?limit=100")
    _, hyps = api(base, tenant, "GET", f"/api/v1/investigations/{iid}/hypotheses")
    concl = conclusion.get("conclusion") or {}
    cstatus = concl.get("status", "")
    corpus = texts_of(
        json.dumps(findings), json.dumps(hyps), json.dumps(evidence),
        concl.get("root_cause") or "",
    )
    kind = case["score"]
    if kind == "location":
        want = case["expect_path"].lower()
        if cstatus == "ANSWERED" and want in corpus:
            return "PASS", f"ANSWERED citing {case['expect_path']}"
        if want in corpus:
            return "PARTIAL", "names correct file, not verified"
        return "FAIL", "correct file absent"
    if kind == "mentions":
        hits = [w for w in case["expect_any"] if w.lower() in corpus]
        if cstatus == "ANSWERED" and hits:
            return "PASS", f"ANSWERED, mentions {hits}"
        if hits:
            return "PARTIAL", f"mentions {hits}, unverified"
        return "FAIL", "no expected reference"
    if kind == "evidence_type":
        types = [e.get("evidence_type") for e in evidence.get("items", [])]
        if case["expect_evidence_type"] in types and cstatus == "ANSWERED":
            return "PASS", f"{case['expect_evidence_type']} banked + ANSWERED"
        if case["expect_evidence_type"] in types:
            return "PARTIAL", f"{case['expect_evidence_type']} banked, unverified"
        return "FAIL", f"no {case['expect_evidence_type']} rows"
    if kind == "abstention":
        if cstatus in ("INSUFFICIENT_EVIDENCE", "NO_ROOT_CAUSE_FOUND"):
            return "PASS", f"honest abstention ({cstatus})"
        if cstatus == "ANSWERED":
            return "FAIL", "fabricated answer to unanswerable"
        return "PARTIAL", f"status={cstatus or inv.get('status')}"
    return "FAIL", "unknown scorer"


def main() -> int:
    ap = argparse.ArgumentParser(description="Tranche B battery runner.")
    ap.add_argument("--api-base", default="http://localhost:8080")
    ap.add_argument("--tenant", default="n-a")
    ap.add_argument("--timeout", type=int, default=600)
    ap.add_argument("--only", default="", help="Comma-separated case IDs, e.g. L1,L4")
    args = ap.parse_args()
    only = {c.strip() for c in args.only.split(",") if c.strip()}
    results = []
    for case in CASES:
        if only and case["id"] not in only:
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
    print(f"Totals: {counts}")
    print("Note: L6 (logs) / L7 (refdocs) N/A — code-only application by design.")
    return 0 if counts.get("FAIL", 0) == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
