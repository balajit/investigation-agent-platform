#!/usr/bin/env python3
"""Batch GitHub org / single-repo loader for IAP Layer 3 topology (Part 7).

Usage:
    uv run python scripts/ingest_github.py --org <login> --tenant-id <tenant>
    uv run python scripts/ingest_github.py --repo <owner/name> --tenant-id <tenant>
    uv run python scripts/ingest_github.py --resume ./tmp/ingest-<org>.jsonl --tenant-id <tenant>

What it does, in order: list (or single get) -> pin SHA per repo -> fetch
(blobless, SHA-asserted) -> register org/repo identities (+ CODEOWNERS domain
when resolvable) -> parse working tree (TreeSitterParser, macro tier) ->
ingest one ASTTopologyPayload per repo@sha via TopologyIngestionPort ->
manifest row READY/FAILED/SKIPPED.

What it never does: no Temporal, no Kafka, no new REST endpoints, no synthetic
ownership. See docs/IAP-implementation-part7-knowledge-v1.md (D1-D7).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from investigation_agent_platform.infrastructure.topology.payload import (
    MAX_EDGES_PER_REPO,
    MAX_FILE_BYTES,
    MAX_FILES_PER_REPO,
    MAX_NODE_SYMBOLS,
    SKIP_DIRS,
    SKIP_FILES,
    SKIP_SUFFIXES,
    PayloadTruncated,
    build_payload_for_repo,
    walk_source_files,
)

# Re-exported for backward compatibility (tests/external imports resolve
# payload helpers through this module); canonical home is topology.payload.
__all__ = [
    "MAX_EDGES_PER_REPO",
    "MAX_FILES_PER_REPO",
    "MAX_FILE_BYTES",
    "MAX_NODE_SYMBOLS",
    "SKIP_DIRS",
    "SKIP_FILES",
    "SKIP_SUFFIXES",
    "PayloadTruncated",
    "build_payload_for_repo",
    "walk_source_files",
]

# The loader runs as `python scripts/ingest_github.py` where `src/` is not on
# sys.path by default. Bootstrap it so platform imports work outside `uv run`.
_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = _REPO_ROOT / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("iap.ingest_github")

GITHUB_API = "https://api.github.com"


@dataclass(frozen=True)
class RepoListing:
    full_name: str
    default_branch: str
    clone_url: str
    private: bool
    fork: bool
    archived: bool
    disabled: bool
    size_kb: int
    language: str | None
    pushed_at: str | None = None


class IngestManifestRow(BaseModel):
    """One line of the loader manifest. Written by the driver, never by the platform."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: str = Field(..., max_length=128)
    git_org: str = Field(..., max_length=128)
    repository_full_name: str = Field(..., max_length=256)
    repository_id: str = Field(..., max_length=128)
    default_branch: str = Field(..., max_length=128)
    revision_sha: str = Field(..., max_length=128)
    status: Literal["LISTED", "FETCHING", "READY", "FAILED", "SKIPPED"] = Field(...)
    node_count: int = Field(default=0, ge=0)
    edge_count: int = Field(default=0, ge=0)
    file_count: int = Field(default=0, ge=0)
    truncated: bool = Field(default=False)
    error_summary: str | None = Field(default=None, max_length=2000)


def _redacted(msg: str, token: str) -> str:
    return msg.replace(token, "[REDACTED]") if token else msg


def _github_request(path: str, token: str, max_attempts: int = 5) -> tuple[object, dict[str, str]]:
    """Minimal paginated-REST client (stdlib only, no new deps).

    Honors 403/429 rate limiting via `Retry-After` / `X-RateLimit-Reset` with
    exponential backoff (max_attempts total tries); other errors raise fast.
    """
    url = f"{GITHUB_API}{path}"
    attempts = 0
    while True:
        attempts += 1
        req = urllib.request.Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "iap-ingest-github/7.0",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:  # nosec B310 - pinned api.github.com over HTTPS
                headers = {k.lower(): v for k, v in resp.headers.items()}
                return json.loads(resp.read().decode("utf-8")), headers
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", errors="replace")[:500]
            except Exception:
                pass
            if exc.code in (403, 429) and attempts < max_attempts:
                wait = _rate_limit_wait(exc.headers, attempts)
                logger.warning(
                    "GitHub API %s for %s; retry %d/%d in %.0fs",
                    exc.code,
                    path,
                    attempts,
                    max_attempts,
                    wait,
                )
                time.sleep(wait)
                continue
            raise RuntimeError(f"GitHub API {exc.code} for {path}: {body}") from exc


