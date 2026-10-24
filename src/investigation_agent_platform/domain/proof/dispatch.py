# src/investigation_agent_platform/domain/proof/dispatch.py
"""D1 profile dispatch table + authorization predicate — WP-D1, frozen.

The dispatch row applies to exactly one profile revision (no implicit
inheritance); row digests are re-verified against the policy digest at
read time. ``authorize_edge_use`` applies the §5 six-check predicate
together and rejects (or reports unavailable) on any failure.

Normative source: ``docs/IAP-proof-kernel-d1-spec-v1.2.md`` §§3–5.
``IAP_TYPED_EDGES`` is created here (plan Step 5): off by default, and
off means no new record is persisted and no additional resolver runs
(§6). Nothing in the verifier or publication path reads this module
(D1-H); the flag contract is proven by the D1-G/H suite, not by prose.
"""

from __future__ import annotations

import hashlib
import os
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from investigation_agent_platform.domain.proof.canonical import canonical_dict_bytes
from investigation_agent_platform.domain.proof.edge_observation import (
    ASSUMPTIONS_V1,
    EdgeError,
    EdgeErrorCode,
    EdgeObservation,
    EdgeVariant,
    _payload_dict,
)
from investigation_agent_platform.domain.proof.rules import RULE_REGISTRY

DISPATCH_TABLE_VERSION = "dispatch/v1"

TYPED_EDGES_ENV = "IAP_TYPED_EDGES"


def typed_edges_enabled() -> bool:
    """Flag accessor: enabled iff ``IAP_TYPED_EDGES`` is exactly ``"1"``.

    Exact match, no stripping, no case folding, no truthy synonyms.
    A gate flag must never be half-on by accident.
    """
    return os.environ.get(TYPED_EDGES_ENV, "") == "1"


class DispatchErrorCode(str, Enum):
    """Stable dispatch failure codes (frozen; append-only)."""

    BAD_ROW = "BAD_ROW"
    DUPLICATE_ROW = "DUPLICATE_ROW"
    STALE_ROW = "STALE_ROW"
    UNKNOWN_DISPATCH = "UNKNOWN_DISPATCH"
    DIGEST_MISMATCH = "DIGEST_MISMATCH"
    POLICY_MISMATCH = "POLICY_MISMATCH"
    FORBIDDEN_USE = "FORBIDDEN_USE"


class DispatchError(ValueError):
    """Fail-closed dispatch rejection carrying a stable code."""

    def __init__(self, code: DispatchErrorCode | str, detail: str = "") -> None:
        self.code = str(getattr(code, "value", code))
        super().__init__(f"{self.code}: {detail}" if detail else self.code)


class DispatchRow(BaseModel):
    """One reviewed dispatch row: exact profile revision, closed world.

    Canonical form is the ordered tuple
    ``(profile_id, profile_version, language, claim_kind, fact_category,
    resolver_bundle, bundle_version, contract_digest, artifact_digest,
    rule_id, requires_target_declaration)``; ``row_digest`` binds it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    profile_id: str = Field(..., min_length=1, max_length=128)
    profile_version: str = Field(..., min_length=1, max_length=64)
    language: str = Field(..., min_length=1, max_length=64)
    claim_kind: str = Field(..., min_length=1, max_length=64)
    fact_category: str = Field(..., min_length=1, max_length=64)
    resolver_bundle: str = Field(..., min_length=1, max_length=128)
    bundle_version: str = Field(..., min_length=1, max_length=64)
    contract_digest: str = Field(..., min_length=1, max_length=256)
    artifact_digest: str = Field(..., min_length=1, max_length=256)
    rule_id: str = Field(..., min_length=1, max_length=128)
    requires_target_declaration: bool = Field(...)
    row_digest: str = Field(default="", max_length=256)

    def key(self) -> tuple[str, str, str, str, str]:
        return (
            self.profile_id,
            self.profile_version,
            self.language,
            self.claim_kind,
            self.fact_category,
        )

    def canonical_tuple(self) -> list[Any]:
        return [
            self.profile_id,
            self.profile_version,
            self.language,
            self.claim_kind,
            self.fact_category,
            self.resolver_bundle,
            self.bundle_version,
            self.contract_digest,
            self.artifact_digest,
            self.rule_id,
            self.requires_target_declaration,
        ]

    def computed_digest(self) -> str:
        raw = canonical_dict_bytes({"dispatch_row": self.canonical_tuple()})
        return "sha256:" + hashlib.sha256(raw).hexdigest()

    def sealed(self) -> DispatchRow:
        """Return a copy carrying its recomputed digest (review-time seal)."""
        return self.model_copy(update={"row_digest": self.computed_digest()})

    def verify(self) -> None:
        """Re-verify the carried digest (read-time check, §3)."""
        if self.rule_id not in RULE_REGISTRY:
            raise DispatchError(DispatchErrorCode.BAD_ROW, f"unknown rule_id={self.rule_id!r}")
        if self.row_digest != self.computed_digest():
            raise DispatchError(
                DispatchErrorCode.DIGEST_MISMATCH,
                f"row {self.key()!r}: carried != recomputed",
            )


class DispatchTable(BaseModel):
    """Closed, version-pinned dispatch set with a policy digest."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    table_version: str = Field(default=DISPATCH_TABLE_VERSION, max_length=32)
    rows: list[DispatchRow] = Field(default_factory=list)

    def model_post_init(self, _context: Any) -> None:
        if self.table_version != DISPATCH_TABLE_VERSION:
            raise DispatchError(DispatchErrorCode.BAD_ROW, f"table_version={self.table_version!r}")
        seen: set[tuple[str, str, str, str, str]] = set()
        for row in self.rows:
            row.verify()
            if row.key() in seen:
                raise DispatchError(DispatchErrorCode.DUPLICATE_ROW, f"key={row.key()!r}")
            seen.add(row.key())

    def policy_digest(self) -> str:
        """Digest over the reviewed row set (sorted row digests)."""
        raw = canonical_dict_bytes({"dispatch_policy": sorted(r.row_digest for r in self.rows)})
        return "sha256:" + hashlib.sha256(raw).hexdigest()

    def lookup(
        self,
        profile_id: str,
        profile_version: str,
        language: str,
        claim_kind: str,
        fact_category: str,
    ) -> DispatchRow | None:
        """Exact-match lookup only: no inheritance, no fallback.

        Newer profile revisions inherit nothing; unknown keys abstain
        (None), never a nearest match.
        """
        for row in self.rows:
            if row.key() == (profile_id, profile_version, language, claim_kind, fact_category):
                row.verify()
                return row
        return None


