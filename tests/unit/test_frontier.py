# tests/unit/test_frontier.py
"""P6 chunk 1: collect frontier domain core (frontier-v1). No loop, no
model calls — pure ordering, seeding, caps, and exit logic."""

from uuid import uuid4

import pytest

from investigation_agent_platform.domain.collection.frontier import (
    DEFAULT_CANDIDATE_TTL_ROUNDS,
    FRONTIER_VERSION_CURRENT,
    MAX_FRONTIER_DEPTH,
    CandidateStatus,
    EvidenceState,
    FailureCharacter,
    FrontierCandidate,
    FrontierDimension,
    FrontierExit,
    SeedFetch,
    evaluate_exit,
    order_candidates,
    seed_from_dormant_templates,
)
from investigation_agent_platform.domain.hypothesis.templates import (
    DiscriminatingFetch,
    DiscriminatingFetchKind,
    DormantTemplate,
    SuspectPath,
    SuspectSite,
    SuspectSiteKind,
    TemplateAssumption,
    TemplateKind,
    classify_banked_sites,
    instantiate_templates,
)


def _site(kind=SuspectSiteKind.FLAG_GATE, file_path="tenant/svc/a.py", line=10):
    return SuspectSite(
        node_id=uuid4(),
        kind=kind,
        file_path=file_path,
        line=line,
        detail="matched flag_gate syntax: if feature_flag_enabled:",
    )


def _path(*sites, revision="abc123"):
    return SuspectPath(repository="tenant-repo", revision=revision, sites=list(sites))


def _character(**kw):
    args = {"triage_route": "BUG", "claim_kind": "BEHAVIOR", "has_unbeaten_rival": False}
    args.update(kw)
    return FailureCharacter(**args)


def _pairs(revision="abc123"):
    """Real (path, template) pairs via the P5 seam: range row to template."""
    from datetime import UTC, datetime

    from investigation_agent_platform.domain.evidence.models import Evidence, EvidenceType
    from investigation_agent_platform.domain.provenance.models import (
        EvidenceFreshness,
        EvidenceProvenance,
        QueryFingerprint,
        SourceLocation,
    )

    iid = uuid4()
    now = datetime.now(UTC)
    row = Evidence(
        tenant_id="t",
        investigation_id=iid,
        evidence_type=EvidenceType.SOURCE_CODE,
        provider="code-git",
        source="git://r/a.py@abc123",
        title="t",
        summary="t",
        content_snippet="10| if feature_flag_enabled:\n11|     return new_path(order)",
        attributes={"read_mode": "range"},
        observed_at=now,
        retrieved_at=now,
        provenance=EvidenceProvenance(
            tenant_id="t",
            investigation_id=iid,
            provider_type="code-git",
            requested_provider_id="code-git",
            actual_provider_id="code-git",
            source_system="git",
            retrieval_timestamp=now,
            query_fingerprint=QueryFingerprint(
                provider_type="code-git", operation="get_code", normalized_query_hash="h"
            ),
            source_location=SourceLocation(
                system="git",
                identifier="a.py",
                file_path="tenant/svc/a.py",
                line_start=10,
                line_end=11,
                revision=revision,
            ),
        ),
        freshness=EvidenceFreshness(observed_at=now, retrieved_at=now),
        fingerprint=f"fp-{uuid4()}",
    )
    paths = classify_banked_sites([row])
    assert len(paths) == 1
    return [(paths[0], t) for t in instantiate_templates(paths[0])]


