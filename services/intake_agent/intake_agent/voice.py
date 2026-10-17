"""Live phone calls: the same rules as email, spoken.

A voice platform (Vapi here; Twilio ConversationRelay, Retell or Amazon Connect would work the same way)
answers the call and handles speech. Whenever it needs a fact or a decision it calls one of the tools
below. The tools use the same integration service and the same three levels of handling as email:

- answer_benefits_question: tier 1, answered on the call, only for a verified caller
- check_life_event: tier 2, the checks run during the call, a person approves afterwards
- report_coverage_problem: tier 3, the caller is transferred to a person at once

Each tool returns `say`: the exact words for the voice to read, from reviewed templates. The model on the
call chooses which tool to use; it never writes the wording about benefits. When the call ends, one
Zendesk ticket is created with the transcript, the tier and the facts, so the usual triggers route it.
"""
import logging
import time
from dataclasses import dataclass, field
from datetime import date

from . import replies
from .clients import Integration, Zendesk
from .llm import valid_event_date

log = logging.getLogger("intake_agent")

AUTOMATED, HUMAN_GATE, HUMAN_LED = "ai_tier_automated", "ai_tier_human_gate", "ai_tier_human_led"
EVENTS = {"newborn", "adoption", "marriage", "loss_of_coverage"}


@dataclass
class Call:
    """What happened on one call, kept until the end-of-call report turns it into a ticket."""
    phone: str
    started: float = field(default_factory=time.time)
    tier: str = ""
    intent: str = "other"
    facts: list[str] = field(default_factory=list)
    fields: dict = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    due_at: str | None = None
    solved: bool = False


CALLS: dict[str, Call] = {}


def _call(call_id: str, phone: str) -> Call:
    # Forget calls older than a day: the report arrives within minutes
    for cid in [c for c, v in CALLS.items() if time.time() - v.started > 86400]:
        CALLS.pop(cid, None)
    return CALLS.setdefault(call_id, Call(phone=phone))


def _first(name: str | None) -> str:
    return (name or "there").split()[0]


async def lookup_caller(integ: Integration, call: Call, rid: str) -> dict:
    """Who is calling, from the phone number. Shares nothing about the plan."""
    try:
        ctx = await integ.context({"requester": {"phone": call.phone}, "topic": "benefits_question"}, rid)
    except Integration.Unavailable:
        return {"verified": False, "say": "I'm sorry, I can't look up your account right now. Let me take your details "
                                          "and someone from our team will call you back."}
    if not ctx["verified"]:
        call.facts.append("Caller: phone number not on file, not verified.")
        return {"verified": False, "say": "I couldn't match this phone number to an account, so I can't share plan "
                                          "details on this call. You can log in to the Northwind portal, or call back "
                                          "from the number on file."}
    m = ctx["memberships"][0]
    call.facts.append(f"Caller: {ctx['name']}, verified by phone number against the system of record "
                      f"({m['company_name']}, {m['role']}).")
    return {"verified": True, "first_name": _first(ctx["name"]), "company": m["company_name"],
            "say": f"Thanks, {_first(ctx['name'])}. I can see your account with {m['company_name']}. How can I help?"}


async def answer_benefits_question(integ: Integration, call: Call, rid: str) -> dict:
    """Tier 1: deductible and out-of-pocket maximum, read-only."""
    call.intent = "benefits_question"
    try:
        ctx = await integ.context({"requester": {"phone": call.phone}, "topic": "benefits_question"}, rid)
    except Integration.Unavailable:
        call.tier = HUMAN_GATE
        call.facts.append("Deductible question: the plan could not be looked up. Call back with the answer.")
        return {"say": "I can't reach your plan details right now. Someone from our team will call you back today."}
    plans = [m for m in ctx["memberships"] if (m.get("medical") or {}).get("deductible")] if ctx["verified"] else []
    if len(plans) != 1:
        call.tier = HUMAN_GATE
        why = "caller not verified" if not ctx["verified"] else ("no active plan" if not plans else "plans with more than one company")
        call.facts.append(f"Deductible question not answered on the call: {why}.")
        return {"say": "I'm not able to give you plan details on this call. A benefits specialist will follow up with you."}
    call.tier, call.solved = AUTOMATED, True
    call.tags += ["ai_answered"]
    med = plans[0]["medical"]
    call.facts.append(f"Answered on the call from the plan documents: {med['plan_name']}.")
    return {"say": replies.deductible_spoken(_first(ctx["name"]), med)}


