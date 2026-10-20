# tests/unit/test_acquired_judgement_phases.py
"""Acquired Judgement Phases 1-6 acceptance (pure logic, no live systems)."""

import pytest
from pydantic import ValidationError

from investigation_agent_platform.application.analysis.repository_analyzer import (
    content_dedup_key,
    ingest_edges,
    ingest_endpoints,
    ingest_files,
    ingest_symbols,
    is_generated,
)
from investigation_agent_platform.application.analysis.sync_merge import (
    diff_shards,
    merge_shards,
    normalize_path,
)
from investigation_agent_platform.application.profile.relationships import merge_domain_hubs
from investigation_agent_platform.domain.analysis.associations import (
    AssociationRecord,
    AssociationStore,
    bank_mid_loop_fact,
    jump_start_packet,
    symptom_signature,
)
from investigation_agent_platform.domain.analysis.cross_repo import (
    blast_radius,
    dependency_depth,
    env_asymmetric_diff,
    platform_single_point,
    shared_lib_fanout,
)
from investigation_agent_platform.domain.analysis.customer_context import (
    assemble_standing_profile,
    fuse_trail,
    map_to_hubs,
    mismatch_detector,
)
from investigation_agent_platform.domain.analysis.db_extractor import (
    extractor_fingerprint,
    oracle_fk_sql,
    postgres_fk_sql,
    verified_count_sql,
)
from investigation_agent_platform.domain.analysis.hub_memory import (
    hub_membership_corroborates,
    hub_orientation_block,
    lookup_hubs,
)
from investigation_agent_platform.domain.analysis.question_shape import classify_question_shape


def _good_hash() -> str:
    return "sha256:" + "a" * 64


class TestPhase1:
    def test_ingest_dedups_and_sorts(self):
        raw = [
            {"repo": "r", "sha": "s", "path": "b.py", "content_hash": _good_hash()},
            {"repo": "r", "sha": "s", "path": "a.py", "content_hash": _good_hash()},
            {"repo": "r", "sha": "s", "path": "a.py", "content_hash": _good_hash()},
        ]
        out = ingest_files(raw)
        assert [f.path for f in out] == ["a.py", "b.py"]

    def test_ingest_fail_closed(self):
        with pytest.raises(ValidationError):
            ingest_edges([{"repo": "r"}])

    def test_endpoints_symbols_ok(self):
        assert len(ingest_symbols([{"repo": "r", "sha": "s", "path": "p", "name": "Foo"}])) == 1
        assert (
            len(
                ingest_endpoints(
                    [{"repo": "r", "sha": "s", "path": "p", "method": "GET", "route": "/x"}]
                )
            )
            == 1
        )

    def test_dedup_key_and_generated(self):
        key, ch = content_dedup_key("r", "s", "stage1", b"a\n")
        assert key == "r@s:stage1" and ch.startswith("sha256:")
        assert is_generated("gen/foo.jaxb", b"x")[0] is True
        assert is_generated("src/Foo.java", b"class Foo {}\n")[0] is False


class TestPhase2Remainder:
    def test_oracle_postgres_sql_shapes(self):
        assert "ALL_CONSTRAINTS" in oracle_fk_sql("S")
        assert "information_schema" in postgres_fk_sql("s")
        assert "COUNT(*)" in verified_count_sql("oracle", "product")
        assert "O''BRIEN" in oracle_fk_sql("O'BRIEN")
        with pytest.raises(ValueError):
            verified_count_sql("mysql", "product")

    def test_fingerprint_stable(self):
        f1 = extractor_fingerprint({"a": 1}, "c")
        f2 = extractor_fingerprint({"a": 1}, "c")
        assert f1 == f2 and f1.startswith("sha256:")

    def test_lookup_orientation_corroboration(self):
        hubs = [{"name": "product", "score": 9.0, "witnesses": ["db", "code"]}]
        assert lookup_hubs(hubs, "prod")[0]["name"] == "product"
        block = hub_orientation_block(hubs, [{"hub": "product"}])
        assert block["type"] == "hub_orientation"
        assert hub_membership_corroborates(["product"], ["PRODUCT"]) is True
        assert hub_membership_corroborates(["product"], ["other"]) is False

    def test_no_hub_affinity_signals_abstention(self):
        hubs = [{"name": "product", "score": 9.0}]
        assert lookup_hubs(hubs, "quantum module") == []

    def test_merge_domain_hubs_operator_wins(self):
        derived = [{"name": "product", "score": 9.0}, {"name": "market", "score": 7.0}]
        operator = [{"name": "market", "score": 99.0}]
        merged = merge_domain_hubs(derived, operator)
        by_name = {h["name"]: h for h in merged}
        assert by_name["market"]["source"] == "operator"
        assert by_name["product"]["source"] == "derived"

    def test_case_insensitive_tree_path(self):
        from investigation_agent_platform.infrastructure.evidence.code.git import PyGit2Adapter

        class _Entry:
            def __init__(self, name):
                self.name = name

        class _Tree(list):
            pass

        class _Commit:
            def __init__(self, entries):
                self.tree = _Tree(entries)

        commit = _Commit([_Entry("SampleOrderFilter.java")])
        got = PyGit2Adapter._case_insensitive_tree_path(commit, "SampleOrderfilter.java")
        assert got == "SampleOrderFilter.java"
        assert PyGit2Adapter._case_insensitive_tree_path(commit, "Missing.java") is None


