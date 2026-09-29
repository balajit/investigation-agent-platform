#!/usr/bin/env python3
"""Cognee enrichment pilot driver (Part 8 owned).

The Part 7 loader knows nothing about Cognee; this script reuses its payload
builder as a library, then projects the payload through the Cognee pilot
adapter. Our models are the universe — AST flows one way, platform → Cognee.

Usage:
    IAP_COGNEE_ENABLED=true IAP_COGNEE_DATASET_SALT=<salt> \\
      uv run python scripts/cognee_pilot.py --repo <owner/name> --tenant-id <t>
    uv run python scripts/cognee_pilot.py --checkout ./tmp/repos/acme--pay \\
      --tenant-id <t> --repository-id acme--pay --revision <sha>
    uv run python scripts/cognee_pilot.py --drop --tenant-id <t> \\
      --repository-id <rid> --revision <sha>

Cognee's Neo4j connection is mapped from IAP_TOPOLOGY_NEO4J_* (operator glue,
no secrets written anywhere). Graph-only writes: no vector engine, no LLM.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = _REPO_ROOT / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def _load_loader():  # type: ignore[no-untyped-def]
    path = _REPO_ROOT / "scripts" / "ingest_github.py"
    spec = importlib.util.spec_from_file_location("ingest_github", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["ingest_github"] = module
    spec.loader.exec_module(module)
    return module


def _ownership_for(checkout: Path, tenant_id: str, clone_url: str) -> list[str]:
    """Derive the ownership path without writing anything (read-only).

    Org from the clone URL when known, domain from the checkout's CODEOWNERS
    catch-all; unresolvable stays unset, never manufactured.
    """
    from investigation_agent_platform.infrastructure.topology.ownership import (
        derive_git_organization,
        derive_repository_domain,
    )

    path: list[str] = []
    org = derive_git_organization(tenant_id, clone_url) if clone_url else None
    if org is not None:
        path.append(org.git_org_id)
    domain = derive_repository_domain(checkout)
    if domain:
        path.append(domain)
    return path


def _cognee_env_from_topology() -> None:
    """Map IAP_TOPOLOGY_NEO4J_* to the GRAPH_DATABASE_* vars Cognee reads."""
    mapping = {
        "GRAPH_DATABASE_PROVIDER": ("COGNEE_GRAPH_PROVIDER", "neo4j"),
        "GRAPH_DATABASE_URL": ("IAP_TOPOLOGY_NEO4J_URI", "bolt://localhost:7687"),
        "GRAPH_DATABASE_USERNAME": ("IAP_TOPOLOGY_NEO4J_USERNAME", "neo4j"),
        "GRAPH_DATABASE_PASSWORD": ("IAP_TOPOLOGY_NEO4J_PASSWORD", ""),
        "GRAPH_DATABASE_NAME": ("IAP_TOPOLOGY_NEO4J_DATABASE", "neo4j"),
    }
    for cognee_var, (iap_var, default) in mapping.items():
        os.environ.setdefault(cognee_var, os.environ.get(iap_var, default))


async def run(args: argparse.Namespace) -> int:
    from investigation_agent_platform.infrastructure.topology.cognee_adapter import (
        CogneeTopologyAdapter,
    )

    loader = _load_loader()
    tenant_id = args.tenant_id or os.environ.get("IAP_TENANT_ID", "")
    if not tenant_id:
        print("error: --tenant-id (or IAP_TENANT_ID) is required", file=sys.stderr)
        return 2

    if args.drop:
        if not args.repository_id or not args.revision:
            print("error: --drop requires --repository-id and --revision", file=sys.stderr)
            return 2
        adapter = CogneeTopologyAdapter(
            enabled=os.environ.get("IAP_COGNEE_ENABLED", "false").lower() == "true",
            dataset_salt=os.environ.get("IAP_COGNEE_DATASET_SALT", ""),
        )
        await adapter.drop_projection(tenant_id, args.repository_id, args.revision)
        print(f"dropped projection {tenant_id}/{args.repository_id}@{args.revision[:12]}")
        return 0

    if args.checkout:
        checkout = Path(args.checkout).resolve()
        if not args.repository_id or not args.revision:
            print("error: --checkout requires --repository-id and --revision", file=sys.stderr)
            return 2
        repository_id, sha = args.repository_id, args.revision
        full_name = repository_id
        clone_url = ""
    elif args.repo:
        token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
        if not token:
            print("error: GH_TOKEN (or GITHUB_TOKEN) is required to fetch", file=sys.stderr)
            return 2
        repo = loader.get_single_repo(args.repo, token)
        owner, _, name = repo.full_name.partition("/")
        sha = loader.resolve_sha(owner, name, repo.default_branch, token)
        workdir = Path(
            args.workdir or os.environ.get("IAP_CODE_REPO_BASE", "./tmp/repos")
        ).resolve()
        workdir.mkdir(parents=True, exist_ok=True)
        checkout = await asyncio.to_thread(
            loader.fetch_repo_at_sha, repo, sha, workdir, token, False
        )
        repository_id, full_name = loader.repository_id_for(repo.full_name), repo.full_name
        clone_url = repo.clone_url
    else:
        print("error: one of --repo, --checkout, or --drop is required", file=sys.stderr)
        return 2

    app_id = args.application_id or f"{tenant_id}-{repository_id}"
    payload, truncated = await loader.build_payload_for_repo(
        checkout, tenant_id, app_id, repository_id, sha
    )
    if truncated:
        print(f"error: payload truncated for {full_name}; refusing to project", file=sys.stderr)
        return 1

    _cognee_env_from_topology()
    adapter = CogneeTopologyAdapter(
        enabled=os.environ.get("IAP_COGNEE_ENABLED", "false").lower() == "true",
        dataset_salt=os.environ.get("IAP_COGNEE_DATASET_SALT", ""),
    )
    ownership_path = _ownership_for(checkout, tenant_id, clone_url)
    result = await adapter.project_snapshot(payload, ownership_path=ownership_path)
    print(
        f"COGNEE-PILOT {full_name}@{sha[:12]} dataset={result.dataset} "
        f"points={result.point_count} edges={result.edge_count} hash={result.projection_hash[:12]}"
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Project a repo payload into Cognee (pilot).")
    p.add_argument("--repo", help="Single repository OWNER/NAME (fetches at HEAD SHA)")
    p.add_argument(
        "--checkout", help="Local checkout dir (offline; needs --repository-id/--revision)"
    )
    p.add_argument("--drop", action="store_true", help="Drop one projection instead of projecting")
    p.add_argument("--tenant-id", default="")
    p.add_argument("--repository-id", default="")
    p.add_argument("--revision", default="")
    p.add_argument("--application-id", default="")
    p.add_argument("--workdir", default="")
    return p


def main() -> None:
    sys.exit(asyncio.run(run(build_parser().parse_args())))


if __name__ == "__main__":
    main()
