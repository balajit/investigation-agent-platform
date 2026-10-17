# src/investigation_agent_platform/ports/evidence/api_probe.py
"""Port for sandboxed, profile-scoped HTTP probing (P2-2)."""

from pydantic import BaseModel, ConfigDict, Field


class ApiProbeRequest(BaseModel):
    """Probe one profile-declared endpoint. Never a raw URL."""

    model_config = ConfigDict(frozen=True)

    endpoint_id: str = Field(..., max_length=128)
    path_params: dict[str, str] = Field(default_factory=dict, max_length=10)
    query_params: dict[str, str] = Field(default_factory=dict, max_length=10)


class ApiProbeResult(BaseModel):
    """Bounded, redacted live response."""

    model_config = ConfigDict(frozen=True)

    endpoint_id: str = Field(..., max_length=128)
    method: str = Field(..., max_length=10)
    resolved_url: str = Field(..., max_length=1024)
    status_code: int
    response_headers: dict[str, str] = Field(default_factory=dict)
    response_body_excerpt: str = Field(default="", max_length=4096)
    response_body_truncated: bool = False
    latency_ms: int = Field(default=0, ge=0)


class ApiProbePort:
    """Read-only probe against the investigated application's own declared
    endpoints. Every call MUST resolve through a ProbeableEndpoint allowlist
    entry; there is no raw-URL calling mode."""

    async def probe(
        self, tenant_id: str, application_id: str, request: ApiProbeRequest
    ) -> ApiProbeResult: ...
