"""R11: staged role check + directory grant path (code half).

Written first, red before the fix.
"""

from __future__ import annotations


def test_r11_t01_clean_role_ok() -> None:
    from investigation_agent_platform.infrastructure.persistence.role_guard import (
        RoleReport,
        RoleVerdict,
        evaluate_role,
    )

    report = RoleReport(role="iap_app", superuser=False, bypass_rls=False, unprotected_tables=())
    assert evaluate_role(report, "warn") == RoleVerdict.OK
    assert evaluate_role(report, "enforce") == RoleVerdict.OK


def test_r11_t00_unknown_mode_fails_closed() -> None:
    """S4: the "off" value is deleted; an unknown mode raises, never passes."""
    import pytest

    from investigation_agent_platform.infrastructure.persistence.role_guard import (
        RoleReport,
        evaluate_role,
    )

    report = RoleReport(role="iap_app", superuser=False, bypass_rls=False, unprotected_tables=())
    with pytest.raises(ValueError, match="unknown rls_role_check mode"):
        evaluate_role(report, "off")


def test_r11_t02_privileged_warns_then_refuses() -> None:
    from investigation_agent_platform.infrastructure.persistence.role_guard import (
        RoleReport,
        RoleVerdict,
        evaluate_role,
    )

    report = RoleReport(role="iap", superuser=True, bypass_rls=True, unprotected_tables=("evidence",))
    assert evaluate_role(report, "warn") == RoleVerdict.WARN
    assert evaluate_role(report, "enforce") == RoleVerdict.REFUSE
    assert evaluate_role(None, "enforce") == RoleVerdict.REFUSE
    assert evaluate_role(None, "warn") == RoleVerdict.WARN


def test_r11_t03_gaps_warn_even_unprivileged() -> None:
    from investigation_agent_platform.infrastructure.persistence.role_guard import (
        RoleReport,
        RoleVerdict,
        evaluate_role,
    )

    report = RoleReport(
        role="iap_app", superuser=False, bypass_rls=False, unprotected_tables=("log_exemplars",)
    )
    assert evaluate_role(report, "warn") == RoleVerdict.WARN
    assert evaluate_role(report, "enforce") == RoleVerdict.REFUSE


def test_r11_t05_directory_invariant_fails_closed() -> None:
    import asyncio

    from investigation_agent_platform.domain.common.exceptions import (
        SecurityPolicyViolationException,
    )
    from investigation_agent_platform.domain.observability.mapping import generic_ecs_mapping
    from investigation_agent_platform.infrastructure.evidence.runtime.elastic import (
        AsyncElasticAdapter,
    )

    async def _lookup():  # type: ignore[no-untyped-def]
        return ["other-tenant"]

    adapter = AsyncElasticAdapter.__new__(AsyncElasticAdapter)
    adapter._known_tenant_ids = _lookup  # type: ignore[attr-defined]
    adapter._known_ids_cache = None  # type: ignore[attr-defined]
    try:
        asyncio.run(adapter._sibling_scope_ids(generic_ecs_mapping(), "acme"))
    except SecurityPolicyViolationException:
        return
    raise AssertionError("expected SecurityPolicyViolationException")


def test_r11_t06_delimited_skips_directory() -> None:
    import asyncio

    from investigation_agent_platform.domain.observability.mapping import (
        generic_ecs_delimited_mapping,
    )
    from investigation_agent_platform.infrastructure.evidence.runtime.elastic import (
        AsyncElasticAdapter,
    )

    async def _boom():  # type: ignore[no-untyped-def]
        raise RuntimeError("must not look up")

    adapter = AsyncElasticAdapter.__new__(AsyncElasticAdapter)
    adapter._known_tenant_ids = _boom  # type: ignore[attr-defined]
    adapter._known_ids_cache = None  # type: ignore[attr-defined]
    assert asyncio.run(adapter._sibling_scope_ids(generic_ecs_delimited_mapping(), "acme")) is None
