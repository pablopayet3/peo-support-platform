"""The email intake agent: one decision per new customer message.

The model classifies and extracts. Everything else is code: the thresholds, the questions, the checks
(through the integration service), what is written to Zendesk, and every reply to the customer.
State lives on the ticket as tags, so the agent itself keeps nothing between messages.
"""
import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import date

from . import replies
from .clients import Integration, Zendesk
from .config import get_settings
from .llm import Extractor, valid_event_date
from .redact import redact

log = logging.getLogger("intake_agent")

CONFIRMING, WAIT_DATE, WAIT_COMPANY = "ai_intake_confirming", "ai_intake_waiting_date", "ai_intake_waiting_company"
DONE, SKIPPED = "ai_intake_done", "ai_intake_skipped"
WAITING = [CONFIRMING, WAIT_DATE, WAIT_COMPANY]
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
# The three levels of handling from the case study (Part 1). Each is a tag, shown on the ticket by the
# "AI handling" field: automate the reversible, gate the irreversible, escalate the harmful.
AUTOMATED, HUMAN_GATE, HUMAN_LED = "ai_tier_automated", "ai_tier_human_gate", "ai_tier_human_led"


@dataclass
class Outcome:
    action: str
    detail: str = ""
    intent: str = ""
    event_type: str | None = None
    confidence: float = 0.0
    band: str = ""
    model: str = ""
    tier: str = ""  # automated, human approval or human-led (the case study's three levels)
    channel: str = "email"


class Callback:
    """Voice: the same decisions, but a reply cannot be typed back to a caller.

    Every customer-facing message becomes a callback script in an internal note, nothing is solved
    automatically, and the ticket is tagged for a callback. Everything else passes through unchanged.
    """

    def __init__(self, zd: Zendesk):
        self._zd, self.script = zd, []

    def __getattr__(self, name):
        return getattr(self._zd, name)

    async def update(self, tid: int, *, comment: str | None = None, public: bool = False, status: str | None = None, **kw):
        if public and comment:
            self.script.append(comment)
            comment = None
        if comment or kw:
            await self._zd.update(tid, comment=comment, **kw)

    async def finish(self, tid: int, out: "Outcome") -> None:
        tags = ["ai_channel_voice", CALLBACK]
        if self.script:
            lines = ["Callback script (voicemail: the caller cannot read a reply, so a person calls back and says this):"]
            if out.tier == AUTOMATED:
                lines.append("On email or chat this would have been answered and solved automatically. "
                             "On voice the answer is ready; a person reads it on the callback, then solves the ticket.")
            lines += self.script
            await self._zd.update(tid, comment="\n\n".join(lines))
        await self._zd.add_tags(tid, tags)


VOICE_CHANNELS = {"voice", "voicemail", "phone_call_inbound"}
CALLBACK = "ai_callback_needed"


def channel_of(ticket: dict) -> str:
    """email, voice or messaging: the rules are the same everywhere, only the way the answer travels changes."""
    ch = ((ticket.get("via") or {}).get("channel") or "").lower()
    if ch in VOICE_CHANNELS:
        return "voice"
    if ch in {"native_messaging", "messaging", "chat", "sunshine_conversations_api", "web_widget"}:
        return "messaging"
    return "email"


def _text(c: dict) -> str:
    """A voicemail's words are its transcript; any other message is its body."""
    data = c.get("data") or {}
    if c.get("type") == "VoiceComment" or data.get("transcription_text"):
        return data.get("transcription_text") or ""
    return c.get("plain_body") or c.get("body", "")


CALL_TRANSCRIPT = re.compile(r"^\s*Call transcript:\s*", re.I)
TRANSCRIPT_NOISE = [re.compile(r"\b\d{1,2}:\d{2}\s*(Customer|Agent|Caller)\s*:?\s*", re.I),
                    re.compile(r"If you redact any data from this comment.*$", re.I | re.S)]


