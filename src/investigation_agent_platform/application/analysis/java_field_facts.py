"""Deterministic, revision-pinned Java field initializer/default resolution.

Deprecated re-export shim (Phase 2): the implementation moved to
``application/analysis/java/field_facts.py``. New code imports from the
``application.analysis.java`` package. This module stays so the worker hook
and existing tests keep importing unchanged.
"""

from __future__ import annotations

from investigation_agent_platform.application.analysis.java.field_facts import (
    resolve_java_field_fact,
)

__all__ = ["resolve_java_field_fact"]
