# src/investigation_agent_platform/infrastructure/evidence/code/git.py
"""PyGit2 adapter for retrieving git repository code evidence and history."""

import asyncio
import hashlib
import logging
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pygit2
from opentelemetry import trace

from investigation_agent_platform.domain.common.exceptions import ExecutionError
from investigation_agent_platform.domain.evidence.models import (
    ClassificationLevel,
    Evidence,
    EvidenceType,
)
from investigation_agent_platform.domain.profile.models import CodeProfile
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

    def _require_tenant_scope(self, tenant_id: str, profile: CodeProfile) -> None:
        from investigation_agent_platform.domain.common.exceptions import (
            SecurityPolicyViolationException,
        )

        if not tenant_id or tenant_id == "anonymous":
            raise SecurityPolicyViolationException("Code access requires an authenticated tenant")
        if self._tenant_allowlist is None:
            logger.warning(
                "Git repository access without tenant allow-list (dev mode)",
                extra={"tenant_id": tenant_id, "repository": profile.repository},
            )
            return
        if profile.repository not in self._tenant_allowlist.get(tenant_id, set()):
            raise SecurityPolicyViolationException(
                f"Tenant '{tenant_id}' is not authorized for repository '{profile.repository}'"
            )

    async def get_source(
        self, tenant_id: str, investigation_id: UUID, file_path: str, profile: CodeProfile
    ) -> Evidence:
        """Asynchronously fetch bounded source code snippet from git tree."""
        with tracer.start_as_current_span("PyGit2Adapter.get_source") as span:
            span.set_attribute("tenant_id", tenant_id)
            span.set_attribute("investigation_id", str(investigation_id))
            span.set_attribute("file_path", file_path)
            return await asyncio.to_thread(
                self._get_source_sync, tenant_id, investigation_id, file_path, profile
            )

    def _get_source_sync(
        self, tenant_id: str, investigation_id: UUID, file_path: str, profile: CodeProfile
    ) -> Evidence:
        self._require_tenant_scope(tenant_id, profile)
        self._validate_tree_path(file_path)
        repository = profile.repository
        repo_path = (self._repo_base_path / repository).resolve()
        if not repo_path.is_relative_to(self._repo_base_path) or repo_path.is_symlink():
            raise ExecutionError(f"Repository path traversal attempt blocked: {repository}")

        try:
            repo = pygit2.Repository(str(repo_path))
            commit = repo.revparse_single(profile.default_branch)
            entry = commit.tree[file_path]
            if not isinstance(entry, pygit2.Blob):
                raise ExecutionError(f"Path is not a blob: {file_path}")
            blob: pygit2.Blob = entry
            if blob.size > MAX_SOURCE_BYTE_SIZE:
                raise ExecutionError(
                    f"File size {blob.size} bytes exceeds maximum limit of {MAX_SOURCE_BYTE_SIZE}"
                )
            raw_text = blob.data.decode("utf-8", errors="replace")
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
        start = 0
        end = min(len(lines), MAX_SOURCE_LINE_COUNT)
        selected_content = "\n".join(lines[start:end])
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
            observed_at=now,
            retrieved_at=now,
            provenance=prov,
            freshness=EvidenceFreshness(observed_at=now, retrieved_at=now),
            classification=ClassificationLevel.INTERNAL,
            fingerprint=fingerprint,
        )

    async def get_code_history(
        self, tenant_id: str, investigation_id: UUID, path: str, profile: CodeProfile
    ) -> list[Evidence]:
        """Fetch git commit history for a specified file path."""
        with tracer.start_as_current_span("PyGit2Adapter.get_code_history") as span:
            span.set_attribute("tenant_id", tenant_id)
            span.set_attribute("investigation_id", str(investigation_id))
            span.set_attribute("file_path", path)
            return await asyncio.to_thread(
                self._get_code_history_sync, tenant_id, investigation_id, path, profile
            )

    async def compare_versions(
        self, tenant_id: str, source_ref: str, target_ref: str, profile: CodeProfile
    ) -> CodeDiffResult:
        """Diff two revisions within the tenant-scoped repository.

        Added for `CodeEvidenceProviderProtocol` parity (the selector's
        runtime protocol check rejected this adapter without it). Bounded:
        50 files, 20 KB of patch text per file, binary patches dropped.
        """
        with tracer.start_as_current_span("PyGit2Adapter.compare_versions") as span:
            span.set_attribute("tenant_id", tenant_id)
            return await asyncio.to_thread(
                self._compare_versions_sync, tenant_id, source_ref, target_ref, profile
            )

    def _compare_versions_sync(
        self, tenant_id: str, source_ref: str, target_ref: str, profile: CodeProfile
    ) -> CodeDiffResult:
        self._require_tenant_scope(tenant_id, profile)
        repository = profile.repository
        repo_path = (self._repo_base_path / repository).resolve()
        if not repo_path.is_relative_to(self._repo_base_path) or repo_path.is_symlink():
            raise ExecutionError(f"Repository path traversal attempt blocked: {repository}")

        repo = pygit2.Repository(str(repo_path))
        try:
            old = repo.revparse_single(source_ref)
            new = repo.revparse_single(target_ref)
        except KeyError as exc:
            raise ExecutionError(f"Unknown revision ref: {exc}") from exc
        old_tree = old.tree if isinstance(old, pygit2.Commit) else old
        new_tree = new.tree if isinstance(new, pygit2.Commit) else new
        diff = repo.diff(old_tree, new_tree)

        files_changed: list[str] = []
        diff_contents: dict[str, str] = {}
        for patch in diff:
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
        self, tenant_id: str, investigation_id: UUID, path: str, profile: CodeProfile
    ) -> list[Evidence]:
        self._require_tenant_scope(tenant_id, profile)
        self._validate_tree_path(path)
        repository = profile.repository
        repo_path = (self._repo_base_path / repository).resolve()
        if not repo_path.is_relative_to(self._repo_base_path) or repo_path.is_symlink():
            raise ExecutionError(f"Repository path traversal attempt blocked: {repository}")

        evidences: list[Evidence] = []
        try:
            repo = pygit2.Repository(str(repo_path))
            commit_target = repo.head.target
            walker = repo.walk(commit_target, pygit2.GIT_SORT_TIME)  # type: ignore[arg-type]
            count = 0

            limit = 50
            for commit in walker:
                if count >= limit:
                    break
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
