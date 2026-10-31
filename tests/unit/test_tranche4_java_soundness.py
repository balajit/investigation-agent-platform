# tests/unit/test_tranche4_java_soundness.py
"""Tranche 4 Java soundness (review J1, J2, J3). Synthetic sources only.

Each test pins the unsound behavior the review demonstrated: guards and
fields credited to the wrong host, and definite assignment decided by text
order. Fixtures are written to fail before the fix.
"""

from __future__ import annotations

from test_java_dataflow import _source_evidence as _flow_evidence
from test_java_window_facts import _evidence as _window_evidence

from investigation_agent_platform.application.analysis.java import model as _model
from investigation_agent_platform.application.analysis.java.dataflow import (
    resolve_java_data_flow,
)
from investigation_agent_platform.application.analysis.java.window_facts import (
    enumerate_java_window_facts,
)
from investigation_agent_platform.domain.code.models import CodeFactStatus


def _facts(source: str):  # type: ignore[no-untyped-def]
    return enumerate_java_window_facts(_window_evidence(source)).records


def _pairs(records):  # type: ignore[no-untyped-def]
    return {(r.subject_identity, r.resolved_value) for r in records}


_LAMBDA = """package com.sample;
public class A {
    String foo(java.util.List<String> xs) {
        java.util.function.Predicate<String> p = s -> {
            if (s.isEmpty()) return false;
            return true;
        };
        return compute();
    }
}
"""

_ANON_METHOD = """package com.sample;
public class A {
    String foo() {
        java.util.function.Supplier<String> s = new java.util.function.Supplier<String>() {
            public String get() {
                if (z) return "inner";
                return "x";
            }
        };
        return compute();
    }
}
"""

_NESTED_CLASS = """package com.sample;
public class A {
    String foo() {
        class Local {
            String bar() {
                if (q) return "local";
                return "x";
            }
        }
        return compute();
    }
}
"""

_OWN_GUARD = """package com.sample;
public class A {
    String foo(boolean c) {
        if (c) { return "own"; }
        return compute();
    }
}
"""


class TestJ1GuardReturnsStayWithTheirHost:
    def test_lambda_guard_is_not_credited_to_enclosing_method(self) -> None:
        pairs = _pairs(_facts(_LAMBDA))
        assert ("com.sample.A.foo", "false") not in pairs, pairs

    def test_anonymous_class_method_guard_is_not_credited_to_enclosing_method(self) -> None:
        pairs = _pairs(_facts(_ANON_METHOD))
        assert not any(value == "inner" for _, value in pairs), (
            f"anonymous-class guard emitted under a named method: {pairs}"
        )

    def test_local_class_method_guard_is_not_credited_to_enclosing_method(self) -> None:
        pairs = _pairs(_facts(_NESTED_CLASS))
        assert not any(subject == "com.sample.A.foo" and value == "local" for subject, value in pairs)

    def test_model_guard_list_excludes_lambda_branch(self) -> None:
        model = _model.build_java_model(
            _LAMBDA.encode(), file_path="A.java", repository="r", revision="abc"
        )
        host_branches = [g for g in model.guard_returns if g.method_name == "foo"]
        assert host_branches == [], "lambda body guard leaked into foo's guard list"

    def test_a_methods_own_guard_is_still_emitted(self) -> None:
        pairs = _pairs(_facts(_OWN_GUARD))
        assert ("com.sample.A.foo", "own") in pairs


_ANON_FIELD = """package com.sample;
public class A {
    Runnable r = new Runnable() {
        int retries = 3;
        public void run() {}
    };
    int retries = 5;
}
"""


