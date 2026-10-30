# src/investigation_agent_platform/application/onboarding/telemetry_verification.py
"""Live verification for per-tenant telemetry routing (DES-tenant-telemetry-config).

Platform home for the checks that prove a tenant's telemetry declaration
actually serves: routed search returns rows through the home endpoint,
service filters constrain, unhomed/unknown contexts fail closed loud and
typed, and GET-fetch resolves the home endpoint from the stored index.

Both onboarding surfaces invoke this one implementation: the ``onboard``
CLI (``verify-telemetry``, offline against a telemetry dir) and the
onboarding REST route (inline endpoint declarations, no server
filesystem). Drivers stay thin — argparse/HTTP mapping, result printing —
and never reimplement the flow.

Results are plain dataclasses with ``to_dict()`` so CLI ``--json`` and the
REST response share the shape. Secret values never appear in results.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from investigation_agent_platform.application.evidence.selector import (
    EvidenceProviderSelector,
)
from investigation_agent_platform.domain.common.exceptions import (
    ProviderNotConfiguredError,
)
from investigation_agent_platform.domain.evidence.requests import (
    RuntimeEvidenceRequest,
    TimeRange,
)
from investigation_agent_platform.domain.profile.models import ApplicationProfile
from investigation_agent_platform.infrastructure.evidence.runtime.elastic import (
    AsyncElasticAdapter,
    ElasticAdapterSettings,
)
from investigation_agent_platform.infrastructure.evidence.runtime.tenant_telemetry import (
    TenantTelemetryConfig,
    build_tenant_client,
    build_tenant_router,
    load_tenant_telemetry_configs,
)

logger = logging.getLogger(__name__)

DEFAULT_QUERY_TIMEOUT_SECONDS = 25.0
PROBE_WINDOW_DAYS = 6
PROBE_LIMIT = 5
#: Repo-relative default the CLI uses when the caller does not supply a
#: mapping directory. The service itself never assumes a CWD: callers may
#: pass a built registry or an explicit path instead.
DEFAULT_MAPPING_PROFILES_PATH = "config/observability-mappings"


@dataclass(frozen=True)
class TelemetryCheckResult:
    """One named verification outcome (JSON-serializable, no secrets)."""

    name: str
    passed: bool
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "passed": self.passed, "detail": self.detail}


@dataclass
class _RecordingClient:
    """Delegates to a real client while recording search kwargs (index audit)."""

    real: Any
    searches: list[dict[str, Any]] = field(default_factory=list)

    async def search(self, *args: Any, **kwargs: Any) -> Any:
        self.searches.append(kwargs or {"args": args})
        return await self.real.search(*args, **kwargs)

    def options(self, **kwargs: Any) -> _RecordingClient:
        """Mirror client.options(): shared transport, shared record list."""
        return _RecordingClient(self.real.options(**kwargs), self.searches)

    async def close(self) -> None:
        await self.real.close()


def _probe_profile(tenant_id: str, app_id: str) -> ApplicationProfile:
    return ApplicationProfile(
        id=app_id,
        tenant_id=tenant_id,
        name=app_id,
        description="telemetry verification probe",
        environment="production",
    )


def _probe_request(**overrides: Any) -> RuntimeEvidenceRequest:
    now = datetime.now(UTC)
    kwargs: dict[str, Any] = {
        "environment": "production",
        "time_range": TimeRange(start_time=now - timedelta(days=PROBE_WINDOW_DAYS), end_time=now),
        "limit": PROBE_LIMIT,
    }
    kwargs.update(overrides)
    return RuntimeEvidenceRequest(**kwargs)


class _EvidenceRow:
    def __init__(
        self, tenant_id: str, investigation_id: UUID, evidence_id: UUID, source: str
    ) -> None:
        self.tenant_id = tenant_id
        self.investigation_id = investigation_id
        self.evidence_id = evidence_id
        self.provider = "ELASTIC"
        self.source = source


class _EvidenceLookup:
    def __init__(self, row: _EvidenceRow) -> None:
        self._row = row

    async def get_evidence(self, tenant_id: str, evidence_id: UUID) -> _EvidenceRow:
        return self._row


async def _run_search_checks(
    *,
    adapter: Any,
    tenant_id: str,
    home_service: str,
    expected_endpoint: str,
    expected_pattern: str,
    recorded: list[_RecordingClient],
    record: Any,
    mapping_source_id: str | None = None,
) -> Any | None:
    """Search-dependent checks; returns the probe result (None on failure)."""
    try:
        result = await adapter.search_runtime_evidence(
            tenant_id,
            uuid.uuid4(),
            _probe_request(mapping_source_id=mapping_source_id),
            _probe_profile(tenant_id, home_service),
        )
    except Exception as exc:
        record("routed search returns rows", False, f"{type(exc).__name__}: {exc}")
        record("endpoint attributed", True, "SKIP: search did not return")
        record("home pattern used", True, "SKIP: search did not return")
        record("service filter returns rows", True, "SKIP: search did not return")
        return None
    record("routed search returns rows", len(result.items) > 0, f"items={len(result.items)}")
    record(
        "endpoint attributed",
        result.execution_metadata.get("endpoint") == expected_endpoint,
        str(result.execution_metadata.get("endpoint")),
    )
    indexes = [s.get("index") for c in recorded for s in c.searches]
    record(
        "home pattern used",
        bool(indexes) and all(i == expected_pattern for i in indexes),
        str(indexes),
    )

    seen_service: str | None = None
    for item in result.items:
        try:
            seen_service = json.loads(item.content_snippet or "{}").get("service")
        except ValueError:
            seen_service = None
        if seen_service:
            break
    if seen_service:
        for c in recorded:
            c.searches.clear()
        filtered = await adapter.search_runtime_evidence(
            tenant_id,
            uuid.uuid4(),
            _probe_request(services=[seen_service], mapping_source_id=mapping_source_id),
            _probe_profile(tenant_id, home_service),
        )
        record(
            "service filter returns rows",
            len(filtered.items) > 0,
            f"service={seen_service} items={len(filtered.items)}",
        )
    else:
        record("service filter returns rows", True, "SKIP: rows carry no service attribution")
    return result


async def verify_tenant_telemetry(
    *,
    configs: dict[str, TenantTelemetryConfig],
    tenant_id: str,
    home_service: str,
    client_factory: Callable[[Any], Any] | None = None,
    query_timeout_seconds: float = DEFAULT_QUERY_TIMEOUT_SECONDS,
    cursor_signing_key: bytes | None = None,
    mapping_source_id: str | None = None,
    mapping_registry: Any | None = None,
    mapping_profiles_path: str | None = None,
) -> list[TelemetryCheckResult]:
    """Run the live verification flow over loaded tenant configs.

    ``client_factory`` defaults to the production shared factory (the same
    one bootstrap uses); tests inject fakes. Every check that touches the
    network performs real provider I/O — there is no dry-run mode because
    a dry run cannot prove the deployment serves.
    """
    results: list[TelemetryCheckResult] = []

    def record(name: str, passed: bool, detail: str = "") -> None:
        results.append(TelemetryCheckResult(name=name, passed=passed, detail=detail))

    factory = client_factory or build_tenant_client
    record("config loads", tenant_id in configs, f"tenants={sorted(configs)}")
    if tenant_id not in configs:
        return results
    home = configs[tenant_id].endpoint_for(home_service)
    record("home endpoint homing service", home is not None, home_service)
    if home is None:
        return results
    expected_pattern, expected_endpoint = home.pattern, home.name

    recorded: list[_RecordingClient] = []

    def recording_factory(endpoint: Any) -> Any:
        rec = _RecordingClient(factory(endpoint))
        recorded.append(rec)
        return rec

    router = build_tenant_router(
        configs, recording_factory, query_timeout_seconds=query_timeout_seconds
    )
    assert router is not None
    record("router built", router.has_tenant(tenant_id))

    if mapping_source_id is not None and mapping_registry is None:
        if not mapping_profiles_path:
            raise ValueError(
                "mapping_source_id requires a mapping_registry or mapping_profiles_path."
            )
        from investigation_agent_platform.infrastructure.configuration.mapping_registry import (
            load_mapping_profiles,
        )

        mapping_registry = load_mapping_profiles(mapping_profiles_path)

    adapter = AsyncElasticAdapter(
        client=None,  # tenant-only mode: no global deployment
        settings=ElasticAdapterSettings(cursor_signing_key=cursor_signing_key or os.urandom(32)),
        tenant_router=router,
        mapping_registry=mapping_registry,
    )

    selector = EvidenceProviderSelector()
    selector.register_runtime_provider("elastic-primary", adapter, default=True)
    resolved = await selector.get_runtime_provider(tenant_id, home_service, "production")
    record("selector default route serves tenant", resolved is adapter)

    # Staged configure-time validation (DES §4: reachable → auth → pattern
    # → sanity read). Each stage names itself so the next failure says
    # WHERE it broke, not just that the search died.
    try:
        info_raw = await asyncio.wait_for(
            recorded[0].real.info(), timeout=min(10.0, float(home.timeout_seconds))
        )
        info = getattr(info_raw, "body", info_raw)
        cluster = info.get("cluster_name", "?") if isinstance(info, dict) else "?"
        record("endpoint reachable and authenticated", True, f"cluster={cluster}")
    except Exception as exc:
        record("endpoint reachable and authenticated", False, f"{type(exc).__name__}: {exc}")
        record("pattern resolves to indices", True, "SKIP: endpoint not reached")
        record("routed search returns rows", True, "SKIP: endpoint not reached")
        record("endpoint attributed", True, "SKIP: endpoint not reached")
        record("home pattern used", True, "SKIP: endpoint not reached")
        record("service filter returns rows", True, "SKIP: endpoint not reached")
    else:
        try:
            resolved_raw = await asyncio.wait_for(
                recorded[0].real.indices.get(index=expected_pattern),
                timeout=min(30.0, float(home.timeout_seconds)),
            )
            resolved_indices = getattr(resolved_raw, "body", resolved_raw)
            names = sorted(resolved_indices.keys()) if isinstance(resolved_indices, dict) else []
        except Exception as exc:
            record("pattern resolves to indices", False, f"{type(exc).__name__}: {exc}")
            names = []
        else:
            record(
                "pattern resolves to indices",
                len(names) > 0,
                f"indices={len(names)} e.g. {names[:3]}",
            )
        await _run_search_checks(
            adapter=adapter,
            tenant_id=tenant_id,
            home_service=home_service,
            expected_endpoint=expected_endpoint,
            expected_pattern=expected_pattern,
            recorded=recorded,
            record=record,
            mapping_source_id=mapping_source_id,
        )

    try:
        await adapter.search_runtime_evidence(
            tenant_id,
            uuid.uuid4(),
            _probe_request(),
            _probe_profile(tenant_id, "no-such-service"),
        )
        record("unhomed service fails closed", False, "no error raised")
    except ProviderNotConfiguredError as exc:
        record("unhomed service fails closed", True, str(exc)[:120])
    try:
        await adapter.search_runtime_evidence(
            "tenant-unknown",
            uuid.uuid4(),
            _probe_request(),
            _probe_profile(tenant_id, home_service),
        )
        record("unknown tenant fails closed", False, "no error raised")
    except ProviderNotConfiguredError as exc:
        record("unknown tenant fails closed", True, str(exc)[:120])

    raw: Any = None
    try:
        # Mirror the production fetch path: fetch something the routed
        # search demonstrably read. The raw lookup shares the probe's time
        # window so it lands on the same recent shards — not on an
        # arbitrary (possibly unreadable) index elsewhere in the pattern.
        window_start = (datetime.now(UTC) - timedelta(days=PROBE_WINDOW_DAYS)).isoformat()
        raw = await recorded[0].real.search(
            index=expected_pattern,
            body={
                "size": 1,
                "query": {"range": {"@timestamp": {"gte": window_start}}},
            },
        )
    except Exception as exc:
        record("fetch resolves home endpoint", False, f"{type(exc).__name__}: {exc}")
    if raw is not None:
        body = getattr(raw, "body", raw)
        hits = body.get("hits", {}).get("hits", []) if isinstance(body, dict) else []
        if not hits:
            record("fetch resolves home endpoint", False, f"no docs in {expected_pattern}")
        else:
            hit = hits[0]
            iid, eid = uuid.uuid4(), uuid.uuid4()
            row = _EvidenceRow(tenant_id, iid, eid, f"elasticsearch://{hit['_index']}/{hit['_id']}")
            try:
                fetched = await adapter.get_runtime_evidence(
                    tenant_id, iid, eid, "production", _EvidenceLookup(row)
                )
            except Exception as exc:
                record(
                    "fetch resolves home endpoint",
                    False,
                    f"index={hit['_index']} {type(exc).__name__}: {exc}",
                )
            else:
                record(
                    "fetch resolves home endpoint", fetched is not None, f"index={hit['_index']}"
                )

    for c in recorded:
        await c.close()
    return results


async def verify_tenant_telemetry_from_dir(
    *,
    telemetry_dir: str,
    tenant_id: str,
    home_service: str,
    client_factory: Callable[[Any], Any] | None = None,
    mapping_source_id: str | None = None,
    mapping_registry: Any | None = None,
    mapping_profiles_path: str | None = None,
) -> list[TelemetryCheckResult]:
    """Dir-based entry: load ``<dir>/<tenant>/telemetry.conf`` then verify.

    Loader failures (malformed file, unknown env overrides) propagate as
    ``PlatformConfigurationError`` — a half-configured evidence layer must
    never verify green.
    """
    configs = load_tenant_telemetry_configs(telemetry_dir)
    return await verify_tenant_telemetry(
        configs=configs,
        tenant_id=tenant_id,
        home_service=home_service,
        client_factory=client_factory,
        mapping_source_id=mapping_source_id,
        mapping_registry=mapping_registry,
        mapping_profiles_path=mapping_profiles_path,
    )
