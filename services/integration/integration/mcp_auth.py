"""Sign-in for the MCP server: OAuth 2.1 with PKCE, dynamic client registration and a consent page.

This is how an AI assistant (Claude, ChatGPT, Copilot) connects to a remote MCP server: the person adding
the connector is sent to a Northwind consent page, approves read-only access, and the assistant receives
a short-lived token. Tokens expire after an hour, can be refreshed, and can be revoked.

Demo scope: the consent page is protected by an administrator passcode, and clients and tokens are kept in
memory (a redeploy means connecting again). In production the sign-in would be delegated to the company's
identity provider (Okta, Microsoft Entra ID) and tokens kept in a database; the MCP side stays the same.
"""
import hmac
import html
import logging
import secrets
import time

from mcp.server.auth.provider import (AccessToken, AuthorizationCode, AuthorizationParams, RefreshToken,
                                      construct_redirect_uri)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from .config import get_settings

log = logging.getLogger("integration")
SCOPE = "mcp:read"
ACCESS_TTL, REFRESH_TTL, CODE_TTL, CONSENT_TTL = 3600, 30 * 86400, 300, 600


class NorthwindAuth:
    """OAuthAuthorizationServerProvider for the MCP server."""

    def __init__(self):
        self.clients: dict[str, OAuthClientInformationFull] = {}
        self.pending: dict[str, tuple[OAuthClientInformationFull, AuthorizationParams, float]] = {}
        self.codes: dict[str, AuthorizationCode] = {}
        self.access: dict[str, AccessToken] = {}
        self.refresh: dict[str, RefreshToken] = {}

    # --- clients (RFC 7591 dynamic registration) ---
    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self.clients.get(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        self.clients[client_info.client_id] = client_info

    # --- authorization: send the person to the consent page ---
    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        req = secrets.token_urlsafe(24)
        self.pending[req] = (client, params, time.time() + CONSENT_TTL)
        return f"{get_settings().mcp_issuer_url()}/oauth/consent?req={req}"

    def approve(self, req: str, approver: str) -> str:
        client, params, expires = self.pending.pop(req)
        if time.time() > expires:
            raise KeyError("expired")
        code = secrets.token_urlsafe(32)
        self.codes[code] = AuthorizationCode(
            code=code, scopes=[SCOPE], expires_at=time.time() + CODE_TTL, client_id=client.client_id,
            code_challenge=params.code_challenge, redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly, resource=params.resource,
            subject=approver)
        return construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state)

    def deny(self, req: str) -> str:
        client, params, _ = self.pending.pop(req)
        return construct_redirect_uri(str(params.redirect_uri), error="access_denied", state=params.state)

    async def load_authorization_code(self, client: OAuthClientInformationFull, authorization_code: str):
        code = self.codes.get(authorization_code)
        if code and code.client_id == client.client_id and code.expires_at > time.time():
            return code
        return None

    def _issue(self, client_id: str, scopes: list[str], resource: str | None, subject: str | None) -> OAuthToken:
        at, rt = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        resource = resource or f"{get_settings().mcp_issuer_url()}/mcp"   # tokens are for this MCP server only
        now = int(time.time())
        self.access[at] = AccessToken(token=at, client_id=client_id, scopes=scopes, expires_at=now + ACCESS_TTL,
                                      resource=resource, subject=subject)
        self.refresh[rt] = RefreshToken(token=rt, client_id=client_id, scopes=scopes, expires_at=now + REFRESH_TTL,
                                        resource=resource, subject=subject)
        return OAuthToken(access_token=at, expires_in=ACCESS_TTL, scope=" ".join(scopes), refresh_token=rt)

    async def exchange_authorization_code(self, client, authorization_code: AuthorizationCode) -> OAuthToken:
        self.codes.pop(authorization_code.code, None)   # a code works once
        return self._issue(client.client_id, authorization_code.scopes, authorization_code.resource,
                           authorization_code.subject)

    async def load_refresh_token(self, client, refresh_token: str):
        rt = self.refresh.get(refresh_token)
        if rt and rt.client_id == client.client_id and (rt.expires_at or 0) > time.time():
            return rt
        return None

    async def exchange_refresh_token(self, client, refresh_token: RefreshToken, scopes: list[str]) -> OAuthToken:
        self.refresh.pop(refresh_token.token, None)      # rotate refresh tokens
        return self._issue(client.client_id, scopes or refresh_token.scopes, refresh_token.resource,
                           refresh_token.subject)

    # --- tokens presented to /mcp ---
    async def load_access_token(self, token: str) -> AccessToken | None:
        s = get_settings()
        # A static key for machine clients (a voice platform, a vendor agent), checked like any other key
        if len(s.mcp_api_key) >= s.min_key_length and hmac.compare_digest(token, s.mcp_api_key):
            return AccessToken(token=token, client_id="mcp-api-key", scopes=[SCOPE], resource=f"{s.mcp_issuer_url()}/mcp")
        for key, info in s.keys().items():
            if SCOPE in info.get("scopes", []) and hmac.compare_digest(token, key):
                return AccessToken(token=token, client_id=info.get("client", "unknown"), scopes=[SCOPE],
                                   resource=f"{s.mcp_issuer_url()}/mcp")
        at = self.access.get(token)
        if at and (at.expires_at or 0) > time.time():
            return at
        return None

    async def revoke_token(self, token) -> None:
        self.access.pop(token.token, None)
        self.refresh.pop(token.token, None)

    async def exchange_identity_assertion(self, *args, **kwargs):
        raise NotImplementedError


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Connect an AI assistant · Northwind PEO</title><style>
body{{margin:0;font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;background:#f6f5fa;color:#1f1b2e}}
.card{{max-width:460px;margin:8vh auto;background:#fff;border:1px solid #e6e2ef;border-radius:14px;padding:28px}}
.logo{{display:flex;gap:10px;align-items:center;font-weight:700;color:#4b2a86;margin-bottom:18px}}
.logo i{{width:28px;height:28px;border-radius:8px;background:linear-gradient(135deg,#4b2a86,#6d45b8)}}
h1{{font-size:20px;margin:0 0 6px}} p{{color:#635d78;margin:0 0 14px}} ul{{margin:0 0 18px;padding-left:18px}}
li{{margin:4px 0}} label{{font-size:13px;font-weight:600}} input{{width:100%;box-sizing:border-box;padding:10px;
border:1px solid #d9d3e6;border-radius:8px;margin:6px 0 16px;font:inherit}} .row{{display:flex;gap:10px}}
button{{flex:1;padding:11px;border-radius:8px;border:1px solid #d9d3e6;background:#fff;font:inherit;font-weight:600;cursor:pointer}}
button.ok{{background:#4b2a86;color:#fff;border-color:#4b2a86}} .err{{color:#a23a2a;font-size:13px;margin:-8px 0 12px}}
.muted{{font-size:12px;color:#8a849c;margin-top:16px}}</style></head><body><form class="card" method="post">
<div class="logo"><i></i>Northwind PEO</div>
<h1>Connect {client} to Northwind support tools</h1>
<p>This assistant is asking for <b>read-only</b> access. It will be able to:</p>
<ul><li>Look up who a person is at Northwind's client companies</li><li>Read health plan details</li>
<li>Run life-event eligibility checks</li><li>Find invoices paid twice</li><li>Read the Benefits Ops procedures</li></ul>
<p>It will <b>not</b> be able to approve enrollments, issue refunds or change anything. Every request is logged.</p>
<input type="hidden" name="req" value="{req}"><label for="p">Administrator passcode</label>
<input id="p" name="passcode" type="password" autocomplete="off" autofocus>{error}
<div class="row"><button class="ok" name="action" value="approve" style="order:2">Approve</button><button name="action" value="deny" formnovalidate style="order:1">Deny</button></div>
<div class="muted">Access expires after an hour and renews while the connector is in use. An administrator can revoke it at any time.</div>
</form></body></html>"""


def consent_route(provider: NorthwindAuth):
    async def consent(request: Request) -> Response:
        if request.method == "GET":
            req = request.query_params.get("req", "")
            error = ""
        else:
            form = await request.form()
            req = str(form.get("req", ""))
            if req not in provider.pending:
                return HTMLResponse("This sign-in request has expired. Start again from your AI assistant.", 400)
            if form.get("action") == "deny":
                log.info("mcp consent", extra={"extra_fields": {"result": "denied"}})
                return RedirectResponse(provider.deny(req), 302)
            passcode = get_settings().mcp_consent_passcode
            if passcode and hmac.compare_digest(str(form.get("passcode", "")), passcode):
                try:
                    log.info("mcp consent", extra={"extra_fields": {"result": "approved"}})
                    return RedirectResponse(provider.approve(req, "northwind-admin"), 302)
                except KeyError:
                    return HTMLResponse("This sign-in request has expired. Start again from your AI assistant.", 400)
            log.info("mcp consent", extra={"extra_fields": {"result": "wrong passcode"}})
            error = '<div class="err">That passcode is not correct.</div>'
        pending = provider.pending.get(req)
        if pending is None:
            return HTMLResponse("This sign-in request has expired. Start again from your AI assistant.", 400)
        name = html.escape(pending[0].client_name or "an AI assistant")
        return HTMLResponse(PAGE.format(client=name, req=html.escape(req), error=error))
    return consent
