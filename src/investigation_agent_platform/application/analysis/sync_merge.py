# src/investigation_agent_platform/application/analysis/sync_merge.py
"""Sync merge: deterministic, idempotent graph rebuilds (Phase 6).

Per-repo fan-out results merge with sorted outputs, pinned sidecar digests,
normalized paths so parallelism never affects output bytes. Writes are
idempotent on (repo, SHA, stage); retries safe; partial results explicitly
marked. Steady-state cost O(diff): only touched edges re-parsed.
"""

from __future__ import annotations


def normalize_path(path: str) -> str:
    p = path.strip().replace("\\", "/")
    while "//" in p:
        p = p.replace("//", "/")
    while p.startswith("./"):
        p = p[2:]
    if p in ("", "."):
        return ""
    return p


def merge_shards(shards: list[list[dict] | None], *, stage: str) -> dict:
    """Deterministic merge: sorted outputs + per-shard counts + partial marking.

    None marks a failed/missing shard (partial); an empty list is a valid
    complete shard with zero records.
    """
    merged: dict[tuple[str, str, str, str, str, str], dict] = {}
    partial_shards = 0
    for shard in shards:
        if shard is None:
            partial_shards += 1
            continue
        for record in shard:
            key = (
                normalize_path(str(record.get("repo", ""))),
                str(record.get("sha", "")),
                normalize_path(str(record.get("src", ""))),
                normalize_path(str(record.get("dst", ""))),
                normalize_path(str(record.get("path", ""))),
                str(record.get("kind", "")),
            )
            merged[key] = record
    ordered = [merged[k] for k in sorted(merged)]
    return {
        "stage": stage,
        "records": ordered,
        "count": len(ordered),
        "shards": len(shards),
        "partial_shards": partial_shards,
        "partial": partial_shards > 0,
    }


def diff_shards(old_sha: str, new_sha: str, files: list[str]) -> dict:
    """Differential re-analysis descriptor: O(diff), not O(repo)."""
    if old_sha == new_sha:
        return {"rebuild": False, "touched": []}
    return {"rebuild": True, "touched": sorted(set(files))}
