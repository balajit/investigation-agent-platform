# tests/unit/test_rev_battery_goldens.py
"""REV_BATTERY goldens — live battery 20261021T030700 L2/L3/L5.

Phase 0 of docs/REV_BATTERY_CLOSE_PLAN.md. Pins the exact
`check_claim_kind` + `evaluate_hypothesis` strings captured live so
Phase 1 fixes flip them deliberately, not accidentally.
"""

from datetime import UTC, datetime
from uuid import uuid4

from investigation_agent_platform.application.investigation.verification import (
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
REV = "54d73240d6b5b03b899bcaf0e0c4e1cdc31be9fd"
MAPPER = "service/src/main/java/com/sample/orders/legacy/LegacyCodeMapper.java"


def _ev(
    etype=EvidenceType.SOURCE_CODE,
    snippet="",
    file_path=None,
    line_start=None,
    line_end=None,
    revision=None,
    operation="GET_SOURCE",
    attrs=None,
):
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
                normalized_query_hash="golden",
            ),
            source_location=SourceLocation(
                system="Git",
                identifier=file_path or "x",
                file_path=file_path,
                line_start=line_start,
                line_end=line_end,
                revision=revision,
            ),
        ),
        freshness=EvidenceFreshness(observed_at=now, retrieved_at=now),
    )


def _hyp_with_edge(statement, target, row, status=HypothesisStatus.UNDER_INVESTIGATION):
    hyp_id = uuid4()
    asm = HypothesisEvidenceAssessment(
        hypothesis_id=hyp_id,
        evidence_id=row.evidence_id,
        assessment=AssessmentType.SUPPORTS,
        strength=AssessmentStrength.MODERATE,
        reason="cited by CALL_EDGE claim",
        claim_kind=ClaimKind.CALL_EDGE,
        claim_target=target,
    )
    return Hypothesis(
        tenant_id="n-batt",
        investigation_id=row.investigation_id,
        id=hyp_id,
        title=statement[:256],
        description=statement,
        statement=statement,
        status=status,
        supporting_evidence_ids=[row.evidence_id],
        assessments=[asm],
    )


def test_L2_1_enumeration_guard_fixed():
    """Live L2#1 8755dfa3 — Class.method counterpart matches class stem."""
    path = "service/src/main/java/com/sample/orders/proxy/OrderServiceRestProxy.java"
    snippet = (
        "1| package com.sample.orders.proxy;\n"
        "10|     public String submit(String orderId) {\n"
        "11|         return handler.handle(orderId);"
    )
    row = _ev(
        snippet=snippet, file_path=path, line_start=1, line_end=13, revision=REV
    )
    target = ClaimTarget(
        symbol="OrderServiceHandler.handle",
        counterpart="OrderServiceRestProxy.submit",
        file_path=path,
        line_start=10,
        line_end=11,
        scope="OrderServiceRestProxy.submit",
        declaring_type="OrderServiceRestProxy",
        commit_id=REV,
    )
    hyp = _hyp_with_edge(
        "OrderServiceRestProxy calls OrderServiceHandler through its submit method.",
        target,
        row,
    )
    ok, reason = ENGINE.check_claim_kind(hyp, [row])
    assert (ok, reason) == (True, "")
    res = ENGINE.evaluate_hypothesis(hyp, [row], [], [], POLICY)
    # Kind-gate now passes; heuristic scoring still abstains (coverage 1/4,
    # single provider, causal 0) — Phase 1c decision owns the scoring question.
    assert res.status == VerificationStatus.INCONCLUSIVE


def test_enumeration_guard_still_catches_unbound_caller():
    """Negative guard: statement names two callers, only one is bound."""
    proxy_path = "service/src/main/java/com/sample/orders/proxy/OrderServiceRestProxy.java"
    helper_path = "service/src/main/java/com/sample/orders/helper/OrderQualifyHelper.java"
    proxy_row = _ev(
        snippet="10|     public String submit(String orderId) {\n11|         return handler.handle(orderId);",
        file_path=proxy_path,
        line_start=10,
        line_end=12,
        revision=REV,
    )
    helper_row = _ev(
        snippet="10|     public String qualify(String orderId, boolean preChecked) {\n12|             return handler.handle(orderId);",
        file_path=helper_path,
        line_start=10,
        line_end=12,
        revision=REV,
    )
    target = ClaimTarget(
        symbol="OrderServiceHandler.handle",
        counterpart="OrderServiceRestProxy.submit",
        file_path=proxy_path,
        line_start=10,
        line_end=11,
        commit_id=REV,
    )
    hyp = _hyp_with_edge(
        "OrderServiceRestProxy and OrderQualifyHelper call OrderServiceHandler.",
        target,
        proxy_row,
    )
    ok, reason = ENGINE.check_claim_kind(hyp, [proxy_row, helper_row])
    assert ok is False
    assert reason == "caller enumeration names a caller without its own CALL_EDGE claim"


