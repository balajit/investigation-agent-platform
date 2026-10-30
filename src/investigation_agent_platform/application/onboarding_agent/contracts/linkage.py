# src/investigation_agent_platform/application/onboarding_agent/contracts/linkage.py
"""Coverage linkage contract: report identity, idempotency, content hashes.

Implements the canonicalization the design mandates (§5.1, AG-02b): canonical
JSON over explicitly listed fields, volatile metadata always excluded, identical
inputs hash identically regardless of when or where the analysis ran.

Floats are rejected outright: hash inputs must be JSON-safe
(str/int/bool/None/list/dict) with decimals pre-normalized to schema-defined
strings by the caller. Silent float formatting is a divergence source.

Unknown-field policy (binding): hash inputs are closed. ``report_content_hash``
rejects any body key outside the approved report-body field set instead of
hashing it silently — a producer/consumer version skew must fail loud, never
produce a hash over fields the verifier does not know. ``input_manifest_hash``
builds its manifest from explicitly listed entry fields for the same reason.

``CANONICALIZATION_VERSION`` is part of every hashed preimage. Bumping it
intentionally invalidates all prior hashes; the bump, its rationale, and the
test updates it requires are a reviewable unit, never a drive-by.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

CANONICALIZATION_VERSION = "1"

_APPROVED_REPORT_BODY_FIELDS = frozenset(
    {
        "request_id",
        "revision",
        "generation",
        "report_version",
        "registry_version",
        "catalog_version",
        "estate_sha",
        "input_manifest_hash",
        "analyzer_version",
        "model_version",
        "verdicts",
        "bridges",
        "gaps",
        "remediation",
        "limitations",
    }
)

_EXCLUDED_REPORT_BODY_FIELDS = frozenset(
    {
        "report_content_hash",
        "report_id",
        "produced_at",
        "filed_at",
        "filing",
        "audit",
        "duration_seconds",
        "hostname",
    }
)


class ReportLinkage(BaseModel):
    """The reference a readiness/activation decision holds to a report."""

    model_config = ConfigDict(extra="forbid")

    request_id: UUID
    revision: int = Field(ge=1)
    generation: int = Field(ge=1)
    report_id: UUID
    report_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


def analysis_idempotency_key(request_id: UUID, revision: int, generation: int) -> str:
    """Stable idempotency key for the coverage analysis activity."""
    if revision < 1 or generation < 1:
        raise ValueError("revision and generation must be >= 1")
    return f"{request_id}:{revision}:{generation}"


def canonical_json_bytes(payload: Mapping[str, Any]) -> bytes:
    """Canonical JSON bytes: sorted keys, no whitespace, UTF-8.

    Raises TypeError on float values so callers normalize decimals to
    schema-defined strings instead of inheriting platform float formatting.
    """

    def _reject_floats(node: Any) -> None:
        if isinstance(node, float):
            raise TypeError(
                "float values are not canonicalizable; pre-normalize to schema-defined strings"
            )
        if isinstance(node, Mapping):
            for value in node.values():
                _reject_floats(value)
        elif isinstance(node, (list, tuple)):
            for value in node:
                _reject_floats(value)

    _reject_floats(payload)
    return json.dumps(
        dict(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    """SHA-256 hex digest."""
    return hashlib.sha256(data).hexdigest()


def input_manifest_hash(
    *,
    sniffer_outputs: Sequence[Mapping[str, Any]],
    estate_sha: str,
    registry_version: str,
    catalog_version: str,
    generation: int,
) -> str:
    """Hash over the canonical input manifest (design §5.1).

    Each sniffer output contributes its content hash, tool identity, and schema
    version. Volatile metadata (produced_at, durations, hostnames) is never an
    input — identical evidence always hashes identically.
    """
    entries = []
    for output in sniffer_outputs:
        for key in ("content_hash", "tool", "tool_version", "schema_version"):
            if key not in output:
                raise ValueError(f"input manifest entry missing {key!r}")
        entries.append(
            {
                "content_hash": output["content_hash"],
                "tool": output["tool"],
                "tool_version": output["tool_version"],
                "schema_version": output["schema_version"],
            }
        )
    entries.sort(key=lambda entry: str(entry["content_hash"]))
    manifest = {
        "canonicalization_version": CANONICALIZATION_VERSION,
        "estate_sha": estate_sha,
        "registry_version": registry_version,
        "catalog_version": catalog_version,
        "generation": generation,
        "inputs": entries,
    }
    return sha256_hex(canonical_json_bytes(manifest))


def report_content_hash(report_body: Mapping[str, Any]) -> str:
    """Hash over the canonical report body (design §5.1).

    Covers verdicts, confidence, evidence pointers, bridges, gaps,
    remediation, and limitations. Excludes the hash itself, the report
    identity envelope, volatile metadata, and any workflow filing envelope.

    Raises ValueError on any key outside the approved body field set: unknown
    fields are rejected, never hashed silently (see module unknown-field
    policy).
    """
    unknown = set(report_body) - _APPROVED_REPORT_BODY_FIELDS - _EXCLUDED_REPORT_BODY_FIELDS
    if unknown:
        raise ValueError(
            "unknown report body fields rejected from content hash: " + ", ".join(sorted(unknown))
        )
    body = {key: value for key, value in report_body.items() if key in _APPROVED_REPORT_BODY_FIELDS}
    body["canonicalization_version"] = CANONICALIZATION_VERSION
    return sha256_hex(canonical_json_bytes(body))
