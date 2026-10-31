"""J5: newer Java constructs are modeled or carry a typed limitation (synthetic only).

Covers D5-D8: path conditions, nested-type attribution, interface constants,
enum-constant and switch markers, long-literal containment, typed failure
(WindowAnalysis), verifier explanation, failure payload validity, and the
python field-chain failure path. No analyzed repository is referenced.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

from investigation_agent_platform.application.analysis.failure import (
    GrammarUnavailable,
    analysis_failed_resolution,
    failure_code,
)
from investigation_agent_platform.application.analysis.java import window_facts as wf
from investigation_agent_platform.application.analysis.java.model import (
    SCHEMA_VERSION,
    build_java_model,
)
from investigation_agent_platform.application.analysis.java.window_result import (
    WindowAnalysis,
    WindowStatus,
)
from investigation_agent_platform.application.investigation.verification import (
    VerificationEngine,
)
from investigation_agent_platform.domain.code.models import (
    WINDOW_ANALYSIS_KEY,
    CodeFactStatus,
    JavaFactCategory,
)
from investigation_agent_platform.domain.evidence.models import Evidence, EvidenceType
from investigation_agent_platform.domain.provenance.models import (
    EvidenceFreshness,
    EvidenceProvenance,
    QueryFingerprint,
    SourceLocation,
)

_PATH = "service/src/main/java/com/sample/orders/OrderServiceHandler.java"


def _evidence(
    source,
    *,
    path=_PATH,
    revision="54d73240d6b5",
    numbered=True,
    operation="GET_CODE",
):
    iid, eid = uuid4(), uuid4()
    now = datetime.now(UTC)
    if numbered:
        snippet = "\n".join(f"{i + 1}| {ln}" for i, ln in enumerate(source.splitlines()))
        end = max(1, len(source.splitlines()))
    else:
        snippet = source
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
        content_snippet=snippet,
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
                provider_type="GIT", operation=operation, normalized_query_hash="h"
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


def _hypothesis(symbol, statement="typed claim"):
    return SimpleNamespace(
        id=uuid4(),
        statement=statement,
        claimed_value=None,
        claim_target=SimpleNamespace(symbol=symbol, claimed_value=None),
        fact_category=JavaFactCategory.SOURCE_VALUE_FACT,
        supporting_evidence_ids=[],
    )


_ELSE_IF = """package com.sample.orders;
public class OrderServiceHandler {
    public String kind(int v) {
        if (v > 0) {
            return "pos";
        } else if (v < 0) {
            return "neg";
        }
        return "zero";
    }
}
"""

_NESTED_IF = """package com.sample.orders;
public class OrderServiceHandler {
    public String deep(boolean a, boolean b, boolean c) {
        if (a) {
            if (b) {
                if (c) {
                    return "deep";
                }
            }
        }
        return "shallow";
    }
}
"""

_ENUM = """package com.sample.orders;
public class OrderServiceHandler {
    enum Mode {
        FAST("f"), SLOW("s");
        private final String code;
        Mode(String code) { this.code = code; }
        String label() {
            if (this == FAST) {
                return "fast-label";
            }
            return "other";
        }
    }
}
"""

_NESTED_CLASS = """package com.sample.orders;
public class OrderServiceHandler {
    static class Inner {
        int n = 7;
        String get() {
            if (n > 3) {
                return "nine";
            }
            return "zero";
        }
    }
}
"""

_INTERFACE = """package com.sample.orders;
public interface Limits {
    int LIMIT = 5;
    String NAME = "n";
}
"""

_ENUM_CONSTANTS_ONLY = """package com.sample.orders;
public class OrderServiceHandler {
    enum Mode {
        FAST("a"), SLOW
    }
}
"""

_SWITCH_EXPR = """package com.sample.orders;
public class OrderServiceHandler {
    String sw(int v) {
        return switch (v) {
            case 1 -> "one";
            default -> "o";
        };
    }
    String clean(int v) {
        if (v > 0) {
            return "up";
        }
        return "down";
    }
}
"""

_SWITCH_STMT = """package com.sample.orders;
public class OrderServiceHandler {
    String old(int v) {
        String out;
        switch (v) {
            case 1:
                out = "one";
                break;
            default:
                out = "o";
        }
        return out;
    }
}
"""

_LAMBDA_SWITCH = """package com.sample.orders;
public class OrderServiceHandler {
    Runnable make() {
        return () -> {
            int v = 1;
            switch (v) {
                case 1:
                    System.out.println("one");
                    break;
                default:
                    break;
            }
        };
    }
    String clean(int v) {
        if (v > 0) {
            return "up";
        }
        return "down";
    }
}
"""

_LAMBDA_ANON = """package com.sample.orders;
public class OrderServiceHandler {
    Runnable make() {
        return () -> {
            int v = 1;
            if (v > 0) {
                System.out.println("up");
            }
        };
    }
    Runnable anon() {
        return new Runnable() {
            public void run() {
                int v = 2;
                if (v > 0) {
                    System.out.println("up");
                }
            }
        };
    }
    String clean(int v) {
        if (v > 0) {
            return "up";
        }
        return "down";
    }
}
"""


def _records_by_subject(analysis):
    return {r.subject_identity: r for r in analysis.records}


def test_j5_t01_else_if_carries_negated_outer_guard():
    analysis = wf.enumerate_java_window_facts(_evidence(_ELSE_IF))
    assert isinstance(analysis, WindowAnalysis)
    rec = _records_by_subject(analysis).get("com.sample.orders.OrderServiceHandler.kind")
    assert rec is not None and rec.status == CodeFactStatus.RESOLVED
    assert rec.resolved_value == "neg"
    assert rec.structured_fact is not None
    assert rec.structured_fact["guard_text"] == "(v < 0)"
    assert rec.structured_fact["path_conditions"] == [{"text": "(v > 0)", "holds": False}]


def test_j5_t02_nested_if_carries_positive_outer_guards_outermost_first():
    analysis = wf.enumerate_java_window_facts(_evidence(_NESTED_IF))
    rec = _records_by_subject(analysis).get("com.sample.orders.OrderServiceHandler.deep")
    assert rec is not None and rec.resolved_value == "deep"
    assert rec.structured_fact["path_conditions"] == [
        {"text": "(a)", "holds": True},
        {"text": "(b)", "holds": True},
    ]


def test_j5_t03_enum_method_attributed_to_enum():
    analysis = wf.enumerate_java_window_facts(_evidence(_ENUM))
    by_subject = _records_by_subject(analysis)
    assert "com.sample.orders.OrderServiceHandler.Mode.label" in by_subject
    assert "com.sample.orders.OrderServiceHandler.label" not in by_subject


def test_j5_t04_nested_class_method_keeps_outer_class():
    analysis = wf.enumerate_java_window_facts(_evidence(_NESTED_CLASS))
    by_subject = _records_by_subject(analysis)
    assert "com.sample.orders.OrderServiceHandler.Inner.get" in by_subject
    assert "com.sample.orders.Inner.get" not in by_subject
    assert "com.sample.orders.OrderServiceHandler.Inner.n" in by_subject


def test_j5_t05_interface_constant_resolves_static():
    analysis = wf.enumerate_java_window_facts(_evidence(_INTERFACE))
    by_subject = _records_by_subject(analysis)
    limit = by_subject.get("com.sample.orders.Limits.LIMIT")
    name = by_subject.get("com.sample.orders.Limits.NAME")
    assert limit is not None and limit.status == CodeFactStatus.RESOLVED
    assert limit.resolved_value == "5"
    assert limit.field_category == "STATIC_FIELD"
    assert name is not None and name.resolved_value == "n"


def test_j5_t06_enum_constant_yields_typed_marker_and_inconclusive():
    ev = _evidence(_ENUM_CONSTANTS_ONLY)
    backfilled = VerificationEngine._with_window_backfill([ev])
    assert len(backfilled) == 1
    attrs = backfilled[0].attributes
    envelope = attrs.get("code_fact_resolutions") or []
    markers = [
        e["resolution"]
        for e in envelope
        if isinstance(e, dict)
        and isinstance(e.get("resolution"), dict)
        and e["resolution"].get("status") == CodeFactStatus.UNRESOLVED.value
    ]
    assert markers
    assert any(
        m.get("limitation_codes") == ["enum_constant_not_modeled"]
        and (m.get("subject_identity") or "").endswith("OrderServiceHandler.Mode.FAST")
        for m in markers
    )
    hyp = _hypothesis(
        "com.sample.orders.OrderServiceHandler.Mode.FAST",
        "Mode FAST has code a",
    )
    assert VerificationEngine().validate_java_fact_claim(hyp, backfilled, []) is None
    partial = VerificationEngine().java_partial_result(hyp, backfilled)
    assert partial is not None
    assert "enum_constant_not_modeled" in (partial.explanation or "")


def test_j5_t07_switch_returns_yield_typed_markers():
    for source in (_SWITCH_EXPR, _SWITCH_STMT):
        analysis = wf.enumerate_java_window_facts(_evidence(source))
        markers = [r for r in analysis.records if r.status == CodeFactStatus.UNRESOLVED]
        assert any(
            r.limitation_codes == ("switch_return_not_modeled",) for r in markers
        ), source
    clean = _records_by_subject(wf.enumerate_java_window_facts(_evidence(_SWITCH_EXPR)))
    assert clean["com.sample.orders.OrderServiceHandler.clean"].resolved_value == "up"


def test_j5_t07b_lambda_switch_not_credited_to_method():
    analysis = wf.enumerate_java_window_facts(_evidence(_LAMBDA_SWITCH))
    markers = [r for r in analysis.records if r.status == CodeFactStatus.UNRESOLVED]
    assert not [r for r in markers if r.limitation_codes == ("switch_return_not_modeled",)]
    assert (
        _records_by_subject(analysis)["com.sample.orders.OrderServiceHandler.clean"].status
        == CodeFactStatus.RESOLVED
    )


def test_j5_t08_long_literals_contained_per_declaration():
    long_field = "x" * 1200
    long_guard = "y" * 1200
    source = (
        "package com.sample.orders;\n"
        "public class OrderServiceHandler {\n"
        f'    private String big = "{long_field}";\n'
        '    private String small = "ok";\n'
        "    public String guarded(boolean flag) {\n"
        "        if (flag) {\n"
        f'            return "{long_guard}";\n'
        "        }\n"
        '        return "fine";\n'
        "    }\n"
        "    public String clean(boolean flag) { if (flag) { return \"c\"; } return \"d\"; }\n"
        "}\n"
    )
    assert len(source) < 4096
    analysis = wf.enumerate_java_window_facts(_evidence(source))
    assert analysis.status in (WindowStatus.OK, WindowStatus.DEGRADED)
    by_subject = _records_by_subject(analysis)
    assert by_subject["com.sample.orders.OrderServiceHandler.small"].status == (
        CodeFactStatus.RESOLVED
    )
    assert by_subject["com.sample.orders.OrderServiceHandler.big"].status == (
        CodeFactStatus.RESOLVED
    )
    assert (
        by_subject["com.sample.orders.OrderServiceHandler.guarded"].status
        == CodeFactStatus.RESOLVED
    )
    assert (
        by_subject["com.sample.orders.OrderServiceHandler.clean"].status
        == CodeFactStatus.RESOLVED
    )


def test_j5_t08b_overlong_return_becomes_typed_marker():
    long_return = "z" * 3000
    source = (
        "package com.sample.orders;\n"
        "public class OrderServiceHandler {\n"
        "    public String huge(boolean flag) {\n"
        "        if (flag) {\n"
        f'            return "{long_return}";\n'
        "        }\n"
        '        return "tiny";\n'
        "    }\n"
        "    public String clean() { if (true) { return \"c\"; } return \"d\"; }\n"
        "}\n"
    )
    assert len(source) < 4096
    analysis = wf.enumerate_java_window_facts(_evidence(source))
    assert analysis.status in (WindowStatus.OK, WindowStatus.DEGRADED)
    by_subject = _records_by_subject(analysis)
    assert by_subject["com.sample.orders.OrderServiceHandler.clean"].status == (
        CodeFactStatus.RESOLVED
    )
    huge = by_subject.get("com.sample.orders.OrderServiceHandler.huge")
    assert huge is not None and huge.status == CodeFactStatus.UNRESOLVED
    assert huge.limitation_codes == ("literal_too_long",)


def test_j5_t08c_long_local_and_argument_do_not_crash_model():
    long_local = "a" * 1200
    long_arg = "w" * 1200
    source = (
        "package com.sample.orders;\n"
        "public class OrderServiceHandler {\n"
        "    public String caller() {\n"
        f'        String v = "{long_local}";\n'
        f'        return sink("{long_arg}", v);\n'
        "    }\n"
        '    private String sink(String a, String b) { return a; }\n'
        "}\n"
    ).encode()
    model = build_java_model(
        source, file_path=_PATH, repository="repo", revision="54d73240d6b5"
    )
    assert model.schema_version == "iap/java-model/v2"


def test_j5_t09_builder_exception_is_typed_never_silent():
    import investigation_agent_platform.application.analysis.java.model as model_mod

    def boom(*args, **kwargs):
        raise RuntimeError("secret-looking text")

    import pytest

    with pytest.MonkeyPatch().context() as mp:
        mp.setattr(model_mod, "build_java_model", boom)
        analysis = wf.enumerate_java_window_facts(_evidence(_ELSE_IF))
    assert analysis.status == WindowStatus.FAILED
    assert analysis.limitations == ("analysis_failed",)
    assert analysis.failure_type == "RuntimeError"
    assert "secret" not in str(analysis.to_attribute())


def test_j5_t10_grammar_failure_is_typed_distinctly(monkeypatch):
    def boom(*args, **kwargs):
        raise GrammarUnavailable("cold cache")

    monkeypatch.setattr(wf._p, "parse_java", boom)
    analysis = wf.enumerate_java_window_facts(_evidence(_ELSE_IF))
    assert analysis.status == WindowStatus.FAILED
    assert analysis.limitations == ("grammar_unavailable",)
    assert failure_code(GrammarUnavailable("x")) == "grammar_unavailable"
    assert failure_code(ValueError("x")) == "analysis_failed"


def test_j5_t11_provenance_refusal_is_not_failure():
    unnumbered = _evidence(_ELSE_IF, numbered=False)
    analysis = wf.enumerate_java_window_facts(unnumbered)
    assert analysis.status == WindowStatus.REFUSED
    assert analysis.limitations == ("source_range_missing_numbered_lines",)
    unpinned = _evidence(_ELSE_IF, revision="HEAD")
    assert wf.enumerate_java_window_facts(unpinned).status == WindowStatus.REFUSED
    nonjava = _evidence(_ELSE_IF, path="service/normalize.py")
    refused = wf.enumerate_java_window_facts(nonjava)
    assert refused.status == WindowStatus.REFUSED
    assert refused.limitations == ("not_a_java_source_file",)


def test_j5_t12_verifier_names_failed_window_analysis(monkeypatch):
    import investigation_agent_platform.application.analysis.java.model as model_mod

    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(model_mod, "build_java_model", boom)
    ev = _evidence(_ELSE_IF)
    backfilled = VerificationEngine._with_window_backfill([ev])
    assert len(backfilled) == 1
    stamped = backfilled[0].attributes.get(WINDOW_ANALYSIS_KEY)
    assert stamped is not None and stamped["status"] == "failed"
    hyp = _hypothesis("com.sample.orders.OrderServiceHandler.kind")
    partial = VerificationEngine().java_partial_result(hyp, backfilled)
    assert partial is not None
    assert "window analysis failed: analysis_failed" in (partial.explanation or "")


def test_j5_t13_failure_payload_is_valid_for_every_selector_category():
    from investigation_agent_platform.domain.code.models import CodeFactResolutionV2

    categories = [
        JavaFactCategory.CALL_TARGET_FACT,
        JavaFactCategory.SOURCE_VALUE_FACT,
        JavaFactCategory.LANGUAGE_SEMANTIC_FACT,
        JavaFactCategory.DATA_FLOW_FACT,
        JavaFactCategory.CONTROL_FLOW_FACT,
        JavaFactCategory.POSSIBLE_EFFECT_FACT,
        JavaFactCategory.CONFIGURATION_FACT,
    ]
    for category in categories:
        payload = analysis_failed_resolution(
            _evidence(_ELSE_IF),
            category=category,
            subject="OrderServiceHandler.kind",
            exc=ValueError("bad"),
            parser_version="tree-sitter-java",
        )
        validated = CodeFactResolutionV2.model_validate(payload.model_dump(mode="json"))
        assert validated.status == CodeFactStatus.UNRESOLVED
        assert validated.limitation_codes == ("analysis_failed",)
        assert validated.assumptions == ("exception_type=ValueError",)


def test_j5_t14_failure_only_row_adds_no_analyzer_family():
    payload = analysis_failed_resolution(
        _evidence(_ELSE_IF),
        category=JavaFactCategory.SOURCE_VALUE_FACT,
        subject="OrderServiceHandler.kind",
        exc=ValueError("bad"),
        parser_version="tree-sitter-java",
    )
    ev = _evidence(_ELSE_IF)
    row = ev.model_copy(
        update={"attributes": {**ev.attributes, "code_fact_java.local": payload.model_dump(mode="json")}}
    )
    families = VerificationEngine._java_evidence_families([row], None)
    assert "ANALYZER_RESOLUTION" not in families


def test_j5_t15_python_field_chain_reports_failure(monkeypatch):
    import asyncio

    from investigation_agent_platform.application.analysis.python import dispatch as py_dispatch

    def boom(*args, **kwargs):
        raise RuntimeError("chain down")

    monkeypatch.setattr(py_dispatch, "resolve_python_framework_fact", boom)
    monkeypatch.setattr(py_dispatch, "resolve_python_class_attr_fact", boom)
    monkeypatch.setattr(py_dispatch, "resolve_python_value_fact", boom)
    resolution, _ = asyncio.run(
        py_dispatch.resolve_python_field_fact(
            _evidence("x = 1\n", path="service/mod.py"),
            field_name="x",
            declaring_type="Mod",
        )
    )
    assert resolution is not None and resolution.status == CodeFactStatus.UNRESOLVED
    assert "analysis_failed" in resolution.limitation_codes


def test_j5_t16_multi_guard_methods_stay_fail_closed():
    backfilled = VerificationEngine._with_window_backfill([_evidence(_ELSE_IF)])
    for value, symbol_suffix in (
        ("pos", "OrderServiceHandler.kind"),
        ("neg", "OrderServiceHandler.kind"),
    ):
        hyp = _hypothesis(f"com.sample.orders.{symbol_suffix}", f"kind returns {value}")
        hyp.claimed_value = value
        hyp.claim_target.claimed_value = value
        ok, reason = VerificationEngine.java_fact_proof(hyp, backfilled)
        assert not ok
        assert reason == "conflicting matching analyzer resolutions"


def test_j5_t17_lambda_and_anonymous_scopes_emit_no_guard_records():
    analysis = wf.enumerate_java_window_facts(_evidence(_LAMBDA_ANON))
    by_subject = _records_by_subject(analysis)
    assert set(by_subject) == {"com.sample.orders.OrderServiceHandler.clean"}


def test_j5_schema_version_is_v2():
    assert SCHEMA_VERSION == "iap/java-model/v2"
    model = build_java_model(
        _ELSE_IF.encode(),
        file_path=_PATH,
        repository="repo",
        revision="54d73240d6b5",
    )
    assert model.schema_version == "iap/java-model/v2"


def test_j5_registry_covers_new_codes():
    from investigation_agent_platform.application.analysis.limitations import limitation_class

    for code in (
        "enum_constant_not_modeled",
        "switch_return_not_modeled",
        "literal_too_long",
        "grammar_unavailable",
        "analysis_failed",
    ):
        assert limitation_class(code) is not None