def test_L2_2_comment_row_shows_no_call():
    """Live L2#2 4bb62078 — bound comment range shows no call site."""
    path = "service/src/main/java/com/sample/orders/proxy/OrderServiceRestProxy.java"
    row = _ev(
        snippet="5| /** REST proxy: remote facade over the order service handler. */",
        file_path=path,
        line_start=5,
        line_end=5,
        revision=REV,
    )
    target = ClaimTarget(
        symbol="OrderServiceHandler",
        counterpart="OrderServiceRestProxy",
        file_path=path,
        line_start=5,
        line_end=5,
        commit_id=REV,
    )
    hyp = _hyp_with_edge(
        "OrderServiceRestProxy calls the order service handler.", target, row
    )
    ok, reason = ENGINE.check_claim_kind(hyp, [row])
    assert ok is False
    assert "no cited row shows a call" in reason or "no exact bound range read" in reason
    res = ENGINE.evaluate_hypothesis(hyp, [row], [], [], POLICY)
    assert res.status == VerificationStatus.INCONCLUSIVE


def test_L3_absence_gate_passes_but_scoring_abstains():
    """Live L3#1 18b7e151 — absence proof passes, heuristic score abstains."""
    from investigation_agent_platform.domain.hypothesis.models import (
        HypothesisEvidenceAssessment as Asm,
    )

    hyp_id = uuid4()
    iid = uuid4()
    now = datetime.now(UTC)
    row = _ev(
        etype=EvidenceType.COMMIT_HISTORY,
        snippet="No matches in scope",
        operation="get_code_history",
        attrs={
            "match_count": 0,
            "search_scope": "spring-sample-service:history=OrderServiceImpl.java",
        },
    )
    row = row.model_copy(update={"investigation_id": iid})
    target = ClaimTarget(scope="spring-sample-service:history=OrderServiceImpl.java")
    asm = Asm(
        hypothesis_id=hyp_id,
        evidence_id=row.evidence_id,
        assessment=AssessmentType.SUPPORTS,
        strength=AssessmentStrength.MODERATE,
        reason="cited by ABSENCE claim",
        claim_kind=ClaimKind.ABSENCE,
        claim_target=target,
    )
    hyp = Hypothesis(
        tenant_id="n-batt",
        investigation_id=iid,
        id=hyp_id,
        title="No recent changes were found in OrderServiceImpl.java."[:256],
        description="No recent changes were found in OrderServiceImpl.java.",
        statement="No recent changes were found in OrderServiceImpl.java.",
        status=HypothesisStatus.UNDER_INVESTIGATION,
        supporting_evidence_ids=[row.evidence_id],
        assessments=[asm],
    )
    _ = now
    ok, reason = ENGINE.check_claim_kind(hyp, [row])
    assert (ok, reason) == (True, "")
    res = ENGINE.evaluate_hypothesis(hyp, [row], [], [], POLICY)
    assert res.status == VerificationStatus.INCONCLUSIVE
    assert res.confidence.overall_confidence < 0.80


