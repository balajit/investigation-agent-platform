# tests/unit/test_part10_phase11.py
"""Part 10 Phase 11: mapping-aware trace-hop resolution."""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from investigation_agent_platform.domain.observability.mapping import (
    FieldMapping,
    FieldValueType,
    TraceHopMapping,
    generic_ecs_mapping,
)
from investigation_agent_platform.infrastructure.topology.trace_hop import (
    GatewayTraceHopResolver,
)

TENANT = "tenant-a"


def _hop_mapping():  # type: ignore[no-untyped-def]
    base = generic_ecs_mapping()
    return base.model_copy(
        update={
            "source_id": "hop-test",
            "trace_hop": TraceHopMapping(
                service=FieldMapping(candidates=["appName"], value_type=FieldValueType.KEYWORD),
                span_id=FieldMapping(candidates=["messageId"], value_type=FieldValueType.KEYWORD),
                parent_id=None,
            ),
        }
    )


def _item(attrs):  # type: ignore[no-untyped-def]
    return SimpleNamespace(attributes=attrs)


def _gateway(items):  # type: ignore[no-untyped-def]
    gateway = MagicMock()
    gateway.search_runtime_evidence = AsyncMock(
        return_value=SimpleNamespace(items=items, has_more=False, total_count=len(items))
    )
    return gateway


def _base_args(iid=None, **overrides):  # type: ignore[no-untyped-def]
    args = {
        "tenant_id": TENANT,
        "investigation_id": iid or uuid.uuid4(),
        "application_id": "checkout-app",
        "environment": "production",
        "trace_id": "abc",
        "source_span_id": "sp-source",
    }
    args.update(overrides)
    return args


@pytest.mark.asyncio
async def test_hop_resolves_gen_shaped_attributes_via_mapping():
    gateway = _gateway([_item({"appName": "billing", "messageId": "sp-2", "traceId": "abc"})])
    resolver = GatewayTraceHopResolver(gateway, mapping=_hop_mapping())
    target = await resolver.resolve_target_service(**_base_args())
    assert target is not None
    assert target.target_service == "billing"
    assert target.target_span_id == "sp-2"
    # Retrieval used the same mapping: trace_id rendered as traceId term.
    (_, _, _, request) = gateway.search_runtime_evidence.call_args.args
    assert request.identifiers == {"trace_id": "abc"}
    assert request.mapping_source_id == "hop-test"


@pytest.mark.asyncio
async def test_hop_returns_none_when_mapping_fields_absent():
    gateway = _gateway([_item({"message": "no hop fields here"})])
    resolver = GatewayTraceHopResolver(gateway, mapping=_hop_mapping())
    assert await resolver.resolve_target_service(**_base_args()) is None


@pytest.mark.asyncio
async def test_hop_without_mapping_keeps_legacy_paths():
    gateway = _gateway([_item({"span": {"id": "sp-9"}, "service": {"name": "ledger"}})])
    resolver = GatewayTraceHopResolver(gateway)
    target = await resolver.resolve_target_service(**_base_args())
    assert target is not None
    assert (target.target_service, target.target_span_id) == ("ledger", "sp-9")
    (_, _, _, request) = gateway.search_runtime_evidence.call_args.args
    assert request.mapping_source_id is None


@pytest.mark.asyncio
async def test_hop_mapping_without_hop_section_falls_back():
    gateway = _gateway([_item({"span": {"id": "sp-9"}, "service": {"name": "ledger"}})])
    mapping = generic_ecs_mapping()
    assert mapping.trace_hop is None
    resolver = GatewayTraceHopResolver(gateway, mapping=mapping)
    target = await resolver.resolve_target_service(**_base_args())
    assert target is not None
    assert target.target_service == "ledger"


def test_trace_hop_mapping_model_validation():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        TraceHopMapping(
            service=FieldMapping(candidates=[], value_type=FieldValueType.KEYWORD),
            span_id=FieldMapping(candidates=["x"], value_type=FieldValueType.KEYWORD),
        )


def test_consumer_audit_only_known_readers():
    """Pin the inventory of code that reads projected attribute paths.

    trace_hop (mapping-aware as of this phase), trace_telemetry (mapping
    gated), security redactor (generic recursion), and MCP serializers
    (verbatim passthrough) are the complete set. Any new direct reader of
    ECS-shaped paths must update this test deliberately.
    """
    import pathlib
    import re

    base = pathlib.Path(__file__).resolve().parents[2] / "src" / "investigation_agent_platform"
    readers = set()
    pattern = re.compile(r"_nested_get\(|attributes\[[\"'](?:span|service|parent|code|log|db)")
    for path in base.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if pattern.search(text):
            readers.add(str(path.relative_to(base)))
    assert readers <= {
        "infrastructure/topology/trace_hop.py",
        "infrastructure/evidence/logs/trace_telemetry.py",
    }, readers
