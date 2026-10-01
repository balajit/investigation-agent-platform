# src/investigation_agent_platform/infrastructure/reference/fetchers.py
"""Trusted reference-document fetchers (Part 11.6).

Git (shallow pygit2 clone), confined local paths, and S3 objects. Every
fetcher enforces endpoint allowlists (git/S3 via `validate_endpoint`),
resolved-root/symlink confinement (local), and clone/object/file/chunk
caps. Failures raise — the indexing service treats any fetch failure as a
failed run that keeps the prior generation active.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import tempfile
from pathlib import Path
from typing import Any

from investigation_agent_platform.domain.reference.documents import (
    MAX_REFERENCE_FILE_BYTES,
    MAX_REFERENCE_FILES,
    FetchedFile,
    GitSourceSpec,
    LocalPathSourceSpec,
    ReferenceDocumentSource,
    S3SourceSpec,
)
from investigation_agent_platform.infrastructure.security.endpoint_guard import (
    EndpointPolicy,
    validate_endpoint,
)

logger = logging.getLogger(__name__)

#: Clone/fetch timeout (seconds) bounding network operations.
FETCH_TIMEOUT_SECONDS = 300

#: Allowed env names for S3 credential SecretReferences (never arbitrary).
S3_SECRET_ALLOWLIST = frozenset({"IAP_REFDOC_S3_ACCESS_KEY", "IAP_REFDOC_S3_SECRET_KEY"})


def _hash_content(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _is_binary(sample: bytes) -> bool:
    return b"\x00" in sample[:8192]


def _decode_bounded(content: bytes) -> str:
    return content.decode("utf-8", errors="replace")


def _default_policy(allowed_hosts: frozenset[str] | None = None) -> EndpointPolicy:
    return EndpointPolicy(
        allowed_hosts=allowed_hosts or frozenset(),
        require_https=True,
        allow_loopback=False,
        timeout_seconds=30.0,
        max_response_bytes=MAX_REFERENCE_FILE_BYTES,
    )


class GitReferenceFetcher:
    """Shallow git clone at a pinned revision (Part 11.6)."""

    def __init__(self, endpoint_policy: EndpointPolicy | None = None) -> None:
        self._policy = endpoint_policy or _default_policy()

    async def fetch(self, tenant_id: str, source: ReferenceDocumentSource) -> list[FetchedFile]:
        if not isinstance(source, GitSourceSpec):
            raise TypeError(f"Git fetcher cannot handle {type(source).__name__}")
        url = validate_endpoint(source.repo_url, self._policy, purpose="reference-git")
        with tempfile.TemporaryDirectory(prefix="iap-refdoc-") as tmpdir:
            target = str(Path(tmpdir) / "repo")
            revision = await asyncio.wait_for(
                asyncio.to_thread(self._clone, url, target, source.revision),
                timeout=FETCH_TIMEOUT_SECONDS,
            )
            return self._walk_files(Path(target), source.paths, revision)

    def _clone(self, url: str, target: str, revision: str) -> str:
        import pygit2

        # Shallow fetch is unsupported by the local transport; local paths
        # clone fully (still bounded downstream by file/size caps).
        kwargs: dict[str, Any] = {} if "://" not in url else {"depth": 1}
        if revision not in ("HEAD", "") and len(revision) != 40:
            kwargs["checkout_branch"] = revision
        repo = pygit2.clone_repository(url, target, **kwargs)
        if len(revision) == 40:
            try:
                commit = repo.revparse_single(revision)
                repo.checkout(str(commit.id))
            except Exception as exc:
                raise RuntimeError(f"Cannot checkout revision {revision!r}: {exc}") from exc
        try:
            return str(repo.head.target)
        except Exception:
            return revision

    def _walk_files(self, root: Path, prefixes: list[str], revision: str) -> list[FetchedFile]:
        files: list[FetchedFile] = []
        skipped = 0
        for path in sorted(root.rglob("*")):
            if len(files) + skipped >= MAX_REFERENCE_FILES:
                break
            if ".git" in path.parts:
                continue
            if path.is_symlink() or not path.is_file():
                continue
            try:
                resolved = path.resolve()
                resolved.relative_to(root.resolve())
            except (OSError, ValueError):
                continue
            rel = str(path.relative_to(root))
            if prefixes and not any(
                rel == prefix or rel.startswith(prefix.rstrip("/") + "/") for prefix in prefixes
            ):
                continue
            try:
                if path.stat().st_size > MAX_REFERENCE_FILE_BYTES:
                    skipped += 1
                    continue
                content = path.read_bytes()
            except OSError:
                continue
            if _is_binary(content):
                skipped += 1
                continue
            files.append(
                FetchedFile(
                    path=rel,
                    content=_decode_bounded(content[:MAX_REFERENCE_FILE_BYTES]),
                    content_hash=_hash_content(content),
                    source_revision=revision,
                )
            )
        if skipped:
            logger.info("Reference git fetch skipped files", extra={"skipped": skipped})
        return files


class LocalPathReferenceFetcher:
    """Confined local-path fetch for development (Part 11.6)."""

    def __init__(self, allowed_roots: list[str] | None = None) -> None:
        self._roots = [Path(root).resolve() for root in (allowed_roots or [])]

    async def fetch(self, tenant_id: str, source: ReferenceDocumentSource) -> list[FetchedFile]:
        if not isinstance(source, LocalPathSourceSpec):
            raise TypeError(f"Local fetcher cannot handle {type(source).__name__}")
        root = Path(source.root).resolve()
        if self._roots and not any(
            root == allowed or root.is_relative_to(allowed) for allowed in self._roots
        ):
            raise RuntimeError(f"Local source root {source.root!r} is not allowlisted")
        if not root.is_dir():
            raise RuntimeError(f"Local source root {source.root!r} is not a directory")
        revision = f"local:{root}"
        files: list[FetchedFile] = []
        candidates = [root / prefix for prefix in source.paths] if source.paths else [root]
        for candidate in candidates:
            try:
                resolved = candidate.resolve()
                resolved.relative_to(root)
            except (OSError, ValueError):
                raise RuntimeError(f"Path escapes approved root: {candidate}")
            targets = [resolved] if resolved.is_file() else sorted(resolved.rglob("*"))
            for path in targets:
                if len(files) >= MAX_REFERENCE_FILES:
                    break
                if path.is_symlink() or not path.is_file():
                    continue
                try:
                    path.resolve().relative_to(root)
                except (OSError, ValueError):
                    continue
                try:
                    if path.stat().st_size > MAX_REFERENCE_FILE_BYTES:
                        continue
                    content = path.read_bytes()
                except OSError:
                    continue
                if _is_binary(content):
                    continue
                files.append(
                    FetchedFile(
                        path=str(path.relative_to(root)),
                        content=_decode_bounded(content[:MAX_REFERENCE_FILE_BYTES]),
                        content_hash=_hash_content(content),
                        source_revision=revision,
                    )
                )
        return files


class S3ReferenceFetcher:
    """S3 object fetch by explicit keys (Part 11.6)."""

    def __init__(self, endpoint_policy: EndpointPolicy | None = None) -> None:
        self._policy = endpoint_policy or _default_policy()

    async def fetch(self, tenant_id: str, source: ReferenceDocumentSource) -> list[FetchedFile]:
        if not isinstance(source, S3SourceSpec):
            raise TypeError(f"S3 fetcher cannot handle {type(source).__name__}")
        endpoint = validate_endpoint(source.endpoint, self._policy, purpose="reference-s3")
        from minio import Minio

        from investigation_agent_platform.application.extensions.registries import (
            resolve_secret,
        )

        access_key = (
            resolve_secret(source.access_key_ref, allowed_env_names=S3_SECRET_ALLOWLIST)
            if source.access_key_ref is not None
            else None
        )
        secret_key = (
            resolve_secret(source.secret_key_ref, allowed_env_names=S3_SECRET_ALLOWLIST)
            if source.secret_key_ref is not None
            else None
        )
        host = endpoint.replace("https://", "").replace("http://", "").rstrip("/")
        client = Minio(
            host,
            access_key=access_key,
            secret_key=secret_key,
            secure=endpoint.startswith("https://"),
        )
        try:
            version = await asyncio.wait_for(
                asyncio.to_thread(self._bucket_version, client, source.bucket),
                timeout=FETCH_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            raise RuntimeError(f"S3 source unreachable: {exc}") from exc
        files: list[FetchedFile] = []
        for key in source.keys:
            if len(files) >= MAX_REFERENCE_FILES:
                break
            if not key or key.startswith("/") or ".." in key.split("/"):
                raise RuntimeError(f"S3 key escapes scope: {key!r}")
            try:
                response = await asyncio.wait_for(
                    asyncio.to_thread(client.get_object, source.bucket, key),
                    timeout=FETCH_TIMEOUT_SECONDS,
                )
                try:
                    content = response.read(MAX_REFERENCE_FILE_BYTES + 1)
                finally:
                    response.close()
                    response.release_conn()
            except Exception as exc:
                raise RuntimeError(f"S3 fetch failed for {key!r}: {exc}") from exc
            if len(content) > MAX_REFERENCE_FILE_BYTES:
                continue
            if _is_binary(content):
                continue
            files.append(
                FetchedFile(
                    path=key,
                    content=_decode_bounded(content),
                    content_hash=_hash_content(content),
                    source_revision=version,
                )
            )
        return files

    def _bucket_version(self, client: Any, bucket: str) -> str:
        try:
            tags = client.get_bucket_versioning(bucket)
            status = getattr(tags, "status", None) or ""
        except Exception:
            status = ""
        return f"s3:{bucket}:{status or 'unversioned'}"


def fetcher_for(source: ReferenceDocumentSource) -> Any:
    """Resolve the trusted fetcher for a source spec (no tenant input)."""
    from investigation_agent_platform.domain.reference.documents import ReferenceSourceKind

    if source.kind == ReferenceSourceKind.GIT:
        return GitReferenceFetcher()
    if source.kind == ReferenceSourceKind.LOCAL_PATH:
        return LocalPathReferenceFetcher()
    return S3ReferenceFetcher()
