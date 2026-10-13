"""The agent console: what the intake agent decided, with intent and confidence, like a triage vendor's dashboard.

Decisions are kept in memory (the latest 200, since the last deploy). The durable record is on each Zendesk
ticket: the ai_intent_* and ai_confidence_* tags, and the internal note.
"""
import html
from collections import Counter, deque
from datetime import datetime, timezone

decisions: deque = deque(maxlen=200)

ACTIONS = {"handed_off": "Handed off to Benefits Ops", "asked_to_confirm": "Asked the customer to confirm",
           "asked_for_date": "Asked for the date", "asked_for_company": "Asked which company",
           "skipped": "Left to normal routing", "ignored": "Already handled"}


def record(ticket_id: int, out, ms: float, request_id: str) -> None:
    decisions.appendleft({"at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), "ticket_id": ticket_id,
                          "intent": out.intent, "event_type": out.event_type, "confidence": round(out.confidence, 2),
                          "band": out.band, "action": out.action, "detail": out.detail, "model": out.model,
                          "ms": round(ms), "request_id": request_id})


def summary() -> dict:
    first = [d for d in decisions if d["action"] != "ignored" and d["intent"]]
    return {"decisions": len(first), "by_intent": dict(Counter(d["intent"] for d in first)),
            "by_band": dict(Counter(d["band"] for d in first)),
            "handed_off": sum(1 for d in first if d["action"] == "handed_off"),
            "recent": list(decisions)[:50]}


def render(settings, model_name: str) -> str:
    e = html.escape
    s = summary()
    hi, med = settings.high_confidence, settings.medium_confidence
    bands = (f'<div class="bands"><div class="band high"><b>High · ≥ {hi:.2f}</b><span>The agent acts on its own: '
             f'extracts the details, runs the checks, hands off to Benefits Ops.</span></div>'
             f'<div class="band medium"><b>Medium · {med:.2f} to {hi:.2f}</b><span>The agent asks the customer one '
             f'question to confirm before doing anything.</span></div>'
             f'<div class="band low"><b>Low · &lt; {med:.2f}</b><span>The agent stays out. Normal Zendesk routing '
             f'applies; the intent is still tagged for reporting.</span></div></div>')
    intents = " ".join(f'<span class="chip">{e(k.replace("_", " "))} <b>{v}</b></span>'
                       for k, v in sorted(s["by_intent"].items(), key=lambda kv: -kv[1])) or '<span class="muted">none yet</span>'
    rows = "".join(
        f'<tr><td class="mono">{e(d["at"][11:])}</td><td>#{d["ticket_id"]}</td>'
        f'<td>{e(d["intent"].replace("_", " "))}{(" · " + e(d["event_type"].replace("_", " "))) if d["event_type"] and d["intent"] == "life_event" else ""}</td>'
        f'<td><span class="pill {e(d["band"])}">{d["confidence"]:.2f} {e(d["band"])}</span></td>'
        f'<td>{e(ACTIONS.get(d["action"], d["action"]))}{(" · " + e(d["detail"])) if d["detail"] and d["action"] == "handed_off" else ""}</td>'
        f'<td class="mono">{e(d["model"])}</td><td class="mono">{d["ms"]} ms</td></tr>'
        for d in s["recent"]) or '<tr><td colspan="7" class="muted center">No emails processed since the last deploy</td></tr>'
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="15"><title>Intake agent console · Northwind PEO</title><style>
:root{{--bg:#f6f5fa;--panel:#fff;--ink:#1f1b2e;--muted:#635d78;--line:#e4e0ee;--brand:#4b2a86;--hi:#1f7a45;--hi-bg:#e7f5ec;
--med:#8a5a00;--med-bg:#fff4dc;--lo:#5d6779;--lo-bg:#eef0f4}}
*{{box-sizing:border-box}}body{{margin:0;font:14px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;background:var(--bg);color:var(--ink)}}
header{{background:var(--brand);color:#fff;padding:14px 24px;font-weight:650}}header span{{font-weight:400;opacity:.8;font-size:12px;margin-left:10px}}
.demo{{background:#fffbe6;border-bottom:1px solid #f0e2a8;color:#6b5600;font-size:12.5px;padding:6px 24px}}
main{{max-width:1180px;margin:0 auto;padding:16px 24px 40px}}h2{{font-size:16px;margin:22px 0 8px}}.muted{{color:var(--muted)}}
.facts{{display:flex;flex-wrap:wrap;gap:10px}}.fact{{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:10px 14px}}
.fact b{{display:block;font-size:20px}}.fact span{{color:var(--muted);font-size:12px}}
.bands{{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:10px}}
.band{{border-radius:10px;padding:10px 14px;border:1px solid var(--line);background:var(--panel)}}.band b{{display:block;margin-bottom:2px}}
.band span{{color:var(--muted);font-size:13px}}.band.high{{border-left:5px solid var(--hi)}}.band.medium{{border-left:5px solid var(--med)}}.band.low{{border-left:5px solid var(--lo)}}
.chip{{display:inline-block;background:var(--panel);border:1px solid var(--line);border-radius:999px;padding:2px 10px;margin:2px 4px 2px 0}}
table{{width:100%;border-collapse:collapse;background:var(--panel);border:1px solid var(--line);border-radius:10px;overflow:hidden}}
th,td{{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line)}}th{{background:#faf9fc;color:var(--muted);font-size:12px}}
.mono{{font-family:ui-monospace,Menlo,monospace;font-size:12.5px}}.center{{text-align:center}}
.pill{{display:inline-block;border-radius:999px;padding:1px 9px;font-size:12px;font-weight:600}}
.pill.high{{background:var(--hi-bg);color:var(--hi)}}.pill.medium{{background:var(--med-bg);color:var(--med)}}.pill.low{{background:var(--lo-bg);color:var(--lo)}}
.tbl{{overflow-x:auto}}
</style></head><body><header>Northwind PEO · AI intake agent console<span>The Forethought role · owned by Support Ops</span></header>
<div class="demo">Demo system with fictional data. Decisions since the last deploy; each ticket also keeps its intent and confidence as tags.</div>
<main>
<div class="facts"><div class="fact"><b>{e(model_name)}</b><span>Model (rules take over if it fails)</span></div>
<div class="fact"><b>{s["decisions"]}</b><span>Emails classified</span></div>
<div class="fact"><b>{s["handed_off"]}</b><span>Life events taken to handoff</span></div></div>
<h2>Confidence bands: what the agent may do</h2>{bands}
<h2>Intents seen</h2><div>{intents}</div>
<h2>Latest decisions</h2><div class="tbl"><table><tr><th>Time (UTC)</th><th>Ticket</th><th>Intent</th><th>Confidence</th><th>Action</th><th>Model</th><th>Time taken</th></tr>{rows}</table></div>
<p class="muted">The model only classifies and extracts. Every action (questions, checks, writing to Zendesk) is taken by code, within these bands. The agent can never approve an enrollment.</p>
</main></body></html>"""
