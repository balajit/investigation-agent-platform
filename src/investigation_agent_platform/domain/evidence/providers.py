# src/investigation_agent_platform/domain/evidence/providers.py
"""DEPRECATED PLACEHOLDER — relocated to investigation_agent_platform.ports.evidence.

This file is kept for backwards compatibility and will issue a deprecation warning on import.
The canonical protocol contracts live under ``src/investigation_agent_platform/ports/evidence/``.
"""

import warnings

warnings.warn(
    "investigation_agent_platform.domain.evidence.providers is deprecated and relocated to "
    "investigation_agent_platform.ports.evidence.",
    DeprecationWarning,
    stacklevel=2,
)