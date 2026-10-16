import base64
import hashlib
import hmac
import json
from datetime import datetime, timezone

import httpx

from integration.core_client import CoreClient
from integration.main import app

from .conftest import AGENT, SIDEBAR


def check(client, requester, **kw):
    body = {"requester": requester, "event_type": "newborn", "event_date": "2026-10-06", **kw}
    return client.post("/v1/qle/check", json=body, headers=AGENT)


# ---- the checks, one per case ----------------------------------------------------------------------

def test_employee_newborn_complete(client):
    r = check(client, {"email": "maria.lopez@acmebakery.example"})
    assert r.status_code == 200
    body = r.json()
    assert body["ai_intake_result"] == "ai_intake_complete"
    assert body["employee_id"] == "E-1001"
    assert body["deadline"] == "2026-11-06T04:59:00Z"   # November 5, 11:59 pm Eastern
    assert [c["name"] for c in body["checks"]] == ["identity", "active_plan", "enrollment_window"]
    assert r.headers["X-Request-Id"]


def test_admin_for_employee_of_same_company(client):
    r = check(client, {"email": "daniel.kim@acmebakery.example"}, employee={"email": "maria.lopez@acmebakery.example"})
    assert r.json()["ai_intake_result"] == "ai_intake_complete"
    assert r.json()["checks"][0]["detail"] == "admin acting for the employee"


def test_admin_for_employee_of_another_company_fails_identity(client):
    r = check(client, {"email": "daniel.kim@acmebakery.example"}, employee={"email": "tom.becker@bluebird.example"})
    assert r.json()["ai_intake_result"] == "ai_fail_identity"


def test_unknown_sender_fails_identity(client):
    assert check(client, {"email": "someone@unknown.example"}).json()["failed_check"] == "identity"


def test_no_active_plan(client):
    assert check(client, {"email": "priya.shah@acmebakery.example"}).json()["ai_intake_result"] == "ai_fail_no_plan"


def test_window_closed_still_returns_the_deadline(client):
    r = check(client, {"email": "tom.becker@bluebird.example"}, event_date="2026-09-01").json()
    assert r["ai_intake_result"] == "ai_fail_window"
    assert r["deadline"] == "2026-10-02T06:59:00Z"   # October 1, 11:59 pm Pacific
    assert r["last_day"] == "2026-10-01"   # the date the note and replies show


def test_two_companies_asks_which_one_then_completes(client):
    r = check(client, {"email": "sam.rivera@harborpine.example"}).json()
    assert r["failed_check"] == "company_unclear" and r["ai_intake_result"] == "ai_intake_incomplete"
    assert {o["company_id"] for o in r["company_options"]} == {"C-1002", "C-1003"}
    r2 = check(client, {"email": "sam.rivera@harborpine.example"}, company_id="C-1002").json()
    assert r2["ai_intake_result"] == "ai_intake_complete" and r2["employee_id"] == "E-1006"


def test_phone_lookup(client):
    assert check(client, {"phone": "+12125550101"}).json()["employee_id"] == "E-1001"


# ---- failure handling --------------------------------------------------------------------------------

def test_core_down_gives_503_so_the_intake_ends_incomplete(client):
    app.state.core = CoreClient(transport=httpx.MockTransport(lambda req: httpx.Response(503)))
    r = check(client, {"email": "maria.lopez@acmebakery.example"})
    assert r.status_code == 503 and r.json()["error"] == "unavailable"


def test_switch_turns_checks_off(client, monkeypatch):
    from integration.config import get_settings
    monkeypatch.setattr(get_settings(), "checks_enabled", False)
    assert check(client, {"email": "maria.lopez@acmebakery.example"}).status_code == 503


def test_keys_and_scopes(client):
    assert client.post("/v1/qle/check", json={}).status_code == 401
    r = client.get("/v1/employees/E-1001/summary", headers=AGENT)   # the agent has no employees:read scope
    assert r.status_code == 403


def test_bad_input_is_rejected(client):
    r = client.post("/v1/qle/check", json={"requester": {}, "event_type": "newborn", "event_date": "2026-10-06"},
                    headers=AGENT)
    assert r.status_code == 422


