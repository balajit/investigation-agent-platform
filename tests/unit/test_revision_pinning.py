# tests/unit/test_revision_pinning.py
"""Revision provenance guards (Wave-1 audit follow-up).

A row must never carry a fabricated revision, and a missing revision
must never satisfy a same-revision join:

- The live-checkout search provider banks ``revision=None`` when HEAD
  is unresolvable — never a query-derived hash shaped like a revision.
- ``_same_pinned_revision`` is False unless both sides name the same
  established revision (unknown != unknown, unknown != pinned).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock
from uuid import uuid4

from investigation_agent_platform.application.investigation.verification import (
    _same_pinned_revision,
)


def _profile():
    svc = MagicMock()
    svc.name = "svc"
    code = MagicMock()
    code.org = "acme"
    code.repos = ["svc"]
    svc.code = code
    profile = MagicMock()
    profile.services = [svc]
    return profile


def _adapter(repo_base: Path):
    from investigation_agent_platform.infrastructure.evidence.code.intelligence import (
        TreeSitterCodeIntelligenceProvider,
    )

    return TreeSitterCodeIntelligenceProvider(repo_base_path=str(repo_base))


def test_same_pinned_revision_matrix() -> None:
    assert _same_pinned_revision("abc123", "abc123") is True
    assert _same_pinned_revision("abc123", "def456") is False
    assert _same_pinned_revision("", "abc123") is False
    assert _same_pinned_revision("abc123", "") is False
    assert _same_pinned_revision("", "") is False
    assert _same_pinned_revision(None, None) is False
    assert _same_pinned_revision(None, "abc123") is False
    assert _same_pinned_revision("  ", "abc123") is False


def test_non_git_checkout_banks_no_revision(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "acme" / "svc"
    root.mkdir(parents=True)
    (root / "mod.py").write_text("def handler(order_id):\n    return order_id\n", encoding="utf-8")
    adapter = _adapter(tmp_path)
    monkeypatch.setattr(type(adapter), "_repo_head_sha", staticmethod(lambda path: None))
    rows = adapter._search_code_sync("t", uuid4(), "handler", _profile())
    assert rows, "expected hits in the fixture file"
    for row in rows:
        rev = row.provenance.source_location.revision
        assert rev is None, f"unresolvable checkout must not fabricate a revision, got {rev!r}"


def test_git_checkout_banks_real_oid(tmp_path: Path) -> None:
    import pygit2

    root = tmp_path / "acme" / "svc"
    root.mkdir(parents=True)
    repo = pygit2.init_repository(str(root))
    (root / "mod.py").write_text("def handler(order_id):\n    return order_id\n", encoding="utf-8")
    repo.index.add("mod.py")
    repo.index.write()
    author = pygit2.Signature("t", "t@example.com")
    repo.create_commit("HEAD", author, author, "init", repo.index.write_tree(), [])
    adapter = _adapter(tmp_path)
    rows = adapter._search_code_sync("t", uuid4(), "handler", _profile())
    assert rows, "expected hits in the fixture file"
    tip = str(repo.revparse_single("HEAD").id)
    for row in rows:
        assert row.provenance.source_location.revision == tip
