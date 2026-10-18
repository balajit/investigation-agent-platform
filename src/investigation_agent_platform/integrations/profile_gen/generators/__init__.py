# src/investigation_agent_platform/integrations/profile_gen/generators/__init__.py
"""Per-type profile generators behind one interface (G1).

Each generator implements the module-level protocol below; the
assembler consumes the interface, never the framework. New languages
add a module here with zero core changes.
"""

from investigation_agent_platform.integrations.profile_gen.generators import (
    django as django,
)
from investigation_agent_platform.integrations.profile_gen.generators import (
    flask as flask,
)
from investigation_agent_platform.integrations.profile_gen.generators import (
    java_quarkus as java_quarkus,
)
from investigation_agent_platform.integrations.profile_gen.generators import (
    java_spring as java_spring,
)
from investigation_agent_platform.integrations.profile_gen.generators import (
    python_fastapi as python_fastapi,
)

_GENERATORS = {
    "java-spring": java_spring,
    "java-quarkus": java_quarkus,
    "java-micronaut": None,
    "python-fastapi": python_fastapi,
    "python-flask": flask,
    "python-django": django,
    "go-generic": None,
    "rust-generic": None,
}


def get_generator(repo_type: str):
    """Return the generator module for a repo type, or None (generic)."""
    try:
        return _GENERATORS.get(str(repo_type or ""), None)
    except Exception:
        return None
