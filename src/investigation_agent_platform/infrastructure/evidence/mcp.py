# src/investigation_agent_platform/infrastructure/evidence/mcp.py
import asyncio
import hashlib
import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from opentelemetry import trace

from investigation_agent_platform.domain.common.exceptions import (
    ExecutionError,
    SecurityPolicyViolationException,
)
from investigation_agent_platform.domain.evidence.models import (
    ClassificationLevel,
    Evidence,
    EvidenceType,
)
from investigation_agent_platform.domain.evidence.requests import (
    ApplicationStateRequest,
    RuntimeEvidenceRequest,
)
from investigation_agent_platform.domain.profile.models import ApplicationProfile
from investigation_agent_platform.domain.provenance.models import (
    EvidenceFreshness,
    EvidenceProvenance,
    QueryFingerprint,
    SourceLocation,
)
from investigation_agent_platform.infrastructure.observability.tracing import traced
from investigation_agent_platform.ports.evidence.gateway import EvidenceQueryResult

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)

# F-042: broker-level bounds applied to every MCP tool invocation regardless
# of what the caller requested.
MCP_TOOL_TIMEOUT_SECONDS = 20.0
MCP_MAX_ITEMS_PER_CALL = 50
MCP_MAX_SNIPPET_BYTES = 4000
MCP_MAX_ARGUMENT_BYTES = 8192


def _mcp_extra(result: Any) -> dict[str, Any]:
    """Result counts (never row contents)."""
    try:
        items = getattr(result, "items", None)
        if isinstance(items, list):
            return {"iap.items_count": len(items)}
        total = getattr(result, "total_count", None)
        if isinstance(total, int):
            return {"iap.items_count": total}
        return {}
    except Exception:
        return {}


def _expand_env_refs(value: str) -> str:
    """Expand ``${VAR}`` / ``${VAR:-default}`` against the process env.

    Server YAML entries use shell-style mount paths (e.g.
    ``${IAP_ORACLE_MCP_CONNECTIONS_DIR:-${HOME}/...}/connections``); plain
    ``docker run`` never expands them, so the literal spec kills the
    container instantly. Expansion runs here — operator-owned YAML only,
    values flow into docker argv (no shell), unset-without-default stays
    literal so misconfiguration surfaces at the container, not silently.
    """
    import os as _os
    import re as _re

    _pattern = _re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^{}]*))?\}")
    for _ in range(5):
        expanded, count = _pattern.subn(
            lambda m: _os.environ.get(m.group(1), m.group(2) if m.group(2) is not None else m.group(0)),
            value,
        )
        value = expanded
        if not count:
            break
    return value


def _normalize_server_params(server_params: Any) -> Any:
    """Convert a ``{"command","args"}`` mapping to ``StdioServerParameters``.

    Both bootstrap registration and the datasource-registry catalog carry
    plain dicts (YAML-native); ``stdio_client`` needs the typed object.
    Without the optional ``mcp`` package the dict passes through untouched
    so adapter construction (and unit tests) never hard-require it — the
    later session open fails with the package's own clear error instead.
    """
    if isinstance(server_params, dict):
        try:
            from mcp.client.stdio import StdioServerParameters
        except ImportError:
            return server_params
        return StdioServerParameters(
            command=_expand_env_refs(str(server_params.get("command", ""))),
            args=[_expand_env_refs(str(a)) for a in (server_params.get("args", []) or [])],
        )
    return server_params