def test_L5_1_behavior_lacks_runtime():
    """Live L5#1 41d4966f — BEHAVIOR kind-gate needs runtime."""
    path = "service/src/main/java/com/sample/orders/legacy/LegacyCodeMapper.java"
    snippet = (
        "6|     public String normalize(String orderType) {\n"
        "7|         if (orderType == null) {\n"
        '8|             return "STANDARD";'
    )
    row = _ev(
        snippet=snippet, file_path=path, line_start=6, line_end=19, revision=REV
    )
    hyp_id = uuid4()
    target = ClaimTarget(
        symbol="LegacyCodeMapper.normalize",
        file_path=path,
        line_start=6,
        line_end=19,
        scope="LegacyCodeMapper.normalize",
        declaring_type="LegacyCodeMapper",
        commit_id=REV,
        claimed_value="STANDARD",
    )
    asm = HypothesisEvidenceAssessment(
        hypothesis_id=hyp_id,
        evidence_id=row.evidence_id,
        assessment=AssessmentType.SUPPORTS,
        strength=AssessmentStrength.MODERATE,
        reason="cited by BEHAVIOR claim",
        claim_kind=ClaimKind.BEHAVIOR,
        claim_target=target,
        claimed_value="STANDARD",
    )
    hyp = Hypothesis(
        tenant_id="n-batt",
        investigation_id=row.investigation_id,
        id=hyp_id,
        title="When orderType is null, the normalize method returns STANDARD."[:256],
        description="When orderType is null, the normalize method returns STANDARD.",
        statement="When orderType is null, the normalize method returns STANDARD.",
        status=HypothesisStatus.UNDER_INVESTIGATION,
        supporting_evidence_ids=[row.evidence_id],
        assessments=[asm],
    )
    ok, reason = ENGINE.check_claim_kind(hyp, [row])
    assert ok is False
    assert "runtime" in reason


def _resolved_l5_row():
    """Live 042953 L5#1 shape: 6-19 window + RESOLVED V2 payload, as banked."""
    from investigation_agent_platform.application.analysis.java.method_return_facts import (
        resolve_java_method_return_fact,
    )
    from investigation_agent_platform.domain.code.models import CODE_FACT_RESOLUTION_V2_KEY

    snippet = (
        "6|     public String normalize(String orderType) {\n"
        "7|         if (orderType == null) {\n"
        '8|             return "STANDARD";\n'
        "9|         }\n"
        "10|         switch (orderType) {\n"
        '11|             case "LEGACY-BULK":\n'
        '12|                 return "BULK";\n'
        "13|             case \"RESIDENTIAL\":\n"
        '14|                 return "RESIDENTIAL";\n'
        "15|             default:\n"
        "16|                 return orderType;\n"
        "17|         }\n"
        "18|     }\n"
        "19| }"
    )
    row = _ev(snippet=snippet, file_path=MAPPER, line_start=6, line_end=19, revision=REV)
    row = row.model_copy(
        update={
            "provenance": row.provenance.model_copy(
                update={
                    "source_location": row.provenance.source_location.model_copy(
                        update={"identifier": f"samplecorp/orders-internal/{MAPPER}"}
                    )
                }
            )
        }
    )
    resolution = resolve_java_method_return_fact(
        row,
        "normalize",
        guard_condition="orderType == null",
        declaring_type="LegacyCodeMapper",
    )
    assert resolution.status.value == "RESOLVED"
    return row.model_copy(
        update={
            "attributes": {
                **row.attributes,
                CODE_FACT_RESOLUTION_V2_KEY: resolution.model_dump(mode="json"),
            }
        }
    )


def test_conclusion_maps_resolved_source_value():
    """Product fix for 042953 L5 PARTIAL: sentence restates the bound RESOLVED fact."""
    from investigation_agent_platform.application.investigation.verification import (
        VerificationEngine as VE,
    )

    row = _resolved_l5_row()
    root = (
        "When orderType is null, LegacyCodeMapper.normalize returns 'STANDARD', "
        "making 'STANDARD' the default applied for null orderType during order filtering."
    )
    assert VE.map_conclusion_sentences(root, [row]) == []


def test_conclusion_still_flags_unresolved_value():
    """Without a RESOLVED payload the same sentence stays unmapped (no laundering)."""
    from investigation_agent_platform.application.investigation.verification import (
        VerificationEngine as VE,
    )

    row = _ev(
        snippet="6| x = 1",
        file_path=MAPPER,
        line_start=6,
        line_end=6,
    )
    root = "When orderType is null, LegacyCodeMapper.normalize returns 'STANDARD'."
    assert VE.map_conclusion_sentences(root, [row]) != []


