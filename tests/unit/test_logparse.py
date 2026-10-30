# tests/unit/test_logparse.py
"""Generic log compression: exact-tier behavior + invariants (no IAP imports)."""

from datetime import UTC, datetime

import pytest

hypothesis = pytest.importorskip("hypothesis")
from hypothesis import given, settings
from hypothesis import strategies as st

from investigation_agent_platform.application.compression.logparse import (
    Coverage,
    ParsedLine,
    coverage,
    dedup_collapse,
    filter_lines,
    mask_volatiles,
    merge_ordered,
    order_temporal,
    parse_lines,
)


def _line(
    seq,
    message="GET /api/v1/things HTTP/1.1 200",
    ts=None,
    logger_name="api",
    level="INFO",
    raw=None,
):
    return ParsedLine(
        seq=seq,
        source_file="f.log",
        timestamp=ts,
        record={"logger": logger_name, "message": message, "level": level},
        raw=raw if raw is not None else message,
        ids=frozenset(),
    )


def _ts(day=17, hour=0, minute=0, second=0):
    return datetime(2026, 10, day, hour, minute, second, tzinfo=UTC)


# --- masking: statuses survive, volatiles collapse ---


def test_mask_preserves_status_but_collapses_ids():
    a = "GET /api/v1/investigations/34df140f-1bf3-4193-8f52-5e2fcbd8c3fc/evidence HTTP/1.1 200"
    b = "GET /api/v1/investigations/c178b3ae-305b-4566-832b-83569b2448b4/evidence HTTP/1.1 200"
    c = "GET /api/v1/investigations/34df140f-1bf3-4193-8f52-5e2fcbd8c3fc/evidence HTTP/1.1 500"
    assert mask_volatiles(a) == mask_volatiles(b)
    assert mask_volatiles(a) != mask_volatiles(c)


def test_mask_collapses_durations_and_hashes():
    assert mask_volatiles("op took 12.3ms id abcdef0123456789") == mask_volatiles(
        "op took 450 ms id 99887766554433221100"
    )


# --- exact collapse ---


def test_consecutive_run_collapses_with_span():
    lines = [_line(i, ts=_ts(second=i), raw=f"req corr-{i}") for i in range(5)]
    out = dedup_collapse(lines)
    assert len(out) == 1
    exemplar = out[0]
    assert exemplar.count == 5
    assert exemplar.first.seq == 0 and exemplar.last.seq == 4
    assert exemplar.first.timestamp < exemplar.last.timestamp


def test_non_consecutive_repeats_stay_separate_phase1():
    lines = [_line(0, "alpha"), _line(1, "beta"), _line(2, "alpha")]
    out = dedup_collapse(lines)
    assert len(out) == 3  # template grouping is phase 2


def test_keep_single_forces_singletons():
    lines = [_line(0, "ok"), _line(1, "boom"), _line(2, "ok")]
    out = dedup_collapse(lines, keep_single=lambda line: "boom" in line.raw)
    assert [e.count for e in out] == [1, 1, 1]
    assert out[1].forced_singleton is True


def test_different_levels_never_merge():
    lines = [_line(0, "same", level="INFO"), _line(1, "same", level="ERROR")]
    assert len(dedup_collapse(lines)) == 2


# --- ordering ---


def test_order_temporal_stable_with_missing_ts():
    lines = [_line(2, ts=_ts(second=5)), _line(0, ts=None), _line(1, ts=_ts(second=1))]
    ordered = order_temporal(lines)
    assert [line.seq for line in ordered] == [1, 2, 0]


def test_merge_ordered_k_way():
    a = [_line(0, ts=_ts(second=1)), _line(1, ts=_ts(second=4))]
    b = [_line(2, ts=_ts(second=2)), _line(3, ts=_ts(second=3))]
    merged = list(merge_ordered([iter(a), iter(b)]))
    assert [line.seq for line in merged] == [0, 2, 3, 1]


# --- parse / filter ---


