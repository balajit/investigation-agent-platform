"""B2 kernel wiring tests: flag behavior, shadow never blocks, enforcing denies."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from investigation_agent_platform.application.proof import kernel_conclude_wiring as wiring
from investigation_agent_platform.domain.proof.obligations import DecisionPolicy

REV = "ab" * 20


def _policy(**over: Any) -> DecisionPolicy:
    base: dict[str, Any] = {
        "policy_id": "policy:t:unit:v1",
        "repositories": ("tenant-repo",),
        "approved_mapping_provenance_refs": (),
    }
    base.update(over)
    return DecisionPolicy(**base)


def _row(symbol: str = "handle", revision: str = REV, **over: Any) -> Any:
    window = over.pop("window_text", "def handle(event):\n    return route(event)")
    fact = {
        "status": "RESOLVED",
        "subject_identity": symbol,
        "revision": revision,
        "declaration_location": {"file_path": "src/handler.py", "line_start": 10, "line_end": 14},
    }
    fact.update(over.pop("fact_over", {}))
    row = SimpleNamespace(
        evidence_id=over.pop("evidence_id", uuid4()),
        evidence_type=SimpleNamespace(value="SOURCE_CODE"),
        attributes={"code_fact_resolution_v2": fact, **over.pop("attributes", {})},
        content_snippet=window,
        **over,
    )
    return row


def _ctx(rows: list[Any]) -> Any:
    async def find_by_investigation_id(tenant_id: str, inv_id: object) -> list[Any]:
        return list(rows)

    return SimpleNamespace(evidence_repo=SimpleNamespace(find_by_investigation_id=find_by_investigation_id))


class _Env:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, **env: str) -> None:
        self._monkeypatch = monkeypatch
        for key in (
            "IAP_KERNEL_WIRING",
            "IAP_KERNEL_POLICY_REPOSITORIES",
            "IAP_KERNEL_POLICY_AUTHORITIES",
        ):
            monkeypatch.delenv(key, raising=False)
        for key, value in env.items():
            monkeypatch.setenv(key, value)


def test_flag_defaults_off(monkeypatch: pytest.MonkeyPatch) -> None:
    _Env(monkeypatch)
    assert wiring.kernel_wiring_mode() == "off"


def test_flag_unknown_fails_closed_to_off(monkeypatch: pytest.MonkeyPatch) -> None:
    _Env(monkeypatch, IAP_KERNEL_WIRING="yes-please")
    assert wiring.kernel_wiring_mode() == "off"
    _Env(monkeypatch, IAP_KERNEL_WIRING="shadow")
    assert wiring.kernel_wiring_mode() == "shadow"
    _Env(monkeypatch, IAP_KERNEL_WIRING="enforcing")
    assert wiring.kernel_wiring_mode() == "enforcing"


def test_off_mode_never_touches_repos(monkeypatch: pytest.MonkeyPatch) -> None:
    _Env(monkeypatch)

    async def _boom(tenant_id: str, inv_id: object) -> list[Any]:
        raise AssertionError("must not be called when off")

    ctx = SimpleNamespace(evidence_repo=SimpleNamespace(find_by_investigation_id=_boom))
    allowed, blockers = _run(wiring.kernel_gate_for_conclusion("t", uuid4(), ctx))
    assert (allowed, blockers) == (True, [])


def _run(coro: Any) -> Any:
    import asyncio

    return asyncio.new_event_loop().run_until_complete(coro)


def test_shadow_evaluates_but_never_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    _Env(
        monkeypatch,
        IAP_KERNEL_WIRING="shadow",
        IAP_KERNEL_POLICY_REPOSITORIES="tenant-repo",
    )
    # Unpinned revision: checker-VALID inputs, OPEN obligation -> DENY verdict,
    # but shadow must still allow.
    row = _row()
    object.__setattr__(row, "attributes", {"code_fact_resolution_v2": {
        "status": "RESOLVED",
        "subject_identity": "handle",
        "revision": REV,
        "declaration_location": {"file_path": "src/handler.py", "line_start": 10, "line_end": 5},
    }})
    allowed, blockers = _run(wiring.kernel_gate_for_conclusion("t", uuid4(), _ctx([row])))
    assert allowed is True and blockers == []


def test_shadow_commit_path_reports_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    _Env(
        monkeypatch,
        IAP_KERNEL_WIRING="shadow",
        IAP_KERNEL_POLICY_REPOSITORIES="tenant-repo",
    )
    report = wiring.evaluate_conclusion_kernel("t", uuid4(), [_row()], _policy())
    assert len(report["claims"]) == 1
    assert report["claims"][0]["decision"] == "COMMIT"
    assert report["skips"] == []


def test_enforcing_deny_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    _Env(
        monkeypatch,
        IAP_KERNEL_WIRING="enforcing",
        IAP_KERNEL_POLICY_REPOSITORIES="tenant-repo",
    )
    # Inverted range passes the adapter (ints present) but fails certify's
    # strict range check -> DENY entry -> enforcing blocks.
    row = _row()
    object.__setattr__(row, "attributes", {"code_fact_resolution_v2": {
        "status": "RESOLVED",
        "subject_identity": "handle",
        "revision": REV,
        "declaration_location": {"file_path": "src/handler.py", "line_start": 10, "line_end": 5},
    }})
    allowed, blockers = _run(wiring.kernel_gate_for_conclusion("t", uuid4(), _ctx([row])))
    assert allowed is False
    assert blockers and blockers[0].startswith("kernel-denied:")


def test_enforcing_commit_allows(monkeypatch: pytest.MonkeyPatch) -> None:
    _Env(
        monkeypatch,
        IAP_KERNEL_WIRING="enforcing",
        IAP_KERNEL_POLICY_REPOSITORIES="tenant-repo",
    )
    allowed, blockers = _run(wiring.kernel_gate_for_conclusion("t", uuid4(), _ctx([_row()])))
    assert (allowed, blockers) == (True, [])


def test_enforcing_no_policy_denies_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    _Env(monkeypatch, IAP_KERNEL_WIRING="enforcing")
    allowed, blockers = _run(wiring.kernel_gate_for_conclusion("t", uuid4(), _ctx([_row()])))
    assert allowed is False
    assert blockers == ["kernel-no-policy"]


def test_enforcing_skips_only_allows(monkeypatch: pytest.MonkeyPatch) -> None:
    _Env(
        monkeypatch,
        IAP_KERNEL_WIRING="enforcing",
        IAP_KERNEL_POLICY_REPOSITORIES="tenant-repo",
    )
    row = SimpleNamespace(
        evidence_id=uuid4(),
        evidence_type=SimpleNamespace(value="RUNTIME_LOG"),
        attributes={},
        content_snippet="log line",
    )
    allowed, blockers = _run(wiring.kernel_gate_for_conclusion("t", uuid4(), _ctx([row])))
    assert (allowed, blockers) == (True, [])


def test_adapter_skip_reasons() -> None:
    rows = [
        _row(symbol=""),
        _row(revision=""),
        _row(window_text="unrelated text"),
    ]
    other = SimpleNamespace(
        evidence_id=uuid4(),
        evidence_type=SimpleNamespace(value="RUNTIME_LOG"),
        attributes={},
        content_snippet="x",
    )
    mappable, skips = wiring.mappable_definition_rows([*rows, other])
    assert mappable == []
    assert sorted(s["reason"] for s in skips) == [
        "missing-revision",
        "missing-symbol-or-file",
        "missing-window-text",
        "unmapped-kind",
    ]


def test_gate_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _Env(monkeypatch, IAP_KERNEL_WIRING="enforcing")

    async def _boom(tenant_id: str, inv_id: object) -> list[Any]:
        raise RuntimeError("repo down")

    ctx = SimpleNamespace(evidence_repo=SimpleNamespace(find_by_investigation_id=_boom))
    allowed, blockers = _run(wiring.kernel_gate_for_conclusion("t", uuid4(), ctx))
    assert allowed is False and blockers
