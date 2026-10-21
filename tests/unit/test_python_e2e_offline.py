"""Offline end-to-end: temp git repo → pinned adapter read → dispatch → typed claim → verdict.

No live environment, no Temporal loop. The adapter produces real
revision-pinned evidence; the worker-extracted dispatch resolves it; the
shared verifier judges correct and incorrect claims. This is the Phase 5
offline gate: revision drift, path-only reads, and wrong values must all
fail closed here.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from investigation_agent_platform.application.analysis.python.dispatch import (
    resolve_python_field_fact,
    resolve_python_method_fact,
)
from investigation_agent_platform.application.investigation.verification import (
    VerificationEngine,
)
from investigation_agent_platform.domain.code.models import (
    CODE_FACT_RESOLUTION_V2_KEY,
    CodeFactStatus,
    JavaFactCategory,
)

_CONFIG_PY = 'import os\n\ntimeout = 30\nretries = 3\nlimit = int(os.getenv("LIMIT", "10"))\n'
_MODELS_PY = "class Base:\n    def handle(self):\n        return 1\n\nclass Sub(Base):\n    def handle(self):\n        return 2\n"
_VERSION = "3.10\n"


def _repo(tmp_path, files):
    import pygit2

    repo_dir = tmp_path / "o" / "r"
    repo_dir.mkdir(parents=True)
    repo = pygit2.init_repository(str(repo_dir))
    for rel, content in files.items():
        target = repo_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        repo.index.add(rel)
    repo.index.write()
    author = pygit2.Signature("t", "t@example.com")
    sha = str(repo.create_commit("HEAD", author, author, "init", repo.index.write_tree(), []))
    try:
        repo.create_branch("main", repo.get(sha))
    except ValueError:
        pass
    return sha


def _profile():
    code = SimpleNamespace(org="o", repos=["r"], default_branch="main")
    svc = SimpleNamespace(code=code, name="r")
    return SimpleNamespace(services=[svc], primary_service=svc)


def _adapter(tmp_path):
    from investigation_agent_platform.infrastructure.evidence.code.git import (
        PyGit2Adapter,
    )

    return PyGit2Adapter(repo_base_path=str(tmp_path))


async def _range_read(adapter, profile, path, revision, start, end):
    evidence = await adapter.get_source(
        "tenant-a", uuid4(), path, profile, revision=revision, start_line=start, end_line=end
    )
    return evidence.model_copy(
        update={"attributes": {**dict(evidence.attributes or {}), "read_mode": "range"}}
    )


def _banked(resolution, evidence, extra_attrs=None):
    attrs = {
        **dict(evidence.attributes or {}),
        CODE_FACT_RESOLUTION_V2_KEY: resolution.model_dump(mode="json"),
    }
    if extra_attrs:
        attrs.update(extra_attrs)
    return evidence.model_copy(update={"attributes": attrs})


def _claim(evidence, symbol, value, statement, category, declaring_type=None):
    target = SimpleNamespace(
        symbol=symbol,
        declaring_type=declaring_type,
        file_path="svc/config.py",
        line_start=3,
        claimed_value=value,
        value_semantics="EXPLICIT_INITIALIZER",
        fact_category=category,
        node_id=None,
    )
    return SimpleNamespace(
        id=uuid4(),
        statement=statement,
        claim_value_semantics="EXPLICIT_INITIALIZER",
        claimed_value=value,
        claim_target=target,
        fact_category=category,
        node_id=None,
        supporting_evidence_ids=[evidence.evidence_id],
    )


@pytest.mark.asyncio
async def test_e2e_value_claim_verifies_at_pinned_sha(tmp_path):
    sha = _repo(tmp_path, {"svc/config.py": _CONFIG_PY, ".python-version": _VERSION})
    adapter, profile = _adapter(tmp_path), _profile()
    evidence = await _range_read(adapter, profile, "svc/config.py", sha, 3, 3)

    async def reader(name):
        try:
            ev = await adapter.get_source("tenant-a", uuid4(), name, profile, revision=sha)
        except Exception:
            return None
        if (ev.attributes or {}).get("truncated"):
            return None
        return ev.content_snippet or None

    resolution, _ = await resolve_python_field_fact(
        evidence, field_name="timeout", read_version_file=reader
    )
    assert resolution.status == CodeFactStatus.RESOLVED
    assert resolution.resolved_value == "30"
    assert resolution.revision == sha
    # Repo-declared 3.10 wins over the runtime default: no assumed marker.
    assert resolution.language_version == "3.10"
    assert "assumed_default_language_version" not in resolution.assumptions

    banked = _banked(resolution, evidence)
    good = _claim(banked, "timeout", "30", "The default value of timeout is 30", JavaFactCategory.SOURCE_VALUE_FACT)
    ok, _ = VerificationEngine.java_fact_proof(good, [banked])
    assert ok
    assert VerificationEngine().validate_java_fact_claim(good, [banked], []) is not None


@pytest.mark.asyncio
async def test_e2e_wrong_value_rejected(tmp_path):
    sha = _repo(tmp_path, {"svc/config.py": _CONFIG_PY})
    adapter, profile = _adapter(tmp_path), _profile()
    evidence = await _range_read(adapter, profile, "svc/config.py", sha, 3, 3)
    resolution, _ = await resolve_python_field_fact(evidence, field_name="timeout")
    banked = _banked(resolution, evidence)
    bad = _claim(banked, "timeout", "999", "The default value of timeout is 999", JavaFactCategory.SOURCE_VALUE_FACT)
    ok, _ = VerificationEngine.java_fact_proof(bad, [banked])
    assert not ok
    assert VerificationEngine().validate_java_fact_claim(bad, [banked], []) is None


@pytest.mark.asyncio
async def test_e2e_new_value_from_later_commit_rejected_at_old_sha(tmp_path):
    import pygit2

    sha1 = _repo(tmp_path, {"svc/config.py": _CONFIG_PY})
    repo_dir = tmp_path / "o" / "r"
    repo = pygit2.Repository(str(repo_dir))
    (repo_dir / "svc" / "config.py").write_text(_CONFIG_PY.replace("timeout = 30", "timeout = 31"), encoding="utf-8")
    repo.index.add("svc/config.py")
    repo.index.write()
    author = pygit2.Signature("t", "t@example.com")
    repo.create_commit("HEAD", author, author, "bump", repo.index.write_tree(), [repo.head.peel().id])

    adapter, profile = _adapter(tmp_path), _profile()
    old_evidence = await _range_read(adapter, profile, "svc/config.py", sha1, 3, 3)
    resolution, _ = await resolve_python_field_fact(old_evidence, field_name="timeout")
    assert resolution.resolved_value == "30"
    banked = _banked(resolution, old_evidence)
    # A model hallucinating the newer value against old-SHA evidence fails.
    drifted = _claim(banked, "timeout", "31", "The default value of timeout is 31", JavaFactCategory.SOURCE_VALUE_FACT)
    ok, _ = VerificationEngine.java_fact_proof(drifted, [banked])
    assert not ok


@pytest.mark.asyncio
async def test_e2e_cross_revision_evidence_substitution_fails(tmp_path):
    sha = _repo(tmp_path, {"svc/config.py": _CONFIG_PY})
    adapter, profile = _adapter(tmp_path), _profile()
    old_evidence = await _range_read(adapter, profile, "svc/config.py", sha, 3, 3)
    resolution, _ = await resolve_python_field_fact(old_evidence, field_name="timeout")
    assert resolution.resolved_value == "30"
    # Same range, same path, different commit: provenance must not match.
    other = await _range_read(adapter, profile, "svc/config.py", sha, 4, 4)
    banked_old = _banked(resolution, old_evidence)
    hyp = _claim(banked_old, "timeout", "30", "The default value of timeout is 30", JavaFactCategory.SOURCE_VALUE_FACT)
    ok, reason = VerificationEngine.java_fact_proof(hyp, [other])
    assert not ok


@pytest.mark.asyncio
async def test_e2e_path_only_read_fails_closed(tmp_path):
    sha = _repo(tmp_path, {"svc/config.py": _CONFIG_PY})
    adapter, profile = _adapter(tmp_path), _profile()
    evidence = await adapter.get_source("tenant-a", uuid4(), "svc/config.py", profile, revision=sha)
    evidence = evidence.model_copy(
        update={"attributes": {**dict(evidence.attributes or {}), "read_mode": "path_only", "discovery_only": True}}
    )
    resolution, _ = await resolve_python_field_fact(evidence, field_name="timeout")
    assert resolution.status == CodeFactStatus.UNRESOLVED


@pytest.mark.asyncio
async def test_e2e_config_claim_verifies_likely(tmp_path):
    sha = _repo(tmp_path, {"svc/config.py": _CONFIG_PY})
    adapter, profile = _adapter(tmp_path), _profile()
    evidence = await _range_read(adapter, profile, "svc/config.py", sha, 5, 5)
    resolution, config_source = await resolve_python_field_fact(evidence, field_name="limit")
    assert resolution.status == CodeFactStatus.RESOLVED
    assert resolution.resolved_value == "10"
    assert config_source == "svc/config.py"
    banked = _banked(resolution, evidence, {"configuration_source": config_source})
    hyp = _claim(banked, "limit", "10", "limit defaults to 10", JavaFactCategory.CONFIGURATION_FACT)
    ok, _ = VerificationEngine.java_fact_proof(hyp, [banked])
    assert ok
    result = VerificationEngine().validate_java_fact_claim(hyp, [banked], [])
    assert result is not None and result.tier == "likely"


@pytest.mark.asyncio
async def test_e2e_method_owner_claim_verifies(tmp_path):
    sha = _repo(tmp_path, {"svc/models.py": _MODELS_PY})
    adapter, profile = _adapter(tmp_path), _profile()
    evidence = await adapter.get_source(
        "tenant-a", uuid4(), "svc/models.py", profile, revision=sha, start_line=1, end_line=7
    )
    evidence = evidence.model_copy(
        update={"attributes": {**dict(evidence.attributes or {}), "read_mode": "range"}}
    )
    resolution = await resolve_python_method_fact(
        evidence, method_name="handle", declaring_type="Sub"
    )
    assert resolution.status == CodeFactStatus.RESOLVED
    assert resolution.resolved_value == "Sub.handle"
    banked = _banked(resolution, evidence)
    hyp = _claim(banked, "handle", "Sub.handle", "Sub.handle runs for Sub", JavaFactCategory.DECLARATION_FACT, declaring_type="Sub")
    hyp.claim_target.file_path = "svc/models.py"
    ok, _ = VerificationEngine.java_fact_proof(hyp, [banked])
    assert ok
