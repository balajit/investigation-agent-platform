# tests/unit/test_f8_golden_master.py
"""F8-D16 golden master equality + coverage (recorded pre-D15, never regenerated for D15)."""

import json
import pathlib

from tests.unit.cascade_corpus import PINS, PRE, run_case

GOLDEN = pathlib.Path("tests/fixtures/cascade_golden_v1.json")


def _load() -> dict:
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


def test_golden_file_exists_and_covers() -> None:
    payload = _load()
    assert payload["header"]["cases"] >= 500
    assert set(payload["header"]["pins"]) == set(PINS)
    # coverage: every cascade-written Force fires in >=5 cases (pre-cascade
    # ABSENCE_DISCONFIRMATION / NOT_CHECKABLE arrive as turn state, never written),
    # each PRE combo in >=20
    from collections import Counter

    force_hits: Counter[str] = Counter()
    pre_hits: Counter[str] = Counter()
    for case, rec in zip(payload["cases"], payload["records"]):
        for k, v in (rec["reason"] or {}).items():
            if str(k).startswith("forced_") and v:
                force_hits[str(k)] += 1
        pre_hits[str(tuple(case[2]))] += 1
    for force in (
        "forced_pivot_recollect", "forced_walk_stack", "forced_requery", "forced_floor",
        "forced_formulate", "forced_reread", "forced_callsite", "forced_log_search",
        "forced_replan", "forced_smallest_fetch",
    ):
        assert force_hits[force] >= 5, (force, force_hits[force])
    for combo in PRE:
        assert pre_hits[str(tuple(combo))] >= 20, (combo, pre_hits[str(tuple(combo))])


def test_golden_master_equality() -> None:
    payload = _load()
    for case, expected in zip(payload["cases"], payload["records"]):
        got = run_case(tuple(case))
        assert got == expected, f"case {tuple(case)} differs: {[k for k in expected if expected[k] != got.get(k)]}"
