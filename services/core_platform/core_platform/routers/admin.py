"""Internal admin screens. Read-only: the support tools change data only through the API."""
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import AuditEvent, Company, Employee, Enrollment, Invoice, Payment
from ..security import require_admin

templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))
router = APIRouter(include_in_schema=False, dependencies=[Depends(require_admin)])


@router.get("/")
def root():
    return RedirectResponse("/admin")


@router.get("/admin", response_class=HTMLResponse)
def overview(request: Request, db: Session = Depends(get_db)):
    stats = {
        "companies": db.scalar(select(func.count()).select_from(Company)),
        "employees": db.scalar(select(func.count()).select_from(Employee).where(Employee.status == "active")),
        "pending": db.scalar(select(func.count()).select_from(Enrollment).where(Enrollment.status == "pending_carrier")),
        "audit": db.scalar(select(func.count()).select_from(AuditEvent)),
    }
    enrollments = db.scalars(select(Enrollment).order_by(Enrollment.pk.desc()).limit(5)).all()
    events = db.scalars(select(AuditEvent).order_by(AuditEvent.id.desc()).limit(8)).all()
    return templates.TemplateResponse(request, "overview.html",
                                      {"stats": stats, "enrollments": enrollments, "events": events, "nav": "overview"})


@router.get("/admin/employees", response_class=HTMLResponse)
def employees(request: Request, db: Session = Depends(get_db)):
    rows = db.scalars(select(Employee).order_by(Employee.id)).all()
    return templates.TemplateResponse(request, "employees.html", {"rows": rows, "nav": "employees"})


@router.get("/admin/employees/{employee_id}", response_class=HTMLResponse)
def employee(employee_id: str, request: Request, db: Session = Depends(get_db)):
    e = db.get(Employee, employee_id)
    if e is None:
        raise HTTPException(404)
    enrollments = db.scalars(select(Enrollment).where(Enrollment.employee_id == e.id).order_by(Enrollment.pk.desc())).all()
    return templates.TemplateResponse(request, "employee.html", {"e": e, "enrollments": enrollments, "nav": "employees"})


@router.get("/admin/enrollments", response_class=HTMLResponse)
def enrollments(request: Request, db: Session = Depends(get_db)):
    rows = db.scalars(select(Enrollment).order_by(Enrollment.pk.desc())).all()
    return templates.TemplateResponse(request, "enrollments.html", {"rows": rows, "nav": "enrollments", "refresh": 5})


@router.get("/admin/billing", response_class=HTMLResponse)
def billing(request: Request, db: Session = Depends(get_db)):
    invoices = db.scalars(select(Invoice).order_by(Invoice.id.desc())).all()
    payments = {i.id: db.scalars(select(Payment).where(Payment.invoice_id == i.id)).all() for i in invoices}
    return templates.TemplateResponse(request, "billing.html", {"invoices": invoices, "payments": payments, "nav": "billing"})


@router.get("/admin/audit", response_class=HTMLResponse)
def audit_log(request: Request, db: Session = Depends(get_db)):
    rows = db.scalars(select(AuditEvent).order_by(AuditEvent.id.desc()).limit(200)).all()
    return templates.TemplateResponse(request, "audit.html", {"rows": rows, "nav": "audit", "refresh": 10})