async def check_life_event(integ: Integration, call: Call, event_type: str, event_date: str | None,
                           today: date, rid: str) -> dict:
    """Tier 2: the checks run during the call; Benefits Ops approves afterwards."""
    call.intent = "life_event"
    if event_type not in EVENTS:
        return {"say": "Is this about a birth, an adoption, a marriage, or losing other coverage?"}
    d = valid_event_date(event_date, today)
    if d is None:
        return {"say": replies.ask_date(event_type).replace("please reply with", "can you tell me")
                .replace(" (for example, October 6, 2026)", "")}
    call.tier = HUMAN_GATE
    call.fields = {"life_event_type": f"qle_{event_type}", "event_date": d.isoformat()}
    try:
        result = await integ.check({"requester": {"phone": call.phone}, "event_type": event_type,
                                    "event_date": d.isoformat()}, rid)
    except Integration.Unavailable:
        call.fields["ai_intake_result"] = "ai_intake_incomplete"
        call.facts.append("The checks could not run during the call. Run them in the sidebar app.")
        return {"say": "Thank you. I've noted the date. A benefits specialist will confirm the next steps with you, "
                       "well before your deadline."}
    call.fields["ai_intake_result"] = result["ai_intake_result"]
    call.due_at = result.get("deadline")
    for c in result["checks"]:
        call.facts.append(f"- {c['name'].replace('_', ' ')}: {'passed' if c['passed'] else 'FAILED'}, {c['detail']}")
    if result.get("last_day"):
        call.facts.append(f"Enrollment deadline: {replies.deadline_text(result['last_day'])}")
    complete = result["ai_intake_result"] == "ai_intake_complete"
    return {"checks_passed": complete,
            "say": replies.handed_off_spoken(event_type, complete, result.get("last_day"))}


def report_coverage_problem(call: Call, description: str) -> dict:
    """Tier 3: harm may already be happening, so a person takes over now, on this call."""
    call.intent, call.tier = "benefits_question", HUMAN_LED
    call.tags += ["ai_coverage_failure"]
    call.facts.append(f"Possible coverage failure, reported on the call: {description[:300]}. "
                      "The AI did not try to resolve it and transferred the call to a person.")
    return {"transfer": True, "say": replies.escalated_spoken()}


def _transcript(report: dict) -> str:
    art = report.get("artifact") or {}
    if art.get("transcript"):
        return art["transcript"]
    lines = []
    for m in art.get("messages") or report.get("messages") or []:
        if m.get("role") in ("user", "bot", "assistant") and m.get("message"):
            lines.append(f"{'Caller' if m['role'] == 'user' else 'AI'}: {m['message']}")
    return "\n".join(lines)


async def create_ticket(zd: Zendesk, call_id: str, report: dict) -> tuple[int, str, str]:
    """One ticket per call, with everything the team needs. The routing triggers do the rest."""
    phone = ((report.get("call") or {}).get("customer") or {}).get("number") or ""
    call = CALLS.pop(call_id, None) or Call(phone=phone)
    summary = ((report.get("analysis") or {}).get("summary") or "").strip()
    transcript = _transcript(report)
    recording = (report.get("artifact") or {}).get("recordingUrl") or report.get("recordingUrl")
    spoke = any(m.get("role") == "user" for m in ((report.get("artifact") or {}).get("messages") or report.get("messages") or []))
    if not call.tier and not spoke and "User:" not in transcript and "Caller:" not in transcript:
        # The caller never said anything: record it for reporting, but nobody needs to act on it
        call.solved = True
        call.tags.append("ai_call_no_request")
    tier = call.tier or HUMAN_GATE
    label = {AUTOMATED: "answered on the call", HUMAN_GATE: "a person follows up",
             HUMAN_LED: "transferred to a person"}[tier]
    note = [f"Live phone call with the AI support agent ({label}).", f"Handling: {tier.removeprefix('ai_tier_').replace('_', ' ')}.",
            summary or "-", *call.facts]
    if recording:
        note.append(f"Recording: {recording}")
    note += ["", "Transcript:", transcript or "(no transcript)"]
    requester_id = await zd.user_by_phone(call.phone) if call.phone else None
    subject = {"life_event": "Phone call: life event", "benefits_question": "Phone call: benefits question"}.get(
        call.intent, "Phone call")
    tid = await zd.create_ticket(
        subject=subject + (f" ({summary[:60]})" if summary else ""), note="\n".join(note), requester_id=requester_id,
        phone=call.phone, fields=call.fields, due_at=call.due_at,
        tags=["ai_channel_voice", "ai_voice_live", tier, f"ai_intent_{call.intent}", "ai_intake_done", *call.tags],
        status="solved" if call.solved else None)
    log.info("voice call ticket", extra={"extra_fields": {"call_id": call_id, "ticket_id": tid, "tier": tier,
                                                          "intent": call.intent}})
    return tid, tier, call.intent
