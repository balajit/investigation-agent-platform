# tests/unit/test_code_intelligence_test_exclusion.py
"""JUnit/test sources never surface as production-code lookup candidates.

SEARCH_CODE, FIND_SYMBOL, and the call-graph fallback enumerate through
_iter_source_files; the graphify fast path filters at load. Test doubles
under src/test/** or *Test.java must not back a conclusion about runtime
behavior. Explicit GET_CODE reads are out of scope here (named-file reads
are judged by the verifier, not the lookup layer).
"""

from __future__ import annotations

from pathlib import Path

MAIN_FOO = """package com.example.svc;

public class Foo {
    public String greet(String name) {
        return "hi " + name;
    }
}
"""

TEST_FOO = """package com.example.svc;

import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.assertEquals;

public class FooTest {
    @Test
    public void greetReturnsHi() {
        Foo foo = new Foo();
        assertEquals("hi bob", foo.greet("bob"));
    }
}
"""

MAIN_BAR = """package com.example.svc;

public class Bar {
    public String shout(String name) {
        Foo foo = new Foo();
        return foo.greet(name).toUpperCase();
    }
}
"""


def _layout(base: Path) -> Path:
    repo = base / "org" / "repo"
    (repo / "src/main/java/com/example/svc").mkdir(parents=True)
    (repo / "src/test/java/com/example/svc").mkdir(parents=True)
    (repo / "src/main/java/com/example/svc/Foo.java").write_text(MAIN_FOO)
    (repo / "src/main/java/com/example/svc/Bar.java").write_text(MAIN_BAR)
    (repo / "src/test/java/com/example/svc/FooTest.java").write_text(TEST_FOO)
    return repo


def _profile():
    from unittest.mock import MagicMock

    code = MagicMock()
    code.org = "org"
    code.repos = ["repo"]
    code.default_branch = "main"
    code.source_roots = []
    code.paths = []
    svc = MagicMock()
    svc.code = code
    svc.name = "svc"
    profile = MagicMock()
    profile.services = [svc]
    return profile


def _provider(base: Path):
    from investigation_agent_platform.infrastructure.evidence.code.intelligence import (
        TreeSitterCodeIntelligenceProvider,
    )

    return TreeSitterCodeIntelligenceProvider(repo_base_path=str(base))


class TestIsTestSource:
    def test_predicate(self) -> None:
        from investigation_agent_platform.infrastructure.evidence.code.intelligence import (
            is_test_source,
        )

        assert is_test_source("org/repo/src/test/java/com/FooTest.java")
        assert is_test_source("org/repo/src/main/java/com/FooTest.java")
        assert is_test_source("org/repo/src/main/java/com/FooIT.java")
        assert is_test_source("org/repo/tests/test_foo.py")
        assert is_test_source("org/repo/pkg/foo/bar_test.go")
        assert not is_test_source("org/repo/src/main/java/com/Foo.java")
        assert not is_test_source("org/repo/src/main/java/com/testing/Foo.java")
        assert not is_test_source("org/repo/src/main/resources/application.yaml")


class TestDiscoveryExcludesTests:
    def test_iter_source_files_skips_tests(self, tmp_path: Path) -> None:
        _layout(tmp_path)
        provider = _provider(tmp_path)
        roots = [tmp_path / "org" / "repo"]
        files = provider._iter_source_files(roots)
        rel = sorted(str(f.relative_to(tmp_path)).replace("\\", "/") for f in files)
        assert rel == [
            "org/repo/src/main/java/com/example/svc/Bar.java",
            "org/repo/src/main/java/com/example/svc/Foo.java",
        ]

    def test_search_code_never_returns_test_rows(self, tmp_path: Path) -> None:
        import uuid

        _layout(tmp_path)
        provider = _provider(tmp_path)
        rows = provider._search_code_sync("t", uuid.uuid4(), "greet", _profile(), None, None, 20)
        assert rows, "expected production hits for 'greet'"
        for row in rows:
            loc = row.provenance.source_location
            assert "src/test" not in (loc.file_path or ""), row.provenance.source_location
            assert "FooTest" not in (loc.file_path or "")

    def test_find_symbol_ignores_test_declarations(self, tmp_path: Path) -> None:
        _layout(tmp_path)
        provider = _provider(tmp_path)
        syms = provider._find_symbol_sync("FooTest", _profile())
        assert syms == []
        syms = provider._find_symbol_sync("Foo", _profile())
        assert syms, "expected the production Foo declaration"
        assert all("src/test" not in (s.location.file_path or "") for s in syms)

    def test_graphify_load_drops_test_nodes(self, tmp_path: Path) -> None:
        import json

        repo = _layout(tmp_path)
        (repo / "call_graph.json").write_text(
            json.dumps(
                {
                    "nodes": [
                        {
                            "id": "m1",
                            "name": "greet",
                            "file_path": "org/repo/src/main/java/com/example/svc/Foo.java",
                        },
                        {
                            "id": "t1",
                            "name": "greetReturnsHi",
                            "file_path": "org/repo/src/test/java/com/example/svc/FooTest.java",
                        },
                    ],
                    "links": [{"source": "t1", "target": "m1"}],
                }
            )
        )
        provider = _provider(tmp_path)
        graph = provider._load_graphify_graph(_profile())
        assert graph is not None
        names = set()
        for idx in graph.node_indices():
            data = graph.get_node_data(idx)
            if isinstance(data, dict):
                names.add(data.get("name"))
        assert names == {"greet"}
