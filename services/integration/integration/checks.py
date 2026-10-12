"""The intake checks (case study, section 3, steps 2 to 4), run in one call.

The order never changes: who is asking, is there an active medical plan, is the window open.
The first check that fails stops the run, and the answer uses the same words as the Zendesk field
"AI intake result", so every caller writes it to the ticket without translating it.
"""
from dataclasses import dataclass, field
from datetime import date

from .core_client import CoreClient, NotFound

RESULT_FOR = {
    "none": "ai_intake_complete",
    "identity": "ai_fail_identity",
    "no_plan": "ai_fail_no_plan",
    "window_closed": "ai_fail_window",
    "company_unclear": "ai_intake_incomplete",
}


@dataclass
class Contact:
    email: str | None = None
    phone: str | None = None


@dataclass
class CheckRequest:
    requester: Contact
    event_type: str
    event_date: date
    employee: Contact | None = None
    company_id: str | None = None


@dataclass
class CheckResult:
    failed_check: str = "none"
    employee_id: str | None = None
    company_name: str | None = None
    deadline: str | None = None
    checks: list[dict] = field(default_factory=list)
    company_options: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        out = {"failed_check": self.failed_check, "ai_intake_result": RESULT_FOR[self.failed_check],
               "eligible": self.failed_check == "none", "employee_id": self.employee_id,
               "company_name": self.company_name, "deadline": self.deadline, "checks": self.checks}
        if self.company_options:
            out["company_options"] = self.company_options
        return out


async def _lookup(core: CoreClient, c: Contact, rid: str) -> dict | None:
    try:
        return await core.lookup_person(email=c.email, phone=c.phone, request_id=rid)
    except NotFound:
        return None


async def run_qle_check(core: CoreClient, req: CheckRequest, rid: str) -> CheckResult:
    """Raises resilience.Unavailable if a system does not answer; the caller then ends the intake as Incomplete."""
    res = CheckResult()

    def fail(name: str, failed_check: str, detail: str) -> CheckResult:
        res.checks.append({"name": name, "passed": False, "detail": detail})
        res.failed_check = failed_check
        return res

    # Step 2: who is asking, and for which employee
    requester = await _lookup(core, req.requester, rid)
    if requester is None:
        return fail("identity", "identity", "the requester's email or phone is not on file")
    target = requester
    if req.employee and (req.employee.email or req.employee.phone):
        target = await _lookup(core, req.employee, rid)
        if target is None:
            return fail("identity", "identity", "the employee named in the request is not on file")

    memberships = [m for m in target["memberships"] if m["status"] == "active"]
    if req.company_id:
        memberships = [m for m in memberships if m["company_id"] == req.company_id]
    if not memberships:
        return fail("identity", "identity", "no active employment found")
    if len(memberships) > 1:
        res.company_options = [{"company_id": m["company_id"], "company_name": m["company_name"]} for m in memberships]
        return fail("identity", "company_unclear", "the person belongs to several companies; ask which one")
    m = memberships[0]
    res.employee_id, res.company_name = m["employee_id"], m["company_name"]

    if requester["person_id"] != target["person_id"]:
        if not await core.can_act_for(requester["person_id"], m["employee_id"], rid):
            return fail("identity", "identity", "the requester is not allowed to act for this employee")
        res.checks.append({"name": "identity", "passed": True, "detail": "admin acting for the employee"})
    else:
        res.checks.append({"name": "identity", "passed": True, "detail": "the employee themself"})

    # Step 3: an active medical plan to add the dependent to
    coverage = await core.coverage(m["employee_id"], rid)
    if coverage["medical"] is None:
        return fail("active_plan", "no_plan", "no active medical plan")
    res.checks.append({"name": "active_plan", "passed": True,
                       "detail": f"{coverage['medical']['plan_name']} ({coverage['medical']['coverage_level']})"})

    # Step 4: the enrollment window
    window = await core.evaluate_qle(m["employee_id"], req.event_type, req.event_date.isoformat(), rid)
    res.deadline = window["deadline"]
    if not window["window_open"]:
        return fail("enrollment_window", "window_closed", f"the window closed on {window['last_day']}")
    res.checks.append({"name": "enrollment_window", "passed": True, "detail": f"open until {window['last_day']}"})
    return res
