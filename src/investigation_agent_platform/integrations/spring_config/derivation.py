# src/investigation_agent_platform/integrations/spring_config/derivation.py
"""Spring properties → DRAFT profile derivation (appl-profile 1.0 §10.2).

Pure function: no I/O, no secrets (values are never persisted — only key
names reach the report). Every rule is tagged observed (direct read) or
inferred (heuristic, flagged in ``needs_review``). Derivation output is
ALWAYS ``status=DRAFT``; promotion requires operator completion.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from investigation_agent_platform.domain.profile.models import (
    ApplicationProfile,
    Datasource,
    DatasourceKind,
    DatasourceRole,
    DerivationReport,
    DynamoDBDatasourceSpec,
    KafkaDatasourceSpec,
    ProfileSource,
    ProfileStatus,
    RedisDatasourceSpec,
    S3DatasourceSpec,
    Service,
    ServiceCode,
    ServiceDependency,
    ServiceTelemetry,
)
from investigation_agent_platform.integrations.spring_config.models import (
    ConfigResult,
    SpringBinding,
)
from investigation_agent_platform.integrations.spring_config.security import looks_sensitive

_JDBC_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("postgres", re.compile(r"^jdbc:postgresql://([^/:?]+)(?::\d+)?/([^?;]+)")),
    ("oracle", re.compile(r"^jdbc:oracle:thin:@(?://)?([^/:]+)(?::\d+)?[:/]([^?;]+)")),
    ("mysql", re.compile(r"^jdbc:mysql://([^/:?]+)(?::\d+)?/([^?;]+)")),
    ("sqlserver", re.compile(r"^jdbc:sqlserver://([^/:;]+).*?[Dd]atabase[Nn]ame=([^;]+)")),
)


_CRED_PREFIX_RE = re.compile(
    r"^(?P<prefix>jdbc:[^:]+:(?:thin:)?(?:@(?://)?|//))(?P<creds>[^@/]*@)?"
)


def parse_jdbc_url(url: str) -> tuple[str, str | None, str | None]:
    """Parse a JDBC URL into (kind, host, database). Credentials never returned."""
    text = url.strip()
    # Strip any userinfo (user:pass@) before matching — it is dropped, never stored.
    cred_match = _CRED_PREFIX_RE.match(text)
    if cred_match and cred_match.group("creds"):
        text = text[: cred_match.start("creds")] + text[cred_match.end("creds") :]
    for kind, pattern in _JDBC_PATTERNS:
        match = pattern.match(text)
        if match:
            host, database = match.group(1), match.group(2).strip()
            # Last-resort guard: never let credential-shaped text into the profile.
            if "@" in (host or "") or "@" in (database or ""):
                return kind, None, None
            return kind, host, database
    return "other", None, None


def _str(props: dict[str, Any], key: str) -> str | None:
    value = props.get(key)
    return value if isinstance(value, str) and value.strip() else None


def derive_profile(
    tenant_id: str,
    tenant_app_id: str,
    binding: SpringBinding,
    result: ConfigResult,
) -> tuple[ApplicationProfile, DerivationReport]:
    """Derive a DRAFT profile + provenance report from fetched properties."""
    props = dict(result.properties)
    needs_review: list[str] = []
    notes: list[str] = []
    dropped_sensitive: list[str] = []
    unmapped: list[str] = []
    consumed: set[str] = set()

    def take(key: str) -> str | None:
        if key in props:
            consumed.add(key)
            return _str(props, key)
        return None

    def take_any(prefix: str) -> dict[str, str]:
        found = {k: v for k, v in props.items() if k.startswith(prefix) and isinstance(v, str)}
        consumed.update(found)
        return found

    now = datetime.now(UTC)

    # -- identity / services (observed) ------------------------------------
    spring_name = take("spring.application.name") or tenant_app_id
    service_names: list[str] = [spring_name]
    otel_service = take("otel.service.name")
    if otel_service and otel_service not in service_names:
        service_names.append(otel_service)  # observed where explicit

    # -- environment --------------------------------------------------------
    active_raw = props.get("spring.profiles.active")
    consumed.add("spring.profiles.active")
    active = (
        active_raw[0]
        if isinstance(active_raw, list) and active_raw
        else (active_raw if isinstance(active_raw, str) else "production")
    )
    active_lower = active.lower()
    if active_lower.startswith("prod"):
        environment, environments = "production", [active] if active != "production" else []
    elif active_lower.startswith("stag"):
        environment, environments = "staging", [active] if active != "staging" else []
    else:
        environment, environments = active[:64], []

    # -- telemetry ----------------------------------------------------------
    trace_backend: str | None = None
    if take("management.zipkin.base-url") or take_any("management.zipkin."):
        trace_backend = "zipkin"  # observed
    if take("otel.exporter.otlp.endpoint") or take_any("otel.exporter."):
        trace_backend = trace_backend or "otlp"  # observed
    take_any("management.tracing.")
    logstash = take_any("logging.logstash.")
    if logstash:
        # Appender props hint at an ELK family, not index names — never invent.
        needs_review.append("services[].telemetry.log_indices")
        notes.append(
            f"{len(logstash)} logging.logstash.* keys present; "
            "index family needs operator confirmation (inferred)."
        )
    for port_key in ("server.port", "management.server.port"):
        if take(port_key):
            notes.append(f"{port_key} present: liveness hint only, not authoritative (inferred).")
    level_keys = take_any("logging.level.")
    if level_keys:
        notes.append(
            f"{len(level_keys)} logging.level.* packages recorded as scoping hints (inferred)."
        )

    # -- datasources ---------------------------------------------------------
    datasources: list[Datasource] = []
    jdbc_url = take("spring.datasource.url")
    if jdbc_url:
        kind_name, host, database = parse_jdbc_url(jdbc_url)
        try:
            kind = DatasourceKind(kind_name)
        except ValueError:
            kind = DatasourceKind.OTHER
        label = f"{database or 'unknown-db'} ({kind.value})"
        if host:
            label += f" @ {host}"
        datasources.append(
            Datasource(
                id="primary",
                name=label[:256],
                kind=kind,
                role=DatasourceRole.STATE,
                description="Observed datasource shape; credentials dropped at fetch.",
                connection_ref="TBD",
            )
        )
        needs_review.append("datasources[primary].connection_ref")
        take_any("spring.datasource.hikari.")
        take_any("spring.jpa.")
        notes.append("spring.jpa/hikari pool hints folded into datasource ceilings (inferred).")

    redis_keys = take_any("spring.data.redis.")
    if redis_keys:
        host = _str(props, "spring.data.redis.host")
        port_raw = props.get("spring.data.redis.port")
        db_raw = props.get("spring.data.redis.database")
        datasources.append(
            Datasource(
                id="redis",
                name="redis (cache)",
                kind=DatasourceKind.REDIS,
                role=DatasourceRole.CACHE,
                connection_ref="TBD",
                redis=RedisDatasourceSpec(
                    host=host,
                    port=int(port_raw) if isinstance(port_raw, int) else None,
                    db=int(db_raw) if isinstance(db_raw, int) else None,
                ),
            )
        )
        needs_review.append("datasources[redis].connection_ref")
    mongo_uri = take("spring.mongodb.uri") or take("spring.data.mongodb.uri")
    if mongo_uri or take_any("spring.data.mongodb.") or take_any("spring.mongodb."):
        datasources.append(
            Datasource(
                id="mongo",
                name="mongo (document store)",
                kind=DatasourceKind.MONGO,
                role=DatasourceRole.STATE,
                connection_ref="TBD",
            )
        )
        needs_review.append("datasources[mongo].connection_ref")
    cassandra = take_any("spring.cassandra.")
    if cassandra:
        keyspace = _str(props, "spring.cassandra.keyspace-name")
        datasources.append(
            Datasource(
                id="cassandra",
                name="cassandra" + (f" ({keyspace})" if keyspace else ""),
                kind=DatasourceKind.CASSANDRA,
                role=DatasourceRole.STATE,
                connection_ref="TBD",
            )
        )
        needs_review.append("datasources[cassandra].connection_ref")
    bootstrap = take("spring.kafka.bootstrap-servers")
    kafka_extra = take_any("spring.kafka.")
    if bootstrap or kafka_extra:
        servers = [s.strip() for s in str(bootstrap or "").split(",") if s.strip()]
        topic = _str(props, "spring.kafka.template.default-topic")
        datasources.append(
            Datasource(
                id="kafka",
                name="kafka (event bus)",
                kind=DatasourceKind.KAFKA,
                role=DatasourceRole.QUEUE,
                connection_ref="TBD",
                kafka=KafkaDatasourceSpec(
                    bootstrap_servers=servers[:10],
                    topics=[topic] if topic else [],
                ),
            )
        )
        needs_review.append("datasources[kafka].connection_ref")
    bucket = take("cloud.aws.s3.bucket") or take("aws.s3.bucket")
    s3_extra = take_any("cloud.aws.s3.") or take_any("aws.s3.")
    if bucket or s3_extra:
        region = _str(props, "cloud.aws.s3.region") or _str(props, "aws.s3.region")
        datasources.append(
            Datasource(
                id="s3",
                name=f"s3 ({bucket})" if bucket else "s3 (object store)",
                kind=DatasourceKind.S3,
                role=DatasourceRole.OBJECT_STORE,
                connection_ref="TBD",
                s3=S3DatasourceSpec(bucket=bucket or "TBD", region=region),
            )
        )
        needs_review.append("datasources[s3].connection_ref")
    dynamo = take_any("spring.dynamodb.")
    if dynamo:
        table = _str(props, "spring.dynamodb.table-name") or "TBD"
        datasources.append(
            Datasource(
                id="dynamodb",
                name=f"dynamodb ({table})",
                kind=DatasourceKind.DYNAMODB,
                role=DatasourceRole.STATE,
                connection_ref="TBD",
                dynamodb=DynamoDBDatasourceSpec(
                    table=table, region=_str(props, "spring.dynamodb.region")
                ),
            )
        )
        needs_review.append("datasources[dynamodb].connection_ref")
    rabbit = take_any("spring.rabbitmq.")
    if rabbit:
        datasources.append(
            Datasource(
                id="rabbitmq",
                name="rabbitmq (broker)",
                kind=DatasourceKind.RABBITMQ,
                role=DatasourceRole.QUEUE,
                connection_ref="TBD",
            )
        )
        needs_review.append("datasources[rabbitmq].connection_ref")

    # -- code (inferred only — config repo is never authoritative) -----------
    if take("spring.cloud.config.server.git.uri") or take_any("spring.cloud.config.server.git."):
        needs_review.append("services[].code (config-server git uri is not a service repo)")
        notes.append(
            "spring.cloud.config.server.git.uri seen: config repo != service repos "
            "(inferred, never authoritative)."
        )
    build_info = take_any("info.") or take_any("build.")
    if build_info:
        notes.append("info./build. language/build hints recorded, not applied (inferred).")

    # -- dependencies (inferred candidates, review-flagged) ------------------
    dependencies: list[ServiceDependency] = []
    gateway_routes = take_any("spring.cloud.gateway.routes")
    for key, value in gateway_routes.items():
        if isinstance(value, str) and value.strip():
            dependencies.append(
                ServiceDependency(service=value.strip()[:128], type="edge", critical=False)
            )
    if dependencies:
        needs_review.append("services[].dependencies (gateway-route candidates)")
        notes.append(
            f"{len(dependencies)} gateway-route dependency candidates, critical=false (inferred)."
        )
    if take_any("eureka.client.") or take_any("spring.cloud.discovery."):
        needs_review.append("services[].dependencies (registry entries)")
        notes.append(
            "Discovery/registry entries seen; dependency candidates need review (inferred)."
        )

    # -- leftovers: sensitivity recheck, then unmapped ------------------------
    for key in sorted(props):
        if key in consumed:
            continue
        if looks_sensitive(key):
            dropped_sensitive.append(key)
            continue
        unmapped.append(key)

    service = Service(
        name=spring_name[:128],
        role="edge",
        description=f"DRAFT derived from Spring Config {binding.application}/{binding.profile}.",
        telemetry=ServiceTelemetry(
            log_indices=[],
            service_names=[n[:128] for n in service_names[1:]] or [],
            trace_backend=trace_backend,
        ),
        code=ServiceCode(),
        dependencies=dependencies,
    )
    profile = ApplicationProfile(
        id=tenant_app_id,
        tenant_id=tenant_id,
        name=tenant_app_id,
        description=(
            f"DRAFT derived from Spring Config {binding.application}/{binding.profile}; "
            "complete TBD fields and confirm inferred values before activating."
        ),
        environment=environment,
        environments=environments[:10],
        status=ProfileStatus.DRAFT,
        owner="spring-derivation",
        mapping_source_id="generic-ecs",
        services=[service],
        datasources=datasources,
        source=ProfileSource(
            type="spring_config",
            server_url=binding.server_url,
            spring_application=binding.application,
            spring_profile=binding.profile,
            label=binding.label,
            fetched_at=now,
        ),
        derivation=DerivationReport(
            fetched_at=now,
            property_sources=list(result.property_sources),
            property_count=len(props),
            unmapped_keys=unmapped[:200],
            needs_review=needs_review[:200],
            dropped_sensitive_keys=dropped_sensitive[:200],
            notes=notes[:200],
        ),
    )
    report = profile.derivation
    assert report is not None
    return profile, report