def _call_transcript(c: dict) -> str:
    """Zendesk's call transcript note (added a few seconds after a voicemail), as the caller's words."""
    body = c.get("plain_body") or c.get("body") or ""
    if not CALL_TRANSCRIPT.match(body):
        return ""
    text = CALL_TRANSCRIPT.sub("", body)
    for noise in TRANSCRIPT_NOISE:
        text = noise.sub(" ", text)
    return " ".join(text.split())


def _conversation(comments: list[dict], requester_id: int) -> tuple[str, str]:
    """What the customer and Northwind said to each other, oldest first. Returns (raw, redacted)."""
    parts = []
    for c in comments:
        transcript = _call_transcript(c) if not c.get("public", True) else ""
        if transcript:
            parts.append(f"Customer: {transcript}")
            continue
        if not c.get("public", True) and c.get("type") != "VoiceComment":
            continue
        text = _text(c)
        if not text:
            continue
        who = "Customer" if c.get("author_id") == requester_id else "Northwind"
        parts.append(f"{who}: {text}".strip())
    raw = "\n---\n".join(parts)
    return raw, redact(raw)[0]


TRANSCRIPT_WAITS, TRANSCRIPT_WAIT_S = 9, 10   # up to 90 seconds


async def process(ticket_id: int, zd: Zendesk, integ: Integration, extractor: Extractor, today: date,
                  request_id: str) -> Outcome:
    s = get_settings()
    ticket = await zd.ticket(ticket_id)
    tags = set(ticket.get("tags", []))
    if channel_of(ticket) == "messaging":
        # Chats are handled live (chat.py). The ticket appears when the AI hands the chat to a person.
        from . import chat
        if await chat.finish_handoff(zd, ticket_id, await zd.user(ticket["requester_id"])):
            return Outcome("handed_off", "chat handoff note added", channel="messaging")
        return Outcome("ignored", "chat ticket", channel="messaging")
    if DONE in tags or SKIPPED in tags or zd.field(ticket, "ai_intake_result"):
        return Outcome("ignored", "intake already finished")

    channel = channel_of(ticket)
    requester = await zd.user(ticket["requester_id"])
    raw, safe = _conversation(await zd.comments(ticket_id), ticket["requester_id"])
    # Zendesk adds the voicemail transcript a few seconds after it creates the ticket: wait for it
    for _ in range(TRANSCRIPT_WAITS if channel == "voice" else 0):
        if raw:
            break
        await asyncio.sleep(TRANSCRIPT_WAIT_S)
        raw, safe = _conversation(await zd.comments(ticket_id), ticket["requester_id"])
    if channel == "voice" and not raw:
        # The voicemail has no transcript (yet): nothing to read, so a person listens to the recording
        await zd.update(ticket_id, comment="The AI support agent found no voicemail transcript. "
                        "Listen to the recording and call the customer back. Normal routing applies.")
        await zd.add_tags(ticket_id, [SKIPPED, "ai_channel_voice", CALLBACK])
        return Outcome("skipped", "no voicemail transcript")
    if channel == "voice":
        zd = Callback(zd)
    ex = await extractor.extract(safe, today)

    band = "high" if ex.confidence >= s.high_confidence else ("medium" if ex.confidence >= s.medium_confidence else "low")
    first_look = not (tags & {CONFIRMING, WAIT_DATE, WAIT_COMPANY})
    triage = (f"Intent: {ex.intent.replace('_', ' ')}"
              + (f" ({ex.event_type.replace('_', ' ')})" if ex.intent == "life_event" and ex.event_type else "")
              + f" · confidence {ex.confidence:.2f} ({band}) · model: {getattr(extractor, 'last_used', extractor.name)}")
    if first_look:
        # Like a triage model's prediction: every ticket gets its intent and confidence band, whatever happens next
        await zd.add_tags(ticket_id, [f"ai_intent_{ex.intent}", f"ai_confidence_{band}"])

    async def decide() -> Outcome:
        async def skip(why: str) -> Outcome:
            await zd.update(ticket_id, comment=f"The AI support agent did not take this request: {why}. Normal routing applies.\n{triage}")
            await zd.add_tags(ticket_id, [SKIPPED])
            await zd.remove_tags(ticket_id, WAITING)
            return Outcome("skipped", why)

        if ex.prompt_injection:
            return await skip("the message contains instructions aimed at the AI")

        if first_look and ex.confidence >= s.medium_confidence:
            # Harm may already be happening: a person takes over at once, whatever else the message asks
            if ex.coverage_failure:
                return await escalate_coverage_failure(zd, integ, ticket_id, requester, ex, request_id, triage)
            # Money going back out is irreversible: Payments decides, and only for a verified admin
            if ex.intent == "billing" and ex.billing_request == "money_correction":
                return await route_money_correction(zd, integ, ticket_id, requester, ex, request_id, triage)
            # A factual answer that changes nothing: the AI answers on its own, but only when it is sure
            if ex.intent == "benefits_question" and ex.asks_deductible and ex.confidence >= s.high_confidence:
                return await answer_deductible(zd, integ, ticket_id, requester, ex, request_id, triage)

        # Step 1: is this a life-event request? (confidence thresholds, case study section 3)
        if WAIT_DATE in tags or WAIT_COMPANY in tags:
            pass  # already established in an earlier message; this one answers our question
        elif CONFIRMING in tags:
            if not ex.confirms:
                return await skip("the customer did not confirm")
        elif ex.intent != "life_event" or ex.event_type is None or ex.confidence < s.medium_confidence:
            return await skip("not a life-event request" if ex.intent != "life_event" else f"low confidence ({ex.confidence:.2f})")
        elif ex.confidence < s.high_confidence:
            await zd.update(ticket_id, comment=replies.clarify(ex.event_type), public=True)
            await zd.add_tags(ticket_id, [CONFIRMING])
            return Outcome("asked_to_confirm", f"confidence {ex.confidence:.2f}")
        event_type = ex.event_type or "newborn"

        # The date the deadline is counted from
        event_date = valid_event_date(ex.event_date, today)
        if event_date is None:
            if WAIT_DATE in tags:
                return await handoff(zd, ticket_id, event_type, None, None, ex, request_id, triage,
                                     note="The customer did not give a usable date. A specialist should ask for it.")
            await zd.update(ticket_id, comment=replies.ask_date(event_type), public=True)
            await zd.add_tags(ticket_id, [WAIT_DATE])
            return Outcome("asked_for_date")

        # Steps 2 to 4: one call to the integration service
        body = {"requester": {"email": requester.get("email")} if requester.get("email") else {"phone": requester.get("phone")},
                "event_type": event_type, "event_date": event_date.isoformat()}
        others = [e for e in EMAIL.findall(raw) if e.lower() != (requester.get("email") or "").lower()]
        if ex.writing_for_someone_else and others:
            body["employee"] = {"email": others[0]}
        try:
            result = await integ.check(body, request_id)
            if result["failed_check"] == "company_unclear":
                match = _pick_company(raw, ex.company_hint, result["company_options"]) if WAIT_COMPANY in tags else None
                if match:
                    result = await integ.check({**body, "company_id": match}, request_id)
                elif WAIT_COMPANY not in tags:
                    await zd.update(ticket_id, comment=replies.ask_company(result["company_options"]), public=True)
                    await zd.add_tags(ticket_id, [WAIT_COMPANY])
                    return Outcome("asked_for_company")
        except Integration.Unavailable as exc:
            return await handoff(zd, ticket_id, event_type, event_date, None, ex, request_id, triage,
                                 note=f"The checks could not run ({exc}). A specialist should run them in the sidebar app.")
        return await handoff(zd, ticket_id, event_type, event_date, result, ex, request_id, triage)

    out = await decide()
    if channel == "voice":
        await zd.finish(ticket_id, out)
    out.channel = channel
    out.intent, out.event_type, out.confidence, out.band = ex.intent, ex.event_type, ex.confidence, band
    out.model = getattr(extractor, "last_used", extractor.name)
    return out


