"""Deterministic redaction, before any text reaches the model. Personal data never enters a prompt."""
import re

PATTERNS = {
    "SSN": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    "CARD": re.compile(r"\b(?:\d[ -]?){13,16}\b"),
    "BANK_ACCOUNT": re.compile(r"\b(?:account|acct|routing)\s*(?:number|no\.?|#)?\s*:?\s*\d{6,17}\b", re.I),
    "PHONE": re.compile(r"\+?\b1?[ .-]?\(?\d{3}\)?[ .-]?\d{3}[ .-]?\d{4}\b"),
    "EMAIL": re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"),
}


def redact(text: str) -> tuple[str, list[str]]:
    """Returns the redacted text and the kinds of data found. Emails are kept out of the prompt too;
    the agent reads them from the ticket fields, not from the model."""
    found = []
    for label, pattern in PATTERNS.items():
        if pattern.search(text):
            found.append(label)
            text = pattern.sub(f"[{label}]", text)
    return text, found
