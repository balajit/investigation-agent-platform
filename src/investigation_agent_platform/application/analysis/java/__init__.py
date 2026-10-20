# src/investigation_agent_platform/application/analysis/java/__init__.py
"""Deterministic Java fact resolution layers (Phase 2)."""

from investigation_agent_platform.application.analysis.java.boundary_facts import (
    resolve_java_framework_boundary,
)
from investigation_agent_platform.application.analysis.java.dataflow import (
    resolve_java_data_flow,
)
from investigation_agent_platform.application.analysis.java.field_facts import (
    resolve_java_field_fact,
)
from investigation_agent_platform.application.analysis.java.flow_facts import (
    resolve_java_control_flow,
    resolve_java_possible_effect,
)
from investigation_agent_platform.application.analysis.java.init_facts import (
    resolve_java_init_fact,
)
from investigation_agent_platform.application.analysis.java.local_facts import (
    resolve_java_local_fact,
)
from investigation_agent_platform.application.analysis.java.method_facts import (
    resolve_java_call_target,
)
from investigation_agent_platform.application.analysis.java.method_return_facts import (
    resolve_java_method_return_fact,
)
from investigation_agent_platform.application.analysis.java.type_facts import (
    resolve_java_type_fact,
)

__all__ = [
    "resolve_java_call_target",
    "resolve_java_control_flow",
    "resolve_java_data_flow",
    "resolve_java_field_fact",
    "resolve_java_framework_boundary",
    "resolve_java_init_fact",
    "resolve_java_local_fact",
    "resolve_java_method_return_fact",
    "resolve_java_possible_effect",
    "resolve_java_type_fact",
]
