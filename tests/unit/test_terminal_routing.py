# tests/unit/test_terminal_routing.py
"""Terminal next_check routing (D4, claim-proof gaps).

The verification_failed_or_limit_reached branch routes persisted
hypothesis assessment reasons through the existing abstention reason
router. Only a recognized targeted directive replaces the generic
fallback; ordinary assessment prose is ignored. Fail-closed throughout.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from investigation_agent_platform.application.worker.activities.lifecycle import (
    _collect_hypothesis_gate_reasons,
    _route_terminal_check,
)

GENERIC = (
    "Bind supporting evidence and a cited causal "
    "chain to a candidate hypothesis, then re-verify."
)


def _asm(reason: str):
    return SimpleNamespace(reason=reason)


def _hyp(*reasons: str):
    return SimpleNamespace(assessments=[_asm(r) for r in reasons])


class TestCollectHypothesisGateReasons:
    @pytest.mark.asyncio
    async def test_collects_assessment_reasons(self) -> None:
        from unittest.mock import AsyncMock

        repo = SimpleNamespace(
            find_by_investigation_id=AsyncMock(
                return_value=[_hyp("no bound supporting evidence", "ordinary prose")]
            )
        )
        ctx = SimpleNamespace(hypothesis_repo=repo)
        reasons = await _collect_hypothesis_gate_reasons(ctx, "t", uuid4())
        assert reasons == ["no bound supporting evidence", "ordinary prose"]

    @pytest.mark.asyncio
    async def test_missing_repo_fails_closed(self) -> None:
        ctx = SimpleNamespace(hypothesis_repo=None)
        assert await _collect_hypothesis_gate_reasons(ctx, "t", uuid4()) == []

    @pytest.mark.asyncio
    async def test_repo_error_fails_closed(self) -> None:
        from unittest.mock import AsyncMock

        repo = SimpleNamespace(
            find_by_investigation_id=AsyncMock(side_effect=RuntimeError("db down"))
        )
        ctx = SimpleNamespace(hypothesis_repo=repo)
        assert await _collect_hypothesis_gate_reasons(ctx, "t", uuid4()) == []

    @pytest.mark.asyncio
    async def test_blank_reasons_skipped(self) -> None:
        from unittest.mock import AsyncMock

        repo = SimpleNamespace(
            find_by_investigation_id=AsyncMock(return_value=[_hyp("", "  ")])
        )
        ctx = SimpleNamespace(hypothesis_repo=repo)
        assert await _collect_hypothesis_gate_reasons(ctx, "t", uuid4()) == []


class TestRouteTerminalCheck:
    def test_recognized_reason_routes(self) -> None:
        routed = _route_terminal_check(["no bound supporting evidence"], GENERIC)
        assert routed != GENERIC
        assert "re-verify" in routed

    def test_ordinary_prose_keeps_generic(self) -> None:
        assert (
            _route_terminal_check(["cited by CALL_EDGE claim"], GENERIC) == GENERIC
        )
        assert _route_terminal_check(["recent change"], GENERIC) == GENERIC

    def test_empty_reasons_keep_generic(self) -> None:
        assert _route_terminal_check([], GENERIC) == GENERIC

    def test_caller_enumeration_reason_routes(self) -> None:
        routed = _route_terminal_check(
            ["caller enumeration names a caller without its own CALL_EDGE claim"],
            GENERIC,
        )
        assert routed != GENERIC
        assert "CALL_EDGE" in routed

    def test_targeted_resolve_reason_routes(self) -> None:
        # Router-literal shape ("item(s)"): the targeted "Resolve the ..."
        # row fires and survives the passthrough net.
        routed = _route_terminal_check(
            ["3 refuting evidence item(s) bound to rival"], GENERIC
        )
        assert routed != GENERIC
        assert routed.startswith("Resolve the 3 refuting")

    def test_passthrough_shaped_prose_ignored(self) -> None:
        # An unrecognized reason that happens to look like the fallback
        # must not slip through: the closed set drops it pre-router.
        assert (
            _route_terminal_check(["Resolve 'x', then re-verify."], GENERIC)
            == GENERIC
        )

    def test_router_fallback_format_pinned(self) -> None:
        # Coupling pin: _route_terminal_check's passthrough template must
        # match the router's actual fallback. If the router changes its
        # fallback format, this test names the second place to update.
        from investigation_agent_platform.application.investigation.verification import (
            VerificationEngine,
        )

        assert (
            VerificationEngine.next_check_for_abstention(
                "unverified", ["zzz unrecognized prose"]
            )
            == "Resolve 'zzz unrecognized prose', then re-verify."
        )
