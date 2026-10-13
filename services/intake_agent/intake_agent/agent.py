"""The email intake agent: one decision per new customer message.

The model classifies and extracts. Everything else is code: the thresholds, the questions, the checks
(through the integration service), what is written to Zendesk, and every reply to the customer.
State lives on the ticket as tags, so the agent itself keeps nothing between messages.
"""
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


@dataclass
class Outcome:
    action: str
    detail: str = ""
    intent: str = ""
    event_type: str | None = None
    confidence: float = 0.0
    band: str = ""
    model: str = ""


def _conversation(comments: list[dict], requester_id: int) -> tuple[str, str]:
    """Public messages only, oldest first. Returns (raw, redacted)."""
    parts = []
    for c in comments:
        if not c.get("public", True):
            continue
        who = "Customer" if c.get("author_id") == requester_id else "Northwind"
        parts.append(f"{who}: {c.get('plain_body') or c.get('body', '')}".strip())
    raw = "\n---\n".join(parts)
    return raw, redact(raw)[0]


async def process(ticket_id: int, zd: Zendesk, integ: Integration, extractor: Extractor, today: date,
                  request_id: str) -> Outcome:
    s = get_settings()
    ticket = await zd.ticket(ticket_id)
    tags = set(ticket.get("tags", []))
    if DONE in tags or SKIPPED in tags or zd.field(ticket, "ai_intake_result"):
        return Outcome("ignored", "intake already finished")

    requester = await zd.user(ticket["requester_id"])
    raw, safe = _conversation(await zd.comments(ticket_id), ticket["requester_id"])
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
    if note:
        lines.append(note)
    deadline = result.get("deadline") if result else None
    kwargs = {"ticket_type": "task", "due_at": deadline} if deadline else {}
    await zd.update(ticket_id, fields=fields, comment="\n".join(lines), **kwargs)
    complete = ai_result == "ai_intake_complete"
    await zd.update(ticket_id, comment=replies.handed_off(event_type, complete, deadline), public=True)
    await zd.add_tags(ticket_id, [DONE])
    await zd.remove_tags(ticket_id, WAITING)
    return Outcome("handed_off", ai_result)
