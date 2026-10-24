# src/investigation_agent_platform/domain/proof/definition_shadow.py
"""Phase 0 shadow definition evaluation — pure bundle assembly + evaluator.

Two-layer evaluation (package v1.5 §4.0a): this module performs the
PURE construction + authorization layer only. It consumes validated
bundles assembled from already-loaded native rows and performs no
I/O: no fetches, no resolver invocations, no clock reads, no
persistence. Anything not resident is named in ``unavailable`` —
never fetched.

Honest Phase 0 semantics (package §§2.5-2.6, §9.2 schema audit):
the native rows carry no repository identity and no context-side
revision/scope, so the repository currency property is always
``unknown`` and construction of a bound record is never possible
live. The evaluator therefore runs every check it can, records a
full diagnostic trace, and abstains with a precise reason
(``unbound-repository``) — binding/abstention conformance, never
a weaker claim. The ADMISSIBLE association outcome lives at the
authorize level (``definition_dispatch.authorize_definition_use``,
fully covered there); this module proves the shadow path
evaluates resident evidence and fails closed on what is missing.

Normative sources: ``docs/IAP-proof-kernel-d3-definition-plan-v1.3.md``
§§2-4 and ``docs/IAP-proof-kernel-d3-phase0-approval-package-v1.5.md``
§§1-4. No imports from infrastructure, repos, clients, resolvers,
or workflows (statically enforced); native rows are read by
attribute (duck-typed) so this module depends on no application
model.
"""

from __future__ import annotations

import hashlib
import logging
import os
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictInt

from investigation_agent_platform.domain.proof.canonical import (
    CanonError,
    CanonicalizationError,
    canonical_dict_bytes,
)
from investigation_agent_platform.domain.proof.definition_dispatch import (
    DefinitionStage,
    shadow_typed_definitions_enabled,
    typed_definitions_enabled,
)
from investigation_agent_platform.domain.proof.dispatch import DispatchTable, ResolverRegistry

BUNDLE_VERSION = "definition-bundle/v1"
MAX_BOUND_ROWS = 256
MAX_RESIDENT_BYTES = 1024 * 1024


class ShadowOutcome(str, Enum):
    """Shadow outcome vocabulary (package §0.2): no AUTHORIZED value exists."""

    OBSERVATION_ADMISSIBLE = "OBSERVATION_ADMISSIBLE"
    ABSTAINED = "ABSTAINED"
    UNAVAILABLE = "UNAVAILABLE"


REASON_CODES = frozenset(
    {
        "not-a-definition-claim",
        "no-candidate-rows",
        "revision-mismatch",
        "unbound-repository",
        "unknown-scope",
        "oversized-input",
        "unavailable-input",
        "stale-or-unknown-context",
    }
)
"""Closed reason vocabulary: every abstention/unavailability names one."""


class CurrencyProperty(str, Enum):
    VALIDATED = "validated"
    UNKNOWN = "unknown"
    MISMATCH = "mismatch"


class CurrencyState(BaseModel):
    """Per-property currency (package §2.5): never a producer assertion."""

    model_config = ConfigDict(frozen=True)

    repository: CurrencyProperty = Field(...)
    revision: CurrencyProperty = Field(...)
    scope: CurrencyProperty = Field(...)


