# tests/unit/test_s4_qualification.py
"""P2 (S4 shape-aware qualification): predicates judge readiness only."""

from __future__ import annotations

from datetime import UTC, datetime

from investigation_agent_platform.domain.evidence.qualification import (
    QUALIFIER_VERSION,
    Qualification,
    qualify_absence_proof,
    qualify_code_row,
    qualify_contrast_pair,
    qualify_history_row,
    qualify_item,
    qualify_runtime_row,
    s4_ready,
)


def _row(etype, location=None, attributes=None, observed_at=None):
    row = {"evidence_type": etype}
    if location is not None:
        row["provenance"] = {"source_location": dict(location), "source_system": "git"}
    if attributes is not None:
        row["attributes"] = dict(attributes)
    if observed_at is not None:
        row["observed_at"] = observed_at
    return row


def _code_row(**overrides):
    loc = {"file_path": "src/A.java", "line_start": 10, "line_end": 20, "revision": "abc123"}
    loc.update(overrides.pop("location", {}))
    base = _row("SOURCE_CODE", location=loc, attributes=overrides.pop("attributes", {}))
    base.update(overrides)
    return base


_G = datetime(2026, 10, 29, 10, tzinfo=UTC)
_T = datetime(2026, 10, 30, 10, tzinfo=UTC)
_WINDOW = (_G, _T)


class TestCodeQualification:
    def test_four_pinned_fields_qualify(self):
        qual = qualify_code_row(_code_row())
        assert qual == Qualification(True, "code", "4 pinned fields present", QUALIFIER_VERSION)

    def test_start_line_only_does_not_qualify(self):
        qual = qualify_code_row(_code_row(location={"line_end": None}))
        assert qual.qualified is False
        assert "start-line-only" in qual.reason

    def test_missing_revision_does_not_qualify(self):
        qual = qualify_code_row(_code_row(location={"revision": ""}))
        assert qual.qualified is False

    def test_bool_lines_rejected(self):
        qual = qualify_code_row(_code_row(location={"line_start": True, "line_end": True}))
        assert qual.qualified is False

    def test_failure_rows_never_qualify(self):
        qual = qualify_code_row(
            _code_row(attributes={"miss_reason": "timeout"})
        )
        assert qual.qualified is False


class TestHistoryQualification:
    def _history(self, **kw):
        loc = {"file_path": "src/A.java", "revision": "sha1"}
        loc.update(kw.pop("location", {}))
        return _row("COMMIT_HISTORY", location=loc, attributes=kw.pop("attributes", {}))

    def test_pinned_commit_qualifies(self):
        assert qualify_history_row(self._history()).qualified is True

    def test_discovery_only_never_qualifies(self):
        qual = qualify_history_row(self._history(attributes={"discovery_only": True}))
        assert qual.qualified is False

    def test_zero_match_routes_to_proof(self):
        qual = qualify_item(
            self._history(attributes={"match_count": 0, "search_scope": "repo@tip:src/A.java"}),
            scope_path="src/A.java",
        )
        assert qual.qualified is True
        assert qual.layer == "absence-proof"

    def test_zero_match_code_row_routes_to_proof(self):
        # Execution-banked absence markers are SOURCE_CODE-typed; the
        # zero-count routes them to the proof branch for a precise reason.
        row = _row(
            "SOURCE_CODE",
            attributes={"match_count": 0, "search_scope": "svc:query=x"},
        )
        qual = qualify_item(row)
        assert qual.layer == "absence-proof"
        assert qual.qualified is False  # discovery markers never prove

    def test_zero_match_without_discovery_flag_can_qualify(self):
        row = _row(
            "SOURCE_CODE",
            location={"file_path": "x", "revision": "tip"},
            attributes={"match_count": 0, "search_scope": "svc:query=x"},
        )
        qual = qualify_item(row, scope_path="svc:query=x")
        assert qual.qualified is True
        assert qual.layer == "absence-proof"

    def test_scope_mismatch_rejected(self):
        qual = qualify_history_row(self._history(), scope_path="src/Other.java")
        assert qual.qualified is False


class TestAbsenceProof:
    def test_scoped_proof_qualifies(self):
        row = _row(
            "COMMIT_HISTORY",
            location={"file_path": "src/A.java", "revision": "tip"},
            attributes={"match_count": 0, "search_scope": "repo@tip:src/A.java"},
        )
        qual = qualify_absence_proof(row, "src/A.java")
        assert qual.qualified is True

    def test_wrong_scope_rejected(self):
        row = _row(
            "COMMIT_HISTORY",
            location={"file_path": "src/A.java", "revision": "tip"},
            attributes={"match_count": 0, "search_scope": "repo@tip:src/A.java"},
        )
        assert qualify_absence_proof(row, "src/B.java").qualified is False

    def test_unpinned_proof_rejected(self):
        row = _row(
            "COMMIT_HISTORY",
            location={"file_path": "src/A.java", "revision": ""},
            attributes={"match_count": 0, "search_scope": "src/A.java"},
        )
        assert qualify_absence_proof(row, "src/A.java").qualified is False


