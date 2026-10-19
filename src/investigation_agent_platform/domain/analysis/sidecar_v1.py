# src/investigation_agent_platform/domain/analysis/sidecar_v1.py
"""Sidecar JSON contracts, analyzer/v1 (Acquired Judgement Phase 0).

Stage 1 (batch, cheap) sidecars — cdxgen, Noir, tree-sitter fast pass — emit
these records as JSON. Validated at ingestion boundaries; stored in
analyzer_* tables keyed by (repo, SHA). No parsing logic here, only shapes.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = "analyzer/v1"


class FileRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["analyzer/v1"] = "analyzer/v1"
    repo: str = Field(min_length=1)
    sha: str = Field(min_length=1)
    path: str = Field(min_length=1)
    content_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    language: str = Field(default="unknown")
    is_generated: bool = False
    generated_reason: str = ""


class SymbolRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["analyzer/v1"] = "analyzer/v1"
    repo: str = Field(min_length=1)
    sha: str = Field(min_length=1)
    path: str = Field(min_length=1)
    name: str = Field(min_length=1)
    kind: str = Field(default="unknown")
    line: int = Field(ge=1, default=1)
    annotations: list[str] = Field(default_factory=list)


class EndpointRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["analyzer/v1"] = "analyzer/v1"
    repo: str = Field(min_length=1)
    sha: str = Field(min_length=1)
    path: str = Field(min_length=1)
    method: str = Field(min_length=1)
    route: str = Field(min_length=1)
    handler: str = ""


class EdgeRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["analyzer/v1"] = "analyzer/v1"
    repo: str = Field(min_length=1)
    sha: str = Field(min_length=1)
    src: str = Field(min_length=1)
    dst: str = Field(min_length=1)
    kind: str = Field(default="depends_on")
    confidence: Literal["Exact", "Inferred"] = "Exact"
