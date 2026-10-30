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


def _mini_java_repo(path: Any, classname: str = "Main") -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "build.gradle").write_text("plugins { id 'java' }\n")
    src = path / "src"
    src.mkdir(exist_ok=True)
    (src / f"{classname}.java").write_text(f"public class {classname} {{}}\n")


def test_generate_profile_single_repo_offline(tmp_path: Any) -> None:
    repo = tmp_path / "svc"
    _mini_java_repo(repo)
    out = tmp_path / "profile.json"
    code = main(
        [
            "--tenant",
            "t",
            "generate-profile",
            "--repo-dir",
            str(repo),
            "--app-name",
            "testapp",
            "--output",
            str(out),
        ]
    )
    assert code == 0
    data = json.loads(out.read_text())
    assert data["profile"]["id"] == "testapp"
    assert data["profile"]["tenant_id"] == "t"


def test_generate_profile_batch_org_dir(tmp_path: Any) -> None:
    org = tmp_path / "org"
    _mini_java_repo(org / "svc-a")
    _mini_java_repo(org / "svc-b")
    outdir = tmp_path / "profiles"
    code = main(
        [
            "--tenant",
            "t",
            "generate-profile",
            "--org-dir",
            str(org),
            "--app-prefix",
            "pfx",
            "--output-dir",
            str(outdir),
        ]
    )
    assert code == 0
    assert {p.name for p in outdir.glob("*.json")} == {"pfx-svc-a.json", "pfx-svc-b.json"}


def test_generate_profile_needs_exactly_one_source(tmp_path: Any) -> None:
    code = main(["--tenant", "t", "generate-profile"])
    assert code == 2


def test_generate_profile_single_dry_run_writes_nothing(tmp_path: Any) -> None:
    repo = tmp_path / "svc"
    _mini_java_repo(repo)
    out = tmp_path / "profile.json"
    code = main(
        [
            "--tenant",
            "t",
            "generate-profile",
            "--repo-dir",
            str(repo),
            "--app-name",
            "testapp",
            "--output",
            str(out),
            "--dry-run",
        ]
    )
    assert code == 0
    assert not out.exists()


def test_generate_profile_single_refuses_overwrite(tmp_path: Any) -> None:
    repo = tmp_path / "svc"
    _mini_java_repo(repo)
    out = tmp_path / "profile.json"
    out.write_text("sentinel")
    code = main(
        [
            "--tenant",
            "t",
            "generate-profile",
            "--repo-dir",
            str(repo),
            "--app-name",
            "testapp",
            "--output",
            str(out),
        ]
    )
    assert code == 2
    assert out.read_text() == "sentinel"


def test_generate_profile_batch_dry_run_writes_nothing(tmp_path: Any) -> None:
    org = tmp_path / "org"
    _mini_java_repo(org / "svc-a")
    _mini_java_repo(org / "svc-b")
    outdir = tmp_path / "profiles"
    code = main(
        [
            "--tenant",
            "t",
            "generate-profile",
            "--org-dir",
            str(org),
            "--app-prefix",
            "pfx",
            "--output-dir",
            str(outdir),
            "--dry-run",
        ]
    )
    assert code == 0
    assert list(outdir.glob("*.json")) == [] if outdir.exists() else True


def test_generate_profile_batch_duplicate_ids_fail_closed(tmp_path: Any) -> None:
    org = tmp_path / "org"
    _mini_java_repo(org / "My App")
    _mini_java_repo(org / "my-app")
    outdir = tmp_path / "profiles"
    code = main(
        [
            "--tenant",
            "t",
            "generate-profile",
            "--org-dir",
            str(org),
            "--app-prefix",
            "pfx",
            "--output-dir",
            str(outdir),
        ]
    )
    assert code == 2
    assert not outdir.exists()


def test_generate_profile_batch_existing_output_skipped(tmp_path: Any) -> None:
    org = tmp_path / "org"
    _mini_java_repo(org / "svc-a")
    _mini_java_repo(org / "svc-b")
    outdir = tmp_path / "profiles"
    outdir.mkdir(parents=True)
    sentinel = outdir / "pfx-svc-a.json"
    sentinel.write_text("sentinel")
    code = main(
        [
            "--tenant",
            "t",
            "generate-profile",
            "--org-dir",
            str(org),
            "--app-prefix",
            "pfx",
            "--output-dir",
            str(outdir),
        ]
    )
    assert code == 1
    assert sentinel.read_text() == "sentinel"
    assert (outdir / "pfx-svc-b.json").exists()


def test_generate_profile_persist_requires_db_uri(tmp_path: Any, monkeypatch: Any) -> None:
    monkeypatch.delenv("IAP_DATABASE_URI", raising=False)
    repo = tmp_path / "svc"
    _mini_java_repo(repo)
    out = tmp_path / "profile.json"
    code = main(
        [
            "--tenant",
            "t",
            "generate-profile",
            "--repo-dir",
            str(repo),
            "--app-name",
            "testapp",
            "--output",
            str(out),
            "--persist",
        ]
    )
    assert code == 2


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


def test_verify_telemetry_missing_dir_fails(tmp_path: Any, capsys: Any) -> None:
    code = main(
        [
            "--tenant",
            "tenant-a",
            "verify-telemetry",
            "--telemetry-dir",
            str(tmp_path / "nope"),
            "--home-service",
            "svc-a",
        ]
    )
    assert code == 1
    assert "[FAIL] config loads" in capsys.readouterr().out


def test_verify_telemetry_missing_dir_json(tmp_path: Any, capsys: Any) -> None:
    code = main(
        [
            "--tenant",
            "tenant-a",
            "--json",
            "verify-telemetry",
            "--telemetry-dir",
            str(tmp_path / "nope"),
            "--home-service",
            "svc-a",
        ]
    )
    assert code == 1
    body = json.loads(capsys.readouterr().out)
    assert body[0]["name"] == "config loads" and body[0]["passed"] is False


def test_verify_telemetry_malformed_file_errors(tmp_path: Any, capsys: Any) -> None:
    tenant_dir = tmp_path / "tenant-a"
    tenant_dir.mkdir()
    (tenant_dir / "telemetry.conf").write_text(
        "[one]\nurl = https://a.example.com\nservices = svc\n\n"
        "[two]\nurl = https://b.example.com\nservices = svc\n"
    )
    code = main(
        [
            "--tenant",
            "tenant-a",
            "verify-telemetry",
            "--telemetry-dir",
            str(tmp_path),
            "--home-service",
            "svc-a",
        ]
    )
    assert code == 2
    assert "exactly one endpoint" in capsys.readouterr().out
