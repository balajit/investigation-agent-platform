"""Application profiles lifecycle router (appl-profile 1.0 §5, OpenAPI spec 1.0).

Immutable revisions, tenant-scoped. Reads are sanitized projections
(`ProfileView.from_domain` — template SQL and connection refs never leave the
API). All mutations accept ``X-Idempotency-Key`` with the same reserve/replay
pattern as investigations.

Serialization discipline (spec §3.3): CRUD routes use
``response_model_exclude_unset=True`` so explicit nulls in the sanitized body
are preserved byte-for-byte while the optional ``idempotent_replay`` flag is
emitted only on replay paths (constructors omit it otherwise). Spring routes
use the same setting with present-keys-only construction.
"""

import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from functools import lru_cache
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Header, Query, Response, status
from pydantic import ValidationError

from investigation_agent_platform.api.dependencies import get_app_context
from investigation_agent_platform.api.errors import ApiError
from investigation_agent_platform.api.tenant import require_tenant
from investigation_agent_platform.api.v1.schemas.envelopes import ListEnvelope
from investigation_agent_platform.api.v1.schemas.errors import COMMON_ERROR_RESPONSES
from investigation_agent_platform.api.v1.schemas.profiles import (
    DerivationReportView,
    GeneratedProfileView,
    GeneratedRefreshView,
    ProfileDeleteAck,
    ProfileDetail,
    ProfileRevisionView,
    ProfileView,
    SpringDerivationView,
    SpringDriftView,
    SpringRefreshView,
)
from investigation_agent_platform.domain.common.exceptions import (
    IdempotencyConflictError,
    IdempotencyInProgressError,
)
from investigation_agent_platform.domain.investigation.models import ActionType
from investigation_agent_platform.domain.observability.mapping import GENERIC_ECS_SOURCE_ID
from investigation_agent_platform.domain.profile.models import (
    ApplicationProfile,
    ProfileStatus,
    check_status_transition,
)
from investigation_agent_platform.integrations.spring_config.client import (
    OAuth2ClientCredentials,
    SpringConfigClient,
)
from investigation_agent_platform.integrations.spring_config.derivation import derive_profile
from investigation_agent_platform.integrations.spring_config.exceptions import (
    ConfigAuthenticationError,
    ConfigAuthorizationError,
    ConfigClientError,
    ConfigNotFoundError,
    ConfigRateLimitError,
)
from investigation_agent_platform.integrations.spring_config.models import SpringBinding
from investigation_agent_platform.integrations.spring_config.security import (
    allowed_property_patterns,
    allowed_server_urls,
    allowed_spring_applications,
    allowed_spring_profiles,
    oauth_settings,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["profiles"])

_VALID_CAPABILITIES = frozenset(a.value for a in ActionType)
_MAPPINGS_DIR = "config/observability-mappings"


@lru_cache(maxsize=4)
def _known_mapping_ids(mappings_dir: str) -> frozenset[str]:
    try:
        from investigation_agent_platform.infrastructure.configuration.mapping_registry import (
            load_mapping_profiles,
        )

        return frozenset(load_mapping_profiles(Path(mappings_dir)).source_ids)
    except Exception:
        return frozenset()


def _err(code: str, message: str, status_code: int) -> ApiError:
    return ApiError(code=code, http_status=status_code, message=message)


def _parse_body(tenant_id: str, data: dict[str, Any]) -> ApplicationProfile:
    body = dict(data)
    if body.get("tenant_id") is not None and body["tenant_id"] != tenant_id:
        raise _err("PROFILE_TENANT_MISMATCH", "Body tenant_id does not match caller.", 400)
    body["tenant_id"] = tenant_id
    try:
        return ApplicationProfile.model_validate(body)
    except ValidationError as exc:
        fields = [".".join(str(p) for p in e.get("loc", ())) for e in exc.errors()][:5]
        raise _err(
            "PROFILE_SCHEMA_INVALID",
            f"Profile schema validation failed at: {fields}",
            400,
        ) from exc


def _check_semantics(profile: ApplicationProfile) -> None:
    known = set(_known_mapping_ids(_MAPPINGS_DIR)) | {GENERIC_ECS_SOURCE_ID}
    if profile.mapping_source_id not in known:
        raise _err(
            "PROFILE_UNKNOWN_MAPPING",
            f"mapping_source_id {profile.mapping_source_id!r} does not resolve.",
            422,
        )
    unknown_caps = [c for c in profile.allowed_capabilities if c not in _VALID_CAPABILITIES]
    if unknown_caps:
        raise _err(
            "PROFILE_UNKNOWN_CAPABILITY",
            f"Unknown capabilities: {unknown_caps[:5]}",
            422,
        )


def _check_promotable(profile: ApplicationProfile) -> None:
    if not profile.services:
        raise _err("PROFILE_NOT_PROMOTABLE", "ACTIVE profiles require at least one service.", 422)
    tbd = [ds.id for ds in profile.datasources if (ds.connection_ref or "").strip() == "TBD"]
    if tbd:
        raise _err(
            "PROFILE_NOT_PROMOTABLE",
            f"TBD connection refs must be completed before ACTIVE: {tbd[:5]}",
            422,
        )


