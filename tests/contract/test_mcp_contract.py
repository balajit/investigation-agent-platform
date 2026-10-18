# tests/contract/test_mcp_contract.py
"""Part 9 Phase 6 MCP contract tests: tool catalog, schemas, error shape.

Compatibility suite for investigation agents: exact tool names, required and
optional arguments, output shape (SDK ``result`` wrapper with success/error
arms), and error codes. Schema changes must update these tests deliberately —
never silently.
"""

from unittest.mock import MagicMock

import pytest
from jsonschema import Draft202012Validator

from investigation_agent_platform.mcp.registry import EXPECTED_TOOLS

FORBIDDEN_INPUT_PROPERTIES = {
    "tenant_id",
    "tenant",
    "investigation_id",
    "investigation",
    "authorization",
    "token",
    "index",
    "query",
    "body",
    "dsl",
    "sort",
    "aggregation",
    "aggregations",
    "script",
    "source_filter",
}


async def _tools():  # type: ignore[no-untyped-def]
    from investigation_agent_platform.mcp.server import build_mcp_server

    server = build_mcp_server(MagicMock())
    tools = await server.list_tools()
    return {tool.name: tool for tool in tools}


def _input_schema(tool):  # type: ignore[no-untyped-def]
    return tool.input_schema


def _resolve(node, defs):  # type: ignore[no-untyped-def]
    """Follow `#/$defs/` references to the concrete schema node."""
    seen = 0
    while isinstance(node, dict) and node.get("$ref", "").startswith("#/$defs/") and seen < 10:
        node = defs[node["$ref"].split("/")[-1]]
        seen += 1
    return node


def _non_null_arm(prop):  # type: ignore[no-untyped-def]
    """Return the non-null arm of an optional property (or the property itself)."""
    for arm in prop.get("anyOf", [prop]):
        if arm.get("type") != "null":
            return arm
    return prop


def _output_arms(tool):  # type: ignore[no-untyped-def]
    output = tool.output_schema
    defs = output.get("$defs", {})
    arms = output["properties"]["result"].get("anyOf", [])
    return [(_resolve(arm, defs), defs) for arm in arms]


@pytest.mark.asyncio
async def test_tool_catalog_exact():
    tools = await _tools()
    assert set(tools) == EXPECTED_TOOLS
    assert set(tools) == {
        "search_runtime_evidence",
        "get_evidence",
        "investigate_trace",
        "search_reference_docs",
    }
    names = [name for name in tools]
    assert len(names) == len(set(names))


@pytest.mark.asyncio
async def test_all_tools_have_schemas_and_annotations():
    tools = await _tools()
    for name, tool in tools.items():
        assert tool.input_schema, name
        assert tool.output_schema, name
        Draft202012Validator.check_schema(tool.input_schema)
        Draft202012Validator.check_schema(tool.output_schema)
        annotations = tool.annotations
        assert annotations is not None, name
        assert annotations.read_only_hint is True, name
        assert annotations.destructive_hint is False, name
        assert annotations.idempotent_hint is True, name
        assert annotations.open_world_hint is True, name


@pytest.mark.asyncio
async def test_search_input_contract():
    tools = await _tools()
    schema = _input_schema(tools["search_runtime_evidence"])
    assert schema.get("type") == "object"
    assert set(schema.get("required", [])) == {
        "environment",
        "identifiers",
        "keywords",
        "services",
        "severities",
        "start_time",
        "end_time",
        "limit",
        "cursor",
    }
    properties = schema.get("properties", {})
    assert set(properties) == set(schema["required"])
    assert not (set(properties) & FORBIDDEN_INPUT_PROPERTIES)
    assert properties["environment"].get("pattern") == "^[A-Za-z0-9][A-Za-z0-9._-]*$"
    assert properties["limit"]["minimum"] == 1 and properties["limit"]["maximum"] == 100
    assert _non_null_arm(properties["cursor"])["maxLength"] == 4096
    assert _non_null_arm(properties["cursor"])["minLength"] == 1
    assert _non_null_arm(properties["keywords"])["maxItems"] == 20
    assert _non_null_arm(properties["services"])["maxItems"] == 20
    assert _non_null_arm(properties["severities"])["maxItems"] == 10
    identifiers_arm = _non_null_arm(properties["identifiers"])
    assert identifiers_arm["maxProperties"] == 10
    assert identifiers_arm["additionalProperties"]["maxLength"] == 500
    severities = _resolve_severity_enum(schema)
    assert set(severities) == {"TRACE", "DEBUG", "INFO", "WARN", "WARNING", "ERROR", "FATAL"}


def _resolve_severity_enum(schema):  # type: ignore[no-untyped-def]
    sev = schema["properties"]["severities"]
    candidates = []
    for arm in sev.get("anyOf", []):
        items = arm.get("items", {})
        ref = items.get("$ref", "")
        if ref.startswith("#/$defs/"):
            candidates.append(ref.split("/")[-1])
    assert candidates, "severity enum $ref missing"
    name = candidates[0]
    return schema["$defs"][name]["enum"]


@pytest.mark.asyncio
async def test_get_input_contract():
    tools = await _tools()
    schema = _input_schema(tools["get_evidence"])
    assert set(schema.get("required", [])) == {"evidence_id"}
    assert set(schema.get("properties", {})) == {"evidence_id"}
    assert schema["properties"]["evidence_id"].get("format") == "uuid"


