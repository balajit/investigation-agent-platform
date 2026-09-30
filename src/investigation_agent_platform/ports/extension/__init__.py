# src/investigation_agent_platform/ports/extension/__init__.py
"""Extension port interfaces: plugin lifecycle and schema versioning."""

from investigation_agent_platform.ports.extension.lifecycle import PluginLifecycle
from investigation_agent_platform.ports.extension.versioning import (
    SchemaUpcaster,
    UnknownSchemaVersionError,
    upcast_to,
)

__all__ = [
    "PluginLifecycle",
    "SchemaUpcaster",
    "UnknownSchemaVersionError",
    "upcast_to",
]
