"""IAP onboarding agent — read-only advisory services.

The agent may change minds, never state: it analyses pinned snapshots and
validated sniffer outputs, and produces evidence-backed guidance. All writes
(report filing, state transitions, grants) belong to workflow activities
under their own authority; this package holds no write handles.

Layout:
- ``contracts``: dependency-free pydantic shapes for sniffer outputs, the
  support registry, coverage reports, report linkage, watchdog snapshots,
  and guidance Q&A. Safe for the onboarding intake schemas (Phase 1B) and
  workflow activities to import.
- ``agent``: the three stateless invocation contracts — coverage analysis,
  watchdog diagnosis, and report Q&A. Pure functions only: analysis output
  is bytes for the workflow to file, never a write performed here.
"""