async def _with_idempotency(
    ctx: Any,
    tenant_id: str,
    key: str | None,
    payload: dict[str, Any],
    produce: Callable[[], Awaitable[dict[str, Any]]],
    response: Response,
    created_code: int = 201,
) -> dict[str, Any]:
    """Run ``produce`` once per key; replay the stored response on retry."""
    if not key:
        return await produce()
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    request_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    try:
        cached, reserved = await ctx.idempotency_store.reserve_or_get(
            tenant_id, key, request_hash
        )
    except (IdempotencyConflictError, IdempotencyInProgressError) as exc:
        raise _err("IDEMPOTENCY_CONFLICT", str(exc), 409) from exc
    if not reserved:
        response.status_code = status.HTTP_200_OK
        replayed: dict[str, Any] = dict(cached or {})
        return replayed
    result = await produce()
    await ctx.idempotency_store.complete(tenant_id, key, result)
    response.status_code = created_code
    return result


def _detail_payload(profile: ApplicationProfile, version: int) -> dict[str, Any]:
    """ProfileDetail dict with the route's exclusion semantics (no replay flag)."""
    return ProfileDetail(
        profile=ProfileView.from_domain(profile), version=version
    ).model_dump(mode="json", by_alias=True, exclude_unset=True)


def _replay_payload(profile: ApplicationProfile, version: int) -> dict[str, Any]:
    """ProfileDetail dict for idempotent replays (flag present by construction)."""
    return ProfileDetail(
        profile=ProfileView.from_domain(profile), version=version, idempotent_replay=True
    ).model_dump(mode="json", by_alias=True, exclude_unset=True)


@router.get(
    "/profiles",
    response_model=ListEnvelope[ProfileView],
    response_model_by_alias=True,
    response_model_exclude_unset=True,
    responses=COMMON_ERROR_RESPONSES,
)
async def list_profiles(
    x_tenant_id: str = Depends(require_tenant),
    status_filter: str | None = Query(default=None, alias="status"),
) -> ListEnvelope[ProfileView]:
    """Lists latest revision per application id, sanitized, optional status filter."""
    ctx = get_app_context()
    if status_filter is not None:
        try:
            ProfileStatus(status_filter)
        except ValueError as exc:
            raise _err("PROFILE_UNKNOWN_STATUS", f"Unknown status {status_filter!r}.", 400) from exc
    profiles = await ctx.profile_repo.list(x_tenant_id, status=status_filter)
    items = [ProfileView.from_domain(p) for p in profiles]
    return ListEnvelope[ProfileView](items=items, total=len(items))


@router.post(
    "/profiles",
    response_model=ProfileDetail,
    response_model_by_alias=True,
    response_model_exclude_unset=True,
    responses=COMMON_ERROR_RESPONSES,
    status_code=status.HTTP_201_CREATED,
)
async def create_profile(
    body: dict[str, Any],
    response: Response,
    x_tenant_id: str = Depends(require_tenant),
    x_idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
) -> ProfileDetail:
    """Creates version 1 of a new application profile (DRAFT default)."""
    ctx = get_app_context()
    profile = _parse_body(x_tenant_id, body)
    if profile.version != 1:
        raise _err("PROFILE_VERSION_CONFLICT", "New profiles must start at version 1.", 409)
    if profile.status not in (ProfileStatus.DRAFT, ProfileStatus.ACTIVE):
        raise _err(
            "PROFILE_SEMANTIC",
            "New profiles must be DRAFT or ACTIVE (never DEPRECATED/ARCHIVED).",
            422,
        )
    _check_semantics(profile)
    if profile.status == ProfileStatus.ACTIVE:
        _check_promotable(profile)

    async def _produce() -> dict[str, Any]:
        existing = await ctx.profile_repo.get_by_application_id(
            x_tenant_id, profile.id, version=1
        )
        if existing is not None:
            if existing.model_dump(mode="json", by_alias=True) == profile.model_dump(
                mode="json", by_alias=True
            ):
                response.status_code = status.HTTP_200_OK
                return _replay_payload(existing, 1)
            raise _err(
                "PROFILE_VERSION_CONFLICT",
                f"Profile {profile.id!r} version 1 already exists with different content.",
                409,
            )
        await ctx.profile_repo.save(x_tenant_id, profile)
        return _detail_payload(profile, 1)

    return ProfileDetail.model_validate(
        await _with_idempotency(
            ctx, x_tenant_id, x_idempotency_key, body, _produce, response, created_code=201
        )
    )


# ---------------------------------------------------------------------------
# Spring Config Server derivation (§10). Registered BEFORE /{app_id} so the
# literal `:action` suffix routes win over the plain detail route.
# ---------------------------------------------------------------------------

_DIFF_EXCLUDED_TOP_KEYS = frozenset({"version", "status", "derivation", "source"})


