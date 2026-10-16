"""Context for requests the AI agent may answer, or must hand to a person (case study, Part 1 and Part 3).

One call returns what is known about the sender and the facts a specialist would look up first:
- benefits_question: the plan and its deductible, for an answer the AI can give on its own
- coverage_failure: every membership and its coverage, so the escalation starts with the facts
- billing: invoices paid more than once, for the companies where the sender is an admin

The agent decides nothing here. It reads the answer and follows its rules.
"""
import logging

import httpx

from .checks import Contact
from .core_client import CoreClient, NotFound

log = logging.getLogger("integration")


def _medical(cov: dict) -> dict | None:
    m = cov.get("medical")
    if not m:
        return None
    return {"plan_name": m["plan_name"], "carrier": m["carrier"], "coverage_level": m["coverage_level"],
            "status": m["status"], "plan_year": m.get("plan_year"), "deductible": m.get("deductible"),
            "out_of_pocket_max": m.get("out_of_pocket_max")}


async def request_context(core: CoreClient, requester: Contact, topic: str, rid: str) -> dict:
    out: dict = {"topic": topic, "verified": False, "name": None, "memberships": [], "notes": []}
    try:
        person = await core.lookup_person(email=requester.email, phone=requester.phone, request_id=rid)
    except NotFound:
        out["notes"].append("The sender's email or phone is not on file.")
        return out
    active = [m for m in person["memberships"] if m["status"] == "active"]
    out.update(verified=bool(active), name=person["name"],
               memberships=[{k: m[k] for k in ("employee_id", "company_id", "company_name", "role")} for m in active])
    if not active:
        out["notes"].append("The sender is on file but has no active employment.")
        return out

    if topic in ("benefits_question", "coverage_failure"):
        for m in out["memberships"]:
            m["medical"] = _medical(await core.coverage(m["employee_id"], rid))
        if len(active) > 1:
            out["notes"].append("The sender works for more than one client company.")

    if topic == "billing":
        admin_of = [m for m in out["memberships"] if m["role"] == "admin"]
        out["is_admin"] = bool(admin_of)
        out["billing"] = []
        if not admin_of:
            out["notes"].append("The sender is not an admin of any client company.")
        for m in admin_of:
            try:
                bill = await core.billing(m["company_id"], rid)
            except httpx.HTTPStatusError as exc:  # e.g. the key lacks billing:read; context only, never blocking
                log.warning("billing unavailable", extra={"extra_fields": {"request_id": rid, "status": exc.response.status_code}})
                out["notes"].append("Billing records could not be read; check them in the core platform.")
                continue
            over = [i for i in bill["invoices"] if i["overpaid_by"] > 0]
            out["billing"].append({"company_id": m["company_id"], "company_name": m["company_name"],
                                   "overpaid_invoices": over})
    return out
