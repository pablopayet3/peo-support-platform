"""Client for the Zendesk Support API, plus webhook signature verification."""
import base64
import hashlib
import hmac
import time
from datetime import datetime

import httpx

from .config import get_settings
from .zendesk_auth import ZendeskAuth
from .resilience import CircuitBreaker, Unavailable, call_with_retries

breaker = CircuitBreaker("zendesk", failure_threshold=8, reset_after_s=20)


def verify_signature(body: bytes, signature: str | None, timestamp: str | None, secret: str,
                     max_age_s: int = 300, now: float | None = None) -> bool:
    """Zendesk signs base64(HMAC-SHA256(secret, timestamp + body)). Old timestamps are refused (replay)."""
    if not (signature and timestamp and secret):
        return False
    try:
        ts = datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return False
    if abs((now or time.time()) - ts) > max_age_s:
        return False
    digest = hmac.new(secret.encode(), timestamp.encode() + body, hashlib.sha256).digest()
    return hmac.compare_digest(base64.b64encode(digest).decode(), signature)


class ZendeskClient:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None):
        s = get_settings()
        self.auth = ZendeskAuth(s.zendesk_subdomain, client_id=s.zendesk_oauth_client_id,
                                client_secret=s.zendesk_oauth_client_secret, scope=s.zendesk_oauth_scope,
                                email=s.zendesk_email, api_token=s.zendesk_api_token, transport=transport)
        self._client = httpx.AsyncClient(
            base_url=f"https://{s.zendesk_subdomain}.zendesk.com", timeout=s.zendesk_timeout_s, transport=transport,
            auth=self.auth, headers={"User-Agent": "northwind-integration/1.0"})
        self.fields = s.field_ids()
        self._secret: str = s.zendesk_webhook_secret
        self._secret_from_api = not s.zendesk_webhook_secret and bool(s.zendesk_webhook_id)
        self._secret_read_at = 0.0

    async def webhook_secret(self, refresh: bool = False) -> str:
        """The signing secret of our webhook. Read from Zendesk when only the webhook ID is configured;
        re-read at most once a minute, so a rotated secret works without a redeploy."""
        if not self._secret_from_api:
            return self._secret
        if self._secret and not (refresh and time.time() - self._secret_read_at > 60):
            return self._secret
        wid = get_settings().zendesk_webhook_id
        data = (await self._request("GET", f"/api/v2/webhooks/{wid}/signing_secret")).json()
        self._secret, self._secret_read_at = data["signing_secret"]["secret"], time.time()
        return self._secret

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        resp = await call_with_retries(lambda: self._client.request(method, path, **kwargs), breaker)
        if resp.status_code >= 500:
            raise Unavailable(f"zendesk error {resp.status_code}")
        resp.raise_for_status()
        return resp

    async def ticket(self, ticket_id: int) -> dict:
        return (await self._request("GET", f"/api/v2/tickets/{ticket_id}.json")).json()["ticket"]

    async def recent_tickets(self, limit: int = 100) -> list[dict]:
        params = {"sort_by": "created_at", "sort_order": "desc", "per_page": min(limit, 100)}
        return (await self._request("GET", "/api/v2/tickets.json", params=params)).json()["tickets"]

    async def user(self, user_id: int) -> dict:
        return (await self._request("GET", f"/api/v2/users/{user_id}.json")).json()["user"]

    async def user_group_ids(self, user_id: int) -> set[int]:
        data = (await self._request("GET", f"/api/v2/users/{user_id}/group_memberships.json")).json()
        return {m["group_id"] for m in data.get("group_memberships", [])}

    def field_value(self, ticket: dict, name: str):
        fid = self.fields.get(name)
        return next((f["value"] for f in ticket.get("custom_fields", []) if f["id"] == fid), None)

    async def update_ticket(self, ticket_id: int, *, fields: dict[str, object] | None = None,
                            ticket_type: str | None = None, due_at: str | None = None,
                            internal_note: str | None = None, add_tags: list[str] | None = None) -> None:
        """One update per call, so the triggers see every value at once. Tags are added, never replaced."""
        body: dict = {}
        if fields:
            body["custom_fields"] = [{"id": self.fields[k], "value": v} for k, v in fields.items()]
        if ticket_type:
            body["type"] = ticket_type
        if due_at:
            body["due_at"] = due_at
        if internal_note:
            body["comment"] = {"body": internal_note, "public": False}
        if body:
            await self._request("PUT", f"/api/v2/tickets/{ticket_id}.json", json={"ticket": body})
        if add_tags:
            # The tags endpoint adds to the list. Writing "tags" on the ticket would replace the list and
            # empty the drop-down fields, whose values are stored as tags (found while testing in Zendesk).
            await self._request("PUT", f"/api/v2/tickets/{ticket_id}/tags.json", json={"tags": add_tags})

    async def remove_tags(self, ticket_id: int, tags: list[str]) -> None:
        await self._request("DELETE", f"/api/v2/tickets/{ticket_id}/tags.json", json={"tags": tags})

    async def update_app_settings(self, installation_id: str, settings: dict) -> None:
        await self._request("PUT", f"/api/v2/apps/installations/{installation_id}.json",
                            json={"settings": settings, "enabled": True})