class TestRuntimeQualification:
    def _runtime(self, observed, identity="Elasticsearch"):
        row = _row("RUNTIME_LOG", attributes={}, observed_at=observed)
        row["provenance"] = {
            "source_location": {},
            "source_system": identity,
        }
        return row

    def test_window_pinned_row_qualifies(self):
        qual = qualify_runtime_row(self._runtime("2026-10-29T12:00:00Z"), _WINDOW)
        assert qual.qualified is True

    def test_outside_window_rejected(self):
        qual = qualify_runtime_row(self._runtime("2026-10-28T12:00:00Z"), _WINDOW)
        assert qual.qualified is False

    def test_no_timestamp_rejected(self):
        assert qualify_runtime_row(self._runtime(None), _WINDOW).qualified is False

    def test_no_window_rejected(self):
        qual = qualify_runtime_row(self._runtime("2026-10-29T12:00:00Z"), None)
        assert qual.qualified is False
        assert "window" in qual.reason


class TestContrastQualification:
    def test_ordered_halves_qualify(self):
        old = _row("RUNTIME_LOG", observed_at="2026-10-29T09:00:00Z")
        new = _row("RUNTIME_LOG", observed_at="2026-10-30T11:00:00Z")
        assert qualify_contrast_pair(old, new).qualified is True

    def test_misordered_rejected(self):
        old = _row("RUNTIME_LOG", observed_at="2026-10-30T11:00:00Z")
        new = _row("RUNTIME_LOG", observed_at="2026-10-29T09:00:00Z")
        assert qualify_contrast_pair(old, new).qualified is False

    def test_missing_timestamp_rejected(self):
        assert qualify_contrast_pair(_row("RUNTIME_LOG"), _row("RUNTIME_LOG")).qualified is False


class TestS4Ready:
    def test_code_shape_needs_code_row(self):
        assert s4_ready("code", [])[0] is False
        assert s4_ready("code", [Qualification(True, "code", "")])[0] is True

    def test_code_shape_accepts_history(self):
        ready, _, by = s4_ready("code", [Qualification(True, "history", "")])
        assert ready is True and by == "history"

    def test_path_only_rows_never_carry_readiness(self):
        quals = [Qualification(False, "code", "start-line-only row is not ready")]
        ready, missing, _ = s4_ready("code", quals)
        assert ready is False
        assert "qualified code row" in missing

    def test_absence_branch_carries_log_shape(self):
        quals = [Qualification(True, "absence-proof", "")]
        ready, _, by = s4_ready("log", quals)
        assert ready is True and by == "absence-proof"

    def test_absence_proof_loses_to_positive_rows(self):
        quals = [
            Qualification(True, "absence-proof", ""),
            Qualification(True, "runtime", ""),
        ]
        ready, _, by = s4_ready("log", quals)
        assert ready is True and by == "runtime"

    def test_unknown_shape_takes_any_qualified_layer(self):
        assert s4_ready("unknown", [Qualification(True, "probe", "")])[0] is True
        assert s4_ready("unknown", [Qualification(False, "code", "x")])[0] is False

    def test_state_shape(self):
        assert s4_ready("state", [Qualification(True, "state", "")])[0] is True
        assert s4_ready("state", [])[0] is False


class TestBankStamp:
    def test_stamp_marks_qualified_dict_row(self):
        from investigation_agent_platform.application.worker.activities.execution import (
            _with_s4_qualification,
        )

        row = _row(
            "SOURCE_CODE",
            location={
                "file_path": "src/A.java",
                "line_start": 1,
                "line_end": 9,
                "revision": "r",
            },
        )
        out = _with_s4_qualification(row, None)
        assert out["attributes"]["s4_qualified"] is True
        assert out["attributes"]["s4_layer"] == "code"

    def test_stamp_marks_path_only_row_unqualified(self):
        from investigation_agent_platform.application.worker.activities.execution import (
            _with_s4_qualification,
        )

        row = _row(
            "SOURCE_CODE",
            location={"file_path": "src/A.java", "line_start": 1},
        )
        out = _with_s4_qualification(row, None)
        assert out["attributes"]["s4_qualified"] is False
        assert "start-line-only" in out["attributes"]["s4_reason"]

    def test_stamp_marks_unknown_layer(self):
        from investigation_agent_platform.application.worker.activities.execution import (
            _with_s4_qualification,
        )

        out = _with_s4_qualification(_row("JOURNEY", location={}), None)
        assert out["attributes"]["s4_qualified"] is False


class TestReasonReadiness:
    def test_readiness_keys_present_for_code_question(self):
        from investigation_agent_platform.application.worker.activities.reasoning import (
            _s4_readiness,
        )

        class _Request:
            problem_description = "Where is SampleOrderFilter implemented?"

        class _Investigation:
            metadata = {"anchors": {"onset_t": None, "last_good_g": None}}
            request = _Request()

        rows = [
            _row(
                "SOURCE_CODE",
                location={
                    "file_path": "src/SampleOrderFilter.java",
                    "line_start": 72,
                    "line_end": 80,
                    "revision": "abc",
                },
            )
        ]
        out = _s4_readiness(rows, _Investigation())
        assert out["s4_ready"] is True
        assert out["s4_satisfied_by"] == "code"
        assert out["s4_shape"] == "code"
        assert out["s4_qualified_layers"] == {"code": 1}

    def test_readiness_false_without_qualified_rows(self):
        from investigation_agent_platform.application.worker.activities.reasoning import (
            _s4_readiness,
        )

        class _Request:
            problem_description = "Where is SampleOrderFilter implemented?"

        class _Investigation:
            metadata = {}
            request = _Request()

        out = _s4_readiness([], _Investigation())
        assert out["s4_ready"] is False
        assert out["s4_missing"] != ""
