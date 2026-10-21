"""Offline integration: the composed verify-to-conclude path, end to end.

HERMITIC — no live gate, no stack, no network/DB/Temporal/LLM. "Integration"
here means scope, not liveness: each test drives the real production
functions in the order `verify_root_cause_activity` / `conclude_investigation_activity`
apply them, on real domain objects shaped like the live batteries:

    resolve supporting IDs within-investigation
      -> statement_range_reads augmentation (incl. bare-stem fallback)
      -> check_claim_kind -> evaluate_hypothesis
      -> ConclusionGate.evaluate -> next_check_for_abstention

Unit goldens (`tests/unit/test_rev_battery_goldens.py`) pin each stage in
isolation; this suite pins the composed outcome, so a regression in the
wiring between stages — the class of defect behind REV_BATTERY L2#1/L2#2 —
fails loudly. Live retrieval, LLM formulation, and Temporal orchestration
stay with `scripts/live_battery.py` and `test_live_platform.py`.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from investigation_agent_platform.application.investigation.verification import (
    ConclusionGate,
    RootCauseVerificationPolicy,
    VerificationEngine,
    VerificationStatus,
)
from investigation_agent_platform.domain.evidence.models import Evidence, EvidenceType
from investigation_agent_platform.domain.hypothesis.models import (
    AssessmentStrength,
    AssessmentType,
    ClaimKind,
    ClaimTarget,
    Hypothesis,
    HypothesisEvidenceAssessment,
    HypothesisStatus,
)
from investigation_agent_platform.domain.provenance.models import (
    EvidenceFreshness,
    EvidenceProvenance,
    QueryFingerprint,
    SourceLocation,
)

ENGINE = VerificationEngine()
POLICY = RootCauseVerificationPolicy()
GATE = ConclusionGate()
REV = "54d73240d6b5b03b899bcaf0e0c4e1cdc31be9fd"
PROXY = "service/src/main/java/com/sample/orders/proxy/OrderServiceRestProxy.java"
HELPER = (
    "samplecorp/orders-internal/service/src/main/java/com/sample/orders/helper/"
    "OrderQualifyHelper.java"
)
MAPPER = "service/src/main/java/com/sample/orders/legacy/LegacyCodeMapper.java"


def _ev(
    snippet: str = "",
    file_path: str | None = None,
    line_start: int | None = None,
    line_end: int | None = None,
    revision: str | None = REV,
    operation: str = "GET_SOURCE",
    etype: EvidenceType = EvidenceType.SOURCE_CODE,
    attrs: dict | None = None,
) -> Evidence:
    iid = uuid4()
    now = datetime.now(UTC)
    return Evidence(
        tenant_id="n-batt",
        investigation_id=iid,
        evidence_type=etype,
        provider="pygit-local",
        source="urn:test",
        title="t",
        summary="s",
        content_snippet=snippet,
        fingerprint=f"fp-{uuid4()}",
        observed_at=now,
        retrieved_at=now,
        attributes=dict(attrs or {}),
        provenance=EvidenceProvenance(
            tenant_id="n-batt",
            investigation_id=iid,
            provider_type="GIT",
            requested_provider_id="pygit-local",
            actual_provider_id="pygit-local",
            source_system="Git",
            retrieval_timestamp=now,
            query_fingerprint=QueryFingerprint(
                provider_type="GIT",
                operation=operation,
                normalized_query_hash="path",
            ),
            source_location=SourceLocation(
                system="Git",
                identifier=f"samplecorp/orders-internal/{file_path}"
                if file_path
                else "x",
                file_path=file_path,
                line_start=line_start,
                line_end=line_end,
                revision=revision,
            ),
        ),
        freshness=EvidenceFreshness(observed_at=now, retrieved_at=now),
    )


def _hyp(statement: str, assessments: list, supporting: list, iid: UUID) -> Hypothesis:
    return Hypothesis(
        tenant_id="n-batt",
        investigation_id=iid,
        id=uuid4(),
        title=statement[:256],
        description=statement,
        statement=statement,
        status=HypothesisStatus.UNDER_INVESTIGATION,
        supporting_evidence_ids=list(supporting),
        assessments=list(assessments),
    )


def _asm(
    hyp_id: UUID,
    ev_id: UUID,
    kind: ClaimKind,
    target: ClaimTarget,
    claimed: str | None = None,
) -> HypothesisEvidenceAssessment:
    return HypothesisEvidenceAssessment(
        hypothesis_id=hyp_id,
        evidence_id=ev_id,
        assessment=AssessmentType.SUPPORTS,
        strength=AssessmentStrength.MODERATE,
        reason=f"cited by {kind.value} claim",
        claim_kind=kind,
        claim_target=target,
        claimed_value=claimed,
    )


def run_path(statement: str, hyp: Hypothesis, banked: list[Evidence]) -> dict[str, Any]:
    """Mirror the activities' bound-augment-verify-gate-next_check sequence."""
    evidence_by_id = {e.evidence_id: e for e in banked}
    bound = [
        evidence_by_id[k]
        for k in (hyp.supporting_evidence_ids or [])
        if k in evidence_by_id
    ]
    bound_ids = {e.evidence_id for e in bound}
    for extra in VerificationEngine.statement_range_reads(statement, banked):
        if extra.evidence_id not in bound_ids:
            bound_ids.add(extra.evidence_id)
            bound.append(extra)
    kind_gate = ENGINE.check_claim_kind(hyp, bound)
    verification = ENGINE.evaluate_hypothesis(hyp, bound, [], [], POLICY)
    # Mirrors conclude_investigation_activity: a validated code-value answer
    # carries the verdict and exempts the gate's causal-chain requirement.
    code_value_result = ENGINE.validate_code_value_claim(hyp, bound, [])
    code_value_answered = code_value_result is not None
    if code_value_answered:
        verification = code_value_result

    async def _gate():
        return await GATE.evaluate(
            "n-batt",
            hyp.investigation_id,
            verification,
            [],
            [],
            bound,
            POLICY,
            code_value_validated=code_value_answered,
        )

    decision = asyncio.run(_gate())
    # Mirrors conclude_investigation_activity: published conclusions must map
    # to citations (unmapped sentences demote to likely), then gate-denial
    # abstention leads with the verification explanation.
    unmapped = VerificationEngine.map_conclusion_sentences(hyp.statement, bound, [])
    verification = VerificationEngine.apply_conclusion_mapping(verification, unmapped)
    denial_reasons = (
        [verification.explanation] if str(verification.explanation or "").strip() else []
    ) + list(decision.blockers)
    next_check = VerificationEngine.next_check_for_abstention(
        "insufficient", denial_reasons
    )
    return {
        "bound": bound,
        "kind_gate": kind_gate,
        "verification": verification,
        "decision": decision,
        "next_check": next_check,
        "unmapped": unmapped,
    }


