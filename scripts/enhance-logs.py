#!/usr/bin/env python3
"""enhance-logs.py — ID-first log deciphering through the generic compression flow.

Flow (generic compression first, IAP attach second):
  discover (tmp/*.log, data/*.log, mtime order)
    → logparse.parse_lines (streaming)
    → filter by investigation ID
    → order_temporal
    → dedup_collapse (exact runs; errors/signals forced singleton)
    → resolve each distinct ID ONCE via MCP (memoized)
    → attach full resolutions at first + last + signal exemplars,
      compact refs elsewhere
    → one ordered file per investigation ID:
      tmp/enhanced-<id8>-<utc>.jsonl

Usage:
    set -a && source .env.local && set +a
    uv run scripts/enhance-logs.py --investigation-id <uuid> [--investigation-id <uuid2>]
    uv run scripts/enhance-logs.py --investigation-id <uuid1>,<uuid2> --limit 200 --tenant n-a

Env:
    IAP_MCP_LOGS_DB_URL  MCP SSE endpoint (default http://localhost:8100/sse)

Secrets: the DB URI lives inside the MCP container; this script never sees
or prints credentials. Only SELECT statements are ever issued.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import glob
import json
import os
import sys
from uuid import UUID

from investigation_agent_platform.application.compression import logparse as lp

MAX_TEXT = 500  # resolved text fields truncated for the enriched file
MAX_INVESTIGATIONS = 200
MAX_EVIDENCE_PER_INV = 25
MAX_HYPOTHESES_PER_INV = 10

# IAP signal vocabulary: lines that always survive collapse AND receive full
# resolution attachments (conclusions, dispositions, verdicts, failures).
SIGNAL_LEVELS = frozenset({"ERROR", "CRITICAL", "WARNING"})
SIGNAL_TOKENS = (
    "conclusion",
    "disposition",
    "verdict",
    "failed",
    "abstain",
    "insufficient_evidence",
    "root_cause",
    "completed",
    "duplicate",
)


def is_signal(line: lp.ParsedLine) -> bool:
    """IAP-side keep predicate: errors + outcome vocabulary. Pure."""
    try:
        if line.record is not None:
            level = str(line.record.get("level", "")).upper()
            if level in SIGNAL_LEVELS:
                return True
        blob = line.raw.lower()
        return any(token in blob for token in SIGNAL_TOKENS)
    except Exception:
        return False


def contains_id(line: lp.ParsedLine, inv_id: str) -> bool:
    """Match predicate for one investigation ID (parsed ids or raw text)."""
    try:
        return inv_id in line.ids or inv_id in line.raw
    except Exception:
        return False


def investigation_node(inv_id: str) -> str:
    return f"investigation:{inv_id}"


class McpDb:
    """Thin read-only wrapper over the Postgres MCP server (SSE)."""

    def __init__(self, url: str) -> None:
        self._url = url

    async def query(self, sql: str, max_rows: int = 100) -> list[dict]:
        from mcp import ClientSession
        from mcp.client.sse import sse_client

        async with sse_client(self._url) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(
                    "execute_sql", arguments={"sql": sql, "max_rows": max_rows}
                )
        rows: list[dict] = []
        for item in result.content or []:
            text = getattr(item, "text", None)
            if not text:
                continue
            parsed = _parse_result_text(text)
            if isinstance(parsed, list):
                rows.extend(r for r in parsed if isinstance(r, dict))
            elif isinstance(parsed, dict):
                rows.append(parsed)
        return rows[:max_rows]


def _parse_result_text(text: str) -> object:
    """Parse one MCP result payload. crystaldba returns Python literals
    (single quotes), not strict JSON — try JSON first, literal_eval second."""
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        pass
    try:
        import ast

        return ast.literal_eval(text)
    except (ValueError, SyntaxError):
        return None


def _short(text: object, limit: int = MAX_TEXT) -> str:
    text = str(text or "")
    return text if len(text) <= limit else text[:limit] + f"...[{len(text) - limit} chars]"


async def resolve_investigation(db: McpDb, tenant: str, inv_id: str) -> dict:
    """Resolve one investigation + conclusion/hypotheses/evidence/links via MCP."""
    resolved: dict = {"id": inv_id}
    inv_rows = await db.query(
        "SELECT id::text AS id, status, phase, application_id, session_id, "
        "created_at::text AS created_at, completed_at::text AS completed_at, "
        f"LEFT(problem_description, {MAX_TEXT}) AS problem "
        f"FROM investigations WHERE id = '{inv_id}' AND tenant_id = '{tenant}'",
        max_rows=2,
    )
    if not inv_rows:
        resolved["found"] = False
        return resolved
    resolved.update({k: v for k, v in inv_rows[0].items()})
    resolved["found"] = True

    conc_rows = await db.query(
        "SELECT status, confidence, LEFT(root_cause, 1000) AS root_cause, "
        "LEFT(array_to_string(limitations, ' | '), 500) AS limitations "
        f"FROM investigation_conclusions WHERE investigation_id = '{inv_id}' "
        f"AND tenant_id = '{tenant}'",
        max_rows=2,
    )
    if conc_rows:
        resolved["conclusion"] = conc_rows[0]

    hyp_rows = await db.query(
        "SELECT id::text AS id, status::text AS status, confidence_score, "
        f"LEFT(statement, {MAX_TEXT}) AS statement "
        f"FROM hypotheses WHERE investigation_id = '{inv_id}' "
        f"AND tenant_id = '{tenant}' ORDER BY created_at",
        max_rows=MAX_HYPOTHESES_PER_INV,
    )
    resolved["hypotheses"] = hyp_rows

    ev_rows = await db.query(
        "SELECT id::text AS id, type, source_type, LEFT(source, 200) AS source "
        f"FROM evidence WHERE investigation_id = '{inv_id}' "
        f"AND tenant_id = '{tenant}' ORDER BY created_at LIMIT {MAX_EVIDENCE_PER_INV}",
        max_rows=MAX_EVIDENCE_PER_INV,
    )
    resolved["evidence"] = ev_rows

    link_rows = await db.query(
        "SELECT source_node_id AS source, target_node_id AS target, "
        "relationship_type AS type, LEFT(reason, 200) AS reason "
        "FROM evidence_relationships WHERE tenant_id = "
        f"'{tenant}' AND (source_node_id = '{investigation_node(inv_id)}' "
        f"OR target_node_id = '{investigation_node(inv_id)}')",
        max_rows=20,
    )
    resolved["links"] = link_rows
    return resolved


def discover_files(dirs: list[str], pattern: str = "*.log") -> list[str]:
    """Log discovery: mtime-ordered files across dirs (oldest first). Pure I/O list."""
    import os as _os

    found: list[str] = []
    for directory in dirs:
        try:
            matches = glob.glob(_os.path.join(directory, pattern))
        except OSError:
            continue
        found.extend(matches)
    try:
        found.sort(key=lambda path: _os.path.getmtime(path))
    except OSError:
        pass
    # De-duplicate while preserving order.
    seen: set[str] = set()
    ordered = [p for p in found if not (p in seen or seen.add(p))]
    return ordered


def enrich_exemplar(
    exemplar: lp.Exemplar,
    resolutions: dict[str, dict],
    source_ids: set[str],
    meaningful: bool,
) -> dict:
    """Render one exemplar: original fields + dedup accounting + resolutions.

    Full resolution blobs attach only at meaningful points (first + last +
    signal exemplars); everything else carries compact refs. Pure.
    """
    first = exemplar.first
    record = first.record or {}
    attached = (
        {uid: resolutions[uid] for uid in source_ids if uid in resolutions}
        if meaningful
        else {}
    )
    return {
        "message": record.get("message", record.get("event", first.raw[:300])),
        "level": record.get("level"),
        "timestamp": first.timestamp.isoformat() if first.timestamp else None,
        "last_seen": exemplar.last.timestamp.isoformat() if exemplar.last.timestamp else None,
        "source_file": first.source_file,
        "extra": record.get("extra", {}),
        "dedup": {
            "count": exemplar.count,
            "span_first_seq": exemplar.first.seq,
            "span_last_seq": exemplar.last.seq,
            "forced_singleton": exemplar.forced_singleton,
        },
        "ids": sorted(source_ids),
        "resolved": attached,
        "refs": sorted(source_ids - set(attached)),
    }


async def process_one(
    db: McpDb,
    tenant: str,
    inv_id: str,
    files: list[str],
    limit: int,
    out_dir: str,
    resolutions: dict[str, dict],
) -> str:
    """Full flow for one investigation ID → one output file. Returns path."""
    try:
        UUID(inv_id)
    except ValueError as exc:
        raise SystemExit(f"error: not a UUID: {inv_id}") from exc

    matched = [line for line in lp.parse_lines(files) if contains_id(line, inv_id)]
    ordered = lp.order_temporal(matched)
    exemplars = lp.dedup_collapse(ordered, keep_single=is_signal)

    # Resolve every distinct ID seen (target + co-occurring), exactly once.
    co_ids: set[str] = set()
    for line in ordered:
        co_ids.update(line.ids)
    co_ids.add(inv_id)
    for uid in sorted(co_ids):
        if uid not in resolutions:
            try:
                resolved = await resolve_investigation(db, tenant, uid)
                if resolved.get("found"):
                    resolutions[uid] = resolved
            except Exception as exc:
                print(f"warn: resolve {uid[:8]} failed: {exc}", file=sys.stderr)

    stamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")
    out = os.path.join(out_dir, f"enhanced-{inv_id[:8]}-{stamp}.jsonl")
    n = len(exemplars)
    written = 0
    with open(out, "w", encoding="utf-8") as handle:  # noqa: ASYNC230 - single-shot CLI
        for index, exemplar in enumerate(exemplars):
            if index >= limit:
                break
            source_ids = set(exemplar.first.ids) | set(exemplar.last.ids)
            source_ids.add(inv_id)
            meaningful = (
                index == 0 or index == n - 1 or exemplar.forced_singleton
            )
            handle.write(
                json.dumps(
                    enrich_exemplar(exemplar, resolutions, source_ids, meaningful),
                    ensure_ascii=False,
                )
                + "\n"
            )
            written += 1
    cov = lp.coverage(len(ordered), exemplars)
    print(
        f"{inv_id[:8]}: {len(ordered)} lines -> {written} exemplars "
        f"({cov.ratio:.0%} compressed) -> {out}"
    )
    return out


async def _async_main(args: argparse.Namespace) -> int:
    raw_ids: list[str] = []
    for chunk in args.investigation_id:
        raw_ids.extend(part.strip() for part in chunk.split(",") if part.strip())
    inv_ids = list(dict.fromkeys(raw_ids))  # de-dupe, preserve order
    if not inv_ids:
        print("error: at least one --investigation-id is required", file=sys.stderr)
        return 2
    if len(inv_ids) > MAX_INVESTIGATIONS:
        print(f"error: at most {MAX_INVESTIGATIONS} ids per run", file=sys.stderr)
        return 2

    files = discover_files(args.log_dir)
    if not files:
        print("error: no log files discovered", file=sys.stderr)
        return 2

    url = os.environ.get("IAP_MCP_LOGS_DB_URL", "http://localhost:8100/sse")
    db = McpDb(url)
    try:
        if not await db.query("SELECT 1 AS ok", max_rows=1):
            print("error: MCP server returned nothing", file=sys.stderr)
            return 2
    except Exception as exc:
        print(f"error: MCP query failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    tenant = args.tenant
    if not tenant:
        # Derive from lines referencing our IDs (must unanimously agree).
        matched_tenants: set[str] = set()
        for line in lp.parse_lines(files):
            if not any(inv in line.ids or inv in line.raw for inv in inv_ids):
                continue
            if line.record:
                for key in ("tenant_id", "requested_tenant_id", "authenticated_tenant_id"):
                    value = line.record.get(key)
                    if value and value not in ("anonymous", "unknown"):
                        matched_tenants.add(str(value))
        if len(matched_tenants) != 1:
            print(
                f"error: tenant ambiguous {sorted(matched_tenants)} (use --tenant)",
                file=sys.stderr,
            )
            return 2
        tenant = next(iter(matched_tenants))

    resolutions: dict[str, dict] = {}
    for inv_id in inv_ids:
        await process_one(db, tenant, inv_id, files, args.limit, args.out_dir, resolutions)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--investigation-id",
        action="append",
        required=True,
        help="investigation UUID (repeatable; comma-separated also accepted)",
    )
    parser.add_argument("--tenant", default="", help="tenant scope (auto-derived if omitted)")
    parser.add_argument(
        "--limit", type=int, default=500, help="max exemplars per investigation file"
    )
    parser.add_argument(
        "--log-dir", action="append", default=["tmp", "data"], help="log dirs to scan"
    )
    parser.add_argument("--out-dir", default="tmp", help="output directory")
    args = parser.parse_args()
    return asyncio.run(_async_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
