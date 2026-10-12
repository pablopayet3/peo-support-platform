"""The two workflows that touch Zendesk: the check behind trigger 45-CHECK, and the approval."""
import logging
from datetime import date

import httpx

from .checks import CheckRequest, Contact, run_qle_check
from .config import get_settings
from .core_client import CoreClient
from .resilience import Unavailable
from .zendesk_client import ZendeskClient

log = logging.getLogger("integration")


class NotAllowed(Exception):
    pass


def _note_for(result: dict) -> str:
    lines = ["Automatic checks (integration service):"]
    for c in result["checks"]:
        lines.append(f"- {c['name'].replace('_', ' ')}: {'passed' if c['passed'] else 'FAILED'}, {c['detail']}")
    if result.get("deadline"):
        lines.append(f"Enrollment deadline: {result['deadline']} (UTC)")
    return "\n".join(lines)


async def check_ticket(core: CoreClient, zd: ZendeskClient, ticket_id: int, rid: str) -> str:
    """Run the checks for a ticket whose fields a person filled in (phone, or set by hand). Idempotent."""
    ticket = await zd.ticket(ticket_id)
    if zd.field_value(ticket, "ai_intake_result"):
        return "skipped: checks already recorded"
    life_event = zd.field_value(ticket, "life_event_type")
    if not life_event:
        return "skipped: no life event type"
    event_date = zd.field_value(ticket, "event_date")
    if not event_date:
        await zd.update_ticket(ticket_id, internal_note="The checks need the Event date. Fill it in and they run again.")
        await zd.remove_tags(ticket_id, ["qle_check_requested"])
        return "waiting: no event date"

    requester = await zd.user(ticket["requester_id"])
    req = CheckRequest(requester=Contact(email=requester.get("email"), phone=requester.get("phone")),
                       event_type=life_event.removeprefix("qle_"), event_date=date.fromisoformat(event_date))
    try:
        result = (await run_qle_check(core, req, rid)).to_dict()
    except Unavailable as exc:
        log.warning("checks unavailable", extra={"extra_fields": {"request_id": rid, "ticket_id": ticket_id}})
        await zd.update_ticket(ticket_id, fields={"ai_intake_result": "ai_intake_incomplete"},
                               internal_note=f"The checks could not run ({exc}). A specialist should run them by hand.")
        return "incomplete: a system was unavailable"

    kwargs = {}
    if result.get("deadline"):
        kwargs = {"ticket_type": "task", "due_at": result["deadline"]}
    await zd.update_ticket(ticket_id, fields={"ai_intake_result": result["ai_intake_result"]},
                           internal_note=_note_for(result), **kwargs)
    return f"done: {result['ai_intake_result']}"


async def approve(core: CoreClient, zd: ZendeskClient, body: dict, rid: str) -> tuple[dict, bool]:
    """Create the pending enrollment (at most once per ticket), then record the outcome on the ticket."""
    s = get_settings()
    approver = body["approver_zendesk_user_id"]
    if s.benefits_ops_group_id and s.benefits_ops_group_id not in await zd.user_group_ids(approver):
        raise NotAllowed("the approver is not in the Benefits Ops group")

    ticket_id = body["ticket_id"]
    core_body = {"employee_id": body["employee_id"], "event_type": body["event_type"],
                 "event_date": body["event_date"], "dependent": body["dependent"],
                 "approved_by": f"zendesk:{approver}", "source_ref": f"zendesk-ticket:{ticket_id}"}
    # Raises core_client.Conflict if the checks no longer pass (for example the window has closed).
    enrollment, created = await core.create_enrollment(core_body, f"zendesk-ticket-{ticket_id}-enrollment", rid)

    ticket_updated = True
    try:
        await zd.update_ticket(
            ticket_id, fields={"enrollment_outcome": "qle_approved", "enrollment_id": enrollment["enrollment_id"]},
            internal_note=(f"Enrollment {enrollment['enrollment_id']} created, status {enrollment['status']}. "
                           f"Approved by Zendesk user {approver}. Request {rid}."))
    except (Unavailable, httpx.HTTPError) as exc:
        # The enrollment exists; only the ticket update failed. Retrying the approval is safe (idempotent).
        ticket_updated = False
        log.error("ticket update failed after enrollment",
                  extra={"extra_fields": {"request_id": rid, "ticket_id": ticket_id, "error": str(exc)}})
    return {"enrollment_id": enrollment["enrollment_id"], "status": enrollment["status"],
            "ticket_updated": ticket_updated}, created