class PremiseBinding(BaseModel):
    """Per-premise triple carried by the certificate (§2.4)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    observation_id: str = Field(..., min_length=1, max_length=256)
    content_digest: str = Field(..., min_length=1, max_length=256)
    fact_category: str = Field(..., min_length=1, max_length=64)


class CertificateBinding(BaseModel):
    """Certificate-side dispatch binding (generic record stays policy-free)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    profile_id: str = Field(..., min_length=1, max_length=128)
    profile_version: str = Field(..., min_length=1, max_length=64)
    dispatch_row_digest: str = Field(..., min_length=1, max_length=256)
    policy_digest: str = Field(..., min_length=1, max_length=256)
    claim_kind: str = Field(..., min_length=1, max_length=64)
    premises: list[PremiseBinding] = Field(..., min_length=1, max_length=256)


class BundleApproval(BaseModel):
    """Reviewed per-version digests for one resolver bundle."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    bundle: str = Field(..., min_length=1, max_length=128)
    versions: dict[str, dict[str, str]] = Field(...)
    # version -> {"contract_digest": ..., "artifact_digest": ...}


class ResolverRegistry(BaseModel):
    """Approved resolver bundles + rule universe for authorization."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    approved_bundles: dict[str, BundleApproval] = Field(default_factory=dict)

    def rule_ids(self) -> frozenset[str]:
        return frozenset(RULE_REGISTRY.keys())


def content_digest(record: EdgeObservation) -> str:
    """Digest over the record's canonical bytes (recomputed, never trusted)."""
    raw = canonical_dict_bytes(
        {
            "domain": "iap:call-edge-content",
            "record_version": record.record_version,
            "variant": record.variant.value,
            **_payload_dict(record.payload),
        }
    )
    return "sha256:" + hashlib.sha256(raw).hexdigest()


class AuthorizationDecision(BaseModel):
    """Outcome of the §5 predicate (frozen; reasons are stable codes)."""

    model_config = ConfigDict(frozen=True)

    authorized: bool = Field(...)
    reason: str = Field(...)
    check: str = Field(default="")