class McpEvidenceAdapter:
    """Model Context Protocol (MCP) evidence adapter for client/server external tools."""

    def __init__(
        self,
        server_params: Any,
        allowed_tools: list[str],
        tool_timeout_seconds: float = MCP_TOOL_TIMEOUT_SECONDS,
        max_items_per_call: int = MCP_MAX_ITEMS_PER_CALL,
        connection_name: str = "",
        tenant_allowlist: dict[str, set[str]] | None = None,
        # P3-1: pluggable transport + tool mapping. stdio (default) spawns
        # server_params per call; sse connects to a long-lived URL (crystaldba).
        # query_tool/include_connection adapt per server kind. Stateful
        # servers (SQLcl 26.x: `connect` then session-scoped `sql_run`) set
        # connection_tool/connection_arg; single-shot servers (crystaldba)
        # leave connection_tool None and take the connection inline.
        # query_tool/include_connection adapt per server kind (SQLcl run-sql
        # vs crystaldba execute_sql). headers supports static values; oauth
        # (P3-4) resolves per call through credential_provider when set.
        transport: str = "stdio",
        sse_url: str = "",
        sse_headers: dict[str, str] | None = None,
        query_tool: str = "run-sql",
        include_connection: bool = True,
        connection_tool: str | None = None,
        connection_arg: str = "connection_name",
        credential_provider: Any = None,
        # #65: SQL dialect this server speaks (oracle, postgres, ...).
        # None = unknown: participates in every fan-out (backward compatible
        # single-source behavior). Set on multi-store deployments so the
        # gateway only sends each query to dialect-matching servers.
        dialect: str | None = None,
    ) -> None:
        self._server_params = _normalize_server_params(server_params)
        self._allowed_tools: set[str] = set(allowed_tools)
        self._tool_timeout_seconds = tool_timeout_seconds
        self._max_items_per_call = max_items_per_call
        # SQLcl connection name selected per call (e.g. "EXAMPLEDB-conn"). Empty
        # means the server exposes exactly one connection; anything else is
        # resolved per call from the allowlisted set below.
        self._connection_name = connection_name
        # tenant_id -> allowed SQLcl connection names. None = unrestricted
        # (dev only); production entries must name their tenants explicitly.
        self._tenant_allowlist = tenant_allowlist
        if transport not in ("stdio", "sse"):
            raise ValueError(f"Unknown MCP transport {transport!r}")
        self._transport = transport
        self._sse_url = sse_url
        self._sse_headers = dict(sse_headers or {})
        self._query_tool = query_tool
        self._include_connection = include_connection
        self._connection_tool = connection_tool
        self._connection_arg = connection_arg
        self._dialect = str(dialect or "").lower() or None
        self._credential_provider = credential_provider

    @property
    def dialect(self) -> str | None:
        """SQL dialect this server speaks; None exposes to every fan-out."""
        return self._dialect

    async def _resolve_headers(self, tenant_id: str) -> dict[str, str]:
        """Static headers + per-call OAuth token (P3-4 hook)."""
        headers = dict(self._sse_headers)
        provider = self._credential_provider
        if provider is not None:
            try:
                token = await provider.get_token(tenant_id)
            except Exception as exc:
                raise SecurityPolicyViolationException(
                    f"OAuth token acquisition failed: {exc}"
                ) from exc
            if token:
                headers["Authorization"] = f"Bearer {token}"
        return headers

    def _session(self, headers: dict[str, str] | None = None) -> Any:
        """ClientSession context manager for the configured transport."""
        from mcp import ClientSession

        adapter = self
        resolved_headers = dict(headers or {})

        class _Session:
            async def __aenter__(self) -> Any:
                if adapter._transport == "sse":
                    from mcp.client.sse import sse_client

                    if not adapter._sse_url:
                        raise ExecutionError("SSE transport requires sse_url")
                    self._stack_cm = sse_client(adapter._sse_url, headers=resolved_headers)
                    read, write = await self._stack_cm.__aenter__()
                else:
                    from mcp.client.stdio import stdio_client

                    self._stack_cm = stdio_client(adapter._server_params)
                    read, write = await self._stack_cm.__aenter__()
                self._session_cm = ClientSession(read, write)
                session = await self._session_cm.__aenter__()
                await session.initialize()
                return session

            async def __aexit__(self, *exc: object) -> None:
                try:
                    await self._session_cm.__aexit__(*exc)
                finally:
                    await self._stack_cm.__aexit__(*exc)

        return _Session()

    def _validate_tool_call(
        self, tenant_id: str, tool_name: str, arguments: dict[str, Any]
    ) -> None:
        """Strict pre-dispatch validation: allow-list, schema shape, size caps."""
        if tool_name not in self._allowed_tools:
            raise SecurityPolicyViolationException(
                f"MCP Tool '{tool_name}' is not in allowed tools list."
            )
        if not isinstance(arguments, dict):
            raise SecurityPolicyViolationException("MCP tool arguments must be a key-value mapping")
        try:
            import json as _json

            encoded = _json.dumps(arguments, default=str).encode("utf-8")
        except Exception as exc:
            raise SecurityPolicyViolationException(
                f"MCP tool arguments are not serializable: {exc}"
            ) from exc
        if len(encoded) > MCP_MAX_ARGUMENT_BYTES:
            raise SecurityPolicyViolationException(
                f"MCP tool arguments exceed {MCP_MAX_ARGUMENT_BYTES} bytes"
            )
        for key, value in arguments.items():
            if isinstance(value, str) and ("\x00" in value or "<script" in value.lower()):
                raise SecurityPolicyViolationException(
                    f"Forbidden pattern in MCP tool argument '{key}'"
                )

    @traced("provider", component="mcp_adapter")
    async def search_runtime_evidence(
        self,
        tenant_id: str,
        investigation_id: UUID,
        request: RuntimeEvidenceRequest,
        profile: ApplicationProfile,
    ) -> EvidenceQueryResult:
        with tracer.start_as_current_span("McpEvidenceAdapter.search_runtime_evidence") as span:
            span.set_attribute("tenant_id", tenant_id)
            span.set_attribute("investigation_id", str(investigation_id))

            try:
                from mcp import ClientSession  # noqa: F401  (kept: optional-dependency probe)
            except ImportError as exc:
                raise ExecutionError("mcp package is not installed.") from exc

            target_tool = "search_logs"
            arguments = {
                "tenant_id": tenant_id,
                "investigation_id": str(investigation_id),
                "environment": request.environment,
                "query": request.query_string or "",
                "limit": min(request.limit, self._max_items_per_call),
            }
            self._validate_tool_call(tenant_id, target_tool, arguments)

            try:
                async with self._session(await self._resolve_headers(tenant_id)) as session:
                    result = await asyncio.wait_for(
                        session.call_tool(target_tool, arguments=arguments),
                        timeout=self._tool_timeout_seconds,
                    )
                content_items = list(result.content)[: self._max_items_per_call]
                items = [
                    self._map_mcp_result_to_evidence(item, tenant_id, investigation_id)
                    for item in content_items
                ]
                logger.info(
                    "MCP tool invocation completed",
                    extra={
                        "context": {
                            "tenant_id": tenant_id,
                            "investigation_id": str(investigation_id),
                            "tool": target_tool,
                            "items": len(items),
                        }
                    },
                )
                return EvidenceQueryResult(
                    items=items,
                    cursor=None,
                            has_more=False,
                            total_count=len(items),
                        )
            except TimeoutError as exc:
                logger.error(
                    "MCP tool invocation timed out",
                    exc_info=exc,
                    extra={
                        "context": {
                            "tenant_id": tenant_id,
                            "investigation_id": str(investigation_id),
                        }
                    },
                )
                raise ExecutionError(
                    f"MCP invocation timed out after {self._tool_timeout_seconds}s"
                ) from exc
            except Exception as exc:
                logger.error(
                    "MCP tool invocation failed",
                    exc_info=exc,
                    extra={
                        "context": {
                            "tenant_id": tenant_id,
                            "investigation_id": str(investigation_id),
                        }
                    },
                )
                raise ExecutionError(f"MCP invocation failure: {exc}") from exc

    @traced("provider", component="mcp_adapter")
    async def get_application_state(
        self,
        tenant_id: str,
        investigation_id: UUID,
        request: ApplicationStateRequest,
        profile: ApplicationProfile,
    ) -> EvidenceQueryResult:
        """Single-state fetch via the same run-sql path (SQLcl has no point fetch).

        Protocol parity with `search_application_state`; both execute the
        gated SELECT in ``request.parameters``. Kept as a separate entry
        point so the selector's runtime protocol check holds honestly.
        """
        return await self.search_application_state(tenant_id, investigation_id, request, profile)

    @traced("provider", component="mcp_adapter", extra_from=_mcp_extra)
    async def search_application_state(
        self,
        tenant_id: str,
        investigation_id: UUID,
        request: ApplicationStateRequest,
        profile: ApplicationProfile,
    ) -> EvidenceQueryResult:
        """Execute agent-generated read-only SQL via the configured query tool.

        The SQL arrives in ``request.parameters["sql"]`` and passes
        ``QuerySafetyPolicy.validate_generated_sql`` (SELECT/WITH-only) before
        dispatch — the sanctioned dynamic-SQL exception, backstopped by
        read-only grants, timeout/row caps, and full provenance. SQLcl servers
        additionally scope a connection name; single-database servers
        (crystaldba) omit it.
        """
        from investigation_agent_platform.infrastructure.evidence.security import (
            QuerySafetyPolicy,
        )

        raw_sql = request.parameters.get("sql", "")
        if (not isinstance(raw_sql, str) or not raw_sql.strip()) and request.template_id:
            # P3-2: template-based QUERY_STATE (the normal agent path) carries
            # template_id, not raw SQL. Resolve against the profile's declared
            # templates exactly like the direct adapters, so MCP routing is a
            # transport change, not a capability change.
            raw_sql = self._resolve_template_sql(request, profile, tenant_id)
        if not isinstance(raw_sql, str) or not raw_sql.strip():
            raise SecurityPolicyViolationException("MCP state query requires parameters['sql']")
        sql = await QuerySafetyPolicy().validate_generated_sql(tenant_id, raw_sql)
        connection_name = ""
        if self._include_connection:
            # Tenant gating always applies, both dispatch modes.
            connection_name = self._resolve_connection(
                tenant_id, request.parameters.get("connection", "")
            )
        tool = self._query_tool
        if self._connection_tool is not None:
            # Stateful server (SQLcl 26.x): connect once inside this fresh
            # session, then run the session-scoped query tool. The query
            # takes bare SQL; the row cap is enforced client-side below.
            connect_arguments: dict[str, Any] = {self._connection_arg: connection_name}
            query_arguments: dict[str, Any] = {"sql": sql}
            self._validate_tool_call(tenant_id, self._connection_tool, connect_arguments)
            self._validate_tool_call(tenant_id, tool, query_arguments)
        else:
            # Single-shot server: connection travels inline with the query.
            query_arguments = {
                "sql": sql,
                "max_rows": min(int(request.limit), self._max_items_per_call),
            }
            if self._include_connection:
                query_arguments["connection"] = connection_name
            self._validate_tool_call(tenant_id, tool, query_arguments)
        try:
            async with self._session(await self._resolve_headers(tenant_id)) as session:
                if self._connection_tool is not None:
                    await asyncio.wait_for(
                        session.call_tool(self._connection_tool, arguments=connect_arguments),
                        timeout=self._tool_timeout_seconds,
                    )
                    schema_name = self._resolve_schema(request, profile)
                    if schema_name:
                        # Adapter-issued session control (not agent SQL, so
                        # the SELECT-only gate does not apply — the profile
                        # validator already restricted this to identifier
                        # charset). Unqualified tables then resolve under
                        # the declared schema for every call in this session.
                        alter_arguments: dict[str, Any] = {
                            "sql": f"ALTER SESSION SET CURRENT_SCHEMA = {schema_name}"
                        }
                        self._validate_tool_call(tenant_id, tool, alter_arguments)
                        await asyncio.wait_for(
                            session.call_tool(tool, arguments=alter_arguments),
                            timeout=self._tool_timeout_seconds,
                        )
                result = await asyncio.wait_for(
                    session.call_tool(tool, arguments=query_arguments),
                    timeout=self._tool_timeout_seconds,
                )
                content_items = list(result.content)[: self._max_items_per_call]
                # #65: state rows bank as DATABASE_STATE with connection
                # provenance (not RUNTIME_LOG) so state-claim validation
                # sees them.
                state_source = (
                    f"mcp://{connection_name}/{getattr(request, 'template_id', '')}"
                    if self._connection_tool is not None and connection_name
                    else None
                )
                items = [
                    self._map_mcp_result_to_evidence(
                        item,
                        tenant_id,
                        investigation_id,
                        evidence_type=EvidenceType.DATABASE_STATE,
                        source=state_source,
                        provider=(
                            f"mcp-state-{connection_name}"
                            if connection_name
                            else "mcp-state"
                        ),
                    )
                    for item in content_items
                ]
                return EvidenceQueryResult(
                    items=items,
                    cursor=None,
                    has_more=len(content_items) == self._max_items_per_call,
                    total_count=len(items),
                )
        except TimeoutError as exc:
            raise ExecutionError(
                f"MCP {tool} timed out after {self._tool_timeout_seconds}s"
            ) from exc
        except Exception as exc:
            raise ExecutionError(f"MCP {tool} failure: {exc}") from exc

    def serves_tenant(self, tenant_id: str) -> bool:
        """Whether this MCP server may serve the tenant (P3-2 fan-out)."""
        if self._tenant_allowlist is None:
            return True
        return tenant_id in self._tenant_allowlist

    @staticmethod
    def _resolve_schema(request: Any, profile: Any) -> str:
        """Schema declared by the datasource named in the template_id.

        Mirrors ``_resolve_template_sql``'s lookup (``<ds_id>.<name>`` or
        bare ``<name>``). Returns "" when none declares one — the session
        then keeps the login default. The profile validator already
        restricts this to identifier charset, so interpolation below is safe.
        """
        template_id = str(getattr(request, "template_id", "") or "")
        datasources = getattr(profile, "datasources", []) or []
        if "." in template_id:
            ds_id, _ = template_id.split(".", 1)
            for ds in datasources:
                if getattr(ds, "id", "") == ds_id:
                    return str(getattr(ds, "schema_name", "") or "")
            return ""
        for ds in datasources:
            templates = getattr(ds, "query_templates", {}) or {}
            if template_id in templates:
                return str(getattr(ds, "schema_name", "") or "")
        return ""

    @staticmethod
    def _resolve_template_sql(
        request: Any, profile: Any, tenant_id: str
    ) -> str:
        """Render a profile-declared template into SQL (P3-2).

        Matches ``<datasource_id>.<name>`` or bare ``<name>`` against the
        profile datasources; binds ``:tenant_id`` plus scalar parameters.
        Values are literal-quoted and the result still passes SELECT-only
        validation downstream — this mirrors direct-adapter binding.
        """
        import re as _re

        template_id = str(getattr(request, "template_id", "") or "")
        params = getattr(request, "parameters", {}) or {}
        datasources = getattr(profile, "datasources", []) or []
        sql_text = ""
        if "." in template_id:
            ds_id, name = template_id.split(".", 1)
            for ds in datasources:
                if getattr(ds, "id", "") == ds_id:
                    tpl = (getattr(ds, "query_templates", {}) or {}).get(name)
                    sql_text = str(getattr(tpl, "sql", "") or "")
                    break
        else:
            for ds in datasources:
                tpl = (getattr(ds, "query_templates", {}) or {}).get(template_id)
                if tpl is not None:
                    sql_text = str(getattr(tpl, "sql", "") or "")
                    break
        if not sql_text:
            return ""
        bindings: dict[str, str] = {"tenant_id": tenant_id}
        if isinstance(params, dict):
            for key, value in params.items():
                if isinstance(value, (str, int, float, bool)):
                    bindings[str(key)] = str(value)

        def _sub(match: Any) -> str:
            name = match.group(1)
            if name not in bindings:
                return match.group(0)
            return "'" + bindings[name].replace("'", "''") + "'"

        return _re.sub(r":([A-Za-z_][A-Za-z0-9_]*)", _sub, sql_text)

    def _resolve_connection(self, tenant_id: str, requested: Any) -> str:
        """Resolve the SQLcl connection name under default-deny tenant scoping."""
        if isinstance(requested, str) and requested.strip():
            chosen = requested.strip()
        elif self._connection_name:
            chosen = self._connection_name
        else:
            raise SecurityPolicyViolationException(
                "MCP state query names no connection and the adapter has no default"
            )
        allowed = self._tenant_allowlist
        if allowed is not None and chosen not in allowed.get(tenant_id, set()):
            raise SecurityPolicyViolationException(
                f"Tenant '{tenant_id}' may not use connection '{chosen}'"
            )
        return chosen

    def _map_mcp_result_to_evidence(
        self,
        content_item: Any,
        tenant_id: str,
        investigation_id: UUID,
        evidence_type: Any | None = None,
        source: str | None = None,
        provider: str | None = None,
    ) -> Evidence:
        now = datetime.now(UTC)
        text_payload = getattr(content_item, "text", str(content_item))
        item_hash = hashlib.sha256(
            f"{tenant_id}:{investigation_id}:{text_payload}".encode()
        ).hexdigest()

        prov = EvidenceProvenance(
            tenant_id=tenant_id,
            investigation_id=investigation_id,
            provider_type="MCP",
            requested_provider_id="mcp-server",
            actual_provider_id="mcp-server",
            source_system="MCP-Tool",
            retrieval_timestamp=now,
            query_fingerprint=QueryFingerprint(
                provider_type="MCP", operation="CALL_TOOL", normalized_query_hash=item_hash
            ),
            source_location=SourceLocation(system="MCP", identifier="mcp-tool"),
        )
        fresh = EvidenceFreshness(observed_at=now, retrieved_at=now)

        return Evidence(
            tenant_id=tenant_id,
            investigation_id=prov.investigation_id,
            evidence_type=evidence_type or EvidenceType.RUNTIME_LOG,
            provider=provider or "MCP",
            source=source or "mcp://tool/search_logs",
            title="MCP Tool Result",
            summary=text_payload[:200],
            content_snippet=text_payload[:MCP_MAX_SNIPPET_BYTES],
            content_uri=f"mcp://result/{item_hash}",
            fingerprint=item_hash,
            classification=ClassificationLevel.INTERNAL,
            observed_at=now,
            retrieved_at=now,
            attributes={"raw_payload": text_payload, "tenant_id": tenant_id},
            provenance=prov,
            freshness=fresh,
        )
