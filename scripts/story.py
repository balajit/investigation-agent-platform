"""story.py — reconstruct one investigation's narrative from log + DB (read-only).

Reads story.* DEBUG events (plus key INFO milestones) from the worker log
and joins them with retained DB rows (evidence, transitions, conclusion).
Prints a per-iteration narrative: what the reasoner saw, proposed, how
forcing overrode it, what executed, what evidence came back, and how each
gate judged it.

Usage:
  uv run python scripts/story.py <investigation-id> [--log tmp/worker.log]

Requires the layered dev env (./.env.local, ~/bin/.iap.env.local) for the
read-only DB connection. Prints host/port/database only, never credentials.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path

STORY_ORDER = [
    "story.reason_in",
    "story.reason_out",
    "story.forcing_outcome",
    "story.defer_accepted",
    "story.defer_exhausted",
    "story.execute_in",
    "story.execute_out",
    "story.hypothesis_saved",
    "story.verify_gate",
    "story.conclude_decision",
    "story.loop_exit",
]

MILESTONES = {
    "reason_activity decision",
    "reason_activity completed",
    "execute_action_activity dispatched",
    "execute_action_activity completed",
    "conclusion.save",
    "abstention persist failed",
    "conclusion persist failed",
}


def load_dev_env() -> None:
    for candidate in (Path("./.env.local"), Path.home() / "bin" / ".iap.env.local"):
        if candidate.is_file():
            for line in candidate.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                if key.startswith("export "):
                    key = key[len("export ") :].strip()
                if key and key not in os.environ:
                    os.environ[key] = value.strip().strip("\"'")


def short(value, limit: int = 300) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return text if len(text) <= limit else text[:limit] + "…"


def collect_log_events(log_path: Path, inv_id: str) -> dict[int, list[tuple[str, dict]]]:
    """Group story + milestone events by iteration (loop-level events go last)."""
    by_iter: dict[int, list[tuple[str, dict]]] = {}
    loop_level: list[tuple[str, dict]] = []
    order = {name: i for i, name in enumerate(STORY_ORDER)}
    with log_path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if inv_id not in line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            message = str(record.get("message", ""))
            extra = record.get("extra", {})
            if not isinstance(extra, dict):
                continue
            if message.startswith("story."):
                iteration = extra.get("iteration", -1)
                try:
                    iteration = int(iteration)
                except (TypeError, ValueError):
                    iteration = -1
                payload = {k: v for k, v in extra.items() if k not in ("tenant_id", "investigation_id", "event")}
                if iteration < 0:
                    loop_level.append((message, payload))
                else:
                    by_iter.setdefault(iteration, []).append((message, payload))
            elif message in MILESTONES:
                iteration = extra.get("iteration", -1)
                try:
                    iteration = int(iteration)
                except (TypeError, ValueError):
                    iteration = -1
                payload = {k: v for k, v in extra.items() if k not in ("tenant_id", "investigation_id", "event")}
                entry = (f"info:{message}", payload)
                if iteration < 0:
                    loop_level.append(entry)
                else:
                    by_iter.setdefault(iteration, []).append(entry)
    for events in by_iter.values():
        events.sort(key=lambda item: order.get(item[0], 99))
    loop_level.sort(key=lambda item: order.get(item[0], 99))
    if loop_level:
        by_iter.setdefault(10**9, []).extend(loop_level)
    return by_iter


def render_event(name: str, payload: dict) -> list[str]:
    lines = [f"  [{name}]"]
    if name == "story.reason_out":
        lines.append(f"    disposition={payload.get('disposition')} next={payload.get('next_action')}")
        lines.append(f"    params={short(payload.get('action_parameters'), 250)}")
        if payload.get("defer_gathering"):
            lines.append(f"    defer={short(payload.get('defer_gathering'), 400)}")
        lines.append(f"    claims={len(payload.get('claims') or [])} hyps={len(payload.get('hypotheses_evaluated') or [])} readiness={payload.get('conclusion_readiness')}")
    elif name in ("story.forcing_outcome", "story.defer_accepted", "story.defer_exhausted", "story.loop_exit"):
        lines.append(f"    {short(payload, 300)}")
    elif name == "story.execute_in":
        lines.append(f"    action={payload.get('action')} params={short(payload.get('parameters'), 250)}")
        lines.append(f"    claims={len(payload.get('claims') or [])} hyps={len(payload.get('hypotheses') or [])}")
    elif name == "story.execute_out":
        outcome = payload.get("action_outcome", {}) or {}
        attempt = outcome.get("retrieval_attempt", {}) or {}
        lines.append(f"    status={outcome.get('status')} outcome={attempt.get('outcome')} miss={outcome.get('miss_reason')}")
        lines.append(f"    evidence_ids={len(outcome.get('evidence_ids') or [])} items={payload.get('items_count')} reconciled={payload.get('caller_edges_reconciled')}")
        if attempt:
            lines.append(f"    retrieval={short(attempt, 300)}")
    elif name == "story.hypothesis_saved":
        lines.append(f"    statement={short(payload.get('statement'), 200)}")
        lines.append(f"    assessments={short(payload.get('assessment_codes'), 200)} verify={short(payload.get('required_verification'), 200)}")
    elif name == "story.verify_gate":
        lines.append(f"    statement={short(payload.get('statement'), 200)}")
        lines.append(f"    status={payload.get('status')} path={payload.get('producing_path')} bound={payload.get('bound_count')}")
        lines.append(f"    explanation={short(payload.get('explanation'), 300)}")
    elif name == "story.conclude_decision":
        lines.append(f"    status={payload.get('status')} coverage={payload.get('caller_coverage')}")
        lines.append(f"    limitations={short(payload.get('limitations'), 400)}")
        lines.append(f"    proven={short(payload.get('proven_callers'), 200)} unresolved={short(payload.get('unresolved_callers'), 200)}")
    else:
        lines.append(f"    {short(payload, 300)}")
    return lines


async def collect_db(inv_id: str) -> dict:
    import asyncpg

    uri = os.environ.get("IAP_DATABASE_URI", "")
    dsn = re.sub(r"^postgresql\+asyncpg://", "postgresql://", uri)
    conn = await asyncpg.connect(dsn)
    try:
        inv = await conn.fetchrow(
            "SELECT status FROM investigations WHERE id=$1::uuid", inv_id
        )
        evidence = await conn.fetch(
            "SELECT payload_json FROM evidence WHERE investigation_id=$1::uuid",
            inv_id,
        )
        conclusions = await conn.fetch(
            "SELECT status, limitations FROM investigation_conclusions "
            "WHERE investigation_id=$1::uuid",
            inv_id,
        )
        return {
            "status": inv[0] if inv else "UNKNOWN",
            "evidence": evidence,
            "conclusions": conclusions,
        }
    finally:
        await conn.close()


def summarize_evidence(rows) -> list[str]:
    lines = []
    for i, row in enumerate(rows):
        payload = row[0]
        if isinstance(payload, str):
            payload = json.loads(payload)
        attrs = payload.get("attributes", {}) or {}
        provenance = payload.get("provenance", {}) or {}
        location = provenance.get("source_location", {}) or {}
        lines.append(
            f"  row{i}: type={payload.get('evidence_type')} "
            f"scope={attrs.get('search_scope')} match={attrs.get('match_count')} "
            f"discovery_only={attrs.get('discovery_only')} "
            f"file={location.get('file_path')} rev={location.get('revision')} "
            f"lines={location.get('line_start')}-{location.get('line_end')}"
        )
    return lines


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("investigation_id")
    parser.add_argument("--log", default="tmp/worker.log")
    args = parser.parse_args()
    inv_id = args.investigation_id

    log_path = Path(args.log)
    if not log_path.is_file():
        print(f"no log file: {log_path}", file=sys.stderr)
        return 2
    by_iter = collect_log_events(log_path, inv_id)

    load_dev_env()
    from urllib.parse import urlparse

    parsed = urlparse(os.environ.get("IAP_DATABASE_URI", ""))
    print(f"db: host={parsed.hostname} port={parsed.port} database={parsed.path.lstrip('/')}")
    db = await collect_db(inv_id)

    print(f"investigation: {inv_id} status={db['status']}")
    for iteration in sorted(by_iter):
        label = f"iteration {iteration}" if iteration < 10**9 else "run level"
        print(f"\n## {label}")
        for name, payload in by_iter[iteration]:
            for line in render_event(name, payload):
                print(line)
    print("\n## evidence rows (DB)")
    for line in summarize_evidence(db["evidence"]) or ["  (none)"]:
        print(line)
    print("\n## conclusions (DB)")
    for row in db["conclusions"] or []:
        print(f"  status={row[0]} limitations={short(row[1], 400)}")
    if not db["conclusions"]:
        print("  (none)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
