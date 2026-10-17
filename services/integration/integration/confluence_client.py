"""Confluence: the Benefits Ops procedure, read live so the sidebar always shows the current version.

The procedure page is the single source of truth. The sidebar asks this service for the steps that fit
the ticket's situation; this service reads the page through the Confluence REST API, takes the right
part of it, and returns the steps with their links (a Tango walkthrough sits on the step it belongs to).
Edit the page in Confluence and the sidebar changes on the next ticket, with no deploy.

Confluence is guidance, never a gate: if it is slow or down, the last copy read is used, then a built-in
copy, and the response says which one the specialist is looking at.
"""
import json
import logging
import time
from html.parser import HTMLParser

import httpx

from .config import get_settings
from .resilience import CircuitBreaker, Unavailable, call_with_retries

log = logging.getLogger("integration")
breaker = CircuitBreaker("confluence", failure_threshold=3, reset_after_s=60)

# The "When a check failed" table names each result the way the sidebar shows it.
ROW_FOR = {"ai_fail_identity": "identity", "ai_fail_no_plan": "no active plan",
           "ai_fail_window": "window", "ai_intake_incomplete": "incomplete"}

# Used only when Confluence cannot be read and nothing was read before. Kept short on purpose.
FALLBACK = {
    "ai_intake_complete": ["Confirm the baby's legal name with the parent.",
                           "Approve in the sidebar app. The enrollment is created as pending with the carrier."],
    "ai_intake_incomplete": ["Run the checks in the sidebar app. Ask the parent only for what is still missing."],
    "ai_fail_identity": ["Do not share any account detail until the requester is verified."],
    "ai_fail_no_plan": ["Check whether the employee waived coverage or has an enrollment in progress."],
    "ai_fail_window": ["Do not approve. Tell Benefits Escalation the same day."],
}


class _Page(HTMLParser):
    """Reads Confluence storage format (XHTML) into sections: ordered-list steps and table rows, with links."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.sections: dict[str, dict] = {}
        self._section, self._heading = None, None
        self._li_depth, self._item, self._row, self._cell = 0, None, None, None
        self._href = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("h1", "h2", "h3"):
            self._heading = ""
        elif tag == "li" and self._section is not None:
            self._li_depth += 1
            if self._li_depth == 1:
                self._item = {"text": "", "links": []}
        elif tag == "tr" and self._section is not None:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = {"text": "", "links": []}
        elif tag == "a":
            self._href = a.get("href")
            target = self._cell if self._cell is not None else self._item
            if target is not None and self._href:
                target["links"].append({"url": self._href, "label": ""})

    def handle_endtag(self, tag):
        if tag in ("h1", "h2", "h3") and self._heading is not None:
            self._section = " ".join(self._heading.split())
            self.sections[self._section] = {"steps": [], "rows": []}
            self._heading = None
        elif tag == "li" and self._li_depth:
            self._li_depth -= 1
            if self._li_depth == 0 and self._item is not None:
                self._item["text"] = " ".join(self._item["text"].split())
                self.sections[self._section]["steps"].append(self._item)
                self._item = None
        elif tag in ("td", "th") and self._cell is not None:
            self._cell["text"] = " ".join(self._cell["text"].split())
            self._row.append(self._cell)
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.sections[self._section]["rows"].append(self._row)
            self._row = None
        elif tag == "a":
            self._href = None

    def handle_data(self, data):
        if self._heading is not None:
            self._heading += data
            return
        for target in (self._cell, self._item):
            if target is not None:
                target["text"] += data
                if self._href and target["links"]:
                    target["links"][-1]["label"] += data
                return


def _step(text: str, links: list[dict]) -> dict:
    step = {"text": text}
    for link in links:
        if "tango" in link["url"]:
            step["how_to"] = {"url": link["url"], "label": "Show me how (Tango)"}
            break
    return step


def guidance_from(storage_html: str, situation: str) -> list[dict]:
    """The steps for one situation, taken from the procedure page as it is written."""
    page = _Page()
    page.feed(storage_html)
    secs = page.sections
    if situation == "ai_intake_complete":
        steps = next((v["steps"] for k, v in secs.items() if k.lower() == "steps"), [])
        return [_step(s["text"], s["links"]) for s in steps if s["text"]]
    key = ROW_FOR.get(situation)
    table = next((v["rows"] for k, v in secs.items() if "check failed" in k.lower()), [])
    for row in table[1:] if table else []:
        if key and key in row[0]["text"].lower():
            what_to_do = row[-1]
            sentences = [s.strip() for s in what_to_do["text"].replace("? ", "?|").replace(". ", ".|").split("|")]
            return [_step(s, what_to_do["links"]) for s in sentences if s]
    return []


class ConfluenceClient:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None):
        s = get_settings()
        self.base = s.confluence_base_url.rstrip("/")
        # One procedure page per life event: {"newborn": "524289", "marriage": "...", ...}. Missing → the default page.
        self.pages = {k: str(v) for k, v in json.loads(s.confluence_page_ids or "{}").items()}
        self.page_id = s.confluence_page_id
        self.configured = bool(self.base and (self.page_id or self.pages) and s.confluence_email and s.confluence_api_token)
        self._client = httpx.AsyncClient(timeout=s.confluence_timeout_s, transport=transport,
                                         auth=(s.confluence_email, s.confluence_api_token),
                                         headers={"Accept": "application/json"})
        self._cache: dict[str, dict] = {}
        self._read_at: dict[str, float] = {}

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _read_page(self, page_id: str) -> dict:
        r = await call_with_retries(lambda: self._client.get(
            f"{self.base}/wiki/api/v2/pages/{page_id}", params={"body-format": "storage"}), breaker, attempts=2)
        if r.status_code >= 400:
            raise Unavailable(f"confluence answered {r.status_code}")
        data = r.json()
        return {"title": data["title"], "version": data["version"]["number"],
                "updated_at": data["version"].get("createdAt"),
                "url": f"{self.base}/wiki{data['_links']['webui']}" if data.get("_links", {}).get("webui") else None,
                "body": data["body"]["storage"]["value"]}

    async def guidance(self, situation: str, event_type: str | None = None) -> dict:
        ttl = get_settings().confluence_cache_s
        page_id = self.pages.get(event_type or "", self.page_id)
        cached = self._cache.get(page_id)
        source = "confluence"
        if self.configured and page_id and (cached is None or time.time() - self._read_at.get(page_id, 0) > ttl):
            try:
                cached = self._cache[page_id] = await self._read_page(page_id)
                self._read_at[page_id] = time.time()
            except (Unavailable, httpx.HTTPError, KeyError) as exc:
                log.warning("confluence unavailable", extra={"extra_fields": {"error": repr(exc)[:200]}})
                source = "confluence_cached"
        if cached is None:
            return {"situation": situation, "source": "built_in", "page": None,
                    "steps": [{"text": t} for t in FALLBACK.get(situation, [])]}
        page = {k: cached[k] for k in ("title", "version", "updated_at", "url")}
        return {"situation": situation, "source": source, "page": page,
                "steps": guidance_from(cached["body"], situation)}