class TestSeedFromTemplates:
    def test_in_tenant_template_seeds_candidate_with_identity(self):
        pairs = _pairs()
        assert len(pairs) == 1
        seeded, refused = seed_from_dormant_templates(pairs, _character(), EvidenceState.NO_ROWS)
        assert refused == []
        assert len(seeded) == 1
        candidate = seeded[0]
        assert candidate.version == "frontier-v1"
        assert candidate.file_path == "tenant/svc/a.py"
        assert candidate.span == "10-10"
        assert candidate.seed_fetch.fetch_kind == "GATE_STATE_T_VS_G"
        assert candidate.dimension == FrontierDimension.LAYER_SPREAD
        assert candidate.addresses.triage_route == "BUG"
        assert candidate.depth == 0
        assert candidate.dispatchable is True
        assert candidate.seed_source.startswith("dormant-template:FLAG_GATED/")

    def test_cross_boundary_template_refuses_as_handoff(self):
        site = SuspectSite(
            node_id=uuid4(),
            kind=SuspectSiteKind.EXTERNAL_CALL,
            file_path="upstream/lib/a.py",
            line=30,
            detail="matched external_call syntax: resp = httpClient.post(path)",
            cross_boundary=True,
            upstream_identity="upstream",
        )
        path = _path(site)
        templates = instantiate_templates(path)
        assert len(templates) == 1
        assert templates[0].handoff_candidate is not None
        seeded, refused = seed_from_dormant_templates(
            [(path, templates[0])], _character(), EvidenceState.NO_ROWS
        )
        assert seeded == []
        assert refused == ["upstream"]

    def test_local_complete_seeds_nothing(self):
        seeded, refused = seed_from_dormant_templates(
            _pairs(), _character(), EvidenceState.LOCAL_COMPLETE
        )
        assert seeded == []
        assert refused == []

    def test_over_budget_template_still_seeds_pricing_stays_at_exit(self):
        path = _path(_site())
        template = DormantTemplate(
            kind=TemplateKind.FLAG_GATED,
            suspect_path_id=path.path_id,
            site_node_ids=[path.sites[0].node_id],
            discriminating_fetch=DiscriminatingFetch(
                fetch_kind=DiscriminatingFetchKind.GATE_STATE_T_VS_G, target="a.py:10"
            ),
            assumptions=[
                TemplateAssumption(expects="flag on at T", check_order=0, cost_estimate=10_000)
            ],
        )
        seeded, _ = seed_from_dormant_templates(
            [(path, template)], _character(), EvidenceState.NO_ROWS
        )
        assert len(seeded) == 1
        assert seeded[0].cost_estimate == 10_000


class TestSpreadDimension:
    def test_unbeaten_rival_wins_over_every_row_state(self):
        character = _character(has_unbeaten_rival=True)
        for state in (
            EvidenceState.NO_ROWS,
            EvidenceState.ROWS_WITHOUT_MATCH,
            EvidenceState.STALE_MATCH,
            EvidenceState.LOCAL_COMPLETE,
        ):
            assert character.spread_dimension(state) == FrontierDimension.RIVAL_SPREAD

    def test_row_states_map_to_postures(self):
        character = _character()
        assert character.spread_dimension(EvidenceState.NO_ROWS) == FrontierDimension.LAYER_SPREAD
        assert (
            character.spread_dimension(EvidenceState.ROWS_WITHOUT_MATCH)
            == FrontierDimension.ASSUMPTION_SPREAD
        )
        assert (
            character.spread_dimension(EvidenceState.STALE_MATCH) == FrontierDimension.TIME_SPREAD
        )
        assert character.spread_dimension(EvidenceState.LOCAL_COMPLETE) is None


def _candidate(
    cost=0,
    file_path="tenant/svc/a.py",
    span="10-11",
    status=CandidateStatus.PENDING,
    depth=0,
    ttl=10,
):
    return FrontierCandidate(
        dimension=FrontierDimension.LAYER_SPREAD,
        seed_fetch=SeedFetch(fetch_kind="GATE_STATE_T_VS_G", target="a.py:10"),
        addresses=_character(),
        cost_estimate=cost,
        file_path=file_path,
        span=span,
        status=status,
        depth=depth,
        ttl_rounds=ttl,
    )


