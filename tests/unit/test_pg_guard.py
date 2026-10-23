"""Disposable-PostgreSQL guard tests: verdict matrix, override, commands."""


import pytest

from investigation_agent_platform.infrastructure.persistence.pg_guard import (
    GuardDecision,
    Verdict,
    command_is_write,
    require_disposable,
    verdict_for_url,
)


@pytest.fixture(autouse=True)
def _clean_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("IAP_PG_GUARD_OVERRIDE", raising=False)


@pytest.mark.parametrize(
    ("url", "verdict"),
    [
        ("postgresql://iap:iap@localhost:5433/iap_test", Verdict.ALLOW),
        ("postgresql://iap:iap@127.0.0.1:5544/db", Verdict.ALLOW),
        ("postgresql://u@127.0.0.2:5433/db", Verdict.ALLOW),
        ("postgresql://u@[::1]:5433/db", Verdict.ALLOW),
        ("postgresql+asyncpg://u@localhost:5439/db", Verdict.ALLOW),
        ("postgresql://iap:iap@localhost:5432/iap_test", Verdict.DENY_PROTECTED),
        ("postgresql://iap:iap@localhost/iap_test", Verdict.DENY_PROTECTED),
        ("postgresql://iap:iap@localhost:5452/iap", Verdict.DENY_PROTECTED),
        ("postgresql://u:secretpw@127.0.0.1:5432/db", Verdict.DENY_PROTECTED),
        ("postgresql://u@db.internal:5433/db", Verdict.DENY_REMOTE),
        ("postgresql://u@203.0.113.7:5433/db", Verdict.DENY_REMOTE),
        ("postgresql://u@203.0.113.7:5432/db", Verdict.DENY_REMOTE),
        ("", Verdict.DENY_UNPARSABLE),
        ("not-a-url", Verdict.DENY_UNPARSABLE),
        ("mysql://u@localhost:5433/db", Verdict.DENY_UNPARSABLE),
        ("postgresql:///no-host-here", Verdict.DENY_UNPARSABLE),
    ],
)
def test_verdict_matrix(url: str, verdict: Verdict) -> None:
    decision = verdict_for_url(url)
    assert isinstance(decision, GuardDecision)
    assert decision.verdict == verdict


def test_denial_messages_never_carry_credentials() -> None:
    url = "postgresql://admin:s3cret-pw@localhost:5432/db"
    decision = verdict_for_url(url)
    assert decision.verdict == Verdict.DENY_PROTECTED
    assert "s3cret-pw" not in decision.reason
    with pytest.raises(RuntimeError) as exc:
        require_disposable(url, purpose="alembic stamp")
    assert "s3cret-pw" not in str(exc.value)
    assert "localhost:5432" in str(exc.value)


def test_override_allows_with_reason_on_stderr(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("IAP_PG_GUARD_OVERRIDE", "deploy pipeline checkout-42")
    require_disposable(
        "postgresql://iap:iap@localhost:5432/iap", purpose="alembic upgrade"
    )
    assert "deploy pipeline checkout-42" in capsys.readouterr().err


def test_override_still_rejects_garbage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IAP_PG_GUARD_OVERRIDE", "deploy pipeline checkout-42")
    with pytest.raises(RuntimeError, match="not a usable postgresql URL"):
        require_disposable("garbage-url", purpose="alembic upgrade")


def test_empty_override_does_not_bypass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IAP_PG_GUARD_OVERRIDE", "   ")
    with pytest.raises(RuntimeError, match="refusing"):
        require_disposable(
            "postgresql://iap:iap@localhost:5432/iap", purpose="alembic stamp"
        )


@pytest.mark.parametrize(
    ("command", "write"),
    [
        ("upgrade", True),
        ("downgrade", True),
        ("stamp", True),
        (None, True),
        ("current", False),
        ("heads", False),
        ("history", False),
        ("show", False),
        ("check", False),
        ("list_templates", False),
    ],
)
def test_command_is_write_matrix(command: str | None, write: bool) -> None:
    assert command_is_write(command) is write


def test_allow_path_is_silent(
    capsys: pytest.CaptureFixture[str],
) -> None:
    require_disposable(
        "postgresql://iap:iap@localhost:5439/iap_test", purpose="alembic stamp"
    )
    assert capsys.readouterr().err == ""


def test_protected_defaults_deny_without_explicit_port() -> None:
    decision = verdict_for_url("postgresql://iap:iap@localhost/iap_test")
    assert decision.verdict == Verdict.DENY_PROTECTED


def test_similarly_named_vars_do_not_bypass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IAP_PG_GUARD_OVERRIDE_EXTRA", "deploy")
    monkeypatch.setenv("PG_GUARD_OVERRIDE", "deploy")
    with pytest.raises(RuntimeError, match="refusing"):
        require_disposable(
            "postgresql://iap:iap@localhost:5432/iap", purpose="alembic stamp"
        )
