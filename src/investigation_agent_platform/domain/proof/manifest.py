# src/investigation_agent_platform/domain/proof/manifest.py
"""WP-B2 evidence manifests (canon/v1): frozen, content-addressed, pure.

An :class:`EvidenceManifest` is the ``E`` in ``Evaluate(C,E,R,M)``: the
ordered premise list plus content hashes, retrieval config, universe
exclusions, and the coverage result. The manifest digest binds all of
it; evaluation recomputes from inputs every run — new evidence means a
new manifest and new IDs, never in-place mutation.

Coverage vocabulary mirrors contracts §4.3: ``COMPLETE_EMPTY`` (clean
run over a stated universe with zero matches — the only empty that can
support absence), ``MATCHES_FOUND``, ``INCOMPLETE`` (universe only
partly covered), ``FAILED`` (retrieval itself failed).
"""

from __future__ import annotations

import hashlib
import re
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from investigation_agent_platform.domain.proof.canonical import (
    CANON_VERSION,
    CanonError,
    CanonicalizationError,
    canonical_dict_bytes,
)


class ManifestCoverage(str, Enum):
    """Coverage result vocabulary (frozen; mirrors contracts §4.3)."""

    COMPLETE_EMPTY = "COMPLETE_EMPTY"
    MATCHES_FOUND = "MATCHES_FOUND"
    INCOMPLETE = "INCOMPLETE"
    FAILED = "FAILED"


class ManifestError(str, Enum):
    """Manifest construction failure codes (frozen)."""

    DUPLICATE_PREMISE_ID = "DUPLICATE_PREMISE_ID"
    BAD_CONTENT_HASH = "BAD_CONTENT_HASH"


class ManifestConstructionError(ValueError):
    """Fail-closed manifest construction failure carrying a stable code."""

    def __init__(self, code: ManifestError | str, detail: str = "") -> None:
        self.code = str(getattr(code, "value", code))
        super().__init__(f"{self.code}: {detail}" if detail else self.code)


_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class EvidenceManifest(BaseModel):
    """Frozen evidence manifest: ordered premise IDs + content hashes.

    ``content_hashes`` maps premise ID to its full ``sha256:`` digest.
    Premises listed without a known hash are recorded (the run happened)
    but unevaluable — :func:`evaluate` reports them PREMISE_UNAVAILABLE
    instead of proving over them. ``exclusions`` declares universe
    exclusions (scope carved out, never silently unsearched).
    """

    model_config = ConfigDict(frozen=True)

    evidence_ids: list[str] = Field(default_factory=list, max_length=500)
    content_hashes: dict[str, str] = Field(default_factory=dict)
    retrieval_config: dict[str, Any] = Field(default_factory=dict)
    exclusions: list[str] = Field(default_factory=list, max_length=50)
    coverage: ManifestCoverage = ManifestCoverage.MATCHES_FOUND


def build_manifest(
    evidence_ids: list[str],
    content_hashes: dict[str, str] | None = None,
    retrieval_config: dict[str, Any] | None = None,
    exclusions: list[str] | None = None,
    coverage: ManifestCoverage = ManifestCoverage.MATCHES_FOUND,
) -> EvidenceManifest:
    """Build a manifest (pure; fail-closed on duplicates and bad hashes)."""
    ids = [str(i) for i in evidence_ids or []]
    if len(set(ids)) != len(ids):
        raise ManifestConstructionError(
            ManifestError.DUPLICATE_PREMISE_ID, "premise IDs must be unique (K14)"
        )
    hashes = dict(content_hashes or {})
    for premise_id, digest in hashes.items():
        if not isinstance(digest, str) or not _HASH_RE.fullmatch(digest):
            raise ManifestConstructionError(
                ManifestError.BAD_CONTENT_HASH, f"{premise_id}={digest!r}"
            )
    return EvidenceManifest(
        evidence_ids=ids,
        content_hashes=hashes,
        retrieval_config=dict(retrieval_config or {}),
        exclusions=[str(e) for e in exclusions or []],
        coverage=coverage,
    )


def manifest_digest(manifest: EvidenceManifest) -> str:
    """Content digest of a manifest: ``sha256:`` + hex of canonical bytes."""
    try:
        payload = manifest.model_dump(mode="json")
    except Exception as exc:
        raise CanonicalizationError(CanonError.MISSING_FIELD, f"manifest: {exc}") from exc
    return "sha256:" + hashlib.sha256(canonical_dict_bytes(payload)).hexdigest()


def manifest_envelope(manifest: EvidenceManifest) -> dict[str, Any]:
    """Manifest as a plain dict with its digest attached (for contexts)."""
    payload = manifest.model_dump(mode="json")
    payload["canon_version"] = CANON_VERSION
    payload["manifest_digest"] = manifest_digest(manifest)
    return payload
