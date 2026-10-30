# src/investigation_agent_platform/application/onboarding_agent/contracts/coverage.py
"""Coverage Report contract shapes (agent design §5).

Per-claim confidence, stable evidence pointers, and validation rules that
reject silent gaps. Disposition vocabulary is platform-owned (D2.1/F-11);
values here are plain strings so the platform enum remains authoritative.
"""

from __future__ import annotations

import re
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

SHA256_HEX_RE = r"^[0-9a-f]{64}$"
SHELL_METACHAR_RE = re.compile(r"[|;&`$><\n\r]|\$\(")


class Confidence(StrEnum):
    VERIFIED = "VERIFIED"
    PROJECTED = "PROJECTED"
    SPECULATIVE = "SPECULATIVE"


class SupportTier(StrEnum):
    DEEP = "deep"
    GENERIC = "generic"
    COUNTED = "counted"
    INVISIBLE = "invisible"


class EvidencePointer(BaseModel):
    """Stable reference into a content-addressed sniffer output."""

    model_config = ConfigDict(extra="forbid")

    output_hash: str = Field(pattern=SHA256_HEX_RE)
    field_path: str = Field(min_length=1, max_length=1024)
    line_hint: int | None = Field(default=None, ge=1)


class Claim(BaseModel):
    """One verifiable statement with its own confidence and probe."""

    model_config = ConfigDict(extra="forbid")

    statement: str = Field(min_length=1)
    confidence: Confidence
    confirming_probe: str | None = Field(default=None, max_length=1024)
    evidence: list[EvidencePointer] = Field(default_factory=list)

    @model_validator(mode="after")
    def _require_probe_when_not_verified(self) -> Claim:
        if self.confidence in (Confidence.PROJECTED, Confidence.SPECULATIVE):
            if not self.confirming_probe:
                raise ValueError(f"{self.confidence.value} claims must name a confirming probe")
        return self


class CapabilityDisposition(BaseModel):
    """One capability verdict. Disposition values follow the platform enum."""

    model_config = ConfigDict(extra="forbid")

    capability: str = Field(min_length=1, max_length=128)
    disposition: str = Field(min_length=1, max_length=32)
    reasons: list[str] = Field(default_factory=list)


class ServiceVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service: str = Field(min_length=1, max_length=256)
    languages: list[str] = Field(default_factory=list)
    tier: SupportTier
    dispositions: list[CapabilityDisposition] = Field(default_factory=list)
    claims: list[Claim] = Field(default_factory=list)


class BridgeUse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool: str = Field(min_length=1, max_length=128)
    version_pin: str = Field(min_length=1, max_length=64)
    trust_state: str = Field(min_length=1, max_length=32)
    verification_evidence: list[str] = Field(min_length=1)
    caveats: list[str] = Field(default_factory=list)


class EnhancementGap(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=256)
    scoped_ask: str = Field(min_length=1)
    sizing_evidence: str = Field(default="")


class Remediation(BaseModel):
    """Operator guidance. Structured argv only — never shell text."""

    model_config = ConfigDict(extra="forbid")

    action: str = Field(min_length=1)
    command_argv: list[str] = Field(default_factory=list)
    expected_outcome: str = Field(default="")
    verification: str = Field(default="")

    @model_validator(mode="after")
    def _reject_shell_text(self) -> Remediation:
        for token in self.command_argv:
            if SHELL_METACHAR_RE.search(token):
                raise ValueError(
                    f"command_argv must be data, not shell text: metacharacter in {token!r}"
                )
        return self


class CoverageReport(BaseModel):
    """Versioned coverage report. Linkage lives here; intake stays untouched."""

    model_config = ConfigDict(extra="forbid")

    report_id: UUID
    request_id: UUID
    revision: int = Field(ge=1)
    generation: int = Field(ge=1)
    report_version: str = Field(min_length=1, max_length=32)
    registry_version: str = Field(min_length=1, max_length=32)
    catalog_version: str = Field(min_length=1, max_length=32)
    estate_sha: str = Field(min_length=1, max_length=128)
    input_manifest_hash: str = Field(pattern=SHA256_HEX_RE)
    report_content_hash: str = Field(pattern=SHA256_HEX_RE)
    analyzer_version: str = Field(min_length=1, max_length=64)
    model_version: str | None = Field(default=None, max_length=128)
    verdicts: list[ServiceVerdict] = Field(default_factory=list)
    bridges: list[BridgeUse] = Field(default_factory=list)
    gaps: list[EnhancementGap] = Field(default_factory=list)
    remediation: list[Remediation] = Field(default_factory=list)
    limitations: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def _require_limitations(self) -> CoverageReport:
        if not any(item.strip() for item in self.limitations):
            raise ValueError("a report without stated limitations is invalid")
        return self
