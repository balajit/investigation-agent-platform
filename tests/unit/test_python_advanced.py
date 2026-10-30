"""Python advanced facts: imports, MRO/override, control flow, write sites, config."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

from investigation_agent_platform.application.analysis.python.config import (
    resolve_python_config_fact,
)
from investigation_agent_platform.application.analysis.python.control import (
    resolve_python_control_fact,
    resolve_python_write_sites_fact,
)
from investigation_agent_platform.application.analysis.python.imports import (
    resolve_python_import_fact,
)
from investigation_agent_platform.application.analysis.python.mro import (
    resolve_python_method_owner_fact,
    resolve_python_mro_fact,
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


def _evidence(source, *, path="svc/mod.py", revision="abc123"):
    iid = uuid4()
    eid = uuid4()
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
                line_start=1,
                line_end=end,
                revision=revision,
            ),
        ),
        freshness=EvidenceFreshness(observed_at=now, retrieved_at=now),
        fingerprint=str(eid),
    )


def _banked(v2, evidence, extra_attrs=None):
    attrs = {**evidence.attributes, CODE_FACT_RESOLUTION_V2_KEY: v2.model_dump(mode="json")}
    if extra_attrs:
        attrs.update(extra_attrs)
    return evidence.model_copy(update={"attributes": attrs})


# --- imports ---


def test_import_alias_resolves():
    r = resolve_python_import_fact(_evidence("import a.b as c"), "c")
    assert r.status == CodeFactStatus.RESOLVED
    assert r.resolved_value == "a.b"


def test_from_import_alias_resolves():
    r = resolve_python_import_fact(_evidence("from m import n as c"), "c")
    assert r.status == CodeFactStatus.RESOLVED
    assert r.resolved_value == "m.n"


def test_conditional_import_is_partial():
    r = resolve_python_import_fact(
        _evidence("try:\n    import opt\nexcept ImportError:\n    pass"), "opt"
    )
    assert r.status == CodeFactStatus.PARTIAL
    assert "conditional_import_may_not_execute" in r.limitation_codes


def test_dynamic_import_is_partial():
    r = resolve_python_import_fact(
        _evidence('import importlib\nmod = importlib.import_module("x")'), "mod"
    )
    assert r.status == CodeFactStatus.PARTIAL
    assert "dynamic_import_requires_runtime" in r.limitation_codes


def test_missing_import_binding_unresolved():
    r = resolve_python_import_fact(_evidence("import os"), "missing")
    assert r.status == CodeFactStatus.UNRESOLVED


# --- rebinding and deletion ---


def test_augmented_assignment_counts_as_rewrite():
    from investigation_agent_platform.application.analysis.python.scope_facts import (
        resolve_python_value_fact,
    )

    r = resolve_python_value_fact(_evidence("x = 1\nx += 2"), "x")
    assert r.status == CodeFactStatus.PARTIAL
    assert "multiple_write_sites" in r.limitation_codes


def test_loop_only_binding_has_no_baseline():
    from investigation_agent_platform.application.analysis.python.scope_facts import (
        resolve_python_value_fact,
    )

    r = resolve_python_value_fact(_evidence("for i in range(3):\n    pass"), "i")
    assert r.status == CodeFactStatus.PARTIAL
    assert "rebinding_without_baseline" in r.limitation_codes


def test_unconditional_del_unbinds():
    from investigation_agent_platform.application.analysis.python.scope_facts import (
        resolve_python_value_fact,
    )

    r = resolve_python_value_fact(_evidence("x = 5\ndel x"), "x")
    assert r.status == CodeFactStatus.UNRESOLVED
    assert "name_deleted" in r.limitation_codes


def test_conditional_del_is_partial():
    from investigation_agent_platform.application.analysis.python.scope_facts import (
        resolve_python_value_fact,
    )

    r = resolve_python_value_fact(_evidence("x = 5\nif True:\n    del x"), "x")
    assert r.status == CodeFactStatus.PARTIAL
    assert "possible_deletion" in r.limitation_codes


def test_initvar_skipped_by_value_and_class_resolvers():
    from investigation_agent_platform.application.analysis.python.class_facts import (
        resolve_python_class_attr_fact,
    )
    from investigation_agent_platform.application.analysis.python.scope_facts import (
        resolve_python_value_fact,
    )

    src = "from dataclasses import InitVar\n\nclass C:\n    tmp: InitVar[int] = 1\n"
    r = resolve_python_class_attr_fact(_evidence(src), "tmp", class_scope="C")
    assert r.status == CodeFactStatus.UNRESOLVED
    r2 = resolve_python_value_fact(_evidence(src), "tmp")
    assert r2.status == CodeFactStatus.UNRESOLVED


def test_relative_bare_import_binds():
    r = resolve_python_import_fact(_evidence("from . import sibling"), "sibling")
    assert r.status == CodeFactStatus.RESOLVED
    assert r.resolved_value == ".sibling"


def test_deleted_name_claim_never_verifies():
    from investigation_agent_platform.application.analysis.python.scope_facts import (
        resolve_python_value_fact,
    )

    ev = _evidence("x = 5\ndel x")
    r = resolve_python_value_fact(ev, "x")
    banked = _banked(r, ev)
    hyp = _hypothesis(banked, "x", "5", "x is 5", JavaFactCategory.SOURCE_VALUE_FACT)
    ok, _ = VerificationEngine.java_fact_proof(hyp, [banked])
    assert not ok


# --- MRO ---


def test_single_inheritance_mro():
    src = "class Base:\n    pass\n\nclass Sub(Base):\n    pass\n"
    r = resolve_python_mro_fact(_evidence(src), "Sub")
    assert r.status == CodeFactStatus.RESOLVED
    assert r.structured_fact == {"mro": ["Sub", "Base", "object"]}


def test_diamond_mro_c3_order():
    src = "class A:\n    pass\nclass B(A):\n    pass\nclass C(A):\n    pass\nclass D(B, C):\n    pass\n"
    r = resolve_python_mro_fact(_evidence(src), "D")
    assert r.status == CodeFactStatus.RESOLVED
    assert r.structured_fact == {"mro": ["D", "B", "C", "A", "object"]}


def test_unknown_base_is_partial():
    src = "from ext import Base\n\nclass Sub(Base):\n    pass\n"
    r = resolve_python_mro_fact(_evidence(src), "Sub")
    assert r.status == CodeFactStatus.PARTIAL
    assert "cross_module_base_class" in r.limitation_codes


def test_method_owner_prefers_override():
    src = "class Base:\n    def handle(self):\n        return 1\nclass Sub(Base):\n    def handle(self):\n        return 2\n"
    r = resolve_python_method_owner_fact(_evidence(src), "handle", class_scope="Sub")
    assert r.status == CodeFactStatus.RESOLVED
    assert r.resolved_value == "Sub.handle"
    assert "runtime_receiver_unknown" in r.assumptions


def test_method_owner_falls_back_to_base():
    src = "class Base:\n    def handle(self):\n        return 1\nclass Sub(Base):\n    pass\n"
    r = resolve_python_method_owner_fact(_evidence(src), "handle", class_scope="Sub")
    assert r.status == CodeFactStatus.RESOLVED
    assert r.resolved_value == "Base.handle"


def test_method_owner_unknown_base_partial():
    src = "from ext import Base\n\nclass Sub(Base):\n    pass\n"
    r = resolve_python_method_owner_fact(_evidence(src), "handle", class_scope="Sub")
    assert r.status == CodeFactStatus.PARTIAL
    assert "cross_module_base_class" in r.limitation_codes


def test_method_missing_from_mro():
    src = "class Base:\n    pass\nclass Sub(Base):\n    pass\n"
    r = resolve_python_method_owner_fact(_evidence(src), "nope", class_scope="Sub")
    assert r.status == CodeFactStatus.UNRESOLVED


# --- write sites ---


def test_module_write_sites_partial():
    r = resolve_python_write_sites_fact(_evidence("x = 1\nx = 2"), "x")
    assert r.status == CodeFactStatus.PARTIAL
    sites = r.structured_fact["write_sites"]
    assert sites == ["x@1", "x@2"]
    assert "multiple_write_sites" in r.limitation_codes


def test_class_write_sites_name_methods():
    src = 'class C:\n    def __init__(self):\n        self.s = "new"\n    def close(self):\n        self.s = "done"\n'
    r = resolve_python_write_sites_fact(_evidence(src), "s", class_scope="C")
    assert r.status == CodeFactStatus.PARTIAL
    assert r.structured_fact["write_sites"] == ["C.s@3:__init__", "C.s@5:close"]


def test_write_sites_missing_unresolved():
    r = resolve_python_write_sites_fact(_evidence("x = 1"), "missing")
    assert r.status == CodeFactStatus.UNRESOLVED


# --- control flow ---


def test_control_inventory_partial():
    src = "def f(x):\n    if x:\n        return 1\n    return 0\n"
    r = resolve_python_control_fact(_evidence(src), "f")
    assert r.status == CodeFactStatus.PARTIAL
    assert r.fact_category == JavaFactCategory.CONTROL_FLOW_FACT
    assert any(v.startswith("return 1@") for v in r.structured_fact["returns"])
    assert any("if_statement" in b for b in r.structured_fact["branches"])


def test_control_branch_hint_filters():
    src = "def f(x):\n    if x:\n        return 1\n    return 0\n"
    r = resolve_python_control_fact(_evidence(src), "f", branch_hint="if")
    assert r.status == CodeFactStatus.PARTIAL
    r2 = resolve_python_control_fact(_evidence(src), "f", branch_hint="zzz-no-match")
    assert r2.status == CodeFactStatus.UNRESOLVED
    assert "branch_hint_not_found" in r2.limitation_codes


def test_control_async_and_generator_marked():
    src = "async def f():\n    yield 1\n"
    r = resolve_python_control_fact(_evidence(src), "f")
    assert r.status == CodeFactStatus.PARTIAL
    assert "async_function" in r.limitation_codes
    assert "generator_function" in r.limitation_codes


# --- config ---


def test_getenv_default_resolves():
    r = resolve_python_config_fact(
        _evidence('import os\nTIMEOUT = os.getenv("TIMEOUT", "30")'), "TIMEOUT"
    )
    assert r.status == CodeFactStatus.RESOLVED
    assert r.fact_category == JavaFactCategory.CONFIGURATION_FACT
    assert r.resolved_value == "30"
    assert r.structured_fact["env_keys"] == ["TIMEOUT"]
    assert "effective_value_requires_runtime" in r.assumptions


def test_environ_subscript_without_default_partial():
    r = resolve_python_config_fact(_evidence('import os\nKEY = os.environ["KEY"]'), "KEY")
    assert r.status == CodeFactStatus.PARTIAL
    assert "undeclared_default_requires_runtime" in r.limitation_codes


def test_plain_assignment_not_config():
    r = resolve_python_config_fact(_evidence("timeout = 30"), "timeout")
    assert r.status == CodeFactStatus.UNRESOLVED
    assert "not_a_config_declaration" in r.limitation_codes


# --- verifier integration ---


def _hypothesis(evidence, symbol, value, statement, category, declaring_type=None):
    target = SimpleNamespace(
        symbol=symbol,
        declaring_type=declaring_type,
        file_path="svc/mod.py",
        line_start=1,
        claimed_value=value,
        value_semantics="EXPLICIT_INITIALIZER",
        fact_category=category,
        node_id=None,
    )
    return SimpleNamespace(
        id=uuid4(),
        statement=statement,
        claim_value_semantics="EXPLICIT_INITIALIZER",
        claimed_value=value,
        claim_target=target,
        fact_category=category,
        node_id=None,
        supporting_evidence_ids=[evidence.evidence_id],
    )


def test_import_declaration_verifies():
    ev = _evidence("from m import n as c")
    r = resolve_python_import_fact(ev, "c")
    banked = _banked(r, ev)
    hyp = _hypothesis(
        banked, "c", "m.n", "c is imported from m.n", JavaFactCategory.DECLARATION_FACT
    )
    ok, _ = VerificationEngine.java_fact_proof(hyp, [banked])
    assert ok
    assert VerificationEngine().validate_java_fact_claim(hyp, [banked], []) is not None


def test_mro_semantic_verifies():
    ev = _evidence("class Base:\n    pass\n\nclass Sub(Base):\n    pass\n")
    r = resolve_python_mro_fact(ev, "Sub")
    banked = _banked(r, ev)
    hyp = _hypothesis(
        banked,
        "Sub",
        None,
        "Sub inherits Base then object",
        JavaFactCategory.LANGUAGE_SEMANTIC_FACT,
    )
    ok, _ = VerificationEngine.java_fact_proof(hyp, [banked])
    assert ok


def test_config_verifies_at_likely_with_source():
    ev = _evidence('import os\nTIMEOUT = os.getenv("TIMEOUT", "30")')
    r = resolve_python_config_fact(ev, "TIMEOUT")
    banked = _banked(r, ev, {"configuration_source": "svc/mod.py"})
    hyp = _hypothesis(
        banked,
        "TIMEOUT",
        "30",
        "TIMEOUT defaults to 30 unless overridden",
        JavaFactCategory.CONFIGURATION_FACT,
    )
    ok, _ = VerificationEngine.java_fact_proof(hyp, [banked])
    assert ok
    result = VerificationEngine().validate_java_fact_claim(hyp, [banked], [])
    assert result is not None and result.tier == "likely"
    # Without the configuration source family the claim must not verify.
    bare = _banked(r, ev)
    ok, _ = VerificationEngine.java_fact_proof(hyp, [bare])
    assert not ok


def test_partial_write_sites_never_verify():
    ev = _evidence("x = 1\nx = 2")
    r = resolve_python_write_sites_fact(ev, "x")
    banked = _banked(r, ev)
    hyp = _hypothesis(banked, "x", "2", "x is 2", JavaFactCategory.SOURCE_VALUE_FACT)
    ok, _ = VerificationEngine.java_fact_proof(hyp, [banked])
    assert not ok
