"""Northwind PEO core platform: identity, benefits and billing (Engineering-owned, in this demo)."""
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles

from .config import get_settings
from .db import Base, SessionLocal, engine
from .routers import admin, benefits, billing, identity
from .seed import apply_demo_email_overrides, seed, seed_additions


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {"level": record.levelname, "logger": record.name, "msg": record.getMessage()}
        payload.update(getattr(record, "extra_fields", {}))
        return json.dumps(payload)


def _configure_logging() -> logging.Logger:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    log = logging.getLogger("core")
    log.handlers[:] = [handler]
    log.setLevel(get_settings().log_level)
    log.propagate = False
    return log


log = _configure_logging()


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(engine)
    if get_settings().seed_on_start:
        with SessionLocal() as db:
            if seed(db):
                log.info("seeded sample data")
            if added := seed_additions(db):
                log.info("added sample people", extra={"extra_fields": {"added": added}})
    s = get_settings()
    overrides = json.loads(s.demo_email_overrides or "{}")
    if overrides:
        with SessionLocal() as db:
            log.info("demo email overrides", extra={"extra_fields": {
                "people": sorted(overrides), "changed": apply_demo_email_overrides(db, overrides)}})
    phones = json.loads(s.demo_phone_overrides or "{}")
    if phones:
        with SessionLocal() as db:
            log.info("demo phone overrides", extra={"extra_fields": {
                "people": sorted(phones), "changed": apply_demo_email_overrides(db, phones, field="phone")}})
    # Configuration status at startup, never the values themselves
    log.info("startup config", extra={"extra_fields": {
        "database": s.sqlalchemy_url.split(":", 1)[0], "caller_keys": len(s.keys()),
        "admin_public_demo": s.admin_public_demo}})
    yield


app = FastAPI(title="Northwind PEO Core Platform", version="1.0.0", lifespan=lifespan,
              description="Internal system of record. Fictional company, sample data.")
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")


@app.middleware("http")
async def correlation(request: Request, call_next):
    """Every request carries an ID that is logged here and returned to the caller."""
    rid = request.headers.get("x-request-id") or uuid.uuid4().hex
    request.state.request_id = rid
    start = time.perf_counter()
    response = await call_next(request)
    response.headers["X-Request-Id"] = rid
    if not request.url.path.startswith("/static"):
        log.info("request", extra={"extra_fields": {
            "request_id": rid, "method": request.method, "path": request.url.path,
            "status": response.status_code, "ms": round((time.perf_counter() - start) * 1000, 1)}})
    return response


@app.get("/healthz", tags=["health"])
def healthz():
    return {"status": "ok"}


app.include_router(identity.router)
app.include_router(benefits.router)
app.include_router(billing.router)
app.include_router(admin.router)