def _rate_limit_wait(headers: object, attempts: int) -> float:
    """Seconds until retry: Retry-After first, then X-RateLimit-Reset, else backoff."""
    try:
        get = getattr(headers, "get", None)
        if callable(get):
            retry_after = get("Retry-After") or get("retry-after")
            if retry_after:
                return min(float(retry_after), 300.0)
            reset = get("X-RateLimit-Reset") or get("x-ratelimit-reset")
            if reset:
                return min(max(float(reset) - datetime.now(UTC).timestamp(), 1.0), 300.0)
    except (TypeError, ValueError):
        pass
    return min(2.0**attempts, 60.0)


def list_org_repos(
    org: str, token: str, include_forks: bool, include_archived: bool, include_private: bool = True
) -> tuple[list[RepoListing], list[tuple[str, str]]]:
    """List org repos; returns (repos, skipped) where skipped is (full_name, reason)."""
    repos: list[RepoListing] = []
    skipped: list[tuple[str, str]] = []
    path = f"/orgs/{org}/repos?per_page=100&type=all&sort=full_name"
    while path:
        data, headers = _github_request(path, token)
        assert isinstance(data, list)
        for item in data:
            name = item["full_name"]
            if item.get("disabled"):
                skipped.append((name, "disabled"))
                continue
            if item.get("archived") and not include_archived:
                skipped.append((name, "archived"))
                continue
            if item.get("fork") and not include_forks:
                skipped.append((name, "fork"))
                continue
            if item.get("private") and not include_private:
                skipped.append((name, "private"))
                continue
            if not item.get("size"):
                skipped.append((name, "empty"))
                continue
            repos.append(
                RepoListing(
                    full_name=item["full_name"],
                    default_branch=item.get("default_branch") or "main",
                    clone_url=item["clone_url"],
                    private=bool(item.get("private")),
                    fork=bool(item.get("fork")),
                    archived=bool(item.get("archived")),
                    disabled=bool(item.get("disabled")),
                    size_kb=int(item.get("size") or 0),
                    language=item.get("language"),
                    pushed_at=item.get("pushed_at"),
                )
            )
        link = headers.get("link", "")
        path = None
        for part in link.split(","):
            if 'rel="next"' in part:
                nxt = part.split(";")[0].strip().strip("<>")
                parsed = urllib.parse.urlparse(nxt)
                path = parsed.path + ("?" + parsed.query if parsed.query else "")
    return repos, skipped


def get_single_repo(full_name: str, token: str) -> RepoListing:
    owner, _, name = full_name.partition("/")
    if not owner or not name:
        raise ValueError("--repo must be OWNER/NAME")
    data, _ = _github_request(f"/repos/{owner}/{name}", token)
    assert isinstance(data, dict)
    return RepoListing(
        full_name=data["full_name"],
        default_branch=data.get("default_branch") or "main",
        clone_url=data["clone_url"],
        private=bool(data.get("private")),
        fork=bool(data.get("fork")),
        archived=bool(data.get("archived")),
        disabled=bool(data.get("disabled")),
        size_kb=int(data.get("size") or 0),
        language=data.get("language"),
        pushed_at=data.get("pushed_at"),
    )


def resolve_sha(owner: str, repo: str, branch: str, token: str) -> str:
    data, _ = _github_request(f"/repos/{owner}/{repo}/commits/{branch}", token)
    assert isinstance(data, dict)
    return str(data["sha"])


def _run_git(args: list[str], cwd: Path | None = None) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()[:1000]}")
    return proc.stdout.strip()


def authed_clone_url(clone_url: str, token: str) -> str:
    parsed = urllib.parse.urlparse(clone_url)
    return urllib.parse.urlunparse(parsed._replace(netloc=f"oauth2:{token}@{parsed.hostname}"))


