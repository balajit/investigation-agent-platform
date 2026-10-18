# src/investigation_agent_platform/infrastructure/artifacts/local_store.py
"""Resolved-root-confined local artifact adapter for development (Part 11.3A)."""

from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import uuid4

from investigation_agent_platform.domain.common.extension import CapabilityScope
from investigation_agent_platform.infrastructure.artifacts.keys import (
    MAX_ARTIFACT_BYTES,
    MAX_METADATA_ENTRIES,
    build_object_key,
)
from investigation_agent_platform.ports.artifacts.store import (
    ArtifactObject,
    ArtifactPage,
    ArtifactRef,
)


class LocalArtifactStore:
    """Filesystem artifact store confined to one resolved root directory."""

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root).expanduser().resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    def _resolve(self, object_key: str) -> Path:
        candidate = (self._root / object_key).resolve()
        if candidate != self._root and self._root not in candidate.parents:
            raise ValueError(f"artifact key escapes storage root: {object_key!r}")
        return candidate

    async def put(
        self,
        scope: CapabilityScope,
        key: str,
        content: bytes,
        content_type: str,
        metadata: dict[str, str] | None = None,
    ) -> ArtifactRef:
        if len(content) > MAX_ARTIFACT_BYTES:
            raise ValueError(f"artifact exceeds {MAX_ARTIFACT_BYTES} bytes")
        meta = dict(metadata or {})
        if len(meta) > MAX_METADATA_ENTRIES:
            raise ValueError("too many artifact metadata entries")
        object_key = build_object_key(scope.tenant_id, scope.application_id, key)
        path = self._resolve(object_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        digest = hashlib.sha256(content).hexdigest()
        return ArtifactRef(
            artifact_id=uuid4(),
            key=key,
            content_digest=f"sha256:{digest}",
            content_type=content_type,
            size_bytes=len(content),
        )

    async def get(self, scope: CapabilityScope, ref: ArtifactRef) -> ArtifactObject:
        object_key = build_object_key(scope.tenant_id, scope.application_id, ref.key)
        path = self._resolve(object_key)
        if not path.is_file():
            raise FileNotFoundError(f"artifact not found: {ref.key!r}")
        return ArtifactObject(ref=ref, content=path.read_bytes(), metadata={})

    async def list(
        self,
        scope: CapabilityScope,
        prefix: str,
        cursor: str | None = None,
        limit: int = 50,
    ) -> ArtifactPage:
        _ = cursor
        if prefix:
            # Reuse key validation without building a full key.
            build_object_key(scope.tenant_id, scope.application_id, prefix.rstrip("/") + "/x")
            base_prefix = f"{scope.tenant_id}/{scope.application_id or '-'}/{prefix}"
        else:
            base_prefix = f"{scope.tenant_id}/{scope.application_id or '-'}/"
        items: list[ArtifactRef] = []
        for path in sorted(self._root.rglob("*")):
            if not path.is_file():
                continue
            try:
                rel = path.relative_to(self._root).as_posix()
            except ValueError:
                continue
            if not rel.startswith(base_prefix):
                continue
            logical = rel[len(f"{scope.tenant_id}/{scope.application_id or '-'}/") :]
            items.append(
                ArtifactRef(
                    artifact_id=uuid4(),
                    key=logical,
                    content_type="application/octet-stream",
                    size_bytes=path.stat().st_size,
                )
            )
            if len(items) >= max(1, min(limit, 200)):
                break
        return ArtifactPage(items=items, next_cursor=None, has_more=False)

    async def delete(self, scope: CapabilityScope, ref: ArtifactRef) -> None:
        object_key = build_object_key(scope.tenant_id, scope.application_id, ref.key)
        path = self._resolve(object_key)
        try:
            path.unlink()
        except FileNotFoundError:
            return

    async def signed_url(
        self, scope: CapabilityScope, ref: ArtifactRef, ttl_seconds: int = 3600
    ) -> str:
        _ = ttl_seconds
        object_key = build_object_key(scope.tenant_id, scope.application_id, ref.key)
        self._resolve(object_key)
        return f"file://{self._root.as_posix()}/{object_key}"
