import hashlib
import json
from datetime import date, timezone
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import audit, plans
from ..db import get_db
from ..models import Coverage, Dependent, Employee, Enrollment, IdempotencyKey, QleRule
from ..rules import DEFAULT_WINDOWS, enrollment_window, today_in
from ..security import Caller, request_id, require_scope

router = APIRouter(prefix="/v1", tags=["benefits"])
EventType = Literal["newborn", "adoption", "marriage", "loss_of_coverage"]


def _active_medical(e: Employee) -> Coverage | None:
    return next((c for c in e.coverages if c.status == "active" and c.plan.kind == "medical"), None)


def _window_days(db: Session, event_type: str) -> int:
    rule = db.get(QleRule, event_type)
    return rule.window_days if rule else DEFAULT_WINDOWS[event_type]


def _employee_or_404(db: Session, employee_id: str) -> Employee:
    e = db.get(Employee, employee_id)
    if e is None:
        raise HTTPException(404, "employee not found")
    return e


@router.get("/employees/{employee_id}/coverage")
def get_coverage(employee_id: str, db: Session = Depends(get_db),
                 caller: Caller = Depends(require_scope("benefits:read"))):
    e = _employee_or_404(db, employee_id)
    c = _active_medical(e)
    return {
        "employee_id": e.id,
        "medical": None if c is None else {"plan_id": c.plan_id, "plan_name": c.plan.name, "carrier": c.plan.carrier,
                                           "coverage_level": c.level, "status": c.status, **plans.details(c.plan_id)},
        "dependents": [{"dependent_id": d.id, "name": d.name, "relationship": d.relationship_to_employee,
                        "status": d.status} for d in e.dependents],
        "open_life_events": [{"enrollment_id": en.id, "event_type": en.event_type, "status": en.status}
                             for en in db.scalars(select(Enrollment).where(
                                 Enrollment.employee_id == e.id, Enrollment.status == "pending_carrier"))],
    }


class QleEvaluate(BaseModel):
    employee_id: str
    event_type: EventType
    event_date: date


@router.post("/qle/evaluate")
def evaluate_qle(body: QleEvaluate, db: Session = Depends(get_db),
                 caller: Caller = Depends(require_scope("benefits:read"))):
    e = _employee_or_404(db, body.employee_id)
    if body.event_date > today_in(e.company.timezone):
        raise HTTPException(400, "event_date is in the future")
    days = _window_days(db, body.event_type)
    w = enrollment_window(body.event_date, days, e.company.timezone)
    return {"window_open": w.is_open, "window_days": days, "last_day": w.last_day.isoformat(),
            "deadline": w.deadline_utc.isoformat().replace("+00:00", "Z")}


class DependentIn(BaseModel):
    first_name: str = Field(min_length=1, max_length=60)
    last_name: str = Field(min_length=1, max_length=60)
    date_of_birth: date


class EnrollmentCreate(BaseModel):
    employee_id: str
    event_type: EventType
    event_date: date
    dependent: DependentIn
    approved_by: str = Field(min_length=3, max_length=80)
    source_ref: str = Field(min_length=3, max_length=80)


def iso_utc(dt):
    """SQLite drops the time zone; every timestamp here is UTC, so say so explicitly."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _enrollment_out(en: Enrollment) -> dict:
    return {"enrollment_id": en.id, "employee_id": en.employee_id, "event_type": en.event_type,
            "event_date": en.event_date.isoformat(), "status": en.status, "approved_by": en.approved_by,
            "approved_at": iso_utc(en.approved_at), "source_ref": en.source_ref,
            "dependent_id": en.dependent_id}


@router.post("/enrollments", status_code=201)
def create_enrollment(body: EnrollmentCreate, response: Response,
                      idempotency_key: str = Header(alias="Idempotency-Key", min_length=8, max_length=128),
                      db: Session = Depends(get_db), caller: Caller = Depends(require_scope("enrollments:write")),
                      rid: str = Depends(request_id)):
    body_hash = hashlib.sha256(json.dumps(body.model_dump(mode="json"), sort_keys=True).encode()).hexdigest()
    seen = db.get(IdempotencyKey, idempotency_key)
    if seen is not None:
        if seen.request_hash != body_hash:
            raise HTTPException(409, "this Idempotency-Key was already used with different data")
        response.status_code = 200
        return _enrollment_out(db.get(Enrollment, int(seen.resource_ref)))

    e = _employee_or_404(db, body.employee_id)
    if e.status != "active":
        raise HTTPException(409, "employee is not active")
    if _active_medical(e) is None:
        raise HTTPException(409, "no active medical plan")
    w = enrollment_window(body.event_date, _window_days(db, body.event_type), e.company.timezone)
    if not w.is_open:
        raise HTTPException(409, "the enrollment window is closed")

    n = len(e.dependents) + 1
    dep = Dependent(id=f"D-{e.id[2:]}-{n:02d}", employee_id=e.id, first_name=body.dependent.first_name,
                    last_name=body.dependent.last_name,
                    relationship_to_employee="spouse" if body.event_type == "marriage" else "child",
                    date_of_birth=body.dependent.date_of_birth, status="pending_enrollment")
    en = Enrollment(employee_id=e.id, dependent_id=dep.id, event_type=body.event_type, event_date=body.event_date,
                    status="pending_carrier", approved_by=body.approved_by, source_ref=body.source_ref)
    db.add_all([dep, en])
    db.flush()
    db.add(IdempotencyKey(key=idempotency_key, client=caller.client, request_hash=body_hash,
                          resource_ref=str(en.pk)))
    audit.record(db, actor=caller.client, action="enrollment.created", entity="enrollment", entity_id=en.id,
                 request_id=rid, employee_id=e.id, approved_by=body.approved_by, source_ref=body.source_ref)
    try:
        db.commit()
    except IntegrityError:
        # Two identical requests raced. The first one won; return its enrollment.
        db.rollback()
        seen = db.get(IdempotencyKey, idempotency_key)
        if seen is None or seen.request_hash != body_hash:
            raise HTTPException(409, "conflicting request")
        response.status_code = 200
        return _enrollment_out(db.get(Enrollment, int(seen.resource_ref)))
    db.refresh(en)
    return _enrollment_out(en)


@router.get("/enrollments")
def list_enrollments(limit: int = 200, db: Session = Depends(get_db),
                     caller: Caller = Depends(require_scope("benefits:read"))):
    """Latest enrollments first. Used for reporting (time from ticket to approval)."""
    rows = db.scalars(select(Enrollment).order_by(Enrollment.pk.desc()).limit(max(1, min(limit, 500)))).all()
    return {"enrollments": [_enrollment_out(en) for en in rows]}


@router.get("/enrollments/{enrollment_id}")
def get_enrollment(enrollment_id: str, db: Session = Depends(get_db),
                   caller: Caller = Depends(require_scope("benefits:read"))):
    try:
        pk = int(enrollment_id.removeprefix("ENR-"))
    except ValueError:
        raise HTTPException(404, "enrollment not found")
    en = db.get(Enrollment, pk)
    if en is None:
        raise HTTPException(404, "enrollment not found")
    return _enrollment_out(en)