def _spring_http_error(exc: ConfigClientError) -> ApiError:
    if isinstance(exc, ConfigAuthenticationError):
        return ApiError(
            code=exc.code, http_status=502, message="Spring upstream rejected credentials."
        )
    if isinstance(exc, ConfigAuthorizationError):
        return ApiError(
            code=exc.code, http_status=403, message="Spring import outside permitted scope."
        )
    if isinstance(exc, ConfigNotFoundError):
        return ApiError(
            code=exc.code, http_status=404, message="Spring application/profile/label not found."
        )
    if isinstance(exc, ConfigRateLimitError):
        headers = (
            {"Retry-After": str(exc.retry_after)}
            if exc.retry_after is not None
            else None
        )
        return ApiError(
            code=exc.code,
            http_status=429,
            message="Spring upstream throttled the request.",
            headers=headers,
        )
    return ApiError(
        code=exc.code, http_status=503, message="Spring Config Server unavailable."
    )


def _fetch_config(binding: SpringBinding) -> Any:
    """Fetch + merge Spring configuration (monkeypatch seam for tests)."""
    server = binding.server_url.rstrip("/")
    allowed_servers = allowed_server_urls()
    if not allowed_servers or server not in allowed_servers:
        raise ApiError(code="SPRING_FORBIDDEN", http_status=403, message="Config server not allowlisted.")
    apps = allowed_spring_applications()
    profiles = allowed_spring_profiles()
    if not apps or binding.application not in apps:
        raise ApiError(code="SPRING_FORBIDDEN", http_status=403, message="Spring app not importable.")
    if not profiles or binding.profile not in profiles:
        raise ApiError(code="SPRING_FORBIDDEN", http_status=403, message="Spring profile not importable.")
    oauth = oauth_settings()
    if not oauth["token_url"] or not oauth["client_id"] or not oauth["client_secret"]:
        raise ApiError(code="SPRING_UNAVAILABLE", http_status=503, message="Spring OAuth not configured.")
    try:
        credentials = OAuth2ClientCredentials(
            token_url=oauth["token_url"],
            client_id=oauth["client_id"],
            client_secret=oauth["client_secret"],
            scope=oauth["scope"] or None,
        )
        client = SpringConfigClient(
            server_url=server,
            oauth=credentials,
            allowed_applications=set(apps),
            allowed_profiles=set(profiles),
            allowed_property_patterns=allowed_property_patterns(),
        )
    except ValueError as exc:
        raise ApiError(code="SPRING_FORBIDDEN", http_status=403, message=str(exc)) from exc
    try:
        return client.fetch(binding.application, binding.profile, binding.label)
    except ConfigClientError as exc:
        raise _spring_http_error(exc) from exc


def _diff_dicts(old: Any, new: Any, prefix: str, out: list[str]) -> None:
    if len(out) >= 100:
        return
    if isinstance(old, dict) and isinstance(new, dict):
        for key in sorted(set(old) | set(new)):
            if not prefix and key in _DIFF_EXCLUDED_TOP_KEYS:
                continue
            _diff_dicts(old.get(key), new.get(key), f"{prefix}.{key}" if prefix else key, out)
    elif isinstance(old, list) and isinstance(new, list):
        if old != new:
            out.append(prefix or "$")
    elif old != new:
        out.append(prefix or "$")


def _derive_warnings(profile: ApplicationProfile) -> list[str]:
    warnings = ["Derived output is DRAFT: complete TBD fields before activating."]
    tbd = [ds.id for ds in profile.datasources if (ds.connection_ref or "").strip() == "TBD"]
    if tbd:
        warnings.append(f"TBD connection refs: {tbd[:5]}")
    report = profile.derivation
    if report and report.needs_review:
        warnings.append(f"{len(report.needs_review)} inferred fields need review.")
    return warnings


@router.post(
    "/profiles:derive-from-spring",
    response_model=SpringDerivationView,
    response_model_by_alias=True,
    response_model_exclude_unset=True,
    responses=COMMON_ERROR_RESPONSES,
    status_code=status.HTTP_201_CREATED,
)
async def derive_from_spring(
    body: dict[str, Any],
    x_tenant_id: str = Depends(require_tenant),
) -> SpringDerivationView:
    """Derive (and by default persist) a DRAFT profile from Spring Config."""
    ctx = get_app_context()
    tenant_app_id = body.get("tenant_app_id")
    if not isinstance(tenant_app_id, str) or not tenant_app_id or len(tenant_app_id) > 128:
        raise _err("SPRING_BINDING_INVALID", "tenant_app_id is required (max 128).", 400)
    spring = body.get("spring")
    if not isinstance(spring, dict):
        raise _err("SPRING_BINDING_INVALID", "spring binding block is required.", 400)
    try:
        binding = SpringBinding.model_validate(spring)
    except ValidationError as exc:
        raise _err("SPRING_BINDING_INVALID", f"Invalid spring binding: {exc.errors()[:2]}", 400) from exc
    persist = body.get("persist", True)
    if not isinstance(persist, bool):
        raise _err("SPRING_BINDING_INVALID", "persist must be boolean.", 400)

    try:
        result = _fetch_config(binding)
    except ConfigClientError as exc:
        raise _spring_http_error(exc) from exc
    profile, report = derive_profile(x_tenant_id, tenant_app_id, binding, result)
    _check_semantics(profile)

    if not persist:
        return SpringDerivationView(
            profile=ProfileView.from_domain(profile),
            derivation=DerivationReportView.model_validate(report.model_dump(mode="json")),
            persisted=False,
            warnings=_derive_warnings(profile),
        )
    revisions = await ctx.profile_repo.list_revisions(x_tenant_id, tenant_app_id)
    version = (revisions[0].version + 1) if revisions else 1
    stamped = profile.model_copy(update={"version": version})
    await ctx.profile_repo.save(x_tenant_id, stamped)
    return SpringDerivationView(
        profile=ProfileView.from_domain(stamped),
        version=version,
        derivation=DerivationReportView.model_validate(report.model_dump(mode="json")),
        persisted=True,
        warnings=_derive_warnings(profile),
    )


