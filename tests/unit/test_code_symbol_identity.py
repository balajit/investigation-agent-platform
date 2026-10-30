# tests/unit/test_code_symbol_identity.py
"""D1/D2: deterministic CodeSymbol identity, legacy compatibility, no-Cognee isolation."""

from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from investigation_agent_platform.domain.code.models import (
    SYMBOL_TO_TOPOLOGY_TYPE,
    CodeSymbol,
    SymbolType,
)
from investigation_agent_platform.domain.topology.models import (
    ASTNodeIdentity,
    TopologyNodeType,
    compute_ast_node_id,
)
from investigation_agent_platform.infrastructure.evidence.code.parser import (
    CodeSymbol as ParserSymbol,
)
from investigation_agent_platform.infrastructure.evidence.code.parser import (
    to_domain_symbol,
)

_TENANT = "tenant-a"
_REPO = "repo-a"
_REPO_ID = "repo-a"
_REV = "abc1234567890abcdef"
_PATH = "src/service.py"
_QN = "Service.handle"
_SIG = "def handle(self, req):"


def _create(**overrides):
    base = {
        "tenant_id": _TENANT,
        "repository_id": _REPO_ID,
        "name": "handle",
        "qualified_name": _QN,
        "symbol_type": SymbolType.METHOD,
        "repository": _REPO,
        "file_path": _PATH,
        "line_start": 10,
        "line_end": 20,
        "signature": _SIG,
        "revision": _REV,
        "start_column": 4,
        "end_column": 12,
        "parser_version": "test-1",
    }
    base.update(overrides)
    return CodeSymbol.create(**base)


def _topology_peer():
    return ASTNodeIdentity.create(
        tenant_id=_TENANT,
        repository_id=_REPO_ID,
        revision=_REV,
        name="handle",
        qualified_name=_QN,
        node_type=TopologyNodeType.METHOD,
        file_path=_PATH,
        start_line=10,
        end_line=20,
        start_column=4,
        end_column=12,
        signature=_SIG,
        parser_version="test-1",
    )


def test_create_agrees_with_topology_identity():
    assert _create().node_id == _topology_peer().node_id


def test_create_is_deterministic():
    assert _create().node_id == _create().node_id


def test_identity_diverges_per_input():
    baseline = _create().node_id
    variants = {
        "tenant": _create(tenant_id="tenant-b").node_id,
        "repository": _create(repository_id="repo-b").node_id,
        "revision": _create(revision="def9876543210abcdef").node_id,
        "path": _create(file_path="src/other.py").node_id,
        "qualified_name": _create(qualified_name="Service.other").node_id,
        "node_type": _create(symbol_type=SymbolType.FUNCTION).node_id,
        "range": _create(line_start=11).node_id,
        "column": _create(start_column=5).node_id,
    }
    for label, node_id in variants.items():
        assert node_id != baseline, f"identity did not diverge for {label}"


def test_compute_ast_node_id_parity():
    expected = compute_ast_node_id(
        tenant_id=_TENANT,
        repository_id=_REPO_ID,
        revision=_REV,
        file_path=_PATH,
        qualified_name=_QN,
        node_type=TopologyNodeType.METHOD,
        start_line=10,
        start_column=4,
        end_line=20,
        end_column=12,
    )
    assert _create().node_id == expected


def test_signature_hash_matches_topology():
    assert (
        _create().signature_hash
        == _topology_peer().signature_hash
        == hashlib.sha256(_SIG.encode()).hexdigest()
    )


def test_legacy_payload_still_validates():
    symbol_id = uuid4()
    legacy = CodeSymbol.model_validate(
        {
            "symbol_id": str(symbol_id),
            "name": "legacy",
            "symbol_type": "METHOD",
            "repository": _REPO,
            "file_path": _PATH,
            "line_start": 1,
            "line_end": 2,
            "signature": "def legacy():",
            "parent_symbol": "EnclosingClass",
        }
    )
    assert legacy.node_id == symbol_id
    assert legacy.symbol_id == symbol_id
    assert legacy.tenant_id == ""
    assert legacy.qualified_name == ""
    assert legacy.start_column == 0
    assert legacy.end_column == 0
    assert legacy.signature_hash == ""
    assert legacy.parser_version == "unknown"
    assert legacy.revision == "HEAD"
    # Pre-D1 parent was a bare name, not an identity: coerced, never fails.
    assert legacy.parent_node_id is None


