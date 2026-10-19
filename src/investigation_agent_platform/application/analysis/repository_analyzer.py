# src/investigation_agent_platform/application/analysis/repository_analyzer.py
"""RepositoryAnalyzer: Stage-1 sidecar ingest → store queries (Phase 1).

Sidecars (cdxgen CycloneDX, Noir OAS3, tree-sitter fast pass) emit analyzer/v1
JSON. This module validates at the boundary (pydantic), dedups by content
hash, and exposes store-query helpers. Postgres tables land in migration 021;
here the pure ingest/merge logic is unit-testable without a DB.
"""

from __future__ import annotations

from investigation_agent_platform.domain.analysis.content_keys import result_key, sha256_content
from investigation_agent_platform.domain.analysis.sidecar_v1 import (
    EdgeRecord,
    EndpointRecord,
    FileRecord,
    SymbolRecord,
)


def ingest_files(raw: list[dict]) -> list[FileRecord]:
    """Validate + dedup file records by (repo, sha, path). Sorted for determinism."""
    seen: dict[tuple[str, str, str], FileRecord] = {}
    for item in raw:
        rec = FileRecord.model_validate(item)
        seen[(rec.repo, rec.sha, rec.path)] = rec
    return [seen[k] for k in sorted(seen)]


def ingest_symbols(raw: list[dict]) -> list[SymbolRecord]:
    seen: dict[tuple[str, str, str, str, int], SymbolRecord] = {}
    for item in raw:
        rec = SymbolRecord.model_validate(item)
        seen[(rec.repo, rec.sha, rec.path, rec.name, rec.line)] = rec
    return [seen[k] for k in sorted(seen)]


def ingest_endpoints(raw: list[dict]) -> list[EndpointRecord]:
    seen: dict[tuple[str, str, str, str], EndpointRecord] = {}
    for item in raw:
        rec = EndpointRecord.model_validate(item)
        seen[(rec.repo, rec.sha, rec.method, rec.route)] = rec
    return [seen[k] for k in sorted(seen)]


def ingest_edges(raw: list[dict]) -> list[EdgeRecord]:
    """Fail-closed: invalid records raise (never fail-open ingestion)."""
    seen: dict[tuple[str, str, str, str, str], EdgeRecord] = {}
    for item in raw:
        rec = EdgeRecord.model_validate(item)
        seen[(rec.repo, rec.sha, rec.src, rec.dst, rec.kind)] = rec
    return [seen[k] for k in sorted(seen)]


def content_dedup_key(repo: str, sha: str, stage: str, payload: bytes) -> tuple[str, str]:
    """Content-addressed global key: (result_key, content_hash). Shared libs parsed once."""
    return result_key(repo, sha, stage), sha256_content(payload)


def is_generated(path: str, content: bytes) -> tuple[bool, str]:
    """Heuristic stack on stored file records: markers, paths, logic density."""
    text = content.decode("utf-8", errors="ignore")
    lowered = path.lower().replace("\\", "/")
    segments = lowered.split("/")
    if "generated" in lowered or ".jaxb" in lowered or "gen" in segments or "/gen/" in lowered:
        return True, "path-marker"
    for marker in ("@Generated", "auto-generated", "DO NOT EDIT"):
        if marker in text:
            return True, "marker"
    lines = [line for line in text.splitlines() if line.strip()]
    if lines and sum(1 for line in lines if line.strip().startswith(("@", "import"))) / len(lines) > 0.8:
        return True, "logic-density"
    return False, ""
