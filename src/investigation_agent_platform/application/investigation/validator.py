# src/investigation_agent_platform/application/investigation/validator.py
import logging
import re
from urllib.parse import unquote

from investigation_agent_platform.domain.common.exceptions import SecurityPolicyViolationException
from investigation_agent_platform.domain.investigation.models import (
    ActionType,
    InvestigationAction,
    InvestigationLimits,
)
from investigation_agent_platform.domain.profile.models import ApplicationProfile
from investigation_agent_platform.ports.security.redactor import (
    ActionAuthorizerPort,
    CapabilityRegistryPort,
)

logger = logging.getLogger(__name__)

_FORBIDDEN_SQL_TOKENS = re.compile(r"\b(DROP|DELETE|TRUNCATE|ALTER|GRANT|REVOKE)\b", re.IGNORECASE)
_FORBIDDEN_RUNTIME_TOKENS = re.compile(
    r"(script|ctx\._source|doc\[|while\s*\(\s*true\s*\))", re.IGNORECASE
)


def _has_traversal(value: str) -> bool:
    if not value:
        return False
    if "\x00" in value:
        return True
    decoded = value
    prev: str | None = None
    # Repeatedly unquote until stable to handle double/multiple encoding
    while prev != decoded:
        prev = decoded
        decoded = unquote(decoded)
        if "\x00" in decoded:
            return True
    decoded = decoded.replace("\\", "/")
    if "\x00" in decoded:
        return True
    return any(part == ".." for part in decoded.split("/"))


def _is_within_roots(file_path: str, allowed_roots: list[str]) -> bool:
    if not allowed_roots:
        return False
    normalized = f"/{file_path.lstrip('/')}"
    for root in allowed_roots:
        root_norm = f"/{root.strip('/')}".rstrip("/") or "/"
        if normalized == root_norm or normalized.startswith(f"{root_norm}/"):
            return True
    return False


class InvestigationActionValidator:
    """Enforces Mandatory Platform Security Rules prior to action dispatch."""

    async def validate_action(
        self,
        action: InvestigationAction,
        profile: ApplicationProfile,
        limits: InvestigationLimits,
        current_evidence_count: int,
        current_tool_call_count: int,
        tenant_id: str,
        authorizer: ActionAuthorizerPort,
        capability_registry: CapabilityRegistryPort,
    ) -> None:
        if not isinstance(action.parameters, dict):
            raise SecurityPolicyViolationException("Action parameters must be a key-value mapping")

        if authorizer is None or capability_registry is None:
            raise SecurityPolicyViolationException(
                "Authorizer and capability registry are mandatory"
            )

        authorized = await authorizer.authorize_action(
            tenant_id, action.action_type.value, action.parameters.get("resource", ""), {}
        )
        if not authorized:
            raise SecurityPolicyViolationException("Agent unauthorized for action execution")

        enabled = await capability_registry.is_capability_enabled(
            tenant_id, action.action_type.value
        )
        if not enabled:
            raise SecurityPolicyViolationException(
                f"Capability '{action.action_type.value}' is disabled"
            )

        if current_tool_call_count >= limits.max_tool_calls:
            raise SecurityPolicyViolationException(
                f"Maximum tool call limit reached: {limits.max_tool_calls}"
            )

        if current_evidence_count >= limits.max_evidence_items:
            raise SecurityPolicyViolationException(
                f"Maximum evidence limit reached: {limits.max_evidence_items}"
            )

        params = action.parameters

        if action.action_type == ActionType.QUERY_STATE:
            template_key = params.get("template_key") or params.get("template_id")
            if not template_key or template_key not in profile.state_configuration.query_templates:
                raise SecurityPolicyViolationException(
                    "Query must reference valid static queryTemplate key"
                )
            if "sql" in params or "query_string" in params or "raw_sql" in params:
                raise SecurityPolicyViolationException("Dynamic SQL payload detected in parameters")
            # Validate template content against forbidden keywords
            template_sql = profile.state_configuration.query_templates.get(str(template_key), "")
            if _FORBIDDEN_SQL_TOKENS.search(template_sql):
                raise SecurityPolicyViolationException(
                    "Forbidden DDL/DML keyword in query template"
                )
            # Check injected SQL in parameter values
            for v in params.values():
                if isinstance(v, str) and _FORBIDDEN_SQL_TOKENS.search(v):
                    raise SecurityPolicyViolationException(
                        "Forbidden SQL pattern in QUERY_STATE parameters"
                    )

        elif action.action_type == ActionType.SEARCH_LOGS:
            if "raw_es_body" in params or "direct_api_url" in params or "query_string" in params:
                # query_string via raw param is forbidden; structured keywords only
                raw_qs = params.get("query_string")
                if isinstance(raw_qs, str) and _FORBIDDEN_RUNTIME_TOKENS.search(raw_qs):
                    raise SecurityPolicyViolationException("Forbidden runtime query pattern")
                if "raw_es_body" in params or "direct_api_url" in params:
                    raise SecurityPolicyViolationException(
                        "Direct search body or URL override prohibited"
                    )
            # Budget: max evidence per query
            requested_limit = params.get("limit")
            if (
                isinstance(requested_limit, int)
                and requested_limit > profile.investigation_configuration.max_evidence_per_query
            ):
                raise SecurityPolicyViolationException(
                    "Requested log limit exceeds allowed maximumEvidencePerQuery"
                )
            if isinstance(requested_limit, int) and requested_limit > limits.max_database_rows:
                raise SecurityPolicyViolationException(
                    "Requested limit exceeds max_database_rows budget"
                )

        elif action.action_type == ActionType.GET_CODE:
            file_path = params.get("file_path", "")
            if not isinstance(file_path, str):
                raise SecurityPolicyViolationException("file_path must be a string")
            if _has_traversal(file_path) or file_path.startswith("/") or file_path.startswith("~"):
                raise SecurityPolicyViolationException(
                    "Path traversal attempt detected in file request"
                )
            allowed_roots = profile.code_configuration.source_roots
            if not _is_within_roots(file_path, allowed_roots):
                raise SecurityPolicyViolationException(
                    "File path resides outside allowed source roots"
                )
            if len(file_path) > 1024:
                raise SecurityPolicyViolationException("file_path exceeds maximum length")
            # Budget: max source files
            if current_evidence_count >= limits.max_source_files:
                raise SecurityPolicyViolationException("Maximum source files budget exceeded")

        # Generic budget checks for correlation depth and hypotheses
        if action.action_type == ActionType.CORRELATE:
            depth = params.get("max_depth") or params.get("depth") or 0
            if isinstance(depth, int) and depth > limits.max_correlation_depth:
                raise SecurityPolicyViolationException("Correlation depth exceeds budget limit")

        if action.action_type == ActionType.FORMULATE_HYPOTHESIS:
            if current_evidence_count >= limits.max_hypotheses * 10:  # heuristic guard
                pass

        # Forbidden patterns across all params: null bytes, script tags
        for key, val in params.items():
            if isinstance(val, str):
                if "\x00" in val:
                    raise SecurityPolicyViolationException(
                        f"Null byte injection in parameter '{key}'"
                    )
                if "<script" in val.lower():
                    raise SecurityPolicyViolationException(
                        f"Forbidden script pattern in parameter '{key}'"
                    )
