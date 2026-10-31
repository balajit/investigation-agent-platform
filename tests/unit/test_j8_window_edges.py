"""J8: window-enumeration edges (synthetic only).

Covers D12 (one splitter), D13 (subject-first fair budget), D14 (no bare
subjects, inferred owners labeled), D15 (byte vs code-point columns).
No analyzed repository is referenced; the T02/T13 repos are synthetic.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from investigation_agent_platform.application.analysis.java import window_facts as wf
from investigation_agent_platform.application.analysis.java.model import (
    build_java_model,
    code_point_column,
)
from investigation_agent_platform.application.analysis.java.window_result import (
    WindowStatus,
)
from investigation_agent_platform.application.investigation.verification import (
    VerificationEngine,
)
from investigation_agent_platform.domain.code.lines import split_source_lines
from investigation_agent_platform.domain.code.models import (
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


def _evidence(source, *, path=_PATH, revision="54d73240d6b5", numbered=True):
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


def _hypothesis(symbol, statement="typed claim"):
    return SimpleNamespace(
        id=uuid4(),
        statement=statement,
        claimed_value=None,
        claim_target=SimpleNamespace(symbol=symbol, claimed_value=None),
        fact_category=JavaFactCategory.SOURCE_VALUE_FACT,
        supporting_evidence_ids=[],
    )


def test_j8_t01_splitter_table():
    assert split_source_lines("") == []
    assert split_source_lines("a") == ["a"]
    assert split_source_lines("a\n") == ["a"]
    assert split_source_lines("\n") == [""]
    assert split_source_lines("a\r\nb") == ["a", "b"]
    assert split_source_lines("a\x0cb\nc") == ["a\x0cb", "c"]
    assert split_source_lines("a b\n") == ["a b"]
    assert split_source_lines("a\rb\n") == ["a\rb"]


def test_j8_t02_provider_numbering_equals_git_numbering(tmp_path):
    import asyncio
    from unittest.mock import MagicMock

    import pygit2

    from investigation_agent_platform.infrastructure.evidence.code.git import (
        PyGit2Adapter,
    )

    repo_path = tmp_path / "o1" / "r1"
    repo_path.mkdir(parents=True)
    repo = pygit2.init_repository(str(repo_path))
    content = "class A {\n\x0c\n  int a = 1;\n  String s = \"x y\";\n  int b = 2;\n}\n"
    target = repo_path / "A.java"
    target.write_text(content, encoding="utf-8")
    repo.index.add("A.java")
    repo.index.write()
    author = pygit2.Signature("t", "t@example.com")
    sha = str(repo.create_commit("HEAD", author, author, "init", repo.index.write_tree(), []))
    code = MagicMock()
    code.org = "o1"
    code.repos = ["r1"]
    code.default_branch = "main"
    code.source_roots = []
    code.paths = []
    svc = MagicMock()
    svc.code = code
    svc.name = "svc"
    profile = MagicMock()
    profile.services = [svc]
    adapter = PyGit2Adapter(repo_base_path=str(tmp_path))
    row = asyncio.run(
        adapter.get_source(
            tenant_id="t",
            investigation_id=uuid4(),
            file_path="A.java",
            profile=profile,
            revision=sha,
            start_line=5,
            end_line=5,
        )
    )
    assert "5|" in (row.content_snippet or "") and "int b = 2;" in (row.content_snippet or "")


def test_j8_t03_analyzers_accept_exotic_separators():
    from investigation_agent_platform.application.analysis.java._provenance import (
        java_source_lines,
    )
    from investigation_agent_platform.application.analysis.python._provenance import (
        python_source_lines,
    )

    assert java_source_lines(_evidence("class A {\n\x0c\n}\n"))[1] is None
    assert java_source_lines(_evidence('  String s = "x y";\n'))[1] is None
    py = _evidence("x = 1\n\x0c\n", path="service/mod.py")
    assert python_source_lines(py)[1] is None


def test_j8_t04_topology_line_indexing(tmp_path):
    import asyncio

    from investigation_agent_platform.infrastructure.topology.payload import (
        build_payload_for_repo,
    )

    src = "class A {\n  String s = \"x y\";\n  void m() {\n    foo();\n  }\n  void foo() {}\n}\n"
    target = tmp_path / "A.java"
    target.write_text(src, encoding="utf-8")
    payload, _ = asyncio.run(
        build_payload_for_repo(
            checkout=tmp_path,
            tenant_id="t",
            application_id="app",
            repository_id="r",
            sha="abc",
        )
    )
    edges = [e for e in payload.calls if e.call_site_line == 4]
    assert edges, "the foo() call site must be reported on git line 4"
    assert all(e.call_site_file.endswith("A.java") for e in edges)


def test_j8_t05_no_stray_splitlines_on_snippet_paths():
    modules = [
        "src/investigation_agent_platform/infrastructure/evidence/code/git.py",
        "src/investigation_agent_platform/application/analysis/java/_provenance.py",
        "src/investigation_agent_platform/application/analysis/python/_provenance.py",
        "src/investigation_agent_platform/application/investigation/verification.py",
        "src/investigation_agent_platform/domain/hypothesis/templates.py",
        "src/investigation_agent_platform/application/investigation/context_compiler.py",
        "src/investigation_agent_platform/application/worker/activities/execution.py",
        "src/investigation_agent_platform/infrastructure/topology/payload.py",
        "src/investigation_agent_platform/infrastructure/evidence/code/pipeline.py",
    ]
    root = Path(__file__).resolve().parents[2]
    offenders = []
    for module in modules:
        text = (root / module).read_text()
        for i, line in enumerate(text.splitlines(), 1):
            if ".splitlines()" in line:
                offenders.append(f"{module}:{i}")
    assert not offenders, f"stray splitlines on snippet paths: {offenders}"


def _budget_source():
    methods = "".join(
        f"    String m{i}(int v) {{\n        if (v > {i}) {{\n            return \"v{i}\";\n        }}\n        return \"z\";\n    }}\n"
        for i in range(12)
    )
    return (
        "package com.sample.orders;\n"
        "public class OrderServiceHandler {\n"
        f"{methods}"
        '    private String tail = "LATE";\n'
        "}\n"
    )


def test_j8_t06_budget_is_subject_first(monkeypatch):
    monkeypatch.setattr(wf, "MAX_WINDOW_RECORDS", 4)
    analysis = wf.enumerate_java_window_facts(
        _evidence(_budget_source()), subject_hint="OrderServiceHandler.tail"
    )
    subjects = {r.subject_identity for r in analysis.records}
    assert "com.sample.orders.OrderServiceHandler.tail" in subjects
    assert analysis.omitted == 9
    assert analysis.limitations == ("window_records_truncated",)
    assert analysis.status == WindowStatus.DEGRADED


def test_j8_t07_budget_is_fair_without_hint(monkeypatch):
    monkeypatch.setattr(wf, "MAX_WINDOW_RECORDS", 4)
    analysis = wf.enumerate_java_window_facts(_evidence(_budget_source()))
    assert len(analysis.records) == 4
    assert any(
        (r.subject_identity or "").endswith(".tail") for r in analysis.records
    ), "fair share must not starve the field"


# A method-only window: the leading orphan method and trailing lone brace
# parse as an error with no class on record, so the file stem names the
# owner (labeled inference). A parseable secondary class would route the
# orphan to the T10 marker path instead (tree-sitter recovers class headers).
_ORPHAN_WINDOW = """package com.sample.orders;
    String helper(int v) {
        if (v > 0) {
            return "h";
        }
        return "z";
    }
    }
