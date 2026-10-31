# src/investigation_agent_platform/application/worker/activities/verification.py
"""Temporal activity thin adapters — each delegates to the application/domain layer via AppContext."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from temporalio import activity

try:
    from temporalio.exceptions import ApplicationFailure  # type: ignore[attr-defined]
except ImportError:
    from temporalio.exceptions import (
        ApplicationError as ApplicationFailure,  # type: ignore[assignment, no-redef]
    )

logger = logging.getLogger(__name__)

from investigation_agent_platform.domain.common.exceptions import PlatformConfigurationError
from investigation_agent_platform.domain.proof.definition_dispatch import (
    load_reviewed_definition_table,
)
from investigation_agent_platform.domain.proof.definition_shadow import (
    ShadowError,
    attach_shadow_observations,
    resolve_definition_mode,
    run_shadow_turn,
)
from investigation_agent_platform.infrastructure.observability.tracing import traced

from .common import (
    COMP_ACTVTY,
    OUTCOME_COMPLETED,
    OUTCOME_FAILED,
    OUTCOME_SKIPPED,
    OUTCOME_STARTED,
    _activity_extra,
    _activity_outcome,
    _application_failure_from_exc,
    _coerced_data,
    _emit_verify_telemetry,
    _story_debug,
    elapsed_ms,
    story_log,
    tick,
)
from .inputs import (
    GenericActivityResult,
    VerifyRootCauseInput,
    _get_ctx,
    _policy_for_loop_version,
    _select_verification_policy,
)

_DEFINITION_DISPATCH_TABLE: Any = None
_DEFINITION_DISPATCH_REGISTRY: Any = None


def _load_definition_dispatch_table() -> tuple[Any, Any]:
    """Cached reviewed table (loads via definition_dispatch; see it)."""
    global _DEFINITION_DISPATCH_TABLE, _DEFINITION_DISPATCH_REGISTRY
    if _DEFINITION_DISPATCH_TABLE is not None and _DEFINITION_DISPATCH_REGISTRY is not None:
        return _DEFINITION_DISPATCH_TABLE, _DEFINITION_DISPATCH_REGISTRY
    table, registry = load_reviewed_definition_table()
    _DEFINITION_DISPATCH_TABLE = table
    _DEFINITION_DISPATCH_REGISTRY = registry
    return table, registry


def reload_definition_dispatch_table() -> tuple[Any, Any]:
    """Reload entry point (package §3.2, reload row).

    Drops the cached table and re-derives it from the reviewed
    fixture: rows are re-sealed (digests recomputed) and re-verified
    before any further construction. A tampered or unreadable
    fixture raises (fail closed) and leaves no half-loaded cache
    behind — further construction is refused until digests match
    again. Nothing in the native path calls this; it exists so the
    reload path has a single governed mechanism to test.
    """
    global _DEFINITION_DISPATCH_TABLE, _DEFINITION_DISPATCH_REGISTRY
    table, registry = load_reviewed_definition_table()
    _DEFINITION_DISPATCH_TABLE = table
    _DEFINITION_DISPATCH_REGISTRY = registry
    return table, registry


def _verification_status_value(result: Any) -> str:
    """Verification status as a plain string, Enum- or string-safe.

    Never raises: unparseable/missing statuses read as "" (unverified).
    """
    try:
        status = getattr(result, "status", None)
        if status is None:
            return ""
        return str(getattr(status, "value", status) or "")
    except Exception:
        return ""


@activity.defn
@traced("worker", outcome_from=_activity_outcome, extra_from=_activity_extra)
async def verify_root_cause_activity(
    params: VerifyRootCauseInput,
) -> GenericActivityResult:
    # TO_DO-011: not idempotency-wired — pure compute over persisted state,
    # no durable writes of its own; re-execution is safe by construction.
    t0 = tick()
    data = _coerced_data(VerifyRootCauseInput, params, activity="verify_root_cause_activity")
    tenant_id_probe = str(data.get("tenant_id", ""))
    inv_id_probe = str(data.get("investigation_id", ""))
    story_log(
        logger,
        logging.INFO,
        "verify_root_cause_activity started",
        comp=COMP_ACTVTY,
        func="verify_root_cause_activity",
        stage="verify",
        step="start",
        outcome=OUTCOME_STARTED,
        tenant_id=tenant_id_probe,
        investigation_id=inv_id_probe,
        event="verify_root_cause_activity started",
    )
    try:
        tenant_id = str(data.get("tenant_id", ""))
        inv_id_str = str(data.get("investigation_id", ""))
        if not tenant_id or not inv_id_str:
            return GenericActivityResult(
                success=False, error="tenant_id and investigation_id required"
            )
        try:
            investigation_id = UUID(inv_id_str)
        except (ValueError, TypeError) as exc:
            # Malformed IDs are non-retryable client errors: fail fast here
            # instead of bubbling into ApplicationFailure (Temporal retry).
            return GenericActivityResult(
                success=False, error=f"invalid investigation_id format: {exc}"
            )
        _inv: Any = None
        try:
            definition_shadow_mode = resolve_definition_mode()
        except ShadowError as exc:
            # Authority flag without Phase 1 authorization: hard refusal of
            # the governed path (non-retryable config error, loud). The
            # native path is not silently continued under a forbidden flag.
            story_log(
                logger,
                logging.ERROR,
                "verify_root_cause_activity refusing governed path",
                comp=COMP_ACTVTY,
                func="verify_root_cause_activity",
                stage="verify",
                step="init_shadow",
                outcome=OUTCOME_FAILED,
                tenant_id=tenant_id,
                investigation_id=inv_id_str,
                event="verify_root_cause_activity refusing governed path",
                error=str(exc),
            )
            raise PlatformConfigurationError(f"definition shadow refused: {exc}") from exc
        ctx = _get_ctx()
        evidence_repo: Any = getattr(ctx, "evidence_repo", None)
        hypothesis_repo: Any = getattr(ctx, "hypothesis_repo", None)

        evidence_items: list[Any] = []
        hypotheses: list[Any] = []
        if evidence_repo is not None:
            try:
                evidence_items = await evidence_repo.find_by_investigation_id(
                    tenant_id, investigation_id
                )
            except Exception as exc:
                story_log(
                    logger,
                    logging.ERROR,
                    "verify_root_cause_activity evidence fetch failed",
                    comp=COMP_ACTVTY,
                    func="verify_root_cause_activity",
                    stage="verify",
                    step="fetch_evidence",
                    outcome=OUTCOME_FAILED,
                    tenant_id=tenant_id,
                    investigation_id=inv_id_str,
                    event="verify_root_cause_activity evidence fetch failed",
                    error=str(exc),
                    error_type=type(exc).__name__,
                    exc_info=True,
                )
                raise _application_failure_from_exc(exc) from exc
        if hypothesis_repo is not None:
            try:
                hypotheses = await hypothesis_repo.find_by_investigation_id(
                    tenant_id, investigation_id
                )
            except Exception as exc:
                story_log(
                    logger,
                    logging.ERROR,
                    "verify_root_cause_activity hypothesis fetch failed",
                    comp=COMP_ACTVTY,
                    func="verify_root_cause_activity",
                    stage="verify",
                    step="fetch_hypotheses",
                    outcome=OUTCOME_FAILED,
                    tenant_id=tenant_id,
                    investigation_id=inv_id_str,
                    event="verify_root_cause_activity hypothesis fetch failed",
                    error=str(exc),
                    error_type=type(exc).__name__,
                    exc_info=True,
                )
                raise _application_failure_from_exc(exc) from exc

        if not hypotheses:
            # Story integrity: the started record above must always pair
            # with a terminal record — otherwise the trace shows dangling
            # verify starts (live trace 3c80875c: two starts, zero
            # completions, VERIFYING bouncing straight back).
            story_log(
                logger,
                logging.INFO,
                "verify_root_cause_activity skipped; no hypotheses to verify",
                comp=COMP_ACTVTY,
                func="verify_root_cause_activity",
                stage="verify",
                step="verify",
                outcome=OUTCOME_SKIPPED,
                tenant_id=tenant_id,
                investigation_id=inv_id_str,
                event="verify_root_cause_activity skipped",
                elapsed_ms=elapsed_ms(t0),
                outputs={"verified": False, "reason": "no_hypotheses"},
                evidence_count=len(evidence_items or []),
            )
            return GenericActivityResult(
                success=True, data={"verified": False, "reason": "no_hypotheses"}
            )

        from investigation_agent_platform.application.investigation.verification import (
            CLAIM_BINDING_CBV1,
            CLAIM_BINDING_CBV2,
            CLAIM_BINDING_CBV3,
            VERIFIER_VERSION_CURRENT,
            VERIFIER_VERSION_LEGACY,
            CausalRelationship,
            ClaimBindingEnforcement,
            UnknownVerifierVersion,
            VerificationEngine,
            VerifierVersionUnavailable,
            bound_kept_evidence_only,
            rank_hypotheses,
            resolve_claim_binding_version,
            resolve_verifier_version,
            select_verifiable_candidate,
        )
        from investigation_agent_platform.domain.hypothesis.models import ClaimKind

        # Version-contained routing (g4-closure): the investigation's recorded
        # verifier version decides the semantics — never an env switch.
        # Unmarked rows resolve to current; only rows explicitly pinned v1
        # run legacy. Anything else fails closed. L2-D3-fix gates key on
        # VERIFIER_VERSION_CURRENT, so legacy rows keep their exact v1 shape.
        investigation_repo: Any = getattr(ctx, "investigation_repo", None)
        if investigation_repo is None:
            # No repository wired (tests/dev): no persisted row exists to
            # misinterpret, so unmarked current semantics apply. Loud, not
            # silent: the provenance reads "no-repository", never "pinned".
            story_log(
                logger,
                logging.WARNING,
                "verify proceeding without investigation repository; "
                "unmarked current verifier semantics apply",
                comp=COMP_ACTVTY,
                func="verify_root_cause_activity",
                stage="verify",
                step="resolve_version",
                outcome=OUTCOME_SKIPPED,
                tenant_id=tenant_id,
                investigation_id=inv_id_str,
                event="verify no-repository fallback",
                outputs={"version_source": "no-repository"},
            )
            _inv = None
            verifier_version = VERIFIER_VERSION_CURRENT
            _version_source = "no-repository"
            # Phase 1 (claim binding): no persisted row exists to misread,
            # so unmarked resolves to current (cbv1 during rollout).
            claim_binding_version = CLAIM_BINDING_CBV1
            _binding_source = "no-repository"
        else:
            try:
                _inv = await investigation_repo.get_by_id(tenant_id, investigation_id)
            except Exception as exc:
                return GenericActivityResult(
                    success=False,
                    error="verifier version unavailable: "
                    f"repository read failed ({type(exc).__name__})",
                )
            if _inv is None:
                return GenericActivityResult(
                    success=False,
                    error="verifier version unavailable: investigation not found",
                )
            try:
                verifier_version, _version_source = resolve_verifier_version(
                    getattr(_inv, "metadata", None)
                )
            except (UnknownVerifierVersion, VerifierVersionUnavailable) as exc:
                return GenericActivityResult(
                    success=False,
                    error=f"verifier version unavailable: {exc}",
                )
            # Phase 1 (claim binding): same recorded-version contract on
            # the same metadata row. Unknown/unreadable pins fail closed,
            # exactly like the verifier pin above.
            try:
                claim_binding_version, _binding_source = resolve_claim_binding_version(
                    getattr(_inv, "metadata", None)
                )
            except (UnknownVerifierVersion, VerifierVersionUnavailable) as exc:
                return GenericActivityResult(
                    success=False,
                    error=f"claim binding version unavailable: {exc}",
                )
        contradiction_repo: Any = getattr(ctx, "contradiction_repo", None)
        contradictions: list[Any] = []
        if contradiction_repo is not None:
            try:
                contradictions = await contradiction_repo.find_by_investigation_id(
                    tenant_id, investigation_id
                )
            except Exception:
                contradictions = []
        evidence_by_id = {getattr(e, "evidence_id", None): e for e in evidence_items}

        # P0-3: ranked candidates, bound evidence, real contradictions and
        # causal chain. Rows pinned to v1 keep the old first-hypothesis /
        # whole-corpus / empty-lists behavior they started with.
        ranked = rank_hypotheses(list(hypotheses))
        # G9: prefer the candidate whose citations resolve — a reprocess-
        # carried copy with purged supporting rows must not shadow a live
        # twin. Falls back to head-eligible; abstention path unchanged.
        candidate = select_verifiable_candidate(
            ranked, evidence_by_id, ("REJECTED", "CONTRADICTED", "VERIFIED")
        )
        if candidate is None:
            # Finding M: no-candidate/no-hypotheses is an explicit empty
            # feed (claim_gaps=[]), never a missing key. The carrier turns
            # [] into a tombstone that supersedes stale feeds; a missing
            # key means enforcement faulted — distinct states, distinct
            # signals. Legacy-pinned rows keep their exact keyless shape.
            _no_candidate_data: dict[str, Any] = {
                "verified": False,
                "reason": "no_eligible_hypothesis",
                "producing_path": "no-candidate",
            }
            try:
                if verifier_version != VERIFIER_VERSION_LEGACY:
                    _no_candidate_data["claim_gaps"] = []
            except Exception:
                pass
            return GenericActivityResult(
                success=True,
                data=_no_candidate_data,
            )
        # Finding L: opinion-only candidates skip every activity-layer
        # validator (fast paths + repairs). The shared predicate (N)
        # screens on assessments alone — no evidence, no model, no
        # retrieval. The general evaluate path below keeps its own
        # projection as defense in depth; enforcement, gap feed, marker,
        # and telemetry run unchanged so the INCONCLUSIVE surfaces in
        # the same evidence-only channel.
        try:
            from investigation_agent_platform.application.evidence.retrieval_ledger import (
                all_typed_claims_not_checkable as _all_nc_early,
            )
        except Exception:
            _all_nc_early = None
        try:
            _nc_early = bool(_all_nc_early(candidate)) if _all_nc_early is not None else False
        except Exception:
            _nc_early = False

        def _resolve(ids: list[Any]) -> list[Any]:
            out = []
            for raw in ids or []:
                try:
                    key = raw if not isinstance(raw, str) else UUID(raw)
                except (ValueError, TypeError, AttributeError):
                    continue
                item = evidence_by_id.get(key)
                if item is not None:
                    out.append(item)
            return out

        engine = VerificationEngine(
            verifier_version=verifier_version,
            claim_binding_version=claim_binding_version,
        )
        policy = _policy_for_loop_version(
            _select_verification_policy(data.get("triage_route", "")),
            getattr(_inv, "metadata", None),
        )
        # D3 Phase 0 shadow state is observational and runs beside the one
        # active dev verifier behavior.
        definition_shadow_observations: list[dict[str, Any]] = []
        producing_path: str | None = None
        # G3: timeline-backed recorded attempts for the verify-time
        # directive producer below. Bound here so every producing path
        # (legacy, fast-path, blocked, general) sees a defined value;
        # the general branch re-derives it before evaluating.
        _verify_recorded_attempts: list | None = None
        _shadow_identity: dict[str, Any] = {}
        if candidate is not None:
            bound = _resolve(getattr(candidate, "supporting_evidence_ids", []) or [])
            refuting = _resolve(getattr(candidate, "refuting_evidence_ids", []) or [])
            # Phase 1 (claim binding): timeline-backed linkage for the
            # authoritative partition. Fail-open to None (unmapped
            # degradation — unknown evaporation still applies); a read
            # fault here never blocks verification.
            _binding_recorded: list[Any] | None = None
            _binding_enforcement = None
            # Phase 3 (claim binding): fast-path repair records collected
            # by the history/location wraps below. Initialized up front so
            # the enforcement choke never reads an unbound name.
            _verify_repairs: list[Any] = []
            if claim_binding_version in (CLAIM_BINDING_CBV2, CLAIM_BINDING_CBV3):
                try:
                    from investigation_agent_platform.application.evidence.retrieval_ledger import (
                        merge_recorded_attempts as _merge_attempts,
                    )
                    from investigation_agent_platform.application.evidence.retrieval_ledger import (
                        recorded_attempt_summaries as _attempt_summaries,
                    )
                    from investigation_agent_platform.application.evidence.retrieval_ledger import (
                        recorded_retrieval_attempts as _timeline_attempts,
                    )

                    _b_recent = data.get("recent_actions", []) or []
                    _b_summaries = _attempt_summaries(_b_recent)
                    _b_repo = getattr(ctx, "timeline_repo", None)
                    _b_events = None
                    if _b_repo is not None:
                        try:
                            _b_events = await _b_repo.find_by_investigation_id(
                                tenant_id, investigation_id
                            )
                        except Exception:
                            _b_events = None
                    if _b_events is None:
                        _binding_recorded = _b_summaries
                    else:
                        _binding_recorded = _merge_attempts(
                            _timeline_attempts(_b_events or []),
                            _b_summaries or [],
                        )
                except Exception:
                    _binding_recorded = None
            # Tranche B: range-read rows for files the claim names join the
            # bound set even when the model cited only the search-match
            # sibling (statement-grounded inclusion — same file, proven
            # provenance; concession/contradiction gates still apply).
            # Under cbv2 the inclusion is gated: only claim-local-linked
            # or explicitly cited rows join; it can never substitute for
            # zero cited-kept survival.
            try:
                _bound_ids = {getattr(e, "evidence_id", None) for e in bound}
                if claim_binding_version in (CLAIM_BINDING_CBV2, CLAIM_BINDING_CBV3):
                    _gated_cited: set[str] = set()
                    try:
                        for _raw in getattr(candidate, "supporting_evidence_ids", []) or []:
                            _gated_cited.add(str(_raw))
                        for _asm in getattr(candidate, "assessments", []) or []:
                            _gated_cited.add(str(getattr(_asm, "evidence_id", "") or ""))
                        for _step in getattr(candidate, "causal_steps", []) or []:
                            for _raw in getattr(_step, "evidence_ids", []) or []:
                                _gated_cited.add(str(_raw))
                    except Exception:
                        pass
                    _gated_linked: set[str] = set()
                    try:
                        for _banked, _att in _binding_recorded or []:
                            for _raw in _banked or []:
                                _gated_linked.add(str(_raw))
                    except Exception:
                        pass
                    _gated_allow = _gated_cited | _gated_linked
                    for _extra in VerificationEngine.statement_range_reads(
                        str(getattr(candidate, "statement", "") or ""),
                        list(evidence_by_id.values()),
                    ):
                        _xid = getattr(_extra, "evidence_id", None)
                        if _xid in _bound_ids:
                            continue
                        if str(_xid) not in _gated_allow:
                            continue
                        _bound_ids.add(_xid)
                        bound.append(_extra)
                else:
                    for _extra in VerificationEngine.statement_range_reads(
                        str(getattr(candidate, "statement", "") or ""),
                        list(evidence_by_id.values()),
                    ):
                        if getattr(_extra, "evidence_id", None) not in _bound_ids:
                            _bound_ids.add(getattr(_extra, "evidence_id", None))
                            bound.append(_extra)
            except Exception:
                pass
            # Phase 1 (claim binding): filter the working set to kept
            # citations before any kind gate runs. Uncited rows pass
            # through; cited-but-unbanked rows evaporate (recorded, never
            # silent). Under cbv1 this block is skipped byte-identically.
            if claim_binding_version in (CLAIM_BINDING_CBV2, CLAIM_BINDING_CBV3):
                try:
                    _binding_enforcement = engine.claim_binding_enforcement(
                        candidate,
                        list(evidence_by_id.values()),
                        _binding_recorded,
                    )
                    if _binding_enforcement is not None:
                        _kept = set(_binding_enforcement.kept_ids or [])
                        _cited = set(_binding_enforcement.cited_ids or [])
                        bound, _evap, _ckept = bound_kept_evidence_only(bound, _kept, _cited)
                except Exception:
                    # Fail closed: without a partition the filtered set is
                    # untrusted; a fault enforcement forces INCONCLUSIVE at
                    # the choke point below instead of verifying on it.
                    _binding_enforcement = ClaimBindingEnforcement(fault=True)
            # D3 Phase 0 shadow tap T1 (read-only; never feeds back).
            # Observes the already-loaded candidate/bound rows through the
            # pure shadow evaluator. Fail-closed for the shadow (any fault
            # records a SHADOW_FAILED marker); fail-open for native (the
            # native path below is untouched by whatever happens here).
            # T1 observes only the general (non-legacy) branch: legacy
            # rows keep version-pinned semantics with no shadow tap, and
            # ``definition_shadow_observations`` (initialized above) simply
            # stays empty there.
            if definition_shadow_mode == "shadow":
                try:
                    _shadow_info = activity.info()
                    _shadow_identity = {
                        "namespace": _shadow_info.namespace,
                        "workflow_id": _shadow_info.workflow_id,
                        "workflow_run_id": _shadow_info.workflow_run_id,
                        "activity_id": _shadow_info.activity_id,
                        "attempt": _shadow_info.attempt,
                    }
                except Exception:
                    pass
                try:
                    _shadow_table, _shadow_registry = _load_definition_dispatch_table()
                    _shadow_obs = run_shadow_turn(
                        candidate=candidate,
                        bound=list(bound),
                        refuting_ids=[
                            str(getattr(e, "evidence_id", "") or "") for e in (refuting or [])
                        ],
                        triage_route=str(data.get("triage_route", "") or ""),
                        verifier_version=str(verifier_version),
                        investigation_id=str(inv_id_str),
                        hypothesis_id=str(getattr(candidate, "id", "") or ""),
                        application_id=str(getattr(_inv, "application_id", "") or ""),
                        input_set="BOUND_FULL",
                        table=_shadow_table,
                        registry=_shadow_registry,
                    )
                    if _shadow_obs is not None:
                        _shadow_obs.setdefault("activity_identity", _shadow_identity)
                        definition_shadow_observations.append(_shadow_obs)
                except Exception:
                    definition_shadow_observations.append(
                        {"capture_error": True, "reason": "tap-fault", "input_set": "BOUND_FULL"}
                    )

            def _definition_observe(note: dict[str, Any]) -> None:
                """T2 mirror-note sink (record-only; never feeds back).

                Mode-gated: with the shadow flag off no note is recorded,
                so the additive result key stays absent (per-flag table
                Off/Off, D3P-N21/F01/F09). The gate lives here — one point
                covering every engine call site in this activity.
                """
                try:
                    if definition_shadow_mode != "shadow":
                        return
                    if isinstance(note, dict):
                        stamped = dict(note)
                        stamped.setdefault("activity_identity", dict(_shadow_identity))
                        definition_shadow_observations.append(stamped)
                except Exception:
                    pass

            # Finding L: skipped on the opinion-only early path — an
            # unfalsifiable claim is never code-value validated.
            if _nc_early:
                code_value_result = None
            else:
                code_value_result = engine.validate_code_value_claim(
                    candidate, bound, contradictions
                )
            code_value_claim = (
                VerificationEngine.claims_field_value(
                    str(getattr(candidate, "statement", "") or "")
                )
                or str(VerificationEngine._fact_category_of(candidate) or "") == "SOURCE_VALUE_FACT"
            )
            code_value_answered = code_value_result is not None and not refuting
            # Phase 3 (claim binding): fast-path repair wrap (D6 site 2).
            # revalidate_with_repair runs the validate call verbatim —
            # identical under cbv1 — and performs one repair pass under
            # cbv2/cbv3. Repairs collect here for enforcement extension
            # and diagnostics attachment below.
            _verify_siblings = [
                h
                for h in (hypotheses or [])
                if getattr(h, "id", None) != getattr(candidate, "id", None)
            ]
            # Finding L: skipped on the opinion-only early path (no
            # history proof is attempted for an unfalsifiable claim).
            if _nc_early:
                history_result = None
            else:
                try:
                    history_result, _history_repairs = engine.revalidate_with_repair(
                        lambda h, w: engine.validate_history_claim(h, w, contradictions),
                        candidate,
                        bound,
                        list(evidence_by_id.values()),
                        contradictions,
                        _verify_siblings,
                        enforcement=_binding_enforcement,
                        expect_kinds=(ClaimKind.HISTORY,),
                    )
                    _verify_repairs.extend(list(_history_repairs or []))
                except Exception:
                    history_result = engine.validate_history_claim(candidate, bound, contradictions)
            history_answered = history_result is not None and not refuting
            # Init up front: every fast path below assigns only on its taken
            # branch, and the general-build guard is skipped when
            # code_value_claim is true — without this, a field-value claim
            # with no deterministic resolution leaves causal_chain unbound
            # (UnboundLocalError at the filter below; live L5 WORKFLOW_ERROR).
            causal_chain: list = []
            if code_value_answered:
                result = code_value_result
                producing_path = "code-value"
                causal_chain = []
            elif history_answered:
                result = history_result
                producing_path = "history"
                causal_chain = []
            # G1: location-answer fast path — validated code citations answer
            # location questions without causal machinery. Skipped under
            # legacy verifier mode (pre-P0-3 semantics preserved).
            # Finding L: skipped on the opinion-only early path.
            location_result = None
            _location_repairs = []
            if _nc_early:
                pass
            else:
                try:
                    location_result, _location_repairs = engine.revalidate_with_repair(
                        lambda h, w: engine.validate_location_claim(
                            h, w, contradictions, _definition_observe
                        ),
                        candidate,
                        bound,
                        list(evidence_by_id.values()),
                        contradictions,
                        _verify_siblings,
                        enforcement=_binding_enforcement,
                        expect_kinds=(ClaimKind.DEFINITION, ClaimKind.CALL_EDGE),
                    )
                    _verify_repairs.extend(list(_location_repairs or []))
                except Exception:
                    location_result = engine.validate_location_claim(
                        candidate, bound, contradictions, _definition_observe
                    )
            location_answered = (
                location_result is not None
                and not refuting
                and not code_value_claim
                and not history_answered
                and not (
                    verifier_version == VERIFIER_VERSION_CURRENT
                    and VerificationEngine.resolve_claim_kind(
                        candidate,
                        strict=engine.claim_binding_version
                        in (CLAIM_BINDING_CBV2, CLAIM_BINDING_CBV3),
                    )
                    == ClaimKind.CALL_EDGE
                )
            )
            if location_answered:
                result = location_result
                producing_path = "location"
                causal_chain = []
            # T1-1: doc-answer fast path (mirrors location). Skipped under
            # legacy verifier mode like the location path.
            # Finding L: skipped on the opinion-only early path.
            doc_answered = False
            doc_result = None
            if (
                not _nc_early
                and not location_answered
                and not code_value_answered
                and not history_answered
                and not code_value_claim
            ):
                try:
                    doc_result = engine.validate_doc_claim(candidate, bound, contradictions)
                    doc_answered = doc_result is not None and not refuting
                    if doc_answered:
                        result = doc_result
                        producing_path = "doc"
                        causal_chain = []
                    elif doc_result is not None and refuting:
                        story_log(
                            logger,
                            logging.INFO,
                            "doc answer withheld: refuting evidence bound",
                            comp=COMP_ACTVTY,
                            func="verify_root_cause_activity",
                            stage="verify",
                            step="validate_doc",
                            outcome=OUTCOME_SKIPPED,
                            tenant_id=tenant_id,
                            investigation_id=inv_id_str,
                            event="doc answer withheld",
                            outputs={"reason": "refuting_evidence_bound"},
                            hypothesis_id=str(candidate.id),
                        )
                except Exception:
                    pass
            else:
                if location_result is not None and refuting:
                    story_log(
                        logger,
                        logging.INFO,
                        "location answer withheld: refuting evidence bound",
                        comp=COMP_ACTVTY,
                        func="verify_root_cause_activity",
                        stage="verify",
                        step="validate_location",
                        outcome=OUTCOME_SKIPPED,
                        tenant_id=tenant_id,
                        investigation_id=inv_id_str,
                        event="location answer withheld",
                        outputs={"reason": "refuting_evidence_bound"},
                        hypothesis_id=str(candidate.id),
                    )
            # #65: state-answer fast path (mirrors doc). Banked DATABASE_STATE
            # rows answer value questions without causal machinery.
            # Finding L: skipped on the opinion-only early path.
            state_answered = False
            state_result = None
            if (
                not _nc_early
                and not location_answered
                and not code_value_answered
                and not history_answered
                and not doc_answered
                and not code_value_claim
            ):
                try:
                    state_result = engine.validate_state_claim(candidate, bound, contradictions)
                    state_answered = state_result is not None and not refuting
                    story_log(
                        logger,
                        logging.INFO,
                        "state answer decision",
                        comp=COMP_ACTVTY,
                        func="verify_root_cause_activity",
                        stage="verify",
                        step="validate_state",
                        outcome=OUTCOME_COMPLETED,
                        tenant_id=tenant_id,
                        investigation_id=inv_id_str,
                        event="state answer decision",
                        outputs={"validated": state_result is not None},
                        hypothesis_id=str(candidate.id),
                        bound_count=len(bound),
                        bound_types=sorted(
                            {
                                str(
                                    getattr(
                                        getattr(e, "evidence_type", ""),
                                        "value",
                                        getattr(e, "evidence_type", ""),
                                    )
                                )
                                for e in bound
                            }
                        ),
                        validated=state_result is not None,
                    )
                    if state_answered:
                        result = state_result
                        producing_path = "state"
                        causal_chain = []
                    elif state_result is not None and refuting:
                        story_log(
                            logger,
                            logging.INFO,
                            "state answer withheld: refuting evidence bound",
                            comp=COMP_ACTVTY,
                            func="verify_root_cause_activity",
                            stage="verify",
                            step="validate_state",
                            outcome=OUTCOME_SKIPPED,
                            tenant_id=tenant_id,
                            investigation_id=inv_id_str,
                            event="state answer withheld",
                            outputs={"reason": "refuting_evidence_bound"},
                            hypothesis_id=str(candidate.id),
                        )
                except Exception:
                    pass
            if (
                not location_answered
                and not code_value_answered
                and not history_answered
                and not doc_answered
                and not state_answered
                and not code_value_claim
            ):

                def _resolve_evidence_key(raw: Any) -> Any:
                    if raw in evidence_by_id:
                        return raw
                    try:
                        as_uuid = raw if not isinstance(raw, str) else UUID(raw)
                        if as_uuid in evidence_by_id:
                            return as_uuid
                        as_str = str(raw)
                        if as_str in evidence_by_id:
                            return as_str
                    except (ValueError, TypeError, AttributeError):
                        pass
                    return None

                causal_chain = []
                for s in getattr(candidate, "causal_steps", []) or []:
                    resolved_ids = []
                    for raw_eid in s.evidence_ids or []:
                        matched_key = _resolve_evidence_key(raw_eid)
                        if matched_key is not None:
                            resolved_ids.append(matched_key)
                    if resolved_ids:
                        causal_chain.append(
                            CausalRelationship(
                                cause=s.cause,
                                effect=s.effect,
                                mechanism=s.mechanism,
                                confidence=0.7,
                                evidence_ids=resolved_ids,
                            )
                        )
            causal_chain = [c for c in causal_chain if c.evidence_ids]
            caller_edge_gate = None
            caller_edge_validated = False
            if (
                verifier_version == VERIFIER_VERSION_CURRENT
                and VerificationEngine.resolve_claim_kind(
                    candidate,
                    strict=engine.claim_binding_version in (CLAIM_BINDING_CBV2, CLAIM_BINDING_CBV3),
                )
                == ClaimKind.CALL_EDGE
            ):
                caller_edge_gate = engine.evaluate_caller_edge(candidate, bound)
                caller_edge_validated = (
                    bool(caller_edge_gate is not None and caller_edge_gate[0]) and not refuting
                )
            blockers: list[str] = []
            if not bound:
                blockers.append("no bound supporting evidence")
            if refuting:
                blockers.append(f"{len(refuting)} refuting evidence item(s) bound")
            if code_value_claim and not code_value_answered:
                blockers.append("code value claim lacks a matching deterministic resolution")
            if code_value_answered or history_answered:
                blockers.clear()
            if (
                not causal_chain
                and not location_answered
                and not code_value_answered
                and not history_answered
                and not doc_answered
                and not state_answered
                and not caller_edge_validated
                and not code_value_claim
            ):
                blockers.append("no validated causal chain")
            # Finding L: blockers describe checkable-claim failure modes;
            # an opinion-only candidate bypasses them to the general path,
            # whose evaluate-level projection returns the NC-specific
            # INCONCLUSIVE (same verdict as without the early skip).
            if blockers and not _nc_early:
                from investigation_agent_platform.application.investigation.verification import (
                    InvestigationConfidence,
                    VerificationResult,
                    VerificationStatus,
                )

                result = VerificationResult(
                    hypothesis_id=candidate.id,
                    status=VerificationStatus.INCONCLUSIVE,
                    corroborating_evidence_ids=[e.evidence_id for e in bound],
                    explanation="; ".join(blockers),
                    confidence=InvestigationConfidence(
                        coverage_score=0.0,
                        reliability_score=0.0,
                        causal_score=0.0,
                        contradiction_penalty=0.0,
                    ),
                )
                producing_path = "blocked"
            elif caller_edge_gate is not None and not caller_edge_validated:
                if caller_edge_gate is not None:
                    result = VerificationResult(
                        hypothesis_id=candidate.id,
                        status=VerificationStatus.INCONCLUSIVE,
                        corroborating_evidence_ids=[e.evidence_id for e in bound],
                        explanation=caller_edge_gate[1],
                        confidence=InvestigationConfidence(
                            coverage_score=0.3,
                            reliability_score=0.5,
                            causal_score=0.0,
                            contradiction_penalty=0.0,
                        ),
                    )
                    producing_path = "caller-edge-incomplete"
            elif caller_edge_validated:
                if location_result is not None:
                    result = location_result
                    producing_path = "caller-edge"
                    causal_chain = []
                else:
                    from investigation_agent_platform.application.investigation.verification import (
                        InvestigationConfidence,
                        VerificationResult,
                        VerificationStatus,
                    )

                    result = VerificationResult(
                        hypothesis_id=candidate.id,
                        status=VerificationStatus.INCONCLUSIVE,
                        corroborating_evidence_ids=[e.evidence_id for e in bound],
                        explanation="caller edge gate passed but location result resolution returned None",
                        confidence=InvestigationConfidence(
                            coverage_score=0.3,
                            reliability_score=0.5,
                            causal_score=0.0,
                            contradiction_penalty=0.0,
                        ),
                    )
                    producing_path = "caller-edge-incomplete"
            elif code_value_answered:
                result = code_value_result
                producing_path = "code-value"
                causal_chain = []
            elif history_answered:
                result = history_result
                producing_path = "history"
                causal_chain = []
            elif _nc_early or (
                not code_value_claim
                and not location_answered
                and not history_answered
                and not doc_answered
                and not state_answered
                and not caller_edge_validated
            ):
                _candidate_id = getattr(candidate, "id", None)
                producing_path = "general"
                # G3: timeline-backed recorded attempts (diagnostics-only,
                # fail-open). Derived from the workflow-carried history;
                # None (legacy callers, failed derivation) keeps the exact
                # legacy diagnostics shape. Dict-shaped history entries
                # yield no tuples until Step 5 carries persisted markers.
                _verify_recorded_attempts = None
                try:
                    from investigation_agent_platform.application.evidence.retrieval_ledger import (
                        recorded_retrieval_attempts as _recorded_retrieval_attempts,
                    )

                    _verify_recorded_attempts = _recorded_retrieval_attempts(
                        data.get("recent_actions", []) or []
                    )
                except Exception:
                    _verify_recorded_attempts = None
                # Empty/None keeps the exact legacy call shape (no
                # kwarg at all); only a non-empty marker set threads
                # through. Downstream [] and None already degrade
                # identically to unmapped, so this changes no verdict.
                _evaluate_kwargs: dict[str, Any] = {}
                if _verify_recorded_attempts:
                    _evaluate_kwargs["recorded_attempts"] = _verify_recorded_attempts
                # Phase 1 (claim binding): the timeline-backed linkage
                # derived up front carries the attempt join the general
                # branch needs; marker-only history stays as-is.
                if (
                    claim_binding_version in (CLAIM_BINDING_CBV2, CLAIM_BINDING_CBV3)
                    and _binding_recorded
                    and "recorded_attempts" not in _evaluate_kwargs
                ):
                    _evaluate_kwargs["recorded_attempts"] = _binding_recorded
                # Phase 3 (claim binding): the full bank feeds the repair
                # donor pool (the working set may exclude uncited donors).
                # cbv2/cbv3 runs only — legacy keeps the exact call shape.
                if claim_binding_version in (CLAIM_BINDING_CBV2, CLAIM_BINDING_CBV3):
                    try:
                        _evaluate_kwargs["evidence_bank"] = list(evidence_by_id.values())
                    except Exception:
                        pass
                result = engine.evaluate_hypothesis(
                    candidate,
                    bound,
                    contradictions,
                    causal_chain,
                    policy,
                    sibling_hypotheses=[
                        h for h in (hypotheses or []) if getattr(h, "id", None) != _candidate_id
                    ],
                    observer=_definition_observe,
                    **_evaluate_kwargs,
                )
            # Phase 1 (claim binding): the taken verdict — fast-path or
            # general — passes through authoritative enforcement. Fast
            # paths bypass the engine's internal enforcement, so this is
            # the single choke point; it is idempotent on general-branch
            # results. Under cbv1 _binding_enforcement is None: no-op.
            # Phase 3 (claim binding, D6 site 2): applied fast-path
            # rebinds extend the enforcement before the choke so donor
            # corroborating survives the kept filter.
            if (
                claim_binding_version in (CLAIM_BINDING_CBV2, CLAIM_BINDING_CBV3)
                and _binding_enforcement is not None
                and result is not None
            ):
                try:
                    _applied_donors = [
                        str(getattr(r, "donor_id", "") or "")
                        for r in _verify_repairs
                        if str(getattr(r, "outcome", "") or "") == "applied"
                        and str(getattr(r, "donor_id", "") or "")
                    ]
                    if _applied_donors:
                        _binding_enforcement = VerificationEngine.enforcement_with_repairs(
                            _binding_enforcement, _applied_donors
                        )
                except Exception:
                    pass
                try:
                    result = engine.enforce_claim_binding(candidate, result, _binding_enforcement)
                except Exception:
                    # Fail closed (NEW-1): an enforcement fault must never
                    # leave the pre-enforcement (possibly VERIFIED) verdict
                    # standing. The callee guards everything it touches, so
                    # this is unreachable in practice; it exists so a future
                    # regression cannot silently reopen the fast path.
                    try:
                        result = engine._fault_inconclusive(candidate)
                    except Exception:
                        pass
                # Attach fast-path repairs (shadow or applied) to the
                # taken result's diagnostics; engine-branch repairs are
                # already attached (attach dedupes by value).
                try:
                    result = VerificationEngine.attach_repairs(result, list(_verify_repairs or []))
                except Exception:
                    pass
            # Review H3/H4: the taken verdict must hold for EVERY checkable
            # claim group, not just the first assessment's. Runs for every
            # claim-binding version (it is a soundness rule, not a binding
            # feature) and fails closed: a fault must never leave an
            # unchecked VERIFIED standing on a multi-claim hypothesis.
            from investigation_agent_platform.application.investigation.verification import (
                VerificationStatus as _GroupVerifyStatus,
            )

            if (
                result is not None
                and getattr(result, "status", None) == _GroupVerifyStatus.VERIFIED
            ):
                try:
                    result = engine.enforce_group_conjunction(
                        candidate, result, bound, contradictions, causal_chain, policy
                    )
                except Exception:
                    try:
                        result = engine._fault_inconclusive(candidate)
                    except Exception:
                        pass
            # Review H8 (lv3): a cited group that fails its own gate gets a
            # re-fetch route. Computed only for a taken verdict that is not
            # VERIFIED, and only for runs pinned lv3+ (in-flight lv2 runs keep
            # their semantics). A fault yields no extra gaps (fail closed to
            # the existing behavior).
            _failed_cited_groups: set[str] | None = None
            try:
                from investigation_agent_platform.application.investigation.verification import (
                    VerificationStatus as _RefetchStatus,
                )
                from investigation_agent_platform.domain.investigation.loop_version import (
                    LOOP_VERSION_LV3,
                    loop_version_at_least,
                    lv_h_enabled,
                )

                if (
                    result is not None
                    and getattr(result, "status", None) != _RefetchStatus.VERIFIED
                    and loop_version_at_least(getattr(_inv, "metadata", None), LOOP_VERSION_LV3)
                ):
                    _failed_cited_groups = set(
                        engine.failed_cited_group_ids(
                            candidate, result, bound, contradictions, causal_chain, policy,
                            include_no_target=lv_h_enabled(getattr(_inv, "metadata", None)),
                        )
                    )
            except Exception:
                _failed_cited_groups = None
            # Phase 4 (claim binding): smallest-fetch gap feed. Built from
            # the candidate plus the post-repair enforcement's cited_groups
            # — the only scope where both coexist. Faulted enforcement or
            # a builder error yields no feed (fail closed); an error
            # envelope is never mistaken for an empty feed.
            _claim_gaps: list[Any] | None = None
            _not_checkable_groups: list[str] = []
            try:
                if _binding_enforcement is not None and not bool(
                    getattr(_binding_enforcement, "fault", False)
                ):
                    from investigation_agent_platform.application.investigation.verification import (
                        build_claim_gaps as _build_claim_gaps,
                    )

                    _feed = _build_claim_gaps(
                        candidate,
                        set(_binding_enforcement.cited_groups or []),
                        _failed_cited_groups,
                        include_no_target=lv_h_enabled(getattr(_inv, "metadata", None)),
                    )
                    if isinstance(_feed, dict) and not _feed.get("error"):
                        _raw_gaps = _feed.get("gaps")
                        _claim_gaps = (
                            [g for g in _raw_gaps if isinstance(g, dict)]
                            if isinstance(_raw_gaps, list)
                            else []
                        )
                        _raw_nc = _feed.get("not_checkable_groups")
                        _not_checkable_groups = (
                            sorted({str(g) for g in _raw_nc if str(g)})
                            if isinstance(_raw_nc, list)
                            else []
                        )
                    else:
                        story_log(
                            logger,
                            logging.WARNING,
                            "claim gap feed build failed",
                            comp=COMP_ACTVTY,
                            func="verify_root_cause_activity",
                            stage="verify",
                            step="build_claim_gaps",
                            outcome=OUTCOME_SKIPPED,
                            tenant_id=tenant_id,
                            investigation_id=inv_id_str,
                            event="claim gap feed build failed",
                        )
            except Exception:
                _claim_gaps = None
                _not_checkable_groups = []
            # Phase 4.2: verification decision + location-exemption counters.
            # Phase 1 (claim binding): per-claim dispositions, version
            # stamp, and HISTORY subject outcome ride the same call.
            _emit_verify_telemetry(
                ctx,
                tenant_id,
                investigation_id,
                candidate,
                result,
                [
                    h
                    for h in (hypotheses or [])
                    if getattr(h, "id", None) != getattr(candidate, "id", None)
                ],
                binding_version=claim_binding_version,
                binding_source=_binding_source,
                recorded=_binding_recorded,
                evidence=list(evidence_by_id.values()),
            )
            verified = _verification_status_value(result).upper() == "VERIFIED"
            # Debug story: per-candidate gate verdict. Shows which proof
            # path answered (or which blockers abstained) for each ranked
            # hypothesis — the verify side of the gate story.
            _story_debug(
                tenant_id=tenant_id,
                investigation_id=inv_id_str,
                event="story.verify_gate",
                hypothesis_id=str(getattr(candidate, "id", "")),
                statement=str(getattr(candidate, "statement", ""))[:500],
                claim_kind=str(
                    getattr(
                        getattr(candidate, "claim_kind", None),
                        "value",
                        getattr(candidate, "claim_kind", "?"),
                    )
                    or "?"
                ),
                status=str(getattr(result.status, "value", result.status)),
                producing_path=str(producing_path) if "producing_path" in dir() else "?",
                explanation=str(getattr(result, "explanation", ""))[:500],
                bound_count=len(bound or []),
                caller_edge_validated=bool(caller_edge_validated)
                if "caller_edge_validated" in dir()
                else False,
            )
            # Persist the verdict back onto the candidate (scores, bindings,
            # lifecycle) so the next ranking sorts on maintained fields.
            # Remediation plan ARCH-HIGH-008: a failed persistence write is a
            # typed denial, never silent. The evaluated verdict must not
            # return success-shaped when the row was not actually updated —
            # the loop keys off data["verified"], which stays False here, so
            # an unpersisted VERIFIED can never drive the run to COMPLETED.
            try:
                from investigation_agent_platform.application.investigation.verification import (
                    apply_verification_outcome,
                )

                updated = apply_verification_outcome(candidate, result)
            except Exception:
                story_log(
                    logger,
                    logging.ERROR,
                    "verify verdict materialization failed",
                    comp=COMP_ACTVTY,
                    func="verify_root_cause_activity",
                    stage="verify",
                    step="materialize_verdict",
                    outcome=OUTCOME_FAILED,
                    tenant_id=tenant_id,
                    investigation_id=inv_id_str,
                    event="verify verdict materialization failed",
                    exc_info=True,
                    hypothesis_id=str(candidate.id),
                )
                return GenericActivityResult(
                    success=False,
                    error="verify verdict materialization failed",
                    data={
                        "verified": False,
                        "status": _verification_status_value(result),
                        "hypothesis_id": str(result.hypothesis_id),
                        "reason": "outcome_materialization_failed",
                        "persisted": False,
                        "verifier_version": str(verifier_version),
                        "verifier_version_source": _version_source,
                    },
                )
            if updated != candidate and hypothesis_repo is not None:
                try:
                    await hypothesis_repo.save(
                        tenant_id, updated, investigation_id, candidate.version
                    )
                except Exception as exc:
                    story_log(
                        logger,
                        logging.ERROR,
                        "verify verdict persistence failed",
                        comp=COMP_ACTVTY,
                        func="verify_root_cause_activity",
                        stage="verify",
                        step="persist_verdict",
                        outcome=OUTCOME_FAILED,
                        tenant_id=tenant_id,
                        investigation_id=inv_id_str,
                        event="verify verdict persistence failed",
                        error=str(exc),
                        error_type=type(exc).__name__,
                        exc_info=True,
                        hypothesis_id=str(candidate.id),
                    )
                    return GenericActivityResult(
                        success=False,
                        error=f"verify verdict persistence conflict ({type(exc).__name__})",
                        data={
                            "verified": False,
                            "status": _verification_status_value(result),
                            "confidence": result.confidence.overall_confidence,
                            "hypothesis_id": str(result.hypothesis_id),
                            "reason": "persistence_conflict",
                            "persisted": False,
                            "verifier_version": str(verifier_version),
                            "verifier_version_source": _version_source,
                        },
                    )
        story_log(
            logger,
            logging.INFO,
            "verify_root_cause_activity completed",
            comp=COMP_ACTVTY,
            func="verify_root_cause_activity",
            stage="verify",
            step="complete",
            outcome=OUTCOME_COMPLETED,
            tenant_id=tenant_id,
            investigation_id=inv_id_str,
            event="verify_root_cause_activity completed",
            outputs={
                "outcome": "verified" if verified else "unverified",
                "verification_status": _verification_status_value(result),
            },
            elapsed_ms=elapsed_ms(t0),
            verification_status=_verification_status_value(result),
        )
        # G3: bounded re-retrieval directive producer (D3 lifecycle
        # step 1). Current-version runs only — legacy-pinned rows keep
        # their exact v1 result shape. Attempt history prefers the
        # timeline read (durable, worker-independent) and falls back to
        # the workflow-carried summaries. Fail-open: None on any fault,
        # on unavailable history, or when no unsatisfied claim maps to
        # a dispatchable recent action.
        _ledger_requery: dict[str, Any] | None = None
        try:
            if verifier_version != VERIFIER_VERSION_LEGACY:
                from investigation_agent_platform.application.evidence.retrieval_ledger import (
                    merge_recorded_attempts as _merge_recorded_attempts,
                )
                from investigation_agent_platform.application.evidence.retrieval_ledger import (
                    recorded_attempt_summaries as _recorded_attempt_summaries,
                )
                from investigation_agent_platform.application.evidence.retrieval_ledger import (
                    recorded_retrieval_attempts as _recorded_timeline_attempts,
                )
                from investigation_agent_platform.application.investigation.verification import (
                    select_ledger_requery as _select_ledger_requery,
                )

                # Fail-closed default: None (history unavailable) authorizes
                # nothing. A zero count must mean confirmed-empty, never a
                # failed read or an unconvertible summary list.
                _directive_recorded = None
                try:
                    _recent_for_directive = data.get("recent_actions", []) or []
                    _summary_pairs = _recorded_attempt_summaries(_recent_for_directive)
                    _timeline_repo = getattr(ctx, "timeline_repo", None)
                    _timeline_events: list | None = None
                    if _timeline_repo is not None:
                        try:
                            _timeline_events = await _timeline_repo.find_by_investigation_id(
                                tenant_id, investigation_id
                            )
                        except Exception:
                            _timeline_events = None
                    if _timeline_events is None:
                        # Timeline unavailable: summaries alone, or None
                        # when nothing convertible (fail-closed downstream).
                        _directive_recorded = _summary_pairs
                    else:
                        _directive_recorded = _merge_recorded_attempts(
                            _recorded_timeline_attempts(_timeline_events or []),
                            _summary_pairs or [],
                        )
                except Exception:
                    _directive_recorded = None
                _ledger_requery = _select_ledger_requery(
                    candidate,
                    evidence_items,
                    _directive_recorded,
                    data.get("recent_actions", []) or [],
                )
        except Exception:
            _ledger_requery = None
        # G3: VERIFY_EVALUATION timeline marker (diagnostics-only,
        # fail-open — marker faults never touch the result below). One row
        # per verify turn carrying the recomputed claim bindings, their
        # ledgers, and missing obligations, so the benefit battery measures
        # changed bindings and missing-obligation events from persisted
        # state instead of recomputing the join. The diagnostics are
        # recomputed here (not read off result) so every producing path —
        # general, fast-path validators, and blocked — emits the same
        # timeline-backed join the directive producer used above; paths
        # that never ran the evaluator still get a measured snapshot, and
        # an empty evaluation still persists a skeleton row (no marker is
        # ever fabricated, but no current-version turn goes unrecorded).
        # Current-version runs only; legacy-pinned rows emit nothing.
        # result itself is never mutated. A skipped write is logged — a
        # missing marker must be a reported persistence failure, never
        # silent.
        try:
            if verifier_version != VERIFIER_VERSION_LEGACY:
                from investigation_agent_platform.application.investigation.verification import (
                    build_verify_evaluation_attributes as _build_eval_attrs,
                )
                from investigation_agent_platform.application.investigation.verification import (
                    collect_verification_diagnostics as _collect_eval_diag,
                )
                from investigation_agent_platform.domain.timeline.models import (
                    TimelineEvent,
                    TimelineEventType,
                )

                _eval_diag = None
                try:
                    _eval_diag = _collect_eval_diag(
                        candidate,
                        evidence_items,
                        result,
                        _directive_recorded,
                    )
                except Exception:
                    _eval_diag = None
                if _eval_diag is None:
                    _eval_diag = getattr(result, "diagnostics", None)
                _eval_attrs = _build_eval_attrs(
                    _eval_diag,
                    turn=data.get("iteration"),
                    hypothesis_id=getattr(result, "hypothesis_id", ""),
                    verifier_version=str(verifier_version),
                )
                # Phase 1 (claim binding): additive marker keys for the
                # battery series (bounded scalars only; marker stays a
                # small JSON object). Fail-open: faults skip the keys,
                # never the marker write.
                if isinstance(_eval_attrs, dict):
                    try:
                        _eval_attrs["verification_status"] = str(
                            getattr(
                                getattr(result, "status", ""),
                                "value",
                                getattr(result, "status", ""),
                            )
                            or ""
                        )
                        _eval_attrs["verification_tier"] = str(getattr(result, "tier", "") or "")
                        _eval_attrs["claim_binding_version"] = str(claim_binding_version)
                    except Exception:
                        pass
                    # Phase 4 (claim binding): NOT_CHECKABLE denominator
                    # input (D5/D7). Exact count always; identities capped
                    # at 10 with a truncation flag mirroring the repaired
                    # precedent. Fail-open: faults skip the keys, never
                    # the marker write.
                    try:
                        _nc_groups = list(_not_checkable_groups or [])
                        _eval_attrs["not_checkable_count"] = len(_nc_groups)
                        _eval_attrs["not_checkable_groups"] = [str(g)[:96] for g in _nc_groups[:10]]
                        _eval_attrs["not_checkable_truncated"] = bool(len(_nc_groups) > 10)
                    except Exception:
                        pass
                    try:
                        from investigation_agent_platform.application.investigation.verification import (
                            VerificationEngine as _MarkerEngine,
                        )

                        _m_kind = _MarkerEngine.effective_claim_kind(candidate)
                        _m_kind_value = (
                            str(getattr(_m_kind, "value", _m_kind) or "")
                            if _m_kind is not None
                            else ""
                        )
                        if _m_kind_value == "HISTORY":
                            _m_target = getattr(candidate, "claim_target", None)
                            _m_claimed = (
                                str(getattr(_m_target, "commit_subject", "") or "")
                                if _m_target is not None
                                else ""
                            )
                            _m_best = "mismatched_subject"
                            for _m_corro in (
                                getattr(result, "corroborating_evidence_ids", None) or []
                            ):
                                try:
                                    _m_row = evidence_by_id.get(_m_corro)
                                except Exception:
                                    continue
                                if _m_row is None:
                                    try:
                                        _m_row = evidence_by_id.get(str(_m_corro))
                                    except Exception:
                                        continue
                                if _m_row is None:
                                    continue
                                _m_summary = str(getattr(_m_row, "summary", "") or "")
                                _m_o = _MarkerEngine.subject_match_outcome(_m_claimed, _m_summary)
                                if _m_o == "matched_exact":
                                    _m_best = _m_o
                                    break
                                if _m_o == "matched_token_set":
                                    _m_best = _m_o
                            _eval_attrs["history_subject_outcome"] = _m_best
                    except Exception:
                        pass
                _eval_event_type = getattr(TimelineEventType, "VERIFY_EVALUATION", None)
                _eval_repo = getattr(ctx, "timeline_repo", None)
                if _eval_attrs is None:
                    story_log(
                        logger,
                        logging.WARNING,
                        "verify evaluation marker skipped: attributes build failed",
                        comp=COMP_ACTVTY,
                        func="verify_root_cause_activity",
                        stage="verify",
                        step="mark_evaluation",
                        outcome=OUTCOME_SKIPPED,
                        tenant_id=tenant_id,
                        investigation_id=inv_id_str,
                        event="verify evaluation marker skipped",
                    )
                elif _eval_event_type is None or _eval_repo is None:
                    story_log(
                        logger,
                        logging.WARNING,
                        "verify evaluation marker unwritable: repository unavailable",
                        comp=COMP_ACTVTY,
                        func="verify_root_cause_activity",
                        stage="verify",
                        step="mark_evaluation",
                        outcome=OUTCOME_SKIPPED,
                        tenant_id=tenant_id,
                        investigation_id=inv_id_str,
                        event="verify evaluation marker unwritable",
                    )
                else:
                    # No sequence-number read: TimelineEventORM persists
                    # no sequence column (ordering is by timestamp), so a
                    # full-history read here would buy nothing.
                    try:
                        await _eval_repo.append(
                            tenant_id,
                            TimelineEvent(
                                tenant_id=tenant_id,
                                investigation_id=investigation_id,
                                timestamp=datetime.now(UTC),
                                event_type=_eval_event_type,
                                description=(
                                    f"verify evaluation turn "
                                    f"{_eval_attrs.get('turn')}: "
                                    f"{len(_eval_attrs.get('bindings') or [])} bindings, "
                                    f"{len(_eval_attrs.get('missing_obligations') or [])} missing"
                                )[:2048],
                                attributes=_eval_attrs,
                            ),
                            investigation_id,
                        )
                    except Exception as exc:
                        story_log(
                            logger,
                            logging.WARNING,
                            "verify evaluation marker write failed",
                            comp=COMP_ACTVTY,
                            func="verify_root_cause_activity",
                            stage="verify",
                            step="mark_evaluation",
                            outcome=OUTCOME_SKIPPED,
                            tenant_id=tenant_id,
                            investigation_id=inv_id_str,
                            event="verify evaluation marker write failed",
                            error=str(exc),
                            error_type=type(exc).__name__,
                        )
        except Exception as exc:
            # Fail-open by design (diagnostics must never break the loop),
            # but never fail-silent: an unexpected marker-block fault is
            # observable at the source, not just as a downstream coverage gap.
            story_log(
                logger,
                logging.WARNING,
                "verify evaluation marker block failed",
                comp=COMP_ACTVTY,
                func="verify_root_cause_activity",
                stage="verify",
                step="mark_evaluation",
                outcome=OUTCOME_SKIPPED,
                tenant_id=tenant_id,
                investigation_id=inv_id_str,
                event="verify evaluation marker block failed",
                error=str(exc),
                error_type=type(exc).__name__,
            )
        _verify_result_data: dict[str, Any] = {
            "verified": verified,
            "status": _verification_status_value(result),
            "confidence": result.confidence.overall_confidence,
            "hypothesis_id": str(result.hypothesis_id),
            "answer_mode": bool(getattr(result, "location_validated", False)),
            "code_value_validated": bool(getattr(result, "code_value_validated", False)),
            "producing_path": producing_path,
            "shadow_activity_identity": dict(_shadow_identity),
            "verifier_version": str(verifier_version),
            "verifier_version_source": _version_source,
        }
        # The directive key exists only on current-version runs, so
        # legacy-pinned rows keep their exact v1 result shape (no new
        # key, not even a None one). Absent directive on a current run
        # reads as an explicit None.
        if verifier_version != VERIFIER_VERSION_LEGACY:
            _verify_result_data["ledger_requery"] = _ledger_requery
        # Phase 4 (claim binding): the gap feed key exists on every
        # current-version run that produced one; absent (not None) when
        # the feed errored or enforcement faulted, so the carrier can
        # distinguish "no gaps" ([]) from "no feed".
        if verifier_version != VERIFIER_VERSION_LEGACY and _claim_gaps is not None:
            _verify_result_data["claim_gaps"] = list(_claim_gaps)
        # Phase 1 (claim binding): bounded per-turn counts ride the result
        # data so the workflow accumulates them into loop_telemetry.
        # cbv2/cbv3 runs only — legacy rows keep their exact result shape.
        # Phase 3 adds applied/shadow repair counts from the taken
        # result's diagnostics.
        if claim_binding_version in (CLAIM_BINDING_CBV2, CLAIM_BINDING_CBV3):
            try:
                _b_cited_n = len(
                    list(_binding_enforcement.cited_ids or [])
                    if _binding_enforcement is not None
                    else []
                )
                _b_kept_n = int(
                    _binding_enforcement.cited_kept_count if _binding_enforcement is not None else 0
                )
                _b_evap_n = len(
                    list(_binding_enforcement.evaporated_ids or [])
                    if _binding_enforcement is not None
                    else []
                )
                _verify_result_data["claim_binding_kept"] = _b_kept_n
                _verify_result_data["claim_binding_evaporated"] = _b_evap_n
                _verify_result_data["claim_binding_unmapped"] = (
                    1
                    if (
                        _binding_enforcement is not None
                        and _b_cited_n
                        and bool(_binding_enforcement.unmapped)
                    )
                    else 0
                )
                _verify_result_data["claim_binding_version"] = str(claim_binding_version)
                try:
                    _r_diag = getattr(result, "diagnostics", None)
                    _r_reps = list(getattr(_r_diag, "repaired", None) or [])
                    _verify_result_data["claim_binding_repaired"] = sum(
                        1 for _r in _r_reps if str(getattr(_r, "outcome", "") or "") == "applied"
                    )
                    _verify_result_data["claim_binding_repair_shadow"] = sum(
                        1 for _r in _r_reps if str(getattr(_r, "outcome", "") or "") != "applied"
                    )
                except Exception:
                    pass
            except Exception:
                pass
        return GenericActivityResult(
            success=True,
            data=attach_shadow_observations(
                _verify_result_data,
                definition_shadow_observations,
            ),
        )
    except ApplicationFailure:
        raise
    except Exception as exc:
        story_log(
            logger,
            logging.ERROR,
            "verify_root_cause_activity failed",
            comp=COMP_ACTVTY,
            func="verify_root_cause_activity",
            stage="verify",
            step="verify",
            outcome=OUTCOME_FAILED,
            tenant_id=tenant_id_probe,
            investigation_id=inv_id_probe,
            event="verify_root_cause_activity failed",
            error=str(exc),
            error_type=type(exc).__name__,
            exc_info=True,
        )
        raise _application_failure_from_exc(exc) from exc
