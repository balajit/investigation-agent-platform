# tests/unit/test_jls_semantic_ceiling.py
"""JLS: LANGUAGE_SEMANTIC_FACT verifies at VERIFIED (decision record).

The category is "what the language rules say about declared source", which
source plus the rule version proves — not a claim about what a run did. The
change is a docstring plus pins: the ceiling table, the execution-disclaiming
assumptions on every RESOLVED producer record, and PARTIAL for out-of-window
input. No ceiling change without evidence of a false VERIFIED.
"""

from __future__ import annotations

from test_java_boundaries import BANNED_WORDING


def test_jls_t01_ceiling_table_pinned() -> None:
    from investigation_agent_platform.application.investigation.verification import (
        CATEGORY_PROOF_RULES,
    )
    from investigation_agent_platform.domain.code.models import JavaFactCategory as C

    ceilings = {cat: rule.max_verdict for cat, rule in CATEGORY_PROOF_RULES.items()}
    assert ceilings[C.DECLARATION_FACT] == "VERIFIED"
    assert ceilings[C.SOURCE_VALUE_FACT] == "VERIFIED"
    assert ceilings[C.LANGUAGE_SEMANTIC_FACT] == "VERIFIED"
    for capped in (
        C.CONTROL_FLOW_FACT,
        C.DATA_FLOW_FACT,
        C.CALL_TARGET_FACT,
        C.POSSIBLE_EFFECT_FACT,
        C.CONFIGURATION_FACT,
    ):
        assert ceilings[capped] == "LIKELY", capped


def _no_applied_wording(record) -> None:  # type: ignore[no-untyped-def]
    payload = record.model_dump_json().lower()
    for banned in BANNED_WORDING:
        assert banned not in payload, banned


def test_jls_t02_resolved_records_disclaim_execution() -> None:
    from test_java_flow import _source_evidence as _java_evidence
    from test_java_init import _BOUNDED
    from test_java_init import _source_evidence as _init_evidence
    from test_python_advanced import _evidence as _py_evidence

    from investigation_agent_platform.application.analysis.java.init_facts import (
        resolve_java_init_fact,
    )
    from investigation_agent_platform.application.analysis.java.type_facts import (
        resolve_java_type_fact,
    )
    from investigation_agent_platform.application.analysis.python.mro import (
        resolve_python_mro_fact,
    )
    from investigation_agent_platform.domain.code.models import CodeFactStatus

    type_rec = resolve_java_type_fact(
        _java_evidence("record Point(int x, int y) {}"), "Point", language_version=17
    )
    init_rec = resolve_java_init_fact(_init_evidence(_BOUNDED), "Widget")
    mro_rec = resolve_python_mro_fact(
        _py_evidence("class Base:\n    pass\n\nclass Sub(Base):\n    pass\n"), "Sub"
    )
    for rec in (type_rec, init_rec, mro_rec):
        assert rec.status == CodeFactStatus.RESOLVED
        assert rec.assumptions, "a RESOLVED semantic record must disclaim execution"
        _no_applied_wording(rec)


def test_jls_t03_out_of_window_input_is_partial() -> None:
    from test_java_init import _source_evidence

    from investigation_agent_platform.application.analysis.java.init_facts import (
        resolve_java_init_fact,
    )
    from investigation_agent_platform.domain.code.models import CodeFactStatus

    source = "class Child extends Base {\n  Child() {\n    super(1);\n  }\n}"
    result = resolve_java_init_fact(_source_evidence(source), "Child")
    assert result.status == CodeFactStatus.PARTIAL
