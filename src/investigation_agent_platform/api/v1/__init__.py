"""v1 API routers (Part 4, section 4.1)."""

from investigation_agent_platform.api.v1.routers.evidence import router as evidence
from investigation_agent_platform.api.v1.routers.health import router as health
from investigation_agent_platform.api.v1.routers.hypotheses import router as hypotheses
from investigation_agent_platform.api.v1.routers.investigations import router as investigations
from investigation_agent_platform.api.v1.routers.profiles import router as profiles
from investigation_agent_platform.api.v1.routers.timeline import router as timeline

__all__ = ["evidence", "health", "hypotheses", "investigations", "profiles", "timeline"]