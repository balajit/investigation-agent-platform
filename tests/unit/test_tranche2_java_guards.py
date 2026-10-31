# tests/unit/test_tranche2_java_guards.py
"""Tranche 2 Java analysis guards (review J4, J7). Synthetic sources only."""

from __future__ import annotations

import pytest
from test_java_window_facts import _evidence

from investigation_agent_platform.application.analysis.java.window_facts import (
    enumerate_java_window_facts,
)

# f and g share one source line. g is syntactically broken (missing ';').
_SAME_LINE = (
    "package com.sample;\n"
    "public class A {\n"
    '    String f(){ if (c) return "a"; return "q"; } '
    'String g(){ if (d) return "b" return "z"; }\n'
    "}\n"
)


def _subjects(records):  # type: ignore[no-untyped-def]
    return {(r.subject_identity, r.resolved_value) for r in records}


class TestJ4ByteSpanDeclarationIdentity:
    def test_clean_method_on_same_line_does_not_vouch_for_broken_one(self) -> None:
        records = enumerate_java_window_facts(_evidence(_SAME_LINE)).records
        subjects = _subjects(records)
        assert not any(subject and subject.endswith(".g") for subject, _ in subjects), (
            f"broken method g was emitted via its neighbour's clean span: {subjects}"
        )

    def test_clean_neighbour_is_still_emitted(self) -> None:
        records = enumerate_java_window_facts(_evidence(_SAME_LINE)).records
        subjects = _subjects(records)
        assert any(subject and subject.endswith(".f") for subject, _ in subjects), subjects

    def test_ordinary_multiline_source_is_unchanged(self) -> None:
        source = (
            "package com.sample;\n"
            "public class A {\n"
            "    String f(boolean c) {\n"
            '        if (c) { return "a"; }\n'
            '        return "q";\n'
            "    }\n"
            "}\n"
        )
        records = enumerate_java_window_facts(_evidence(source)).records
        assert any(r.subject_identity.endswith(".f") for r in records)


class TestJ7PathOnlyHeadReadIsNotARangeRead:
    """TRACE_LOGIC banked a file-head read (no anchor line) with no read_mode,
    and the verifier's range-read guard accepted it as decisive."""

    @staticmethod
    async def _run_trace(symbol_line: int):  # type: ignore[no-untyped-def]
        from types import SimpleNamespace

        from test_phase0_contract_repair import _code_evidence, _ctx_with, _input

        from investigation_agent_platform.application.worker.activities import (
            execute_action_activity,
        )

        seen_requests: list = []

        async def _find_symbol(tenant_id, investigation_id, application_id, req):  # type: ignore[no-untyped-def]
            return SimpleNamespace(
                items=[],
                total_count=1,
                provider_metadata={
                    "symbols": [
                        {
                            "name": "price",
                            "kind": "method_definition",
                            "location": {
                                "file_path": "OfferSvc.java",
                                "line_number": symbol_line,
                                "snippet": "public BigDecimal price()",
                            },
                        }
                    ]
                },
            )

        async def _get_source(tenant_id, investigation_id, application_id, req):  # type: ignore[no-untyped-def]
            seen_requests.append(req)
            # What the provider returns for an un-ranged read: the bounded
            # head, with provenance recording the ACTUAL lines 1..80.
            return _code_evidence(file_path="OfferSvc.java", line_start=1, line_end=80)

        _, saved = _ctx_with(SimpleNamespace(find_symbol=_find_symbol, get_source=_get_source))
        await execute_action_activity(_input("TRACE_LOGIC", {"symbol": "price"}))
        return saved, seen_requests

    @pytest.mark.asyncio
    async def test_unanchored_head_read_is_labeled_path_only(self) -> None:
        from investigation_agent_platform.application.investigation.verification import (
            VerificationEngine,
        )

        saved, requests = await self._run_trace(symbol_line=0)
        assert requests and requests[0].start_line is None, "premise: un-ranged read"
        code_rows = [
            r for r in saved if getattr(r.provenance.query_fingerprint, "operation", "") == "GET_SOURCE"
        ]
        assert code_rows, "the head read was not banked"
        for row in code_rows:
            assert row.attributes.get("read_mode") == "path_only"
            assert row.attributes.get("discovery_only") is True
            assert not VerificationEngine._is_range_read(row), (
                "a file-head read must never count as a decisive range read"
            )

    @pytest.mark.asyncio
    async def test_anchored_window_read_is_labeled_range(self) -> None:
        from investigation_agent_platform.application.investigation.verification import (
            VerificationEngine,
        )

        saved, requests = await self._run_trace(symbol_line=200)
        assert requests and requests[0].start_line is not None, "premise: ranged read"
        code_rows = [
            r for r in saved if getattr(r.provenance.query_fingerprint, "operation", "") == "GET_SOURCE"
        ]
        assert code_rows
        for row in code_rows:
            assert row.attributes.get("read_mode") == "range"
            assert not row.attributes.get("discovery_only")
            assert VerificationEngine._is_range_read(row)


class TestJ7ResidualGuardRequiresExplicitRangeLabel:
    """A provenance window is not proof of a range read (the provider records
    the actual lines even for an un-ranged head read)."""

    @staticmethod
    def _row(attributes):  # type: ignore[no-untyped-def]
        from test_phase0_contract_repair import _code_evidence

        return _code_evidence(
            file_path="A.java", line_start=1, line_end=80, attributes=attributes
        )

    def test_unlabeled_windowed_row_is_not_a_range_read(self) -> None:
        from investigation_agent_platform.application.investigation.verification import (
            VerificationEngine,
        )

        assert not VerificationEngine._is_range_read(self._row({}))

    def test_path_only_and_discovery_rows_are_not_range_reads(self) -> None:
        from investigation_agent_platform.application.investigation.verification import (
            VerificationEngine,
        )

        assert not VerificationEngine._is_range_read(self._row({"read_mode": "path_only"}))
        assert not VerificationEngine._is_range_read(
            self._row({"read_mode": "range", "discovery_only": True})
        )

    def test_labeled_range_row_is_a_range_read(self) -> None:
        from investigation_agent_platform.application.investigation.verification import (
            VerificationEngine,
        )

        assert VerificationEngine._is_range_read(self._row({"read_mode": "range"}))