async def _rederive_latest(
    ctx: Any, tenant_id: str, app_id: str
) -> tuple[ApplicationProfile, ApplicationProfile, list[str]]:
    revisions = await ctx.profile_repo.list_revisions(tenant_id, app_id)
    if not revisions:
        raise _err("PROFILE_NOT_FOUND", f"Unknown application {app_id!r}.", 404)
    latest = revisions[0]
    source = latest.source
    if source is None or source.type != "spring_config":
        raise _err(
            "SPRING_NO_BINDING",
            "Profile has no spring_config source binding; use derive-from-spring first.",
            422,
        )
    binding = SpringBinding(
        server_url=source.server_url,
        application=source.spring_application,
        profile=source.spring_profile,
        label=source.label,
    )
    try:
        result = _fetch_config(binding)
    except ConfigClientError as exc:
        raise _spring_http_error(exc) from exc
    fresh, _ = derive_profile(tenant_id, app_id, binding, result)
    diff: list[str] = []
    _diff_dicts(
        latest.model_dump(mode="json", by_alias=True),
        fresh.model_dump(mode="json", by_alias=True),
        "",
        diff,
    )
    return latest, fresh, diff


@router.post(
    "/profiles/{app_id}:refresh-from-spring",
    response_model=SpringRefreshView,
    response_model_by_alias=True,
    response_model_exclude_unset=True,
    responses=COMMON_ERROR_RESPONSES,
)
async def refresh_from_spring(
    app_id: str,
    x_tenant_id: str = Depends(require_tenant),
) -> SpringRefreshView:
    """Re-derive from the stored binding; writes latest+1 only on material change."""
    ctx = get_app_context()
    latest, fresh, diff = await _rederive_latest(ctx, x_tenant_id, app_id)
    if not diff:
        return SpringRefreshView(changed=False, version=latest.version)
    stamped = fresh.model_copy(update={"version": latest.version + 1})
    await ctx.profile_repo.save(x_tenant_id, stamped)
    return SpringRefreshView(
        changed=True,
        version=stamped.version,
        diff=diff,
        profile=ProfileView.from_domain(stamped),
        warnings=_derive_warnings(fresh),
    )