# ---- sidebar app: summary and approval ---------------------------------------------------------------

def test_summary_has_no_sensitive_data(client):
    r = client.get("/v1/employees/E-1001/summary", headers=SIDEBAR)
    assert r.status_code == 200
    assert r.json()["medical_plan"]["name"] == "Northwind Gold PPO"
    assert "ssn" not in r.text.lower()


APPROVAL = {"ticket_id": 11, "employee_id": "E-1001", "event_type": "newborn", "event_date": "2026-10-06",
            "dependent": {"first_name": "Sofia", "last_name": "Lopez", "date_of_birth": "2026-10-06"},
            "approver_zendesk_user_id": 9001}


def test_approval_creates_one_enrollment_and_updates_the_ticket(client, fake_zd):
    first = client.post("/v1/enrollments", json=APPROVAL, headers=SIDEBAR)
    assert first.status_code == 201, first.text
    enr = first.json()["enrollment_id"]
    assert first.json()["ticket_updated"] is True
    tid, update = fake_zd.updates[-1]
    assert tid == 11
    fields = {f["id"]: f["value"] for f in update["custom_fields"]}
    assert fields == {104: "qle_approved", 105: enr}
    assert update["comment"]["public"] is False
    again = client.post("/v1/enrollments", json=APPROVAL, headers=SIDEBAR)   # double click, or a retry
    assert again.status_code == 200 and again.json()["enrollment_id"] == enr


def test_approval_refused_outside_benefits_ops(client):
    r = client.post("/v1/enrollments", json={**APPROVAL, "approver_zendesk_user_id": 9002}, headers=SIDEBAR)
    assert r.status_code == 403


def test_approval_refused_when_window_closed(client):
    body = {**APPROVAL, "ticket_id": 12, "employee_id": "E-1004", "event_date": "2026-09-01",
            "dependent": {**APPROVAL["dependent"], "date_of_birth": "2026-09-01"}}
    assert client.post("/v1/enrollments", json=body, headers=SIDEBAR).status_code == 409


# ---- the Zendesk webhook (trigger 45-CHECK) -----------------------------------------------------------

def signed(body: dict, secret="whsec-test"):
    raw = json.dumps(body).encode()
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    sig = base64.b64encode(hmac.new(secret.encode(), ts.encode() + raw, hashlib.sha256).digest()).decode()
    return raw, {"X-Zendesk-Webhook-Signature": sig, "X-Zendesk-Webhook-Signature-Timestamp": ts,
                 "Content-Type": "application/json"}


def test_webhook_rejects_a_bad_signature(client):
    raw, headers = signed({"ticket_id": 20}, secret="wrong")
    assert client.post("/v1/webhooks/zendesk", content=raw, headers=headers).status_code == 401


def test_webhook_runs_the_checks_and_writes_the_ticket(client, fake_zd):
    fake_zd.add_ticket(20, 7001, {101: "qle_newborn", 103: "2026-10-06"})
    raw, headers = signed({"ticket_id": 20})
    r = client.post("/v1/webhooks/zendesk", content=raw, headers=headers)
    assert r.status_code == 202
    tid, update = fake_zd.updates[-1]
    assert tid == 20
    assert update["type"] == "task" and update["due_at"] == "2026-11-06T04:59:00Z"
    assert {f["id"]: f["value"] for f in update["custom_fields"]} == {102: "ai_intake_complete"}
    assert "Automatic checks" in update["comment"]["body"]


def test_webhook_failed_check_is_written_too(client, fake_zd):
    fake_zd.add_ticket(21, 7002, {101: "qle_newborn", 103: "2026-10-06"})
    raw, headers = signed({"ticket_id": 21})
    client.post("/v1/webhooks/zendesk", content=raw, headers=headers)
    assert {f["id"]: f["value"] for f in fake_zd.updates[-1][1]["custom_fields"]} == {102: "ai_fail_no_plan"}


def test_webhook_without_event_date_asks_for_it_and_rearms_the_trigger(client, fake_zd):
    fake_zd.add_ticket(22, 7001, {101: "qle_newborn"})
    raw, headers = signed({"ticket_id": 22})
    client.post("/v1/webhooks/zendesk", content=raw, headers=headers)
    assert "Event date" in fake_zd.updates[-1][1]["comment"]["body"]
    assert "qle_check_requested" not in fake_zd.tags[22]


