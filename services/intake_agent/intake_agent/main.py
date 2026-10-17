"""Email intake agent service. Zendesk calls it when a customer writes; it answers at once and works after."""
import asyncio
import base64
import hashlib
import hmac
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse

from . import agent, console, voice
from .clients import Integration, Zendesk
from .config import get_settings
from .llm import FallbackExtractor, GeminiExtractor, RuleExtractor


class JsonFormatter(logging.Formatter):
    def format(self, record):
        payload = {"level": record.levelname, "logger": record.name, "msg": record.getMessage()}
        payload.update(getattr(record, "extra_fields", {}))
        return json.dumps(payload)


log = logging.getLogger("intake_agent")
_h = logging.StreamHandler()
_h.setFormatter(JsonFormatter())
log.handlers[:] = [_h]
log.setLevel(get_settings().log_level)
log.propagate = False


def build_extractor():
    s = get_settings()
    primary = GeminiExtractor(s.gemini_api_key, s.model, s.llm_timeout_s) if s.gemini_api_key else None
    return FallbackExtractor(primary, RuleExtractor())


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.zd, app.state.integ, app.state.extractor = Zendesk(), Integration(), build_extractor()
    s = get_settings()
    # Configuration status at startup, never the values themselves
    log.info("startup config", extra={"extra_fields": {
        "extractor": app.state.extractor.name, "model": s.model, "zendesk_auth": app.state.zd.auth.mode,
        "integration_key_set": bool(s.integration_api_key), "webhook_secret_source": "zendesk_api" if
        s.zendesk_webhook_id else ("variable" if s.zendesk_webhook_secret else "missing"),
        "gemini_key": "missing" if not s.gemini_api_key else ("set" if s.gemini_api_key.strip() == s.gemini_api_key
                                                               else "set, with surrounding spaces")}})
    if app.state.extractor.primary is not None:
        asyncio.create_task(probe_model(app.state.extractor.primary))
    yield


async def probe_model(model) -> None:
    """One tiny call at startup, so a bad key or model name shows in the logs before the first customer."""
    try:
        await model.extract("Customer: Hello, I have a question about my invoice.", date.today())
        log.info("model probe", extra={"extra_fields": {"result": "ok", "model": get_settings().model}})
    except Exception as exc:  # noqa: BLE001
        log.error("model probe", extra={"extra_fields": {"result": "failed", "error": type(exc).__name__,
                                                         "detail": str(exc)[:300]}})


app = FastAPI(title="Northwind PEO email intake agent", version="1.0.0", lifespan=lifespan)


