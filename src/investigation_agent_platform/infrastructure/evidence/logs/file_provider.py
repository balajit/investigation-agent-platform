# src/investigation_agent_platform/infrastructure/evidence/logs/file_provider.py
"""File-backed runtime log evidence provider (generic, profile-driven).

Reads application log files declared by ``file_log`` datasources on the
attached profile — no hardcoded paths, apps, or formats. Understands three
line shapes, newest-first:

- structured JSON (``message`` + ``service.{name,environment}`` +
  ``code.{file,line,function}`` + ``trace_id``/``span_id`` +
  ``exception.details`` + ``extra``);
- structlog JSON (``event``/``message`` + ``level`` + ``logger`` +
  ``timestamp`` + ``exception``);
- plain text (whole line is the message, severity INFO).

Security: every candidate path must resolve inside an allowlisted root
(``IAP_FILE_LOG_ROOTS``); ``..``, absolute escapes, and escaping symlinks
are refused. Files are read-only tail scans bounded per file and per call;
contents are never logged (counts and paths only). Cursor offsets are
HMAC-signed with the same cursor key as the Elastic provider.
"""

from __future__ import annotations

import asyncio
import glob
import hashlib
import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from opentelemetry import trace
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from investigation_agent_platform.domain.common.exceptions import ExecutionError
from investigation_agent_platform.domain.common.pagination import (
    CursorError,
    decode_cursor,
    encode_cursor,
)
from investigation_agent_platform.domain.evidence.models import (
    ClassificationLevel,
    Evidence,
    EvidenceType,
)
from investigation_agent_platform.domain.evidence.requests import RuntimeEvidenceRequest
from investigation_agent_platform.domain.profile.models import (
    ApplicationProfile,
    DatasourceKind,
)
from investigation_agent_platform.domain.provenance.models import (
    EvidenceFreshness,
    EvidenceProvenance,
    QueryFingerprint,
    SourceLocation,
)
from investigation_agent_platform.infrastructure.observability.tracing import traced
from investigation_agent_platform.ports.evidence.gateway import EvidenceQueryResult
from investigation_agent_platform.ports.evidence.mapping import EvidenceMappingLookup

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)

_PROVIDER_ID = "file-log"

_LEVEL_MAP = {
    "TRACE": "TRACE",
    "DEBUG": "DEBUG",
    "INFO": "INFO",
    "INFORMATION": "INFO",
    "WARN": "WARNING",
    "WARNING": "WARNING",
    "ERROR": "ERROR",
    "ERR": "ERROR",
    "CRITICAL": "ERROR",
    "FATAL": "ERROR",
}


class FileLogSettings(BaseModel):
    """Provider wiring (all non-secret; no credentials exist for files)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    roots: list[str] = Field(default_factory=lambda: ["data"])
    max_bytes_per_file: int = Field(default=2_097_152, ge=1024, le=104857600)
    max_files: int = Field(default=20, ge=1, le=200)
    cursor_signing_key: SecretStr = Field(default=SecretStr(""))


class _ParsedLine(BaseModel):
    """Normalized log line (internal; never persisted)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    timestamp: datetime | None = None
    level: str = "INFO"
    logger_name: str = ""
    service: str = ""
    function: str = ""
    message: str = ""
    trace_id: str | None = None
    exception_text: str | None = None
    log_tenant_hint: str = ""
    log_investigation_hint: str = ""
    raw: str = ""