"""

# Same orphan shape, but the secondary class parses: the orphan method has
# no owner at all and becomes a marker instead of a bare-named record.
_ORPHAN_WITH_CLASS_WINDOW = """package com.sample.orders;
    String helper(int v) {
        if (v > 0) {
            return "h";
        }
        return "z";
    }
    }
    class SecondaryOne {
        String other(int v) {
            if (v > 1) {
                return "o";
            }
            return "z";
        }
    }
"""


def test_j8_t08_no_bare_subjects_inferred_owner_labeled():
    analysis = wf.enumerate_java_window_facts(_evidence(_ORPHAN_WINDOW))
    assert analysis.records, "the stem-inferred owner keeps the live method-only behavior"
    for record in analysis.records:
        assert "." in (record.subject_identity or ""), record.subject_identity
    helper = next(
        r for r in analysis.records if (r.subject_identity or "").endswith(".helper")
    )
    assert helper.subject_identity == "com.sample.orders.OrderServiceHandler.helper"
    assert (
        "declaring class inferred from the file name" in " ".join(helper.assumptions)
    )
    assert (helper.structured_fact or {}).get("class_attribution") == "file_stem_inferred"


def test_j8_t09_wrong_class_claim_no_longer_verifies():
    backfilled = VerificationEngine._with_window_backfill([_evidence(_ORPHAN_WINDOW)])
    for symbol in (
        "com.sample.orders.WrongClass.helper",
        "com.sample.orders.SecondaryOne.helper",
    ):
        hyp = _hypothesis(symbol, "helper returns 'h' on positive input")
        hyp.claimed_value = "h"
        hyp.claim_target.claimed_value = "h"
        assert VerificationEngine().validate_java_fact_claim(hyp, backfilled, []) is None
    good = _hypothesis(
        "com.sample.orders.OrderServiceHandler.helper", "helper returns 'h' on positive input"
    )
    good.claimed_value = "h"
    good.claim_target.claimed_value = "h"
    result = VerificationEngine().validate_java_fact_claim(good, backfilled, [])
    assert result is not None


def test_j8_t10_unknown_owner_becomes_marker():
    ev = _evidence(_ORPHAN_WITH_CLASS_WINDOW, path="service/helper.java")
    analysis = wf.enumerate_java_window_facts(ev)
    markers = [r for r in analysis.records if r.status == CodeFactStatus.UNRESOLVED]
    assert any(
        r.limitation_codes == ("declaring_type_not_in_window",) for r in markers
    )
    resolved = [r for r in analysis.records if r.status == CodeFactStatus.RESOLVED]
    assert resolved and all("." in (r.subject_identity or "") for r in resolved)


def test_j8_t11_nested_method_only_window_labels_inference():
    source = (
        "package com.sample.orders;\n"
        "    String get(int v) {\n"
        "        if (v > 3) {\n"
        '            return "nine";\n'
        "        }\n"
        '        return "zero";\n'
        "    }\n"
    )
    analysis = wf.enumerate_java_window_facts(_evidence(source))
    assert analysis.records
    assert all("." in (r.subject_identity or "") for r in analysis.records)
    record = next(
        r
        for r in analysis.records
        if (r.subject_identity or "").endswith(".get")
        and r.status == CodeFactStatus.RESOLVED
    )
    assert record.subject_identity == "com.sample.orders.OrderServiceHandler.get"
    assert (record.structured_fact or {}).get("class_attribution") == "file_stem_inferred"
    assert "declaring class inferred from the file name" in " ".join(
        record.assumptions
    )


def test_j8_t12_columns_are_named_and_converted():
    from investigation_agent_platform.application.analysis.java.dataflow import (
        resolve_java_data_flow,
    )

    source = 'package com.sample.orders;\npublic class OrderServiceHandler {\n    String m(String s) {\n        String t = "é";\n        return s + t;\n    }\n}\n'
    model = build_java_model(
        source.encode(), file_path=_PATH, repository="repo", revision="54d73240d6b5"
    )
    assert model.locals[0].span.byte_col_end == 24
    line = '        String t = "é";'
    assert code_point_column(line, 24) == 23
    assert code_point_column(line, 15) == 15
    assert code_point_column("abc", 2) == 2
    ev = _evidence(source)
    resolution = resolve_java_data_flow(ev, "t", method_scope="m")
    assert resolution is not None and resolution.status == CodeFactStatus.RESOLVED
    assert (resolution.structured_fact or {}).get("column_unit") == "code_point"
    assert resolution.data_flow_refs[0].endswith("def(4:15-22)->use(5:19-20)")


def test_j8_t13_declaration_locations_match_git_numbering():
    # A form feed glued to line 1 and a U+2028 inside line 2: git (and
    # tree-sitter) number the guarded return on line 6; str.splitlines
    # would shift every line below the exotic characters.
    raw_lines = [
        "package com.sample.orders;\x0c",
        "public class OrderServiceHandler {",
        "    String kind(int v) {",
        '        String tag = "x y";',
        "        if (v > 0) {",
        '            return "pos";',
        "        }",
        '        return "neg";',
        "    }",
        "}",
    ]
    numbered = "\n".join(f"{i + 1}| {ln}" for i, ln in enumerate(raw_lines))
    end = len(raw_lines)
    iid, eid = uuid4(), uuid4()
    now = datetime.now(UTC)
    path = _PATH
    ev = Evidence(
        evidence_id=eid,
        tenant_id="tenant-a",
        investigation_id=iid,
        evidence_type=EvidenceType.SOURCE_CODE,
        provider="pygit2-local",
        source=f"git://repo/{path}@54d73240d6b5",
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
                provider_type="GIT", operation="GET_SOURCE", normalized_query_hash="h"
            ),
            source_location=SourceLocation(
                system="Git",
                identifier=f"repo/{path}",
                file_path=path,
                line_start=1,
                line_end=end,
                revision="54d73240d6b5",
            ),
        ),
        freshness=EvidenceFreshness(observed_at=now, retrieved_at=now),
        fingerprint=str(eid),
    )
    analysis = wf.enumerate_java_window_facts(ev)
    by_subject = {r.subject_identity: r for r in analysis.records}
    rec = by_subject.get("com.sample.orders.OrderServiceHandler.kind")
    assert rec is not None and rec.resolved_value == "pos"
    assert rec.declaration_location.line_start == 6


def test_j8_t14_crlf_equals_lf():
    lf = (
        "package com.sample.orders;\n"
        "public class OrderServiceHandler {\n"
        '    private String tail = "LATE";\n'
        "    String kind(int v) {\n"
        "        if (v > 0) {\n"
        '            return "pos";\n'
        "        }\n"
        '        return "neg";\n'
        "    }\n"
        "}\n"
    )
    crlf = lf.replace("\n", "\r\n")
    left = wf.enumerate_java_window_facts(_evidence(lf))
    right = wf.enumerate_java_window_facts(_evidence(crlf))
    assert [(r.subject_identity, r.resolved_value) for r in left.records] == [
        (r.subject_identity, r.resolved_value) for r in right.records
    ]
    assert [r.declaration_location.line_start for r in left.records] == [
        r.declaration_location.line_start for r in right.records
    ]
