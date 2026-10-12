"""Support Ops dashboard: reporting on life-event tickets, and monitoring of this service.

Reporting reads Zendesk (tickets, fields, Fin's tags) and the core platform (when each enrollment was
approved). Monitoring comes from this service's own structured logs, counted in memory since the last
deploy: request volume, errors, latency, webhook outcomes, and the state of each downstream system.
In production, monitoring would ship to Datadog and reporting would live in Zendesk Explore or the
data warehouse; the metrics and their definitions stay the same.
"""
import html
import logging
import statistics
import time
from collections import Counter, deque
from datetime import datetime, timedelta, timezone

from . import core_client, salesforce_client, zendesk_client

LIFE_EVENTS = {"qle_newborn": "Newborn", "qle_adoption": "Adoption", "qle_marriage": "Marriage",
               "qle_loss_of_coverage": "Loss of other coverage"}
RESULTS = {"ai_intake_complete": "Checks passed", "ai_intake_incomplete": "Incomplete",
           "ai_fail_no_plan": "Failed: no active plan", "ai_fail_identity": "Failed: identity not confirmed",
           "ai_fail_window": "Failed: outside enrollment window"}
FIN_TAGS = {"involved": "fin-involved", "resolved": "fin-resolved", "handed_off": "fin-routed-to-team"}
CACHE_S = 30


# ---- Monitoring: counted from this service's own log records ------------------------------------------

