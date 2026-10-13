"""Coverage guarantee: the registry covers exactly the spec's operations (design §7).

``test_registry_matches_local_spec`` runs without a network (import-time spec
from the app) and guards every plain ``pytest`` run. The live cross-checks run
only when the suite is enabled.
"""

from __future__ import annotations

from typing import Any

from .conftest import local_spec, needs_live
from .scenarios import SCENARIO_KEYS


def _spec_keys(spec: dict[str, Any]) -> frozenset[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    for path, item in spec.get("paths", {}).items():
        if not isinstance(item, dict):
            continue
        for method in item:
            if method.lower() in ("parameters", "$ref", "summary", "description", "servers"):
                continue
            keys.add((method.upper(), path))
    return frozenset(keys)


def test_registry_matches_local_spec() -> None:
    missing = sorted(set(_spec_keys(local_spec())) - set(SCENARIO_KEYS))
    extra = sorted(set(SCENARIO_KEYS) - set(_spec_keys(local_spec())))
    assert not missing, f"operations without a scenario: {missing}"
    assert not extra, f"scenarios without an operation: {extra}"


@needs_live
def test_registry_matches_live_spec(live_spec: dict[str, Any]) -> None:
    missing = sorted(set(_spec_keys(live_spec)) - set(SCENARIO_KEYS))
    extra = sorted(set(SCENARIO_KEYS) - set(_spec_keys(live_spec)))
    assert not missing, f"live operations without a scenario: {missing}"
    assert not extra, f"scenarios without a live operation: {extra}"


@needs_live
def test_live_spec_matches_local_spec(live_spec: dict[str, Any]) -> None:
    """A route removed from the server must not silently vanish from both sides."""
    live_only = sorted(set(_spec_keys(live_spec)) - set(_spec_keys(local_spec())))
    local_only = sorted(set(_spec_keys(local_spec())) - set(_spec_keys(live_spec)))
    assert not live_only, f"live-only operations: {live_only}"
    assert not local_only, f"local-only operations: {local_only}"
