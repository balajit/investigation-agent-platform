# src/investigation_agent_platform/infrastructure/evidence/security.py
import logging
import re
from re import Pattern
from typing import Any, ClassVar

from opentelemetry import trace

from investigation_agent_platform.application.investigation.validator import _has_traversal
from investigation_agent_platform.domain.common.exceptions import SecurityPolicyViolationException
from investigation_agent_platform.domain.evidence.models import Evidence, RedactionEntry
from investigation_agent_platform.ports.security.redactor import (
    EvidenceSanitizerPort,
    QueryPolicyPort,
)

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)


class SensitiveDataRedactor(EvidenceSanitizerPort):
    """Redacts sensitive information (secrets, PII, credentials) recursively across all evidence fields."""

    _PATTERN_SPECS: ClassVar[list[tuple[str, Pattern[str], str]]] = [
        (
            "API_KEY",
            re.compile(
                r"(?i)(api_key|apikey|secret|token|password|passwd|client_secret)\s*=\s*['\"]?([a-zA-Z0-9_\-]{16,})['\"]?"
            ),
            "[REDACTED_API_KEY]",
        ),
        ("SSN", re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[REDACTED_SSN]"),
        ("CREDIT_CARD", re.compile(r"\b(?:\d[ -]*?){13,16}\b"), "[REDACTED_CREDIT_CARD]"),
        (
            "BEARER_TOKEN",
            re.compile(r"(?i)\bbearer\s+[a-z0-9._\-\+]{16,}"),
            "[REDACTED_BEARER_TOKEN]",
        ),
        (
            "JWT",
            re.compile(r"\beyJ[a-zA-Z0-9_\-]{10,}\.eyJ[a-zA-Z0-9_\-]{10,}\.[a-zA-Z0-9_\-]{10,}\b"),
            "[REDACTED_JWT]",
        ),
        ("AWS_ACCESS_KEY", re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED_AWS_ACCESS_KEY]"),
        (
            "GITHUB_TOKEN",
            re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b"),
            "[REDACTED_GITHUB_TOKEN]",
        ),
        (
            "PRIVATE_KEY",
            re.compile(r"-----BEGIN [A-Z][A-Z ]*PRIVATE KEY-----"),
            "[REDACTED_PRIVATE_KEY]",
        ),
        (
            "CONNECTION_STRING",
            re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)([^\s/@\"]+@)"),
            r"\1[REDACTED_CREDENTIALS]@",
        ),
    ]

    def _redact_with_manifest(self, text: str) -> tuple[str, list[RedactionEntry]]:
        redacted = text
        entries: list[RedactionEntry] = []
        for label, pattern, token in self._PATTERN_SPECS:
            matches = list(pattern.finditer(redacted))
            if not matches:
                continue
            original_length = sum(m.end() - m.start() for m in matches)
            redacted = pattern.sub(token, redacted)
            entries.append(
                RedactionEntry(
                    redaction_type=label,
                    original_length=original_length,
                    replacement_token=token,
                )
            )
        return redacted, entries

    def _redact_node(self, value: Any) -> tuple[Any, list[RedactionEntry]]:
        if isinstance(value, str):
            return self._redact_with_manifest(value)
        if isinstance(value, dict):
            entries: list[RedactionEntry] = []
            out: dict[str, Any] = {}
            for key, nested in value.items():
                redacted_nested, sub_entries = self._redact_node(nested)
                out[key] = redacted_nested
                entries.extend(sub_entries)
            return out, entries
        if isinstance(value, list):
            list_entries: list[RedactionEntry] = []
            list_out: list[Any] = []
            for nested in value:
                redacted_nested, sub_entries = self._redact_node(nested)
                list_out.append(redacted_nested)
                list_entries.extend(sub_entries)
            return list_out, list_entries
        return value, []

    async def redact_text(self, tenant_id: str, text_content: str) -> str:
        redacted, _ = self._redact_with_manifest(text_content)
        return redacted

    async def sanitize_evidence(self, tenant_id: str, evidence: Evidence) -> Evidence:
        return await self.redact_evidence(evidence)

    async def redact_evidence(self, evidence: Evidence) -> Evidence:
        with tracer.start_as_current_span("SensitiveDataRedactor.redact_evidence"):
            try:
                redacted_snippet, entries_content = self._redact_with_manifest(
                    evidence.content_snippet
                )
                redacted_title, entries_title = self._redact_with_manifest(evidence.title)
                redacted_summary, entries_summary = self._redact_with_manifest(evidence.summary)
                redacted_attributes, entries_attr = self._redact_node(evidence.attributes)

                all_entries = (
                    list(evidence.redaction_manifest)
                    + entries_content
                    + entries_title
                    + entries_summary
                    + entries_attr
                )

                return evidence.model_copy(
                    update={
                        "title": redacted_title,
                        "summary": redacted_summary,
                        "content_snippet": redacted_snippet,
                        "attributes": redacted_attributes,
                        "redaction_manifest": all_entries,
                        "is_redacted": bool(all_entries) or evidence.is_redacted,
                    }
                )
            except Exception as exc:
                logger.error("Fail-closed: evidence redaction encountered error", exc_info=exc)
                raise SecurityPolicyViolationException(f"Evidence redaction failed: {exc}") from exc


class QuerySafetyPolicy(QueryPolicyPort):
    """Enforces execution policies and parameter validation on runtime and state queries."""

    async def validate_runtime_request(self, tenant_id: str, request: Any) -> None:
        qs: str | None = getattr(request, "query_string", None)
        if qs:
            forbidden = ["script", "ctx._source", "doc[", "while(true)"]
            if any(term in qs.lower() for term in forbidden):
                raise SecurityPolicyViolationException(
                    "Runtime query contains illegal script or injection constructs."
                )
        # Reject raw ES body overrides if present
        if (
            getattr(request, "raw_es_body", None) is not None
            or getattr(request, "direct_api_url", None) is not None
        ):
            raise SecurityPolicyViolationException("Direct search body or URL override prohibited.")

    async def validate_state_request(
        self, tenant_id: str, template_id: str, parameters: dict[str, Any]
    ) -> None:
        if not template_id or not template_id.replace("_", "").isalnum():
            raise SecurityPolicyViolationException(
                f"Invalid or unsafe template ID format: {template_id}"
            )
        # Reject dynamic SQL injection in parameters
        for k, v in (parameters or {}).items():
            if isinstance(v, str) and any(
                tok in v.upper() for tok in ("; DROP", "; DELETE", "--", "/*")
            ):
                raise SecurityPolicyViolationException(
                    "Dynamic SQL fragment detected in state parameters."
                )

    async def validate_code_search_request(self, tenant_id: str, request: Any) -> None:
        query: str | None = getattr(request, "query", None)
        if query and _has_traversal(query):
            raise SecurityPolicyViolationException("Path traversal in code search query.")
        path_prefix: str | None = getattr(request, "path_prefix", None)
        if path_prefix and _has_traversal(path_prefix):
            raise SecurityPolicyViolationException("Path traversal in path_prefix.")

    async def validate_source_request(self, tenant_id: str, request: Any) -> None:
        file_path: str | None = getattr(request, "file_path", None)
        if file_path and (_has_traversal(file_path) or file_path.startswith("~")):
            raise SecurityPolicyViolationException("Path traversal attempt in source request.")

    async def validate_symbol_request(self, tenant_id: str, request: Any) -> None:
        symbol_name: str | None = getattr(request, "symbol_name", None)
        if symbol_name and _has_traversal(symbol_name):
            raise SecurityPolicyViolationException("Illegal traversal in symbol request.")

    async def validate_code_history_request(self, tenant_id: str, request: Any) -> None:
        file_path: str | None = getattr(request, "file_path", None)
        if file_path and _has_traversal(file_path):
            raise SecurityPolicyViolationException(
                "Path traversal attempt in code history request."
            )