def test_statement_range_reads_stem_fallback_for_bare_callers():
    """Phase 1b: bare stems join banked call-site ranges (live L2#2 shape)."""
    from investigation_agent_platform.application.investigation.verification import (
        VerificationEngine as VE,
    )

    proxy_path = "service/src/main/java/com/sample/orders/proxy/OrderServiceRestProxy.java"
    helper_path = "service/src/main/java/com/sample/orders/helper/OrderQualifyHelper.java"
    proxy_row = _ev(
        snippet="10|     public String submit(String orderId) {\n11|         return handler.handle(orderId);",
        file_path=proxy_path,
        line_start=10,
        line_end=12,
        revision=REV,
    )
    helper_row = _ev(
        snippet="10|     public String qualify(String orderId, boolean preChecked) {\n12|             return handler.handle(orderId);",
        file_path=helper_path,
        line_start=10,
        line_end=12,
        revision=REV,
    )
    search_row = _ev(
        snippet="OrderServiceRestProxy",
        file_path=proxy_path,
        line_start=5,
        line_end=5,
        revision=REV,
        operation="SEARCH_CODE",
    )
    extra = VE.statement_range_reads(
        "OrderServiceRestProxy calls the order service handler.",
        [proxy_row, helper_row, search_row],
    )
    ids = {e.evidence_id for e in extra}
    assert proxy_row.evidence_id in ids
    # SEARCH operation rows never join, even on stem match.
    assert search_row.evidence_id not in ids


def test_same_claim_sibling_repair_035908():
    """Live 035908 L2: helper cites a search hit, construction range is bound.

    Bare-class callee ``OrderServiceHandler`` is shown by ``new
    OrderServiceHandler()`` in the caller file — the ``handler.handle()``
    method call alone does not name the class.
    """
    from investigation_agent_platform.domain.hypothesis.models import (
        AssessmentStrength,
        AssessmentType,
        Hypothesis,
        HypothesisEvidenceAssessment,
        HypothesisStatus,
    )

    helper_path = "samplecorp/orders-internal/service/src/main/java/com/sample/orders/helper/OrderQualifyHelper.java"
    investigation_id = uuid4()

    def _same_investigation(row):
        return row.model_copy(
            update={
                "investigation_id": investigation_id,
                "provenance": row.provenance.model_copy(
                    update={"investigation_id": investigation_id}
                ),
            }
        )

    cited = _ev(
        snippet="import com.sample.orders.handler.OrderServiceHandler;",
        file_path=helper_path,
        line_start=3,
        line_end=3,
        revision=REV,
        operation="SEARCH_CODE",
    )
    cited = _same_investigation(cited)
    declaring = _ev(
        snippet="8|     private final OrderServiceHandler handler = new OrderServiceHandler();",
        file_path=helper_path,
        line_start=1,
        line_end=8,
        revision=REV,
    )
    declaring = _same_investigation(declaring)
    invoking = _ev(
        snippet="1|     private final OrderServiceHandler handler = new OrderServiceHandler();\n10|     public String qualify(String orderId, boolean preChecked) {\n12|             return handler.handle(orderId);",
        file_path=helper_path,
        line_start=1,
        line_end=12,
        revision=REV,
    )
    invoking = _same_investigation(invoking)
    target = ClaimTarget(
        symbol="OrderServiceHandler",
        counterpart="OrderQualifyHelper",
        file_path=helper_path,
        line_start=1,
        line_end=12,
        commit_id=REV,
    )
    hid = uuid4()
    hyp = Hypothesis(
        tenant_id="n-batt",
        investigation_id=cited.investigation_id,
        id=hid,
        title="OrderQualifyHelper calls the order service handler.",
        description="OrderQualifyHelper calls the order service handler.",
        statement="OrderQualifyHelper calls the order service handler.",
        status=HypothesisStatus.UNDER_INVESTIGATION,
        supporting_evidence_ids=[cited.evidence_id],
        assessments=[
            HypothesisEvidenceAssessment(
                hypothesis_id=hid,
                evidence_id=cited.evidence_id,
                assessment=AssessmentType.SUPPORTS,
                strength=AssessmentStrength.MODERATE,
                reason="cited by CALL_EDGE claim",
                claim_kind=ClaimKind.CALL_EDGE,
                claim_target=target,
            )
        ],
    )
    # Cited row alone shows no call.
    ok, _ = ENGINE.check_claim_kind(hyp, [cited])
    assert ok is False
    # A constructor line alone is construction, not invocation.
    ok, _ = ENGINE.check_claim_kind(hyp, [cited, declaring])
    assert ok is False
    # Declaration row + invocation row in one file verifies the join.
    ok, reason = ENGINE.check_claim_kind(hyp, [cited, declaring, invoking])
    assert (ok, reason) == (True, "")


