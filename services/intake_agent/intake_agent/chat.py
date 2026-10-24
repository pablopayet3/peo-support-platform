"""Live chat: the AI agent answers first in the Zendesk messaging widget, then hands off to a person.

Zendesk messaging runs on the Conversations API (Sunshine Conversations). The agent is connected to it
as a third-party bot, the same way Ada, Forethought or Decagon connect:

- It is the first responder on the conversation (the switchboard's default integration).
- Each customer message arrives on a webhook; the agent decides with the same rules as email.
- Tier 1 (a deductible question): it answers in the chat, and a solved ticket keeps the record in Zendesk.
- Tier 2 (a life event) and tier 3 (coverage failure): it passes control to Zendesk Agent Workspace with
  tags and ticket fields already filled, so the usual triggers route it, and a specialist continues in the
  same conversation. The agent then adds its internal note (checks, deadline) and the due date.

The model only classifies and extracts. Every reply is a reviewed template, as on email.
"""
import base64
import logging
import time
from dataclasses import dataclass, field
from datetime import date

import httpx

from . import replies
from .clients import Integration, Zendesk
from .config import get_settings
from .llm import Extractor, valid_event_date
from .redact import redact
from .tracing import traced

log = logging.getLogger("intake_agent")

AUTOMATED, HUMAN_GATE, HUMAN_LED = "ai_tier_automated", "ai_tier_human_gate", "ai_tier_human_led"
BOT_NAME = "Northwind assistant"
INTEGRATION_NAME = "Northwind AI agent"


@dataclass
class Conversation:
    messages: list[str] = field(default_factory=list)
    email: str | None = None
    name: str | None = None
    asked: str = ""                 # what the agent is waiting for: "", "confirm", "date"
    handed_off: bool = False
    note: str = ""                  # internal note for the ticket created by the handoff
    due_at: str | None = None
    updated: float = field(default_factory=time.time)


CONVERSATIONS: dict[str, Conversation] = {}
PENDING_NOTES: dict[str, Conversation] = {}   # by email, until the handoff ticket exists


class SunCo:
    """Minimal Conversations API client (basic auth with the API key's ID and secret)."""

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None):
        s = get_settings()
        self.app_id = s.sunco_app_id
        self.configured = bool(s.sunco_app_id and s.sunco_key_id and s.sunco_key_secret)
        token = base64.b64encode(f"{s.sunco_key_id}:{s.sunco_key_secret}".encode()).decode()
        self.c = httpx.AsyncClient(base_url=f"https://{s.zendesk_subdomain}.zendesk.com/sc/v2/apps/{s.sunco_app_id}",
                                   headers={"Authorization": f"Basic {token}"}, timeout=10, transport=transport)
        self.webhook_secret = ""

    async def _ok(self, method: str, path: str, **kw) -> dict:
        r = await self.c.request(method, path, **kw)
        r.raise_for_status()
        return r.json() if r.content else {}

    async def say(self, conversation_id: str, text: str) -> None:
        await self._ok("POST", f"/conversations/{conversation_id}/messages", json={
            "author": {"type": "business", "displayName": BOT_NAME}, "content": {"type": "text", "text": text}})

    async def pass_to_agents(self, conversation_id: str, metadata: dict) -> None:
        await self._ok("POST", f"/conversations/{conversation_id}/passControl",
                       json={"switchboardIntegration": "next", "metadata": metadata})

    async def setup(self, public_url: str) -> dict:
        """Make the agent the first responder: a webhook integration plus the switchboard order. Safe to rerun."""
        target = public_url.rstrip("/") + "/v1/chat/sunco"
        ints = (await self._ok("GET", "/integrations", params={"filter[types]": "custom"}))["integrations"]
        mine = next((i for i in ints if i.get("displayName") == INTEGRATION_NAME), None)
        if mine is None:
            mine = (await self._ok("POST", "/integrations", json={
                "type": "custom", "displayName": INTEGRATION_NAME,
                "webhooks": [{"target": target, "triggers": ["conversation:message"], "includeFullUser": True}]}))["integration"]
        hooks = (await self._ok("GET", f"/integrations/{mine['id']}/webhooks"))["webhooks"]
        hook = next((h for h in hooks if h["target"] == target), None)
        if hook is None:
            hook = (await self._ok("POST", f"/integrations/{mine['id']}/webhooks", json={
                "target": target, "triggers": ["conversation:message"], "includeFullUser": True}))["webhook"]
        self.webhook_secret = hook.get("secret", "")
        sb = (await self._ok("GET", "/switchboards"))["switchboards"][0]
        sis = (await self._ok("GET", f"/switchboards/{sb['id']}/switchboardIntegrations"))["switchboardIntegrations"]
        agents = next(i for i in sis if i.get("integrationType") == "zd:agentWorkspace")
        ours = next((i for i in sis if i.get("integrationId") == mine["id"]), None)
        if ours is None:
            ours = (await self._ok("POST", f"/switchboards/{sb['id']}/switchboardIntegrations", json={
                "name": "northwind-ai-agent", "integrationId": mine["id"], "deliverStandbyEvents": False,
                "nextSwitchboardIntegrationId": agents["id"], "messageHistoryCount": 10}))["switchboardIntegration"]
        if sb.get("defaultSwitchboardIntegrationId") != ours["id"]:
            await self._ok("PATCH", f"/switchboards/{sb['id']}", json={"defaultSwitchboardIntegrationId": ours["id"]})
        return {"integration": mine["id"], "switchboard_integration": ours["id"], "webhook": bool(self.webhook_secret)}


