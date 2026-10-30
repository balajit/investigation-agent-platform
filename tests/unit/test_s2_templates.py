# tests/unit/test_s2_templates.py
"""P5 chunk 1: S2 dormant-activation templates (DES-S2-dormant-templates)."""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from investigation_agent_platform.domain.hypothesis.templates import (
    TEMPLATES_VERSION_CURRENT,
    DiscriminatingFetchKind,
    DormantTemplate,
    SuspectPath,
    SuspectSite,
    SuspectSiteKind,
    TemplateKind,
    instantiate_templates,
)


def _site(kind: SuspectSiteKind, **kw) -> SuspectSite:
    args = {"node_id": uuid4(), "kind": kind, "file_path": "svc/app.py", "line": 10}
    args.update(kw)
    return SuspectSite(**args)


def _path(*sites: SuspectSite) -> SuspectPath:
    return SuspectPath(repository="tenant-repo", revision="abc123", sites=list(sites))


def test_all_four_families_activate_with_birth_fetch():
    path = _path(
        _site(SuspectSiteKind.FLAG_GATE, detail="if feature_x"),
        _site(SuspectSiteKind.BRANCH_MATCH, detail="match event"),
        _site(SuspectSiteKind.EXTERNAL_CALL, detail="upstream GET /v1/items"),
        _site(SuspectSiteKind.LOOP_POOL_RETRY, detail="retry loop"),
    )
    templates = instantiate_templates(path)
    by_kind = {t.kind: t for t in templates}
    assert set(by_kind) == {
        TemplateKind.FLAG_GATED,
        TemplateKind.DATA_GATED,
        TemplateKind.CONTRACT_DRIFT,
        TemplateKind.REGIME_CROSSING,
    }
    expected_fetch = {
        TemplateKind.FLAG_GATED: DiscriminatingFetchKind.GATE_STATE_T_VS_G,
        TemplateKind.DATA_GATED: DiscriminatingFetchKind.FIRST_SEEN_PAYLOAD_SHAPE,
        TemplateKind.CONTRACT_DRIFT: DiscriminatingFetchKind.LIVE_RESPONSE_VS_ASSUMED_SHAPE,
        TemplateKind.REGIME_CROSSING: DiscriminatingFetchKind.TREND_BREAK_AT_T,
    }
    for kind, template in by_kind.items():
        assert template.version == TEMPLATES_VERSION_CURRENT
        assert template.discriminating_fetch is not None
        assert template.discriminating_fetch.fetch_kind == expected_fetch[kind]
        assert template.handoff_candidate is None
        assert template.assumptions, "every template enumerates checkable assumptions"


def test_assumptions_cost_ordered_and_priced():
    (template,) = instantiate_templates(_path(_site(SuspectSiteKind.FLAG_GATE)))
    orders = [a.check_order for a in template.assumptions]
    assert orders == sorted(orders)
    assert all(a.cost_estimate >= 0 for a in template.assumptions)
    # Spread-budget pricing: default envelope budget is 50 units.
    assert template.priced_within(50)
    assert template.total_cost == sum(a.cost_estimate for a in template.assumptions)
    assert not template.priced_within(template.total_cost - 1) or template.total_cost == 0


def test_other_sites_activate_nothing():
    assert instantiate_templates(_path(_site(SuspectSiteKind.OTHER))) == []


def test_cross_boundary_terminates_as_handoff_never_fetch():
    path = _path(
        _site(
            SuspectSiteKind.EXTERNAL_CALL,
            cross_boundary=True,
            upstream_identity="upstream-billing",
            detail="POST /charge",
        )
    )
    (template,) = instantiate_templates(path)
    assert template.discriminating_fetch is None
    assert template.assumptions == []
    assert template.handoff_candidate is not None
    assert template.handoff_candidate.upstream_identity == "upstream-billing"
    assert "svc/app.py:10" in template.handoff_candidate.localizing_evidence


def test_cross_boundary_groups_by_upstream_identity():
    path = _path(
        _site(
            SuspectSiteKind.EXTERNAL_CALL,
            cross_boundary=True,
            upstream_identity="upstream-a",
        ),
        _site(
            SuspectSiteKind.FLAG_GATE,
            cross_boundary=True,
            upstream_identity="upstream-b",
        ),
    )
    templates = instantiate_templates(path)
    assert len(templates) == 2
    assert {t.handoff_candidate.upstream_identity for t in templates} == {
        "upstream-a",
        "upstream-b",
    }


def test_cross_boundary_requires_upstream_identity():
    with pytest.raises(ValidationError):
        _site(SuspectSiteKind.EXTERNAL_CALL, cross_boundary=True)


def test_handoff_candidate_rejects_fetch_and_assumptions():
    site = _site(SuspectSiteKind.FLAG_GATE)
    with pytest.raises(ValidationError):
        DormantTemplate(
            kind=TemplateKind.FLAG_GATED,
            suspect_path_id=uuid4(),
            site_node_ids=[site.node_id],
            handoff_candidate=None,
            discriminating_fetch=None,
        )


def test_in_tenant_and_cross_boundary_coexist():
    path = _path(
        _site(SuspectSiteKind.FLAG_GATE),
        _site(
            SuspectSiteKind.EXTERNAL_CALL,
            cross_boundary=True,
            upstream_identity="upstream-x",
        ),
    )
    templates = instantiate_templates(path)
    assert len(templates) == 2
    fetches = [t for t in templates if t.discriminating_fetch is not None]
    handoffs = [t for t in templates if t.handoff_candidate is not None]
    assert len(fetches) == 1 and len(handoffs) == 1


def test_unpinned_revision_rejected():
    for bad in ("HEAD", "UNPINNED"):
        with pytest.raises(ValidationError):
            SuspectPath(
                repository="tenant-repo",
                revision=bad,
                sites=[_site(SuspectSiteKind.FLAG_GATE)],
            )


def test_pinned_revision_accepted():
    path = _path(_site(SuspectSiteKind.FLAG_GATE))
    assert path.revision == "abc123"


def test_long_target_summarized_deterministically():
    sites = [_site(SuspectSiteKind.FLAG_GATE, detail="x" * 900) for _ in range(5)]
    (template,) = instantiate_templates(_path(*sites))
    assert template.discriminating_fetch is not None
    target = template.discriminating_fetch.target
    assert len(target) <= 1024
    assert "more sites" in target
    # Leading whole segments survive; the omit count is explicit.
    assert target.startswith("svc/app.py:10")
    assert "+4 more sites" in target
    # Deterministic: same input order, same output.
    (repeat,) = instantiate_templates(_path(*sites))
    assert repeat.discriminating_fetch is not None
    assert repeat.discriminating_fetch.target == target


def test_long_localizing_evidence_summarized():
    sites = [
        _site(
            SuspectSiteKind.EXTERNAL_CALL,
            cross_boundary=True,
            upstream_identity="upstream-u",
            detail="y" * 900,
        )
        for _ in range(5)
    ]
    (template,) = instantiate_templates(_path(*sites))
    assert template.handoff_candidate is not None
    evidence = template.handoff_candidate.localizing_evidence
    assert len(evidence) <= 2048
    assert "more sites" in evidence
