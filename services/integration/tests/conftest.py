"""Integration tests: the real integration service talks to the real core platform in-process,
and to a fake Zendesk that records every update."""
import json
import os
import sys
from pathlib import Path

os.environ.update({
    "CORE_DATABASE_URL": "sqlite:///:memory:",
    "CORE_FROZEN_TODAY": "2026-10-12",
    "CORE_MIN_KEY_LENGTH": "6",
    "INT_MIN_KEY_LENGTH": "6",
    "CORE_API_KEYS": json.dumps({"core-key": {"client": "integration-service",
                                              "scopes": ["identity:read", "benefits:read", "enrollments:write"]}}),
    "INT_API_KEYS": json.dumps({
        "agent-key": {"client": "intake-agent", "scopes": ["checks:run"]},
        "sidebar-key": {"client": "zendesk-sidebar-app", "scopes": ["checks:run", "employees:read", "enrollments:approve"]},
    }),
    "INT_CORE_BASE_URL": "http://core.internal",
    "INT_CORE_API_KEY": "core-key",
    "INT_ZENDESK_SUBDOMAIN": "northwind-test",
    "INT_ZENDESK_EMAIL": "ops@northwind.example",
    "INT_ZENDESK_API_TOKEN": "test-token",
    "INT_ZENDESK_WEBHOOK_SECRET": "whsec-test",
    "INT_ZENDESK_FIELD_IDS": json.dumps({"life_event_type": 101, "ai_intake_result": 102, "event_date": 103,
                                         "enrollment_outcome": 104, "enrollment_id": 105}),
    "INT_BENEFITS_OPS_GROUP_ID": "500",
})
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "core_platform"))

import httpx  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from core_platform.db import Base, SessionLocal, engine  # noqa: E402
from core_platform.main import app as core_app  # noqa: E402
from core_platform.seed import seed  # noqa: E402
from integration import core_client, zendesk_client  # noqa: E402
from integration.core_client import CoreClient  # noqa: E402
from integration.main import app  # noqa: E402
from integration.zendesk_client import ZendeskClient  # noqa: E402

AGENT = {"Authorization": "Bearer agent-key"}
SIDEBAR = {"Authorization": "Bearer sidebar-key"}


class FakeZendesk:
    """Just enough of the Zendesk API for these workflows."""

    def __init__(self):
        self.tickets: dict[int, dict] = {}
        self.users = {7001: {"id": 7001, "email": "maria.lopez@acmebakery.example", "phone": None},
                      7002: {"id": 7002, "email": "priya.shah@acmebakery.example", "phone": None}}
        self.groups = {9001: [500], 9002: [600]}  # 9001 is in Benefits Ops, 9002 is not
        self.updates: list[tuple[int, dict]] = []
        self.tags: dict[int, set] = {}
        self.signing_secret, self.secret_reads = "whsec-from-zendesk", 0
        self.app_settings_updates: list[dict] = []

    def add_ticket(self, tid: int, requester_id: int, fields: dict[int, object]):
        self.tickets[tid] = {"id": tid, "requester_id": requester_id,
                             "custom_fields": [{"id": k, "value": v} for k, v in fields.items()]}
        self.tags[tid] = {"qle_check_requested"}

    def handler(self, request: httpx.Request) -> httpx.Response:
        path, m = request.url.path, request.method
        parts = path.strip("/").split("/")
        if parts[:3] == ["api", "v2", "tickets"]:
            tid = int(parts[3].removesuffix(".json"))
            if len(parts) == 5 and parts[4] == "tags.json":
                tags = set(json.loads(request.content)["tags"])
                self.tags[tid] = self.tags[tid] | tags if m == "PUT" else self.tags[tid] - tags
                return httpx.Response(200, json={"tags": sorted(self.tags[tid])})
            if m == "GET":
                return httpx.Response(200, json={"ticket": self.tickets[tid]})
            body = json.loads(request.content)["ticket"]
            self.updates.append((tid, body))
            t = self.tickets.setdefault(tid, {"id": tid, "custom_fields": []})
            for f in body.get("custom_fields", []):
                t["custom_fields"] = [x for x in t["custom_fields"] if x["id"] != f["id"]] + [f]
            return httpx.Response(200, json={"ticket": t})
        if parts[:4] == ["api", "v2", "apps", "installations"]:
            self.app_settings_updates.append(json.loads(request.content))
            return httpx.Response(200, json={"id": 1})
        if parts[:3] == ["api", "v2", "webhooks"] and parts[-1] == "signing_secret":
            self.secret_reads += 1
            return httpx.Response(200, json={"signing_secret": {"algorithm": "SHA256", "secret": self.signing_secret}})
        if parts[:3] == ["api", "v2", "users"]:
            uid = int(parts[3].removesuffix(".json"))
            if len(parts) == 5:
                return httpx.Response(200, json={"group_memberships": [{"group_id": g} for g in self.groups.get(uid, [])]})
            return httpx.Response(200, json={"user": self.users[uid]})
        return httpx.Response(404, json={"error": "not found"})


@pytest.fixture()
def fake_zd():
    return FakeZendesk()


@pytest.fixture()
def client(fake_zd):
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        seed(db)
    core_client.breaker.on_success()
    zendesk_client.breaker.on_success()
    with TestClient(app) as c:
        app.state.core = CoreClient(transport=httpx.ASGITransport(app=core_app))
        app.state.zendesk = ZendeskClient(transport=httpx.MockTransport(fake_zd.handler))
        yield c
