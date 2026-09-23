from lobstr_mcp import config


def test_defaults(monkeypatch):
    for var in ("LOBSTR_API_BASE", "LOBSTR_DEV_TOKEN",
                "LOBSTR_REQUEST_TIMEOUT", "LOBSTR_RUN_CONFIRM_THRESHOLD"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("LOBSTR_MCP_PUBLIC_URL", raising=False)
    monkeypatch.delenv("LOBSTR_MCP_SERVICE_CREDENTIAL", raising=False)
    monkeypatch.delenv("LOBSTR_CONSENT_URL", raising=False)
    s = config.get_settings()
    assert s.lobstr_api_base == "https://api.lobstr.io/v1"
    assert s.dev_token is None
    assert s.request_timeout == 30.0
    assert s.run_confirm_threshold == 100
    assert s.public_base_url == "https://mcp.lobstr.io"
    assert s.service_credential is None
    assert s.consent_url == "https://app.lobstr.io/connect-ai"


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("LOBSTR_API_BASE", "https://staging.lobstr.io/v1")
    monkeypatch.setenv("LOBSTR_DEV_TOKEN", "tok123")
    monkeypatch.setenv("LOBSTR_REQUEST_TIMEOUT", "5")
    monkeypatch.setenv("LOBSTR_RUN_CONFIRM_THRESHOLD", "250")
    s = config.get_settings()
    assert s.lobstr_api_base == "https://staging.lobstr.io/v1"
    assert s.dev_token == "tok123"
    assert s.request_timeout == 5.0
    assert s.run_confirm_threshold == 250


def test_persistence_settings_default_to_in_memory(monkeypatch):
    monkeypatch.delenv("LOBSTR_MCP_REDIS_URL", raising=False)
    monkeypatch.delenv("LOBSTR_MCP_TOKEN_KEY", raising=False)
    s = config.get_settings()
    assert s.redis_url is None
    assert s.token_key is None


def test_persistence_settings_from_env(monkeypatch):
    monkeypatch.setenv("LOBSTR_MCP_REDIS_URL", "redis://127.0.0.1:6379/3")
    monkeypatch.setenv("LOBSTR_MCP_TOKEN_KEY", "abc")
    s = config.get_settings()
    assert s.redis_url == "redis://127.0.0.1:6379/3"
    assert s.token_key == "abc"
