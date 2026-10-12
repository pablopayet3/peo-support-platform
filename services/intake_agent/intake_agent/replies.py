"""Customer-facing replies are fixed templates, reviewed once, not generated text.
Wording about benefits is a compliance matter, so the model never writes it."""
from datetime import datetime
from zoneinfo import ZoneInfo

EVENT_WORDS = {"newborn": "your new baby", "adoption": "your adopted child", "marriage": "your spouse",
               "loss_of_coverage": "your coverage change"}


def clarify(event_type: str | None) -> str:
    if event_type == "newborn":
        return "Thanks for reaching out. Are you looking to add your new baby to your health plan? Just reply yes and we'll get started."
    return "Thanks for reaching out. Are you looking to update your benefits because of a life event? Just reply yes and we'll get started."


def ask_date(event_type: str) -> str:
    what = {"newborn": "your baby's date of birth", "adoption": "the date of the adoption",
            "marriage": "the date of your marriage", "loss_of_coverage": "the date your other coverage ended"}[event_type]
    lead = "Congratulations! " if event_type in ("newborn", "adoption", "marriage") else ""
    return f"{lead}To get started, please reply with {what} (for example, October 6, 2026). The enrollment deadline is counted from that date."


def ask_company(options: list[dict]) -> str:
    names = " or ".join(o["company_name"] for o in options)
    return f"Thanks. You're connected to more than one company with us. Which company is this request about: {names}?"


def handed_off(event_type: str, complete: bool, deadline_iso: str | None, tz: str = "America/New_York") -> str:
    if complete and deadline_iso:
        dl = datetime.fromisoformat(deadline_iso.replace("Z", "+00:00")).astimezone(ZoneInfo(tz))
        when = dl.strftime("%B %-d, %Y")
        return (f"Thank you. Your request to add {EVENT_WORDS[event_type]} to your plan is now with our benefits team. "
                f"The deadline to enroll is {when}, and we'll make sure it's handled well before then. "
                "A benefits specialist will review it and confirm by email. If we need a document, we'll ask.")
    return ("Thank you. Your request is now with our benefits team. "
            "A benefits specialist will review it and get back to you by email.")