def _pick_company(raw: str, hint: str | None, options: list[dict]) -> str | None:
    latest = raw.split("\n---\n")[-1].lower()
    for o in options:
        name = o["company_name"].lower()
        if name in latest or (hint and hint.lower() in name) or name.split()[0] in latest:
            return o["company_id"]
    return None


async def handoff(zd: Zendesk, ticket_id: int, event_type: str, event_date: date | None, result: dict | None,
                  ex, request_id: str, triage: str = "", note: str = "") -> Outcome:
    """One update with every value, so Zendesk's triggers see them together (case study, section 6)."""
    ai_result = result["ai_intake_result"] if result else "ai_intake_incomplete"
    fields = {"life_event_type": f"qle_{event_type}", "ai_intake_result": ai_result}
    if event_date:
        fields["event_date"] = event_date.isoformat()
    lines = [f"AI support agent summary (request {request_id}):", triage, ex.summary or "-"] if triage else \
        [f"AI support agent summary (request {request_id}):", ex.summary or "-"]
    if ex.dependent_first_name:
        lines.append(f"Dependent named in the conversation: {ex.dependent_first_name} {ex.dependent_last_name or ''}".strip())
    if result:
        for c in result["checks"]:
            lines.append(f"- {c['name'].replace('_', ' ')}: {'passed' if c['passed'] else 'FAILED'}, {c['detail']}")
    deadline = result.get("deadline") if result else None
    last_day = result.get("last_day") if result else None
    if last_day:
        lines.append(f"Enrollment deadline: {replies.deadline_text(last_day)}")
    if note:
        lines.append(note)
    kwargs = {"ticket_type": "task", "due_at": deadline} if deadline else {}
    await zd.update(ticket_id, fields=fields, comment="\n".join(lines), **kwargs)
    complete = ai_result == "ai_intake_complete"
    await zd.update(ticket_id, comment=replies.handed_off(event_type, complete, last_day), public=True)
    await zd.add_tags(ticket_id, [DONE, HUMAN_GATE])
    await zd.remove_tags(ticket_id, WAITING)
    return Outcome("handed_off", ai_result, tier=HUMAN_GATE)


