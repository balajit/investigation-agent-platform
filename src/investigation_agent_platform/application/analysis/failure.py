# src/investigation_agent_platform/application/analysis/failure.py
"""Typed analyzer failures (J D7: one failure helper, no silent excepts)."""

from __future__ import annotations


class AnalysisFailure(Exception):
    """Base for typed analyzer failures. Never carries payloads or rows."""


class GrammarUnavailable(AnalysisFailure):
    """The grammar for a language cannot be loaded (cold cache, air gap, unknown language)."""


def failure_code(exc: BaseException) -> str:
    """Limitation code for a caught exception: ``grammar_unavailable`` only for
    :class:`GrammarUnavailable`, ``analysis_failed`` for everything else (registered in
    limitations.py). The exception name rides in ``assumptions``, never in the code."""
    if isinstance(exc, GrammarUnavailable):
        return "grammar_unavailable"
    return "analysis_failed"
