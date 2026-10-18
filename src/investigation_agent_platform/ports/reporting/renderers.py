# src/investigation_agent_platform/ports/reporting/renderers.py
"""Report-renderer port (Part 11.8).

Renderers turn a bound `AggregateReport` into bytes. They must be
deterministic (same report → identical bytes), escape untrusted text, and
emit no external resource references.
"""

from typing import Protocol, runtime_checkable

from investigation_agent_platform.domain.reporting.aggregate import AggregateReport


@runtime_checkable
class ReportRendererPort(Protocol):
    """Versioned aggregate-report renderer (Part 11.8)."""

    @property
    def renderer_id(self) -> str:
        """Manifest plugin id this implementation serves."""
        ...

    @property
    def content_type(self) -> str:
        """MIME type of the rendered bytes."""
        ...

    def render(self, report: AggregateReport) -> bytes:
        """Render a bound report deterministically (no wall-clock inside)."""
        ...
