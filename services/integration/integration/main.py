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
from fastapi.responses import JSONResponse
from pydantic import BaseModel, EmailStr, Field, model_validator

from . import workflows
from .checks import CheckRequest, Contact, run_qle_check
from .config import get_settings
from .core_client import Conflict, CoreClient, NotFound
from .resilience import Unavailable
from .security import Caller, require_scope
from .zendesk_client import ZendeskClient, verify_signature


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {"level": record.levelname, "logger": record.name, "msg": record.getMessage()}
        payload.update(getattr(record, "extra_fields", {}))
        return json.dumps(payload)


log = logging.getLogger("integration")
_h = logging.StreamHandler()
_h.setFormatter(JsonFormatter())
log.handlers[:] = [_h]
log.setLevel(get_settings().log_level)
log.propagate = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.core = CoreClient()
    app.state.zendesk = ZendeskClient()
    yield
    await app.state.core.aclose()
    await app.state.zendesk.aclose()


app = FastAPI(title="Northwind PEO Support Integration API", version="1.0.0", lifespan=lifespan,
              description="Owned by Support Ops. Fictional company, sample data.")


def core(request: Request) -> CoreClient:
    return request.app.state.core


def zendesk(request: Request) -> ZendeskClient:
    return request.app.state.zendesk


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
def healthz():
    return {"status": "ok", "checks_enabled": get_settings().checks_enabled}


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


@app.get("/v1/employees/{employee_id}/summary", tags=["Employees"])
async def employee_summary(employee_id: str, request_id: str = Depends(rid), client: CoreClient = Depends(core),
                           caller: Caller = Depends(require_scope("employees:read"))):
    try:
        e = await client.employee(employee_id, request_id)
        cov = await client.coverage(employee_id, request_id)
    except NotFound:
        raise HTTPException(404, "employee not found")
    med = cov["medical"]
    return {"employee_id": e["employee_id"], "name": e["name"], "company_name": e["company_name"], "role": e["role"],
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
async def approve_enrollment(body: ApproveIn, response: Response, request_id: str = Depends(rid),
                             client: CoreClient = Depends(core), zd: ZendeskClient = Depends(zendesk),
                             caller: Caller = Depends(require_scope("enrollments:approve"))):
    try:
        result, created = await workflows.approve(client, zd, body.model_dump(mode="json"), request_id)
    except workflows.NotAllowed as exc:
        raise HTTPException(403, str(exc))
    except Conflict as exc:
        raise HTTPException(409, str(exc))
    if not created:
        response.status_code = 200
    return result


@app.post("/v1/webhooks/zendesk", tags=["Webhooks"], status_code=202)
async def zendesk_webhook(request: Request, background: BackgroundTasks, request_id: str = Depends(rid),
                          client: CoreClient = Depends(core), zd: ZendeskClient = Depends(zendesk)):
    raw = await request.body()
    sig = request.headers.get("x-zendesk-webhook-signature")
    ts = request.headers.get("x-zendesk-webhook-signature-timestamp")
    if not verify_signature(raw, sig, ts, await zd.webhook_secret()) and \
            not verify_signature(raw, sig, ts, await zd.webhook_secret(refresh=True)):
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
