"""Parity and contract tests for the Tree-sitter query substrate."""

from tree_sitter_language_pack import get_parser

from investigation_agent_platform.application.analysis.java import _parse


def test_query_substrate_captures_methods_and_parser_identity():
    source = b"package demo; import java.util.List; class X { void run() {} }"
    tree = get_parser("java").parse(source)
    captures = _parse.query_captures(
        tree.root_node,
        source,
        "(method_declaration name: (identifier) @name)",
    )
    assert [_parse.node_text(node, source) for node in captures["name"]] == ["run"]
    assert _parse.parser_version() == "tree-sitter-java"
    assert _parse.query_version() == "tree-sitter-query-v1"


def test_package_query_matches_nested_package_names():
    source = b"package com.example.query; class X {}"
    tree = get_parser("java").parse(source)
    assert _parse.package_name(tree.root_node, source) == "com.example.query"
