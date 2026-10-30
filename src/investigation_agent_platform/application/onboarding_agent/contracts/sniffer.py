# src/investigation_agent_platform/application/onboarding_agent/contracts/sniffer.py
"""Sniffer output contracts v1 (agent design §4).

Deterministic, JSON-serializable shapes. Sniffers do the measuring; these
models only validate shape. Anything the design marks as a finding
(unknown systems, unparseable sections, redaction uncertainty) is an explicit
field here, never a silent omission.
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION = "sniffer/v1"


class SnifferEnvelope(BaseModel):
    """Version envelope every sniffer output carries."""

    model_config = ConfigDict(extra="forbid")

    tool: str = Field(min_length=1, max_length=128)
    tool_version: str = Field(min_length=1, max_length=32)
    input_sha: str = Field(min_length=1, max_length=128)
    schema_version: Literal["sniffer/v1"] = "sniffer/v1"
    produced_at: datetime


class FileKind(StrEnum):
    TEXT = "text"
    BINARY = "binary"


class ParseStatus(StrEnum):
    """Parser outcome only — never a verdict on support tier."""

    PARSED = "parsed"
    SYNTAX_ERRORS = "syntax-errors"
    GRAMMAR_UNAVAILABLE = "grammar-unavailable"
    SKIPPED = "skipped"


class ExtractedCapability(BaseModel):
    """One capability actually extracted for a file, with provenance."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    extractor: str = Field(min_length=1, max_length=128)
    extractor_version: str = Field(min_length=1, max_length=32)


class CensusFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1)
    kind: FileKind
    size_bytes: int = Field(ge=0)
    language: str = Field(default="unknown", max_length=64)
    parse_status: ParseStatus = ParseStatus.SKIPPED
    parse_error: str = Field(default="", max_length=1024)
    capabilities: list[ExtractedCapability] = Field(default_factory=list)
    loc: int | None = Field(default=None, ge=0)
    generated: bool = False


class LanguageTotals(BaseModel):
    model_config = ConfigDict(extra="forbid")

    files: int = Field(ge=0)
    loc: int = Field(ge=0)


class CensusOutput(BaseModel):
    """File inventory + parse census. Unknown bucket always present."""

    model_config = ConfigDict(extra="forbid")

    envelope: SnifferEnvelope
    files: list[CensusFile] = Field(default_factory=list)
    skipped: list[str] = Field(default_factory=list)
    totals_by_language: dict[str, LanguageTotals] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_totals_reconcile(self) -> CensusOutput:
        by_lang_loc: dict[str, int] = {}
        by_lang_files: dict[str, int] = {}
        for item in self.files:
            by_lang_files[item.language] = by_lang_files.get(item.language, 0) + 1
            if item.loc is not None:
                by_lang_loc[item.language] = by_lang_loc.get(item.language, 0) + item.loc
        for language, totals in self.totals_by_language.items():
            if by_lang_files.get(language, 0) != totals.files:
                raise ValueError(f"file count mismatch for language {language!r}")
            if by_lang_loc.get(language, 0) != totals.loc:
                raise ValueError(f"LOC mismatch for language {language!r}")
        if "unknown" not in self.totals_by_language:
            raise ValueError("totals_by_language must include the 'unknown' bucket")
        return self


class DirectoryEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1)
    role: str = Field(default="unknown", max_length=64)


class BuildModule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=256)
    source_roots: list[str] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)


class BuildDependency(BaseModel):
    model_config = ConfigDict(extra="forbid")

    coordinates: str = Field(min_length=1, max_length=512)
    version: str | None = Field(default=None, max_length=64)


class BuildSystem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool: str = Field(min_length=1, max_length=64)
    version: str | None = Field(default=None, max_length=64)
    confidence: str = Field(default="inferred", max_length=32)
    modules: list[BuildModule] = Field(default_factory=list)
    dependencies: list[BuildDependency] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    excerpt_paths: list[str] = Field(default_factory=list)


