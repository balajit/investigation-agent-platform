# tests/integration/spring_config/test_security.py
"""Property allowlist, sensitivity matcher, and env allowlist loaders."""

from investigation_agent_platform.integrations.spring_config import security


def test_sensitivity_matcher() -> None:
    for key in (
        "spring.datasource.password",
        "server.ssl.key-store-password",
        "app.truststore.path",
        "spring.rabbitmq.listener.password",
        "cloud.aws.credentials.secret-key",
        "apiKey",
        "client_secret",
        "db.private_key",
        "auth.token",
    ):
        assert security.looks_sensitive(key), key
    for key in (
        "spring.application.name",
        "server.port",
        "spring.datasource.url",
        "logging.level.com.acme",
        "info.app.version",
    ):
        assert not security.looks_sensitive(key), key


def test_allowlist_fnmatch() -> None:
    patterns = ("spring.application.*", "server.port")
    assert security.is_allowed_property("spring.application.name", patterns)
    assert security.is_allowed_property("server.port", patterns)
    assert not security.is_allowed_property("spring.datasource.url", patterns)


def test_env_loaders(monkeypatch) -> None:
    monkeypatch.setenv("IAP_SPRING_ALLOWED_APPLICATIONS", "shop, billing")
    monkeypatch.setenv("IAP_SPRING_ALLOWED_PROFILES", "prod")
    monkeypatch.setenv("IAP_SPRING_ALLOWED_SERVER_URLS", "https://cfg.example")
    monkeypatch.setenv("IAP_SPRING_EXTRA_PROPERTY_PATTERNS", "custom.*")
    monkeypatch.setenv("IAP_SPRING_OAUTH_TOKEN_URL", "https://auth.example/token")
    monkeypatch.setenv("IAP_SPRING_OAUTH_CLIENT_ID", "id")
    monkeypatch.setenv("IAP_SPRING_OAUTH_CLIENT_SECRET", "s")
    assert security.allowed_spring_applications() == {"shop", "billing"}
    assert security.allowed_spring_profiles() == {"prod"}
    assert security.allowed_server_urls() == ("https://cfg.example",)
    assert "custom.*" in security.allowed_property_patterns()
    assert security.oauth_settings()["token_url"] == "https://auth.example/token"


def test_empty_server_allowlist_is_deny_all(monkeypatch) -> None:
    monkeypatch.delenv("IAP_SPRING_ALLOWED_SERVER_URLS", raising=False)
    assert security.allowed_server_urls() == ()