def test_parse_json_and_raw(tmp_path):
    path = tmp_path / "mix.log"
    path.write_text(
        '{"message": "hi", "timestamp": "2026-10-17T00:00:01Z"}\n'
        "plain uvicorn line\n"
        '{"message": "with id 34df140f-1bf3-4193-8f52-5e2fcbd8c3fc"}\n',
        encoding="utf-8",
    )
    parsed = list(parse_lines([str(path)]))
    assert len(parsed) == 3
    assert parsed[0].timestamp is not None and parsed[0].record is not None
    assert parsed[1].record is None and parsed[1].timestamp is None
    assert parsed[2].ids == frozenset({"34df140f-1bf3-4193-8f52-5e2fcbd8c3fc"})


def test_parse_skips_unreadable():
    assert list(parse_lines(["/nonexistent/x.log"])) == []


def test_filter_lines_predicate_error_fails_open():
    lines = [_line(0), _line(1)]

    def bad(line):
        raise RuntimeError("nope")

    assert list(filter_lines(lines, bad)) == []


# --- coverage ---


def test_coverage_ratio():
    lines = [_line(i) for i in range(4)]
    out = dedup_collapse(lines)
    cov = coverage(4, out)
    assert isinstance(cov, Coverage)
    assert cov.ratio == pytest.approx(0.75)
    assert coverage(0, []).ratio == 0.0


# --- property tests: invariants over generated streams ---


def _parsed_strategy():
    timestamps = st.one_of(
        st.none(),
        st.datetimes(
            min_value=datetime(2026, 1, 1, tzinfo=UTC),
            max_value=datetime(2026, 12, 31, tzinfo=UTC),
        ),
    )
    texts = st.sampled_from(["alpha", "beta", "GET /x HTTP/1.1 200", "boom", ""])
    return st.builds(
        ParsedLine,
        seq=st.integers(min_value=0, max_value=10_000),
        source_file=st.just("f.log"),
        timestamp=timestamps,
        record=st.none(),
        raw=texts,
        ids=st.just(frozenset()),
    ).map(
        # Keep raw/message coherent: signature derives from the record.
        lambda draft: ParsedLine(
            seq=draft.seq,
            source_file=draft.source_file,
            timestamp=draft.timestamp,
            record={
                "logger": "t",
                "message": draft.raw or "alpha",
                "level": "INFO",
            },
            raw=draft.raw or "alpha",
            ids=frozenset(),
        )
    )


@given(lines=st.lists(_parsed_strategy(), min_size=0, max_size=60))
@settings(max_examples=60, derandomize=False)
def test_count_conservation(lines):
    ordered = sorted(lines, key=lambda line: line.seq)
    out = dedup_collapse(ordered)
    assert sum(e.count for e in out) == len(ordered)


@given(lines=st.lists(_parsed_strategy(), min_size=0, max_size=60))
@settings(max_examples=60, derandomize=False)
def test_first_appearance_order_preserved(lines):
    ordered = sorted(lines, key=lambda line: line.seq)
    out = dedup_collapse(ordered)
    firsts = [e.first.seq for e in out]
    assert firsts == sorted(firsts)
    lasts = [e.last.seq for e in out]
    assert lasts == sorted(lasts)
    for exemplar in out:
        assert exemplar.first.seq <= exemplar.last.seq


@given(lines=st.lists(_parsed_strategy(), min_size=1, max_size=40))
@settings(max_examples=40, derandomize=False)
def test_idempotent_on_exemplars(lines):
    ordered = sorted(lines, key=lambda line: line.seq)
    once = dedup_collapse(ordered)
    # Re-collapsing the exemplar representatives is a fixed point in count.
    reps = []
    for exemplar in once:
        reps.append(exemplar.first)
        if exemplar.count > 1:
            reps.append(exemplar.last)
    twice = dedup_collapse(sorted(reps, key=lambda line: line.seq))
    assert sum(e.count for e in twice) == len(reps)
