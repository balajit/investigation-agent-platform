# src/investigation_agent_platform/infrastructure/artifacts/s3_store.py
"""S3-compatible artifact adapter via minio (Part 11.3A).

The minio client is synchronous; all calls are offloaded with
``asyncio.to_thread`` so event loops never block. Bucket creation is lazy on
first ``put``. Signed URLs use presigned GET with a clamped TTL.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
from typing import Any, cast
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

_MAX_SIGNED_URL_TTL = 86_400


class S3ArtifactStore:
    """S3-compatible artifact store (MinIO, AWS S3, or any S3 API)."""

    def __init__(
        self,
        endpoint_url: str,
        bucket_name: str,
        access_key: str,
        secret_key: str,
        region: str = "us-east-1",
        secure: bool = True,
    ) -> None:
        from minio import Minio

        host = endpoint_url.replace("https://", "").replace("http://", "").rstrip("/")
        if endpoint_url.startswith("http://"):
            secure = False
        self._client = Minio(
            host, access_key=access_key, secret_key=secret_key, secure=secure, region=region
        )
        self._bucket = bucket_name

    async def _ensure_bucket(self) -> None:
        exists = await asyncio.to_thread(self._client.bucket_exists, self._bucket)
        if not exists:
            await asyncio.to_thread(self._client.make_bucket, self._bucket)

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
        await self._ensure_bucket()
        await asyncio.to_thread(
            self._client.put_object,
            self._bucket,
            object_key,
            io.BytesIO(content),
            len(content),
            content_type=content_type,
            metadata=cast("dict[str, Any] | None", meta or None),
        )
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
        response = await asyncio.to_thread(self._client.get_object, self._bucket, object_key)
        try:
            content = await asyncio.to_thread(response.read)
        finally:
            response.close()
            response.release_conn()
        return ArtifactObject(ref=ref, content=content, metadata={})

    async def list(
        self,
        scope: CapabilityScope,
        prefix: str,
        cursor: str | None = None,
        limit: int = 50,
    ) -> ArtifactPage:
        _ = cursor
        if prefix:
            build_object_key(scope.tenant_id, scope.application_id, prefix.rstrip("/") + "/x")
        full_prefix = (
            f"{scope.tenant_id}/{scope.application_id or '-'}/{prefix}" if prefix else None
        )
        scope_prefix = f"{scope.tenant_id}/{scope.application_id or '-'}/"
        items: list[ArtifactRef] = []
        objects = await asyncio.to_thread(
            lambda: list(
                self._client.list_objects(self._bucket, prefix=full_prefix, recursive=True)
            )
        )
        for obj in objects[: max(1, min(limit, 200))]:
            name = obj.object_name or ""
            if not name.startswith(scope_prefix):
                continue
            items.append(
                ArtifactRef(
                    artifact_id=uuid4(),
                    key=name[len(scope_prefix) :],
                    content_type="application/octet-stream",
                    size_bytes=int(obj.size or 0),
                )
            )
        return ArtifactPage(items=items, next_cursor=None, has_more=False)

    async def delete(self, scope: CapabilityScope, ref: ArtifactRef) -> None:
        object_key = build_object_key(scope.tenant_id, scope.application_id, ref.key)
        await asyncio.to_thread(self._client.remove_object, self._bucket, object_key)

    async def signed_url(
        self, scope: CapabilityScope, ref: ArtifactRef, ttl_seconds: int = 3600
    ) -> str:
        from datetime import timedelta

        object_key = build_object_key(scope.tenant_id, scope.application_id, ref.key)
        ttl = max(60, min(ttl_seconds, _MAX_SIGNED_URL_TTL))
        return await asyncio.to_thread(
            self._client.presigned_get_object,
            self._bucket,
            object_key,
            timedelta(seconds=ttl),
        )