def verify(body: bytes, sig: str | None, ts: str | None, secret: str, max_age_s: int = 300) -> bool:
    if not (sig and ts and secret):
        return False
    try:
        age = abs(time.time() - datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return False
    digest = base64.b64encode(hmac.new(secret.encode(), ts.encode() + body, hashlib.sha256).digest()).decode()
    return age <= max_age_s and hmac.compare_digest(digest, sig)


@app.get("/healthz")
def healthz():
    s = get_settings()
    return {"status": "ok", "extractor": app.state.extractor.name, "zendesk_auth": app.state.zd.auth.mode,
            "integration_key_set": bool(s.integration_api_key)}


@app.get("/console", response_class=HTMLResponse)
def agent_console():
    """What the agent decided, with intent and confidence. Read-only; no customer text is shown."""
    s = get_settings()
    model = s.model if app.state.extractor.primary is not None else "rules only"
    return console.render(s, model)


@app.get("/v1/decisions")
def decisions():
    return console.summary()


@app.post("/v1/webhooks/zendesk", status_code=202)
async def webhook(request: Request, background: BackgroundTasks):
    raw = await request.body()
    sig = request.headers.get("x-zendesk-webhook-signature")
    ts = request.headers.get("x-zendesk-webhook-signature-timestamp")
    zd = request.app.state.zd
    try:
        valid = verify(raw, sig, ts, await zd.webhook_secret()) or \
            verify(raw, sig, ts, await zd.webhook_secret(refresh=True))
    except Exception as exc:  # noqa: BLE001 - Zendesk retries a 503; a crash would end the delivery
        log.error("signing secret unavailable", extra={"extra_fields": {"error": repr(exc)[:300]}})
        raise HTTPException(503, "signing secret unavailable")
    if not valid:
        raise HTTPException(401, "invalid signature")
    try:
        ticket_id = int(json.loads(raw)["ticket_id"])
    except (ValueError, KeyError, TypeError):
        raise HTTPException(400, "ticket_id is required")
    rid = request.headers.get("x-request-id") or uuid.uuid4().hex

    async def run():
        today = datetime.now(ZoneInfo("America/New_York")).date()
        start = time.perf_counter()
        try:
            out = await agent.process(ticket_id, request.app.state.zd, request.app.state.integ,
                                      request.app.state.extractor, today, rid)
            console.record(ticket_id, out, (time.perf_counter() - start) * 1000, rid)
            log.info("intake step", extra={"extra_fields": {"request_id": rid, "ticket_id": ticket_id,
                                                            "action": out.action, "detail": out.detail,
                                                            "intent": out.intent, "confidence": out.confidence,
                                                            "band": out.band, "model": out.model,
                                                            "channel": out.channel, "tier": out.tier}})
        except Exception as exc:  # noqa: BLE001
            log.error("intake failed", extra={"extra_fields": {"request_id": rid, "ticket_id": ticket_id,
                                                               "error": repr(exc)}})

    background.add_task(run)
    return {"accepted": True, "ticket_id": ticket_id, "request_id": rid}


def _vapi_ok(request: Request) -> bool:
    secret = get_settings().vapi_secret
    got = request.headers.get("x-vapi-secret") or ""
    return bool(secret) and len(secret) >= 24 and hmac.compare_digest(got, secret)


def _args(tc: dict) -> dict:
    a = (tc.get("function") or {}).get("arguments") or tc.get("arguments") or {}
    return json.loads(a) if isinstance(a, str) else a


@app.post("/v1/voice/vapi")
async def vapi(request: Request, background: BackgroundTasks):
    """Live phone calls (Vapi server messages): tool calls during the call, and the end-of-call report."""
    if not _vapi_ok(request):
        raise HTTPException(401, "invalid secret")
    msg = (await request.json()).get("message") or {}
    call_info = msg.get("call") or {}
    call_id = call_info.get("id") or "unknown"
    phone = (call_info.get("customer") or {}).get("number") or ""
    rid = request.headers.get("x-request-id") or uuid.uuid4().hex
    integ = request.app.state.integ
    if msg.get("type") == "tool-calls":
        call = voice._call(call_id, phone)
        today = datetime.now(ZoneInfo("America/New_York")).date()
        results = []
        for tc in msg.get("toolCallList") or msg.get("toolCalls") or []:
            name = (tc.get("function") or {}).get("name") or tc.get("name")
            a = _args(tc)
            if name == "lookup_caller":
                out = await voice.lookup_caller(integ, call, rid)
            elif name == "answer_benefits_question":
                out = await voice.answer_benefits_question(integ, call, rid)
            elif name == "check_life_event":
                out = await voice.check_life_event(integ, call, a.get("event_type", ""), a.get("event_date"), today, rid)
            elif name == "report_coverage_problem":
                out = voice.report_coverage_problem(call, a.get("description", ""))
            else:
                out = {"say": "Sorry, I can't help with that on this call. A specialist will follow up."}
            log.info("voice tool", extra={"extra_fields": {"request_id": rid, "call_id": call_id, "tool": name,
                                                           "tier": call.tier}})
            results.append({"toolCallId": tc.get("id"), "result": json.dumps(out)})
        return {"results": results}
    if msg.get("type") == "end-of-call-report":
        async def make():
            try:
                tid, tier, intent = await voice.create_ticket(request.app.state.zd, call_id, msg)
                console.record(tid, agent.Outcome("live_call", "phone", intent=intent, band="high", confidence=1.0,
                                                  model="voice", tier=tier, channel="voice"), 0, rid)
            except Exception as exc:  # noqa: BLE001
                log.error("voice ticket failed", extra={"extra_fields": {"call_id": call_id, "error": repr(exc)[:300]}})
        background.add_task(make)
        return {"ok": True}
    return {"ok": True}


# ---- Chat demo: a Northwind employee portal page with the Zendesk messaging widget ----
PORTAL_USER = {"external_id": "P-1008", "first_name": "Aisha", "last_name": "Rahman", "name": "Aisha Rahman",
               "email": "aisha.rahman@bluebird.example", "company": "Bluebird Design Studio",
               "plan": "Northwind Silver HMO", "carrier": "Meridian Care", "level": "Employee only"}


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def messaging_jwt(user: dict, key_id: str, secret: str, ttl_s: int = 600) -> str:
    """Zendesk messaging authentication: HS256, the signing key's ID as kid, scope user."""
    header = {"alg": "HS256", "typ": "JWT", "kid": key_id}
    claims = {"scope": "user", "external_id": user["external_id"], "name": user["name"], "email": user["email"],
              "email_verified": True, "exp": int(time.time()) + ttl_s}
    signing_input = _b64url(json.dumps(header, separators=(",", ":")).encode()) + "." + \
        _b64url(json.dumps(claims, separators=(",", ":")).encode())
    sig = hmac.new(secret.encode(), signing_input.encode(), hashlib.sha256).digest()
    return signing_input + "." + _b64url(sig)


@app.get("/portal", response_class=HTMLResponse, include_in_schema=False)
def portal(request: Request, guest: bool = False):
    s = get_settings()
    if not s.portal_demo:
        raise HTTPException(404)
    page = (Path(__file__).parent / "portal.html").read_text()
    base = str(request.base_url).rstrip("/").replace("http://", "https://")
    demo = {"user": None if guest else PORTAL_USER}
    return page.replace("__DEMO__", json.dumps(demo)).replace("__WIDGET_KEY__", s.widget_key).replace("__BASE__", base)


@app.post("/portal/token", include_in_schema=False)
def portal_token():
    s = get_settings()
    if not (s.portal_demo and s.messaging_key_id and len(s.messaging_secret) >= 16):
        raise HTTPException(404)
    return {"jwt": messaging_jwt(PORTAL_USER, s.messaging_key_id, s.messaging_secret)}
