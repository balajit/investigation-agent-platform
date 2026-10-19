# tests/unit/test_git_multi_repo.py
"""Multi-repo code resolution (all services x repos, first hit wins)."""

from __future__ import annotations

from pathlib import Path


def _profile(services):
    from unittest.mock import MagicMock

    profile = MagicMock()
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


def _git_repo(path: Path, files: dict[str, str]) -> None:
    import pygit2

    repo = pygit2.init_repository(str(path))
    for rel, content in files.items():
        target = path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        repo.index.add(rel)
    repo.index.write()
    author = pygit2.Signature("t", "t@example.com")
    repo.create_commit("HEAD", author, author, "init", repo.index.write_tree(), [])


class TestAllLocators:
    def test_services_x_repos_order(self) -> None:
        from investigation_agent_platform.infrastructure.evidence.code.git import (
            PyGit2Adapter,
        )

        profile = _profile(
            [
                _service("o1", ["r1", "r2"], branch="dev"),
                _service("o2", ["r3"]),
                _service("", ["bare"]),
            ]
        )
        assert PyGit2Adapter._all_locators(profile) == [
            ("o1/r1", "dev"),
            ("o1/r2", "dev"),
            ("o2/r3", "main"),
            ("bare", "main"),
        ]

    def test_service_locators_cover_all_repos(self) -> None:
        from investigation_agent_platform.infrastructure.evidence.code.git import (
            PyGit2Adapter,
        )

        profile = _profile([_service("o1", ["r1", "r2"])])
        assert PyGit2Adapter._service_locators(profile) == ["o1/r1", "o1/r2"]

    def test_traversal_guard(self, tmp_path) -> None:
        import pytest

        from investigation_agent_platform.domain.common.exceptions import ExecutionError
        from investigation_agent_platform.infrastructure.evidence.code.git import (
            PyGit2Adapter,
        )

        adapter = PyGit2Adapter(repo_base_path=str(tmp_path))
        profile = _profile([_service("o1", ["../../escape"])])
        with pytest.raises(ExecutionError):
            adapter._resolve_repo_paths(profile)


class TestFetchBlobMultiRepo:
    def test_second_repo_wins_on_miss(self, tmp_path) -> None:
        from investigation_agent_platform.infrastructure.evidence.code.git import (
            PyGit2Adapter,
        )

        r1 = tmp_path / "o1" / "r1"
        r2 = tmp_path / "o1" / "r2"
        r1.mkdir(parents=True)
        r2.mkdir(parents=True)
        _git_repo(r1, {"a.txt": "from-one"})
        _git_repo(r2, {"b.txt": "from-two"})
        adapter = PyGit2Adapter(repo_base_path=str(tmp_path))
        profile = _profile([_service("o1", ["r1", "r2"])])

        text, _commit, tree_path = adapter._fetch_blob(r2, "o1/r2", "main", "b.txt", profile, None)
        assert "from-two" in text

        # Full path: file only in second repo resolves through the loop.
        roots = adapter._resolve_repo_paths(profile)
        assert [locator for locator, _, _ in roots] == ["o1/r1", "o1/r2"]
        hits = []
        for _locator, _branch, _path in roots:
            try:
                out, _, _ = adapter._fetch_blob(_path, _locator, _branch, "b.txt", profile, None)
                hits.append(out)
                break
            except Exception:
                continue
        assert hits and "from-two" in hits[0]

    def test_miss_names_all_roots(self, tmp_path) -> None:
        import pytest

        from investigation_agent_platform.domain.common.exceptions import (
            CodePathNotFoundError,
        )
        from investigation_agent_platform.infrastructure.evidence.code.git import (
            PyGit2Adapter,
        )

        r1 = tmp_path / "o1" / "r1"
        r1.mkdir(parents=True)
        _git_repo(r1, {"a.txt": "x"})
        adapter = PyGit2Adapter(repo_base_path=str(tmp_path))
        profile = _profile([_service("o1", ["r1", "r2"])])
        # r2 does not exist on disk: _open_repo misses, loop must name both.
        roots = [r for r in adapter._resolve_repo_paths(profile)]
        assert len(roots) == 2
        with pytest.raises(CodePathNotFoundError):
            adapter._fetch_blob(roots[1][2], "o1/r2", "main", "nope.txt", profile, None)


class TestCrossOrgEntries:
    def test_repo_own_org_wins(self) -> None:
        from investigation_agent_platform.infrastructure.evidence.code.git import (
            PyGit2Adapter,
        )

        assert PyGit2Adapter._split_repo_entry("svc-org", "other-org/shared-libs") == (
            "other-org/shared-libs"
        )
        assert PyGit2Adapter._split_repo_entry("svc-org", "plain-repo") == ("svc-org/plain-repo")
        assert PyGit2Adapter._split_repo_entry("", "bare") == "bare"
        assert PyGit2Adapter._split_repo_entry("o", "a/b/c") == "a/b/c"

    def test_all_locators_mixed_orgs(self) -> None:
        from investigation_agent_platform.infrastructure.evidence.code.git import (
            PyGit2Adapter,
        )

        profile = _profile(
            [
                _service("svc-org", ["main-repo", "other-org/shared-libs"]),
            ]
        )
        assert PyGit2Adapter._all_locators(profile) == [
            ("svc-org/main-repo", "main"),
            ("other-org/shared-libs", "main"),
        ]


