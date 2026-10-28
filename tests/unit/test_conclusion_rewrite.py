"""LWW-removal contract: conclusion rewrite transition predicate."""

from __future__ import annotations

from investigation_agent_platform.domain.finding.models import (
    TERMINAL_CONCLUSION_STATUSES,
    conclusion_rewrite_allowed,
)


def test_no_row_allows_insert() -> None:
    assert conclusion_rewrite_allowed(None, "ANSWERED") is True
    assert conclusion_rewrite_allowed(None, "INSUFFICIENT_EVIDENCE") is True


def test_same_status_allows_idempotent_rewrite() -> None:
    for s in (
        "ANSWERED",
        "ROOT_CAUSE_ESTABLISHED",
        "ROOT_CAUSE_LIKELY",
        "INSUFFICIENT_EVIDENCE",
        "NO_ROOT_CAUSE_FOUND",
    ):
        assert conclusion_rewrite_allowed(s, s) is True


def test_non_terminal_current_allows_anything() -> None:
    for cur in ("INSUFFICIENT_EVIDENCE", "NO_ROOT_CAUSE_FOUND"):
        for new in (
            "ANSWERED",
            "ROOT_CAUSE_ESTABLISHED",
            "INSUFFICIENT_EVIDENCE",
            "NO_ROOT_CAUSE_FOUND",
        ):
            assert conclusion_rewrite_allowed(cur, new) is True


def test_terminal_current_denies_different_status() -> None:
    assert TERMINAL_CONCLUSION_STATUSES == frozenset(
        {"ANSWERED", "ROOT_CAUSE_ESTABLISHED", "ROOT_CAUSE_LIKELY"}
    )
    for cur in ("ANSWERED", "ROOT_CAUSE_ESTABLISHED", "ROOT_CAUSE_LIKELY"):
        for new in (
            "ANSWERED",
            "ROOT_CAUSE_ESTABLISHED",
            "ROOT_CAUSE_LIKELY",
            "INSUFFICIENT_EVIDENCE",
            "NO_ROOT_CAUSE_FOUND",
        ):
            assert conclusion_rewrite_allowed(cur, new) is (cur == new)
