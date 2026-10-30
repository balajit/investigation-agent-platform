# tests/unit/test_s1_anchors.py
"""P1 (S1 intake anchors): onset T / last-good G / spread budget.

Domain anchors are version-pinned and stamped at both creation paths
(manual create + error intake). Unmarked rows resolve to explicit-unknown.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from investigation_agent_platform.domain.investigation.models import (
    ANCHORS_VERSION,
    InvestigationAnchors,
    InvestigationLimits,
    anchors_from_metadata,
    derive_working_window,
    stamp_anchors,
)


def _now() -> datetime:
    return datetime(2026, 10, 30, 12, 0, 0, tzinfo=UTC)


class TestStampAnchors:
    def test_absent_parameters_mean_explicit_unknown(self):
        anchors = stamp_anchors(None, _now(), 50)
        assert anchors.onset_t is None
        assert anchors.last_good_g is None
        assert anchors.onset_given is False
        assert anchors.last_good_given is False
        assert anchors.spread_budget == 50
        assert anchors.anchors_version == ANCHORS_VERSION

    def test_empty_dict_means_explicit_unknown(self):
        anchors = stamp_anchors({}, _now(), 50)
        assert anchors.onset_t is None and anchors.last_good_g is None
        assert anchors.onset_given is False and anchors.last_good_given is False

    def test_supplied_bounds_are_recorded_given(self):
        anchors = stamp_anchors(
            {"onset_t": "2026-10-30T10:00:00Z", "last_good_g": "2026-10-29T10:00:00Z"},
            _now(),
            40,
        )
        assert anchors.onset_t == datetime(2026, 10, 30, 10, 0, tzinfo=UTC)
        assert anchors.last_good_g == datetime(2026, 10, 29, 10, 0, tzinfo=UTC)
        assert anchors.onset_given is True and anchors.last_good_given is True
        assert anchors.spread_budget == 40

    def test_naive_datetimes_assumed_utc(self):
        anchors = stamp_anchors({"onset_t": "2026-10-30T10:00:00"}, _now(), 50)
        assert anchors.onset_t is not None and anchors.onset_t.tzinfo is not None

    def test_naive_datetime_objects_normalized_to_utc(self):
        anchors = stamp_anchors({"onset_t": datetime(2026, 10, 30, 10, 0, 0)}, _now(), 50)
        assert anchors.onset_t == datetime(2026, 10, 30, 10, tzinfo=UTC)
        assert anchors.onset_given is True

    def test_mixed_naive_and_aware_bounds_derive_cleanly(self):
        # Review feedback: a naive last_good_g with an aware onset_t used
        # to crash derive_working_window with TypeError. Both shapes now
        # normalize to UTC at the boundary.
        anchors = stamp_anchors(
            {
                "onset_t": datetime(2026, 10, 30, 10, tzinfo=UTC),
                "last_good_g": datetime(2026, 10, 29, 10, 0, 0),
            },
            _now(),
            50,
        )
        start, end, assumed = derive_working_window(anchors, _now(), 3600)
        assert (start, end) == (
            datetime(2026, 10, 29, 10, tzinfo=UTC),
            datetime(2026, 10, 30, 10, tzinfo=UTC),
        )
        assert assumed == ()

    def test_mixed_string_and_datetime_bounds_derive_cleanly(self):
        anchors = stamp_anchors(
            {
                "onset_t": "2026-10-30T10:00:00Z",
                "last_good_g": datetime(2026, 10, 29, 10, 0, 0),
            },
            _now(),
            50,
        )
        start, end, assumed = derive_working_window(anchors, _now(), 3600)
        assert end == datetime(2026, 10, 30, 10, tzinfo=UTC)
        assert start == datetime(2026, 10, 29, 10, tzinfo=UTC)
        assert assumed == ()

    def test_garbage_bound_fails_closed(self):
        with pytest.raises(ValueError):
            stamp_anchors({"onset_t": "not-a-time"}, _now(), 50)


class TestDeriveWorkingWindow:
    def test_both_given_window_is_exact(self):
        anchors = InvestigationAnchors(
            onset_t=datetime(2026, 10, 30, 10, tzinfo=UTC),
            last_good_g=datetime(2026, 10, 29, 10, tzinfo=UTC),
            onset_given=True,
            last_good_given=True,
        )
        start, end, assumed = derive_working_window(anchors, _now(), 3600)
        assert (start, end, assumed) == (
            datetime(2026, 10, 29, 10, tzinfo=UTC),
            datetime(2026, 10, 30, 10, tzinfo=UTC),
            (),
        )

    def test_unknown_g_assumes_lookback(self):
        anchors = InvestigationAnchors(
            onset_t=datetime(2026, 10, 30, 10, tzinfo=UTC), onset_given=True
        )
        start, end, assumed = derive_working_window(anchors, _now(), 3600)
        assert end == datetime(2026, 10, 30, 10, tzinfo=UTC)
        assert start == end - timedelta(seconds=3600)
        assert assumed == ("last_good",)

    def test_unknown_t_anchors_end_at_now(self):
        anchors = InvestigationAnchors()
        start, end, assumed = derive_working_window(anchors, _now(), 7200)
        assert end == _now()
        assert start == _now() - timedelta(seconds=7200)
        assert assumed == ("onset", "last_good")

    def test_inverted_bounds_clamp(self):
        anchors = InvestigationAnchors(
            onset_t=datetime(2026, 10, 29, 10, tzinfo=UTC),
            last_good_g=datetime(2026, 10, 30, 10, tzinfo=UTC),
            onset_given=True,
            last_good_given=True,
        )
        start, end, assumed = derive_working_window(anchors, _now(), 3600)
        assert start == end
        assert assumed == ()


class TestAnchorsFromMetadata:
    def test_unmarked_rows_resolve_explicit_unknown(self):
        anchors = anchors_from_metadata({})
        assert anchors.onset_t is None and anchors.last_good_g is None
        assert anchors.spread_budget == InvestigationLimits().max_tool_calls

    def test_malformed_anchors_fall_back(self):
        anchors = anchors_from_metadata({"anchors": {"spread_budget": "lots"}})
        assert anchors.onset_t is None
        assert anchors.spread_budget == InvestigationLimits().max_tool_calls

    def test_round_trip(self):
        stamped = stamp_anchors({"onset_t": "2026-10-30T10:00:00Z"}, _now(), 33)
        back = anchors_from_metadata({"anchors": stamped.model_dump(mode="json")})
        assert back == stamped


class TestIntakeStampsAnchors:
    def test_error_intake_records_explicit_unknown(self):
        from investigation_agent_platform.application.knowledge.intake import (
            ErrorIntakeService,
        )

        seen: dict = {}

        class _Repo:
            async def create(self, tenant_id, investigation):
                seen["metadata"] = dict(getattr(investigation, "metadata", None) or {})

        class _Clock:
            def utcnow(self):
                return _now()

        async def _run():
            svc = ErrorIntakeService(
                investigation_repo=_Repo(),
                session_repo=object(),
                code_issue_index=object(),
                clock=_Clock(),
            )
            await svc._create_investigation(
                tenant_id="t",
                application_id="a",
                problem_description="p",
                session_id="s",
                requested_by="u",
                fingerprint="f",
            )

        asyncio.run(_run())
        anchors = anchors_from_metadata(seen["metadata"])
        assert anchors.onset_t is None and anchors.last_good_g is None
        assert anchors.onset_given is False and anchors.last_good_given is False
        assert anchors.spread_budget == InvestigationLimits().max_tool_calls

    def test_error_intake_uses_profile_budget(self):
        from investigation_agent_platform.application.knowledge.intake import (
            ErrorIntakeService,
        )

        seen: dict = {}

        class _Repo:
            async def create(self, tenant_id, investigation):
                seen["metadata"] = dict(getattr(investigation, "metadata", None) or {})

        class _Cfg:
            evidence_budget = {"max_tool_calls": 12}

        class _Profile:
            investigation = _Cfg()

        class _ProfileRepo:
            async def get_by_application_id(self, tenant_id, application_id):
                return _Profile()

        class _Clock:
            def utcnow(self):
                return _now()

        # intake() also touches session_repo + code_issue_index; stub them.
        async def _run():
            sessions = []

            class _SessionRepo:
                async def append(self, tenant_id, session):
                    sessions.append(session)

            class _Index:
                async def latest_investigation(self, fp):
                    return None

                async def sessions_for_fingerprint(self, fp):
                    return []

                async def next_session_number(self, fp):
                    return 1

                async def record_session(self, fp, n, iid, now):
                    return None

            svc = ErrorIntakeService(
                investigation_repo=_Repo(),
                session_repo=_SessionRepo(),
                code_issue_index=_Index(),
                clock=_Clock(),
                profile_repo=_ProfileRepo(),
            )
            await svc.intake(
                tenant_id="t",
                application_id="a",
                error_class="E",
                repository="",
                revision="",
                failing_symbol="f",
                problem_description="p",
            )

        asyncio.run(_run())
        anchors = anchors_from_metadata(seen["metadata"])
        assert anchors.spread_budget == 12


class TestSpreadBudget:
    def test_default_envelope_without_profile(self):
        from investigation_agent_platform.application.investigation.services import (
            _spread_budget,
        )

        assert _spread_budget(None) == InvestigationLimits().max_tool_calls
        assert _spread_budget(object()) == InvestigationLimits().max_tool_calls

    def test_profile_overlay_wins(self):
        from investigation_agent_platform.application.investigation.services import (
            _spread_budget,
        )

        class _Cfg:
            evidence_budget = {"max_tool_calls": 12}

        class _Profile:
            investigation = _Cfg()

        assert _spread_budget(_Profile()) == 12

    def test_invalid_overlay_falls_back(self):
        from investigation_agent_platform.application.investigation.services import (
            _spread_budget,
        )

        class _Cfg:
            evidence_budget = {"max_tool_calls": "many"}

        class _Profile:
            investigation = _Cfg()

        assert _spread_budget(_Profile()) == InvestigationLimits().max_tool_calls

    def test_zero_and_non_int_overlay_fall_back_like_runtime(self):
        # The runtime overlay (overlay_evidence_budget) only applies
        # positive-int values, so a recorded 0 would disagree with the
        # effective envelope. _spread_budget reuses the runtime function,
        # pinned here against drift.
        from investigation_agent_platform.application.investigation.services import (
            _spread_budget,
        )

        for bad in (0, -3, None, "12"):

            class _Cfg:
                evidence_budget = {"max_tool_calls": bad}

            class _Profile:
                investigation = _Cfg()

            assert _spread_budget(_Profile()) == InvestigationLimits().max_tool_calls


class TestManualCreateStampsAnchors:
    @pytest.mark.asyncio
    async def test_create_stamps_explicit_unknown_and_budget(self):
        from investigation_agent_platform.api.dependencies import AppContext, set_app_context
        from investigation_agent_platform.domain.investigation.models import (
            InvestigationRequest,
            anchors_from_metadata,
        )

        ctx = AppContext()
        set_app_context(ctx)
        req = InvestigationRequest(
            application_id="example-app",
            problem_description="anchor stamping check",
            session_id="sess-anchors",
            requested_by="tester",
        )
        created = await ctx.create_investigation_service().execute(req, tenant_id="tenant-a")
        anchors = anchors_from_metadata(dict(getattr(created, "metadata", None) or {}))
        assert anchors.onset_t is None and anchors.last_good_g is None
        assert anchors.spread_budget >= 0

    @pytest.mark.asyncio
    async def test_create_honours_supplied_bounds(self):
        from investigation_agent_platform.api.dependencies import AppContext, set_app_context
        from investigation_agent_platform.domain.investigation.models import (
            InvestigationRequest,
            anchors_from_metadata,
        )

        ctx = AppContext()
        set_app_context(ctx)
        req = InvestigationRequest(
            application_id="example-app",
            problem_description="anchor bounds check",
            session_id="sess-anchors-given",
            requested_by="tester",
            parameters={
                "code_issue_fingerprint": "fp-anchors-given",
                "onset_t": "2026-10-30T10:00:00Z",
                "last_good_g": "2026-10-29T10:00:00Z",
            },
        )
        created = await ctx.create_investigation_service().execute(req, tenant_id="tenant-a")
        anchors = anchors_from_metadata(dict(getattr(created, "metadata", None) or {}))
        assert anchors.onset_t == datetime(2026, 10, 30, 10, tzinfo=UTC)
        assert anchors.last_good_g == datetime(2026, 10, 29, 10, tzinfo=UTC)
        assert anchors.onset_given is True and anchors.last_good_given is True
