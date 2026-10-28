# src/investigation_agent_platform/application/analysis/java/_parse.py
"""Shared Tree-sitter Java parsing and identity helpers (Phase 2)."""

from __future__ import annotations

from typing import Any

_FIELD_TYPES = frozenset({"field_declaration"})
_CLASS_TYPES = frozenset({"class_declaration"})
_PARSER_VERSION = "tree-sitter-java"
_QUERY_VERSION = "tree-sitter-query-v1"


def parse_java(source: bytes) -> Any:
    """Parse a source window; raises the parser exception to the caller."""
    from tree_sitter_language_pack import get_parser

    return get_parser("java").parse(source)


def parser_version() -> str:
    """Parser identity recorded on resolutions for grammar-drift auditing."""
    return _PARSER_VERSION


def query_version() -> str:
    """Identity for the shared Tree-sitter query substrate."""
    return _QUERY_VERSION


def query_captures(root: Any, source: bytes, pattern: str) -> dict[str, list[Any]]:
    """Run a grammar-pinned Java query and return captures by name.

    Query syntax is kept at call sites so each matcher remains reviewable; the
    parser and cursor construction is centralized for API-version stability.
    """
    from tree_sitter import Query, QueryCursor
    from tree_sitter_language_pack import get_language

    language = get_language("java")
    query = Query(language, pattern)
    return dict(QueryCursor(query).captures(root))


def node_text(node: Any, source: bytes) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def walk(node: Any) -> Any:
    """Yield a node and all descendants in document order."""
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(current.children))


def package_name(root: Any, source: bytes) -> str:
    """Return the window's package (dotted) or empty when absent/unparseable."""
    captures = query_captures(
        root,
        source,
        "(package_declaration (scoped_identifier) @package) (package_declaration (identifier) @package)",
    )
    packages = captures.get("package", [])
    if packages:
        return node_text(packages[0], source).strip()
    return ""


def qualified_class_name(class_node: Any, source: bytes, package: str = "") -> str:
    """Dotted name of a class node including nesting and optional package."""
    names: list[str] = []
    current: Any = class_node
    while current is not None:
        if current.type in _CLASS_TYPES:
            name = current.child_by_field_name("name")
            if name is None:
                return ""
            names.append(node_text(name, source))
        current = current.parent
    dotted = ".".join(reversed(names))
    if package and dotted:
        return f"{package}.{dotted}"
    return dotted


def matches_declaring_type(actual: str, requested: str) -> bool:
    """Segment-aware qualified-name match (Phase 2 gap fix).

    The requested name must equal trailing dot-separated segments of the
    actual qualified name. ``TransactionProcessor`` matches
    ``com.foo.TransactionProcessor``; ``foo.TransactionProcessor`` does not
    match ``otherfoo.TransactionProcessor``.
    """
    actual_segments = [s for s in actual.split(".") if s]
    requested_segments = [s for s in requested.split(".") if s]
    if not requested_segments or len(requested_segments) > len(actual_segments):
        return False
    return actual_segments[-len(requested_segments) :] == requested_segments


def find_field_declarations(
    root: Any, source: bytes, field_name: str, package: str = ""
) -> list[tuple[Any, Any, Any]]:
    """Collect (declaration, declarator, qualified_class_name) for a field name."""
    found: list[tuple[Any, Any, Any]] = []
    captures = query_captures(
        root,
        source,
        "(field_declaration declarator: (variable_declarator name: (identifier) @field)) @declaration",
    )
    declarations = captures.get("declaration", [])
    names = captures.get("field", [])
    for node, name_node in zip(declarations, names):
        if node_text(name_node, source) != field_name:
            continue
        parent = node.parent
        class_node = parent.parent if parent is not None else None
        if class_node is None or class_node.type not in _CLASS_TYPES:
            continue
        declarator = next(
            (child for child in node.children if child.type == "variable_declarator"), None
        )
        if declarator is not None:
            found.append((node, declarator, qualified_class_name(class_node, source, package)))
    return found
