# src/investigation_agent_platform/application/analysis/python/__init__.py
"""Deterministic Python fact resolution layers (Python-lang plan Phase 2+)."""

from investigation_agent_platform.application.analysis.python.class_facts import (
    resolve_python_class_attr_fact,
)
from investigation_agent_platform.application.analysis.python.config import (
    resolve_python_config_fact,
)
from investigation_agent_platform.application.analysis.python.control import (
    resolve_python_control_fact,
    resolve_python_write_sites_fact,
)
from investigation_agent_platform.application.analysis.python.dispatch import (
    resolve_python_field_fact,
    resolve_python_method_fact,
)
from investigation_agent_platform.application.analysis.python.framework_facts import (
    resolve_python_framework_fact,
)
from investigation_agent_platform.application.analysis.python.imports import (
    resolve_python_import_fact,
)
from investigation_agent_platform.application.analysis.python.mro import (
    resolve_python_method_owner_fact,
    resolve_python_mro_fact,
)
from investigation_agent_platform.application.analysis.python.repo_version import (
    resolve_repo_python_version,
)
from investigation_agent_platform.application.analysis.python.scope_facts import (
    resolve_python_param_default_fact,
    resolve_python_value_fact,
)

__all__ = [
    "resolve_python_class_attr_fact",
    "resolve_python_config_fact",
    "resolve_python_control_fact",
    "resolve_python_field_fact",
    "resolve_python_framework_fact",
    "resolve_python_import_fact",
    "resolve_python_method_fact",
    "resolve_python_method_owner_fact",
    "resolve_python_mro_fact",
    "resolve_python_param_default_fact",
    "resolve_python_value_fact",
    "resolve_python_write_sites_fact",
    "resolve_repo_python_version",
]