def test_legacy_uuid_parent_symbol_preserved():
    symbol_id = uuid4()
    parent_id = uuid4()
    legacy = CodeSymbol.model_validate(
        {
            "symbol_id": str(symbol_id),
            "name": "child",
            "symbol_type": "METHOD",
            "repository": _REPO,
            "file_path": _PATH,
            "line_start": 1,
            "line_end": 2,
            "signature": "def child():",
            "parent_symbol": str(parent_id),
        }
    )
    assert legacy.parent_node_id == parent_id
    assert legacy.parent_symbol == parent_id


def test_canonical_names_validate():
    node_id = uuid4()
    parent_id = uuid4()
    sym = CodeSymbol.model_validate(
        {
            "node_id": str(node_id),
            "name": "child",
            "symbol_type": "METHOD",
            "repository": _REPO,
            "file_path": _PATH,
            "line_start": 1,
            "line_end": 2,
            "signature": "def child():",
            "parent_node_id": str(parent_id),
        }
    )
    assert sym.node_id == node_id
    assert sym.parent_node_id == parent_id


def test_wire_shape_uses_legacy_aliases():
    sym = _create()
    dumped = sym.model_dump(by_alias=True)
    assert UUID(str(dumped["symbol_id"])) == sym.node_id
    assert "node_id" not in dumped
    assert "parent_symbol" in dumped


def test_single_line_column_validator():
    with pytest.raises(ValueError, match="end_column"):
        CodeSymbol.model_validate(
            {
                "name": "bad",
                "symbol_type": "FUNCTION",
                "repository": _REPO,
                "file_path": _PATH,
                "line_start": 5,
                "line_end": 5,
                "signature": "def bad():",
                "start_column": 10,
                "end_column": 3,
            }
        )
    ok = CodeSymbol.model_validate(
        {
            "name": "ok",
            "symbol_type": "FUNCTION",
            "repository": _REPO,
            "file_path": _PATH,
            "line_start": 5,
            "line_end": 6,
            "signature": "def ok():",
            "start_column": 10,
            "end_column": 3,
        }
    )
    assert ok.start_column == 10


def test_file_path_safety_preserved():
    with pytest.raises(ValueError, match="unsafe"):
        CodeSymbol.model_validate(
            {
                "name": "evil",
                "symbol_type": "FUNCTION",
                "repository": _REPO,
                "file_path": "../evil.py",
                "line_start": 1,
                "line_end": 1,
                "signature": "x",
            }
        )


def test_taxonomy_map_covers_symbol_types():
    assert set(SYMBOL_TO_TOPOLOGY_TYPE) == set(SymbolType)
    assert SYMBOL_TO_TOPOLOGY_TYPE[SymbolType.METHOD] is TopologyNodeType.METHOD
    assert set(SYMBOL_TO_TOPOLOGY_TYPE.values()) == {
        TopologyNodeType.CLASS,
        TopologyNodeType.INTERFACE,
        TopologyNodeType.METHOD,
        TopologyNodeType.FUNCTION,
        TopologyNodeType.FIELD,
        TopologyNodeType.CONSTRUCTOR,
        TopologyNodeType.ENUM,
    }


def test_parser_adapter_helper_agrees_with_topology():
    parsed = ParserSymbol(
        name="handle",
        symbol_type="method_declaration",
        start_line=10,
        start_column=4,
        end_line=20,
        end_column=12,
        signature=_SIG,
        parent_symbol="Service",
    )
    domain = to_domain_symbol(
        parsed,
        tenant_id=_TENANT,
        repository_id=_REPO_ID,
        repository=_REPO,
        file_path=_PATH,
        revision=_REV,
        parser_version="test-1",
    )
    assert isinstance(domain, CodeSymbol)
    assert domain.qualified_name == "Service.handle"
    assert domain.node_id == _topology_peer().node_id


def test_parser_adapter_helper_rejects_unmapped_kind():
    parsed = ParserSymbol(
        name="x",
        symbol_type="lexical_declaration",
        start_line=1,
        start_column=0,
        end_line=1,
        end_column=2,
        signature="x = 1",
    )
    with pytest.raises(ValueError, match="cannot map"):
        to_domain_symbol(
            parsed,
            tenant_id=_TENANT,
            repository_id=_REPO_ID,
            repository=_REPO,
            file_path=_PATH,
            revision=_REV,
        )


def test_domain_code_and_topology_import_no_cognee():
    roots = [
        Path("src/investigation_agent_platform/domain/code"),
        Path("src/investigation_agent_platform/domain/topology"),
    ]
    offenders = [
        path
        for root in roots
        for path in sorted(root.rglob("*.py"))
        if "cognee" in path.read_text(encoding="utf-8").lower()
    ]
    assert offenders == []
