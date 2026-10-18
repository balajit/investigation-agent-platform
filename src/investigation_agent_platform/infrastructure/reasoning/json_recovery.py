# src/investigation_agent_platform/infrastructure/reasoning/json_recovery.py
"""Defensive LLM JSON recovery (Part 11.11).

Strict provider-native schema enforcement (`response_format: json_schema`,
F-027) remains primary. This module is a gated fallback for provider output
that is structurally JSON but textually wrapped or truncated: fence-strip,
direct parse, outermost-object scan, then brace/bracket-balance truncation
repair — in that order, first success wins.

The function is pure (no I/O) and never logs payloads: errors carry a
stable code, parse mode, repair list, and content digest — never a raw
response excerpt. Recovery beyond a direct parse requires a known schema
plus provider-confirmed truncation or explicit call-site permission, and is
refused outright for sensitive or destructive uses.
"""

from __future__ import annotations

import hashlib
import json
import logging
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from investigation_agent_platform.domain.common.provenance import ProvenanceRecord

logger = logging.getLogger(__name__)

#: Hard cap on recoverable input size (chars); larger inputs fail closed.
MAX_RECOVERY_CHARS = 256_000

#: Hard cap on parsed nesting depth; deeper structures fail closed.
MAX_NESTING_DEPTH = 32

#: Provider finish reason confirming the output was cut short.
TRUNCATION_FINISH_REASON = "length"

RECOVERY_PLUGIN_ID = "json-recovery"
RECOVERY_PLUGIN_VERSION = "1.0"


class RecoveryParseMode(StrEnum):
    """How the payload was obtained (Part 11.11 provenance)."""

    DIRECT = "direct"
    FENCE_STRIPPED = "fence_stripped"
    SCANNED = "scanned"
    REPAIRED_TRUNCATION = "repaired_truncation"


class JsonRecoveryError(Exception):
    """Typed recovery failure (Part 11.11).

    Carries a stable machine-readable `code` plus provenance fields. Never
    carries a raw response excerpt — only the SHA-256 digest.
    """

    def __init__(
        self,
        code: str,
        parse_mode: RecoveryParseMode | None = None,
        repairs: list[str] | None = None,
        schema_version: str = "1.0",
        content_digest: str = "",
    ) -> None:
        super().__init__(f"JSON recovery failed: {code}")
        self.code = code
        self.parse_mode = parse_mode
        self.repairs = list(repairs or [])
        self.schema_version = schema_version
        self.content_digest = content_digest


class RecoveryPermission(BaseModel):
    """Call-site permission for recovery beyond a direct parse."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = Field(..., min_length=1, max_length=32)
    truncation_confirmed: bool = Field(
        default=False,
        description="Provider metadata (finish_reason=length) confirms truncation.",
    )
    explicit_permission: bool = Field(
        default=False,
        description="Call site explicitly permits recovery for this call.",
    )
    sensitive: bool = Field(
        default=False,
        description="True when the payload/decision is sensitive: recovery refused.",
    )
    destructive: bool = Field(
        default=False,
        description="True when the result could mutate/authorize: recovery refused.",
    )


class JsonRecoveryResult(BaseModel):
    """Validated parse outcome with provenance (Part 11.11)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    parsed: dict[str, Any] | list[Any]
    parse_mode: RecoveryParseMode
    repairs: list[str] = Field(default_factory=list)
    finish_reason: str | None = Field(default=None, max_length=64)
    schema_version: str = Field(default="1.0", min_length=1, max_length=32)
    content_digest: str = Field(default="", max_length=128)

    def to_provenance(self) -> ProvenanceRecord:
        """Provenance linking the repaired output (no raw content)."""
        return ProvenanceRecord(
            plugin_id=RECOVERY_PLUGIN_ID,
            plugin_version=RECOVERY_PLUGIN_VERSION,
            schema_version=self.schema_version,
            input_digests={"response": self.content_digest},
            metadata={
                "parse_mode": self.parse_mode.value,
                "repairs": ",".join(self.repairs),
                "finish_reason": self.finish_reason or "",
            },
        )


def _digest(text: str) -> str:
    return f"sha256:{hashlib.sha256(text.encode()).hexdigest()}"


