# tests/unit/test_h16_claim_identity.py
"""H16: canonical claim-target identity, overload signature, stamp routing (LV_H)."""

import pytest

from investigation_agent_platform.application.evidence.retrieval_ledger import target_group_key
from investigation_agent_platform.application.investigation.claim_identity import (
    canonical_target,
    identity_key,
    stamp_identity,
)


def _key(**target):  # type: ignore[no-untyped-def]
    return identity_key(canonical_target(target))


def test_whitespace_variants_collapse() -> None:
    assert _key(symbol="OrderServiceHandler.validateOrder") == _key(symbol="  OrderServiceHandler.validateOrder  ")
    assert _key(symbol="a  b") == _key(symbol="a b")


def test_path_variants_collapse() -> None:
    base = _key(file_path="src/Order.java")
    assert _key(file_path="./src/Order.java") == base
    assert _key(file_path="src\\Order.java") == base
    assert _key(file_path="src//Order.java") == base
    assert _key(file_path="src/Order.java/") == base


def test_symbol_case_never_folds() -> None:
    assert _key(symbol="getID") != _key(symbol="getId")


def test_path_case_does_not_fold() -> None:
    assert _key(file_path="src/Order.java") != _key(file_path="src/order.java")


def test_range_is_not_identity_with_symbol() -> None:
    assert (
        _key(symbol="m", file_path="svc/a.py", line_start=10)
        == _key(symbol="m", file_path="svc/a.py", line_start=40)
    )


def test_range_is_identity_without_symbol() -> None:
    assert (
        _key(file_path="svc/a.py", line_start=10)
        != _key(file_path="svc/a.py", line_start=40)
    )


def test_overloads_split_by_signature() -> None:
    assert _key(symbol="m", signature="process(Order)") != _key(symbol="m", signature="process(Order,int)")


def test_signature_whitespace_is_not_identity() -> None:
    assert _key(symbol="m", signature="process(Order, int)") == _key(symbol="m", signature="process(Order,int)")


def test_no_signature_keeps_one_group() -> None:
    assert _key(symbol="m") == _key(symbol="m")


def test_stamped_key_wins_unstamped_uses_legacy() -> None:
    key, rest = target_group_key({"symbol": "Foo.a", "identity_key": "deadbeefcafe0001"})
    assert key == "deadbeefcafe0001"
    assert "identity_key" not in rest
    legacy, _ = target_group_key({"symbol": "OrderServiceHandler.validateOrder", "file_path": "src/Order.java"})
    assert legacy == "a16d762dcfe96267"


def test_malformed_input() -> None:
    assert identity_key(canonical_target(None)) == "NO_TARGET"
    assert identity_key(canonical_target({"file_path": 123})) != "NO_TARGET"
    # unusable values never raise; lists stringify, Nones drop
    assert canonical_target({"symbol": ["x"]}) == {"symbol": "['x']"}
    assert canonical_target({"symbol": None}) == {}
    assert stamp_identity(None) == {}
    big = "x" * (10 * 1024 * 1024)
    assert isinstance(identity_key(canonical_target({"symbol": big})), str)


def test_gap_id_stability_end_to_end() -> None:
    from types import SimpleNamespace

    from investigation_agent_platform.application.investigation.verification import (
        build_claim_gaps,
    )

    def gaps_for(target):  # type: ignore[no-untyped-def]
        # the FORMULATE site stamps under LV_H; gap ids follow the stamp
        asm = SimpleNamespace(
            claim_kind="DEFINITION", claim_target=stamp_identity(target),
            evidence_id=None, search_query=None,
        )
        hyp = SimpleNamespace(statement="s defines x", assessments=[asm])
        return build_claim_gaps(hyp, set(), None)["gaps"]

    g1 = gaps_for({"symbol": "s", "file_path": "src/A.java", "line_start": 1, "line_end": 5})
    g2 = gaps_for({"symbol": "s", "file_path": "./src/A.java", "line_start": 1, "line_end": 5})
    assert [g["gap_id"] for g in g1] == [g["gap_id"] for g in g2] != []


