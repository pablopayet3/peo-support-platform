"""Northwind PEO support integration service (Support Ops-owned).

Implements contracts/integration-api.yaml. The AI intake agents and the Zendesk sidebar app call it;
it calls the core platform and Zendesk.
"""
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from datetime import date
from typing import Literal

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.routing import Route
from pydantic import BaseModel, EmailStr, Field, model_validator

from . import context, mcp_server, ops, provisioning, workflows
from .checks import CheckRequest, Contact, run_qle_check
from .config import get_settings
from .core_client import Conflict, CoreClient, NotFound
from .resilience import Unavailable
from .confluence_client import ConfluenceClient
from .security import Caller, require_scope
from .salesforce_client import SalesforceClient
from .zendesk_client import ZendeskClient, verify_signature


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {"level": record.levelname, "logger": record.name, "msg": record.getMessage()}
        payload.update(getattr(record, "extra_fields", {}))
        return json.dumps(payload)


log = logging.getLogger("integration")
_h = logging.StreamHandler()
_h.setFormatter(JsonFormatter())
log.handlers[:] = [_h, ops.monitor]
log.setLevel(get_settings().log_level)
log.propagate = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.core = CoreClient()
    app.state.zendesk = ZendeskClient()
    app.state.salesforce = SalesforceClient()
    app.state.confluence = ConfluenceClient()
    s = get_settings()
    # Configuration status at startup, never the values themselves
    log.info("startup config", extra={"extra_fields": {
        "zendesk_auth": app.state.zendesk.auth.mode, "core_key_set": bool(s.core_api_key),
        "caller_keys": len(s.keys()), "webhook_secret_source": "zendesk_api" if s.zendesk_webhook_id else (
            "variable" if s.zendesk_webhook_secret else "missing"), "field_ids": sorted(s.field_ids()),
        "confluence": app.state.confluence.configured}})
    log.info("sidebar app settings", extra={"extra_fields": {
        "result": await provisioning.sync_sidebar_app(app.state.zendesk)}})
    if app.state.salesforce.configured and s.salesforce_seed_demo:
        try:
            made = await app.state.salesforce.seed_demo()
            log.info("salesforce demo data", extra={"extra_fields": {"accounts_created": made}})
        except Exception as exc:  # noqa: BLE001 - context only; the service starts regardless
            log.error("salesforce demo data", extra={"extra_fields": {"error": repr(exc)[:300]}})
    mcp_server.STATE.update(core=app.state.core, confluence=app.state.confluence)
    mcp_server.build_app()
    async with mcp_server.STATE["server"].session_manager.run():
        yield
    await app.state.salesforce.aclose()
    await app.state.confluence.aclose()
    await app.state.core.aclose()
    await app.state.zendesk.aclose()


app = FastAPI(title="Northwind PEO Support Integration API", version="1.0.0", lifespan=lifespan,
              description="The one API that support tools call. Fictional company, sample data.")
# The same tools over MCP (Model Context Protocol), for any AI agent. Read-only, key with scope mcp:read.
for _path in mcp_server.PATHS:
    app.router.routes.append(Route(_path, endpoint=mcp_server.Endpoint(), methods=["GET", "POST", "DELETE", "OPTIONS"]))


def core(request: Request) -> CoreClient:
    return request.app.state.core


def zendesk(request: Request) -> ZendeskClient:
    return request.app.state.zendesk


def salesforce(request: Request) -> SalesforceClient:
    return request.app.state.salesforce


def rid(request: Request) -> str:
    return request.state.request_id


@app.middleware("http")
async def correlation(request: Request, call_next):
    request.state.request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
    start = time.perf_counter()
    response = await call_next(request)
    response.headers["X-Request-Id"] = request.state.request_id
    log.info("request", extra={"extra_fields": {
        "request_id": request.state.request_id, "method": request.method, "path": request.url.path,
        "status": response.status_code, "ms": round((time.perf_counter() - start) * 1000, 1)}})
    return response


@app.exception_handler(Unavailable)
async def unavailable(request: Request, exc: Unavailable):
    return JSONResponse(status_code=503, content={"error": "unavailable", "detail": str(exc),
                                                  "request_id": request.state.request_id})


@app.get("/healthz", tags=["Health"])
def healthz(request: Request):
    """Liveness plus configuration status. Never returns a secret, only whether it is set."""
    s = get_settings()
    return {"status": "ok", "checks_enabled": s.checks_enabled,
            "zendesk_auth": request.app.state.zendesk.auth.mode,
            "core_key_set": bool(s.core_api_key), "caller_keys": len(s.keys()),
            "salesforce": "configured" if request.app.state.salesforce.configured else "not_configured"}


