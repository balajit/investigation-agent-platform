# src/investigation_agent_platform/infrastructure/reporting/__init__.py
"""Reporting infrastructure (Part 11.8)."""

from investigation_agent_platform.infrastructure.reporting.html_renderer import (
    HtmlAggregateReportRenderer,
    renderer_manifest,
)

__all__ = ["HtmlAggregateReportRenderer", "renderer_manifest"]
