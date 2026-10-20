"""Model step 6: legacy alias spellings warn but still validate."""

import warnings
from uuid import uuid4

from investigation_agent_platform.domain.code.models import (
    CallGraph,
    CodeSymbol,
    DatabaseOperation,
    ExceptionPath,
    SymbolType,
)


def _symbol_kwargs(**overrides):
    base = {
        "name": "Widget",
        "symbol_type": SymbolType.CLASS,
        "repository": "repo",
        "file_path": "src/Widget.java",
        "line_start": 1,
        "line_end": 5,
        "signature": "class Widget",
    }
    base.update(overrides)
    return base


def test_symbol_id_alias_warns_but_validates():
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        symbol = CodeSymbol(**_symbol_kwargs(symbol_id=uuid4()))
    assert any(issubclass(w.category, DeprecationWarning) for w in caught)
    assert symbol.node_id is not None


def test_parent_symbol_alias_warns_but_validates():
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        symbol = CodeSymbol(**_symbol_kwargs(parent_symbol="Widget"))
    assert any(issubclass(w.category, DeprecationWarning) for w in caught)
    assert symbol.parent_node_id is None


def test_canonical_spelling_is_silent():
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        CodeSymbol(**_symbol_kwargs(node_id=uuid4()))
    assert not [w for w in caught if issubclass(w.category, DeprecationWarning)]


def test_legacy_graph_and_edge_shapes_warn():
    investigation_id = uuid4()
    symbol = CodeSymbol(**_symbol_kwargs(node_id=uuid4()))
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        CallGraph(
            tenant_id="t",
            investigation_id=investigation_id,
            root_symbol="Widget",
            nodes=[{"symbol": symbol, "depth": 0, "invocations": ["run"]}],
        )
    assert any(issubclass(w.category, DeprecationWarning) for w in caught)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        ExceptionPath(
            tenant_id="t",
            investigation_id=investigation_id,
            exception_class="Boom",
            throw_location=symbol,
        )
    assert any(issubclass(w.category, DeprecationWarning) for w in caught)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        DatabaseOperation(
            tenant_id="t",
            investigation_id=investigation_id,
            target_entity_or_table="orders",
            operation_type="SELECT",
            source_symbol=symbol,
        )
    assert any(issubclass(w.category, DeprecationWarning) for w in caught)
