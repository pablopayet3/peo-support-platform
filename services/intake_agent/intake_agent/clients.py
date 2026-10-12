"""Small clients for Zendesk and the integration service. Both use time limits and retries."""
import asyncio
import base64

import httpx

from .config import get_settings


async def _send(client: httpx.AsyncClient, method: str, url: str, attempts: int = 3, **kw) -> httpx.Response:
    for attempt in range(1, attempts + 1):
        try:
            resp = await client.request(method, url, **kw)
            if resp.status_code not in (429, 502, 503, 504) or attempt == attempts:
                return resp
            delay = int(resp.headers.get("retry-after", "1")) if resp.status_code == 429 else 0.3 * 2 ** attempt
        except (httpx.TimeoutException, httpx.TransportError):
            if attempt == attempts:
                raise
            delay = 0.3 * 2 ** attempt
        await asyncio.sleep(min(delay, 10))
    raise RuntimeError("unreachable")


class Zendesk:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None):
        s = get_settings()
        token = base64.b64encode(f"{s.zendesk_email}/token:{s.zendesk_api_token}".encode()).decode()
        self.c = httpx.AsyncClient(base_url=f"https://{s.zendesk_subdomain}.zendesk.com", timeout=10,
                                   headers={"Authorization": f"Basic {token}"}, transport=transport)
        self.fields = s.field_ids()

    async def _ok(self, method, url, **kw) -> dict:
        r = await _send(self.c, method, url, **kw)
        r.raise_for_status()
        return r.json() if r.content else {}

    async def ticket(self, tid: int) -> dict:
        return (await self._ok("GET", f"/api/v2/tickets/{tid}.json"))["ticket"]

    async def comments(self, tid: int) -> list[dict]:
        return (await self._ok("GET", f"/api/v2/tickets/{tid}/comments.json"))["comments"]

    async def user(self, uid: int) -> dict:
        return (await self._ok("GET", f"/api/v2/users/{uid}.json"))["user"]

    def field(self, ticket: dict, name: str):
        fid = self.fields.get(name)
        return next((f["value"] for f in ticket.get("custom_fields", []) if f["id"] == fid), None)

    async def update(self, tid: int, *, fields: dict | None = None, comment: str | None = None, public: bool = False,
                     ticket_type: str | None = None, due_at: str | None = None) -> None:
        body: dict = {}
        if fields:
            body["custom_fields"] = [{"id": self.fields[k], "value": v} for k, v in fields.items()]
        if comment:
            body["comment"] = {"body": comment, "public": public}
        if ticket_type:
            body["type"] = ticket_type
        if due_at:
            body["due_at"] = due_at
        await self._ok("PUT", f"/api/v2/tickets/{tid}.json", json={"ticket": body})

    async def add_tags(self, tid: int, tags: list[str]) -> None:
        await self._ok("PUT", f"/api/v2/tickets/{tid}/tags.json", json={"tags": tags})

    async def remove_tags(self, tid: int, tags: list[str]) -> None:
        await self._ok("DELETE", f"/api/v2/tickets/{tid}/tags.json", json={"tags": tags})


class Integration:
    """Client for the integration service (contracts/integration-api.yaml)."""

    class Unavailable(Exception):
        pass

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None):
        s = get_settings()
        self.c = httpx.AsyncClient(base_url=s.integration_base_url, timeout=8, transport=transport,
                                   headers={"Authorization": f"Bearer {s.integration_api_key}"})

    async def check(self, body: dict, request_id: str) -> dict:
        try:
            r = await _send(self.c, "POST", "/v1/qle/check", json=body, headers={"X-Request-Id": request_id})
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise self.Unavailable(repr(exc))
        if r.status_code >= 500:
            raise self.Unavailable(f"integration service answered {r.status_code}")
        r.raise_for_status()
        return r.json()
