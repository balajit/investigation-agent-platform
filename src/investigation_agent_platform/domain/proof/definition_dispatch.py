# src/investigation_agent_platform/domain/proof/definition_dispatch.py
"""D3 definition dispatch + authorization predicate — Phase 0, frozen.

Two-stage authorization contract (package v1.5 §2.3): Stage 1
evaluates association admissibility against the reviewed dispatch
rows; Stage 2 (the declaration-proof requirement check) is a named
placeholder stub that always records ``unsatisfied`` in Phase 0.
There is no ``AUTHORIZED`` value anywhere in this module: proof
authorization does not exist in Phase 0 code.

Dispatch machinery (``DispatchTable``, ``DispatchRow``,
``ResolverRegistry``, ``verify_resolver_binding``) is reused from
``dispatch.py`` unchanged — new definition rows, new fixture, no
change to D1 rows (owner decision 4: dedicated authorize path,
shared primitives only where contracts match).

Normative sources: ``docs/IAP-proof-kernel-d3-definition-plan-v1.3.md``
§§3, 8 and ``docs/IAP-proof-kernel-d3-phase0-approval-package-v1.5.md``
§§2-3. ``IAP_TYPED_DEFINITIONS`` authority is never exercised here.
"""

from __future__ import annotations

import hashlib
import os
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from investigation_agent_platform.domain.proof.canonical import canonical_dict_bytes
from investigation_agent_platform.domain.proof.definition_observation import (
    DEFINITION_DOMAIN,
    DEFINITION_RECORD_VERSION,
    DefinitionObservation,
    DefinitionVariant,
    ResolvedDefinitionPayload,
    _definition_payload_dict,
)
from investigation_agent_platform.domain.proof.dispatch import (
    ActualResolver,
    BundleApproval,
    CertificateBinding,
    DispatchError,
    DispatchErrorCode,
    DispatchRow,
    DispatchTable,
    PremiseBinding,
    ResolverRegistry,
    verify_resolver_binding,
)

SHADOW_TYPED_DEFINITIONS_ENV = "IAP_SHADOW_TYPED_DEFINITIONS"
TYPED_DEFINITIONS_ENV = "IAP_TYPED_DEFINITIONS"


def shadow_typed_definitions_enabled() -> bool:
    """Flag accessor: enabled iff ``IAP_SHADOW_TYPED_DEFINITIONS`` is exactly ``"1"``.

    Exact match, no stripping, no case folding, no truthy synonyms.
    Capture-only dual-run; never implies authority (package §4.0b).
    """
    return os.environ.get(SHADOW_TYPED_DEFINITIONS_ENV, "") == "1"


def typed_definitions_enabled() -> bool:
    """Flag accessor: enabled iff ``IAP_TYPED_DEFINITIONS`` is exactly ``"1"``.

    Exact match, no stripping, no case folding, no truthy synonyms.
    Without separate Phase 1 authorization, enabling this flag is a
    configuration error and must fail closed at startup (package §4.0b).
    """
    return os.environ.get(TYPED_DEFINITIONS_ENV, "") == "1"


class DefinitionStage(str, Enum):
    """Stage 1 association outcomes (closed; no authorization value exists)."""

    ASSOCIATION_ADMISSIBLE = "ASSOCIATION_ADMISSIBLE"
    ABSTAINED = "ABSTAINED"
    UNAVAILABLE = "UNAVAILABLE"


STUB_PHASE0_DECLARATION_CHECK_UNIMPLEMENTED = "STUB_PHASE0_DECLARATION_CHECK_UNIMPLEMENTED"
"""Named placeholder for the Stage 2 declaration-requirement check.

Phase 0 cannot demonstrate that a future proof requirement has been
correctly evaluated, so Stage 2 does not evaluate anything: it is a
non-authoritative negative guard that always records ``unsatisfied``
(see ``evaluate_declaration_requirement``). A unit test pins the stub
identity and the constant — any future logic beyond the constant fails
the test rather than silently passing. Phase 1 must deliberately
replace this stub (separately reviewed, separately authorized).
"""


