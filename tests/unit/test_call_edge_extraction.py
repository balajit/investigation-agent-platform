# tests/unit/test_call_edge_extraction.py
"""Call-edge extraction + inference matrix (D2 harness, claim-proof gaps).

Red-first contract: every new phrasing form is pinned here before the
implementation lands. Covers typed claims (extraction within the gate)
and untyped inference (gate entry), both pinned per-version. Legacy
compatibility is I/O equivalence on the pinned case list, not regex
identity.
"""

from __future__ import annotations

import pytest

from investigation_agent_platform.application.investigation.verification import (
    VerificationEngine as VE,
)


@pytest.mark.parametrize(
    ("statement", "callee", "caller"),
    [
        # Legacy forms (pinned I/O on both tables).
        ("callers of OrderService", "OrderService", None),
        ("caller of OrderService", "OrderService", None),
        ("OrderService is called by OrderController", "OrderService", "OrderController"),
        ("OrderService called by OrderController", "OrderService", "OrderController"),
        ("OrderService are called by A and B", "OrderService", "A"),
        # New interrogative forms.
        ("What calls OrderService?", "OrderService", None),
        ("What invokes OrderService.submit?", "OrderService.submit", None),
        ("Who calls OrderService?", "OrderService", None),
        ("Who invokes OrderService?", "OrderService", None),
        ("Where is OrderService called?", "OrderService", None),
        ("Where is OrderService invoked?", "OrderService", None),
        # Multi-word prose is not a symbol: the typed path owns those.
        ("What calls the order service handler?", None, None),
        # New nominal / edge forms.
        ("call sites of OrderService", "OrderService", None),
        ("invocations of OrderService.submit", "OrderService.submit", None),
        ("OrderService is invoked by OrderController", "OrderService", "OrderController"),
        ("OrderService invoked by OrderController", "OrderService", "OrderController"),
        # Quoted + qualified identifiers.
        ("What calls `OrderService`?", "OrderService", None),
        ("What calls `handler`?", "handler", None),
        ("Who invokes order_service?", "order_service", None),
        ('callers of "com.sample.OrderService"', "com.sample.OrderService", None),
        ("Who calls OrderService#submit?", "OrderService#submit", None),
        ("Who calls OrderService::submit?", "OrderService::submit", None),
        # Adversarial: no edge.
        ("", None, None),
        ("OrderService", None, None),
        ("Where is the configuration stored?", None, None),
        ("Recall is invoked by the schedule", "Recall", "schedule"),
    ],
)
def test_extract_call_edge_matrix(statement: str, callee, caller) -> None:
    assert VE._extract_call_edge(statement, legacy=False) == (callee, caller)


@pytest.mark.parametrize(
    ("statement", "callee", "caller"),
    [
        ("callers of OrderService", "OrderService", None),
        ("OrderService is called by OrderController", "OrderService", "OrderController"),
        ("OrderService called by OrderController", "OrderService", "OrderController"),
    ],
)
def test_extract_call_edge_legacy_pinned(statement: str, callee, caller) -> None:
    assert VE._extract_call_edge(statement, legacy=True) == (callee, caller)
    # Legacy table does not know the new forms.
    assert VE._extract_call_edge("What calls OrderService?", legacy=True) == (None, None)
    assert VE._extract_call_edge("Where is OrderService called?", legacy=True) == (None, None)


@pytest.mark.parametrize(
    "statement",
    [
        "What calls OrderService?",
        "What invokes OrderService.submit?",
        "Who calls OrderService?",
        "Who invokes OrderService?",
        "call sites of OrderService",
        "invocations of OrderService.submit",
        "OrderService invoked by OrderController",
    ],
)
def test_infers_call_edge_positive(statement: str) -> None:
    assert VE.infers_call_edge(statement) is True


