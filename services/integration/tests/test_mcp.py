"""The integration layer over MCP: discoverable, read-only tools behind the same key check."""
import json

import pytest

from integration import mcp_server

KEY = {"Authorization": "Bearer mcp-test-key-123456"}
HDR = {**KEY, "Accept": "application/json, text/event-stream", "Content-Type": "application/json",
       "MCP-Protocol-Version": "2025-06-18"}


@pytest.fixture()
def mcp_client(client, monkeypatch):
    monkeypatch.setenv("INT_MCP_API_KEY", "mcp-test-key-123456")
    from integration.config import get_settings
    get_settings.cache_clear()
    mcp_server.STATE.update(core=client.app.state.core)
    yield client
    get_settings.cache_clear()


def rpc(c, method, params=None, headers=HDR, i=1):
    r = c.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": i, "method": method, "params": params or {}})
    return r


def init(c):
    return rpc(c, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                 "clientInfo": {"name": "test", "version": "1"}})


def test_mcp_needs_a_key(mcp_client):
    r = init_no_key = mcp_client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert r.status_code == 401 and "invalid_token" in r.headers["www-authenticate"]


def test_mcp_lists_read_only_tools(mcp_client):
    assert init(mcp_client).status_code == 200
    tools = rpc(mcp_client, "tools/list", i=2).json()["result"]["tools"]
    names = {t["name"] for t in tools}
    assert {"find_person", "get_health_plan", "check_life_event_eligibility", "find_duplicate_payments",
            "get_procedure_steps"} <= names
    assert all(t["annotations"]["readOnlyHint"] for t in tools)


def test_mcp_runs_the_same_checks_as_email(mcp_client):
    init(mcp_client)
    r = rpc(mcp_client, "tools/call", {"name": "check_life_event_eligibility", "arguments": {
        "email": "maria.lopez@acmebakery.example", "event_type": "newborn", "event_date": "2026-10-10"}}, i=3)
    out = r.json()["result"]
    data = out.get("structuredContent") or json.loads(out["content"][0]["text"])
    data = data.get("result", data)
    assert data["ai_intake_result"] == "ai_intake_complete" and "11:59 pm" in data["deadline_text"]