def evaluate_declaration_requirement(record: DefinitionObservation) -> bool:
    """Stage 2 placeholder: always ``False`` in Phase 0.

    The argument is accepted (and type-checked) so the call site has
    the shape Phase 1 will fill; the result never depends on it.
    """
    if not isinstance(record, DefinitionObservation):
        raise DispatchError(DispatchErrorCode.BAD_ROW, "stage 2 requires a record")
    return False


class DefinitionAuthorizationDecision(BaseModel):
    """Outcome of the two-stage definition predicate (frozen).

    ``observation`` mirrors the shadow outcome vocabulary (package
    §0.2): the only success value is ``OBSERVATION_ADMISSIBLE``.
    ``declaration_requirement_satisfied`` is always ``False`` in
    Phase 0 (machine-readable, schema-pinned).
    """

    model_config = ConfigDict(frozen=True)

    stage1: DefinitionStage = Field(...)
    observation: str = Field(...)
    reason_code: str = Field(...)
    rule_trace: list[str] = Field(default_factory=list)
    association_scope: str = Field(default="source-range-only")
    declaration_requirement_satisfied: bool = Field(default=False)
    dispatch_row_digest: str = Field(default="")
    policy_digest: str = Field(default="")


def definition_content_digest(record: DefinitionObservation) -> str:
    """Digest over the record's canonical bytes (recomputed, never trusted)."""
    raw = canonical_dict_bytes(
        {
            "domain": DEFINITION_DOMAIN,
            "record_version": DEFINITION_RECORD_VERSION,
            "variant": record.variant.value,
            **_definition_payload_dict(record.payload),
        }
    )
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _deny(reason_code: str, trace: list[str]) -> DefinitionAuthorizationDecision:
    return DefinitionAuthorizationDecision(
        stage1=DefinitionStage.ABSTAINED,
        observation="ABSTAINED",
        reason_code=reason_code,
        rule_trace=list(trace),
    )


