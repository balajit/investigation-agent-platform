"""Onboarding agent behavior: the three stateless invocation contracts.

- ``analysis``: coverage analysis — pinned snapshot + sniffer evidence +
  registry in, validated coverage report out.
- ``watchdog``: watchdog diagnosis — supplied state snapshot in, ranked
  diagnosis out.
- ``questions``: report Q&A — validated report in, cited answer out.

All functions are pure: no I/O, no wall-clock reads, no network, no state
mutation. Identity (report IDs), clocks (snapshot ``now``), and authority
(grants, filing) belong to the caller — the workflow activity.
"""

from investigation_agent_platform.application.onboarding_agent.agent.analysis import (
    AssemblyErrorCode,
    DispositionDraft,
    DraftStatus,
    ReportAssemblyError,
    assemble_coverage_report,
    draft_dispositions,
    needs_gap_names,
)
from investigation_agent_platform.application.onboarding_agent.agent.questions import (
    answer_coverage_question,
    answerable_claims,
)
from investigation_agent_platform.application.onboarding_agent.agent.watchdog import (
    REPEATED_FAILURE_THRESHOLD,
    diagnose,
)

__all__ = [
    "REPEATED_FAILURE_THRESHOLD",
    "AssemblyErrorCode",
    "DispositionDraft",
    "DraftStatus",
    "ReportAssemblyError",
    "answer_coverage_question",
    "answerable_claims",
    "assemble_coverage_report",
    "diagnose",
    "draft_dispositions",
    "needs_gap_names",
]
