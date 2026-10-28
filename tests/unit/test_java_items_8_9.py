"""Regression tests for the versioned boundary catalog and value roles."""

from __future__ import annotations

import json
from pathlib import Path

from investigation_agent_platform.application.analysis.java import model, query


def test_framework_boundary_catalog_is_versioned_and_well_formed():
    path = Path(
        "src/investigation_agent_platform/application/analysis/java/data/framework_boundaries_v1.json"
    )
    payload = json.loads(path.read_text())
    assert payload["schema_version"] == "iap/java-framework-boundaries/v1"
    assert payload["boundaries"]
    assert all(item["code"] and item["markers"] for item in payload["boundaries"])


def test_dataflow_events_expose_lvalue_rvalue_roles():
    source = b"class X { void run() { int value = 1; value = 2; value += 1; value++; sink(value); } void sink(int x) {} }"
    code = model.build_java_model(
        source,
        file_path="src/X.java",
        repository="repo",
        revision="rev",
    )
    events = list(query.all_dataflow_events(code))
    assert any(event.value_role == "lvalue" for event in events)
    assert any(event.value_role == "read_modify_write" for event in events)
    assert any(event.value_role == "rvalue" for event in events)
    assert list(query.events_with_value_role(events, "lvalue"))
