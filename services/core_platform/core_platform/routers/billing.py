from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..db import get_db
from ..models import Company, Invoice, Payment
from ..security import Caller, request_id, require_scope

router = APIRouter(prefix="/v1", tags=["billing"])


@router.get("/companies/{company_id}/billing")
def company_billing(company_id: str, db: Session = Depends(get_db),
                    caller: Caller = Depends(require_scope("billing:read")), rid: str = Depends(request_id)):
    """Invoices with their payments. Flags an invoice paid more than once, the 'billed twice' case."""
    if db.get(Company, company_id) is None:
        raise HTTPException(404, "company not found")
    invoices = db.scalars(select(Invoice).where(Invoice.company_id == company_id).order_by(Invoice.id.desc())).all()
    out = []
    for inv in invoices:
        pays = db.scalars(select(Payment).where(Payment.invoice_id == inv.id).order_by(Payment.id)).all()
        paid = sum(float(p.amount) for p in pays)
        out.append({"invoice_id": inv.id, "period": inv.period, "amount": float(inv.amount), "status": inv.status,
                    "payments": [{"payment_id": p.id, "amount": float(p.amount), "paid_on": p.paid_on.isoformat()}
                                 for p in pays],
                    "overpaid_by": round(paid - float(inv.amount), 2) if paid > float(inv.amount) else 0.0})
    audit.record(db, actor=caller.client, action="billing.read", entity="company", entity_id=company_id,
                 request_id=rid, invoices=len(out))
    db.commit()
    return {"company_id": company_id, "invoices": out}
