# src/investigation_agent_platform/application/analysis/python/dispatch.py
"""Worker-facing Python fact dispatch (field and method paths).

Selects the resolver chain from GET_CODE parameters, threads the trusted
repo-declared language version, and tiers chained attempts so an
informative PARTIAL is never clobbered by a later UNRESOLVED. Emits V2
payloads only, never the Java field-shaped legacy key. All failures yield
``None`` (or UNRESOLVED-shaped payloads from the resolvers themselves) —
dispatch must never break the surrounding GET_CODE read.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from investigation_agent_platform.application.analysis.failure import (
    analysis_failed_resolution,
)
from investigation_agent_platform.application.analysis.python.class_facts import (
    resolve_python_class_attr_fact,
)
from investigation_agent_platform.application.analysis.python.config import (
    resolve_python_config_fact,
)
from investigation_agent_platform.application.analysis.python.control import (
    resolve_python_control_fact,
)
from investigation_agent_platform.application.analysis.python.framework_facts import (
    resolve_python_framework_fact,
)
from investigation_agent_platform.application.analysis.python.imports import (
    resolve_python_import_fact,
)
from investigation_agent_platform.application.analysis.python.mro import (
    resolve_python_method_owner_fact,
)
from investigation_agent_platform.application.analysis.python.repo_version import (
    resolve_repo_python_version,
)
from investigation_agent_platform.application.analysis.python.scope_facts import (
    resolve_python_param_default_fact,
    resolve_python_value_fact,
)
from investigation_agent_platform.domain.code.models import JavaFactCategory


def _tier(resolution: Any) -> int:
    try:
        status = str(getattr(resolution.status, "value", resolution.status))
    except Exception:
        return -1
    return {"RESOLVED": 2, "PARTIAL": 1, "UNRESOLVED": 0}.get(status, -1)


def _is_resolved(resolution: Any) -> bool:
    return resolution is not None and _tier(resolution) == 2


def _prefer(current: Any, candidate: Any) -> Any:
    if candidate is None:
        return current
    if current is None:
        return candidate
    # First-wins within a tier: keep the earliest informative result
    # instead of letting a later UNRESOLVED clobber a PARTIAL from an
    # earlier, more specific resolver.
    if _tier(candidate) > _tier(current):
        return candidate
    return current


async def _trusted_version(
    evidence: Any,
    read_version_file: Callable[[str], Awaitable[str | None]] | None,
) -> tuple[int, int] | None:
    """Repo-declared version at the analyzed SHA; None falls back to default."""
    if read_version_file is None:
        return None
    try:
        location = getattr(getattr(evidence, "provenance", None), "source_location", None)
        pinned = str(getattr(location, "revision", "") or "")
        if not pinned or pinned in {"HEAD", "UNPINNED"}:
            return None
        return await resolve_repo_python_version(read_version_file)
    except Exception:
        return None


async def resolve_python_field_fact(
    evidence: Any,
    *,
    field_name: str,
    declaring_type: str | None = None,
    class_scope: str | None = None,
    function_scope: str | None = None,
    read_version_file: Callable[[str], Awaitable[str | None]] | None = None,
) -> tuple[Any | None, str | None]:
    """Run the field dispatch chain. Returns (resolution, configuration_source).

    The configuration source is the declaration file path when the winning
    resolution is a CONFIGURATION_FACT (feeds the verifier's
    CONFIGURATION_SOURCE family), else None.
    """
    scope = (declaring_type or "").strip() or (class_scope or "").strip() or None
    version = await _trusted_version(evidence, read_version_file)
    resolution: Any | None = None
    failures: list[BaseException] = []
    if (function_scope or "").strip():
        resolution = resolve_python_param_default_fact(
            evidence,
            field_name,
            function_scope=function_scope.strip(),
            language_version=version,
        )
    elif scope:
        for attempt in (
            lambda: resolve_python_framework_fact(
                evidence, field_name, class_scope=scope, language_version=version
            ),
            lambda: resolve_python_class_attr_fact(
                evidence, field_name, class_scope=scope, language_version=version
            ),
            lambda: resolve_python_value_fact(
                evidence, field_name, scope=scope, language_version=version
            ),
        ):
            try:
                candidate = attempt()
            except Exception as exc:
                failures.append(exc)
                continue
            resolution = _prefer(resolution, candidate)
            if _is_resolved(resolution):
                break
    else:
        for attempt in (
            lambda: resolve_python_config_fact(evidence, field_name, language_version=version),
            lambda: resolve_python_value_fact(evidence, field_name, language_version=version),
            lambda: resolve_python_import_fact(evidence, field_name, language_version=version),
        ):
            try:
                candidate = attempt()
            except Exception as exc:
                failures.append(exc)
                continue
            resolution = _prefer(resolution, candidate)
            if _is_resolved(resolution):
                break
    if resolution is None and failures:
        from investigation_agent_platform.application.analysis.python import _parse as _p

        resolution = analysis_failed_resolution(
            evidence,
            category=JavaFactCategory.SOURCE_VALUE_FACT,
            subject=field_name,
            exc=failures[0],
            parser_version=_p.parser_version(),
        )
    config_source: str | None = None
    if resolution is not None:
        try:
            category = getattr(resolution.fact_category, "value", resolution.fact_category)
        except Exception:
            category = None
        if category == "CONFIGURATION_FACT":
            try:
                path = getattr(
                    getattr(getattr(evidence, "provenance", None), "source_location", None),
                    "file_path",
                    "",
                )
                config_source = str(path or "") or None
            except Exception:
                config_source = None
    return resolution, config_source


async def resolve_python_method_fact(
    evidence: Any,
    *,
    method_name: str,
    declaring_type: str | None = None,
    class_scope: str | None = None,
    guard_condition: str | None = None,
    read_version_file: Callable[[str], Awaitable[str | None]] | None = None,
) -> Any | None:
    """Run the method dispatch: override ownership with a class scope, else control inventory."""
    method_scope = (declaring_type or "").strip() or (class_scope or "").strip() or None
    version = await _trusted_version(evidence, read_version_file)
    if method_scope:
        return resolve_python_method_owner_fact(
            evidence, method_name, class_scope=method_scope, language_version=version
        )
    return resolve_python_control_fact(
        evidence, method_name, branch_hint=guard_condition, language_version=version
    )
