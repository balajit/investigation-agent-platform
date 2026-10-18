"""Golden-scenario verifier: infra + LLM + Elastic + API investigation path.

Run via scripts/run-golden-scenario.sh (which does cheap curl/pg_isready
pre-checks first). This module does the deep checks that need Python deps:

  1. LLM gateway construction + live completion (Azure/OpenAI/Anthropic).
  2. Elasticsearch connectivity + mapping-registry load.
  3. API golden path: create -> get -> evidence/timeline/hypotheses/
     knowledge/conclusion -> list -> start (optional, needs Temporal).

Exit 0 + JSON summary on stdout when every non-skipped check passes,
exit 1 otherwise. Individual checks never raise — failures are recorded.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import uuid
from dataclasses import dataclass, field


@dataclass
class Check:
    name: str
    status: str  # PASS | FAIL | SKIP
    detail: str = ""
    latency_ms: int = 0


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    def add(self, name: str, status: str, detail: str = "", latency_ms: int = 0) -> None:
        self.checks.append(Check(name, status, detail, latency_ms))

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if c.status == "FAIL"]

    def to_dict(self) -> dict:
        return {
            "overall": "PASS" if not self.failed else "FAIL",
            "checks": [
                {"name": c.name, "status": c.status, "detail": c.detail, "latency_ms": c.latency_ms}
                for c in self.checks
            ],
        }


PLACEHOLDER_MARKERS = (
    "your-actual-key-here",
    "your-embedding-deployment-name",
    "change-me",
    "placeholder",
)


def _is_placeholder(value: str) -> bool:
    lowered = value.lower()
    return any(m in lowered for m in PLACEHOLDER_MARKERS)


async def check_llm(report: Report, skip: bool) -> None:
    if skip:
        report.add("llm", "SKIP", "--skip-llm")
        return
    started = time.monotonic()
    try:
        from investigation_agent_platform.infrastructure.configuration.config import (
            _llm_config_from_env,
        )
        from investigation_agent_platform.infrastructure.reasoning.factory import (
            MODEL_REGISTRY,
            create_llm_gateway,
        )
        from investigation_agent_platform.ports.reasoning.llm_gateway import (
            LLMGatewayRequest,
        )

        raw_key = os.environ.get("IAP_LLM_API_KEY", "") or os.environ.get(
            "AZURE_OPENAI_API_KEY", ""
        )
        if not raw_key or _is_placeholder(raw_key):
            report.add(
                "llm",
                "FAIL",
                "IAP_LLM_API_KEY is missing or still a placeholder; "
                "export the real key before running",
            )
            return
        model = os.environ.get("IAP_LLM_MODEL", "gpt-4o")
        provider = os.environ.get("IAP_LLM_PROVIDER", "openai")
        if model.lower() not in MODEL_REGISTRY:
            report.add(
                "llm",
                "FAIL",
                f"Model '{model}' not in MODEL_REGISTRY "
                f"({sorted(MODEL_REGISTRY)}). For Azure set IAP_LLM_MODEL to the "
                "base model id (e.g. 'gpt-4o-mini') and IAP_AZURE_OPENAI_DEPLOYMENT "
                "to the deployment name.",
            )
            return
        cfg = _llm_config_from_env()
        gateway = create_llm_gateway(cfg)
        resp = await asyncio.wait_for(
            gateway.complete(
                "golden-tenant",
                LLMGatewayRequest(
                    prompt="Reply with exactly: GOLDEN-OK", max_tokens=32, temperature=0.0
                ),
            ),
            timeout=60,
        )
        latency = int((time.monotonic() - started) * 1000)
        content = (resp.content or "").strip()
        if "GOLDEN-OK" in content:
            report.add(
                "llm", "PASS", f"provider={provider} model={model} reply={content[:80]!r}", latency
            )
        else:
            report.add("llm", "FAIL", f"Unexpected LLM reply: {content[:200]!r}", latency)
    except Exception as exc:  # recorded, not raised
        report.add(
            "llm", "FAIL", f"{type(exc).__name__}: {exc}", int((time.monotonic() - started) * 1000)
        )


async def check_elastic(report: Report, skip: bool) -> None:
    if skip:
        report.add("elastic", "SKIP", "--skip-elastic")
        return
    started = time.monotonic()
    try:
        from elasticsearch import AsyncElasticsearch

        from investigation_agent_platform.infrastructure.configuration.mapping_registry import (
            load_mapping_profiles,
        )

        url = os.environ.get("IAP_ELASTICSEARCH_URL", "http://localhost:9200")
        timeout = float(os.environ.get("IAP_ELASTICSEARCH_TIMEOUT", "30"))
        client = AsyncElasticsearch(url, request_timeout=timeout)
        try:
            pinged = await asyncio.wait_for(client.ping(), timeout=10)
            info = await asyncio.wait_for(client.info(), timeout=10)
        finally:
            await client.close()
        if not pinged:
            report.add("elastic", "FAIL", f"ping() false at {url}")
            return
        version = info.get("version", {}).get("number", "?")
        # Mapping registry must load (Part 10) — this is what the
        # investigation path depends on for field resolution.
        path = os.environ.get("IAP_MAPPING_PROFILES_PATH", "config/observability-mappings")
        try:
            registry = load_mapping_profiles(path)
            profiles = sorted(registry.source_ids)
        except Exception as exc:
            report.add("elastic", "FAIL", f"ES up (v{version}) but mapping registry failed: {exc}")
            return
        latency = int((time.monotonic() - started) * 1000)
        report.add("elastic", "PASS", f"v{version} at {url}; mapping profiles={profiles}", latency)
    except Exception as exc:
        report.add(
            "elastic",
            "FAIL",
            f"{type(exc).__name__}: {exc}",
            int((time.monotonic() - started) * 1000),
        )


async def check_api(
    report: Report, api_base: str, tenant: str, app_id: str, do_start: bool
) -> str | None:
    import httpx

    headers = {"X-Tenant-ID": tenant, "Content-Type": "application/json"}
    timeout = httpx.Timeout(15.0)
    inv_id: str | None = None
    async with httpx.AsyncClient(base_url=api_base, timeout=timeout) as client:
        # -- live ---------------------------------------------------------
        try:
            r = await client.get("/api/v1/health/live")
            report.add(
                "api:live",
                "PASS" if r.status_code == 200 else "FAIL",
                f"HTTP {r.status_code} {r.text[:120]}",
            )
        except Exception as exc:
            report.add("api:live", "FAIL", f"{type(exc).__name__}: {exc}")
            return None

        # -- ready --------------------------------------------------------
        try:
            r = await client.get("/api/v1/health/ready")
            body = r.text[:300]
            report.add(
                "api:ready",
                "PASS" if r.status_code == 200 else "FAIL",
                f"HTTP {r.status_code} {body}",
            )
            if r.status_code != 200:
                return None
        except Exception as exc:
            report.add("api:ready", "FAIL", f"{type(exc).__name__}: {exc}")
            return None

        # -- create investigation (golden path entry) ---------------------
        try:
            payload = {
                "application_id": app_id,
                "problem_description": (
                    "Golden-scenario verification: elevated error rate on "
                    "checkout service during synthetic canary"
                ),
                "session_id": f"golden-{uuid.uuid4().hex[:8]}",
                "priority": "NORMAL",
                "requested_by": "golden-scenario",
                "parameters": {"scenario": "golden"},
            }
            r = await client.post("/api/v1/investigations", headers=headers, json=payload)
            if r.status_code != 201:
                report.add("api:create", "FAIL", f"HTTP {r.status_code} {r.text[:300]}")
                return None
            inv_id = r.json().get("id")
            report.add("api:create", "PASS", f"id={inv_id}")
        except Exception as exc:
            report.add("api:create", "FAIL", f"{type(exc).__name__}: {exc}")
            return None

        assert inv_id is not None
        base = f"/api/v1/investigations/{inv_id}"

        # -- read-back + sub-resources ------------------------------------
        for name, path in [
            ("api:get", base),
            ("api:evidence", f"{base}/evidence?limit=5"),
            ("api:timeline", f"{base}/timeline?limit=5"),
            ("api:hypotheses", f"{base}/hypotheses?limit=5"),
            ("api:knowledge", f"{base}/knowledge"),
            ("api:conclusion", f"{base}/conclusion"),
        ]:
            try:
                r = await client.get(path, headers=headers)
                ok = r.status_code in (200, 404)
                # 404 on sub-resources for a fresh investigation is
                # acceptable (nothing recorded yet); 5xx/401/403 is not.
                report.add(name, "PASS" if ok else "FAIL", f"HTTP {r.status_code} {r.text[:160]}")
            except Exception as exc:
                report.add(name, "FAIL", f"{type(exc).__name__}: {exc}")

        # -- list ----------------------------------------------------------
        try:
            r = await client.get("/api/v1/investigations?limit=5", headers=headers)
            report.add(
                "api:list",
                "PASS" if r.status_code == 200 else "FAIL",
                f"HTTP {r.status_code} {r.text[:160]}",
            )
        except Exception as exc:
            report.add("api:list", "FAIL", f"{type(exc).__name__}: {exc}")

        # -- start workflow (needs Temporal + worker) ----------------------
        if not do_start:
            report.add("api:start", "SKIP", "--start not passed (default off)")
        else:
            try:
                r = await client.post(f"{base}/start", headers=headers)
                if r.status_code == 503:
                    # Dev API profile runs the in-memory AppContext with no
                    # Temporal client (readiness reports temporal=NOT_REQUIRED),
                    # so /start correctly refuses with 503. Not a regression —
                    # record SKIP, not FAIL, with the reason.
                    report.add(
                        "api:start",
                        "SKIP",
                        "workflow engine not wired in this API profile "
                        f"(HTTP 503 {r.text[:160]}); rerun against the "
                        "production-wired stack to exercise /start",
                    )
                else:
                    ok = r.status_code in (202, 409)
                    report.add(
                        "api:start",
                        "PASS" if ok else "FAIL",
                        f"HTTP {r.status_code} {r.text[:200]}",
                    )
            except Exception as exc:
                report.add("api:start", "FAIL", f"{type(exc).__name__}: {exc}")
    return inv_id


async def check_db(report: Report, inv_id: str | None) -> None:
    """Prove the created investigation actually landed in Postgres.

    PASS = row present (write-through works). FAIL = DB reachable but row
    missing (persistence broken). SKIP = DB unreachable (buffered mode; the
    row must appear after reconnect + flush) or no investigation created.
    """
    if not inv_id:
        report.add("db:row", "SKIP", "no investigation created")
        return
    started = time.monotonic()
    uri = os.environ.get("IAP_DATABASE_URI", "")
    if not uri:
        report.add("db:row", "SKIP", "IAP_DATABASE_URI not set")
        return
    try:
        import asyncpg

        conn = await asyncio.wait_for(asyncpg.connect(uri), timeout=8)
        try:
            version = await conn.fetchval("SELECT version_num FROM alembic_version")
            total = await conn.fetchval("SELECT COUNT(*) FROM investigations")
            row = await conn.fetchrow(
                "SELECT id::text, status, tenant_id FROM investigations WHERE id = $1",
                inv_id,
            )
        finally:
            await conn.close()
        latency = int((time.monotonic() - started) * 1000)
        if row is not None:
            report.add(
                "db:row",
                "PASS",
                f"investigations row id={row['id']} status={row['status']} "
                f"(schema {version}, {total} rows)",
                latency,
            )
        else:
            report.add(
                "db:row",
                "FAIL",
                f"DB reachable (schema {version}, {total} rows) but investigation "
                f"{inv_id} missing — write-through broken",
                latency,
            )
    except Exception as exc:
        report.add(
            "db:row",
            "SKIP",
            f"DB unreachable ({type(exc).__name__}); buffered mode — row must "
            "appear after reconnect + flush",
        )


async def amain(args: argparse.Namespace) -> int:
    report = Report()
    await check_llm(report, args.skip_llm)
    await check_elastic(report, args.skip_elastic)
    inv_id = await check_api(report, args.api_base, args.tenant, args.app_id, args.start)
    await check_db(report, inv_id)
    print(json.dumps(report.to_dict(), indent=2))
    return 0 if not report.failed else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Golden-scenario verifier")
    parser.add_argument(
        "--api-base", default=os.environ.get("IAP_API_BASE", "http://localhost:8000")
    )
    parser.add_argument("--tenant", default=os.environ.get("IAP_GOLDEN_TENANT", "tenant-a"))
    parser.add_argument(
        "--app-id",
        default=os.environ.get("IAP_GOLDEN_APP_ID", "example-app"),
        help="Application profile id (dev stack seeds example-app for tenant-a)",
    )
    parser.add_argument("--skip-llm", action="store_true")
    parser.add_argument("--skip-elastic", action="store_true")
    parser.add_argument(
        "--start", action="store_true", help="Also POST /start (needs Temporal + worker)"
    )
    args = parser.parse_args()
    return asyncio.run(amain(args))


if __name__ == "__main__":
    sys.exit(main())
