# src/investigation_agent_platform/infrastructure/sanitization/ingress.py
"""Ingress sanitization and payload processing engine."""

import asyncio
from typing import Protocol
from uuid import UUID

import structlog
from opentelemetry import trace
from pydantic import BaseModel, ConfigDict, Field
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from investigation_agent_platform.domain.common.exceptions import ExecutionError

logger = structlog.get_logger(__name__)
tracer = trace.get_tracer(__name__)


class RawEvidencePayload(BaseModel):
    """Raw evidence payload structure prior to sanitization."""

    id: UUID
    tenant_id: str
    investigation_id: UUID
    text_content: str
    metadata: dict[str, str] = Field(default_factory=dict)

    model_config = ConfigDict(frozen=True)


class SanitizedEvidenceArtifact(BaseModel):
    """Processed artifact resulting from ingress scrubbing."""

    id: UUID
    tenant_id: str
    investigation_id: UUID
    summary: str
    content_reference: str
    is_sanitized: bool

    model_config = ConfigDict(frozen=True)


class SanitizerAdapterProtocol(Protocol):
    """Port for text sanitization adapters."""

    def redact_pii_and_secrets(self, text: str) -> str:
        """Redact sensitive PII and keys from string content."""
        ...


class ObjectStorageClientProtocol(Protocol):
    """Port for tier-2 object storage persistence."""

    async def upload_payload(self, bucket: str, key: str, data: bytes) -> str:
        """Upload raw payload bytes returning content URI."""
        ...


class EvidenceIngressPipeline:
    """Orchestrates off-thread sanitization and resilient offloading."""

    def __init__(
        self,
        sanitizer: SanitizerAdapterProtocol,
        storage_client: ObjectStorageClientProtocol,
        bucket_name: str = "iap-evidence-sanitized",
    ) -> None:
        self._sanitizer = sanitizer
        self._storage_client = storage_client
        self._bucket_name = bucket_name

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception(lambda e: getattr(e, "retryable", False)),
        reraise=True,
    )
    async def _upload_with_retry(self, key: str, data_bytes: bytes) -> str:
        return await asyncio.wait_for(
            self._storage_client.upload_payload(
                bucket=self._bucket_name,
                key=key,
                data=data_bytes,
            ),
            timeout=30,
        )

    async def process_and_store(self, raw_payload: RawEvidencePayload) -> SanitizedEvidenceArtifact:
        """Processes raw text off-thread and uploads sanitized output safely."""
        with tracer.start_as_current_span("process_and_store_evidence") as span:
            span.set_attribute("tenant_id", raw_payload.tenant_id)
            span.set_attribute("investigation_id", str(raw_payload.investigation_id))

            logger.info(
                "Processing evidence payload",
                tenant_id=raw_payload.tenant_id,
                investigation_id=str(raw_payload.investigation_id),
                payload_id=str(raw_payload.id),
            )

            try:
                # Offload CPU-bound NLP sanitization to executor pool
                sanitized_text = await asyncio.to_thread(
                    self._sanitizer.redact_pii_and_secrets,
                    raw_payload.text_content,
                )

                s3_key = f"tenant/{raw_payload.tenant_id}/{raw_payload.investigation_id}/{raw_payload.id}.bin"
                content_uri = await self._upload_with_retry(
                    key=s3_key,
                    data_bytes=sanitized_text.encode("utf-8"),
                )

                summary = sanitized_text[:500] if len(sanitized_text) > 500 else sanitized_text

                return SanitizedEvidenceArtifact(
                    id=raw_payload.id,
                    tenant_id=raw_payload.tenant_id,
                    investigation_id=raw_payload.investigation_id,
                    summary=summary,
                    content_reference=content_uri,
                    is_sanitized=True,
                )
            except Exception as exc:
                logger.error(
                    "Ingress sanitization failed",
                    tenant_id=raw_payload.tenant_id,
                    investigation_id=str(raw_payload.investigation_id),
                    error=str(exc),
                    exc_info=True,
                )
                raise ExecutionError(
                    message="Failed to process and sanitize evidence payload",
                    details={"payload_id": str(raw_payload.id), "error": str(exc)},
                ) from exc