# tests/unit/test_w6_domain_hygiene.py
"""W6: dead surface, required ids, strict JSON, version routing, stale prose (D29-D32)."""

import ast
import inspect
import pathlib
import uuid

import pytest
from pydantic import ValidationError

from investigation_agent_platform.application.worker.workflows import forcing
from investigation_agent_platform.application.worker.workflows.forcing import (
    WorkOrderPolicy,
    _should_force_order_requery,
)
from investigation_agent_platform.domain.collection.work_orders import (
    KNOWN_WORK_ORDER_VERSIONS,
    WORK_ORDER_VERSION_CURRENT,
    FailureCharacter,
    WorkOrder,
    WorkOrderKind,
)


def test_no_empty_import_blocks() -> None:
    path = pathlib.Path("src/investigation_agent_platform/application/worker/workflows/forcing.py")
    tree = ast.parse(path.read_text(encoding="utf-8"))
    empties = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.With) and all(isinstance(b, ast.Pass) for b in n.body)
    ]
    assert empties == []


def test_work_order_needs_explicit_id() -> None:
    with pytest.raises(ValidationError):
        WorkOrder(
            kind=WorkOrderKind.REQUERY, action_type="GET_CODE",
            action_parameters={"file_path": "svc/a.py"},
            addresses=FailureCharacter(triage_route="BUG", claim_kind="u"),
            cost_estimate=1,
        )


def test_nan_and_infinity_are_not_json_safe() -> None:
    for bad in ({"q": float("nan")}, {"q": float("inf")}, {"q": float("-inf")}, {"n": {"q": float("nan")}}):
        with pytest.raises(ValidationError):
            WorkOrder(
                work_order_id=uuid.uuid4(), kind=WorkOrderKind.REQUERY, action_type="SEARCH_CODE",
                action_parameters=bad,
                addresses=FailureCharacter(triage_route="BUG", claim_kind="u"),
                cost_estimate=1,
            )


def test_valid_params_round_trip_unchanged() -> None:

    params = {"file_path": "svc/a.py", "start_line": 1, "query": "x", "n": 2, "flag": True, "nothing": None}
    order = WorkOrder(
        work_order_id=uuid.uuid4(), kind=WorkOrderKind.REQUERY, action_type="SEARCH_CODE",
        action_parameters=dict(params),
        addresses=FailureCharacter(triage_route="BUG", claim_kind="u"),
        cost_estimate=1,
    )
    assert order.model_dump(mode="json")["action_parameters"] == params


def test_unknown_order_version_is_invalid() -> None:
    carrier = {
        "kind": "work_order", "order_kind": "REQUERY", "iteration": 1, "turn_issued": 1,
        "claim_ref": "c", "query": "q", "scope": "", "max_attempts": 3, "consumed": False,
        "order": {
            "work_order_id": "00000000-0000-0000-0000-000000000001", "kind": "REQUERY",
            "version": "workorder-v9",
            "action_type": "SEARCH_CODE", "action_parameters": {"query": "q"},
            "addresses": {"triage_route": "BUG", "claim_kind": "u"},
            "cost_estimate": 1, "seed_source": "s", "status": "PENDING",
        },
    }
    out = _should_force_order_requery([carrier], [], WorkOrderPolicy(hardened=True, iteration=9))
    assert out is not None
    assert out["mark_consumed"] == "invalid_order" and out["detail"] == "unknown_version"
    assert "action_type" not in out


def test_current_version_dispatches() -> None:
    from investigation_agent_platform.application.worker.workflows.forcing import (
        _carry_requery_order,
    )

    carrier = _carry_requery_order(
        {"ledger_requery": {"query": "q", "action_type": "SEARCH_CODE",
                            "action_parameters": {"query": "q"}, "claim_ref": "c"}},
        1,
    )
    from investigation_agent_platform.domain.collection.work_orders import WorkOrder as _WO

    assert carrier is not None
    assert _WO.model_validate(carrier["order"]).version == WORK_ORDER_VERSION_CURRENT
    assert WORK_ORDER_VERSION_CURRENT in KNOWN_WORK_ORDER_VERSIONS


def test_max_attempts_mirror_agrees() -> None:
    from investigation_agent_platform.application.worker.workflows.forcing import (
        _carry_requery_order,
    )

    for attempts, expected in ((1, 1), (3, 3), (7, 7), ("junk", 3), (0, 1)):
        carrier = _carry_requery_order(
            {"ledger_requery": {"query": "q", "action_type": "SEARCH_CODE",
                                "action_parameters": {"query": "q"}, "claim_ref": "c",
                                "max_attempts": attempts}},
            1,
        )
        assert carrier is not None
        assert carrier["max_attempts"] == expected, attempts
        assert carrier["order"]["max_attempts"] == expected, attempts


def test_ttl_is_live() -> None:
    src = inspect.getsource(forcing._should_force_order_requery)
    assert "order.ttl_rounds" in src
    src = inspect.getsource(forcing._pick_gap_order)
    assert "order.ttl_rounds" in src


def test_frontier_library_fenced_from_workflow_code() -> None:
    roots = list((pathlib.Path("src/investigation_agent_platform/application/worker")).rglob("*.py"))
    assert roots
    offenders = []
    for path in roots:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("investigation_agent_platform.domain.collection.frontier"):
                offenders.append(f"{path.name}:{node.lineno}")
    assert offenders == []


def test_stale_prose_is_gone() -> None:
    allow_files = {"retrieval_ledger.py"}  # "replaces the derived guess" is live prose, not legacy
    hits = []
    for path in list(pathlib.Path("src/investigation_agent_platform/domain/collection").rglob("*.py")) + [
        pathlib.Path("src/investigation_agent_platform/application/worker/workflows/forcing.py"),
    ]:
        text = path.read_text(encoding="utf-8")
        if path.name in allow_files:
            continue
        for needle in ("chunk 2b", "chunk 2a", "replaces the", "2b step"):
            if needle in text:
                hits.append(f"{path.name}: {needle}")
    assert hits == []


def test_negative_control_restore_uuid_default() -> None:
    src = inspect.getsource(WorkOrder)
    assert "default_factory=uuid4" not in src


def test_negative_control_allow_nan() -> None:
    import investigation_agent_platform.domain.collection.work_orders as _wo

    src = inspect.getsource(_wo._is_json_safe)
    assert "allow_nan=False" in src