def _contact(requester: dict) -> dict:
    return {"email": requester["email"]} if requester.get("email") else {"phone": requester.get("phone")}


async def _context(integ: Integration, requester: dict, topic: str, request_id: str) -> dict | None:
    try:
        return await integ.context({"requester": _contact(requester), "topic": topic}, request_id)
    except Integration.Unavailable:
        return None


def _who(ctx: dict | None) -> list[str]:
    if ctx is None:
        return ["Sender: could not be checked (a system did not answer). Verify by hand."]
    if not ctx["verified"]:
        return ["Sender: NOT verified. " + " ".join(ctx["notes"])]
    lines = [f"Sender: {ctx['name']}, verified against the system of record."]
    for m in ctx["memberships"]:
        line = f"- {m['company_name']}: {m['role']}"
        if "medical" in m:
            med = m["medical"]
            line += f", medical: {med['plan_name']} ({med['status']})" if med else ", medical: no active plan"
        lines.append(line)
    lines += [n for n in ctx["notes"]]
    return lines


async def escalate_coverage_failure(zd: Zendesk, integ: Integration, ticket_id: int, requester: dict, ex,
                                    request_id: str, triage: str) -> Outcome:
    """Tier 3. The AI gathers the facts and steps aside. Trigger 10-ESC routes it to Benefits Escalation, urgent."""
    ctx = await _context(integ, requester, "coverage_failure", request_id)
    lines = [f"AI support agent summary (request {request_id}):", triage,
             "Handling: human-led. Possible coverage failure. The AI did not try to resolve it.",
             ex.summary or "-"] + _who(ctx)
    await zd.update(ticket_id, comment="\n".join(lines))
    await zd.add_tags(ticket_id, ["ai_coverage_failure", HUMAN_LED, DONE])
    await zd.update(ticket_id, comment=replies.escalated(), public=True)
    return Outcome("escalated", "coverage failure", tier=HUMAN_LED)


