"""Keeps the Zendesk sidebar app's settings in step with this service.

The sidebar app calls this service with its own key (scope: checks, employee summary, approval).
Railway generates that key; on startup this service writes it, with the field IDs and its own URL,
into the app installation through the Zendesk Apps API. Nobody copies a secret by hand, and rotating
the key in Railway updates Zendesk on the next deploy. The key travels only between the two servers:
Zendesk stores it as a secure setting and never sends it to the browser.
"""
import logging
import os

from .config import get_settings
from .zendesk_client import ZendeskClient

log = logging.getLogger("integration")
SIDEBAR_CLIENT = "zendesk-sidebar-app"


def desired_settings() -> dict | None:
    s = get_settings()
    key = next((k for k, v in s.keys().items() if v.get("client") == SIDEBAR_CLIENT), None)
    base = s.public_base_url or (f"https://{os.environ['RAILWAY_PUBLIC_DOMAIN']}"
                                 if os.environ.get("RAILWAY_PUBLIC_DOMAIN") else "")
    ids = s.field_ids()
    needed = ["life_event_type", "ai_intake_result", "event_date", "enrollment_outcome", "enrollment_id"]
    if not (key and base and all(n in ids for n in needed)):
        return None
    return {"integration_base_url": base.rstrip("/"), "integration_api_key": key,
            **{f"field_{n}": ids[n] for n in needed}}


async def sync_sidebar_app(zd: ZendeskClient) -> str:
    """Returns what happened, for the startup log. Never raises: the service must start regardless."""
    installation = get_settings().zendesk_app_installation_id
    if not installation:
        return "skipped: no installation id"
    settings = desired_settings()
    if settings is None:
        return "skipped: sidebar key, public URL or field ids missing"
    try:
        await zd.update_app_settings(installation, settings)
        return "updated"
    except Exception as exc:  # noqa: BLE001 - logged; the checks and webhooks still work
        return f"failed: {type(exc).__name__}"