@pytest.mark.asyncio
async def test_formulate_stamps_only_under_lv_h() -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from uuid import uuid4

    from test_claim_binding_phase2 import _formulate_ctx

    from investigation_agent_platform.application.worker.activities import execute_action_activity
    from investigation_agent_platform.application.worker.activities.inputs import ExecuteActionInput

    async def run(claims, loop_version):  # type: ignore[no-untyped-def]
        saved: list = []
        ea, eb = uuid4(), uuid4()

        async def _save(tenant_id, hyp, investigation_id, expected_version=0):  # type: ignore[no-untyped-def]
            saved.append(hyp)

        _formulate_ctx(
            [SimpleNamespace(evidence_id=ea), SimpleNamespace(evidence_id=eb)],
            SimpleNamespace(find_by_investigation_id=AsyncMock(return_value=[]), save=_save),
        )
        action_input = ExecuteActionInput(
            tenant_id="tenant-a", investigation_id=str(uuid4()),
            action="FORMULATE_HYPOTHESIS", parameters={},
            loop_version=loop_version,
        )
        action_input.hypotheses = ["Foo defines a"]
        action_input.claims = claims(ea, eb)
        await execute_action_activity(action_input)
        return saved

    def claims(ea, eb):  # type: ignore[no-untyped-def]
        return [
            {"statement": "Foo defines a", "claim_kind": "DEFINITION", "evidence_ids": [str(ea)],
             "target": {"symbol": "Foo.a", "file_path": "  src/Foo.java  "}},
            {"statement": "Foo defines a", "claim_kind": "DEFINITION", "evidence_ids": [str(eb)],
             "target": {"symbol": "Foo.a", "file_path": "src/Foo.java"}},
        ]

    saved_h = await run(claims, "lv10")
    assert len(saved_h) == 1
    groups = {(a.claim_kind, a.claim_target.identity_key) for a in saved_h[0].assessments if a.claim_target}
    assert len(groups) == 1
    assert saved_h[0].assessments[0].claim_target.file_path == "src/Foo.java"

    saved_7 = await run(claims, "lv7")
    assert len(saved_7) == 1
    keys = {
        a.claim_target.file_path for a in saved_7[0].assessments if a.claim_target
    }
    assert keys == {"src/Foo.java", "  src/Foo.java  "} or len(saved_7[0].assessments) == 2


@pytest.mark.asyncio
async def test_h2_no_overwrite_regression_under_lv_h() -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from uuid import uuid4

    from test_claim_binding_phase2 import _formulate_ctx

    from investigation_agent_platform.application.worker.activities import execute_action_activity
    from investigation_agent_platform.application.worker.activities.inputs import ExecuteActionInput

    saved: list = []
    ea, eb = uuid4(), uuid4()

    async def _save(tenant_id, hyp, investigation_id, expected_version=0):  # type: ignore[no-untyped-def]
        saved.append(hyp)

    _formulate_ctx(
        [SimpleNamespace(evidence_id=ea), SimpleNamespace(evidence_id=eb)],
        SimpleNamespace(find_by_investigation_id=AsyncMock(return_value=[]), save=_save),
    )
    action_input = ExecuteActionInput(
        tenant_id="tenant-a", investigation_id=str(uuid4()),
        action="FORMULATE_HYPOTHESIS", parameters={}, loop_version="lv10",
    )
    action_input.hypotheses = ["Foo defines a and b"]
    action_input.claims = [
        {"statement": "Foo defines a and b", "claim_kind": "DEFINITION", "evidence_ids": [str(ea)],
         "target": {"symbol": "Foo.a"}},
        {"statement": "Foo defines a and b", "claim_kind": "DEFINITION", "evidence_ids": [str(eb)],
         "target": {"symbol": "Foo.b"}},
    ]
    await execute_action_activity(action_input)
    symbols = {str(a.claim_target.symbol) for a in saved[0].assessments if a.claim_target}
    assert symbols == {"Foo.a", "Foo.b"}