def test_l2_enumeration_fixed_end_to_end():
    """030700 L2#1: correct range bound, Class.method counterpart verifies kind."""
    snippet = (
        "1| package com.sample.orders.proxy;\n"
        "10|     public String submit(String orderId) {\n"
        "11|         return handler.handle(orderId);"
    )
    row = _ev(snippet=snippet, file_path=PROXY, line_start=1, line_end=13)
    hyp_id, iid = uuid4(), uuid4()
    target = ClaimTarget(
        symbol="OrderServiceHandler.handle",
        counterpart="OrderServiceRestProxy.submit",
        file_path=PROXY,
        line_start=10,
        line_end=11,
        scope="OrderServiceRestProxy.submit",
        declaring_type="OrderServiceRestProxy",
        commit_id=REV,
    )
    hyp = _hyp(
        "OrderServiceRestProxy calls OrderServiceHandler through its submit method.",
        [_asm(hyp_id, row.evidence_id, ClaimKind.CALL_EDGE, target)],
        [row.evidence_id],
        iid,
    )
    hyp = hyp.model_copy(update={"id": hyp_id})
    out = run_path(hyp.statement, hyp, [row])
    assert out["kind_gate"] == (True, "")
    assert out["verification"].status == VerificationStatus.INCONCLUSIVE
    assert out["verification"].confidence.overall_confidence < 0.80
    assert out["decision"].approved is False
    assert any("no causal chain established" in b for b in out["decision"].blockers)
    assert out["next_check"] is not None and "causal chain" in out["next_check"]


def test_l2_comment_row_needs_rebind_end_to_end():
    """030700 L2#2: comment row cited, call site banked elsewhere."""
    cited = _ev(
        snippet="5| /** REST proxy: remote facade over the order service handler. */",
        file_path=PROXY,
        line_start=5,
        line_end=5,
    )
    showing = _ev(
        snippet="10|     public String qualify(String orderId, boolean preChecked) {\n"
        "12|             return handler.handle(orderId);",
        file_path=HELPER,
        line_start=10,
        line_end=12,
    )
    hyp_id, iid = uuid4(), uuid4()
    target = ClaimTarget(
        symbol="OrderServiceHandler",
        counterpart="OrderServiceRestProxy",
        file_path=PROXY,
        line_start=5,
        line_end=5,
    )
    hyp = _hyp(
        "OrderServiceRestProxy calls the order service handler.",
        [_asm(hyp_id, cited.evidence_id, ClaimKind.CALL_EDGE, target)],
        [cited.evidence_id],
        iid,
    )
    hyp = hyp.model_copy(update={"id": hyp_id})
    out = run_path(hyp.statement, hyp, [cited, showing])
    assert out["kind_gate"][0] is False
    assert "no cited row shows a call" in out["kind_gate"][1]
    assert out["verification"].status == VerificationStatus.INCONCLUSIVE
    assert out["next_check"] is not None and "range-read" in out["next_check"]


