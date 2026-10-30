# tests/unit/test_claim_binding_phase2.py
"""Phase 2 (claim binding): per-claim search_query prompting and ledger plumbing.

The model emits a scoped retrieval hint per claim; FORMULATE carries it into
persisted assessments (including citation-less CLAIM_UNRESOLVED rows); the
ledger prefers the stored pair over the derived heuristic; the directive
fires for never-retrieved claims via trigger (c). Stored queries never
confer locality under either pin.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from pydantic import ValidationError

from investigation_agent_platform.api.dependencies import AppContext, set_app_context
from investigation_agent_platform.application.evidence.retrieval_ledger import (
    _distinct_typed_claims,
    build_claim_ledger,
    derive_claim_query,
)
from investigation_agent_platform.application.investigation.reasoning import (
    _clean_claim_query,
    _sanitize_decision_dict,
    build_system_prompt,
)
from investigation_agent_platform.application.investigation.verification import (
    VerificationEngine,
    _match_recent_action,
    select_ledger_requery,
)
from investigation_agent_platform.application.worker.activities import (
    ExecuteActionInput,
    execute_action_activity,
)
from investigation_agent_platform.domain.hypothesis.models import (
    AssessmentStrength,
    AssessmentType,
    ClaimKind,
    ClaimTarget,
    Hypothesis,
    HypothesisEvidenceAssessment,
    HypothesisStatus,
)


def _aid():
    return uuid4()


def _hyp_with(
    statement="Widget is defined in src/widgets.py",
    assessments=None,
    status=HypothesisStatus.SUPPORTED,
):
    iid = uuid4()
    asms = list(assessments or [])
    return Hypothesis(
        tenant_id="t",
        investigation_id=iid,
        title=statement[:256],
        description=statement,
        statement=statement,
        status=status,
        supporting_evidence_ids=[a.evidence_id for a in asms if a.evidence_id is not None],
        assessments=asms,
    )


def _asm(quote="q", scope=None, kind=ClaimKind.DEFINITION, eid=None):
    return HypothesisEvidenceAssessment(
        hypothesis_id=uuid4(),
        evidence_id=eid or uuid4(),
        assessment=AssessmentType.SUPPORTS,
        strength=AssessmentStrength.STRONG,
        reason="definition",
        claim_kind=kind,
        claim_target=ClaimTarget(symbol="Widget", file_path="src/widgets.py"),
        search_query=quote,
        search_scope=scope,
    )


# ---------------------------------------------------------------------------
# Prompt and sanitizer (D1)
# ---------------------------------------------------------------------------


class TestClaimQuerySanitizer:
    def test_cleaner_matrix(self):
        assert _clean_claim_query(None, 1024) is None
        assert _clean_claim_query("", 1024) is None
        assert _clean_claim_query("   ", 1024) is None
        assert _clean_claim_query("  Widget  ", 1024) == "Widget"
        assert _clean_claim_query("x" * 2000, 1024) == "x" * 1024
        assert _clean_claim_query(42, 1024) == "42"
        assert _clean_claim_query(True, 1024) == "True"
        assert _clean_claim_query({"q": 1}, 1024) is None
        assert _clean_claim_query(["q"], 1024) is None

    def test_prompt_section_present(self):
        prompt = build_system_prompt()
        assert "## claim-queries" in prompt
        assert "search_query" in prompt

    def test_per_claim_pair_survives(self):
        cleaned = _sanitize_decision_dict(
            {
                "hypotheses_evaluated": ["s"],
                "claims": [
                    {
                        "statement": "s",
                        "claim_kind": "DEFINITION",
                        "evidence_ids": [],
                        "target": {"symbol": "Widget"},
                        "search_query": "  Widget  ",
                        "search_scope": " src/ ",
                    }
                ],
            }
        )
        claim = cleaned["claims"][0]
        assert claim["search_query"] == "Widget"
        assert claim["search_scope"] == "src/"

    def test_garbage_query_never_fabricated(self):
        cleaned = _sanitize_decision_dict(
            {
                "hypotheses_evaluated": ["s"],
                "claims": [
                    {
                        "statement": "s",
                        "search_query": {"nested": "dict"},
                        "search_scope": ["list"],
                    }
                ],
            }
        )
        assert cleaned["claims"][0]["search_query"] is None
        assert cleaned["claims"][0]["search_scope"] is None


class TestCommitSubjectRoundTrip:
    """Finding A: strict HISTORY proof needs target.commit_subject, so the
    sanitizer must preserve it and the prompt must emit it."""

    def test_sanitizer_preserves_commit_subject(self):
        cleaned = _sanitize_decision_dict(
            {
                "hypotheses_evaluated": ["s"],
                "claims": [
                    {
                        "statement": "s",
                        "claim_kind": "HISTORY",
                        "evidence_ids": [],
                        "target": {
                            "file_path": "src/orders.py",
                            "commit_id": "abc1234",
                            "commit_subject": "Fix filter edge case",
                        },
                    }
                ],
            }
        )
        assert cleaned["claims"][0]["target"]["commit_subject"] == "Fix filter edge case"

    def test_sanitizer_truncates_commit_subject(self):
        cleaned = _sanitize_decision_dict(
            {
                "hypotheses_evaluated": ["s"],
                "claims": [
                    {
                        "statement": "s",
                        "claim_kind": "HISTORY",
                        "evidence_ids": [],
                        "target": {"commit_subject": "x" * 600},
                    }
                ],
            }
        )
        assert cleaned["claims"][0]["target"]["commit_subject"] == "x" * 512

    def test_prompt_emits_commit_subject(self):
        prompt = build_system_prompt()
        assert '"commit_subject"' in prompt
        assert "target.commit_subject" in prompt

    def test_strict_history_proof_matches_sanitized_subject(self):
        from investigation_agent_platform.application.investigation.verification import (
            CLAIM_BINDING_CBV2,
        )
        from investigation_agent_platform.ports.reasoning.reasoner import DecisionClaim

        cleaned = _sanitize_decision_dict(
            {
                "hypotheses_evaluated": ["filter_orders changed recently"],
                "claims": [
                    {
                        "statement": "filter_orders changed recently",
                        "claim_kind": "HISTORY",
                        "evidence_ids": [],
                        "target": {
                            "file_path": "src/orders.py",
                            "commit_id": "abc1234",
                            "commit_subject": "Fix filter edge case",
                        },
                    }
                ],
            }
        )
        claim = DecisionClaim(**cleaned["claims"][0])
        assert claim.target is not None
        assert claim.target.commit_subject == "Fix filter edge case"
        row = SimpleNamespace(
            evidence_type="COMMIT_HISTORY",
            attributes={"discovery_only": False, "match_count": 3},
            provenance=SimpleNamespace(
                source_location=SimpleNamespace(file_path="src/orders.py", revision="abc1234def")
            ),
            summary="Fix filter edge case",
        )
        ok, _ = VerificationEngine.history_proof([row], claim.target, CLAIM_BINDING_CBV2)
        assert ok is True

    def test_top_level_query_stripped(self):
        # Layering: sanitize passes top-level keys through; the
        # _validate_llm_decision known-fields filter drops them (field
        # names only in the warning). Per-claim placement survives both.
        from investigation_agent_platform.ports.reasoning.reasoner import (
            InvestigationDecision,
        )

        cleaned = _sanitize_decision_dict(
            {
                "hypotheses_evaluated": ["s"],
                "search_query": "top-level",
                "claims": [{"statement": "s", "search_query": "kept"}],
            }
        )
        known = set(InvestigationDecision.model_fields)
        final = {k: v for k, v in cleaned.items() if k in known}
        assert "search_query" not in final
        assert cleaned["claims"][0]["search_query"] == "kept"

    def test_target_embedded_query_dropped(self):
        cleaned = _sanitize_decision_dict(
            {
                "hypotheses_evaluated": ["s"],
                "claims": [
                    {
                        "statement": "s",
                        "target": {"symbol": "Widget", "search_query": "Widget"},
                    }
                ],
            }
        )
        assert "search_query" not in cleaned["claims"][0]["target"]

    def test_edge_pair_preserved(self):
        cleaned = _sanitize_decision_dict(
            {
                "hypotheses_evaluated": ["s"],
                "claims": [
                    {
                        "statement": "s",
                        "claim_kind": "CALL_EDGE",
                        "caller_edges": [
                            {
                                "target": {"symbol": "c", "counterpart": "p"},
                                "evidence_ids": [],
                                "search_query": "p",
                                "search_scope": "src/",
                            },
                            {
                                "target": {"symbol": "c", "counterpart": "q"},
                                "evidence_ids": [],
                            },
                        ],
                    }
                ],
            }
        )
        edges = cleaned["claims"][0]["caller_edges"]
        assert edges[0]["search_query"] == "p"
        assert edges[0]["search_scope"] == "src/"
        assert edges[1]["search_query"] is None
        assert edges[1]["search_scope"] is None


# ---------------------------------------------------------------------------
# FORMULATE carry-through (D3)
# ---------------------------------------------------------------------------


def _formulate_ctx(evidence_rows, hyp_repo):
    ctx = AppContext()
    service = SimpleNamespace(
        authorize=AsyncMock(return_value=None), execute=AsyncMock(return_value=None)
    )
    ctx.execute_action_service = lambda: service  # type: ignore[method-assign]
    ctx.action_repo = SimpleNamespace(count_by_investigation=AsyncMock(return_value=0))
    ctx.evidence_repo = SimpleNamespace(
        find_by_investigation_id=AsyncMock(return_value=list(evidence_rows or [])),
        save_batch=AsyncMock(),
        save=AsyncMock(),
    )
    ctx.profile_repo = SimpleNamespace(get_by_application_id=AsyncMock(return_value=None))
    ctx.hypothesis_repo = hyp_repo
    set_app_context(ctx)


def _formulate_input(stmt, claims):
    action_input = ExecuteActionInput(
        tenant_id="tenant-a",
        investigation_id=str(uuid4()),
        action="FORMULATE_HYPOTHESIS",
        parameters={},
    )
    action_input.hypotheses = [stmt]
    action_input.claims = claims
    return action_input


class TestFormulateCarryThrough:
    @pytest.mark.asyncio
    async def test_assessment_carries_stored_pair(self):
        eid = uuid4()
        saved = []

        async def _save(tenant_id, hyp, investigation_id, expected_version=0):
            saved.append(hyp)

        _formulate_ctx(
            [SimpleNamespace(evidence_id=eid)],
            SimpleNamespace(find_by_investigation_id=AsyncMock(return_value=[]), save=_save),
        )
        stmt = "Widget is defined"
        await execute_action_activity(
            _formulate_input(
                stmt,
                [
                    {
                        "statement": stmt,
                        "claim_kind": "DEFINITION",
                        "evidence_ids": [str(eid)],
                        "target": {"symbol": "Widget"},
                        "search_query": "Widget",
                        "search_scope": "src/",
                    }
                ],
            )
        )
        assert len(saved) == 1
        asm = saved[0].assessments[0]
        assert asm.search_query == "Widget"
        assert asm.search_scope == "src/"

    @pytest.mark.asyncio
    async def test_claim_without_pair_formulates_none(self):
        eid = uuid4()
        saved = []

        async def _save(tenant_id, hyp, investigation_id, expected_version=0):
            saved.append(hyp)

        _formulate_ctx(
            [SimpleNamespace(evidence_id=eid)],
            SimpleNamespace(find_by_investigation_id=AsyncMock(return_value=[]), save=_save),
        )
        stmt = "Widget is defined"
        await execute_action_activity(
            _formulate_input(
                stmt,
                [
                    {
                        "statement": stmt,
                        "claim_kind": "DEFINITION",
                        "evidence_ids": [str(eid)],
                        "target": {"symbol": "Widget"},
                    }
                ],
            )
        )
        asm = saved[0].assessments[0]
        assert asm.search_query is None
        assert asm.search_scope is None

    @pytest.mark.asyncio
    async def test_citationless_claim_persists_unresolved_with_query(self):
        # Non-CALL_EDGE claim with no usable evidence IDs: exactly one
        # evidence_id=None CLAIM_UNRESOLVED assessment carrying the pair.
        saved = []

        async def _save(tenant_id, hyp, investigation_id, expected_version=0):
            saved.append(hyp)

        _formulate_ctx(
            [],
            SimpleNamespace(find_by_investigation_id=AsyncMock(return_value=[]), save=_save),
        )
        stmt = "Widget is defined"
        await execute_action_activity(
            _formulate_input(
                stmt,
                [
                    {
                        "statement": stmt,
                        "claim_kind": "DEFINITION",
                        "evidence_ids": ["not-a-uuid", str(uuid4())],
                        "target": {"symbol": "Widget"},
                        "search_query": "Widget",
                        "search_scope": "src/",
                    }
                ],
            )
        )
        assert len(saved) == 1
        assert len(saved[0].assessments) == 1
        asm = saved[0].assessments[0]
        assert asm.evidence_id is None
        assert asm.reason_code == "CLAIM_UNRESOLVED"
        assert asm.search_query == "Widget"
        assert asm.search_scope == "src/"
        # The group exists for the ledger join.
        groups = _distinct_typed_claims(saved[0])
        assert len(groups) == 1
        assert groups[0]["stored_query"] == "Widget"
        ledger, _binding, _unknown = build_claim_ledger(
            statement=stmt,
            claim_kind="DEFINITION",
            claim_target={"symbol": "Widget"},
            cited_ids=[],
            bank_ids=set(),
            attempts=[],
            stored_query="Widget",
            stored_scope="src/",
        )
        assert ledger.checkable is True
        assert ledger.query_source == "stored"
        assert ledger.search_query == "Widget"

    @pytest.mark.asyncio
    async def test_caller_fanout_keeps_own_pair_with_top_level_fallback(self):
        e1, e2 = uuid4(), uuid4()
        saved = []

        async def _save(tenant_id, hyp, investigation_id, expected_version=0):
            saved.append(hyp)

        _formulate_ctx(
            [SimpleNamespace(evidence_id=e1), SimpleNamespace(evidence_id=e2)],
            SimpleNamespace(find_by_investigation_id=AsyncMock(return_value=[]), save=_save),
        )
        stmt = "checkout calls price"
        await execute_action_activity(
            _formulate_input(
                stmt,
                [
                    {
                        "statement": stmt,
                        "claim_kind": "CALL_EDGE",
                        "target": {"symbol": "price"},
                        "search_query": "top",
                        "caller_edges": [
                            {
                                "target": {"symbol": "price", "counterpart": "a"},
                                "evidence_ids": [str(e1)],
                                "search_query": "edge-a",
                            },
                            {
                                "target": {"symbol": "price", "counterpart": "b"},
                                "evidence_ids": [str(e2)],
                            },
                        ],
                    }
                ],
            )
        )
        by_counterpart = {a.claim_target.counterpart: a for a in saved[0].assessments}
        assert by_counterpart["a"].search_query == "edge-a"
        assert by_counterpart["b"].search_query == "top"


# ---------------------------------------------------------------------------
# Ledger preference and fallback (D4)
# ---------------------------------------------------------------------------


def _attempt(query, banked=(), scope=None):
    return (
        list(banked),
        {"search_query": query, "scope": scope} if query else None,
    )


class TestLedgerPreference:
    def test_stored_beats_derived(self):
        # The attempt matches the stored query WITHOUT banking the cited
        # row (else the ID-overlap branch links it and the label stays
        # id_overlap — locality still comes from overlap only).
        cited = [str(_aid())]
        ledger, _binding, _u = build_claim_ledger(
            statement="Widget is defined",
            claim_kind="DEFINITION",
            claim_target={"symbol": "Widget"},
            cited_ids=cited,
            bank_ids=set(),
            attempts=[_attempt("Widget", banked=["unrelated-row"])],
            stored_query="Widget",
            stored_scope="src/",
        )
        assert ledger.link_source == "stored_query_match"
        assert ledger.query_source == "stored"
        assert ledger.search_query == "Widget"
        assert ledger.attempt_indexes == [0]

    def test_stored_miss_is_countable(self):
        ledger, _binding, _u = build_claim_ledger(
            statement="Widget is defined",
            claim_kind="DEFINITION",
            claim_target={"symbol": "Widget"},
            cited_ids=[],
            bank_ids=set(),
            attempts=[_attempt("other")],
            stored_query="Widget",
        )
        assert ledger.query_source == "stored"
        assert ledger.attempt_indexes == []
        assert ledger.checkable is True

    def test_legacy_none_is_byte_identical(self):
        cited = [str(_aid())]
        kwargs = dict(
            statement="Widget is defined",
            claim_kind="DEFINITION",
            claim_target={"symbol": "Widget"},
            cited_ids=cited,
            bank_ids=set(cited),
            attempts=[_attempt("Widget", banked=cited)],
        )
        legacy = build_claim_ledger(**kwargs)
        assert legacy[0].query_source == "derived"
        assert legacy[0].link_source in {"id_overlap", "query_match"}

    def test_normalization_strip_case_sensitive(self):
        cited = [str(_aid())]
        ledger, _b, _u = build_claim_ledger(
            statement="s",
            claim_kind="DEFINITION",
            claim_target={"symbol": "Widget"},
            cited_ids=cited,
            bank_ids=set(),
            attempts=[_attempt("Widget")],
            stored_query="  Widget  ",
        )
        assert ledger.attempt_indexes == [0]
        assert ledger.link_source == "stored_query_match"
        # Case-sensitive: lowercase attempt does not match.
        ledger2, _b2, _u2 = build_claim_ledger(
            statement="s",
            claim_kind="DEFINITION",
            claim_target={"symbol": "Widget"},
            cited_ids=cited,
            bank_ids=set(),
            attempts=[_attempt("widget")],
            stored_query="Widget",
        )
        assert ledger2.attempt_indexes == []

    def test_scope_never_gates_linkage(self):
        cited = [str(_aid())]
        ledger, _b, _u = build_claim_ledger(
            statement="s",
            claim_kind="DEFINITION",
            claim_target={"symbol": "Widget"},
            cited_ids=cited,
            bank_ids=set(),
            attempts=[_attempt("Widget", scope="repo@HEAD:/:query=Widget")],
            stored_query="Widget",
            stored_scope="repo@HEAD",
        )
        assert ledger.attempt_indexes == [0]
        assert ledger.link_source == "stored_query_match"

    def test_label_precedence_stored_over_derived(self):
        cited = [str(_aid())]
        derived, _scope = derive_claim_query("s", "DEFINITION", {"symbol": "Widget"})
        assert derived == "Widget"
        ledger, _b, _u = build_claim_ledger(
            statement="s",
            claim_kind="DEFINITION",
            claim_target={"symbol": "Widget"},
            cited_ids=cited,
            bank_ids=set(),
            attempts=[_attempt("Widget")],
            stored_query="Widget",
        )
        # Both bases match the same attempt: stored label wins.
        assert ledger.link_source == "stored_query_match"

    def test_group_precedence_first_non_empty(self):
        hyp = _hyp_with(
            assessments=[
                _asm(quote=None, eid=_aid()),
                _asm(quote="second", eid=_aid()),
            ]
        )
        groups = _distinct_typed_claims(hyp)
        assert len(groups) == 1
        assert groups[0]["stored_query"] == "second"

    def test_group_precedence_keeps_query_scope_pair_together(self):
        first = _asm(quote="first", scope=None, eid=_aid())
        second = _asm(quote="second", scope="src/", eid=_aid())
        groups = _distinct_typed_claims(_hyp_with(assessments=[first, second]))
        assert len(groups) == 1
        assert groups[0]["stored_query"] == "first"
        assert groups[0]["stored_scope"] is None

    def test_no_cross_claim_borrowing_with_stored(self):
        row_a, row_b = str(_aid()), str(_aid())
        hyp = _hyp_with(
            assessments=[
                _asm(quote="Widget", kind=ClaimKind.DEFINITION, eid=row_a),
                _asm(quote="Widget", kind=ClaimKind.CALL_EDGE, eid=row_b),
            ]
        )
        hyp = hyp.model_copy(
            update={
                "assessments": [
                    hyp.assessments[0].model_copy(
                        update={
                            "claim_target": ClaimTarget(symbol="Widget"),
                        }
                    ),
                    hyp.assessments[1].model_copy(
                        update={
                            "claim_target": ClaimTarget(symbol="price", counterpart="checkout"),
                        }
                    ),
                ]
            }
        )
        groups = _distinct_typed_claims(hyp)
        assert len(groups) == 2
        # Only the DEFINITION attempt banked row_a: CALL_EDGE stays unlinked.
        recorded = [([row_a], {"search_query": "Widget"})]
        linked = {}
        for group in groups:
            _ledger, binding, _u = build_claim_ledger(
                statement="s",
                claim_kind=str(group.get("kind_value")),
                claim_target=dict(group.get("target_dict") or {}),
                cited_ids=list(group.get("cited_ids") or []),
                bank_ids={row_a, row_b},
                attempts=recorded,
                stored_query=str(group.get("stored_query") or "") or None,
            )
            linked[str(group.get("kind_value"))] = binding.claim_local
        assert linked["DEFINITION"] == [row_a]
        assert linked["CALL_EDGE"] == []

    @pytest.mark.parametrize("pin", ["cbv1", "cbv2"])
    def test_enforcement_identical_with_and_without_pair(self, pin):
        row = str(_aid())
        hyp = _hyp_with(assessments=[_asm(quote="Widget", eid=row)])
        banked = [SimpleNamespace(evidence_id=row)]
        recorded = [([row], {"search_query": "Widget"})]
        engine = VerificationEngine(claim_binding_version=pin)
        with_pair = engine.claim_binding_enforcement(hyp, banked, recorded)
        plain = hyp.model_copy(
            update={
                "assessments": [
                    hyp.assessments[0].model_copy(
                        update={"search_query": None, "search_scope": None}
                    )
                ]
            }
        )
        without_pair = engine.claim_binding_enforcement(plain, banked, recorded)
        if pin == "cbv1":
            assert with_pair is None and without_pair is None
        else:
            assert with_pair is not None and without_pair is not None
            assert with_pair.kept_ids == without_pair.kept_ids
            assert with_pair.local_groups == without_pair.local_groups
            assert with_pair.cited_groups == without_pair.cited_groups


# ---------------------------------------------------------------------------
# Directive end to end (D5)
# ---------------------------------------------------------------------------


def _recent(action, params):
    return [{"action": action, "parameters": params}]


class TestDirectiveTriggerC:
    def test_unresolved_claim_with_stored_query_directs(self):
        hyp = _hyp_with(
            assessments=[
                HypothesisEvidenceAssessment(
                    hypothesis_id=uuid4(),
                    evidence_id=None,
                    assessment=AssessmentType.SUPPORTS,
                    strength=AssessmentStrength.MODERATE,
                    reason="claim unresolved: no usable evidence",
                    claim_kind=ClaimKind.DEFINITION,
                    claim_target=ClaimTarget(symbol="Widget"),
                    search_query="Widget",
                    search_scope="src/",
                    reason_code="CLAIM_UNRESOLVED",
                    schema_version="v2",
                )
            ]
        )
        directive = select_ledger_requery(
            hyp,
            [],
            [],
            _recent("SEARCH_CODE", {"query": "Widget"}),
            3,
        )
        assert directive is not None

    def test_unresolved_claim_without_mapping_yields_none(self):
        hyp = _hyp_with(
            assessments=[
                HypothesisEvidenceAssessment(
                    hypothesis_id=uuid4(),
                    evidence_id=None,
                    assessment=AssessmentType.SUPPORTS,
                    strength=AssessmentStrength.MODERATE,
                    reason="claim unresolved: no usable evidence",
                    claim_kind=ClaimKind.DEFINITION,
                    claim_target=ClaimTarget(symbol="Widget"),
                    search_query="Widget",
                    reason_code="CLAIM_UNRESOLVED",
                    schema_version="v2",
                )
            ]
        )
        assert (
            select_ledger_requery(hyp, [], [], _recent("PROBE_API", {"endpoint_id": "x"}), 3)
            is None
        )

    def test_unresolved_claim_with_prior_attempt_yields_none(self):
        hyp = _hyp_with(
            assessments=[
                HypothesisEvidenceAssessment(
                    hypothesis_id=uuid4(),
                    evidence_id=None,
                    assessment=AssessmentType.SUPPORTS,
                    strength=AssessmentStrength.MODERATE,
                    reason="claim unresolved: no usable evidence",
                    claim_kind=ClaimKind.DEFINITION,
                    claim_target=ClaimTarget(symbol="Widget"),
                    search_query="Widget",
                    reason_code="CLAIM_UNRESOLVED",
                    schema_version="v2",
                )
            ]
        )
        recorded = [([], {"search_query": "Widget", "scope": "src/"})]
        assert (
            select_ledger_requery(hyp, [], recorded, _recent("SEARCH_CODE", {"query": "Widget"}), 3)
            is None
        )

    def test_unresolved_claim_with_failed_prior_attempt_retries(self):
        hyp = _hyp_with(
            assessments=[
                HypothesisEvidenceAssessment(
                    hypothesis_id=uuid4(),
                    evidence_id=None,
                    assessment=AssessmentType.SUPPORTS,
                    strength=AssessmentStrength.MODERATE,
                    reason="claim unresolved: no usable evidence",
                    claim_kind=ClaimKind.DEFINITION,
                    claim_target=ClaimTarget(symbol="Widget"),
                    search_query="Widget",
                    search_scope="src/",
                    reason_code="CLAIM_UNRESOLVED",
                    schema_version="v2",
                )
            ]
        )
        recorded = [([], {"search_query": "Widget", "scope": "src/", "outcome": "SEARCH_FAILED"})]
        directive = select_ledger_requery(
            hyp, [], recorded, _recent("SEARCH_CODE", {"query": "Widget"}), 3
        )
        assert directive is not None
        assert directive["scope"] == "src/"

    def test_motivating_gap_stored_maps_where_derived_does_not(self):
        # The recent SEARCH_CODE derives a synthesized scope the claim's
        # scope-less derived guess cannot match; the stored pair's
        # query-only pass maps it.
        unknown = str(_aid())
        stored = _hyp_with(assessments=[_asm(quote="Widget", eid=unknown)])
        plain = _hyp_with(
            assessments=[
                _asm(quote=None, eid=unknown).model_copy(
                    update={"claim_target": ClaimTarget(symbol="Widget")}
                )
            ]
        )
        recent = _recent("SEARCH_CODE", {"query": "Widget"})
        assert select_ledger_requery(plain, [], [], recent, 3) is None
        assert select_ledger_requery(stored, [], [], recent, 3) is not None

    def test_budget_still_gates(self):
        unknown = str(_aid())
        hyp = _hyp_with(assessments=[_asm(quote="Widget", eid=unknown)])
        recent = _recent("SEARCH_CODE", {"query": "Widget"})
        # Durable count at max: no directive even with a stored query.
        recorded = [
            ([], {"search_query": "Widget"}),
            ([], {"search_query": "Widget"}),
            ([], {"search_query": "Widget"}),
        ]
        assert select_ledger_requery(hyp, [], recorded, recent, 3) is None

    def test_same_query_different_scope_does_not_suppress_retry(self):
        # Finding B: stored linkage is query-only, but the durable budget
        # counts (query, scope). A same-query attempt at another scope must
        # not suppress the never-retrieved trigger for the stored pair.
        def _hyp():
            return _hyp_with(
                assessments=[
                    HypothesisEvidenceAssessment(
                        hypothesis_id=uuid4(),
                        evidence_id=None,
                        assessment=AssessmentType.SUPPORTS,
                        strength=AssessmentStrength.MODERATE,
                        reason="claim unresolved: no usable evidence",
                        claim_kind=ClaimKind.DEFINITION,
                        claim_target=ClaimTarget(symbol="Widget"),
                        search_query="Widget",
                        search_scope="src/",
                        reason_code="CLAIM_UNRESOLVED",
                        schema_version="v2",
                    )
                ]
            )

        other_scope = [([], {"search_query": "Widget", "scope": "lib/"})]
        directive = select_ledger_requery(
            _hyp(), [], other_scope, _recent("SEARCH_CODE", {"query": "Widget"}), 3
        )
        assert directive is not None
        assert directive["scope"] == "src/"

    def test_same_query_same_scope_still_suppresses(self):
        def _hyp():
            return _hyp_with(
                assessments=[
                    HypothesisEvidenceAssessment(
                        hypothesis_id=uuid4(),
                        evidence_id=None,
                        assessment=AssessmentType.SUPPORTS,
                        strength=AssessmentStrength.MODERATE,
                        reason="claim unresolved: no usable evidence",
                        claim_kind=ClaimKind.DEFINITION,
                        claim_target=ClaimTarget(symbol="Widget"),
                        search_query="Widget",
                        search_scope="src/",
                        reason_code="CLAIM_UNRESOLVED",
                        schema_version="v2",
                    )
                ]
            )

        same_scope = [([], {"search_query": "Widget", "scope": "src/"})]
        assert (
            select_ledger_requery(
                _hyp(), [], same_scope, _recent("SEARCH_CODE", {"query": "Widget"}), 3
            )
            is None
        )


class TestStoredScopeDispatch:
    """Finding C: the stored scope rides the ledger budget key as "scope";
    the SEARCH_CODE handler must map its path-prefix form onto the
    path_prefix parameter the provider actually reads."""

    def _ctx_with_capture(self, seen):
        from investigation_agent_platform.application.worker.activities import (
            ExecuteActionInput as _In,
        )

        ctx = AppContext()
        service = SimpleNamespace(
            authorize=AsyncMock(return_value=None),
            execute=AsyncMock(return_value=None),
        )
        ctx.execute_action_service = lambda: service  # type: ignore[method-assign]
        ctx.action_repo = SimpleNamespace(count_by_investigation=AsyncMock(return_value=0))
        ctx.evidence_repo = SimpleNamespace(
            find_by_investigation_id=AsyncMock(return_value=[]),
            save_batch=AsyncMock(return_value=None),
            save=AsyncMock(return_value=None),
        )
        ctx.profile_repo = SimpleNamespace(get_by_application_id=AsyncMock(return_value=None))

        async def _search_code(tenant_id, investigation_id, application_id, req):
            seen.append(req)
            return SimpleNamespace(items=[], provider_metadata={})

        ctx.evidence_gateway = SimpleNamespace(search_code=_search_code)
        ctx.hypothesis_repo = SimpleNamespace(
            find_by_investigation_id=AsyncMock(return_value=[]),
            save=AsyncMock(return_value=None),
        )
        set_app_context(ctx)
        return _In

    @pytest.mark.asyncio
    async def test_scope_maps_to_path_prefix(self):
        from investigation_agent_platform.application.worker.activities import (
            ExecuteActionInput,
            execute_action_activity,
        )

        seen: list = []
        self._ctx_with_capture(seen)
        result = await execute_action_activity(
            ExecuteActionInput(
                tenant_id="t",
                investigation_id=str(uuid4()),
                action="SEARCH_CODE",
                parameters={"query": "Widget", "scope": "src/"},
            )
        )
        assert result.success is True
        assert len(seen) == 1
        assert seen[0].path_prefix == "src/"

    @pytest.mark.asyncio
    async def test_explicit_path_prefix_wins(self):
        from investigation_agent_platform.application.worker.activities import (
            ExecuteActionInput,
            execute_action_activity,
        )

        seen: list = []
        self._ctx_with_capture(seen)
        result = await execute_action_activity(
            ExecuteActionInput(
                tenant_id="t",
                investigation_id=str(uuid4()),
                action="SEARCH_CODE",
                parameters={"query": "Widget", "scope": "src/", "path_prefix": "lib/"},
            )
        )
        assert result.success is True
        assert seen[0].path_prefix == "lib/"

    @pytest.mark.asyncio
    async def test_repository_revision_scope_maps_to_no_prefix(self):
        # Documented limitation: repository@revision scopes carry no path
        # information, so the handler keeps its default rather than
        # searching a fabricated prefix.
        from investigation_agent_platform.application.worker.activities import (
            ExecuteActionInput,
            execute_action_activity,
        )

        seen: list = []
        self._ctx_with_capture(seen)
        result = await execute_action_activity(
            ExecuteActionInput(
                tenant_id="t",
                investigation_id=str(uuid4()),
                action="SEARCH_CODE",
                parameters={"query": "Widget", "scope": "example-app@abc123"},
            )
        )
        assert result.success is True
        assert seen[0].path_prefix is None

    def test_scope_path_prefix_unit(self):
        from investigation_agent_platform.application.worker.activities.execution import (
            _scope_path_prefix,
        )

        assert _scope_path_prefix("src/") == "src/"
        assert _scope_path_prefix("  src/  ") == "src/"
        assert _scope_path_prefix("example-app@abc123") is None
        assert _scope_path_prefix("") is None
        assert _scope_path_prefix(None) is None

    def test_match_recent_action_two_pass_scope(self):
        from investigation_agent_platform.application.evidence.retrieval_ledger import (
            ClaimRetrievalLedger,
        )

        stored = ClaimRetrievalLedger(
            claim_key="k",
            search_query="Widget",
            scope="src/",
            checkable=True,
            query_source="stored",
        )
        # Attempt scope differs in synthesized form: query-only pass matches.
        recent = [
            {
                "action": "SEARCH_CODE",
                "parameters": {"query": "Widget"},
                "retrieval_attempt": {
                    "search_query": "Widget",
                    "scope": "repo@HEAD:/:query=Widget",
                },
            }
        ]
        action_type, params = _match_recent_action(stored, recent)
        assert action_type == "SEARCH_CODE"
        assert params["scope"] == "src/"
        # Derived ledger with the same mismatch does not match.
        derived = ClaimRetrievalLedger(
            claim_key="k",
            search_query="Widget",
            scope="src/",
            checkable=True,
            query_source="derived",
        )
        action_type2, _p2 = _match_recent_action(derived, recent)
        assert action_type2 is None


# ---------------------------------------------------------------------------
# Telemetry and battery (D7)
# ---------------------------------------------------------------------------


class TestStoredTelemetry:
    def test_closed_vocabularies(self):
        for stored_query, expected_source in (("Widget", "stored"), (None, "derived")):
            ledger, _b, _u = build_claim_ledger(
                statement="s",
                claim_kind="DEFINITION",
                claim_target={"symbol": "Widget"},
                cited_ids=[],
                bank_ids=set(),
                attempts=[],
                stored_query=stored_query,
            )
            assert ledger.query_source == expected_source
            assert ledger.link_source == "id_overlap"

    def test_closed_vocabularies_reject_unknown_values(self):
        from investigation_agent_platform.application.evidence.retrieval_ledger import (
            ClaimRetrievalLedger,
        )

        with pytest.raises(ValidationError):
            ClaimRetrievalLedger(claim_key="k", query_source="unknown")
        with pytest.raises(ValidationError):
            ClaimRetrievalLedger(claim_key="k", link_source="unknown")

    def test_stored_query_rate_counts_persisted_only(self):
        attrs = {
            "ledgers": [
                {"query_source": "stored", "link_source": "stored_query_match"},
                {"query_source": "stored", "link_source": "id_overlap"},
                {"query_source": "derived", "link_source": "query_match"},
                {},  # legacy row without query_source reads as derived
            ]
        }
        stored = [
            l for l in attrs["ledgers"] if str(l.get("query_source") or "derived") == "stored"
        ]
        linked = [l for l in stored if str(l.get("link_source") or "") == "stored_query_match"]
        assert len(stored) == 2
        assert len(linked) == 1
