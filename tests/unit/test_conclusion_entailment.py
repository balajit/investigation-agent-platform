# tests/unit/test_conclusion_entailment.py
"""Phase 2b: causal conclusion sentences need single-row entailment."""

from __future__ import annotations

from types import SimpleNamespace

from investigation_agent_platform.application.investigation.verification import (
    VerificationEngine as E,
)


def _row(**kw):
    base = {"title": "", "summary": "", "content_snippet": "", "source": ""}
    base.update(kw)
    return SimpleNamespace(**base)


class TestCausalSentenceEntailed:
    def test_cooccurring_row_entails(self) -> None:
        rows = [_row(content_snippet="null default flows into handler causing NPE on retry")]
        assert E.causal_sentence_entailed("null default causes NPE", rows) is True

    def test_scattered_rows_do_not_entail(self) -> None:
        rows = [
            _row(content_snippet="null default configured in application properties"),
            _row(content_snippet="handler threw NPE during replay"),
        ]
        assert E.causal_sentence_entailed("null default causes NPE", rows) is False

    def test_non_causal_sentence_is_not_applicable(self) -> None:
        assert E.causal_sentence_entailed("handler lives in src/a.py", [_row()]) is None

    def test_vague_effect_falls_back(self) -> None:
        # No extractable effect tokens: not applicable, existing mapping decides.
        assert E.causal_sentence_entailed("null default causes it", [_row()]) is None

    def test_chain_link_text_can_entail(self) -> None:
        assert (
            E.causal_sentence_entailed(
                "null default causes NPE",
                [_row(content_snippet="unrelated")],
                ["null default flows into handler ; NPE thrown"],
            )
            is True
        )

    def test_garbage_never_demotes(self) -> None:
        assert E.causal_sentence_entailed(None, None) is None  # type: ignore[arg-type]
        assert E.causal_sentence_entailed("x causes y", [{"a": 1}]) is None


class TestMapConclusionSentences:
    def test_entailed_causal_sentence_maps(self) -> None:
        rows = [_row(content_snippet="null default flows into handler causing NPE")]
        assert E.map_conclusion_sentences("Null default causes NPE.", rows, []) == []

    def test_scattered_causal_sentence_unmapped(self) -> None:
        rows = [
            _row(content_snippet="null default configured in application properties"),
            _row(content_snippet="handler threw NPE during replay"),
        ]
        unmapped = E.map_conclusion_sentences("Null default causes NPE.", rows, [])
        assert unmapped == ["Null default causes NPE"]

    def test_non_causal_mapping_unchanged(self) -> None:
        rows = [_row(content_snippet="handler lives in src/orders.py")]
        assert E.map_conclusion_sentences("Handler lives in src/orders.py.", rows, []) == []
