# tests/unit/test_part10_phase0.py
"""Part 10 Phase 0: real-data mapping fixtures load and are distinct."""

import json
from pathlib import Path

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "elastic_mappings"

EXPECTED_FILES = {
    "gen_verbose_prod.json",
    "fault_alerts.json",
    "gen_root_cause_prod.json",
    "ui_buyflow_err_grp.json",
    "buyflow_order_details.json",
}


def _load(name: str) -> dict:
    with open(FIXTURE_DIR / name, encoding="utf-8") as f:
        return json.load(f)


def test_all_fixtures_present_and_valid():
    actual = {p.name for p in FIXTURE_DIR.glob("*.json")}
    assert EXPECTED_FILES <= actual
    for name in EXPECTED_FILES:
        doc = _load(name)
        assert isinstance(doc["index"], str) and doc["index"]
        assert isinstance(doc["properties"], dict) and len(doc["properties"]) >= 1


def test_fixture_key_sets_differ():
    """Guards against copy-paste fixture rot: each stream really is distinct."""
    key_sets = [frozenset(_load(name)["properties"]) for name in EXPECTED_FILES]
    assert len(set(key_sets)) == len(key_sets)


def test_fixtures_capture_known_real_world_shapes():
    verbose = _load("gen_verbose_prod.json")["properties"]
    assert verbose["traceId"]["type"] == "keyword"
    assert verbose["error"]["type"] == "boolean"
    assert verbose["@timestamp"]["type"] == "date"
    assert "code" not in verbose and "db" not in verbose

    root_cause = _load("gen_root_cause_prod.json")["properties"]
    assert "@timestamp" not in root_cause
    assert root_cause["createdAt"]["type"] == "long"

    buyflow = _load("buyflow_order_details.json")["properties"]
    assert buyflow["session_id"]["type"] == "keyword"