class TestOrdering:
    def test_unknown_files_rank_first_exhausted_spans_drop(self):
        fresh = _candidate(cost=5, file_path="tenant/svc/new.py", span="1-9")
        same_span = _candidate(cost=0, file_path="tenant/svc/a.py", span="10-11")
        ordered = order_candidates([same_span, fresh], [("tenant/svc/a.py", "10-11")])
        assert [c.candidate_id for c in ordered] == [fresh.candidate_id]

    def test_same_file_new_span_is_half_novel(self):
        new_span = _candidate(cost=0, file_path="tenant/svc/a.py", span="20-25")
        new_file = _candidate(cost=0, file_path="tenant/svc/b.py", span="1-5")
        ordered = order_candidates([new_span, new_file], [("tenant/svc/a.py", "10-11")])
        assert [c.candidate_id for c in ordered] == [new_file.candidate_id, new_span.candidate_id]

    def test_non_dispatchable_and_capped_never_order(self):
        ok = _candidate()
        excluded = [
            _candidate(status=CandidateStatus.IN_FLIGHT),
            _candidate(status=CandidateStatus.EXHAUSTED),
            _candidate(status=CandidateStatus.BLOCKED_ON_TELEMETRY),
            _candidate(status=CandidateStatus.WAITING),
            _candidate(status=CandidateStatus.DEFERRED),
            _candidate(depth=MAX_FRONTIER_DEPTH + 1),
            _candidate(ttl=0),
        ]
        ordered = order_candidates([ok, *excluded], [])
        assert [c.candidate_id for c in ordered] == [ok.candidate_id]

    def test_order_is_stable_across_input_order(self):
        first = [_candidate(cost=i, file_path=f"tenant/svc/m{i}.py") for i in range(6)]
        assert order_candidates(first, []) == order_candidates(list(reversed(first)), [])

    def test_cheaper_ranks_higher_at_equal_novelty(self):
        cheap = _candidate(cost=0, file_path="tenant/svc/a.py")
        pricey = _candidate(cost=50, file_path="tenant/svc/b.py")
        ordered = order_candidates([pricey, cheap], [])
        assert [c.candidate_id for c in ordered] == [cheap.candidate_id, pricey.candidate_id]


class TestAdmissible:
    def test_depth_and_ttl_caps(self):
        assert _candidate().admissible() is True
        assert _candidate(depth=MAX_FRONTIER_DEPTH).admissible() is True
        assert _candidate(depth=MAX_FRONTIER_DEPTH + 1).admissible() is False
        assert _candidate(ttl=0).admissible() is False

    def test_candidate_requires_identity(self):
        with pytest.raises(ValueError):
            FrontierCandidate(
                dimension=FrontierDimension.LAYER_SPREAD,
                seed_fetch=SeedFetch(fetch_kind="GATE_STATE_T_VS_G", target="x"),
                addresses=_character(),
                cost_estimate=0,
            )


class TestExits:
    def test_budget_first_even_with_candidates(self):
        assert (
            evaluate_exit(
                spread_budget_remaining=0,
                ordered_pending=[_candidate()],
                top_gain_history=[],
                independent_artifact_types=3,
                posterior=0.99,
            )
            == FrontierExit.BUDGET_EXHAUSTED
        )

    def test_empty_frontier_is_fixpoint(self):
        assert (
            evaluate_exit(
                spread_budget_remaining=50,
                ordered_pending=[],
                top_gain_history=[],
                independent_artifact_types=0,
                posterior=0.0,
            )
            == FrontierExit.FIXPOINT
        )

    def test_flat_gains_for_k_rounds_diminish(self):
        assert (
            evaluate_exit(
                spread_budget_remaining=50,
                ordered_pending=[_candidate()],
                top_gain_history=[0.5, 0.001, 0.002, 0.0],
                independent_artifact_types=1,
                posterior=0.5,
            )
            == FrontierExit.DIMINISHING_RETURNS
        )

    def test_fewer_than_k_flat_rounds_continues(self):
        assert (
            evaluate_exit(
                spread_budget_remaining=50,
                ordered_pending=[_candidate()],
                top_gain_history=[0.001, 0.0],
                independent_artifact_types=1,
                posterior=0.5,
            )
            == FrontierExit.CONTINUE
        )

    def test_corroborated_confidence_exits(self):
        assert (
            evaluate_exit(
                spread_budget_remaining=50,
                ordered_pending=[_candidate()],
                top_gain_history=[0.9],
                independent_artifact_types=2,
                posterior=0.95,
            )
            == FrontierExit.CONFIDENCE_CORROBORATED
        )

    def test_single_artifact_type_never_corroborates(self):
        assert (
            evaluate_exit(
                spread_budget_remaining=50,
                ordered_pending=[_candidate()],
                top_gain_history=[0.9],
                independent_artifact_types=1,
                posterior=0.99,
            )
            == FrontierExit.CONTINUE
        )


class TestVersionPin:
    def test_frontier_version_current(self):
        assert FRONTIER_VERSION_CURRENT == "frontier-v1"
        assert DEFAULT_CANDIDATE_TTL_ROUNDS == 10
        assert MAX_FRONTIER_DEPTH == 4
        assert _candidate().version == "frontier-v1"
