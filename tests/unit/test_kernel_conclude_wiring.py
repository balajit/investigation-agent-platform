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


# ---------------------------------------------------------------------------
# R4b: trusted context producer + policy source
# ---------------------------------------------------------------------------


def test_select_policy_from_operator_env(monkeypatch: pytest.MonkeyPatch) -> None:
    _Env(monkeypatch)
    assert wiring.select_policy("t") is None
    _Env(
        monkeypatch,
        IAP_KERNEL_POLICY_REPOSITORIES="repo-a,repo-b, repo-a",
        IAP_KERNEL_POLICY_AUTHORITIES="auth-1",
    )
    policy = wiring.select_policy("t")
    assert policy is not None
    assert policy.repositories == ("repo-a", "repo-b")
    assert policy.approved_mapping_provenance_refs == ("auth-1",)
    assert policy.policy_id == "policy:t:conclude:v1"


def test_assemble_trusted_context_round_trip() -> None:
    from investigation_agent_platform.domain.proof.obligations import (
        SnapshotError,
        assemble_trusted_context_bytes,
        encode_trusted_context_bytes,
    )

    payload = {
        "rule_bundle_version": "rules/v2",
        "model_encoding": {},
        "reconciliation_state": "unreconciled",
        "known_normalization_ops": [],
        "derivation_deps": {},
        "repository_bindings": [
            {"evidence_id": "r1", "content_digest": "sha256:" + "ab" * 32, "repository_id": "repo-a"}
        ],
        "expected_context_id": "ctx-1",
        "reconciliation_adjudicated": True,
        "policy_eligible": True,
        "policy_reason": "",
    }
    assert assemble_trusted_context_bytes(**payload) == encode_trusted_context_bytes(payload)
    with pytest.raises(SnapshotError):
        assemble_trusted_context_bytes(**{**payload, "repository_bindings": [
            {"evidence_id": "r1", "content_digest": "bad", "repository_id": "repo-a"}
        ][:1]})
    dupes = [
        {"evidence_id": "r1", "content_digest": "sha256:" + "ab" * 32, "repository_id": "repo-a"},
        {"evidence_id": "r1", "content_digest": "sha256:" + "ab" * 32, "repository_id": "repo-a"},
    ]
    with pytest.raises(SnapshotError):
        assemble_trusted_context_bytes(**{**payload, "repository_bindings": dupes})


def test_assemble_trusted_context_boundary_rejects_malformed() -> None:
    from investigation_agent_platform.domain.proof.obligations import (
        SnapshotError,
        assemble_trusted_context_bytes,
    )

    good_binding = {"evidence_id": "r1", "content_digest": "sha256:" + "ab" * 32, "repository_id": "repo-a"}
    base = {
        "rule_bundle_version": "rules/v2",
        "model_encoding": {},
        "reconciliation_state": "unreconciled",
        "known_normalization_ops": [],
        "derivation_deps": {},
        "repository_bindings": [dict(good_binding)],
        "expected_context_id": "ctx-1",
        "reconciliation_adjudicated": True,
        "policy_eligible": True,
        "policy_reason": "",
    }
    for bad in (
        {"evidence_id": "", "content_digest": good_binding["content_digest"], "repository_id": "repo-a"},
        {"evidence_id": "r1", "content_digest": "", "repository_id": "repo-a"},
        {"evidence_id": "r1", "content_digest": good_binding["content_digest"], "repository_id": ""},
        {"evidence_id": "r1", "content_digest": "sha256:" + "zz" * 32, "repository_id": "repo-a"},
        {"evidence_id": "r1", "content_digest": "md5:abc", "repository_id": "repo-a"},
    ):
        with pytest.raises(SnapshotError):
            assemble_trusted_context_bytes(**{**base, "repository_bindings": [bad]})
    with pytest.raises(SnapshotError):
        assemble_trusted_context_bytes(**{**base, "repository_bindings": ["not-a-mapping"]})  # type: ignore[list-item]
    with pytest.raises(SnapshotError):
        assemble_trusted_context_bytes(**{**base, "model_encoding": ["not", "a", "mapping"]})  # type: ignore[dict-item]
    with pytest.raises(SnapshotError):
        assemble_trusted_context_bytes(**{**base, "known_normalization_ops": "op"})  # type: ignore[dict-item]
    with pytest.raises(SnapshotError):
        assemble_trusted_context_bytes(**{**base, "derivation_deps": ["not", "a", "mapping"]})  # type: ignore[dict-item]


# ---------------------------------------------------------------------------
# R4c: writer routing single-first
# ---------------------------------------------------------------------------


