# tests/unit/test_jni_node_identity.py
"""JNI: tree-sitter Node wrappers compare by span, never by `is`.

py-tree-sitter hands out a fresh ``Node`` wrapper per access, so ``is``
between separately obtained wrappers is always false. The two latent sites
are fixed to span comparison (``_p.same_node``) and a lint guard pins the rule.
"""

from __future__ import annotations

import re
from pathlib import Path


def _parse_java(source: str):  # type: ignore[no-untyped-def]
    from investigation_agent_platform.application.analysis.java import _parse as _p

    return _p.parse_java(source.encode("utf-8"))


def test_jni_t01_wrapper_identity_premise() -> None:
    tree = _parse_java("class A {}")
    assert (tree.root_node is tree.root_node) is False
    assert tree.root_node == tree.root_node


def test_jni_t02_sibling_field_scan_excludes_the_declaration() -> None:
    from investigation_agent_platform.application.analysis.java import _parse as _p
    from investigation_agent_platform.application.analysis.java.boundary_facts import (
        _with_field_context,
    )

    source = b"class C {\n    RestTemplate rest;\n    int n;\n}\n"
    tree = _parse_java(source.decode("utf-8"))
    declaration = next(
        node
        for node in _p.walk(tree.root_node)
        if node.type == "field_declaration" and b"rest" in _p.node_text(node, source).encode()
    )
    subtree = _p.node_text(declaration, source)
    assert _with_field_context(declaration, source, subtree).count(subtree.strip()) == 1


def test_jni_t03_permits_scan() -> None:
    from investigation_agent_platform.application.analysis.java import _parse as _p
    from investigation_agent_platform.application.analysis.java.type_facts import _permits_names

    source = b"sealed interface S permits A, B {}\n"
    tree = _parse_java(source.decode("utf-8"))
    declaration = next(
        node for node in _p.walk(tree.root_node) if node.type == "interface_declaration"
    )
    assert _permits_names(declaration, source) == ["A", "B"]


_ALLOWLIST = {
    # model.py: compares objects drawn from one list (same wrappers).
    "bound_node is bound_nodes[0]",
}


def test_jni_t04_lint_guard() -> None:
    root = Path(__file__).resolve().parents[2] / "src" / "investigation_agent_platform"
    nodeish = re.compile(r"\b(?:node|nodes|decl\w*|child\w*|parent\w*|declaration)\b", re.IGNORECASE)
    hits: list[str] = []
    for package in ("application/analysis/java", "application/analysis/python"):
        for path in sorted((root / package).rglob("*.py")):
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if "is not None" in line or " is None" in line:
                    continue
                match = re.search(r"\b(is not|is)\b", line)
                if not match or "same_node" in line:
                    continue
                sides = [s.strip() for s in re.split(r"\bis not\b|\bis\b", line)]
                if len(sides) == 2 and all(nodeish.search(s) for s in sides):
                    snippet = " ".join(line.split())
                    if snippet not in _ALLOWLIST:
                        hits.append(f"{path.name}:{lineno}: {snippet}")
    assert hits == [], f"node identity must use same_node: {hits}"
    # The guard itself must be capable of firing (negative control shape):
    assert any(
        " is " in f" {snippet} " for snippet in _ALLOWLIST
    ), "allowlist must pin the known exception"
