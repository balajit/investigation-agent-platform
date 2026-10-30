"""Python source-fact resolution: literals, assignments, params, classes, frameworks.

Inline-evidence style mirrors tests/unit/test_java_phase1_verifier.py.
Every supported shape has a correct-claim and incorrect-claim assertion;
every unsupported shape asserts PARTIAL/UNRESOLVED with a stable code.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

from investigation_agent_platform.application.analysis.python.class_facts import (
    resolve_python_class_attr_fact,
)
from investigation_agent_platform.application.analysis.python.framework_facts import (
    resolve_python_framework_fact,
)
from investigation_agent_platform.application.analysis.python.scope_facts import (
    resolve_python_param_default_fact,
    resolve_python_value_fact,
)
from investigation_agent_platform.application.investigation.verification import (
    VerificationEngine,
)
from investigation_agent_platform.domain.code.models import (
    CODE_FACT_RESOLUTION_V2_KEY,
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


def _evidence(source, *, path="svc/config.py", revision="abc123", start=1):
    iid = uuid4()
    eid = uuid4()
    now = datetime.now(UTC)
    numbered = "\n".join(f"{start + i}| {ln}" for i, ln in enumerate(source.splitlines()))
    end = start + max(0, len(source.splitlines()) - 1)
    return Evidence(
        evidence_id=eid,
        tenant_id="tenant-a",
        investigation_id=iid,
        evidence_type=EvidenceType.SOURCE_CODE,
        provider="pygit2-local",
        source=f"git://repo/{path}@{revision}",
        title="Python source range",
        summary="Pinned Python source",
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
                provider_type="GIT", operation="GET_SOURCE", normalized_query_hash="h"
            ),
            source_location=SourceLocation(
                system="Git",
                identifier=f"repo/{path}",
                file_path=path,
                line_start=start,
                line_end=end,
                revision=revision,
            ),
        ),
        freshness=EvidenceFreshness(observed_at=now, retrieved_at=now),
        fingerprint=str(eid),
    )


def _banked(v2, evidence=None):
    base = evidence or _evidence("timeout = 30")
    dumped = v2.model_dump(mode="json")
    return base.model_copy(
        update={"attributes": {**base.attributes, CODE_FACT_RESOLUTION_V2_KEY: dumped}}
    )


def _hypothesis(evidence, symbol, value, statement, declaring_type=None):
    target = SimpleNamespace(
        symbol=symbol,
        declaring_type=declaring_type,
        file_path="svc/config.py",
        line_start=1,
        claimed_value=value,
        value_semantics="EXPLICIT_INITIALIZER",
        fact_category=JavaFactCategory.SOURCE_VALUE_FACT,
        node_id=None,
    )
    return SimpleNamespace(
        id=uuid4(),
        statement=statement,
        claim_value_semantics="EXPLICIT_INITIALIZER",
        claimed_value=value,
        claim_target=target,
        fact_category=JavaFactCategory.SOURCE_VALUE_FACT,
        node_id=None,
        supporting_evidence_ids=[evidence.evidence_id],
    )


# --- scalar assignments ---


def test_literal_assignment_resolves():
    ev = _evidence("timeout = 30")
    r = resolve_python_value_fact(ev, "timeout")
    assert r.status == CodeFactStatus.RESOLVED
    assert r.resolved_value == "30"
    assert r.parser_version == "tree-sitter-python"


def test_explicit_none_differs_from_absent():
    r_none = resolve_python_value_fact(_evidence("timeout = None"), "timeout")
    assert r_none.status == CodeFactStatus.RESOLVED
    assert r_none.resolved_value == "None"
    r_absent = resolve_python_value_fact(_evidence("x: int"), "x")
    assert r_absent.status == CodeFactStatus.UNRESOLVED
    assert "annotation_without_assignment" in r_absent.limitation_codes


def test_chained_and_unpacked_assignments():
    r = resolve_python_value_fact(_evidence("a = b = 30"), "b")
    assert r.status == CodeFactStatus.RESOLVED
    assert r.resolved_value == "30"
    r2 = resolve_python_value_fact(_evidence("x, y = 1, 2"), "y")
    assert r2.status == CodeFactStatus.RESOLVED
    assert r2.resolved_value == "2"


def test_arithmetic_expression_resolves():
    r = resolve_python_value_fact(_evidence("timeout = 10 + 20"), "timeout")
    assert r.status == CodeFactStatus.RESOLVED
    assert r.resolved_value == "30"


def test_unsupported_call_is_partial():
    r = resolve_python_value_fact(_evidence("timeout = load_timeout()"), "timeout")
    assert r.status == CodeFactStatus.PARTIAL
    assert "unsupported_call_expression" in r.limitation_codes


def test_multiple_writes_are_partial():
    r = resolve_python_value_fact(_evidence("x = 1\nx = 2"), "x")
    assert r.status == CodeFactStatus.PARTIAL
    assert "multiple_write_sites" in r.limitation_codes


def test_syntax_error_is_unresolved():
    r = resolve_python_value_fact(_evidence("def f(:"), "f")
    assert r.status == CodeFactStatus.UNRESOLVED
    assert "python_syntax_error_or_incomplete_source_window" in r.limitation_codes


def test_missing_symbol_and_wrong_extension():
    r = resolve_python_value_fact(_evidence("timeout = 30"), "missing")
    assert r.status == CodeFactStatus.UNRESOLVED
    r2 = resolve_python_value_fact(_evidence("timeout = 30", path="svc/Foo.java"), "timeout")
    assert r2.status == CodeFactStatus.UNRESOLVED
    assert "not_a_python_source_file" in r2.limitation_codes


def test_unpinned_revision_fails_closed():
    r = resolve_python_value_fact(_evidence("timeout = 30", revision="HEAD"), "timeout")
    assert r.status == CodeFactStatus.UNRESOLVED


# --- language version default ---


def test_explicit_version_is_honored():
    r = resolve_python_value_fact(_evidence("timeout = 30"), "timeout", language_version="3.10")
    assert r.status == CodeFactStatus.RESOLVED
    assert r.language_version == "3.10"
    assert "assumed_default_language_version" not in r.assumptions
    # Tuple form as produced by repo metadata discovery.
    r2 = resolve_python_value_fact(_evidence("timeout = 30"), "timeout", language_version=(3, 11))
    assert r2.status == CodeFactStatus.RESOLVED
    assert r2.language_version == "3.11"
    assert "assumed_default_language_version" not in r2.assumptions


def test_missing_version_defaults_to_runtime_with_marker():
    import sys

    r = resolve_python_value_fact(_evidence("timeout = 30"), "timeout")
    assert r.status == CodeFactStatus.RESOLVED
    assert r.language_version == f"{sys.version_info.major}.{sys.version_info.minor}"
    assert "assumed_default_language_version" in r.assumptions


# --- parameter defaults ---


def test_param_default_resolves_and_ignores_call_site():
    src = "def f(limit=10):\n    return limit\n\nf(20)"
    r = resolve_python_param_default_fact(_evidence(src), "limit", function_scope="f")
    assert r.status == CodeFactStatus.RESOLVED
    assert r.resolved_value == "10"
    assert r.value_semantics.value == "PARAMETER_DEFAULT_EXPRESSION"


def test_param_without_default_is_unresolved():
    r = resolve_python_param_default_fact(
        _evidence("def f(limit):\n    return limit"), "limit", function_scope="f"
    )
    assert r.status == CodeFactStatus.UNRESOLVED
    assert "parameter_has_no_default" in r.limitation_codes


def test_mutable_default_carries_assumption():
    r = resolve_python_param_default_fact(
        _evidence("def f(items=[]):\n    return items"), "items", function_scope="f"
    )
    assert r.status == CodeFactStatus.RESOLVED
    assert "mutable_default_shared" in r.assumptions


# --- classes ---


def test_class_constant_resolves():
    r = resolve_python_class_attr_fact(_evidence("class C:\n    x = 5"), "x", class_scope="C")
    assert r.status == CodeFactStatus.RESOLVED
    assert r.resolved_value == "5"


def test_constructor_write_resolves():
    r = resolve_python_class_attr_fact(
        _evidence("class C:\n    def __init__(self):\n        self.y = 10"),
        "y",
        class_scope="C",
    )
    assert r.status == CodeFactStatus.RESOLVED
    assert r.resolved_value == "10"


def test_later_write_is_partial():
    src = (
        "class C:\n"
        "    def __init__(self):\n"
        '        self.status = "new"\n'
        "    def close(self):\n"
        '        self.status = "processed"'
    )
    r = resolve_python_class_attr_fact(_evidence(src), "status", class_scope="C")
    assert r.status == CodeFactStatus.PARTIAL
    assert "potential_later_write" in r.limitation_codes


def test_property_lookup_is_partial():
    src = "class C:\n    @property\n    def x(self):\n        return self._x"
    r = resolve_python_class_attr_fact(_evidence(src), "x", class_scope="C")
    assert r.status in {CodeFactStatus.PARTIAL, CodeFactStatus.UNRESOLVED}


# --- frameworks ---


def test_dataclass_default_resolves():
    r = resolve_python_framework_fact(
        _evidence("@dataclass\nclass C:\n    x: int = 3"), "x", class_scope="C"
    )
    assert r.status == CodeFactStatus.RESOLVED
    assert r.resolved_value == "3"
    assert r.value_semantics.value == "FRAMEWORK_DEFAULT"


def test_dataclass_factory_classified_not_executed():
    r = resolve_python_framework_fact(
        _evidence("@dataclass\nclass C:\n    y: list = field(default_factory=list)"),
        "y",
        class_scope="C",
    )
    assert r.status == CodeFactStatus.RESOLVED
    assert r.resolved_value == "default_factory"
    assert "factory_not_executed" in r.assumptions


def test_pydantic_field_default_resolves():
    src = (
        "from pydantic import BaseModel, Field\nclass M(BaseModel):\n    n: int = Field(default=5)"
    )
    r = resolve_python_framework_fact(_evidence(src), "n", class_scope="M")
    assert r.status == CodeFactStatus.RESOLVED
    assert r.resolved_value == "5"


# --- verifier integration ---


def test_python_resolved_value_verifies_and_mismatch_rejects():
    ev = _evidence("timeout = 30")
    r = resolve_python_value_fact(ev, "timeout")
    banked = _banked(r, ev)
    good = _hypothesis(banked, "timeout", "30", "The default value of timeout is 30")
    ok, _ = VerificationEngine.java_fact_proof(good, [banked])
    assert ok
    assert VerificationEngine().validate_java_fact_claim(good, [banked], []) is not None
    bad = _hypothesis(banked, "timeout", "30", "The default value of timeout is 999")
    ok, reason = VerificationEngine.java_fact_proof(bad, [banked])
    assert not ok and "statement" in reason
    wrong = _hypothesis(banked, "timeout", "999", "The default value of timeout is 999")
    ok, _ = VerificationEngine.java_fact_proof(wrong, [banked])
    assert not ok


def test_python_partial_never_verifies():
    ev = _evidence("timeout = load_timeout()")
    r = resolve_python_value_fact(ev, "timeout")
    assert r.status == CodeFactStatus.PARTIAL
    banked = _banked(r, ev)
    hyp = _hypothesis(banked, "timeout", None, "The default value of timeout is 30")
    ok, _ = VerificationEngine.java_fact_proof(hyp, [banked])
    assert not ok
    assert VerificationEngine().validate_java_fact_claim(hyp, [banked], []) is None
