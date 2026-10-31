"""J6 grammar operations: recovery, query parity, fault distinction (synthetic only).

Shared infra already pins identity format/version/never-raises, loader
typing, negative-cache bounds, per-thread parsers, cached queries,
preflight, and the approval-gated pins xfail. This module pins the
remainder: a seeded cache heals without restart (T06), cached queries
return exactly what a fresh build would (T09), and every resolver family
reports GrammarUnavailable distinctly from code bugs (T10).
"""

from __future__ import annotations


def _grammar_state():
    from investigation_agent_platform.application.analysis import grammar as _g

    return (
        dict(_g._languages),
        dict(_g._failed_at),
        dict(_g._abi),
        getattr(_g._local, "parsers", None),
        getattr(_g._local, "queries", None),
    )


def _restore_grammar_state(saved):
    from investigation_agent_platform.application.analysis import grammar as _g

    languages, failed_at, abi, parsers, queries = saved
    _g._languages.clear()
    _g._languages.update(languages)
    _g._failed_at.clear()
    _g._failed_at.update(failed_at)
    _g._abi.clear()
    _g._abi.update(abi)
    if parsers is None:
        try:
            del _g._local.parsers
        except AttributeError:
            pass
    else:
        _g._local.parsers = parsers
    if queries is None:
        try:
            del _g._local.queries
        except AttributeError:
            pass
    else:
        _g._local.queries = queries


def test_j6_t06_seeded_cache_heals_without_restart(monkeypatch):
    import time as _time

    from investigation_agent_platform.application.analysis import grammar as _g

    saved = _grammar_state()
    calls: list = []
    now = [2000.0]
    monkeypatch.setattr(_time, "monotonic", lambda: now[0])
    import tree_sitter_language_pack as _pack

    real_get_language = _pack.get_language

    def flaky(name):  # type: ignore[no-untyped-def]
        calls.append(name)
        if len(calls) == 1:
            raise RuntimeError("cold cache, no network")
        return real_get_language(name)

    monkeypatch.setattr(_pack, "get_language", flaky)
    _g._languages.pop("java", None)
    _g._failed_at.pop("java", None)
    try:
        from investigation_agent_platform.application.analysis.failure import (
            GrammarUnavailable,
        )

        try:
            _g.language("java")
            raise AssertionError("expected GrammarUnavailable while cold")
        except GrammarUnavailable:
            pass
        assert calls == ["java"]
        now[0] += 61.0
        assert _g.language("java") is not None
        assert "java" not in _g._failed_at
        assert calls == ["java", "java"]
        assert _g.parser("java").parse(b"class A {}").root_node is not None
    finally:
        _restore_grammar_state(saved)


def test_j6_t09_cached_queries_match_fresh_builds():
    from investigation_agent_platform.application.analysis import grammar as _g
    from investigation_agent_platform.application.analysis.java import _parse as _p

    saved = _grammar_state()
    source = b"""package com.sample.orders;
import java.util.List;
public class OrderServiceHandler {
    private String name = "n";
    OrderServiceHandler() {}
    public String kind(int v) {
        if (v > 0) {
            return helper("x");
        }
        return "z";
    }
    private String helper(String s) { return s; }
}
"""
    patterns = [
        "(method_declaration) @method",
        "(constructor_declaration) @method",
        "(import_declaration) @import",
        "(package_declaration (scoped_identifier) @package) (package_declaration (identifier) @package)",
        "(field_declaration) @declaration",
        "(class_declaration) @class",
        "(method_invocation) @call",
        "(string_literal) @text",
        "(method_declaration name: (identifier) @name)",
        "(if_statement) @branch",
        "(return_statement) @ret",
        "(formal_parameter) @param",
    ]
    try:
        tree = _p.parse_java(source)

        def snapshot():  # type: ignore[no-untyped-def]
            out = {}
            for pattern in patterns:
                captures = _p.query_captures(tree.root_node, source, pattern)
                out[pattern] = sorted(
                    (node.type, node.start_byte, node.end_byte, _p.node_text(node, source))
                    for nodes in captures.values()
                    for node in nodes
                )
            return out

        first = snapshot()
        try:
            del _g._local.queries
        except AttributeError:
            pass
        second = snapshot()
        assert first == second
        assert all(first[pattern] for pattern in patterns)
    finally:
        _restore_grammar_state(saved)


def test_j6_t10_resolvers_distinguish_grammar_fault_from_code_bug(monkeypatch):
    from investigation_agent_platform.application.analysis.failure import (
        GrammarUnavailable,
    )
    from investigation_agent_platform.application.analysis.java import (
        _parse as _java_parse,
    )
    from investigation_agent_platform.application.analysis.java.method_return_facts import (
        resolve_java_method_return_fact,
    )
    from investigation_agent_platform.application.analysis.python import (
        _parse as _py_parse,
    )
    from investigation_agent_platform.application.analysis.python.config import (
        resolve_python_config_fact,
    )
    from tests.unit.test_j5_newer_constructs import _evidence as _java_evidence

    java_source = (
        "package com.sample.orders;\n"
        "public class OrderServiceHandler {\n"
        "    String kind(int v) {\n"
        "        if (v > 0) {\n"
        '            return "pos";\n'
        "        }\n"
        '        return "neg";\n'
        "    }\n"
        "}\n"
    )
    python_source = "LIMIT = 5\n"
    java_ev = _java_evidence(java_source)
    from tests.unit.test_j8_window_edges import _evidence as _any_evidence

    py_ev = _any_evidence(python_source, path="service/mod.py")

    def grammar_boom(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise GrammarUnavailable("cold cache")

    def code_boom(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise ValueError("model bug")

    monkeypatch.setattr(_java_parse, "parse_java", grammar_boom)
    result = resolve_java_method_return_fact(java_ev, "kind", guard_condition="v > 0")
    assert result.status.value == "UNRESOLVED"
    assert result.limitation_codes == ("grammar_unavailable",)
    monkeypatch.setattr(_java_parse, "parse_java", code_boom)
    result = resolve_java_method_return_fact(java_ev, "kind", guard_condition="v > 0")
    assert result.limitation_codes == ("analysis_failed",)
    assert "java_parser_unavailable" not in ",".join(result.limitation_codes)

    monkeypatch.setattr(_py_parse, "parse_python", grammar_boom)
    result = resolve_python_config_fact(py_ev, "LIMIT")
    assert result.limitation_codes == ("grammar_unavailable",)
    monkeypatch.setattr(_py_parse, "parse_python", code_boom)
    result = resolve_python_config_fact(py_ev, "LIMIT")
    assert result.limitation_codes == ("analysis_failed",)
    assert "python_parser_unavailable" not in ",".join(result.limitation_codes)