def fetch_repo_at_sha(
    repo: RepoListing, sha: str, workdir: Path, token: str, full_clone: bool
) -> Path:
    """Clone/fetch with C git only; assert HEAD == sha. Returns checkout dir."""
    dest = workdir / repo.full_name.replace("/", "--")
    if dest.exists() and not (dest / ".git").exists():
        raise RuntimeError(f"Refusing to reuse non-git dir: {dest}")
    authed = authed_clone_url(repo.clone_url, token)
    try:
        if not dest.exists():
            args = ["clone", "--single-branch", "--branch", repo.default_branch]
            if not full_clone:
                args += ["--filter=blob:none"]
            _run_git([*args, authed, str(dest)])
        _run_git(["fetch", "origin", sha], cwd=dest)
        _run_git(["checkout", "--detach", sha], cwd=dest)
        head = _run_git(["rev-parse", "HEAD"], cwd=dest)
        if head != sha:
            raise RuntimeError(f"SHA mismatch after checkout: {head} != {sha}")
        return dest
    finally:
        # Never leave the token in the local git config.
        try:
            _run_git(["remote", "set-url", "origin", repo.clone_url], cwd=dest)
        except Exception:
            pass


def repository_id_for(full_name: str, prefix: str = "") -> str:
    slug = full_name.lower().replace("/", "--")
    safe = "".join(c if (c.isalnum() or c in "-_.") else "-" for c in slug)[:120]
    return f"{prefix}{safe}" if prefix else safe


def parse_repo_types(entries: list[str]) -> dict[str, str]:
    """Parse repeatable OWNER/NAME=TYPE mappings, validated against RepositoryType."""
    from investigation_agent_platform.domain.topology.models import RepositoryType

    valid = {t.value for t in RepositoryType}
    mapping: dict[str, str] = {}
    for entry in entries:
        name, sep, rtype = entry.partition("=")
        if not sep or not name.strip() or not rtype.strip():
            raise ValueError(f"--repo-types must be OWNER/NAME=TYPE, got: {entry!r}")
        rtype = rtype.strip().upper()
        if rtype not in valid:
            raise ValueError(f"unknown RepositoryType {rtype!r} (valid: {sorted(valid)})")
        mapping[name.strip().lower()] = rtype
    return mapping


def resolve_repo_type(full_name: str, default: str, mapping: dict[str, str]) -> str:
    rtype = mapping.get(full_name.lower(), default).upper()
    from investigation_agent_platform.domain.topology.models import RepositoryType

    valid = {t.value for t in RepositoryType}
    if rtype not in valid:
        raise ValueError(f"unknown RepositoryType {rtype!r} (valid: {sorted(valid)})")
    return rtype


async def _build_adapter():  # type: ignore[no-untyped-def]
    """One adapter per run: in-memory dev default, connected Neo4j when enabled."""
    if os.environ.get("IAP_TOPOLOGY_ENABLED", "false").lower() != "true":
        from investigation_agent_platform.infrastructure.topology.in_memory import (
            InMemoryTopologyAdapter,
        )

        return InMemoryTopologyAdapter()
    from pydantic import SecretStr

    from investigation_agent_platform.infrastructure.configuration.config import TopologyConfig
    from investigation_agent_platform.infrastructure.topology.neo4j_adapter import (
        Neo4jTopologyAdapter,
    )

    adapter = Neo4jTopologyAdapter(
        TopologyConfig(
            uri=SecretStr(os.environ.get("IAP_TOPOLOGY_NEO4J_URI", "")),
            username=SecretStr(os.environ.get("IAP_TOPOLOGY_NEO4J_USERNAME", "")),
            password=SecretStr(os.environ.get("IAP_TOPOLOGY_NEO4J_PASSWORD", "")),
            database=os.environ.get("IAP_TOPOLOGY_NEO4J_DATABASE", "neo4j"),
            encrypted=os.environ.get("IAP_TOPOLOGY_NEO4J_ENCRYPTED", "true").lower() == "true",
        )
    )
    await adapter.connect()
    return adapter


