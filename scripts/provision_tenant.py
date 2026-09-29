#!/usr/bin/env python3
"""Provision tenant application profiles from a loader manifest (WIP §2.1).

Reads READY rows, grounds the `code` section in observed checkouts (language
by dominant extension, source roots by layout, locator/branch from the
manifest), and saves DRAFT profiles via `profile_repo.save`. Observability
and state sections are explicit TBD placeholders — DRAFT status plus the
description say so; activation requires operator-supplied facts. Never
fabricates indices, tables, or query templates.

Usage:
    IAP_DATABASE_URI='postgresql+asyncpg://iap:iap@localhost:5432/iap' \\
      uv run python scripts/provision_tenant.py --manifest ./tmp/ingest-TENANT-1-v2.jsonl \\
        --tenant-id TENANT-1 --workdir ./tmp/repos
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import Counter
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = _REPO_ROOT / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

_EXT_LANGUAGE = {
    ".py": "python",
    ".java": "java",
    ".js": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".go": "go",
    ".rs": "rust",
    ".rb": "ruby",
    ".php": "php",
    ".c": "c",
    ".cpp": "cpp",
    ".cs": "csharp",
    ".kt": "kotlin",
    ".swift": "swift",
    ".xml": "xml",
    ".xsd": "xml",
    ".wsdl": "xml",
    ".sql": "sql",
}
_BUILD_SYSTEM = {
    "java": "maven",
    "python": "uv",
    "javascript": "npm",
    "typescript": "npm",
    "go": "go",
    "rust": "cargo",
}


def _detect_language(checkout: Path) -> tuple[str, list[str]]:
    counts: Counter[str] = Counter()
    roots: set[str] = set()
    for path in checkout.rglob("*"):
        if not path.is_file() or ".git/" in path.as_posix():
            continue
        lang = _EXT_LANGUAGE.get(path.suffix.lower())
        if lang is None:
            continue
        counts[lang] += 1
        rel = path.relative_to(checkout).as_posix()
        if "/" in rel:
            roots.add(rel.split("/")[0])
    language = counts.most_common(1)[0][0] if counts else "unknown"
    return language, sorted(roots)[:10]


async def run(args: argparse.Namespace) -> int:
    from sqlalchemy.ext.asyncio import create_async_engine

    from investigation_agent_platform.domain.profile.models import ApplicationProfile
    from investigation_agent_platform.infrastructure.persistence.profile_repository import (
        SqlAlchemyApplicationProfileRepository,
    )

    db_uri = os.environ.get("IAP_DATABASE_URI", "")
    if not db_uri:
        print("error: IAP_DATABASE_URI is required", file=sys.stderr)
        return 2
    tenant_id = args.tenant_id or os.environ.get("IAP_TENANT_ID", "")
    if not tenant_id:
        print("error: --tenant-id (or IAP_TENANT_ID) is required", file=sys.stderr)
        return 2
    workdir = Path(args.workdir).resolve()
    rows = [
        json.loads(line) for line in Path(args.manifest).read_text().splitlines() if line.strip()
    ]
    ready = [r for r in rows if r.get("status") == "READY" and r.get("tenant_id") == tenant_id]
    if not ready:
        print("error: no READY rows for tenant in manifest", file=sys.stderr)
        return 1

    engine = create_async_engine(db_uri)
    from sqlalchemy.ext.asyncio import async_sessionmaker

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    repo = SqlAlchemyApplicationProfileRepository(session_factory)
    created = 0
    try:
        for row in ready:
            full_name = row["repository_full_name"]
            rid = row["repository_id"]
            checkout = workdir / full_name.replace("/", "--")
            if checkout.is_dir():
                language, roots = await asyncio.to_thread(_detect_language, checkout)
            else:
                language, roots = "unknown", []
            profile = ApplicationProfile.model_validate(
                {
                    "id": rid,
                    "tenant_id": tenant_id,
                    "name": full_name,
                    "description": (
                        "DRAFT auto-provisioned from ingest manifest; code section "
                        "observed, observability/state are TBD placeholders — replace "
                        "with real facts before activating."
                    ),
                    "environment": "production",
                    "status": "DRAFT",
                    "owner": "tenant-provisioning",
                    "observability": {
                        "provider": "elastic",
                        "indices": ["TBD"],
                        "timestampField": "@timestamp",
                        "serviceField": "service.name",
                        "environmentField": "service.environment",
                        "sessionField": "labels.sessionId",
                        "requestField": "labels.requestId",
                        "traceField": "trace.id",
                        "logLevelField": "log.level",
                    },
                    "state": {
                        "provider": "unknown",
                        "database": "TBD",
                        "schema": "TBD",
                        "tables": ["TBD"],
                        "primaryIdentifiers": ["TBD"],
                        "stateFields": ["TBD"],
                        "timestampFields": ["TBD"],
                        "queryTemplates": {},
                    },
                    "code": {
                        "provider": "git",
                        "repository": f"https://github.com/{full_name}.git",
                        "defaultBranch": row.get("default_branch") or "main",
                        "language": language,
                        "sourceRoots": roots or ["src"],
                        "buildSystem": _BUILD_SYSTEM.get(language, "unknown"),
                        "moduleStructure": "multi-module" if len(roots) > 3 else "single-module",
                        "repositoryId": rid,
                    },
                    "correlation": {
                        "fields": ["sessionId", "requestId", "traceId", "correlationId"]
                    },
                    "investigation": {},
                }
            )
            await repo.save(tenant_id, profile)
            created += 1
            print(f"DRAFT {full_name} language={language} roots={roots}")
    finally:
        await engine.dispose()
    print(f"done: {created} DRAFT profiles for {tenant_id}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Provision DRAFT tenant profiles from a manifest.")
    p.add_argument("--manifest", required=True)
    p.add_argument("--tenant-id", default="")
    p.add_argument("--workdir", default="./tmp/repos")
    return p


def main() -> None:
    sys.exit(asyncio.run(run(build_parser().parse_args())))


if __name__ == "__main__":
    main()