def _who(user: dict) -> tuple[str | None, str | None]:
    """Email and name of a logged-in user (from the portal's signed token). None for a visitor."""
    profile = user.get("profile") or {}
    email = profile.get("email")
    for ident in user.get("identities") or []:
        if ident.get("type") == "email" and ident.get("value"):
            email = email or ident["value"]
    if not user.get("externalId"):
        email = None   # an email typed by an anonymous visitor is not proof of who they are
    name = " ".join(x for x in (profile.get("givenName"), profile.get("surname")) if x) or None
    return (email.lower() if email else None), name


def chat_reply(text: str) -> str:
    """Email wording, adjusted for a chat that is happening now."""
    return (text.replace("confirm by email", "confirm with you here or by email")
            .replace("Just reply yes", "Just say yes").replace("please reply with", "please tell me")
            .replace("reply with the best phone number", "tell me the best phone number")
            .replace("If anything here doesn't look right, just reply and a benefits specialist will help.",
                     "Anything else I can help with?"))


@traced("chat message", keep=("conv_id", "rid", "today"))
async def handle(sunco: SunCo, integ: Integration, extractor: Extractor, conv_id: str, user: dict, text: str,
                 today: date, rid: str) -> str:
    """One customer message. Returns what was done, for the console."""
    s = get_settings()
    c = CONVERSATIONS.setdefault(conv_id, Conversation())
    if c.handed_off:
        return "ignored"
    c.updated = time.time()
    c.email, c.name = _who(user)
    c.messages.append(f"Customer: {text}")
    safe = redact("\n---\n".join(c.messages))[0]
    ex = await extractor.extract(safe, today)
    triage = f"Intent: {ex.intent.replace('_', ' ')} · confidence {ex.confidence:.2f} · channel: chat"

    async def say(t: str) -> None:
        await sunco.say(conv_id, chat_reply(t))
        c.messages.append(f"Northwind: {t}")

    async def hand_off(tags: list[str], fields: dict, note: list[str]) -> None:
        c.handed_off = True
        ids = s.field_ids()
        meta = {"dataCapture.systemField.tags": " ".join(["ai_channel_chat", "ai_intake_done", f"ai_intent_{ex.intent}", *tags])}
        for k, v in fields.items():
            if k in ids:
                meta[f"dataCapture.ticketField.{ids[k]}"] = v
        c.note = "\n".join([f"AI support agent summary (request {rid}):", triage, *note, "",
                            "Chat so far:", *c.messages])
        if c.email:
            PENDING_NOTES[c.email] = c
        await sunco.pass_to_agents(conv_id, meta)

    ctx = None
    if c.email:
        try:
            ctx = await integ.context({"requester": {"email": c.email}, "topic": "benefits_question"}, rid)
        except Integration.Unavailable:
            ctx = None
    verified = bool(ctx and ctx["verified"])
    first = (ctx or {}).get("name") or c.name or "there"
    first = first.split()[0]

    if ex.prompt_injection:
        await say("Let me connect you with a member of our team.")
        await hand_off(["ai_intake_skipped"], {}, ["The message contains instructions aimed at the AI. Not processed."])
        return "skipped"

    # Tier 3: harm may already be happening
    if ex.coverage_failure and ex.confidence >= s.medium_confidence:
        await say(replies.escalated())
        await hand_off(["ai_coverage_failure", HUMAN_LED],
                       {}, ["Handling: human-led. Possible coverage failure. The AI did not try to resolve it.",
                            f"Sender: {(ctx or {}).get('name') or 'not verified'}."])
        return "escalated"

    # Tier 1: a factual answer that changes nothing, only for a logged-in, verified employee
    if ex.intent == "benefits_question" and ex.asks_deductible and ex.confidence >= s.high_confidence:
        if not verified:
            await say("To protect your account, I can only share plan details once you're logged in. Please log in to "
                      "the Northwind portal and open this chat again.")
            return "asked_to_verify"
        plans = [m for m in ctx["memberships"] if (m.get("medical") or {}).get("deductible")]
        if len(plans) == 1:
            await say(replies.deductible(first, plans[0]["medical"]))
            return "answered"

    # Tier 2: a life event. The checks run now; a person approves.
    if c.asked == "confirm" and not ex.confirms:
        await say("No problem. Let me connect you with a member of our team.")
        await hand_off(["ai_intake_skipped"], {}, ["The customer did not confirm the life event."])
        return "skipped"
    if ex.intent == "life_event" and ex.event_type and ex.confidence >= s.medium_confidence or c.asked in ("confirm", "date"):
        event_type = ex.event_type or "newborn"
        if ex.confidence < s.high_confidence and c.asked == "":
            c.asked = "confirm"
            await say(replies.clarify(event_type))
            return "asked_to_confirm"
        d = valid_event_date(ex.event_date, today)
        if d is None:
            c.asked = "date"
            await say(replies.ask_date(event_type))
            return "asked_for_date"
        if not c.email:
            await say("Thanks. To make changes to your benefits, please log in to the Northwind portal and open this "
                      "chat again. A specialist can also help you by email.")
            await hand_off(["ai_sender_unverified", HUMAN_GATE], {"life_event_type": f"qle_{event_type}",
                                                                   "event_date": d.isoformat()},
                           ["Visitor not logged in: identity not verified."])
            return "handed_off"
        try:
            result = await integ.check({"requester": {"email": c.email}, "event_type": event_type,
                                        "event_date": d.isoformat()}, rid)
        except Integration.Unavailable:
            result = None
        lines = ["Handling: a person approves. The checks ran during the chat."]
        if result:
            lines += [f"- {k['name'].replace('_', ' ')}: {'passed' if k['passed'] else 'FAILED'}, {k['detail']}"
                      for k in result["checks"]]
            if result.get("last_day"):
                lines.append(f"Enrollment deadline: {replies.deadline_text(result['last_day'])}")
            c.due_at = result.get("deadline")
        else:
            lines.append("The checks could not run. Run them in the sidebar app.")
        ai_result = result["ai_intake_result"] if result else "ai_intake_incomplete"
        await say(replies.handed_off(event_type, ai_result == "ai_intake_complete", (result or {}).get("last_day")))
        await hand_off([HUMAN_GATE], {"life_event_type": f"qle_{event_type}", "event_date": d.isoformat(),
                                      "ai_intake_result": ai_result}, lines)
        return "handed_off"

    # Anything else: a person, with the intent already tagged
    await say("Thanks. Let me connect you with a member of our team, who will pick this up right here.")
    await hand_off([HUMAN_GATE], {}, ["Not handled by the AI on chat. Normal routing applies."])
    return "handed_off"


async def finish_handoff(zd: Zendesk, ticket_id: int, requester: dict) -> bool:
    """When Zendesk creates the ticket for a handed-off chat, add the agent's note and the due date."""
    email = (requester.get("email") or "").lower()
    c = PENDING_NOTES.pop(email, None)
    if c is None or time.time() - c.updated > 3600:
        return False
    kwargs = {"ticket_type": "task", "due_at": c.due_at} if c.due_at else {}
    await zd.update(ticket_id, comment=c.note, **kwargs)
    return True