async def register_identities(
    checkout: Path,
    tenant_id: str,
    repo: RepoListing,
    repository_id: str,
    repo_type: str,
    adapter: object,
) -> dict[str, str | None]:
    """Upsert org + repository + CODEOWNERS domain before any graph write.

    Returns {"git_org_id", "domain_id"} for manifest logging. An unresolvable
    org or domain is a first-class outcome (left unset, logged), never
    manufactured — the ISSUE-7 invariant holds in batch as in single ingest.
    """
    from investigation_agent_platform.domain.topology.models import (
        RepositoryIdentity,
        RepositoryType,
    )
    from investigation_agent_platform.infrastructure.topology.ownership import (
        derive_git_organization,
        derive_repository_domain,
    )

    git_org = derive_git_organization(tenant_id, repo.clone_url)
    git_org_id = (
        git_org.git_org_id
        if git_org is not None
        else f"github:{repo.full_name.split('/')[0]}".lower()
    )
    domain_id = await asyncio.to_thread(derive_repository_domain, checkout)
    identity = RepositoryIdentity(
        tenant_id=tenant_id,
        repository_id=repository_id,
        name=repo.full_name,
        locator=repo.clone_url,
        git_org_id=git_org_id,
        repository_type=RepositoryType(repo_type),
        default_branch=repo.default_branch,
    )
    await adapter.register_ownership(identity, git_org, domain_id)  # type: ignore[union-attr]
    return {"git_org_id": git_org_id, "domain_id": domain_id}






async def parse_repo_to_payload(
    checkout: Path,
    tenant_id: str,
    application_id: str,
    repository_id: str,
    sha: str,
) -> dict:
    """Legacy dict-shape wrapper kept for backward compatibility.

    Prefer `build_payload_for_repo`, which returns a validated
    `ASTTopologyPayload`. This wrapper converts for callers that still expect
    the Phase 0 dict shape.
    """
    payload, truncated = await build_payload_for_repo(
        checkout, tenant_id, application_id, repository_id, sha
    )
    return {
        "schema_version": payload.schema_version,
        "parser_version": payload.parser_version,
        "tenant_id": payload.tenant_id,
        "application_id": payload.application_id,
        "repository_id": payload.repository_id,
        "revision": payload.revision,
        "snapshot_id": str(payload.snapshot_id),
        "source_files": [
            {"path": f.path, "language": f.language, "content_hash": f.content_hash}
            for f in payload.source_files
        ],
        "symbol_count": len(payload.ast_nodes),
        "diagnostics": [
            {"file_path": d.file_path, "message": d.message, "severity": d.severity}
            for d in payload.diagnostics
        ],
        "payload_hash": payload.payload_hash,
        "truncated": truncated,
    }


async def ingest_payload(payload: object, adapter: object, max_attempts: int = 3) -> dict:
    """Ingest one payload through `TopologyIngestionPort.ingest()`.

    Bounded retries apply ONLY to recognized transient provider failures
    (`TopologyProviderUnavailableError`); every other error fails the repo
    closed on first attempt. Returns a manifest-facing summary dict.
    """
    from investigation_agent_platform.domain.common.exceptions import (
        TopologyProviderUnavailableError,
    )

    attempts = 0
    while True:
        attempts += 1
        try:
            result = await adapter.ingest(payload)  # type: ignore[union-attr]
            return {
                "status": result.status.value,
                "snapshot_id": str(result.snapshot_id),
                "node_count": result.node_count,
                "edge_count": result.edge_count,
                "micro_skipped": result.micro_skipped_count,
                "error": result.error_summary,
            }
        except TopologyProviderUnavailableError as exc:
            if attempts >= max_attempts:
                return {
                    "status": "FAILED",
                    "error": f"provider unavailable after {attempts} attempts: {exc}",
                }
            await asyncio.sleep(2**attempts)


def manifest_row(
    tenant_id: str,
    org: str,
    repo: RepoListing,
    repository_id: str,
    sha: str,
    status: Literal["LISTED", "FETCHING", "READY", "FAILED", "SKIPPED"],
    counts: dict,
    error: str | None = None,
) -> IngestManifestRow:
    return IngestManifestRow(
        tenant_id=tenant_id,
        git_org=org,
        repository_full_name=repo.full_name,
        repository_id=repository_id,
        default_branch=repo.default_branch,
        revision_sha=sha,
        status=status,
        node_count=counts.get("node_count", 0),
        edge_count=counts.get("edge_count", 0),
        file_count=counts.get("file_count", 0),
        truncated=counts.get("truncated", False),
        error_summary=(error or "")[:2000] or None,
    )


