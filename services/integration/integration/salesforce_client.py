"""Salesforce: the CRM view of each client company, and a record of what Support did for it.

Auth is the OAuth 2.0 client credentials flow of an External Client App, running as an integration user.
Salesforce is context, never a gate: if it is slow or down, the checks and the approval still work and the
sidebar shows the account box as unavailable.

The core platform's company ID (C-1001) is stored on the Account as AccountNumber, so both systems agree
on who the client is without copying data between them.
"""
import asyncio
import logging
import re
import time
from datetime import date

import httpx

from .config import get_settings
from .resilience import CircuitBreaker, Unavailable, call_with_retries

log = logging.getLogger("integration")
breaker = CircuitBreaker("salesforce", failure_threshold=4, reset_after_s=60)
API = "v62.0"
COMPANY_ID = re.compile(r"^C-\d{4}$")

# Demo accounts, matching the core platform's sample companies. Created once if missing.
DEMO_ACCOUNTS = [
    {"AccountNumber": "C-1001", "Name": "Acme Bakery", "Industry": "Food & Beverage", "NumberOfEmployees": 24,
     "Type": "Customer - Direct", "Rating": "Warm", "BillingCity": "New York",
     "renewal": ("Acme Bakery - 2027 renewal", "2027-01-01", 101400, "Negotiation/Review"),
     "contact": ("Daniel", "Kim", "Office Manager, Northwind admin")},
    {"AccountNumber": "C-1002", "Name": "Bluebird Design Studio", "Industry": "Media", "NumberOfEmployees": 18,
     "Type": "Customer - Direct", "Rating": "Hot", "BillingCity": "Los Angeles",
     "renewal": ("Bluebird Design Studio - 2027 renewal", "2027-03-01", 64800, "Value Proposition"),
     "contact": ("Tom", "Becker", "Studio Lead")},
    {"AccountNumber": "C-1003", "Name": "Harbor & Pine Accounting", "Industry": "Finance", "NumberOfEmployees": 12,
     "Type": "Customer - Direct", "Rating": "Cold", "BillingCity": "Chicago",
     "renewal": ("Harbor & Pine Accounting - renewal", "2026-12-01", 52200, "Perception Analysis"),
     "contact": ("Sam", "Rivera", "Managing Partner, Northwind admin")},
]


class SalesforceClient:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None):
        s = get_settings()
        self.login_url = s.salesforce_instance_url.rstrip("/")
        self.configured = bool(self.login_url and s.salesforce_client_id and s.salesforce_client_secret)
        self._form = {"grant_type": "client_credentials", "client_id": s.salesforce_client_id,
                      "client_secret": s.salesforce_client_secret}
        self._client = httpx.AsyncClient(timeout=s.salesforce_timeout_s, transport=transport,
                                         headers={"User-Agent": "northwind-integration/1.0"})
        self._token, self._instance, self._expires_at = "", self.login_url, 0.0
        self._lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _access_token(self, force: bool = False) -> str:
        async with self._lock:
            if force or not self._token or time.time() > self._expires_at:
                resp = await self._client.post(f"{self.login_url}/services/oauth2/token", data=self._form)
                if resp.status_code != 200:
                    raise Unavailable(f"salesforce token request failed ({resp.status_code})")
                data = resp.json()
                self._token, self._instance = data["access_token"], data.get("instance_url", self.login_url)
                self._expires_at = time.time() + 30 * 60  # renewed well before the session timeout
            return self._token

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        if not self.configured:
            raise Unavailable("salesforce is not configured")

        async def send() -> httpx.Response:
            token = await self._access_token()
            resp = await self._client.request(method, f"{self._instance}{path}",
                                              headers={"Authorization": f"Bearer {token}"}, **kwargs)
            if resp.status_code == 401:  # session expired or revoked
                token = await self._access_token(force=True)
                resp = await self._client.request(method, f"{self._instance}{path}",
                                                  headers={"Authorization": f"Bearer {token}"}, **kwargs)
            return resp

        resp = await call_with_retries(send, breaker)
        if resp.status_code >= 500:
            raise Unavailable(f"salesforce error {resp.status_code}")
        if resp.status_code >= 400:  # Salesforce explains what it refused in the body: keep it for the logs
            raise httpx.HTTPStatusError(f"salesforce {resp.status_code}: {resp.text[:300]}",
                                        request=resp.request, response=resp)
        return resp

    async def query(self, soql: str) -> list[dict]:
        return (await self._request("GET", f"/services/data/{API}/query", params={"q": soql})).json()["records"]

    async def create(self, sobject: str, fields: dict) -> str:
        return (await self._request("POST", f"/services/data/{API}/sobjects/{sobject}", json=fields)).json()["id"]

    async def account_context(self, company_id: str) -> dict | None:
        """The account box in the sidebar: who the client is commercially, who owns it, when it renews."""
        if not COMPANY_ID.match(company_id):
            return None
        rows = await self.query(
            "SELECT Id, Name, Industry, NumberOfEmployees, Rating, Type, Owner.Name, "
            "(SELECT Name, CloseDate, Amount, StageName FROM Opportunities WHERE IsClosed = false "
            "ORDER BY CloseDate LIMIT 1) FROM Account WHERE AccountNumber = '" + company_id + "' LIMIT 1")
        if not rows:
            return None
        a = rows[0]
        opp = ((a.get("Opportunities") or {}).get("records") or [None])[0]
        renewal = None
        if opp:
            days = (date.fromisoformat(opp["CloseDate"]) - date.today()).days
            renewal = {"name": opp["Name"], "date": opp["CloseDate"], "days_away": days, "amount": opp["Amount"],
                       "stage": opp["StageName"]}
        return {"account_id": a["Id"], "name": a["Name"], "industry": a.get("Industry"),
                "employees": a.get("NumberOfEmployees"), "rating": a.get("Rating"), "type": a.get("Type"),
                "owner": (a.get("Owner") or {}).get("Name"), "renewal": renewal,
                "url": f"{self._instance}/lightning/r/Account/{a['Id']}/view"}

    async def log_task(self, account_id: str, subject: str, description: str) -> str:
        return await self.create("Task", {"WhatId": account_id, "Subject": subject[:255], "Description": description,
                                          "Status": "Completed", "Priority": "Normal",
                                          "ActivityDate": date.today().isoformat()})

    async def seed_demo(self) -> int:
        """Create the demo accounts, renewals and contacts that are missing. Returns how many accounts it made."""
        existing = {r["AccountNumber"] for r in await self.query(
            "SELECT AccountNumber FROM Account WHERE AccountNumber IN ('C-1001','C-1002','C-1003')")}
        made = 0
        for a in DEMO_ACCOUNTS:
            if a["AccountNumber"] in existing:
                continue
            fields = {k: v for k, v in a.items() if k not in ("renewal", "contact")}
            account_id = await self.create("Account", fields)
            name, close, amount, stage = a["renewal"]
            await self.create("Opportunity", {"AccountId": account_id, "Name": name, "CloseDate": close,
                                              "Amount": amount, "StageName": stage, "Type": "Existing Customer - Upgrade"})
            first, last, title = a["contact"]
            await self.create("Contact", {"AccountId": account_id, "FirstName": first, "LastName": last, "Title": title})
            made += 1
        return made
