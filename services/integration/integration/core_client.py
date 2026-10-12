"""Client for the core platform API (contracts/core-platform-api.yaml)."""
import httpx

from .config import get_settings
from .resilience import CircuitBreaker, Unavailable, call_with_retries

breaker = CircuitBreaker("core-platform")


class NotFound(Exception):
    pass


class Conflict(Exception):
    pass


class CoreClient:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None):
        s = get_settings()
        self._client = httpx.AsyncClient(
            base_url=s.core_base_url, timeout=s.core_timeout_s, transport=transport,
            headers={"Authorization": f"Bearer {s.core_api_key}", "User-Agent": "northwind-integration/1.0"})

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, path: str, request_id: str, **kwargs) -> httpx.Response:
        headers = {"X-Request-Id": request_id, **kwargs.pop("headers", {})}
        resp = await call_with_retries(lambda: self._client.request(method, path, headers=headers, **kwargs), breaker)
        if resp.status_code >= 500:
            raise Unavailable(f"core platform error {resp.status_code}")
        if resp.status_code == 404:
            raise NotFound(path)
        if resp.status_code == 409:
            raise Conflict(resp.json().get("detail", "conflict"))
        resp.raise_for_status()
        return resp

    async def lookup_person(self, *, email: str | None, phone: str | None, request_id: str) -> dict:
        params = {"email": email} if email else {"phone": phone}
        return (await self._request("GET", "/v1/people/lookup", request_id, params=params)).json()

    async def can_act_for(self, actor_person_id: str, employee_id: str, request_id: str) -> bool:
        body = {"actor_person_id": actor_person_id, "employee_id": employee_id}
        return (await self._request("POST", "/v1/authorizations/check", request_id, json=body)).json()["allowed"]

    async def employee(self, employee_id: str, request_id: str) -> dict:
        return (await self._request("GET", f"/v1/employees/{employee_id}", request_id)).json()

    async def coverage(self, employee_id: str, request_id: str) -> dict:
        return (await self._request("GET", f"/v1/employees/{employee_id}/coverage", request_id)).json()

    async def evaluate_qle(self, employee_id: str, event_type: str, event_date: str, request_id: str) -> dict:
        body = {"employee_id": employee_id, "event_type": event_type, "event_date": event_date}
        return (await self._request("POST", "/v1/qle/evaluate", request_id, json=body)).json()

    async def enrollments(self, request_id: str, limit: int = 200) -> list[dict]:
        resp = await self._request("GET", "/v1/enrollments", request_id, params={"limit": limit})
        return resp.json()["enrollments"]

    async def create_enrollment(self, body: dict, idempotency_key: str, request_id: str) -> tuple[dict, bool]:
        resp = await self._request("POST", "/v1/enrollments", request_id, json=body,
                                   headers={"Idempotency-Key": idempotency_key})
        return resp.json(), resp.status_code == 201
