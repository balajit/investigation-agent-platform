# src/investigation_agent_platform/domain/proof/evaluation.py
"""WP-B2 immutable evaluation: ``P = Evaluate(C, E, R, M)`` (pure).

Evaluation binds one canonical proposition (C), one evidence manifest
(E), one rule-bundle version (R), and one model/encoding config (M) into
a derivation record — or fails typed instead of proving over missing
premises. It never mutates its inputs: frozen models in, new records
out. New evidence means a new manifest digest, hence a new context and
a new derivation; old records are never updated.

Retention rule (K10): a premise whose artifact is unavailable, or whose
stored content hash does not match the available bytes, makes the whole
evaluation ``PREMISE_UNAVAILABLE`` — infrastructure failure never becomes
a negative proof verdict, and evaluation never proceeds on partial
premises. Availability is an explicit input (``available_hashes`` mapping
premise ID to the ``sha256:`` digest of the bytes actually on hand);
rule-dispatch and certificate checking belong to WP-B3/WP-B4.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from investigation_agent_platform.domain.proof.canonical import (
    context_id,
    derivation_id,
    proposition_id,
)
from investigation_agent_platform.domain.proof.manifest import (
    EvidenceManifest,
    manifest_digest,
)


class EvaluationFailure(str, Enum):
    """Typed evaluation failures (frozen; subset of the §3.4 outcomes)."""

    PREMISE_UNAVAILABLE = "PREMISE_UNAVAILABLE"


class PremiseUnavailable(Exception):
    """Evaluation refused: premises missing or hash-mismatched (K10).

    Carries the offending premise IDs so callers can retrieve, restore,
    or reevaluate — never prove.
    """

    def __init__(
        self,
        missing: list[str] | None = None,
        mismatched: list[str] | None = None,
    ) -> None:
        self.missing = list(missing or [])
        self.mismatched = list(mismatched or [])
        super().__init__(
            f"{EvaluationFailure.PREMISE_UNAVAILABLE.value}: "
            f"missing={self.missing} mismatched={self.mismatched}"
        )


class EvaluationInputs(BaseModel):
    """Frozen inputs to one evaluation: C, E, R, M."""

    model_config = ConfigDict(frozen=True)

    proposition: dict[str, Any] = Field(...)
    manifest: EvidenceManifest = Field(...)
    rule_bundle_version: str = Field(default="", max_length=64)
    model_encoding: dict[str, Any] = Field(default_factory=dict)
    rule_id: str = Field(default="", max_length=128)
    rule_version: str = Field(default="", max_length=64)
    implementation_digest: str = Field(default="", max_length=128)
    inference: dict[str, Any] = Field(default_factory=dict)


class Derivation(BaseModel):
    """One immutable derivation record binding C, E, R, M.

    ``inference`` carries the rule-instance application when a rule
    engine has run (WP-B3); empty here means manifest-bound but not yet
    rule-discharged. The IDs bind inputs by content: any input change
    yields a different derivation, never a mutated one.
    """

    model_config = ConfigDict(frozen=True)

    derivation_id: str = Field(...)
    proposition_id: str = Field(...)
    context_id: str = Field(...)
    manifest_digest: str = Field(...)
    rule_id: str = Field(default="", max_length=128)
    rule_version: str = Field(default="", max_length=64)
    implementation_digest: str = Field(default="", max_length=128)
    premise_ids: list[str] = Field(default_factory=list)
    inference: dict[str, Any] = Field(default_factory=dict)


def _freeze_context(
    manifest: EvidenceManifest,
    rule_bundle_version: str,
    model_encoding: dict[str, Any],
) -> dict[str, Any]:
    """Frozen evaluation context (reconciliation state starts unreconciled)."""
    return {
        "evidence_manifest_digest": manifest_digest(manifest),
        "model_encoding": dict(model_encoding or {}),
        "reconciliation_state": "unreconciled",
        "rule_bundle_version": rule_bundle_version,
    }


def evaluate(
    inputs: EvaluationInputs,
    available_hashes: Mapping[str, str] | None = None,
) -> Derivation:
    """Run one immutable evaluation; fail typed on unavailable premises.

    When ``available_hashes`` is given, every manifest premise must be
    present with a matching content hash, else :class:`PremiseUnavailable`.
    When omitted, manifest-declared hashes are trusted as the retention
    record (the B4 checker always re-verifies against actual bytes).
    """
    manifest = inputs.manifest
    if available_hashes is not None:
        missing = [pid for pid in manifest.evidence_ids if pid not in available_hashes]
        mismatched = [
            pid
            for pid in manifest.evidence_ids
            if pid in available_hashes
            and manifest.content_hashes.get(pid) is not None
            and available_hashes[pid] != manifest.content_hashes[pid]
        ]
        if missing or mismatched:
            raise PremiseUnavailable(missing=missing, mismatched=mismatched)
    prop_id = proposition_id(inputs.proposition)
    digest = manifest_digest(manifest)
    ctx = _freeze_context(manifest, inputs.rule_bundle_version, inputs.model_encoding)
    ctx_id = context_id(prop_id, ctx)
    premise_ids = list(manifest.evidence_ids)
    der_id = derivation_id(
        ctx_id,
        inputs.rule_id,
        inputs.rule_version,
        inputs.implementation_digest,
        premise_ids,
        dict(inputs.inference or {}),
    )
    return Derivation(
        derivation_id=der_id,
        proposition_id=prop_id,
        context_id=ctx_id,
        manifest_digest=digest,
        rule_id=inputs.rule_id,
        rule_version=inputs.rule_version,
        implementation_digest=inputs.implementation_digest,
        premise_ids=premise_ids,
        inference=dict(inputs.inference or {}),
    )
