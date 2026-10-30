# tests/unit/test_s2_wiring.py
"""P5 chunk 2: S2 template wiring — classifier over pinned banked ranges,
reasoning-context section, budget pricing. No live model calls."""

from datetime import UTC, datetime
from uuid import uuid4

from investigation_agent_platform.application.investigation.context_compiler import (
    compile_dormant_templates,
    compile_reasoning_context,
    external_classifier_for_roots,
)
from investigation_agent_platform.domain.evidence.models import Evidence, EvidenceType
from investigation_agent_platform.domain.investigation.models import (
    Investigation,
    InvestigationAnchors,
    InvestigationContext,
    InvestigationRequest,
    InvestigationState,
)
from investigation_agent_platform.domain.provenance.models import (
    EvidenceFreshness,
    EvidenceProvenance,
    QueryFingerprint,
    SourceLocation,
)


def _range_row(
    snippet,
    file_path="tenant/svc/app.py",
    revision="abc123",
    line_start=10,
    etype=EvidenceType.SOURCE_CODE,
    attrs=None,
    attrs_override=None,
):
    iid = uuid4()
    attributes = {"read_mode": "range"} if attrs_override is None else dict(attrs_override)
    attributes.update(attrs or {})
    return Evidence(
        tenant_id="tenant-a",
        investigation_id=iid,
        evidence_type=etype,
        provider="code-git",
        source=f"git://tenant-repo/{file_path}@{revision}",
        title=f"Source: {file_path}",
        summary="range read",
        content_snippet=snippet,
        attributes=attributes,
        observed_at=datetime.now(UTC),
        retrieved_at=datetime.now(UTC),
        provenance=EvidenceProvenance(
            tenant_id="tenant-a",
            investigation_id=iid,
            provider_type="code-git",
            requested_provider_id="code-git",
            actual_provider_id="code-git",
            source_system="git",
            retrieval_timestamp=datetime.now(UTC),
            query_fingerprint=QueryFingerprint(
                provider_type="code-git",
                operation="get_code",
                normalized_query_hash="abc",
            ),
            source_location=SourceLocation(
                system="git",
                identifier=file_path,
                file_path=file_path,
                line_start=line_start,
                revision=revision,
            ),
        ),
        freshness=EvidenceFreshness(
            observed_at=datetime.now(UTC), retrieved_at=datetime.now(UTC)
        ),
        fingerprint=f"fp-{uuid4()}",
    )


def _anchors(**kw):
    args = {"spread_budget": 50}
    args.update(kw)
    return InvestigationAnchors(**args)


def _state(evidence, anchors=None):
    now = datetime.now(UTC)
    inv = Investigation(
        session_id="sess-1",
        application_id="app",
        tenant_id="tenant-a",
        request=InvestigationRequest(
            application_id="app",
            problem_description="p",
            session_id="sess-1",
            requested_by="tester",
        ),
        context=InvestigationContext(
            environment="production",
            time_window=(now, now),
        ),
        metadata={"anchors": (anchors or _anchors()).model_dump(mode="json")},
    )
    return InvestigationState(investigation=inv, evidence=evidence)


FLAG_SNIPPET = "10| if feature_flag_enabled:\n11|     return new_path()"
BRANCH_SNIPPET = "20| switch (event.type) {\n21| case ORDER_CREATED:"
EXTERNAL_SNIPPET = '30| resp = requests.get("https://billing.upstream/api")'
LOOP_SNIPPET = "40| for attempt in range(3):\n41|     retry_submit()"
PLAIN_SNIPPET = "50| x = 1\n51| y = x + 2"


def test_each_family_classifies_from_pinned_range():
    rows = [
        _range_row(FLAG_SNIPPET, file_path="tenant/svc/a.py"),
        _range_row(BRANCH_SNIPPET, file_path="tenant/svc/b.py"),
        _range_row(EXTERNAL_SNIPPET, file_path="tenant/svc/c.py"),
        _range_row(LOOP_SNIPPET, file_path="tenant/svc/d.py"),
    ]
    section = compile_dormant_templates(rows, anchors=_anchors())
    assert section["templates_version"] == "templates-v1"
    kinds = {t["kind"] for t in section["templates"]}
    assert kinds == {"FLAG_GATED", "DATA_GATED", "CONTRACT_DRIFT", "REGIME_CROSSING"}
    for template in section["templates"]:
        assert template["discriminating_fetch"] is not None
        assert template["handoff"] is None
        assert template["assumptions"], "every template carries checkable assumptions"
        assert template["priced_within_budget"] is True


