# tests/unit/test_j_shared_infra.py
"""J shared infra: limitation registry (D1), window result (D2), line splitter (D3), grammar (D6)."""

import ast
import pathlib
import re

import pytest

SRC = pathlib.Path("src/investigation_agent_platform")


def _emitted_codes() -> set[str]:
    """Every literal limitation code analyzers can emit."""
    found: set[str] = set()
    for path in list((SRC / "application/analysis").rglob("*.py")) + [
        SRC / "domain/code/models.py",
    ]:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, OSError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "unresolved":
                if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                    found.add(node.args[0].value)
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "CodeFactResolutionV2"
            ):
                for kw in node.keywords:
                    if kw.arg == "limitation_codes" and isinstance(kw.value, (ast.Tuple, ast.List)):
                        for elt in kw.value.elts:
                            if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                                found.add(elt.value)
    return found


def test_registry_covers_every_emitted_code() -> None:
    from investigation_agent_platform.application.analysis.limitations import (
        LimitationClass,
        _REGISTRY,
        limitation_class,
    )

    codes = _emitted_codes()
    assert codes, "the scan must find emitted codes"
    missing = sorted(c for c in codes if c not in _REGISTRY)
    assert missing == [], f"unregistered limitation codes: {missing}"
    assert limitation_class("no_such_code") == LimitationClass.UNSUPPORTED
    assert limitation_class("analysis_failed:ValueError") == LimitationClass.FAILURE


def test_split_source_lines_matches_git_counting() -> None:
    from investigation_agent_platform.domain.code.lines import split_source_lines

    assert split_source_lines("") == []
    assert split_source_lines("a") == ["a"]
    assert split_source_lines("a\n") == ["a"]
    assert split_source_lines("a\nb\n") == ["a", "b"]
    assert split_source_lines("a\r\nb\r\n") == ["a", "b"]
    assert split_source_lines("a\fb") == ["a\fb"]
    assert split_source_lines("a b") == ["a b"]
    assert split_source_lines("a\rb") == ["a\rb"]
    assert split_source_lines("\n") == [""]
    assert split_source_lines("a\n\nb\n") == ["a", "", "b"]


def test_window_analysis_serializes() -> None:
    from investigation_agent_platform.application.analysis.java.window_result import (
        WindowAnalysis,
        WindowStatus,
    )

    w = WindowAnalysis(status=WindowStatus.DEGRADED, limitations=("symbol_not_in_window",), omitted=2)
    attr = w.to_attribute()
    assert attr == {"status": "degraded", "limitations": ["symbol_not_in_window"], "failure_type": None, "omitted": 2}


def test_window_analysis_key() -> None:
    from investigation_agent_platform.domain.code.models import WINDOW_ANALYSIS_KEY

    assert WINDOW_ANALYSIS_KEY == "window_analysis"


def test_identity_format() -> None:
    import re as _re

    from investigation_agent_platform.application.analysis import grammar as _g

    for lang in ("java", "python"):
        ident = _g.identity(lang)
        assert _re.match(r"^tree-sitter-(java|python);pack=\d+\.\d+\.\d+;ts=\d+\.\d+\.\d+;abi=(\d+|unavailable)$", ident), ident
        assert len(ident) <= 64


