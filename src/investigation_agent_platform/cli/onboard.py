# src/investigation_agent_platform/cli/onboard.py
"""``onboard`` command: capture/apply/attach/status, lifecycle actions, and profile generation."""

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


def cmd_generate_profile(args: argparse.Namespace, transport: Transport | None) -> int:
    """Generate DRAFT application profile(s) from local repo checkout(s), offline.

    Single mode (--repo-dir + --app-name) writes one profile JSON; batch mode
    (--org-dir + --app-prefix) writes one JSON per repo directory into
    --output-dir. --persist saves DRAFT revision(s) DB-direct: the profiles
    API is read-only, so this is the supported write path.
    """
    from investigation_agent_platform.integrations.profile_gen.assembler import (
        assemble_profile,
    )
    from investigation_agent_platform.integrations.profile_gen.detect import (
        detect_repo_type,
    )
    from investigation_agent_platform.integrations.profile_gen.generate import (
        dominant_language,
        list_files,
        parse_checkout,
    )
    from investigation_agent_platform.integrations.profile_gen.generators import (
        get_generator,
    )
    from investigation_agent_platform.integrations.profile_gen.github import (
        detect_modules as _detect_modules,
    )
    from investigation_agent_platform.integrations.profile_gen.identity import (
        derive_app_id,
        slugify_app_name,
    )
    from investigation_agent_platform.integrations.profile_gen.precedence import (
        merge_configs,
    )

    def generate_one(checkout: Path, app_id: str, org: str, repo: str) -> dict[str, Any]:
        if not checkout.is_dir():
            raise ValueError(f"checkout missing: {checkout}")
        files = list_files(checkout)
        modules = [{"path": m.path, "build_system": m.build_system} for m in _detect_modules(files)]
        samples: dict[str, str] = {}
        for rel in files:
            base = rel.rsplit("/", 1)[-1]
            if base in (
                "pom.xml",
                "build.gradle",
                "pyproject.toml",
                "manage.py",
                "main.py",
                "app.py",
            ) or base.endswith("Application.java"):
                try:
                    samples[rel] = (checkout / rel).read_text(encoding="utf-8", errors="replace")[
                        :20000
                    ]
                except OSError:
                    continue
                if len(samples) >= 20:
                    break
        detection = detect_repo_type(files, samples)
        repo_type = str(detection.get("repo_type", "") or "")
        generator = get_generator(repo_type)
        if generator is None:
            from investigation_agent_platform.integrations.profile_gen.generators import (
                java_spring as _java_spring_fallback,
            )

            generator = _java_spring_fallback
            repo_type = "java-spring"
        parsed = parse_checkout(checkout, files, generator)
        merged, overridden = merge_configs({}, parsed.get("spring_files", {}) or {})
        profile, report = assemble_profile(
            tenant_id=args.tenant,
            app_id=app_id,
            org=org,
            repo=repo,
            default_branch=args.branch or "main",
            modules=modules,
            controllers=parsed.get("controllers", []),
            validations=parsed.get("validations", []),
            feign_clients=parsed.get("feign_clients", []),
            config=merged,
            overridden_keys=overridden,
            operator={},
            language=dominant_language(files),
            repo_type=repo_type,
        )
        report["detection"] = detection
        return {"profile": profile, "report": report}

    def persist(items: list[dict[str, Any]]) -> None:
        import asyncio
        import os as _os

        from investigation_agent_platform.domain.profile.models import (
            ApplicationProfile,
            ProfileStatus,
        )
        from investigation_agent_platform.infrastructure.persistence.profile_repository import (
            SqlAlchemyApplicationProfileRepository,
        )

        uri = args.db_uri or _os.environ.get("IAP_DATABASE_URI", "")
        if not uri:
            raise ValueError("generate-profile --persist needs --db-uri or IAP_DATABASE_URI")
        if uri.startswith("postgresql://"):
            uri = "postgresql+asyncpg://" + uri[len("postgresql://") :]

        async def _save() -> None:
            from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

            engine = create_async_engine(uri)
            try:
                repo = SqlAlchemyApplicationProfileRepository(
                    async_sessionmaker(engine, expire_on_commit=False)
                )
                for item in items:
                    validated = ApplicationProfile.model_validate(item["profile"])
                    draft = validated.model_copy(update={"status": ProfileStatus.DRAFT})
                    existing = await repo.list_revisions(draft.tenant_id, draft.id)
                    taken = {r.version for r in existing or []}
                    version = draft.version
                    while version in taken:
                        version += 1
                    draft = draft.model_copy(update={"version": version})
                    await repo.save(draft.tenant_id, draft)
                    print(
                        f"persisted: id={draft.id} tenant={draft.tenant_id} "
                        f"version={draft.version} status=DRAFT"
                    )
            finally:
                await engine.dispose()

        asyncio.run(_save())

    if bool(args.repo_dir) == bool(args.org_dir):
        raise ValueError("generate-profile needs exactly one of --repo-dir or --org-dir")
    if args.repo_dir:
        if not args.app_name:
            raise ValueError("single mode needs --app-name")
        if not args.output:
            raise ValueError("single mode needs --output")
        try:
            app_id = derive_app_id(args.tenant, args.app_name)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
        checkout = Path(args.repo_dir).expanduser()
        item = generate_one(checkout, app_id, args.org_label or "local", checkout.name)
        services = len(item["profile"].get("services", []))
        if args.dry_run:
            print(
                f"dry-run: would write {args.output} (app_id={app_id}, "
                f"services={services}); nothing written, nothing persisted"
            )
            return 0
        output_path = Path(args.output)
        if output_path.exists():
            raise ValueError(f"refusing to overwrite existing output: {output_path}")
        output_path.write_text(json.dumps(item, indent=1, default=str) + "\n", encoding="utf-8")
        print(f"generated: {app_id} services={services} -> {args.output}")
        if args.persist:
            persist([item])
        return 0
    if not args.app_prefix:
        raise ValueError("batch mode needs --app-prefix")
    if not args.output_dir:
        raise ValueError("batch mode needs --output-dir")
    org_dir = Path(args.org_dir).expanduser()
    if not org_dir.is_dir():
        raise ValueError(f"org dir missing: {org_dir}")
    only = {s.strip() for s in (args.only or "").split(",") if s.strip()}
    skip = {s.strip() for s in (args.skip or "").split(",") if s.strip()}
    repos = sorted(
        d
        for d in org_dir.iterdir()
        if d.is_dir()
        and d.name != ".github"
        and (not only or d.name in only)
        and d.name not in skip
    )
    out_dir = Path(args.output_dir)
    # Remediation plan ARCH-MED-014/015: generate everything first, then
    # decide. No file is written and no DB row is persisted before the
    # duplicate/pre-existing/dry-run gates below have all passed, so a
    # skewed batch can never leave a half-written directory behind.
    ok: list[dict[str, Any]] = []
    failed: list[str] = []
    pending: list[tuple[Any, str, dict[str, Any]]] = []
    seen_ids: dict[str, str] = {}
    for repo_dir in repos:
        slug = slugify_app_name(repo_dir.name)
        if not slug:
            print(f"FAILED: {repo_dir.name}: empty slug (nothing to derive an ID from)")
            failed.append(repo_dir.name)
            continue
        app_id = f"{args.app_prefix}-{slug}"
        if app_id in seen_ids:
            raise ValueError(
                f"duplicate app_id {app_id!r} from {seen_ids[app_id]!r} and "
                f"{repo_dir.name!r}: refusing batch before any write"
            )
        seen_ids[app_id] = repo_dir.name
        try:
            item = generate_one(repo_dir, app_id, org_dir.name, repo_dir.name)
        except Exception as exc:
            print(f"FAILED: {repo_dir.name}: {type(exc).__name__}: {exc}")
            failed.append(repo_dir.name)
            continue
        target = out_dir / f"{app_id}.json"
        if target.exists():
            print(f"FAILED: {repo_dir.name}: refusing to overwrite {target}")
            failed.append(repo_dir.name)
            continue
        pending.append((repo_dir, app_id, item))
    if args.dry_run:
        print(
            f"dry-run: would write {len(pending)} profiles to {out_dir}; "
            f"{len(failed)} failed; nothing written, nothing persisted"
        )
        return 1 if failed else 0
    out_dir.mkdir(parents=True, exist_ok=True)
    for repo_dir, app_id, item in pending:
        (out_dir / f"{app_id}.json").write_text(
            json.dumps(item, indent=1, default=str) + "\n", encoding="utf-8"
        )
        print(
            f"generated: {repo_dir.name} -> {app_id} "
            f"services={len(item['profile'].get('services', []))}"
        )
        ok.append(item)
    print(f"done: {len(ok)} generated, {len(failed)} failed")
    if failed:
        print(f"failed repos: {failed}")
    if args.persist and ok:
        persist(ok)
    return 1 if failed else 0


