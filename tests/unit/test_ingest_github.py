# tests/unit/test_ingest_github.py
"""Part 7 loader unit tests (combined plan Phase 0).

Covers the loader's pure building blocks against the shared fixture repos in
``tests/fixtures/github_loader/``: repository-id derivation, token redaction,
source-file walking with skip rules, manifest rows, listing skip rules, and
fetch guards. Payload construction, registration, port ingest, and resume are
Phase 1–4 work with their own tests.
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path
from typing import ClassVar

import pytest

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "github_loader"


def _load_loader():  # type: ignore[no-untyped-def]
    path = Path(__file__).resolve().parents[2] / "scripts" / "ingest_github.py"
    spec = importlib.util.spec_from_file_location("ingest_github", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["ingest_github"] = module  # dataclasses resolve types via sys.modules
    spec.loader.exec_module(module)
    return module


loader = _load_loader()


def _stage_fixture(tmp_path: Path, name: str) -> Path:
    dest = tmp_path / name
    shutil.copytree(FIXTURES / name, dest)
    return dest


class TestRepositoryId:
    def test_stable_slug(self) -> None:
        assert loader.repository_id_for("Acme/Payments") == "acme--payments"

    def test_unsafe_chars_sanitized(self) -> None:
        rid = loader.repository_id_for("Acme/Pay$ments.Service")
        assert "/" not in rid and "$" not in rid
        assert rid == loader.repository_id_for("Acme/Pay$ments.Service")

    def test_prefix_and_length_cap(self) -> None:
        rid = loader.repository_id_for("o/" + "r" * 200, "t__")
        assert rid.startswith("t__")
        assert len(rid) <= len("t__") + 120


class TestTokenHygiene:
    def test_redacted_strips_token(self) -> None:
        assert loader._redacted("failed for abc123token", "abc123token") == "failed for [REDACTED]"

    def test_redacted_no_token_passthrough(self) -> None:
        assert loader._redacted("plain message", "") == "plain message"

    def test_authed_url_redactable(self) -> None:
        url = loader.authed_clone_url("https://github.com/acme/pay.git", "sekret")
        assert "sekret" in url
        assert "sekret" not in loader._redacted(f"clone {url} failed", "sekret")


class TestWalkSourceFiles:
    def test_fixture_files_found(self, tmp_path: Path) -> None:
        root = _stage_fixture(tmp_path, "acme--payments")
        rel = {p.relative_to(root).as_posix() for p in loader.walk_source_files(root)}
        assert "src/payments.py" in rel
        assert "src/app.js" in rel
        assert "schema.sql" in rel

    def test_skip_rules(self, tmp_path: Path) -> None:
        root = _stage_fixture(tmp_path, "acme--payments")
        (root / "node_modules").mkdir()
        (root / "node_modules" / "dep.js").write_text("x=1\n")
        (root / "package-lock.json").write_text("{}\n")
        (root / "empty.py").write_text("")
        (root / "blob.bin").write_bytes(b"\x00\x01binary")
        big = root / "generated_big.py"
        big.write_bytes(b"# pad\n" * (loader.MAX_FILE_BYTES // 6 + 10))
        rel = {p.relative_to(root).as_posix() for p in loader.walk_source_files(root)}
        assert "node_modules/dep.js" not in rel
        assert "package-lock.json" not in rel
        assert "empty.py" not in rel
        assert "blob.bin" not in rel
        assert "generated_big.py" not in rel
        assert "src/payments.py" in rel


class TestManifestRow:
    def test_ready_row_shape(self) -> None:
        repo = loader.RepoListing("acme/pay", "main", "https://x", False, False, False, False, 1, "Python")
        row = loader.manifest_row("t1", "acme", repo, "acme--pay", "deadbeef" * 5, "READY",
                                  {"node_count": 3, "edge_count": 1, "file_count": 2})
        assert isinstance(row, loader.IngestManifestRow)
        assert row.status == "READY"
        assert row.revision_sha == "deadbeef" * 5
        assert row.error_summary is None
        loader.IngestManifestRow.model_validate_json(row.model_dump_json())

    def test_error_truncated(self) -> None:
        repo = loader.RepoListing("acme/pay", "main", "https://x", False, False, False, False, 1, None)
        row = loader.manifest_row("t1", "acme", repo, "r", "sha", "FAILED", {}, "e" * 5000)
        assert row.error_summary is not None and len(row.error_summary) <= 2000

    def test_invalid_status_rejected(self) -> None:
        from pydantic import ValidationError

        repo = loader.RepoListing("acme/pay", "main", "https://x", False, False, False, False, 1, None)
        with pytest.raises(ValidationError):
            loader.manifest_row("t1", "acme", repo, "r", "sha", "BOGUS", {})  # type: ignore[arg-type]

    def test_resume_map_terminal_only(self, tmp_path: Path) -> None:
        repo = loader.RepoListing("acme/pay", "main", "https://x", False, False, False, False, 1, None)
        rows = [
            loader.manifest_row("t", "acme", repo, "r", "sha1", "FETCHING", {}),
            loader.manifest_row("t", "acme", repo, "r", "sha1", "READY", {}),
            loader.manifest_row("t", "acme", repo, "r", "sha2", "FAILED", {}),
            loader.manifest_row("t", "acme", repo, "r", "sha3", "LISTED", {}),
        ]
        manifest = tmp_path / "m.jsonl"
        manifest.write_text("\n".join(r.model_dump_json() for r in rows) + "\nnot-json\n")
        done = loader.load_resume_map(manifest)
        assert done == {("acme/pay", "sha1"): "READY", ("acme/pay", "sha2"): "FAILED"}


class TestListing:
    def _item(self, **over: object) -> dict:
        base: dict = {"full_name": "acme/r", "default_branch": "main",
                      "clone_url": "https://github.com/acme/r.git", "private": False,
                      "fork": False, "archived": False, "disabled": False,
                      "size": 10, "language": "Python"}
        base.update(over)
        return base

    def test_skip_rules(self, monkeypatch: pytest.MonkeyPatch) -> None:
        items = [self._item(), self._item(full_name="acme/f", fork=True),
                 self._item(full_name="acme/a", archived=True),
                 self._item(full_name="acme/d", disabled=True),
                 self._item(full_name="acme/e", size=0)]
        monkeypatch.setattr(loader, "_github_request", lambda path, token: (items, {}))
        repos, skipped = loader.list_org_repos("acme", "tok", False, False)
        assert [r.full_name for r in repos] == ["acme/r"]
        assert sorted(n for n, _ in skipped) == ["acme/a", "acme/d", "acme/e", "acme/f"]
        repos, skipped = loader.list_org_repos("acme", "tok", True, True)
        assert [r.full_name for r in repos] == ["acme/r", "acme/f", "acme/a"]
        assert sorted(n for n, _ in skipped) == ["acme/d", "acme/e"]

    def test_pagination_followed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        first = [self._item(full_name="acme/1")]
        second = [self._item(full_name="acme/2")]
        calls: list[str] = []

        def fake(path: str, token: str):  # type: ignore[no-untyped-def]
            calls.append(path)
            if "page=2" in path:
                return second, {}
            return first, {"link": '<https://api.github.com/x?page=2>; rel="next"'}

        monkeypatch.setattr(loader, "_github_request", fake)
        repos, _ = loader.list_org_repos("acme", "tok", False, False)
        assert [r.full_name for r in repos] == ["acme/1", "acme/2"]

    def test_single_repo_shape(self) -> None:
        with pytest.raises(ValueError, match="OWNER/NAME"):
            loader.get_single_repo("not-a-slug", "tok")

    def test_resolve_sha(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(loader, "_github_request", lambda p, t: ({"sha": "abc123"}, {}))
        assert loader.resolve_sha("acme", "r", "main", "tok") == "abc123"


class TestFetchGuards:
    def test_refuses_non_git_dir(self, tmp_path: Path) -> None:
        repo = loader.RepoListing("acme/pay", "main", "https://github.com/acme/pay.git",
                                  False, False, False, False, 1, None)
        (tmp_path / "acme--pay").mkdir()
        with pytest.raises(RuntimeError, match="non-git"):
            loader.fetch_repo_at_sha(repo, "sha", tmp_path, "tok", False)


class TestCli:
    def test_source_group_exclusive(self) -> None:
        with pytest.raises(SystemExit):
            loader.build_parser().parse_args(["--org", "a", "--repo", "a/b", "--tenant-id", "t"])

    def test_single_repo_parsed(self) -> None:
        args = loader.build_parser().parse_args(["--repo", "acme/pay", "--tenant-id", "t"])
        assert args.repo == "acme/pay"

    def test_resume_is_companion(self) -> None:
        args = loader.build_parser().parse_args(
            ["--org", "acme", "--tenant-id", "t", "--resume", "m.jsonl"])
        assert args.resume == "m.jsonl" and args.org == "acme"


class TestRateLimitBackoff:
    def test_retry_after_honored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import urllib.error

        sleeps: list[float] = []
        monkeypatch.setattr(loader.time, "sleep", lambda s: sleeps.append(s))
        err = urllib.error.HTTPError("http://x", 429, "slow", {"Retry-After": "7"}, None)
        calls = {"n": 0}

        def fake_open(req, timeout):  # type: ignore[no-untyped-def]
            calls["n"] += 1
            if calls["n"] == 1:
                raise err
            class Resp:
                headers: ClassVar[dict] = {}
                def __enter__(self): return self
                def __exit__(self, *a): return False
                def read(self): return b"[]"
            return Resp()

        monkeypatch.setattr(loader.urllib.request, "urlopen", fake_open)
        data, _ = loader._github_request("/x", "tok")
        assert data == [] and calls["n"] == 2 and sleeps == [7.0]

    def test_non_rate_error_raises_fast(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import urllib.error

        def fake_open(req, timeout):  # type: ignore[no-untyped-def]
            raise urllib.error.HTTPError("http://x", 404, "nope", {}, None)

        monkeypatch.setattr(loader.urllib.request, "urlopen", fake_open)
        with pytest.raises(RuntimeError, match="404"):
            loader._github_request("/x", "tok")


class TestBuildPayload:
    async def _build(self, tmp_path: Path, name: str = "acme--payments"):  # type: ignore[no-untyped-def]
        root = _stage_fixture(tmp_path, name)
        return await loader.build_payload_for_repo(root, "tenant-a", "app-1", name, "f" * 40)

    @pytest.mark.asyncio
    async def test_valid_envelope(self, tmp_path: Path) -> None:
        from investigation_agent_platform.domain.topology.models import ASTTopologyPayload

        payload, truncated = await self._build(tmp_path)
        assert isinstance(payload, ASTTopologyPayload)
        assert truncated is False
        assert payload.tenant_id == "tenant-a"
        assert payload.revision == "f" * 40
        paths = {f.path for f in payload.source_files}
        assert "src/payments.py" in paths and "src/app.js" in paths
        # One MODULE node per file.
        modules = [n for n in payload.ast_nodes if n.node_type.value == "MODULE"]
        assert len(modules) == len(payload.source_files)
        # Known symbols present with qualified names.
        quals = {n.qualified_name for n in payload.ast_nodes}
        assert "PaymentService.charge" in quals
        assert "process_payment" in quals

    @pytest.mark.asyncio
    async def test_deterministic_ids(self, tmp_path: Path) -> None:
        first, _ = await self._build(tmp_path)
        shutil.rmtree(tmp_path / "acme--payments")
        second, _ = await self._build(tmp_path)
        assert first.payload_hash == second.payload_hash
        assert {n.node_id for n in first.ast_nodes} == {n.node_id for n in second.ast_nodes}

    @pytest.mark.asyncio
    async def test_calls_resolved_same_payload(self, tmp_path: Path) -> None:
        from investigation_agent_platform.domain.topology.models import CallResolutionStatus

        payload, _ = await self._build(tmp_path)
        assert len(payload.calls) >= 1  # process_payment -> validate
        node_ids = {n.node_id for n in payload.ast_nodes}
        for edge in payload.calls:
            assert edge.resolution_status == CallResolutionStatus.RESOLVED
            assert edge.caller_node_id in node_ids
            assert edge.callee_node_id in node_ids

    @pytest.mark.asyncio
    async def test_db_access_from_sql_context(self, tmp_path: Path) -> None:
        payload, _ = await self._build(tmp_path)
        tables = {e.target_entity_or_table for e in payload.database_accesses}
        assert "payments" in tables

    @pytest.mark.asyncio
    async def test_unknown_extension_module_only(self, tmp_path: Path) -> None:
        root = _stage_fixture(tmp_path, "acme--payments")
        (root / "notes.txt").write_text("hello\n")
        payload, _ = await loader.build_payload_for_repo(
            root, "tenant-a", "app-1", "acme--payments", "f" * 40)
        txt_nodes = [n for n in payload.ast_nodes if n.file_path == "notes.txt"]
        assert len(txt_nodes) == 1 and txt_nodes[0].node_type.value == "MODULE"

    @pytest.mark.asyncio
    async def test_truncated_on_cap_breach(self, tmp_path: Path,
                                           monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(loader, "MAX_NODE_SYMBOLS", 2)
        _, truncated = await self._build(tmp_path)
        assert truncated is True

    @pytest.mark.asyncio
    async def test_legacy_wrapper_shape(self, tmp_path: Path) -> None:
        root = _stage_fixture(tmp_path, "acme--billing")
        wrapped = await loader.parse_repo_to_payload(root, "tenant-a", "app-1",
                                                      "acme--billing", "e" * 40)
        assert wrapped["revision"] == "e" * 40
        assert wrapped["symbol_count"] >= 3
        assert wrapped["truncated"] is False


class TestRepoTypes:
    def test_mapping_parsed(self) -> None:
        assert loader.parse_repo_types(["acme/pay=SERVICE"]) == {"acme/pay": "SERVICE"}

    def test_mapping_rejects_unknown(self) -> None:
        with pytest.raises(ValueError, match="unknown RepositoryType"):
            loader.parse_repo_types(["acme/pay=BANANA"])

    def test_mapping_rejects_malformed(self) -> None:
        with pytest.raises(ValueError, match="OWNER/NAME=TYPE"):
            loader.parse_repo_types(["acme/pay"])

    def test_per_repo_override_wins(self) -> None:
        assert loader.resolve_repo_type("acme/pay", "UNKNOWN", {"acme/pay": "SERVICE"}) == "SERVICE"
        assert loader.resolve_repo_type("acme/other", "UNKNOWN", {"acme/pay": "SERVICE"}) == "UNKNOWN"


class TestRegisterIdentities:
    def _repo(self, full_name: str = "acme/pay") -> object:
        return loader.RepoListing(full_name, "main", f"https://github.com/{full_name}.git",
                                  False, False, False, False, 1, "Python")

    @pytest.mark.asyncio
    async def test_codeowners_domain_resolved(self, tmp_path: Path) -> None:
        from investigation_agent_platform.infrastructure.topology.in_memory import (
            InMemoryTopologyAdapter,
        )

        root = _stage_fixture(tmp_path, "acme--payments")
        adapter = InMemoryTopologyAdapter()
        result = await loader.register_identities(root, "tenant-a", self._repo(),
                                                  "acme--pay", "SERVICE", adapter)
        assert result["git_org_id"] == "github:acme"
        assert result["domain_id"] == "team-payments"
        stored = adapter._repositories_by_id[("tenant-a", "acme--pay")]
        assert stored.repository_type.value == "SERVICE"
        assert adapter._ownership[("tenant-a", "acme--pay")] == ["github:acme", "team-payments"]

    @pytest.mark.asyncio
    async def test_missing_codeowners_leaves_domain_unset(self, tmp_path: Path) -> None:
        from investigation_agent_platform.infrastructure.topology.in_memory import (
            InMemoryTopologyAdapter,
        )

        root = _stage_fixture(tmp_path, "acme--billing")
        adapter = InMemoryTopologyAdapter()
        result = await loader.register_identities(root, "tenant-a",
                                                  self._repo("acme/bill"),
                                                  "acme--bill", "UNKNOWN", adapter)
        assert result["domain_id"] is None
        assert adapter._ownership[("tenant-a", "acme--bill")] == ["github:acme"]


class TestIngestPayload:
    _run: ClassVar[dict[str, int]] = {"n": 0}

    async def _payload(self, tmp_path: Path, tenant: str = "tenant-a"):  # type: ignore[no-untyped-def]
        self._run["n"] += 1
        dest = tmp_path / f"stage-{self._run['n']}"
        shutil.copytree(FIXTURES / "acme--payments", dest / "acme--payments")
        payload, truncated = await loader.build_payload_for_repo(
            dest / "acme--payments", tenant, "app-1", "acme--pay", "f" * 40)
        assert truncated is False
        return payload

    @pytest.mark.asyncio
    async def test_ready_and_replay(self, tmp_path: Path) -> None:
        from investigation_agent_platform.infrastructure.topology.in_memory import (
            InMemoryTopologyAdapter,
        )

        adapter = InMemoryTopologyAdapter()
        first = await loader.ingest_payload(await self._payload(tmp_path), adapter)
        assert first["status"] == "READY"
        assert first["node_count"] > 0
        second = await loader.ingest_payload(await self._payload(tmp_path), adapter)
        assert second["status"] == "READY"
        assert second["snapshot_id"] == first["snapshot_id"]

    @pytest.mark.asyncio
    async def test_tenant_isolation(self, tmp_path: Path) -> None:
        from investigation_agent_platform.infrastructure.topology.in_memory import (
            InMemoryTopologyAdapter,
        )

        adapter = InMemoryTopologyAdapter()
        for tenant in ("tenant-a", "tenant-b"):
            result = await loader.ingest_payload(await self._payload(tmp_path, tenant), adapter)
            assert result["status"] == "READY"

    @pytest.mark.asyncio
    async def test_transient_retries_then_succeeds(self, tmp_path: Path) -> None:
        from investigation_agent_platform.domain.common.exceptions import (
            TopologyProviderUnavailableError,
        )
        from investigation_agent_platform.infrastructure.topology.in_memory import (
            InMemoryTopologyAdapter,
        )

        real = InMemoryTopologyAdapter()
        calls = {"n": 0}
        real_ingest = real.ingest

        async def flaky(payload):  # type: ignore[no-untyped-def]
            calls["n"] += 1
            if calls["n"] < 3:
                raise TopologyProviderUnavailableError("boom")
            return await real_ingest(payload)

        real.ingest = flaky  # type: ignore[method-assign]
        result = await loader.ingest_payload(await self._payload(tmp_path), real)
        assert result["status"] == "READY"
        assert calls["n"] == 3

    @pytest.mark.asyncio
    async def test_transient_exhaustion_fails_closed(self, tmp_path: Path) -> None:
        from investigation_agent_platform.domain.common.exceptions import (
            TopologyProviderUnavailableError,
        )
        from investigation_agent_platform.infrastructure.topology.in_memory import (
            InMemoryTopologyAdapter,
        )

        real = InMemoryTopologyAdapter()

        async def down(payload):  # type: ignore[no-untyped-def]
            raise TopologyProviderUnavailableError("down")

        real.ingest = down  # type: ignore[method-assign]
        result = await loader.ingest_payload(await self._payload(tmp_path), real,
                                             max_attempts=2)
        assert result["status"] == "FAILED"
        assert "2 attempts" in (result["error"] or "")

    @pytest.mark.asyncio
    async def test_non_transient_no_retry(self, tmp_path: Path) -> None:
        real_calls = {"n": 0}

        class Adapter:
            async def ingest(self, payload):  # type: ignore[no-untyped-def]
                real_calls["n"] += 1
                raise ValueError("schema")

        with pytest.raises(ValueError, match="schema"):
            await loader.ingest_payload(await self._payload(tmp_path), Adapter())
        assert real_calls["n"] == 1
