# src/investigation_agent_platform/application/analysis/failure.py
"""One failure vocabulary for every analyzer (J D7).

Replaces ad-hoc ``java_parser_unavailable:<ExceptionName>`` /
``python_parser_unavailable:<ExceptionName>`` strings and the malformed
``analysis_failed`` stamp with a valid UNRESOLVED payload. The exception
message is never recorded — only the class name, in ``assumptions``.
"""

from __future__ import annotations

from typing import Any

from investigation_agent_platform.domain.code.models import (
    CodeFactResolutionV2,
    CodeFactStatus,
    CodeValueSemantics,
    JavaFactCategory,
)


class GrammarUnavailable(Exception):
    """The grammar library could not be loaded (cold cache, no network, bad binary)."""


def failure_code(exc: BaseException) -> str:
    """Coarse failure code for an analyzer exception (no message, no type string)."""
    return "grammar_unavailable" if isinstance(exc, GrammarUnavailable) else "analysis_failed"


def analysis_failed_resolution(
    evidence: Any,
    *,
    category: JavaFactCategory,
    subject: str | None,
    exc: BaseException,
    parser_version: str,
) -> CodeFactResolutionV2:
    """A valid UNRESOLVED payload naming the failure; the message is never recorded."""
    location = getattr(getattr(evidence, "provenance", None), "source_location", None)
    evidence_id = getattr(evidence, "evidence_id", None)
    return CodeFactResolutionV2(
        status=CodeFactStatus.UNRESOLVED,
        revision=str(getattr(location, "revision", "") or "") or None,
        fact_category=category,
        subject_identity=subject or None,
        value_semantics=CodeValueSemantics.EXPLICIT_INITIALIZER,
        source_evidence_ids=(evidence_id,) if evidence_id is not None else (),
        limitation_codes=(failure_code(exc),),
        assumptions=(f"exception_type={type(exc).__name__}",),
        parser_version=parser_version,
    )