async def route_money_correction(zd: Zendesk, integ: Integration, ticket_id: int, requester: dict, ex,
                                 request_id: str, triage: str) -> Outcome:
    """Case study, Part 3. Trigger 15-PAY routes it to Payments, urgent, verified or not. No refund until verified."""
    ctx = await _context(integ, requester, "billing", request_id)
    verified_admin = bool(ctx and ctx["verified"] and ctx.get("is_admin"))
    lines = [f"AI support agent summary (request {request_id}):", triage,
             "Handling: a person decides. Request type: money correction.", ex.summary or "-"] + _who(ctx)
    for co in (ctx or {}).get("billing", []):
        for inv in co["overpaid_invoices"]:
            pays = ", ".join(f"{p['payment_id']} {replies._usd(p['amount'])} on {p['paid_on']}" for p in inv["payments"])
            lines.append(f"Billing record: {co['company_name']} invoice {inv['invoice_id']} ({inv['period']}, "
                         f"{replies._usd(inv['amount'])}) was paid {len(inv['payments'])} times: {pays}. "
                         f"Overpaid by {replies._usd(inv['overpaid_by'])}.")
    if not verified_admin:
        lines.append("Do not issue any refund until the sender is verified as an admin of the company. "
                     "A verification request was sent to the customer.")
    await zd.update(ticket_id, comment="\n".join(lines))
    tags = ["ai_request_money_correction", HUMAN_GATE, DONE] + ([] if verified_admin else ["ai_sender_unverified"])
    await zd.add_tags(ticket_id, tags)
    await zd.update(ticket_id, public=True,
                    comment=replies.payments_received() if verified_admin else replies.verify_first(already_started=True))
    return Outcome("routed_to_payments", "verified admin" if verified_admin else "sender not verified", tier=HUMAN_GATE)


async def answer_deductible(zd: Zendesk, integ: Integration, ticket_id: int, requester: dict, ex,
                            request_id: str, triage: str) -> Outcome:
    """Tier 1. Read-only and reversible, so the AI answers and solves the ticket. Any doubt goes to a person."""
    ctx = await _context(integ, requester, "benefits_question", request_id)
    header = [f"AI support agent summary (request {request_id}):", triage]
    if ctx is None:
        await zd.update(ticket_id, comment="\n".join(header + ["The plan could not be looked up. Normal routing applies."]))
        await zd.add_tags(ticket_id, [SKIPPED])
        return Outcome("skipped", "plan lookup unavailable")
    if not ctx["verified"]:
        await zd.update(ticket_id, comment="\n".join(header + ["Handling: automated, but the sender is not verified, "
                                                               "so no plan details were shared."] + _who(ctx)))
        await zd.add_tags(ticket_id, [AUTOMATED, "ai_sender_unverified", DONE])
        await zd.update(ticket_id, comment=replies.verify_first(), public=True)
        return Outcome("asked_to_verify", "sender not verified", tier=AUTOMATED)
    plans = [m for m in ctx["memberships"] if m.get("medical") and m["medical"].get("deductible")]
    if len(plans) != 1:
        why = "no active medical plan" if not plans else "plans with more than one company"
        await zd.update(ticket_id, comment="\n".join(header + [f"Not answered automatically: {why}. Normal routing applies."] + _who(ctx)))
        await zd.add_tags(ticket_id, [SKIPPED])
        return Outcome("skipped", why)
    await zd.update(ticket_id, comment="\n".join(header + ["Handling: fully automated. Answered from the plan documents."] + _who(ctx)))
    await zd.add_tags(ticket_id, [AUTOMATED, "ai_answered", DONE])
    first = (ctx["name"] or "there").split()[0]
    await zd.update(ticket_id, comment=replies.deductible(first, plans[0]["medical"]), public=True, status="solved")
    return Outcome("answered", "deductible", tier=AUTOMATED)
