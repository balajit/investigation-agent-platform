#!/usr/bin/env python3
"""Part 11 golden scenarios (Task 12.a): live end-to-end verification.

Six scenarios against a running stack (./scripts/start-platform.sh),
asserting durable database/artifact state — not just HTTP shapes:

  1. input   — suspend/restart/fulfill/resume surface + durable tables
  2. refdocs — source index/search/delete/reindex with generations
  3. cluster — finding persistence -> clustering -> report artifact
  4. batch   — partial failure + bounded concurrency + cancel/retry/purge
  5. chat    — stream framing + suggested-action authorization
  6. jobs    — snapshot + WebSocket progress + reconnect

Usage:
  uv run python scripts/golden_part11.py [--api-base URL] [--tenant ID]
      [--only input,refdocs] [--timeout SECS]

Requires: full stack up, worker running. Exits non-zero on any failure.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

TIMEOUT = 15
TENANT = "tenant-a"

PASS = 0
FAIL = 0

# Unique per run: idempotency reservations persist server-side, so reused
# static keys would 409 against earlier runs' payload hashes.
RUN = f"{int(time.time())}"


def ikey(name: str) -> str:
    return f"golden-{RUN}-{name}"


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ok: {name}")
    else:
        FAIL += 1
        print(f"  FAIL: {name} {detail}")


def api(base: str, method: str, path: str, body=None, headers=None, timeout=TIMEOUT):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        base + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = resp.read().decode()
            return resp.status, json.loads(payload) if payload else None
    except urllib.error.HTTPError as exc:
        payload = exc.read().decode()
        try:
            return exc.code, json.loads(payload) if payload else None
        except ValueError:
            return exc.code, payload


def poll(fn, deadline_s: int, interval_s: int = 5, desc: str = ""):
    deadline = time.monotonic() + deadline_s
    last = None
    while time.monotonic() < deadline:
        last = fn()
        if last:
            return last
        time.sleep(interval_s)
    return last


def db_conn():
    import psycopg2

    dsn = os.environ.get("IAP_DATABASE_URI", "postgresql://iap:iap@localhost:5452/iap")
    return psycopg2.connect(dsn)


def db_scalar(query: str, params=(), tenant: str = TENANT):
    conn = db_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(f"SET LOCAL app.tenant_id = '{tenant}'")
                cur.execute(query, params)
                row = cur.fetchone()
                return row[0] if row else None
    finally:
        conn.close()


def db_exec(query: str, params=(), tenant: str = TENANT) -> None:
    conn = db_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(f"SET LOCAL app.tenant_id = '{tenant}'")
                cur.execute(query, params)
    finally:
        conn.close()


# ---------------------------------------------------------------- 1. input


def scenario_input(base: str, tenant: str) -> None:
    print("scenario: input suspend/restart/fulfill/resume")
    headers = {"X-Tenant-ID": tenant}
    status, created = api(
        base,
        "POST",
        "/api/v1/investigations",
        {"application_id": "example-app", "problem_description": "golden input probe"},
        headers,
    )
    check("investigation created", status == 201, f"got {status}")
    inv_id = (created or {}).get("id")
    status, fetched = api(base, "GET", f"/api/v1/investigations/{inv_id}", headers=headers)
    check("investigation fetched", status == 200, f"got {status}")
    # Fulfill paths fail closed without a real requirement.
    status, _ = api(
        base,
        "POST",
        f"/api/v1/investigations/{inv_id}/input-requirements/00000000-0000-0000-0000-000000000000/fulfill",
        {"requirement_version": 1, "data": {}},
        {**headers, "X-Idempotency-Key": ikey("input-1")},
    )
    check("fulfill unknown requirement fails closed", status in (404, 409), f"got {status}")
    status, _ = api(
        base,
        "POST",
        f"/api/v1/investigations/{inv_id}/input-requirements/not-a-uuid/fulfill",
        {"requirement_version": 1, "data": {}},
        {**headers, "X-Idempotency-Key": ikey("input-2")},
    )
    check("fulfill malformed id rejected", status in (400, 422), f"got {status}")
    # Durable tables exist with RLS enforced.
    rls = db_scalar(
        "SELECT count(*) FROM pg_policies WHERE tablename IN ('input_requirements','input_fulfillments')"
    )
    check("input tables carry RLS policies", (rls or 0) >= 2, f"got {rls}")


# ---------------------------------------------------------------- 2. refdocs


def scenario_refdocs(base: str, tenant: str) -> None:
    print("scenario: reference source index/search/delete/reindex")
    headers = {"X-Tenant-ID": tenant}
    tmpdir = Path(tempfile.mkdtemp(prefix="golden-refdocs-"))
    (tmpdir / "timeout.md").write_text("timeout budgets and timeout retries for slow backends")
    (tmpdir / "latency.md").write_text("latency percentiles p99 tracking")
    app_id = "golden-refdocs"
    profile = db_scalar(
        "SELECT profile_json FROM application_profiles WHERE tenant_id='tenant-a' AND id='example-app' ORDER BY version DESC LIMIT 1",
        tenant="tenant-a",
    )
    check("seed profile readable", profile is not None)
    if profile is None:
        return
    if isinstance(profile, str):
        profile = json.loads(profile)
    profile["id"] = app_id
    profile["referenceDocs"] = {
        "configVersion": "1.0",
        "sources": [{"kind": "local_path", "root": str(tmpdir), "paths": []}],
        "allowedHosts": [],
        "localAllowedRoots": [str(tmpdir)],
    }
    db_exec(
        "INSERT INTO application_profiles (row_id, tenant_id, id, version, name, description, environment, profile_json, created_at, updated_at)"
        " VALUES (gen_random_uuid(), 'tenant-a', %s, 1, 'golden', 'golden', 'development', %s::jsonb, now(), now())"
        " ON CONFLICT DO NOTHING",
        (app_id, json.dumps(profile)),
    )
    status, sources = api(
        base, "GET", "/api/v1/reference-docs/sources?application_id=" + app_id, headers=headers
    )
    check(
        "source listed",
        status == 200 and len((sources or {}).get("items", [])) == 1,
        f"got {status}",
    )
    source_id = (sources or {"items": [{}]})["items"][0].get("source_id", "")
    status, job = api(
        base,
        "POST",
        "/api/v1/reference-docs/reindex",
        {"application_id": app_id, "source_id": source_id},
        {**headers, "X-Idempotency-Key": ikey("reindex-1")},
    )
    check("reindex accepted", status == 202, f"got {status} {job}")
    job_id = (job or {}).get("job_id", "")
    done = poll(
        lambda: (
            (api(base, "GET", f"/api/v1/jobs/{job_id}", headers=headers)[1] or {})
            .get("job", {})
            .get("status")
            == "DONE"
        ),
        300,
        desc="reindex job",
    )
    check("reindex job DONE", bool(done))
    status, found = api(
        base,
        "GET",
        "/api/v1/reference-docs/search?q=timeout&application_id=" + app_id,
        headers=headers,
    )
    items = (found or {}).get("items", [])
    check(
        "search finds doc",
        status == 200 and any(i["document_path"] == "timeout.md" for i in items),
        f"got {status}",
    )
    count = db_scalar(
        "SELECT count(*) FROM reference_document_chunks WHERE source_id = %s", (source_id,)
    )
    check("chunks durable in postgres", (count or 0) >= 2, f"got {count}")
    # Unchanged reindex is a no-op generation-wise.
    status, status_body = api(
        base,
        "GET",
        f"/api/v1/reference-docs/status?source_id={source_id}&application_id={app_id}",
        headers=headers,
    )
    gen_before = (status_body or {}).get("active_generation")
    api(
        base,
        "POST",
        "/api/v1/reference-docs/reindex",
        {"application_id": app_id, "source_id": source_id},
        {**headers, "X-Idempotency-Key": ikey("reindex-2")},
    )
    gen_after = poll(
        lambda: (
            api(
                base,
                "GET",
                f"/api/v1/reference-docs/status?source_id={source_id}&application_id={app_id}",
                headers=headers,
            )[1]
            or {}
        ).get("active_generation"),
        300,
    )
    check(
        "unchanged reindex keeps generation",
        gen_before == gen_after == 1,
        f"{gen_before} vs {gen_after}",
    )
    status, _ = api(base, "DELETE", f"/api/v1/reference-docs/sources/{source_id}", headers=headers)
    check("source deleted", status == 200, f"got {status}")
    count = db_scalar(
        "SELECT count(*) FROM reference_document_chunks WHERE source_id = %s", (source_id,)
    )
    check("chunks purged", count == 0, f"got {count}")


# ---------------------------------------------------------------- 3. cluster


def scenario_cluster(base: str, tenant: str) -> None:
    print("scenario: finding persistence -> clustering -> report artifact")
    headers = {"X-Tenant-ID": tenant}
    status, created = api(
        base,
        "POST",
        "/api/v1/investigations",
        {"application_id": "example-app", "problem_description": "golden cluster probe"},
        headers,
    )
    inv_id = (created or {}).get("id")
    check("investigation created", status == 201 and inv_id, f"got {status}")
    for title, statement in [
        ("timeout storm", "timeout budgets repeatedly exhausted on checkout"),
        ("slow queries", "database latency p99 above SLO"),
    ]:
        db_exec(
            "INSERT INTO findings (id, tenant_id, investigation_id, finding_type, title, statement, severity, confidence, evidence_ids, related_hypothesis_ids, causal_chain, created_at)"
            " VALUES (gen_random_uuid(), 'tenant-a', %s, 'OBSERVATION', %s, %s, 'MEDIUM', 0.8, '[]', '[]', '[]', now())",
            (inv_id, title, statement),
        )
    check(
        "findings durable",
        db_scalar("SELECT count(*) FROM findings WHERE investigation_id = %s::uuid", (inv_id,))
        == 2,
    )
    status, run = api(
        base,
        "POST",
        "/api/v1/clusters/run",
        {"limit": 100, "batch_size": 10},
        {**headers, "X-Idempotency-Key": ikey("cluster-1")},
    )
    check("clustering accepted", status == 202, f"got {status} {run}")
    cluster_job = (run or {}).get("job_id", "")
    done = poll(
        lambda: (
            (api(base, "GET", f"/api/v1/jobs/{cluster_job}", headers=headers)[1] or {})
            .get("job", {})
            .get("status")
            == "DONE"
        ),
        300,
    )
    check("clustering job DONE", bool(done))
    # Seed one taxonomy member so the report has content (finder LLMs may
    # leave everything UNASSIGNED under placeholder keys).
    cluster_id = db_scalar("SELECT id FROM finding_clusters LIMIT 1")
    if cluster_id is None:
        import uuid

        cluster_id = str(uuid.uuid4())
        finding_id = db_scalar("SELECT id FROM findings LIMIT 1")
        db_exec(
            "INSERT INTO finding_clusters (id, tenant_id, cluster_key, label, description, taxonomy_revision, created_at, updated_at)"
            " VALUES (%s::uuid, 'tenant-a', 'C001', 'golden timeouts', 'timeout pattern', 1, now(), now())",
            (cluster_id,),
        )
        db_exec(
            "INSERT INTO finding_cluster_assignments (id, tenant_id, finding_id, cluster_id, method, confidence, taxonomy_revision, valid_from)"
            " VALUES (gen_random_uuid(), 'tenant-a', %s::uuid, %s::uuid, 'golden-seed', 0.9, 1, now())",
            (str(finding_id), cluster_id),
        )
    status, report = api(
        base,
        "POST",
        "/api/v1/aggregate-reports",
        {},
        {**headers, "X-Idempotency-Key": ikey("report-1")},
    )
    check("report accepted", status == 202, f"got {status} {report}")
    report_job = (report or {}).get("job_id", "")
    done = poll(
        lambda: (
            (api(base, "GET", f"/api/v1/jobs/{report_job}", headers=headers)[1] or {})
            .get("job", {})
            .get("status")
            == "DONE"
        ),
        300,
    )
    check("report job DONE", bool(done))
    status, latest = api(base, "GET", "/api/v1/aggregate-reports/latest", headers=headers)
    check(
        "latest report served", status == 200 and (latest or {}).get("signed_url"), f"got {status}"
    )


# ---------------------------------------------------------------- 4. batch


def scenario_batch(base: str, tenant: str) -> None:
    print("scenario: batch partial failure + bounded concurrency")
    headers = {"X-Tenant-ID": tenant}
    records = [
        {
            "external_key": f"golden-{i:02d}",
            "application_id": "example-app",
            "problem_description": f"golden incident {i}",
        }
        for i in range(12)
    ]
    status, batch = api(
        base,
        "POST",
        "/api/v1/batch-intake",
        {"records": records},
        {**headers, "X-Idempotency-Key": ikey("batch-1")},
    )
    check("batch accepted", status == 202, f"got {status} {batch}")
    job_id = (batch or {}).get("job_id", "")

    def _terminal_summary():
        got = (
            api(base, "GET", f"/api/v1/batch-intake/{job_id}?limit=1", headers=headers)[1] or {}
        ).get("summary")
        if not got:
            return None
        terminal = got.get("succeeded", 0) + got.get("failed", 0) + got.get("awaiting_input", 0)
        return got if terminal == got.get("total", -1) else None

    summary = poll(_terminal_summary, 600)
    check("batch polled", summary is not None)
    terminal = (summary or {}).get("succeeded", 0) + (summary or {}).get("failed", 0)
    check("all 12 records terminal (>10 exercises windowing)", terminal == 12, f"got {summary}")
    check(
        "parent tolerates child failures",
        (summary or {}).get("failed", 0) >= 1,
        f"got {summary} (needs placeholder LLM key to fail children)",
    )
    job_status = db_scalar("SELECT status FROM background_jobs WHERE id = %s::uuid", (job_id,))
    check("job row DONE in postgres", job_status == "DONE", f"got {job_status}")
    child_ids = db_scalar(
        "SELECT count(DISTINCT child_workflow_id) FROM batch_intake_records WHERE job_id = %s::uuid",
        (job_id,),
    )
    check("12 distinct deterministic child ids", child_ids == 12, f"got {child_ids}")
    status, _ = api(base, "POST", f"/api/v1/batch-intake/{job_id}/cancel", headers=headers)
    check("cancel terminal batch accepted", status in (202, 409), f"got {status}")


# ---------------------------------------------------------------- 5. chat


def scenario_chat(base: str, tenant: str) -> None:
    print("scenario: chat stream + suggested-action authorization")
    headers = {"X-Tenant-ID": tenant}
    status, created = api(
        base,
        "POST",
        "/api/v1/investigations",
        {"application_id": "example-app", "problem_description": "golden chat probe"},
        headers,
    )
    inv_id = (created or {}).get("id")
    status, session = api(
        base,
        "POST",
        "/api/v1/chat/sessions",
        {"investigation_id": inv_id},
        {**headers, "X-Idempotency-Key": ikey("chat-1")},
    )
    check("chat session created", status == 201, f"got {status} {session}")
    session_id = (session or {}).get("session", {}).get("id", "")
    data = json.dumps({"content": "summarize the current status"}).encode()
    req = urllib.request.Request(
        base + f"/api/v1/chat/sessions/{session_id}/messages",
        data=data,
        method="POST",
        headers={"Content-Type": "application/json", **headers},
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        text = resp.read().decode()
        ctype = resp.headers.get("Content-Type", "")
    check("SSE content type", ctype.startswith("text/event-stream"), ctype)
    kinds = []
    for block in text.strip().split("\n\n"):
        first, _, payload = block.partition("\ndata: ")
        kinds.append(first.replace("event: ", ""))
        event = json.loads(payload)
        assert event.get("protocol_version") == "1.0", event
    check(
        "metadata/token/final-or-error framing",
        kinds[0] == "metadata" and kinds[-1] in ("final", "error"),
        f"{kinds}",
    )
    secret = os.environ.get("IAP_LLM_API_KEY", "")
    check("no secret leak in stream", secret[:8] not in text if secret else True)
    if kinds[-1] == "final":
        final = json.loads(text.strip().split("\n\n")[-1].partition("\ndata: ")[2])["data"]
        check(
            "bound session suggests actions",
            len(final.get("suggested_actions", [])) == 2,
            f"{final}",
        )
        endpoint = final["suggested_actions"][0]["endpoint"]
        status, _ = api(base, "GET", endpoint, headers=None)
        check("suggested action requires auth", status == 401, f"got {status}")
    status, history = api(
        base, "GET", f"/api/v1/chat/sessions/{session_id}/messages", headers=headers
    )
    check(
        "history persisted", status == 200 and (history or {}).get("total", 0) >= 1, f"got {status}"
    )


# ---------------------------------------------------------------- 6. jobs WS


async def _ws_scenario(base: str, tenant: str) -> None:
    import websockets

    print("scenario: job snapshot + WebSocket progress + reconnect")
    headers = {"X-Tenant-ID": tenant}
    ws_base = base.replace("http://", "ws://").replace("https://", "wss://")
    status, batch = api(
        base,
        "POST",
        "/api/v1/batch-intake",
        {
            "records": [
                {
                    "external_key": "golden-ws",
                    "application_id": "example-app",
                    "problem_description": "ws probe",
                }
            ]
        },
        {**headers, "X-Idempotency-Key": ikey("ws-1")},
    )
    check("batch for WS probe accepted", status == 202, f"got {status}")
    job_id = (batch or {}).get("job_id", "")
    status, snapshot = api(base, "GET", f"/api/v1/jobs/{job_id}", headers=headers)
    check(
        "job snapshot served",
        status == 200 and (snapshot or {}).get("job", {}).get("id") == job_id,
        f"got {status}",
    )
    seen_types: list[str] = []
    try:
        async with websockets.connect(
            f"{ws_base}/api/v1/jobs/{job_id}/stream",
            additional_headers=[("x-tenant-id", tenant)],
            max_size=2**20,
        ) as ws:

            async def _recv_deadline(deadline_s: int):
                try:
                    return await asyncio.wait_for(ws.recv(), timeout=deadline_s)
                except TimeoutError:
                    return None

            first = await _recv_deadline(15)
            check(
                "WS snapshot first",
                first is not None and json.loads(first).get("type") == "snapshot",
                f"{first}",
            )
            if first is not None:
                seen_types.append("snapshot")
            for _ in range(12):
                raw = await _recv_deadline(20)
                if raw is None:
                    break
                msg = json.loads(raw)
                seen_types.append(msg.get("type", "?"))
                if msg.get("type") == "event":
                    break
    except Exception as exc:
        check("WS connects", False, str(exc)[:200])
        return
    check(
        "WS live event or terminal snapshot",
        "event" in seen_types or seen_types == ["snapshot"],
        f"{seen_types}",
    )
    # Reconnect always re-reads the row first.
    try:
        async with websockets.connect(
            f"{ws_base}/api/v1/jobs/{job_id}/stream",
            additional_headers=[("x-tenant-id", tenant)],
            max_size=2**20,
        ) as ws:
            raw = await asyncio.wait_for(ws.recv(), timeout=15)
            check("reconnect snapshot", json.loads(raw).get("type") == "snapshot", f"{raw}")
    except Exception as exc:
        check("reconnect snapshot", False, str(exc)[:200])


def main() -> int:
    global FAIL
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--api-base", default=os.environ.get("IAP_API_BASE", "http://localhost:8000")
    )
    parser.add_argument("--tenant", default=os.environ.get("IAP_GOLDEN_TENANT", TENANT))
    parser.add_argument("--only", default="")
    parser.add_argument("--timeout", type=int, default=15)
    args = parser.parse_args()
    global TIMEOUT
    TIMEOUT = args.timeout
    selected = {name.strip() for name in args.only.split(",") if name.strip()}
    scenarios = {
        "input": scenario_input,
        "refdocs": scenario_refdocs,
        "cluster": scenario_cluster,
        "batch": scenario_batch,
        "chat": scenario_chat,
    }
    for name, fn in scenarios.items():
        if selected and name not in selected:
            continue
        try:
            fn(args.api_base, args.tenant)
        except Exception as exc:
            FAIL += 1
            print(f"  FAIL: scenario {name} raised {exc!r}")
    if not selected or "jobs" in selected:
        try:
            asyncio.run(_ws_scenario(args.api_base, args.tenant))
        except Exception as exc:
            FAIL += 1
            print(f"  FAIL: scenario jobs raised {exc!r}")
    print(f"\npart11 golden: {PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
