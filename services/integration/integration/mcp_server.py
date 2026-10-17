"""The integration layer as an MCP server: the same governed tools, for any AI agent.

MCP (Model Context Protocol) is the open standard AI assistants use to discover and call tools. Exposing
the integration layer over MCP means Claude, ChatGPT, a voice platform or a vendor agent such as Decagon
can all use the same checks, without a custom integration for each one.

Nothing new is opened up:
- every call needs a key, checked like any other caller of this service, with the scope mcp:read;
- the tools are read-only (lookups and checks); approving an enrollment stays with a person;
- every call is logged with the caller, the tool and a request number that also reaches the core
  platform's audit log, so an AI's question can be traced like a specialist's.

Transport: Streamable HTTP at /mcp. Sign-in: OAuth 2.1 (mcp_auth.py), or a static key for machine clients.
"""
import logging
import uuid
from datetime import date

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

from .checks import CheckRequest, Contact, run_qle_check
from .config import get_settings
from .context import request_context
from .mcp_auth import NorthwindAuth, consent_route
from .resilience import Unavailable
from .workflows import deadline_text

log = logging.getLogger("integration")
SCOPE = "mcp:read"
READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
INSTRUCTIONS = ("Tools for Northwind PEO, a professional employer organization (payroll, benefits and HR "
                "for small businesses). All tools are read-only. Identify people by the email or phone on "
                "file. Never present a check result as an approval: enrollments are approved by Benefits Ops.")
STATE: dict = {}   # set at startup: core client, Confluence client, the MCP server and its app
AUTH = NorthwindAuth()


def _contact(email: str | None, phone: str | None) -> Contact:
    if not email and not phone:
        raise ValueError("give the person's email or phone number")
    return Contact(email=email.strip().lower() if email else None, phone=phone)


def _rid(tool: str) -> str:
    rid = f"mcp-{uuid.uuid4().hex[:12]}"
    token = get_access_token()
    caller = token.client_id if token else "unknown"
    log.info("mcp tool", extra={"extra_fields": {"request_id": rid, "caller": caller, "tool": tool}})
    return rid


async def find_person(email: str | None = None, phone: str | None = None) -> dict:
    """Who is this person at Northwind's client companies? Returns whether they are verified (on file with
    active employment), their name, and each company with their role. No plan details."""
    ctx = await request_context(STATE["core"], _contact(email, phone), "identity", _rid("find_person"))
    return {k: ctx[k] for k in ("verified", "name", "memberships", "notes")}


async def get_health_plan(email: str | None = None, phone: str | None = None) -> dict:
    """The person's medical plan for each company: plan, carrier, coverage level, status, deductible and
    out-of-pocket maximum for the plan year. How much has been used lives with the carrier, not here."""
    return await request_context(STATE["core"], _contact(email, phone), "benefits_question", _rid("get_health_plan"))


async def check_life_event_eligibility(event_type: str, event_date: str, email: str | None = None,
                                       phone: str | None = None, company_id: str | None = None) -> dict:
    """Run the three eligibility checks for a qualifying life event: identity, active medical plan, and the
    30-day enrollment window in the company's time zone. event_type: newborn, adoption, marriage or
    loss_of_coverage. event_date: YYYY-MM-DD. If the person works for several companies, the result asks
    which one; call again with company_id. This is a check, not an approval."""
    if event_type not in ("newborn", "adoption", "marriage", "loss_of_coverage"):
        raise ValueError("event_type must be newborn, adoption, marriage or loss_of_coverage")
    req = CheckRequest(requester=_contact(email, phone), event_type=event_type,
                       event_date=date.fromisoformat(event_date), company_id=company_id)
    try:
        result = (await run_qle_check(STATE["core"], req, _rid("check_life_event_eligibility"))).to_dict()
    except Unavailable as exc:
        return {"ai_intake_result": "ai_intake_incomplete", "error": f"a system did not answer: {exc}"}
    if result.get("last_day"):
        result["deadline_text"] = deadline_text(result["last_day"])
    return result


async def find_duplicate_payments(admin_email: str) -> dict:
    """For a company administrator: invoices of their companies that were paid more than once, with each
    payment. Returns nothing for someone who is not an admin. Refunds are decided by Payments, not here."""
    ctx = await request_context(STATE["core"], _contact(admin_email, None), "billing", _rid("find_duplicate_payments"))
    return {k: ctx.get(k) for k in ("verified", "name", "is_admin", "billing", "notes")}


async def get_procedure_steps(situation: str, event_type: str | None = None) -> dict:
    """The current Benefits Ops procedure steps, read live from Confluence. situation: ai_intake_complete
    (checks passed), ai_intake_incomplete, ai_fail_identity, ai_fail_no_plan or ai_fail_window."""
    _rid("get_procedure_steps")
    return await STATE["confluence"].guidance(situation, event_type)


TOOLS = [find_person, get_health_plan, check_life_event_eligibility, find_duplicate_payments, get_procedure_steps]
# Paths the MCP app serves, routed to it by the main app
PATHS = ["/mcp", "/authorize", "/token", "/register", "/revoke", "/oauth/consent",
         "/.well-known/oauth-authorization-server", "/.well-known/oauth-protected-resource",
         "/.well-known/oauth-protected-resource/mcp"]


def build_app():
    """The MCP server and its app. Built at each startup, because a session manager runs once."""
    s = get_settings()
    issuer = s.mcp_issuer_url()
    server = MCPServer(
        name="Northwind PEO support tools", instructions=INSTRUCTIONS, auth_server_provider=AUTH,
        auth=AuthSettings(issuer_url=issuer, resource_server_url=f"{issuer}/mcp", required_scopes=[SCOPE],
                          client_registration_options=ClientRegistrationOptions(
                              enabled=True, valid_scopes=[SCOPE], default_scopes=[SCOPE]),
                          revocation_options=RevocationOptions(enabled=True), validate_token_resource=True))
    for fn in TOOLS:
        server.add_tool(fn, annotations=READ_ONLY)
    server.custom_route("/oauth/consent", methods=["GET", "POST"])(consent_route(AUTH))
    host = issuer.replace("https://", "").replace("http://", "")
    security = TransportSecuritySettings(enable_dns_rebinding_protection=not host.startswith(("localhost", "testserver")),
                                         allowed_hosts=[host], allowed_origins=[])
    STATE["server"] = server
    STATE["app"] = server.streamable_http_app(streamable_http_path="/mcp", stateless_http=True,
                                              json_response=True, transport_security=security)
    return STATE["app"]


class Endpoint:
    """The routes' ASGI app (a class instance, so Starlette passes it the raw request): the MCP app built at startup."""

    async def __call__(self, scope, receive, send):
        await STATE["app"](scope, receive, send)