def test_excluded_rows_activate_nothing():
    rows = [
        # Discovery marker with a path-head snippet (never a range).
        _range_row(
            "tenant/svc/app.py",
            attrs_override={"discovery_only": True},
        ),
        # Explicit path-only declaration wins over the numbered shape.
        _range_row(FLAG_SNIPPET, attrs={"read_mode": "path_only"}),
        # Unpinned revision.
        _range_row(FLAG_SNIPPET, revision="HEAD"),
        # Wrong evidence type.
        _range_row("some log line", etype=EvidenceType.LOG),
        # No classifiable syntax.
        _range_row(PLAIN_SNIPPET),
        # Empty snippet.
        _range_row("   "),
    ]
    section = compile_dormant_templates(rows, anchors=_anchors())
    assert section["templates"] == []


def test_templates_reach_compiled_reasoning_context():
    state = _state([_range_row(FLAG_SNIPPET)])
    ctx = compile_reasoning_context(state)
    section = ctx["dormant_templates"]
    assert section["templates_version"] == "templates-v1"
    assert len(section["templates"]) == 1
    entry = section["templates"][0]
    assert entry["kind"] == "FLAG_GATED"
    assert entry["discriminating_fetch"]["fetch_kind"] == "GATE_STATE_T_VS_G"


def test_anchors_and_budget_rendered():
    anchors = _anchors(
        onset_t=datetime(2026, 10, 1, tzinfo=UTC),
        last_good_g=datetime(2026, 9, 25, tzinfo=UTC),
        spread_budget=2,
    )
    state = _state([_range_row(FLAG_SNIPPET)], anchors=anchors)
    section = compile_reasoning_context(state)["dormant_templates"]
    assert section["anchors"]["spread_budget"] == 2
    assert section["anchors"]["onset_t"].startswith("2026-10-01")
    assert section["anchors"]["last_good_g"].startswith("2026-09-25")
    # Cheapest family costs 1+2+3=6 against a budget of 2.
    assert section["templates"][0]["priced_within_budget"] is False
    assert section["templates"][0]["total_cost"] == 6


def test_grouped_cross_boundary_handoff_never_fetches():
    is_external = external_classifier_for_roots(["tenant/"])
    rows = [
        _range_row(
            "30| resp = httpClient.post(path, body)\n"
            "31| x = 1\n"
            "40| resp2 = httpClient.post(path2, body2)",
            file_path="upstream/lib/a.py",
            line_start=30,
        ),
    ]
    section = compile_dormant_templates(rows, anchors=_anchors(), is_external=is_external)
    assert len(section["templates"]) == 1
    entry = section["templates"][0]
    assert entry["discriminating_fetch"] is None
    assert entry["assumptions"] == []
    handoff = entry["handoff"]
    assert handoff is not None
    assert handoff["upstream_identity"] == "upstream"
    assert "upstream/lib/a.py:30" in handoff["localizing_evidence"]
    assert "upstream/lib/a.py:40" in handoff["localizing_evidence"]
    # No fetch target keys leak into the handoff contract.
    assert "file_path" not in handoff
    assert "fetch_kind" not in handoff


def test_in_tenant_default_when_no_roots():
    assert external_classifier_for_roots(None) is None
    assert external_classifier_for_roots([]) is None
    section = compile_dormant_templates(
        [_range_row(EXTERNAL_SNIPPET, file_path="tenant/svc/c.py")],
        anchors=_anchors(),
        is_external=None,
    )
    assert section["templates"][0]["discriminating_fetch"] is not None


def test_classifier_roots_mirror_validator():
    is_external = external_classifier_for_roots(["tenant/"])
    assert is_external is not None
    assert is_external("tenant/svc/app.py") is False
    assert is_external("tenant") is False
    assert is_external("upstream/lib/a.py") is True
    assert is_external("tenant-evil/x.py") is True


def test_template_count_capped():
    rows = [
        _range_row(FLAG_SNIPPET, file_path=f"tenant/svc/f{i}.py") for i in range(12)
    ]
    section = compile_dormant_templates(rows, anchors=_anchors())
    assert len(section["templates"]) == 8
