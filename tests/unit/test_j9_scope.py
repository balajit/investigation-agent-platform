"""J9: cross-boundary classification is a path-prefix test (synthetic only).

Covers D16 (three-state repo-qualified scope, honest unknown, repo locator
move), D17 (honest mismatch codes), D18 as a strict xfail (owner-gated),
and the prompt-shape stamp (T11). No analyzed repository is referenced.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from investigation_agent_platform.domain.evidence.models import Evidence, EvidenceType
from investigation_agent_platform.domain.profile.models import (
    ApplicationProfile,
    Service,
    ServiceCode,
)
from investigation_agent_platform.domain.profile.scope import (
    BoundaryClass,
    SourceScope,
    classify,
    normalize_source_root,
)
from investigation_agent_platform.domain.provenance.models import (
    EvidenceFreshness,
    EvidenceProvenance,
    QueryFingerprint,
    SourceLocation,
)


def _profile(services) -> ApplicationProfile:
    return ApplicationProfile(
        id="app1",
        tenant_id="t1",
        name="n",
        description="d",
        environment="production",
        services=services,
    )


def _svc(name, org, repos, roots=None, paths=None) -> Service:
    return Service(
        name=name,
        code=ServiceCode(
            org=org,
            repos=list(repos),
            default_branch="main",
            language="python",
            source_roots=list(roots or []),
            paths=list(paths or []),
        ),
    )


def _range_row(snippet, file_path, repository="tenant-repo", revision="abc123"):
    iid = uuid4()
    now = datetime.now(UTC)
    return Evidence(
        evidence_id=uuid4(),
        tenant_id="tenant-a",
        investigation_id=iid,
        evidence_type=EvidenceType.SOURCE_CODE,
        provider="code-git",
        source=f"git://{repository}/{file_path}@{revision}",
        title=f"Source: {file_path}",
        summary="range read",
        content_snippet=snippet,
        attributes={"read_mode": "range"},
        observed_at=now,
        retrieved_at=now,
        provenance=EvidenceProvenance(
            tenant_id="tenant-a",
            investigation_id=iid,
            provider_type="code-git",
            requested_provider_id="code-git",
            actual_provider_id="code-git",
            source_system="git",
            retrieval_timestamp=now,
            query_fingerprint=QueryFingerprint(
                provider_type="code-git",
                operation="get_code",
                normalized_query_hash="abc",
            ),
            source_location=SourceLocation(
                system="git",
                identifier=file_path,
                file_path=file_path,
                line_start=10,
                line_end=11,
                revision=revision,
            ),
        ),
        freshness=EvidenceFreshness(observed_at=now, retrieved_at=now),
        fingerprint=f"fp-{uuid4()}",
    )


EXTERNAL_SNIPPET = '10| resp = requests.get("https://billing.upstream/api")\n11| x = 1'


def test_j9_t01_unknown_roots_do_not_invent_a_handoff():
    from investigation_agent_platform.application.investigation.context_compiler import (
        boundary_classifier,
        compile_dormant_templates,
    )

    section = compile_dormant_templates(
        [_range_row(EXTERNAL_SNIPPET, file_path="tenant/svc/c.py")],
        anchors=None,
        classify=boundary_classifier(None),
        roots_status="unavailable",
    )
    assert section["roots_status"] == "unavailable"
    first = section["templates"][0]
    assert first["discriminating_fetch"] is not None
    assert first["handoff"] is None
    assert first["boundary"] == "unknown"


def test_j9_t02_store_error_is_surfaced_not_silent(caplog):
    import asyncio

    from investigation_agent_platform.application.investigation import reasoning as _r

    class BoomRepo:  # type: ignore[no-untyped-def]
        async def get_by_application_id(self, *args, **kwargs):
            raise RuntimeError("secret-looking-text")

    async def go():  # type: ignore[no-untyped-def]
        return await _r._load_boundary_profile(BoomRepo(), "t", "app")

    with caplog.at_level(logging.WARNING):
        profile, status = asyncio.run(go())
    assert profile is None
    assert status == "unavailable"
    assert any(
        "RuntimeError" in (rec.message or "") and "secret-looking-text" not in (rec.message or "")
        for rec in caplog.records
    )


def test_j9_t03_declared_roots_still_classify_external():
    from investigation_agent_platform.application.investigation.context_compiler import (
        boundary_classifier,
        compile_dormant_templates,
    )

    profile = _profile([_svc("svc", "tenant", ["repo"], roots=["svc/src/main/java"])])
    classify = boundary_classifier(profile)
    assert classify("tenant/repo", "upstream/lib/a.py") == BoundaryClass.EXTERNAL
    assert classify("tenant/repo", "svc/src/main/java/X.java") == BoundaryClass.IN_TENANT
    section = compile_dormant_templates(
        [_range_row(EXTERNAL_SNIPPET, file_path="upstream/lib/a.py")],
        anchors=None,
        classify=classify,
        roots_status="known",
    )
    first = section["templates"][0]
    assert first["handoff"] is not None
    assert first["boundary"] == "external"


def test_j9_t04_roots_are_repo_qualified():
    profile = _profile(
        [
            _svc("a", "org", ["a"], roots=["src/main/java"]),
            _svc("b", "org", ["b"], roots=["other/"]),
        ]
    )
    assert (
        profile.classify_source("org/b", "src/main/java/X.java") == BoundaryClass.EXTERNAL
    )
    assert (
        profile.classify_source("org/a", "src/main/java/X.java") == BoundaryClass.IN_TENANT
    )


def test_j9_t05_repos_without_roots_mean_the_whole_repo():
    profile = _profile([_svc("a", "org", ["a"])])
    assert profile.classify_source("org/a", "anything/at/all.py") == BoundaryClass.IN_TENANT
    assert profile.classify_source("org/z", "anything.py") == BoundaryClass.EXTERNAL


def test_j9_t06_whole_repository_root():
    profile = _profile([_svc("a", "org", ["a"], roots=["."])])
    assert profile.classify_source("org/a", "src/A.java") == BoundaryClass.IN_TENANT


def test_j9_t07_root_normalization_table():
    assert normalize_source_root(".") == ""
    assert normalize_source_root("./") == ""
    assert normalize_source_root("") == ""
    assert normalize_source_root("/") == ""
    assert normalize_source_root("/svc/") == "svc"
    assert normalize_source_root("svc/../x") is None
    assert normalize_source_root("a\\b") == "a/b"


def test_j9_t08_undeterminable_repository_falls_back_to_roots():
    scopes = [SourceScope(repos=("org/a",), roots=("src",))]
    assert classify(scopes, "unknown-repo", "src/X.py") == BoundaryClass.IN_TENANT
    assert classify(scopes, "unknown-repo", "other/X.py") == BoundaryClass.EXTERNAL
    assert classify([], "org/a", "src/X.py") == BoundaryClass.UNKNOWN


def test_j9_t09_codes_are_honest():
    from investigation_agent_platform.application.analysis.java.method_facts import (
        resolve_java_call_target as _resolve,
    )
    from tests.unit.test_j5_newer_constructs import _evidence as _java_evidence

    mismatch_window = (
        "package com.sample.orders;\n"
        "public class Widget {\n"
        "    void process(int v) {}\n"
        "    void run() {\n"
        "        process(5);\n"
        "    }\n"
        "}\n"
    )
    result = _resolve(_java_evidence(mismatch_window), "process", declaring_type="com.external.Lib")
    assert result.status.value == "UNRESOLVED"
    assert result.limitation_codes == ("declaring_type_mismatch_in_window",)
    empty_window = "// nothing declared here\n"
    result = _resolve(_java_evidence(empty_window), "process", declaring_type="com.external.Lib")
    assert result.limitation_codes == ("declaring_type_not_in_window",)


def test_j9_t11_prompt_shape_stamp():
    from investigation_agent_platform.application.investigation.context_compiler import (
        REASONER_PROMPT_VERSION,
    )

    assert re.match(r"^v3-claim-grounded-\d+$", REASONER_PROMPT_VERSION)
    assert REASONER_PROMPT_VERSION != "v3-claim-grounded-019"


@pytest.mark.xfail(strict=True, reason="D18 validator alignment requires owner approval")
def test_j9_t12_validator_alignment_gated():
    from investigation_agent_platform.application.investigation.validator import (
        _is_within_roots,
    )

    assert _is_within_roots("src/A.java", ["."]) is True
