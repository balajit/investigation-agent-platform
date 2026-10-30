"""Manifest-driven battery over tests/fixtures/python/ (Python-lang Phase 4).

Each manifest case builds pinned inline range evidence from the fixture
file and asserts resolver status, value, codes, and assumptions.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from investigation_agent_platform.application.analysis.python.class_facts import (
    resolve_python_class_attr_fact,
)
from investigation_agent_platform.application.analysis.python.config import (
    resolve_python_config_fact,
)
from investigation_agent_platform.application.analysis.python.control import (
    resolve_python_control_fact,
    resolve_python_write_sites_fact,
)
from investigation_agent_platform.application.analysis.python.framework_facts import (
    resolve_python_framework_fact,
)
from investigation_agent_platform.application.analysis.python.imports import (
    resolve_python_import_fact,
)
from investigation_agent_platform.application.analysis.python.mro import (
    resolve_python_method_owner_fact,
    resolve_python_mro_fact,
)
from investigation_agent_platform.application.analysis.python.scope_facts import (
    resolve_python_param_default_fact,
    resolve_python_value_fact,
)
from investigation_agent_platform.domain.code.models import CodeFactStatus
from investigation_agent_platform.domain.evidence.models import Evidence, EvidenceType
from investigation_agent_platform.domain.provenance.models import (
    EvidenceFreshness,
    EvidenceProvenance,
    QueryFingerprint,
    SourceLocation,
)

_FIXTURES = Path(__file__).parent.parent / "fixtures" / "python"


def _load_manifest():
    return json.loads((_FIXTURES / "manifest.json").read_text())


def _build(source: str, filename: str):
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
        source=f"git://repo/svc/{filename}@abc123",
        title="Python fixture range",
        summary="Pinned fixture source",
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
                identifier=f"repo/svc/{filename}",
                file_path=f"svc/{filename}",
                line_start=1,
                line_end=end,
                revision="abc123",
            ),
        ),
        freshness=EvidenceFreshness(observed_at=now, retrieved_at=now),
        fingerprint=str(eid),
    )


def _run_case(case):
    source = (_FIXTURES / case["file"]).read_text()
    ev = _build(source, case["file"])
    resolver = case["resolver"]
    if resolver == "value":
        return resolve_python_value_fact(ev, case["symbol"])
    if resolver == "param":
        return resolve_python_param_default_fact(
            ev, case["symbol"], function_scope=case["function_scope"]
        )
    if resolver == "class":
        return resolve_python_class_attr_fact(ev, case["symbol"], class_scope=case["class_scope"])
    if resolver == "framework":
        return resolve_python_framework_fact(ev, case["symbol"], class_scope=case["class_scope"])
    if resolver == "import":
        return resolve_python_import_fact(ev, case["symbol"])
    if resolver == "mro":
        return resolve_python_mro_fact(ev, case["class_scope"])
    if resolver == "owner":
        return resolve_python_method_owner_fact(ev, case["symbol"], class_scope=case["class_scope"])
    if resolver == "control":
        return resolve_python_control_fact(ev, case["symbol"], branch_hint=case.get("branch_hint"))
    if resolver == "writes":
        return resolve_python_write_sites_fact(
            ev, case["symbol"], class_scope=case.get("class_scope")
        )
    if resolver == "config":
        return resolve_python_config_fact(ev, case["symbol"])
    raise AssertionError(f"unknown resolver {resolver}")


def test_python_fixture_battery():
    manifest = _load_manifest()
    assert manifest["cases"], "manifest must define cases"
    failures = []
    for case in manifest["cases"]:
        result = _run_case(case)
        try:
            assert result.status == CodeFactStatus(case["expect_status"]), (
                f"{case['case_id']}: status {result.status} != {case['expect_status']} "
                f"(codes={result.limitation_codes})"
            )
            if "expect_value" in case:
                assert result.resolved_value == case["expect_value"], (
                    f"{case['case_id']}: value {result.resolved_value!r} != {case['expect_value']!r}"
                )
            if "expect_structured" in case:
                assert result.structured_fact is not None, f"{case['case_id']}: no structured fact"
                for key, want in case["expect_structured"].items():
                    got = result.structured_fact.get(key)
                    assert got == want, f"{case['case_id']}: structured[{key}] {got!r} != {want!r}"
            for code in case.get("expect_codes", []):
                assert code in result.limitation_codes, (
                    f"{case['case_id']}: missing limitation {code} in {result.limitation_codes}"
                )
            for assumption in case.get("expect_assumptions", []):
                assert assumption in result.assumptions, (
                    f"{case['case_id']}: missing assumption {assumption} in {result.assumptions}"
                )
        except AssertionError as exc:
            failures.append(str(exc))
    assert not failures, "battery failures:\n" + "\n".join(failures)
