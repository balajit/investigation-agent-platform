#!/usr/bin/env python3
"""Generate an application profile from a git repo + Spring config.

Clones shallow, parses Java/Spring sources, merges optional Spring Config
Server values (config-server-wins) and an operator overlay, then prints or
persists a DRAFT profile. Never invents telemetry indices, secrets, or
journey keys (needs_review entries instead).

Usage:
  uv run python scripts/generate-profile.py --clone-url URL --tenant ID --app-id ID
      [--branch main] [--operator overlay.yaml] [--spring-json '{"a.b": 1}']
      [--persist] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = _REPO_ROOT / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate IAP application profile.")
    parser.add_argument("--clone-url", default="")
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--app-id", default="")
    parser.add_argument("--app-name", default="")
    parser.add_argument("--repo", default="")
    parser.add_argument("--branch", default="")
    parser.add_argument("--operator", default="")
    parser.add_argument("--spring-json", default="{}")
    parser.add_argument("--persist", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    from investigation_agent_platform.integrations.profile_gen.assembler import (
        assemble_profile,
    )
    from investigation_agent_platform.integrations.profile_gen.generate import (
        clone_repo,
        dominant_language,
        list_files,
        parse_checkout,
    )
    from investigation_agent_platform.integrations.profile_gen.github import (
        detect_modules as _detect_modules,
    )
    from investigation_agent_platform.integrations.profile_gen.identity import (
        derive_app_id,
        parse_repo_ref,
    )
    from investigation_agent_platform.integrations.profile_gen.precedence import (
        merge_configs,
    )

    # Minimal contract: --app-name + --repo. Deprecated --app-id +
    # --clone-url still accepted (mapped internally, not removed).
    if args.app_id:
        app_id = args.app_id
    else:
        try:
            app_id = derive_app_id(args.tenant, args.app_name)
        except ValueError as exc:
            parser.error(str(exc))
    if args.clone_url:
        clone_url = args.clone_url
        org, repo = "unknown", app_id
        try:
            path = args.clone_url.rstrip("/").removesuffix(".git").split("github.com/")[-1]
            parts = path.split("/")
            if len(parts) >= 2:
                org, repo = parts[-2], parts[-1]
        except Exception:
            pass
    else:
        try:
            org, repo = parse_repo_ref(args.repo)
        except ValueError as exc:
            parser.error(str(exc))
        clone_url = f"https://github.com/{org}/{repo}.git"
    checkout = clone_repo(clone_url, args.branch)
    try:
        files = list_files(checkout)
        modules = [
            {"path": m.path, "build_system": m.build_system}
            for m in _detect_modules(files)
        ]
        from investigation_agent_platform.integrations.profile_gen.detect import (
            detect_repo_type,
        )
        from investigation_agent_platform.integrations.profile_gen.generators import (
            get_generator,
        )

        samples: dict[str, str] = {}
        for rel in files:
            try:
                base = rel.rsplit("/", 1)[-1]
                if base in (
                    "pom.xml", "build.gradle", "pyproject.toml", "manage.py",
                    "main.py", "app.py",
                ) or base.endswith("Application.java"):
                    samples[rel] = (checkout / rel).read_text(
                        encoding="utf-8", errors="replace"
                    )[:20000]
                    if len(samples) >= 20:
                        break
            except Exception:
                continue
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
        try:
            server_config = json.loads(args.spring_json or "{}")
            if not isinstance(server_config, dict):
                server_config = {}
        except Exception:
            server_config = {}
        merged_config, overridden = merge_configs(
            server_config, parsed.get("spring_files", {}) or {},
        )
        operator: dict[str, Any] = {}
        if args.operator:
            import yaml as _yaml

            with open(args.operator, encoding="utf-8") as handle:
                loaded = _yaml.safe_load(handle.read()) or {}
            if isinstance(loaded, dict):
                operator = loaded
        # org/repo resolved above from --repo or --clone-url.
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
            config=merged_config,
            overridden_keys=overridden,
            operator=operator,
            language=dominant_language(files),
            repo_type=repo_type,
        )
        report["detection"] = detection
        print(json.dumps({"profile": profile, "report": report}, indent=1, default=str)[:6000])
        if args.persist and not args.dry_run:
            from investigation_agent_platform.domain.profile.models import (
                ApplicationProfile,
            )

            validated = ApplicationProfile.model_validate(profile)
            print(f"validated: id={validated.id} services={len(validated.services)}", file=sys.stderr)
    finally:
        shutil.rmtree(checkout, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
