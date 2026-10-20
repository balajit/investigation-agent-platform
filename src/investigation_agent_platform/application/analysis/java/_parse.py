# src/investigation_agent_platform/application/analysis/java/_parse.py
"""Shared Tree-sitter Java parsing and identity helpers (Phase 2)."""

from __future__ import annotations

from typing import Any

_FIELD_TYPES = frozenset({"field_declaration"})
_CLASS_TYPES = frozenset({"class_declaration"})
_PARSER_VERSION = "tree-sitter-java"


def parse_java(source: bytes) -> Any:
    """Parse a source window; raises the parser exception to the caller."""
    from tree_sitter_language_pack import get_parser

    return get_parser("java").parse(source)


def parser_version() -> str:
    """Parser identity recorded on resolutions for grammar-drift auditing."""
    return _PARSER_VERSION


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
    for node in walk(root):
        if node.type == "package_declaration":
            for child in node.children:
                if child.type in {"scoped_identifier", "identifier"}:
                    return node_text(child, source).strip()
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
    for node in walk(root):
        if node.type != "field_declaration":
            continue
        parent = node.parent
        class_node = parent.parent if parent is not None else None
        if class_node is None or class_node.type not in _CLASS_TYPES:
            continue
        for child in node.children:
            if child.type != "variable_declarator":
                continue
            name_node = child.child_by_field_name("name")
            if name_node is not None and node_text(name_node, source) == field_name:
                found.append((node, child, qualified_class_name(class_node, source, package)))
    return found
