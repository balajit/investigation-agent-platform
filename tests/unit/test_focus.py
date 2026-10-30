# tests/unit/test_focus.py
"""P6 2b-i focus pivot: extraction, shift detection, liveness, forcing."""

from investigation_agent_platform.application.worker.workflows.forcing import (
    _should_force_pivot_recollect,
)
from investigation_agent_platform.domain.collection.focus import (
    FocusPoint,
    FocusShift,
    detect_shift,
    focus_from_reason_data,
    pivot_can_pay_off,
)


def _prev(**over):  # type: ignore[no-untyped-def]
    base = {
        "surface": "tenant/svc/a.py",
        "surface_kind": "file",
        "line_start": 1,
        "line_end": 50,
    }
    base.update(over)
    return base


def _reason(data=None, **kw):  # type: ignore[no-untyped-def]
    if isinstance(data, dict):
        return data
    if "file_path" in kw:
        params = {"file_path": kw["file_path"]}
        if "start_line" in kw:
            params["start_line"] = kw["start_line"]
        if "end_line" in kw:
            params["end_line"] = kw["end_line"]
        return {"next_action": "GET_CODE", "action_parameters": params}
    if "symbol" in kw:
        return {
            "next_action": "FIND_SYMBOL",
            "action_parameters": {"symbol_name": kw["symbol"]},
        }
    return {"next_action": kw.get("next_action", "SEARCH_LOGS"), "action_parameters": {}}


class TestFocusExtraction:
    def test_file_focus_from_get_code(self) -> None:
        focus = focus_from_reason_data(
            _reason(file_path="tenant/svc/a.py", start_line=10, end_line=20)
        )
        assert focus is not None
        assert focus.surface == "tenant/svc/a.py"
        assert focus.surface_kind == "file"
        assert (focus.line_start, focus.line_end) == (10, 20)

    def test_symbol_focus_from_find_symbol(self) -> None:
        focus = focus_from_reason_data(_reason(symbol="normalize"))
        assert focus is not None
        assert focus.surface == "normalize"
        assert focus.surface_kind == "symbol"

    def test_no_focus_without_actionable_params(self) -> None:
        assert focus_from_reason_data(_reason({"next_action": "SEARCH_LOGS"})) is None

    def test_garbage_reason_data_returns_none(self) -> None:
        assert focus_from_reason_data(None) is None
        assert focus_from_reason_data({"claims": [None]}) is None
        assert focus_from_reason_data("junk") is None  # type: ignore[arg-type]

    def test_line_normalization(self) -> None:
        focus = focus_from_reason_data(
            {"next_action": "GET_CODE", "action_parameters": {"file_path": "a.py", "start_line": 0, "end_line": "x"}}
        )
        assert focus is not None
        assert focus.line_start is None
        assert focus.line_end is None

    def test_any_action_with_file_path_yields_file_focus(self) -> None:
        focus = focus_from_reason_data(
            {"next_action": "SEARCH_CODE", "action_parameters": {"file_path": "svc/b.py"}}
        )
        assert focus is not None
        assert focus.surface == "svc/b.py"
        assert focus.surface_kind == "file"


class TestShiftDetection:
    def test_first_focus_is_no_shift(self) -> None:
        curr = FocusPoint.model_validate(_prev())
        assert detect_shift(None, curr) is FocusShift.NONE

    def test_same_surface_same_window_is_none(self) -> None:
        point = FocusPoint.model_validate(_prev())
        assert detect_shift(point, FocusPoint.model_validate(_prev())) is FocusShift.NONE

    def test_changed_surface_is_pivot(self) -> None:
        prev = FocusPoint.model_validate(_prev())
        curr = FocusPoint.model_validate(_prev(surface="tenant/svc/new.py"))
        assert detect_shift(prev, curr) is FocusShift.PIVOTED

    def test_same_surface_new_window_is_refined(self) -> None:
        prev = FocusPoint.model_validate(_prev())
        curr = FocusPoint.model_validate(_prev(line_end=20))
        assert detect_shift(prev, curr) is FocusShift.REFINED


class TestLivenessGate:
    def test_pay_off_boundary(self) -> None:
        assert pivot_can_pay_off(10) is True
        assert pivot_can_pay_off(2) is True
        assert pivot_can_pay_off(1) is False
        assert pivot_can_pay_off(0) is False
        assert pivot_can_pay_off(-1) is False
        assert pivot_can_pay_off("junk") is False
        assert pivot_can_pay_off(True) is False
        assert pivot_can_pay_off(None) is False


class TestPivotForcing:
    def test_first_turn_never_forces(self) -> None:
        assert _should_force_pivot_recollect(None, _reason(file_path="tenant/svc/a.py"), 10, []) is None

    def test_no_override_without_shift(self) -> None:
        reason = _reason(file_path="tenant/svc/a.py", start_line=1, end_line=50)
        assert _should_force_pivot_recollect(_prev(), reason, 10, []) is None

    def test_pivot_overrides_get_code_with_lines(self) -> None:
        override = _should_force_pivot_recollect(
            _prev(), _reason(file_path="tenant/svc/new.py", start_line=10, end_line=20), 10, []
        )
        assert override is not None
        assert override["next_action"] == "GET_CODE"
        assert override["action_parameters"]["file_path"] == "tenant/svc/new.py"
        assert override["action_parameters"]["start_line"] == 10
        assert override["action_parameters"]["end_line"] == 20
        assert override["forced_pivot_recollect"] is True

    def test_symbol_pivot_overrides_find_symbol(self) -> None:
        override = _should_force_pivot_recollect(_prev(), _reason(symbol="normalize"), 10, [])
        assert override is not None
        assert override["next_action"] == "FIND_SYMBOL"
        assert override["action_parameters"] == {"symbol_name": "normalize", "revision": "HEAD"}

    def test_no_override_when_turns_exhausted(self) -> None:
        assert (
            _should_force_pivot_recollect(_prev(), _reason(file_path="tenant/svc/new.py"), 1, [])
            is None
        )
        assert (
            _should_force_pivot_recollect(_prev(), _reason(file_path="tenant/svc/new.py"), 0, [])
            is None
        )

    def test_recent_surface_damps_refire(self) -> None:
        assert (
            _should_force_pivot_recollect(
                _prev(), _reason(file_path="tenant/svc/new.py"), 10, ["tenant/svc/new.py"]
            )
            is None
        )

    def test_refined_does_not_force(self) -> None:
        reason = _reason(file_path="tenant/svc/a.py", start_line=1, end_line=20)
        assert _should_force_pivot_recollect(_prev(), reason, 10, []) is None

    def test_junk_inputs_never_raise(self) -> None:
        assert _should_force_pivot_recollect("junk", None, "junk", "junk") is None  # type: ignore[arg-type]
        assert _should_force_pivot_recollect({"surface": 1}, {"claims": [None]}, -1, None) is None
