# src/investigation_agent_platform/domain/proof/premises.py
"""G4 strict immutable premise records (premises/v1): closed, frozen, pure.

The semantic boundary for Component 2 rule application: every premise
artifact is parsed through one of these closed Pydantic v2 models before
any producer validator or independent checker function runs. All models
use ``frozen=True``, ``extra="forbid"``, and ``strict=True``: unknown
fields, coerced values, and null required fields fail at parse time,
never inside rule semantics.

Hash-preimage contract (frozen): the hashed artifact payload is the
normalized premise record MINUS the identity envelope — ``evidence_id``,
``content_hash``, and ``premise_type`` are never part of the hashed
bytes. The envelope rides alongside the artifact and is bound by the
manifest maps instead. Rationale: hashing ``content_hash`` would make
the preimage self-referential (the claimed digest would be an input to
its own computation), and hashing ``evidence_id``/``premise_type`` would
couple the content digest to inventory addressing. Both the producer
(``certificate._artifact_hash`` over the artifact as supplied) and the
independent checker (``checker_semantics._artifact_hash``) hash the
envelope-free bytes; callers supply artifacts without envelope keys and
both sides verify the envelope against the manifest separately.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Sha256 = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
FullSha = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
NonEmpty = Annotated[str, Field(min_length=1)]

CLOSED_PREMISE_TYPES = frozenset(
    {
        "RANGE_READ_FACT",
        "NORMALIZED_CALLSITE_FACT",
        "RUNTIME_OBSERVATION_FACT",
        "BUILD_SOURCE_MAPPING",
        "ABSENCE_COVERAGE_FACT",
        "COMMIT_HISTORY",
    }
)
"""Closed G4 premise vocabulary (frozen; append-only by reviewed contract)."""


class PremiseBase(BaseModel):
    """Shared closed envelope: identity, content hash, literal type tag."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    evidence_id: NonEmpty
    content_hash: Sha256
    premise_type: str


def _freeze_exclusions(value: Any) -> Any:
    """Coerce a JSON list into an immutable tuple before strict validation.

    Frozen models prevent field rebinding but cannot prevent in-place
    mutation of a nested list — which would silently alter the hashed
    content. Tuples have no mutating API, so the frozen guarantee holds
    by construction. Runs before strict type checks, which then see
    only tuples.
    """
    if isinstance(value, list):
        return tuple(value)
    return value


def _check_range(instance: Any) -> Any:
    """Shared line-range check: positive coordinates, start <= end."""
    if instance.line_start > instance.line_end:
        raise ValueError(
            f"line_start ({instance.line_start}) exceeds line_end ({instance.line_end})"
        )
    return instance


class RangeReadFact(PremiseBase):
    """One cited source range showing a defined symbol (definition/v1)."""

    premise_type: Literal["RANGE_READ_FACT"]
    repository: NonEmpty
    file_path: NonEmpty
    revision: NonEmpty
    line_start: Annotated[int, Field(gt=0)]
    line_end: Annotated[int, Field(gt=0)]
    target: NonEmpty
    window_text: NonEmpty

    _range_check = model_validator(mode="after")(_check_range)


class NormalizedCallsiteFact(PremiseBase):
    """One normalized call site binding caller and callee (call_edge/v1)."""

    premise_type: Literal["NORMALIZED_CALLSITE_FACT"]
    repository: NonEmpty
    file_path: NonEmpty
    revision: NonEmpty
    line_start: Annotated[int, Field(gt=0)]
    line_end: Annotated[int, Field(gt=0)]
    caller: NonEmpty
    callee: NonEmpty
    call_text: NonEmpty

    _range_check = model_validator(mode="after")(_check_range)


class BehaviorScope(BaseModel):
    """Closed BEHAVIOR scope: repository, revision, build, execution, env."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    repository: NonEmpty
    revision: FullSha
    build_identity: NonEmpty
    execution_id: NonEmpty
    environment: NonEmpty


class RuntimeObservationFact(PremiseBase):
    """One runtime observation of an asserted behavior (behavior/v1)."""

    premise_type: Literal["RUNTIME_OBSERVATION_FACT"]
    repository: NonEmpty
    symbol: NonEmpty
    predicate: NonEmpty
    observed_value: str | int | bool | None
    execution_id: NonEmpty
    build_identity: NonEmpty
    environment: NonEmpty
    source_revision: FullSha

    @field_validator("observed_value")
    @classmethod
    def _reject_floats(cls, value: str | int | bool | None) -> str | int | bool | None:
        """Canonical scalars exclude floats, even integral ones.

        Strict mode already rejects float input at type validation; this
        guard keeps the float prohibition explicit at the field where the
        contract lives (a failed strict-union error would otherwise be
        the only signal). ``bool`` passes: it is a canonical scalar,
        and strict mode keeps it distinct from ``int``.
        """
        if isinstance(value, float):
            raise TypeError("observed_value forbids floats (canonical scalar only)")
        return value


class BuildSourceMapping(PremiseBase):
    """Independent attestation binding a build to its source (behavior/v1).

    Admitted only with an approved provenance authority (test-only
    synthetic authority approved 2026-10-24; real-world authority OPEN).
    """

    premise_type: Literal["BUILD_SOURCE_MAPPING"]
    build_identity: NonEmpty
    repository: NonEmpty
    source_revision: FullSha
    provenance_ref: NonEmpty
    mapping_digest: Sha256


class AbsenceScope(BaseModel):
    """Closed ABSENCE scope: repository, revision, path prefix."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    repository: NonEmpty
    revision: FullSha
    path_scope: NonEmpty


class CoverageBasis(BaseModel):
    """Closed coverage-basis record: how completeness was established."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    kind: NonEmpty
    value: NonEmpty


class AbsenceCoverageFact(PremiseBase):
    """One typed complete-empty coverage record (absence/v1)."""

    premise_type: Literal["ABSENCE_COVERAGE_FACT"]
    universe: NonEmpty
    predicate: NonEmpty
    search_status: Literal["COMPLETE_EMPTY", "MATCHES_FOUND", "INCOMPLETE", "FAILED"]
    search_config_digest: Sha256
    scope: AbsenceScope
    exclusions: tuple[NonEmpty, ...]
    coverage_basis: CoverageBasis
    match_count: Annotated[int, Field(ge=0)]

    _freeze_exclusions_list = field_validator("exclusions", mode="before")(_freeze_exclusions)


class AbsenceCoverage(BaseModel):
    """Manifest-bound coverage record: the fact without identity envelope.

    Carries no ``evidence_id`` or ``content_hash`` (no self-reference):
    the manifest binds it by ID, hash, and type maps, while the full
    ``AbsenceCoverageFact`` artifact bytes live in the artifact store.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    premise_type: Literal["ABSENCE_COVERAGE_FACT"]
    universe: NonEmpty
    predicate: NonEmpty
    search_status: Literal["COMPLETE_EMPTY", "MATCHES_FOUND", "INCOMPLETE", "FAILED"]
    search_config_digest: Sha256
    scope: AbsenceScope
    exclusions: tuple[NonEmpty, ...]
    coverage_basis: CoverageBasis
    match_count: Annotated[int, Field(ge=0)]

    _freeze_exclusions_list = field_validator("exclusions", mode="before")(_freeze_exclusions)


class CommitHistory(PremiseBase):
    """One cited commit-history row (history/v1): full lowercase SHA only."""

    premise_type: Literal["COMMIT_HISTORY"]
    repository: NonEmpty
    repository_revision: FullSha
    commit_id: FullSha
    file_path: NonEmpty
    subject: NonEmpty