def _duplicate_guard(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    seen: set[str] = set()
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError(f"duplicate key: {key}")
        seen.add(key)
        result[key] = value
    return result


def _depth(value: Any) -> int:
    if isinstance(value, dict):
        return 1 + max((_depth(item) for item in value.values()), default=0)
    if isinstance(value, list):
        return 1 + max((_depth(item) for item in value), default=0)
    return 0


def _strict_parse(candidate: str) -> dict[str, Any] | list[Any]:
    """Parse with duplicate-key and nesting guards (fail-closed)."""
    try:
        parsed = json.loads(candidate, object_pairs_hook=_duplicate_guard)
    except ValueError as exc:
        message = str(exc)
        if message.startswith("duplicate key:"):
            raise JsonRecoveryError("DUPLICATE_KEY") from exc
        raise JsonRecoveryError("PARSE_FAILED") from exc
    if not isinstance(parsed, (dict, list)):
        raise JsonRecoveryError("NOT_AN_OBJECT")
    if _depth(parsed) > MAX_NESTING_DEPTH:
        raise JsonRecoveryError("NESTING_TOO_DEEP")
    return parsed


def _strip_fences(text: str) -> str | None:
    """Remove one pair of Markdown code fences; None when absent."""
    stripped = text.strip()
    if not stripped.startswith("```"):
        return None
    lines = stripped.splitlines()
    if len(lines) < 2 or not lines[-1].strip().startswith("```"):
        return None
    body = "\n".join(lines[1:-1])
    return body.strip() or None


def _scan_outermost(text: str) -> str | None:
    """Slice the first balanced {...} or [...] span; None when absent.

    String- and escape-aware; the first structural opener wins and only its
    balanced span is returned (bounded single attempt, no whole-text hunt).
    """
    start: int | None = None
    opener = ""
    for index, char in enumerate(text):
        if char in "{[":
            start = index
            opener = char
            break
    if start is None:
        return None
    stack = [opener]
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "{[":
            if index != start:
                stack.append(char)
        elif char in "}]":
            expected = "}" if stack[-1] == "{" else "]"
            if char != expected:
                return None
            stack.pop()
            if not stack:
                return text[start : index + 1]
    return None


def _repair_truncation(text: str) -> tuple[str, list[str]] | None:
    """Close an unterminated string plus open brackets/braces.

    Returns (repaired, repairs) or None when the text is not repairably
    truncated (mismatched closers, no structural opener).
    """
    start: int | None = None
    for index, char in enumerate(text):
        if char in "{[":
            start = index
            break
    if start is None:
        return None
    stack: list[str] = []
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "{[":
            stack.append(char)
        elif char in "}]":
            if not stack:
                return None
            expected = "}" if stack[-1] == "{" else "]"
            if char != expected:
                return None
            stack.pop()
    if not stack and not in_string:
        return None  # structurally complete: nothing to repair
    repairs: list[str] = []
    repaired = text[start:]
    if in_string:
        repaired += '"'
        repairs.append("close-string")
    while stack:
        opener = stack.pop()
        repaired += "}" if opener == "{" else "]"
        repairs.append(f"close-{'object' if opener == '{' else 'array'}")
    return repaired, repairs


def resilient_json_extract(
    text: str,
    *,
    permission: RecoveryPermission,
    finish_reason: str | None = None,
) -> JsonRecoveryResult:
    """Extract JSON defensively (Part 11.11).

    Order: direct parse → fence-strip → outermost scan → truncation repair.
    Direct parses need no permission; every later stage requires a known
    schema plus confirmed truncation or explicit call-site permission, and
    all recovery is refused for sensitive/destructive uses.
    """
    if not isinstance(text, str) or not text.strip():
        raise JsonRecoveryError(
            "EMPTY_INPUT", schema_version=permission.schema_version, content_digest=_digest("")
        )
    digest = _digest(text)
    if len(text) > MAX_RECOVERY_CHARS:
        raise JsonRecoveryError(
            "INPUT_TOO_LARGE",
            schema_version=permission.schema_version,
            content_digest=digest,
        )
    try:
        return JsonRecoveryResult(
            parsed=_strict_parse(text.strip()),
            parse_mode=RecoveryParseMode.DIRECT,
            finish_reason=finish_reason,
            schema_version=permission.schema_version,
            content_digest=digest,
        )
    except JsonRecoveryError as exc:
        if exc.code in ("DUPLICATE_KEY", "NESTING_TOO_DEEP", "NOT_AN_OBJECT"):
            exc.schema_version = permission.schema_version
            exc.content_digest = digest
            raise
        # PARSE_FAILED falls through to gated recovery below.
    if permission.sensitive or permission.destructive:
        raise JsonRecoveryError(
            "SENSITIVE_USE_DENIED",
            schema_version=permission.schema_version,
            content_digest=digest,
        )
    if not (permission.truncation_confirmed or permission.explicit_permission):
        raise JsonRecoveryError(
            "REPAIR_NOT_PERMITTED",
            schema_version=permission.schema_version,
            content_digest=digest,
        )
    fenced = _strip_fences(text)
    if fenced is not None:
        try:
            return JsonRecoveryResult(
                parsed=_strict_parse(fenced),
                parse_mode=RecoveryParseMode.FENCE_STRIPPED,
                repairs=["strip-fences"],
                finish_reason=finish_reason,
                schema_version=permission.schema_version,
                content_digest=digest,
            )
        except JsonRecoveryError as exc:
            if exc.code in ("DUPLICATE_KEY", "NESTING_TOO_DEEP", "NOT_AN_OBJECT"):
                exc.schema_version = permission.schema_version
                exc.content_digest = digest
                raise
    scanned = _scan_outermost(text)
    if scanned is not None:
        try:
            return JsonRecoveryResult(
                parsed=_strict_parse(scanned),
                parse_mode=RecoveryParseMode.SCANNED,
                repairs=["outermost-scan"],
                finish_reason=finish_reason,
                schema_version=permission.schema_version,
                content_digest=digest,
            )
        except JsonRecoveryError as exc:
            if exc.code in ("DUPLICATE_KEY", "NESTING_TOO_DEEP", "NOT_AN_OBJECT"):
                exc.schema_version = permission.schema_version
                exc.content_digest = digest
                raise
    if (
        (finish_reason or "") == TRUNCATION_FINISH_REASON
        or permission.truncation_confirmed
        or permission.explicit_permission
    ):
        # Repair on the scanned span when one exists (trailing prose after
        # a cut-off object), else on the raw text.
        repaired = _repair_truncation(scanned if scanned is not None else text)
        if repaired is not None:
            candidate, repairs = repaired
            try:
                return JsonRecoveryResult(
                    parsed=_strict_parse(candidate),
                    parse_mode=RecoveryParseMode.REPAIRED_TRUNCATION,
                    repairs=repairs,
                    finish_reason=finish_reason,
                    schema_version=permission.schema_version,
                    content_digest=digest,
                )
            except JsonRecoveryError as exc:
                if exc.code in ("DUPLICATE_KEY", "NESTING_TOO_DEEP", "NOT_AN_OBJECT"):
                    exc.schema_version = permission.schema_version
                    exc.content_digest = digest
                    raise
    raise JsonRecoveryError(
        "PARSE_FAILED",
        schema_version=permission.schema_version,
        content_digest=digest,
    )


def record_json_recovery(
    observability: Any | None,
    outcome: str,
    parse_mode: str,
    schema_version: str = "1.0",
    provider: str = "",
    model: str = "",
) -> None:
    """Emit bounded-cardinality recovery metrics (no sensitive content).

    Labels: outcome (success/repaired/failed), parse_mode, schema_version,
    provider, model — all low-cardinality by construction (provider/model
    truncated; tenant/request ids never labels).
    """
    if observability is None:
        return
    observability.record_metric(
        "json_recovery",
        1.0,
        {
            "outcome": outcome[:32],
            "parse_mode": parse_mode[:32],
            "schema_version": schema_version[:32],
            "provider": provider[:32],
            "model": model[:128],
        },
    )
    if outcome == "failed":
        logger.warning(
            "JSON recovery failed",
            extra={"parse_mode": parse_mode, "schema_version": schema_version},
        )
