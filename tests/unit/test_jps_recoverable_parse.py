# tests/unit/test_jps_recoverable_parse.py
"""JPS: the parser returns an outcome, keeps error-free declarations, says what was skipped.

A typo in one method must not remove every symbol of the file from the
topology graph. Nodes that themselves contain an error are skipped (J4: a
declaration is trusted only when its own subtree is error-free), but their
error-free descendants are kept and keep their parent linkage.
"""

from __future__ import annotations

import pytest

BROKEN = """\
public class OrderServiceHandler {
    public void a() {
        dispatch();
    }
    public int b( {
        return 1;
    }
    public void c() {
        dispatch();
    }
}
"""

FIXED = BROKEN.replace("public int b( {", "public int b() {")


def _parse(code: str):  # type: ignore[no-untyped-def]
    import asyncio

    from investigation_agent_platform.infrastructure.evidence.code.parser import (
        TreeSitterParser,
    )

    return asyncio.run(TreeSitterParser().parse_symbols(code, "java"))


def test_jps_t01_recoverable_symbols_survive() -> None:
    outcome = _parse(BROKEN)
    names = [s.name for s in outcome.symbols]
    assert "a" in names and "c" in names
    assert "b" not in names
    # The enclosing class node carries the error, so it is skipped too.
    assert "OrderServiceHandler" not in names
    assert outcome.has_errors is True
    assert outcome.skipped_error_nodes >= 1


def test_jps_t02_clean_file_unchanged() -> None:
    outcome = _parse(FIXED)
    names = [s.name for s in outcome.symbols]
    assert "OrderServiceHandler" in names
    assert {"a", "b", "c"} <= set(names)
    assert outcome.has_errors is False
    assert outcome.skipped_error_nodes == 0


def test_jps_t03_parent_linkage_survives_a_skipped_container() -> None:
    code = """\
public class Outer {
    public int broken( {
        return 1;
    }
    public static class Inner {
        public void m() {
            work();
        }
    }
}
"""
    outcome = _parse(code)
    by_name = {s.name: s for s in outcome.symbols}
    assert "m" in by_name
    assert by_name["m"].parent_symbol == "Inner"


def test_jps_t04_diagnostic_reaches_the_payload(tmp_path) -> None:
    import asyncio

    from investigation_agent_platform.infrastructure.topology.payload import (
        build_payload_for_repo,
    )

    (tmp_path / "Broken.java").write_text(BROKEN, encoding="utf-8")
    (tmp_path / "Clean.java").write_text(FIXED, encoding="utf-8")
    payload, _ = asyncio.run(
        build_payload_for_repo(tmp_path, "t", "app", "repo", "abc123")
    )
    warnings = [
        d for d in payload.diagnostics if d.severity == "WARNING" and "Broken.java" in d.file_path
    ]
    assert warnings, "the broken file must surface a syntax warning"
    assert any("skipped" in w.message and "recoverable" in w.message for w in warnings)
    clean_methods = [
        n.name
        for n in payload.ast_nodes
        if "Clean.java" in n.file_path and n.name in ("a", "b", "c")
    ]
    assert {"a", "b", "c"} <= set(clean_methods)


def test_jps_t05_size_guard_unchanged() -> None:
    import asyncio

    from investigation_agent_platform.domain.common.exceptions import ExecutionError
    from investigation_agent_platform.infrastructure.evidence.code.parser import (
        MAX_CODE_SIZE_BYTES,
        TreeSitterParser,
    )

    with pytest.raises(ExecutionError):
        asyncio.run(
            TreeSitterParser().parse_symbols("x" * (MAX_CODE_SIZE_BYTES + 1), "java")
        )
