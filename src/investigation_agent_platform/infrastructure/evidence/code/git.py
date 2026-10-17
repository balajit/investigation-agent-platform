from investigation_agent_platform.infrastructure.observability.tracing import traced

# src/investigation_agent_platform/infrastructure/evidence/code/git.py
"""PyGit2 adapter for retrieving git repository code evidence and history."""

import asyncio
import hashlib
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pygit2
from opentelemetry import trace

from investigation_agent_platform.domain.common.exceptions import (
    CodePathNotFoundError,
    ExecutionError,
)
from investigation_agent_platform.domain.evidence.models import (
    ClassificationLevel,
    Evidence,
    EvidenceType,
)
from investigation_agent_platform.domain.profile.models import ApplicationProfile
from investigation_agent_platform.domain.provenance.models import (
    EvidenceFreshness,
    EvidenceProvenance,
    QueryFingerprint,
    SourceLocation,
)
from investigation_agent_platform.ports.evidence.code import CodeDiffResult

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)

MAX_SOURCE_LINE_COUNT = 2000
MAX_SOURCE_BYTE_SIZE = 1_048_576  # 1MB text ceiling


class PyGit2Adapter:
    """Off-thread PyGit2 adapter for executing non-blocking repository traversals."""

    def __init__(
        self,
        repo_base_path: str,
        provider_id: str = "pygit2-local",
        tenant_repository_allowlist: dict[str, set[str]] | None = None,
    ) -> None:
        self._repo_base_path = Path(repo_base_path).resolve()
        self._provider_id = provider_id
        self._tenant_allowlist = tenant_repository_allowlist

    @staticmethod
    def _validate_tree_path(file_path: str) -> None:
        """Reject tree paths that could escape the git tree (F-041)."""
        from investigation_agent_platform.domain.common.exceptions import (
            SecurityPolicyViolationException,
        )

        if not file_path or "\x00" in file_path:
            raise SecurityPolicyViolationException("Invalid file path for git tree lookup")
        if file_path.startswith("/") or file_path.startswith("~"):
            raise SecurityPolicyViolationException("Absolute file paths are not permitted")
        normalized = file_path.replace("\\", "/").strip("/")
        if any(part in ("..", ".") for part in normalized.split("/")):
            raise SecurityPolicyViolationException(
                "Path traversal attempt blocked in git tree lookup"
            )

    @staticmethod
    def _service_locators(profile: ApplicationProfile) -> list[str]:
        """Checkout locators for every service: ``{org}/{repos[0]}``."""
        locators: list[str] = []
        for svc in profile.services:
            if svc.code.org and svc.code.repos:
                locators.append(f"{svc.code.org}/{svc.code.repos[0]}")
            elif svc.code.repos:
                locators.append(svc.code.repos[0])
            else:
                locators.append(svc.name)
        return locators

    @staticmethod
    def _primary_locator(profile: ApplicationProfile) -> tuple[str, str]:
        """Locator + branch of the primary service (fail-closed when none)."""
        svc = profile.primary_service
        if svc is None:
            raise ExecutionError("Application profile declares no services")
        locator = (
            f"{svc.code.org}/{svc.code.repos[0]}"
            if svc.code.org and svc.code.repos
            else (svc.code.repos[0] if svc.code.repos else svc.name)
        )
        return locator, svc.code.default_branch

    def _require_tenant_scope(self, tenant_id: str, profile: ApplicationProfile) -> None:
        from investigation_agent_platform.domain.common.exceptions import (
            SecurityPolicyViolationException,
        )

        if not tenant_id or tenant_id == "anonymous":
            raise SecurityPolicyViolationException("Code access requires an authenticated tenant")
        locators = self._service_locators(profile)
        if self._tenant_allowlist is None:
            logger.warning(
                "Git repository access without tenant allow-list (dev mode)",
                extra={"tenant_id": tenant_id, "repository": locators},
            )
            return
        allowed = self._tenant_allowlist.get(tenant_id, set())
        if not any(loc in allowed for loc in locators):
            raise SecurityPolicyViolationException(
                f"Tenant '{tenant_id}' is not authorized for repositories {locators}"
            )

    @traced("provider", component="code_git")
    async def get_source(
        self, tenant_id: str, investigation_id: UUID, file_path: str, profile: ApplicationProfile,
        revision: str | None = None,
        start_line: int | None = None,
        end_line: int | None = None,
    ) -> Evidence:
        """Asynchronously fetch bounded source code snippet from git tree."""
        with tracer.start_as_current_span("PyGit2Adapter.get_source") as span:
            span.set_attribute("tenant_id", tenant_id)
            span.set_attribute("investigation_id", str(investigation_id))
            span.set_attribute("file_path", file_path)
            return await asyncio.to_thread(
                self._get_source_sync, tenant_id, investigation_id, file_path, profile,
                revision, start_line, end_line,
            )

    def _get_source_sync(
        self,
        tenant_id: str,
        investigation_id: UUID,
        file_path: str,
        profile: ApplicationProfile,
        revision: str | None = None,
        start_line: int | None = None,
        end_line: int | None = None,
    ) -> Evidence:
        self._require_tenant_scope(tenant_id, profile)
        self._validate_tree_path(file_path)
        repository, branch = self._primary_locator(profile)
        repo_path = (self._repo_base_path / repository).resolve()
        if not repo_path.is_relative_to(self._repo_base_path) or repo_path.is_symlink():
            raise ExecutionError(f"Repository path traversal attempt blocked: {repository}")

        try:
            repo = pygit2.Repository(str(repo_path))
            # P1-2: honor an explicitly requested immutable revision instead
            # of silently substituting the default branch. Unknown revisions
            # are exploration misses (DEGRADED downstream), not outages.
            rev = (revision or "").strip() or branch
            try:
                commit = repo.revparse_single(rev)
            except Exception as exc:
                raise CodePathNotFoundError(
                    f"Revision not found in {repository}: {rev}"
                ) from exc
            try:
                entry = commit.tree[file_path]
            except KeyError as exc:
                raise CodePathNotFoundError(
                    f"Path not found in {repository}@{rev}: {file_path}"
                ) from exc
            if not isinstance(entry, pygit2.Blob):
                raise CodePathNotFoundError(f"Path is not a file: {file_path}")
            blob: pygit2.Blob = entry
            if blob.size > MAX_SOURCE_BYTE_SIZE:
                raise ExecutionError(
                    f"File size {blob.size} bytes exceeds maximum limit of {MAX_SOURCE_BYTE_SIZE}"
                )
            raw_text = blob.data.decode("utf-8", errors="replace")
        except CodePathNotFoundError:
            raise
        except Exception as exc:
            logger.error(
                "Git source fetch failure",
                exc_info=exc,
                extra={
                    "context": {"file": file_path, "repository": repository, "tenant_id": tenant_id}
                },
            )
            raise ExecutionError(f"Failed to fetch source for {file_path}: {exc}") from exc

        lines = raw_text.splitlines()
        # P1-2: explicit 1-indexed inclusive ranges return exactly the
        # requested window with numbered lines; otherwise the bounded head
        # (previous behavior). Provenance always records the ACTUAL range.
        numbered = False
        if start_line is not None or end_line is not None:
            lo = max(1, int(start_line or 1))
            hi = int(end_line or (lo + MAX_SOURCE_LINE_COUNT - 1))
            hi = min(hi, len(lines), lo + MAX_SOURCE_LINE_COUNT - 1)
            lo = min(lo, max(hi, 1))
            start, end = lo - 1, hi
            numbered = True
        else:
            start = 0
            end = min(len(lines), MAX_SOURCE_LINE_COUNT)
        # Evidence.content_snippet caps at 4096 chars: truncate long files to
        # the head (previously any file over ~4KB failed Evidence validation
        # and GET_CODE errored instead of returning a snippet).
        window = lines[start:end]
        if numbered:
            selected_content = "\n".join(
                f"{n}| {text}" for n, text in zip(range(start + 1, end + 1), window)
            )[:4000]
        else:
            selected_content = "\n".join(window)[:4000]
        try:
            observed_at = datetime.fromtimestamp(commit.commit_time, UTC)
        except Exception:
            observed_at = datetime.now(UTC)
        now = datetime.now(UTC)

        commit_sha = str(commit.id)
        query_hash = hashlib.sha256(
            f"{repository}:{file_path}:{commit_sha}:{start}:{end}".encode()
        ).hexdigest()

        prov = EvidenceProvenance(
            tenant_id=tenant_id,
            investigation_id=investigation_id,
            provider_type="GIT",
            requested_provider_id=self._provider_id,
            actual_provider_id=self._provider_id,
            source_system="Git",
            retrieval_timestamp=now,
            query_fingerprint=QueryFingerprint(
                provider_type="GIT",
                operation="GET_SOURCE",
                normalized_query_hash=query_hash,
            ),
            source_location=SourceLocation(
                system="Git",
                identifier=f"{repository}/{file_path}",
                file_path=file_path,
                line_start=start + 1,
                line_end=end,
                revision=commit_sha,
            ),
        )

        fingerprint = hashlib.sha256(
            f"{tenant_id}:{repository}:{file_path}:{commit_sha}:{selected_content}".encode()
        ).hexdigest()

        return Evidence(
            tenant_id=tenant_id,
            investigation_id=prov.investigation_id,
            evidence_type=EvidenceType.SOURCE_CODE,
            provider=self._provider_id,
            source=f"git://{repository}/{file_path}@{commit_sha}",
            title=f"Source: {file_path} (lines {start + 1}-{end})",
            summary=f"Code snippet from {repository}/{file_path} at revision {commit_sha[:8]}",
            content_snippet=selected_content,
            observed_at=observed_at,
            retrieved_at=now,
            provenance=prov,
            freshness=EvidenceFreshness(observed_at=observed_at, retrieved_at=now),
            classification=ClassificationLevel.INTERNAL,
            fingerprint=fingerprint,
        )

    @traced("provider", component="code_git")
    async def get_code_history(
        self, tenant_id: str, investigation_id: UUID, path: str, profile: ApplicationProfile
    ) -> list[Evidence]:
        """Fetch git commit history for a specified file path."""
        with tracer.start_as_current_span("PyGit2Adapter.get_code_history") as span:
            span.set_attribute("tenant_id", tenant_id)
            span.set_attribute("investigation_id", str(investigation_id))
            span.set_attribute("file_path", path)
            return await asyncio.to_thread(
                self._get_code_history_sync, tenant_id, investigation_id, path, profile
            )

    @traced("provider", component="code_git")
    async def compare_versions(
        self,
        tenant_id: str,
        source_ref: str,
        target_ref: str,
        profile: ApplicationProfile,
        investigation_id: UUID | None = None,
    ) -> CodeDiffResult:
        """Diff two revisions within the tenant-scoped repository.

        Added for `CodeEvidenceProviderProtocol` parity (the selector's
        runtime protocol check rejected this adapter without it). Bounded:
        50 files, 20 KB of patch text per file, binary patches dropped.
        """
        with tracer.start_as_current_span("PyGit2Adapter.compare_versions") as span:
            span.set_attribute("tenant_id", tenant_id)
            if investigation_id is not None:
                span.set_attribute("investigation_id", str(investigation_id))
            return await asyncio.to_thread(
                self._compare_versions_sync, tenant_id, source_ref, target_ref, profile
            )

    def _compare_versions_sync(
        self, tenant_id: str, source_ref: str, target_ref: str, profile: ApplicationProfile
    ) -> CodeDiffResult:
        self._require_tenant_scope(tenant_id, profile)
        repository, _ = self._primary_locator(profile)
        repo_path = (self._repo_base_path / repository).resolve()
        if not repo_path.is_relative_to(self._repo_base_path) or repo_path.is_symlink():
            raise ExecutionError(f"Repository path traversal attempt blocked: {repository}")

        repo = pygit2.Repository(str(repo_path))
        try:
            old = repo.revparse_single(source_ref)
            new = repo.revparse_single(target_ref)
        except KeyError as exc:
            raise ExecutionError(f"Unknown revision ref: {exc}") from exc
        if isinstance(old, pygit2.Commit) and isinstance(new, pygit2.Commit):
            diff = repo.diff(old, new)
        else:
            # Annotated tags or other non-commit objects: resolve via rev
            # strings (same trees; matches the diff() overloads).
            diff = repo.diff(source_ref, target_ref)

        files_changed: list[str] = []
        diff_contents: dict[str, str] = {}
        for patch in diff:
            if patch is None:
                continue
            if len(files_changed) >= 50:
                break
            path = patch.delta.new_file.path or patch.delta.old_file.path
            files_changed.append(path)
            try:
                text = patch.text or ""
            except (ValueError, UnicodeDecodeError):
                continue  # binary patch: name recorded, content dropped
            diff_contents[path] = text[:20_480]
        return CodeDiffResult(
            source_ref=source_ref,
            target_ref=target_ref,
            files_changed=files_changed,
            diff_contents=diff_contents,
        )

    def _get_code_history_sync(
        self, tenant_id: str, investigation_id: UUID, path: str, profile: ApplicationProfile
    ) -> list[Evidence]:
        self._require_tenant_scope(tenant_id, profile)
        self._validate_tree_path(path)
        # P1-2: resolve through the primary-service locator like get_source
        # (ApplicationProfile has no `repository` attribute — the old access
        # raised AttributeError on every call).
        repository, branch = self._primary_locator(profile)
        repo_path = (self._repo_base_path / repository).resolve()
        if not repo_path.is_relative_to(self._repo_base_path) or repo_path.is_symlink():
            raise ExecutionError(f"Repository path traversal attempt blocked: {repository}")

        def _tree_entry_id(commit: Any, path: str) -> str | None:
            try:
                entry = commit.tree[path]
            except KeyError:
                return None
            return str(entry.id)

        evidences: list[Evidence] = []
        try:
            repo = pygit2.Repository(str(repo_path))
            tip = repo.revparse_single(branch)
            walker = repo.walk(tip.id, pygit2.GIT_SORT_TIME)  # type: ignore[arg-type]
            count = 0

            limit = 50
            for commit in walker:
                if count >= limit:
                    break
                # P1-2: only commits that actually changed the requested path.
                # Compare the tree entry against the first parent; root
                # commits count when the path exists in them.
                current_id = _tree_entry_id(commit, path)
                parents = list(commit.parents)
                if parents:
                    parent_id = _tree_entry_id(parents[0], path)
                    if current_id is None or current_id == parent_id:
                        continue
                elif current_id is None:
                    continue
                commit_sha = str(commit.id)
                now = datetime.now(UTC)
                commit_time = datetime.fromtimestamp(commit.commit_time, tz=UTC)

                query_hash = hashlib.sha256(
                    f"{repository}:{path}:{commit_sha}".encode()
                ).hexdigest()
                prov = EvidenceProvenance(
                    tenant_id=tenant_id,
                    investigation_id=investigation_id,
                    provider_type="GIT",
                    requested_provider_id=self._provider_id,
                    actual_provider_id=self._provider_id,
                    source_system="Git",
                    retrieval_timestamp=now,
                    query_fingerprint=QueryFingerprint(
                        provider_type="GIT",
                        operation="GET_CODE_HISTORY",
                        normalized_query_hash=query_hash,
                    ),
                    source_location=SourceLocation(
                        system="Git",
                        identifier=f"{repository}/{path}",
                        file_path=path,
                        revision=commit_sha,
                    ),
                )

                fingerprint = hashlib.sha256(
                    f"{tenant_id}:{repository}:{commit_sha}".encode()
                ).hexdigest()
                evidences.append(
                    Evidence(
                        tenant_id=tenant_id,
                        investigation_id=prov.investigation_id,
                        evidence_type=EvidenceType.COMMIT_HISTORY,
                        provider=self._provider_id,
                        source=f"git://{repository}/commit/{commit_sha}",
                        title=f"Commit: {commit_sha[:8]} by {commit.author.name}",
                        summary=commit.message.strip(),
                        content_snippet=f"Author: {commit.author.name} <{commit.author.email}>\nDate: {commit_time.isoformat()}\n\n{commit.message}",
                        observed_at=commit_time,
                        retrieved_at=now,
                        provenance=prov,
                        freshness=EvidenceFreshness(observed_at=commit_time, retrieved_at=now),
                        classification=ClassificationLevel.INTERNAL,
                        fingerprint=fingerprint,
                    )
                )
                count += 1
            return evidences
        except Exception as exc:
            logger.error(
                "Git commit history fetch failure",
                exc_info=exc,
                extra={"context": {"file": path, "repository": repository}},
            )
            raise ExecutionError(f"Failed to fetch commit history for {path}: {exc}") from exc
