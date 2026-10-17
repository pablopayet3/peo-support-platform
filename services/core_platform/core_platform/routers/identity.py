from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..db import get_db
from ..models import Employee, Person
from ..seed import normalize_phone
from ..security import Caller, request_id, require_scope

router = APIRouter(prefix="/v1", tags=["identity"])


def _membership(e: Employee) -> dict:
    return {"employee_id": e.id, "company_id": e.company_id, "company_name": e.company.name,
            "role": e.role, "status": e.status}


@router.get("/people/lookup")
def lookup_person(email: str | None = Query(default=None), phone: str | None = Query(default=None),
                  db: Session = Depends(get_db), caller: Caller = Depends(require_scope("identity:read")),
                  rid: str = Depends(request_id)):
    if not email and not phone:
        raise HTTPException(400, "email or phone is required")
    stmt = select(Person)
    stmt = stmt.where(Person.email == email.strip().lower()) if email else stmt.where(Person.phone == normalize_phone(phone))
    person = db.scalars(stmt).first()
    audit.record(db, actor=caller.client, action="identity.lookup", entity="person",
                 entity_id=person.id if person else "-", request_id=rid, found=bool(person))
    db.commit()
    if person is None:
        raise HTTPException(404, "no person with that contact")
    return {"person_id": person.id, "name": person.name,
            "memberships": [_membership(m) for m in person.memberships]}


class AuthorizationCheck(BaseModel):
    actor_person_id: str
    employee_id: str


@router.post("/authorizations/check")
def check_authorization(body: AuthorizationCheck, db: Session = Depends(get_db),
                        caller: Caller = Depends(require_scope("identity:read"))):
    employee = db.get(Employee, body.employee_id)
    if employee is None or employee.status != "active":
        return {"allowed": False, "reason": "employee not found or not active"}
    if employee.person_id == body.actor_person_id:
        return {"allowed": True, "reason": "self"}
    is_admin = db.scalars(select(Employee).where(
        Employee.person_id == body.actor_person_id, Employee.company_id == employee.company_id,
        Employee.role == "admin", Employee.status == "active")).first()
    if is_admin:
        return {"allowed": True, "reason": "admin of the same company"}
    return {"allowed": False, "reason": "not the employee and not an admin of their company"}


@router.get("/employees/{employee_id}")
def get_employee(employee_id: str, db: Session = Depends(get_db),
                 caller: Caller = Depends(require_scope("identity:read"))):
    e = db.get(Employee, employee_id)
    if e is None:
        raise HTTPException(404, "employee not found")
    return {"employee_id": e.id, "person_id": e.person_id, "name": e.person.name, "company_id": e.company_id,
            "company_name": e.company.name, "company_timezone": e.company.timezone,
            "role": e.role, "status": e.status}