def authorize_edge_use(
    binding: CertificateBinding,
    record: EdgeObservation,
    table: DispatchTable,
    registry: ResolverRegistry,
    premise: PremiseBinding,
) -> AuthorizationDecision:
    """Single authorization predicate (§5): all six checks together.

    Rejects (or reports unavailable) on any failure. Claim kind and fact
    category are never stored in the edge and never inferred: category
    comes from the row via the premise triple, kind from the binding.
    """

    def deny(check: str, reason: str) -> AuthorizationDecision:
        return AuthorizationDecision(authorized=False, reason=reason, check=check)

    if record.variant != EdgeVariant.RESOLVED_EDGE:
        return deny("variant", f"FORBIDDEN_USE: {record.variant.value} never satisfies a premise")
    resolver = record.payload.resolver
    codes = list(record.payload.limitation_codes)
    if codes:
        return deny(
            "variant", f"FORBIDDEN_USE: limitation codes present: {[str(c) for c in codes]}"
        )
    assumptions = list(record.payload.assumptions)
    unknown = [a for a in assumptions if a not in ASSUMPTIONS_V1]
    if unknown:
        return deny("variant", f"FORBIDDEN_USE: unknown assumptions: {unknown!r}")

    row = table.lookup(
        binding.profile_id,
        binding.profile_version,
        resolver.language,
        binding.claim_kind,
        premise.fact_category,
    )
    if row is None:
        return deny("dispatch", "UNKNOWN_DISPATCH: no exact row; abstain, never nearest match")
    if (
        binding.dispatch_row_digest != row.row_digest
        or binding.policy_digest != table.policy_digest()
    ):
        return deny("dispatch", "POLICY_MISMATCH: row or policy digest does not match binding")

    if premise.observation_id != record.observation_id:
        return deny("premise", "DIGEST_MISMATCH: premise observation_id != record id")
    if premise.content_digest != content_digest(record):
        return deny("premise", "DIGEST_MISMATCH: premise content digest != recomputed")
    if premise.fact_category != row.fact_category:
        return deny("premise", "FORBIDDEN_USE: premise category != row category")
    if binding.claim_kind != row.claim_kind:
        return deny("premise", "FORBIDDEN_USE: binding claim kind != row claim kind")

    if (
        resolver.bundle != row.resolver_bundle
        or resolver.bundle_version != row.bundle_version
        or resolver.contract_digest != row.contract_digest
        or resolver.artifact_digest != row.artifact_digest
    ):
        return deny("resolver", "DIGEST_MISMATCH: record resolver != row expectations")
    approval = registry.approved_bundles.get(resolver.bundle)
    if approval is None:
        return deny("resolver", f"FORBIDDEN_USE: bundle {resolver.bundle!r} not approved")
    expected = approval.versions.get(resolver.bundle_version)
    if (
        expected is None
        or expected.get("contract_digest") != resolver.contract_digest
        or expected.get("artifact_digest") != resolver.artifact_digest
    ):
        return deny("resolver", "DIGEST_MISMATCH: version digests not approved")

    if row.rule_id not in registry.rule_ids():
        return deny("rule", f"FORBIDDEN_USE: unknown rule {row.rule_id!r}")
    target = getattr(record.payload, "target_declaration", "absent")
    if row.requires_target_declaration and target is None:
        return deny("rule", "FORBIDDEN_USE: row requires target_declaration")
    return AuthorizationDecision(authorized=True, reason="all six §5 checks satisfied", check="all")


class ActualResolver(BaseModel):
    """Observed resolver identity at execution (for §4.1 reverification)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    bundle: str = Field(..., min_length=1, max_length=128)
    bundle_version: str = Field(..., min_length=1, max_length=64)
    contract_digest: str = Field(..., min_length=1, max_length=256)
    artifact_digest: str = Field(..., min_length=1, max_length=256)


def verify_resolver_binding(row: DispatchRow, actual: ActualResolver) -> None:
    """Execution-time binding check (§4.1, D1-C unit half).

    Refuses on any substitution: unchanged version/metadata with a
    different executable, contract-only changes, and dependency/
    configuration changes (all surface as digest or version drift).
    Raises DispatchError; returns None on match.
    """
    if actual.bundle != row.resolver_bundle:
        raise DispatchError(DispatchErrorCode.FORBIDDEN_USE, f"bundle {actual.bundle!r}")
    if actual.bundle_version != row.bundle_version:
        raise DispatchError(DispatchErrorCode.STALE_ROW, f"version {actual.bundle_version!r}")
    if actual.contract_digest != row.contract_digest:
        raise DispatchError(DispatchErrorCode.DIGEST_MISMATCH, "contract digest drift")
    if actual.artifact_digest != row.artifact_digest:
        raise DispatchError(DispatchErrorCode.DIGEST_MISMATCH, "artifact digest drift")


class AnalysisModelVocabulary(BaseModel):
    """Closed per-resolver analysis-model vocabulary (D1-impl mapping artifact)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    resolver_bundle: str = Field(..., min_length=1, max_length=128)
    models: list[str] = Field(..., min_length=1, max_length=64)

    def check(self, model: str) -> None:
        if model not in self.models:
            raise EdgeError(EdgeErrorCode.UNKNOWN_MODEL, f"{self.resolver_bundle}: {model!r}")


class AuthoritativeSource(BaseModel):
    """Per-resolver source-of-truth row (D1-impl mapping artifact)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    resolver_bundle: str = Field(..., min_length=1, max_length=128)
    artifact: str = Field(..., min_length=1, max_length=256)
    encoding: str = Field(..., min_length=1, max_length=64)
    preprocessing_boundary: str = Field(..., min_length=1, max_length=512)
    repository_revision_scheme: str = Field(..., min_length=1, max_length=128)


class CategoryMapping(BaseModel):
    """CATEGORY_PROOF_RULES entry mapped to a disposition (reviewed artifact).

    Required-evidence families are never copied into unsupported cases:
    each mapping row is explicit and reviewed.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    fact_category: str = Field(..., min_length=1, max_length=64)
    disposition: str = Field(..., min_length=1, max_length=64)
    required_evidence_families: list[str] = Field(default_factory=list, max_length=32)