def test_l2_sibling_repair_and_proxy_gap_end_to_end():
    """035908 L2: helper edge repaired via declaration+invocation join; proxy missing."""
    proxy_comment = _ev(
        snippet="5| /** REST proxy: remote facade over the order service handler. */",
        file_path=PROXY,
        line_start=5,
        line_end=5,
    )
    helper_search = _ev(
        snippet="import com.sample.orders.handler.OrderServiceHandler;",
        file_path=HELPER,
        line_start=3,
        line_end=3,
        operation="SEARCH_CODE",
    )
    helper_decl = _ev(
        snippet="8|     private final OrderServiceHandler handler = new OrderServiceHandler();",
        file_path=HELPER,
        line_start=1,
        line_end=8,
    )
    helper_call = _ev(
        snippet="10|     public String qualify(String orderId, boolean preChecked) {\n12|             return handler.handle(orderId);",
        file_path=HELPER,
        line_start=10,
        line_end=12,
    )
    hyp_id, iid = uuid4(), uuid4()
    proxy_target = ClaimTarget(
        symbol="OrderServiceHandler",
        counterpart="OrderServiceRestProxy",
        file_path=PROXY,
        line_start=5,
        line_end=5,
    )
    helper_target = ClaimTarget(
        symbol="OrderServiceHandler",
        counterpart="OrderQualifyHelper",
        file_path=HELPER,
        line_start=5,
        line_end=5,
    )
    statement = "OrderServiceRestProxy and OrderQualifyHelper both call the order service handler."
    hyp = _hyp(
        statement,
        [
            _asm(hyp_id, proxy_comment.evidence_id, ClaimKind.CALL_EDGE, proxy_target),
            _asm(hyp_id, helper_search.evidence_id, ClaimKind.CALL_EDGE, helper_target),
        ],
        [proxy_comment.evidence_id, helper_search.evidence_id],
        iid,
    )
    hyp = hyp.model_copy(update={"id": hyp_id})
    banked = [proxy_comment, helper_search, helper_decl, helper_call]
    out = run_path(statement, hyp, banked)
    # Stem fallback binds both helper rows; the helper edge verifies in
    # isolation via the declaration+invocation join ...
    bound_ids = {e.evidence_id for e in out["bound"]}
    assert helper_decl.evidence_id in bound_ids
    assert helper_call.evidence_id in bound_ids
    assert (
        VerificationEngine.call_site_proof(
            [helper_search, helper_decl, helper_call], helper_target, statement
        )[0]
        is True
    )
    # ... but no proxy showing row exists anywhere, so the run still abstains,
    # localized to the missing call site rather than a generic chain message.
    assert out["kind_gate"][0] is False
    assert "no cited row shows a call" in out["kind_gate"][1]
    assert out["next_check"] is not None and "range-read" in out["next_check"]


def test_l2_joint_construction_only_abstains_end_to_end():
    """115939 L2#2: joint 'both call' claim on constructor lines alone abstains.

    The product previously ANSWERED this shape at 0.9 via the location fast
    path (comment + `new X()` field lines). Constructor lines are not
    invocations: kind-gate fails, the location path inherits the failure,
    and the run abstains instead of publishing a false answer.
    """
    proxy_comment = _ev(
        snippet="5| /** REST proxy: remote facade over the order service handler. */",
        file_path=PROXY,
        line_start=5,
        line_end=5,
    )
    proxy_ctor = _ev(
        snippet="8|     private final OrderServiceHandler handler = new OrderServiceHandler();",
        file_path=PROXY,
        line_start=1,
        line_end=8,
    )
    helper_ctor = _ev(
        snippet="8|     private final OrderServiceHandler handler = new OrderServiceHandler();",
        file_path=HELPER,
        line_start=1,
        line_end=8,
    )
    hyp_id, iid = uuid4(), uuid4()
    statement = "OrderServiceRestProxy and OrderQualifyHelper both call the order service handler."
    hyp = _hyp(
        statement,
        [
            _asm(
                hyp_id,
                proxy_ctor.evidence_id,
                ClaimKind.CALL_EDGE,
                ClaimTarget(
                    symbol="OrderServiceHandler",
                    counterpart="OrderServiceRestProxy",
                    file_path=PROXY,
                    line_start=5,
                    line_end=5,
                ),
            ),
            _asm(
                hyp_id,
                helper_ctor.evidence_id,
                ClaimKind.CALL_EDGE,
                ClaimTarget(
                    symbol="OrderServiceHandler",
                    counterpart="OrderQualifyHelper",
                    file_path=HELPER,
                    line_start=5,
                    line_end=5,
                ),
            ),
        ],
        [proxy_ctor.evidence_id, helper_ctor.evidence_id],
        iid,
    )
    hyp = hyp.model_copy(update={"id": hyp_id})
    out = run_path(statement, hyp, [proxy_comment, proxy_ctor, helper_ctor])
    assert out["kind_gate"][0] is False
    assert "no cited row shows a call" in out["kind_gate"][1]
    assert out["verification"].status == VerificationStatus.INCONCLUSIVE
    assert out["decision"].approved is False


