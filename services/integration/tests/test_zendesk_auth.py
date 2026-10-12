"""OAuth client credentials against a fake Zendesk: token fetched once, reused, renewed on 401."""
import asyncio

import httpx

from integration.zendesk_auth import ZendeskAuth


class FakeOAuthZendesk:
    def __init__(self):
        self.tokens_issued, self.valid, self.forms = 0, set(), []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/tokens":
            form = dict(httpx.QueryParams(request.content.decode()))
            self.forms.append(form)
            if form.get("client_secret") != "s3cret":
                return httpx.Response(401, json={"error": "invalid_client"})
            self.tokens_issued += 1
            token = f"tok-{self.tokens_issued}"
            self.valid = {token}
            return httpx.Response(200, json={"access_token": token, "token_type": "bearer", "expires_in": 3600})
        if request.headers.get("authorization", "").removeprefix("Bearer ") in self.valid:
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(401, json={"error": "invalid_token"})


def run(coro):
    return asyncio.run(coro)


def test_token_is_fetched_once_and_reused():
    z = FakeOAuthZendesk()
    t = httpx.MockTransport(z.handler)
    auth = ZendeskAuth("acme", client_id="northwind_integration", client_secret="s3cret", scope="read write",
                       transport=t)

    async def go():
        async with httpx.AsyncClient(base_url="https://acme.zendesk.com", auth=auth, transport=t) as c:
            return [(await c.get("/api/v2/tickets/1.json")).status_code for _ in range(3)]

    assert run(go()) == [200, 200, 200]
    assert z.tokens_issued == 1
    assert z.forms[0]["grant_type"] == "client_credentials" and z.forms[0]["scope"] == "read write"
    assert auth.mode == "oauth_client_credentials"


def test_revoked_token_is_replaced_and_the_call_retried():
    z = FakeOAuthZendesk()
    t = httpx.MockTransport(z.handler)
    auth = ZendeskAuth("acme", client_id="northwind_integration", client_secret="s3cret", transport=t)

    async def go():
        async with httpx.AsyncClient(base_url="https://acme.zendesk.com", auth=auth, transport=t) as c:
            first = (await c.get("/api/v2/tickets/1.json")).status_code
            z.valid = set()  # Zendesk revokes the token
            second = (await c.get("/api/v2/tickets/1.json")).status_code
            return first, second

    assert run(go()) == (200, 200)
    assert z.tokens_issued == 2


def test_api_token_mode_still_works_for_older_accounts():
    auth = ZendeskAuth("acme", email="ops@acme.example", api_token="t")
    assert auth.mode == "api_token"


def test_missing_credentials_do_not_stop_the_service():
    auth = ZendeskAuth("acme")
    assert auth.mode == "not_configured"