def _generate_profile_from_repo(
    tenant_id: str,
    tenant_app_id: str,
    org: str,
    repo: str,
    branch: str,
    spring: dict[str, Any] | None,
    operator: dict[str, Any] | None,
) -> tuple[Any, dict[str, Any]]:
    """Clone → parse → merge → assemble (shared by generate/refresh)."""
    import shutil as _shutil

    from investigation_agent_platform.domain.profile.models import (
        ApplicationProfile as _ApplicationProfile,
    )
    from investigation_agent_platform.integrations.profile_gen.assembler import (
        assemble_profile as _assemble,
    )
    from investigation_agent_platform.integrations.profile_gen.generate import (
        clone_repo as _clone,
    )
    from investigation_agent_platform.integrations.profile_gen.generate import (
        list_files as _list_files,
    )
    from investigation_agent_platform.integrations.profile_gen.generate import (
        parse_checkout as _parse,
    )
    from investigation_agent_platform.integrations.profile_gen.github import (
        detect_modules as _modules,
    )
    from investigation_agent_platform.integrations.profile_gen.precedence import (
        merge_configs as _merge_configs,
    )

    clone_url = f"https://github.com/{org}/{repo}.git"
    checkout = _clone(clone_url, branch)
    try:
        files = _list_files(checkout)
        modules = [
            {"path": m.path, "build_system": m.build_system}
            for m in _modules(files)
        ]
        # G1+G3: detect repo type from paths + marker samples, then parse
        # with the matching generator (generic fallback preserves legacy
        # Spring behavior when detection is inconclusive).
        from investigation_agent_platform.integrations.profile_gen.detect import (
            detect_repo_type as _detect,
        )
        from investigation_agent_platform.integrations.profile_gen.generators import (
            get_generator as _get_generator,
        )

        samples: dict[str, str] = {}
        try:
            for rel in files:
                try:
                    base = rel.rsplit("/", 1)[-1]
                    if base in (
                        "pom.xml", "build.gradle", "build.gradle.kts",
                        "pyproject.toml", "manage.py", "settings.py",
                        "main.py", "app.py", "application.yml",
                        "application.yaml", "application.properties",
                    ) or base.endswith("Application.java"):
                        samples[rel] = (checkout / rel).read_text(
                            encoding="utf-8", errors="replace"
                        )[:20000]
                        if len(samples) >= 20:
                            break
                except Exception:
                    continue
        except Exception:
            samples = {}
        detection = _detect(files, samples)
        repo_type = str(detection.get("repo_type", "") or "")
        generator = _get_generator(repo_type)
        if generator is None:
            from investigation_agent_platform.integrations.profile_gen.generators import (
                java_spring as _java_spring_fallback,
            )

            generator = _java_spring_fallback
            repo_type = "java-spring"
        parsed = _parse(checkout, files, generator)
        server_config: dict[str, Any] = {}
        if spring:
            try:
                binding = SpringBinding.model_validate(spring)
                result = _fetch_config(binding)
                server_config = dict(result.properties or {})
            except Exception as exc:
                raise _err(
                    "GENERATE_SPRING_FAILED",
                    f"Spring config fetch failed: {str(exc)[:200]}",
                    502,
                ) from exc
        merged_config, overridden = _merge_configs(
            server_config, parsed.get("spring_files", {}) or {},
        )
        from investigation_agent_platform.integrations.profile_gen.generate import (
            dominant_language as _dominant_language,
        )

        profile_dict, report = _assemble(
            tenant_id=tenant_id,
            app_id=tenant_app_id,
            org=org,
            repo=repo,
            default_branch=branch or "main",
            modules=modules,
            controllers=parsed.get("controllers", []),
            validations=parsed.get("validations", []),
            feign_clients=parsed.get("feign_clients", []),
            config=merged_config,
            overridden_keys=overridden,
            operator=operator or {},
            language=_dominant_language(files),
            repo_type=repo_type,
        )
        profile_dict = dict(profile_dict)
        report = dict(report)
        report["detection"] = detection
        try:
            profile = _ApplicationProfile.model_validate(profile_dict)
        except ValidationError as exc:
            raise _err(
                "GENERATE_PROFILE_INVALID",
                f"Assembled profile failed validation: {exc.errors()[:2]}",
                500,
            ) from exc
        return profile, report
    finally:
        _shutil.rmtree(checkout, ignore_errors=True)


@router.post(
    "/profiles:generate",
    response_model=GeneratedProfileView,
    response_model_by_alias=True,
    response_model_exclude_unset=True,
    responses=COMMON_ERROR_RESPONSES,
    status_code=status.HTTP_201_CREATED,
)
async def generate_profile(
    body: dict[str, Any],
    x_tenant_id: str = Depends(require_tenant),
) -> GeneratedProfileView:
    """Generate (and by default persist) a DRAFT profile from a GitHub repo.

    Minimal contract: app_name + repo ("github.com/org/repo", URL, or
    bare org/repo). Profile ID derives from tenant + slug(app_name);
    an existing ID replays instead of forking. Deprecated: tenant_app_id
    + org + repo (mapped internally, not removed).
    """
    ctx = get_app_context()
    # Minimal contract first; deprecated explicit fields as fallback.
    app_name = body.get("app_name", "")
    repo_ref = body.get("repo", "")
    if isinstance(body.get("tenant_app_id"), str) and body.get("tenant_app_id"):
        tenant_app_id = body["tenant_app_id"]
    else:
        if not isinstance(app_name, str) or not app_name or len(app_name) > 128:
            raise _err("GENERATE_BINDING_INVALID", "app_name is required (max 128).", 400)
        from investigation_agent_platform.integrations.profile_gen.identity import (
            derive_app_id as _derive_app_id,
        )

        try:
            tenant_app_id = _derive_app_id(x_tenant_id, app_name)
        except ValueError as exc:
            raise _err("GENERATE_BINDING_INVALID", str(exc), 400) from exc
    if isinstance(body.get("org"), str) and isinstance(body.get("repo"), str) and body.get("org") and body.get("repo"):
        org, repo = body["org"], body["repo"]
    else:
        if not isinstance(repo_ref, str) or not repo_ref:
            raise _err("GENERATE_BINDING_INVALID", "repo is required (github.com/org/repo).", 400)
        from investigation_agent_platform.integrations.profile_gen.identity import (
            parse_repo_ref as _parse_repo_ref,
        )

        try:
            org, repo = _parse_repo_ref(repo_ref)
        except ValueError as exc:
            raise _err("GENERATE_BINDING_INVALID", str(exc), 400) from exc
    for value, label in ((org, "org"), (repo, "repo")):
        if not isinstance(value, str) or not value or len(value) > 128 or "/" in value or ".." in value:
            raise _err("GENERATE_BINDING_INVALID", f"{label} is required (max 128, no slashes).", 400)
    branch = body.get("branch", "") or "main"
    if not isinstance(branch, str) or len(branch) > 128:
        raise _err("GENERATE_BINDING_INVALID", "branch must be a string (max 128).", 400)
    spring = body.get("spring")
    if spring is not None and not isinstance(spring, dict):
        raise _err("GENERATE_BINDING_INVALID", "spring must be an object.", 400)
    operator = body.get("operator", {})
    if not isinstance(operator, dict):
        raise _err("GENERATE_BINDING_INVALID", "operator must be an object.", 400)
    persist = body.get("persist", True)
    if not isinstance(persist, bool):
        raise _err("GENERATE_BINDING_INVALID", "persist must be boolean.", 400)
    try:
        profile, report = await __import__("asyncio").to_thread(
            _generate_profile_from_repo,
            x_tenant_id,
            tenant_app_id,
            org,
            repo,
            branch,
            spring,
            operator,
        )
    except ApiError:
        raise
    except Exception as exc:
        raise _err("GENERATE_FAILED", f"Profile generation failed: {str(exc)[:200]}", 500) from exc
    _check_semantics(profile)
    if not persist:
        return GeneratedProfileView(
            profile=ProfileView.from_domain(profile),
            derivation=report,
            persisted=False,
            warnings=_derive_warnings(profile),
        )
    revisions = await ctx.profile_repo.list_revisions(x_tenant_id, tenant_app_id)
    version = (revisions[0].version + 1) if revisions else 1
    stamped = profile.model_copy(update={"version": version})
    await ctx.profile_repo.save(x_tenant_id, stamped)
    return GeneratedProfileView(
        profile=ProfileView.from_domain(stamped),
        version=version,
        derivation=report,
        persisted=True,
        warnings=_derive_warnings(stamped),
    )