class TestSearchProviderLocators:
    def test_all_repos_listed(self) -> None:
        from investigation_agent_platform.infrastructure.evidence.code.intelligence import (
            TreeSitterCodeIntelligenceProvider,
        )

        profile = _profile([_service("o1", ["r1", "other/r2"])])
        assert TreeSitterCodeIntelligenceProvider._service_locators(profile) == [
            "o1/r1",
            "other/r2",
        ]


class TestResolveRootsFallback:
    def test_flat_repo_falls_back_to_root(self, tmp_path) -> None:
        from investigation_agent_platform.infrastructure.evidence.code.intelligence import (
            TreeSitterCodeIntelligenceProvider,
        )

        nested = tmp_path / "o" / "r-nested"
        (nested / "service").mkdir(parents=True)
        flat = tmp_path / "o" / "r-flat"
        flat.mkdir(parents=True)
        (flat / "src").mkdir()
        provider = TreeSitterCodeIntelligenceProvider(repo_base_path=str(tmp_path))
        profile = _profile([_service("o", ["r-nested", "r-flat"], roots=["service"])])
        roots = provider._resolve_roots(profile)
        assert nested / "service" in roots
        assert flat in roots


class TestGraphArtifactFreshness:
    def _provider(self, tmp_path):
        from investigation_agent_platform.infrastructure.evidence.code.intelligence import (
            TreeSitterCodeIntelligenceProvider,
        )

        return TreeSitterCodeIntelligenceProvider(repo_base_path=str(tmp_path))

    def test_changed_artifact_reloads(self, tmp_path) -> None:
        import json
        import time

        provider = self._provider(tmp_path)
        locator_dir = tmp_path / "o" / "r1"
        locator_dir.mkdir(parents=True)
        artifact = locator_dir / "call_graph.json"
        artifact.write_text(json.dumps({"nodes": [{"id": "a"}], "links": []}))
        profile = _profile([_service("o", ["r1"])])
        first = provider._load_graphify_graph(profile)
        assert first is not None
        time.sleep(0.02)
        artifact.write_text(json.dumps({"nodes": [{"id": "a"}, {"id": "b"}], "links": []}))
        second = provider._load_graphify_graph(profile)
        assert second is not None
        assert second is not first

    def test_unchanged_artifact_cached(self, tmp_path) -> None:
        import json

        provider = self._provider(tmp_path)
        locator_dir = tmp_path / "o" / "r1"
        locator_dir.mkdir(parents=True)
        (locator_dir / "call_graph.json").write_text(json.dumps({"nodes": [], "links": []}))
        profile = _profile([_service("o", ["r1"])])
        assert provider._load_graphify_graph(profile) is provider._load_graphify_graph(profile)

    def test_missing_artifact_none(self, tmp_path) -> None:
        provider = self._provider(tmp_path)
        profile = _profile([_service("o", ["r1"])])
        assert provider._load_graphify_graph(profile) is None


class TestHonestRangeProvenance:
    """A range read larger than the snippet budget must claim only the
    lines the snippet actually contains (never the requested window),
    and disclose how to continue."""

    def _big_repo(self, tmp_path) -> tuple:
        from uuid import uuid4

        from investigation_agent_platform.infrastructure.evidence.code.git import (
            PyGit2Adapter,
        )

        repo_dir = tmp_path / "o1" / "r1"
        repo_dir.mkdir(parents=True)
        body = "\n".join(f"result = compute_{n}(value_{n})" for n in range(1, 301))
        _git_repo(repo_dir, {"svc/handler.py": body})
        adapter = PyGit2Adapter(repo_base_path=str(tmp_path))
        profile = _profile([_service("o1", ["r1"])])
        return adapter, profile, uuid4()

    def test_oversize_range_claims_only_included_lines(self, tmp_path) -> None:
        adapter, profile, iid = self._big_repo(tmp_path)
        ev = adapter._get_source_sync("tenant-a", iid, "svc/handler.py", profile, None, 1, 300)
        loc = ev.provenance.source_location
        assert loc.line_start == 1
        assert loc.line_end < 300
        assert ev.attributes["truncated"] is True
        assert ev.attributes["next_start_line"] == loc.line_end + 1
        assert ev.attributes["requested_end_line"] == 300
        assert len(ev.content_snippet) <= 4096
        numbered = [
            int(raw.split("|", 1)[0])
            for raw in ev.content_snippet.splitlines()
            if raw.split("|", 1)[0].strip().isdigit()
        ]
        assert numbered
        assert min(numbered) == loc.line_start
        assert max(numbered) == loc.line_end

    def test_fitting_range_is_unchanged_and_untruncated(self, tmp_path) -> None:
        adapter, profile, iid = self._big_repo(tmp_path)
        ev = adapter._get_source_sync("tenant-a", iid, "svc/handler.py", profile, None, 10, 20)
        loc = ev.provenance.source_location
        assert (loc.line_start, loc.line_end) == (10, 20)
        assert ev.attributes["truncated"] is False
        assert "10| " in ev.content_snippet