class TestPhase3:
    def test_capture_reinforce_recall_sweep(self):
        store = AssociationStore()
        sig = symptom_signature("timeout on checkout", "product", "TimeoutError")
        rec = store.capture(
            AssociationRecord(
                tenant_id="n-a",
                symptom_signature=sig,
                hub_entity="product",
                root_cause_statement="missing index",
                fix_locations=("db/schema.sql",),
            )
        )
        assert rec.weight == 1.0
        w = store.reinforce("n-a", sig, "product", verified=True)
        assert w > 1.0
        assert store.recall("n-a", sig)[0].hub_entity == "product"
        packet = jump_start_packet(store.recall("n-a", sig))
        assert packet["ranked"][0]["hub"] == "product"
        fact = bank_mid_loop_fact(
            "Index missing on product table.", "product", "lacks", "index", {"evidence_id": "e1"}
        )
        assert fact["triple"]["predicate"] == "lacks"
        store.reinforce("n-a", sig, "product", verified=False)
        store.reinforce("n-a", sig, "product", verified=False)
        assert store.sweep() in (0, 1)

    def test_bank_rejects_empty(self):
        with pytest.raises(ValueError):
            bank_mid_loop_fact("  ", "s", "p", "o", {})


class TestPhase4:
    def test_mismatch_top_signal(self):
        standing = assemble_standing_profile(
            {"account_id": "a1", "segment": "smb", "lifecycle": "active"},
            [{"offer": "base", "catalog": "cat", "hub": "base_offer"}],
        )
        fused = fuse_trail([{"id": "1", "ts": "t1"}], [{"id": "2", "ts": "t2", "offer": "premium"}])
        mapped = map_to_hubs(fused, {"premium": "product", "base": "base_offer"})
        findings = mismatch_detector(standing, mapped)
        assert findings and findings[0]["signal"] == "unentitled_item"


class TestPhase5:
    def test_blast_and_depth(self):
        edges = [
            {"src": "svc-a", "dst": "lib", "kind": "calls"},
            {"src": "svc-b", "dst": "lib", "kind": "calls"},
            {"src": "lib", "dst": "db", "kind": "reads"},
        ]
        assert blast_radius(edges, "lib") == ["svc-a", "svc-b"]
        assert shared_lib_fanout(edges, "lib") == ["svc-a", "svc-b"]
        assert dependency_depth(edges, "svc-a", "db") == 2
        assert dependency_depth(edges, "db", "svc-a") == -1

    def test_env_diff_and_single_point(self):
        edges = [
            {"src": "svc-a", "dst": "lib", "kind": "calls"},
            {"src": "svc-b", "dst": "lib", "kind": "calls"},
            {"src": "lib", "dst": "db", "kind": "reads"},
        ]
        diff = env_asymmetric_diff(edges, edges[:2])
        assert diff["only_in_prod"] == []
        assert diff["only_in_dev"]
        sp = platform_single_point(
            [{"src": "a", "dst": "config-server"}, {"src": "b", "dst": "config-server"}],
            ["config-server"],
        )
        assert sp[0]["count"] == 2


class TestPhase6:
    def test_merge_deterministic(self):
        a = [{"repo": "r", "sha": "s", "src": "b", "path": "b"}]
        b = [{"repo": "r", "sha": "s", "src": "a", "path": "a"}]
        m1 = merge_shards([a, b], stage="s1")
        m2 = merge_shards([b, a], stage="s1")
        assert [r["src"] for r in m1["records"]] == ["a", "b"]
        assert m1["records"] == m2["records"]
        assert m1["partial"] is False

    def test_merge_keeps_same_src_edges(self):
        shards = [
            [
                {"repo": "r", "sha": "s", "src": "svc-a", "dst": "lib", "kind": "calls"},
                {"repo": "r", "sha": "s", "src": "svc-a", "dst": "db", "kind": "calls"},
            ]
        ]
        m = merge_shards(shards, stage="s1")
        assert m["count"] == 2
        assert m["partial"] is False

    def test_partial_marked_and_diff(self):
        m = merge_shards([None, [{"repo": "r", "sha": "s", "src": "x"}]], stage="s1")
        assert m["partial"] is True
        empty_ok = merge_shards([[], [{"repo": "r", "sha": "s", "src": "x"}]], stage="s1")
        assert empty_ok["partial"] is False
        assert diff_shards("a", "a", ["f"]) == {"rebuild": False, "touched": []}
        assert diff_shards("a", "b", ["f"])["rebuild"] is True

    def test_normalize_path(self):
        assert normalize_path("./a//b") == "a/b"
        assert normalize_path("../a") == "../a"
        assert normalize_path("gen/foo.py") == "gen/foo.py"

    def test_malformed_edge_fail_closed(self):
        with pytest.raises(ValueError):
            blast_radius([{"src": "a"}], "a")


class TestQuestionShape:
    def test_code_questions(self):
        assert classify_question_shape("Where is SampleOrderFilter implemented?") == "code"
        assert classify_question_shape("What calls the order service handler?") == "code"
        assert classify_question_shape("What changed in OrderServiceImpl.java recently?") == "code"
        assert classify_question_shape("Which domain hub holds order eligibility?") == "code"

    def test_source_field_default_questions_are_code_shaped(self):
        assert classify_question_shape("What is the default value of retryCount?") == "code"
        assert classify_question_shape("What is the field initializer for retryCount?") == "code"
        assert classify_question_shape("What value is assigned during initialization?") == "code"

    def test_runtime_value_questions_are_not_forced_to_source_only(self):
        assert classify_question_shape("What is retryCount at runtime in production?") != "code"

    def test_state_log_unknown(self):
        assert classify_question_shape("What Oracle database version backs the store?") == "state"
        assert (
            classify_question_shape("NullPointerException: orderType is null, stack trace attached")
            == "log"
        )
        assert classify_question_shape("Where is the quantum module?") in ("code", "unknown")
