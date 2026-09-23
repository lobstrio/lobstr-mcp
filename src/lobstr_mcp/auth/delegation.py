"""Server-to-server client for the Lobstr consent-grant redemption endpoint.

In the frontend-consent model, the user approves the connection on Lobstr's own
frontend (where they are already logged in); the frontend obtains a short-lived,
single-use grant from Lobstr and hands it to the MCP. The MCP redeems that grant
here (gated by a shared service credential) to obtain the user's identity and
API token. No password ever reaches the MCP or the AI client.

Endpoint (additive Lobstr API endpoint): POST {LOBSTR_API_BASE}/oauth/mcp-grant/redeem
Exercised via mocked transports in tests; the request/response shape here is the
proposed contract to finalize with the API owners.
"""
from __future__ import annotations

import httpx

from lobstr_mcp.auth.token_store import TokenLink
from lobstr_mcp.lobstr_client import USER_AGENT


class LobstrDelegationError(Exception):
    pass


class LobstrDelegationClient:
    def __init__(self, base_url: str, service_credential: str, *,
                 transport: httpx.BaseTransport | None = None,
                 timeout: float = 30.0) -> None:
        self._base = base_url.rstrip("/")
        self._http = httpx.Client(
            headers={"X-MCP-Service-Credential": service_credential,
                     "User-Agent": USER_AGENT},
            timeout=timeout,
            transport=transport,
        )

    def redeem_grant(self, grant: str) -> TokenLink:
        """Exchange a single-use consent grant for the user's identity + token."""
        resp = self._http.post(
            f"{self._base}/oauth/mcp-grant/redeem",
            json={"grant": grant},
        )
        if resp.status_code >= 400:
            raise LobstrDelegationError(
                f"grant redeem failed: {resp.status_code} {resp.text}"
            )
        data = resp.json()
        return TokenLink(
            lobstr_user_id=str(data["user_id"]),
            lobstr_api_token=data["token"],
            scopes=data.get("scopes", []),
            expires_at=data.get("expires_at"),
        )

    def close(self) -> None:
        self._http.close()
