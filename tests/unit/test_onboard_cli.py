"""Unit tests for the onboard thin CLI (fake transport, no network)."""

from __future__ import annotations

import json
from typing import Any

from investigation_agent_platform.cli.onboard import build_parser, main


class FakeTransport:
    def __init__(self, status: int = 200, body: Any = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.status = status
        self.body = body if body is not None else {"ok": True}

    def request(
        self, method: str, url: str, *, headers: dict[str, str], body: Any | None
    ) -> tuple[int, dict[str, Any]]:
        self.calls.append({"method": method, "url": url, "headers": headers, "body": body})
        assert "X-Tenant-ID" in headers
        return self.status, self.body


def test_status_uses_tenant_header_and_json_flag(capsys: Any) -> None:
    transport = FakeTransport(body={"state": "QUEUED"})
    code = main(
        ["--tenant", "tenant-a", "--json", "status", "--request-id", "r1"],
        transport=transport,
    )
    assert code == 0
    assert transport.calls[0]["method"] == "GET"
    assert "/tenants/tenant-a/onboarding/requests/r1" in transport.calls[0]["url"]
    out = json.loads(capsys.readouterr().out)
    assert out["body"]["state"] == "QUEUED"


def test_apply_posts_intake_and_reports_errors(capsys: Any, tmp_path: Any) -> None:
    artifact = tmp_path / "intake.json"
    artifact.write_text(json.dumps({"intake": {"application_id": "a", "environment": "e"}}))
    transport = FakeTransport(status=202, body={"dispatch_state": "QUEUED"})
    code = main(
        ["--tenant", "t", "--idempotency-key", "k", "apply", str(artifact)],
        transport=transport,
    )
    assert code == 0
    assert transport.calls[0]["method"] == "POST"
    assert transport.calls[0]["headers"]["X-Idempotency-Key"] == "k"

    failing = FakeTransport(
        status=409, body={"error": {"code": "STALE_REVISION", "message": "stale"}}
    )
    code = main(
        ["--tenant", "t", "--idempotency-key", "k", "apply", str(artifact)],
        transport=failing,
    )
    assert code == 1
    assert "STALE_REVISION" in capsys.readouterr().out


def test_capture_validates_offline(tmp_path: Any) -> None:
    source = tmp_path / "in.yaml"
    source.write_text("application_id: app-a\nenvironment: test\n")
    out = tmp_path / "out.json"
    code = main(["--tenant", "t", "capture", str(source), "--output", str(out)])
    assert code == 0
    data = json.loads(out.read_text())
    assert data["intake"]["application_id"] == "app-a"


def test_help_lists_commands() -> None:
    parser = build_parser()
    actions = {action.dest for action in parser._actions}
    assert "command" in actions


def test_capture_rejects_oversize_artifact(tmp_path: Any) -> None:
    from investigation_agent_platform.cli.onboard import MAX_ARTIFACT_BYTES

    big = tmp_path / "big.json"
    big.write_text("x" * (MAX_ARTIFACT_BYTES + 1))
    code = main(["--tenant", "t", "capture", str(big), "--output", str(tmp_path / "o.json")])
    assert code != 0


def test_cleanup_plan_and_approve(capsys: Any) -> None:
    transport = FakeTransport(body={"manifest_id": "m1", "state": "APPROVAL_PENDING"})
    code = main(
        [
            "--tenant",
            "t",
            "cleanup-plan",
            "--environment",
            "prod",
            "--deployment-id",
            "dep-1",
            "--policy-version",
            "p1",
            "--requester",
            "req",
            "--backup-id",
            "b1",
            "--expires-at",
            "2026-12-01T00:00:00+00:00",
        ],
        transport=transport,
    )
    assert code == 0
    assert ":deletion-request" in transport.calls[0]["url"]

    approver = FakeTransport(body={"consumed": True})
    code = main(
        [
            "--tenant",
            "t",
            "cleanup-approve",
            "--manifest-id",
            "m1",
            "--approver",
            "a1",
            "--token-value",
            "tok",
        ],
        transport=approver,
    )
    assert code == 0
    assert ":approve" in approver.calls[0]["url"]
