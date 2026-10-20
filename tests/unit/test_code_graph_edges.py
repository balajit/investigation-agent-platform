# tests/unit/test_code_graph_edges.py
"""D3/D4: edge-based CallGraph, node-referencing ExceptionPath/DatabaseOperation."""

from __future__ import annotations

import inspect
from uuid import uuid4

import pytest

from investigation_agent_platform.domain.code.models import (
    LEGACY_TRANSLATED_EDGE_CONFIDENCE,
    CallGraph,
    CallGraphEdge,
    CallGraphNode,
    CodeSymbol,
    DatabaseOperation,
    ExceptionPath,
    SymbolType,
)
from investigation_agent_platform.domain.topology.models import CallResolutionStatus

_TENANT = "tenant-a"
_INVESTIGATION = uuid4()
_REV = "abc1234567890abcdef"
_REPO = "repo-a"


def _symbol(name="handle", revision=_REV, **overrides):
    base = {
        "tenant_id": _TENANT,
        "repository_id": _REPO,
        "name": name,
        "qualified_name": f"Service.{name}",
        "symbol_type": SymbolType.METHOD,
        "repository": _REPO,
        "file_path": "src/service.py",
        "line_start": 10,
        "line_end": 20,
        "signature": f"def {name}(self):",
        "revision": revision,
    }
    base.update(overrides)
    return CodeSymbol.create(**base)


def _graph(**overrides):
    caller = _symbol("caller")
    callee = _symbol("callee")
    base = {
        "tenant_id": _TENANT,
        "investigation_id": _INVESTIGATION,
        "root_symbol": "Service.caller",
        "root_node_id": caller.node_id,
        "revision": _REV,
        "nodes": {caller.node_id: caller, callee.node_id: callee},
        "edges": [
            CallGraphEdge(
                caller_node_id=caller.node_id,
                callee_node_id=callee.node_id,
                callee_qualified_name="Service.callee",
                call_site_file="src/service.py",
                call_site_line=12,
                resolution_status=CallResolutionStatus.RESOLVED,
            )
        ],
    }
    base.update(overrides)
    return CallGraph.model_validate(base)


def test_valid_graph_passes():
    graph = _graph()
    assert len(graph.nodes) == 2
    assert graph.edges[0].resolution_status == CallResolutionStatus.RESOLVED


def test_unknown_caller_fails_closed():
    caller = _symbol("caller")
    with pytest.raises(ValueError, match="unknown caller"):
        _graph(nodes={caller.node_id: caller}, edges=[
            CallGraphEdge(
                caller_node_id=uuid4(),
                call_site_file="src/service.py",
                call_site_line=1,
            )
        ])


def test_unknown_callee_fails_closed():
    caller = _symbol("caller")
    with pytest.raises(ValueError, match="unknown callee"):
        _graph(nodes={caller.node_id: caller}, edges=[
            CallGraphEdge(
                caller_node_id=caller.node_id,
                callee_node_id=uuid4(),
                call_site_file="src/service.py",
                call_site_line=1,
                resolution_status=CallResolutionStatus.RESOLVED,
            )
        ])


def test_external_callee_exempt():
    caller = _symbol("caller")
    graph = _graph(nodes={caller.node_id: caller}, edges=[
        CallGraphEdge(
            caller_node_id=caller.node_id,
            callee_node_id=uuid4(),
            callee_qualified_name="external.Lib.run",
            call_site_file="src/service.py",
            call_site_line=3,
            resolution_status=CallResolutionStatus.EXTERNAL,
        )
    ])
    assert graph.edges[0].resolution_status == CallResolutionStatus.EXTERNAL


def test_resolved_without_callee_rejected():
    with pytest.raises(ValueError, match="requires callee_node_id"):
        CallGraphEdge(
            caller_node_id=uuid4(),
            call_site_file="src/service.py",
            call_site_line=1,
            resolution_status=CallResolutionStatus.RESOLVED,
        )


def test_unresolved_without_callee_allowed():
    edge = CallGraphEdge(
        caller_node_id=uuid4(),
        callee_qualified_name="Service.unknown",
        call_site_file="src/service.py",
        call_site_line=1,
        resolution_status=CallResolutionStatus.UNRESOLVED,
    )
    assert edge.callee_node_id is None


def test_mixed_revisions_fail_closed():
    pinned = _symbol("a", revision=_REV)
    other = _symbol("b", revision="def9876543210abcdef")
    with pytest.raises(ValueError, match="revision does not match"):
        _graph(nodes={pinned.node_id: pinned, other.node_id: other}, edges=[])


def test_head_revision_leniency():
    mixed_a = _symbol("a", revision=_REV)
    mixed_b = _symbol("b", revision="def9876543210abcdef")
    graph = CallGraph.model_validate(
        {
            "tenant_id": _TENANT,
            "investigation_id": _INVESTIGATION,
            "root_symbol": "Service.a",
            "revision": "HEAD",
            "nodes": {mixed_a.node_id: mixed_a, mixed_b.node_id: mixed_b},
            "edges": [],
        }
    )
    assert len(graph.nodes) == 2