@app.get("/ops", response_class=HTMLResponse, tags=["Ops"])
async def ops_dashboard(request_id: str = Depends(rid), client: CoreClient = Depends(core),
                        zd: ZendeskClient = Depends(zendesk)):
    """Support Ops dashboard: life-event reporting and this service's health. Aggregates and ticket numbers only."""
    return ops.render(await ops.reporter.report(zd, client, request_id), ops.monitor.snapshot())


@app.get("/v1/ops/report", tags=["Ops"])
async def ops_report(request_id: str = Depends(rid), client: CoreClient = Depends(core),
                     zd: ZendeskClient = Depends(zendesk)):
    return {"reporting": await ops.reporter.report(zd, client, request_id), "monitoring": ops.monitor.snapshot()}


class ContactIn(BaseModel):
    email: EmailStr | None = None
    phone: str | None = Field(default=None, pattern=r"^\+[1-9]\d{6,14}$")

    @model_validator(mode="after")
    def one_of(self):
        if not (self.email or self.phone):
            raise ValueError("email or phone is required")
        return self


class QleCheckIn(BaseModel):
    requester: ContactIn
    employee: ContactIn | None = None
    event_type: Literal["newborn", "adoption", "marriage", "loss_of_coverage"]
    event_date: date
    company_id: str | None = None


@app.post("/v1/qle/check", tags=["Checks"])
async def check_life_event(body: QleCheckIn, request_id: str = Depends(rid), client: CoreClient = Depends(core),
                           caller: Caller = Depends(require_scope("checks:run"))):
    if not get_settings().checks_enabled:
        raise Unavailable("checks are switched off")
    req = CheckRequest(
        requester=Contact(**body.requester.model_dump()),
        employee=Contact(**body.employee.model_dump()) if body.employee else None,
        event_type=body.event_type, event_date=body.event_date, company_id=body.company_id)
    result = await run_qle_check(client, req, request_id)
    log.info("qle check", extra={"extra_fields": {"request_id": request_id, "caller": caller.client,
                                                  "result": result.failed_check, "employee_id": result.employee_id}})
    return result.to_dict()


class RequestContextIn(BaseModel):
    requester: ContactIn
    topic: Literal["benefits_question", "coverage_failure", "billing"]


@app.post("/v1/requests/context", tags=["Checks"])
async def request_context(body: RequestContextIn, request_id: str = Depends(rid), client: CoreClient = Depends(core),
                          caller: Caller = Depends(require_scope("checks:run"))):
    """Who is writing, and the facts for this kind of request. Read-only; changes nothing anywhere."""
    out = await context.request_context(client, Contact(**body.requester.model_dump()), body.topic, request_id)
    log.info("request context", extra={"extra_fields": {"request_id": request_id, "caller": caller.client,
                                                        "topic": body.topic, "verified": out["verified"]}})
    return out


SITUATIONS = {"ai_intake_complete", "ai_intake_incomplete", "ai_fail_identity", "ai_fail_no_plan", "ai_fail_window"}


@app.get("/v1/guidance", tags=["Guidance"])
async def guidance(request: Request, situation: str, event_type: str | None = None, caller: Caller = Depends(require_scope("employees:read"))):
    """What to do now, read live from the Benefits Ops procedure in Confluence."""
    if situation not in SITUATIONS:
        raise HTTPException(400, f"situation must be one of {sorted(SITUATIONS)}")
    return await request.app.state.confluence.guidance(situation, event_type)


@app.get("/v1/employees/{employee_id}/summary", tags=["Employees"])
async def employee_summary(employee_id: str, request_id: str = Depends(rid), client: CoreClient = Depends(core),
                           sf: SalesforceClient = Depends(salesforce),
                           caller: Caller = Depends(require_scope("employees:read"))):
    try:
        e = await client.employee(employee_id, request_id)
        cov = await client.coverage(employee_id, request_id)
    except NotFound:
        raise HTTPException(404, "employee not found")
    med = cov["medical"]
    # The CRM view of the client company. Context only: an outage shows as "unavailable", never as an error.
    account: dict | None = {"status": "not_configured"}
    if sf.configured:
        try:
            ctx = await sf.account_context(e["company_id"])
            account = {"status": "ok", **ctx} if ctx else {"status": "not_found"}
        except Exception as exc:  # noqa: BLE001
            log.warning("salesforce unavailable", extra={"extra_fields": {"request_id": request_id, "error": repr(exc)[:200]}})
            account = {"status": "unavailable"}
    return {"employee_id": e["employee_id"], "name": e["name"], "company_id": e["company_id"],
            "company_name": e["company_name"], "role": e["role"], "account": account,
            "medical_plan": None if med is None else {"name": med["plan_name"], "carrier": med["carrier"],
                                                      "coverage_level": med["coverage_level"]},
            "dependents": [{"name": d["name"], "relationship": d["relationship"], "status": d["status"]}
                           for d in cov["dependents"]],
            "open_life_events": cov.get("open_life_events", [])}