def cmd_verify_telemetry(args: argparse.Namespace, transport: Transport | None) -> int:
    """Verify a tenant's telemetry routing live, offline (no API server needed).

    Loads ``<telemetry-dir>/<tenant>/telemetry.conf`` and runs the platform
    verification flow against the declared deployments: routed search,
    attribution, pattern use, service filtering, fail-closed paths, and
    fetch-by-stored-index. Human lines by default, JSON array with --json.
    """
    import asyncio

    from investigation_agent_platform.application.onboarding.telemetry_verification import (
        DEFAULT_MAPPING_PROFILES_PATH,
        verify_tenant_telemetry_from_dir,
    )
    from investigation_agent_platform.domain.common.exceptions import (
        PlatformConfigurationError,
    )

    try:
        results = asyncio.run(
            verify_tenant_telemetry_from_dir(
                telemetry_dir=args.telemetry_dir,
                tenant_id=args.tenant,
                home_service=args.home_service,
                mapping_source_id=args.mapping_source_id,
                mapping_profiles_path=args.mapping_profiles_path
                if args.mapping_profiles_path is not None
                else (DEFAULT_MAPPING_PROFILES_PATH if args.mapping_source_id else None),
            )
        )
    except PlatformConfigurationError as exc:
        print(f"error: {exc}")
        return 2
    except Exception as exc:  # noqa: BLE001 — probe tool: loud message, no traceback
        print(f"error: verification failed: {exc}")
        return 1
    if args.json:
        print(json.dumps([r.to_dict() for r in results], indent=2))
    else:
        for r in results:
            line = f"[{'PASS' if r.passed else 'FAIL'}] {r.name}"
            if r.detail:
                line += f" — {r.detail}"
            print(line)
    return 0 if all(r.passed for r in results) else 1


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

    generate = sub.add_parser(
        "generate-profile",
        help="Generate DRAFT application profile(s) from local checkout(s), offline",
    )
    generate.add_argument("--repo-dir", default="", help="Single repo checkout path")
    generate.add_argument("--org-dir", default="", help="Org directory of repo checkouts")
    generate.add_argument("--app-name", default="", help="Single mode: application name")
    generate.add_argument("--app-prefix", default="", help="Batch mode: app-id prefix")
    generate.add_argument("--org-label", default="local", help="Provenance org label")
    generate.add_argument("--branch", default="main")
    generate.add_argument("--only", default="", help="Batch mode: comma-separated repo dirs")
    generate.add_argument("--skip", default="", help="Batch mode: comma-separated repo dirs")
    generate.add_argument("--output", default="", help="Single mode: profile JSON path")
    generate.add_argument("--output-dir", default="", help="Batch mode: profile JSON dir")
    generate.add_argument("--persist", action="store_true", help="Save DRAFT revision(s) DB-direct")
    generate.add_argument("--db-uri", default="", help="DB URI (or IAP_DATABASE_URI)")
    generate.add_argument("--dry-run", action="store_true")
    generate.set_defaults(func=cmd_generate_profile)
    verify = sub.add_parser(
        "verify-telemetry",
        help="Verify tenant telemetry routing live from a telemetry dir, offline",
    )
    verify.add_argument(
        "--telemetry-dir",
        required=True,
        help="dir holding <tenant>/telemetry.conf (operator scope)",
    )
    verify.add_argument(
        "--home-service",
        required=True,
        help="homed service to probe (must be homed on one endpoint)",
    )
    verify.add_argument(
        "--mapping-source-id",
        default=None,
        help="observability mapping to verify field attribution with",
    )
    verify.add_argument(
        "--mapping-profiles-path",
        default=None,
        help="mapping profiles dir (default: platform config dir)",
    )
    verify.set_defaults(func=cmd_verify_telemetry)
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
