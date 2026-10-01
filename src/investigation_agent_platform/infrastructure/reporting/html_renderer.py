# src/investigation_agent_platform/infrastructure/reporting/html_renderer.py
"""Jinja2 HTML aggregate-report renderer (Part 11.8).

Deterministic output (rows arrive pre-sorted; no timestamps in the body),
full autoescaping, restrictive Content-Security-Policy meta, no scripts and
no external resources. Untrusted cluster/finding text is escaped by
construction — never marked safe.
"""

from __future__ import annotations

from jinja2 import Environment

from investigation_agent_platform.domain.common.extension import (
    PluginKind,
    PluginManifest,
)
from investigation_agent_platform.domain.reporting.aggregate import (
    HTML_AGGREGATE_RENDERER_ID,
    HTML_AGGREGATE_RENDERER_VERSION,
    AggregateReport,
)

RENDERER_PLUGIN_ID = HTML_AGGREGATE_RENDERER_ID
RENDERER_CONTRACT_VERSION = "1.0"
CONTENT_TYPE = "text/html; charset=utf-8"

_CSP = "default-src 'none'; style-src 'unsafe-inline'; img-src 'none'"

_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="{{ csp }}">
<title>Aggregate investigation report</title>
<style>
body { font-family: sans-serif; margin: 2rem; color: #111; }
table { border-collapse: collapse; width: 100%; }
th, td { border: 1px solid #999; padding: 0.4rem; text-align: left; }
.meta { color: #444; font-size: 0.9rem; }
.empty { font-style: italic; }
</style>
</head>
<body>
<h1>Aggregate investigation report</h1>
<p class="meta">Taxonomy revision {{ report.taxonomy_revision }} · renderer {{ report.renderer_id }} v{{ report.renderer_version }}</p>
{% if report.empty %}
<p class="empty">No clustered findings yet — run finding clustering first.</p>
{% else %}
<p class="meta">{{ report.assignment_count }} assignments · {{ report.unassigned_count }} unassigned</p>
<table>
<thead><tr><th>Cluster</th><th>Label</th><th>Members</th></tr></thead>
<tbody>
{% for row in report.rows %}
<tr><td>{{ row.cluster_key }}</td><td>{{ row.label }}</td><td>{{ row.member_count }}{% if row.truncated %} (truncated){% endif %}</td></tr>
{% endfor %}
</tbody>
</table>
{% endif %}
</body>
</html>
"""


def renderer_manifest() -> PluginManifest:
    """Manifest for the inaugural HTML renderer (registry wiring)."""
    return PluginManifest(
        plugin_id=RENDERER_PLUGIN_ID,
        plugin_kind=PluginKind.REPORT_RENDERER,
        contract_version=RENDERER_CONTRACT_VERSION,
        implementation_version=HTML_AGGREGATE_RENDERER_VERSION,
        config_schema_version="1.0",
        capabilities=frozenset({"render-html"}),
        config_schema={"type": "object"},
    )


class HtmlAggregateReportRenderer:
    """Deterministic Jinja2 renderer for bound aggregate reports."""

    def __init__(self) -> None:
        self._env = Environment(autoescape=True)
        self._template = self._env.from_string(_TEMPLATE)

    @property
    def renderer_id(self) -> str:
        return RENDERER_PLUGIN_ID

    @property
    def content_type(self) -> str:
        return CONTENT_TYPE

    def render(self, report: AggregateReport) -> bytes:
        return self._template.render(report=report, csp=_CSP).encode("utf-8")