class OpsLogHandler(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.INFO)
        self.started = datetime.now(timezone.utc)
        self.requests = Counter()           # by status class: 2xx, 4xx, 5xx
        self.latency_ms = deque(maxlen=500)  # recent API requests
        self.events = Counter()             # webhook and approval outcomes
        self.errors = deque(maxlen=10)

    def emit(self, record: logging.LogRecord) -> None:
        f = getattr(record, "extra_fields", {})
        msg = record.getMessage()
        if msg == "request":
            if f.get("path", "").startswith(("/healthz", "/ops", "/v1/ops")):
                return  # health checks and the dashboard itself are not traffic
            self.requests[f"{str(f.get('status', 0))[0]}xx"] += 1
            self.latency_ms.append(f.get("ms", 0.0))
        elif msg == "webhook processed":
            self.events["checks: " + str(f.get("outcome", "")).split(":")[0]] += 1
        elif msg in ("webhook failed", "salesforce activity", "salesforce activity failed",
                     "ticket update failed after enrollment", "checks unavailable"):
            self.events[msg] += 1
        if record.levelno >= logging.ERROR:
            self.errors.appendleft({"at": datetime.now(timezone.utc).strftime("%H:%M:%S"), "msg": msg,
                                    "request_id": f.get("request_id", ""),
                                    "detail": str(f.get("error", ""))[:160]})

    def snapshot(self) -> dict:
        lat = sorted(self.latency_ms)
        total = sum(self.requests.values())
        return {"since": self.started.strftime("%Y-%m-%d %H:%M"), "requests": total,
                "by_status": dict(self.requests),
                "error_rate": round(self.requests["5xx"] / total, 4) if total else 0.0,
                "p50_ms": lat[len(lat) // 2] if lat else None,
                "p95_ms": lat[min(len(lat) - 1, int(len(lat) * 0.95))] if lat else None,
                "events": dict(self.events), "recent_errors": list(self.errors),
                "dependencies": {b.name: b.state for b in (core_client.breaker, zendesk_client.breaker,
                                                           salesforce_client.breaker)}}


monitor = OpsLogHandler()


# ---- Reporting: Zendesk tickets joined with core enrollments --------------------------------------------

def _field(ticket: dict, fid: int | None):
    return next((f["value"] for f in ticket.get("custom_fields", []) if f["id"] == fid), None)


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def human(td: timedelta | None) -> str:
    if td is None:
        return "–"
    minutes = int(td.total_seconds() // 60)
    if minutes < 60:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return f"{hours} h {minutes:02d} min"
    days, hours = divmod(hours, 24)
    return f"{days} d {hours} h"


def build_report(tickets: list[dict], enrollments: list[dict], fields: dict[str, int], now: datetime,
                 days: int = 30) -> dict:
    since = now - timedelta(days=days)
    approved_at = {e["source_ref"].removeprefix("zendesk-ticket:"): _ts(e["approved_at"])
                   for e in enrollments if str(e.get("source_ref", "")).startswith("zendesk-ticket:")}
    recent = [t for t in tickets if _ts(t["created_at"]) >= since
              and (t.get("via") or {}).get("channel") != "sample_ticket"]

    fin = {k: sum(1 for t in recent if tag in (t.get("tags") or [])) for k, tag in FIN_TAGS.items()}
    rows, waits, results = [], [], Counter()
    for t in recent:
        life_event = _field(t, fields.get("life_event_type"))
        if not life_event:
            continue
        result = _field(t, fields.get("ai_intake_result"))
        results[RESULTS.get(result, "Not checked yet")] += 1
        approved = approved_at.get(str(t["id"]))
        wait = approved - _ts(t["created_at"]) if approved else None
        if wait is not None:
            waits.append(wait)
        due = _ts(t["due_at"]) if t.get("due_at") else None
        open_ = t.get("status") not in ("solved", "closed")
        at_risk = bool(due and open_ and not approved and due - now < timedelta(days=3))
        rows.append({"id": t["id"], "life_event": LIFE_EVENTS.get(life_event, life_event),
                     "channel": (t.get("via") or {}).get("channel", ""), "status": t.get("status"),
                     "result": RESULTS.get(result, "Not checked yet"), "fin": FIN_TAGS["involved"] in (t.get("tags") or []),
                     "due_in": human(due - now) if due and due > now else ("overdue" if due else "–"),
                     "enrollment_id": _field(t, fields.get("enrollment_id")) or "",
                     "time_to_approval": human(wait), "at_risk": at_risk})

    median = timedelta(seconds=statistics.median(w.total_seconds() for w in waits)) if waits else None
    checked = sum(v for k, v in results.items() if k != "Not checked yet")
    return {"days": days, "tickets": len(recent), "fin": fin,
            "fin_resolution_rate": round(fin["resolved"] / fin["involved"], 3) if fin["involved"] else None,
            "life_events": len(rows), "results": dict(results),
            "checks_pass_rate": round(results["Checks passed"] / checked, 3) if checked else None,
            "approved": len(waits), "median_time_to_approval": human(median),
            "at_risk": sum(1 for r in rows if r["at_risk"]), "rows": rows}


class Reporter:
    def __init__(self):
        self._cache: tuple[float, dict] | None = None

    async def report(self, zd, core, request_id: str) -> dict:
        if self._cache and time.monotonic() - self._cache[0] < CACHE_S:
            return self._cache[1]
        now = datetime.now(timezone.utc)
        try:
            tickets = await zd.recent_tickets()
        except Exception as exc:  # noqa: BLE001 - the dashboard shows what it can
            return {"error": f"Zendesk unavailable ({type(exc).__name__})"}
        try:
            enrollments = await core.enrollments(request_id)
        except Exception:  # noqa: BLE001 - time to approval is then unknown, the rest still shows
            enrollments = []
        data = build_report(tickets, enrollments, zd.fields, now)
        data["generated_at"] = now.strftime("%Y-%m-%d %H:%M")
        self._cache = (time.monotonic(), data)
        return data


reporter = Reporter()


# ---- The page -------------------------------------------------------------------------------------------

def _pct(x: float | None) -> str:
    return "–" if x is None else f"{round(x * 100)}%"


def render(report: dict, mon: dict) -> str:
    e = html.escape
    if "error" in report:
        reporting = f'<p class="warn">{e(report["error"])}. Monitoring below is still live.</p>'
    else:
        fin = report["fin"]
        cards = [
            (report["tickets"], f"Tickets, last {report['days']} days"),
            (_pct(report["fin_resolution_rate"]), f"Resolved by Fin without a person ({fin['resolved']} of {fin['involved']})"),
            (report["life_events"], "Life-event tickets"),
            (_pct(report["checks_pass_rate"]), "Passed the automatic checks"),
            (report["approved"], "Enrollments approved"),
            (report["median_time_to_approval"], "Median time from ticket to approval"),
            (report["at_risk"], "At risk: deadline in under 3 days, not approved"),
        ]
        card_html = "".join(f'<div class="card{" bad" if "At risk" in l and n else ""}"><div class="n">{e(str(n))}</div>'
                            f'<div class="l">{e(l)}</div></div>' for n, l in cards)
        rows = "".join(
            f'<tr class="{"risk" if r["at_risk"] else ""}"><td>#{r["id"]}</td><td>{e(r["life_event"])}</td>'
            f'<td>{e(r["channel"])}</td><td>{"Yes" if r["fin"] else "No"}</td><td>{e(r["result"])}</td>'
            f'<td>{e(r["due_in"])}</td><td>{e(r["status"] or "")}</td><td class="mono">{e(r["enrollment_id"])}</td>'
            f'<td>{e(r["time_to_approval"])}</td></tr>' for r in report["rows"]) or \
            '<tr><td colspan="9" class="empty">No life-event tickets yet</td></tr>'
        breakdown = " · ".join(f"{e(k)}: {v}" for k, v in sorted(report["results"].items())) or "–"
        reporting = (f'<div class="cards">{card_html}</div>'
                     f'<p class="sub">Check results: {breakdown}</p>'
                     '<table><tr><th>Ticket</th><th>Life event</th><th>Channel</th><th>Fin</th><th>Checks</th>'
                     '<th>Deadline in</th><th>Status</th><th>Enrollment</th><th>Ticket to approval</th></tr>'
                     f'{rows}</table><p class="foot">From Zendesk and the core platform, {e(report["generated_at"])} UTC. '
                     'Refreshed every 30 seconds.</p>')

    st = mon["by_status"]
    deps = "".join(f'<span class="dep {"ok" if s == "closed" else "bad"}">{e(n)}: '
                   f'{"healthy" if s == "closed" else e(s.replace("_", " "))}</span>' for n, s in mon["dependencies"].items())
    events = "".join(f"<li>{e(k)}: <b>{v}</b></li>" for k, v in sorted(mon["events"].items())) or "<li>No webhooks or approvals yet</li>"
    errors = "".join(f'<tr><td class="mono">{e(x["at"])}</td><td>{e(x["msg"])}</td><td class="mono">{e(x["request_id"][:12])}</td>'
                     f'<td class="mono">{e(x["detail"])}</td></tr>' for x in mon["recent_errors"]) or \
        '<tr><td colspan="4" class="empty">No errors</td></tr>'
    p95 = "–" if mon["p95_ms"] is None else f'{mon["p95_ms"]:.0f} ms'
    monitoring = (
        '<div class="cards">'
        f'<div class="card"><div class="n">{mon["requests"]}</div><div class="l">API requests ({st.get("2xx", 0)} ok, {st.get("4xx", 0)} refused)</div></div>'
        f'<div class="card{" bad" if mon["error_rate"] > 0.02 else ""}"><div class="n">{_pct(mon["error_rate"])}</div><div class="l">Server errors (alert above 2%)</div></div>'
        f'<div class="card"><div class="n">{p95}</div><div class="l">p95 latency</div></div>'
        f'<div class="card"><div class="n">{len(mon["recent_errors"])}</div><div class="l">Recent errors</div></div></div>'
        f'<p class="deps">{deps}</p><ul class="events">{events}</ul>'
        '<table><tr><th>When (UTC)</th><th>Error</th><th>Request ID</th><th>Detail</th></tr>' + errors + '</table>'
        f'<p class="foot">This service since its last deploy ({e(mon["since"])} UTC). In production these metrics go to Datadog with alerts.</p>')

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="30"><title>Support Ops dashboard · Northwind PEO</title><style>
:root{{--bg:#f6f7f9;--panel:#fff;--ink:#1d2433;--muted:#5d6779;--line:#e3e6ec;--brand:#24407a;--ok:#1f7a45;--ok-bg:#e7f5ec;--bad:#a1281f;--bad-bg:#fdecea}}
*{{box-sizing:border-box}}body{{margin:0;font:14px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;background:var(--bg);color:var(--ink)}}
header{{background:var(--brand);color:#fff;padding:14px 24px;font-weight:650}}header span{{font-weight:400;opacity:.8;font-size:12px;margin-left:10px}}
.demo{{background:#fffbe6;border-bottom:1px solid #f0e2a8;color:#6b5600;font-size:12.5px;padding:6px 24px}}
main{{max-width:1180px;margin:0 auto;padding:16px 24px 40px}}h2{{font-size:16px;margin:24px 0 4px}}.sub{{color:var(--muted);margin:8px 0}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:12px 0}}
.card{{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px 14px}}.card .n{{font-size:24px;font-weight:650}}
.card .l{{color:var(--muted);font-size:12px}}.card.bad{{border-color:#f1b8b2;background:var(--bad-bg)}}.card.bad .n{{color:var(--bad)}}
table{{width:100%;border-collapse:collapse;background:var(--panel);border:1px solid var(--line);border-radius:10px;overflow:hidden}}
th,td{{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line)}}th{{background:#fafbfc;color:var(--muted);font-size:12px}}
tr.risk td{{background:var(--bad-bg)}}.mono{{font-family:ui-monospace,Menlo,monospace;font-size:12.5px}}.empty{{color:var(--muted);text-align:center}}
.foot{{color:var(--muted);font-size:12px}}.warn{{background:var(--bad-bg);color:var(--bad);padding:10px;border-radius:8px}}
.dep{{display:inline-block;margin-right:8px;padding:2px 10px;border-radius:999px;font-size:12.5px;font-weight:600}}.dep.ok{{background:var(--ok-bg);color:var(--ok)}}.dep.bad{{background:var(--bad-bg);color:var(--bad)}}
.events{{columns:2;color:var(--ink);padding-left:18px}}@media(max-width:700px){{.events{{columns:1}}table{{font-size:12px}}}}
</style></head><body><header>Northwind PEO · Support Ops dashboard<span>Owned by Support Ops</span></header>
<div class="demo">Demo system. Northwind PEO and every person and company here are fictional. Read-only; in production this sits behind single sign-on.</div>
<main><h2>Reporting: benefits life events</h2>{reporting}<h2>Monitoring: integration service</h2>{monitoring}</main></body></html>"""
