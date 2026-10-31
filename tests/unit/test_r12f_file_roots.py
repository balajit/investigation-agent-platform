"""R12F: IAP_FILE_LOG_ROOTS is global; the symlink check is a no-op.

Per-tenant roots with fail-closed default outside dev, and a link
check that actually inspects the link. Written first, red before the
fix.
"""

from __future__ import annotations

import os


def _settings(**over):  # type: ignore[no-untyped-def]
    from investigation_agent_platform.infrastructure.evidence.logs.file_provider import (
        FileLogSettings,
    )

    base: dict = {"roots": ["data"]}
    base.update(over)
    return FileLogSettings(**base)


def _adapter(settings, root):  # type: ignore[no-untyped-def]
    from investigation_agent_platform.infrastructure.evidence.logs.file_provider import (
        FileLogAdapter,
    )

    return FileLogAdapter(settings=settings, project_root=root)


def _profile_with(tenant, pattern):  # type: ignore[no-untyped-def]
    from types import SimpleNamespace

    from investigation_agent_platform.domain.profile.models import DatasourceKind

    return SimpleNamespace(
        tenant_id=tenant,
        datasources=[
            SimpleNamespace(
                kind=DatasourceKind.FILE_LOG,
                file_log=SimpleNamespace(paths=[pattern], max_bytes_per_file=None),
            )
        ],
    )


def test_r12f_t01_tenant_cannot_reach_another_tenants_root(tmp_path) -> None:
    root_a = tmp_path / "root-a"
    root_b = tmp_path / "root-b"
    root_a.mkdir()
    root_b.mkdir()
    (root_a / "a.log").write_text("alpha error\n")
    (root_b / "b.log").write_text("beta error\n")
    adapter = _adapter(
        _settings(
            roots=[],
            tenant_roots={"tenant-a": [str(root_a)], "tenant-b": [str(root_b)]},
        ),
        tmp_path,
    )
    got_a = adapter._resolve_files(_profile_with("tenant-a", "*.log"))
    assert [p.name for p in got_a] == ["a.log"]
    got_b = adapter._resolve_files(_profile_with("tenant-b", "*.log"))
    assert [p.name for p in got_b] == ["b.log"]


def test_r12f_t02_no_mapping_outside_dev_fails_closed(tmp_path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "a.log").write_text("alpha\n")
    adapter = _adapter(
        _settings(roots=[str(root)], tenant_roots={}, allow_global_roots=False),
        tmp_path,
    )
    assert adapter._resolve_files(_profile_with("tenant-a", "*.log")) == []
    dev = _adapter(
        _settings(roots=[str(root)], tenant_roots={}, allow_global_roots=True),
        tmp_path,
    )
    assert [p.name for p in dev._resolve_files(_profile_with("tenant-a", "*.log"))] == [
        "a.log"
    ]


def test_r12f_t03_link_check_works(tmp_path) -> None:
    outside = tmp_path / "outside"
    inside = tmp_path / "root"
    outside.mkdir()
    inside.mkdir()
    (outside / "secret.log").write_text("secret\n")
    (inside / "real.log").write_text("real log line\n")
    os.symlink(str(outside / "secret.log"), str(inside / "escape.log"))
    os.symlink(str(inside / "real.log"), str(inside / "alias.log"))
    adapter = _adapter(
        _settings(roots=[str(inside)], tenant_roots={}, allow_global_roots=True),
        tmp_path,
    )
    names = sorted(
        p.name for p in adapter._resolve_files(_profile_with("t", "*.log"))
    )
    assert "real.log" in names
    # The escaping link is refused outright; the link entry itself is
    # skipped (never read as a file).
    assert "escape.log" not in names
    assert "alias.log" not in names