class TestJ2AnonymousScopeIsDistinct:
    def test_anonymous_field_does_not_shadow_outer_field(self) -> None:
        pairs = _pairs(_facts(_ANON_FIELD))
        retries = {value for subject, value in pairs if subject == "com.sample.A.retries"} | {
            value for subject, value in pairs if subject == "A.retries"
        }
        assert retries == {"5"}, f"anonymous-class field leaked into A.retries: {pairs}"

    def test_model_gives_anonymous_field_its_own_enclosing_scope(self) -> None:
        model = _model.build_java_model(
            _ANON_FIELD.encode(), file_path="A.java", repository="r", revision="abc"
        )
        enclosing = {f.enclosing_class for f in model.fields if f.name == "retries"}
        assert len(enclosing) == 2, f"both retries fields share one scope: {enclosing}"

    def test_conflicting_resolved_values_are_unestablished(self) -> None:
        from types import SimpleNamespace

        from investigation_agent_platform.application.investigation.verification import (
            VerificationEngine,
        )
        from investigation_agent_platform.domain.code.models import (
            CodeFactResolutionV2,
            CodeValueSemantics,
            JavaFactCategory,
        )
        from investigation_agent_platform.domain.provenance.models import SourceLocation

        def _record(value: str) -> dict:
            return CodeFactResolutionV2(
                status=CodeFactStatus.RESOLVED,
                repository="r",
                revision="abc",
                fact_category=JavaFactCategory.SOURCE_VALUE_FACT,
                declaring_type="A",
                subject_identity="A.retries",
                field_name="retries",
                value_semantics=CodeValueSemantics.EXPLICIT_INITIALIZER,
                resolved_value=value,
                declaration_location=SourceLocation(
                    system="Git",
                    identifier="r/A.java",
                    file_path="A.java",
                    line_start=1,
                    line_end=1,
                    revision="abc",
                ),
            ).model_dump(mode="json")

        rows = [
            SimpleNamespace(attributes={"code_fact_resolution_v2": _record("3")}),
            SimpleNamespace(attributes={"code_fact_resolution_v2": _record("5")}),
        ]
        picked = VerificationEngine._v2_any_from_evidence(
            rows, JavaFactCategory.SOURCE_VALUE_FACT, "A.retries"
        )
        assert picked is None, "two RESOLVED values for one field must not pick the first"

    def test_agreeing_resolved_values_are_still_established(self) -> None:
        from types import SimpleNamespace

        from investigation_agent_platform.application.investigation.verification import (
            VerificationEngine,
        )
        from investigation_agent_platform.domain.code.models import (
            CodeFactResolutionV2,
            CodeValueSemantics,
            JavaFactCategory,
        )
        from investigation_agent_platform.domain.provenance.models import SourceLocation

        record = CodeFactResolutionV2(
            status=CodeFactStatus.RESOLVED,
            repository="r",
            revision="abc",
            fact_category=JavaFactCategory.SOURCE_VALUE_FACT,
            declaring_type="A",
            subject_identity="A.retries",
            field_name="retries",
            value_semantics=CodeValueSemantics.EXPLICIT_INITIALIZER,
            resolved_value="5",
            declaration_location=SourceLocation(
                    system="Git",
                    identifier="r/A.java",
                    file_path="A.java",
                    line_start=1,
                    line_end=1,
                    revision="abc",
                ),
        ).model_dump(mode="json")
        rows = [SimpleNamespace(attributes={"code_fact_resolution_v2": record})] * 2
        picked = VerificationEngine._v2_any_from_evidence(
            rows, JavaFactCategory.SOURCE_VALUE_FACT, "A.retries"
        )
        assert picked is not None and picked.resolved_value == "5"


def _flow(body: str, symbol: str = "x"):  # type: ignore[no-untyped-def]
    source = "class R {\n  int m(boolean c) {\n" + body + "\n  }\n}\n"
    return resolve_java_data_flow(_flow_evidence(source), symbol)


class TestJ3DefiniteAssignmentNeedsControlFlow:
    def test_conditional_assignment_before_use_is_not_definite(self) -> None:
        # javac rejects this program: x is not definitely assigned at the return.
        result = _flow("    int x;\n    if (c) { x = 1; }\n    return x;")
        assert result.status != CodeFactStatus.RESOLVED
        assert "control_flow_not_modeled" in result.limitation_codes

    def test_initializer_then_conditional_overwrite_is_not_a_single_reaching_def(self) -> None:
        result = _flow("    int y = 0;\n    if (c) { y = 5; }\n    return y;", symbol="y")
        assert result.status != CodeFactStatus.RESOLVED
        assert "control_flow_not_modeled" in result.limitation_codes

    def test_loop_assignment_is_not_definite(self) -> None:
        result = _flow("    int x;\n    for (int i = 0; i < 3; i++) { x = i; }\n    return x;")
        assert result.status != CodeFactStatus.RESOLVED
        assert "control_flow_not_modeled" in result.limitation_codes

    def test_try_assignment_is_not_definite(self) -> None:
        result = _flow(
            "    int x;\n    try { x = compute(); } catch (RuntimeException e) { x = -1; }\n    return x;"
        )
        assert result.status != CodeFactStatus.RESOLVED
        assert "control_flow_not_modeled" in result.limitation_codes

    def test_straight_line_assignment_is_still_definite(self) -> None:
        result = _flow("    int x;\n    x = 1;\n    return x;")
        assert result.status == CodeFactStatus.RESOLVED
        assert result.structured_fact["definite_assignment"] == "established"

    def test_initializer_with_nested_read_is_still_definite(self) -> None:
        # The def dominates every use; a read inside a branch is fine.
        result = _flow("    int x = 1;\n    if (c) { int z = x + 1; }\n    return x;")
        assert result.status == CodeFactStatus.RESOLVED

    def test_declaration_and_assignment_inside_the_same_branch_stay_definite(self) -> None:
        result = _flow(
            "    if (c) {\n      int t;\n      t = 4;\n      return t;\n    }\n    return 0;",
            symbol="t",
        )
        assert result.status == CodeFactStatus.RESOLVED
