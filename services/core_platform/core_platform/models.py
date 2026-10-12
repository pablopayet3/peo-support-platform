"""The core platform's data model: identity, benefits and billing.

Sensitive values (Social Security numbers, salary, health details) are deliberately not modelled.
The support tools never need them, so the API cannot leak them.
"""
from datetime import date, datetime, timezone

from sqlalchemy import JSON, Date, DateTime, ForeignKey, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


class Company(Base):
    __tablename__ = "companies"
    id: Mapped[str] = mapped_column(String(16), primary_key=True)  # C-1001
    name: Mapped[str] = mapped_column(String(120))
    timezone: Mapped[str] = mapped_column(String(64), default="America/New_York")
    status: Mapped[str] = mapped_column(String(16), default="active")
    employees: Mapped[list["Employee"]] = relationship(back_populates="company")


class Person(Base):
    """One human being. A person can belong to several companies (owners, accountants, job changes)."""
    __tablename__ = "people"
    id: Mapped[str] = mapped_column(String(16), primary_key=True)  # P-1001
    first_name: Mapped[str] = mapped_column(String(60))
    last_name: Mapped[str] = mapped_column(String(60))
    email: Mapped[str | None] = mapped_column(String(160), unique=True, index=True)
    phone: Mapped[str | None] = mapped_column(String(20), unique=True, index=True)
    memberships: Mapped[list["Employee"]] = relationship(back_populates="person")

    @property
    def name(self) -> str:
        return f"{self.first_name} {self.last_name}"


class Employee(Base):
    """A person's membership in one company, with their role there."""
    __tablename__ = "employees"
    id: Mapped[str] = mapped_column(String(16), primary_key=True)  # E-1001
    person_id: Mapped[str] = mapped_column(ForeignKey("people.id"))
    company_id: Mapped[str] = mapped_column(ForeignKey("companies.id"))
    role: Mapped[str] = mapped_column(String(16), default="employee")  # employee | admin
    status: Mapped[str] = mapped_column(String(16), default="active")  # active | terminated
    person: Mapped[Person] = relationship(back_populates="memberships")
    company: Mapped[Company] = relationship(back_populates="employees")
    coverages: Mapped[list["Coverage"]] = relationship(back_populates="employee")
    dependents: Mapped[list["Dependent"]] = relationship(back_populates="employee")


class Plan(Base):
    __tablename__ = "plans"
    id: Mapped[str] = mapped_column(String(16), primary_key=True)  # PL-01
    company_id: Mapped[str] = mapped_column(ForeignKey("companies.id"))
    kind: Mapped[str] = mapped_column(String(16), default="medical")
    name: Mapped[str] = mapped_column(String(120))
    carrier: Mapped[str] = mapped_column(String(120))


class Coverage(Base):
    __tablename__ = "coverages"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    employee_id: Mapped[str] = mapped_column(ForeignKey("employees.id"))
    plan_id: Mapped[str] = mapped_column(ForeignKey("plans.id"))
    level: Mapped[str] = mapped_column(String(32))  # employee_only | employee_spouse | employee_children | family
    status: Mapped[str] = mapped_column(String(16), default="active")  # active | ended
    employee: Mapped[Employee] = relationship(back_populates="coverages")
    plan: Mapped[Plan] = relationship()


class Dependent(Base):
    __tablename__ = "dependents"
    id: Mapped[str] = mapped_column(String(16), primary_key=True)  # D-1001
    employee_id: Mapped[str] = mapped_column(ForeignKey("employees.id"))
    first_name: Mapped[str] = mapped_column(String(60))
    last_name: Mapped[str] = mapped_column(String(60))
    relationship_to_employee: Mapped[str] = mapped_column(String(16))  # spouse | child
    date_of_birth: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(24), default="active")  # active | pending_enrollment
    employee: Mapped[Employee] = relationship(back_populates="dependents")

    @property
    def name(self) -> str:
        return f"{self.first_name} {self.last_name}"


class QleRule(Base):
    """How long the enrollment window stays open after each qualifying life event."""
    __tablename__ = "qle_rules"
    event_type: Mapped[str] = mapped_column(String(32), primary_key=True)
    window_days: Mapped[int] = mapped_column(Integer)


class Enrollment(Base):
    __tablename__ = "enrollments"
    pk: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    employee_id: Mapped[str] = mapped_column(ForeignKey("employees.id"))
    dependent_id: Mapped[str | None] = mapped_column(ForeignKey("dependents.id"))
    event_type: Mapped[str] = mapped_column(String(32))
    event_date: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(24), default="pending_carrier")
    approved_by: Mapped[str] = mapped_column(String(80))
    approved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    source_ref: Mapped[str] = mapped_column(String(80))
    employee: Mapped[Employee] = relationship()
    dependent: Mapped[Dependent | None] = relationship()

    @property
    def id(self) -> str:
        return f"ENR-{self.pk:06d}"


class IdempotencyKey(Base):
    """Remembers each write request, so the same key always gives the same answer."""
    __tablename__ = "idempotency_keys"
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    client: Mapped[str] = mapped_column(String(64))
    request_hash: Mapped[str] = mapped_column(String(64))
    resource_ref: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class Invoice(Base):
    __tablename__ = "invoices"
    id: Mapped[str] = mapped_column(String(16), primary_key=True)  # INV-2026-10-001
    company_id: Mapped[str] = mapped_column(ForeignKey("companies.id"))
    period: Mapped[str] = mapped_column(String(7))  # 2026-10
    amount: Mapped[float] = mapped_column(Numeric(10, 2))
    status: Mapped[str] = mapped_column(String(16), default="paid")


class Payment(Base):
    __tablename__ = "payments"
    id: Mapped[str] = mapped_column(String(16), primary_key=True)  # PAY-1001
    invoice_id: Mapped[str] = mapped_column(ForeignKey("invoices.id"))
    amount: Mapped[float] = mapped_column(Numeric(10, 2))
    paid_on: Mapped[date] = mapped_column(Date)


class AuditEvent(Base):
    """Append-only. The API has no route that updates or deletes these rows.
    In Postgres the application's database role is also denied UPDATE and DELETE on this table."""
    __tablename__ = "audit_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    actor: Mapped[str] = mapped_column(String(80))
    action: Mapped[str] = mapped_column(String(48))
    entity: Mapped[str] = mapped_column(String(48))
    entity_id: Mapped[str] = mapped_column(String(48))
    request_id: Mapped[str | None] = mapped_column(String(64))
    details: Mapped[dict] = mapped_column(JSON, default=dict)
