# src/investigation_agent_platform/cli/onboard.py
"""``onboard`` command: capture/apply/attach/status and lifecycle actions."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Protocol

#: Offline capture/attach artifact size bound (Phase 6: bounded request sizes).
MAX_ARTIFACT_BYTES = 1_000_000


class Transport(Protocol):
    """HTTP transport; the default uses urllib, tests inject fakes."""

    def request(
        self, method: str, url: str, *, headers: dict[str, str], body: Any | None
    ) -> tuple[int, dict[str, Any]]:
        """Perform one request; return (status, decoded JSON body)."""
        ...


class UrllibTransport:
    """Production transport over HTTPS/HTTP with bearer + tenant headers."""

    def __init__(self, token: str) -> None:
        self._token = token

    def request(
        self, method: str, url: str, *, headers: dict[str, str], body: Any | None
    ) -> tuple[int, dict[str, Any]]:
        payload = None if body is None else json.dumps(body).encode("utf-8")
        outgoing = dict(headers)
        if self._token:
            outgoing["Authorization"] = f"Bearer {self._token}"
        outgoing.setdefault("Content-Type", "application/json")
        request = urllib.request.Request(url, data=payload, headers=outgoing, method=method)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read().decode("utf-8") or "{}"
                return response.status, json.loads(raw)
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8") or "{}"
            try:
                return exc.code, json.loads(raw)
            except json.JSONDecodeError:
                return exc.code, {"error": {"code": "HTTP_ERROR", "message": raw[:500]}}


def _client(args: argparse.Namespace, transport: Transport | None) -> tuple[str, Transport]:
    endpoint = args.endpoint.rstrip("/")
    if transport is not None:
        return endpoint, transport
    return endpoint, UrllibTransport(token=args.token or "")


def _headers(args: argparse.Namespace, extra: dict[str, str] | None = None) -> dict[str, str]:
    headers = {"X-Tenant-ID": args.tenant}
    if args.idempotency_key:
        headers["X-Idempotency-Key"] = args.idempotency_key
    if extra:
        headers.update(extra)
    return headers


def _emit(args: argparse.Namespace, status: int, body: dict[str, Any]) -> int:
    if args.json:
        print(json.dumps({"status": status, "body": body}, sort_keys=True))
    elif status >= 400:
        error = body.get("error", {}) if isinstance(body, dict) else {}
        print(f"error {status}: {error.get('code', 'UNKNOWN')}: {error.get('message', '')}")
    else:
        print(json.dumps(body, sort_keys=True))
    return 0 if status < 400 else 1


def _read_artifact(path: str) -> str:
    size = Path(path).stat().st_size
    if size > MAX_ARTIFACT_BYTES:
        raise ValueError(f"artifact {size} bytes exceeds {MAX_ARTIFACT_BYTES} byte bound")
    return Path(path).read_text(encoding="utf-8")


def cmd_capture(args: argparse.Namespace, transport: Transport | None) -> int:
    """Validate intake offline and write the normalized artifact only."""
    import yaml  # local import: capture works without server dependencies

    from investigation_agent_platform.application.onboarding.intake import TenantIntakeV1

    raw = _read_artifact(args.input)
    data = yaml.safe_load(raw) if args.input.endswith((".yaml", ".yml")) else json.loads(raw)
    intake = TenantIntakeV1.model_validate(data)
    out = {"intake": intake.model_dump(mode="json", exclude_none=True)}
    Path(args.output).write_text(json.dumps(out, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if not args.json:
        print(f"captured {args.output}")
    else:
        print(json.dumps({"status": "captured", "output": args.output}))
    return 0


def cmd_apply(args: argparse.Namespace, transport: Transport | None) -> int:
    endpoint, transport = _client(args, transport)
    body = json.loads(_read_artifact(args.input))
    status, response = transport.request(
        "POST",
        f"{endpoint}/api/v1/tenants/{args.tenant}/onboarding/requests",
        headers=_headers(args),
        body=body,
    )
    return _emit(args, status, response)


def cmd_attach(args: argparse.Namespace, transport: Transport | None) -> int:
    endpoint, transport = _client(args, transport)
    body = json.loads(_read_artifact(args.input))
    status, response = transport.request(
        "PUT",
        f"{endpoint}/api/v1/tenants/{args.tenant}/onboarding/requests/{args.request_id}/intake",
        headers=_headers(args),
        body={
            "expected_revision": args.expected_revision,
            "idempotency_key": args.idempotency_key,
            "intake": body.get("intake", body),
        },
    )
    return _emit(args, status, response)


def cmd_status(args: argparse.Namespace, transport: Transport | None) -> int:
    endpoint, transport = _client(args, transport)
    status, response = transport.request(
        "GET",
        f"{endpoint}/api/v1/tenants/{args.tenant}/onboarding/requests/{args.request_id}",
        headers=_headers(args),
        body=None,
    )
    return _emit(args, status, response)


def cmd_action(action: str) -> Any:
    def _run(args: argparse.Namespace, transport: Transport | None) -> int:
        endpoint, transport = _client(args, transport)
        payload: dict[str, Any] = {}
        if action in ("approve", "cancel"):
            payload = {"revision": args.revision, "idempotency_key": args.idempotency_key}
        elif action == "retry":
            payload = {"dispatch_id": args.dispatch_id}
        status, response = transport.request(
            "POST",
            f"{endpoint}/api/v1/tenants/{args.tenant}/onboarding/requests/{args.request_id}:{action}",
            headers=_headers(args),
            body=payload,
        )
        return _emit(args, status, response)

    _run.__name__ = f"cmd_{action}"
    return _run


def cmd_lifecycle(action: str) -> Any:
    def _run(args: argparse.Namespace, transport: Transport | None) -> int:
        endpoint, transport = _client(args, transport)
        status, response = transport.request(
            "POST",
            f"{endpoint}/api/v1/tenants/{args.tenant}:{action}",
            headers=_headers(args),
            body={},
        )
        return _emit(args, status, response)

    _run.__name__ = f"cmd_{action}"
    return _run


def cmd_capabilities(args: argparse.Namespace, transport: Transport | None) -> int:
    endpoint, transport = _client(args, transport)
    status, response = transport.request(
        "GET",
        f"{endpoint}/api/v1/tenants/{args.tenant}/capabilities?tier={args.tier}",
        headers=_headers(args),
        body=None,
    )
    return _emit(args, status, response)


def cmd_cleanup_plan(args: argparse.Namespace, transport: Transport | None) -> int:
    endpoint, transport = _client(args, transport)
    status, response = transport.request(
        "POST",
        f"{endpoint}/api/v1/tenants/{args.tenant}:deletion-request",
        headers=_headers(args),
        body={
            "application_id": args.application_id,
            "environment": args.environment,
            "deployment_id": args.deployment_id,
            "policy_version": args.policy_version,
            "requester_id": args.requester,
            "approver_ids": args.approver or [],
            "backup_id": args.backup_id,
            "expires_at": args.expires_at,
        },
    )
    return _emit(args, status, response)


def cmd_cleanup_approve(args: argparse.Namespace, transport: Transport | None) -> int:
    endpoint, transport = _client(args, transport)
    if args.token_value:
        status, response = transport.request(
            "POST",
            f"{endpoint}/api/v1/deletion-requests/{args.manifest_id}:approve",
            headers=_headers(args),
            body={"approval_token": args.token_value, "approver_id": args.approver},
        )
        return _emit(args, status, response)
    status, response = transport.request(
        "POST",
        f"{endpoint}/api/v1/tenants/{args.tenant}/deletion-requests/{args.manifest_id}:approve-mint",
        headers=_headers(args),
        body={
            "manifest_id": args.manifest_id,
            "approver_id": args.approver,
            "requester_id": args.requester,
            "policy_version": args.policy_version,
            "store_registry_version": args.registry_version,
            "expires_at": args.expires_at,
        },
    )
    return _emit(args, status, response)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="onboard", description="IAP tenant onboarding thin client"
    )
    parser.add_argument("--endpoint", default="http://127.0.0.1:8000", help="API base URL")
    parser.add_argument("--tenant", required=True, help="Canonical tenant ID")
    parser.add_argument("--token", default="", help="Bearer token (or IAP_TOKEN env)")
    parser.add_argument("--idempotency-key", default="", help="Idempotency key for mutating calls")
    parser.add_argument("--json", action="store_true", help="Machine-readable output")
    sub = parser.add_subparsers(dest="command", required=True)

    capture = sub.add_parser("capture", help="Validate intake offline and export it")
    capture.add_argument("input", help="Intake YAML/JSON file")
    capture.add_argument("--output", required=True, help="Normalized artifact path")
    capture.set_defaults(func=cmd_capture)

    apply = sub.add_parser("apply", help="Submit immutable intake and poll the durable job")
    apply.add_argument("input", help="Captured intake artifact")
    apply.set_defaults(func=cmd_apply)

    attach = sub.add_parser("attach", help="Create an immutable profile/config revision")
    attach.add_argument("--request-id", required=True)
    attach.add_argument("--expected-revision", type=int, required=True)
    attach.add_argument("input", help="Captured intake artifact")
    attach.set_defaults(func=cmd_attach)

    status_cmd = sub.add_parser(
        "status", help="Show request, job, readiness, and per-capability status"
    )
    status_cmd.add_argument("--request-id", required=True)
    status_cmd.set_defaults(func=cmd_status)

    for action in ("approve", "retry", "cancel"):
        action_cmd = sub.add_parser(action, help=f"{action} a request or dispatch")
        action_cmd.add_argument("--request-id", required=True)
        if action in ("approve", "cancel"):
            action_cmd.add_argument("--revision", type=int, required=True)
        if action == "retry":
            action_cmd.add_argument("--dispatch-id", required=True)
        action_cmd.set_defaults(func=cmd_action(action))

    for action in ("suspend", "resume"):
        lifecycle_cmd = sub.add_parser(action, help=f"{action} tenant access")
        lifecycle_cmd.set_defaults(func=cmd_lifecycle(action))

    capabilities = sub.add_parser("capabilities", help="Show policy-resolved capabilities")
    capabilities.add_argument("--tier", default="standard")
    capabilities.set_defaults(func=cmd_capabilities)

    cleanup_plan = sub.add_parser(
        "cleanup-plan", help="Display store coverage, exact manifest, and holds"
    )
    cleanup_plan.add_argument("--application-id", default=None)
    cleanup_plan.add_argument("--environment", required=True)
    cleanup_plan.add_argument("--deployment-id", required=True)
    cleanup_plan.add_argument("--policy-version", required=True)
    cleanup_plan.add_argument("--requester", required=True)
    cleanup_plan.add_argument("--approver", action="append", default=[])
    cleanup_plan.add_argument("--backup-id", required=True)
    cleanup_plan.add_argument("--expires-at", required=True)
    cleanup_plan.set_defaults(func=cmd_cleanup_plan)

    cleanup_approve = sub.add_parser(
        "cleanup-approve", help="Mint (no token) or consume (with token) a cleanup approval"
    )
    cleanup_approve.add_argument("--manifest-id", required=True)
    cleanup_approve.add_argument("--approver", required=True)
    cleanup_approve.add_argument("--requester", default="")
    cleanup_approve.add_argument("--policy-version", default="p1")
    cleanup_approve.add_argument("--registry-version", default="reg-v1")
    cleanup_approve.add_argument("--expires-at", default="")
    cleanup_approve.add_argument("--token-value", default="")
    cleanup_approve.set_defaults(func=cmd_cleanup_approve)
    return parser


def main(argv: list[str] | None = None, transport: Transport | None = None) -> int:
    """Entry point; transport is injectable for tests."""
    import os

    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.token:
        args.token = os.environ.get("IAP_TOKEN", "")
    func = getattr(args, "func", None)
    if func is None:
        parser.print_help()
        return 2
    try:
        return int(func(args, transport))
    except ValueError as exc:
        print(f"error: {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