def test_webhook_is_idempotent(client, fake_zd):
    fake_zd.add_ticket(23, 7001, {101: "qle_newborn", 103: "2026-10-06", 102: "ai_intake_complete"})
    raw, headers = signed({"ticket_id": 23})
    client.post("/v1/webhooks/zendesk", content=raw, headers=headers)
    assert fake_zd.updates == []


def test_signing_secret_is_read_from_zendesk_when_only_the_webhook_id_is_set(client, fake_zd, monkeypatch):
    from integration.config import get_settings
    monkeypatch.setattr(get_settings(), "zendesk_webhook_id", "01HWEBHOOK")
    zd = app.state.zendesk
    zd._secret, zd._secret_from_api = "", True
    fake_zd.add_ticket(23, 7001, {101: "qle_newborn", 103: "2026-10-06"})
    raw, headers = signed({"ticket_id": 23}, secret="whsec-from-zendesk")
    assert client.post("/v1/webhooks/zendesk", content=raw, headers=headers).status_code == 202
    raw, headers = signed({"ticket_id": 23}, secret="whsec-from-zendesk")
    assert client.post("/v1/webhooks/zendesk", content=raw, headers=headers).status_code == 202
    assert fake_zd.secret_reads == 1  # cached after the first read

    # Zendesk rotates the secret: the next webhook triggers one fresh read, and works
    fake_zd.signing_secret = "whsec-rotated"
    zd._secret_read_at = 0
    raw, headers = signed({"ticket_id": 23}, secret="whsec-rotated")
    assert client.post("/v1/webhooks/zendesk", content=raw, headers=headers).status_code == 202
    assert fake_zd.secret_reads == 2


def test_note_words_the_deadline_in_company_time_and_asks_for_the_company():
    from integration.workflows import _note_for
    note = _note_for({"failed_check": "none", "last_day": "2026-10-01", "deadline": "2026-10-02T06:59:00Z",
                      "checks": [{"name": "identity", "passed": True, "detail": "the employee themself"}]})
    assert "Enrollment deadline: October 1, 2026, 11:59 pm company time" in note
    note = _note_for({"failed_check": "company_unclear", "checks": [],
                      "company_options": [{"company_id": "C-1003", "company_name": "Harbor & Pine Accounting"},
                                          {"company_id": "C-1002", "company_name": "Bluebird Design Studio"}]})
    assert "needs input" in note and "Harbor & Pine Accounting and Bluebird Design Studio" in note
    assert "FAILED" not in note


def test_context_for_a_deductible_question_returns_the_plan(client):
    r = client.post("/v1/requests/context", headers=AGENT, json={
        "requester": {"email": "maria.lopez@acmebakery.example"}, "topic": "benefits_question"}).json()
    assert r["verified"] and r["name"] == "Maria Lopez"
    med = r["memberships"][0]["medical"]
    assert med["plan_name"] == "Northwind Gold PPO" and med["deductible"] == {"individual": 500, "family": 1000}


def test_context_for_an_unknown_sender_is_not_verified(client):
    r = client.post("/v1/requests/context", headers=AGENT, json={
        "requester": {"email": "stranger@nowhere.example"}, "topic": "billing"}).json()
    assert r["verified"] is False and r["memberships"] == []


def test_context_for_billing_finds_the_double_payment_for_an_admin_only(client):
    admin = client.post("/v1/requests/context", headers=AGENT, json={
        "requester": {"email": "daniel.kim@acmebakery.example"}, "topic": "billing"}).json()
    assert admin["is_admin"] is True
    inv = admin["billing"][0]["overpaid_invoices"][0]
    assert inv["invoice_id"] == "INV-2026-10-001" and inv["overpaid_by"] == 8450.0
    employee = client.post("/v1/requests/context", headers=AGENT, json={
        "requester": {"email": "maria.lopez@acmebakery.example"}, "topic": "billing"}).json()
    assert employee["is_admin"] is False and employee["billing"] == []