def test_l3_absence_policy_abstain_end_to_end():
    """L3: scoped empty-search proof passes the kind gate; scoring abstains."""
    scope = "spring-sample-service:history=OrderServiceImpl.java"
    row = _ev(
        snippet="No matches in scope",
        etype=EvidenceType.COMMIT_HISTORY,
        operation="get_code_history",
        attrs={"match_count": 0, "search_scope": scope},
    )
    hyp_id, iid = uuid4(), uuid4()
    hyp = _hyp(
        "No recent changes were found in OrderServiceImpl.java.",
        [
            _asm(
                hyp_id,
                row.evidence_id,
                ClaimKind.ABSENCE,
                ClaimTarget(scope=scope),
            )
        ],
        [row.evidence_id],
        iid,
    )
    hyp = hyp.model_copy(update={"id": hyp_id})
    out = run_path(hyp.statement, hyp, [row])
    assert out["kind_gate"] == (True, "")
    assert out["verification"].status == VerificationStatus.INCONCLUSIVE
    assert out["verification"].confidence.overall_confidence < 0.80
    assert out["decision"].approved is False


def test_l5_answered_via_method_return_end_to_end():
    """L5#1 (041339 shape): bank-time resolver fixes the 6-19 window, claim verifies.

    Mirrors the worker: the GET_CODE row carries the resolver's V2 payload
    (method_name + guard + declaring type, as the worker's method_name
    wiring passes them), then the composed path runs kind-gate (BEHAVIOR
    still wants runtime) → Java SOURCE_VALUE_FACT preempts and VERIFIES.
    """
    from investigation_agent_platform.application.analysis.java.method_return_facts import (
        resolve_java_method_return_fact,
    )
    from investigation_agent_platform.domain.code.models import (
        CODE_FACT_RESOLUTION_V2_KEY,
        JavaFactCategory,
    )

    snippet = (
        "6|     public String normalize(String orderType) {\n"
        "7|         if (orderType == null) {\n"
        '8|             return "STANDARD";\n'
        "9|         }\n"
        "10|         switch (orderType) {\n"
        '11|             case "LEGACY-BULK":\n'
        '12|                 return "BULK";\n'
        "13|             case \"LEGACY-RES\":\n"
        '14|                 return "RESIDENTIAL";\n'
        "15|             default:\n"
        "16|                 return orderType;\n"
        "17|         }\n"
        "18|     }\n"
        "19| }"
    )
    row = _ev(
        snippet=snippet,
        file_path=MAPPER,
        line_start=6,
        line_end=19,
    )
    resolution = resolve_java_method_return_fact(
        row,
        "normalize",
        guard_condition="orderType == null",
        declaring_type="LegacyCodeMapper",
    )
    assert resolution.status.value == "RESOLVED"
    row = row.model_copy(
        update={
            "attributes": {
                **row.attributes,
                CODE_FACT_RESOLUTION_V2_KEY: resolution.model_dump(mode="json"),
            }
        }
    )
    hyp_id, iid = uuid4(), uuid4()
    target = ClaimTarget(
        symbol="LegacyCodeMapper.normalize",
        file_path=MAPPER,
        line_start=6,
        line_end=19,
        scope="LegacyCodeMapper.normalize",
        declaring_type="LegacyCodeMapper",
        commit_id=REV,
        claimed_value="STANDARD",
        fact_category=JavaFactCategory.SOURCE_VALUE_FACT,
    )
    hyp = _hyp(
        "When orderType is null, the normalize method in LegacyCodeMapper returns 'STANDARD'.",
        [_asm(hyp_id, row.evidence_id, ClaimKind.BEHAVIOR, target, claimed="STANDARD")],
        [row.evidence_id],
        iid,
    )
    hyp = hyp.model_copy(update={"id": hyp_id})
    out = run_path(hyp.statement, hyp, [row])
    # BEHAVIOR kind-gate still wants runtime, but the Java SOURCE_VALUE_FACT
    # path preempts it and verifies from the resolved range read.
    assert out["kind_gate"][0] is False
    assert out["verification"].status == VerificationStatus.VERIFIED
    assert out["verification"].tier == "established"
    # The sentence restates the bound RESOLVED fact (subject + literal), so
    # no CONCLUSION_UNMAPPED demotion fires — the 042953 L5 PARTIAL shape.
    assert out["unmapped"] == []
    assert out["decision"].approved is True
