# src/investigation_agent_platform/domain/proof/definition_observation.py
"""D3 definition observation record (``definition/v1``) — Phase 0, frozen.

A new, separate record: a bound source-range association, never a
reinterpretation of existing evidence rows and never declaration
proof (Option B). The envelope is tagged; the tag is the status
(no separate ``status`` field, so variant/status drift is
unrepresentable). ``observation_id`` is the content hash of the
canonical preimage (never self-referential): schema validation
and recomputation precede availability on every read.

Normative sources: ``docs/IAP-proof-kernel-d3-definition-plan-v1.3.md``
§§2-2.2a (record contracts, canonical target, candidate selection,
TargetReference vs BoundTargetIdentity) and
``docs/IAP-proof-kernel-d3-phase0-approval-package-v1.5.md`` §§1-2
(bundle assembly, two-layer identity, currency contract).
``IAP_TYPED_DEFINITIONS`` authority is never exercised here (Phase 0:
shadow-only; see ``definition_dispatch.py``).

Field contracts: nested objects (``repository``, ``resolver``,
``lineage``) reuse the D1 ``edge_observation`` models verbatim —
same names, types, maxima, revision rules. Only the target and
payload shapes are definition-specific.
"""

from __future__ import annotations

import hashlib
import unicodedata
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from investigation_agent_platform.domain.proof.canonical import canonical_dict_bytes
from investigation_agent_platform.domain.proof.edge_observation import (
    EdgeError,
    EdgeErrorCode,
    LimitationCode,
    Lineage,
    LineageSource,
    RangeReference,
    RepositoryId,
    ResolverId,
    SymbolReference,
    _check_assumptions,
    _check_path,
    _check_revision,
    limitation_category,
)

DEFINITION_RECORD_VERSION = "definition/v1"
DEFINITION_DOMAIN = "iap:definition"

_MISSING_CLOSED = frozenset({"target", "lineage-source"})
"""Closed ``missing`` vocabulary (design §2.2a): nothing else is a nameable
unbound part. ``repository``/``resolver`` are envelope prerequisites, never
``missing`` values — their absence is a construction failure."""

# Closed (missing value) -> permitted limitation codes (design §2.2a).
_MISSING_CODE_MAP: dict[str, frozenset[str]] = {
    "target": frozenset({"MISSING_INPUT", "AMBIGUOUS_TARGET", "NOT_YET_RESOLVED"}),
    "lineage-source": frozenset({"MISSING_INPUT", "INPUT_NOT_FOUND"}),
}


class DefinitionVariant(str, Enum):
    """Tagged envelope variants (design §2.2)."""

    RESOLVED_TARGET_BINDING = "RESOLVED_TARGET_BINDING"
    PARTIAL_OBSERVATION = "PARTIAL_OBSERVATION"
    UNRESOLVED_OBSERVATION = "UNRESOLVED_OBSERVATION"


class TargetReference(BaseModel):
    """Possibly incomplete search constraint; input to candidate enumeration.

    Omission means "unconstrained", never a default value (design §2.1b).
    Unlike the D1 ``SymbolReference``, ``revision`` is optional here: a
    target may omit it, in which case candidates match without a revision
    constraint (but the resulting bound identity must still equal the
    observation-level repository revision — no silent completion).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str = Field(..., min_length=1, max_length=512)
    declaring_type: str | None = Field(default=None, max_length=512)
    file_path: str | None = Field(default=None, max_length=1024)
    revision: str | None = Field(default=None, max_length=128)

    @field_validator("symbol")
    @classmethod
    def _norm_symbol(cls, v: str) -> str:
        return _check_symbol(v, "target.symbol")

    @field_validator("file_path")
    @classmethod
    def _norm_path(cls, v: str | None) -> str | None:
        return None if v is None else _check_path(v, "target.file_path")

    @field_validator("revision")
    @classmethod
    def _pin_revision(cls, v: str | None) -> str | None:
        return None if v is None else _check_revision(v, "target.revision")


class BoundTargetIdentity(BaseModel):
    """Complete, canonical bound identity: output of a successful binding.

    The only target form a resolved observation may carry (design §2.1b).
    ``declaring_type`` may be explicitly null (= unknown type), which is
    still a complete statement — absence (omitted) is inexpressible here.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str = Field(..., min_length=1, max_length=512)
    declaring_type: str | None = Field(default=None, max_length=512)
    file_path: str = Field(..., min_length=1, max_length=1024)
    revision: str = Field(..., min_length=1, max_length=128)

    @field_validator("symbol")
    @classmethod
    def _norm_symbol(cls, v: str) -> str:
        return _check_symbol(v, "bound_target.symbol")

    @field_validator("file_path")
    @classmethod
    def _norm_path(cls, v: str) -> str:
        return _check_path(v, "bound_target.file_path")

    @field_validator("revision")
    @classmethod
    def _pin_revision(cls, v: str) -> str:
        return _check_revision(v, "bound_target.revision")


class ResolvedDefinitionPayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    target: BoundTargetIdentity = Field(...)
    repository: RepositoryId = Field(...)
    resolver: ResolverId = Field(...)
    limitation_codes: list[LimitationCode] = Field(default_factory=list, max_length=16)
    lineage: Lineage = Field(...)
    assumptions: list[str] = Field(default_factory=list, max_length=16)


class PartialDefinitionPayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    target: BoundTargetIdentity | None = Field(default=None)
    limitation_codes: list[LimitationCode] = Field(..., min_length=1, max_length=16)
    missing: list[str] = Field(..., min_length=1, max_length=8)
    repository: RepositoryId = Field(...)
    resolver: ResolverId = Field(...)
    lineage: Lineage = Field(...)
    assumptions: list[str] = Field(default_factory=list, max_length=16)


class UnresolvedDefinitionPayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    observed_ref: RangeReference | SymbolReference = Field(...)
    limitation_codes: list[LimitationCode] = Field(..., min_length=1, max_length=16)
    repository: RepositoryId | None = Field(default=None)
    resolver: ResolverId = Field(...)
    lineage: Lineage | None = Field(default=None)
    assumptions: list[str] = Field(default_factory=list, max_length=16)


class DefinitionObservation(BaseModel):
    """Tagged envelope: the tag IS the status (no separate status field)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    observation_id: str = Field(..., min_length=1, max_length=256)
    variant: DefinitionVariant = Field(...)
    record_version: str = Field(default=DEFINITION_RECORD_VERSION, max_length=32)
    payload: (
        ResolvedDefinitionPayload | PartialDefinitionPayload | UnresolvedDefinitionPayload
    ) = Field(...)


def _definition_payload_dict(payload: BaseModel) -> dict[str, Any]:
    """JSON-scalar payload with absent optionals omitted (never nulled).

    Mirrors the D1 ``_payload_dict`` contract: set-valued fields are
    sorted (assumptions, missing); lineage source order is significant
    (consumption order) and preserved; lineage ``scope`` is excluded
    from identity bytes (design §2.2a invariant (d)).
    """
    raw = payload.model_dump(mode="json")
    kept: dict[str, Any] = {}
    for key, value in raw.items():
        if value is None:
            continue
        kept[key] = value
    if isinstance(kept.get("assumptions"), list):
        kept["assumptions"] = sorted(kept["assumptions"])
    if isinstance(kept.get("missing"), list):
        kept["missing"] = sorted(kept["missing"])
    lineage = kept.get("lineage")
    if isinstance(lineage, dict) and isinstance(lineage.get("sources"), list):
        lineage["sources"] = [
            {k: v for k, v in src.items() if k != "scope"} for src in lineage["sources"]
        ]
    return kept


def _check_missing_taxonomy(missing: list[str], codes: list[LimitationCode]) -> None:
    """Closed missing<->code mapping (design §2.2a): anything else fails."""
    for value in missing:
        allowed = _MISSING_CODE_MAP.get(value)
        if allowed is None:
            raise EdgeError(
                EdgeErrorCode.NOT_RESOLVED, f"missing outside closed list: {value!r}"
            )
    code_values = {c.value for c in codes}
    for value in missing:
        if not (code_values & _MISSING_CODE_MAP[value]):
            raise EdgeError(
                EdgeErrorCode.NOT_RESOLVED,
                f"missing {value!r} names no permitted code from {sorted(code_values)!r}",
            )


def _check_nfc_symbol(value: str, where: str) -> str:
    """Exact identifier text: stripped of surrounding whitespace (design
    §2.1), no case folding; non-NFC rejected like D1 window text."""
    if not isinstance(value, str):
        raise EdgeError(EdgeErrorCode.BAD_RANGE, f"{where}: non-string")
    stripped = value.strip()
    if not stripped:
        raise EdgeError(EdgeErrorCode.BAD_RANGE, f"{where}: empty after strip")
    if stripped != unicodedata.normalize("NFC", stripped):
        raise EdgeError(EdgeErrorCode.BAD_RANGE, f"{where}: not NFC-normalized")
    return stripped


def _check_symbol(value: str, where: str) -> str:
    stripped = _check_nfc_symbol(value, where)
    if len(stripped) > 512:
        raise EdgeError(EdgeErrorCode.BAD_RANGE, f"{where}: over length")
    return stripped


def _check_definition_lineage(
    sources: list[LineageSource],
    repository: RepositoryId,
) -> None:
    """Full-tuple identity + definition single-repo binding rule.

    Entry uniqueness and entry-revision-equals-repository-revision
    mirror the D1 lineage contract; on top, invariant (e) requires
    EVERY lineage entry used for binding to live in the
    observation-level repository — no cross-repo binding in v1.
    """
    seen: set[tuple[str, ...]] = set()
    for src in sources:
        key = (
            src.evidence_id,
            src.evidence_digest,
            src.artifact_digest,
            src.repository.name,
            src.repository.revision,
        )
        if key in seen:
            raise EdgeError(EdgeErrorCode.DUPLICATE_SOURCE, f"{src.evidence_id!r}")
        seen.add(key)
        if src.revision != src.repository.revision:
            raise EdgeError(
                EdgeErrorCode.REVISION_MISMATCH,
                f"entry revision {src.revision!r} != repository revision "
                f"{src.repository.revision!r}",
            )
        if src.repository.name != repository.name or (
            src.repository.revision != repository.revision
        ):
            raise EdgeError(
                EdgeErrorCode.REPO_MISMATCH,
                f"binding entry {src.evidence_id!r} outside observation repository "
                f"{repository.name}@{repository.revision}",
            )


def _check_definition_variant(
    payload: (
        ResolvedDefinitionPayload | PartialDefinitionPayload | UnresolvedDefinitionPayload
    ),
    variant: DefinitionVariant,
) -> None:
    """Per-variant contracts + cross-field invariants (design §2.2a)."""
    if variant == DefinitionVariant.RESOLVED_TARGET_BINDING:
        if not isinstance(payload, ResolvedDefinitionPayload):
            raise EdgeError(
                EdgeErrorCode.NOT_RESOLVED, "RESOLVED_TARGET_BINDING needs resolved payload"
            )
        if payload.limitation_codes:
            raise EdgeError(
                EdgeErrorCode.NOT_RESOLVED, "RESOLVED_TARGET_BINDING with limitation codes"
            )
        _check_definition_lineage(payload.lineage.sources, payload.repository)
    elif variant == DefinitionVariant.PARTIAL_OBSERVATION:
        if not isinstance(payload, PartialDefinitionPayload):
            raise EdgeError(
                EdgeErrorCode.NOT_RESOLVED, "PARTIAL_OBSERVATION needs partial payload"
            )
        if not payload.limitation_codes:
            raise EdgeError(
                EdgeErrorCode.NOT_RESOLVED, "PARTIAL_OBSERVATION without limitation codes"
            )
        for code in payload.limitation_codes:
            limitation_category(code)
        _check_missing_taxonomy(list(payload.missing), list(payload.limitation_codes))
        if payload.target is not None and "target" in payload.missing:
            raise EdgeError(
                EdgeErrorCode.NOT_RESOLVED, "missing lists bound target: ['target'] present"
            )
        _check_definition_lineage(payload.lineage.sources, payload.repository)
    else:
        if not isinstance(payload, UnresolvedDefinitionPayload):
            raise EdgeError(
                EdgeErrorCode.NOT_RESOLVED, "UNRESOLVED_OBSERVATION needs unresolved payload"
            )
        if not payload.limitation_codes:
            raise EdgeError(
                EdgeErrorCode.NOT_RESOLVED, "UNRESOLVED_OBSERVATION without limitation codes"
            )
        for code in payload.limitation_codes:
            limitation_category(code)
        lineage = payload.lineage
        if lineage is not None:
            if payload.repository is None:
                raise EdgeError(
                    EdgeErrorCode.NOT_RESOLVED,
                    "UNRESOLVED lineage without repository: unbound endpoint inexpressible",
                )
            _check_definition_lineage(lineage.sources, payload.repository)
    _check_assumptions(list(payload.assumptions))


def compute_definition_id(
    variant: DefinitionVariant,
    payload: (
        ResolvedDefinitionPayload | PartialDefinitionPayload | UnresolvedDefinitionPayload
    ),
) -> str:
    """Content hash over the canonical preimage (never self-referential).

    Preimage: ``{"domain": "iap:definition", "record_version":
    "definition/v1", "variant": ..., <payload>}`` with the D1 canon/v1
    byte layout. Duplicates are defined by exact preimage equality.
    """
    _check_definition_variant(payload, variant)
    preimage = {
        "domain": DEFINITION_DOMAIN,
        "record_version": DEFINITION_RECORD_VERSION,
        "variant": variant.value,
        **_definition_payload_dict(payload),
    }
    raw = canonical_dict_bytes(preimage)
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def make_definition_observation(
    variant: DefinitionVariant,
    payload: (
        ResolvedDefinitionPayload | PartialDefinitionPayload | UnresolvedDefinitionPayload
    ),
) -> DefinitionObservation:
    """Build a well-formed observation (id computed, never supplied)."""
    return DefinitionObservation(
        observation_id=compute_definition_id(variant, payload),
        variant=variant,
        record_version=DEFINITION_RECORD_VERSION,
        payload=payload,
    )


def verify_definition_observation(data: dict[str, Any]) -> DefinitionObservation:
    """Parse + re-verify a stored record.

    Schema validation and ``observation_id`` recomputation precede
    availability: a malformed or hash-mismatched record becomes typed
    unavailability (EdgeError), never repaired, never normalized into a
    different record, never passed to the kernel.
    """
    if not isinstance(data, dict):
        raise EdgeError(EdgeErrorCode.HASH_MISMATCH, "record must be a mapping")
    if data.get("record_version", DEFINITION_RECORD_VERSION) != DEFINITION_RECORD_VERSION:
        raise EdgeError(
            EdgeErrorCode.HASH_MISMATCH, f"record_version={data.get('record_version')!r}"
        )
    try:
        variant = DefinitionVariant(data.get("variant"))
    except ValueError:
        raise EdgeError(EdgeErrorCode.HASH_MISMATCH, f"variant={data.get('variant')!r}") from None
    payload_data = data.get("payload")
    if not isinstance(payload_data, dict):
        raise EdgeError(EdgeErrorCode.HASH_MISMATCH, "payload must be a mapping")
    if variant is DefinitionVariant.RESOLVED_TARGET_BINDING:
        payload_cls: type[
            ResolvedDefinitionPayload | PartialDefinitionPayload | UnresolvedDefinitionPayload
        ] = ResolvedDefinitionPayload
    elif variant is DefinitionVariant.PARTIAL_OBSERVATION:
        payload_cls = PartialDefinitionPayload
    else:
        payload_cls = UnresolvedDefinitionPayload
    try:
        payload = payload_cls.model_validate(payload_data)
    except Exception as exc:
        raise EdgeError(EdgeErrorCode.HASH_MISMATCH, f"payload invalid: {exc}") from None
    _check_definition_variant(payload, variant)
    expected = compute_definition_id(variant, payload)
    if data.get("observation_id") != expected:
        raise EdgeError(
            EdgeErrorCode.HASH_MISMATCH,
            f"id {data.get('observation_id')!r} != recomputed {expected!r}",
        )
    return DefinitionObservation(
        observation_id=expected,
        variant=variant,
        record_version=DEFINITION_RECORD_VERSION,
        payload=payload,
    )


class TargetAdjudication(BaseModel):
    """Candidate-selection outcome (design §2.1a): pure, testable."""

    model_config = ConfigDict(frozen=True)

    variant: DefinitionVariant = Field(...)
    limitation_codes: list[LimitationCode] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)


def adjudicate_target_binding(n_candidates: int, has_usable_ref: bool) -> TargetAdjudication:
    """Apply the §2.1a triple to a candidate count (pure).

    Exactly one candidate → resolved; zero → PARTIAL/MISSING_INPUT
    (or UNRESOLVED when no usable ref exists at all); more than one →
    PARTIAL/AMBIGUOUS_TARGET. Negative counts are malformed input, never
    "zero candidates". No best-effort pick, ever.
    """
    if (
        not isinstance(n_candidates, int)
        or isinstance(n_candidates, bool)
        or n_candidates < 0
    ):
        raise EdgeError(EdgeErrorCode.BAD_RANGE, f"n_candidates: {n_candidates!r}")
    if n_candidates == 1:
        return TargetAdjudication(variant=DefinitionVariant.RESOLVED_TARGET_BINDING)
    if not has_usable_ref:
        return TargetAdjudication(
            variant=DefinitionVariant.UNRESOLVED_OBSERVATION,
            limitation_codes=[LimitationCode.MISSING_INPUT],
        )
    if n_candidates == 0:
        return TargetAdjudication(
            variant=DefinitionVariant.PARTIAL_OBSERVATION,
            limitation_codes=[LimitationCode.MISSING_INPUT],
            missing=["target"],
        )
    return TargetAdjudication(
        variant=DefinitionVariant.PARTIAL_OBSERVATION,
        limitation_codes=[LimitationCode.AMBIGUOUS_TARGET],
        missing=["target"],
    )