class BuildMapOutput(BaseModel):
    """Build-system inventory. `tool="unrecognized"` is a finding, not a gap."""

    model_config = ConfigDict(extra="forbid")

    envelope: SnifferEnvelope
    systems: list[BuildSystem] = Field(default_factory=list)
    directories: list[DirectoryEntry] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_source_roots_resolve(self) -> BuildMapOutput:
        if not self.directories:
            return self
        known = {entry.path.rstrip("/") for entry in self.directories}
        for system in self.systems:
            for module in system.modules:
                for root in module.source_roots:
                    normalized = root.rstrip("/")
                    if normalized not in known and not any(
                        normalized.startswith(prefix + "/") for prefix in known
                    ):
                        raise ValueError(
                            f"source root {root!r} of module {module.name!r} "
                            "resolves to no known directory"
                        )
        return self


class PipelineRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    system: str = Field(min_length=1, max_length=64)
    config_path: str = Field(min_length=1)
    builds: list[str] = Field(default_factory=list)
    tests: list[str] = Field(default_factory=list)
    publishes: list[str] = Field(default_factory=list)
    artifact_destinations: list[str] = Field(default_factory=list)
    environments: list[str] = Field(default_factory=list)


class CiMapOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    envelope: SnifferEnvelope
    pipelines: list[PipelineRecord] = Field(default_factory=list)
    test_commands: list[str] = Field(default_factory=list)
    environments: list[str] = Field(default_factory=list)


class LockfileRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool: str = Field(min_length=1, max_length=64)
    path: str = Field(min_length=1)
    resolved: list[str] = Field(default_factory=list)


class DepScanOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    envelope: SnifferEnvelope
    lockfiles: list[LockfileRecord] = Field(default_factory=list)
    declared_but_unlocked: list[str] = Field(default_factory=list)
    locked_but_undeclared: list[str] = Field(default_factory=list)
    consistency: Literal["consistent", "divergent", "no-lockfile"] = "consistent"


_HIGH_CONFIDENCE_SECRET_RE = re.compile(
    r"(sk-[A-Za-z0-9]{20,}|xox[bap]-[A-Za-z0-9-]+|"
    r"aws_secret_access_key|-----BEGIN [A-Z ]*PRIVATE KEY-----)"
)


class HygieneFinding(BaseModel):
    """A redacted hygiene finding. Never carries raw secret material."""

    model_config = ConfigDict(extra="forbid")

    kind: str = Field(min_length=1, max_length=64)
    location: str = Field(min_length=1)
    redacted_pattern: str = Field(min_length=1, max_length=1024)
    redaction_confident: bool = True

    @model_validator(mode="after")
    def _reject_unredacted_secrets(self) -> HygieneFinding:
        for value in (self.location, self.redacted_pattern):
            if _HIGH_CONFIDENCE_SECRET_RE.search(value):
                raise ValueError("finding carries unredacted secret-shaped material")
        return self


class HygieneOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    envelope: SnifferEnvelope
    findings: list[HygieneFinding] = Field(default_factory=list)
    excluded_inputs: list[str] = Field(default_factory=list)


class SizerOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    envelope: SnifferEnvelope
    loc_by_service: dict[str, int] = Field(default_factory=dict)
    generated: list[str] = Field(default_factory=list)
    vendor: list[str] = Field(default_factory=list)
    binaries: list[str] = Field(default_factory=list)
    exclusion_rationale: dict[str, str] = Field(default_factory=dict)


def validate_sniffer_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Best-effort structural check for raw sniffer JSON before parsing.

    Returns the payload unchanged when it looks like a versioned sniffer
    output; raises ValueError otherwise. Typed models remain authoritative.
    """
    for key in ("tool", "tool_version", "input_sha", "schema_version"):
        if not isinstance(payload.get(key), str) or not payload[key]:
            raise ValueError(f"sniffer payload missing required envelope field: {key}")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"unsupported sniffer schema version: {payload.get('schema_version')!r}")
    return payload