@pytest.mark.parametrize(
    "statement",
    [
        "",
        "OrderService handles orders.",
        "Where is the configuration stored?",
        "The caller hung up.",
        "Recall was scheduled hourly.",
    ],
)
def test_infers_call_edge_negative(statement: str) -> None:
    assert VE.infers_call_edge(statement) is False


def _untyped(statement: str):
    from types import SimpleNamespace

    return SimpleNamespace(claim_kind=None, statement=statement)


def test_legacy_inference_routes_through_effective_kind() -> None:
    from investigation_agent_platform.domain.hypothesis.models import ClaimKind

    assert VE.effective_claim_kind(_untyped("callers of OrderService")) == ClaimKind.CALL_EDGE
    assert VE.effective_claim_kind(_untyped("What calls OrderService?")) is None


def test_resolve_claim_kind_seam() -> None:
    from investigation_agent_platform.domain.hypothesis.models import ClaimKind

    hyp = _untyped("What calls OrderService?")
    assert VE.resolve_claim_kind(hyp, strict=False) is None
    assert VE.resolve_claim_kind(hyp, strict=True) == ClaimKind.CALL_EDGE
    legacy = _untyped("callers of OrderService")
    assert VE.resolve_claim_kind(legacy, strict=False) == ClaimKind.CALL_EDGE
    plain = _untyped("OrderService handles orders.")
    assert VE.resolve_claim_kind(plain, strict=True) is None
    assert VE.resolve_claim_kind(plain, strict=False) is None


def test_caller_edge_seam_admits_untyped_only_under_strict() -> None:
    """Consumer audit: evaluate_caller_edge (the single decision seam)
    must enter the gate for an untyped new-form statement under cbv2
    and stay out under cbv1. A tuple (even False) proves entry;
    None proves the seam stayed shut."""

    from types import SimpleNamespace
    from uuid import uuid4

    hyp = SimpleNamespace(
        id=uuid4(), claim_kind=None, statement="What calls OrderService?", assessments=[]
    )
    cbv1 = VE(claim_binding_version="cbv1")
    assert cbv1.evaluate_caller_edge(hyp, []) is None
    cbv2 = VE(claim_binding_version="cbv2")
    entered = cbv2.evaluate_caller_edge(hyp, [])
    assert entered is not None
    assert entered[0] is False


class TestCallShownConstruction:
    def test_construction_excluded_only_with_flag(self) -> None:
        snippet = "holder = new OrderServiceHandler();"
        assert (
            VE._call_shown("OrderServiceHandler", snippet, exclude_construction=True)
            is False
        )
        assert (
            VE._call_shown("OrderServiceHandler", snippet, exclude_construction=False)
            is True
        )

    def test_real_call_shown_regardless_of_flag(self) -> None:
        snippet = "proxy.submit(orderId);"
        assert (
            VE._call_shown("OrderService.submit", snippet, exclude_construction=True)
            is True
        )
        assert (
            VE._call_shown("OrderService.submit", snippet, exclude_construction=False)
            is True
        )

    def test_declaration_never_a_call_site(self) -> None:
        snippet = "public void submit(String orderId) {"
        assert (
            VE._call_shown("OrderService.submit", snippet, exclude_construction=False)
            is False
        )

    def test_every_call_site_pins_the_flag(self) -> None:
        """Audit: every _call_shown call site passes exclude_construction
        explicitly — no default reliance, so a future site cannot
        silently inherit the permissive behavior."""
        import re as _re
        from pathlib import Path as _Path

        root = _Path(__file__).resolve().parents[2]
        src = (
            root
            / "src/investigation_agent_platform/application/investigation/verification.py"
        ).read_text()
        calls = [
            m.start() for m in _re.finditer(r"(?<!def )_call_shown\(", src)
        ]
        assert calls, "expected _call_shown call sites"
        for pos in calls:
            window = src[pos : pos + 400]
            assert "exclude_construction=" in window, (
                "call site passes exclude_construction explicitly"
            )
