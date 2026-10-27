# tests/unit/test_claim_binding_phase4.py
"""Phase 4 (claim binding): smallest-fetch forcing + NOT_CHECKABLE denominator.

Covers D1 (shared core, builders, dispatch validation, cascade wiring
contracts), D2 (gap feed, carrier, gap_id stamp), D3 (RETRYABLE freeze,
directive key discipline), D4 (compiler pin, partition, pool carry, body
queue), D5 (NOT_CHECKABLE taxonomy, sanitizer, evaluate projection),
D6 (fast-path predicate, break helper), D7 (battery dimension).
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from investigation_agent_platform.application.evidence.retrieval_ledger import (
    RETRYABLE_ACTIONS,
    claim_key,
    claim_ref,
)
from investigation_agent_platform.application.investigation.reasoning import (
    _sanitize_decision_dict,
)
from investigation_agent_platform.application.investigation.verification import (
    _SMALLEST_FETCH_ORDER,
    LimitationCode,
    RootCauseVerificationPolicy,
    VerificationEngine,
    VerificationStatus,
    build_claim_gaps,
)
from investigation_agent_platform.application.worker.workflows.forcing import (
    KIND_FETCH,
    SMALLEST_FETCH_ACTIONS_BY_KIND,
    SMALLEST_FETCH_ORDER,
    _behavior_fetch_params,
    _carry_claim_gap_feed,
    _forced_conclude_break,
    _should_force_conclude_not_checkable,
    _smallest_fetch_already_covered,
    _smallest_fetch_dispatchable,
    _smallest_fetch_target,
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

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import tranche_b_battery as battery


def _aid():
    return uuid4()


def _asm(kind, target=None, evidence_id=None, query=None, scope=None):
    return HypothesisEvidenceAssessment(
        hypothesis_id=_aid(),
        evidence_id=evidence_id,
        assessment=AssessmentType.NEUTRAL,
        strength=AssessmentStrength.MODERATE,
        reason="r",
        claim_kind=kind,
        claim_target=target,
        search_query=query,
        search_scope=scope,
    )


def _hyp(statement="Widget is defined in src/widgets.py", assessments=None):
    iid = uuid4()
    asms = list(assessments or [])
    return Hypothesis(
        tenant_id="t",
        investigation_id=iid,
        title=statement[:256],
        description=statement,
        statement=statement,
        status=HypothesisStatus.PROPOSED,
        supporting_evidence_ids=[a.evidence_id for a in asms if a.evidence_id is not None],
        assessments=asms,
    )


def _target(**kw):
    base = {"file_path": "src/widgets.py", "symbol": "Widget"}
    base.update(kw)
    return ClaimTarget(**base)


def _gap(kind="DEFINITION", gap_id="DEFINITION|abc123", target=None, query="", scope=""):
    if target is None:
        target = {"file_path": "src/widgets.py", "symbol": "Widget"}
    return {
        "gap_id": gap_id,
        "claim_ref": "ref1",
        "kind": kind,
        "target_key": gap_id.split("|", 1)[-1],
        "target": target,
        "query": query,
        "scope": scope,
    }


class TestKindTableAgreement:
    def test_kind_fetch_keys_equal_evaluation_order(self):
        assert set(KIND_FETCH) == set(SMALLEST_FETCH_ORDER)
        assert len(SMALLEST_FETCH_ORDER) == 5

    def test_every_builder_resolves_to_callable(self):
        for kind in SMALLEST_FETCH_ORDER:
            assert callable(KIND_FETCH[kind]["builder"]), kind

    def test_retryable_actions_frozen_at_four(self):
        assert RETRYABLE_ACTIONS == frozenset(
            {"SEARCH_CODE", "FIND_SYMBOL", "FIND_CALL_GRAPH", "GET_CODE_HISTORY"}
        )

    def test_sort_order_agrees_with_fire_order(self):
        """F5 pin: D2 sort order and D1 fire order are one order."""
        assert tuple(_SMALLEST_FETCH_ORDER) == tuple(SMALLEST_FETCH_ORDER)


class TestBuilders:
    def test_definition_builds_window_params(self):
        params = KIND_FETCH["DEFINITION"]["builder"](
            _gap(target={"file_path": "src/a.py", "symbol": "S", "line_start": 10, "line_end": 20})
        )
        assert params["file_path"] == "src/a.py"
        assert params["start_line"] == 10
        assert params["end_line"] == 20
        assert "claim_ref" not in params and "gap_id" not in params

    def test_definition_none_without_file(self):
        assert KIND_FETCH["DEFINITION"]["builder"](_gap(target={})) is None

    def test_definition_path_prefix_scope_falls_back_to_head(self):
        """F2 pin: a Phase 2 search scope (path prefix) is not a revision."""
        params = KIND_FETCH["DEFINITION"]["builder"](
            _gap(
                target={"file_path": "src/a.py", "symbol": "S"},
                scope="src/",
            )
        )
        assert params is not None and params["revision"] == "HEAD"

    def test_definition_sha_scope_forwarded(self):
        params = KIND_FETCH["DEFINITION"]["builder"](
            _gap(
                target={"file_path": "src/a.py", "symbol": "S"},
                scope="abc1234",
            )
        )
        assert params is not None and params["revision"] == "abc1234"

    def test_history_builds_file_symbol_limit(self):
        params = KIND_FETCH["HISTORY"]["builder"](_gap())
        assert params["file_path"] == "src/widgets.py"
        assert params["symbol"] == "Widget"
        assert params["limit"] == 10

    def test_behavior_log_leg(self):
        params = _behavior_fetch_params(_gap(query="null pointer"))
        assert params is not None and "keywords" in params
        assert "_leg" not in params

    def test_behavior_query_state_leg(self):
        params = _behavior_fetch_params(_gap(target={"scope": "production:environment"}, query=""))
        assert params is not None and params.get("_leg") == "QUERY_STATE"

    def test_behavior_none_without_inputs(self):
        assert _behavior_fetch_params(_gap(target={}, query="")) is None

    def test_absence_builds_scoped_query(self):
        params = KIND_FETCH["ABSENCE"]["builder"](_gap(query="missing_symbol", scope="src/"))
        assert params["query"] == "missing_symbol"
        assert params["path_prefix"] == "src/"
        assert params["revision"] == "HEAD"

    def test_absence_none_without_query(self):
        assert KIND_FETCH["ABSENCE"]["builder"](_gap(target={}, query="")) is None

    def test_causal_builds_window(self):
        params = KIND_FETCH["CAUSAL"]["builder"](_gap())
        assert params["file_path"] == "src/widgets.py"


class TestSmallestFetchTarget:
    def test_fires_on_matching_open_gap(self):
        out = _smallest_fetch_target([], 0, [_gap()], kind="DEFINITION")
        assert out is not None
        assert out["next_action"] == "GET_CODE"
        assert out["gap_id"] == "DEFINITION|abc123"
        assert out["fetch_kind"] == "DEFINITION"
        assert set(out) == {
            "next_action",
            "action_parameters",
            "gap_id",
            "claim_ref",
            "fetch_kind",
        }
        assert "claim_ref" not in out["action_parameters"]
        assert "target_key" not in out["action_parameters"]
        assert not any(k.startswith("_") for k in out["action_parameters"])

    def test_silent_on_kind_mismatch(self):
        assert _smallest_fetch_target([], 0, [_gap()], kind="HISTORY") is None

    def test_silent_at_cap(self):
        assert _smallest_fetch_target([], 2, [_gap()], kind="DEFINITION") is None

    def test_silent_when_covered(self):
        history = [{"forced_smallest_fetch_gap_id": "DEFINITION|abc123"}]
        assert _smallest_fetch_target(history, 0, [_gap()], kind="DEFINITION") is None

    def test_silent_on_unbuildable_gap(self):
        assert _smallest_fetch_target([], 0, [_gap(target={})], kind="DEFINITION") is None

    def test_gap_without_gap_id_skipped(self):
        assert _smallest_fetch_target([], 0, [{"kind": "DEFINITION"}], kind="DEFINITION") is None

    def test_malformed_sibling_does_not_suppress_open_gap(self):
        """Finding-1 pin: one malformed entry skips, never aborts the kind."""

        class _BadDict(dict):
            def get(self, *args, **kwargs):
                raise RuntimeError("boom")

        out = _smallest_fetch_target(
            [], 0, [_BadDict({"kind": "DEFINITION"}), _gap()], kind="DEFINITION"
        )
        assert out is not None
        assert out["gap_id"] == "DEFINITION|abc123"

    def test_behavior_query_state_leg_dispatches(self):
        out = _smallest_fetch_target(
            [],
            0,
            [
                _gap(
                    kind="BEHAVIOR",
                    gap_id="BEHAVIOR|xyz",
                    target={"scope": "production:environment"},
                    query="",
                )
            ],
            kind="BEHAVIOR",
        )
        assert out is not None
        assert out["next_action"] == "QUERY_STATE"
        assert "_leg" not in out["action_parameters"]

    def test_fixed_order_two_gaps(self):
        gaps = [
            _gap(kind="CAUSAL", gap_id="CAUSAL|z"),
            _gap(kind="DEFINITION", gap_id="DEFINITION|a"),
        ]
        first = None
        for kind in SMALLEST_FETCH_ORDER:
            first = _smallest_fetch_target([], 0, gaps, kind=kind)
            if first:
                break
        assert first is not None and first["fetch_kind"] == "DEFINITION"

    def test_same_kind_two_targets_independent(self):
        gaps = [
            _gap(kind="DEFINITION", gap_id="DEFINITION|a"),
            _gap(kind="DEFINITION", gap_id="DEFINITION|b"),
        ]
        history = [{"forced_smallest_fetch_gap_id": "DEFINITION|a"}]
        out = _smallest_fetch_target(history, 0, gaps, kind="DEFINITION")
        assert out is not None and out["gap_id"] == "DEFINITION|b"


class TestDispatchValidation:
    def test_rejects_cross_kind(self):
        assert _smallest_fetch_dispatchable("BEHAVIOR", "GET_CODE", {"keywords": ["x"]}) is False

    def test_rejects_empty_params(self):
        assert _smallest_fetch_dispatchable("DEFINITION", "GET_CODE", {}) is False

    def test_rejects_unknown_action(self):
        assert (
            _smallest_fetch_dispatchable("DEFINITION", "TRACE_LOGIC", {"file_path": "a"}) is False
        )

    def test_accepts_mapped_pair(self):
        assert (
            _smallest_fetch_dispatchable("HISTORY", "GET_CODE_HISTORY", {"file_path": "a"}) is True
        )

    def test_accepts_behavior_query_state_leg(self):
        assert (
            _smallest_fetch_dispatchable("BEHAVIOR", "QUERY_STATE", {"template_id": "default"})
            is True
        )

    def test_returns_bool_not_none(self):
        assert _smallest_fetch_dispatchable("NOPE", "NOPE", {}) is False

    def test_allowlist_covers_kind_fetch_actions(self):
        for kind, spec in KIND_FETCH.items():
            assert spec["action"] in SMALLEST_FETCH_ACTIONS_BY_KIND[kind]
        assert "QUERY_STATE" in SMALLEST_FETCH_ACTIONS_BY_KIND["BEHAVIOR"]


class TestGapFeed:
    def test_uncited_group_produces_gap(self):
        hyp = _hyp(assessments=[_asm(ClaimKind.DEFINITION, _target())])
        feed = build_claim_gaps(hyp, set())
        assert len(feed["gaps"]) == 1
        gap = feed["gaps"][0]
        assert gap["gap_id"].startswith("DEFINITION|")
        assert gap["kind"] == "DEFINITION"
        assert feed["not_checkable_groups"] == []

    def test_cited_group_produces_no_gap(self):
        hyp = _hyp(assessments=[_asm(ClaimKind.DEFINITION, _target(), evidence_id=_aid())])
        feed = build_claim_gaps(hyp, {f"DEFINITION|{_tkey(hyp)}"})
        assert feed["gaps"] == []

    def test_claim_ref_is_informational_via_statement_kind(self):
        hyp = _hyp(
            statement="Widget is defined in src/widgets.py",
            assessments=[_asm(ClaimKind.DEFINITION, _target())],
        )
        feed = build_claim_gaps(hyp, set())
        expected = claim_ref(claim_key("Widget is defined in src/widgets.py", "DEFINITION"))
        assert feed["gaps"][0]["claim_ref"] == expected

    def test_two_targets_two_gaps(self):
        hyp = _hyp(
            assessments=[
                _asm(ClaimKind.DEFINITION, _target(file_path="src/a.py")),
                _asm(ClaimKind.DEFINITION, _target(file_path="src/b.py")),
            ]
        )
        feed = build_claim_gaps(hyp, set())
        assert len(feed["gaps"]) == 2
        assert feed["gaps"][0]["gap_id"] != feed["gaps"][1]["gap_id"]

    def test_same_target_reworded_statement_no_refire(self):
        mk = lambda stmt: _hyp(statement=stmt, assessments=[_asm(ClaimKind.DEFINITION, _target())])
        g1 = build_claim_gaps(mk("Widget is defined in src/widgets.py"), set())["gaps"]
        g2 = build_claim_gaps(mk("Widget lives in src/widgets.py"), set())["gaps"]
        assert len(g1) == len(g2) == 1
        assert g1[0]["gap_id"] == g2[0]["gap_id"]

    def test_not_checkable_inventoried_not_gapped(self):
        hyp = _hyp(assessments=[_asm(ClaimKind.NOT_CHECKABLE, _target())])
        feed = build_claim_gaps(hyp, set())
        assert feed["gaps"] == []
        assert len(feed["not_checkable_groups"]) == 1

    def test_untyped_and_untargeted_skipped(self):
        hyp = _hyp(
            assessments=[
                _asm(None, _target()),
                _asm(ClaimKind.DEFINITION, None),
            ]
        )
        feed = build_claim_gaps(hyp, set())
        assert feed["gaps"] == []

    def test_sorted_in_fetch_order(self):
        hyp = _hyp(
            assessments=[
                _asm(ClaimKind.CAUSAL, _target(file_path="src/c.py")),
                _asm(ClaimKind.DEFINITION, _target(file_path="src/a.py")),
            ]
        )
        feed = build_claim_gaps(hyp, set())
        assert [g["kind"] for g in feed["gaps"]] == ["DEFINITION", "CAUSAL"]

    def test_import_failure_returns_error_envelope(self, monkeypatch):
        import investigation_agent_platform.application.investigation.verification as V

        def _boom(hypothesis):
            raise RuntimeError("ledger gone")

        monkeypatch.setattr(V, "_distinct_typed_claims", _boom)
        hyp = _hyp(assessments=[_asm(ClaimKind.DEFINITION, _target())])
        feed = build_claim_gaps(hyp, set())
        assert feed["gaps"] == []
        assert feed.get("error") == "ledger_import_failed"


def _tkey(hyp):
    from investigation_agent_platform.application.evidence.retrieval_ledger import (
        _distinct_typed_claims,
        target_group_key,
    )

    groups = _distinct_typed_claims(hyp)
    assert len(groups) == 1
    tkey, _ = target_group_key(groups[0]["target_dict"])
    return tkey


class TestCarrier:
    def test_valid_feed_carried(self):
        out = _carry_claim_gap_feed({"claim_gaps": [_gap()]}, 3)
        assert out is not None
        assert out["kind"] == "claim_gap_feed"
        assert out["turn_issued"] == 3
        assert len(out["gaps"]) == 1

    def test_empty_list_is_tombstone(self):
        out = _carry_claim_gap_feed({"claim_gaps": []}, 4)
        assert out is not None and out["gaps"] == []

    def test_malformed_returns_none(self):
        assert _carry_claim_gap_feed({}, 1) is None
        assert _carry_claim_gap_feed({"claim_gaps": "nope"}, 1) is None
        assert _carry_claim_gap_feed({"claim_gaps": [{"kind": "DEFINITION"}]}, 1) is None

    def test_unknown_kind_dropped_with_count(self):
        out = _carry_claim_gap_feed(
            {"claim_gaps": [_gap(), {**_gap(), "kind": "CALL_EDGE", "gap_id": "CALL_EDGE|x"}]}, 1
        )
        assert out is not None and len(out["gaps"]) == 1
        assert out["dropped"]["unknown_kind"] == 1

    def test_missing_gap_id_dropped_with_count(self):
        out = _carry_claim_gap_feed({"claim_gaps": [_gap(), {"kind": "DEFINITION"}]}, 1)
        assert out is not None and len(out["gaps"]) == 1
        assert out["dropped"]["missing_gap_id"] == 1

    def test_duplicates_deduped_first_wins(self):
        out = _carry_claim_gap_feed({"claim_gaps": [_gap(), _gap()]}, 1)
        assert out is not None and len(out["gaps"]) == 1
        assert out["dropped"]["duplicate_gap_id"] == 1


class TestGapDedupe:
    def test_stamp_match(self):
        history = [{"forced_smallest_fetch_gap_id": "DEFINITION|x"}]
        assert _smallest_fetch_already_covered(history, "DEFINITION|x") is True

    def test_no_match(self):
        history = [{"forced_smallest_fetch_gap_id": "DEFINITION|x"}]
        assert _smallest_fetch_already_covered(history, "DEFINITION|y") is False

    def test_empty_history_and_gap_id(self):
        assert _smallest_fetch_already_covered([], "DEFINITION|x") is False
        assert _smallest_fetch_already_covered([{}], "") is False


class TestFastPathPredicate:
    def _reason(self, stmts, claims):
        return {"hypotheses_evaluated": stmts, "claims": claims}

    def _claim(self, statement, kind):
        return {"statement": statement, "claim_kind": kind}

    def test_all_opinion_fires(self):
        rd = self._reason(["s1"], [self._claim("s1", "NOT_CHECKABLE")])
        assert _should_force_conclude_not_checkable(rd) is True

    def test_mixed_does_not_fire(self):
        rd = self._reason(
            ["s1", "s2"],
            [self._claim("s1", "NOT_CHECKABLE"), self._claim("s2", "BEHAVIOR")],
        )
        assert _should_force_conclude_not_checkable(rd) is False

    def test_empty_fires_not(self):
        assert _should_force_conclude_not_checkable({}) is False
        assert (
            _should_force_conclude_not_checkable({"hypotheses_evaluated": [], "claims": []})
            is False
        )

    def test_unmatched_statement_disables(self):
        rd = self._reason(["s1", "s2"], [self._claim("s1", "NOT_CHECKABLE")])
        assert _should_force_conclude_not_checkable(rd) is False

    def test_statement_join_casefolded(self):
        rd = self._reason(["  S1 "], [self._claim("s1", "NOT_CHECKABLE")])
        assert _should_force_conclude_not_checkable(rd) is True

    def test_defaulted_kind_is_checkable(self):
        rd = self._reason(["s1"], [{"statement": "s1"}])
        assert _should_force_conclude_not_checkable(rd) is False

    def test_break_helper(self):
        assert _forced_conclude_break({"forced_not_checkable": True}) is True
        assert _forced_conclude_break({}) is False
        assert _forced_conclude_break(None) is False

    def test_d6_fast_path_cascade_guards(self):
        """F1 pin: every cascade override after the D6 fast path must be
        guarded by _d6_fired, so a later rule cannot clobber the forced
        CONCLUDE while the flag stays set. Counts are structural: adding
        a cascade override without the guard breaks the ratio and forces
        a conscious update here."""
        import pathlib

        src = (
            (
                pathlib.Path(__file__).resolve().parents[2]
                / "src"
                / "investigation_agent_platform"
                / "application"
                / "worker"
                / "workflows"
                / "run_investigation.py"
            )
            .read_text()
            .splitlines()
        )
        start = next(i for i, l in enumerate(src) if "opinion-only fast path" in l)
        end = next(i for i, l in enumerate(src) if "smallest-fetch slot" in l)
        region = src[start:end]
        overrides = [l for l in region if '_reason_data["next_action"] =' in l]
        guards = [l for l in region if "not _d6_fired" in l or "or _d6_fired" in l]
        assert len(overrides) == 12, f"cascade override count changed: {len(overrides)}"
        assert len(guards) == len(overrides) - 1, (
            f"D6 fast path has {len(overrides)} overrides but only "
            f"{len(guards)} downstream guards — an override can clobber "
            "the forced CONCLUDE"
        )


class TestNotCheckableTaxonomy:
    def test_enum_member_exists(self):
        assert ClaimKind.NOT_CHECKABLE.value == "NOT_CHECKABLE"

    def test_limitation_member_exists(self):
        assert LimitationCode.NOT_CHECKABLE_SKIPPED.value == "NOT_CHECKABLE_SKIPPED"

    def test_sanitizer_normalizes_case(self):
        out = _sanitize_decision_dict(
            {"claims": [{"statement": "s", "claim_kind": "not_checkable"}]}
        )
        assert out["claims"][0]["claim_kind"] == "NOT_CHECKABLE"

    def test_sanitizer_normalizes_whitespace_case(self):
        out = _sanitize_decision_dict(
            {"claims": [{"statement": "s", "claim_kind": " Not_Checkable "}]}
        )
        assert out["claims"][0]["claim_kind"] == "NOT_CHECKABLE"

    def test_sanitizer_coerces_garbage_to_causal(self):
        out = _sanitize_decision_dict({"claims": [{"statement": "s", "claim_kind": "BOGUS"}]})
        assert out["claims"][0]["claim_kind"] == "CAUSAL"

    def test_sanitizer_keeps_valid_members(self):
        out = _sanitize_decision_dict({"claims": [{"statement": "s", "claim_kind": "BEHAVIOR"}]})
        assert out["claims"][0]["claim_kind"] == "BEHAVIOR"

    def test_check_claim_kind_not_checkable(self):
        engine = VerificationEngine(claim_binding_version="cbv2")
        hyp = SimpleNamespace(claim_kind=ClaimKind.NOT_CHECKABLE)
        assert engine.check_claim_kind(hyp, []) == (False, "NOT_CHECKABLE_SKIPPED")

    def test_check_claim_kind_causal_none(self):
        engine = VerificationEngine(claim_binding_version="cbv2")
        hyp = SimpleNamespace(claim_kind=ClaimKind.CAUSAL)
        assert engine.check_claim_kind(hyp, []) is None

    def test_evaluate_all_opinion_inconclusive_with_limitation(self):
        engine = VerificationEngine(claim_binding_version="cbv2")
        hyp = _hyp(
            statement="Best practice is X",
            assessments=[_asm(ClaimKind.NOT_CHECKABLE, _target())],
        )
        result = engine.evaluate_hypothesis(hyp, [], [], [], RootCauseVerificationPolicy())
        assert result.status == VerificationStatus.INCONCLUSIVE
        codes = [getattr(lim, "code", None) for lim in (result.limitations or [])]
        assert LimitationCode.NOT_CHECKABLE_SKIPPED in codes
        assert result.corroborating_evidence_ids == []

    def test_evaluate_mixed_projects_checkable_subset(self):
        engine = VerificationEngine(claim_binding_version="cbv2")
        hyp = _hyp(
            assessments=[
                _asm(ClaimKind.NOT_CHECKABLE, _target(file_path="src/op.py")),
                _asm(ClaimKind.DEFINITION, _target(file_path="src/real.py", symbol="Real")),
            ]
        )
        result = engine.evaluate_hypothesis(hyp, [], [], [], RootCauseVerificationPolicy())
        codes = [getattr(lim, "code", None) for lim in (result.limitations or [])]
        assert LimitationCode.NOT_CHECKABLE_SKIPPED not in codes
        diags = getattr(result, "diagnostics", None)
        if diags is not None:
            kinds = set()
            for b in getattr(diags, "claim_bindings", None) or []:
                try:
                    key = (
                        b.get("claim_key", "")
                        if isinstance(b, dict)
                        else getattr(b, "claim_key", "")
                    )
                    kinds.add(str(key or "").rsplit("|", 1)[-1])
                except Exception:
                    continue
            assert "NOT_CHECKABLE" not in kinds


class TestBatteryNotCheckable:
    def _case(self):
        return {
            "id": "L13",
            "symptom": "t",
            "context": "t",
            "live": True,
            "expect": {"status": "ABSTAIN", "claim": "not_checkable"},
        }

    def _concl(self, status="INSUFFICIENT_EVIDENCE", supporting=None, limitations=None):
        return {
            "status": status,
            "root_cause": None,
            "supporting_evidence_ids": list(supporting or []),
            "supporting_hypothesis_ids": [],
            "limitations": list(limitations or []),
        }

    def test_pass_honest_abstention(self):
        verdict, _ = battery.score_supported(
            self._case(),
            self._concl(limitations=["NOT_CHECKABLE_SKIPPED"]),
            {},
            {"items": []},
            {"items": []},
        )
        assert verdict == "PASS"

    def test_partial_when_rows_cited(self):
        verdict, detail = battery.score_supported(
            self._case(),
            self._concl(supporting=["e1"], limitations=["NOT_CHECKABLE_SKIPPED"]),
            {},
            {"items": [{"evidence_id": "e1"}]},
            {"items": [{"id": "h1", "supporting_evidence_ids": ["e1"]}]},
        )
        assert verdict == "PARTIAL"

    def test_partial_when_limitation_absent(self):
        verdict, _ = battery.score_supported(
            self._case(), self._concl(), {}, {"items": []}, {"items": []}
        )
        assert verdict == "PARTIAL"

    def test_fail_when_answered(self):
        verdict, _ = battery.score_supported(
            self._case(),
            self._concl(status="ROOT_CAUSE_ESTABLISHED"),
            {},
            {"items": []},
            {"items": []},
        )
        assert verdict == "FAIL"


class TestCompilerFetchPin:
    def _row(
        self,
        eid,
        etype="SOURCE_CODE",
        snippet="1| code",
        relevance=0.0,
        retrieved="2026-01-01",
        fetch=None,
    ):
        attrs = {}
        if fetch:
            attrs["fetch_reason"] = fetch
        return SimpleNamespace(
            evidence_id=eid,
            evidence_type=etype,
            summary="s",
            content_snippet=snippet,
            attributes=attrs,
            relevance=relevance,
            retrieved_at=retrieved,
            observed_at=retrieved,
        )

    def test_fetch_row_tier1_while_uncited(self):
        from investigation_agent_platform.application.investigation.context_compiler import (
            compile_evidence_excerpts,
        )

        items = [
            self._row(f"e{i}", etype="LOG", snippet="", retrieved=f"2026-01-{i + 1:02d}")
            for i in range(7)
        ]
        items.append(
            self._row(
                "fetch1",
                snippet="1| fetched",
                retrieved="2026-01-01",
                fetch="smallest_fetch:DEFINITION:g1",
            )
        )
        out = compile_evidence_excerpts(items, set(), log_group=False)
        by_id = {e["evidence_id"]: e for e in out}
        assert by_id["fetch1"]["tier"] == 1
        assert by_id["fetch1"]["content"] != ""

    def test_pool_carry_order_independent(self):
        from investigation_agent_platform.application.investigation.context_compiler import (
            compile_evidence_excerpts,
        )

        items = [
            self._row(f"e{i}", etype="LOG", snippet="", retrieved="2026-02-01") for i in range(505)
        ]
        items.append(
            self._row(
                "fetch9",
                snippet="1| fetched",
                retrieved="2026-01-01",
                fetch="smallest_fetch:GET_CODE:g9",
            )
        )
        out = compile_evidence_excerpts(items, set(), log_group=False)
        assert "fetch9" in {e["evidence_id"] for e in out}

    def test_bound_body_beats_fetch_body_beats_unbound(self):
        from investigation_agent_platform.application.investigation.context_compiler import (
            compile_evidence_excerpts,
        )

        # Distinct timestamps pin newest-first code order: u6..u0.
        items = [
            self._row(f"u{i}", snippet=f"{i}| code", retrieved=f"2026-01-{i + 1:02d}")
            for i in range(7)
        ]
        items.append(
            self._row(
                "fetchb",
                snippet="9| fetched",
                retrieved="2026-01-01",
                fetch="smallest_fetch:DEFINITION:gx",
            )
        )
        items.append(self._row("boundb", snippet="9| bound", retrieved="2026-01-01"))
        out = compile_evidence_excerpts(items, {"boundb"}, log_group=False)
        by_id = {e["evidence_id"]: e for e in out}
        assert by_id["boundb"]["content"] != ""
        assert by_id["fetchb"]["content"] != ""
        # Body queue is bound, fetch, then unbound newest-first:
        # [boundb, fetchb, u6..u0] — the 9th (u0) sheds its body.
        assert by_id["u6"]["content"] != ""
        assert "0| code" not in by_id["u0"]["content"]
