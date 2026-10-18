# tests/unit/test_iap_operator.py
"""Operator-home CLI (scripts/iap-operator.py): scopes, merge, gates."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _mod():
    spec = importlib.util.spec_from_file_location(
        "iap_operator", REPO_ROOT / "scripts" / "iap-operator.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _args(home, **over):
    import argparse

    args = argparse.Namespace(home=str(home), scope="")
    for key, value in over.items():
        setattr(args, key, value)
    return args


def _scope_home(home: Path, scope: str = "sample/orders/dev") -> Path:
    org, app, env = scope.split("/")
    (home / "scopes" / org / env).mkdir(parents=True)
    sdir = home / "scopes" / org / app / env
    (sdir / "seed").mkdir(parents=True)
    os.chmod(home / "scopes" / org, 0o700)
    return sdir


class TestScopeValidation:
    def test_bad_shapes_rejected(self) -> None:
        op = _mod()
        for bad in ["a/b", "a/b/c/d", "has space/x/y", "a/../b", "A/B", ""]:
            with pytest.raises(SystemExit):
                op.split_scope(bad)

    def test_good_shape_splits(self) -> None:
        op = _mod()
        assert op.split_scope("sample/orders/dev") == ("sample", "orders", "dev")

    def test_unknown_scope_fails(self, tmp_path) -> None:
        op = _mod()
        assert op.cmd_init(_args(tmp_path)) == 0
        assert op.cmd_status(_args(tmp_path, scope="sample/orders/dev")) == 1


class TestMergeAndGates:
    def test_lever_leaf_merge_with_provenance(self, tmp_path, capsys) -> None:
        op = _mod()
        assert op.cmd_init(_args(tmp_path)) == 0
        sdir = _scope_home(tmp_path)
        (tmp_path / "scopes" / "sample" / "dev" / "app.env").write_text("A=1\nB=lever\n")
        (sdir / "app.env").write_text("B=leaf\nC='x y'\n")
        assert op.cmd_env(_args(tmp_path, scope="sample/orders/dev")) == 0
        out = capsys.readouterr().out
        assert "export A='1' # sample/orders/dev:lever" in out
        assert "export B='leaf' # sample/orders/dev:leaf" in out
        assert "export C='x y'" in out

    def test_secret_denylist_fail_closed(self, tmp_path) -> None:
        op = _mod()
        assert op.cmd_init(_args(tmp_path)) == 0
        _scope_home(tmp_path)
        (tmp_path / "scopes" / "sample" / "dev" / "app.env").write_text("DB_PASSWORD=x\n")
        assert op.cmd_doctor(_args(tmp_path)) == 1
        assert op.cmd_env(_args(tmp_path, scope="sample/orders/dev")) == 1

    def test_env_needs_scope(self, tmp_path) -> None:
        op = _mod()
        assert op.cmd_init(_args(tmp_path)) == 0
        assert op.cmd_env(_args(tmp_path)) == 1


class TestHomeFlows:
    def test_init_doctor_status_backup(self, tmp_path) -> None:
        op = _mod()
        home = tmp_path / "op"
        assert op.cmd_init(_args(home)) == 0
        assert (home / "README.md").is_file()
        assert (home / "seed" / "state.json").is_file()
        # second init without --force refuses
        assert op.cmd_init(_args(home)) == 1
        assert op.cmd_doctor(_args(home)) == 0
        assert op.cmd_status(_args(home)) == 0
        assert op.cmd_backup(_args(home)) == 0
        assert len(list((home / "backups").glob("*.tar.gz"))) == 1

    def test_doctor_flags_bad_perms(self, tmp_path) -> None:
        op = _mod()
        home = tmp_path / "op"
        assert op.cmd_init(_args(home)) == 0
        sdir = _scope_home(home)
        cred = sdir / "oracle.yaml"
        cred.write_text("app: demo\nenvironment: local\ndatabases:\n  a.b.c:\n    user_name: u\n    password: p\n    jdbcUrl: j\n")
        cred.chmod(0o644)
        assert op.cmd_doctor(_args(home)) == 1
        cred.chmod(0o600)
        assert op.cmd_doctor(_args(home)) == 0

    def test_operator_dir_perms_gated(self, tmp_path) -> None:
        op = _mod()
        home = tmp_path / "op"
        assert op.cmd_init(_args(home)) == 0
        _scope_home(home)
        (home / "scopes" / "sample").chmod(0o755)
        assert op.cmd_doctor(_args(home)) == 1
        (home / "scopes" / "sample").chmod(0o700)
        assert op.cmd_doctor(_args(home)) == 0

    def test_backup_excludes_credentials_by_default(self, tmp_path) -> None:
        op = _mod()
        home = tmp_path / "op"
        assert op.cmd_init(_args(home)) == 0
        sdir = _scope_home(home)
        (sdir / "oracle.yaml").write_text("secret: yes\n")
        (sdir / "app.env").write_text("A=1\n")
        assert op.cmd_backup(_args(home)) == 0
        import tarfile

        archive = next((home / "backups").glob("*.tar.gz"))
        with tarfile.open(archive) as tar:
            names = tar.getnames()
        assert not any(n.endswith("oracle.yaml") for n in names)
        assert any(n.endswith("app.env") for n in names)
        assert op.cmd_backup(_args(home, include_credentials=True)) == 0