class DependentIn(BaseModel):
    first_name: str = Field(min_length=1, max_length=60)
    last_name: str = Field(min_length=1, max_length=60)
    date_of_birth: date


class ApproveIn(BaseModel):
    ticket_id: int
    employee_id: str
    event_type: Literal["newborn", "adoption", "marriage", "loss_of_coverage"]
    event_date: date
    dependent: DependentIn
    approver_zendesk_user_id: int


@app.post("/v1/enrollments", tags=["Enrollments"], status_code=201)
async def approve_enrollment(body: ApproveIn, response: Response, background: BackgroundTasks,
                             request_id: str = Depends(rid),
                             client: CoreClient = Depends(core), zd: ZendeskClient = Depends(zendesk),
                             sf: SalesforceClient = Depends(salesforce),
                             caller: Caller = Depends(require_scope("enrollments:approve"))):
    try:
        result, created = await workflows.approve(client, zd, body.model_dump(mode="json"), request_id)
    except workflows.NotAllowed as exc:
        raise HTTPException(403, str(exc))
    except Conflict as exc:
        raise HTTPException(409, str(exc))
    if not created:
        response.status_code = 200
    elif sf.configured:
        background.add_task(record_in_salesforce, sf, client, body, result, request_id)
    return result


async def record_in_salesforce(sf: SalesforceClient, client: CoreClient, body: "ApproveIn", result: dict,
                               request_id: str) -> None:
    """Leave a completed activity on the client's account, so Sales and the CSM see what Support did."""
    try:
        e = await client.employee(body.employee_id, request_id)
        ctx = await sf.account_context(e["company_id"])
        if not ctx:
            return
        dep = body.dependent
        task_id = await sf.log_task(
            ctx["account_id"],
            f"Benefits: {body.event_type} enrollment {result.get('enrollment_id')} approved for {e['name']}",
            f"Zendesk ticket #{body.ticket_id}. Dependent {dep.first_name} {dep.last_name}, born "
            f"{dep.date_of_birth.isoformat()}. Enrollment {result.get('enrollment_id')} is pending with the carrier. "
            f"Request {request_id}.")
        log.info("salesforce activity", extra={"extra_fields": {"request_id": request_id, "task_id": task_id}})
    except Exception as exc:  # noqa: BLE001 - the approval already succeeded; this is a record, not a step
        log.error("salesforce activity failed", extra={"extra_fields": {"request_id": request_id,
                                                                         "error": repr(exc)[:300]}})


@app.post("/v1/webhooks/zendesk", tags=["Webhooks"], status_code=202)
async def zendesk_webhook(request: Request, background: BackgroundTasks, request_id: str = Depends(rid),
                          client: CoreClient = Depends(core), zd: ZendeskClient = Depends(zendesk)):
    raw = await request.body()
    sig = request.headers.get("x-zendesk-webhook-signature")
    ts = request.headers.get("x-zendesk-webhook-signature-timestamp")
    try:
        valid = verify_signature(raw, sig, ts, await zd.webhook_secret()) or \
            verify_signature(raw, sig, ts, await zd.webhook_secret(refresh=True))
    except Exception as exc:  # noqa: BLE001 - Zendesk retries a 503; a crash would end the delivery
        log.error("signing secret unavailable", extra={"extra_fields": {"error": repr(exc)[:300]}})
        raise HTTPException(503, "signing secret unavailable")
    if not valid:
        raise HTTPException(401, "invalid signature")
    try:
        ticket_id = int(json.loads(raw)["ticket_id"])
    except (ValueError, KeyError, TypeError):
        raise HTTPException(400, "ticket_id is required")

    async def run():
        try:
            outcome = await workflows.check_ticket(client, zd, ticket_id, request_id)
            log.info("webhook processed", extra={"extra_fields": {"request_id": request_id, "ticket_id": ticket_id,
                                                                  "outcome": outcome}})
        except Exception as exc:  # noqa: BLE001 - logged for the alert; Zendesk already has its 202
            log.error("webhook failed", extra={"extra_fields": {"request_id": request_id, "ticket_id": ticket_id,
                                                                "error": repr(exc)}})

    # Answer at once: Zendesk waits only 12 seconds before retrying. The work happens after the response.
    background.add_task(run)
    return {"accepted": True, "ticket_id": ticket_id, "request_id": request_id}
