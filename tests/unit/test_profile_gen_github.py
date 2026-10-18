# tests/unit/test_profile_gen_github.py
"""profile_gen GitHub auth: env-first, `gh` CLI fallback, fail-open."""

from __future__ import annotations


class TestAuthHeaders:
    def test_env_token_wins(self, monkeypatch) -> None:
        from investigation_agent_platform.integrations.profile_gen import github

        monkeypatch.setenv("IAP_GITHUB_TOKEN", "env-token")
        headers = github._auth_headers()
        assert headers["Authorization"] == "Bearer env-token"

    def test_gh_fallback_when_env_unset(self, monkeypatch) -> None:
        from investigation_agent_platform.integrations.profile_gen import github

        monkeypatch.delenv("IAP_GITHUB_TOKEN", raising=False)
        monkeypatch.setattr(github, "_gh_cli_token", lambda: "gh-token")
        headers = github._auth_headers()
        assert headers["Authorization"] == "Bearer gh-token"

    def test_no_token_no_header(self, monkeypatch) -> None:
        from investigation_agent_platform.integrations.profile_gen import github

        monkeypatch.delenv("IAP_GITHUB_TOKEN", raising=False)
        monkeypatch.setattr(github, "_gh_cli_token", lambda: "")
        assert "Authorization" not in github._auth_headers()

    def test_gh_failure_returns_empty(self, monkeypatch) -> None:
        import subprocess

        from investigation_agent_platform.integrations.profile_gen import github

        def _boom(*args, **kwargs):
            raise FileNotFoundError("no gh binary")

        monkeypatch.setattr(subprocess, "run", _boom)
        assert github._gh_cli_token() == ""


class TestCloneRepo:
    def test_branch_fallback_to_default(self, monkeypatch, tmp_path) -> None:
        import sys

        from investigation_agent_platform.integrations.profile_gen import generate

        calls = []

        class FakeGit:
            class RemoteCallbacks:
                def __init__(self, credentials=None):
                    self.credentials = credentials

            @staticmethod
            def UserPass(user, password):
                return (user, password)

            @staticmethod
            def clone_repository(url, target, **kwargs):
                calls.append(dict(kwargs))
                if "checkout_branch" in kwargs:
                    raise RuntimeError("reference 'refs/remotes/origin/main' not found")
                Path = __import__("pathlib").Path
                Path(target).mkdir(parents=True, exist_ok=True)
                return True

        monkeypatch.setitem(sys.modules, "pygit2", FakeGit())
        monkeypatch.setenv("IAP_GITHUB_TOKEN", "tok")
        out = generate.clone_repo("https://github.com/o/r.git", "main")
        assert out.is_dir()
        assert calls[0].get("checkout_branch") == "main"
        assert "checkout_branch" not in calls[1]
        assert calls[0]["callbacks"].credentials == ("git", "tok")

    def test_no_token_no_callbacks(self, monkeypatch) -> None:
        import sys

        from investigation_agent_platform.integrations.profile_gen import generate

        seen = {}

        class FakeGit:
            class RemoteCallbacks:
                def __init__(self, credentials=None):
                    self.credentials = credentials

            @staticmethod
            def UserPass(user, password):
                return (user, password)

            @staticmethod
            def clone_repository(url, target, **kwargs):
                seen.update(kwargs)
                Path = __import__("pathlib").Path
                Path(target).mkdir(parents=True, exist_ok=True)
                return True

        monkeypatch.setitem(sys.modules, "pygit2", FakeGit())
        monkeypatch.delenv("IAP_GITHUB_TOKEN", raising=False)
        monkeypatch.setattr(
            "investigation_agent_platform.integrations.profile_gen.github._gh_cli_token",
            lambda: "",
        )
        generate.clone_repo("https://github.com/o/r.git", "")
        assert "callbacks" not in seen

    def test_auth_error_guides(self, monkeypatch) -> None:
        import sys

        from investigation_agent_platform.integrations.profile_gen import generate

        class FakeGit:
            @staticmethod
            def clone_repository(url, target, **kwargs):
                raise RuntimeError("remote authentication required but no callback set")

        monkeypatch.setitem(sys.modules, "pygit2", FakeGit())
        monkeypatch.delenv("IAP_GITHUB_TOKEN", raising=False)
        monkeypatch.setattr(
            "investigation_agent_platform.integrations.profile_gen.github._gh_cli_token",
            lambda: "",
        )
        try:
            generate.clone_repo("https://github.com/o/r.git", "")
        except RuntimeError as exc:
            assert "gh auth login" in str(exc)
        else:
            raise AssertionError("expected RuntimeError")