def test_legacy_node_list_translates():
    caller = _symbol("caller")
    legacy_node = CallGraphNode(symbol=caller, depth=0, invocations=["Service.callee"])
    graph = CallGraph.model_validate(
        {
            "tenant_id": _TENANT,
            "investigation_id": _INVESTIGATION,
            "root_symbol": "Service.caller",
            "revision": _REV,
            "nodes": [legacy_node.model_dump()],
            "edges": [],
        }
    )
    assert set(graph.nodes) == {caller.node_id}
    assert len(graph.edges) == 1
    edge = graph.edges[0]
    assert edge.caller_node_id == caller.node_id
    assert edge.callee_node_id is None
    assert edge.callee_qualified_name == "Service.callee"
    assert edge.resolution_status == CallResolutionStatus.UNRESOLVED
    assert edge.confidence == LEGACY_TRANSLATED_EDGE_CONFIDENCE < 1.0
    assert edge.call_site_file == caller.file_path
    assert edge.call_site_line == caller.line_start


def test_from_legacy_nodes_translator():
    caller = _symbol("caller")
    graph = CallGraph.from_legacy_nodes(
        [CallGraphNode(symbol=caller, depth=1, invocations=["x", "y"])],
        tenant_id=_TENANT,
        investigation_id=_INVESTIGATION,
        root_symbol="Service.caller",
        revision=_REV,
    )
    assert set(graph.nodes) == {caller.node_id}
    assert len(graph.edges) == 2
    assert all(e.resolution_status == CallResolutionStatus.UNRESOLVED for e in graph.edges)


def test_legacy_exception_path_validates():
    throw = _symbol("thrower")
    handler = _symbol("handler")
    path = ExceptionPath.model_validate(
        {
            "tenant_id": _TENANT,
            "investigation_id": _INVESTIGATION,
            "exception_class": "ValueError",
            "throw_location": throw.model_dump(by_alias=True),
            "handler_locations": [handler.model_dump(by_alias=True)],
        }
    )
    assert path.throw_node_id == throw.node_id
    assert path.handler_node_ids == [handler.node_id]
    assert path.revision == "HEAD"
    assert path.confidence == 1.0


def test_canonical_exception_path():
    path = ExceptionPath(
        tenant_id=_TENANT,
        investigation_id=_INVESTIGATION,
        exception_class="ValueError",
        revision=_REV,
        throw_node_id=uuid4(),
        handler_node_ids=[uuid4()],
        confidence=0.8,
    )
    assert path.confidence == 0.8


def test_legacy_database_operation_validates():
    source = _symbol("reader")
    op = DatabaseOperation.model_validate(
        {
            "tenant_id": _TENANT,
            "investigation_id": _INVESTIGATION,
            "target_entity_or_table": "orders",
            "operation_type": "SELECT",
            "source_symbol": source.model_dump(by_alias=True),
            "raw_query_snippet": "SELECT * FROM orders",
        }
    )
    assert op.source_node_id == source.node_id
    assert op.revision == "HEAD"


def test_canonical_database_operation():
    op = DatabaseOperation(
        tenant_id=_TENANT,
        investigation_id=_INVESTIGATION,
        target_entity_or_table="orders",
        operation_type="SELECT",
        revision=_REV,
        source_node_id=uuid4(),
        raw_query_snippet="SELECT * FROM orders",
    )
    assert op.revision == _REV


def test_sensitive_snippet_guard_unchanged():
    # The guard code and field order are byte-identical to the pre-D4 shape,
    # so construction behavior is preserved exactly. Exercise the validator
    # logic directly to prove it still rejects sensitive snippets.
    from types import SimpleNamespace

    assert (
        "sanitized/redacted"
        in inspect.getsource(DatabaseOperation.validate_sensitive_snippet)
    )
    sensitive = SimpleNamespace(data={"is_classified_sensitive": True})
    with pytest.raises(ValueError, match="sanitized/redacted"):
        DatabaseOperation.validate_sensitive_snippet("SELECT * FROM orders", sensitive)
    assert (
        DatabaseOperation.validate_sensitive_snippet("SELECT * FROM orders", SimpleNamespace(data={}))
        == "SELECT * FROM orders"
    )
    ok = DatabaseOperation(
        tenant_id=_TENANT,
        investigation_id=_INVESTIGATION,
        target_entity_or_table="orders",
        operation_type="SELECT",
        source_node_id=uuid4(),
        is_classified_sensitive=True,
    )
    assert ok.raw_query_snippet is None


def test_node_cap_preserved():
    from pydantic import ValidationError

    symbol = _symbol("capped").model_dump()
    nodes = {str(uuid4()): symbol for _ in range(501)}
    with pytest.raises(ValidationError):
        CallGraph.model_validate(
            {
                "tenant_id": _TENANT,
                "investigation_id": _INVESTIGATION,
                "root_symbol": "Service.capped",
                "revision": "HEAD",
                "nodes": nodes,
                "edges": [],
            }
        )