def load_resume_map(path: Path) -> dict[tuple[str, str], str]:
    """Prior manifest {(full_name, sha): latest terminal status}.

    Only terminal states (READY/FAILED/SKIPPED) count; FETCHING/LISTED rows
    never mark a repo done, so interrupted runs retry them.
    """
    done: dict[tuple[str, str], str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = IngestManifestRow.model_validate_json(line)
        except Exception:
            continue
        if row.status in ("READY", "FAILED", "SKIPPED"):
            done[(row.repository_full_name, row.revision_sha)] = row.status
    return done


async def run(args: argparse.Namespace) -> int:
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    if not token and not args.dry_run:
        print("error: GH_TOKEN (or GITHUB_TOKEN) is required unless --dry-run", file=sys.stderr)
        return 2
    tenant_id = args.tenant_id or os.environ.get("IAP_TENANT_ID", "")
    if not tenant_id:
        print("error: --tenant-id (or IAP_TENANT_ID) is required", file=sys.stderr)
        return 2
    workdir = Path(args.workdir or os.environ.get("IAP_CODE_REPO_BASE", "./tmp/repos")).resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    manifest_path = Path(args.manifest) if args.manifest else None

    if args.repo:
        skipped: list[tuple[str, str]] = []
        repos = (
            [get_single_repo(args.repo, token)]
            if not args.dry_run
            else [RepoListing(args.repo, "main", "", False, False, False, False, 0, None)]
        )
        org = args.repo.split("/")[0]
    else:
        org = args.org
        repos, skipped = list_org_repos(
            org,
            token,
            args.include_forks,
            args.include_archived,
            include_private=not args.exclude_private,
        )
    if args.max_repos:
        repos = repos[: args.max_repos]
    print(
        f"org={org} tenant={tenant_id} repos={len(repos)} skipped={len(skipped)} workdir={workdir}"
    )

    if manifest_path is None:
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        manifest_path = Path(f"./tmp/ingest-{org}-{stamp}.jsonl")
    manifest_path.parent.mkdir(parents=True, exist_ok=True)

    resume_done: dict[tuple[str, str], str] = {}
    if args.resume:
        resume_done = load_resume_map(Path(args.resume))
        print(f"resume: {len(resume_done)} terminal rows from {args.resume}")

    try:
        repo_types_map = parse_repo_types(args.repo_types)
        resolve_repo_type("__default__", args.repo_type, {})  # validates --repo-type early
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    adapter = await _build_adapter()

    def emit(row: IngestManifestRow) -> None:
        mf.write(row.model_dump_json() + "\n")
        mf.flush()

    failures = 0
    skipped_count = 0
    # Sync manifest appends inside the async loop are deliberate: each row is
    # flushed immediately so a killed run keeps everything ingested so far
    # (resume reads the same file). Throughput is operator-scale, not hot-path.
    with manifest_path.open("a", encoding="utf-8") as mf:  # noqa: ASYNC230
        for name, reason in skipped:
            skipped_count += 1
            placeholder = RepoListing(name, "main", "", False, False, False, False, 0, None)
            emit(
                manifest_row(
                    tenant_id,
                    org,
                    placeholder,
                    repository_id_for(name, args.repository_id_prefix),
                    "",
                    "SKIPPED",
                    {},
                    f"skipped: {reason}",
                )
            )
            print(f"SKIPPED {name} ({reason})")
        for repo in repos:
            owner, _, name = repo.full_name.partition("/")
            rid = repository_id_for(repo.full_name, args.repository_id_prefix)
            try:
                rtype = resolve_repo_type(repo.full_name, args.repo_type, repo_types_map)
            except ValueError as exc:
                failures += 1
                emit(manifest_row(tenant_id, org, repo, rid, "unresolved", "FAILED", {}, str(exc)))
                print(f"FAILED {repo.full_name}: {exc}", file=sys.stderr)
                continue
            try:
                sha = repo.full_name and (
                    resolve_sha(owner, name, repo.default_branch, token)
                    if not args.dry_run
                    else "dry-run-sha"
                )
                assert sha
                if (repo.full_name, sha) in resume_done and resume_done[
                    (repo.full_name, sha)
                ] == "READY":
                    print(f"READY {repo.full_name}@{sha[:12]} (resume skip)")
                    continue
                if args.dry_run:
                    emit(manifest_row(tenant_id, org, repo, rid, sha, "LISTED", {}))
                    print(f"LISTED {repo.full_name} branch={repo.default_branch}")
                    continue
                emit(manifest_row(tenant_id, org, repo, rid, sha, "FETCHING", {}))
                checkout = await asyncio.to_thread(
                    fetch_repo_at_sha, repo, sha, workdir, token, args.full_clone
                )
                registration = await register_identities(
                    checkout, tenant_id, repo, rid, rtype, adapter
                )
                if registration["domain_id"]:
                    print(
                        f"registered {repo.full_name} org={registration['git_org_id']} "
                        f"domain={registration['domain_id']}"
                    )
                else:
                    print(
                        f"registered {repo.full_name} org={registration['git_org_id']} "
                        f"(no CODEOWNERS domain; attribution falls back)"
                    )
                app_id = args.application_id or f"{tenant_id}-{rid}"
                payload, truncated = await build_payload_for_repo(
                    checkout, tenant_id, app_id, rid, sha
                )
                counts = {
                    "node_count": len(payload.ast_nodes),
                    "edge_count": len(payload.calls) + len(payload.references),
                    "file_count": len(payload.source_files),
                    "truncated": truncated,
                }
                if truncated:
                    failures += 1
                    emit(
                        manifest_row(
                            tenant_id,
                            org,
                            repo,
                            rid,
                            sha,
                            "FAILED",
                            counts,
                            "truncated: payload caps exceeded; snapshot not marked READY",
                        )
                    )
                    print(
                        f"FAILED {repo.full_name}@{sha[:12]} truncated "
                        f"files={counts['file_count']} symbols={counts['node_count']}"
                    )
                    continue
                result = await ingest_payload(payload, adapter)
                status = result["status"]
                counts = {
                    "node_count": result.get("node_count") or counts["node_count"],
                    "edge_count": result.get("edge_count") or counts["edge_count"],
                    "file_count": counts["file_count"],
                    "truncated": False,
                }
                if status != "READY":
                    failures += 1
                emit(
                    manifest_row(
                        tenant_id, org, repo, rid, sha, status, counts, result.get("error")
                    )
                )
                print(
                    f"{status} {repo.full_name}@{sha[:12]} files={counts['file_count']} "
                    f"symbols={counts['node_count']} snapshot={result.get('snapshot_id', '-')[:8]}"
                )
            except Exception as exc:  # per-repo containment: rest of org continues
                failures += 1
                safe = _redacted(str(exc), token)[:1000]
                emit(manifest_row(tenant_id, org, repo, rid, "unresolved", "FAILED", {}, safe))
                print(f"FAILED {repo.full_name}: {safe}", file=sys.stderr)
    print(
        f"done: {len(repos) - failures} READY, {failures} FAILED, "
        f"{skipped_count} SKIPPED — manifest: {manifest_path}"
    )
    close = getattr(adapter, "close", None)
    if callable(close):
        await close()
    return 1 if failures else 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Batch-load a GitHub org or single repo into IAP Layer 3."
    )
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--org", help="GitHub organization login")
    src.add_argument("--repo", help="Single repository OWNER/NAME")
    p.add_argument(
        "--resume", default="", help="Prior manifest path: READY SHAs skip, FAILED retry"
    )
    p.add_argument("--tenant-id", default="", help="Tenant partition (or IAP_TENANT_ID)")
    p.add_argument("--workdir", default="", help="Checkout root (or IAP_CODE_REPO_BASE)")
    p.add_argument(
        "--manifest", default="", help="Manifest JSONL path (default ./tmp/ingest-<org>-<ts>.jsonl)"
    )
    p.add_argument(
        "--application-id", default="", help="Payload application_id (default <tenant>-<repo>)"
    )
    p.add_argument("--repository-id-prefix", default="", help="Prefix for derived repository_id")
    p.add_argument(
        "--repo-type",
        default="UNKNOWN",
        help="RepositoryType for all repos (unless --repo-types overrides)",
    )
    p.add_argument(
        "--repo-types",
        action="append",
        default=[],
        help="Repeatable per-repo override OWNER/NAME=TYPE",
    )
    p.add_argument("--include-forks", action="store_true")
    p.add_argument("--include-archived", action="store_true")
    p.add_argument(
        "--exclude-private", action="store_true", help="Skip private repos (included by default)"
    )
    p.add_argument("--full-clone", action="store_true", help="Full clone instead of blobless")
    p.add_argument("--max-repos", type=int, default=0, help="Cap repos for smoke tests (0 = all)")
    p.add_argument(
        "--dry-run", action="store_true", help="List + pin SHAs only, no fetch/parse/ingest"
    )
    return p


def main() -> None:
    sys.exit(asyncio.run(run(build_parser().parse_args())))


if __name__ == "__main__":
    main()