def _parse_line(line: str) -> _ParsedLine:
    text = line.strip()
    if not text:
        return _ParsedLine(message="")
    try:
        doc = json.loads(text)
    except (ValueError, TypeError):
        return _ParsedLine(message=text[:4000], raw=text[:4000])
    if not isinstance(doc, dict):
        return _ParsedLine(message=text[:4000], raw=text[:4000])
    service = doc.get("service", {})
    code = doc.get("code", {})
    function = str(code.get("function", "") or "") if isinstance(code, dict) else ""
    message = str(
        doc.get("message", doc.get("event", doc.get("msg", doc.get("log", "")))) or ""
    )
    extra = doc.get("extra", {})
    tenant_hint, inv_hint = "", ""
    if isinstance(extra, dict):
        # Hints for triage display only — never trusted for tenant scoping.
        tenant_hint = str(extra.get("tenant_id", "") or "")
        inv_hint = str(extra.get("investigation_id", "") or "")
    timestamp = _parse_timestamp(doc.get("timestamp", doc.get("time", doc.get("@timestamp"))))
    level = _LEVEL_MAP.get(str(doc.get("level", doc.get("severity", "INFO"))).upper(), "INFO")
    exception = doc.get("exception")
    exception_text = None
    if isinstance(exception, dict):
        exception_text = str(exception.get("details", exception.get("message", "")) or "") or None
    elif exception:
        exception_text = str(exception)
    service_name = ""
    if isinstance(service, dict):
        service_name = str(service.get("name", "") or "")
    return _ParsedLine(
        timestamp=timestamp,
        level=level,
        logger_name=str(doc.get("logger", doc.get("name", "") or "")),
        service=service_name,
        function=function,
        message=message[:4000],
        trace_id=(str(doc.get("trace_id")) if doc.get("trace_id") else None),
        exception_text=(exception_text[:2000] if exception_text else None),
        log_tenant_hint=tenant_hint,
        log_investigation_hint=inv_hint,
        raw=text[:4000],
    )


def _parse_timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(float(value), tz=UTC)
        text = str(value).strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed
    except (ValueError, TypeError, OverflowError):
        return None


def _match_score(parsed: _ParsedLine, request: RuntimeEvidenceRequest) -> int:
    """Relevance score for one parsed line (0 = no match).

    Keyword semantics are OR with scoring, matching the Elastic provider's
    multi_match behavior: a line scores one point per distinct keyword found
    in its message/exception text, and any score above zero matches.
    Requiring EVERY keyword in a SINGLE line guaranteed empty results for
    multi-facet incident queries (e.g. ["profile", "iap", "tenant"]) against
    one-line-per-request access logs. Services, severities, and time range
    remain hard filters.
    """
    if request.services:
        haystack = f"{parsed.service} {parsed.logger_name}".lower()
        if not any(s.lower() in haystack for s in request.services):
            return 0
    if request.severities:
        wanted = {s.value if hasattr(s, "value") else str(s) for s in request.severities}
        if parsed.level not in wanted:
            return 0
    if request.time_range is not None and parsed.timestamp is not None:
        start = getattr(request.time_range, "start_time", None)
        end = getattr(request.time_range, "end_time", None)
        if start is not None and parsed.timestamp < start:
            return 0
        if end is not None and parsed.timestamp > end:
            return 0
    if not (parsed.message or parsed.exception_text):
        return 0
    if not request.keywords:
        return 1
    haystack = f"{parsed.message} {parsed.exception_text or ''}".lower()
    return sum(1 for k in request.keywords if k.lower() in haystack)


