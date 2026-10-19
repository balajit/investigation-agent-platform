# src/investigation_agent_platform/domain/analysis/question_shape.py
"""Question-shape signal for datasource-choice scoping (misrouting fix).

Code-shaped questions ("where is X defined?", "who calls Y?") used to fall
into the prompt's empty-evidence default (SEARCH_LOGS first) or drift into
QUERY_STATE (Oracle banner rows for a code-location question). This pure
classifier labels the shape so reason orientation steers to the right
evidence family first. Guidance only — the validator still decides.
"""

from __future__ import annotations

import re

_CODE_PATTERNS = (
    "where is", "where are", "defined in", "implemented in", "declared in",
    "who calls", "who call", "callers of", "called by", "calls the",
    "what changed in", "recent changes", "commit history", "history of",
    "which file", "what file", "source of", "source for",
    "domain hub", "hub",
)
_CODE_EXT_RE = re.compile(r"\.(java|py|ts|tsx|js|go|rb|xml|gradle|yaml|yml)\b", re.IGNORECASE)
_SYMBOL_RE = re.compile(r"[A-Z][A-Za-z0-9]+(?:[A-Z][A-Za-z0-9]+)+")

_STATE_PATTERNS = (
    "database", "table", "how many", "count of", "row count", "schema",
    "oracle", "postgres", "version of the", "what version",
)

_LOG_PATTERNS = (
    "error", "exception", "traceback", "timed out", "timeout", "failed",
    "failing", "failure", "stack trace", " 500", " 503", "nullpointer",
)


def classify_question_shape(problem: str) -> str:
    """Return code | state | log | unknown for a problem description (pure)."""
    try:
        lowered = f" {(problem or '').lower()} "
        code_hits = sum(1 for p in _CODE_PATTERNS if p in lowered)
        if _CODE_EXT_RE.search(problem or ""):
            code_hits += 2
        if _SYMBOL_RE.search(problem or "") and any(
            w in lowered for w in ("where", "who", "what", "which", "how", "call")
        ):
            code_hits += 1
        state_hits = sum(1 for p in _STATE_PATTERNS if p in lowered)
        log_hits = sum(1 for p in _LOG_PATTERNS if p in lowered)
        if code_hits >= 2 or (code_hits == 1 and state_hits == 0 and log_hits == 0):
            return "code"
        if state_hits > code_hits and state_hits >= log_hits:
            return "state"
        if log_hits > code_hits and log_hits >= state_hits:
            return "log"
        return "unknown"
    except Exception:
        return "unknown"
