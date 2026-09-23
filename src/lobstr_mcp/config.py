import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    lobstr_api_base: str
    dev_token: str | None
    request_timeout: float
    run_confirm_threshold: int
    public_base_url: str
    service_credential: str | None
    consent_url: str
    # Persistence. Without redis_url the OAuth/idempotency state is in-process
    # and a restart drops it; token_key encrypts Lobstr API tokens at rest.
    redis_url: str | None = None
    token_key: str | None = None


def get_settings() -> Settings:
    return Settings(
        lobstr_api_base=os.getenv("LOBSTR_API_BASE", "https://api.lobstr.io/v1"),
        dev_token=os.getenv("LOBSTR_DEV_TOKEN") or None,
        request_timeout=float(os.getenv("LOBSTR_REQUEST_TIMEOUT", "30")),
        run_confirm_threshold=int(os.getenv("LOBSTR_RUN_CONFIRM_THRESHOLD", "100")),
        public_base_url=os.getenv("LOBSTR_MCP_PUBLIC_URL", "https://mcp.lobstr.io"),
        service_credential=os.getenv("LOBSTR_MCP_SERVICE_CREDENTIAL") or None,
        # Lobstr frontend consent page (user already logged in) that the OAuth
        # /authorize step redirects the browser to.
        consent_url=os.getenv("LOBSTR_CONSENT_URL", "https://app.lobstr.io/connect-ai"),
        redis_url=os.getenv("LOBSTR_MCP_REDIS_URL") or None,
        token_key=os.getenv("LOBSTR_MCP_TOKEN_KEY") or None,
    )