@router.post(
    "/profiles:refresh",
    response_model=GeneratedRefreshView,
    response_model_by_alias=True,
    response_model_exclude_unset=True,
    responses=COMMON_ERROR_RESPONSES,
)
async def refresh_generated_profile(
    body: dict[str, Any],
    x_tenant_id: str = Depends(require_tenant),
) -> GeneratedRefreshView:
    """Re-derive a generated profile; operator overlay reapplies (non-destructive).

    Minimal contract: app_name + repo (same ID derivation as generate).
    Deprecated tenant_app_id + org + repo still accepted.
    """
    ctx = get_app_context()
    if isinstance(body.get("tenant_app_id"), str) and body.get("tenant_app_id"):
        tenant_app_id = body["tenant_app_id"]
    else:
        app_name = body.get("app_name", "")
        if not isinstance(app_name, str) or not app_name:
            raise _err("GENERATE_BINDING_INVALID", "app_name is required.", 400)
        from investigation_agent_platform.integrations.profile_gen.identity import (
            derive_app_id as _derive_app_id,
        )

        try:
            tenant_app_id = _derive_app_id(x_tenant_id, app_name)
        except ValueError as exc:
            raise _err("GENERATE_BINDING_INVALID", str(exc), 400) from exc
    revisions = await ctx.profile_repo.list_revisions(x_tenant_id, tenant_app_id)
    if not revisions:
        raise _err("PROFILE_NOT_FOUND", f"Unknown application {tenant_app_id!r}.", 404)
    latest = revisions[0]
    if (
        isinstance(body.get("org"), str) and isinstance(body.get("repo"), str)
        and body.get("org") and body.get("repo")
    ):
        org, repo = body["org"], body["repo"]
    else:
        repo_ref = body.get("repo", "")
        if not isinstance(repo_ref, str) or not repo_ref:
            raise _err("GENERATE_BINDING_INVALID", "repo is required (github.com/org/repo).", 400)
        from investigation_agent_platform.integrations.profile_gen.identity import (
            parse_repo_ref as _parse_repo_ref,
        )

        try:
            org, repo = _parse_repo_ref(repo_ref)
        except ValueError as exc:
            raise _err("GENERATE_BINDING_INVALID", str(exc), 400) from exc
    for value, label in ((org, "org"), (repo, "repo")):
        if not isinstance(value, str) or not value or len(value) > 128 or "/" in value or ".." in value:
            raise _err("GENERATE_BINDING_INVALID", f"{label} is required (max 128, no slashes).", 400)
    branch = body.get("branch", "") or "main"
    spring = body.get("spring")
    operator = body.get("operator", {})
    if not isinstance(operator, dict):
        raise _err("GENERATE_BINDING_INVALID", "operator must be an object.", 400)
    try:
        fresh, _report = await __import__("asyncio").to_thread(
            _generate_profile_from_repo,
            x_tenant_id,
            tenant_app_id,
            org,
            repo,
            branch,
            spring if isinstance(spring, dict) else None,
            {},
        )
    except ApiError:
        raise
    except Exception as exc:
        raise _err("GENERATE_FAILED", f"Profile re-derivation failed: {str(exc)[:200]}", 500) from exc
    from investigation_agent_platform.integrations.profile_gen.assembler import (
        refresh_profile as _refresh_merge,
    )

    merged_dict = _refresh_merge(
        latest.model_dump(mode="json", by_alias=True),
        fresh.model_dump(mode="json", by_alias=True),
        operator,
    )
    try:
        from investigation_agent_platform.domain.profile.models import (
            ApplicationProfile as _ApplicationProfile,
        )

        merged = _ApplicationProfile.model_validate(merged_dict)
    except ValidationError as exc:
        raise _err(
            "GENERATE_PROFILE_INVALID",
            f"Merged profile failed validation: {exc.errors()[:2]}",
            500,
        ) from exc
    diff = _diff_dicts(
        latest.model_dump(mode="json", by_alias=True),
        merged.model_dump(mode="json", by_alias=True),
    )
    if not diff:
        return GeneratedRefreshView(changed=False, version=latest.version)
    stamped = merged.model_copy(update={"version": latest.version + 1})
    await ctx.profile_repo.save(x_tenant_id, stamped)
    return GeneratedRefreshView(
        changed=True,
        version=stamped.version,
        diff=diff,
        profile=ProfileView.from_domain(stamped),
        warnings=_derive_warnings(stamped),
    )


