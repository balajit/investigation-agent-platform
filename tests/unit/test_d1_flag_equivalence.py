"""D1 flag contract + no-authoritative-consumer (D1-G/H).

Proves the §6 contract executable-style, not prose-style:
- the flag defaults off and parses strictly ("1" only);
- no module outside the D1 pair imports it (static scan);
- the D1 pair imports nothing that persists, executes, or calls out;
- enabling the flag changes no kernel verdict (publication decision
  and canonical identity are flag-indifferent at runtime; no kernel
  consumer references the flag at all).
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

from investigation_agent_platform.application.investigation.action_policy import (
    PlannerSnapshot,
    decide,
)
from investigation_agent_platform.domain.proof import dispatch as dispatch_mod
from investigation_agent_platform.domain.proof import edge_observation as edge_mod
from investigation_agent_platform.domain.proof.canonical import proposition_id
from investigation_agent_platform.domain.proof.dispatch import typed_edges_enabled
from investigation_agent_platform.domain.proof.publication import (
    PublicationRequest,
    decide_publication,
)

_REPO = Path(__file__).parent.parent.parent
_EDGE_MODULES = {
    "investigation_agent_platform.domain.proof.edge_observation",
    "investigation_agent_platform.domain.proof.dispatch",
}
_ALLOWED_TOP_IMPORTS = {
    "action_policy": {"__future__", "enum", "typing", "pydantic", "investigation_agent_platform"},
    "replay": {"__future__", "typing", "pydantic", "investigation_agent_platform"},
    "edge_observation": {
        "__future__",
        "enum",
        "hashlib",
        "re",
        "typing",
        "unicodedata",
        "pydantic",
        "investigation_agent_platform",
    },
    "dispatch": {
        "__future__",
        "enum",
        "hashlib",
        "os",
        "typing",
        "pydantic",
        "investigation_agent_platform",
    },
}


def _module_ast(name: str) -> ast.Module:
    root = _REPO / "src" / "investigation_agent_platform"
    if name == "action_policy":
        path = root / "application" / "investigation" / "action_policy.py"
    elif name == "replay":
        path = root / "application" / "investigation" / "replay.py"
    elif name in ("edge_observation", "dispatch"):
        path = root / "domain" / "proof" / f"{name}.py"
    else:
        raise AssertionError(name)
    return ast.parse(path.read_text())


def test_d1_g01_flag_default_off() -> None:
    saved = os.environ.pop("IAP_TYPED_EDGES", None)
    try:
        assert typed_edges_enabled() is False
    finally:
        if saved is not None:
            os.environ["IAP_TYPED_EDGES"] = saved


@pytest.mark.parametrize("value", ["true", "yes", "on", "0", " 1 ", "2", "TRUE"])
def test_d1_g02_flag_parses_strictly(value: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IAP_TYPED_EDGES", value)
    assert typed_edges_enabled() is False
    monkeypatch.setenv("IAP_TYPED_EDGES", "1")
    assert typed_edges_enabled() is True


def test_d1_g03_d1_pair_imports_nothing_effectful() -> None:
    for module in ("edge_observation", "dispatch"):
        tree = _module_ast(module)
        top = {
            (n.names[0].name if isinstance(n, ast.Import) else n.module or "").split(".")[0]
            for n in ast.walk(tree)
            if isinstance(n, (ast.Import, ast.ImportFrom))
        }
        assert top <= _ALLOWED_TOP_IMPORTS[module], (module, top)


def test_d1_g04_env_read_is_flag_only() -> None:
    text = (
        _REPO / "src" / "investigation_agent_platform" / "domain" / "proof" / "dispatch.py"
    ).read_text()
    assert text.count("os.environ") == 1
    assert "IAP_TYPED_EDGES" in text


def _importing_modules() -> dict[str, set[str]]:
    """Map: D1 module -> set of importing files (repo-wide, tests excluded)."""
    hits: dict[str, set[str]] = {m: set() for m in _EDGE_MODULES}
    for path in list((_REPO / "src").rglob("*.py")) + list((_REPO / "scripts").rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text())
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "") in _EDGE_MODULES:
                hits[node.module].add(str(path.relative_to(_REPO)))
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in _EDGE_MODULES:
                        hits[alias.name].add(str(path.relative_to(_REPO)))
    return hits


def test_d1_h01_no_kernel_consumer_imports_d1() -> None:
    hits = _importing_modules()
    assert hits["investigation_agent_platform.domain.proof.dispatch"] == set()
    assert hits["investigation_agent_platform.domain.proof.edge_observation"] == {
        "src/investigation_agent_platform/domain/proof/dispatch.py"
    }, hits


def test_d1_h02_no_kernel_module_references_flag() -> None:
    proof = _REPO / "src" / "investigation_agent_platform" / "domain" / "proof"
    offenders = [
        p.name
        for p in proof.glob("*.py")
        if p.stem not in ("edge_observation", "dispatch") and "TYPED_EDGES" in p.read_text()
    ]
    assert offenders == [], offenders


def test_d1_g05_flag_on_changes_no_kernel_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    req = PublicationRequest(
        checker_outcome="VALID",
        certificate_context_id="c",
        expected_context_id="c",
        derivation_id="d",
        reconciliation_adjudicated=True,
        policy_eligible=True,
    )
    prop = {"canon_version": "canon/v1", "predicate": "p", "quantifier": "UNIVERSAL"}
    snap = PlannerSnapshot(evidence_banked_total=2)
    monkeypatch.delenv("IAP_TYPED_EDGES", raising=False)
    off = (decide_publication(req), proposition_id(prop), decide(snap).model_dump())
    for value in ("1", "true", "yes"):
        monkeypatch.setenv("IAP_TYPED_EDGES", value)
        on = (decide_publication(req), proposition_id(prop), decide(snap).model_dump())
        assert on == off
    assert edge_mod.EDGE_RECORD_VERSION == "edge/v1"
    assert dispatch_mod.DISPATCH_TABLE_VERSION == "dispatch/v1"


def test_d1_g06_modules_expose_no_execution_entrypoint() -> None:
    banned = ("execute", "run", "publish", "commit", "persist", "save", "load", "store")
    for mod in (edge_mod, dispatch_mod):
        for name in (n for n in dir(mod) if not n.startswith("_")):
            lowered = name.lower()
            assert not any(lowered == w or lowered.startswith(w + "_") for w in banned), (
                mod.__name__,
                name,
            )
