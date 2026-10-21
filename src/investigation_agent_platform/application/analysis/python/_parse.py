# src/investigation_agent_platform/application/analysis/python/_parse.py
"""Shared Tree-sitter Python parsing and identity helpers."""

from __future__ import annotations

from typing import Any

_PARSER_VERSION = "tree-sitter-python"


def parse_python(source: bytes) -> Any:
    """Parse a source window; raises the parser exception to the caller."""
    from tree_sitter_language_pack import get_parser

    return get_parser("python").parse(source)


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


def has_syntax_error(root: Any) -> bool:
    """True when the window parsed with errors or missing nodes."""
    if getattr(root, "has_error", False):
        return True
    for node in walk(root):
        if node.type == "ERROR" or getattr(node, "is_missing", False):
            return True
    return False


def matches_scope(actual: str, requested: str) -> bool:
    """Segment-aware dotted-scope match (mirrors Java declaring-type match)."""
    actual_segments = [s for s in actual.split(".") if s]
    requested_segments = [s for s in requested.split(".") if s]
    if not requested_segments or len(requested_segments) > len(actual_segments):
        return False
    return actual_segments[-len(requested_segments) :] == requested_segments