@router.get(
    "/profiles/{app_id}:spring-drift",
    response_model=SpringDriftView,
    response_model_by_alias=True,
    response_model_exclude_unset=True,
    responses=COMMON_ERROR_RESPONSES,
)
async def spring_drift(
    app_id: str,
    x_tenant_id: str = Depends(require_tenant),
) -> SpringDriftView:
    """Drift preview against the stored binding (no write)."""
    ctx = get_app_context()
    latest, fresh, diff = await _rederive_latest(ctx, x_tenant_id, app_id)
    report = fresh.derivation
    return SpringDriftView(
        changed=bool(diff),
        version=latest.version,
        diff=diff,
        needs_review=list(report.needs_review) if report else [],
        warnings=_derive_warnings(fresh),
    )


@router.get(
    "/profiles/{app_id}",
    response_model=ProfileDetail,    response_model_by_alias=True,
    response_model_exclude_unset=True,
    responses=COMMON_ERROR_RESPONSES,
)
async def get_profile(
    app_id: str,
    x_tenant_id: str = Depends(require_tenant),
    version: int | None = Query(default=None),
) -> ProfileDetail:
    """Latest revision by default (any status — DRAFTs are retrievable); ``?version=N`` pins exactly (no fallback)."""
    ctx = get_app_context()
    if version is not None:
        profile = await ctx.profile_repo.get_by_application_id(
            x_tenant_id, app_id, version=version
        )
        if profile is None:
            raise _err("PROFILE_NOT_FOUND", f"No revision {version} for {app_id!r}.", 404)
        return ProfileDetail(profile=ProfileView.from_domain(profile), version=profile.version)
    revisions = await ctx.profile_repo.list_revisions(x_tenant_id, app_id)
    if not revisions:
        raise _err("PROFILE_NOT_FOUND", f"Unknown application {app_id!r}.", 404)
    latest = revisions[0]
    return ProfileDetail(profile=ProfileView.from_domain(latest), version=latest.version)


@router.get(
    "/profiles/{app_id}/revisions",
    response_model=ListEnvelope[ProfileRevisionView],
    response_model_by_alias=True,
    response_model_exclude_unset=True,
    responses=COMMON_ERROR_RESPONSES,
)
async def list_profile_revisions(
    app_id: str,
    x_tenant_id: str = Depends(require_tenant),
) -> ListEnvelope[ProfileRevisionView]:
    """Revision summaries newest-first (full bodies via GET with ?version=)."""
    ctx = get_app_context()
    revisions = await ctx.profile_repo.list_revisions(x_tenant_id, app_id)
    if not revisions:
        raise _err("PROFILE_NOT_FOUND", f"Unknown application {app_id!r}.", 404)
    items = [
        ProfileRevisionView(
            id=r.id,
            version=r.version,
            status=r.status.value,
            name=r.name,
            environment=r.environment,
        )
        for r in revisions
    ]
    return ListEnvelope[ProfileRevisionView](items=items, total=len(items))


