"""Authentication to the Zendesk API.

Preferred: OAuth 2.0 client credentials. The service holds a confidential client's ID and secret and
exchanges them for a short-lived access token, renewed before it expires and again on any 401.
Zendesk is retiring API tokens (no new ones for new accounts; all deactivated on April 30, 2027),
so the API-token mode is kept only for older accounts and local tests.
"""
import asyncio
import base64
import time

import httpx


class ZendeskAuth(httpx.Auth):
    def __init__(self, subdomain: str, *, client_id: str = "", client_secret: str = "", scope: str = "",
                 email: str = "", api_token: str = "", token_lifetime_s: int = 3600,
                 transport: httpx.AsyncBaseTransport | None = None):
        self._oauth = bool(client_id and client_secret)
        # Missing credentials must not stop the service from starting (health checks, other endpoints);
        # every Zendesk call fails clearly instead, and /healthz reports it.
        self.configured = self._oauth or bool(email and api_token)
        self._url = f"https://{subdomain}.zendesk.com/oauth/tokens"
        self._form = {"grant_type": "client_credentials", "client_id": client_id, "client_secret": client_secret,
                      "expires_in": str(token_lifetime_s)}
        if scope:
            self._form["scope"] = scope
        self._basic = base64.b64encode(f"{email}/token:{api_token}".encode()).decode() if not self._oauth else ""
        self._token, self._expires_at = "", 0.0
        self._lock = asyncio.Lock()
        self._transport = transport

    @property
    def mode(self) -> str:
        if not self.configured:
            return "not_configured"
        return "oauth_client_credentials" if self._oauth else "api_token"

    async def _access_token(self, force: bool = False) -> str:
        async with self._lock:
            if force or not self._token or time.time() > self._expires_at - 60:
                async with httpx.AsyncClient(timeout=10, transport=self._transport) as c:
                    resp = await c.post(self._url, data=self._form)
                if resp.status_code != 200:
                    raise httpx.HTTPStatusError(f"Zendesk token request failed ({resp.status_code})",
                                                request=resp.request, response=resp)
                data = resp.json()
                self._token = data["access_token"]
                self._expires_at = time.time() + int(data.get("expires_in", 1800))
            return self._token

    async def async_auth_flow(self, request: httpx.Request):
        if not self.configured:
            raise RuntimeError("Zendesk credentials are not configured")
        if not self._oauth:
            request.headers["Authorization"] = f"Basic {self._basic}"
            yield request
            return
        request.headers["Authorization"] = f"Bearer {await self._access_token()}"
        response = yield request
        if response.status_code == 401:  # expired or revoked: get a fresh token once and retry
            request.headers["Authorization"] = f"Bearer {await self._access_token(force=True)}"
            yield request
