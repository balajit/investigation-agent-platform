# tests/unit/test_git_history_basename.py
"""History bare-basename resolution (live L3 041339 class).

A get_code_history request for bare "OrderServiceImpl.java" used to walk
commit.tree["OrderServiceImpl.java"] (always KeyError), bank zero rows, and
let the caller certify "no recent changes" from a failed lookup — while the
file had changed in every commit. Bare names now resolve against the tip
tree (unique match) or fail closed with CodePathNotFoundError, which degrades
as a miss and never banks an empty-search proof.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock
from uuid import uuid4

import pytest


def _profile(services):
    profile = MagicMock()
    profile.services = services
    return profile


def _service(org, repos, branch="main", roots=None):
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


IMPL = "service/src/main/java/com/sample/orders/service/OrderServiceImpl.java"
IMPL_V1 = "package com.sample.orders.service;\npublic class OrderServiceImpl {}\n"
IMPL_V2 = "package com.sample.orders.service;\npublic class OrderServiceImpl {\n boolean ok;\n}\n"


def _adapter(tmp_path: Path):
    from investigation_agent_platform.infrastructure.evidence.code.git import (
        PyGit2Adapter,
    )

    return PyGit2Adapter(repo_base_path=str(tmp_path))


def _history(adapter, tmp_path: Path, path: str, branch: str = "main"):
    from investigation_agent_platform.infrastructure.evidence.code.git import (
        PyGit2Adapter,
    )

    _ = PyGit2Adapter  # kept explicit: resolution lives on the adapter
    profile = _profile([_service("acme", ["svc"], branch=branch)])
    assert (tmp_path / "acme" / "svc").is_dir()
    return adapter._get_code_history_sync("t", uuid4(), path, profile)


def test_bare_basename_resolves_to_nested_history(tmp_path: Path) -> None:
    root = tmp_path / "acme" / "svc"
    root.mkdir(parents=True)
    _git_repo(root, {IMPL: IMPL_V1}, message="init")
    _git_repo(root, {IMPL: IMPL_V2}, message="change")
    rows = _history(_adapter(tmp_path), tmp_path, "OrderServiceImpl.java")
    assert len(rows) == 2
    assert all(r.evidence_type.value == "COMMIT_HISTORY" for r in rows)


def test_unknown_basename_fails_closed(tmp_path: Path) -> None:
    from investigation_agent_platform.domain.common.exceptions import (
        CodePathNotFoundError,
    )

    root = tmp_path / "acme" / "svc"
    root.mkdir(parents=True)
    _git_repo(root, {IMPL: IMPL_V1})
    with pytest.raises(CodePathNotFoundError):
        _history(_adapter(tmp_path), tmp_path, "NoSuchFile.java")


def test_ambiguous_basename_fails_closed(tmp_path: Path) -> None:
    from investigation_agent_platform.domain.common.exceptions import (
        CodePathNotFoundError,
    )

    root = tmp_path / "acme" / "svc"
    root.mkdir(parents=True)
    _git_repo(
        root,
        {
            "a/OrderServiceImpl.java": IMPL_V1,
            "b/OrderServiceImpl.java": IMPL_V1,
        },
    )
    with pytest.raises(CodePathNotFoundError):
        _history(_adapter(tmp_path), tmp_path, "OrderServiceImpl.java")
