"""R2-ops: reserved `--` delimiter cutover (code half).

Written first, red before the fix.
"""

from __future__ import annotations

import fnmatch


def test_r2ops_t01_delimited_pattern_excludes_sibling() -> None:
    from investigation_agent_platform.infrastructure.evidence.runtime.validation import (
        build_delimited_pattern,
    )

    assert not fnmatch.fnmatchcase("logs-acme-eu--svc-prod-1", build_delimited_pattern("acme", "prod"))


def test_r2ops_t02_own_indexes_match() -> None:
    from investigation_agent_platform.infrastructure.evidence.runtime.validation import (
        build_delimited_pattern,
    )

    assert fnmatch.fnmatchcase(
        "logs-acme-eu--svc-prod-1", build_delimited_pattern("acme-eu", "prod", services=["svc"])
    )
    assert fnmatch.fnmatchcase(
        "logs-acme-eu--svc-prod-1", build_delimited_pattern("acme-eu", "prod")
    )


def test_r2ops_t03_reverse_direction() -> None:
    from investigation_agent_platform.infrastructure.evidence.runtime.validation import (
        build_delimited_pattern,
    )

    assert not fnmatch.fnmatchcase(
        "logs-acme--eu-x-prod-1", build_delimited_pattern("acme-eu", "prod")
    )


def test_r2ops_t04_exhaustive_no_collision() -> None:
    import itertools

    from investigation_agent_platform.infrastructure.evidence.runtime.validation import (
        INDEX_DELIM,
        build_delimited_pattern,
        validate_index_token,
    )

    alphabet = ["a", "b", "-", ".", "1"]
    tokens: list[str] = []
    for n in (1, 2, 3):
        for tup in itertools.product(alphabet, repeat=n):
            cand = "".join(tup)
            try:
                validate_index_token(cand, "tenant identifier")
            except Exception:
                continue
            tokens.append(cand)
    assert tokens
    for x, y in itertools.product(tokens, repeat=2):
        if x == y:
            continue
        idx = f"logs-{y}{INDEX_DELIM}s-prod-1"
        assert not fnmatch.fnmatchcase(idx, build_delimited_pattern(x, "prod")), (x, y)


def test_r2ops_t05_token_validation() -> None:
    import pytest

    from investigation_agent_platform.domain.common.exceptions import (
        SecurityPolicyViolationException,
    )
    from investigation_agent_platform.infrastructure.evidence.runtime.validation import (
        validate_index_token,
    )

    for bad in ["a--b", "-a", "a-", "A", "", "a b", "a,b", "a*", "x" * 129]:
        with pytest.raises(SecurityPolicyViolationException):
            validate_index_token(bad, "tenant identifier")
    for good in ["a", "a-b", "a.b", "a_b", "acme-eu"]:
        assert validate_index_token(good, "tenant identifier") == good


def test_r2ops_t06_mapping_pin_routing() -> None:
    import pytest

    from investigation_agent_platform.domain.common.exceptions import (
        DomainValidationException,
    )
    from investigation_agent_platform.domain.observability.mapping import (
        generic_ecs_delimited_mapping,
        generic_ecs_mapping,
    )
    from investigation_agent_platform.infrastructure.evidence.runtime.validation import (
        resolve_index_pattern,
    )

    dash = resolve_index_pattern(generic_ecs_mapping(), "acme", "prod")
    assert dash == "logs-acme-*-prod-*"
    delimited = resolve_index_pattern(generic_ecs_delimited_mapping(), "acme", "prod")
    assert delimited == "logs-acme--*-prod-*"
    from investigation_agent_platform.domain.observability.mapping import IndexSynthesis

    bogus = generic_ecs_mapping().model_copy(update={"source_id": "nope", "legacy_index_synthesis": False, "index_synthesis": IndexSynthesis.NONE})
    assert bogus.index_synthesis.value == "none"
    with pytest.raises(DomainValidationException):
        resolve_index_pattern(bogus, "acme", "prod")


def test_r2ops_t07_delimited_needs_no_registry_lookup() -> None:
    import asyncio

    from investigation_agent_platform.domain.observability.mapping import (
        generic_ecs_delimited_mapping,
    )
    from investigation_agent_platform.infrastructure.evidence.runtime.elastic import (
        AsyncElasticAdapter,
    )

    calls = {"n": 0}

    async def _lookup():  # type: ignore[no-untyped-def]
        calls["n"] += 1
        return ["acme"]

    adapter = AsyncElasticAdapter.__new__(AsyncElasticAdapter)
    adapter._known_tenant_ids = _lookup  # type: ignore[attr-defined]
    adapter._known_ids_cache = None  # type: ignore[attr-defined]
    out = asyncio.run(adapter._sibling_scope_ids(generic_ecs_delimited_mapping()))
    assert out is None
    assert calls["n"] == 0