@pytest.mark.asyncio
async def test_trace_input_contract():
    tools = await _tools()
    schema = _input_schema(tools["investigate_trace"])
    assert set(schema.get("required", [])) == {
        "trace_id",
        "environment",
        "start_time",
        "end_time",
        "max_evidence",
    }
    properties = schema["properties"]
    assert not (set(properties) & FORBIDDEN_INPUT_PROPERTIES)
    assert properties["trace_id"].get("pattern") == "^[A-Za-z0-9._:-]+$"
    assert properties["trace_id"]["maxLength"] == 200
    assert properties["max_evidence"]["minimum"] == 1
    assert properties["max_evidence"]["maximum"] == 100


@pytest.mark.asyncio
async def test_output_wrapper_and_error_arm():
    tools = await _tools()
    for name, tool in tools.items():
        output = tool.output_schema
        assert set(output.get("required", [])) == {"result"}, name
        resolved = _output_arms(tool)
        assert len(resolved) == 2, name
        shapes = [sorted(arm.get("properties", {}).keys()) for arm, _ in resolved]
        assert any("error" in keys for keys in shapes), name
        error_arm = next(arm for arm, _ in resolved if "error" in arm.get("properties", {}))
        defs = tool.output_schema.get("$defs", {})
        codes = _resolve_error_codes(defs, error_arm)
        assert {
            "INVALID_REQUEST",
            "INVALID_CURSOR",
            "CURSOR_EXPIRED",
            "FORBIDDEN",
            "EVIDENCE_NOT_FOUND",
            "PROVIDER_TIMEOUT",
            "PROVIDER_UNAVAILABLE",
            "INCOMPLETE_EVIDENCE",
            "INTERNAL_ERROR",
        } <= set(codes), name


def _resolve_error_codes(defs, error_arm):  # type: ignore[no-untyped-def]
    detail = _resolve(error_arm["properties"]["error"], defs)
    code = _resolve(detail["properties"]["code"], defs)
    assert code.get("type") == "string"
    return code["enum"]


@pytest.mark.asyncio
async def test_output_success_shapes():
    tools = await _tools()
    search_arms = [arm for arm, _ in _output_arms(tools["search_runtime_evidence"])]
    success = next(arm for arm in search_arms if "items" in arm.get("properties", {}))
    assert set(success["properties"]) == {
        "items",
        "next_cursor",
        "has_more",
        "total_count",
        "completeness",
    }
    assert set(success["required"]) == {
        "items",
        "next_cursor",
        "has_more",
        "total_count",
        "completeness",
    }
    trace_arms = [arm for arm, _ in _output_arms(tools["investigate_trace"])]
    trace_success = next(arm for arm in trace_arms if "trace_id" in arm.get("properties", {}))
    # Part 10: support flags distinguish "source has no such data" from
    # "supported, found nothing" (deliberate schema evolution).
    assert set(trace_success["properties"]) == {
        "trace_id",
        "code_locations",
        "tables",
        "sql_statements",
        "evidence",
        "code_location_supported",
        "sql_supported",
        "completeness",
    }
    assert {"code_location_supported", "sql_supported"} <= set(trace_success.get("required", []))
    get_arms = [arm for arm, _ in _output_arms(tools["get_evidence"])]
    get_success = next(arm for arm in get_arms if "attributes" in arm.get("properties", {}))
    assert "provenance" in get_success["properties"]
    assert "tenant_id" not in get_success["properties"]


@pytest.mark.asyncio
async def test_completeness_reason_vocabulary():
    tools = await _tools()
    output = tools["search_runtime_evidence"].output_schema
    defs = output.get("$defs", {})
    reasons: set[str] = set()

    def _walk(node):  # type: ignore[no-untyped-def]
        if isinstance(node, dict):
            node = _resolve(node, defs)
            if set(node.get("required", [])) == {
                "complete",
                "truncated",
                "reason",
                "returned_count",
                "examined_count",
            }:
                reason = _resolve(node["properties"]["reason"], defs)
                for arm in reason.get("anyOf", [reason]):
                    arm = _resolve(arm, defs)
                    enum = arm.get("enum")
                    if enum:
                        reasons.update(v for v in enum if v is not None)
            for value in node.values():
                _walk(value)
        elif isinstance(node, list):
            for value in node:
                _walk(value)

    _walk(output)
    assert reasons == {
        "pagination_available",
        "provider_limit",
        "application_limit",
        "provider_failure",
        "metadata_incomplete",
        "input_bound",
    }


@pytest.mark.asyncio
async def test_table_schema_key_on_wire():
    tools = await _tools()
    output = tools["investigate_trace"].output_schema
    defs = output.get("$defs", {})
    found = []

    def _walk(node):  # type: ignore[no-untyped-def]
        if isinstance(node, dict):
            node = _resolve(node, defs)
            props = node.get("properties", {})
            if {"catalog", "schema", "table", "evidence_ids"} <= set(props):
                found.append(True)
            for value in node.values():
                _walk(value)
        elif isinstance(node, list):
            for value in node:
                _walk(value)

    _walk(output)
    assert found, "table reference must serialize the 'schema' contract key"
