# src/investigation_agent_platform/integrations/spring_config/security.py
"""Property allowlisting, sensitivity redaction, and tenant import allowlists.

Defense in depth: the tenant/app/profile allowlist gates *before* any I/O,
the property allowlist selects what survives the merge, and the sensitivity
matcher drops credential-shaped keys *after* allowlisting. Only key names
ever reach logs, reports, or audit rows — never values.
"""

from __future__ import annotations

import fnmatch
import os
import re

# Conservative default: the Spring namespaces derivation understands.
# Operators extend (never narrow) via IAP_SPRING_EXTRA_PROPERTY_PATTERNS.
DEFAULT_ALLOWED_PROPERTY_PATTERNS: tuple[str, ...] = (
    "spring.application.*",
    "spring.profiles.active",
    "spring.datasource.url",
    "spring.datasource.hikari.*",
    "spring.jpa.*",
    "spring.data.redis.*",
    "spring.data.mongodb.*",
    "spring.mongodb.*",
    "spring.cassandra.*",
    "spring.kafka.*",
    "spring.rabbitmq.*",
    "spring.dynamodb.*",
    "spring.cloud.config.server.git.uri",
    "spring.cloud.config.server.git.search-paths",
    "spring.cloud.discovery.*",
    "spring.cloud.gateway.routes[*]",
    "server.port",
    "management.*",
    "logging.level.*",
    "logging.logstash.*",
    "otel.*",
    "eureka.*",
    "info.*",
    "build.*",
    "cloud.aws.s3.bucket",
    "aws.s3.*",
)

_SENSITIVE_RE = re.compile(
    r"password|passwd|secret|token|credential|api[-_.]?key|"
    r"private[-_.]?key|client[-_.]?secret|"
    r"access[-_.]?key|secretkey|authorization|"
    r"keystore|truststore|"
    r"spring\.datasource\.password|"
    r"spring\.rabbitmq\..*password|"
    r"cloud\.aws\.credentials|aws\.secret",
    re.IGNORECASE,
)


def looks_sensitive(key: str) -> bool:
    return bool(_SENSITIVE_RE.search(key))


def is_allowed_property(key: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatchcase(key, pattern) for pattern in patterns)


def extra_property_patterns() -> tuple[str, ...]:
    raw = os.environ.get("IAP_SPRING_EXTRA_PROPERTY_PATTERNS", "")
    return tuple(p.strip() for p in raw.split(",") if p.strip())


def allowed_property_patterns() -> tuple[str, ...]:
    return DEFAULT_ALLOWED_PROPERTY_PATTERNS + extra_property_patterns()


def _csv_env(name: str) -> frozenset[str]:
    return frozenset(v.strip() for v in os.environ.get(name, "").split(",") if v.strip())


def allowed_spring_applications() -> frozenset[str]:
    return _csv_env("IAP_SPRING_ALLOWED_APPLICATIONS")


def allowed_spring_profiles() -> frozenset[str]:
    return _csv_env("IAP_SPRING_ALLOWED_PROFILES")


def allowed_server_urls() -> tuple[str, ...]:
    """Exact-prefix allowlist for Config Server URLs (SSRF guard).

    Empty means nothing is fetchable — fail closed, never open.
    """
    raw = os.environ.get("IAP_SPRING_ALLOWED_SERVER_URLS", "")
    return tuple(v.strip().rstrip("/") for v in raw.split(",") if v.strip())


def oauth_settings() -> dict[str, str]:
    return {
        "token_url": os.environ.get("IAP_SPRING_OAUTH_TOKEN_URL", ""),
        "client_id": os.environ.get("IAP_SPRING_OAUTH_CLIENT_ID", ""),
        "client_secret": os.environ.get("IAP_SPRING_OAUTH_CLIENT_SECRET", ""),
        "scope": os.environ.get("IAP_SPRING_OAUTH_SCOPE", ""),
    }