def test_writer_routing_defaults_single_only(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    from investigation_agent_platform.application.proof.publication_commit_service import (
        ProofPublicationService,
    )

    _Env(monkeypatch)
    svc = ProofPublicationService(None)
    denied = asyncio.new_event_loop().run_until_complete(
        svc.commit("t", uuid4(), None, [], mode="all")
    )
    assert denied.committed is False
    assert denied.reason == "MODE_NOT_ENABLED:all"
    _Env(monkeypatch, IAP_PUBLISH_WRITERS="single,all,any")
    denied2 = asyncio.new_event_loop().run_until_complete(
        svc.commit("t", uuid4(), None, [], mode="all")
    )
    # Passes routing; denies later on empty routes (no DB touched either way).
    assert denied2.reason == "NO_ROUTES"


def test_writer_routing_single_needs_exactly_one_route(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    from investigation_agent_platform.application.proof.publication_commit_service import (
        ProofPublicationService,
        RouteInput,
    )

    _Env(monkeypatch)
    svc = ProofPublicationService(None)
    run = asyncio.new_event_loop().run_until_complete
    stub = RouteInput(proposition={}, manifest=None)
    assert run(svc.commit("t", uuid4(), None, [], mode="single")).reason == "NO_ROUTES"
    two = run(svc.commit("t", uuid4(), None, [stub, stub], mode="single"))
    assert two.committed is False
    assert two.reason == "MODE_SINGLE_NEEDS_ONE_ROUTE"


def test_writer_routing_env_parsing_tolerant(monkeypatch: pytest.MonkeyPatch) -> None:
    from investigation_agent_platform.application.proof.publication_commit_service import (
        _allowed_modes,
    )

    _Env(monkeypatch)
    assert _allowed_modes() == frozenset({"single"})
    _Env(monkeypatch, IAP_PUBLISH_WRITERS=" Single ,, ALL ")
    assert _allowed_modes() == frozenset({"single", "all"})
    _Env(monkeypatch, IAP_PUBLISH_WRITERS="   ")
    assert _allowed_modes() == frozenset({"single"})


# ---------------------------------------------------------------------------
# R4d: publish seam (gated, unwired)
# ---------------------------------------------------------------------------


def _committed_report(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    _Env(
        monkeypatch,
        IAP_KERNEL_WIRING="shadow",
        IAP_KERNEL_POLICY_REPOSITORIES="tenant-repo",
    )
    return wiring.evaluate_conclusion_kernel("t", uuid4(), [_row()], _policy())


def test_publish_seam_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    _Env(monkeypatch)
    calls: list[str] = []

    class _Svc:
        async def commit(self, *args: Any, **kwargs: Any) -> Any:
            calls.append("commit")
            raise AssertionError("must not be called when disabled")

    report = _committed_report(monkeypatch)
    assert report["claims"] and report["claims"][0]["decision"] == "COMMIT"
    import asyncio

    out = asyncio.new_event_loop().run_until_complete(
        wiring.maybe_publish_committed(_Svc(), "t", uuid4(), None, None, report, _policy())
    )
    assert out == {"published": False, "reason": "publish-disabled"}
    assert calls == []


def test_publish_seam_calls_service_for_commits(monkeypatch: pytest.MonkeyPatch) -> None:
    _Env(monkeypatch, IAP_KERNEL_PUBLISH="1")
    calls: list[dict[str, Any]] = []

    class _Svc:
        async def commit(self, *args: Any, **kwargs: Any) -> Any:
            from types import SimpleNamespace

            calls.append(
                {
                    "mode": kwargs.get("mode", args[4] if len(args) > 4 else None),
                    "routes": kwargs.get("routes", args[3] if len(args) > 3 else None),
                }
            )
            return SimpleNamespace(committed=True, reason="COMMITTED")

    report = _committed_report(monkeypatch)
    import asyncio

    out = asyncio.new_event_loop().run_until_complete(
        wiring.maybe_publish_committed(_Svc(), "t", uuid4(), None, None, report, _policy())
    )
    assert out["published"] is True and out["committed"] is True
    assert calls and calls[0]["mode"] == "single"
    forwarded = calls[0]["routes"]
    assert forwarded is not None and len(forwarded) == 1
    assert forwarded[0].repository_bindings == (
        report["claims"][0]["bundle"]["binding"],
    )


def test_publish_seam_skips_without_commits(monkeypatch: pytest.MonkeyPatch) -> None:
    _Env(monkeypatch, IAP_KERNEL_PUBLISH="1")

    class _Svc:
        async def commit(self, *args: Any, **kwargs: Any) -> Any:
            raise AssertionError("must not be called with no commits")

    import asyncio

    out = asyncio.new_event_loop().run_until_complete(
        wiring.maybe_publish_committed(_Svc(), "t", uuid4(), None, None, {"claims": []}, _policy())
    )
    assert out == {"published": False, "reason": "no-committed-claims"}