class ProvenanceWindow(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    file_path: str | None = Field(default=None, max_length=1024)
    line_start: StrictInt | None = Field(default=None)
    line_end: StrictInt | None = Field(default=None)
    revision: str | None = Field(default=None, max_length=128)


class BundleRowRef(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_id: str = Field(..., min_length=1, max_length=256)
    resident_content_digest: str = Field(..., min_length=1, max_length=256)
    provenance_window: ProvenanceWindow = Field(...)
    lineage_link: str = Field(..., min_length=1, max_length=256)


class ContextAnchor(BaseModel):
    """Typed context actually loaded (package §9.2 audit): ids only.

    No typed context-side repository/revision/scope exists on the
    investigation row, so there is no context side to compare against.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    investigation_id: str = Field(..., min_length=1, max_length=256)
    application_id: str = Field(default="", max_length=256)


class ValidatedDefinitionBundle(BaseModel):
    """Immutable validated input bundle (package §2.1, C1 layer 1).

    Carries NO iteration (activity-stamped only) and NO envelope:
    correlation identity is workflow-stamped at receipt (§1.4).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    bundle_version: str = Field(default=BUNDLE_VERSION, max_length=32)
    investigation_id: str = Field(..., min_length=1, max_length=256)
    hypothesis_id: str = Field(..., min_length=1, max_length=256)
    verifier_version: str = Field(..., min_length=1, max_length=32)
    triage_route: str = Field(default="", max_length=32)
    input_set: str = Field(..., min_length=1, max_length=32)
    claim_kind_snapshot: str = Field(default="DEFINITION", max_length=32)
    claim_target_snapshot: dict[str, Any] = Field(default_factory=dict)
    bound_rows: list[BundleRowRef] = Field(default_factory=list, max_length=256)
    refuting_ids: list[str] = Field(default_factory=list, max_length=256)
    resident_window_bytes_ref: str = Field(default="", max_length=256)
    context_anchor: ContextAnchor = Field(...)
    currency: CurrencyState = Field(...)
    unavailable: list[str] = Field(default_factory=list, max_length=32)
    bundle_digest: str = Field(default="", max_length=256)


class ShadowDefinitionObservation(BaseModel):
    """Shadow evaluator output (package §2.3): admissibility only.

    ``hypothesis_id`` binds the observation to the evaluated candidate:
    the join audit voids any pairing whose native outcome names a
    different candidate (no cross-candidate attribution, ever).
    """

    model_config = ConfigDict(frozen=True)

    observation_id: str = Field(..., min_length=1, max_length=256)
    bundle_digest: str = Field(..., min_length=1, max_length=256)
    hypothesis_id: str = Field(default="", max_length=256)
    stage1: DefinitionStage = Field(...)
    outcome: ShadowOutcome = Field(...)
    reason_code: str = Field(..., min_length=1, max_length=128)
    rule_trace: list[str] = Field(default_factory=list)
    association_scope: str = Field(default="source-range-only", max_length=64)
    declaration_requirement_satisfied: bool = Field(default=False)
    input_set: str = Field(..., min_length=1, max_length=32)
    flag_config_snapshot: dict[str, bool] = Field(default_factory=dict)


class ShadowError(ValueError):
    """Fail-closed shadow-evaluation error with a stable code."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        super().__init__(f"{code}: {detail}" if detail else code)


def _row_digest(evidence_id: str, snippet: str) -> str:
    raw = canonical_dict_bytes(
        {"domain": "iap:definition-resident-row", "evidence_id": evidence_id, "bytes": snippet}
    )
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def compute_bundle_digest(bundle: ValidatedDefinitionBundle) -> str:
    """Digest over the bundle content (excluding the digest itself)."""
    doc = bundle.model_dump(mode="json")
    doc.pop("bundle_digest", None)
    raw = canonical_dict_bytes({"domain": "iap:definition-bundle", **doc})
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def compute_currency(
    provenance_revisions: list[str | None],
    claim_commit_id: str | None,
    claim_scope: str | None,
) -> CurrencyState:
    """Currency from resident values only (package §2.5 + §9.2 audit).

    repository: no bound source on either side — always unknown.
    revision: validated iff row revisions AND claim commit_id are all
      present and equal; mismatch iff >1 distinct present value;
      otherwise unknown. revision-mismatch and unknown both abstain
      the checks that need corroborated revision (none may proceed
      on disagreement; absence is unknown, never assumed current).
    scope: validated-as-stated iff the claim states one (single-sided;
      corroboration is impossible in Phase 0 and is not claimed);
      unknown iff absent.
    """
    present_revs = sorted({r for r in provenance_revisions if r} | ({claim_commit_id} if claim_commit_id else set()))
    claim_side_present = claim_commit_id is not None and claim_commit_id != ""
    rows_present = any(provenance_revisions)
    if len(set(present_revs)) > 1:
        revision = CurrencyProperty.MISMATCH
    elif rows_present and claim_side_present and len(set(present_revs)) == 1:
        revision = CurrencyProperty.VALIDATED
    else:
        revision = CurrencyProperty.UNKNOWN
    scope = CurrencyProperty.VALIDATED if claim_scope else CurrencyProperty.UNKNOWN
    return CurrencyState(
        repository=CurrencyProperty.UNKNOWN,
        revision=revision,
        scope=scope,
    )


def _is_definition_claim(candidate: Any) -> bool:
    kind = getattr(candidate, "claim_kind", None)
    name = getattr(kind, "value", kind)
    if name == "DEFINITION":
        return True
    assessments = getattr(candidate, "assessments", None) or []
    for asm in assessments:
        akind = getattr(asm, "claim_kind", None)
        if getattr(akind, "value", akind) == "DEFINITION":
            return True
    return False


def assemble_bundle(
    *,
    candidate: Any,
    bound: list[Any],
    refuting_ids: list[str],
    triage_route: str,
    verifier_version: str,
    investigation_id: str,
    hypothesis_id: str,
    application_id: str,
    input_set: str,
) -> ValidatedDefinitionBundle:
    """Assemble the validated bundle from already-loaded locals (pure).

    No I/O: rows absent from the inputs are named in ``unavailable``,
    never fetched. Oversized resident input fails closed before any
    work. Claim target fields mirror the native ``ClaimTarget``
    (symbol may be absent → no usable reference, recorded downstream).
    """
    unavailable: list[str] = []
    if candidate is None:
        unavailable.append("candidate")
        claim_snapshot: dict[str, Any] = {}
        usable_ref = False
        is_definition = False
    else:
        is_definition = _is_definition_claim(candidate)
        target = getattr(candidate, "claim_target", None)
        if target is None:
            assessments = getattr(candidate, "assessments", None) or []
            for asm in assessments:
                found = getattr(asm, "claim_target", None)
                if found is not None:
                    target = found
                    break
        if target is None:
            claim_snapshot = {}
            usable_ref = False
        else:
            claim_snapshot = {
                "symbol": getattr(target, "symbol", None),
                "declaring_type": getattr(target, "declaring_type", None),
                "file_path": getattr(target, "file_path", None),
                "commit_id": getattr(target, "commit_id", None),
                "scope": getattr(target, "scope", None),
            }
            usable_ref = bool(claim_snapshot.get("symbol"))
        if not usable_ref:
            unavailable.append("usable-target-reference")

    if not is_definition and candidate is not None:
        unavailable.append("definition-claim")
    if not bound:
        unavailable.append("bound-rows")

    rows: list[BundleRowRef] = []
    total_bytes = 0
    provenance_revisions: list[str | None] = []
    for row in bound[:MAX_BOUND_ROWS]:
        snippet = getattr(row, "content_snippet", "") or ""
        if not isinstance(snippet, str):
            continue
        total_bytes += len(snippet.encode("utf-8"))
        if total_bytes > MAX_RESIDENT_BYTES:
            raise ShadowError("OVERSIZED_INPUT", f"resident bytes exceed {MAX_RESIDENT_BYTES}")
        prov = getattr(row, "provenance", None)
        loc = getattr(prov, "source_location", None) if prov is not None else None
        window = ProvenanceWindow(
            file_path=getattr(loc, "file_path", None),
            line_start=getattr(loc, "line_start", None),
            line_end=getattr(loc, "line_end", None),
            revision=getattr(loc, "revision", None),
        )
        provenance_revisions.append(window.revision)
        eid = str(getattr(row, "evidence_id", ""))
        if not eid:
            continue
        rows.append(
            BundleRowRef(
                evidence_id=eid,
                resident_content_digest=_row_digest(eid, snippet),
                provenance_window=window,
                lineage_link=eid,
            )
        )
    if bound and not rows:
        unavailable.append("resident-bytes")

    window_ref_raw = canonical_dict_bytes(
        {"resident_rows": sorted(r.resident_content_digest for r in rows)}
    )
    window_ref = "sha256:" + hashlib.sha256(window_ref_raw).hexdigest()

    currency = compute_currency(
        provenance_revisions,
        (claim_snapshot.get("commit_id") if claim_snapshot else None),
        (claim_snapshot.get("scope") if claim_snapshot else None),
    )
    bundle = ValidatedDefinitionBundle(
        investigation_id=str(investigation_id),
        hypothesis_id=str(hypothesis_id),
        verifier_version=str(verifier_version),
        triage_route=str(triage_route or ""),
        input_set=str(input_set),
        claim_target_snapshot=claim_snapshot,
        bound_rows=rows,
        refuting_ids=[str(r) for r in (refuting_ids or [])],
        resident_window_bytes_ref=window_ref,
        context_anchor=ContextAnchor(
            investigation_id=str(investigation_id),
            application_id=str(application_id or ""),
        ),
        currency=currency,
        unavailable=sorted(set(unavailable)),
    )
    return bundle.model_copy(update={"bundle_digest": compute_bundle_digest(bundle)})


def _observation_id(
    bundle_digest: str,
    hypothesis_id: str,
    stage1: DefinitionStage,
    outcome: ShadowOutcome,
    reason: str,
    trace: list[str],
    input_set: str,
) -> str:
    raw = canonical_dict_bytes(
        {
            "domain": "iap:definition-shadow-observation",
            "bundle_digest": bundle_digest,
            "hypothesis_id": hypothesis_id,
            "stage1": stage1.value,
            "outcome": outcome.value,
            "reason": reason,
            "trace": trace,
            "association_scope": "source-range-only",
            "declaration_requirement_satisfied": False,
            "input_set": input_set,
        }
    )
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def evaluate_definition_shadow(
    bundle: ValidatedDefinitionBundle,
    table: DispatchTable,
    registry: ResolverRegistry,
    flags: dict[str, bool],
) -> ShadowDefinitionObservation:
    """Pure shadow evaluation over a validated bundle (no I/O).

    Runs every applicable check, records a full diagnostic trace, and
    applies the strict outcome rule: missing inputs → UNAVAILABLE;
    revision mismatch → ABSTAINED (stale-or-unknown-context);
    unknown scope → ABSTAINED (stale-or-unknown-context);
    unbound repository → ABSTAINED (unbound-repository). The bundle
    digest is recomputed (never trusted). Dispatch lookup and record
    authorization run only behind a bound repository (package §2.2a:
    an unbound endpoint cannot drift into a binding); in Phase 0 the
    repository is never bound, so live evaluation always abstains
    with a precise reason — binding/abstention conformance. The
    ADMISSIBLE association outcome is proven at the authorize level
    (definition_dispatch), fully covered there.
    """

    def decide(
        stage1: DefinitionStage,
        outcome: ShadowOutcome,
        reason: str,
        trace: list[str],
    ) -> ShadowDefinitionObservation:
        if reason not in REASON_CODES:
            raise ShadowError("BAD_REASON", reason)
        return ShadowDefinitionObservation(
            observation_id=_observation_id(
                bundle.bundle_digest, bundle.hypothesis_id, stage1, outcome, reason, trace, bundle.input_set
            ),
            bundle_digest=bundle.bundle_digest,
            hypothesis_id=bundle.hypothesis_id,
            stage1=stage1,
            outcome=outcome,
            reason_code=reason,
            rule_trace=list(trace),
            input_set=bundle.input_set,
            flag_config_snapshot=dict(flags),
        )

    trace = ["evaluate:start", f"input_set:{bundle.input_set}"]
    for row in table.rows:
        row.verify()
    trace.append(f"dispatch-policy-verified:{table.policy_digest()[:19]}")
    if not registry.approved_bundles:
        raise ShadowError("UNAPPROVED_BUNDLE", "no approved bundles at evaluation")
    if bundle.bundle_digest != compute_bundle_digest(bundle):
        trace.append("bundle-digest-mismatch")
        return decide(DefinitionStage.UNAVAILABLE, ShadowOutcome.UNAVAILABLE,
                      "unavailable-input", trace)
    if "definition-claim" in bundle.unavailable:
        trace.append("not-a-definition-claim")
        return decide(DefinitionStage.UNAVAILABLE, ShadowOutcome.UNAVAILABLE,
                      "not-a-definition-claim", trace)
    if "bound-rows" in bundle.unavailable or not bundle.bound_rows:
        trace.append("no-candidate-rows")
        return decide(DefinitionStage.UNAVAILABLE, ShadowOutcome.UNAVAILABLE,
                      "no-candidate-rows", trace)
    if bundle.currency.revision == CurrencyProperty.MISMATCH:
        trace.append("revision-mismatch")
        return decide(DefinitionStage.ABSTAINED, ShadowOutcome.ABSTAINED,
                      "stale-or-unknown-context", trace)
    if bundle.currency.scope == CurrencyProperty.UNKNOWN:
        trace.append("scope-unknown")
        return decide(DefinitionStage.ABSTAINED, ShadowOutcome.ABSTAINED,
                      "stale-or-unknown-context", trace)
    trace.append("repository-unbound")
    return decide(DefinitionStage.ABSTAINED, ShadowOutcome.ABSTAINED,
                  "unbound-repository", trace)


# --- Trust verification (package §3; pure functions over explicit inputs) -----


def derive_manifest_digest(
    package_files: dict[str, bytes] | None,
    dependencies: dict[str, str] | None,
    config_surface: dict[str, str] | None,
) -> str:
    """Manifest digest over exact bytes + dependency map + config surface.

    Any unmeasurable component (None) refuses closed: fail closed,
    never skip. Combination order is fixed (artifact, dependency,
    configuration) so two builds of the same approved inputs produce
    byte-identical manifests.
    """
    if package_files is None or dependencies is None or config_surface is None:
        raise ShadowError("UNMEASURABLE_INPUT", "manifest component missing")
    artifact_raw = canonical_dict_bytes(
        {"artifact": sorted((k, v.hex()) for k, v in package_files.items())}
    )
    artifact_digest = "sha256:" + hashlib.sha256(artifact_raw).hexdigest()
    dep_raw = canonical_dict_bytes({"dependencies": sorted(dependencies.items())})
    dep_digest = "sha256:" + hashlib.sha256(dep_raw).hexdigest()
    cfg_lines = "".join(f"{k}={v}\n" for k, v in sorted(config_surface.items()))
    cfg_raw = cfg_lines.encode("utf-8")
    cfg_digest = "sha256:" + hashlib.sha256(cfg_raw).hexdigest()
    combined = canonical_dict_bytes(
        {"artifact": artifact_digest, "dependencies": dep_digest, "config": cfg_digest}
    )
    return "sha256:" + hashlib.sha256(combined).hexdigest()


def check_config_surface(reads: set[str], allowlist: set[str]) -> None:
    """Unallowlisted behavior-affecting reads are contract violations."""
    outside = set(reads) - set(allowlist)
    if outside:
        raise ShadowError("ALLOWLIST_VIOLATION", f"unallowlisted reads: {sorted(outside)}")


def verify_shadow_startup(
    table: DispatchTable,
    registry: ResolverRegistry,
    manifest_inputs: dict[str, Any] | None,
    allowlist: set[str],
    reads: set[str],
) -> str:
    """Startup trust verification: refuse the shadow path loudly on drift.

    Re-verifies loaded row digests against the reviewed table, the
    manifest digest, and the config-surface allowlist. Returns the
    manifest digest on success; raises ShadowError (fail closed) on
    any mismatch, unmeasurable input, or unallowlisted read.
    """
    for row in table.rows:
        row.verify()
    policy = table.policy_digest()
    if manifest_inputs is None:
        raise ShadowError("UNMEASURABLE_INPUT", "manifest inputs missing")
    manifest = derive_manifest_digest(
        manifest_inputs.get("package_files"),
        manifest_inputs.get("dependencies"),
        manifest_inputs.get("config_surface"),
    )
    check_config_surface(reads, allowlist)
    if not registry.approved_bundles:
        raise ShadowError("UNAPPROVED_BUNDLE", "no approved bundles")
    return policy + ":" + manifest


def summarize_shadow_definitions(
    observations: list[dict[str, Any]],
    dropped_count: int = 0,
    duplicate_count: int = 0,
    telemetry_omissions: int = 0,
) -> dict[str, Any]:
    """Pure per-turn summary (mirrors summarize_trace): counts only.

    ``by_input_set`` / ``by_reason`` are the §1.6 frozen-inventory
    per-input-set counts and reason-code breakdown (additive dicts;
    keys bounded by the closed input-set and reason vocabularies).
    """
    summary: dict[str, Any] = {
        "turns": 0,
        "admissible": 0,
        "abstained": 0,
        "unavailable": 0,
        "capture_errors": 0,
        "mirror_notes": 0,
        "dropped_count": max(0, int(dropped_count or 0)),
        "duplicate_count": max(0, int(duplicate_count or 0)),
        "telemetry_omissions": max(0, int(telemetry_omissions or 0)),
        "by_input_set": {},
        "by_reason": {},
    }
    by_input_set: dict[str, int] = summary["by_input_set"]
    by_reason: dict[str, int] = summary["by_reason"]
    for obs in observations:
        if not isinstance(obs, dict):
            summary["capture_errors"] += 1
            continue
        summary["turns"] += 1
        input_set = obs.get("input_set")
        if isinstance(input_set, str) and input_set:
            by_input_set[input_set[:64]] = by_input_set.get(input_set[:64], 0) + 1
        reason = obs.get("reason_code", obs.get("reason", ""))
        if isinstance(reason, str) and reason:
            by_reason[reason[:128]] = by_reason.get(reason[:128], 0) + 1
        outcome = obs.get("outcome")
        if outcome == "OBSERVATION_ADMISSIBLE":
            summary["admissible"] += 1
        elif outcome == "ABSTAINED":
            summary["abstained"] += 1
        elif outcome == "UNAVAILABLE":
            summary["unavailable"] += 1
        elif isinstance(obs.get("tap"), str) and obs.get("tap") in MIRROR_NOTE_TAPS:
            # T2/T3 mirror note (gate-note shape carries no outcome
            # vocabulary): a recorded coverage note, never a capture
            # error. Counted in turns/by_input_set/by_reason above.
            summary["mirror_notes"] += 1
            tag = f"mirror:{obs['tap']}"
            by_reason[tag] = by_reason.get(tag, 0) + 1
        else:
            summary["capture_errors"] += 1
    return summary


_logger = logging.getLogger(__name__)


def resolve_definition_mode() -> str:
    """Flag gate for the governed path: returns "off" or "shadow".

    Malformed values in either position mean off AND are logged loudly
    at the governed entry point (visible misconfiguration, zero
    authority risk). ``"1"`` in the authority position without Phase 1
    authorization is a hard failure: raises ShadowError, which the
    activity translates to a non-retryable configuration error —
    governed paths refuse, never silently fall back.
    """
    for env_name in ("IAP_SHADOW_TYPED_DEFINITIONS", "IAP_TYPED_DEFINITIONS"):
        raw = os.environ.get(env_name)
        if raw is not None and raw != "" and raw != "1":
            _logger.warning(
                "definition flag misconfigured: %s=%r treated as off (exact '1' required)",
                env_name,
                raw,
            )
    if typed_definitions_enabled():
        raise ShadowError(
            "AUTHORITY_WITHOUT_PHASE1",
            "IAP_TYPED_DEFINITIONS=1 without Phase 1 authorization: refusing governed path",
        )
    return "shadow" if shadow_typed_definitions_enabled() else "off"


_SDK_IDENTITY_FIELDS = (
    "namespace",
    "workflow_id",
    "workflow_run_id",
    "activity_id",
    "attempt",
)
"""Temporal activity identity tuple (C2): exact field names of
``temporalio.activity.Info`` (pinned SDK). The accepted-attempt rule
groups observations by (activity_id, attempt); the group present in
the accepted result is the accepted attempt."""


def run_shadow_turn(
    *,
    candidate: Any,
    bound: list[Any],
    refuting_ids: list[str],
    triage_route: str,
    verifier_version: str,
    investigation_id: str,
    hypothesis_id: str,
    application_id: str,
    input_set: str,
    table: DispatchTable,
    registry: ResolverRegistry,
) -> dict[str, Any] | None:
    """T1/T3 tap worker (pure wiring around the pure evaluator).

    Returns an observation dict, a SHADOW_FAILED marker dict on any
    failure (native path continues unchanged), or None when the shadow
    flag is off (no observation, no key, no persistence — N21/F01).
    """
    if resolve_definition_mode() != "shadow":
        return None
    try:
        bundle = assemble_bundle(
            candidate=candidate,
            bound=list(bound),
            refuting_ids=list(refuting_ids or []),
            triage_route=triage_route,
            verifier_version=verifier_version,
            investigation_id=investigation_id,
            hypothesis_id=hypothesis_id,
            application_id=application_id,
            input_set=input_set,
        )
        flags = {
            "shadow_flag": True,
            "authority_flag": False,
        }
        obs = evaluate_definition_shadow(bundle, table, registry, flags)
        return obs.model_dump(mode="json")
    except ShadowError as exc:
        return {
            "capture_error": True,
            "reason": exc.code,
            "input_set": input_set,
        }
    except Exception as exc:  # never let shadow break native
        return {
            "capture_error": True,
            "reason": type(exc).__name__,
            "input_set": input_set,
        }


def attach_shadow_observations(
    result_data: dict[str, Any], observations: list[dict[str, Any]]
) -> dict[str, Any]:
    """Additive result key (N21): absent unless observations exist."""
    if not observations:
        return result_data
    merged = dict(result_data)
    merged["shadow_definitions"] = [dict(o) for o in observations if isinstance(o, dict)]
    if not merged["shadow_definitions"]:
        return result_data
    return merged


_FLOAT_FORMAT_SPEC = ".6f"


MIRROR_NOTE_TAPS = ("T2a", "T2b", "T3")
"""Tap tags whose gate-note shape carries no outcome vocabulary.

T2a/T2b verify-side mirror notes and T3 conclude-side retagged notes are
recorded coverage, never capture errors.
"""


def _canonicalizable(value: Any, where: str) -> Any:
    """Rebuild `value` with no floats for canonical envelope bytes (DIGEST-01).

    JSON floats have no canonical text form, so the envelope digest cannot
    cover them: finite floats become fixed-precision decimal strings
    (``format(x, ".6f")`` — 1e-6 fidelity, deterministic across platforms).
    Non-finite floats (NaN/±Infinity) raise CanonicalizationError with
    INVALID_FLOAT, exactly as the canonical-byte validation does today.
    Mappings and sequences are rebuilt (inputs are never mutated); every
    other value passes through unchanged for downstream validation.
    """
    if isinstance(value, float):
        # No math import (static import gate): non-finite floats are exactly
        # the ones whose shortest repr is a non-numeric token.
        if str(value).lower() in ("nan", "inf", "-inf", "infinity", "-infinity"):
            raise CanonicalizationError(CanonError.INVALID_FLOAT, where)
        return format(value, _FLOAT_FORMAT_SPEC)
    if isinstance(value, dict):
        return {k: _canonicalizable(v, f"{where}.{k}") for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonicalizable(v, f"{where}[{i}]") for i, v in enumerate(value)]
    return value


def build_correlation_envelope(
    observation: dict[str, Any],
    iteration: int,
    receipt_index: int,
    native_outcome: dict[str, Any],
    activity_identity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """    Workflow-stamped correlation envelope (C1 layer 2, C3 receipt pairing).

    The activity never mutates the observation: the workflow copies it
    into the envelope and digests (observation + iteration +
    receipt_index + native snapshot + activity identity) once, at receipt.

    Float normalization: the canonical bytes forbid floats (no canonical
    text form), so finite floats anywhere in the envelope body become
    fixed-precision decimal strings before digesting, and the envelope
    stores exactly those normalized bytes — stored and digested forms are
    identical, so offline recompute always agrees.
    """
    norm_observation = _canonicalizable(observation, "$.observation")
    norm_native = _canonicalizable(native_outcome, "$.native_outcome")
    norm_identity = _canonicalizable(activity_identity or {}, "$.activity_identity")
    body = canonical_dict_bytes(
        {
            "domain": "iap:definition-correlation-envelope",
            "observation": norm_observation,
            "iteration": int(iteration),
            "receipt_index": int(receipt_index),
            "native_outcome": norm_native,
            "activity_identity": norm_identity,
        }
    )
    return {
        "observation": norm_observation,
        "iteration": int(iteration),
        "receipt_index": int(receipt_index),
        "native_outcome": norm_native,
        "activity_identity": norm_identity,
        "envelope_digest": "sha256:" + hashlib.sha256(body).hexdigest(),
    }


def pair_receipt(
    observations: list[dict[str, Any]],
    iteration: int,
    receipt_start_index: int,
    native_outcome: dict[str, Any],
    activity_identity: dict[str, Any],
) -> tuple[list[dict[str, Any]], int]:
    """Receipt-order pairing with duplicate delivery dedup (C2/RV08).

    Returns (envelopes, duplicates). Duplicates share the full identity
    key ((namespace, workflow_id, workflow_run_id, activity_id, attempt)
    plus the canonical observation bytes): keep-first, count the rest.
    Content-identity (not field subset) is the criterion — two distinct
    notes always differ in tap, input set, reason, or outcome, so exact
    duplicates can only be redeliveries. Observations carrying a
    non-accepted (activity_id, attempt) group are classified
    superseded-attempt downstream (N22).
    """
    envelopes: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    duplicates = 0
    receipt_index = int(receipt_start_index)
    for obs in observations:
        if not isinstance(obs, dict):
            envelopes.append(
                build_correlation_envelope(
                    {"capture_error": True, "reason": "malformed-observation"},
                    iteration,
                    receipt_index,
                    native_outcome,
                    activity_identity,
                )
            )
            receipt_index += 1
            continue
        content_ref = canonical_dict_bytes(
            {"observation": {k: obs.get(k) for k in sorted(obs.keys())}}
        )
        key = (
            activity_identity.get("namespace"),
            activity_identity.get("workflow_id"),
            activity_identity.get("workflow_run_id"),
            activity_identity.get("activity_id"),
            activity_identity.get("attempt"),
            "sha256:" + hashlib.sha256(content_ref).hexdigest(),
        )
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
        envelopes.append(
            build_correlation_envelope(
                obs, iteration, receipt_index, native_outcome, activity_identity
            )
        )
        receipt_index += 1
    return envelopes, duplicates


TELEMETRY_ENTRY_KEYS = (
    "observation_id",
    "bundle_digest",
    "outcome",
    "reason_code",
    "iteration",
    "input_set",
    "producing_path",
    "native_status",
    "flag_config",
)
"""Frozen telemetry projection (F-B3/RV03): ids, digests, labels, reason
codes only — no snippets, no source text, no rule traces. Not
expandable without review."""


def project_envelope_for_telemetry(envelope: dict[str, Any]) -> dict[str, Any]:
    """Project one envelope onto the allowlisted telemetry shape."""
    obs = envelope.get("observation", {}) if isinstance(envelope, dict) else {}
    native = envelope.get("native_outcome", {}) if isinstance(envelope, dict) else {}
    if not isinstance(obs, dict):
        obs = {}
    if not isinstance(native, dict):
        native = {}
    projected = {
        "observation_id": str(obs.get("observation_id", ""))[:256],
        "bundle_digest": str(obs.get("bundle_digest", ""))[:256],
        "outcome": str(obs.get("outcome", ""))[:64],
        "reason_code": str(obs.get("reason_code", ""))[:128],
        "iteration": envelope.get("iteration", -1) if isinstance(envelope, dict) else -1,
        "input_set": str(obs.get("input_set", ""))[:64],
        "producing_path": str(native.get("producing_path", ""))[:64],
        "native_status": str(native.get("status", ""))[:64],
        "flag_config": {
            k: bool(v)
            for k, v in (obs.get("flag_config_snapshot", {}) or {}).items()
            if k in ("shadow_flag", "authority_flag")
        },
    }
    return {k: v for k, v in projected.items() if k in TELEMETRY_ENTRY_KEYS}


def project_trace_for_telemetry(
    envelopes: list[dict[str, Any]], cap: int = 256
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Bounded trace projection: first-``cap`` prefix retained (C7).

    Running counts are computed by the summarizer over ALL turns;
    per-turn detail keeps the first ``cap`` entries. Later entries
    are dropped with ``dropped_count`` + ``trace_truncated`` (never
    silent); dropped turns are excluded as truncation exclusions and
    never reconstructed (D3P-N26). Malformed entries (non-dict, or
    boolean iteration) are dropped with ``malformed_dropped`` —
    malformed detail never silently passes shaping.
    """
    projected: list[dict[str, Any]] = []
    malformed = 0
    cap_dropped = 0
    for entry in envelopes:
        if not isinstance(entry, dict) or isinstance(entry.get("iteration"), bool):
            malformed += 1
            continue
        if len(projected) >= cap:
            cap_dropped += 1
            continue
        projected.append(project_envelope_for_telemetry(entry))
    dropped = cap_dropped + malformed
    health = {
        "dropped_count": dropped,
        "trace_truncated": cap_dropped > 0,
        "malformed_dropped": malformed,
    }
    return projected, health


def audit_envelope_join(envelope: dict[str, Any]) -> str:
    """Join audit for one paired envelope (C3/N24): "ok" or a void reason.

    Void conditions: missing producing_path with no site-reached
    evidence (ambiguous join — never guessed); execution-identity
    mismatch; missing native outcome. The harness voids the run set
    on any void verdict (N22/N24).
    """
    if not isinstance(envelope, dict):
        return "void:malformed-envelope"
    native = envelope.get("native_outcome")
    if not isinstance(native, dict) or not native:
        return "void:missing-native-outcome"
    obs = envelope.get("observation", {})
    if not isinstance(obs, dict) or not obs:
        return "void:missing-observation"
    native_hyp = native.get("hypothesis_id")
    obs_hyp = obs.get("hypothesis_id")
    if native_hyp and obs_hyp and str(native_hyp) != str(obs_hyp):
        return "void:candidate-mismatch"
    if not native.get("producing_path") and not obs.get("site_reached"):
        return "void:unattributable-path"
    identity = envelope.get("activity_identity", {})
    if isinstance(identity, dict) and identity.get("superseded_attempt"):
        return "void:superseded-attempt"
    return "ok"


def telemetry_omission_flagged(passed_telemetry: Any, derived: Any) -> bool:
    """Whether a telemetry omission must be counted (C5 Path A).

    True iff the caller supplied a non-empty telemetry payload but
    derivation produced nothing — i.e. the omission is attributable
    to the derivation step, not to an absent input. Pure and
    unit-testable; the activity stamps the in-band flag from this.
    """
    try:
        return bool(passed_telemetry) and derived is None
    except Exception:
        return False
