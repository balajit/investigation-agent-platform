# src/investigation_agent_platform/domain/knowledge/fingerprint.py
"""Tenant-agnostic code-issue fingerprinting (Part 6 D8).

The fingerprint is built EXCLUSIVELY from static fields: normalized error
class, repo@rev, qualified failing/caller symbols, top-frame file. Tenant
IDs, customer identifiers, timestamps, environment names, and session data
are never inputs — a fingerprint that varies by tenant is a bug, enforced
by the two-tenant determinism test.
"""

from __future__ import annotations

import hashlib
import re

_WS_RUNS = re.compile(r"\s+")
_HEX_ADDR = re.compile(r"0x[0-9a-fA-F]+")
_NUMBER_RUNS = re.compile(r"\d+")


def normalize_error_class(raw: str) -> str:
    """Strip memory addresses, PIDs, and other runtime noise from error text."""
    text = _HEX_ADDR.sub("0xADDR", raw.strip())
    return _WS_RUNS.sub(" ", text)[:256]


def normalize_frame_file(path: str) -> str:
    """Repository-relative file path; rejects absolute/traversal paths.

    Traversal is rejected BEFORE normalization so `..` can never be
    laundered into an innocent path by prefix stripping.
    """
    if not path or "\x00" in path:
        raise ValueError(f"unsafe frame file path: {path!r}")
    if path.startswith("/") or path.startswith("~") or path.startswith("\\"):
        raise ValueError(f"unsafe frame file path: {path!r}")
    parts = path.replace("\\", "/").split("/")
    if any(part == ".." for part in parts):
        raise ValueError(f"unsafe frame file path: {path!r}")
    normalized = "/".join(part for part in parts if part not in (".", ""))
    if not normalized:
        raise ValueError(f"unsafe frame file path: {path!r}")
    return normalized


def build_code_issue_fingerprint(
    *,
    error_class: str,
    repository: str,
    revision: str,
    failing_symbol: str,
    caller_symbol: str | None = None,
    top_frame_file: str | None = None,
) -> str:
    """Compute the canonical tenant-agnostic code-issue key (hex SHA-256)."""
    if not error_class.strip():
        raise ValueError("error_class must be non-empty")
    if not repository.strip() or not revision.strip() or not failing_symbol.strip():
        raise ValueError("repository, revision, and failing_symbol must be non-empty")
    parts = [
        normalize_error_class(error_class),
        repository.strip(),
        revision.strip(),
        failing_symbol.strip(),
        (caller_symbol or "").strip(),
        normalize_frame_file(top_frame_file) if top_frame_file else "",
    ]
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


def normalize_symbol_for_match(qualified_name: str) -> str:
    """Normalize a qualified symbol for cross-run comparison (strip generics/arity noise)."""
    text = _NUMBER_RUNS.sub("#", qualified_name.strip())
    return _WS_RUNS.sub(" ", text)
