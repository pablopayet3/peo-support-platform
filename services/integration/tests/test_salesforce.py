"""Salesforce as context: account box, activity on approval, demo seeding, and graceful failure."""
import asyncio
import json
from urllib.parse import parse_qs, urlparse

import httpx

from integration import salesforce_client
from integration.config import get_settings
from integration.main import app
from integration.salesforce_client import SalesforceClient

from .conftest import SIDEBAR

INSTANCE = "https://northwind.my.salesforce.com"


class FakeSalesforce:
    def __init__(self):
        self.accounts = {"C-1001": {"Id": "001A", "Name": "Acme Bakery", "Industry": "Food & Beverage",
                                    "NumberOfEmployees": 24, "Rating": "Warm", "Type": "Customer - Direct",
                                    "Owner": {"Name": "Pablo Payet"},
                                    "Opportunities": {"records": [{"Name": "Acme Bakery - 2027 renewal",
                                                                   "CloseDate": "2027-01-01", "Amount": 101400,
                                                                   "StageName": "Negotiation/Review"}]}}}
        self.created: list[tuple[str, dict]] = []
        self.tokens = 0
        self.down = False

    def handler(self, req: httpx.Request) -> httpx.Response:
        if self.down:
            return httpx.Response(503)
        if req.url.path == "/services/oauth2/token":
            self.tokens += 1
            return httpx.Response(200, json={"access_token": "sf-token", "instance_url": INSTANCE})
        assert req.headers["authorization"] == "Bearer sf-token"
        if req.url.path.endswith("/query"):
            q = parse_qs(urlparse(str(req.url)).query)["q"][0]
            if "IN ('C-1001'" in q:
                return httpx.Response(200, json={"records": [{"AccountNumber": k} for k in self.accounts]})
            cid = q.split("AccountNumber = '")[1].split("'")[0]
            rec = self.accounts.get(cid)
            return httpx.Response(200, json={"records": [rec] if rec else []})
        if req.method == "POST" and "/sobjects/" in req.url.path:
            sobject = req.url.path.rsplit("/", 1)[-1]
            self.created.append((sobject, json.loads(req.content)))
            return httpx.Response(201, json={"id": f"00T{len(self.created)}", "success": True})
        return httpx.Response(404)


def configured(monkeypatch, fake):
    s = get_settings()
    monkeypatch.setattr(s, "salesforce_instance_url", INSTANCE)
    monkeypatch.setattr(s, "salesforce_client_id", "consumer-key")
    monkeypatch.setattr(s, "salesforce_client_secret", "consumer-secret")
    salesforce_client.breaker.on_success()
    return SalesforceClient(transport=httpx.MockTransport(fake.handler))


def test_sidebar_summary_includes_the_salesforce_account(client, monkeypatch):
    fake = FakeSalesforce()
    app.state.salesforce = configured(monkeypatch, fake)
    body = client.get("/v1/employees/E-1001/summary", headers=SIDEBAR).json()
    acc = body["account"]
    assert acc["status"] == "ok" and acc["name"] == "Acme Bakery" and acc["owner"] == "Pablo Payet"
    assert acc["renewal"]["date"] == "2027-01-01" and acc["url"].endswith("/lightning/r/Account/001A/view")


def test_salesforce_outage_never_breaks_the_sidebar(client, monkeypatch):
    fake = FakeSalesforce()
    app.state.salesforce = configured(monkeypatch, fake)
    fake.down = True
    r = client.get("/v1/employees/E-1001/summary", headers=SIDEBAR)
    assert r.status_code == 200 and r.json()["account"] == {"status": "unavailable"}


def test_seed_creates_only_missing_accounts(monkeypatch):
    fake = FakeSalesforce()
    sf = configured(monkeypatch, fake)
    assert asyncio.run(sf.seed_demo()) == 2
    made = [o for o, _ in fake.created]
    assert made.count("Account") == 2 and made.count("Opportunity") == 2 and made.count("Contact") == 2


def test_approval_leaves_an_activity_on_the_account(client, fake_zd, monkeypatch):
    fake = FakeSalesforce()
    app.state.salesforce = configured(monkeypatch, fake)
    fake_zd.add_ticket(30, 7001, {101: "qle_newborn", 103: "2026-10-06"})
    body = {"ticket_id": 30, "employee_id": "E-1001", "event_type": "newborn", "event_date": "2026-10-06",
            "dependent": {"first_name": "Sofia", "last_name": "Lopez", "date_of_birth": "2026-10-06"},
            "approver_zendesk_user_id": 9001}
    r = client.post("/v1/enrollments", json=body, headers=SIDEBAR)
    assert r.status_code == 201
    tasks = [f for o, f in fake.created if o == "Task"]
    assert len(tasks) == 1 and tasks[0]["WhatId"] == "001A" and "approved" in tasks[0]["Subject"]