@router.put(
    "/profiles/{app_id}",
    response_model=ProfileDetail,
    response_model_by_alias=True,
    response_model_exclude_unset=True,
    responses=COMMON_ERROR_RESPONSES,
)
async def update_profile(
    app_id: str,
    body: dict[str, Any],
    response: Response,
    x_tenant_id: str = Depends(require_tenant),
    x_idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
) -> ProfileDetail:
    """Full replacement → latest+1. Transitions enforced; body version must be absent or latest+1."""
    ctx = get_app_context()
    revisions = await ctx.profile_repo.list_revisions(x_tenant_id, app_id)
    if not revisions:
        raise _err("PROFILE_NOT_FOUND", f"Unknown application {app_id!r}.", 404)
    latest = revisions[0]
    profile = _parse_body(x_tenant_id, body)
    if profile.id != app_id:
        raise _err("PROFILE_ID_MISMATCH", "Body id does not match path application id.", 400)
    expected = latest.version + 1
    if "version" in body and profile.version != expected:
        raise _err(
            "PROFILE_VERSION_CONFLICT",
            f"Body version {profile.version} does not match expected {expected}.",
            409,
        )
    if not check_status_transition(latest.status.value, profile.status.value):
        raise _err(
            "PROFILE_TRANSITION_FORBIDDEN",
            f"Transition {latest.status.value} -> {profile.status.value} is forbidden.",
            409,
        )
    _check_semantics(profile)
    if profile.status == ProfileStatus.ACTIVE and latest.status != ProfileStatus.ACTIVE:
        _check_promotable(profile)
    # Template-drop guard: GET returns the sanitized view (query templates
    # stripped), so PUTing a GET response back would silently delete every
    # template. Refuse ambiguous drops; explicit confirmation opts out.
    dropped_templates = sorted(
        set(latest.query_template_index()) - set(profile.query_template_index())
    )
    if dropped_templates and body.get("confirm_template_removal") is not True:
        raise _err(
            "PROFILE_TEMPLATES_DROPPED",
            f"Update drops query templates {dropped_templates[:5]}; resubmit with "
            '"confirm_template_removal": true to confirm.',
            409,
        )

    async def _produce() -> dict[str, Any]:
        # Idempotent replay: same content already written at expected version.
        current = await ctx.profile_repo.get_by_application_id(
            x_tenant_id, app_id, version=expected
        )
        stamped = profile.model_copy(update={"version": expected})
        if current is not None:
            if current.model_dump(mode="json", by_alias=True) == stamped.model_dump(
                mode="json", by_alias=True
            ):
                response.status_code = status.HTTP_200_OK
                return _replay_payload(current, expected)
            raise _err(
                "PROFILE_VERSION_CONFLICT",
                f"Revision {expected} already exists with different content.",
                409,
            )
        await ctx.profile_repo.save(x_tenant_id, stamped)
        return _detail_payload(stamped, expected)

    return ProfileDetail.model_validate(
        await _with_idempotency(
            ctx, x_tenant_id, x_idempotency_key, body, _produce, response, created_code=200
        )
    )


@router.delete(
    "/profiles/{app_id}",
    response_model=ProfileDeleteAck,
    response_model_by_alias=True,
    response_model_exclude_unset=True,
    responses=COMMON_ERROR_RESPONSES,
)
async def delete_profile(
    app_id: str,
    response: Response,
    x_tenant_id: str = Depends(require_tenant),
    version: int | None = Query(default=None),
    x_idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
) -> ProfileDeleteAck:
    """Delete DRAFT profile data. Safe because DRAFT revisions are never resolved
    by investigations (which pin ACTIVE) and never served as latest-ACTIVE.

    - No `?version=`: deletes the whole application, allowed only when EVERY
      revision is DRAFT (any ACTIVE/DEPRECATED/ARCHIVED revision → 409, nothing
      deleted — non-DRAFT history is the immutable audit trail).
    - `?version=N`: deletes that single revision, allowed only when it is DRAFT
      (missing → 404, non-DRAFT → 409). Used to discard a bad derived/refresh
      DRAFT sitting atop an ACTIVE revision; the previous revision becomes
      latest again.
    """
    ctx = get_app_context()

    async def _produce() -> dict[str, Any]:
        if version is None:
            revisions = await ctx.profile_repo.list_revisions(x_tenant_id, app_id)
            if not revisions:
                raise _err("PROFILE_NOT_FOUND", f"Unknown application {app_id!r}.", 404)
            retired = [r for r in revisions if r.status != ProfileStatus.DRAFT]
            if retired:
                raise _err(
                    "PROFILE_NOT_DELETABLE",
                    f"Application {app_id!r} has non-DRAFT history; only DRAFT-only profiles can be deleted.",
                    409,
                )
            deleted = await ctx.profile_repo.delete(x_tenant_id, app_id)
            return ProfileDeleteAck(id=app_id, version=None, deleted=deleted).model_dump(
                mode="json", by_alias=True, exclude_unset=True
            )
        revision = await ctx.profile_repo.get_by_application_id(
            x_tenant_id, app_id, version=version
        )
        if revision is None:
            raise _err("PROFILE_NOT_FOUND", f"No revision {version} for {app_id!r}.", 404)
        if revision.status != ProfileStatus.DRAFT:
            raise _err(
                "PROFILE_NOT_DELETABLE",
                f"Revision {version} of {app_id!r} is {revision.status.value}; only DRAFT revisions can be deleted.",
                409,
            )
        deleted = await ctx.profile_repo.delete(x_tenant_id, app_id, version=version)
        return ProfileDeleteAck(id=app_id, version=version, deleted=deleted).model_dump(
            mode="json", by_alias=True, exclude_unset=True
        )

    return ProfileDeleteAck.model_validate(
        await _with_idempotency(
            ctx,
            x_tenant_id,
            x_idempotency_key,
            {"app_id": app_id, "version": version},
            _produce,
            response,
            created_code=200,
        )
    )


__all__ = ["_known_mapping_ids", "router"]
