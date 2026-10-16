#!/usr/bin/env python3
"""Seed application profiles into the platform from YAML config files.

Loads one or more ``config/profiles/*.yaml`` documents through the production
loader (``load_application_profile_from_file``), then inserts each as an
immutable revision via ``SqlAlchemyApplicationProfileRepository.save``
(idempotent on ``(tenant_id, id, version)``). The profiles API is read-only,
so this script (or the DB) is the supported way to register a profile.

Usage:
    IAP_DATABASE_URI='postgresql+asyncpg://iap:iap@localhost:5432/iap' \\
      uv run python scripts/seed_profile.py --file config/profiles/example.yaml

    # whole directory, overriding the tenant embedded in the files:
    uv run python scripts/seed_profile.py --dir config/profiles --tenant-id tenant-b

    # validate + print without touching the database:
    uv run python scripts/seed_profile.py --dir config/profiles --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = _REPO_ROOT / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from investigation_agent_platform.domain.profile.models import ApplicationProfile
from investigation_agent_platform.infrastructure.configuration.config import (
    load_application_profile_from_file,
)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Seed application profiles from YAML.")
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument("--file", help="Path to a single profile YAML document.")
    source.add_argument("--dir", help="Directory of profile YAML documents (*.yaml).")
    p.add_argument(
        "--tenant-id",
        default="",
        help="Override the tenant_id embedded in each document "
        "(falls back to IAP_TENANT_ID, then the file value).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and print resolved profiles without writing to the database.",
    )
    return p


def _resolve_files(args: argparse.Namespace) -> list[Path]:
    if args.file:
        path = Path(args.file)
        if not path.is_file():
            raise SystemExit(f"error: profile file not found: {path}")
        return [path]
    directory = Path(args.dir)
    if not directory.is_dir():
        raise SystemExit(f"error: profile directory not found: {directory}")
    files = sorted(directory.glob("*.yaml"))
    if not files:
        raise SystemExit(f"error: no *.yaml profile documents in {directory}")
    return files


def _with_tenant(profile: ApplicationProfile, tenant_id: str) -> ApplicationProfile:
    if not tenant_id or tenant_id == profile.tenant_id:
        return profile
    return profile.model_copy(update={"tenant_id": tenant_id})


async def run(args: argparse.Namespace) -> int:
    files = _resolve_files(args)
    tenant_override = args.tenant_id or os.environ.get("IAP_TENANT_ID", "")

    profiles = [
        _with_tenant(load_application_profile_from_file(path), tenant_override) for path in files
    ]

    if args.dry_run:
        for path, profile in zip(files, profiles, strict=True):
            print(
                f"DRY-RUN {path.name}: tenant={profile.tenant_id} id={profile.id} "
                f"version={profile.version} status={profile.status.value} "
                f"environment={profile.environment} "
                f"mapping={profile.mapping_source_id} "
                f"services={[s.name for s in profile.services]} "
                f"datasources={[d.id for d in profile.datasources]}"
            )
        print(f"dry-run: {len(profiles)} profile(s) validated, nothing written")
        return 0

    db_uri = os.environ.get("IAP_DATABASE_URI")
    if not db_uri:
        print("error: IAP_DATABASE_URI is required (or pass --dry-run)", file=sys.stderr)
        return 2
    if db_uri.startswith("postgresql://"):
        # create_async_engine needs an async driver; the stock platform URI
        # uses the sync psycopg2 default scheme. Normalize to asyncpg instead
        # of crashing with "The asyncio extension requires an async driver".
        db_uri = "postgresql+asyncpg://" + db_uri[len("postgresql://") :]
        print("warn: normalized IAP_DATABASE_URI scheme to postgresql+asyncpg://", file=sys.stderr)

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from investigation_agent_platform.infrastructure.persistence.profile_repository import (
        SqlAlchemyApplicationProfileRepository,
    )

    engine = create_async_engine(db_uri)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    repo = SqlAlchemyApplicationProfileRepository(session_factory)
    written = 0
    try:
        for path, profile in zip(files, profiles, strict=True):
            await repo.save(profile.tenant_id, profile)
            # Read-after-write: prove the revision landed where the seeder
            # (and any Postgres-backed reader) looks. A silent write/read
            # split here once hid every seeded profile from the API.
            revisions = await repo.list_revisions(profile.tenant_id, profile.id)
            if not any(r.version == profile.version for r in revisions):
                print(
                    f"error: seeded revision not readable back: tenant={profile.tenant_id} "
                    f"id={profile.id} version={profile.version}",
                    file=sys.stderr,
                )
                return 1
            written += 1
            print(
                f"SEEDED {path.name}: tenant={profile.tenant_id} id={profile.id} "
                f"version={profile.version}"
            )
    finally:
        await engine.dispose()
    print(f"done: {written} profile revision(s) saved")
    _warn_on_api_divergence(profiles)
    return 0


def _warn_on_api_divergence(profiles: list) -> None:
    """Best-effort split-brain check against a locally running API.

    The seeder writes straight to Postgres, but an API running with
    process-local stores (IAP_BUFFER_ENABLED=false) serves memory and will
    404 these revisions. When a local API answers, compare; on mismatch warn
    loudly (advisory only — exit status stays 0 so boot ordering never
    breaks when the API simply is not up yet).
    """
    import json
    import urllib.request

    port = os.environ.get("PORT", "8000")
    for profile in profiles:
        url = f"http://localhost:{port}/api/v1/profiles/{profile.id}"
        req = urllib.request.Request(url, headers={"X-Tenant-ID": profile.tenant_id})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                body = json.load(resp)
        except Exception:
            continue  # API not running yet — nothing to compare against.
        try:
            seen_version = (body.get("profile") or {}).get("version")
        except Exception:
            seen_version = None
        if seen_version != profile.version:
            print(
                f"warn: local API does not serve the seeded revision "
                f"(tenant={profile.tenant_id} id={profile.id} "
                f"seeded_version={profile.version} api_version={seen_version}): "
                f"the API is likely on process-local stores "
                f"(IAP_BUFFER_ENABLED=false) while Postgres holds the data. "
                f"Unset IAP_BUFFER_ENABLED on the API/worker and restart them.",
                file=sys.stderr,
            )


def main() -> None:
    sys.exit(asyncio.run(run(build_parser().parse_args())))


if __name__ == "__main__":
    main()
