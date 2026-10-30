# tests/unit/test_analysis_phase0.py
"""Phase 0 + Phase 2 pure-logic goldens (Acquired Judgement)."""

from pydantic import ValidationError

from investigation_agent_platform.domain.analysis.content_keys import (
    normalize_file_bytes,
    parse_result_key,
    result_key,
    sha256_content,
)
from investigation_agent_platform.domain.analysis.hub_ranking import (
    ClassStats,
    RankedClasses,
    TableStats,
    rank_classes,
    score_tables,
    witness_merge,
)
from investigation_agent_platform.domain.analysis.sidecar_v1 import (
    EdgeRecord,
    EndpointRecord,
    FileRecord,
    SymbolRecord,
)


class TestContentKeys:
    def test_normalize_crlf_and_trailing_spaces(self):
        assert normalize_file_bytes(b"a  \r\nb\t \r\n") == b"a\nb\n"

    def test_sha_prefix_and_stable(self):
        h1 = sha256_content(b"a\n")
        h2 = sha256_content(b"a\r\n")
        assert h1 == h2
        assert h1.startswith("sha256:")

    def test_result_key_roundtrip(self):
        k = result_key("org/repo", "abc123", "stage1")
        assert parse_result_key(k) == ("org/repo", "abc123", "stage1")

    def test_result_key_rejects_bad_stage(self):
        import pytest

        with pytest.raises(ValueError):
            result_key("r", "s", "Bad Stage!")

    def test_result_key_rejects_separators(self):
        import pytest

        with pytest.raises(ValueError):
            result_key("a:b", "s", "stage1")
        with pytest.raises(ValueError):
            result_key("r", "a@b", "stage1")


class TestSidecarV1:
    def test_file_record_validates_hash_shape(self):
        import pytest

        good = "sha256:" + "0" * 64
        FileRecord(repo="r", sha="s", path="p", content_hash=good)
        with pytest.raises(ValidationError):
            FileRecord(repo="r", sha="s", path="p", content_hash="nope")

    def test_edge_record_confidence_grades(self):
        EdgeRecord(repo="r", sha="s", src="a", dst="b", kind="calls", confidence="Exact")
        EdgeRecord(repo="r", sha="s", src="a", dst="b", kind="fk", confidence="Inferred")

    def test_symbol_and_endpoint_minimal(self):
        SymbolRecord(repo="r", sha="s", path="p", name="Foo")
        EndpointRecord(repo="r", sha="s", path="p", method="GET", route="/orders")


class TestHubRanking:
    def test_poc_order_product_market_base_offer(self):
        tables = [
            TableStats(name="product", fan_in=79, fan_out=4, num_rows=10000),
            TableStats(name="market", fan_in=71, fan_out=2, num_rows=5000),
            TableStats(name="base_offer", fan_in=44, fan_out=6, num_rows=20000),
            TableStats(name="billing_code", fan_in=27, fan_out=1, num_rows=8000),
            TableStats(
                name="product_market", fan_in=2, fan_out=2, num_rows=15000, is_link_table=True
            ),
        ]
        ranked = [h.name for h in score_tables(tables)]
        assert ranked[:2] == ["product", "market"]
        assert "base_offer" in ranked[:4]

    def test_dormant_excluded(self):
        ranked = score_tables([TableStats(name="empty_child", fan_in=10, fan_out=0, num_rows=0)])
        assert ranked[0].dormant is True
        assert ranked[0].score == -1.0

    def test_negative_rows_clamped(self):
        ranked = score_tables([TableStats(name="bad", fan_in=2, fan_out=0, num_rows=-5)])
        assert ranked[0].num_rows == 0
        assert ranked[0].dormant is True

    def test_class_split_enums_to_vocabulary(self):
        classes = [
            ClassStats(
                name="com.sample.Product",
                import_fan_in=50,
                hits_authority=9.0,
                entity_package=True,
                community="sales",
            ),
            ClassStats(
                name="com.sample.OrderType",
                import_fan_in=0,
                hits_authority=0,
                is_enum=True,
                community="sales",
            ),
            ClassStats(name="com.sample.UtilsHelper", import_fan_in=30, hits_authority=1.0),
        ]
        out: RankedClasses = rank_classes(classes)
        assert out.hubs[0] == "com.sample.Product"
        assert "com.sample.OrderType" in out.vocabulary
        assert out.communities["sales"] == sorted(out.communities["sales"])

    def test_witness_merge_agreement(self):
        m = witness_merge(["product", "market", "base_offer"], ["product", "market", "other"])
        assert m["agreed"] == ["market", "product"]
        assert m["confidence"] > 0
        assert "base_offer" in m["db_only"]
        assert "other" in m["code_only"]
