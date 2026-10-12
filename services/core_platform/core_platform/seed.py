"""Sample data for the fictional Northwind PEO. Each person exists to exercise one case."""
from datetime import date

from sqlalchemy.orm import Session

from .models import Company, Coverage, Dependent, Employee, Invoice, Payment, Person, Plan, QleRule

COMPANIES = [
    ("C-1001", "Acme Bakery", "America/New_York"),
    ("C-1002", "Bluebird Design Studio", "America/Los_Angeles"),
    ("C-1003", "Harbor & Pine Accounting", "America/Chicago"),
]
PLANS = [
    ("PL-01", "C-1001", "Northwind Gold PPO", "Atlas Health"),
    ("PL-02", "C-1002", "Northwind Silver HMO", "Meridian Care"),
    ("PL-03", "C-1003", "Northwind Bronze EPO", "Atlas Health"),
]
# person id, first, last, email, phone
PEOPLE = [
    ("P-1001", "Maria", "Lopez", "maria.lopez@acmebakery.example", "+12125550101"),   # newborn, happy path
    ("P-1002", "Daniel", "Kim", "daniel.kim@acmebakery.example", "+12125550102"),     # admin writing for Maria
    ("P-1003", "Priya", "Shah", "priya.shah@acmebakery.example", "+12125550103"),     # no active plan
    ("P-1004", "Tom", "Becker", "tom.becker@bluebird.example", "+13105550104"),       # late newborn, coverage failure
    ("P-1005", "Sam", "Rivera", "sam.rivera@harborpine.example", "+13125550105"),     # two companies
    ("P-1006", "Grace", "Chen", "grace.chen@bluebird.example", "+13105550106"),       # former employee
]
# employee id, person, company, role, status
EMPLOYEES = [
    ("E-1001", "P-1001", "C-1001", "employee", "active"),
    ("E-1002", "P-1002", "C-1001", "admin", "active"),
    ("E-1003", "P-1003", "C-1001", "employee", "active"),
    ("E-1004", "P-1004", "C-1002", "employee", "active"),
    ("E-1005", "P-1005", "C-1003", "admin", "active"),
    ("E-1006", "P-1005", "C-1002", "employee", "active"),
    ("E-1007", "P-1006", "C-1002", "employee", "terminated"),
]
COVERAGES = [
    ("E-1001", "PL-01", "employee_spouse"),
    ("E-1002", "PL-01", "employee_only"),
    ("E-1004", "PL-02", "family"),
    ("E-1005", "PL-03", "employee_only"),
    ("E-1006", "PL-02", "employee_only"),
]
DEPENDENTS = [
    ("D-1001-01", "E-1001", "Diego", "Lopez", "spouse", date(1990, 4, 12)),
    ("D-1004-01", "E-1004", "Anna", "Becker", "spouse", date(1988, 9, 3)),
    ("D-1004-02", "E-1004", "Leo", "Becker", "child", date(2021, 2, 17)),
]


def seed(db: Session) -> bool:
    """Load the sample data once. Returns False if data is already there."""
    if db.get(Company, "C-1001") is not None:
        return False
    db.add_all(Company(id=i, name=n, timezone=tz) for i, n, tz in COMPANIES)
    db.add_all(Plan(id=i, company_id=c, name=n, carrier=car) for i, c, n, car in PLANS)
    db.add_all(Person(id=i, first_name=f, last_name=l, email=e, phone=p) for i, f, l, e, p in PEOPLE)
    db.flush()
    db.add_all(Employee(id=i, person_id=p, company_id=c, role=r, status=s) for i, p, c, r, s in EMPLOYEES)
    db.flush()
    db.add_all(Coverage(employee_id=e, plan_id=p, level=lvl) for e, p, lvl in COVERAGES)
    db.add_all(Dependent(id=i, employee_id=e, first_name=f, last_name=l, relationship_to_employee=r,
                         date_of_birth=d) for i, e, f, l, r, d in DEPENDENTS)
    db.add_all(QleRule(event_type=t, window_days=30) for t in ("newborn", "adoption", "marriage", "loss_of_coverage"))
    # The "billed twice" case: one invoice, two identical payments on the same day.
    db.add(Invoice(id="INV-2026-10-001", company_id="C-1001", period="2026-10", amount=8450.00, status="paid"))
    db.flush()
    db.add_all([Payment(id="PAY-1001", invoice_id="INV-2026-10-001", amount=8450.00, paid_on=date(2026, 10, 1)),
                Payment(id="PAY-1002", invoice_id="INV-2026-10-001", amount=8450.00, paid_on=date(2026, 10, 1))])
    db.commit()
    return True


def apply_demo_email_overrides(db: Session, overrides: dict[str, str]) -> int:
    """Point sample people at real demo inboxes. Idempotent; returns how many changed."""
    changed = 0
    for person_id, email in overrides.items():
        person = db.get(Person, person_id)
        if person is not None and person.email != email:
            person.email = email
            changed += 1
    db.commit()
    return changed
