"""Verdict statement assembly (review H4 follow-up).

When NOT_CHECKABLE claims were excluded from verification, the statement that ships
in root_cause, Finding.statement and Finding.title is rendered from the claim groups
that DID verify, never from the raw hypothesis text (which may fold an opinion into
the same sentence as a checkable claim). Pure; never raises; fails closed to a fixed
neutral sentence.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from investigation_agent_platform.application.investigation.verification import VerificationEngine
from investigation_agent_platform.domain.hypothesis.models import ClaimKind

_MAX_STATEMENT = 2048
_MAX_SENTENCES = 12
NEUTRAL_STATEMENT = (
    "The checkable claims in this hypothesis verified; the claims that cannot be "
    "checked are excluded and listed under limitations."
)


@dataclass(frozen=True)
class VerdictStatement:
    text: str
    redacted: bool  # True when NOT_CHECKABLE claims were excluded and the text was rebuilt
    basis: tuple[str, ...] = ()  # claim kinds that contributed a sentence
    fell_back: bool = False  # True when only the neutral sentence could be produced


def _one_line(value: object, limit: int = 200) -> str:
    return " ".join(str(value or "").split())[:limit]


def _target(asm: Any) -> dict[str, Any]:
    target = getattr(asm, "claim_target", None)
    dump = getattr(target, "model_dump", None)
    return {k: v for k, v in (dump() if callable(dump) else {}).items() if v is not None}


def _sentence_for(kind: str, targets: list[dict[str, Any]]) -> str | None:
    if kind == ClaimKind.DEFINITION.value:
        parts = [
            f"{_one_line(t.get('symbol'))} is defined in {_one_line(t.get('file_path'))}"
            for t in targets
            if t.get("symbol") and t.get("file_path")
        ]
    elif kind == ClaimKind.CALL_EDGE.value:
        parts = [
            f"{_one_line(t.get('counterpart'))} calls {_one_line(t.get('symbol'))}"
            for t in targets
            if t.get("counterpart") and t.get("symbol")
        ]
    elif kind == ClaimKind.HISTORY.value:
        parts = [
            f"{_one_line(t.get('file_path'))} changed in commit {_one_line(t.get('commit_id'), 12)}"
            + (f" ({_one_line(t.get('commit_subject'))})" if t.get("commit_subject") else "")
            for t in targets
            if t.get("file_path") and t.get("commit_id")
        ]
    elif kind == ClaimKind.ABSENCE.value:
        parts = [
            f"no match for {_one_line(t.get('symbol'))} was found in "
            f"{_one_line(t.get('scope') or 'the searched scope')}"
            for t in targets
            if t.get("symbol")
        ]
    elif kind == ClaimKind.BEHAVIOR.value:
        parts = [
            f"{_one_line(t.get('symbol'))} behaves as claimed ({_one_line(t.get('claimed_value'))})"
            for t in targets
            if t.get("symbol") and t.get("claimed_value")
        ]
    else:
        parts = []
    return "; ".join(parts[:10]) if parts else None


def build_verdict_statement(hypothesis: Any, causal_chain: Sequence[Any] = ()) -> VerdictStatement:
    """The statement to publish for a verified hypothesis. Never raises."""
    try:
        original = str(getattr(hypothesis, "statement", "") or "")
        groups, excluded = VerificationEngine.typed_claim_groups(hypothesis)
        if excluded <= 0:
            return VerdictStatement(text=original[:_MAX_STATEMENT], redacted=False)
        sentences: list[str] = []
        basis: list[str] = []
        for (kind, _key), asms in sorted(groups.items()):
            if kind == ClaimKind.CAUSAL.value:
                steps = [
                    f"{_one_line(getattr(c, 'cause', ''))} leads to "
                    f"{_one_line(getattr(c, 'effect', ''))} via {_one_line(getattr(c, 'mechanism', ''))}"
                    for c in list(causal_chain or [])[:5]
                    if getattr(c, "cause", "") and getattr(c, "effect", "")
                ]
                sentence = "; ".join(steps) or None
            else:
                sentence = _sentence_for(kind, [_target(a) for a in asms])
            if sentence:
                sentences.append(sentence[0].upper() + sentence[1:])
                basis.append(kind)
        if not sentences:
            return VerdictStatement(text=NEUTRAL_STATEMENT, redacted=True, basis=(), fell_back=True)
        text = (". ".join(sentences[:_MAX_SENTENCES]) + ".")[:_MAX_STATEMENT]
        return VerdictStatement(text=text, redacted=True, basis=tuple(basis))
    except Exception:
        # Fail closed: a fault must never ship the raw statement.
        return VerdictStatement(text=NEUTRAL_STATEMENT, redacted=True, basis=(), fell_back=True)
