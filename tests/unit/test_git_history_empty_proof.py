# tests/unit/test_git_history_empty_proof.py
"""History empty-walk proof row (D1 producer, claim-proof gaps).

When the Git history walk completes with zero touching commits, the
provider returns a single proof-capable zero-match row
(COMMIT_HISTORY, non-discovery, file+tip-revision) instead of []. Only
the provider can do this: repository, resolved path, and searched tip
are all in scope there; CodeHistoryRequest carries no revision and the
port returns a bare list. Unresolvable cases (bare-basename ambiguity,
unborn HEAD) raise — never a proof row.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest


def _profile(services):
    profile = __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock()
    profile.services = services
    return profile


def _service(org, repos, branch="main", roots=None):
    from unittest.mock import MagicMock

    code = MagicMock()
    code.org = org
    code.repos = repos
    code.default_branch = branch
    code.source_roots = roots or []
    code.paths = []
    svc = MagicMock()
    svc.code = code
    svc.name = "svc"
    return svc


def _git_repo(path: Path, files: dict[str, str], message: str = "init") -> None:
    import pygit2

    repo = pygit2.init_repository(str(path))
    for rel, content in files.items():
        target = path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        repo.index.add(rel)
    repo.index.write()
    author = pygit2.Signature("t", "t@example.com")
    parents = [] if repo.head_is_unborn else [repo.head.target]
    repo.create_commit("HEAD", author, author, message, repo.index.write_tree(), parents)


def _adapter(tmp_path: Path):
    from investigation_agent_platform.infrastructure.evidence.code.git import (
        PyGit2Adapter,
    )

    return PyGit2Adapter(repo_base_path=str(tmp_path))


def _history(adapter, tmp_path: Path, path: str, branch: str = "main"):
    profile = _profile([_service("acme", ["svc"], branch=branch)])
    assert (tmp_path / "acme" / "svc").is_dir()
    return adapter._get_code_history_sync("t", uuid4(), path, profile)


IMPL = "service/src/main/java/com/sample/orders/service/OrderServiceImpl.java"
IMPL_V1 = "package com.sample.orders.service;\npublic class OrderServiceImpl {}\n"


def test_empty_walk_returns_single_proof_row(tmp_path: Path) -> None:
    root = tmp_path / "acme" / "svc"
    root.mkdir(parents=True)
    _git_repo(root, {IMPL: IMPL_V1})
    rows = _history(_adapter(tmp_path), tmp_path, "service/never/existed.txt")
    assert len(rows) == 1
    row = rows[0]
    assert row.evidence_type.value == "COMMIT_HISTORY"
    assert row.attributes["match_count"] == 0
    assert row.attributes.get("discovery_only") is not True
    assert "history=" in row.attributes["search_scope"]
    location = row.provenance.source_location
    assert location.file_path == "service/never/existed.txt"
    assert location.revision and len(location.revision) == 40

    import pygit2

    tip = pygit2.Repository(str(root)).revparse_single("HEAD")
    assert location.revision == str(tip.id)
    assert f"@{tip.id}:history=" in row.attributes["search_scope"]


def test_proof_row_fingerprint_differs_from_discovery_row(tmp_path: Path) -> None:
    from investigation_agent_platform.application.worker.activities import (
        _absence_proof_row,
    )
    from investigation_agent_platform.domain.evidence.models import EvidenceType

    root = tmp_path / "acme" / "svc"
    root.mkdir(parents=True)
    _git_repo(root, {IMPL: IMPL_V1})
    (proof,) = _history(_adapter(tmp_path), tmp_path, "service/never/existed.txt")
    discovery = _absence_proof_row(
        tenant_id="t",
        investigation_id=uuid4(),
        evidence_type=EvidenceType.SOURCE_CODE,
        provider="code-intelligence",
        operation="get_code_history",
        scope="repo:history=service/never/existed.txt",
    )
    assert proof.fingerprint != discovery.fingerprint
    assert proof.evidence_type.value == "COMMIT_HISTORY"
    assert discovery.evidence_type.value == "SOURCE_CODE"


def test_unborn_head_raises_no_proof_row(tmp_path: Path) -> None:
    import pygit2

    from investigation_agent_platform.domain.common.exceptions import ExecutionError

    root = tmp_path / "acme" / "svc"
    root.mkdir(parents=True)
    pygit2.init_repository(str(root))
    with pytest.raises(ExecutionError):
        _history(_adapter(tmp_path), tmp_path, "service/never/existed.txt")


def test_touched_file_still_returns_positive_rows_only(tmp_path: Path) -> None:
    root = tmp_path / "acme" / "svc"
    root.mkdir(parents=True)
    _git_repo(root, {IMPL: IMPL_V1}, message="init")
    rows = _history(_adapter(tmp_path), tmp_path, IMPL)
    assert len(rows) == 1
    assert rows[0].evidence_type.value == "COMMIT_HISTORY"
    assert rows[0].attributes.get("match_count", 1) != 0


def test_touch_before_cap_still_found(tmp_path: Path) -> None:
    """The 50 cap counts touching commits banked, not commits visited.

    A file touched only in the oldest commit of a 60-commit history must
    still be found — the walk runs to exhaustion when touches < cap.
    """
    import pygit2

    root = tmp_path / "acme" / "svc"
    root.mkdir(parents=True)
    repo = pygit2.init_repository(str(root))
    author = pygit2.Signature("t", "t@example.com")

    def _commit(files: dict[str, str], message: str) -> None:
        for rel, content in files.items():
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            repo.index.add(rel)
        repo.index.write()
        parents = [] if repo.head_is_unborn else [repo.head.target]
        repo.create_commit("HEAD", author, author, message, repo.index.write_tree(), parents)

    _commit({IMPL: IMPL_V1}, "init with target")
    for i in range(59):
        _commit({f"churn/file{i}.txt": f"v{i}\n"}, f"churn {i}")
    rows = _history(_adapter(tmp_path), tmp_path, IMPL)
    assert len(rows) == 1
    assert rows[0].evidence_type.value == "COMMIT_HISTORY"
    assert rows[0].attributes.get("match_count", 1) != 0
    # The proof row must never appear alongside positive history.
    assert all(r.attributes.get("match_count", 1) != 0 for r in rows)


def test_never_touched_in_large_history_banks_proof(tmp_path: Path) -> None:
    """60 commits, none touching the queried path: the exhausted walk
    banks exactly one proof row carrying the searched tip."""
    import pygit2

    root = tmp_path / "acme" / "svc"
    root.mkdir(parents=True)
    repo = pygit2.init_repository(str(root))
    author = pygit2.Signature("t", "t@example.com")
    for i in range(60):
        target = root / f"churn/file{i}.txt"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"v{i}\n", encoding="utf-8")
        repo.index.add(f"churn/file{i}.txt")
    repo.index.write()
    repo.create_commit(
        "HEAD", author, author, "bulk", repo.index.write_tree(), []
    )
    rows = _history(_adapter(tmp_path), tmp_path, "service/never/existed.txt")
    assert len(rows) == 1
    row = rows[0]
    assert row.evidence_type.value == "COMMIT_HISTORY"
    assert row.attributes["match_count"] == 0
    tip = pygit2.Repository(str(root)).revparse_single("HEAD")
    assert row.provenance.source_location.revision == str(tip.id)


class TestBankedPositive:
    def _row(self, **attrs):
        return SimpleNamespace(attributes=dict(attrs))

    def test_empty_is_not_positive(self) -> None:
        from investigation_agent_platform.application.worker.activities import (
            _banked_positive,
        )

        assert _banked_positive([]) is False

    def test_zero_match_marker_is_not_positive(self) -> None:
        from investigation_agent_platform.application.worker.activities import (
            _banked_positive,
        )

        assert _banked_positive([self._row(match_count=0)]) is False

    def test_positive_history_row_is_positive(self) -> None:
        from investigation_agent_platform.application.worker.activities import (
            _banked_positive,
        )

        assert _banked_positive([self._row(match_count=2)]) is True

    def test_row_without_match_count_is_positive(self) -> None:
        from investigation_agent_platform.application.worker.activities import (
            _banked_positive,
        )

        assert _banked_positive([self._row(search_scope="x")]) is True

    def test_proof_row_does_not_suppress_discovery(self) -> None:
        from investigation_agent_platform.application.worker.activities import (
            _banked_positive,
        )

        assert _banked_positive([self._row(match_count=0)]) is False
        assert (
            _banked_positive([self._row(match_count=0), self._row(match_count=1)])
            is True
        )
