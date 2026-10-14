import json
import os

os.environ.update({
    "AGENT_ZENDESK_SUBDOMAIN": "northwind-test", "AGENT_ZENDESK_EMAIL": "ai@northwind.example",
    "AGENT_ZENDESK_API_TOKEN": "t", "AGENT_ZENDESK_WEBHOOK_SECRET": "whsec-agent",
    "AGENT_INTEGRATION_BASE_URL": "http://integration.internal", "AGENT_INTEGRATION_API_KEY": "agent-key",
    "AGENT_ZENDESK_FIELD_IDS": json.dumps({"life_event_type": 101, "ai_intake_result": 102, "event_date": 103,
                                           "enrollment_outcome": 104, "enrollment_id": 105}),
})

import httpx  # noqa: E402
import pytest  # noqa: E402


class FakeZendesk:
    def __init__(self):
        self.tickets, self.comments, self.updates = {}, {}, []
        self.users = {1: {"id": 1, "email": "maria.lopez@acmebakery.example"},
                      2: {"id": 2, "email": "daniel.kim@acmebakery.example"},
                      3: {"id": 3, "email": "sam.rivera@harborpine.example"}}

    def new_ticket(self, tid, requester, text):
        self.tickets[tid] = {"id": tid, "requester_id": requester, "tags": [], "custom_fields": []}
        self.comments[tid] = [{"author_id": requester, "public": True, "plain_body": text}]

    def customer_says(self, tid, text):
        self.comments[tid].append({"author_id": self.tickets[tid]["requester_id"], "public": True, "plain_body": text})

    def public_replies(self, tid):
        return [b["comment"]["body"] for t, b in self.updates if t == tid and b.get("comment", {}).get("public")]

    def handler(self, req: httpx.Request) -> httpx.Response:
        p = req.url.path.strip("/").split("/")
        if p[2] == "users":
            return httpx.Response(200, json={"user": self.users[int(p[3].removesuffix(".json"))]})
        tid = int(p[3].removesuffix(".json"))
        t = self.tickets[tid]
        if len(p) == 5 and p[4] == "comments.json":
            return httpx.Response(200, json={"comments": self.comments[tid]})
        if len(p) == 5 and p[4] == "tags.json":
            tags = json.loads(req.content)["tags"]
            t["tags"] = sorted(set(t["tags"]) | set(tags)) if req.method == "PUT" else [x for x in t["tags"] if x not in tags]
            return httpx.Response(200, json={"tags": t["tags"]})
        if req.method == "GET":
            return httpx.Response(200, json={"ticket": t})
        body = json.loads(req.content)["ticket"]
        self.updates.append((tid, body))
        for f in body.get("custom_fields", []):
            t["custom_fields"] = [x for x in t["custom_fields"] if x["id"] != f["id"]] + [f]
        c = body.get("comment")
        if c and c["public"]:
            self.comments[tid].append({"author_id": 999, "public": True, "plain_body": c["body"]})
        return httpx.Response(200, json={"ticket": t})


class FakeIntegration:
    def __init__(self):
        self.calls = []
        self.down = False

    def handler(self, req: httpx.Request) -> httpx.Response:
        if self.down:
            return httpx.Response(503, json={"error": "unavailable"})
        body = json.loads(req.content)
        self.calls.append(body)
        email = body["requester"].get("email")
        if email == "sam.rivera@harborpine.example" and "company_id" not in body:
            return httpx.Response(200, json={"failed_check": "company_unclear", "ai_intake_result": "ai_intake_incomplete",
                                             "checks": [], "company_options": [
                                                 {"company_id": "C-1003", "company_name": "Harbor & Pine Accounting"},
                                                 {"company_id": "C-1002", "company_name": "Bluebird Design Studio"}]})
        return httpx.Response(200, json={
            "failed_check": "none", "ai_intake_result": "ai_intake_complete", "eligible": True,
            "employee_id": "E-1001", "deadline": "2026-11-06T04:59:00Z", "last_day": "2026-11-05",
            "checks": [{"name": "identity", "passed": True, "detail": "the employee themself"},
                       {"name": "active_plan", "passed": True, "detail": "Northwind Gold PPO"},
                       {"name": "enrollment_window", "passed": True, "detail": "open until 2026-11-05"}]})


@pytest.fixture()
def zd():
    return FakeZendesk()


@pytest.fixture()
def integ():
    return FakeIntegration()
