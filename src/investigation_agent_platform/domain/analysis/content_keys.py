# src/investigation_agent_platform/domain/analysis/content_keys.py
"""Content-hash key helpers (Acquired Judgement Phase 0).

Everything downstream keys off these: normalized file bytes hashed with
sha256, canonical (repo, SHA, stage) result keys. Deterministic, no I/O.
"""

from __future__ import annotations

import hashlib
import re

_STAGE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_REPO_SHA_RE = re.compile(r"^[A-Za-z0-9._/-]{1,256}$")


def normalize_file_bytes(data: bytes) -> bytes:
    """Normalize bytes for stable hashing: CRLF->LF, strip trailing spaces per line, ensure single trailing newline."""
    text = data.decode("utf-8", errors="surrogateescape")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip(" \t") for line in text.split("\n")]
    normalized = "\n".join(lines)
    if not normalized.endswith("\n"):
        normalized += "\n"
    return normalized.encode("utf-8", errors="surrogateescape")


def sha256_content(data: bytes, *, normalize: bool = True) -> str:
    """Return `sha256:<hex>` over (optionally normalized) bytes."""
    payload = normalize_file_bytes(data) if normalize else data
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def result_key(repo: str, sha: str, stage: str) -> str:
    """Canonical analyzer result key: `<repo>@<sha>:<stage>` (fail-closed on @/:-bearing inputs)."""
    repo = repo.strip()
    sha = sha.strip()
    stage = stage.strip()
    if not repo or not sha or not stage:
        raise ValueError("repo, sha, and stage must all be non-empty")
    if not _REPO_SHA_RE.match(repo):
        raise ValueError(f"invalid repo name: {repo!r}")
    if not _REPO_SHA_RE.match(sha):
        raise ValueError(f"invalid sha: {sha!r}")
    if not _STAGE_RE.match(stage):
        raise ValueError(f"invalid stage name: {stage!r}")
    return f"{repo}@{sha}:{stage}"


def parse_result_key(key: str) -> tuple[str, str, str]:
    """Inverse of result_key. Raises ValueError on malformed input."""
    if "@" not in key or ":" not in key:
        raise ValueError(f"malformed result key: {key!r}")
    repo_sha, _, stage = key.partition(":")
    repo, _, sha = repo_sha.partition("@")
    if not repo or not sha or not stage:
        raise ValueError(f"malformed result key: {key!r}")
    if not _REPO_SHA_RE.match(repo) or not _REPO_SHA_RE.match(sha) or not _STAGE_RE.match(stage):
        raise ValueError(f"malformed result key: {key!r}")
    return repo, sha, stage