def test_identity_tracks_versions(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from investigation_agent_platform.application.analysis import grammar as _g

    real_version, real_abi = _g._package_version, dict(_g._abi)
    monkeypatch.setattr(_g, "_package_version", lambda name: "9.9.9" if "pack" in name else "1.2.3")
    _g._abi.clear()
    try:
        assert "pack=9.9.9" in _g.identity("java")
    finally:
        _g._abi.clear()
        _g._abi.update(real_abi)


def test_identity_never_raises(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from investigation_agent_platform.application.analysis import grammar as _g
    from investigation_agent_platform.application.analysis.failure import GrammarUnavailable

    real_abi = dict(_g._abi)
    monkeypatch.setattr(_g, "language", lambda name: (_ for _ in ()).throw(GrammarUnavailable("x")))
    _g._abi.clear()
    try:
        assert _g.identity("java").endswith("abi=unavailable")
    finally:
        _g._abi.clear()
        _g._abi.update(real_abi)


def test_loader_failure_is_typed(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from investigation_agent_platform.application.analysis import grammar as _g
    from investigation_agent_platform.application.analysis.failure import GrammarUnavailable

    class DownloadError(Exception):
        pass

    import tree_sitter_language_pack as _pack

    monkeypatch.setattr(_pack, "get_language", lambda name: (_ for _ in ()).throw(DownloadError("net")))
    _g._languages.pop("java", None)
    _g._failed_at.pop("java", None)
    try:
        with pytest.raises(GrammarUnavailable) as exc:
            _g.language("java")
        assert exc.value.__cause__ is not None and type(exc.value.__cause__).__name__ == "DownloadError"
    finally:
        _g._failed_at.pop("java", None)
    with pytest.raises(GrammarUnavailable):
        _g.language("cobol")


def test_negative_cache_bounds_retries(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import time as _time

    from investigation_agent_platform.application.analysis import grammar as _g
    from investigation_agent_platform.application.analysis.failure import GrammarUnavailable

    calls: list = []
    now = [1000.0]
    monkeypatch.setattr(_time, "monotonic", lambda: now[0])
    monkeypatch.setattr(_g.time, "monotonic", lambda: now[0])
    import tree_sitter_language_pack as _pack

    def boom(name):  # type: ignore[no-untyped-def]
        calls.append(name)
        raise RuntimeError("down")

    monkeypatch.setattr(_pack, "get_language", boom)
    _g._languages.pop("java", None)
    _g._failed_at.pop("java", None)
    try:
        for _ in range(5):
            with pytest.raises(GrammarUnavailable):
                _g.language("java")
        assert calls == ["java"]
        now[0] += 61.0
        with pytest.raises(GrammarUnavailable):
            _g.language("java")
        assert calls == ["java", "java"]
    finally:
        _g._failed_at.pop("java", None)


def test_parsers_are_per_thread_languages_shared() -> None:
    import threading

    from investigation_agent_platform.application.analysis import grammar as _g

    seen: dict[str, object] = {}

    def work(tag: str) -> None:
        seen[tag] = (_g.parser("java"), _g.parser("java"), _g.language("java"))

    threads = [threading.Thread(target=work, args=(f"t{i}",)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert seen["t0"][0] is seen["t0"][1]
    assert seen["t0"][0] is not seen["t1"][0]
    assert seen["t0"][2] is seen["t1"][2]


def test_queries_are_cached() -> None:
    from investigation_agent_platform.application.analysis import grammar as _g

    pattern = "(identifier) @x"
    first = _g.query("java", pattern)
    assert _g.query("java", pattern) is first


def test_preflight_reports_missing(monkeypatch, caplog) -> None:  # type: ignore[no-untyped-def]
    import logging

    from investigation_agent_platform.application.analysis import grammar as _g
    from investigation_agent_platform.application.analysis.failure import GrammarUnavailable
    from investigation_agent_platform.bootstrap.worker import preflight_grammars

    def fake_parser(name):  # type: ignore[no-untyped-def]
        if name == "java":
            raise GrammarUnavailable("x")

        class P:  # type: ignore[no-untyped-def]
            def parse(self, data):  # type: ignore[no-untyped-def]
                return object()

        return P()

    monkeypatch.setattr(_g, "parser", fake_parser)
    with caplog.at_level(logging.ERROR):
        assert preflight_grammars() == ["java"]


@pytest.mark.xfail(strict=True, reason="dependency pins require owner approval")
def test_pins_are_exact() -> None:
    import pathlib as _pathlib

    text = (_pathlib.Path("pyproject.toml")).read_text()
    assert '"tree-sitter==0.26.0"' in text or '"tree-sitter==0.26.0",' in text
    assert "tree-sitter-language-pack==1.20.0" in text
