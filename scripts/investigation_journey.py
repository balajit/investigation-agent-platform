#!/usr/bin/env python3
"""Investigation journey driver: create -> run -> COMPLETED, verifying every stage.

Drives ONE investigation through the whole lifecycle against the LIVE server
and asserts each stage, including the persisted rows behind it:

  Stage 0 — Fixtures & telemetry priming (test setup, not the journey)
  Stage 1 — Intake & admission (external API surface)
  Stage 2 — Dispatch & control plane (workflow verbs + signals)
  Stage 3 — Evidence & reasoning loop (worker activities surfaced via API)
  Stage 4 — Closure & conclusion (terminal state + gate approval)
  Stage 5 — Post-closure conversation & discovery
  Stage 6 — Persistence audit (DB cross-check of every stage)

Step numbers (1.1, 2.1, 3.4, ...) are stable labels only; the names carry the
meaning and mirror the internal/external flows (router verbs, workflow
signals, activity phases, repository writes).

KEY CONSTRAINT: every state change goes through service invocation (HTTP).
Direct database access is READ-ONLY and assertion-only (Stage 6 and a few
targeted row-existence checks). Elasticsearch writes happen only in Stage 0
to prime provider telemetry the workflow queries through the gateway.

Batch intake is excluded by design (no /batch-intake calls anywhere).

Usage:
    uv run python scripts/investigation_journey.py [--api-base URL] [--tenant ID]
        [--elastic-url URL] [--db-uri URI] [--journey-timeout SECS]

Exit 0 + JSON summary when every non-skipped check passes, exit 1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field


# --------------------------------------------------------------------------
# Check model
# --------------------------------------------------------------------------


@dataclass
class Check:
    number: str
    name: str
    status: str  # PASS | FAIL | SKIP
    detail: str = ""
    latency_ms: int = 0


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    def add(self, number: str, name: str, status: str, detail: str = "", latency_ms: int = 0) -> None:
        self.checks.append(Check(number, name, status, detail, latency_ms))

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if c.status == "FAIL"]

    def to_dict(self) -> dict:
        return {
            "overall": "PASS" if not self.failed else "FAIL",
            "checks": [
                {
                    "number": c.number,
                    "name": c.name,
                    "status": c.status,
                    "detail": c.detail,
                    "latency_ms": c.latency_ms,
                }
                for c in self.checks
            ],
        }


class JourneyFailure(Exception):
    """A failed assertion: recorded, never raised past the step runner."""


class JourneySkip(Exception):
    """A conditional step whose precondition did not occur (recorded as SKIP)."""


# --------------------------------------------------------------------------
# Driver context: HTTP service invocation + read-only DB/Elastic probes
# --------------------------------------------------------------------------


class Ctx:
    def __init__(self, args: argparse.Namespace) -> None:
        self.api_base = args.api_base.rstrip("/")
        self.tenant = args.tenant
        self.app_id = args.app_id
        self.elastic_url = args.elastic_url.rstrip("/")
        self.db_uri = args.db_uri
        self.timeout = float(args.request_timeout)
        self.run_id = args.run_id
        self.headers = {"Content-Type": "application/json"}
        self.state: dict = {}  # journey-scoped values (iid, phases, job ids...)

    def _headers(self, idempotency: bool = False) -> dict:
        h = dict(self.headers)
        h["X-Tenant-ID"] = self.tenant
        if idempotency:
            h["X-Idempotency-Key"] = f"journey-{uuid.uuid4().hex[:12]}"
        return h

    def api(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        tenant: str | None = None,
        idempotency: bool = False,
        query: dict | None = None,
    ) -> tuple[int, object]:
        """Service invocation. Returns (status, parsed JSON or raw text)."""
        url = self.api_base + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        headers = self._headers(idempotency)
        if tenant is not None:
            headers["X-Tenant-ID"] = tenant
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode() if body is not None else None,
            method=method,
            headers=headers,
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = resp.read().decode()
                return resp.status, json.loads(payload) if payload else None
        except urllib.error.HTTPError as exc:
            payload = exc.read().decode()
            try:
                return exc.code, json.loads(payload) if payload else None
            except (json.JSONDecodeError, ValueError):
                return exc.code, payload
        except Exception as exc:  # transport failure: surfaced as status -1
            return -1, f"{type(exc).__name__}: {exc}"

    def db(self, query: str, params: tuple = ()) -> list[tuple]:
        """READ-ONLY persistence probe. No writes ever go through this path."""
        import psycopg2

        conn = psycopg2.connect(self.db_uri, connect_timeout=10)
        try:
            with conn.cursor() as cur:
                cur.execute(query, params)
                return cur.fetchall() if cur.description else []
        finally:
            conn.close()

    def db_scalar(self, query: str, params: tuple = ()) -> object:
        rows = self.db(query, params)
        return rows[0][0] if rows else None

    def elastic(self, method: str, path: str, body: dict | None = None) -> tuple[int, object]:
        """Elasticsearch provider probe (Stage 0 priming + read assertions)."""
        req = urllib.request.Request(
            self.elastic_url + path,
            data=json.dumps(body).encode() if body is not None else None,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = resp.read().decode()
                return resp.status, json.loads(payload) if payload else None
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode()[:300]


# --------------------------------------------------------------------------
# Step runner
# --------------------------------------------------------------------------


def run_step(report: Report, number: str, name: str, fn, ctx: Ctx) -> None:
    started = time.monotonic()
    try:
        detail = fn(ctx) or ""
        latency = int((time.monotonic() - started) * 1000)
        report.add(number, name, "PASS", str(detail)[:300], latency)
        print(f"  PASS  {number} {name}")
    except JourneySkip as exc:
        report.add(number, name, "SKIP", str(exc)[:300])
        print(f"  SKIP  {number} {name} -- {exc}")
    except JourneyFailure as exc:
        report.add(number, name, "FAIL", str(exc)[:500])
        print(f"  FAIL  {number} {name} -- {exc}")
    except Exception as exc:  # driver bug or transport loss: fail loudly
        report.add(number, name, "FAIL", f"{type(exc).__name__}: {exc}"[:500])
        print(f"  FAIL  {number} {name} -- {type(exc).__name__}: {exc}")


def require(cond: bool, message: str) -> None:
    if not cond:
        raise JourneyFailure(message)


# --------------------------------------------------------------------------
# Stage 0 — Fixtures & telemetry priming
# --------------------------------------------------------------------------


def s0_1_telemetry_primed(ctx: Ctx) -> str:
    """0.1 Prime provider telemetry (elastic data-stream docs for the run).

    Fixture setup, not the journey: the SEARCH_LOGS provider path needs
    queryable documents. Every doc shares core vocabulary so any reasonable
    model keyword set matches; 3+ services give cross-layer corroboration.
    """
    docs = [
        ("checkout-api", "ERROR", "checkout error 5xx spike POST /checkout 503 after canary deploy v2.14.3"),
        ("checkout-api", "ERROR", "checkout error rate 4.1 percent payment auth timeout suspected"),
        ("payments-auth", "ERROR", "checkout error payment auth timeout 3000ms retry storm canary"),
        ("payments-auth", "ERROR", "checkout error auth latency p99 exception token exchange failing"),
        ("order-ledger", "ERROR", "checkout error ledger write timeout upstream payment auth slow"),
        ("gateway-edge", "ERROR", "checkout error edge 503 spiking payment checkout flow"),
        ("checkout-api", "INFO", "checkout error deploy marker canary monitoring latency"),
        ("payments-auth", "INFO", "checkout error retry budget exhausted auth spike"),
        ("order-ledger", "INFO", "checkout error reconciliation lag errors spike"),
        ("checkout-api", "ERROR", "checkout error checkout failed timeout auth service slow"),
    ]
    import datetime

    now = datetime.datetime.now(datetime.timezone.utc)
    index = f"logs-{ctx.tenant}-app-production-journey"
    for i, (svc, lvl, msg) in enumerate(docs):
        doc = {
            "@timestamp": (now - datetime.timedelta(minutes=30 - i * 2)).isoformat(),
            "message": f"{msg} run {ctx.run_id}",
            "service.name": svc,
            "log.level": lvl,
            "labels.trace_id": f"journey-{i:03d}",
            "labels.session_id": ctx.run_id,
        }
        status, _ = ctx.elastic("POST", f"/{index}/_doc", doc)
        require(status in (200, 201), f"index doc {i}: HTTP {status}")
    ctx.elastic("POST", f"/{index}/_refresh", {})
    deadline = time.monotonic() + 60
    count = 0
    while time.monotonic() < deadline:
        status, data = ctx.elastic("POST", f"/{index}/_count", {})
        count = (data.get("count", 0) if isinstance(data, dict) else 0) or 0
        if status == 200 and count >= len(docs):
            break
        time.sleep(3)
    require(count >= len(docs), f"doc count after 60s: {count}")
    ctx.state["telemetry_index"] = index
    return f"{count} docs in {index}"


# --------------------------------------------------------------------------
# Stage 1 — Intake & admission
# --------------------------------------------------------------------------


def s1_1_health_gates(ctx: Ctx) -> str:
    """1.1 Liveness & readiness gates (external health surface)."""
    status, data = ctx.api("GET", "/api/v1/health/live")
    require(status == 200 and data.get("status") == "UP", f"live: {status} {data}")
    status, data = ctx.api("GET", "/api/v1/health/ready")
    require(status == 200 and data.get("status") == "READY", f"ready: {status} {data}")
    return f"live UP, ready READY ({list((data.get('dependencies') or {}))})"


def s1_2_profile_present(ctx: Ctx) -> str:
    """1.2 Application profile resolves for tenant+app (API + persisted row)."""
    status, data = ctx.api("GET", "/api/v1/profiles")
    require(status == 200 and data["total"] >= 1, f"profiles: {status} {data}")
    apps = [i.get("id") or i.get("application_id") for i in data["items"]]
    require(ctx.app_id in apps, f"app {ctx.app_id} not in {apps}")
    n = ctx.db_scalar(
        "SELECT count(*) FROM application_profiles WHERE tenant_id = %s AND id = %s",
        (ctx.tenant, ctx.app_id),
    )
    require(n and n >= 1, "profile row missing in application_profiles")
    return f"app {ctx.app_id} listed + persisted"


def s1_3_create_investigation(ctx: Ctx) -> str:
    """1.3 Create investigation (intake service invocation) + persisted row."""
    status, data = ctx.api(
        "POST",
        "/api/v1/investigations",
        {
            "application_id": ctx.app_id,
            "problem_description": (
                "checkout error 5xx spike after canary deploy, payment auth timeouts "
                f"(journey {ctx.run_id})"
            ),
            "session_id": f"journey-{ctx.run_id}",
            "priority": "NORMAL",
            "requested_by": "journey-driver",
        },
        idempotency=True,
    )
    require(status == 201, f"create: HTTP {status} {data}")
    for key in ("id", "status", "tenant_id", "investigation"):
        require(key in data, f"create response missing {key}")
    require(data["status"] == "CREATED", f"initial status {data['status']}")
    ctx.state["iid"] = data["id"]
    n = ctx.db_scalar(
        "SELECT count(*) FROM investigations WHERE id = %s AND tenant_id = %s",
        (data["id"], ctx.tenant),
    )
    require(n == 1, "investigations row missing after create")
    return f"id={data['id'][:8]} status=CREATED persisted"


def s1_4_create_validation(ctx: Ctx) -> str:
    """1.4 Malformed intake is rejected (422, fail-closed validation)."""
    status, data = ctx.api(
        "POST", "/api/v1/investigations", {"application_id": ctx.app_id}, idempotency=True
    )
    require(status == 422, f"expected 422, got {status} {str(data)[:150]}")
    return "422 on missing problem_description"


def s1_5_detail_readback(ctx: Ctx) -> str:
    """1.5 Detail readback carries investigation + pending_requirements[]."""
    status, data = ctx.api("GET", f"/api/v1/investigations/{ctx.state['iid']}")
    require(status == 200, f"detail: HTTP {status}")
    require("investigation" in data and isinstance(data.get("pending_requirements"), list),
            "detail envelope missing investigation/pending_requirements")
    return f"status={data['investigation']['status']} pending={len(data['pending_requirements'])}"


def s1_6_tenant_isolation(ctx: Ctx) -> str:
    """1.6 Cross-tenant detail read is refused (403/404, never leaks)."""
    status, _ = ctx.api("GET", f"/api/v1/investigations/{ctx.state['iid']}", tenant="no-such-tenant")
    require(status in (403, 404), f"expected 403/404, got {status}")
    return f"isolated ({status})"


def s1_7_intake_principal_gate(ctx: Ctx) -> str:
    """1.7 Error intake without a service principal is refused (403)."""
    status, _ = ctx.api(
        "POST", "/api/v1/intake/errors",
        {"application_id": ctx.app_id, "error_class": "Checkout5xx",
         "problem_description": "journey guard"},
        idempotency=True,
    )
    require(status == 403, f"expected 403, got {status}")
    return "intake fail-closed without service principal (403)"


# --------------------------------------------------------------------------
# Stage 2 — Dispatch & control plane
# --------------------------------------------------------------------------


def s2_1_start_workflow(ctx: Ctx) -> str:
    """2.1 Start workflow (dispatch ack: RUNNING + workflow/correlation ids)."""
    status, data = ctx.api("POST", f"/api/v1/investigations/{ctx.state['iid']}/start")
    require(status == 202, f"start: HTTP {status} {data}")
    for key in ("investigation_id", "status", "workflow_id", "correlation_id"):
        require(key in data, f"dispatch ack missing {key}")
    require(data["status"] == "RUNNING", f"ack status {data['status']}")
    ctx.state["workflow_id"] = data["workflow_id"]
    return f"workflow {data['workflow_id'][:32]}..."


def s2_2_pipeline_phases(ctx: Ctx) -> str:
    """2.2 Pipeline phases advance past admission (poll CREATED exit)."""
    deadline = time.monotonic() + 180
    seen: list[str] = []
    while time.monotonic() < deadline:
        status, data = ctx.api("GET", f"/api/v1/investigations/{ctx.state['iid']}")
        require(status == 200, f"poll: HTTP {status}")
        current = data["investigation"]["status"]
        if current not in seen:
            seen.append(current)
        if current != "CREATED":
            break
        time.sleep(5)
    require(seen[-1] != "CREATED", "stuck in CREATED: dispatch did not engage")
    ctx.state["phases"] = seen
    return f"left CREATED via {' -> '.join(seen)}"


def s2_3_pause_resume(ctx: Ctx) -> str:
    """2.3 Pause halts the loop; resume re-engages it (control-plane round-trip)."""
    status, data = ctx.api("POST", f"/api/v1/investigations/{ctx.state['iid']}/pause")
    if status in (502, 503, 409):
        # Race guard: a fast loop may already be terminal (nothing to pause)
        # or the signal plane momentarily unavailable — distinguish by state.
        _, detail = ctx.api("GET", f"/api/v1/investigations/{ctx.state['iid']}")
        current = (detail.get("investigation") or {}).get("status", "?") if isinstance(detail, dict) else "?"
        if current in ("COMPLETED", "FAILED", "CANCELLED"):
            raise JourneySkip(f"loop already terminal ({current}) before pause landed: HTTP {status}")
        raise JourneyFailure(f"pause: HTTP {status} {str(data)[:200]} (state={current})")
    require(status == 202, f"pause: HTTP {status} {data}")
    status, data = ctx.api("GET", f"/api/v1/investigations/{ctx.state['iid']}")
    require(data["investigation"]["status"] == "PAUSED", f"not PAUSED: {data['investigation']['status']}")
    n = ctx.db_scalar(
        "SELECT count(*) FROM investigation_transitions "
        "WHERE investigation_id = %s AND to_status = 'PAUSED'",
        (ctx.state["iid"],),
    )
    require(n and n >= 1, "PAUSED edge missing in transition audit")
    status, data = ctx.api("POST", f"/api/v1/investigations/{ctx.state['iid']}/resume")
    require(status == 202, f"resume: HTTP {status} {data}")
    return "PAUSED (audited) -> resumed"


def s2_4_unknown_investigation(ctx: Ctx) -> str:
    """2.4 Unknown investigation id fails closed (400/404, never 500)."""
    status, _ = ctx.api("POST", "/api/v1/investigations/00000000-0000-0000-0000-000000000000/start")
    require(status in (400, 404), f"expected 400/404, got {status}")
    return f"fail-closed ({status})"


# --------------------------------------------------------------------------
# Stage 3 — Evidence & reasoning loop
# --------------------------------------------------------------------------


def _poll_subresource(ctx: Ctx, path: str, minimum: int, timeout_s: int, label: str) -> dict:
    deadline = time.monotonic() + timeout_s
    last: dict = {}
    while time.monotonic() < deadline:
        status, data = ctx.api("GET", path)
        require(status == 200, f"{label}: HTTP {status}")
        last = data
        if data.get("total", 0) >= minimum:
            return data
        # Terminal investigations accumulate nothing more: stop polling and
        # let the caller fail fast with the actual counts.
        try:
            _, detail = ctx.api("GET", f"/api/v1/investigations/{ctx.state['iid']}")
            if isinstance(detail, dict) and detail.get("investigation", {}).get("status") in (
                "COMPLETED", "FAILED", "CANCELLED",
            ):
                break
        except Exception:
            pass
        time.sleep(10)
    raise JourneyFailure(f"{label}: only {last.get('total', 0)}/{minimum} (terminal-reached early)")


def s3_1_evidence_accumulates(ctx: Ctx) -> str:
    """3.1 Evidence accumulates via gateway retrieval (provider -> repo -> API)."""
    data = _poll_subresource(
        ctx, f"/api/v1/investigations/{ctx.state['iid']}/evidence?limit=50",
        minimum=4, timeout_s=ctx.state["loop_timeout"], label="evidence",
    )
    item = data["items"][0]
    for key in ("evidence_id", "tenant_id", "investigation_id", "evidence_type",
                "provider", "source", "title", "summary", "fingerprint"):
        require(key in item, f"evidence item missing {key}")
    n = ctx.db_scalar(
        "SELECT count(*) FROM evidence WHERE investigation_id = %s AND tenant_id = %s",
        (ctx.state["iid"], ctx.tenant),
    )
    require(n and n >= 4, f"evidence rows in DB: {n}")
    return f"{data['total']} items (>=4 for verification math), {n} rows persisted"


def s3_2_timeline_records(ctx: Ctx) -> str:
    """3.2 Timeline records transitions + audit trail mirrors them (API + DB)."""
    status, data = ctx.api("GET", f"/api/v1/investigations/{ctx.state['iid']}/timeline?limit=100")
    require(status == 200 and data["total"] >= 2, f"timeline: {status} {str(data)[:150]}")
    n = ctx.db_scalar(
        "SELECT count(*) FROM timeline_events WHERE investigation_id = %s AND tenant_id = %s",
        (ctx.state["iid"], ctx.tenant),
    )
    require(n and n >= 1, "timeline_events rows missing")
    chain = ctx.db(
        "SELECT from_status, to_status FROM investigation_transitions "
        "WHERE investigation_id = %s ORDER BY timestamp",
        (ctx.state["iid"],),
    )
    require(chain and chain[0][0] == "CREATED", f"audit chain does not start at CREATED: {chain[:2]}")
    ctx.state["mid_chain"] = [(a, b) for a, b in chain]
    return f"timeline={data['total']} events, audit edges={len(chain)} from CREATED"


def s3_3_hypotheses_formulated(ctx: Ctx) -> str:
    """3.3 Reason-proposed hypotheses persist (loop output materialized)."""
    data = _poll_subresource(
        ctx, f"/api/v1/investigations/{ctx.state['iid']}/hypotheses?limit=50",
        minimum=1, timeout_s=ctx.state["loop_timeout"], label="hypotheses",
    )
    item = data["items"][0]
    require(item.get("statement"), "hypothesis missing statement")
    n = ctx.db_scalar(
        "SELECT count(*) FROM hypotheses WHERE investigation_id = %s AND tenant_id = %s",
        (ctx.state["iid"], ctx.tenant),
    )
    require(n and n >= 1, "hypotheses rows missing")
    return f"{data['total']} hypotheses persisted"


def s3_4_findings_recorded(ctx: Ctx) -> str:
    """3.4 Findings recorded (conclusion corpus takes shape)."""
    data = _poll_subresource(
        ctx, f"/api/v1/investigations/{ctx.state['iid']}/findings?offset=0&limit=50",
        minimum=1, timeout_s=ctx.state["loop_timeout"], label="findings",
    )
    n = ctx.db_scalar(
        "SELECT count(*) FROM findings WHERE investigation_id = %s AND tenant_id = %s",
        (ctx.state["iid"], ctx.tenant),
    )
    require(n and n >= 1, "findings rows missing")
    return f"{data['total']} findings persisted"


def s3_5_input_detour(ctx: Ctx) -> str:
    """3.5 Awaiting-input detour: fulfill requirement, workflow resumes (conditional)."""
    status, data = ctx.api("GET", f"/api/v1/investigations/{ctx.state['iid']}")
    require(status == 200, f"detail: HTTP {status}")
    pending = data.get("pending_requirements", [])
    if data["investigation"]["status"] != "AWAITING_INPUT" or not pending:
        raise JourneySkip("workflow never requested input (no AWAITING_INPUT state)")
    req = pending[0]
    schema = req.get("json_schema", {}) or {}
    props = (schema.get("properties") or {})
    fulfillment = {k: ("txn-journey-001" if "txn" in k.lower() or "id" in k.lower() else "journey-value")
                   for k in props} or {"answer": "journey-value"}
    status, data = ctx.api(
        "POST",
        f"/api/v1/investigations/{ctx.state['iid']}/input-requirements/{req['requirement_id']}/fulfill",
        {"requirement_version": req["requirement_version"], "data": fulfillment},
        idempotency=True,
    )
    require(status in (200, 202), f"fulfill: HTTP {status} {str(data)[:200]}")
    n = ctx.db_scalar(
        "SELECT count(*) FROM input_fulfillments WHERE requirement_id = %s",
        (req["requirement_id"],),
    )
    require(n and n >= 1, "fulfillment row missing")
    return f"fulfilled {req['requirement_id'][:8]}, audit row persisted"


def s3_6_knowledge_distilled(ctx: Ctx) -> str:
    """3.6 Knowledge envelope reads back three isolated lists (API shape)."""
    status, data = ctx.api("GET", f"/api/v1/investigations/{ctx.state['iid']}/knowledge")
    require(status == 200, f"knowledge: HTTP {status}")
    for key in ("artifacts", "sessions", "other_tenant_sessions"):
        require(isinstance(data.get(key), list), f"knowledge.{key} not a list")
    return (f"artifacts={len(data['artifacts'])} "
            f"sessions={len(data['sessions'])} others={len(data['other_tenant_sessions'])}")


# --------------------------------------------------------------------------
# Stage 4 — Closure & conclusion
# --------------------------------------------------------------------------


def s4_1_terminal_completed(ctx: Ctx, timeout_s: int) -> str:
    """4.1 Workflow reaches terminal COMPLETED (gate-approved, not merely stopped)."""
    deadline = time.monotonic() + timeout_s
    current = ""
    while time.monotonic() < deadline:
        status, data = ctx.api("GET", f"/api/v1/investigations/{ctx.state['iid']}")
        require(status == 200, f"poll: HTTP {status}")
        current = data["investigation"]["status"]
        if current in ("COMPLETED", "FAILED", "CANCELLED"):
            break
        time.sleep(15)
    require(current == "COMPLETED",
            f"terminal state is {current}, not COMPLETED (gate blockers or loop limit?)")
    return "COMPLETED"


def s4_2_conclusion_present(ctx: Ctx) -> str:
    """4.2 Conclusion + backing finding persisted (gate output materialized)."""
    status, data = ctx.api("GET", f"/api/v1/investigations/{ctx.state['iid']}/conclusion")
    require(status == 200, f"conclusion: HTTP {status}")
    require(isinstance(data.get("conclusion"), dict), "conclusion null on COMPLETED")
    require(data["conclusion"].get("root_cause"), "conclusion lacks root_cause")
    row = ctx.db(
        "SELECT root_cause, status FROM investigation_conclusions WHERE investigation_id = %s",
        (ctx.state["iid"],),
    )
    require(row and row[0][0], "investigation_conclusions row missing")
    n = ctx.db_scalar(
        "SELECT count(*) FROM findings WHERE investigation_id = %s", (ctx.state["iid"],)
    )
    require(n and n >= 1, "no findings behind the conclusion")
    return f"root_cause persisted ({row[0][1]}, {n} findings)"


def s4_3_audit_chain_complete(ctx: Ctx) -> str:
    """4.3 Transition audit spans the full journey CREATED -> COMPLETED."""
    chain = ctx.db(
        "SELECT from_status, to_status FROM investigation_transitions "
        "WHERE investigation_id = %s ORDER BY timestamp",
        (ctx.state["iid"],),
    )
    require(chain, "no transition audit rows")
    require(chain[0][0] == "CREATED", f"chain starts at {chain[0][0]}")
    require(chain[-1][1] == "COMPLETED", f"chain ends at {chain[-1][1]}")
    require(len(chain) >= 6, f"chain suspiciously short: {len(chain)} edges")
    phases = [b for _, b in chain]
    for expected in ("INVESTIGATING", "CONCLUDING", "COMPLETED"):
        require(expected in phases, f"{expected} missing from audit chain")
    return f"{len(chain)} audited edges CREATED -> COMPLETED"


# --------------------------------------------------------------------------
# Stage 5 — Post-closure conversation & discovery
# --------------------------------------------------------------------------


def s5_1_chat_session(ctx: Ctx) -> str:
    """5.1 Chat session binds the investigation; SSE vocabulary + version check."""
    status, data = ctx.api(
        "POST", "/api/v1/chat/sessions",
        {"investigation_id": ctx.state["iid"]}, idempotency=True,
    )
    require(status in (200, 201), f"chat session: HTTP {status} {str(data)[:200]}")
    session = data if isinstance(data.get("id"), str) else data.get("session", {})
    sid = session.get("id")
    require(sid, "no session id returned")
    ctx.state["chat_sid"] = sid
    n = ctx.db_scalar("SELECT count(*) FROM chat_sessions WHERE id = %s", (sid,))
    require(n == 1, "chat_sessions row missing")

    # Stream one turn; provider answers or fails closed with a typed frame.
    url = f"{ctx.api_base}/api/v1/chat/sessions/{sid}/messages"
    req = urllib.request.Request(
        url,
        data=json.dumps({"content": "summarize this investigation"}).encode(),
        method="POST",
        headers={**ctx._headers(), "Accept": "text/event-stream"},
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        raw = resp.read().decode()
    frames = []
    for chunk in raw.split("\n\n"):
        for line in chunk.split("\n"):
            if line.startswith("data:"):
                try:
                    frames.append(json.loads(line[5:].strip()))
                except (json.JSONDecodeError, ValueError):
                    pass
    require(frames, "no SSE frames received")
    for frame in frames:
        require(frame.get("event") in ("token", "metadata", "final", "error"),
                f"unknown SSE event {frame.get('event')}")
        require(frame.get("protocol_version") == "1.0",
                f"protocol mismatch {frame.get('protocol_version')}")
    kinds = sorted({f["event"] for f in frames})
    require("metadata" in kinds, "no metadata frame")
    require("final" in kinds or "error" in kinds, "stream never closed with final/error")
    return f"session {sid[:8]}, frames={kinds}"


def s5_2_knowledge_search(ctx: Ctx) -> str:
    """5.2 Knowledge search surfaces distilled artifacts (post-closure reuse)."""
    status, data = ctx.api(
        "GET", "/api/v1/knowledge/search",
        query={"application_id": ctx.app_id, "limit": 50},
    )
    require(status == 200, f"knowledge search: HTTP {status} {str(data)[:150]}")
    require(data.get("total", 0) >= 1, "no artifacts available for reuse")
    return f"{data['total']} reusable artifacts"


def s5_3_clustering_job(ctx: Ctx) -> str:
    """5.3 Clustering run enqueues a job; terminal job + readable taxonomy."""
    status, data = ctx.api(
        "POST", "/api/v1/clusters/run",
        {"application_id": ctx.app_id, "limit": 50}, idempotency=True,
    )
    require(status in (200, 202), f"clusters/run: HTTP {status} {str(data)[:200]}")
    job_id = (data.get("job") or {}).get("id") or data.get("job_id") or data.get("id")
    require(job_id, f"no job id in {str(data)[:200]}")
    deadline = time.monotonic() + 300
    final = ""
    while time.monotonic() < deadline:
        status, data = ctx.api("GET", f"/api/v1/jobs/{job_id}")
        require(status == 200, f"job poll: HTTP {status}")
        job = data.get("job", data)
        final = job.get("status")
        if final in ("DONE", "FAILED", "CANCELED", "CANCEL_REQUESTED"):
            break
        time.sleep(10)
    require(final in ("DONE", "FAILED", "CANCELED"),
            f"clustering job stuck at {final} (batch-intake excluded; this is the taxonomy worker)")
    status, data = ctx.api("GET", "/api/v1/clusters")
    require(status == 200 and "total" in data, f"taxonomy: HTTP {status}")
    return f"job {final}, taxonomy total={data['total']}"


def s5_4_negative_guards(ctx: Ctx) -> str:
    """5.4 Negative guards: approve on unknown investigation 404s; completed
    approval is honestly undeliverable; sacrificial cancel+retry 409."""
    status, _ = ctx.api(
        "POST", "/api/v1/investigations/00000000-0000-0000-0000-000000000000/approve-action",
        query={"action_id": "nope", "approved": True},
    )
    require(status == 404, f"approve-unknown-investigation: expected 404, got {status}")
    # The journey investigation is COMPLETED (closed execution): an approval
    # signal is undeliverable -> 502, or echoed -> 200. Either is honest;
    # anything else (e.g. 500, timeout) is a failure.
    status, data = ctx.api(
        "POST", f"/api/v1/investigations/{ctx.state['iid']}/approve-action",
        query={"action_id": "00000000-0000-0000-0000-000000000000", "approved": True},
    )
    require(status in (200, 502), f"approve-on-completed: expected 200/502, got {status}")
    if status == 200:
        require(data.get("action_id") == "00000000-0000-0000-0000-000000000000",
                "approve echo mismatched action_id")
    # Sacrificial investigation: full terminal on the side, no fallout.
    status, data = ctx.api(
        "POST", "/api/v1/investigations",
        {"application_id": ctx.app_id,
         "problem_description": f"sacrificial cancel probe {ctx.run_id}",
         "session_id": f"sac-{ctx.run_id}", "priority": "LOW",
         "requested_by": "journey-driver"},
        idempotency=True,
    )
    require(status == 201, f"sacrificial create: HTTP {status}")
    sac = data["id"]
    status, _ = ctx.api("POST", f"/api/v1/investigations/{sac}/cancel", query={"reason": "journey-guard"})
    require(status in (200, 202), f"sacrificial cancel: HTTP {status}")
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        _, detail = ctx.api("GET", f"/api/v1/investigations/{sac}")
        if detail["investigation"]["status"] == "CANCELLED":
            break
        time.sleep(5)
    else:
        raise JourneyFailure("sacrificial investigation never reached CANCELLED")
    status, _ = ctx.api("POST", f"/api/v1/investigations/{sac}/retry")
    require(status == 409, f"retry-on-cancelled: expected 409, got {status}")
    return "approve-unknown-inv 404; approve-on-completed honest; cancel -> CANCELLED; retry -> 409"


# --------------------------------------------------------------------------
# Stage 6 — Persistence audit
# --------------------------------------------------------------------------


def s6_1_row_level_audit(ctx: Ctx) -> str:
    """6.1 Row-level audit: every stage left its persisted trace (read-only)."""
    iid, tenant = ctx.state["iid"], ctx.tenant
    expectations = [
        ("investigations", "SELECT count(*) FROM investigations WHERE id=%s AND tenant_id=%s", (iid, tenant), 1),
        ("evidence", "SELECT count(*) FROM evidence WHERE investigation_id=%s AND tenant_id=%s", (iid, tenant), 4),
        ("timeline_events", "SELECT count(*) FROM timeline_events WHERE investigation_id=%s", (iid,), 1),
        ("hypotheses", "SELECT count(*) FROM hypotheses WHERE investigation_id=%s", (iid,), 1),
        ("findings", "SELECT count(*) FROM findings WHERE investigation_id=%s", (iid,), 1),
        ("investigation_conclusions", "SELECT count(*) FROM investigation_conclusions WHERE investigation_id=%s", (iid,), 1),
        ("investigation_transitions", "SELECT count(*) FROM investigation_transitions WHERE investigation_id=%s", (iid,), 6),
        ("knowledge_artifacts", "SELECT count(*) FROM knowledge_artifacts WHERE investigation_id=%s", (iid,), 1),
        # Sessions track error-report occurrences via POST /intake/errors
        # (service-principal auth). Direct creation (this journey, like the
        # UI) correctly yields zero sessions — assert the query path works
        # and the count is non-negative rather than demanding a row.
        ("investigation_sessions", "SELECT count(*) FROM investigation_sessions WHERE investigation_id=%s", (iid,), 0),
        ("chat_sessions", "SELECT count(*) FROM chat_sessions WHERE investigation_id=%s", (iid,), 1),
        ("chat_messages", "SELECT count(*) FROM chat_messages WHERE session_id=%s", (ctx.state.get("chat_sid"),), 1),
    ]
    missing = []
    for table, query, params, minimum in expectations:
        try:
            n = ctx.db_scalar(query, params) or 0
        except Exception as exc:
            raise JourneyFailure(f"audit query failed on {table}: {exc}")
        if n < minimum:
            missing.append(f"{table}: {n}/{minimum}")
    require(not missing, "under-populated tables: " + "; ".join(missing))
    return f"{len(expectations)} tables meet minimum row counts"


# --------------------------------------------------------------------------
# Stage map + main
# --------------------------------------------------------------------------


STAGES: list[tuple[str, str, list[tuple[str, str, object]]]] = [
    ("0", "Fixtures & telemetry priming", [
        ("0.1", "Prime provider telemetry", s0_1_telemetry_primed),
    ]),
    ("1", "Intake & admission", [
        ("1.1", "Liveness & readiness gates", s1_1_health_gates),
        ("1.2", "Application profile present", s1_2_profile_present),
        ("1.3", "Create investigation", s1_3_create_investigation),
        ("1.4", "Malformed intake rejected", s1_4_create_validation),
        ("1.5", "Detail readback envelope", s1_5_detail_readback),
        ("1.6", "Cross-tenant read refused", s1_6_tenant_isolation),
        ("1.7", "Intake principal gate", s1_7_intake_principal_gate),
    ]),
    ("2", "Dispatch & control plane", [
        ("2.1", "Start workflow dispatch", s2_1_start_workflow),
        ("2.2", "Pipeline phases advance", s2_2_pipeline_phases),
        ("2.3", "Pause/resume round-trip", s2_3_pause_resume),
        ("2.4", "Unknown investigation fail-closed", s2_4_unknown_investigation),
    ]),
    ("3", "Evidence & reasoning loop", [
        ("3.1", "Evidence accumulates", s3_1_evidence_accumulates),
        ("3.2", "Timeline + audit trail", s3_2_timeline_records),
        ("3.3", "Hypotheses formulated", s3_3_hypotheses_formulated),
        ("3.4", "Findings recorded", s3_4_findings_recorded),
        ("3.5", "Awaiting-input detour (conditional)", s3_5_input_detour),
        ("3.6", "Knowledge envelope shape", s3_6_knowledge_distilled),
    ]),
    ("4", "Closure & conclusion", [
        ("4.1", "Terminal COMPLETED", None),  # wired in main (needs timeout)
        ("4.2", "Conclusion materialized", s4_2_conclusion_present),
        ("4.3", "Audit chain CREATED->COMPLETED", s4_3_audit_chain_complete),
    ]),
    ("5", "Post-closure conversation & discovery", [
        ("5.1", "Chat session + SSE stream", s5_1_chat_session),
        ("5.2", "Knowledge search reuse", s5_2_knowledge_search),
        ("5.3", "Clustering job + taxonomy", s5_3_clustering_job),
        ("5.4", "Negative guards", s5_4_negative_guards),
    ]),
    ("6", "Persistence audit", [
        ("6.1", "Row-level audit", s6_1_row_level_audit),
    ]),
]


def main() -> int:
    parser = argparse.ArgumentParser(description="Investigation journey driver (live server).")
    parser.add_argument("--api-base", default="http://127.0.0.1:8000")
    parser.add_argument("--tenant", default="n-a")
    parser.add_argument("--app-id", default="example-app")
    parser.add_argument("--elastic-url", default="http://127.0.0.1:9200")
    parser.add_argument("--db-uri", default="postgresql://iap:iap@127.0.0.1:5452/iap")
    parser.add_argument("--request-timeout", type=float, default=15.0)
    parser.add_argument("--journey-timeout", type=float, default=900.0,
                        help="max seconds to wait for terminal COMPLETED")
    parser.add_argument("--loop-timeout", type=float, default=600.0,
                        help="max seconds per Stage 3 accumulation poll")
    parser.add_argument("--run-id", default="")
    args = parser.parse_args()
    if not args.run_id:
        args.run_id = f"{int(time.time())}-{uuid.uuid4().hex[:6]}"

    ctx = Ctx(args)
    ctx.state["loop_timeout"] = float(args.loop_timeout)
    report = Report()
    failed_stage = False

    def s4_1(ctx: Ctx) -> str:
        return s4_1_terminal_completed(ctx, int(args.journey_timeout))

    for stage_no, stage_name, steps in STAGES:
        print(f"== Stage {stage_no}: {stage_name} ==")
        for number, name, fn in steps:
            run_step(report, number, name, s4_1 if fn is None else fn, ctx)
        if report.failed and not failed_stage:
            failed_stage = True
            print(f"-- stopping after Stage {stage_no}: {report.failed[-1].number} failed --")
            break

    summary = report.to_dict()
    summary["investigation_id"] = ctx.state.get("iid")
    summary["run_id"] = args.run_id
    print(json.dumps(summary, indent=1)[:4000])
    passed = sum(1 for c in report.checks if c.status == "PASS")
    print(f"--- journey: {passed} passed, "
          f"{len(report.failed)} failed, "
          f"{sum(1 for c in report.checks if c.status == 'SKIP')} skipped ---")
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())
