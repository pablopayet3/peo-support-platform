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
    monkeypatch.setenv("INT_MCP_CONSENT_PASSCODE", "let-me-in")
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


def test_mcp_needs_a_token_and_says_where_to_sign_in(mcp_client):
    r = mcp_client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert r.status_code == 401 and "resource_metadata" in r.headers["www-authenticate"]
    meta = mcp_client.get("/.well-known/oauth-protected-resource/mcp").json()
    assert meta["resource"].endswith("/mcp") and meta["authorization_servers"]
    asm = mcp_client.get("/.well-known/oauth-authorization-server").json()
    assert asm["registration_endpoint"].endswith("/register") and "S256" in asm["code_challenge_methods_supported"]


def test_an_ai_assistant_signs_in_with_oauth_and_pkce(mcp_client):
    import base64
    import hashlib
    import secrets
    from urllib.parse import parse_qs, urlparse
    reg = mcp_client.post("/register", json={"redirect_uris": ["https://claude.ai/api/mcp/auth_callback"],
                                             "client_name": "Claude", "token_endpoint_auth_method": "none",
                                             "grant_types": ["authorization_code", "refresh_token"]}).json()
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    r = mcp_client.get("/authorize", params={"response_type": "code", "client_id": reg["client_id"],
                                             "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
                                             "code_challenge": challenge, "code_challenge_method": "S256",
                                             "state": "xyz", "scope": "mcp:read"}, follow_redirects=False)
    consent = r.headers["location"]
    req = parse_qs(urlparse(consent).query)["req"][0]
    page = mcp_client.get("/oauth/consent", params={"req": req})
    assert "Claude" in page.text and "read-only" in page.text
    # Pressing Enter submits the first button in the form: it must be Approve, never Deny
    assert page.text.index('value="approve"') < page.text.index('value="deny"')
    wrong = mcp_client.post("/oauth/consent", data={"req": req, "passcode": "nope", "action": "approve"})
    assert "not correct" in wrong.text
    ok = mcp_client.post("/oauth/consent", data={"req": req, "passcode": "let-me-in", "action": "approve"},
                         follow_redirects=False)
    back = parse_qs(urlparse(ok.headers["location"]).query)
    assert back["state"] == ["xyz"]
    tok = mcp_client.post("/token", data={"grant_type": "authorization_code", "code": back["code"][0],
                                          "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
                                          "client_id": reg["client_id"], "code_verifier": verifier}).json()
    assert tok["token_type"] == "Bearer" and tok["refresh_token"]
    hdr = {**HDR, "Authorization": f"Bearer {tok['access_token']}"}
    assert init(mcp_client).status_code == 200
    assert rpc(mcp_client, "tools/list", headers=hdr, i=2).status_code == 200


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
