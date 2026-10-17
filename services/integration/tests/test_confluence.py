"""The sidebar's guidance is read live from the Confluence procedure, with a cached and a built-in fallback."""
import json
from pathlib import Path

import httpx
import pytest

from integration import confluence_client
from integration.confluence_client import ConfluenceClient, guidance_from

from .conftest import AGENT, SIDEBAR

PAGE = (Path(__file__).resolve().parents[3] / "knowledge/benefits-ops/newborn-enrollment-procedure.html").read_text()
TANGO = "https://app.tango.us/app/workflow/Approve-a-newborn-enrollment--Benefits-Ops--70cdb1f314354997892c4160ec163063"
LIVE = PAGE


def test_steps_come_from_the_page_and_the_tango_link_sits_on_its_step():
    steps = guidance_from(LIVE, "ai_intake_complete")
    assert len(steps) >= 5 and steps[0]["text"].startswith("Open the Northwind Benefits app")
    approve = next(s for s in steps if s["text"].startswith("Approve in the sidebar app"))
    assert approve["how_to"]["url"] == TANGO
    assert sum("how_to" in s for s in steps) == 1


@pytest.mark.parametrize("situation,expect", [("ai_fail_identity", "identity"), ("ai_fail_no_plan", "plan"),
                                              ("ai_fail_window", "approve")])
def test_failed_checks_use_the_matching_row_of_the_table(situation, expect):
    steps = guidance_from(PAGE, situation)
    assert steps and any(expect in s["text"].lower() for s in steps)


class Fake:
    def __init__(self):
        self.status, self.body, self.calls = 200, LIVE, 0

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.calls += 1
        assert req.url.path == "/wiki/api/v2/pages/524289" and req.url.params["body-format"] == "storage"
        if self.status != 200:
            return httpx.Response(self.status)
        return httpx.Response(200, json={"title": "Newborn: add a baby to a health plan", "version": {
            "number": 7, "createdAt": "2026-10-17T12:00:00Z"}, "_links": {"webui": "/spaces/BOPS/pages/524289"},
            "body": {"storage": {"value": self.body}}})


@pytest.fixture()
def live(client, monkeypatch):
    for k, v in {"INT_CONFLUENCE_BASE_URL": "https://northwind.atlassian.net", "INT_CONFLUENCE_PAGE_ID": "524289",
                 "INT_CONFLUENCE_EMAIL": "ops@northwind.example", "INT_CONFLUENCE_API_TOKEN": "t",
                 "INT_CONFLUENCE_CACHE_S": "0"}.items():
        monkeypatch.setenv(k, v)
    from integration.config import get_settings
    get_settings.cache_clear()
    confluence_client.breaker.on_success()
    fake = Fake()
    client.app.state.confluence = ConfluenceClient(transport=httpx.MockTransport(fake.handler))
    yield fake
    get_settings.cache_clear()


def test_sidebar_gets_live_steps_and_an_edit_shows_without_a_deploy(client, live):
    r = client.get("/v1/guidance", params={"situation": "ai_intake_complete"}, headers=SIDEBAR)
    body = r.json()
    assert r.status_code == 200 and body["source"] == "confluence" and body["page"]["version"] == 7
    assert body["page"]["url"] == "https://northwind.atlassian.net/wiki/spaces/BOPS/pages/524289"
    live.body = LIVE.replace("Confirm the baby's legal name", "Confirm the baby's full legal name")
    steps = client.get("/v1/guidance", params={"situation": "ai_intake_complete"}, headers=SIDEBAR).json()["steps"]
    assert any("full legal name" in s["text"] for s in steps)


def test_confluence_down_uses_the_last_copy_then_says_so(client, live):
    client.get("/v1/guidance", params={"situation": "ai_intake_complete"}, headers=SIDEBAR)
    live.status = 503
    body = client.get("/v1/guidance", params={"situation": "ai_intake_complete"}, headers=SIDEBAR).json()
    assert body["source"] == "confluence_cached" and body["steps"]


def test_never_read_and_not_configured_falls_back_to_the_built_in_copy(client):
    body = client.get("/v1/guidance", params={"situation": "ai_fail_window"}, headers=SIDEBAR).json()
    assert body["source"] == "built_in" and body["steps"]


def test_guidance_needs_the_sidebar_scope_and_a_known_situation(client):
    assert client.get("/v1/guidance", params={"situation": "ai_intake_complete"}, headers=AGENT).status_code == 403
    assert client.get("/v1/guidance", params={"situation": "anything"}, headers=SIDEBAR).status_code == 400


def test_each_life_event_reads_its_own_procedure_page(client, live, monkeypatch):
    monkeypatch.setenv("INT_CONFLUENCE_PAGE_IDS", '{"marriage": "524289"}')
    monkeypatch.setenv("INT_CONFLUENCE_PAGE_ID", "999")
    from integration.config import get_settings
    get_settings.cache_clear()
    client.app.state.confluence = ConfluenceClient(transport=httpx.MockTransport(live.handler))
    r = client.get("/v1/guidance", params={"situation": "ai_intake_complete", "event_type": "marriage"}, headers=SIDEBAR)
    assert r.json()["source"] == "confluence"   # the fake only answers for page 524289
