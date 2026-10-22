"""IAP onboarding agent — read-only advisory services.

The agent may change minds, never state: it analyses pinned snapshots and
validated sniffer outputs, and produces evidence-backed guidance. All writes
(report filing, state transitions, grants) belong to workflow activities
under their own authority; this package holds no write handles.

Layout:
- ``contracts``: dependency-free pydantic shapes for sniffer outputs, the
  support registry, coverage reports, and report linkage. Safe for the
  onboarding intake schemas (Phase 1B) and workflow activities to import.
- ``agent``: analysis behavior. Populated in later slices; intentionally
  empty for now so no unreviewed logic ships early.
"""
