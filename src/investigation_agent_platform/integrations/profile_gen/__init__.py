# src/investigation_agent_platform/integrations/profile_gen/__init__.py
"""Profile generator: GitHub org/repos + Spring config → application profiles."""

from investigation_agent_platform.integrations.profile_gen.github import (
    detect_modules as detect_modules,
)
from investigation_agent_platform.integrations.profile_gen.github import (
    discover_repos as discover_repos,
)