class FileLogAdapter:
    """``RuntimeEvidenceProviderProtocol`` over allowlisted log files."""

    def __init__(
        self,
        *,
        settings: FileLogSettings,
        provider_id: str = _PROVIDER_ID,
        project_root: str | Path | None = None,
    ) -> None:
        self._settings = settings
        self._provider_id = provider_id
        self._root = Path(project_root) if project_root else Path.cwd()
        self.profile: ApplicationProfile | None = None

    def _allowed_roots(self) -> list[Path]:
        roots: list[Path] = []
        for raw in self._settings.roots:
            candidate = (self._root / raw).resolve() if not os.path.isabs(raw) else Path(raw).resolve()
            roots.append(candidate)
        return roots

    def _resolve_files(self, profile: ApplicationProfile | None) -> list[Path]:
        """Candidate files from the profile's file_log datasources, newest first."""
        patterns: list[str] = []
        per_file_cap: dict[str, int] = {}
        if profile is not None:
            for ds in getattr(profile, "datasources", []) or []:
                if getattr(ds, "kind", None) != DatasourceKind.FILE_LOG:
                    continue
                spec = getattr(ds, "file_log", None)
                if spec is None:
                    continue
                for pattern in getattr(spec, "paths", []) or []:
                    patterns.append(pattern)
                    cap = getattr(spec, "max_bytes_per_file", None)
                    if cap:
                        per_file_cap[pattern] = int(cap)
        if not patterns:
            return []
        roots = self._allowed_roots()
        found: dict[str, Path] = {}
        for root in roots:
            if not root.is_dir():
                continue
            for pattern in patterns:
                if pattern.startswith("/") or ".." in Path(pattern).parts:
                    logger.warning(
                        "Refusing file-log pattern outside allowlisted roots",
                        extra={"pattern": pattern},
                    )
                    continue
                for match in sorted(glob.glob(str(root / pattern))):
                    candidate = Path(match)
                    try:
                        resolved = candidate.resolve()
                    except OSError:
                        continue
                    if not any(
                        resolved == root or root in resolved.parents for root in roots
                    ):
                        continue
                    if resolved.is_symlink() or not resolved.is_file():
                        continue
                    found[str(resolved)] = resolved
        files = sorted(found.values(), key=lambda p: p.stat().st_mtime, reverse=True)
        return files[: self._settings.max_files]

    @staticmethod
    def _tail_lines(path: Path, max_bytes: int) -> list[tuple[int, str]]:
        """Last ``max_bytes`` of a file as (1-based line number, text) pairs."""
        try:
            size = path.stat().st_size
            with open(path, encoding="utf-8", errors="replace") as handle:
                if size > max_bytes:
                    handle.seek(max(0, size - max_bytes))
                    handle.readline()
                lines = handle.read().splitlines()
            skipped = 0
            if size > max_bytes:
                with open(path, encoding="utf-8", errors="replace") as handle:
                    skipped = max(0, sum(1 for _ in handle) - len(lines))
            return [(skipped + index + 1, text) for index, text in enumerate(lines)]
        except OSError as exc:
            logger.warning("Skipping unreadable log file", extra={"error": str(exc)})
            return []

    def _to_evidence(
        self,
        *,
        tenant_id: str,
        investigation_id: UUID,
        path: Path,
        lineno: int,
        parsed: _ParsedLine,
    ) -> Evidence:
        now = datetime.now(UTC)
        rel = str(path)
        try:
            rel = str(path.relative_to(self._root))
        except ValueError:
            pass
        evidence_id = uuid5(NAMESPACE_URL, f"filelog:{tenant_id}:{rel}:{lineno}")
        fingerprint = hashlib.sha256(
            f"{tenant_id}:{rel}:{lineno}:{hashlib.sha256(parsed.raw.encode()).hexdigest()}".encode()
        ).hexdigest()
        body = parsed.message or "(empty log line)"
        if parsed.exception_text:
            body = f"{body}\n{parsed.exception_text}"
        prov = EvidenceProvenance(
            tenant_id=tenant_id,
            investigation_id=investigation_id,
            provider_type="FILE_LOG",
            requested_provider_id=self._provider_id,
            actual_provider_id=self._provider_id,
            source_system="file-log",
            retrieval_timestamp=now,
            query_fingerprint=QueryFingerprint(
                provider_type="FILE_LOG", operation="TAIL", normalized_query_hash=rel
            ),
            source_location=SourceLocation(system="file-log", identifier=f"{rel}:{lineno}"),
        )
        fresh = EvidenceFreshness(observed_at=parsed.timestamp, retrieved_at=now)
        return Evidence(
            tenant_id=tenant_id,
            investigation_id=prov.investigation_id,
            evidence_id=evidence_id,
            evidence_type=EvidenceType.LOG,
            provider="FILE_LOG",
            source=f"filelog://{rel}/{lineno}",
            title=f"Log line [{rel}:{lineno}]",
            summary=body[:2048],
            content_snippet=body[:4000],
            fingerprint=fingerprint,
            classification=ClassificationLevel.INTERNAL,
            observed_at=parsed.timestamp,
            retrieved_at=now,
            attributes={
                "file": rel,
                "line": lineno,
                "level": parsed.level,
                "logger": parsed.logger_name,
                "service": parsed.service,
                "function": parsed.function,
                "trace_id": parsed.trace_id,
                "log_tenant_hint": parsed.log_tenant_hint,
            },
            provenance=prov,
            freshness=fresh,
        )

    def _scan(
        self,
        *,
        tenant_id: str,
        investigation_id: UUID,
        request: RuntimeEvidenceRequest,
        profile: ApplicationProfile | None,
    ) -> list[Evidence]:
        files = self._resolve_files(profile or self.profile)
        if not files:
            return []
        scored: list[tuple[int, Evidence]] = []
        for path in files:
            for lineno, text in self._tail_lines(path, self._settings.max_bytes_per_file):
                parsed = _parse_line(text)
                score = _match_score(parsed, request)
                if score:
                    scored.append(
                        (
                            score,
                            self._to_evidence(
                                tenant_id=tenant_id,
                                investigation_id=investigation_id,
                                path=path,
                                lineno=lineno,
                                parsed=parsed,
                            ),
                        )
                    )
        # Rank: most keyword hits first. The sort is stable, so ties keep
        # collection order (newest files, then line order) and pagination
        # stays deterministic.
        scored.sort(key=lambda item: item[0], reverse=True)
        return [evidence for _, evidence in scored]

    @traced("provider", component="log_files")
    async def search_runtime_evidence(
        self,
        tenant_id: str,
        investigation_id: UUID,
        request: RuntimeEvidenceRequest,
        profile: ApplicationProfile,
        correlation_id: str | None = None,
    ) -> EvidenceQueryResult:
        with tracer.start_as_current_span("FileLogAdapter.search_runtime_evidence"):
            items = await asyncio.to_thread(
                self._scan,
                tenant_id=tenant_id,
                investigation_id=investigation_id,
                request=request,
                profile=profile,
            )
            limit = max(1, min(request.limit, 200))
            offset = 0
            if request.cursor:
                secret = self._settings.cursor_signing_key.get_secret_value()
                if not secret:
                    raise CursorError("File-log pagination requires a signing key.")
                try:
                    payload = decode_cursor(request.cursor, secret)
                    offset = max(0, int(payload.get("o", 0)))
                except (CursorError, ValueError, TypeError, AttributeError) as exc:
                    raise CursorError("Invalid file-log cursor.") from exc
            page = items[offset : offset + limit]
            has_more = len(items) > offset + limit
            cursor = None
            if has_more:
                secret = self._settings.cursor_signing_key.get_secret_value()
                if secret:
                    cursor = encode_cursor({"o": offset + limit}, secret)
            logger.info(
                "File-log search complete",
                extra={"files": len(self._resolve_files(profile)), "matched": len(items)},
            )
            return EvidenceQueryResult(
                items=page,
                cursor=cursor,
                total_count=len(items),
                has_more=has_more,
                provider_metadata={"provider_id": self._provider_id},
            )

    @traced("provider", component="log_files")
    async def get_runtime_evidence(
        self,
        tenant_id: str,
        investigation_id: UUID,
        evidence_id: UUID,
        environment: str,
        mapping_lookup: EvidenceMappingLookup,
        correlation_id: str | None = None,
        mapping_source_id: str | None = None,
    ) -> Evidence:
        """Fetch one entry by id via a bounded re-scan (ids are content-derived)."""
        found = await asyncio.to_thread(
            self._find_by_id,
            tenant_id=tenant_id,
            investigation_id=investigation_id,
            evidence_id=evidence_id,
        )
        if found is None:
            raise ExecutionError(f"File-log evidence {evidence_id} not found in scope.")
        return found

    def _find_by_id(
        self, *, tenant_id: str, investigation_id: UUID, evidence_id: UUID
    ) -> Evidence | None:
        """Bounded re-scan matching the content-derived id (no profile on this path)."""
        if self.profile is None:
            return None
        for path in self._resolve_files(self.profile):
            rel = str(path)
            try:
                rel = str(path.relative_to(self._root))
            except ValueError:
                pass
            for lineno, text in self._tail_lines(path, self._settings.max_bytes_per_file):
                if uuid5(NAMESPACE_URL, f"filelog:{tenant_id}:{rel}:{lineno}") == evidence_id:
                    parsed = _parse_line(text)
                    return self._to_evidence(
                        tenant_id=tenant_id,
                        investigation_id=investigation_id,
                        path=path,
                        lineno=lineno,
                        parsed=parsed,
                    )
        return None
