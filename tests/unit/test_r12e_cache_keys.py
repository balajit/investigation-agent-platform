"""R12E: colon-delimited cache keys with colon-capable tenant IDs.

The current key is injective (fixed-shape investigation UUID plus a
fixed 32-hex digest make the tenant recoverable from the right), so
there is no behavior change: this pins the injectivity as a regression
guard and adds the length-prefixed join helper for any new key
builder. Written first, red before the fix.
"""

from __future__ import annotations

import uuid


def _key(tenant, app="app", inv=None, kinds=None, fps=None):  # type: ignore[no-untyped-def]
    from investigation_agent_platform.application.knowledge.retrieve import cache_key

    return cache_key(
        tenant,
        app,
        inv or uuid.uuid4(),
        kinds if kinds is not None else ["evidence_summary"],
        fps if fps is not None else ["fp-1"],
    )


def test_r12e_t01_cache_key_is_injective() -> None:
    inv = uuid.uuid4()
    seen: dict[str, tuple] = {}
    tenants = ["a", "a:b", f"a:{inv}", "a:b:c", "a::b"]
    for tenant in tenants:
        for app in ("app", "app:x"):
            key = _key(tenant, app, inv)
            label = (tenant, app, str(inv))
            assert key not in seen, f"collision: {label} vs {seen[key]}"
            seen[key] = label
    # The investigation id participates: same everything but the
    # investigation yields a different key (this is the guard that would
    # fail if investigation_id were ever dropped from the key, aliasing
    # distinct runs).
    inv_a, inv_b = uuid.uuid4(), uuid.uuid4()
    assert _key("a:b", "app", inv_a) != _key("a:b", "app", inv_b)


def test_r12e_t02_length_prefixed_join_is_unambiguous() -> None:
    from investigation_agent_platform.domain.common.keys import join_key_parts

    assert join_key_parts("a:b", "c") != join_key_parts("a", "b:c")
    assert join_key_parts() == ""
    assert join_key_parts("a:b", "c") == "3:a:b1:c"