def authorize_definition_use(
    binding: CertificateBinding,
    record: DefinitionObservation,
    table: DispatchTable,
    registry: ResolverRegistry,
    premise: PremiseBinding,
) -> DefinitionAuthorizationDecision:
    """Dedicated two-stage definition authorization entry point (§8).

    Stage 1 (association): schema already validated at construction;
    canonical digest recomputed; exact dispatch lookup; row/policy
    digest match; premise identity; resolver binding; rule registry +
    ``requires_target_declaration`` gate. Any failure abstains with a
    named reason — never a weaker claim.
    Stage 2 (declaration requirement): the named stub, always
    ``unsatisfied`` in Phase 0.
    A non-resolved record never satisfies a declaration-requiring row
    (design invariant (f)): association identity must be present.
    """
    trace = ["stage1:start"]
    if record.variant != DefinitionVariant.RESOLVED_TARGET_BINDING:
        trace.append("stage1:variant-not-resolved")
        decision = _deny("variant-not-resolved", trace)
        return decision.model_copy(
            update={"declaration_requirement_satisfied": evaluate_declaration_requirement(record)}
        )

    row = table.lookup(
        binding.profile_id,
        binding.profile_version,
        record.payload.resolver.language,
        binding.claim_kind,
        premise.fact_category,
    )
    if row is None:
        trace.append("stage1:no-exact-dispatch-row")
        return _deny("unknown-dispatch", trace)
    trace.append(f"stage1:dispatch-row={row.key()!r}")
    if (
        binding.dispatch_row_digest != row.row_digest
        or binding.policy_digest != table.policy_digest()
    ):
        trace.append("stage1:digest-mismatch")
        return _deny("policy-mismatch", trace)

    if premise.observation_id != record.observation_id:
        trace.append("stage1:premise-id-mismatch")
        return _deny("premise-mismatch", trace)
    if premise.content_digest != definition_content_digest(record):
        trace.append("stage1:content-digest-mismatch")
        return _deny("premise-mismatch", trace)
    if premise.fact_category != row.fact_category:
        trace.append("stage1:premise-category-mismatch")
        return _deny("premise-mismatch", trace)
    if binding.claim_kind != row.claim_kind:
        trace.append("stage1:binding-kind-mismatch")
        return _deny("premise-mismatch", trace)
    if binding.claim_kind != "DEFINITION":
        trace.append("stage1:claim-kind-not-definition")
        return _deny("family-mismatch", trace)

    resolver = record.payload.resolver
    if (
        resolver.bundle != row.resolver_bundle
        or resolver.bundle_version != row.bundle_version
        or resolver.contract_digest != row.contract_digest
        or resolver.artifact_digest != row.artifact_digest
    ):
        trace.append("stage1:resolver-mismatch")
        return _deny("resolver-mismatch", trace)
    approval = registry.approved_bundles.get(resolver.bundle)
    if approval is None:
        trace.append("stage1:bundle-not-approved")
        return _deny("resolver-not-approved", trace)
    expected = approval.versions.get(resolver.bundle_version)
    if (
        expected is None
        or expected.get("contract_digest") != resolver.contract_digest
        or expected.get("artifact_digest") != resolver.artifact_digest
    ):
        trace.append("stage1:bundle-version-not-approved")
        return _deny("resolver-not-approved", trace)

    if row.rule_id not in registry.rule_ids():
        trace.append("stage1:unknown-rule")
        return _deny("unknown-rule", trace)
    # Only a resolved record carries a bound declaration identity; a
    # PARTIAL/UNRESOLVED record can never satisfy a declaration
    # requirement (design §2.1, owner decision 5). The earlier
    # variant check already abstained on non-resolved records, so
    # reaching here with one is a defect, not a decision.
    if isinstance(record.payload, ResolvedDefinitionPayload):
        target_present = record.payload.target is not None
    else:
        target_present = False
    if row.requires_target_declaration and not target_present:
        trace.append("stage1:declaration-identity-absent")
        return _deny("declaration-identity-absent", trace)

    trace.append("stage1:association-admissible")
    stage2 = evaluate_declaration_requirement(record)
    return DefinitionAuthorizationDecision(
        stage1=DefinitionStage.ASSOCIATION_ADMISSIBLE,
        observation="OBSERVATION_ADMISSIBLE",
        reason_code="association-admissible",
        rule_trace=trace,
        association_scope="source-range-only",
        declaration_requirement_satisfied=stage2,
        dispatch_row_digest=row.row_digest,
        policy_digest=table.policy_digest(),
    )


def verify_definition_binding(row: DispatchRow, actual: ActualResolver) -> None:
    """Execution-time binding check: definition rows, shared primitive.

    Delegates to ``verify_resolver_binding`` (owner decision 4: shared
    security primitives where contracts match). Raises DispatchError on
    any drift; returns None on match.
    """
    verify_resolver_binding(row, actual)


def load_reviewed_definition_table(
    fixture_path: object = None,
) -> tuple[DispatchTable, ResolverRegistry]:
    """Load + verify the reviewed definition dispatch artifact.

    The versioned fixture IS the reviewed artifact for Phase 0: rows
    are sealed (digests recomputed) and re-verified at load; the
    table digest is the policy identity. A tampered fixture raises
    at load. Production rollout replaces this with the reviewed
    deployment mapping — never silently.
    """
    from pathlib import Path

    if fixture_path is None:
        fixture_path = (
            Path(__file__).resolve().parents[4]
            / "tests"
            / "fixtures"
            / "proof_kernel"
            / "definition_dispatch_mapping.json"
        )
    else:
        fixture_path = Path(str(fixture_path))
    import json as _json

    doc = _json.loads(Path(fixture_path).read_text())
    rows = [DispatchRow(**dict(r)).sealed() for r in doc["definition_rows"]]
    table = DispatchTable(rows=rows)
    registry = ResolverRegistry(
        approved_bundles={
            name: BundleApproval(
                bundle=name,
                versions={str(v): dict(d) for v, d in info.get("versions", {}).items()},
            )
            for name, info in doc.get("resolver_bundles", {}).items()
        }
    )
    return table, registry