def _typed_hyp(kind):
    from uuid import uuid4

    from investigation_agent_platform.domain.hypothesis.models import (
        AssessmentStrength,
        AssessmentType,
        Hypothesis,
        HypothesisEvidenceAssessment,
        HypothesisStatus,
    )

    hid, eid, iid = uuid4(), uuid4(), uuid4()
    return Hypothesis(
        tenant_id="n-batt",
        investigation_id=iid,
        id=hid,
        title="t",
        description="d",
        statement="typed",
        status=HypothesisStatus.PROPOSED,
        supporting_evidence_ids=[eid],
        assessments=[
            HypothesisEvidenceAssessment(
                hypothesis_id=hid,
                evidence_id=eid,
                assessment=AssessmentType.SUPPORTS,
                strength=AssessmentStrength.MODERATE,
                reason="r",
                claim_kind=kind,
            )
        ],
    )


def test_proof_obligation_routing():
    """Phase 3a: each question kind routes to its proof procedure."""
    from investigation_agent_platform.application.investigation.verification import (
        VerificationEngine as VE,
    )
    from investigation_agent_platform.domain.hypothesis.models import ClaimKind

    assert VE.effective_claim_kind(_typed_hyp(ClaimKind.DEFINITION)) == ClaimKind.DEFINITION
    assert VE.effective_claim_kind(_typed_hyp(ClaimKind.CALL_EDGE)) == ClaimKind.CALL_EDGE
    assert VE.effective_claim_kind(_typed_hyp(ClaimKind.BEHAVIOR)) == ClaimKind.BEHAVIOR
    assert VE.effective_claim_kind(_typed_hyp(ClaimKind.ABSENCE)) == ClaimKind.ABSENCE
    assert VE.effective_claim_kind(_typed_hyp(ClaimKind.HISTORY)) == ClaimKind.HISTORY
    # CAUSAL takes the generic causal path (no kind-specific gate).
    assert ENGINE.check_claim_kind(_typed_hyp(ClaimKind.CAUSAL), []) is None


def test_abstention_next_check_is_diagnostic():
    """Phase 2b: kind-gate reasons map to targeted next checks."""
    from investigation_agent_platform.application.investigation.verification import (
        VerificationEngine as VE,
    )

    assert "CALL_EDGE" in VE.next_check_for_abstention(
        "insufficient",
        ["caller enumeration names a caller without its own CALL_EDGE claim"],
    )
    assert "range-read" in VE.next_check_for_abstention(
        "insufficient", ["no cited row shows a call to 'X'"]
    )
    assert "runtime" in VE.next_check_for_abstention(
        "insufficient", ["behavior claim lacks runtime evidence"]
    )
    assert "source-value" in VE.next_check_for_abstention(
        "insufficient", ["Java fact claim is not established: resolution is UNRESOLVED"]
    )
    assert "Rebind" in VE.next_check_for_abstention(
        "insufficient", ["cited evidence_id ff068c96 is unknown in this investigation"]
    )


def test_absence_outcome_four_states():
    """Phase 2a: NO_SCOPE / EMPTY_SEARCH_PROVED / NO_SEARCH / SCOPE_MISMATCH."""
    from investigation_agent_platform.application.investigation.verification import (
        VerificationEngine as VE,
    )
    from investigation_agent_platform.domain.hypothesis.models import ClaimTarget

    def _scope_row(scope):
        return _ev(
            etype=__import__(
                "investigation_agent_platform.domain.evidence.models", fromlist=["EvidenceType"]
            ).EvidenceType.COMMIT_HISTORY,
            snippet="No matches in scope",
            operation="get_code_history",
            attrs={"match_count": 0, "search_scope": scope},
        )

    assert VE.absence_outcome([], ClaimTarget())[0] == "NO_SCOPE"
    assert VE.absence_outcome([], ClaimTarget(scope="s"))[0] == "NO_SEARCH"
    assert (
        VE.absence_outcome(
            [_scope_row("other")], ClaimTarget(scope="spring:history=A.java")
        )[0]
        == "SCOPE_MISMATCH"
    )
    assert (
        VE.absence_outcome(
            [_scope_row("spring-sample-service:history=OrderServiceImpl.java")],
            ClaimTarget(scope="spring-sample-service:history=OrderServiceImpl.java"),
        )[0]
        == "EMPTY_SEARCH_PROVED"
    )
