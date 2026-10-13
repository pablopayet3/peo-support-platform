"""The Support Ops dashboard: report arithmetic, and the page itself."""
import logging
from datetime import datetime, timedelta, timezone

from integration import ops

from .conftest import SIDEBAR

FIELDS = {"life_event_type": 101, "ai_intake_result": 102, "event_date": 103, "enrollment_outcome": 104,
          "enrollment_id": 105}
NOW = datetime(2026, 10, 12, 18, 0, tzinfo=timezone.utc)


def ticket(tid, created, *, life_event=None, result=None, enrollment=None, due=None, tags=(), status="open",
           channel="email"):
    fields = {101: life_event, 102: result, 105: enrollment}
    return {"id": tid, "created_at": created.isoformat().replace("+00:00", "Z"), "status": status,
            "due_at": due.isoformat().replace("+00:00", "Z") if due else None, "tags": list(tags),
            "via": {"channel": channel}, "custom_fields": [{"id": k, "value": v} for k, v in fields.items()]}


def test_report_joins_tickets_with_approvals():
    tickets = [
        ticket(1, NOW - timedelta(hours=5), life_event="qle_newborn", result="ai_intake_complete", enrollment="ENR-000001",
               due=NOW + timedelta(days=20), tags=["ai_intake_done"]),
        ticket(2, NOW - timedelta(hours=2), life_event="qle_newborn", result="ai_fail_window",
               due=NOW + timedelta(days=1)),                                     # at risk
        ticket(3, NOW - timedelta(hours=1), tags=["ai_intake_skipped"]),  # not a life event
        ticket(4, NOW - timedelta(days=45), life_event="qle_newborn"),           # outside the 30 days
        ticket(5, NOW - timedelta(hours=1), channel="sample_ticket"),            # Zendesk's sample ticket
    ]
    enrollments = [{"enrollment_id": "ENR-000001", "source_ref": "zendesk-ticket:1",
                    "approved_at": (NOW - timedelta(hours=2)).isoformat()}]
    r = ops.build_report(tickets, enrollments, FIELDS, NOW)
    assert r["tickets"] == 3
    assert r["ai"] == {"handed_off": 1, "not_life_event": 1, "read": 2}
    assert r["life_events"] == 2 and r["approved"] == 1 and r["at_risk"] == 1
    assert r["median_time_to_approval"] == "3 h 00 min"
    assert r["checks_pass_rate"] == 0.5
    assert [row["at_risk"] for row in r["rows"]] == [False, True]


def test_dashboard_renders_and_counts_errors(client, fake_zd):
    fake_zd.listed_tickets = [ticket(7, datetime.now(timezone.utc) - timedelta(hours=1), life_event="qle_newborn",
                                     result="ai_intake_complete", due=datetime.now(timezone.utc) + timedelta(days=25))]
    ops.reporter._cache = None
    client.get("/v1/employees/E-1001/summary", headers=SIDEBAR)
    logging.getLogger("integration").error("webhook failed", extra={"extra_fields": {"request_id": "r-1", "error": "boom"}})
    page = client.get("/ops")
    assert page.status_code == 200
    assert "Support Ops dashboard" in page.text and "#7" in page.text and "Newborn" in page.text
    data = client.get("/v1/ops/report").json()
    assert data["reporting"]["life_events"] == 1
    assert data["monitoring"]["requests"] >= 1
    assert data["monitoring"]["recent_errors"][0]["msg"] == "webhook failed"
    assert data["monitoring"]["dependencies"]["zendesk"] == "closed"


def test_dashboard_survives_a_zendesk_outage(client, fake_zd, monkeypatch):
    async def down(*a, **k):
        raise RuntimeError("down")
    monkeypatch.setattr(client.app.state.zendesk, "recent_tickets", down)
    ops.reporter._cache = None
    page = client.get("/ops")
    assert page.status_code == 200 and "Zendesk unavailable" in page.text
