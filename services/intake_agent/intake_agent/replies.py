"""Customer-facing replies are fixed templates, reviewed once, not generated text.
Wording about benefits is a compliance matter, so the model never writes it."""
from datetime import date

EVENT_WORDS = {"newborn": "your new baby", "adoption": "your adopted child", "marriage": "your spouse",
               "loss_of_coverage": "your coverage change"}


def clarify(event_type: str | None) -> str:
    if event_type == "newborn":
        return "Thanks for reaching out. Are you looking to add your new baby to your health plan? Just reply yes and we'll get started."
    if event_type is None:
        return ("Thanks for reaching out. To point you in the right direction: is this about a birth, an adoption, "
                "a marriage, or losing other coverage? Just reply with which one and the date, and we'll get started.")
    return "Thanks for reaching out. Are you looking to update your benefits because of a life event? Just reply yes and we'll get started."


def ask_date(event_type: str) -> str:
    what = {"newborn": "your baby's date of birth", "adoption": "the date of the adoption",
            "marriage": "the date of your marriage", "loss_of_coverage": "the date your other coverage ended"}[event_type]
    lead = "Congratulations! " if event_type in ("newborn", "adoption", "marriage") else ""
    return f"{lead}To get started, please reply with {what} (for example, October 6, 2026). The enrollment deadline is counted from that date."


def ask_company(options: list[dict]) -> str:
    names = " or ".join(o["company_name"] for o in options)
    return f"Thanks. You're connected to more than one company with us. Which company is this request about: {names}?"


def _day(last_day: str) -> str:
    d = date.fromisoformat(last_day)
    return f"{d.strftime('%B')} {d.day}, {d.year}"


def deadline_text(last_day: str) -> str:
    """The deadline as a person reads it: 11:59 pm on the last day, in the company's own time zone."""
    return f"{_day(last_day)}, 11:59 pm company time"


LEVEL_WORDS = {"employee_only": "employee only", "employee_spouse": "employee and spouse",
               "employee_children": "employee and children", "family": "family"}


def _usd(n) -> str:
    return f"${n:,.0f}"


def deductible(first_name: str, plan: dict) -> str:
    """Tier 1, fully automated: a factual answer from the plan documents. Nothing changes anywhere."""
    d, o = plan["deductible"], plan["out_of_pocket_max"]
    level = LEVEL_WORDS.get(plan["coverage_level"], plan["coverage_level"].replace("_", " "))
    return (f"Hi {first_name}, you're enrolled in the {plan['plan_name']} plan ({level} coverage) with {plan['carrier']}. "
            f"For the {plan['plan_year']} plan year, the in-network deductible is {_usd(d['individual'])} per person "
            f"and {_usd(d['family'])} per family. The out-of-pocket maximum is {_usd(o['individual'])} per person "
            f"and {_usd(o['family'])} per family. Your carrier's member portal shows how much you've already paid "
            "toward them this year. If anything here doesn't look right, just reply and a benefits specialist will help.")


def verify_first(already_started: bool = False) -> str:
    lead = "Thanks for reaching out. Our team has already started looking into this. " if already_started else "Thanks for reaching out. "
    return (lead + "To protect your account, we can only share or change account details once we've confirmed who you are. "
            "Please log in to your Northwind account and send your request from there, or reply from the email "
            "address we have on file for you.")


def escalated() -> str:
    """Tier 3, human-led: the AI acknowledges and steps aside. It never tries to fix a coverage failure."""
    return ("Thank you for letting us know. We're treating this as urgent, and a benefits specialist is taking over "
            "your case now. If you or a family member need care or a prescription today, reply with the best phone "
            "number to reach you and we'll call you back.")


def payments_received() -> str:
    return ("Thank you for flagging this. We've passed it to our Payments team as urgent, and they're reviewing the "
            "charges now. We'll confirm the correction with you by email.")


def handed_off(event_type: str, complete: bool, last_day: str | None) -> str:
    if complete and last_day:
        when = _day(last_day)
        return (f"Thank you. Your request to add {EVENT_WORDS[event_type]} to your plan is now with our benefits team. "
                f"The deadline to enroll is {when}, and we'll make sure it's handled well before then. "
                "A benefits specialist will review it and confirm by email. If we need a document, we'll ask.")
    return ("Thank you. Your request is now with our benefits team. "
            "A benefits specialist will review it and get back to you by email.")


# Spoken versions, for live phone calls: shorter sentences, no links, nothing to "reply" to.
def _spoken_usd(n) -> str:
    return f"{n:,.0f} dollars"


def deductible_spoken(first_name: str, plan: dict) -> str:
    d, o = plan["deductible"], plan["out_of_pocket_max"]
    return (f"You're on the {plan['plan_name']} plan with {plan['carrier']}. Your in-network deductible is "
            f"{_spoken_usd(d['individual'])} per person, and {_spoken_usd(d['family'])} per family. Your out-of-pocket "
            f"maximum is {_spoken_usd(o['individual'])} per person. Your carrier's member portal shows how much you've "
            "already paid toward it this year. Is there anything else I can help with?")


def handed_off_spoken(event_type: str, complete: bool, last_day: str | None) -> str:
    lead = "Congratulations! " if event_type in ("newborn", "adoption", "marriage") else ""
    if complete and last_day:
        return (f"{lead}You're eligible to add {EVENT_WORDS[event_type]} to your plan. The deadline is {_day(last_day)}. "
                "I've sent your request to our benefits team. A specialist will review it and confirm by email, and "
                "they'll tell you if any document is needed. Anything else I can help with?")
    return (f"{lead}Thank you. I've passed your request to our benefits team, and a specialist will contact you to "
            "go over the next steps. Anything else I can help with?")


def escalated_spoken() -> str:
    return ("I'm sorry, that shouldn't happen. I'm treating this as urgent and connecting you with a benefits "
            "specialist right now. Please stay on the line.")
