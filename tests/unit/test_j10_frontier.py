"""J10: dataflow stopping points and the frontier's blind spot (synthetic only).

Covers D19 (typed receiver/member/alias/capture stops), D20 (analyzer
summary + history stamp), D21 (pure derivation + dimension mapping, LV-free),
D22 (LV_J-gated derivation; the W1-A-deleted advisory ranker is not
resurrected — the pin lives on the pure helper). No analyzed repository
is referenced.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from investigation_agent_platform.domain.code.models import CodeFactStatus
from investigation_agent_platform.domain.collection.evidence_state import (
    AnalysisStamp,
    derive_evidence_state,
)
from investigation_agent_platform.domain.collection.frontier import (
    EvidenceState,
    FailureCharacter,
    FrontierDimension,
)
from investigation_agent_platform.domain.evidence.models import Evidence, EvidenceType
from investigation_agent_platform.domain.investigation.loop_version import LOOP_VERSION_LVJ
from investigation_agent_platform.domain.provenance.models import (
    EvidenceFreshness,
    EvidenceProvenance,
    QueryFingerprint,
    SourceLocation,
)

_PATH = "service/src/main/java/com/sample/orders/OrderServiceHandler.java"


def _evidence(source, *, path=_PATH, revision="54d73240d6b5"):
    iid, eid = uuid4(), uuid4()
    now = datetime.now(UTC)
    numbered = "\n".join(f"{i + 1}| {ln}" for i, ln in enumerate(source.splitlines()))
    end = max(1, len(source.splitlines()))
    return Evidence(
        evidence_id=eid,
        tenant_id="tenant-a",
        investigation_id=iid,
        evidence_type=EvidenceType.SOURCE_CODE,
        provider="pygit2-local",
        source=f"git://repo/{path}@{revision}",
        title="Java source range",
        summary="Pinned Java source",
        content_snippet=numbered,
        attributes={"read_mode": "range"},
        observed_at=now,
        retrieved_at=now,
        provenance=EvidenceProvenance(
            tenant_id="tenant-a",
            investigation_id=iid,
            provider_type="GIT",
            requested_provider_id="pygit2-local",
            actual_provider_id="pygit2-local",
            source_system="Git",
            retrieval_timestamp=now,
            query_fingerprint=QueryFingerprint(
                provider_type="GIT", operation="GET_CODE", normalized_query_hash="h"
            ),
            source_location=SourceLocation(
                system="Git",
                identifier=f"repo/{path}",
                file_path=path,
                line_start=1,
                line_end=end,
                revision=revision,
            ),
        ),
        freshness=EvidenceFreshness(observed_at=now, retrieved_at=now),
        fingerprint=str(eid),
    )


def _stamp(**kw):
    base = {"resolved": 0, "partial": 0, "unresolved": 0}
    base.update(kw)
    return AnalysisStamp.parse(base)


def test_j10_t01_no_rows():
    assert derive_evidence_state(banked_total=0, stamps=[]) == EvidenceState.NO_ROWS


def test_j10_t02_rows_no_analyzer_signal():
    assert (
        derive_evidence_state(banked_total=3, stamps=[]) == EvidenceState.ROWS_WITHOUT_MATCH
    )


def test_j10_t03_window_class_refusal():
    stamp = _stamp(partial=1, window=1)
    assert stamp is not None
    assert (
        derive_evidence_state(banked_total=2, stamps=[stamp])
        == EvidenceState.ANALYSIS_INCOMPLETE
    )


def test_j10_t04_runtime_or_failure_refusal():
    assert (
        derive_evidence_state(banked_total=1, stamps=[_stamp(unresolved=1, runtime=1)])
        == EvidenceState.ANALYSIS_BLOCKED
    )
    assert (
        derive_evidence_state(banked_total=1, stamps=[_stamp(unresolved=1, failure=1)])
        == EvidenceState.ANALYSIS_BLOCKED
    )


def test_j10_t05_widen_and_retry_supersedes():
    assert (
        derive_evidence_state(
            banked_total=2, stamps=[_stamp(partial=1, window=1), _stamp(resolved=1)]
        )
        == EvidenceState.ROWS_WITHOUT_MATCH
    )


def test_j10_t06_clean_unrelated_row_is_not_gave_up():
    assert (
        derive_evidence_state(banked_total=1, stamps=[_stamp(resolved=1)])
        == EvidenceState.ROWS_WITHOUT_MATCH
    )


def test_j10_t07_producers_need_claim_level_facts():
    resolved = _stamp(resolved=1)
    assert (
        derive_evidence_state(banked_total=1, stamps=[resolved], claims_verified=True)
        == EvidenceState.LOCAL_COMPLETE
    )
    assert (
        derive_evidence_state(banked_total=1, stamps=[resolved], match_predates_onset=True)
        == EvidenceState.STALE_MATCH
    )
    assert (
        derive_evidence_state(banked_total=1, stamps=[resolved])
        == EvidenceState.ROWS_WITHOUT_MATCH
    )


def test_j10_t08_dimension_mapping():
    plain = FailureCharacter(triage_route="UNKNOWN")
    assert plain.spread_dimension(EvidenceState.ANALYSIS_INCOMPLETE) == (
        FrontierDimension.ASSUMPTION_SPREAD
    )
    assert plain.spread_dimension(EvidenceState.ANALYSIS_BLOCKED) == (
        FrontierDimension.LAYER_SPREAD
    )
    rival = FailureCharacter(triage_route="UNKNOWN", has_unbeaten_rival=True)
    assert rival.spread_dimension(EvidenceState.ANALYSIS_INCOMPLETE) == (
        FrontierDimension.RIVAL_SPREAD
    )


def test_j10_t09_summarizer_shape():
    from investigation_agent_platform.application.analysis.summary import (
        summarize_analysis,
    )

    payloads = [
        {"status": "RESOLVED", "limitation_codes": []},
        {"status": "PARTIAL", "limitation_codes": ["interprocedural_aliasing"]},
        {"status": "UNRESOLVED", "limitation_codes": ["analysis_failed"]},
    ]
    summary = summarize_analysis(payloads)
    assert summary == {
        "resolved": 1,
        "partial": 1,
        "unresolved": 1,
        "window": 1,
        "runtime": 0,
        "unsupported": 0,
        "failure": 1,
        "provenance": 0,
    }
    text = str(payloads)
    assert "interprocedural_aliasing" in text  # input fixture, not output
    assert "analysis_failed" not in str(summary)


def test_j10_t10_summarizer_is_total():
    from investigation_agent_platform.application.analysis.summary import (
        summarize_analysis,
    )

    assert summarize_analysis([]) is None
    assert summarize_analysis([{"status": "BOGUS"}, {"nope": 1}, None]) is None
    assert summarize_analysis([{"status": "RESOLVED"}]) is not None


def test_j10_t11_pre_lvj_pin_keeps_row_count():
    from investigation_agent_platform.application.worker.workflows.forcing import (
        _evidence_state_for,
    )

    history = [
        {"evidence_added": 2, "analysis": {"unresolved": 1, "failure": 1}},
    ]
    state, gave_up = _evidence_state_for(history, banked_total=3, loop_version="lv7")
    assert state == EvidenceState.ROWS_WITHOUT_MATCH
    assert gave_up is False


def test_j10_t12_lvj_derives_from_analyzer_status():
    from investigation_agent_platform.application.worker.workflows.forcing import (
        _evidence_state_for,
    )

    history = [
        {"evidence_added": 2, "analysis": {"partial": 1, "window": 1}},
    ]
    state, gave_up = _evidence_state_for(history, banked_total=3, loop_version=LOOP_VERSION_LVJ)
    assert state == EvidenceState.ANALYSIS_INCOMPLETE
    assert gave_up is True
    assert FailureCharacter(triage_route="UNKNOWN").spread_dimension(state) == (
        FrontierDimension.ASSUMPTION_SPREAD
    )


def test_j10_t13_history_stamp_round_trips():
    stamp_dict = {"resolved": 0, "partial": 1, "unresolved": 0, "window": 1}
    parsed = AnalysisStamp.parse(stamp_dict)
    assert parsed is not None and parsed.partial == 1 and parsed.window == 1
    assert AnalysisStamp.parse(None) is None
    assert AnalysisStamp.parse("junk") is None
    assert AnalysisStamp.parse({"partial": "x"}) is None
    assert AnalysisStamp.parse({"resolved": 2}).resolved == 2


def _resolve_flow(source, symbol, scope):
    from investigation_agent_platform.application.analysis.java.dataflow import (
        resolve_java_data_flow,
    )

    return resolve_java_data_flow(_evidence(source), symbol, method_scope=scope)


_MUTABLE = """package com.sample.orders;
public class OrderServiceHandler {
    List<String> items(int v) {
        List<String> x = new ArrayList<>();
        x.add("a");
        return x;
    }
}
"""

_IMMUTABLE = """package com.sample.orders;
public class OrderServiceHandler {
    String clean(String x) {
        return x.trim();
    }
}
"""

_MEMBER = """package com.sample.orders;
public class OrderServiceHandler {
    int get(Holder h) {
        h.count = 4;
        return h.count;
    }
}
"""

_ARRAY = """package com.sample.orders;
public class OrderServiceHandler {
    int get(int[] a) {
        a[0] = 5;
        return a[0];
    }
}
"""

_ALIAS = """package com.sample.orders;
public class OrderServiceHandler {
    int get(String x) {
        this.cache = x;
        return x.length();
    }
}
"""

_CAPTURE = """package com.sample.orders;
public class OrderServiceHandler {
    IntSupplier defer(String x) {
        IntSupplier q = () -> x.length();
        return q;
    }
}
"""


def test_j10_t14_receiver_mutation_is_typed():
    result = _resolve_flow(_MUTABLE, "x", "items")
    assert result.status == CodeFactStatus.PARTIAL
    assert result.limitation_codes == ("receiver_mutation_possible",)


def test_j10_t15_immutable_receiver_stays_resolved():
    result = _resolve_flow(_IMMUTABLE, "x", "clean")
    assert result.status == CodeFactStatus.RESOLVED


def test_j10_t16_field_and_array_store():
    assert _resolve_flow(_MEMBER, "h", "get").limitation_codes == (
        "receiver_mutation_possible",
    )
    assert _resolve_flow(_ARRAY, "a", "get").limitation_codes == (
        "receiver_mutation_possible",
    )


def test_j10_t17_alias_store_and_lambda_capture():
    assert _resolve_flow(_ALIAS, "x", "get").limitation_codes == ("alias_escape",)
    assert _resolve_flow(_CAPTURE, "x", "defer").limitation_codes == (
        "lambda_capture_not_modeled",
    )


def test_j10_t18_registry_covers_new_codes():
    from investigation_agent_platform.application.analysis.limitations import (
        LimitationClass,
        limitation_class,
    )

    for code in ("receiver_mutation_possible", "alias_escape", "lambda_capture_not_modeled"):
        assert limitation_class(code) is LimitationClass.UNSUPPORTED
